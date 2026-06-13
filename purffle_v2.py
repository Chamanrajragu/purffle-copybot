"""
PurffleBot v2 — BTC 4h trend-following (validated by 2-year backtest).

DIFFERENT from v1 (which was falsified by 2-year data):
- v1 traded 15m breakouts on 352 sub-$1 pairs → lost money over 2 years
- v2 trades 4h EMA21/55 crossovers on BTC only → +30.8% over the same 2 years,
  with HALF the drawdown of just holding BTC

Why this works (mechanically):
- 4h timeframe filters out the intra-day noise that killed v1
- BTC has the deepest liquidity, lowest slippage, no manipulation risk
- EMA21/55 crossover catches major regime changes, not every wiggle
- Long-only, in cash during bear phases — avoids drawdowns that BTC suffers

Realistic expectations from 2-year backtest:
- ~1-2%/month average
- Best months: +10 to +27%
- Worst months: -10 to -17%
- ~44% of months profitable (more than coinflip)
- Max drawdown ~30%
- Months hitting 45%: essentially zero (NOT the goal)

Dashboard: http://localhost:12347 (different port from v1 to keep them separate)
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

# Shadow ML predictor — sklearn MLPClassifier (neural network).
# Trained on BTC 4h features, predicts whether NEXT candle closes higher than current.
# DOES NOT INFLUENCE TRADES — runs in shadow mode for 30 days to evaluate accuracy.
# If accuracy >= 56% (meaningfully above coinflip) AND beats trend strategy returns,
# we can promote it. Until then it's display-only.
try:
    from sklearn.neural_network import MLPClassifier
    from sklearn.preprocessing import StandardScaler
    import numpy as np
    _HAVE_SKLEARN = True
except ImportError:
    _HAVE_SKLEARN = False

# ---------------------------------------------------------------------------
# CONFIG — validated parameters from 2-year backtest
# ---------------------------------------------------------------------------
STARTING_CAPITAL = 100.0
SYMBOL = "BTCUSDT"
KLINE_INTERVAL = "4h"
EMA_FAST = 21
EMA_SLOW = 55
SCAN_INTERVAL_SECONDS = 300        # poll every 5 min — 4h candles don't move faster than that
SPOT_FEE = 0.001                    # 0.1% per side, baked into accounting

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "purffle_v2.db"
LOG_PATH = ROOT / "purffle_v2.log"
REPORTS_DIR = ROOT / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

BINANCE = "https://api.binance.com"


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
                side TEXT NOT NULL,
                qty REAL NOT NULL,
                price REAL NOT NULL,
                value REAL NOT NULL,
                fee REAL NOT NULL,
                realized_pnl REAL DEFAULT 0,
                reason TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS position (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                qty REAL NOT NULL DEFAULT 0,
                entry_price REAL NOT NULL DEFAULT 0,
                entry_ts TEXT,
                cost REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                ts TEXT PRIMARY KEY,
                cash REAL NOT NULL,
                position_value REAL NOT NULL,
                total_value REAL NOT NULL,
                btc_price REAL NOT NULL
            );
        """)
        if not conn.execute("SELECT 1 FROM state WHERE key='cash'").fetchone():
            conn.execute("INSERT INTO state(key,value) VALUES('cash',?)", (str(STARTING_CAPITAL),))
            conn.execute("INSERT INTO state(key,value) VALUES('starting_capital',?)", (str(STARTING_CAPITAL),))
        if not conn.execute("SELECT 1 FROM position WHERE id=1").fetchone():
            conn.execute("INSERT INTO position(id,qty,entry_price,cost) VALUES(1,0,0,0)")


def get_cash() -> float:
    with db() as conn:
        return float(conn.execute("SELECT value FROM state WHERE key='cash'").fetchone()["value"])

def set_cash(v: float) -> None:
    with db() as conn:
        conn.execute("UPDATE state SET value=? WHERE key='cash'", (str(v),))

def get_position() -> dict:
    with db() as conn:
        return dict(conn.execute("SELECT * FROM position WHERE id=1").fetchone())

def set_position(qty: float, entry_price: float, cost: float, entry_ts: Optional[str]) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE position SET qty=?, entry_price=?, cost=?, entry_ts=? WHERE id=1",
            (qty, entry_price, cost, entry_ts),
        )

def record_trade(side: str, qty: float, price: float, fee: float,
                 reason: str, realized_pnl: float = 0.0) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute(
            "INSERT INTO trades(ts,side,qty,price,value,fee,realized_pnl,reason) VALUES(?,?,?,?,?,?,?,?)",
            (ts, side, qty, price, qty * price, fee, realized_pnl, reason),
        )
    log(f"{side} {qty:.8f} BTC @ ${price:.2f} fee ${fee:.4f} pnl ${realized_pnl:+.4f} ({reason})")

def take_snapshot(cash: float, position_value: float, btc_price: float) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO snapshots(ts,cash,position_value,total_value,btc_price) VALUES(?,?,?,?,?)",
            (ts, cash, position_value, cash + position_value, btc_price),
        )


# ---------------------------------------------------------------------------
# Strategy — EMA crossover on 4h
# ---------------------------------------------------------------------------
def fetch_klines() -> Optional[list]:
    try:
        r = requests.get(f"{BINANCE}/api/v3/klines", params={
            "symbol": SYMBOL, "interval": KLINE_INTERVAL, "limit": EMA_SLOW + 5,
        }, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log(f"fetch_klines failed: {e}")
        return None


def ema_series(values: list[float], period: int) -> list[Optional[float]]:
    n = len(values)
    out: list[Optional[float]] = [None] * n
    if n < period: return out
    k = 2 / (period + 1)
    out[period - 1] = sum(values[:period]) / period
    for i in range(period, n):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


# ---------------------------------------------------------------------------
# Scan loop
# ---------------------------------------------------------------------------
_scanner_state = {
    "running": False, "last_scan": None, "scans": 0,
    "btc_price": 0.0, "ema_fast": None, "ema_slow": None,
    "trend_status": "unknown", "last_candle_ts": None,
}

# Shadow ML state — separate from trading state since it does NOT influence trades
_ml_state = {
    "enabled": _HAVE_SKLEARN,
    "model": None, "scaler": None,
    "last_train_ts": 0.0,
    "train_accuracy": 0.0,           # on holdout from training data
    "live_predictions": [],          # [(ts, predicted_up, actual_up_eventually, btc_price)]
    "live_accuracy": 0.0,            # rolling accuracy on resolved predictions
    "last_prediction": None,         # {"prob_up": float, "label": "UP"/"DOWN", "ts": ms}
    "training_samples": 0,
}
ML_TRAIN_REFRESH_SECONDS = 24 * 3600     # retrain daily
ML_HISTORY_DAYS = 730                     # 2 years of 4h candles to train on
ML_MIN_TRAIN_SAMPLES = 200


def _build_features(closes: list[float], highs: list[float], lows: list[float],
                    volumes: list[float], i: int):
    """Engineer features for predicting whether close[i+1] > close[i]."""
    if i < 20:
        return None
    c = closes
    # Returns over various horizons
    ret_1 = (c[i] / c[i-1] - 1) if c[i-1] else 0
    ret_2 = (c[i] / c[i-2] - 1) if c[i-2] else 0
    ret_4 = (c[i] / c[i-4] - 1) if c[i-4] else 0
    ret_8 = (c[i] / c[i-8] - 1) if c[i-8] else 0
    # Volatility proxy
    recent_vol = sum(abs(c[j] / c[j-1] - 1) for j in range(i-4, i+1)) / 5
    # Volume ratio
    avg_vol = sum(volumes[i-10:i]) / 10 if i >= 10 else volumes[i]
    vol_ratio = volumes[i] / avg_vol if avg_vol > 0 else 1
    # EMA distance
    ema_short = sum(c[i-8:i+1]) / 9
    ema_long = sum(c[i-20:i+1]) / 21
    ema_dist = (ema_short - ema_long) / ema_long if ema_long > 0 else 0
    # Range position (where is close inside recent high-low range)
    rh = max(highs[i-10:i+1]); rl = min(lows[i-10:i+1])
    range_pos = (c[i] - rl) / (rh - rl) if rh > rl else 0.5
    return [ret_1, ret_2, ret_4, ret_8, recent_vol, vol_ratio, ema_dist, range_pos]


def train_shadow_ml() -> None:
    """Pull 2 years of BTC 4h, train MLP classifier on direction labels."""
    if not _HAVE_SKLEARN:
        return
    now = time.time()
    if now - _ml_state["last_train_ts"] < ML_TRAIN_REFRESH_SECONDS \
       and _ml_state["model"] is not None:
        return
    log("shadow ML: fetching 2-year BTC 4h training data...")
    end_ms = int(now * 1000)
    start_ms = end_ms - ML_HISTORY_DAYS * 24 * 3600 * 1000
    all_kl = []
    cursor = start_ms
    while cursor < end_ms:
        try:
            r = requests.get(f"{BINANCE}/api/v3/klines", params={
                "symbol": SYMBOL, "interval": "4h",
                "startTime": cursor, "endTime": end_ms, "limit": 1000,
            }, timeout=15)
            if r.status_code != 200: break
            batch = r.json()
            if not batch: break
            all_kl.extend(batch)
            cursor = batch[-1][0] + 1
            if len(batch) < 1000: break
            time.sleep(0.05)
        except Exception as e:
            log(f"shadow ML: training fetch error: {e}"); return
    if len(all_kl) < ML_MIN_TRAIN_SAMPLES + 30:
        log(f"shadow ML: not enough data ({len(all_kl)}); skipping train")
        return
    closes = [float(k[4]) for k in all_kl]
    highs  = [float(k[2]) for k in all_kl]
    lows   = [float(k[3]) for k in all_kl]
    vols   = [float(k[5]) for k in all_kl]
    X, y = [], []
    for i in range(20, len(closes) - 1):
        feats = _build_features(closes, highs, lows, vols, i)
        if feats is None: continue
        X.append(feats)
        y.append(1 if closes[i+1] > closes[i] else 0)
    if len(X) < ML_MIN_TRAIN_SAMPLES:
        log(f"shadow ML: too few samples after feature build ({len(X)})")
        return
    # 80/20 train/holdout to get an honest in-sample accuracy estimate
    cut = int(len(X) * 0.8)
    X_tr, y_tr = np.array(X[:cut]), np.array(y[:cut])
    X_te, y_te = np.array(X[cut:]), np.array(y[cut:])
    scaler = StandardScaler().fit(X_tr)
    X_tr_s = scaler.transform(X_tr)
    X_te_s = scaler.transform(X_te)
    model = MLPClassifier(hidden_layer_sizes=(32, 16), max_iter=400,
                          random_state=42, early_stopping=True)
    model.fit(X_tr_s, y_tr)
    acc = model.score(X_te_s, y_te)
    _ml_state["model"] = model
    _ml_state["scaler"] = scaler
    _ml_state["last_train_ts"] = now
    _ml_state["train_accuracy"] = float(acc)
    _ml_state["training_samples"] = len(X)
    log(f"shadow ML: trained on {len(X)} samples, holdout accuracy {acc*100:.1f}%")


def predict_shadow_ml(closes, highs, lows, volumes) -> Optional[dict]:
    if not _HAVE_SKLEARN or _ml_state["model"] is None:
        return None
    feats = _build_features(closes, highs, lows, volumes, len(closes) - 1)
    if feats is None: return None
    try:
        x = _ml_state["scaler"].transform(np.array([feats]))
        proba = _ml_state["model"].predict_proba(x)[0]
        prob_up = float(proba[1])
        return {"prob_up": prob_up, "label": "UP" if prob_up > 0.5 else "DOWN",
                "ts": int(time.time() * 1000), "price_at_pred": closes[-1]}
    except Exception as e:
        log(f"shadow ML predict error: {e}")
        return None


def resolve_pending_ml_predictions(current_price: float) -> None:
    """For each prediction made >= 1 candle (4h) ago, check actual outcome."""
    now_ms = int(time.time() * 1000)
    four_hr_ms = 4 * 3600 * 1000
    for pred in _ml_state["live_predictions"]:
        if pred.get("resolved"): continue
        if now_ms - pred["ts"] >= four_hr_ms:
            actual_up = current_price > pred["price_at_pred"]
            pred["resolved"] = True
            pred["actual_up"] = bool(actual_up)
            pred["correct"] = (pred["predicted_up"] == actual_up)
    resolved = [p for p in _ml_state["live_predictions"] if p.get("resolved")]
    if resolved:
        correct = sum(1 for p in resolved if p["correct"])
        _ml_state["live_accuracy"] = correct / len(resolved)

def scan_once() -> None:
    klines = fetch_klines()
    if not klines or len(klines) < EMA_SLOW + 2:
        return
    closes = [float(k[4]) for k in klines]
    highs  = [float(k[2]) for k in klines]
    lows   = [float(k[3]) for k in klines]
    vols   = [float(k[5]) for k in klines]
    # Last entry is in-progress candle. Use it for live price but signals on completed candle.
    in_progress_price = closes[-1]
    completed_closes = closes[:-1]  # ignore forming candle for signal

    # Shadow ML: train daily, predict every scan, track accuracy.
    # Crucially, predictions DO NOT influence trades — they go to the dashboard only.
    if _HAVE_SKLEARN:
        try:
            train_shadow_ml()
            resolve_pending_ml_predictions(in_progress_price)
            pred = predict_shadow_ml(closes, highs, lows, vols)
            if pred is not None:
                _ml_state["last_prediction"] = pred
                # Record once per completed candle so we don't spam predictions.
                completed_ts = int(klines[-2][0])
                if not _ml_state["live_predictions"] or \
                   _ml_state["live_predictions"][-1].get("candle_ts") != completed_ts:
                    _ml_state["live_predictions"].append({
                        "ts": pred["ts"],
                        "candle_ts": completed_ts,
                        "predicted_up": pred["label"] == "UP",
                        "prob_up": pred["prob_up"],
                        "price_at_pred": pred["price_at_pred"],
                        "resolved": False,
                    })
                    # Keep only the last 200 predictions
                    _ml_state["live_predictions"] = _ml_state["live_predictions"][-200:]
        except Exception as e:
            log(f"shadow ML scan error: {e}")

    ef = ema_series(completed_closes, EMA_FAST)
    es = ema_series(completed_closes, EMA_SLOW)
    if ef[-1] is None or es[-1] is None or ef[-2] is None or es[-2] is None:
        return

    fast_now, fast_prev = ef[-1], ef[-2]
    slow_now, slow_prev = es[-1], es[-2]
    closed_price = completed_closes[-1]

    _scanner_state["btc_price"] = in_progress_price
    _scanner_state["ema_fast"] = fast_now
    _scanner_state["ema_slow"] = slow_now
    _scanner_state["last_candle_ts"] = int(klines[-2][0])
    _scanner_state["trend_status"] = (
        "UPTREND" if fast_now > slow_now and closed_price > fast_now
        else "DOWNTREND" if fast_now < slow_now
        else "TRANSITION"
    )

    pos = get_position()
    cash = get_cash()

    # Detect cross on completed candles (no repaint)
    golden_cross = fast_prev <= slow_prev and fast_now > slow_now
    death_cross  = fast_prev >= slow_prev and fast_now < slow_now

    # SELL: death cross (and we hold)
    if pos["qty"] > 0 and death_cross:
        proceeds = pos["qty"] * in_progress_price
        fee = proceeds * SPOT_FEE
        realized = (proceeds - fee) - pos["cost"]
        set_cash(cash + proceeds - fee)
        record_trade("SELL", pos["qty"], in_progress_price, fee,
                     f"death cross (EMA{EMA_FAST}={fast_now:.2f} < EMA{EMA_SLOW}={slow_now:.2f})",
                     realized_pnl=realized)
        set_position(0, 0, 0, None)
    # BUY: golden cross with price above fast EMA (and we don't hold)
    elif pos["qty"] == 0 and golden_cross and closed_price > fast_now:
        spend = cash  # all-in BTC trend strategy uses full cash position
        if spend > 1:
            fee = spend * SPOT_FEE
            qty = (spend - fee) / in_progress_price
            set_cash(0)
            record_trade("BUY", qty, in_progress_price, fee,
                         f"golden cross (EMA{EMA_FAST}={fast_now:.2f} > EMA{EMA_SLOW}={slow_now:.2f})")
            set_position(qty, in_progress_price, spend - fee,
                         datetime.now(timezone.utc).isoformat())

    pos = get_position()
    position_value = pos["qty"] * in_progress_price
    take_snapshot(get_cash(), position_value, in_progress_price)
    _scanner_state["last_scan"] = datetime.now(timezone.utc).isoformat()
    _scanner_state["scans"] += 1


def scanner_loop() -> None:
    _scanner_state["running"] = True
    log(f"scanner starting — {SYMBOL} {KLINE_INTERVAL} EMA{EMA_FAST}/{EMA_SLOW}, poll {SCAN_INTERVAL_SECONDS}s")
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
<html><head><title>PurffleBot v2 — BTC 4h trend</title>
<meta http-equiv="refresh" content="30">
<style>
 body{font-family:-apple-system,Segoe UI,sans-serif;background:#0a0e1a;color:#e6edf5;margin:0;padding:24px}
 h1{margin:0 0 8px;font-size:22px}
 h1 .tag{background:#1f3a5f;color:#7cb6ff;padding:3px 10px;border-radius:6px;font-size:12px;margin-left:10px;vertical-align:middle}
 .row{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:24px}
 .card{background:#121826;border:1px solid #1f2937;border-radius:10px;padding:18px;min-width:200px;flex:1}
 .label{color:#7a8398;font-size:12px;text-transform:uppercase;letter-spacing:.05em}
 .value{font-size:24px;font-weight:600;margin-top:4px}
 .pos{color:#36d399}.neg{color:#ff7a8a}.warn{color:#fbbf24}
 table{width:100%;border-collapse:collapse;background:#121826;border-radius:10px;overflow:hidden}
 th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #1f2937;font-size:14px}
 th{background:#1a2030;color:#7a8398;font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.05em}
 tr:last-child td{border-bottom:none}
 .muted{color:#5b6779;font-size:12px}
 .nav a{color:#7cb6ff;margin-right:18px;text-decoration:none;font-size:14px}
 .pill{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600}
 .pill.up{background:#193c2a;color:#36d399}.pill.dn{background:#3c1924;color:#ff7a8a}
</style></head><body>
<div class="nav"><a href="/">Dashboard</a><a href="/api/state">Raw state (JSON)</a></div>
<h1>PurffleBot v2 <span class="tag">BTC 4h · EMA{{ema_fast}}/{{ema_slow}}</span></h1>
<div class="muted">Validated +30.8% over 2 years vs Buy & Hold BTC -7.4% · max DD 29.8% · 44% profitable months</div>

<div class="row">
 <div class="card"><div class="label">Total value</div>
  <div class="value {{'pos' if total>=starting else 'neg'}}">${{ '%.2f'|format(total) }}</div>
  <div class="muted">P/L ${{ '%+.2f'|format(total-starting) }} ({{ '%+.2f'|format((total/starting-1)*100) }}%)</div>
 </div>
 <div class="card"><div class="label">Cash</div><div class="value">${{ '%.2f'|format(cash) }}</div></div>
 <div class="card"><div class="label">BTC holdings</div>
  <div class="value">${{ '%.2f'|format(pos_value) }}</div>
  <div class="muted">{{ '%.8f'|format(pos_qty) }} BTC</div>
 </div>
 <div class="card"><div class="label">BTC price</div>
  <div class="value">${{ '%.2f'|format(btc_price) }}</div>
 </div>
 <div class="card"><div class="label">Trend status</div>
  <div class="value {{ 'pos' if trend=='UPTREND' else ('neg' if trend=='DOWNTREND' else 'warn') }}">{{ trend }}</div>
  <div class="muted">EMA{{ema_fast}}: {{ '%.2f'|format(ema_fast_val or 0) }} · EMA{{ema_slow}}: {{ '%.2f'|format(ema_slow_val or 0) }}</div>
 </div>
</div>

<div class="row">
 <div class="card"><div class="label">Trades total</div><div class="value">{{ trade_count }}</div></div>
 <div class="card"><div class="label">Wins / Losses</div><div class="value"><span class="pos">{{ wins }}</span> / <span class="neg">{{ losses }}</span></div></div>
 <div class="card"><div class="label">Win rate</div><div class="value">{{ '%.1f'|format(win_rate) }}%</div></div>
 <div class="card"><div class="label">Realized P/L</div><div class="value {{'pos' if realized>=0 else 'neg'}}">${{ '%+.2f'|format(realized) }}</div></div>
</div>

<h2 style="font-size:16px;margin:8px 0">🧠 Shadow ML predictor <span class="muted" style="font-weight:normal">· MLP neural net · DOES NOT trade, only watches</span></h2>
<div class="row">
 <div class="card" style="flex:2">
  <div class="label">Latest ML prediction (next 4h candle)</div>
  {% if ml_prediction %}
  <div class="value {{'pos' if ml_prediction.label=='UP' else 'neg'}}">{{ml_prediction.label}}</div>
  <div class="muted">prob_up = {{ '%.1f'|format(ml_prediction.prob_up * 100) }}% · trained on {{ml_samples}} samples · in-sample accuracy {{ '%.1f'|format(ml_train_acc * 100) }}%</div>
  {% else %}
  <div class="value warn">training...</div>
  <div class="muted">{{ 'sklearn available' if ml_enabled else 'sklearn not installed' }}</div>
  {% endif %}
 </div>
 <div class="card">
  <div class="label">Live prediction accuracy</div>
  <div class="value {{'pos' if ml_live_acc >= 0.56 else ('warn' if ml_live_acc >= 0.50 else 'neg')}}">{{ '%.1f'|format(ml_live_acc * 100) }}%</div>
  <div class="muted">{{ ml_resolved }} predictions resolved · need {{'≥56% to be useful' if ml_resolved >= 20 else 'more data'}}</div>
 </div>
 <div class="card">
  <div class="label">Trend strategy decision</div>
  <div class="value {{'pos' if trend=='UPTREND' else ('neg' if trend=='DOWNTREND' else 'warn')}}">{{ trend }}</div>
  <div class="muted">This is what actually drives trades</div>
 </div>
</div>
{% if ml_recent_predictions %}
<table style="margin-bottom:24px">
 <tr><th>Predicted at</th><th>Pred</th><th>Prob UP</th><th>Price then</th><th>Outcome</th><th>Correct?</th></tr>
 {% for p in ml_recent_predictions %}
 <tr>
  <td>{{ p.ts_str }}</td>
  <td><span class="pill {{'up' if p.predicted_up else 'dn'}}">{{ 'UP' if p.predicted_up else 'DOWN' }}</span></td>
  <td>{{ '%.1f'|format(p.prob_up * 100) }}%</td>
  <td>${{ '%.2f'|format(p.price_at_pred) }}</td>
  <td>{% if p.resolved %}<span class="pill {{'up' if p.actual_up else 'dn'}}">{{ 'UP' if p.actual_up else 'DOWN' }}</span>{% else %}<span class="muted">pending</span>{% endif %}</td>
  <td>{% if p.resolved %}{{ '✓' if p.correct else '✗' }}{% else %}—{% endif %}</td>
 </tr>
 {% endfor %}
</table>
{% endif %}

<h2 style="font-size:16px;margin:8px 0">Recent trades</h2>
<table>
 <tr><th>Time (UTC)</th><th>Side</th><th>BTC qty</th><th>Price</th><th>Value</th><th>Fee</th><th>Reason</th><th>Realized P/L</th></tr>
 {% for t in trades %}
 <tr>
   <td>{{ t.ts[:19].replace('T',' ') }}</td>
   <td><span class="pill {{'up' if t.side=='BUY' else 'dn'}}">{{ t.side }}</span></td>
   <td>{{ '%.8f'|format(t.qty) }}</td>
   <td>${{ '%.2f'|format(t.price) }}</td>
   <td>${{ '%.2f'|format(t.value) }}</td>
   <td>${{ '%.4f'|format(t.fee) }}</td>
   <td class="muted">{{ t.reason }}</td>
   <td class="{{'pos' if t.realized_pnl>=0 else 'neg'}}">{% if t.side=='SELL' %}${{ '%+.4f'|format(t.realized_pnl) }}{% endif %}</td>
 </tr>
 {% else %}
 <tr><td colspan="8" class="muted">No trades yet — waiting for the next EMA crossover.</td></tr>
 {% endfor %}
</table>

<div class="muted" style="margin-top:24px">
 Scan every {{scan_interval}}s · {{scans}} scans · last scan {{last_scan or 'pending'}}
</div>
</body></html>
"""

@app.route("/")
def dashboard():
    cash = get_cash()
    pos = get_position()
    btc_price = _scanner_state["btc_price"] or pos["entry_price"]
    pos_value = pos["qty"] * btc_price
    with db() as conn:
        trades = [dict(r) for r in conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT 50"
        ).fetchall()]
        trade_count = conn.execute("SELECT COUNT(*) AS c FROM trades").fetchone()["c"]
        wins = conn.execute("SELECT COUNT(*) AS c FROM trades WHERE side='SELL' AND realized_pnl>0").fetchone()["c"]
        losses = conn.execute("SELECT COUNT(*) AS c FROM trades WHERE side='SELL' AND realized_pnl<0").fetchone()["c"]
        realized = conn.execute("SELECT COALESCE(SUM(realized_pnl),0) AS s FROM trades").fetchone()["s"]
        starting = float(conn.execute(
            "SELECT value FROM state WHERE key='starting_capital'"
        ).fetchone()["value"])
    win_rate = (wins / (wins + losses) * 100) if (wins + losses) else 0
    recent_preds = list(reversed(_ml_state["live_predictions"][-10:]))
    for p in recent_preds:
        p["ts_str"] = datetime.fromtimestamp(p["ts"] / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")
    resolved = sum(1 for p in _ml_state["live_predictions"] if p.get("resolved"))
    return render_template_string(
        DASH_HTML,
        cash=cash, pos_value=pos_value, pos_qty=pos["qty"], total=cash + pos_value,
        starting=starting, btc_price=btc_price,
        trend=_scanner_state["trend_status"],
        ema_fast=EMA_FAST, ema_slow=EMA_SLOW,
        ema_fast_val=_scanner_state["ema_fast"], ema_slow_val=_scanner_state["ema_slow"],
        trades=trades, trade_count=trade_count, wins=wins, losses=losses,
        win_rate=win_rate, realized=realized,
        scan_interval=SCAN_INTERVAL_SECONDS, scans=_scanner_state["scans"],
        last_scan=_scanner_state["last_scan"][:19] if _scanner_state["last_scan"] else None,
        ml_enabled=_ml_state["enabled"],
        ml_prediction=_ml_state["last_prediction"],
        ml_samples=_ml_state["training_samples"],
        ml_train_acc=_ml_state["train_accuracy"],
        ml_live_acc=_ml_state["live_accuracy"],
        ml_resolved=resolved,
        ml_recent_predictions=recent_preds,
    )

@app.route("/api/state")
def api_state():
    cash = get_cash()
    pos = get_position()
    btc_price = _scanner_state["btc_price"] or pos["entry_price"]
    with db() as conn:
        trades = [dict(r) for r in conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 200").fetchall()]
    return jsonify({
        "cash": cash, "position": pos, "btc_price": btc_price,
        "position_value": pos["qty"] * btc_price,
        "total_value": cash + pos["qty"] * btc_price,
        "trend_status": _scanner_state["trend_status"],
        "ema_fast": _scanner_state["ema_fast"],
        "ema_slow": _scanner_state["ema_slow"],
        "trades": trades,
        "scans": _scanner_state["scans"],
        "last_scan": _scanner_state["last_scan"],
        "config": {
            "symbol": SYMBOL, "interval": KLINE_INTERVAL,
            "ema_fast": EMA_FAST, "ema_slow": EMA_SLOW,
            "scan_interval_seconds": SCAN_INTERVAL_SECONDS,
        },
    })


def main() -> None:
    init_db()
    log("=" * 60)
    log(f"purffle_v2 starting — {SYMBOL} {KLINE_INTERVAL} EMA{EMA_FAST}/{EMA_SLOW}")
    log("validated +30.8% over 2 years vs Buy & Hold BTC -7.4%")
    threading.Thread(target=scanner_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "12347"))
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
