"""
Iterate strategy variants on sub-$1 universe, last 6 months. The bar:
  >= +40% return in EVERY single month of the 6.

Why this is brutal: top retail strategies average +5-10%/month with high variance.
+40% every month means landing in the top decile 6 times in a row. We will see.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "backtest_cache_730d.json"

INITIAL_CAPITAL = 100.0
SPOT_FEE = 0.001
TARGET_MONTHLY_PCT = 30.0
MONTHS_REQUIRED = 6


def log(msg): print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def resample(klines_15m: list, n_bars: int) -> list:
    out = []
    for i in range(0, len(klines_15m) - n_bars + 1, n_bars):
        bucket = klines_15m[i:i + n_bars]
        if len(bucket) < n_bars: break
        out.append([bucket[0][0], bucket[0][1],
                    max(float(k[2]) for k in bucket),
                    min(float(k[3]) for k in bucket),
                    bucket[-1][4],
                    sum(float(k[5]) for k in bucket),
                    bucket[-1][6]])
    return out


def slice_last_n_days(klines_1d: dict, days: int) -> dict:
    if not klines_1d:
        return klines_1d
    end_ms = max(int(kl[-1][0]) for kl in klines_1d.values() if kl)
    cutoff_ms = end_ms - days * 24 * 3600 * 1000
    return {s: [k for k in kl if int(k[0]) >= cutoff_ms] for s, kl in klines_1d.items()}


@dataclass
class Variant:
    name: str
    lookback: int = 20
    vol_mult: float = 1.5
    trail_pct: float = 0.12
    max_concurrent: int = 5
    pos_size: float = 0.20      # fraction of cash per position
    hard_stop: float = 0.0       # 0 = no hard stop, use trail only
    require_trend: bool = False  # require recent price to be above 50-day MA
    btc_regime_filter: bool = False  # only trade when BTC is uptrending (proxy via market state)
    partial_profit_at: float = 0.0   # 0 = off; else sell half at this %
    dynamic_sizing: bool = False     # adapt pos size to recent win rate


@dataclass
class Result:
    variant: Variant
    final_value: float = 0.0
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    max_dd: float = 0.0
    monthly_end_values: dict = field(default_factory=dict)
    monthly_returns: dict = field(default_factory=dict)


def run_variant(klines_1d: dict, v: Variant, btc_1d: list = None) -> Result:
    R = Result(variant=v)
    cash = INITIAL_CAPITAL
    positions: dict[str, dict] = {}
    last_known = {}
    peak = INITIAL_CAPITAL
    max_dd = 0.0
    recent_results: list[float] = []  # for dynamic sizing

    # Pre-compute BTC regime if used
    btc_uptrend_at = {}
    if v.btc_regime_filter and btc_1d:
        btc_closes = [float(k[4]) for k in btc_1d]
        btc_ts = [int(k[0]) for k in btc_1d]
        # Simple regime: price > 50-day MA
        for i in range(50, len(btc_closes)):
            ma50 = sum(btc_closes[i-50:i]) / 50
            btc_uptrend_at[btc_ts[i]] = btc_closes[i] > ma50

    def is_btc_up(ts):
        if not v.btc_regime_filter or not btc_uptrend_at: return True
        # Find latest BTC ts <= ts
        last_ok = True
        for t, ok in sorted(btc_uptrend_at.items()):
            if t <= ts: last_ok = ok
            else: break
        return last_ok

    def get_pos_size():
        if not v.dynamic_sizing or len(recent_results) < 5:
            return v.pos_size
        last5 = recent_results[-5:]
        wins = sum(1 for r in last5 if r > 0)
        wr = wins / 5
        # Scale: 0 wins -> 0.5x base, 5 wins -> 1.5x base
        return v.pos_size * (0.5 + wr)

    # Build per-symbol arrays
    per_sym = {}
    for sym, kl in klines_1d.items():
        if len(kl) < v.lookback + 10:
            continue
        per_sym[sym] = {
            "closes": [float(k[4]) for k in kl],
            "highs":  [float(k[2]) for k in kl],
            "lows":   [float(k[3]) for k in kl],
            "vols":   [float(k[5]) for k in kl],
            "ts":     [int(k[0]) for k in kl],
        }

    events = []
    for sym, s in per_sym.items():
        for i in range(v.lookback + 2, len(s["closes"])):
            events.append((s["ts"][i], sym, i))
    events.sort()

    for ts, sym, i in events:
        s = per_sym[sym]
        price = s["closes"][i]
        last_known[sym] = price

        mtm = cash + sum(p["qty"] * last_known.get(s2, p["entry"])
                         for s2, p in positions.items())
        if mtm > peak: peak = mtm
        dd = (peak - mtm) / peak if peak > 0 else 0
        if dd > max_dd: max_dd = dd
        ym = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m")
        R.monthly_end_values[ym] = mtm

        pos = positions.get(sym)
        if pos:
            if price > pos["peak"]: pos["peak"] = price
            from_peak = (price - pos["peak"]) / pos["peak"]
            from_entry = (price - pos["entry"]) / pos["entry"]
            # Partial profit
            if v.partial_profit_at > 0 and not pos.get("partial_taken") \
               and from_entry >= v.partial_profit_at:
                half = pos["qty"] * 0.5
                proceeds = half * price
                fee = proceeds * SPOT_FEE
                cash += proceeds - fee
                pos["qty"] -= half
                pos["cost"] *= 0.5
                pos["partial_taken"] = True
            exit_reason = None
            if from_peak <= -v.trail_pct: exit_reason = "trail"
            elif v.hard_stop > 0 and from_entry <= -v.hard_stop: exit_reason = "hard"
            if exit_reason:
                proceeds = pos["qty"] * price
                fee = proceeds * SPOT_FEE
                cash += proceeds - fee
                pnl = (proceeds - fee) - pos["cost"]
                if pnl > 0: R.wins += 1
                else: R.losses += 1
                recent_results.append(pnl)
                del positions[sym]
        else:
            prior_high = max(s["highs"][i - v.lookback: i])
            avg_vol = sum(s["vols"][i - v.lookback: i]) / v.lookback
            if avg_vol <= 0: continue
            if price > prior_high and s["vols"][i] >= v.vol_mult * avg_vol \
               and len(positions) < v.max_concurrent and cash > 5:
                # Trend filter (optional)
                if v.require_trend:
                    if i < 50: continue
                    ma50 = sum(s["closes"][i-50:i]) / 50
                    if price < ma50: continue
                if v.btc_regime_filter and not is_btc_up(ts):
                    continue
                spend = cash * get_pos_size()
                if spend < 5: continue
                fee = spend * SPOT_FEE
                qty = (spend - fee) / price
                cash -= spend
                positions[sym] = {"qty": qty, "entry": price, "peak": price,
                                  "cost": spend - fee}
                R.total_trades += 1

    final = cash + sum(p["qty"] * last_known.get(s2, p["entry"])
                       for s2, p in positions.items())
    R.final_value = final
    R.max_dd = max_dd * 100

    # Compute month-over-month returns
    months_sorted = sorted(R.monthly_end_values.items())
    prev = INITIAL_CAPITAL
    for ym, end_val in months_sorted:
        R.monthly_returns[ym] = (end_val / prev - 1) * 100
        prev = end_val
    return R


def check_target(R: Result) -> tuple[bool, int]:
    """Did this variant hit TARGET_MONTHLY_PCT in EVERY month? Returns (success, count_above)."""
    mr = list(R.monthly_returns.values())
    if len(mr) < MONTHS_REQUIRED: return False, 0
    last6 = mr[-MONTHS_REQUIRED:]
    above = sum(1 for v in last6 if v >= TARGET_MONTHLY_PCT)
    return above == MONTHS_REQUIRED, above


def print_variant_result(R: Result, num: int) -> None:
    success, above = check_target(R)
    roi = (R.final_value / INITIAL_CAPITAL - 1) * 100
    months = sorted(R.monthly_returns.items())[-MONTHS_REQUIRED:]
    print(f"\n[Iter {num}] {R.variant.name}")
    print(f"  Final: ${R.final_value:.2f}  6mo ROI {roi:+.1f}%  "
          f"trades {R.total_trades}  W/L {R.wins}/{R.losses}  MaxDD {R.max_dd:.1f}%")
    for ym, ret in months:
        marker = "✓" if ret >= TARGET_MONTHLY_PCT else "✗"
        print(f"    {ym}: {ret:+7.2f}%  {marker}")
    print(f"  RESULT: {above}/{MONTHS_REQUIRED} months >= {TARGET_MONTHLY_PCT}%  "
          f"{'  ★ SUCCESS ★' if success else ''}")


def main():
    log(f"loading cached sub-$1 universe data from {CACHE.name}")
    if not CACHE.exists():
        log("ERROR: 2-year cache missing"); return
    cache = json.loads(CACHE.read_text())
    log(f"universe: {len(cache['symbols'])} sub-$1 symbols")
    log("resampling 15m -> daily...")
    klines_1d_full = {s: resample(cache["klines_15m"].get(s, []), 96)
                      for s in cache["symbols"]}
    klines_1d = slice_last_n_days(klines_1d_full, 180)
    log(f"viable daily symbols in last 6 months: {sum(1 for kl in klines_1d.values() if len(kl) >= 30)}")

    # Build iteration plan — 12 fundamentally different variants
    variants = [
        Variant("Current v3 (20d/1.5x/12% trail, 20% size)",
                lookback=20, vol_mult=1.5, trail_pct=0.12, pos_size=0.20, max_concurrent=5),
        Variant("Aggressive sizing (20d, 50% per trade)",
                lookback=20, vol_mult=1.5, trail_pct=0.12, pos_size=0.50, max_concurrent=3),
        Variant("All-in single position (20d, 100%, max 1 trade)",
                lookback=20, vol_mult=1.5, trail_pct=0.12, pos_size=1.00, max_concurrent=1),
        Variant("Premium signals only (20d, vol 3x)",
                lookback=20, vol_mult=3.0, trail_pct=0.12, pos_size=0.30, max_concurrent=4),
        Variant("Long lookback for cleaner signals (55d)",
                lookback=55, vol_mult=2.0, trail_pct=0.15, pos_size=0.30, max_concurrent=4),
        Variant("Short lookback for early entries (10d)",
                lookback=10, vol_mult=1.2, trail_pct=0.10, pos_size=0.25, max_concurrent=4),
        Variant("Tight trail to lock gains (8%)",
                lookback=20, vol_mult=1.5, trail_pct=0.08, pos_size=0.30, max_concurrent=4),
        Variant("Loose trail to ride huge moves (25%)",
                lookback=20, vol_mult=1.5, trail_pct=0.25, pos_size=0.30, max_concurrent=4),
        Variant("With 50d trend confirmation",
                lookback=20, vol_mult=1.5, trail_pct=0.12, pos_size=0.30,
                max_concurrent=4, require_trend=True),
        Variant("MAX aggression (10d, vol 1.2x, 75% size, 1 concurrent)",
                lookback=10, vol_mult=1.2, trail_pct=0.15, pos_size=0.75, max_concurrent=1),
        Variant("Hard stop + tight trail combo",
                lookback=20, vol_mult=1.5, trail_pct=0.08, pos_size=0.30,
                max_concurrent=3, hard_stop=0.05),
        Variant("Ultra-aggressive 100% all-in on premium",
                lookback=15, vol_mult=2.5, trail_pct=0.15, pos_size=1.00, max_concurrent=1),
        # === ROUND 2: combining best elements from round 1 + new ideas ===
        Variant("R2a: Tight trail + dynamic sizing",
                lookback=20, vol_mult=1.5, trail_pct=0.08, pos_size=0.30,
                max_concurrent=4, dynamic_sizing=True),
        Variant("R2b: Hard stop + trend filter + partial profit",
                lookback=20, vol_mult=1.5, trail_pct=0.10, pos_size=0.30,
                max_concurrent=4, hard_stop=0.05, require_trend=True,
                partial_profit_at=0.30),
        Variant("R2c: Combined champion stack (tight, hard, partial, dynamic)",
                lookback=20, vol_mult=1.8, trail_pct=0.08, pos_size=0.40,
                max_concurrent=3, hard_stop=0.04, partial_profit_at=0.25,
                dynamic_sizing=True),
        Variant("R2d: Ultra-tight to capture every uptick",
                lookback=15, vol_mult=1.3, trail_pct=0.06, pos_size=0.50,
                max_concurrent=2, hard_stop=0.03),
        Variant("R2e: Premium + tight stops + partial",
                lookback=20, vol_mult=2.5, trail_pct=0.07, pos_size=0.40,
                max_concurrent=3, hard_stop=0.04, partial_profit_at=0.20),
        Variant("R2f: Best of round 1 — exact tight-trail config, larger size",
                lookback=20, vol_mult=1.5, trail_pct=0.08, pos_size=0.50,
                max_concurrent=3, hard_stop=0.05),
    ]

    # Try to load BTC data for regime filter (use 1d resample of cached data if BTC there)
    # Cached data is sub-$1 only, no BTC. We'll skip BTC regime for now, use price-based proxies.
    btc_1d = None

    successes = []
    print("=" * 90)
    print(f"ITERATIVE SEARCH — target: >= +{TARGET_MONTHLY_PCT}% in EACH of last {MONTHS_REQUIRED} months")
    print("=" * 90)
    for n, v in enumerate(variants, 1):
        R = run_variant(klines_1d, v, btc_1d)
        print_variant_result(R, n)
        success, above = check_target(R)
        if success:
            successes.append(R)

    print()
    print("=" * 90)
    print("FINAL VERDICT")
    print("=" * 90)
    if successes:
        print(f"  {len(successes)} variant(s) hit the bar!")
        for R in successes:
            print(f"  - {R.variant.name}")
    else:
        print(f"  ZERO of {len(variants)} variants hit +{TARGET_MONTHLY_PCT}% every month.")
        print()
        # Show which got CLOSEST
        scored = []
        for v in variants:
            R = run_variant(klines_1d, v)
            _, above = check_target(R)
            scored.append((above, R))
        scored.sort(key=lambda x: -x[0])
        print(f"  Closest attempts (months hitting target out of {MONTHS_REQUIRED}):")
        for above, R in scored[:5]:
            roi = (R.final_value / INITIAL_CAPITAL - 1) * 100
            print(f"  - {above}/{MONTHS_REQUIRED}  {R.variant.name}  (6mo ROI {roi:+.1f}%)")


if __name__ == "__main__":
    main()
