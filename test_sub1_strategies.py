"""
Test whether the validated 4h strategies work on the sub-$1 universe.

If they do -> build v3 bot with sub-$1 + 4h trend.
If they don't -> sub-$1 universe is structurally unviable, no bot can fix it.

Resamples cached 15m data to 4h and 1d. Tests trend + mean reversion + breakout.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import strategies as S

ROOT = Path(__file__).resolve().parent
CACHE_15M = ROOT / "backtest_cache_730d.json"   # cached earlier this session
REPORTS = ROOT / "reports"
INITIAL_CAPITAL = 100.0


def log(msg): print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def resample_klines(klines_15m: list, n_bars: int) -> list:
    """Aggregate n_bars 15-min candles into a single candle. 16 = 4h, 96 = 1d."""
    out = []
    for i in range(0, len(klines_15m) - n_bars + 1, n_bars):
        bucket = klines_15m[i:i + n_bars]
        if len(bucket) < n_bars:
            break
        open_time = bucket[0][0]
        open_p   = bucket[0][1]
        high     = max(float(k[2]) for k in bucket)
        low      = min(float(k[3]) for k in bucket)
        close    = bucket[-1][4]
        volume   = sum(float(k[5]) for k in bucket)
        close_time = bucket[-1][6]
        out.append([open_time, open_p, high, low, close, volume, close_time])
    return out


def monthly_returns(R):
    ms = sorted(R.monthly_end_values.items())
    out = {}
    prev = INITIAL_CAPITAL
    for ym, v in ms:
        out[ym] = (v / prev - 1) * 100
        prev = v
    return out


def main():
    log(f"loading cached sub-$1 universe data from {CACHE_15M.name}")
    if not CACHE_15M.exists():
        log("ERROR: 2-year sub-$1 cache missing. Run backtest.py first.")
        return
    cache = json.loads(CACHE_15M.read_text())
    symbols = cache["symbols"]
    log(f"sub-$1 universe: {len(symbols)} symbols")
    log(f"sample: {', '.join(symbols[:10])}...")

    # Resample each symbol's 15m -> 4h and 1d
    log("resampling 15m -> 4h and 1d...")
    klines_4h = {}
    klines_1d = {}
    for sym in symbols:
        kl15 = cache["klines_15m"].get(sym, [])
        if len(kl15) < 100:
            continue
        klines_4h[sym] = resample_klines(kl15, 16)   # 16 * 15m = 4h
        klines_1d[sym] = resample_klines(kl15, 96)   # 96 * 15m = 24h
    log(f"viable 4h symbols: {len(klines_4h)}")
    log(f"viable 1d symbols: {len(klines_1d)}")

    log("running validated strategies on sub-$1 universe...")
    results = [
        S.strat_multi_4h_trend(klines_4h, INITIAL_CAPITAL, fast=21, slow=55),
        S.strat_multi_4h_trend(klines_4h, INITIAL_CAPITAL, fast=9, slow=21),
        S.strat_multi_4h_trend(klines_4h, INITIAL_CAPITAL, fast=12, slow=26, max_concurrent=8),
        S.strat_mean_reversion(klines_4h, INITIAL_CAPITAL, rsi_oversold=25, rsi_exit=60),
        S.strat_mean_reversion(klines_4h, INITIAL_CAPITAL, rsi_oversold=20, rsi_exit=55),
        S.strat_daily_breakout(klines_1d, INITIAL_CAPITAL, lookback=20, vol_mult=1.5),
        S.strat_daily_breakout(klines_1d, INITIAL_CAPITAL, lookback=55, vol_mult=2.0),
    ]

    # Add labels
    for i, name_suffix in enumerate([
        "Sub-$1 multi 4h trend EMA21/55",
        "Sub-$1 multi 4h trend EMA9/21",
        "Sub-$1 multi 4h trend EMA12/26 (8 concurrent)",
        "Sub-$1 mean reversion RSI<25",
        "Sub-$1 mean reversion RSI<20",
        "Sub-$1 daily breakout 20d high",
        "Sub-$1 daily breakout 55d high",
    ]):
        results[i].name = name_suffix

    # Print summary
    print()
    print("=" * 80)
    print("SUB-$1 UNIVERSE × VALIDATED STRATEGIES — 2 years")
    print("=" * 80)
    print(f"Universe: {len(klines_4h)} sub-$1 USDT pairs")
    print(f"Starting capital: ${INITIAL_CAPITAL:.2f} per strategy")
    print()
    header = f"{'Strategy':<48}{'Final':>9}{'ROI%':>8}{'WR%':>6}{'MaxDD%':>8}{'AvgMo%':>8}{'BestMo%':>9}{'ProfMo%':>9}"
    print(header)
    print("-" * len(header))

    profitable = []
    for r in results:
        roi = (r.final_value / INITIAL_CAPITAL - 1) * 100
        wr = (r.wins / r.closed_round_trips * 100) if r.closed_round_trips else 0
        mr = list(monthly_returns(r).values())
        if mr:
            avg_mo = sum(mr) / len(mr)
            best = max(mr)
            pct_profit = sum(1 for v in mr if v > 0) / len(mr) * 100
        else:
            avg_mo = best = pct_profit = 0
        print(f"{r.name:<48}${r.final_value:>7.2f}{roi:>+7.1f}%{wr:>5.1f}%"
              f"{r.max_drawdown_pct:>7.1f}%{avg_mo:>+7.1f}%{best:>+8.1f}%{pct_profit:>7.0f}%")
        if r.final_value > INITIAL_CAPITAL:
            profitable.append(r)

    print()
    print("=" * 80)
    print("VERDICT")
    print("=" * 80)
    if profitable:
        winner = max(profitable, key=lambda r: r.final_value)
        roi = (winner.final_value / INITIAL_CAPITAL - 1) * 100
        print(f"WINNER on sub-$1: {winner.name}")
        print(f"  ROI: {roi:+.1f}% over 2 years")
        print(f"  Max DD: {winner.max_drawdown_pct:.1f}%")
        print(f"  Avg month: {sum(monthly_returns(winner).values()) / len(monthly_returns(winner)):+.2f}%")
        print()
        print(f"Profitable strategies on sub-$1: {len(profitable)}/{len(results)}")
        print("Sub-$1 universe IS viable with the right strategy structure.")
        print("Building bot for the winner.")
    else:
        print(f"ZERO strategies are profitable on sub-$1 over 2 years.")
        print("The sub-$1 universe is structurally unviable for bot trading.")
        print("Recommendation: stick with BTC 4h trend (PurffleBot v2) instead.")


if __name__ == "__main__":
    main()
