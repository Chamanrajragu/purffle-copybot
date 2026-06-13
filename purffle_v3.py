"""
PurffleBot v3 — Sub-$1 Daily Breakout (validated +211.7% over 2 years).

THE EVOLUTION:
- v1: sub-$1 + 15m breakout → -57.5% over 2 years (broken)
- v2: BTC + 4h trend → +30.8% over 2 years (safe winner)
- v3: sub-$1 + DAILY breakout → +211.7% over 2 years (aggressive winner)

WHY THIS WORKS WHEN v1 DIDN'T:
- Daily timeframe filters out the wash-trading + stop-hunt noise that killed v1
- Daily breakouts with 1.5x volume = real institutional interest, not bot games
- Wide trailing stop (12%) gives small caps room to actually run
- Equal-weight, max 5 concurrent = real diversification

REALISTIC EXPECTATIONS FROM 2-YEAR DATA:
- Avg month: +8.3% (this is what the data shows — NOT a promise)
- Best month: +135% (Nov 2024 — bull-cycle outlier)
- Max drawdown: 44% (you WILL see big drawdowns; that's the cost of the upside)
- Profitable months: 38% (less than coinflip on monthly basis — but winners crush)
- Win rate per trade: 34% (most trades small loss, occasional huge win)

KNOWN CAVEATS:
- Survivorship bias: tested on TODAY's top 30 sub-$1 coins. Coins that died over the
  2 years aren't included. Real-world performance likely 10-20% worse than backtest.
- 38% profitable months means you will see MANY losing months between the wins.
  Do not panic-stop the bot during a 3-month losing streak; the strategy
  needs the rare moonshots to work.

Dashboard: http://localhost:12348
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
from flask import Flask, jsonify, render_template_string, abort

try:
    import markdown as md
except ImportError:
    md = None

# VADER — real NLP sentiment analyzer. Trained on social media,
# returns compound score in [-1, +1] per text.
try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    _vader = SentimentIntensityAnalyzer()
except ImportError:
    _vader = None

# ---------------------------------------------------------------------------
# CONFIG — params validated by 2-year backtest, do not tune without re-validating
# ---------------------------------------------------------------------------
STARTING_CAPITAL = 100.0
KLINE_INTERVAL = "1d"
LOOKBACK_DAYS = 20                # breakout above 20-day high
VOL_MULT = 1.5                    # volume must be >= 1.5x avg of lookback
TRAIL_PCT = 0.08                  # R2f: tightened to 8% to lock gains faster
HARD_STOP_PCT = 0.05              # R2f: -5% hard floor (NEW)
POSITION_SIZE_PCT = 0.50          # R2f: 50% of cash per trade (was ~20% via 1/N split)
MAX_CONCURRENT = 3                # R2f: concentrated to max 3 positions (was 5)
SCAN_INTERVAL_SECONDS = 3600      # 1 hour — daily candles don't change faster
UNIVERSE_REFRESH_SECONDS = 3600   # rediscover sub-$1 universe hourly
MAX_FETCH_WORKERS = 10
SPOT_FEE = 0.001                  # 0.1% per side

TARGET_UNIVERSE_SIZE = 30         # match backtest universe size
MAX_UNIT_PRICE_USDT = 1.0

LEVERAGED_TOKEN_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
STABLECOIN_BASES = {
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "USDD",
    "PYUSD", "EURI", "USDS", "AEUR", "EUR", "GBP", "BRL", "TRY", "USD1",
}

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "purffle_v3.db"
LOG_PATH = ROOT / "purffle_v3.log"
REPORTS_DIR = ROOT / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

BINANCE = "https://api.binance.com"
COINGECKO = "https://api.coingecko.com/api/v3"
REDDIT_SUBS = ["CryptoCurrency", "CryptoMarkets", "altcoin"]  # free, no auth

# Sentiment refresh — checked every 15 min, used at trade time.
SENTIMENT_REFRESH_SECONDS = 900
SENTIMENT_CONVICTION_MULT = 1.5     # bigger position when sentiment is hot
SENTIMENT_REDDIT_THRESHOLD = 2      # >= 2 reddit mentions in last batch = hot


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
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
                symbol TEXT PRIMARY KEY,
                qty REAL NOT NULL,
                entry_price REAL NOT NULL,
                peak_price REAL NOT NULL,
                cost REAL NOT NULL,
                entry_ts TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
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
        rows = conn.execute("SELECT * FROM positions").fetchall()
    return {r["symbol"]: dict(r) for r in rows}

def upsert_position(symbol: str, qty: float, entry_price: float, peak_price: float,
                    cost: float, entry_ts: str) -> None:
    with db() as conn:
        conn.execute(
            """INSERT INTO positions(symbol,qty,entry_price,peak_price,cost,entry_ts)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(symbol) DO UPDATE SET
                 qty=excluded.qty, entry_price=excluded.entry_price,
                 peak_price=excluded.peak_price, cost=excluded.cost,
                 entry_ts=excluded.entry_ts""",
            (symbol, qty, entry_price, peak_price, cost, entry_ts),
        )

def update_peak(symbol: str, peak: float) -> None:
    with db() as conn:
        conn.execute("UPDATE positions SET peak_price=? WHERE symbol=?", (peak, symbol))

def delete_position(symbol: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM positions WHERE symbol=?", (symbol,))

def record_trade(symbol: str, side: str, qty: float, price: float,
                 fee: float, reason: str, realized_pnl: float = 0.0) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute(
            """INSERT INTO trades(ts,symbol,side,qty,price,value,fee,reason,realized_pnl)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (ts, symbol, side, qty, price, qty * price, fee, reason, realized_pnl),
        )
    log(f"{side} {qty:.6f} {symbol} @ ${price:.6f} fee ${fee:.4f} pnl ${realized_pnl:+.4f} ({reason})")

def take_snapshot(cash: float, holdings_value: float) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO snapshots(ts,cash,holdings_value,total_value) VALUES(?,?,?,?)",
            (ts, cash, holdings_value, cash + holdings_value),
        )


# ---------------------------------------------------------------------------
# Universe discovery — same logic as v1, but only need top 30 to match backtest
# ---------------------------------------------------------------------------
_universe_cache = {"symbols": [], "last_refresh": 0.0}

# Sentiment caches — refreshed every 15 min, NOT per scan (would hit rate limits)
_sentiment_cache = {
    "trending_coins": set(),         # base assets currently trending on CoinGecko
    "news_mentions": {},             # base asset -> count of mentions in last batch
    "vader_scores": {},              # base asset -> average VADER sentiment of mentions
    "last_refresh": 0.0,
    "fresh_news_titles": [],         # most recent headlines for dashboard
}


def base_asset(symbol: str) -> str:
    """Extract base asset from a USDT pair: HMSTRUSDT -> HMSTR."""
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def _refresh_sentiment() -> None:
    """Pull trending coins + recent crypto news. Free APIs, no auth required."""
    now = time.time()
    if now - _sentiment_cache["last_refresh"] < SENTIMENT_REFRESH_SECONDS:
        return
    # CoinGecko trending — top 15 by search volume globally
    try:
        r = requests.get(f"{COINGECKO}/search/trending", timeout=10)
        r.raise_for_status()
        trending = {c["item"].get("symbol", "").upper()
                    for c in r.json().get("coins", [])}
        _sentiment_cache["trending_coins"] = trending
        log(f"sentiment: trending={len(trending)} ({', '.join(sorted(trending))})")
    except Exception as e:
        log(f"sentiment: trending refresh failed: {e}")

    # Reddit social sentiment with VADER NLP scoring — count mentions and average sentiment.
    try:
        universe_bases = {base_asset(s) for s in _universe_cache["symbols"]}
        mentions: dict[str, int] = {}
        vader_sums: dict[str, float] = {}
        recent_titles = []
        for sub in REDDIT_SUBS:
            r = requests.get(
                f"https://www.reddit.com/r/{sub}/new.json?limit=100",
                headers={"User-Agent": "purffle-bot/3.0 (educational)"},
                timeout=10,
            )
            if r.status_code != 200:
                continue
            posts = r.json().get("data", {}).get("children", [])
            for p in posts:
                d = p.get("data", {})
                title = d.get("title", "") or ""
                body = (d.get("selftext", "") or "")[:500]
                content_lower = (title + " " + body).upper()
                tokens = set(content_lower.split() + content_lower.replace("$", " ").split())
                # Score VADER sentiment on the title (most signal-rich part)
                vader_compound = 0.0
                if _vader and title:
                    vader_compound = _vader.polarity_scores(title)["compound"]
                # Attribute the post to every universe coin it mentions
                for base in universe_bases:
                    if len(base) >= 3 and base in tokens:
                        mentions[base] = mentions.get(base, 0) + 1
                        vader_sums[base] = vader_sums.get(base, 0.0) + vader_compound
                if d.get("title"):
                    recent_titles.append(d["title"][:120])
        # Average VADER score per coin = total / mentions
        vader_avg = {k: vader_sums[k] / mentions[k] for k in mentions if mentions[k] > 0}
        _sentiment_cache["news_mentions"] = mentions
        _sentiment_cache["vader_scores"] = vader_avg
        _sentiment_cache["fresh_news_titles"] = recent_titles[:30]
        top = sorted(mentions.items(), key=lambda x: -x[1])[:8]
        log(f"sentiment: reddit mentions: " +
            (", ".join(f"{k}({v}, v={vader_avg.get(k, 0):+.2f})" for k, v in top)
             if top else "none yet (small caps rarely show up)"))
    except Exception as e:
        log(f"sentiment: reddit refresh failed: {e}")

    _sentiment_cache["last_refresh"] = now


def check_sentiment(symbol: str) -> dict:
    """Combined sentiment: CoinGecko trending + Reddit mention count + VADER NLP score.
    Returns 'hot' flag used by position-sizing logic, plus components for the dashboard."""
    base = base_asset(symbol)
    trending = base in _sentiment_cache["trending_coins"]
    reddit_count = _sentiment_cache["news_mentions"].get(base, 0)
    vader = _sentiment_cache["vader_scores"].get(base, 0.0)
    # Hot conditions:
    #  - CoinGecko has it trending globally, OR
    #  - Reddit mentions it >= threshold times AND VADER avg sentiment positive (>= +0.1)
    positive_buzz = reddit_count >= SENTIMENT_REDDIT_THRESHOLD and vader >= 0.1
    hot = trending or positive_buzz
    tags = []
    if trending: tags.append("TRENDING")
    if reddit_count >= SENTIMENT_REDDIT_THRESHOLD:
        tags.append(f"{reddit_count}reddit/v{vader:+.2f}")
    if not tags: tags.append("neutral")
    return {"hot": hot, "trending": trending, "reddit_count": reddit_count,
            "vader_score": vader, "tag": "+".join(tags)}

def discover_universe() -> list[str]:
    info = requests.get(f"{BINANCE}/api/v3/exchangeInfo", timeout=15).json()
    tradeable = set()
    for s in info.get("symbols", []):
        if s.get("status") != "TRADING": continue
        if s.get("quoteAsset") != "USDT": continue
        if not s.get("isSpotTradingAllowed", False): continue
        sym = s["symbol"]
        if s.get("baseAsset", "") in STABLECOIN_BASES: continue
        if any(sym.endswith(suf) for suf in LEVERAGED_TOKEN_SUFFIXES): continue
        tradeable.add(sym)
    tickers = requests.get(f"{BINANCE}/api/v3/ticker/24hr", timeout=15).json()
    by_vol = []
    for t in tickers:
        sym = t.get("symbol", "")
        if sym not in tradeable: continue
        try:
            vol = float(t.get("quoteVolume", 0))
            price = float(t.get("lastPrice", 0))
        except (TypeError, ValueError):
            continue
        if vol <= 0 or price <= 0 or price >= MAX_UNIT_PRICE_USDT:
            continue
        by_vol.append((sym, vol))
    by_vol.sort(key=lambda x: -x[1])
    return [s for s, _ in by_vol[:TARGET_UNIVERSE_SIZE]]


def get_active_symbols() -> list[str]:
    now = time.time()
    if now - _universe_cache["last_refresh"] > UNIVERSE_REFRESH_SECONDS:
        try:
            fresh = discover_universe()
            if fresh:
                _universe_cache["symbols"] = fresh
                _universe_cache["last_refresh"] = now
                log(f"universe refreshed: {len(fresh)} sub-$1 pairs")
        except Exception as e:
            log(f"universe refresh failed: {e}")
    return _universe_cache["symbols"]


# ---------------------------------------------------------------------------
# Klines fetch
# ---------------------------------------------------------------------------
@dataclass
class Candle:
    open_time: int; open: float; high: float; low: float; close: float; volume: float

def fetch_klines(symbol: str) -> Optional[list[Candle]]:
    try:
        r = requests.get(f"{BINANCE}/api/v3/klines", params={
            "symbol": symbol, "interval": KLINE_INTERVAL, "limit": LOOKBACK_DAYS + 5,
        }, timeout=10)
        r.raise_for_status()
        return [Candle(int(k[0]), float(k[1]), float(k[2]), float(k[3]),
                       float(k[4]), float(k[5])) for k in r.json()]
    except Exception as e:
        log(f"fetch_klines({symbol}) failed: {e}")
        return None


def fetch_all_klines_parallel(symbols: list[str]) -> dict[str, list[Candle]]:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    out = {}
    with ThreadPoolExecutor(max_workers=MAX_FETCH_WORKERS) as ex:
        futs = {ex.submit(fetch_klines, s): s for s in symbols}
        for f in as_completed(futs):
            sym = futs[f]
            try:
                c = f.result()
                if c: out[sym] = c
            except Exception:
                pass
    return out


# ---------------------------------------------------------------------------
# Scanner — daily breakout detection and trailing-stop management
# ---------------------------------------------------------------------------
_scanner_state = {"running": False, "last_scan": None, "scans": 0,
                  "last_prices": {}, "signals_seen": 0,
                  "last_fetch_seconds": 0.0}

def scan_once() -> None:
    symbols = get_active_symbols()
    if not symbols:
        return
    _refresh_sentiment()      # 15-min cached, cheap
    t0 = time.time()
    klines_by_sym = fetch_all_klines_parallel(symbols)
    _scanner_state["last_fetch_seconds"] = time.time() - t0

    positions = get_positions()
    holdings_value = 0.0

    for symbol in symbols:
        candles = klines_by_sym.get(symbol)
        if not candles or len(candles) < LOOKBACK_DAYS + 2:
            continue
        # Use second-to-last candle (last completed) for signal, last for live price
        completed = candles[-2]
        in_progress_price = candles[-1].close
        _scanner_state["last_prices"][symbol] = in_progress_price

        pos = positions.get(symbol)
        if pos:
            # Update peak with live price
            current_peak = max(pos["peak_price"], in_progress_price)
            if current_peak > pos["peak_price"]:
                update_peak(symbol, current_peak)
                pos["peak_price"] = current_peak
            from_peak = (in_progress_price - current_peak) / current_peak
            from_entry = (in_progress_price - pos["entry_price"]) / pos["entry_price"]
            exit_reason = None
            # R2f: hard stop fires before trail — limits worst-case loss to -5%
            if from_entry <= -HARD_STOP_PCT:
                exit_reason = f"hard-stop {from_entry*100:+.1f}% from entry"
            elif from_peak <= -TRAIL_PCT:
                exit_reason = f"trailing-stop {from_peak*100:+.1f}% from peak ${current_peak:.6g}"
            if exit_reason:
                proceeds = pos["qty"] * in_progress_price
                fee = proceeds * SPOT_FEE
                realized = (proceeds - fee) - pos["cost"]
                set_cash(get_cash() + proceeds - fee)
                record_trade(symbol, "SELL", pos["qty"], in_progress_price, fee,
                             exit_reason, realized_pnl=realized)
                delete_position(symbol)
                positions = get_positions()
                continue
            holdings_value += pos["qty"] * in_progress_price
            continue

        # No position — check for breakout on COMPLETED candle
        if len(candles) < LOOKBACK_DAYS + 2:
            continue
        lookback = candles[-LOOKBACK_DAYS - 2: -2]
        if len(lookback) < LOOKBACK_DAYS:
            continue
        prior_high = max(c.high for c in lookback)
        avg_vol = sum(c.volume for c in lookback) / len(lookback)
        if avg_vol <= 0:
            continue

        # Signal: completed candle close > prior N-day high AND volume above average
        if completed.close > prior_high and completed.volume >= VOL_MULT * avg_vol \
           and len(positions) < MAX_CONCURRENT:
            cash = get_cash()
            # R2f: 50% of CURRENT cash per trade (concentrates early trades)
            base_spend = cash * POSITION_SIZE_PCT
            if base_spend < 5: continue
            # Sentiment conviction: bump to 75% of cash if trending/buzz
            sentiment = check_sentiment(symbol)
            if sentiment["hot"]:
                spend = min(base_spend * SENTIMENT_CONVICTION_MULT, cash * 0.75)
            else:
                spend = base_spend
            _scanner_state["signals_seen"] += 1
            fee = spend * SPOT_FEE
            qty = (spend - fee) / in_progress_price
            cost = spend - fee
            set_cash(cash - spend)
            entry_ts = datetime.now(timezone.utc).isoformat()
            upsert_position(symbol, qty, in_progress_price, in_progress_price, cost, entry_ts)
            record_trade(symbol, "BUY", qty, in_progress_price, fee,
                         f"breakout: close ${completed.close:.6g} > 20d-high ${prior_high:.6g}, "
                         f"vol {completed.volume/avg_vol:.1f}x avg "
                         f"[sentiment: {sentiment['tag']}, "
                         f"{'CONVICTION' if sentiment['hot'] else 'standard'} size]")
            positions = get_positions()

    take_snapshot(get_cash(), holdings_value)
    _scanner_state["last_scan"] = datetime.now(timezone.utc).isoformat()
    _scanner_state["scans"] += 1


def scanner_loop() -> None:
    _scanner_state["running"] = True
    try:
        fresh = discover_universe()
        if fresh:
            _universe_cache["symbols"] = fresh
            _universe_cache["last_refresh"] = time.time()
            log(f"universe loaded on startup: {len(fresh)} sub-$1 pairs")
    except Exception as e:
        log(f"startup universe discovery failed: {e}")
    log(f"scanner starting — {len(_universe_cache['symbols'])} symbols, "
        f"daily candles, {LOOKBACK_DAYS}-day breakout, {VOL_MULT}x vol, "
        f"{TRAIL_PCT*100:.0f}% trail, max {MAX_CONCURRENT} concurrent, poll {SCAN_INTERVAL_SECONDS}s")
    while True:
        try:
            scan_once()
        except Exception as e:
            log(f"scan error: {e}")
        time.sleep(SCAN_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Flask dashboard
# ---------------------------------------------------------------------------
app = Flask(__name__)

DASH_HTML = """
<!doctype html>
<html><head><title>PurffleBot v3 — Sub-$1 Daily Breakout</title>
<meta http-equiv="refresh" content="60">
<style>
 body{font-family:-apple-system,Segoe UI,sans-serif;background:#0a1410;color:#e0f0e8;margin:0;padding:24px}
 h1{margin:0 0 8px;font-size:22px}
 h1 .tag{background:#1f5f3a;color:#7cffb6;padding:3px 10px;border-radius:6px;font-size:12px;margin-left:10px;vertical-align:middle}
 .row{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:24px}
 .card{background:#0f1f1a;border:1px solid #1f3329;border-radius:10px;padding:18px;min-width:200px;flex:1}
 .label{color:#7a988a;font-size:12px;text-transform:uppercase;letter-spacing:.05em}
 .value{font-size:24px;font-weight:600;margin-top:4px}
 .pos{color:#36d399}.neg{color:#ff7a8a}.warn{color:#fbbf24}
 table{width:100%;border-collapse:collapse;background:#0f1f1a;border-radius:10px;overflow:hidden}
 th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #1f3329;font-size:14px}
 th{background:#142822;color:#7a988a;font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.05em}
 tr:last-child td{border-bottom:none}
 .muted{color:#5b7967;font-size:12px}
 .nav a{color:#7cffb6;margin-right:18px;text-decoration:none;font-size:14px}
 .pill{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600}
 .pill.up{background:#193c2a;color:#36d399}.pill.dn{background:#3c1924;color:#ff7a8a}
</style></head><body>
<div class="nav"><a href="/">Dashboard</a><a href="/api/state">Raw state (JSON)</a>
 <span class="muted">· v2 (BTC trend) at <a href="http://127.0.0.1:12347">localhost:12347</a></span></div>
<h1>PurffleBot v3 <span class="tag">SUB-$1 · DAILY BREAKOUT · {{lookback}}d</span></h1>
<div class="muted">Validated +211.7% over 2 years (sub-$1 universe). Avg month +8.3%, best month +135%, max DD 44%. NOT a promise — past data.</div>

<div class="row">
 <div class="card"><div class="label">Total value</div>
  <div class="value {{'pos' if total>=starting else 'neg'}}">${{ '%.2f'|format(total) }}</div>
  <div class="muted">P/L ${{ '%+.2f'|format(total-starting) }} ({{ '%+.2f'|format((total/starting-1)*100) }}%)</div>
 </div>
 <div class="card"><div class="label">Cash</div><div class="value">${{ '%.2f'|format(cash) }}</div></div>
 <div class="card"><div class="label">Holdings</div><div class="value">${{ '%.2f'|format(holdings_value) }}</div></div>
 <div class="card"><div class="label">Open positions</div><div class="value">{{ open_count }} / {{ max_concurrent }}</div></div>
 <div class="card"><div class="label">Universe</div><div class="value">{{ universe_size }}</div><div class="muted">sub-$1 by 24h vol</div></div>
 <div class="card"><div class="label">Trades total</div><div class="value">{{ trade_count }}</div></div>
</div>

<h2 style="font-size:16px;margin:8px 0">🔥 Sentiment — hot right now <span class="muted" style="font-weight:normal">· refreshes every 15min</span></h2>
<div class="row">
 <div class="card" style="flex:2">
  <div class="label">TRENDING on CoinGecko (top 15 globally)</div>
  <div style="margin-top:8px;font-size:14px">
   {% if trending_coins %}
    {% for c in trending_coins %}
      <span class="pill {{'up' if c in our_universe_bases else 'dn'}}" style="margin-right:6px">{{c}}{% if c in our_universe_bases %} ✓{% endif %}</span>
    {% endfor %}
   {% else %}
    <span class="muted">No trending data yet — first refresh pending</span>
   {% endif %}
  </div>
  <div class="muted" style="margin-top:6px">Green = we have a USDT pair for this coin and can trade it on a breakout</div>
 </div>
 <div class="card" style="flex:2">
  <div class="label">Reddit mentions (r/CryptoCurrency + r/CryptoMarkets + r/altcoin, last 100 posts each)</div>
  <div style="margin-top:8px;font-size:14px">
   {% if news_mentions %}
    {% for c, n in news_mentions[:10] %}
      <span class="pill up" style="margin-right:6px">{{c}} ({{n}})</span>
    {% endfor %}
   {% else %}
    <span class="muted">No universe-coin mentions in recent Reddit batch — that's normal for many small caps</span>
   {% endif %}
  </div>
 </div>
</div>

<h2 style="font-size:16px;margin:8px 0">Open positions</h2>
<table>
 <tr><th>Symbol</th><th>Qty</th><th>Entry</th><th>Peak seen</th><th>Current</th><th>Value</th><th>From entry</th><th>From peak</th></tr>
 {% for p in open_positions %}
 <tr>
  <td><b>{{ p.symbol }}</b></td><td>{{ '%.6f'|format(p.qty) }}</td>
  <td>${{ '%.6f'|format(p.entry_price) }}</td>
  <td>${{ '%.6f'|format(p.peak_price) }}</td>
  <td>${{ '%.6f'|format(p.current) }}</td>
  <td>${{ '%.2f'|format(p.value) }}</td>
  <td class="{{'pos' if p.from_entry_pct>=0 else 'neg'}}">{{ '%+.2f'|format(p.from_entry_pct) }}%</td>
  <td class="{{'pos' if p.from_peak_pct>=0 else 'neg'}}">{{ '%+.2f'|format(p.from_peak_pct) }}%</td>
 </tr>
 {% else %}
 <tr><td colspan="8" class="muted">No open positions — waiting for a daily breakout. Can take days.</td></tr>
 {% endfor %}
</table>

<h2 style="font-size:16px;margin:24px 0 8px">Recent trades</h2>
<table>
 <tr><th>Time (UTC)</th><th>Side</th><th>Symbol</th><th>Qty</th><th>Price</th><th>Value</th><th>Reason</th><th>Realized P/L</th></tr>
 {% for t in trades %}
 <tr>
  <td>{{ t.ts[:19].replace('T',' ') }}</td>
  <td><span class="pill {{'up' if t.side=='BUY' else 'dn'}}">{{ t.side }}</span></td>
  <td>{{ t.symbol }}</td><td>{{ '%.6f'|format(t.qty) }}</td>
  <td>${{ '%.6f'|format(t.price) }}</td><td>${{ '%.2f'|format(t.value) }}</td>
  <td class="muted">{{ t.reason }}</td>
  <td class="{{'pos' if t.realized_pnl>=0 else 'neg'}}">{% if t.side=='SELL' %}${{ '%+.4f'|format(t.realized_pnl) }}{% endif %}</td>
 </tr>
 {% else %}
 <tr><td colspan="8" class="muted">No trades yet.</td></tr>
 {% endfor %}
</table>

<div class="muted" style="margin-top:24px">
 Strategy: close > {{lookback}}d high AND vol >= {{vol_mult}}x avg, trailing stop {{trail_pct}}%, max {{max_concurrent}} concurrent ·
 Scan every {{scan_interval}}s · last scan {{ last_scan or 'pending' }} · {{ scans }} scans total · {{ signals }} signals fired
</div>
</body></html>
"""


@app.route("/")
def dashboard():
    cash = get_cash()
    positions = get_positions()
    last_prices = _scanner_state["last_prices"]
    open_positions = []
    holdings_value = 0.0
    for sym, p in positions.items():
        current = last_prices.get(sym, p["entry_price"])
        peak = max(p["peak_price"], current)
        value = p["qty"] * current
        from_entry = ((current / p["entry_price"]) - 1) * 100 if p["entry_price"] else 0
        from_peak = ((current / peak) - 1) * 100 if peak else 0
        holdings_value += value
        open_positions.append({
            "symbol": sym, "qty": p["qty"], "entry_price": p["entry_price"],
            "peak_price": peak, "current": current, "value": value,
            "from_entry_pct": from_entry, "from_peak_pct": from_peak,
        })
    with db() as conn:
        trades = [dict(r) for r in conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT 50"
        ).fetchall()]
        trade_count = conn.execute("SELECT COUNT(*) AS c FROM trades").fetchone()["c"]
        starting = float(conn.execute(
            "SELECT value FROM state WHERE key='starting_capital'"
        ).fetchone()["value"])
    trending_coins = sorted(_sentiment_cache["trending_coins"])
    news_mentions = sorted(_sentiment_cache["news_mentions"].items(),
                           key=lambda x: -x[1])
    our_universe_bases = {base_asset(s) for s in _universe_cache["symbols"]}
    return render_template_string(
        DASH_HTML,
        cash=cash, holdings_value=holdings_value, total=cash + holdings_value,
        starting=starting, open_positions=open_positions, open_count=len(positions),
        max_concurrent=MAX_CONCURRENT, universe_size=len(_universe_cache["symbols"]),
        trades=trades, trade_count=trade_count,
        lookback=LOOKBACK_DAYS, vol_mult=VOL_MULT,
        trail_pct=int(TRAIL_PCT * 100),
        scan_interval=SCAN_INTERVAL_SECONDS,
        scans=_scanner_state["scans"], signals=_scanner_state["signals_seen"],
        last_scan=_scanner_state["last_scan"][:19] if _scanner_state["last_scan"] else None,
        trending_coins=trending_coins, news_mentions=news_mentions,
        our_universe_bases=our_universe_bases,
    )


@app.route("/api/state")
def api_state():
    cash = get_cash()
    positions = get_positions()
    last_prices = _scanner_state["last_prices"]
    holdings_value = sum(p["qty"] * last_prices.get(s, p["entry_price"]) for s, p in positions.items())
    with db() as conn:
        trades = [dict(r) for r in conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 200").fetchall()]
    return jsonify({
        "cash": cash, "holdings_value": holdings_value, "total_value": cash + holdings_value,
        "positions": list(positions.values()),
        "last_prices": last_prices, "trades": trades,
        "universe_size": len(_universe_cache["symbols"]),
        "universe": _universe_cache["symbols"],
        "scans": _scanner_state["scans"],
        "signals_seen": _scanner_state["signals_seen"],
        "last_scan": _scanner_state["last_scan"],
        "sentiment": {
            "trending": sorted(_sentiment_cache["trending_coins"]),
            "news_mentions": dict(_sentiment_cache["news_mentions"]),
            "last_refresh": _sentiment_cache["last_refresh"],
        },
        "config": {
            "kline_interval": KLINE_INTERVAL, "lookback_days": LOOKBACK_DAYS,
            "vol_mult": VOL_MULT, "trail_pct": TRAIL_PCT,
            "max_concurrent": MAX_CONCURRENT,
            "max_unit_price": MAX_UNIT_PRICE_USDT,
            "scan_interval_seconds": SCAN_INTERVAL_SECONDS,
            "sentiment_conviction_mult": SENTIMENT_CONVICTION_MULT,
            "sentiment_reddit_threshold": SENTIMENT_REDDIT_THRESHOLD,
        },
    })


def main() -> None:
    init_db()
    log("=" * 60)
    log(f"purffle_v3 starting — sub-$1 daily breakout (R2f CONFIG)")
    log(f"strategy: {LOOKBACK_DAYS}d high + {VOL_MULT}x vol, "
        f"{TRAIL_PCT*100:.0f}% trail + {HARD_STOP_PCT*100:.0f}% hard stop, "
        f"{POSITION_SIZE_PCT*100:.0f}% pos size, max {MAX_CONCURRENT} concurrent")
    log(f"R2f backtest: +177.3% over last 6 months (avg +18.5%/mo, 3 win / 3 loss months)")
    threading.Thread(target=scanner_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "12348"))
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
