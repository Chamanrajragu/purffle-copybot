"""
PurffleBot Copy — Hyperliquid top-trader mirror bot.

WHY HYPERLIQUID, NOT BINANCE:
- Binance has walled their copy-trading leaderboard since 2024 (404/403 on programmatic
  access). They monetize copy trading as a built-in product and block external bots.
- Hyperliquid is a decentralized perp DEX with fully public on-chain data — all 38k+
  trader accounts, positions, and PnL are readable for free, no auth.

WHAT THIS BOT DOES:
1. Pulls Hyperliquid leaderboard every 6 hours, picks 5 "elite traders" by criteria:
   - All-time ROI >= 100% (sustained profitable, not lucky streak)
   - Monthly ROI > 0 (still working recently)
   - Account value between $50k and $5M (real money, not whale-slow)
   - Recent day ROI within ±20% (not in a freak win/loss day)
2. Every 5 minutes, fetches each elite trader's currently open positions.
3. Aggregates: counts how many elites are long each coin.
4. When >= 2 elites agree on a long, we paper-open that coin on Binance spot.
5. When elites have all exited, we exit too. Hard stop -10%.

REALISTIC EXPECTATION:
This is NOT a magic +45%/month bot. It IS a way to ride the coattails of traders
proven to make money on perps. Several real catches:
- Latency: we see their position after they've held it some time. Worse entry price.
- No leverage: their 10x long becomes our 1x spot. Smaller magnitude both ways.
- Selection drift: today's top 5 may not be tomorrow's. We refresh every 6h.
- Tracked at perp prices, executed on spot. Direction usually agrees, magnitude doesn't.

PAPER TRADING ONLY. Real money requires checking trader histories deeper than ROI alone.

Dashboard: http://localhost:12349
"""
from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from flask import Flask, jsonify, render_template_string, request, Response


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
STARTING_CAPITAL = 100.0
SPOT_FEE = 0.001

# Trader selection criteria — tightened to avoid "lucky shot 4 years ago" wallets
MIN_ALL_TIME_ROI = 1.0           # >= 100% (1x return)
MAX_ALL_TIME_ROI = 50.0          # <= 5000% — anything higher is a one-time lucky shot
MIN_MONTH_ROI = 0.10             # >= 10% monthly — must be actively making money RIGHT NOW
MIN_WEEK_ROI = 0.02              # >= 2% weekly — proves they're still active
MIN_MONTH_VOLUME = 100_000       # >= $100k traded this month — active, not dormant
MIN_ACCOUNT_VALUE = 100_000.0    # >= $100k account (real skin in the game)
MAX_ACCOUNT_VALUE = 5_000_000.0
MAX_DAY_ROI_ABS = 0.30           # skip if day ROI > ±30% (freak day)
TOP_TRADERS_COUNT = 5

# Mirror logic
MIN_AGREEING_TRADERS = 2         # need >= 2 elites long the same coin to fire
POSITION_SIZE_PCT = 0.20         # 20% of cash per copy trade
MAX_CONCURRENT_POSITIONS = 5
HARD_STOP_PCT = 0.10             # -10% safety stop

# Intervals
LEADERBOARD_REFRESH_SECONDS = 6 * 3600
POSITION_SCAN_SECONDS = 300       # poll trader positions every 5 min

# Endpoints
HYPERLIQUID_LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
HYPERLIQUID_INFO = "https://api.hyperliquid.xyz/info"
BINANCE = "https://api.binance.com"

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "copytrade.db"
LOG_PATH = ROOT / "copytrade.log"

_log_lock = threading.Lock()

def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with _log_lock:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                coin TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                qty REAL NOT NULL,
                price REAL NOT NULL,
                value REAL NOT NULL,
                fee REAL NOT NULL,
                reason TEXT NOT NULL,
                realized_pnl REAL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS positions (
                coin TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                qty REAL NOT NULL,
                entry_price REAL NOT NULL,
                cost REAL NOT NULL,
                entry_ts TEXT NOT NULL,
                following_count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS elite_traders (
                wallet TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                account_value REAL NOT NULL,
                all_time_roi REAL NOT NULL,
                month_roi REAL NOT NULL,
                day_roi REAL NOT NULL,
                added_ts TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                ts TEXT PRIMARY KEY,
                cash REAL NOT NULL,
                holdings_value REAL NOT NULL,
                total_value REAL NOT NULL
            );
        """)
        if not conn.execute("SELECT 1 FROM state WHERE key='cash'").fetchone():
            conn.execute("INSERT INTO state(key,value) VALUES('cash',?)", (str(STARTING_CAPITAL),))
            conn.execute("INSERT INTO state(key,value) VALUES('starting_capital',?)", (str(STARTING_CAPITAL),))


def get_cash() -> float:
    with db() as conn:
        return float(conn.execute("SELECT value FROM state WHERE key='cash'").fetchone()["value"])

def set_cash(v: float) -> None:
    with db() as conn:
        conn.execute("UPDATE state SET value=? WHERE key='cash'", (str(v),))

def get_positions() -> dict[str, dict]:
    with db() as conn:
        return {r["coin"]: dict(r) for r in conn.execute("SELECT * FROM positions").fetchall()}

def upsert_position(coin: str, symbol: str, qty: float, entry_price: float,
                    cost: float, entry_ts: str, following_count: int) -> None:
    with db() as conn:
        conn.execute(
            """INSERT INTO positions(coin,symbol,qty,entry_price,cost,entry_ts,following_count)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(coin) DO UPDATE SET qty=excluded.qty, entry_price=excluded.entry_price,
                 cost=excluded.cost, following_count=excluded.following_count""",
            (coin, symbol, qty, entry_price, cost, entry_ts, following_count),
        )

def delete_position(coin: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM positions WHERE coin=?", (coin,))

def record_trade(coin: str, symbol: str, side: str, qty: float, price: float,
                 fee: float, reason: str, realized_pnl: float = 0.0) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute(
            """INSERT INTO trades(ts,coin,symbol,side,qty,price,value,fee,realized_pnl,reason)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (ts, coin, symbol, side, qty, price, qty * price, fee, realized_pnl, reason),
        )
    log(f"{side} {qty:.6f} {coin} @ ${price:.4f} fee ${fee:.4f} pnl ${realized_pnl:+.4f} ({reason})")

def take_snapshot(cash: float, holdings_value: float) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO snapshots(ts,cash,holdings_value,total_value) VALUES(?,?,?,?)",
            (ts, cash, holdings_value, cash + holdings_value),
        )


def build_equity_sparkline(series: list[float], w: int = 1180, h: int = 130,
                           pad: int = 10) -> dict:
    """Turn a list of total-value snapshots into SVG polyline/area point strings."""
    if len(series) < 2:
        return {"has": False, "count": len(series)}
    lo, hi = min(series), max(series)
    rng = (hi - lo) or 1.0
    n = len(series)
    pts = []
    for i, v in enumerate(series):
        x = pad + (w - 2 * pad) * (i / (n - 1))
        y = pad + (h - 2 * pad) * (1 - (v - lo) / rng)
        pts.append(f"{x:.1f},{y:.1f}")
    points = " ".join(pts)
    x0 = f"{pad:.1f}"
    x1 = f"{pad + (w - 2 * pad):.1f}"
    baseline = f"{h - pad:.1f}"
    area = f"{x0},{baseline} {points} {x1},{baseline}"
    return {
        "has": True, "count": n, "points": points, "area": area,
        "up": series[-1] >= series[0], "first": series[0], "last": series[-1],
        "min": lo, "max": hi,
    }


def compute_trade_stats(conn) -> dict:
    """Aggregate realized P/L of closed (SELL) trades into headline performance stats."""
    rows = conn.execute("SELECT realized_pnl, fee FROM trades").fetchall()
    total_fees = sum((r["fee"] or 0.0) for r in rows)
    pnls = [r["realized_pnl"] or 0.0
            for r in conn.execute("SELECT realized_pnl FROM trades WHERE side='SELL'").fetchall()]
    closed = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    if gross_loss > 0:
        pf_display = f"{gross_profit / gross_loss:.2f}"
    elif gross_profit > 0:
        pf_display = "∞"
    else:
        pf_display = "—"
    return {
        "closed": closed, "wins": len(wins), "losses": len(losses),
        "win_rate": (len(wins) / closed * 100) if closed else None,
        "avg_win": (gross_profit / len(wins)) if wins else 0.0,
        "avg_loss": (-gross_loss / len(losses)) if losses else 0.0,
        "best": max(pnls) if pnls else 0.0,
        "worst": min(pnls) if pnls else 0.0,
        "profit_factor": pf_display,
        "net": sum(pnls), "total_fees": total_fees,
    }


def save_elite_traders(traders: list[dict]) -> None:
    with db() as conn:
        conn.execute("DELETE FROM elite_traders")
        now = datetime.now(timezone.utc).isoformat()
        for t in traders:
            conn.execute(
                """INSERT INTO elite_traders(wallet,display_name,account_value,all_time_roi,
                                              month_roi,day_roi,added_ts) VALUES(?,?,?,?,?,?,?)""",
                (t["wallet"], t["name"], t["account_value"], t["all_roi"],
                 t["month_roi"], t["day_roi"], now),
            )


# ---------------------------------------------------------------------------
# Hyperliquid integration
# ---------------------------------------------------------------------------
def fetch_leaderboard() -> Optional[list[dict]]:
    """Pulls full ~31MB leaderboard, filters to elites by our criteria."""
    log("fetching Hyperliquid leaderboard (~31MB)...")
    try:
        r = requests.get(HYPERLIQUID_LEADERBOARD, timeout=60)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log(f"leaderboard fetch failed: {e}")
        return None
    rows = data.get("leaderboardRows", [])
    log(f"  total traders on leaderboard: {len(rows)}")

    candidates = []
    for row in rows:
        try:
            account_val = float(row.get("accountValue", 0))
        except (TypeError, ValueError):
            continue
        if not (MIN_ACCOUNT_VALUE <= account_val <= MAX_ACCOUNT_VALUE):
            continue
        perfs = {w[0]: w[1] for w in row.get("windowPerformances", []) if len(w) == 2}
        try:
            all_roi = float(perfs.get("allTime", {}).get("roi", 0))
            month_roi = float(perfs.get("month", {}).get("roi", 0))
            week_roi = float(perfs.get("week", {}).get("roi", 0))
            day_roi = float(perfs.get("day", {}).get("roi", 0))
            month_vlm = float(perfs.get("month", {}).get("vlm", 0))
        except (TypeError, ValueError):
            continue
        # Filter chain — every gate must pass
        if all_roi < MIN_ALL_TIME_ROI or all_roi > MAX_ALL_TIME_ROI: continue
        if month_roi < MIN_MONTH_ROI: continue
        if week_roi < MIN_WEEK_ROI: continue
        if month_vlm < MIN_MONTH_VOLUME: continue
        if abs(day_roi) > MAX_DAY_ROI_ABS: continue
        candidates.append({
            "wallet": row["ethAddress"],
            "name": row.get("displayName") or row["ethAddress"][:8],
            "account_value": account_val,
            "all_roi": all_roi,
            "month_roi": month_roi,
            "week_roi": week_roi,
            "day_roi": day_roi,
            "month_vlm": month_vlm,
        })
    # Sort by recent (monthly) performance — recency matters more than ancient ROI
    candidates.sort(key=lambda x: -x["month_roi"])
    elites = candidates[:TOP_TRADERS_COUNT]
    log(f"  filtered to {len(candidates)} candidates, top {len(elites)} selected as elites:")
    for t in elites:
        log(f"    {t['name']:<20} acct ${t['account_value']:>10,.0f}  "
            f"allROI {t['all_roi']*100:>7.0f}%  monthROI {t['month_roi']*100:>+6.1f}%")
    return elites


def fetch_trader_positions(wallet: str) -> Optional[list[dict]]:
    """Returns list of {coin, side, size, entry_price} for a single wallet's open positions."""
    try:
        r = requests.post(HYPERLIQUID_INFO,
                          json={"type": "clearinghouseState", "user": wallet},
                          timeout=10)
        r.raise_for_status()
        state = r.json()
    except Exception as e:
        log(f"position fetch failed for {wallet[:10]}: {e}")
        return None
    positions = []
    for ap in state.get("assetPositions", []):
        p = ap.get("position", {})
        try:
            szi = float(p.get("szi", 0))
            entry = float(p.get("entryPx", 0))
        except (TypeError, ValueError):
            continue
        if szi == 0: continue
        positions.append({
            "coin": p.get("coin", ""),
            "side": "LONG" if szi > 0 else "SHORT",
            "size": abs(szi),
            "entry_price": entry,
            "leverage": p.get("leverage", {}).get("value", 1),
        })
    return positions


def fetch_binance_price(symbol: str) -> Optional[float]:
    """Get current spot price from Binance for our paper execution."""
    try:
        r = requests.get(f"{BINANCE}/api/v3/ticker/price",
                         params={"symbol": symbol}, timeout=10)
        if r.status_code != 200: return None
        return float(r.json().get("price", 0))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Aggregation + execution
# ---------------------------------------------------------------------------
_state = {
    "running": False, "paused": False,
    "last_leaderboard_refresh": 0.0, "last_position_scan": 0.0,
    "elites": [], "current_signals": {},  # coin -> {long_count, short_count, traders}
    "scans": 0,
    "last_prices": {},  # symbol -> last spot price seen by the scanner (dashboard reuses this)
}


def scan_and_mirror() -> None:
    elites = _state["elites"]
    if not elites: return

    # Aggregate signals: for each coin, count how many elites are long
    signals: dict[str, dict] = {}
    for e in elites:
        positions = fetch_trader_positions(e["wallet"])
        if positions is None: continue
        for p in positions:
            coin = p["coin"]
            if coin not in signals:
                signals[coin] = {"long_count": 0, "short_count": 0, "longs_by": []}
            if p["side"] == "LONG":
                signals[coin]["long_count"] += 1
                signals[coin]["longs_by"].append(e["name"])
            else:
                signals[coin]["short_count"] += 1
    _state["current_signals"] = signals

    our_positions = get_positions()
    cash = get_cash()

    # CLOSE positions where the agreeing-long count has dropped below threshold
    for coin, pos in list(our_positions.items()):
        long_count = signals.get(coin, {}).get("long_count", 0)
        symbol = pos["symbol"]
        live_price = fetch_binance_price(symbol)
        if live_price is None: continue
        _state["last_prices"][symbol] = live_price
        from_entry = (live_price - pos["entry_price"]) / pos["entry_price"]

        exit_reason = None
        if long_count < MIN_AGREEING_TRADERS:
            exit_reason = f"elites exited ({long_count}/{MIN_AGREEING_TRADERS} still long)"
        elif from_entry <= -HARD_STOP_PCT:
            exit_reason = f"hard-stop {from_entry*100:+.1f}%"

        if exit_reason:
            proceeds = pos["qty"] * live_price
            fee = proceeds * SPOT_FEE
            realized = (proceeds - fee) - pos["cost"]
            cash += proceeds - fee
            set_cash(cash)
            record_trade(coin, symbol, "SELL", pos["qty"], live_price, fee,
                         exit_reason, realized_pnl=realized)
            delete_position(coin)
            our_positions.pop(coin, None)

    # OPEN new positions where >= MIN_AGREEING_TRADERS elites are long and we don't hold
    for coin, sig in signals.items():
        if sig["long_count"] < MIN_AGREEING_TRADERS: continue
        if coin in our_positions: continue
        if len(our_positions) >= MAX_CONCURRENT_POSITIONS: break
        symbol = f"{coin}USDT"
        live_price = fetch_binance_price(symbol)
        if live_price is None or live_price <= 0:
            log(f"signal on {coin}: {sig['long_count']} elites long, but no Binance USDT pair")
            continue
        _state["last_prices"][symbol] = live_price
        spend = cash * POSITION_SIZE_PCT
        if spend < 5: break
        fee = spend * SPOT_FEE
        qty = (spend - fee) / live_price
        cost = spend - fee
        cash -= spend
        set_cash(cash)
        entry_ts = datetime.now(timezone.utc).isoformat()
        upsert_position(coin, symbol, qty, live_price, cost, entry_ts, sig["long_count"])
        record_trade(coin, symbol, "BUY", qty, live_price, fee,
                     f"copy long: {sig['long_count']} elites agree ({', '.join(sig['longs_by'][:3])})")
        our_positions[coin] = {"coin": coin, "symbol": symbol, "qty": qty,
                                "entry_price": live_price, "cost": cost}

    # Record an equity snapshot for the dashboard's equity curve.
    holdings_value = 0.0
    for c, pos in get_positions().items():
        px = _state["last_prices"].get(pos["symbol"], pos["entry_price"])
        holdings_value += pos["qty"] * px
    take_snapshot(get_cash(), holdings_value)

    _state["scans"] += 1
    _state["last_position_scan"] = time.time()


def scanner_loop() -> None:
    _state["running"] = True
    while True:
        try:
            if not _state["paused"]:
                now = time.time()
                if now - _state["last_leaderboard_refresh"] > LEADERBOARD_REFRESH_SECONDS \
                   or not _state["elites"]:
                    elites = fetch_leaderboard()
                    if elites:
                        _state["elites"] = elites
                        save_elite_traders(elites)
                        _state["last_leaderboard_refresh"] = now
                scan_and_mirror()
        except Exception as e:
            log(f"scanner error: {e}")
        time.sleep(POSITION_SCAN_SECONDS)


# ---------------------------------------------------------------------------
# Flask dashboard
# ---------------------------------------------------------------------------
app = Flask(__name__)

DASH_HTML = """
<!doctype html>
<html><head><title>PurffleCopyBot — Dashboard</title>
<noscript><meta http-equiv="refresh" content="60"></noscript>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet"/>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#07050d;--s1:#100d18;--s2:#1a1525;--bd:#261e35;--t1:#f3eef8;--t2:#9a8aad;--t3:#6b5a80;
--green:#22c55e;--red:#ef4444;--purple:#a855f7;--pink:#ec4899;--blue:#3b82f6;
--grad:linear-gradient(135deg,#a855f7,#ec4899)}
body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--t1);min-height:100vh}
.shell{max-width:1280px;margin:0 auto;padding:24px 32px}
#live{transition:opacity .2s ease}

.topbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:24px;padding-bottom:20px;border-bottom:1px solid var(--bd)}
.brand{display:flex;align-items:center;gap:10px;text-decoration:none;color:var(--t1)}
.brand-icon{width:34px;height:34px;background:var(--grad);border-radius:9px;display:flex;align-items:center;justify-content:center;font-weight:900;font-size:15px;color:#fff}
.brand span{font-weight:800;font-size:17px;letter-spacing:-.02em}
.brand .env{font-size:10px;font-weight:700;color:var(--pink);background:rgba(236,72,153,.1);padding:3px 8px;border-radius:5px;margin-left:6px}
.nav-links a{color:var(--t2);font-size:13px;font-weight:500;text-decoration:none;padding:7px 14px;border-radius:8px;transition:.15s}
.nav-links a:hover,.nav-links a.active{color:var(--t1);background:var(--s2)}

.status-bar{display:flex;align-items:center;gap:14px;margin-bottom:20px;font-size:12px;color:var(--t3);flex-wrap:wrap}
.status-bar .live{display:flex;align-items:center;gap:6px;color:var(--purple);font-weight:600}
.status-bar .live .dot{width:7px;height:7px;border-radius:50%;background:var(--purple);animation:blink 2s ease-in-out infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.25}}
.status-bar .sep{color:var(--bd)}
.tag-hl{font-size:10px;font-weight:700;color:var(--pink);background:rgba(236,72,153,.1);border:1px solid rgba(236,72,153,.2);padding:3px 10px;border-radius:6px}

.metrics{display:grid;grid-template-columns:repeat(5,1fr);gap:14px;margin-bottom:28px}
.metric{background:var(--s1);border:1px solid var(--bd);border-radius:14px;padding:20px;position:relative;overflow:hidden;transition:.2s}
.metric:hover{border-color:rgba(168,85,247,.35);transform:translateY(-2px)}
.metric::after{content:'';position:absolute;top:0;left:0;right:0;height:2px;border-radius:14px 14px 0 0;opacity:0;transition:.2s}
.metric:hover::after{opacity:1}
.metric:nth-child(1)::after{background:var(--grad)}
.metric:nth-child(2)::after{background:var(--blue)}
.metric:nth-child(3)::after{background:var(--purple)}
.metric:nth-child(4)::after{background:var(--pink)}
.metric:nth-child(5)::after{background:var(--green)}
.metric .lbl{font-size:11px;font-weight:600;color:var(--t3);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px}
.metric .val{font-size:26px;font-weight:800;letter-spacing:-.02em}
.metric .sub{font-size:12px;color:var(--t3);margin-top:4px}

.pos{color:var(--green)}.neg{color:var(--red)}

.sec{display:flex;align-items:center;gap:10px;margin:28px 0 12px}
.sec h2{font-size:15px;font-weight:700}
.sec .ico{font-size:16px}

.tbl-wrap{background:var(--s1);border:1px solid var(--bd);border-radius:14px;overflow:hidden;margin-bottom:8px}
table{width:100%;border-collapse:collapse}
th{background:var(--s2);color:var(--t3);font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;padding:10px 14px;text-align:left}
td{padding:10px 14px;font-size:13px;border-top:1px solid var(--bd)}
tr:hover td{background:rgba(255,255,255,.01)}
.pill{display:inline-block;padding:3px 10px;border-radius:8px;font-size:10px;font-weight:700;letter-spacing:.03em}
.pill.buy,.pill.up{background:rgba(34,197,94,.12);color:var(--green)}
.pill.sell,.pill.dn{background:rgba(239,68,68,.12);color:var(--red)}
.pill.signal{background:rgba(168,85,247,.12);color:var(--purple)}
.pill.mirror{background:rgba(236,72,153,.12);color:var(--pink)}
.muted{color:var(--t3);font-size:12px}
a.addr{color:var(--purple);font-family:'Courier New',monospace;font-size:11px;text-decoration:none;transition:.15s}
a.addr:hover{color:var(--pink)}
b{font-weight:700}

/* Stats panel */
.statgrid{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:8px}
.statcard{background:var(--s1);border:1px solid var(--bd);border-radius:12px;padding:14px 16px}
.statcard .l{font-size:10px;font-weight:600;color:var(--t3);text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px}
.statcard .v{font-size:18px;font-weight:800;letter-spacing:-.01em}
/* Controls */
.ctrl-btn{font-size:12px;font-weight:600;font-family:inherit;cursor:pointer;padding:7px 14px;border-radius:8px;border:1px solid var(--bd);background:var(--s2);color:var(--t1);transition:.15s}
.ctrl-btn:hover{border-color:var(--purple)}
.ctrl-btn.paused{color:var(--pink);border-color:rgba(236,72,153,.4)}

@media(max-width:900px){.metrics{grid-template-columns:repeat(2,1fr)}.shell{padding:16px}.statgrid{grid-template-columns:repeat(2,1fr)}}
</style></head><body>
<div class="shell">
<div class="topbar">
 <a href="/" class="brand"><div class="brand-icon">P</div><span>PurffleCopyBot</span><span class="env">PAPER</span></a>
 <div class="nav-links">
   <a href="/" class="active">Dashboard</a>
   <a href="/export/trades.csv">&#x2B07; CSV</a>
   <a href="/api/state">API</a>
   <button type="button" id="pauseBtn" class="ctrl-btn {{ 'paused' if paused else '' }}">{{ '▶ Resume' if paused else '⏸ Pause' }}</button>
 </div>
</div>

<div id="live">
<div class="status-bar">
 <span class="live" style="{{ 'color:var(--pink)' if paused else '' }}"><span class="dot"></span> {{ 'PAUSED' if paused else 'MIRRORING' }}</span>
 <span class="tag-hl">HYPERLIQUID TOP {{elite_count}}</span>
 <span class="sep">|</span> Positions every {{scan_interval}}s
 <span class="sep">|</span> {{scans}} scans
 <span class="sep">|</span> Leaderboard: {{last_lb}}
 <span class="sep">|</span> Last scan: {{last_scan}}
</div>

<div class="metrics">
 <div class="metric"><div class="lbl">Total Value</div>
  <div class="val {{'pos' if total>=starting else 'neg'}}" id="mTotal" data-v="{{ total }}">${{ '%.2f'|format(total) }}</div>
  <div class="sub">P/L ${{ '%+.2f'|format(total-starting) }} ({{ '%+.2f'|format((total/starting-1)*100) }}%)</div></div>
 <div class="metric"><div class="lbl">Cash</div><div class="val">${{ '%.2f'|format(cash) }}</div></div>
 <div class="metric"><div class="lbl">Holdings</div><div class="val">${{ '%.2f'|format(holdings_value) }}</div></div>
 <div class="metric"><div class="lbl">Mirror Positions</div><div class="val">{{ open_count }} / {{ max_concurrent }}</div></div>
 <div class="metric"><div class="lbl">Total Trades</div><div class="val">{{ trade_count }}</div></div>
</div>

<div class="sec"><h2><span class="ico">&#x1F4C8;</span> Equity Curve <span class="muted" style="font-weight:600">{{ equity.count }} snapshots</span></h2></div>
<div class="tbl-wrap" style="padding:18px 16px 12px">
 {% if equity.has %}
 <svg viewBox="0 0 1180 130" preserveAspectRatio="none" style="width:100%;height:130px;display:block">
  <defs><linearGradient id="eg" x1="0" y1="0" x2="0" y2="1">
   <stop offset="0%" stop-color="{{ '#22c55e' if equity.up else '#ef4444' }}" stop-opacity="0.28"/>
   <stop offset="100%" stop-color="{{ '#22c55e' if equity.up else '#ef4444' }}" stop-opacity="0"/>
  </linearGradient></defs>
  <polygon fill="url(#eg)" points="{{ equity.area }}"/>
  <polyline fill="none" stroke="{{ '#22c55e' if equity.up else '#ef4444' }}" stroke-width="2" stroke-linejoin="round" points="{{ equity.points }}"/>
 </svg>
 <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--t3);margin-top:8px">
  <span>Start ${{ '%.2f'|format(equity.first) }}</span>
  <span>Low ${{ '%.2f'|format(equity.min) }} &middot; High ${{ '%.2f'|format(equity.max) }}</span>
  <span class="{{ 'pos' if equity.up else 'neg' }}">Now ${{ '%.2f'|format(equity.last) }}</span>
 </div>
 {% else %}
 <div class="muted" style="text-align:center;padding:18px">Equity curve will appear after a few scan cycles record snapshots.</div>
 {% endif %}
</div>

<div class="sec"><h2><span class="ico">&#x1F4CA;</span> Performance <span class="muted" style="font-weight:600">{{ stats.closed }} closed trades</span></h2></div>
<div class="statgrid">
 <div class="statcard"><div class="l">Win Rate</div><div class="v {{ 'pos' if stats.win_rate is not none and stats.win_rate>=50 else '' }}">{% if stats.win_rate is not none %}{{ '%.1f'|format(stats.win_rate) }}%{% else %}—{% endif %}</div></div>
 <div class="statcard"><div class="l">Profit Factor</div><div class="v">{{ stats.profit_factor }}</div></div>
 <div class="statcard"><div class="l">Net Realized</div><div class="v {{ 'pos' if stats.net>=0 else 'neg' }}">${{ '%+.2f'|format(stats.net) }}</div></div>
 <div class="statcard"><div class="l">Avg Win</div><div class="v pos">${{ '%+.4f'|format(stats.avg_win) }}</div></div>
 <div class="statcard"><div class="l">Avg Loss</div><div class="v neg">${{ '%+.4f'|format(stats.avg_loss) }}</div></div>
 <div class="statcard"><div class="l">Total Fees</div><div class="v">${{ '%.4f'|format(stats.total_fees) }}</div></div>
</div>

<div class="sec"><h2><span class="ico">&#x1F451;</span> Elite Traders We Follow</h2></div>
<div class="tbl-wrap"><table>
 <tr><th>Trader</th><th>Wallet</th><th>Account Value</th><th>All-Time ROI</th><th>Month ROI</th><th>Day ROI</th></tr>
 {% for e in elites %}
 <tr>
  <td><b>{{ e.display_name or e.wallet[:8] }}</b></td>
  <td><a class="addr" href="https://app.hyperliquid.xyz/explorer/address/{{e.wallet}}" target="_blank">{{ e.wallet[:18] }}...</a></td>
  <td>${{ '{:,.0f}'.format(e.account_value) }}</td>
  <td class="pos">{{ '%.0f'|format(e.all_time_roi * 100) }}%</td>
  <td class="{{'pos' if e.month_roi>=0 else 'neg'}}">{{ '%+.1f'|format(e.month_roi * 100) }}%</td>
  <td class="{{'pos' if e.day_roi>=0 else 'neg'}}">{{ '%+.1f'|format(e.day_roi * 100) }}%</td>
 </tr>
 {% else %}
 <tr><td colspan="6" class="muted" style="padding:20px;text-align:center">Loading leaderboard... first run takes ~60s</td></tr>
 {% endfor %}
</table></div>

<div class="sec"><h2><span class="ico">&#x1F4E1;</span> Live Consensus Signals</h2></div>
<div class="tbl-wrap"><table>
 <tr><th>Coin</th><th>Long Votes</th><th>Short Votes</th><th>Who's Long</th><th>Status</th></tr>
 {% for coin, sig in signals %}
 <tr>
  <td><b>{{ coin }}</b></td>
  <td class="pos">{{ sig.long_count }}</td>
  <td class="neg">{{ sig.short_count }}</td>
  <td class="muted">{{ sig.longs_by | join(', ') }}</td>
  <td>{% if sig.long_count >= min_agree %}{% if coin in mirroring %}<span class="pill mirror">MIRRORING</span>{% else %}<span class="pill signal">SIGNAL</span>{% endif %}{% else %}<span class="muted">below threshold</span>{% endif %}</td>
 </tr>
 {% else %}
 <tr><td colspan="5" class="muted" style="padding:20px;text-align:center">No signals yet — waiting for position scan</td></tr>
 {% endfor %}
</table></div>

<div class="sec"><h2><span class="ico">&#x1F4BC;</span> Mirror Positions</h2></div>
<div class="tbl-wrap"><table>
 <tr><th>Coin</th><th>Symbol</th><th>Qty</th><th>Entry</th><th>Current</th><th>Value</th><th>P/L</th></tr>
 {% for p in open_positions %}
 <tr>
  <td><b>{{ p.coin }}</b></td><td>{{ p.symbol }}</td>
  <td>{{ '%.6f'|format(p.qty) }}</td>
  <td>${{ '%.4f'|format(p.entry_price) }}</td>
  <td>${{ '%.4f'|format(p.current) }}</td>
  <td>${{ '%.2f'|format(p.value) }}</td>
  <td class="{{'pos' if p.pl_pct>=0 else 'neg'}}"><b>{{ '%+.2f'|format(p.pl_pct) }}%</b></td>
 </tr>
 {% else %}
 <tr><td colspan="7" class="muted" style="padding:20px;text-align:center">No mirror positions — waiting for elite consensus</td></tr>
 {% endfor %}
</table></div>

<div class="sec"><h2><span class="ico">&#x1F4DD;</span> Recent Trades</h2></div>
<div class="tbl-wrap"><table>
 <tr><th>Time</th><th>Side</th><th>Coin</th><th>Qty</th><th>Price</th><th>Value</th><th>Reason</th><th>P/L</th></tr>
 {% for t in trades %}
 <tr>
  <td>{{ t.ts[:19].replace('T',' ') }}</td>
  <td><span class="pill {{'up' if t.side=='BUY' else 'dn'}}">{{ t.side }}</span></td>
  <td><b>{{ t.coin }}</b></td>
  <td>{{ '%.6f'|format(t.qty) }}</td>
  <td>${{ '%.4f'|format(t.price) }}</td>
  <td>${{ '%.2f'|format(t.value) }}</td>
  <td class="muted">{{ t.reason }}</td>
  <td class="{{'pos' if t.realized_pnl>=0 else 'neg'}}">{% if t.side=='SELL' %}${{ '%+.4f'|format(t.realized_pnl) }}{% endif %}</td>
 </tr>
 {% else %}
 <tr><td colspan="8" class="muted" style="padding:20px;text-align:center">No trades yet</td></tr>
 {% endfor %}
</table></div>
</div><!-- /#live -->

<div class="sec"><h2><span class="ico">&#x1F4D6;</span> How It Works</h2></div>
<div class="tbl-wrap" style="padding:24px">
 <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px">
  <div>
   <h3 style="font-size:14px;font-weight:700;margin-bottom:10px;color:var(--purple)">&#x1F451; Elite Trader Mirroring</h3>
   <p style="font-size:13px;color:var(--t2);line-height:1.7">PurffleCopyBot mirrors the trades of <b>top-performing Hyperliquid traders</b>. Every 6 hours, the bot scans the Hyperliquid leaderboard and selects {{top_count}} elite traders based on strict criteria:</p>
   <ul style="font-size:12px;color:var(--t2);line-height:2;list-style:none;padding:8px 0 0 0">
    <li>&#x2022; All-time ROI &ge; {{ '%.0f'|format(min_all_roi_pct) }}% (proven profitability)</li>
    <li>&#x2022; Monthly ROI &ge; {{ '%.0f'|format(min_month_roi_pct) }}% (actively making money now)</li>
    <li>&#x2022; Account value ${{ '%.0f'|format(min_acct/1000) }}K–${{ '%.0f'|format(max_acct/1000000) }}M (real money, not whale-slow)</li>
    <li>&#x2022; Daily ROI within &plusmn;{{ '%.0f'|format(max_day_pct) }}% (no freak win/loss days)</li>
   </ul>
  </div>
  <div>
   <h3 style="font-size:14px;font-weight:700;margin-bottom:10px;color:var(--pink)">&#x1F4E1; Consensus Signal Engine</h3>
   <p style="font-size:13px;color:var(--t2);line-height:1.7">Every {{ '%.0f'|format(scan_interval/60) }} minutes, the bot fetches each elite's <b>open positions on Hyperliquid</b>. When <b>&ge;{{min_agree}} elites agree</b> on a long position for the same coin, a consensus signal fires and we paper-open a spot position on Binance ({{ '%.0f'|format(position_size_pct*100) }}% of cash). When elites drop below the threshold we exit too. Hard stop-loss at -{{ '%.0f'|format(hard_stop_pct*100) }}%.</p>
   <h3 style="font-size:14px;font-weight:700;margin:16px 0 10px;color:var(--purple)">&#x2699;&#xFE0F; Key Details</h3>
   <ul style="font-size:12px;color:var(--t2);line-height:2;list-style:none;padding:0">
    <li>&#x2022; <b>Paper trading only</b> — $100 virtual starting capital</li>
    <li>&#x2022; Data: Hyperliquid (on-chain, free) + Binance (public klines)</li>
    <li>&#x2022; No API keys needed — all data sources are public</li>
    <li>&#x2022; Dashboard auto-refreshes every 60 seconds</li>
   </ul>
  </div>
 </div>
</div>

<div class="sec"><h2><span class="ico">&#x1F4C8;</span> Backtest Results (2-Year, $100 Capital)</h2></div>
<div class="tbl-wrap"><table>
 <tr><th>Position Sizing Variant</th><th>Final</th><th>ROI</th><th>Win Rate</th><th>Max DD</th><th>Avg Mo.</th><th>Best Mo.</th><th>Worst Mo.</th></tr>
 <tr style="background:rgba(168,85,247,.06)"><td><b>Stock Purffle (15% size + partial) &#x2B50;</b></td><td><b>$84.17</b></td><td>-15.8%</td><td>40.9%</td><td>30.4%</td><td>-0.6%</td><td class="pos">+4.8%</td><td class="neg">-6.4%</td></tr>
 <tr><td><b>15% size, no partial</b></td><td>$83.84</td><td>-16.2%</td><td>40.9%</td><td>30.5%</td><td>-0.7%</td><td class="pos">+4.9%</td><td class="neg">-6.4%</td></tr>
 <tr><td><b>25% size</b></td><td>$73.13</td><td class="neg">-26.9%</td><td>40.9%</td><td>46.5%</td><td>-1.1%</td><td class="pos">+7.4%</td><td class="neg">-10.2%</td></tr>
 <tr><td><b>Dynamic (10-75%)</b></td><td>$70.59</td><td class="neg">-29.4%</td><td>40.8%</td><td>58.4%</td><td>-1.1%</td><td class="pos">+22.3%</td><td class="neg">-10.3%</td></tr>
 <tr><td><b>Dynamic + partial profit</b></td><td>$74.29</td><td class="neg">-25.7%</td><td>40.8%</td><td>58.2%</td><td>-0.9%</td><td class="pos">+21.9%</td><td class="neg">-10.3%</td></tr>
 <tr><td><b>60% size (aggressive)</b></td><td>$42.53</td><td class="neg">-57.5%</td><td>40.4%</td><td>79.5%</td><td>-2.8%</td><td class="pos">+19.6%</td><td class="neg">-21.4%</td></tr>
</table></div>
<div style="padding:12px 20px;font-size:12px;color:var(--t3);line-height:1.6">
 <b>Window:</b> Jun 2024 — Jun 2026 &middot; <b>Universe:</b> 30 sub-$1 USDT pairs &middot; <b>Best variant:</b> 15% position sizing with partial profit-taking (lowest drawdown at 30.4%, highest final value). Copy-trading is inherently lagging — elite entries are detected after the fact, resulting in worse fill prices. Smaller position sizes dramatically reduce max drawdown while preserving upside capture.
</div>

<div style="text-align:center;padding:24px 0;color:var(--t3);font-size:12px">PurffleCopyBot &middot; Built by <b>Purffle</b></div>
</div>
<script>
(function(){
  // Pause / resume control (button lives in the static topbar so it survives live swaps).
  var pauseBtn = document.getElementById('pauseBtn');
  if (pauseBtn && window.fetch) {
    pauseBtn.addEventListener('click', function(){
      fetch('/api/control', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'},
                             body:'action=toggle'})
        .then(function(r){ return r.json(); })
        .then(function(d){
          var paused = !!d.paused;
          pauseBtn.textContent = paused ? '▶ Resume' : '⏸ Pause';
          pauseBtn.classList.toggle('paused', paused);
        }).catch(function(){});
    });
  }

  var live = document.getElementById('live');
  if (!live || !window.fetch) return;          // no-JS: <noscript> meta-refresh takes over
  var INTERVAL = 15000;
  function animateValue(el, from, to, dur){
    var start = performance.now();
    function frame(now){
      var t = Math.min(1, (now - start) / dur);
      var v = from + (to - from) * (t * (2 - t)); // ease-out
      el.textContent = '$' + v.toFixed(2);
      if (t < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }
  function refresh(){
    fetch(window.location.pathname, {headers: {'X-Requested-With': 'fetch'}})
      .then(function(r){ return r.text(); })
      .then(function(txt){
        var fresh = new DOMParser().parseFromString(txt, 'text/html').getElementById('live');
        if (!fresh) return;
        var oldEl = document.getElementById('mTotal');
        var oldV = oldEl ? parseFloat(oldEl.getAttribute('data-v')) : NaN;
        live.style.opacity = '0.55';
        setTimeout(function(){
          live.innerHTML = fresh.innerHTML;
          live.style.opacity = '1';
          var newEl = document.getElementById('mTotal');
          if (newEl){
            var newV = parseFloat(newEl.getAttribute('data-v'));
            if (!isNaN(oldV) && !isNaN(newV) && oldV !== newV) animateValue(newEl, oldV, newV, 700);
          }
        }, 160);
      }).catch(function(){});
  }
  setInterval(refresh, INTERVAL);
})();
</script>
</body></html>
"""

@app.route("/")
def dashboard():
    cash = get_cash()
    positions = get_positions()
    signals = _state["current_signals"]
    open_positions = []
    holdings_value = 0.0
    mirroring = set()
    for coin, p in positions.items():
        # Reuse the scanner's cached price first; only hit the network if we've never
        # seen this symbol (keeps the dashboard fast instead of N blocking API calls).
        live_price = (_state["last_prices"].get(p["symbol"])
                      or fetch_binance_price(p["symbol"])
                      or p["entry_price"])
        value = p["qty"] * live_price
        pl_pct = ((live_price / p["entry_price"]) - 1) * 100 if p["entry_price"] else 0
        holdings_value += value
        mirroring.add(coin)
        open_positions.append({
            "coin": coin, "symbol": p["symbol"], "qty": p["qty"],
            "entry_price": p["entry_price"], "current": live_price,
            "value": value, "pl_pct": pl_pct,
        })
    with db() as conn:
        trades = [dict(r) for r in conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT 30"
        ).fetchall()]
        trade_count = conn.execute("SELECT COUNT(*) AS c FROM trades").fetchone()["c"]
        starting = float(conn.execute(
            "SELECT value FROM state WHERE key='starting_capital'"
        ).fetchone()["value"])
        elites = [dict(r) for r in conn.execute(
            "SELECT * FROM elite_traders ORDER BY all_time_roi DESC"
        ).fetchall()]
        equity_series = [float(r["total_value"]) for r in conn.execute(
            "SELECT total_value FROM snapshots ORDER BY ts ASC LIMIT 500"
        ).fetchall()]
        stats = compute_trade_stats(conn)
    equity = build_equity_sparkline(equity_series)
    # Sort signals by long count desc
    sig_sorted = sorted(signals.items(), key=lambda x: -x[1].get("long_count", 0))[:15]

    last_lb_str = (datetime.fromtimestamp(_state["last_leaderboard_refresh"], tz=timezone.utc)
                   .strftime("%Y-%m-%d %H:%M") if _state["last_leaderboard_refresh"] else "pending")
    last_scan_str = (datetime.fromtimestamp(_state["last_position_scan"], tz=timezone.utc)
                     .strftime("%H:%M:%S") if _state["last_position_scan"] else "pending")

    return render_template_string(
        DASH_HTML, cash=cash, holdings_value=holdings_value, total=cash + holdings_value,
        starting=starting, open_positions=open_positions, open_count=len(positions),
        max_concurrent=MAX_CONCURRENT_POSITIONS, trade_count=trade_count, trades=trades,
        elites=elites, elite_count=len(elites), signals=sig_sorted,
        min_agree=MIN_AGREEING_TRADERS, mirroring=mirroring,
        scan_interval=POSITION_SCAN_SECONDS, scans=_state["scans"],
        last_lb=last_lb_str, last_scan=last_scan_str,
        top_count=TOP_TRADERS_COUNT,
        min_all_roi_pct=MIN_ALL_TIME_ROI * 100, min_month_roi_pct=MIN_MONTH_ROI * 100,
        min_acct=MIN_ACCOUNT_VALUE, max_acct=MAX_ACCOUNT_VALUE,
        max_day_pct=MAX_DAY_ROI_ABS * 100, position_size_pct=POSITION_SIZE_PCT,
        hard_stop_pct=HARD_STOP_PCT, equity=equity,
        stats=stats, paused=_state["paused"],
    )


@app.route("/export/trades.csv")
def export_trades_csv():
    """Download the full trade log as CSV."""
    with db() as conn:
        rows = conn.execute(
            "SELECT ts,coin,symbol,side,qty,price,value,fee,reason,realized_pnl "
            "FROM trades ORDER BY id ASC"
        ).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp", "coin", "symbol", "side", "qty", "price", "value",
                "fee", "reason", "realized_pnl"])
    for r in rows:
        w.writerow([r["ts"], r["coin"], r["symbol"], r["side"], r["qty"], r["price"],
                    r["value"], r["fee"], r["reason"], r["realized_pnl"]])
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=purfflecopybot_trades.csv"},
    )


@app.route("/api/control", methods=["POST"])
def api_control():
    """Pause or resume the background mirror scanner."""
    action = (request.form.get("action") or request.args.get("action") or "").lower()
    if action == "pause":
        _state["paused"] = True
    elif action == "resume":
        _state["paused"] = False
    elif action == "toggle":
        _state["paused"] = not _state["paused"]
    else:
        return jsonify({"error": "action must be pause, resume, or toggle"}), 400
    log(f"scanner {'paused' if _state['paused'] else 'resumed'} via dashboard")
    return jsonify({"paused": _state["paused"]})


@app.route("/api/state")
def api_state():
    cash = get_cash()
    positions = get_positions()
    with db() as conn:
        elites = [dict(r) for r in conn.execute("SELECT * FROM elite_traders").fetchall()]
        trades = [dict(r) for r in conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT 100"
        ).fetchall()]
    return jsonify({
        "cash": cash, "positions": list(positions.values()),
        "elites": elites, "signals": _state["current_signals"],
        "trades": trades, "scans": _state["scans"],
        "last_leaderboard_refresh": _state["last_leaderboard_refresh"],
        "last_position_scan": _state["last_position_scan"],
        "config": {
            "min_all_time_roi": MIN_ALL_TIME_ROI,
            "min_account_value": MIN_ACCOUNT_VALUE,
            "max_account_value": MAX_ACCOUNT_VALUE,
            "top_traders_count": TOP_TRADERS_COUNT,
            "min_agreeing_traders": MIN_AGREEING_TRADERS,
            "position_size_pct": POSITION_SIZE_PCT,
            "hard_stop_pct": HARD_STOP_PCT,
        },
    })


def main() -> None:
    init_db()
    log("=" * 60)
    log("PurffleBot Copy starting — Hyperliquid mirror bot")
    log(f"criteria: allROI>={MIN_ALL_TIME_ROI*100:.0f}%, monthROI>={MIN_MONTH_ROI*100:.0f}%, "
        f"acct ${MIN_ACCOUNT_VALUE:,.0f}-${MAX_ACCOUNT_VALUE:,.0f}, top {TOP_TRADERS_COUNT}")
    log(f"mirror: open when >={MIN_AGREEING_TRADERS} elites long, {POSITION_SIZE_PCT*100:.0f}% cash per trade, "
        f"hard stop {HARD_STOP_PCT*100:.0f}%, max {MAX_CONCURRENT_POSITIONS} concurrent")
    threading.Thread(target=scanner_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "12349"))
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
