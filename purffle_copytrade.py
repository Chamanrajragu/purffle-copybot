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
from flask import Flask, jsonify, render_template_string


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
    "running": False, "last_leaderboard_refresh": 0.0, "last_position_scan": 0.0,
    "elites": [], "current_signals": {},  # coin -> {long_count, short_count, traders}
    "scans": 0,
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

    _state["scans"] += 1
    _state["last_position_scan"] = time.time()


def scanner_loop() -> None:
    _state["running"] = True
    while True:
        try:
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
<html><head><title>PurffleBot Copy — Hyperliquid mirror</title>
<meta http-equiv="refresh" content="60">
<style>
 body{font-family:-apple-system,Segoe UI,sans-serif;background:#0f0a14;color:#f0e6f5;margin:0;padding:24px}
 h1{margin:0 0 8px;font-size:22px}
 h1 .tag{background:#5f1f3a;color:#ff7cb6;padding:3px 10px;border-radius:6px;font-size:12px;margin-left:10px;vertical-align:middle}
 h2{font-size:16px;margin:24px 0 8px}
 .row{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:24px}
 .card{background:#1a0f25;border:1px solid #2e1a3a;border-radius:10px;padding:18px;min-width:200px;flex:1}
 .label{color:#9a7a98;font-size:12px;text-transform:uppercase;letter-spacing:.05em}
 .value{font-size:24px;font-weight:600;margin-top:4px}
 .pos{color:#36d399}.neg{color:#ff7a8a}
 table{width:100%;border-collapse:collapse;background:#1a0f25;border-radius:10px;overflow:hidden}
 th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #2e1a3a;font-size:14px}
 th{background:#22142e;color:#9a7a98;font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.05em}
 tr:last-child td{border-bottom:none}
 .muted{color:#7a5b87;font-size:12px}
 .nav a{color:#ff7cb6;margin-right:18px;text-decoration:none;font-size:14px}
 .pill{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600}
 .pill.up{background:#193c2a;color:#36d399}.pill.dn{background:#3c1924;color:#ff7a8a}
 .pill.long{background:#1f3a5f;color:#7cb6ff}
 a.addr{color:#c19eff;font-family:monospace;font-size:12px;text-decoration:none}
</style></head><body>
<div class="nav"><a href="/">Dashboard</a><a href="/api/state">Raw state (JSON)</a></div>
<h1>PurffleBot Copy <span class="tag">HYPERLIQUID MIRROR · TOP {{elite_count}}</span></h1>
<div class="muted">Mirrors top traders on Hyperliquid. Opens BTC/ETH/SOL/etc on Binance spot when >= {{min_agree}} elites agree long. Paper only. NOT a guaranteed money machine — read the live signal feed below to see what the elites are actually doing.</div>

<div class="row">
 <div class="card"><div class="label">Total value</div>
  <div class="value {{'pos' if total>=starting else 'neg'}}">${{ '%.2f'|format(total) }}</div>
  <div class="muted">P/L ${{ '%+.2f'|format(total-starting) }} ({{ '%+.2f'|format((total/starting-1)*100) }}%)</div></div>
 <div class="card"><div class="label">Cash</div><div class="value">${{ '%.2f'|format(cash) }}</div></div>
 <div class="card"><div class="label">Holdings</div><div class="value">${{ '%.2f'|format(holdings_value) }}</div></div>
 <div class="card"><div class="label">Open mirror positions</div><div class="value">{{ open_count }} / {{ max_concurrent }}</div></div>
 <div class="card"><div class="label">Trades total</div><div class="value">{{ trade_count }}</div></div>
</div>

<h2>👑 Elite traders we're following</h2>
<table>
 <tr><th>Display name</th><th>Wallet</th><th>Account value</th><th>All-time ROI</th><th>Month ROI</th><th>Day ROI</th></tr>
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
 <tr><td colspan="6" class="muted">Loading leaderboard... (first run takes 60s for 31MB pull)</td></tr>
 {% endfor %}
</table>

<h2>📡 Live consensus signals — what elites are currently long</h2>
<table>
 <tr><th>Coin</th><th>Long votes</th><th>Short votes</th><th>Who's long</th><th>Status</th></tr>
 {% for coin, sig in signals %}
 <tr>
  <td><b>{{ coin }}</b></td>
  <td class="pos">{{ sig.long_count }}</td>
  <td class="neg">{{ sig.short_count }}</td>
  <td class="muted">{{ sig.longs_by | join(', ') }}</td>
  <td>{% if sig.long_count >= min_agree %}<span class="pill long">SIGNAL{% if coin in mirroring %} (mirroring){% endif %}</span>{% else %}<span class="muted">below threshold</span>{% endif %}</td>
 </tr>
 {% else %}
 <tr><td colspan="5" class="muted">No consensus signals yet — waiting for next position scan</td></tr>
 {% endfor %}
</table>

<h2>Our mirror positions</h2>
<table>
 <tr><th>Coin</th><th>Symbol</th><th>Qty</th><th>Entry</th><th>Current</th><th>Value</th><th>P/L</th></tr>
 {% for p in open_positions %}
 <tr>
  <td><b>{{ p.coin }}</b></td><td>{{ p.symbol }}</td>
  <td>{{ '%.6f'|format(p.qty) }}</td>
  <td>${{ '%.4f'|format(p.entry_price) }}</td>
  <td>${{ '%.4f'|format(p.current) }}</td>
  <td>${{ '%.2f'|format(p.value) }}</td>
  <td class="{{'pos' if p.pl_pct>=0 else 'neg'}}">{{ '%+.2f'|format(p.pl_pct) }}%</td>
 </tr>
 {% else %}
 <tr><td colspan="7" class="muted">No mirror positions open. Bot waits for elites to converge on a long.</td></tr>
 {% endfor %}
</table>

<h2>Recent trades</h2>
<table>
 <tr><th>Time</th><th>Side</th><th>Coin</th><th>Qty</th><th>Price</th><th>Value</th><th>Reason</th><th>P/L</th></tr>
 {% for t in trades %}
 <tr>
  <td>{{ t.ts[:19].replace('T',' ') }}</td>
  <td><span class="pill {{'up' if t.side=='BUY' else 'dn'}}">{{ t.side }}</span></td>
  <td>{{ t.coin }}</td>
  <td>{{ '%.6f'|format(t.qty) }}</td>
  <td>${{ '%.4f'|format(t.price) }}</td>
  <td>${{ '%.2f'|format(t.value) }}</td>
  <td class="muted">{{ t.reason }}</td>
  <td class="{{'pos' if t.realized_pnl>=0 else 'neg'}}">{% if t.side=='SELL' %}${{ '%+.4f'|format(t.realized_pnl) }}{% endif %}</td>
 </tr>
 {% else %}
 <tr><td colspan="8" class="muted">No trades yet.</td></tr>
 {% endfor %}
</table>

<div class="muted" style="margin-top:24px">
 Leaderboard refresh every 6h · positions scanned every {{scan_interval}}s · {{scans}} scans · last leaderboard {{last_lb}} · last scan {{last_scan}}
</div>
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
        live_price = fetch_binance_price(p["symbol"]) or p["entry_price"]
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
    )


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
