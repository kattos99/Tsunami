"""
tsunami_validation.py
---------------------
Forward validation tracker for Tsunami.
Logs every Stage 2+ signal at the time it fires.
Checks outcomes at 5, 10, 20 days automatically.
Maintains a running scorecard that proves or disproves the edge.

Rules:
  1. Signal locked at fire time — never adjusted
  2. Outcome locked at check date — never early
  3. All Stage 2+ signals logged — no cherry picking
  4. Conviction score tracked separately to prove calibration
  5. The numbers are the judge
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
import numpy as np
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

DB_PATH = Path.home() / "Downloads" / "tsunami.db"
HORIZONS = [5, 10, 20]
MIN_STAGE = 2


# -----------------------------------------------------------------------
# Schema
# -----------------------------------------------------------------------

def add_trading_days(start_date: date, n_days: int) -> date:
    """
    Add n trading days to start_date, skipping weekends.
    Uses numpy busday_offset for accuracy.
    Note: does not account for public holidays — close enough for our purposes.
    """
    result = np.busday_offset(start_date.isoformat(), n_days, roll="forward")
    return date.fromisoformat(str(result))


def init_validation_tables() -> None:
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS signals_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker          TEXT,
            date_fired      TEXT,
            stage           INTEGER,
            state           TEXT,
            signal          TEXT,
            price_at_signal REAL,
            conviction      INTEGER,
            exc_reversal    INTEGER,
            cwt_slope       REAL,
            energy          REAL,
            compression     REAL,
            volume          REAL,
            cwt_conc        REAL,
            UNIQUE(ticker, date_fired)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS forward_returns (
            signal_id       INTEGER,
            ticker          TEXT,
            date_fired      TEXT,
            horizon_days    INTEGER,
            check_date      TEXT,
            price_at_check  REAL,
            return_pct      REAL,
            outcome         TEXT,
            checked_on      TEXT,
            PRIMARY KEY (signal_id, horizon_days)
        )
    """)

    con.commit()
    con.close()


# -----------------------------------------------------------------------
# Log signals
# -----------------------------------------------------------------------

def log_signals(rows: list[dict]) -> int:
    """
    Log all Stage 2+ signals from today's scan.
    Returns number of new signals logged.
    """
    init_validation_tables()
    today = date.today().isoformat()
    logged = 0

    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()

    for r in rows:
        if r.get("stage", 0) < MIN_STAGE:
            continue

        try:
            cur.execute("""
                INSERT OR IGNORE INTO signals_log
                (ticker, date_fired, stage, state, signal, price_at_signal,
                 conviction, exc_reversal, cwt_slope, energy, compression,
                 volume, cwt_conc)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                r["ticker"],
                r.get("as_of_date", today),
                r.get("stage", 0),
                r.get("state", ""),
                r.get("signal", ""),
                r.get("price"),
                r.get("conviction", 0),
                int(r.get("exc_reversal", 0)),
                r.get("cwt_slope"),
                r.get("energy"),
                r.get("compression"),
                r.get("volume"),
                r.get("cwt_conc"),
            ))
            if cur.rowcount > 0:
                logged += 1
        except Exception as e:
            print(f"  Warning: could not log {r['ticker']}: {e}")

    con.commit()
    con.close()
    return logged


# -----------------------------------------------------------------------
# Check pending outcomes
# -----------------------------------------------------------------------

def _get_price(ticker: str, check_date: date) -> float | None:
    """Fetch closing price for a specific date."""
    try:
        start = check_date - timedelta(days=3)
        end   = check_date + timedelta(days=3)
        raw   = yf.download(ticker, start=str(start), end=str(end),
                            auto_adjust=True, progress=False, timeout=10)
        if raw.empty:
            return None
        raw.columns = [col[0].lower() if isinstance(col, tuple) else str(col).lower()
                       for col in raw.columns]
        raw = raw.reset_index()
        raw.columns = [col[0].lower() if isinstance(col, tuple) else str(col).lower()
                       for col in raw.columns]
        raw["date"] = pd.to_datetime(raw["date"]).dt.date
        row = raw[raw["date"] <= check_date].tail(1)
        if row.empty:
            return None
        return float(row["close"].iloc[0])
    except Exception:
        return None


def check_pending_outcomes() -> int:
    """
    Check all pending forward return outcomes that are due today or earlier.
    Returns number of outcomes resolved.
    """
    init_validation_tables()
    today     = date.today()
    resolved  = 0

    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # Get all logged signals
    cur.execute("SELECT * FROM signals_log")
    signals = [dict(r) for r in cur.fetchall()]

    for sig in signals:
        fired_date = date.fromisoformat(sig["date_fired"])

        for h in HORIZONS:
            check_date = add_trading_days(fired_date, h)

            # Not due yet
            if check_date > today:
                continue

            # Already resolved
            cur.execute("""
                SELECT 1 FROM forward_returns
                WHERE signal_id=? AND horizon_days=? AND outcome != 'pending'
            """, (sig["id"], h))
            if cur.fetchone():
                continue

            # Fetch price
            price = _get_price(sig["ticker"], check_date)
            if price is None:
                continue

            entry_price = sig["price_at_signal"]
            if not entry_price or entry_price == 0:
                continue

            ret_pct = (price / entry_price - 1.0) * 100
            outcome = "win" if ret_pct > 0 else "loss"

            cur.execute("""
                INSERT OR REPLACE INTO forward_returns
                (signal_id, ticker, date_fired, horizon_days, check_date,
                 price_at_check, return_pct, outcome, checked_on)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                sig["id"], sig["ticker"], sig["date_fired"],
                h, check_date.isoformat(),
                price, round(ret_pct, 4), outcome, today.isoformat()
            ))
            resolved += 1

    con.commit()
    con.close()
    return resolved


# -----------------------------------------------------------------------
# Load data for display
# -----------------------------------------------------------------------

def load_pending() -> list[dict]:
    """Load all signals that still have pending outcomes."""
    init_validation_tables()
    today = date.today()
    con   = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    cur   = con.cursor()

    cur.execute("SELECT * FROM signals_log ORDER BY date_fired DESC")
    signals = [dict(r) for r in cur.fetchall()]

    pending = []
    for sig in signals:
        fired = date.fromisoformat(sig["date_fired"])
        horizons_pending = []
        for h in HORIZONS:
            check_date = add_trading_days(fired, h)
            if check_date > today:
                horizons_pending.append(h)
            else:
                cur.execute("""
                    SELECT outcome FROM forward_returns
                    WHERE signal_id=? AND horizon_days=?
                """, (sig["id"], h))
                row = cur.fetchone()
                if not row or row["outcome"] == "pending":
                    horizons_pending.append(h)
        if horizons_pending:
            sig["pending_horizons"] = horizons_pending
            pending.append(sig)

    con.close()
    return pending


def load_resolved() -> list[dict]:
    """Load all resolved outcomes."""
    init_validation_tables()
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    cur.execute("""
        SELECT s.*, f.horizon_days, f.return_pct, f.outcome, f.check_date, f.price_at_check
        FROM signals_log s
        JOIN forward_returns f ON s.id = f.signal_id
        WHERE f.outcome IN ('win','loss')
        ORDER BY s.date_fired DESC, f.horizon_days
    """)
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    return rows


def load_scorecard() -> dict:
    """Compute the running scorecard from all resolved outcomes."""
    init_validation_tables()
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # Total signals logged
    cur.execute("SELECT COUNT(*) as n FROM signals_log")
    total_signals = cur.fetchone()["n"]

    # First signal date
    cur.execute("SELECT MIN(date_fired) as first FROM signals_log")
    row = cur.fetchone()
    first_date = row["first"] if row["first"] else date.today().isoformat()

    days_running = (date.today() - date.fromisoformat(first_date)).days + 1

    scorecard = {
        "total_signals":  total_signals,
        "days_running":   days_running,
        "first_date":     first_date,
        "horizons":       {},
        "by_conviction":  {},
        "by_stage":       {},
        "by_ticker":      {},
    }

    for h in HORIZONS:
        cur.execute("""
            SELECT f.return_pct, f.outcome, s.conviction, s.stage, s.ticker
            FROM forward_returns f
            JOIN signals_log s ON f.signal_id = s.id
            WHERE f.horizon_days=? AND f.outcome IN ('win','loss')
        """, (h,))
        rows = [dict(r) for r in cur.fetchall()]

        if not rows:
            scorecard["horizons"][h] = {"n": 0, "win_rate": None, "mean_ret": None}
            continue

        rets     = [r["return_pct"] for r in rows]
        wins     = [r for r in rows if r["outcome"] == "win"]
        win_rate = len(wins) / len(rows) * 100
        mean_ret = sum(rets) / len(rets)

        scorecard["horizons"][h] = {
            "n":        len(rows),
            "win_rate": round(win_rate, 1),
            "mean_ret": round(mean_ret, 2),
        }

        # By conviction band
        if h == 10:
            bands = [(76, 100, "76-100 HIGH"), (61, 75, "61-75 BUILDING"), (0, 60, "0-60 EARLY")]
            for lo, hi, label in bands:
                band_rows = [r for r in rows if lo <= r["conviction"] <= hi]
                if band_rows:
                    brets = [r["return_pct"] for r in band_rows]
                    bwins = [r for r in band_rows if r["outcome"] == "win"]
                    scorecard["by_conviction"][label] = {
                        "n":        len(band_rows),
                        "win_rate": round(len(bwins)/len(band_rows)*100, 1),
                        "mean_ret": round(sum(brets)/len(brets), 2),
                    }

            # By stage
            for stage in [2, 3, 4, 5]:
                stage_rows = [r for r in rows if r["stage"] == stage]
                if stage_rows:
                    srets = [r["return_pct"] for r in stage_rows]
                    swins = [r for r in stage_rows if r["outcome"] == "win"]
                    scorecard["by_stage"][stage] = {
                        "n":        len(stage_rows),
                        "win_rate": round(len(swins)/len(stage_rows)*100, 1),
                        "mean_ret": round(sum(srets)/len(srets), 2),
                    }

            # By ticker
            tickers = set(r["ticker"] for r in rows)
            for ticker in tickers:
                t_rows = [r for r in rows if r["ticker"] == ticker]
                trets  = [r["return_pct"] for r in t_rows]
                twins  = [r for r in t_rows if r["outcome"] == "win"]
                scorecard["by_ticker"][ticker] = {
                    "n":        len(t_rows),
                    "win_rate": round(len(twins)/len(t_rows)*100, 1),
                    "mean_ret": round(sum(trets)/len(trets), 2),
                }

    con.close()
    return scorecard
