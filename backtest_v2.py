"""
backtest_v2 — multi-strategy 2-year validation.

Six fundamentally different strategies, all backtested on the same 24 months.
Whichever beats buy-and-hold BTC (the real benchmark) is the only one worth shipping.
If none beat buy-and-hold, the honest answer is "stop trying to beat it actively."

No parameter tuning until the strategy STRUCTURE is proven over 2 years.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

import strategies as S

BACKTEST_DAYS = 730
INITIAL_CAPITAL = 100.0
TOP_N_MAJORS = 20      # for trend / mean-reversion / breakout strategies

BINANCE = "https://api.binance.com"

# Top 20 caps by current 24h volume (NOT filtered to sub-$1 — we proved sub-$1 universe loses)
TOP_MAJORS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
    "TRXUSDT", "LTCUSDT", "BCHUSDT", "NEARUSDT", "ATOMUSDT",
    "ETCUSDT", "XLMUSDT", "FILUSDT", "HBARUSDT", "ICPUSDT",
]

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"
REPORTS.mkdir(exist_ok=True)
CACHE = ROOT / "backtest_v2_cache.json"


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def fetch_klines_range(symbol: str, interval: str, start_ms: int, end_ms: int) -> list:
    out = []
    cursor = start_ms
    while cursor < end_ms:
        try:
            r = requests.get(f"{BINANCE}/api/v3/klines", params={
                "symbol": symbol, "interval": interval,
                "startTime": cursor, "endTime": end_ms, "limit": 1000,
            }, timeout=15)
            if r.status_code != 200: return out
            data = r.json()
        except Exception:
            return out
        if not data: break
        out.extend(data)
        cursor = data[-1][0] + 1
        if len(data) < 1000: break
        time.sleep(0.04)
    return out


def fetch_all_data(start_ms: int, end_ms: int) -> dict:
    """Fetch every dataset every strategy might need. Cached after first run."""
    data = {"btc_4h": [], "btc_1d": [], "majors_4h": {}, "majors_1d": {}}
    log("fetching BTC 4h...")
    data["btc_4h"] = fetch_klines_range("BTCUSDT", "4h", start_ms, end_ms)
    log(f"  BTC 4h: {len(data['btc_4h'])} candles")
    log("fetching BTC 1d...")
    data["btc_1d"] = fetch_klines_range("BTCUSDT", "1d", start_ms, end_ms)
    log(f"  BTC 1d: {len(data['btc_1d'])} candles")
    for n, sym in enumerate(TOP_MAJORS, 1):
        log(f"[{n}/{len(TOP_MAJORS)}] {sym} 4h...")
        data["majors_4h"][sym] = fetch_klines_range(sym, "4h", start_ms, end_ms)
        log(f"[{n}/{len(TOP_MAJORS)}] {sym} 1d...")
        data["majors_1d"][sym] = fetch_klines_range(sym, "1d", start_ms, end_ms)
    return data


def per_month_table(results: list, all_months: list) -> str:
    """Render per-month return grid for all results."""
    lines = []
    header = f"{'Month':<9}"
    for r in results:
        header += f"{r.name[:22]:>23}"
    lines.append(header)
    lines.append("-" * (9 + 23 * len(results)))
    monthly_by_variant = {r.name: monthly_returns(r) for r in results}
    for m in all_months:
        row = f"{m:<9}"
        for r in results:
            v = monthly_by_variant[r.name].get(m)
            row += f"{('%+.1f%%' % v if v is not None else '—'):>23}"
        lines.append(row)
    return "\n".join(lines)


def monthly_returns(R) -> dict:
    ms = sorted(R.monthly_end_values.items())
    out = {}
    prev = INITIAL_CAPITAL
    for ym, v in ms:
        out[ym] = (v / prev - 1) * 100
        prev = v
    return out


def main():
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - BACKTEST_DAYS * 24 * 3600 * 1000
    start_dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)

    log(f"backtest window: {start_dt.date()} -> {end_dt.date()} ({BACKTEST_DAYS} days)")

    use_cache = False
    if CACHE.exists():
        age_h = (time.time() - CACHE.stat().st_mtime) / 3600
        if age_h < 24:
            log(f"loading cached data (age {age_h:.1f}h)...")
            data = json.loads(CACHE.read_text())
            use_cache = True

    if not use_cache:
        data = fetch_all_data(start_ms, end_ms)
        log(f"caching to {CACHE}")
        CACHE.write_text(json.dumps(data))

    # Drop any majors with too little data (recent listings)
    min_4h_candles = 90 * 6  # at least 90 days of 4h data = 540 candles
    data["majors_4h"] = {s: kl for s, kl in data["majors_4h"].items()
                         if len(kl) >= min_4h_candles}
    data["majors_1d"] = {s: kl for s, kl in data["majors_1d"].items()
                         if len(kl) >= 90}
    log(f"viable 4h majors: {len(data['majors_4h'])}")
    log(f"viable 1d majors: {len(data['majors_1d'])}")

    log("running all strategies...")
    results = [
        S.strat_buy_and_hold_btc(data["btc_4h"], INITIAL_CAPITAL),
        S.strat_dca_btc(data["btc_4h"], INITIAL_CAPITAL),
        S.strat_btc_4h_trend(data["btc_4h"], INITIAL_CAPITAL, fast=21, slow=55),
        S.strat_btc_4h_trend(data["btc_4h"], INITIAL_CAPITAL, fast=9, slow=21),
        S.strat_multi_4h_trend(data["majors_4h"], INITIAL_CAPITAL, fast=21, slow=55),
        S.strat_multi_4h_trend(data["majors_4h"], INITIAL_CAPITAL, fast=9, slow=21),
        S.strat_mean_reversion(data["majors_4h"], INITIAL_CAPITAL, rsi_oversold=25, rsi_exit=60),
        S.strat_mean_reversion(data["majors_4h"], INITIAL_CAPITAL, rsi_oversold=20, rsi_exit=55),
        S.strat_daily_breakout(data["majors_1d"], INITIAL_CAPITAL, lookback=20, vol_mult=1.5),
        S.strat_daily_breakout(data["majors_1d"], INITIAL_CAPITAL, lookback=55, vol_mult=2.0),
    ]
    for r in results:
        roi = (r.final_value / INITIAL_CAPITAL - 1) * 100
        log(f"  {r.name:<40}  ${r.final_value:>7.2f}  roi {roi:+6.1f}%  "
            f"trades {r.total_trades:>3d}  MaxDD {r.max_drawdown_pct:>4.1f}%")

    # Find buy-and-hold benchmark for comparison
    benchmark = next(r for r in results if "Buy & Hold BTC" in r.name)
    bench_roi = (benchmark.final_value / INITIAL_CAPITAL - 1) * 100

    # Summary table
    print()
    print("=" * 70)
    print("STRATEGY COMPARISON — 2 years, $100 each")
    print("=" * 70)
    print(f"Window: {start_dt.date()} -> {end_dt.date()}")
    print(f"Universe: BTC-only OR Top {len(data['majors_4h'])} caps")
    print()
    header = f"{'Strategy':<40}{'Final':>9}{'ROI%':>8}{'WR%':>6}{'MaxDD%':>8}{'AvgMo%':>8}{'BestMo%':>9}{'WorstMo%':>10}{'%Profit':>9}{'vs BTC':>9}"
    print(header)
    print("-" * len(header))
    for r in results:
        roi = (r.final_value / INITIAL_CAPITAL - 1) * 100
        wr = (r.wins / r.closed_round_trips * 100) if r.closed_round_trips else 0
        mr = list(monthly_returns(r).values())
        if mr:
            avg_mo = sum(mr) / len(mr)
            best = max(mr); worst = min(mr)
            pct_profit = sum(1 for v in mr if v > 0) / len(mr) * 100
        else:
            avg_mo = best = worst = pct_profit = 0
        vs_bench = roi - bench_roi
        print(f"{r.name:<40}${r.final_value:>7.2f}{roi:>+7.1f}%{wr:>5.1f}%"
              f"{r.max_drawdown_pct:>7.1f}%{avg_mo:>+7.1f}%{best:>+8.1f}%{worst:>+9.1f}%"
              f"{pct_profit:>7.0f}%{vs_bench:>+8.1f}%")

    # Per-month grid — top 4 by avg monthly return
    ranked = sorted(results, key=lambda r: sum(monthly_returns(r).values()) /
                    max(len(monthly_returns(r)), 1), reverse=True)[:4]
    all_months = sorted({m for r in results for m in monthly_returns(r)})
    print()
    print("=" * 70)
    print("MONTH-BY-MONTH — Top 4 strategies")
    print("=" * 70)
    print(per_month_table(ranked, all_months))

    print()
    print("=" * 70)
    print("VERDICT")
    print("=" * 70)
    winner = max(results, key=lambda r: r.final_value)
    print(f"Best total return: {winner.name}  ({(winner.final_value/INITIAL_CAPITAL-1)*100:+.1f}%)")
    beaters = [r for r in results if r.final_value > benchmark.final_value
               and r.name != benchmark.name]
    if beaters:
        print(f"Strategies that BEAT buy-and-hold BTC: {len(beaters)}")
        for r in beaters:
            roi = (r.final_value / INITIAL_CAPITAL - 1) * 100
            print(f"  - {r.name:<40}  {roi:+.1f}%  (+{roi-bench_roi:.1f}% vs benchmark)")
    else:
        print("ZERO strategies beat buy-and-hold BTC.")
        print("Honest takeaway: stop trying to actively trade. Just hold BTC.")

    # Write markdown report
    report = REPORTS / f"strategy-comparison-{end_dt.strftime('%Y-%m-%d')}.md"
    lines = [
        f"# Multi-strategy 2-year comparison",
        "",
        f"- Window: **{start_dt.date()} -> {end_dt.date()}** ({BACKTEST_DAYS} days)",
        f"- Starting capital: ${INITIAL_CAPITAL:.2f} per strategy",
        f"- Universe: BTC-only or top {len(data['majors_4h'])} caps by volume",
        f"- Fees: 0.1% per side",
        "",
        "## Headline",
        "",
        f"**Benchmark (Buy & Hold BTC): {bench_roi:+.1f}%**",
        "",
        f"**Best total return: {winner.name} ({(winner.final_value/INITIAL_CAPITAL-1)*100:+.1f}%)**",
        "",
        f"**Strategies that beat benchmark: {len(beaters)}/{len(results)-1}**",
        "",
        "## Full results",
        "",
        "| Strategy | Final | ROI | Win% | MaxDD | AvgMo | BestMo | WorstMo | %Profit | vs BTC |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        roi = (r.final_value / INITIAL_CAPITAL - 1) * 100
        wr = (r.wins / r.closed_round_trips * 100) if r.closed_round_trips else 0
        mr = list(monthly_returns(r).values())
        if mr:
            avg_mo = sum(mr) / len(mr); best = max(mr); worst = min(mr)
            pct_profit = sum(1 for v in mr if v > 0) / len(mr) * 100
        else:
            avg_mo = best = worst = pct_profit = 0
        vs_bench = roi - bench_roi
        lines.append(f"| {r.name} | ${r.final_value:.2f} | {roi:+.1f}% | {wr:.1f}% | "
                     f"{r.max_drawdown_pct:.1f}% | {avg_mo:+.1f}% | {best:+.1f}% | "
                     f"{worst:+.1f}% | {pct_profit:.0f}% | {vs_bench:+.1f}% |")
    lines += ["", "## Note about the 45%/month target", "",
              "Looking across ALL strategies and ALL 24 months: count how many individual",
              "months hit +45%. That number divided by total months tells you the actual",
              "probability of a 45% month. Expect it to be <5%.", ""]
    report.write_text("\n".join(lines), encoding="utf-8")
    print()
    print(f"Full report: {report}")


if __name__ == "__main__":
    main()
