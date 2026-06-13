<p align="center">
  <img src="https://img.shields.io/badge/Purffle-Copy_Trading_Bot-8B5CF6?style=for-the-badge&logo=ethereum&logoColor=white" alt="PurffleCopyBot"/>
</p>

<h1 align="center">PurffleCopyBot — Smart Copy Trading Bot</h1>

<p align="center">
  <strong>Mirror the moves of top-performing Hyperliquid traders. Scans on-chain leaderboards, aggregates elite signals, and paper-trades on Binance spot.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.9+-blue?style=flat-square&logo=python&logoColor=white" />
  <img src="https://img.shields.io/badge/hyperliquid-on_chain-8B5CF6?style=flat-square" />
  <img src="https://img.shields.io/badge/binance-spot_prices-F0B90B?style=flat-square&logo=binance&logoColor=white" />
  <img src="https://img.shields.io/badge/flask-dashboard-000000?style=flat-square&logo=flask&logoColor=white" />
  <img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" />
</p>

---

## Why Hyperliquid?

Binance has walled off their copy-trading leaderboard since 2024 — programmatic access returns 404/403. They monetize copy trading as a built-in product.

**Hyperliquid** is a decentralized perpetuals DEX with fully public on-chain data. All 38,000+ trader accounts, positions, and PnL are readable for free, no authentication required.

## What It Does

PurffleCopyBot identifies and mirrors the best traders on Hyperliquid:

### 1. Elite Trader Selection (every 6 hours)
Scans the Hyperliquid leaderboard and picks 5 traders matching strict criteria:

| Criteria | Threshold |
|----------|-----------|
| All-time ROI | 100% – 5,000% |
| Monthly ROI | ≥ 10% |
| Weekly ROI | ≥ 2% |
| Monthly volume | ≥ $100K |
| Account value | $100K – $5M |
| Daily ROI | Within ±30% (no freak days) |

### 2. Position Mirroring (every 5 minutes)
- Fetches each elite trader's open positions
- Aggregates: counts how many elites are long each coin
- When **≥ 2 elites agree** on a long → paper-open that position on Binance spot
- When all elites exit → we exit too
- Hard stop at **-10%** per position

### 3. Multi-Strategy Backtesting
Includes a full backtesting suite with 6 fundamentally different strategies:
- EMA crossover + RSI filter
- Mean reversion
- Momentum breakout
- And more — each tested on 2-year historical data

## Features

- **On-chain intelligence** — Reads real trader positions from Hyperliquid's public API
- **Consensus-based signals** — Only trades when multiple elite traders agree
- **3 bot versions** — Copy-trader (v1), standalone strategies (v2, v3)
- **Backtesting engine** — 2-year historical backtests with per-symbol P&L breakdown
- **Flask dashboards** — Each version has its own real-time web dashboard
- **SQLite persistence** — Complete trade log and portfolio snapshots
- **Strategy library** — 6 independent strategies in `strategies.py`

## Bot Versions

| Version | File | Port | Strategy |
|---------|------|------|----------|
| Copy Trader | `purffle_copytrade.py` | `:12349` | Mirror Hyperliquid elites |
| V2 | `purffle_v2.py` | `:12350` | Independent EMA/RSI |
| V3 | `purffle_v3.py` | `:12351` | Enhanced multi-strategy |

## Quick Start

### Prerequisites

- Python 3.9+
- Internet connection (for Hyperliquid + Binance public APIs)

### Installation

```bash
# Clone the repo
git clone https://github.com/Chamanrajragu/purffle-copybot.git
cd purffle-copybot

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### Run

```bash
# Run the copy-trading bot
python purffle_copytrade.py

# Or run v2/v3 strategies
python purffle_v2.py
python purffle_v3.py

# Run backtests
python backtest.py
```

## Project Structure

```
purffle-copybot/
├── purffle_copytrade.py       # Main copy-trading bot
├── purffle_v2.py              # V2 standalone strategy bot
├── purffle_v3.py              # V3 enhanced strategy bot
├── strategies.py              # 6-strategy library
├── backtest.py                # Backtesting engine (main)
├── backtest_v2.py             # V2 backtester
├── backtest_iterations.py     # Iterative parameter optimization
├── test_sub1_strategies.py    # Strategy unit tests
├── _test_hyperliquid.py       # Hyperliquid API connectivity test
├── _reset_db.py               # Database reset utilities
├── _reset_v3_db.py            # V3 database reset
├── requirements.txt           # Python dependencies
└── reports/                   # AI-generated trade reviews
```

## Realistic Expectations

> This is **NOT** a magic money printer. It IS a way to ride the coattails of proven traders — with real caveats:

- **Latency** — We see positions after they've been held for some time. Worse entry price.
- **No leverage** — Their 10x perp long becomes our 1x spot. Smaller magnitude both ways.
- **Selection drift** — Today's top 5 may not be tomorrow's. We refresh every 6h.
- **Price mismatch** — Tracked at perp prices, executed on spot. Direction usually agrees, magnitude doesn't.

## Disclaimer

> **Paper trading only.** This bot does not execute real trades. It simulates positions using Binance's public price feeds. Real-money copy trading requires deeper due diligence on trader histories. Always do your own research.

---

<p align="center">
  Built with passion by <a href="https://github.com/Chamanrajragu"><strong>Purffle</strong></a>
  <br/>
  <sub>Part of the Purffle ecosystem — PurffleTools · PurffleAI · Purffle.com</sub>
</p>
