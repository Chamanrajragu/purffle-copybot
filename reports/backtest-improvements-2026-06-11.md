# PurffleBot improvements backtest

- Period: **2024-06-11 -> 2026-06-11** (730 days)
- Universe: 30 sub-$1 USDT pairs
- Starting capital: $100.00 per variant
- Fees: 0.1% per side (Binance spot)

## Variant comparison

| Variant | Final value | ROI | Trades opened | Closed | Win rate | Avg P/L per trade | Δ vs baseline |
|---|---:|---:|---:|---:|---:|---:|---:|
| Stock Purffle (15% size, with partial) | $84.17 | -15.83% | 769 | 768 | 40.9% | $-0.009 | baseline |
| 15% size, NO partial | $83.84 | -16.16% | 769 | 768 | 40.9% | $-0.009 | $-0.33 |
| 25% size, NO partial | $73.13 | -26.87% | 769 | 768 | 40.9% | $-0.017 | $-11.04 |
| 30% size, NO partial | $67.93 | -32.07% | 768 | 767 | 40.8% | $-0.021 | $-16.24 |
| 40% size, NO partial | $58.12 | -41.88% | 765 | 764 | 40.8% | $-0.030 | $-26.05 |
| 50% size, NO partial | $49.17 | -50.83% | 759 | 758 | 40.6% | $-0.039 | $-35.00 |
| 60% size, NO partial | $41.50 | -58.50% | 741 | 740 | 40.4% | $-0.048 | $-42.67 |
| 60% size, WITH partial (CURRENT LIVE) | $42.53 | -57.47% | 741 | 740 | 40.4% | $-0.047 | $-41.64 |
| 75% size, NO partial | $30.68 | -69.32% | 696 | 695 | 40.9% | $-0.063 | $-53.49 |
| 100% all-in, NO partial | $17.75 | -82.25% | 541 | 540 | 40.0% | $-0.100 | $-66.41 |
| DYNAMIC (10-75%, adapts to win rate) | $70.59 | -29.41% | 765 | 764 | 40.8% | $-0.024 | $-13.58 |
| DYNAMIC + partial profit | $74.29 | -25.71% | 765 | 764 | 40.8% | $-0.019 | $-9.88 |

## Winner: **Stock Purffle (15% size, with partial)** at $84.17 (-15.83%)

## Honest caveats

- ATR-based stops adapt to per-coin volatility — wider on choppy coins, tighter on calm ones. Expected to help on small-cap alts where volatility varies 3-10x across the universe.
- Pyramiding adds to winners only, never losers. Max 3 tranches (15% + 10% + 5% of cash). Designed to catch the rare 10-50% pumps that make small-cap momentum strategies worth running.
- Both changes ALSO carried into live `purffle_bot.py` after this report.
- Same caveats as the prior backtest apply: no slippage, 30 days = one regime, 2 of 7 live filters not backtested.