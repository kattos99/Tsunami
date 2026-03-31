"""
tsunami_universe.py
-------------------
Universe scanner for Tsunami.
Fetches top 25 crypto by market cap from CoinGecko (free, no API key).
Runs full Tsunami pipeline on each — same CWT, same five stages, same conviction score.
Results saved to separate table in tsunami.db.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf

DB_PATH = Path.home() / "Downloads" / "tsunami.db"

# -----------------------------------------------------------------------
# CoinGecko — top 25 by market cap
# -----------------------------------------------------------------------

COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/markets"

COINGECKO_TO_YAHOO = {
    "bitcoin":           "BTC-USD",
    "ethereum":          "ETH-USD",
    "tether":            None,          # stablecoin — skip
    "binancecoin":       "BNB-USD",
    "solana":            "SOL-USD",
    "usd-coin":          None,          # stablecoin — skip
    "xrp":               "XRP-USD",
    "dogecoin":          "DOGE-USD",
    "cardano":           "ADA-USD",
    "avalanche-2":       "AVAX-USD",
    "shiba-inu":         "SHIB-USD",
    "polkadot":          "DOT-USD",
    "chainlink":         "LINK-USD",
    "matic-network":     "MATIC-USD",
    "uniswap":           "UNI7083-USD",
    "cosmos":            "ATOM-USD",
    "litecoin":          "LTC-USD",
    "bitcoin-cash":      "BCH-USD",
    "stellar":           "XLM-USD",
    "algorand":          "ALGO-USD",
    "vechain":           "VET-USD",
    "filecoin":          "FIL-USD",
    "internet-computer": "ICP-USD",
    "hedera-hashgraph":  "HBAR-USD",
    "aptos":             "APT21794-USD",
    "arbitrum":          "ARB11841-USD",
    "optimism":          "OP-USD",
    "near":              "NEAR-USD",
    "tron":              "TRX-USD",
    "monero":            "XMR-USD",
}

def fetch_top_25() -> list[dict]:
    """Fetch top 25 crypto by market cap from CoinGecko."""
    try:
        resp = requests.get(COINGECKO_URL, params={
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": 35,
            "page": 1,
            "sparkline": False,
        }, timeout=15)
        data = resp.json()
        results = []
        for coin in data:
            cg_id   = coin.get("id", "")
            yahoo   = COINGECKO_TO_YAHOO.get(cg_id)
            if yahoo is None:
                continue  # skip stablecoins
            results.append({
                "cg_id":      cg_id,
                "yahoo":      yahoo,
                "name":       coin.get("name", cg_id),
                "symbol":     coin.get("symbol", "").upper(),
                "market_cap": coin.get("market_cap", 0),
                "rank":       coin.get("market_cap_rank", 99),
            })
            if len(results) >= 25:
                break
        return results
    except Exception as e:
        print(f"  CoinGecko error: {e}")
        # Fallback hardcoded list
        return FALLBACK_TOP_25

# Verified working tickers only
FALLBACK_TOP_25 = [
    {"cg_id":"bitcoin",          "yahoo":"BTC-USD",  "name":"Bitcoin",      "symbol":"BTC",  "rank":1},
    {"cg_id":"ethereum",         "yahoo":"ETH-USD",  "name":"Ethereum",     "symbol":"ETH",  "rank":2},
    {"cg_id":"binancecoin",      "yahoo":"BNB-USD",  "name":"BNB",          "symbol":"BNB",  "rank":3},
    {"cg_id":"solana",           "yahoo":"SOL-USD",  "name":"Solana",       "symbol":"SOL",  "rank":4},
    {"cg_id":"xrp",              "yahoo":"XRP-USD",  "name":"XRP",          "symbol":"XRP",  "rank":5},
    {"cg_id":"dogecoin",         "yahoo":"DOGE-USD", "name":"Dogecoin",     "symbol":"DOGE", "rank":6},
    {"cg_id":"cardano",          "yahoo":"ADA-USD",  "name":"Cardano",      "symbol":"ADA",  "rank":7},
    {"cg_id":"avalanche-2",      "yahoo":"AVAX-USD", "name":"Avalanche",    "symbol":"AVAX", "rank":8},
    {"cg_id":"shiba-inu",        "yahoo":"SHIB-USD", "name":"Shiba Inu",    "symbol":"SHIB", "rank":9},
    {"cg_id":"chainlink",        "yahoo":"LINK-USD", "name":"Chainlink",    "symbol":"LINK", "rank":10},
    {"cg_id":"litecoin",         "yahoo":"LTC-USD",  "name":"Litecoin",     "symbol":"LTC",  "rank":11},
    {"cg_id":"bitcoin-cash",     "yahoo":"BCH-USD",  "name":"Bitcoin Cash", "symbol":"BCH",  "rank":12},
    {"cg_id":"stellar",          "yahoo":"XLM-USD",  "name":"Stellar",      "symbol":"XLM",  "rank":13},
    {"cg_id":"tron",             "yahoo":"TRX-USD",  "name":"TRON",         "symbol":"TRX",  "rank":14},
    {"cg_id":"monero",           "yahoo":"XMR-USD",  "name":"Monero",       "symbol":"XMR",  "rank":15},
    {"cg_id":"hedera-hashgraph", "yahoo":"HBAR-USD", "name":"Hedera",       "symbol":"HBAR", "rank":16},
]

# -----------------------------------------------------------------------
# Database
# -----------------------------------------------------------------------

def init_universe_table() -> None:
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS universe_scans (
            yahoo_ticker  TEXT,
            cg_id         TEXT,
            name          TEXT,
            symbol        TEXT,
            rank          INTEGER,
            scan_date     TEXT,
            as_of_date    TEXT,
            price         REAL,
            pct_5d        REAL,
            pct_20d       REAL,
            state         TEXT,
            signal        TEXT,
            stage         INTEGER,
            compression   REAL,
            energy        REAL,
            volume        REAL,
            cwt_cycle     REAL,
            cwt_slope     REAL,
            cwt_conc      REAL,
            cwt_conc_3d   REAL,
            exc_slope     REAL,
            exc_reversal  INTEGER,
            history_json  TEXT,
            PRIMARY KEY (yahoo_ticker, scan_date)
        )
    """)
    con.commit()
    con.close()

def save_universe_result(row: dict) -> None:
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO universe_scans VALUES (
            :yahoo_ticker,:cg_id,:name,:symbol,:rank,
            :scan_date,:as_of_date,:price,:pct_5d,:pct_20d,
            :state,:signal,:stage,:compression,:energy,:volume,
            :cwt_cycle,:cwt_slope,:cwt_conc,:cwt_conc_3d,
            :exc_slope,:exc_reversal,:history_json
        )
    """, row)
    con.commit()
    con.close()

def load_universe_latest() -> list[dict]:
    init_universe_table()
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("""
        SELECT u.* FROM universe_scans u
        INNER JOIN (
            SELECT yahoo_ticker, MAX(scan_date) as max_date
            FROM universe_scans GROUP BY yahoo_ticker
        ) latest ON u.yahoo_ticker=latest.yahoo_ticker
            AND u.scan_date=latest.max_date
        ORDER BY u.stage DESC, u.energy DESC
    """)
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    return rows

def get_universe_scan_date() -> str | None:
    """Get the date of the most recent universe scan."""
    try:
        con = sqlite3.connect(DB_PATH, timeout=30)
        cur = con.cursor()
        cur.execute("SELECT MAX(scan_date) FROM universe_scans")
        row = cur.fetchone()
        con.close()
        return row[0] if row and row[0] else None
    except Exception:
        return None

# -----------------------------------------------------------------------
# Pipeline (reuse from engine)
# -----------------------------------------------------------------------

CWT_WAVELET    = "morl"
CWT_SLOPE_BACK = 5
LOOKBACK_DAYS  = 365 * 3

STATE_STAGE = {
    "insufficient_data": 0, "neutral": 0,
    "compressed": 1, "coiling": 1,
    "excursion_reversal": 2, "sustained_focus": 3, "early_watch": 3,
    "pre_breakout": 4, "expanding": 5, "breakout_state": 5,
}

DEFAULT_CRYPTO_CFG = {
    "window": 40, "atr_window": 14,
    "compressed_ratio_strong": 0.83, "compressed_ratio_mild": 1.00,
    "energy_low": 0.85, "energy_high": 1.20, "energy_breakout": 1.60,
    "volume_high": 1.30, "volume_breakout": 1.50,
    "prebreakout_energy_min": 0.80, "prebreakout_cycle_slope_max": -3.0,
    "prebreakout_concentration_min": 3.0, "prebreakout_volume_min": 1.20,
    "earlywatch_energy_min": 0.65, "earlywatch_cycle_slope_max": -3.0,
    "earlywatch_concentration_min": 3.0, "earlywatch_volume_min": 1.20,
    "sustained_concentration_min": 5.0, "sustained_lookback": 3,
    "sustained_energy_min": 0.65, "sustained_volume_min": 0.80,
    "excursion_max_window": 10, "excursion_slope_window": 5,
}

try:
    from tsunami_engine import run_pipeline
except ImportError:
    run_pipeline = None

def _download_crypto(ticker: str) -> pd.DataFrame | None:
    try:
        end   = date.today()
        start = end - timedelta(days=LOOKBACK_DAYS)
        raw   = yf.download(ticker, start=str(start), end=str(end),
                            auto_adjust=True, progress=False, timeout=15)
        if raw.empty:
            return None
        raw.columns = [col[0].lower() if isinstance(col, tuple) else str(col).lower()
                       for col in raw.columns]
        raw = raw.reset_index()
        raw.columns = [col[0].lower() if isinstance(col, tuple) else str(col).lower()
                       for col in raw.columns]
        df = raw[["date","open","high","low","close","volume"]].dropna()
        return df.sort_values("date").reset_index(drop=True)
    except Exception:
        return None

# -----------------------------------------------------------------------
# Universe scan
# -----------------------------------------------------------------------

def run_universe_scan() -> list[dict]:
    init_universe_table()

    if run_pipeline is None:
        print("  ❌ Could not import run_pipeline from tsunami_engine")
        return []

    today   = date.today().isoformat()
    coins   = fetch_top_25()
    results = []

    print(f"\n🌊 Universe Scan — Top {len(coins)} Crypto")
    print("=" * 50)

    for coin in coins:
        ticker = coin["yahoo"]
        name   = coin["name"]
        symbol = coin["symbol"]
        rank   = coin["rank"]

        print(f"  #{rank:2} {symbol:8} ({name[:20]:20})...", end=" ", flush=True)

        df = _download_crypto(ticker)
        if df is None or len(df) < 100:
            print("❌ No data")
            continue

        try:
            out = run_pipeline(df, DEFAULT_CRYPTO_CFG)
        except Exception as e:
            print(f"❌ Pipeline error: {e}")
            continue

        row_data = out.iloc[-1]

        def fv(v):
            try: f=float(v); return round(f,6) if np.isfinite(f) else None
            except: return None

        pct_5d  = (float(out["close"].iloc[-1])/float(out["close"].iloc[-6])-1)*100  if len(out)>=6  else None
        pct_20d = (float(out["close"].iloc[-1])/float(out["close"].iloc[-21])-1)*100 if len(out)>=21 else None

        history = out.tail(60)[["close","compression_ratio","cwt_cycle_slope",
                                 "energy_ratio","volume_ratio","market_state"]].copy()
        history["date"] = out["date"].tail(60).dt.strftime("%Y-%m-%d").values
        history_json    = history.to_json(orient="records")

        state = str(row_data["market_state"])
        stage = STATE_STAGE.get(state, 0)

        result = {
            "yahoo_ticker": ticker,
            "cg_id":        coin["cg_id"],
            "name":         name,
            "symbol":       symbol,
            "rank":         rank,
            "scan_date":    today,
            "as_of_date":   pd.to_datetime(row_data["date"]).strftime("%Y-%m-%d"),
            "price":        fv(row_data["close"]),
            "pct_5d":       round(pct_5d,2)  if pct_5d  is not None else None,
            "pct_20d":      round(pct_20d,2) if pct_20d is not None else None,
            "state":        state,
            "signal":       str(row_data["signal"]),
            "stage":        stage,
            "compression":  fv(row_data["compression_ratio"]),
            "energy":       fv(row_data["energy_ratio"]),
            "volume":       fv(row_data["volume_ratio"]),
            "cwt_cycle":    fv(row_data["cwt_dominant_cycle"]),
            "cwt_slope":    fv(row_data["cwt_cycle_slope"]),
            "cwt_conc":     fv(row_data["cwt_energy_concentration"]),
            "cwt_conc_3d":  fv(row_data["cwt_conc_min_3"]),
            "exc_slope":    fv(row_data["excursion_slope"]),
            "exc_reversal": int(bool(row_data["excursion_reversal"])),
            "history_json": history_json,
        }

        save_universe_result(result)
        results.append(result)

        stage_str = ["·","·","🔄","👁 ","⚡","🚀"][min(stage,5)]
        print(f"{stage_str} Stage {stage} — {state}")

        time.sleep(0.3)  # be polite to Yahoo Finance

    print(f"\n✅ Universe scan complete — {len(results)}/{len(coins)} processed")
    return results


if __name__ == "__main__":
    run_universe_scan()


# -----------------------------------------------------------------------
# TSX Universe — Sector scan
# -----------------------------------------------------------------------

TSX_SECTORS = {
    "🏦 Banks": [
        {"yahoo":"RY.TO",     "name":"Royal Bank",        "symbol":"RY"},
        {"yahoo":"TD.TO",     "name":"TD Bank",           "symbol":"TD"},
        {"yahoo":"BNS.TO",    "name":"Scotiabank",        "symbol":"BNS"},
        {"yahoo":"BMO.TO",    "name":"Bank of Montreal",  "symbol":"BMO"},
        {"yahoo":"CM.TO",     "name":"CIBC",              "symbol":"CM"},
        {"yahoo":"NA.TO",     "name":"National Bank",     "symbol":"NA"},
    ],
    "⚡ Energy": [
        {"yahoo":"CNQ.TO",    "name":"Canadian Natural",  "symbol":"CNQ"},
        {"yahoo":"SU.TO",     "name":"Suncor",            "symbol":"SU"},
        {"yahoo":"CVE.TO",    "name":"Cenovus",           "symbol":"CVE"},
        {"yahoo":"IMO.TO",    "name":"Imperial Oil",      "symbol":"IMO"},
        {"yahoo":"TOU.TO",    "name":"Tourmaline",        "symbol":"TOU"},
    ],
    "⛏ Mining": [
        {"yahoo":"ABX.TO",    "name":"Barrick Gold",      "symbol":"ABX"},
        {"yahoo":"AEM.TO",    "name":"Agnico Eagle",      "symbol":"AEM"},
        {"yahoo":"WPM.TO",    "name":"Wheaton Precious",  "symbol":"WPM"},
        {"yahoo":"FM.TO",     "name":"First Quantum",     "symbol":"FM"},
        {"yahoo":"TECK-B.TO", "name":"Teck Resources",   "symbol":"TECK"},
    ],
    "💻 Tech": [
        {"yahoo":"SHOP.TO",   "name":"Shopify",           "symbol":"SHOP"},
        {"yahoo":"CSU.TO",    "name":"Constellation SW",  "symbol":"CSU"},
        {"yahoo":"BB.TO",     "name":"BlackBerry",        "symbol":"BB"},
        {"yahoo":"OTEX.TO",   "name":"Open Text",         "symbol":"OTEX"},
        {"yahoo":"KXS.TO",    "name":"Kinaxis",           "symbol":"KXS"},
    ],
}

def init_tsx_table() -> None:
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tsx_scans (
            yahoo_ticker  TEXT,
            name          TEXT,
            symbol        TEXT,
            sector        TEXT,
            scan_date     TEXT,
            as_of_date    TEXT,
            price         REAL,
            pct_5d        REAL,
            pct_20d       REAL,
            state         TEXT,
            signal        TEXT,
            stage         INTEGER,
            compression   REAL,
            energy        REAL,
            volume        REAL,
            cwt_cycle     REAL,
            cwt_slope     REAL,
            cwt_conc      REAL,
            cwt_conc_3d   REAL,
            exc_slope     REAL,
            exc_reversal  INTEGER,
            history_json  TEXT,
            PRIMARY KEY (yahoo_ticker, scan_date)
        )
    """)
    con.commit()
    con.close()

def save_tsx_result(row: dict) -> None:
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO tsx_scans VALUES (
            :yahoo_ticker,:name,:symbol,:sector,
            :scan_date,:as_of_date,:price,:pct_5d,:pct_20d,
            :state,:signal,:stage,:compression,:energy,:volume,
            :cwt_cycle,:cwt_slope,:cwt_conc,:cwt_conc_3d,
            :exc_slope,:exc_reversal,:history_json
        )
    """, row)
    con.commit()
    con.close()

def load_tsx_latest() -> list[dict]:
    init_tsx_table()
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("""
        SELECT t.* FROM tsx_scans t
        INNER JOIN (
            SELECT yahoo_ticker, MAX(scan_date) as max_date
            FROM tsx_scans GROUP BY yahoo_ticker
        ) latest ON t.yahoo_ticker=latest.yahoo_ticker
            AND t.scan_date=latest.max_date
        ORDER BY t.stage DESC, t.energy DESC
    """)
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    return rows

def get_tsx_scan_date() -> str | None:
    try:
        con = sqlite3.connect(DB_PATH, timeout=30)
        cur = con.cursor()
        cur.execute("SELECT MAX(scan_date) FROM tsx_scans")
        row = cur.fetchone()
        con.close()
        return row[0] if row and row[0] else None
    except Exception:
        return None

TSX_CFG = {
    "window": 40, "atr_window": 14,
    "compressed_ratio_strong": 0.80, "compressed_ratio_mild": 0.95,
    "energy_low": 0.85, "energy_high": 1.15, "energy_breakout": 1.55,
    "volume_high": 1.20, "volume_breakout": 1.40,
    "prebreakout_energy_min": 0.80, "prebreakout_cycle_slope_max": -3.0,
    "prebreakout_concentration_min": 3.0, "prebreakout_volume_min": 1.20,
    "earlywatch_energy_min": 0.65, "earlywatch_cycle_slope_max": -3.0,
    "earlywatch_concentration_min": 3.0, "earlywatch_volume_min": 1.20,
    "sustained_concentration_min": 5.0, "sustained_lookback": 3,
    "sustained_energy_min": 0.65, "sustained_volume_min": 0.80,
    "excursion_max_window": 10, "excursion_slope_window": 5,
}

def run_tsx_scan() -> list[dict]:
    init_tsx_table()
    if run_pipeline is None:
        print("  Could not import run_pipeline")
        return []

    today   = date.today().isoformat()
    results = []

    print(f"\n🍁 TSX Sector Scan")
    print("=" * 50)

    for sector, stocks in TSX_SECTORS.items():
        print(f"\n  {sector}")
        for stock in stocks:
            ticker = stock["yahoo"]
            name   = stock["name"]
            symbol = stock["symbol"]
            print(f"    {symbol:8} ({name:25})...", end=" ", flush=True)

            df = _download_crypto(ticker)
            if df is None or len(df) < 100:
                print("❌ No data")
                continue

            try:
                out = run_pipeline(df, TSX_CFG)
            except Exception as e:
                print(f"❌ {e}")
                continue

            row_data = out.iloc[-1]

            def fv(v):
                try: f=float(v); return round(f,6) if np.isfinite(f) else None
                except: return None

            pct_5d  = (float(out["close"].iloc[-1])/float(out["close"].iloc[-6])-1)*100  if len(out)>=6  else None
            pct_20d = (float(out["close"].iloc[-1])/float(out["close"].iloc[-21])-1)*100 if len(out)>=21 else None

            history = out.tail(60)[["close","compression_ratio","cwt_cycle_slope",
                                     "energy_ratio","volume_ratio","market_state"]].copy()
            history["date"] = out["date"].tail(60).dt.strftime("%Y-%m-%d").values
            history_json    = history.to_json(orient="records")

            state = str(row_data["market_state"])
            stage = STATE_STAGE.get(state, 0)

            result = {
                "yahoo_ticker": ticker,
                "name":         name,
                "symbol":       symbol,
                "sector":       sector,
                "scan_date":    today,
                "as_of_date":   pd.to_datetime(row_data["date"]).strftime("%Y-%m-%d"),
                "price":        fv(row_data["close"]),
                "pct_5d":       round(pct_5d,2)  if pct_5d  is not None else None,
                "pct_20d":      round(pct_20d,2) if pct_20d is not None else None,
                "state":        state,
                "signal":       str(row_data["signal"]),
                "stage":        stage,
                "compression":  fv(row_data["compression_ratio"]),
                "energy":       fv(row_data["energy_ratio"]),
                "volume":       fv(row_data["volume_ratio"]),
                "cwt_cycle":    fv(row_data["cwt_dominant_cycle"]),
                "cwt_slope":    fv(row_data["cwt_cycle_slope"]),
                "cwt_conc":     fv(row_data["cwt_energy_concentration"]),
                "cwt_conc_3d":  fv(row_data["cwt_conc_min_3"]),
                "exc_slope":    fv(row_data["excursion_slope"]),
                "exc_reversal": int(bool(row_data["excursion_reversal"])),
                "history_json": history_json,
            }

            save_tsx_result(result)
            results.append(result)

            stage_str = ["·","·","🔄","👁 ","⚡","🚀"][min(stage,5)]
            print(f"{stage_str} Stage {stage} — {state}")
            time.sleep(0.2)

    print(f"\n✅ TSX scan complete — {len(results)}/20 processed")
    return results
