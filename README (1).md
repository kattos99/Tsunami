# 🌊 Tsunami — Market Regime Detection System

Tsunami is an open-source market analysis tool that uses **Continuous Wavelet Transform (CWT)** to detect compression and breakout cycles in equities and crypto. It runs entirely on your local machine — no cloud, no subscriptions, no data leaving your computer.

The core idea: instead of asking *"what is price doing?"*, Tsunami asks *"what is the market's energy doing?"* Compression builds, energy concentrates, then releases. That cycle has a shape — and CWT can measure it.

> ⚠️ Experimental research tool. Not financial advice. Past results don't predict future performance.

---

## What's in the box

| File | What it does |
|------|--------------|
| `tsunami.py` | Entry point — installs deps, runs scan, launches dashboard |
| `tsunami_engine.py` | Core CWT pipeline, watchlist, SQLite storage |
| `tsunami_dashboard.py` | Dash web dashboard at localhost:8050 |
| `tsunami_trades.py` | Paper trading, position sizing, P&L tracking |
| `tsunami_universe.py` | Nightly scan of ~270 stocks + top 50 crypto |
| `tsunami_validation.py` | Forward validation — logs every signal, checks outcomes automatically |
| `tsunami_backtest.py` | Historical backtester, point-in-time, no lookahead bias |
| `tsunami_ridge_debug.py` | Diagnostic tool — show historical CWT metrics per ticker |

---

## Getting started

**Requirements:** Python 3.9+, internet connection

```bash
# 1. Drop all 8 files into a folder
cd ~/Downloads

# 2. Run it — installs dependencies automatically
python3 tsunami.py

# Force a fresh scan first (recommended)
python3 tsunami.py --scan
```

On first run it will:
- Install numpy, pandas, yfinance, plotly, dash, PyWavelets automatically
- Scan the default watchlist (~58 assets, takes 3–5 minutes)
- Launch the dashboard at **http://localhost:8050**

**Subsequent launches are fast** — data is cached in a local SQLite database.

```bash
python3 tsunami.py --no-install   # skip dep check, faster restart
python3 tsunami.py --scan-only    # scan without launching dashboard
```

### Windows note
The file descriptor limit call will throw on Windows. Wrap it like this in `tsunami.py`:
```python
try:
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(4096, hard), hard))
except Exception:
    pass  # Windows — safe to skip
```
Everything else runs without changes.

---

## How it works

Tsunami runs a CWT decomposition on daily price data and measures five things:

| Metric | What it means |
|--------|---------------|
| **Compression ratio** | ATR vs rolling ATR mean — is volatility contracting? |
| **Spectral energy** | Total wavelet power — is energy building or releasing? |
| **Dominant cycle** | Period of the strongest frequency — are cycles shortening? |
| **Energy concentration** | Is power focused in one cycle, or dispersed? |
| **Excursion reversal** | Has the energy peak turned over — early warning shot? |

These combine to classify each asset into one of five stages:

| Stage | State | What it means |
|-------|-------|---------------|
| 1 | Compressed / Coiling | Volatility contracting, cycles tightening |
| 2 | Early Signal | Energy excursion reversed — warning shot |
| 3 | Worth Watching / Building | Sustained compression with rising concentration |
| 4 | Setup Loading | All conditions aligning |
| 5 | Breaking Out | Energy released — move underway |

Entry signals only fire at **Stage 5** when CWT confirms directional bias.

### V2 signal layer
V2 adds deeper cycle metrics on top of the base pipeline:

- **Phase Velocity** — how stable is the dominant cycle? Low = steady organic coil. High = external shock, possible false signal.
- **Ridge Sharpness** — how clean is the dominant cycle? High after a move = cycle maturing. Low at entry = cycle just forming.
- **Ridge Delta** — rate of change of ridge sharpness. Rising at entry = cycle actively forming. The key insight from NVDA backtesting: the best entries had *low* ridge with flat or negative delta.
- **Compression Debt** — running integral of compression below baseline. How long and how tightly has the spring been wound?
- **Fisher Information** — spikes before regime transitions. Acts as a regime-shift speedometer.

Optionally uses **Synchrosqueezed Wavelet Transform (SSWT)** via `ssqueezepy` for sharper frequency resolution. Falls back to standard CWT automatically if not installed.

---

## Conviction score

Every signal gets a score from 0–100:

```
Stage × 8                          (max 40)
Excursion reversal present         +20
Compression ratio < 0.80           +15  (< 0.88: +10, < 0.95: +5)
CWT slope < -3.0                   +15  (< -1.5: +8, < 0: +3)
Energy concentration > 5.0         +10  (> 3.0: +5)
```

Higher conviction = deeper compression, tighter cycles, cleaner reversal. The validation tab tracks whether this score actually predicts outcomes over time.

---

## Backtest results

Backtested across 11 assets, 2022–2025. Entry at next-day open, stop at 2×ATR, exit on stage collapse or 10-day time stop. $50k portfolio, 0.5–1.5% risk per trade based on conviction.

| Ticker | Win Rate | Total P&L | Profit Factor | Rating |
|--------|----------|-----------|---------------|--------|
| TSLA | 88.9% | +$11,981 | 92.09 | 🌊🌊🌊 Elite |
| NVDA | 75.0% | +$1,320 | 18.27 | 🌊🌊🌊 Elite |
| XRP-USD | 46.4% | +$3,900 | 2.44 | 🌊🌊 Compatible |
| BNB-USD | 54.5% | +$2,292 | 3.15 | 🌊🌊 Compatible |
| BTC-USD | 63.6% | +$2,107 | 2.42 | 🌊🌊 Compatible |
| SOL-USD | 48.1% | +$2,089 | 2.05 | 🌊🌊 Compatible |
| AAPL | 77.8% | +$454 | 2.13 | 🌊🌊 Compatible |
| XOM | 50.0% | +$520 | 2.53 | 🌊🌊 Compatible |
| ETH-USD | 48.5% | -$1,121 | 0.54 | ⚠️ Poor Fit |
| META | 30.8% | -$513 | 0.55 | ⚠️ Poor Fit |
| MSFT | 37.5% | -$451 | 0.54 | ⚠️ Poor Fit |

The failures matter as much as the wins. MSFT, ETH, and META all failed with the same parameters. The compatibility classification holds out-of-sample — these assets genuinely don't fit the model.

### Walk-forward OOS validation
Parameters frozen on 2022–2023 data, tested blind on 2024:

| Asset | IS Profit Factor | OOS Profit Factor | Result |
|-------|-----------------|-------------------|--------|
| NVDA | 2.10 | 1,236 | ✅ Strong |
| TSLA | 71.08 | 758.77 | ✅ Strong |
| BTC-USD | 3.33 | 1.97 | ✅ Strong |
| MSFT | 0.45 | 0.89 | ❌ Failed |

### Perturbation stability (9/9 gate)
Tested across 3 ATR multipliers × 3 energy thresholds:

| Asset | Passed | Gate | Result |
|-------|--------|------|--------|
| NVDA | 9/9 | 5/9 | ✅ Pass |
| BTC-USD | 9/9 | 5/9 | ✅ Pass |

The edge is structural, not parameter-dependent.

---

## Running a backtest

```bash
# Basic
python3 tsunami_backtest.py --ticker NVDA --start 2022-01-01 --end 2025-01-01

# With conviction filter
python3 tsunami_backtest.py --ticker BTC-USD --start 2022-01-01 --end 2025-01-01 --min-conviction 50

# Walk-forward OOS split
python3 tsunami_backtest.py --ticker TSLA --start 2022-01-01 --end 2025-01-01 --oos-split 2024-01-01

# Perturbation stability test
python3 tsunami_backtest.py --ticker NVDA --start 2022-01-01 --end 2025-01-01 --perturbation

# All flags
--oos-split DATE          Walk-forward IS/OOS split date
--perturbation            9-combination stability test
--dsr                     Deflated Sharpe Ratio
--min-conviction INT      Minimum conviction score filter
--min-ridge FLOAT         Minimum ridge sharpness at entry
--min-ridge-delta FLOAT   Minimum ridge delta at entry
--avoid-mature-collapse   Skip high-ridge falling-delta entries
--max-hold DAYS           Override the 10-day time stop
--portfolio FLOAT         Portfolio size (default $50,000)
```

---

## Dashboard

### 📊 Grid
All tracked assets as cards sorted by stage and conviction. Quiet assets (Stage 0–1) hidden by default. Click any card to expand the detail panel with a 3D phase space chart showing the last 60 days of the asset's trajectory.

### 🚨 Alerts
- **GET IN** — Stage 5 + conviction ≥ 65 + directional bias confirmed
- **GET OUT** — open trades hitting stop, stage collapse, or 10-day time stop
- Tab badge shows live count. Click any alert to open a full trade card.

### 🧠 Intelligence
AI commentary on top signals using Claude API (optional). Shows regime analysis, key metrics, and what to watch for next.

### 🌙 Nightly
Full market scan results — ~100 TSX stocks, ~150 NYSE/NASDAQ, top 50 crypto. Runs automatically at midnight. One-click promote any signal to your main grid.

### 📋 Validation
Every Stage 2+ signal logged at fire time with price locked. Outcomes checked automatically at 5, 10, and 20 trading days. Running win rate, mean return, and breakdown by conviction band and stage. This is the live proof-of-edge experiment — the numbers are the judge.

### 📝 Paper
Paper trading blotter. Enter trades from alerts or any asset card. Live P&L with daily target tracking, trailing stop suggestions, and trade history.

### 🔭 Universe
Top 25 crypto by market cap + TSX sector scan. Promote any asset to your main watchlist with one click.

---

## AI commentary (optional)

The Intelligence tab uses the Anthropic Claude API. Optional — everything else works without it.

1. Get an API key at [console.anthropic.com](https://console.anthropic.com)
2. On first launch, Tsunami will prompt you to paste it in Terminal
3. Saved to `~/.claude_config.json` — never prompted again

---

## Default watchlist

58 assets out of the box:
- **US ETFs** — SPY, QQQ, IWM, GLD, TLT
- **US equities** — AAPL, MSFT, NVDA, TSLA, AMZN, META, GOOGL, AMD, JPM, GS, XOM, CVX, and more
- **TSX** — RY.TO, TD.TO, BNS.TO, CNQ.TO, SU.TO, SHOP.TO, ENB.TO, and more
- **Crypto** — BTC, ETH, SOL, XRP, DOGE, BNB

Add any ticker via the dashboard. Remove anytime.

---

## Contributing

Pull requests welcome. Areas that need work:

- Asset-class specific pipeline configs (crypto vs equity parameters)
- Trailing stop implementation for momentum assets
- Rolling geometric pattern detection (compression cycle shape analysis)
- Multi-timeframe CWT analysis
- Automatic Tsunami Compatibility Rating on ticker add

---

## License

MIT — use it, fork it, improve it, share it.

---

*Built with Python, Dash, PyWavelets, and yfinance. All data stored locally in SQLite at `~/Downloads/tsunami.db`.*
