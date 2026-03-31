# 🌊 Tsunami — Market Regime Detection System

Tsunami is an open-source market regime detection system built on Continuous Wavelet Transform (CWT) analysis. It identifies compression-to-breakout cycles across equities and crypto, runs a live dashboard with real-time price monitoring, and includes a full forward validation framework to prove or disprove its edge over time.

**This is not a black box.** Every signal is logged at fire time, every outcome is checked at fixed horizons, and the backtest engine runs point-in-time simulations with no lookahead bias.

---

## What It Does

Most technical analysis asks *"what is price doing?"* Tsunami asks *"what is the market's energy doing?"*

Using CWT decomposition on daily OHLCV data, Tsunami measures:

- **Compression ratio** — ATR relative to its rolling mean. Is volatility contracting?
- **Spectral energy** — Total wavelet energy in the price signal. Is energy building or releasing?
- **Dominant cycle** — The period of the strongest frequency component. Are cycles shortening?
- **Energy concentration** — Is power concentrated in one cycle or dispersed?
- **Excursion reversal** — Has the energy excursion peaked and started declining?

These five measurements combine to classify each asset into one of nine regime states, progressing through five stages from compressed (Stage 1) to breakout (Stage 5).

---

## The Five Stages

| Stage | States | Meaning |
|-------|--------|---------|
| 1 | Compressed, Coiling | Energy contracting, cycles tightening |
| 2 | Excursion Reversal | Energy peak reversing — early warning |
| 3 | Sustained Focus, Early Watch | Sustained compression with building concentration |
| 4 | Pre-Breakout | All conditions aligning |
| 5 | Expanding, Breakout State | Energy releasing — move underway |

The system only generates entry signals at Stage 5 when the CWT confirms directional bias (bullish or bearish breakout).

---

## Backtest Results

Backtested across 11 assets, 2022–2025, using point-in-time data with no lookahead bias. Entry at next day open, stop at 2×ATR, exit on stage collapse or 10-day time stop.

| Ticker | Win Rate | Total P&L | Profit Factor | Rating |
|--------|----------|-----------|---------------|--------|
| TSLA | 88.9% | +$1,198 | 192.09 | 🌊🌊🌊 Elite |
| NVDA | 75.0% | +$1,320 | 18.27 | 🌊🌊🌊 Elite |
| XRP-USD | 46.4% | +$3,900 | 2.44 | 🌊🌊 Compatible |
| BNB-USD | 54.5% | +$2,292 | 3.15 | 🌊🌊 Compatible |
| BTC-USD | 63.6% | +$2,107 | 2.42 | 🌊🌊 Compatible |
| SOL-USD | 48.1% | +$2,089 | 2.05 | 🌊🌊 Compatible |
| AAPL | 77.8% | +$454 | 2.13 | 🌊🌊 Compatible |
| XOM | 50.0% | +$520 | 2.53 | 🌊🌊 Compatible |
| ETH-USD | 48.5% | -$1,121 | 0.54 | ⚠️ Incompatible |
| META | 30.8% | -$513 | 0.55 | ⚠️ Incompatible |
| MSFT | 37.5% | -$451 | 0.54 | ⚠️ Incompatible |

Portfolio size $50,000. Risk 0.5–1.5% per trade based on conviction score.

**Key finding:** High-volatility momentum assets (TSLA, NVDA, BTC, SOL) have clean compression-release cycles that Tsunami detects reliably. Low-volatility grinders (MSFT, ETH) produce noisy signals with no sustained edge.

---

## Installation

**Requirements:** Python 3.9+

Developed and tested on **macOS**. Should work on **Linux** without changes. **Windows** users may need one minor adjustment — see note below.

```bash
# 1. Clone the repo
git clone https://github.com/YOUR_USERNAME/tsunami.git
cd tsunami

# 2. Install dependencies (Tsunami does this automatically on first run)
pip install numpy pandas yfinance plotly dash PyWavelets

# 3. Run
python3 tsunami.py
```

On first run, Tsunami will scan all watchlist assets (takes 3–5 minutes) then launch the dashboard at `http://localhost:8050`.

### Windows Note

Tsunami uses a Unix system call to raise the file descriptor limit during large scans. On Windows this will throw an error. Fix it by replacing this block near the top of `main()` in `tsunami.py`:

```python
# Replace this:
import resource
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (min(4096, hard), hard))

# With this:
try:
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(4096, hard), hard))
except Exception:
    pass  # Windows — safe to skip
```

Everything else should run without changes. If you hit other platform-specific issues, open an issue and we'll fix it.

---

## Usage

```bash
# Launch dashboard (scans on first run)
python3 tsunami.py

# Force a fresh scan then launch
python3 tsunami.py --scan

# Scan only, no dashboard
python3 tsunami.py --scan-only

# Skip dependency check (faster restart)
python3 tsunami.py --no-install
```

---

## Running a Backtest

```bash
# Basic backtest
python3 tsunami_backtest.py --ticker NVDA --start 2022-01-01 --end 2025-01-01

# With minimum conviction filter
python3 tsunami_backtest.py --ticker BTC-USD --start 2022-01-01 --end 2025-01-01 --min-conviction 50

# Custom portfolio size
python3 tsunami_backtest.py --ticker TSLA --start 2023-01-01 --end 2024-01-01 --portfolio 100000
```

The backtest runs the full CWT pipeline on each trading day using only point-in-time data — no lookahead. Entry is at next day's open. It outputs a full trade log with entry price, stop, exit reason, P&L, and a scorecard by stage and conviction band.

---

## Dashboard Features

### 📊 Grid Tab
- Live asset cards showing stage, state, conviction score, price, and key metrics
- Quiet assets (Stage 0–1) hidden by default — toggle to show all
- Compatibility rating badge on each card (🌊🌊🌊 Elite / 🌊🌊 Compatible / ⚠️ Incompatible)
- Click any card to expand the detail panel with 3D phase space chart

### 🚨 Alerts Tab
- GET IN alerts: Stage 5 + conviction ≥ 65 + bullish/bearish breakout bias
- GET OUT alerts: open trades hitting stop, stage collapse, or time stop
- Click any alert to open a full trade card in a new browser tab
- Badge on tab shows live alert count

### 🧠 Intelligence Tab
- AI-generated commentary on top signals using Claude API
- Phase space trajectory chart per asset

### 📋 Validation Tab
- Every Stage 2+ signal logged at fire time — price locked, never adjusted
- Outcomes checked automatically at 5, 10, and 20 trading days
- Running scorecard by stage, conviction band, and ticker
- This is the live proof-of-edge experiment

### 🔭 Universe Tab
- Top 25 crypto by market cap scanned via CoinGecko
- TSX sector scan (Banks, Energy, Mining, Tech)
- Promote any universe asset to your main watchlist

### ⚡ Trades Tab
- Add custom tickers to your watchlist
- US stocks (AAPL), Canadian TSX (.TO), crypto (BTC-USD)

---

## The Phase Space Chart

Each asset card expands to show a 3D phase space plot — compression ratio (X), CWT cycle slope (Y), and energy ratio (Z). The trail shows the last 60 days of the asset's trajectory through this space. The orange diamond is today's position.

A healthy compression-to-breakout cycle has a recognizable shape in phase space: starting deep in the compressed corner (low energy, low compression ratio, negative slope) and spiraling outward as energy builds. When the diamond breaks away from the historical cluster into high-energy territory, the breakout is confirmed.

---

## AI Commentary (Optional)

The Intelligence tab generates per-asset commentary using the Anthropic Claude API. This is entirely optional — all regime detection, alerts, backtesting, and validation work without it.

To enable it:

1. Get an API key at [console.anthropic.com](https://console.anthropic.com)
2. Create a config file at `~/.claude_config.json`:
```json
{
    "anthropic_api_key": "your-key-here"
}
```
3. Tsunami detects it automatically on startup.

If the file doesn't exist, the Intelligence tab shows "Analysis unavailable" — everything else works normally.

---

```
tsunami.py              — Entry point, dependency installer, launcher
tsunami_engine.py       — CWT pipeline, watchlist, database
tsunami_dashboard.py    — Dash dashboard, all UI and callbacks
tsunami_trades.py       — Trade logic, position sizing, exit rules
tsunami_universe.py     — Crypto universe and TSX sector scanners
tsunami_validation.py   — Forward validation tracker
tsunami_backtest.py     — Point-in-time trade simulation engine
```

---

## The Conviction Score

Each signal gets a conviction score (0–100) built from:

- Stage × 8 (max 40 points)
- Excursion reversal present: +20
- Compression ratio < 0.80: +15, < 0.88: +10, < 0.95: +5
- CWT slope < -3.0: +15, < -1.5: +8, < 0: +3
- Energy concentration > 5.0: +10, > 3.0: +5

Higher conviction means the setup had deeper compression, tighter cycles, and a cleaner excursion reversal — not just that the stage was reached. The validation tab tracks whether conviction score is actually predictive over time.

---

## Watchlist

Default watchlist includes 58 assets across:
- US ETFs (SPY, QQQ, IWM, GLD, TLT)
- US Mega Cap and Large Cap equities
- TSX Canadian equities (banks, energy, mining, tech, utilities)
- Major crypto (BTC, ETH, SOL, XRP, DOGE, BNB)

Add any ticker via the Trades tab. Remove at any time.

---

## Contributing

Pull requests welcome. Areas that need work:

- Asset-class specific pipeline configs (crypto vs equity parameters)
- Trailing stop implementation for momentum assets
- Rolling geometric pattern detection (compression cycle shape analysis)
- Multi-timeframe CWT analysis
- Automatic Tsunami Compatibility Rating on ticker add

---

## Disclaimer

Tsunami is an experimental research tool. Nothing here is financial advice. Past backtest performance does not guarantee future results. Use at your own risk.

---

MIT License

Copyright (c) 2026 K. Fallon

Permission is hereby granted, free of charge, to any person obtaining a copy...

---

*Built with Python, Dash, PyWavelets, and yfinance.*
*Validation data accumulates in a local SQLite database at ~/Downloads/tsunami.db*
