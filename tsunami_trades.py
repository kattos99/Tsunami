"""
tsunami_trades.py
-----------------
Trade execution framework for Tsunami.

Entry rules:
  Stage 3+ AND conviction >= 65 AND direction confirmed

Risk rules (portfolio $50k default, configurable):
  Conviction 65-75  -> 0.5% risk
  Conviction 76-85  -> 1.0% risk
  Conviction 86+    -> 1.5% risk
  Stop = entry - (ATR x 2) for longs
  Stop = entry + (ATR x 2) for shorts

Exit rules (first wins):
  1. Stop hit
  2. Stage drops below 2
  3. Time stop: 10 days

Currency:
  .TO tickers -> CAD
  All others  -> USD
  CAD/USD shown in header
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

DB_PATH        = Path.home() / "Downloads" / "tsunami.db"
DEFAULT_PORT   = 50000.0
MIN_CONVICTION = 65
MIN_STAGE      = 3
ATR_MULT       = 2.0
MAX_HOLD_DAYS  = 10

RISK_BANDS = [
    (86, 100, 0.015),
    (76,  85, 0.010),
    (65,  75, 0.005),
]


# -----------------------------------------------------------------------
# Currency helpers
# -----------------------------------------------------------------------

def is_cad(ticker: str) -> bool:
    return ticker.upper().endswith(".TO")

def currency_symbol(ticker: str) -> str:
    return "CA$" if is_cad(ticker) else "$"

def get_cadusd_rate() -> float:
    """Fetch live CAD/USD exchange rate."""
    try:
        raw = yf.download("CAD=X", period="1d", progress=False)
        if raw.empty:
            return 0.74
        raw.columns = [col[0].lower() if isinstance(col, tuple) else str(col).lower()
                       for col in raw.columns]
        return float(raw["close"].iloc[-1])
    except Exception:
        return 0.74


# -----------------------------------------------------------------------
# Live price fetch (15-min delayed, lightweight)
# -----------------------------------------------------------------------

def get_live_price(ticker: str) -> dict:
    """Fetch current price and day change. Fast — no full pipeline."""
    try:
        raw = yf.download(ticker, period="2d", interval="1h",
                          progress=False, auto_adjust=True)
        if raw.empty:
            return {"price": None, "change_pct": None, "prev_close": None}
        raw.columns = [col[0].lower() if isinstance(col, tuple) else str(col).lower()
                       for col in raw.columns]
        current   = float(raw["close"].iloc[-1])
        prev_rows = raw[raw.index.date < raw.index[-1].date()]
        prev_close = float(prev_rows["close"].iloc[-1]) if not prev_rows.empty else current
        change_pct = (current / prev_close - 1.0) * 100
        return {"price": current, "change_pct": round(change_pct, 2), "prev_close": prev_close}
    except Exception:
        return {"price": None, "change_pct": None, "prev_close": None}


def get_live_prices_batch(tickers: list[str]) -> dict[str, dict]:
    """Fetch live prices for multiple tickers."""
    results = {}
    for ticker in tickers:
        results[ticker] = get_live_price(ticker)
    return results


# -----------------------------------------------------------------------
# ATR calculation
# -----------------------------------------------------------------------

def get_atr(ticker: str, window: int = 14) -> float | None:
    """Calculate ATR for stop loss calculation."""
    try:
        raw = yf.download(ticker, period="60d", progress=False, auto_adjust=True)
        if raw.empty or len(raw) < window:
            return None
        raw.columns = [col[0].lower() if isinstance(col, tuple) else str(col).lower()
                       for col in raw.columns]
        raw = raw.reset_index()
        raw.columns = [col[0].lower() if isinstance(col, tuple) else str(col).lower()
                       for col in raw.columns]
        pc  = raw["close"].shift(1)
        hl  = raw["high"] - raw["low"]
        hc  = (raw["high"] - pc).abs()
        lc  = (raw["low"]  - pc).abs()
        tr  = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        return float(tr.rolling(window).mean().iloc[-1])
    except Exception:
        return None


# -----------------------------------------------------------------------
# Position sizing
# -----------------------------------------------------------------------

def risk_pct(conviction: int) -> float:
    for lo, hi, pct in RISK_BANDS:
        if lo <= conviction <= hi:
            return pct
    return 0.0

def position_size(
    entry_price: float,
    stop_price: float,
    conviction: int,
    portfolio: float = DEFAULT_PORT,
) -> dict:
    """
    Calculate position size based on fixed fractional risk.
    Returns shares, dollar risk, and stop details.
    """
    r_pct      = risk_pct(conviction)
    dollar_risk = portfolio * r_pct
    risk_per_share = abs(entry_price - stop_price)

    if risk_per_share <= 0:
        return {"shares": 0, "dollar_risk": 0, "position_value": 0,
                "risk_pct": r_pct, "stop_price": stop_price}

    shares         = dollar_risk / risk_per_share
    position_value = shares * entry_price

    return {
        "shares":         round(shares, 2),
        "dollar_risk":    round(dollar_risk, 2),
        "position_value": round(position_value, 2),
        "risk_pct":       r_pct,
        "stop_price":     round(stop_price, 4),
    }


# -----------------------------------------------------------------------
# Schema
# -----------------------------------------------------------------------

def init_trades_tables() -> None:
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_config (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS custom_tickers (
            ticker   TEXT PRIMARY KEY,
            label    TEXT,
            category TEXT,
            added_on TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker          TEXT,
            direction       TEXT,
            stage           INTEGER,
            conviction      INTEGER,
            entry_date      TEXT,
            entry_price     REAL,
            stop_price      REAL,
            shares          REAL,
            dollar_risk     REAL,
            position_value  REAL,
            exit_date       TEXT,
            exit_price      REAL,
            exit_reason     TEXT,
            pnl             REAL,
            pnl_pct         REAL,
            status          TEXT DEFAULT 'open'
        )
    """)

    # Default portfolio value
    cur.execute("INSERT OR IGNORE INTO portfolio_config VALUES ('portfolio_value', ?)",
                (str(DEFAULT_PORT),))
    cur.execute("INSERT OR IGNORE INTO portfolio_config VALUES ('base_currency', 'CAD')",)

    con.commit()
    con.close()


# -----------------------------------------------------------------------
# Portfolio config
# -----------------------------------------------------------------------

def get_portfolio_value() -> float:
    init_trades_tables()
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute("SELECT value FROM portfolio_config WHERE key='portfolio_value'")
    row = cur.fetchone()
    con.close()
    return float(row[0]) if row else DEFAULT_PORT

def set_portfolio_value(value: float) -> None:
    init_trades_tables()
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute("INSERT OR REPLACE INTO portfolio_config VALUES ('portfolio_value', ?)",
                (str(value),))
    con.commit()
    con.close()


# -----------------------------------------------------------------------
# Custom tickers
# -----------------------------------------------------------------------

def add_custom_ticker(ticker: str, label: str = "", category: str = "Custom") -> bool:
    init_trades_tables()
    ticker = ticker.upper().strip()
    if not label:
        label = ticker
    try:
        con = sqlite3.connect(DB_PATH, timeout=30)
        cur = con.cursor()
        cur.execute("INSERT OR REPLACE INTO custom_tickers VALUES (?,?,?,?)",
                    (ticker, label, category, date.today().isoformat()))
        con.commit()
        con.close()
        return True
    except Exception:
        return False

def remove_custom_ticker(ticker: str) -> None:
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute("DELETE FROM custom_tickers WHERE ticker=?", (ticker.upper(),))
    con.commit()
    con.close()

def load_custom_tickers() -> list[dict]:
    init_trades_tables()
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("SELECT * FROM custom_tickers ORDER BY added_on DESC")
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    return rows


# -----------------------------------------------------------------------
# Trade logging
# -----------------------------------------------------------------------

def log_trade(
    ticker: str,
    direction: str,
    stage: int,
    conviction: int,
    entry_price: float,
    stop_price: float,
    shares: float,
    dollar_risk: float,
    position_value: float,
) -> int:
    init_trades_tables()
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute("""
        INSERT INTO trades
        (ticker, direction, stage, conviction, entry_date, entry_price,
         stop_price, shares, dollar_risk, position_value, status)
        VALUES (?,?,?,?,?,?,?,?,?,?,'open')
    """, (ticker, direction, stage, conviction, date.today().isoformat(),
          entry_price, stop_price, shares, dollar_risk, position_value))
    trade_id = cur.lastrowid
    con.commit()
    con.close()
    return trade_id

def close_trade(trade_id: int, exit_price: float, exit_reason: str) -> None:
    init_trades_tables()
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("SELECT * FROM trades WHERE id=?", (trade_id,))
    trade = dict(cur.fetchone())

    direction = trade["direction"]
    entry     = trade["entry_price"]
    shares    = trade["shares"]

    if direction == "long":
        pnl     = (exit_price - entry) * shares
        pnl_pct = (exit_price / entry - 1.0) * 100
    else:
        pnl     = (entry - exit_price) * shares
        pnl_pct = (entry / exit_price - 1.0) * 100

    cur.execute("""
        UPDATE trades SET exit_date=?, exit_price=?, exit_reason=?,
        pnl=?, pnl_pct=?, status='closed' WHERE id=?
    """, (date.today().isoformat(), exit_price, exit_reason,
          round(pnl, 2), round(pnl_pct, 2), trade_id))
    con.commit()
    con.close()

def load_open_trades() -> list[dict]:
    init_trades_tables()
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("SELECT * FROM trades WHERE status='open' ORDER BY entry_date DESC")
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    return rows

def load_closed_trades() -> list[dict]:
    init_trades_tables()
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("SELECT * FROM trades WHERE status='closed' ORDER BY exit_date DESC")
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    return rows


# -----------------------------------------------------------------------
# Entry signal filter
# -----------------------------------------------------------------------

def get_entry_signals(rows: list[dict], portfolio: float = DEFAULT_PORT) -> list[dict]:
    """
    Filter scan results to valid entry signals.
    Stage 3+ AND conviction >= 65 AND direction confirmed.
    """
    signals = []
    for r in rows:
        stage     = r.get("stage", 0)
        conviction = r.get("conviction", 0)
        signal    = r.get("signal", "")
        state     = r.get("state", "")

        if stage < MIN_STAGE:
            continue
        if conviction < MIN_CONVICTION:
            continue
        if signal not in {"bullish_breakout_bias", "bearish_breakout_bias"}:
            continue

        ticker    = r["ticker"]
        price     = r.get("price") or 0
        direction = "long" if "bullish" in signal else "short"

        # Calculate stop from ATR
        atr = get_atr(ticker)
        if atr is None or price == 0:
            stop = price * 0.95 if direction == "long" else price * 1.05
        else:
            stop = (price - atr * ATR_MULT) if direction == "long" else (price + atr * ATR_MULT)

        sizing = position_size(price, stop, conviction, portfolio)
        exit_date = (date.today() + timedelta(days=MAX_HOLD_DAYS)).isoformat()
        curr = currency_symbol(ticker)

        signals.append({
            **r,
            "direction":      direction,
            "atr":            round(atr, 4) if atr else None,
            "stop_price":     sizing["stop_price"],
            "shares":         sizing["shares"],
            "dollar_risk":    sizing["dollar_risk"],
            "position_value": sizing["position_value"],
            "risk_pct":       sizing["risk_pct"],
            "time_stop_date": exit_date,
            "currency":       curr,
        })

    return sorted(signals, key=lambda x: x.get("conviction", 0), reverse=True)


# -----------------------------------------------------------------------
# Exit monitoring
# -----------------------------------------------------------------------

def check_exit_signals(open_trades: list[dict], current_rows: list[dict], live_prices: dict | None = None) -> list[dict]:
    """
    Check all open trades for exit triggers.
    live_prices: optional dict of ticker -> current price from fast_info.
    Returns list of trades that should be exited with reason.
    """
    today     = date.today()
    exit_list = []
    state_map = {r["ticker"]: r for r in current_rows}

    for trade in open_trades:
        ticker     = trade["ticker"]
        entry_date = date.fromisoformat(trade["entry_date"])
        direction  = trade["direction"]
        stop       = trade["stop_price"]

        # Get current state from last scan
        current = state_map.get(ticker, {})
        stage   = current.get("stage", 0)

        # Use live price if available, fall back to last scan price
        if live_prices and ticker in live_prices:
            price = live_prices[ticker]
        else:
            price = current.get("price") or 0

        exit_reason = None

        # 1. Time stop
        days_held = (today - entry_date).days
        if days_held >= MAX_HOLD_DAYS:
            exit_reason = f"⏱ Time stop ({days_held} days)"

        # 2. Stage degraded
        elif stage < 2:
            exit_reason = f"📉 Stage collapsed to {stage}"

        # 3. Stop hit
        elif price > 0:
            if direction == "long" and price <= stop:
                exit_reason = f"🛑 Stop hit — price {price:.2f} ≤ stop {stop:.2f}"
            elif direction == "short" and price >= stop:
                exit_reason = f"🛑 Stop hit — price {price:.2f} ≥ stop {stop:.2f}"

        if exit_reason:
            exit_list.append({**trade, "exit_reason": exit_reason, "suggested_exit": price})

    return exit_list


# -----------------------------------------------------------------------
# Performance summary
# -----------------------------------------------------------------------

def trade_summary() -> dict:
    closed = load_closed_trades()
    open_t = load_open_trades()

    if not closed:
        return {"total": 0, "open": len(open_t), "closed": 0,
                "win_rate": None, "mean_pnl": None, "total_pnl": None}

    wins     = [t for t in closed if (t.get("pnl") or 0) > 0]
    pnls     = [t.get("pnl") or 0 for t in closed]
    win_rate = len(wins) / len(closed) * 100

    return {
        "total":     len(closed) + len(open_t),
        "open":      len(open_t),
        "closed":    len(closed),
        "wins":      len(wins),
        "losses":    len(closed) - len(wins),
        "win_rate":  round(win_rate, 1),
        "mean_pnl":  round(sum(pnls) / len(pnls), 2),
        "total_pnl": round(sum(pnls), 2),
    }
