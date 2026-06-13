"""
Strategy library — 6 fundamentally different approaches, each as a pure function.

Each strategy takes (price_data, config) and returns (trades_list, end_value, monthly_values).
Backtested side-by-side on the same 2-year window with the same starting capital.

Why this file exists: parameter-tuning a broken strategy is what wasted today.
This file proves we tried fundamentally different approaches, not 50 flavors of the same
broken thing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


SPOT_FEE = 0.001  # 0.1% per side, applied to every fill


@dataclass
class StrategyResult:
    name: str
    final_value: float = 0.0
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    closed_round_trips: int = 0
    max_drawdown_pct: float = 0.0
    peak_value: float = 0.0
    monthly_end_values: dict = field(default_factory=dict)
    per_symbol_pnl: dict = field(default_factory=dict)
    note: str = ""


# ---------------------------------------------------------------------------
# Indicator helpers (kept tiny — strategies should be readable)
# ---------------------------------------------------------------------------
def ema(values: list[float], period: int) -> list[Optional[float]]:
    n = len(values)
    out: list[Optional[float]] = [None] * n
    if n < period:
        return out
    k = 2 / (period + 1)
    out[period - 1] = sum(values[:period]) / period
    for i in range(period, n):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


def rsi(values: list[float], period: int = 14) -> list[Optional[float]]:
    n = len(values)
    out: list[Optional[float]] = [None] * n
    if n < period + 1:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        ch = values[i] - values[i - 1]
        gains += max(ch, 0); losses += -min(ch, 0)
    avg_g = gains / period
    avg_l = losses / period
    out[period] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    for i in range(period + 1, n):
        ch = values[i] - values[i - 1]
        avg_g = (avg_g * (period - 1) + max(ch, 0)) / period
        avg_l = (avg_l * (period - 1) + -min(ch, 0)) / period
        out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def update_drawdown(state: dict, mtm: float) -> None:
    if mtm > state["peak"]: state["peak"] = mtm
    dd = (state["peak"] - mtm) / state["peak"] if state["peak"] > 0 else 0.0
    if dd > state["max_dd"]: state["max_dd"] = dd


def update_monthly(R: StrategyResult, ts_ms: int, mtm: float) -> None:
    ym = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m")
    R.monthly_end_values[ym] = mtm


# ---------------------------------------------------------------------------
# Strategy 1: Buy & Hold BTC — THE benchmark every active strategy must beat
# ---------------------------------------------------------------------------
def strat_buy_and_hold_btc(btc_klines: list, initial_capital: float = 100.0) -> StrategyResult:
    R = StrategyResult(name="Buy & Hold BTC")
    if not btc_klines:
        return R
    closes = [float(k[4]) for k in btc_klines]
    timestamps = [int(k[0]) for k in btc_klines]
    entry_price = closes[0]
    fee_paid = initial_capital * SPOT_FEE
    qty = (initial_capital - fee_paid) / entry_price
    state = {"peak": initial_capital, "max_dd": 0.0}
    for i, p in enumerate(closes):
        mtm = qty * p
        update_drawdown(state, mtm)
        update_monthly(R, timestamps[i], mtm)
    R.final_value = qty * closes[-1]
    R.peak_value = state["peak"]
    R.max_drawdown_pct = state["max_dd"] * 100
    R.total_trades = 1
    R.note = f"Hold entire period. Entry ${entry_price:.0f}, exit ${closes[-1]:.0f}"
    return R


# ---------------------------------------------------------------------------
# Strategy 2: DCA into BTC — passive baseline that historically crushes most active bots
# ---------------------------------------------------------------------------
def strat_dca_btc(btc_klines: list, initial_capital: float = 100.0,
                  weekly_contribution_pct: float = 0.0) -> StrategyResult:
    """All capital DCA'd evenly over the backtest period, weekly buys."""
    R = StrategyResult(name="DCA BTC (weekly)")
    if not btc_klines:
        return R
    closes = [float(k[4]) for k in btc_klines]
    timestamps = [int(k[0]) for k in btc_klines]
    weekly_ms = 7 * 24 * 3600 * 1000

    # Find weekly buy points
    buy_indices = []
    next_buy_ts = timestamps[0]
    for i, ts in enumerate(timestamps):
        if ts >= next_buy_ts:
            buy_indices.append(i)
            next_buy_ts = ts + weekly_ms

    if not buy_indices:
        return R
    per_buy = initial_capital / len(buy_indices)
    qty_held = 0.0
    cash = initial_capital
    state = {"peak": initial_capital, "max_dd": 0.0}

    for i, ts in enumerate(timestamps):
        if i in buy_indices and cash >= per_buy:
            fee = per_buy * SPOT_FEE
            qty_held += (per_buy - fee) / closes[i]
            cash -= per_buy
            R.total_trades += 1
        mtm = cash + qty_held * closes[i]
        update_drawdown(state, mtm)
        update_monthly(R, ts, mtm)
    R.final_value = cash + qty_held * closes[-1]
    R.peak_value = state["peak"]
    R.max_drawdown_pct = state["max_dd"] * 100
    R.note = f"{len(buy_indices)} weekly buys of ${per_buy:.2f}"
    return R


# ---------------------------------------------------------------------------
# Strategy 3: BTC 4h trend-following (EMA21/EMA55 cross)
# Long when fast > slow AND price > fast EMA. Exit when fast crosses below slow.
# ---------------------------------------------------------------------------
def strat_btc_4h_trend(btc_klines_4h: list, initial_capital: float = 100.0,
                       fast: int = 21, slow: int = 55) -> StrategyResult:
    R = StrategyResult(name=f"BTC 4h trend EMA{fast}/{slow}")
    if not btc_klines_4h or len(btc_klines_4h) < slow + 5:
        return R
    closes = [float(k[4]) for k in btc_klines_4h]
    timestamps = [int(k[0]) for k in btc_klines_4h]
    e_fast = ema(closes, fast)
    e_slow = ema(closes, slow)

    cash = initial_capital
    qty = 0.0
    entry_price = 0.0
    state = {"peak": initial_capital, "max_dd": 0.0}

    for i in range(slow + 2, len(closes)):
        ef, es = e_fast[i], e_slow[i]
        ef_prev, es_prev = e_fast[i - 1], e_slow[i - 1]
        if ef is None or es is None or ef_prev is None or es_prev is None:
            continue
        price = closes[i]
        # Exit signal: fast crosses below slow
        if qty > 0 and ef_prev >= es_prev and ef < es:
            proceeds = qty * price
            fee = proceeds * SPOT_FEE
            cash += proceeds - fee
            pnl = (proceeds - fee) - (qty * entry_price * (1 + SPOT_FEE))
            R.closed_round_trips += 1
            if pnl > 0: R.wins += 1
            else: R.losses += 1
            qty = 0.0
        # Entry signal: fast crosses above slow AND price above fast
        elif qty == 0 and ef_prev <= es_prev and ef > es and price > ef:
            fee = cash * SPOT_FEE
            qty = (cash - fee) / price
            entry_price = price
            cash = 0.0
            R.total_trades += 1
        mtm = cash + qty * price
        update_drawdown(state, mtm)
        update_monthly(R, timestamps[i], mtm)

    R.final_value = cash + qty * closes[-1]
    R.peak_value = state["peak"]
    R.max_drawdown_pct = state["max_dd"] * 100
    R.note = f"EMA{fast}/{slow} crossover, hold while above fast EMA"
    return R


# ---------------------------------------------------------------------------
# Strategy 4: Multi-coin 4h trend-following (top N caps, equal-weight)
# ---------------------------------------------------------------------------
def strat_multi_4h_trend(klines_4h_by_sym: dict, initial_capital: float = 100.0,
                         fast: int = 21, slow: int = 55,
                         max_concurrent: int = 5) -> StrategyResult:
    R = StrategyResult(name=f"Multi-coin 4h trend EMA{fast}/{slow}")
    cash = initial_capital
    positions: dict[str, dict] = {}
    state = {"peak": initial_capital, "max_dd": 0.0}

    # Pre-compute per-symbol EMAs
    per_sym = {}
    for sym, kl in klines_4h_by_sym.items():
        closes = [float(k[4]) for k in kl]
        per_sym[sym] = {
            "closes": closes,
            "ts": [int(k[0]) for k in kl],
            "ef": ema(closes, fast),
            "es": ema(closes, slow),
        }

    # Walk through time in interleaved order
    events = []
    for sym, s in per_sym.items():
        for i in range(slow + 2, len(s["closes"])):
            events.append((s["ts"][i], sym, i))
    events.sort()

    last_known_price: dict[str, float] = {}
    for ts, sym, i in events:
        s = per_sym[sym]
        price = s["closes"][i]
        last_known_price[sym] = price
        ef, es = s["ef"][i], s["es"][i]
        ef_prev, es_prev = s["ef"][i - 1], s["es"][i - 1]
        if ef is None or es is None or ef_prev is None or es_prev is None:
            continue
        pos = positions.get(sym)

        # Exit on death cross
        if pos and ef_prev >= es_prev and ef < es:
            proceeds = pos["qty"] * price
            fee = proceeds * SPOT_FEE
            cash += proceeds - fee
            pnl = (proceeds - fee) - pos["cost"]
            R.closed_round_trips += 1
            if pnl > 0: R.wins += 1
            else: R.losses += 1
            del positions[sym]
        # Enter on golden cross with price above fast
        elif not pos and ef_prev <= es_prev and ef > es and price > ef \
             and len(positions) < max_concurrent and cash > 5:
            spend = cash / (max_concurrent - len(positions))
            if spend < 5: continue
            fee = spend * SPOT_FEE
            qty = (spend - fee) / price
            cash -= spend
            positions[sym] = {"qty": qty, "cost": spend - fee, "entry_price": price}
            R.total_trades += 1

        mtm = cash + sum(p["qty"] * last_known_price.get(s2, p["entry_price"])
                         for s2, p in positions.items())
        update_drawdown(state, mtm)
        update_monthly(R, ts, mtm)

    R.final_value = cash + sum(p["qty"] * last_known_price.get(s2, p["entry_price"])
                                for s2, p in positions.items())
    R.peak_value = state["peak"]
    R.max_drawdown_pct = state["max_dd"] * 100
    R.note = f"Top {len(klines_4h_by_sym)} pairs, max {max_concurrent} concurrent"
    return R


# ---------------------------------------------------------------------------
# Strategy 5: Mean reversion on top caps (buy RSI < 25 on 4h, exit on RSI > 60)
# ---------------------------------------------------------------------------
def strat_mean_reversion(klines_4h_by_sym: dict, initial_capital: float = 100.0,
                         rsi_oversold: float = 25, rsi_exit: float = 60,
                         max_concurrent: int = 5, hard_stop: float = 0.08) -> StrategyResult:
    R = StrategyResult(name=f"Mean reversion (RSI <{rsi_oversold:.0f} buy)")
    cash = initial_capital
    positions: dict[str, dict] = {}
    state = {"peak": initial_capital, "max_dd": 0.0}

    per_sym = {}
    for sym, kl in klines_4h_by_sym.items():
        closes = [float(k[4]) for k in kl]
        per_sym[sym] = {
            "closes": closes, "ts": [int(k[0]) for k in kl],
            "rsi": rsi(closes, 14),
        }

    events = []
    for sym, s in per_sym.items():
        for i in range(16, len(s["closes"])):
            events.append((s["ts"][i], sym, i))
    events.sort()

    last_known_price: dict[str, float] = {}
    for ts, sym, i in events:
        s = per_sym[sym]
        price = s["closes"][i]
        last_known_price[sym] = price
        rsi_val = s["rsi"][i]
        if rsi_val is None: continue
        pos = positions.get(sym)

        if pos:
            from_entry = (price - pos["entry_price"]) / pos["entry_price"]
            if rsi_val > rsi_exit or from_entry <= -hard_stop:
                proceeds = pos["qty"] * price
                fee = proceeds * SPOT_FEE
                cash += proceeds - fee
                pnl = (proceeds - fee) - pos["cost"]
                R.closed_round_trips += 1
                if pnl > 0: R.wins += 1
                else: R.losses += 1
                del positions[sym]
        elif rsi_val < rsi_oversold and len(positions) < max_concurrent and cash > 5:
            spend = cash / (max_concurrent - len(positions))
            if spend < 5: continue
            fee = spend * SPOT_FEE
            qty = (spend - fee) / price
            cash -= spend
            positions[sym] = {"qty": qty, "cost": spend - fee, "entry_price": price}
            R.total_trades += 1

        mtm = cash + sum(p["qty"] * last_known_price.get(s2, p["entry_price"])
                         for s2, p in positions.items())
        update_drawdown(state, mtm)
        update_monthly(R, ts, mtm)

    R.final_value = cash + sum(p["qty"] * last_known_price.get(s2, p["entry_price"])
                                for s2, p in positions.items())
    R.peak_value = state["peak"]
    R.max_drawdown_pct = state["max_dd"] * 100
    R.note = f"RSI<{rsi_oversold:.0f} buy, RSI>{rsi_exit:.0f} sell, {hard_stop*100:.0f}% hard stop"
    return R


# ---------------------------------------------------------------------------
# Strategy 6: Daily breakout on top caps (20-day high breakout)
# Slower timeframe, less noise. Volume confirmation required.
# ---------------------------------------------------------------------------
def strat_daily_breakout(klines_1d_by_sym: dict, initial_capital: float = 100.0,
                         lookback: int = 20, vol_mult: float = 1.5,
                         max_concurrent: int = 5, trail_pct: float = 0.12) -> StrategyResult:
    R = StrategyResult(name=f"Daily breakout {lookback}-day high")
    cash = initial_capital
    positions: dict[str, dict] = {}
    state = {"peak": initial_capital, "max_dd": 0.0}

    per_sym = {}
    for sym, kl in klines_1d_by_sym.items():
        per_sym[sym] = {
            "closes": [float(k[4]) for k in kl],
            "highs":  [float(k[2]) for k in kl],
            "vols":   [float(k[5]) for k in kl],
            "ts":     [int(k[0]) for k in kl],
        }

    events = []
    for sym, s in per_sym.items():
        for i in range(lookback + 2, len(s["closes"])):
            events.append((s["ts"][i], sym, i))
    events.sort()

    last_known_price: dict[str, float] = {}
    for ts, sym, i in events:
        s = per_sym[sym]
        price = s["closes"][i]
        last_known_price[sym] = price
        pos = positions.get(sym)

        if pos:
            if price > pos["peak"]: pos["peak"] = price
            from_peak = (price - pos["peak"]) / pos["peak"]
            if from_peak <= -trail_pct:
                proceeds = pos["qty"] * price
                fee = proceeds * SPOT_FEE
                cash += proceeds - fee
                pnl = (proceeds - fee) - pos["cost"]
                R.closed_round_trips += 1
                if pnl > 0: R.wins += 1
                else: R.losses += 1
                del positions[sym]
        else:
            # Breakout: close > prior N-day high AND volume above average
            prior_high = max(s["highs"][i - lookback: i])
            avg_vol = sum(s["vols"][i - lookback: i]) / lookback
            if price > prior_high and s["vols"][i] > vol_mult * avg_vol \
               and len(positions) < max_concurrent and cash > 5:
                spend = cash / (max_concurrent - len(positions))
                if spend < 5: continue
                fee = spend * SPOT_FEE
                qty = (spend - fee) / price
                cash -= spend
                positions[sym] = {"qty": qty, "cost": spend - fee,
                                  "entry_price": price, "peak": price}
                R.total_trades += 1

        mtm = cash + sum(p["qty"] * last_known_price.get(s2, p["entry_price"])
                         for s2, p in positions.items())
        update_drawdown(state, mtm)
        update_monthly(R, ts, mtm)

    R.final_value = cash + sum(p["qty"] * last_known_price.get(s2, p["entry_price"])
                                for s2, p in positions.items())
    R.peak_value = state["peak"]
    R.max_drawdown_pct = state["max_dd"] * 100
    R.note = f"{lookback}d high + {vol_mult}x vol, {trail_pct*100:.0f}% trail"
    return R
