"""
Backtest: EMA bot vs PurffleBot on the last 30 days.

Both strategies replayed on the SAME data:
- Top 30 sub-$1 USDT spot pairs by current 24h volume
- 30 days of 15-minute candles
- 0.1% Binance spot fee per side (round-trip 0.2%)

Caveats (see report tail for the full list):
- EMA bot live uses 1-min candles; backtest uses 15-min for fair comparison
- 2 of Purffle's 7 live filters (order-book imbalance, aggressive buy flow) cannot
  be backtested because Binance doesn't expose historical order books or
  bulk-queryable trade tape. Backtest Purffle is therefore LESS strict than live.
- 30 days is one regime, not a forecast.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

BACKTEST_DAYS = 730                    # 2 years — full multi-regime test
TOP_N_SYMBOLS = 30
INITIAL_CAPITAL = 100.0
SPOT_FEE = 0.001                       # 0.1% per side

BINANCE = "https://api.binance.com"
FAPI = "https://fapi.binance.com"

# Exclusion lists from live bot
LEVERAGED_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
STABLECOIN_BASES = {
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "USDD",
    "PYUSD", "EURI", "USDS", "AEUR", "EUR", "GBP", "BRL", "TRY", "USD1",
}

ROOT = Path(__file__).resolve().parent
REPORTS_DIR = ROOT / "reports"
REPORTS_DIR.mkdir(exist_ok=True)


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Universe — same filter as live Purffle: sub-$1, top-N by 24h volume
# ---------------------------------------------------------------------------
def get_universe(n: int) -> list[str]:
    info = requests.get(f"{BINANCE}/api/v3/exchangeInfo", timeout=15).json()
    tradeable: set[str] = set()
    for s in info.get("symbols", []):
        if s.get("status") != "TRADING": continue
        if s.get("quoteAsset") != "USDT": continue
        if not s.get("isSpotTradingAllowed", False): continue
        sym = s["symbol"]
        if s.get("baseAsset", "") in STABLECOIN_BASES: continue
        if any(sym.endswith(suf) for suf in LEVERAGED_SUFFIXES): continue
        tradeable.add(sym)
    tickers = requests.get(f"{BINANCE}/api/v3/ticker/24hr", timeout=15).json()
    by_vol = []
    for t in tickers:
        if t.get("symbol", "") not in tradeable: continue
        try:
            vol = float(t.get("quoteVolume", 0))
            price = float(t.get("lastPrice", 0))
        except (TypeError, ValueError):
            continue
        if vol <= 0 or not (0 < price < 1.0):
            continue
        by_vol.append((t["symbol"], vol))
    by_vol.sort(key=lambda x: -x[1])
    return [s for s, _ in by_vol[:n]]


# ---------------------------------------------------------------------------
# History fetchers — handle 1000-per-call limit with pagination
# ---------------------------------------------------------------------------
def fetch_klines_range(symbol: str, interval: str,
                       start_ms: int, end_ms: int,
                       base: str = BINANCE) -> list[list]:
    out = []
    cursor = start_ms
    while cursor < end_ms:
        try:
            r = requests.get(
                f"{base}/api/v3/klines",
                params={"symbol": symbol, "interval": interval,
                        "startTime": cursor, "endTime": end_ms, "limit": 1000},
                timeout=15,
            )
            if r.status_code != 200:
                return out
            data = r.json()
        except Exception as e:
            log(f"  klines fetch error {symbol} {interval}: {e}")
            return out
        if not data:
            break
        out.extend(data)
        cursor = data[-1][0] + 1
        if len(data) < 1000:
            break
        time.sleep(0.04)
    return out


def fetch_funding_range(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    out = []
    cursor = start_ms
    while cursor < end_ms:
        try:
            r = requests.get(
                f"{FAPI}/fapi/v1/fundingRate",
                params={"symbol": symbol, "startTime": cursor,
                        "endTime": end_ms, "limit": 1000},
                timeout=15,
            )
            if r.status_code != 200:
                return out
            data = r.json()
        except Exception:
            return out
        if not data: break
        out.extend(data)
        cursor = int(data[-1]["fundingTime"]) + 1
        if len(data) < 1000: break
        time.sleep(0.04)
    return out


def fetch_ls_ratio_range(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    out = []
    cursor = start_ms
    while cursor < end_ms:
        try:
            r = requests.get(
                f"{FAPI}/futures/data/globalLongShortAccountRatio",
                params={"symbol": symbol, "period": "15m",
                        "startTime": cursor, "endTime": end_ms, "limit": 500},
                timeout=15,
            )
            if r.status_code != 200:
                return out
            data = r.json()
        except Exception:
            return out
        if not data: break
        out.extend(data)
        cursor = int(data[-1]["timestamp"]) + 1
        if len(data) < 500: break
        time.sleep(0.04)
    return out


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------
def ema_series(values: list[float], period: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    sma = sum(values[:period]) / period
    out[period - 1] = sma
    for i in range(period, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


def atr_series(highs: list[float], lows: list[float], closes: list[float],
               period: int = 14) -> list[Optional[float]]:
    """ATR (Average True Range) — adapts our stops to each coin's actual volatility.
    Returns a list aligned with closes; entries are None until enough history."""
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    if n < period + 1:
        return out
    tr = [0.0]
    for i in range(1, n):
        tr.append(max(highs[i] - lows[i],
                      abs(highs[i] - closes[i - 1]),
                      abs(lows[i] - closes[i - 1])))
    out[period] = sum(tr[1:period + 1]) / period
    for i in range(period + 1, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def stops_from_atr(atr_val: Optional[float], price: float,
                   hard_mult: float = 1.5, trail_mult: float = 2.5,
                   hard_floor: float = 0.02, hard_ceiling: float = 0.08,
                   trail_floor: float = 0.04, trail_ceiling: float = 0.12,
                   ) -> tuple[float, float]:
    """Returns (hard_stop_pct, trailing_stop_pct) clamped to safe ranges.
    Falls back to fixed values if ATR not available."""
    if atr_val is None or atr_val <= 0 or price <= 0:
        return 0.05, 0.08
    hard = max(hard_floor, min(hard_ceiling, hard_mult * atr_val / price))
    trail = max(trail_floor, min(trail_ceiling, trail_mult * atr_val / price))
    return hard, trail


def rsi_series(values: list[float], period: int = 14) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    if len(values) < period + 1:
        return out
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        ch = values[i] - values[i - 1]
        if ch >= 0: gains += ch
        else: losses -= ch
    avg_g = gains / period
    avg_l = losses / period
    out[period] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    for i in range(period + 1, len(values)):
        ch = values[i] - values[i - 1]
        g = max(ch, 0)
        l = -min(ch, 0)
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
        out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


# ---------------------------------------------------------------------------
# Shared backtest infrastructure
# ---------------------------------------------------------------------------
@dataclass
class Trade:
    ts: int
    symbol: str
    side: str       # BUY / SELL
    qty: float
    price: float
    realized_pnl: float = 0.0
    reason: str = ""


@dataclass
class BacktestResult:
    name: str
    final_value: float = 0.0
    final_cash: float = 0.0
    open_positions_value: float = 0.0
    realized_pnl: float = 0.0
    total_trades: int = 0
    closed_round_trips: int = 0
    wins: int = 0
    losses: int = 0
    fees_paid: float = 0.0
    max_drawdown_pct: float = 0.0          # worst peak-to-trough as a positive %
    max_drawdown_value: float = 0.0        # dollar value of that drawdown
    peak_value: float = 0.0                # highest portfolio value reached
    per_symbol_pnl: dict = field(default_factory=dict)
    trades: list[Trade] = field(default_factory=list)
    rejection_counts: dict = field(default_factory=dict)
    monthly_end_values: dict = field(default_factory=dict)   # "YYYY-MM" -> last MtM in that month


def _build_event_timeline(klines_15m_by_symbol: dict[str, list]) -> list[tuple[int, str, int]]:
    events = []
    for sym, kl in klines_15m_by_symbol.items():
        for idx, k in enumerate(kl):
            events.append((int(k[0]), sym, idx))
    events.sort()
    return events


# ---------------------------------------------------------------------------
# Strategy 1 — EMA bot (EMA 9/21 cross + RSI 14, TP +3% / SL -2%)
# ---------------------------------------------------------------------------
def backtest_ema(klines_15m: dict[str, list]) -> BacktestResult:
    POS_SIZE = 0.10
    MIN_TRADE = 5.0
    TP, SL = 0.03, 0.02
    R = BacktestResult(name="EMA bot")

    closes_by_sym, ema9_by, ema21_by, rsi_by = {}, {}, {}, {}
    for sym, kl in klines_15m.items():
        closes = [float(k[4]) for k in kl]
        closes_by_sym[sym] = closes
        ema9_by[sym] = ema_series(closes, 9)
        ema21_by[sym] = ema_series(closes, 21)
        rsi_by[sym] = rsi_series(closes, 14)

    cash = INITIAL_CAPITAL
    positions: dict[str, dict] = {}
    fees = 0.0
    R.per_symbol_pnl = {s: 0.0 for s in klines_15m}

    for ts, sym, i in _build_event_timeline(klines_15m):
        closes = closes_by_sym[sym]
        if i < 22: continue
        e9_now, e9_prev = ema9_by[sym][i], ema9_by[sym][i - 1]
        e21_now, e21_prev = ema21_by[sym][i], ema21_by[sym][i - 1]
        rsi_now = rsi_by[sym][i]
        if None in (e9_now, e9_prev, e21_now, e21_prev, rsi_now):
            continue
        price = closes[i]
        golden = e9_prev <= e21_prev and e9_now > e21_now
        death  = e9_prev >= e21_prev and e9_now < e21_now

        pos = positions.get(sym)
        if pos:
            change = (price - pos["avg_price"]) / pos["avg_price"]
            exit_reason = None
            if change >= TP:           exit_reason = f"TP +{change*100:.1f}%"
            elif change <= -SL:        exit_reason = f"SL {change*100:.1f}%"
            elif death:                exit_reason = "EMA death cross"
            elif rsi_now > 70:         exit_reason = f"RSI {rsi_now:.0f} overbought"
            if exit_reason:
                proceeds = pos["qty"] * price
                fee = proceeds * SPOT_FEE
                fees += fee
                pnl = proceeds - fee - pos["cost"]
                cash += proceeds - fee
                R.per_symbol_pnl[sym] += pnl
                R.trades.append(Trade(ts, sym, "SELL", pos["qty"], price, pnl, exit_reason))
                R.closed_round_trips += 1
                if pnl > 0: R.wins += 1
                else:       R.losses += 1
                del positions[sym]
                continue
        else:
            if golden or rsi_now < 30:
                spend = max(MIN_TRADE, cash * POS_SIZE)
                if spend <= cash and spend >= MIN_TRADE:
                    fee = spend * SPOT_FEE
                    fees += fee
                    qty = (spend - fee) / price
                    cash -= spend
                    positions[sym] = {"qty": qty, "avg_price": price, "cost": spend - fee}
                    reason = "EMA golden cross" if golden else f"RSI {rsi_now:.0f} oversold"
                    R.trades.append(Trade(ts, sym, "BUY", qty, price, 0, reason))

    # Mark to market on remaining open positions
    open_val = 0.0
    for sym, pos in positions.items():
        last_price = closes_by_sym[sym][-1]
        open_val += pos["qty"] * last_price
        R.per_symbol_pnl[sym] += pos["qty"] * last_price - pos["cost"]

    R.final_cash = cash
    R.open_positions_value = open_val
    R.final_value = cash + open_val
    R.realized_pnl = sum(R.per_symbol_pnl.values()) - (open_val - sum(p["cost"] for p in positions.values()))
    R.fees_paid = fees
    R.total_trades = sum(1 for t in R.trades if t.side == "BUY")
    return R


# ---------------------------------------------------------------------------
# Strategy 2 — Purffle (15m breakout + volume spike + 5 confirmation gates)
# ---------------------------------------------------------------------------
def backtest_purffle(
    klines_15m: dict[str, list],
    klines_1h: dict[str, list],
    btc_1h: list,
    funding: dict[str, list[dict]],
    ls_ratio: dict[str, list[dict]],
    use_atr_stops: bool = True,
    use_pyramid: bool = True,
    hard_atr_mult: float = 1.5,
    trail_atr_mult: float = 2.5,
    use_chase_guard: bool = True,
    use_partial_profit: bool = False,
    partial_tp_pct: float = 0.20,
    partial_fraction: float = 0.5,
    use_partial_2: bool = False,            # second profit tier
    partial_2_tp_pct: float = 0.50,
    partial_2_fraction: float = 0.5,        # of REMAINING after first partial
    use_breakout_stop: bool = False,
    breakout_buffer: float = 0.005,
    use_conviction_sizing: bool = False,    # bigger size on stronger vol spikes
    conviction_vol_mult: float = 5.0,
    conviction_pos_size: float = 0.25,
    base_pos_size: float = 0.15,
    min_vol_mult: float = 3.0,              # override the default 3x volume threshold
    use_dynamic_sizing: bool = False,       # adapt size to recent win rate (Kelly-ish)
    dynamic_window: int = 20,
    dynamic_min_size: float = 0.10,
    dynamic_max_size: float = 0.75,
    name: str = "PurffleBot",
) -> BacktestResult:
    LOOKBACK = 20
    VOL_MULT = min_vol_mult
    MIN_TRADE = 5.0
    HARD_SL_FIXED = 0.05
    TRAIL_FIXED = 0.08
    MAX_HOLD_MS = 4 * 3600 * 1000
    MAX_24H_CHANGE = 30.0
    MAX_NEAR_HIGH = 0.02
    MAX_FUNDING = 0.001
    MAX_LS = 3.0
    BTC_EMA = 50
    HTF_FAST, HTF_SLOW = 21, 50

    # Pyramiding config — initial 15%, then smaller tranches as we scale into winners.
    # Each tranche fires when price advances PYRAMID_THRESHOLD since the previous tranche.
    TRANCHE_SIZES = [0.15, 0.10, 0.05]
    MAX_TRANCHES = len(TRANCHE_SIZES)
    PYRAMID_THRESHOLD = 0.10

    R = BacktestResult(name=name)
    R.rejection_counts = {k: 0 for k in
        ["btc_regime", "htf_trend", "chase_guard", "funding_rate", "ls_ratio"]}
    pyramid_hits = 0

    closes_15m_by = {s: [float(k[4]) for k in kl] for s, kl in klines_15m.items()}
    highs_15m_by  = {s: [float(k[2]) for k in kl] for s, kl in klines_15m.items()}
    lows_15m_by   = {s: [float(k[3]) for k in kl] for s, kl in klines_15m.items()}
    vol_15m_by    = {s: [float(k[5]) for k in kl] for s, kl in klines_15m.items()}
    ts_15m_by     = {s: [int(k[0]) for k in kl] for s, kl in klines_15m.items()}
    atr_15m_by    = {s: atr_series(highs_15m_by[s], lows_15m_by[s], closes_15m_by[s], 14)
                     for s in klines_15m}

    # Pre-compute 1h trend lookup: per-symbol sorted (ts, ema_fast_ok)
    htf_trend_by = {}
    for sym, kl1h in klines_1h.items():
        closes_1h = [float(k[4]) for k in kl1h]
        ts_1h = [int(k[0]) for k in kl1h]
        efast = ema_series(closes_1h, HTF_FAST)
        eslow = ema_series(closes_1h, HTF_SLOW)
        htf_trend_by[sym] = [(ts_1h[i], efast[i] is not None and eslow[i] is not None
                              and efast[i] > eslow[i] and closes_1h[i] > efast[i])
                             for i in range(len(ts_1h))]

    # BTC regime lookup
    btc_closes = [float(k[4]) for k in btc_1h]
    btc_ts = [int(k[0]) for k in btc_1h]
    btc_ema = ema_series(btc_closes, BTC_EMA)
    btc_regime_at = [(btc_ts[i], btc_ema[i] is not None and btc_closes[i] > btc_ema[i])
                     for i in range(len(btc_ts))]

    def latest_at(table, ts):
        # binary search would be faster; for 30 days at 1h granularity = ~720 entries, linear is fine
        last = None
        for t, v in table:
            if t <= ts:
                last = v
            else:
                break
        return last

    cash = INITIAL_CAPITAL
    positions: dict[str, dict] = {}
    fees = 0.0
    R.per_symbol_pnl = {s: 0.0 for s in klines_15m}
    # Drawdown tracking — approximate mark-to-market on every event using
    # the latest price we've seen for each symbol.
    peak_value = INITIAL_CAPITAL
    max_dd_pct = 0.0
    max_dd_value = 0.0
    last_known_price: dict[str, float] = {}
    # Dynamic sizing: track outcomes of recently closed round-trips.
    recent_closed_pnl: list[float] = []

    def dynamic_size() -> float:
        """Scale position size by recent win rate. Hot streak -> bigger, cold -> smaller."""
        if len(recent_closed_pnl) < dynamic_window:
            return base_pos_size
        last = recent_closed_pnl[-dynamic_window:]
        wins = sum(1 for x in last if x > 0)
        wr = wins / dynamic_window
        # Map 30% wr -> min_size, 70% wr -> max_size, linear between
        if wr <= 0.30: return dynamic_min_size
        if wr >= 0.70: return dynamic_max_size
        return dynamic_min_size + (dynamic_max_size - dynamic_min_size) * (wr - 0.30) / 0.40

    for ts, sym, i in _build_event_timeline(klines_15m):
        closes = closes_15m_by[sym]
        if i >= len(closes): continue
        price = closes[i]
        last_known_price[sym] = price

        # Mark-to-market every event — runs before any branch so drawdown is tracked
        # even on hold/skip events.
        mtm = cash + sum(p["qty"] * last_known_price.get(s, p["avg_price"])
                         for s, p in positions.items())
        if mtm > peak_value: peak_value = mtm
        dd = (peak_value - mtm) / peak_value if peak_value > 0 else 0.0
        if dd > max_dd_pct:
            max_dd_pct = dd
            max_dd_value = peak_value - mtm
        # Track end-of-month portfolio value (last MtM seen for each YYYY-MM).
        ym = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m")
        R.monthly_end_values[ym] = mtm

        # Manage open position first — TP/SL/trail/time-out, then maybe pyramid
        pos = positions.get(sym)
        if pos:
            if price > pos["peak"]:
                pos["peak"] = price
            # Stops — ATR-derived if enabled, else fixed
            if use_atr_stops:
                hard_pct, trail_pct = stops_from_atr(
                    atr_15m_by[sym][i], pos["avg_price"],
                    hard_mult=hard_atr_mult, trail_mult=trail_atr_mult,
                )
            else:
                hard_pct, trail_pct = HARD_SL_FIXED, TRAIL_FIXED
            from_entry = (price - pos["avg_price"]) / pos["avg_price"]
            from_peak  = (price - pos["peak"]) / pos["peak"]
            age = ts - pos["entry_ts"]

            # Partial profit-taking: tier 1 — sell partial_fraction at +partial_tp_pct.
            if use_partial_profit and not pos.get("partial_taken", False) \
               and from_entry >= partial_tp_pct:
                sell_qty = pos["qty"] * partial_fraction
                sell_cost = pos["cost"] * partial_fraction
                proceeds = sell_qty * price
                fee = proceeds * SPOT_FEE
                fees += fee
                pnl = proceeds - fee - sell_cost
                cash += proceeds - fee
                R.per_symbol_pnl[sym] += pnl
                R.trades.append(Trade(ts, sym, "SELL", sell_qty, price, pnl,
                                      f"partial-1 +{from_entry*100:.1f}% ({int(partial_fraction*100)}% off)"))
                pos["qty"] -= sell_qty
                pos["cost"] -= sell_cost
                pos["partial_taken"] = True
            # Partial profit-taking: tier 2 — at +partial_2_tp_pct, sell partial_2_fraction
            # of what remains. Only fires after tier 1 has already taken.
            if use_partial_2 and pos.get("partial_taken", False) \
               and not pos.get("partial_2_taken", False) \
               and from_entry >= partial_2_tp_pct:
                sell_qty = pos["qty"] * partial_2_fraction
                sell_cost = pos["cost"] * partial_2_fraction
                proceeds = sell_qty * price
                fee = proceeds * SPOT_FEE
                fees += fee
                pnl = proceeds - fee - sell_cost
                cash += proceeds - fee
                R.per_symbol_pnl[sym] += pnl
                R.trades.append(Trade(ts, sym, "SELL", sell_qty, price, pnl,
                                      f"partial-2 +{from_entry*100:.1f}% ({int(partial_2_fraction*100)}% of rest)"))
                pos["qty"] -= sell_qty
                pos["cost"] -= sell_cost
                pos["partial_2_taken"] = True

            exit_reason = None
            if from_entry <= -hard_pct:
                exit_reason = f"hard-stop {from_entry*100:+.1f}% (cap {hard_pct*100:.1f}%)"
            # Breakout-level structural stop — fires if price falls back below the
            # level we just broke (the trade premise has failed). Uses TIGHTER of
            # ATR stop and breakout-level stop.
            elif use_breakout_stop and "breakout_level" in pos \
                 and price <= pos["breakout_level"] * (1 - breakout_buffer):
                exit_reason = f"breakout-failed (level ${pos['breakout_level']:.6g}, buffer {breakout_buffer*100:.1f}%)"
            elif pos["peak"] > pos["avg_price"] and from_peak <= -trail_pct:
                exit_reason = f"trail {from_peak*100:+.1f}% (cap {trail_pct*100:.1f}%)"
            elif age > MAX_HOLD_MS and price < pos["peak"]:
                exit_reason = f"time-out {age/3600000:.1f}h"
            if exit_reason:
                proceeds = pos["qty"] * price
                fee = proceeds * SPOT_FEE
                fees += fee
                pnl = proceeds - fee - pos["cost"]
                cash += proceeds - fee
                R.per_symbol_pnl[sym] += pnl
                R.trades.append(Trade(ts, sym, "SELL", pos["qty"], price, pnl, exit_reason))
                R.closed_round_trips += 1
                if pnl > 0: R.wins += 1
                else:       R.losses += 1
                recent_closed_pnl.append(pnl)
                del positions[sym]
                continue
            # Pyramid check — add to winners that keep winning
            if use_pyramid and pos["tranches"] < MAX_TRANCHES and \
               price >= pos["last_tranche_price"] * (1 + PYRAMID_THRESHOLD):
                # Sanity check: BTC regime + HTF trend must still be ok at pyramid time
                if latest_at(btc_regime_at, ts) and latest_at(htf_trend_by.get(sym, []), ts):
                    next_size = TRANCHE_SIZES[pos["tranches"]]
                    spend = max(MIN_TRADE, cash * next_size)
                    if spend <= cash and spend >= MIN_TRADE:
                        fee = spend * SPOT_FEE
                        fees += fee
                        new_qty = (spend - fee) / price
                        cash -= spend
                        total_qty = pos["qty"] + new_qty
                        total_cost = pos["cost"] + (spend - fee)
                        pos["qty"] = total_qty
                        pos["cost"] = total_cost
                        pos["avg_price"] = total_cost / total_qty
                        pos["tranches"] += 1
                        pos["last_tranche_price"] = price
                        pyramid_hits += 1
                        R.trades.append(Trade(ts, sym, "BUY", new_qty, price, 0,
                                              f"pyramid #{pos['tranches']}"))
            continue  # don't BUY same symbol same candle as sell

        # Primary signal — need LOOKBACK candles before current
        if i < LOOKBACK + 1: continue
        lookback_highs = highs_15m_by[sym][i - LOOKBACK: i]
        lookback_vols  = vol_15m_by[sym][i - LOOKBACK: i]
        cur_vol = vol_15m_by[sym][i]
        cur_close = closes[i]
        avg_vol = sum(lookback_vols) / LOOKBACK
        if avg_vol <= 0: continue
        if not (cur_close > max(lookback_highs) and cur_vol >= VOL_MULT * avg_vol):
            continue

        # ---- Confirmation gates (5 of 7; orderbook + buy flow not backtestable) ----
        if not latest_at(btc_regime_at, ts):
            R.rejection_counts["btc_regime"] += 1
            continue
        htf_ok = latest_at(htf_trend_by.get(sym, []), ts)
        if not htf_ok:
            R.rejection_counts["htf_trend"] += 1
            continue
        # chase guard — rolling 24h = 96 prior 15m candles
        if use_chase_guard and i >= 96:
            change_24h_pct = (closes[i] / closes[i - 96] - 1) * 100
            high_24h = max(highs_15m_by[sym][i - 96: i + 1])
            near_high = (high_24h - closes[i]) / high_24h
            if change_24h_pct > MAX_24H_CHANGE or near_high < MAX_NEAR_HIGH:
                R.rejection_counts["chase_guard"] += 1
                continue
        # funding rate
        fund_table = funding.get(sym, [])
        latest_fund = None
        for f in fund_table:
            if int(f["fundingTime"]) <= ts:
                latest_fund = float(f["fundingRate"])
            else:
                break
        if latest_fund is not None and abs(latest_fund) > MAX_FUNDING:
            R.rejection_counts["funding_rate"] += 1
            continue
        # L/S ratio
        ls_table = ls_ratio.get(sym, [])
        latest_ls = None
        for r in ls_table:
            if int(r["timestamp"]) <= ts:
                latest_ls = float(r["longShortRatio"])
            else:
                break
        if latest_ls is not None and latest_ls > MAX_LS:
            R.rejection_counts["ls_ratio"] += 1
            continue

        # Sizing — dynamic (win-rate-adaptive) overrides everything, else conviction, else base.
        vol_ratio_actual = cur_vol / avg_vol
        if use_dynamic_sizing:
            pos_size = dynamic_size()
            size_tag = f"DYNAMIC {pos_size*100:.0f}%"
        elif use_conviction_sizing and vol_ratio_actual >= conviction_vol_mult:
            pos_size = conviction_pos_size
            size_tag = f"CONVICTION {vol_ratio_actual:.1f}x"
        else:
            pos_size = base_pos_size
            size_tag = f"std {vol_ratio_actual:.1f}x"

        # All gates passed — open initial tranche
        spend = max(MIN_TRADE, cash * pos_size)
        if spend > cash or spend < MIN_TRADE: continue
        fee = spend * SPOT_FEE
        fees += fee
        qty = (spend - fee) / price
        cash -= spend
        breakout_level = max(lookback_highs)
        positions[sym] = {"qty": qty, "avg_price": price, "cost": spend - fee,
                          "peak": price, "entry_ts": ts,
                          "tranches": 1, "last_tranche_price": price,
                          "partial_taken": False, "partial_2_taken": False,
                          "breakout_level": breakout_level}
        R.trades.append(Trade(ts, sym, "BUY", qty, price, 0, f"breakout+5gates ({size_tag})"))

    open_val = 0.0
    for sym, pos in positions.items():
        last_price = closes_15m_by[sym][-1]
        open_val += pos["qty"] * last_price
        R.per_symbol_pnl[sym] += pos["qty"] * last_price - pos["cost"]

    R.final_cash = cash
    R.open_positions_value = open_val
    R.final_value = cash + open_val
    R.realized_pnl = sum(R.per_symbol_pnl.values()) - (open_val - sum(p["cost"] for p in positions.values()))
    R.fees_paid = fees
    R.total_trades = sum(1 for t in R.trades if t.side == "BUY")
    R.max_drawdown_pct = max_dd_pct * 100
    R.max_drawdown_value = max_dd_value
    R.peak_value = peak_value
    return R


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------
def fmt_pct(v): return f"{v:+.2f}%" if v is not None else "—"
def fmt_usd(v): return f"${v:+.2f}"

def write_report(symbols, ema_res, purf_res, start_dt, end_dt):
    out = REPORTS_DIR / f"backtest-{end_dt.strftime('%Y-%m-%d')}.md"

    def stats(R: BacktestResult):
        total_pnl = R.final_value - INITIAL_CAPITAL
        roi = (R.final_value / INITIAL_CAPITAL - 1) * 100
        wr = (R.wins / R.closed_round_trips * 100) if R.closed_round_trips else 0
        avg_pnl = R.realized_pnl / R.closed_round_trips if R.closed_round_trips else 0
        return {
            "final_value": R.final_value,
            "total_pnl": total_pnl,
            "roi": roi,
            "trades_opened": R.total_trades,
            "trades_closed": R.closed_round_trips,
            "wins": R.wins, "losses": R.losses,
            "win_rate": wr,
            "avg_pnl_per_trade": avg_pnl,
            "fees_paid": R.fees_paid,
            "open_positions_value": R.open_positions_value,
            "open_position_count": len([1 for s, p in R.per_symbol_pnl.items() if p != 0]) - R.closed_round_trips,
        }

    e, p = stats(ema_res), stats(purf_res)
    winner = "EMA bot" if e["final_value"] > p["final_value"] else "PurffleBot"
    if abs(e["final_value"] - p["final_value"]) < 0.50:
        winner = "tie (within $0.50)"

    purf_top = sorted(purf_res.per_symbol_pnl.items(), key=lambda x: -x[1])[:5]
    purf_bot = sorted(purf_res.per_symbol_pnl.items(), key=lambda x: x[1])[:5]
    ema_top  = sorted(ema_res.per_symbol_pnl.items(),  key=lambda x: -x[1])[:5]
    ema_bot  = sorted(ema_res.per_symbol_pnl.items(),  key=lambda x: x[1])[:5]

    lines = []
    lines.append(f"# Backtest — EMA bot vs PurffleBot")
    lines.append("")
    lines.append(f"- Period: **{start_dt.strftime('%Y-%m-%d')} -> {end_dt.strftime('%Y-%m-%d')}** (last {BACKTEST_DAYS} days)")
    lines.append(f"- Universe: **{len(symbols)} sub-$1 USDT pairs** (top by 24h volume): {', '.join(symbols)}")
    lines.append(f"- Starting capital: **${INITIAL_CAPITAL:.2f}** for each bot, fees included (0.1% per side)")
    lines.append("")
    lines.append(f"## Winner: **{winner}**")
    lines.append("")
    lines.append("## Headline numbers")
    lines.append("")
    lines.append("| Metric | EMA bot | PurffleBot |")
    lines.append("|---|---:|---:|")
    lines.append(f"| Final portfolio value | ${e['final_value']:.2f} | ${p['final_value']:.2f} |")
    lines.append(f"| Total P/L | {fmt_usd(e['total_pnl'])} | {fmt_usd(p['total_pnl'])} |")
    lines.append(f"| ROI | {fmt_pct(e['roi'])} | {fmt_pct(p['roi'])} |")
    lines.append(f"| Trades opened | {e['trades_opened']} | {p['trades_opened']} |")
    lines.append(f"| Trades closed (round-trips) | {e['trades_closed']} | {p['trades_closed']} |")
    lines.append(f"| Wins / Losses | {e['wins']} / {e['losses']} | {p['wins']} / {p['losses']} |")
    lines.append(f"| Win rate | {e['win_rate']:.1f}% | {p['win_rate']:.1f}% |")
    lines.append(f"| Avg P/L per closed trade | ${e['avg_pnl_per_trade']:+.3f} | ${p['avg_pnl_per_trade']:+.3f} |")
    lines.append(f"| Fees paid | ${e['fees_paid']:.2f} | ${p['fees_paid']:.2f} |")
    lines.append("")

    lines.append("## Purffle filter rejections (where signals died)")
    lines.append("")
    lines.append("| Filter | Rejected |")
    lines.append("|---|---:|")
    for f, n in purf_res.rejection_counts.items():
        lines.append(f"| {f} | {n} |")
    lines.append("")

    lines.append("## EMA bot — best & worst symbols")
    lines.append("")
    lines.append("| Best | P/L | Worst | P/L |")
    lines.append("|---|---:|---|---:|")
    for (gs, gp), (bs, bp) in zip(ema_top, ema_bot):
        lines.append(f"| {gs} | {fmt_usd(gp)} | {bs} | {fmt_usd(bp)} |")
    lines.append("")
    lines.append("## Purffle — best & worst symbols")
    lines.append("")
    lines.append("| Best | P/L | Worst | P/L |")
    lines.append("|---|---:|---|---:|")
    for (gs, gp), (bs, bp) in zip(purf_top, purf_bot):
        lines.append(f"| {gs} | {fmt_usd(gp)} | {bs} | {fmt_usd(bp)} |")
    lines.append("")

    lines.append("## Caveats — read these")
    lines.append("")
    lines.append("- **2 of Purffle's 7 live filters were NOT backtested** (order-book imbalance, "
                 "aggressive buy flow) because Binance doesn't expose historical order books or "
                 "bulk-queryable trade tape. Live Purffle is therefore STRICTER than this backtest "
                 "— expect fewer trades but higher quality in production.")
    lines.append("- **EMA bot was designed for 1-min candles**; backtest uses 15-min for apples-to-apples "
                 "with Purffle. EMA bot will produce more (and noisier) signals on its native 1-min timeframe.")
    lines.append("- **No slippage modeled.** Real low-volume sub-$1 pairs have 0.3–1% spreads. "
                 "Subtract another ~0.3% per round-trip from each bot's ROI for a realistic floor.")
    lines.append("- **30 days is one market regime.** A bull run, choppy month, or sharp drawdown "
                 "produces very different numbers. Not predictive.")
    lines.append("- **Survivorship bias.** Universe = current top-30 by 24h volume. 30 days ago a "
                 "different set of coins were popular. Backtest only sees today's survivors.")
    lines.append("- **Fees included** at Binance spot maker/taker 0.1% per side = 0.2% per round trip.")
    lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
CACHE_PATH = ROOT / f"backtest_cache_{BACKTEST_DAYS}d.json"

def main():
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - BACKTEST_DAYS * 24 * 60 * 60 * 1000
    start_dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)

    log(f"backtest window: {start_dt.date()} -> {end_dt.date()}")

    # Try to reuse cached data if it's recent (< 6 hours old)
    use_cache = False
    if CACHE_PATH.exists():
        cache_age = time.time() - CACHE_PATH.stat().st_mtime
        if cache_age < 6 * 3600:
            log(f"loading cached data (age {cache_age/60:.0f} min)...")
            cache = json.loads(CACHE_PATH.read_text())
            symbols = cache["symbols"]
            btc_1h = cache["btc_1h"]
            klines_15m = cache["klines_15m"]
            klines_1h = cache["klines_1h"]
            funding = cache["funding"]
            ls_ratio = cache["ls_ratio"]
            use_cache = True
            log(f"loaded cache: {len(symbols)} symbols")

    if not use_cache:
        log("discovering sub-$1 universe...")
        symbols = get_universe(TOP_N_SYMBOLS)
        log(f"universe ({len(symbols)}): {', '.join(symbols)}")

        log("fetching BTC 1h klines (for regime gate)...")
        btc_1h = fetch_klines_range("BTCUSDT", "1h", start_ms, end_ms)
        log(f"  BTC 1h candles: {len(btc_1h)}")

        klines_15m, klines_1h, funding, ls_ratio = {}, {}, {}, {}
        for n, sym in enumerate(symbols, 1):
            log(f"[{n}/{len(symbols)}] {sym}: 15m...")
            klines_15m[sym] = fetch_klines_range(sym, "15m", start_ms, end_ms)
            log(f"  {sym}: 1h...")
            klines_1h[sym]  = fetch_klines_range(sym, "1h",  start_ms, end_ms)
            log(f"  {sym}: funding history...")
            funding[sym]    = fetch_funding_range(sym, start_ms, end_ms)
            log(f"  {sym}: L/S ratio history...")
            ls_ratio[sym]   = fetch_ls_ratio_range(sym, start_ms, end_ms)
            log(f"  -> 15m={len(klines_15m[sym])} 1h={len(klines_1h[sym])} "
                f"funding={len(funding[sym])} ls={len(ls_ratio[sym])}")
        log(f"caching to {CACHE_PATH}")
        CACHE_PATH.write_text(json.dumps({
            "symbols": symbols, "btc_1h": btc_1h,
            "klines_15m": klines_15m, "klines_1h": klines_1h,
            "funding": funding, "ls_ratio": ls_ratio,
        }))

    log(f"running Purffle variants on {BACKTEST_DAYS} days...")
    # ATR only (no partial), since partial hurt over 6 months
    ATR = {"use_atr_stops": True}
    ATR_P = {"use_atr_stops": True, "use_partial_profit": True, "partial_tp_pct": 0.20}
    variants = [
        ("Stock Purffle (15% size, with partial)",   {**ATR_P, "base_pos_size": 0.15}),
        ("15% size, NO partial",                     {**ATR,   "base_pos_size": 0.15}),
        ("25% size, NO partial",                     {**ATR,   "base_pos_size": 0.25}),
        ("30% size, NO partial",                     {**ATR,   "base_pos_size": 0.30}),
        ("40% size, NO partial",                     {**ATR,   "base_pos_size": 0.40}),
        ("50% size, NO partial",                     {**ATR,   "base_pos_size": 0.50}),
        ("60% size, NO partial",                     {**ATR,   "base_pos_size": 0.60}),
        ("60% size, WITH partial (CURRENT LIVE)",    {**ATR_P, "base_pos_size": 0.60}),
        ("75% size, NO partial",                     {**ATR,   "base_pos_size": 0.75}),
        ("100% all-in, NO partial",                  {**ATR,   "base_pos_size": 1.00}),
        ("DYNAMIC (10-75%, adapts to win rate)",     {**ATR,   "use_dynamic_sizing": True}),
        ("DYNAMIC + partial profit",                 {**ATR_P, "use_dynamic_sizing": True}),
    ]
    results = []
    for name, kwargs in variants:
        log(f"  - {name} ...")
        r = backtest_purffle(klines_15m, klines_1h, btc_1h, funding, ls_ratio,
                             use_pyramid=False, use_chase_guard=True,
                             name=name, **kwargs)
        results.append(r)
        log(f"    ${r.final_value:7.2f}  buys:{r.total_trades:3d}  closed:{r.closed_round_trips:3d}  "
            f"W/L:{r.wins}/{r.losses}  win_rate:{(r.wins/r.closed_round_trips*100 if r.closed_round_trips else 0):.1f}%")

    # Compute per-month returns for each variant
    def monthly_returns(R):
        months_sorted = sorted(R.monthly_end_values.items())
        if not months_sorted:
            return {}
        out_returns = {}
        prev = INITIAL_CAPITAL
        for ym, end_val in months_sorted:
            out_returns[ym] = (end_val / prev - 1) * 100
            prev = end_val
        return out_returns

    monthly_by_variant = {r.name: monthly_returns(r) for r in results}
    all_months = sorted(set(m for mr in monthly_by_variant.values() for m in mr))

    # Markdown report
    out = REPORTS_DIR / f"backtest-improvements-{end_dt.strftime('%Y-%m-%d')}.md"
    baseline = results[0]
    lines = [
        "# PurffleBot improvements backtest",
        "",
        f"- Period: **{start_dt.date()} -> {end_dt.date()}** ({BACKTEST_DAYS} days)",
        f"- Universe: {len(symbols)} sub-$1 USDT pairs",
        f"- Starting capital: ${INITIAL_CAPITAL:.2f} per variant",
        f"- Fees: 0.1% per side (Binance spot)",
        "",
        "## Variant comparison",
        "",
        "| Variant | Final value | ROI | Trades opened | Closed | Win rate | Avg P/L per trade | Δ vs baseline |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        roi = (r.final_value / INITIAL_CAPITAL - 1) * 100
        wr = (r.wins / r.closed_round_trips * 100) if r.closed_round_trips else 0
        avg = (r.realized_pnl / r.closed_round_trips) if r.closed_round_trips else 0
        delta = r.final_value - baseline.final_value
        lines.append(
            f"| {r.name} | ${r.final_value:.2f} | {roi:+.2f}% | {r.total_trades} | "
            f"{r.closed_round_trips} | {wr:.1f}% | ${avg:+.3f} | "
            f"{'baseline' if r is baseline else f'${delta:+.2f}'} |"
        )

    winner = max(results, key=lambda r: r.final_value)
    lines += ["",
              f"## Winner: **{winner.name}** at ${winner.final_value:.2f} "
              f"({(winner.final_value/INITIAL_CAPITAL-1)*100:+.2f}%)",
              "",
              "## Honest caveats",
              "",
              "- ATR-based stops adapt to per-coin volatility — wider on choppy coins, tighter on calm ones. "
              "Expected to help on small-cap alts where volatility varies 3-10x across the universe.",
              "- Pyramiding adds to winners only, never losers. Max 3 tranches (15% + 10% + 5% of cash). "
              "Designed to catch the rare 10-50% pumps that make small-cap momentum strategies worth running.",
              "- Both changes ALSO carried into live `purffle_bot.py` after this report.",
              "- Same caveats as the prior backtest apply: no slippage, 30 days = one regime, "
              "2 of 7 live filters not backtested.",
              ]
    out.write_text("\n".join(lines), encoding="utf-8")

    # Console summary
    print()
    print("=" * 70)
    print("MULTI-YEAR BACKTEST")
    print("=" * 70)
    print(f"Period: {start_dt.date()} -> {end_dt.date()} ({BACKTEST_DAYS} days)")
    print(f"Universe: {len(symbols)} sub-$1 USDT pairs")
    print(f"Starting capital: ${INITIAL_CAPITAL:.2f} per variant")
    print()

    # Summary table — also include per-month stats
    print(f"{'Variant':<42}{'Final':>9}{'ROI%':>8}{'WR%':>6}{'MaxDD%':>8}"
          f"{'AvgMo%':>8}{'MedMo%':>8}{'Best%':>7}{'Worst%':>8}{'%Profit':>8}")
    print("-" * 114)
    for r in results:
        roi = (r.final_value / INITIAL_CAPITAL - 1) * 100
        wr = (r.wins / r.closed_round_trips * 100) if r.closed_round_trips else 0
        mr = list(monthly_by_variant[r.name].values())
        if mr:
            avg_mo = sum(mr) / len(mr)
            med_mo = sorted(mr)[len(mr) // 2]
            best = max(mr)
            worst = min(mr)
            pct_profit = sum(1 for v in mr if v > 0) / len(mr) * 100
        else:
            avg_mo = med_mo = best = worst = pct_profit = 0
        print(f"{r.name:<42}${r.final_value:>7.2f}{roi:>+7.1f}%{wr:>5.1f}%"
              f"{r.max_drawdown_pct:>7.1f}%{avg_mo:>+7.1f}%{med_mo:>+7.1f}%"
              f"{best:>+6.1f}%{worst:>+7.1f}%{pct_profit:>6.0f}%")

    # Per-month grid — limit to top 4 variants by avg monthly return for readability
    ranked = sorted(results, key=lambda r: sum(monthly_by_variant[r.name].values()) /
                    max(len(monthly_by_variant[r.name]), 1), reverse=True)[:4]
    print()
    print("=" * 70)
    print("MONTH-BY-MONTH RETURNS — top 4 variants by avg monthly return")
    print("=" * 70)
    header = f"{'Month':<9}"
    for r in ranked:
        nm = r.name[:18]
        header += f"{nm:>20}"
    print(header)
    print("-" * (9 + 20 * len(ranked)))
    for m in all_months:
        row = f"{m:<9}"
        for r in ranked:
            v = monthly_by_variant[r.name].get(m)
            row += f"{('+%.1f%%' % v if v is not None else '—'):>20}"
        print(row)
    print()
    print(f"Winner: {winner.name}")
    print(f"Improvement vs baseline: ${winner.final_value - baseline.final_value:+.2f} "
          f"({((winner.final_value/baseline.final_value)-1)*100:+.1f}%)")
    print()
    print(f"Full report: {out}")


if __name__ == "__main__":
    main()
