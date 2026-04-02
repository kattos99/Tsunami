"""
tsunami_engine.py  (v2 — TSX + CAD/USD + custom tickers)
----------------------------------------------------------
Scans all watchlist assets including TSX and custom tickers.
Saves results to local SQLite database.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path
import time

import numpy as np, pandas as pd
import yfinance as yf

try:
    import pywt
    HAS_PYWT = True
except ImportError:
    HAS_PYWT = False

try:
    import ssqueezepy as ssq
    HAS_SSQ = True
except ImportError:
    HAS_SSQ = False

# -----------------------------------------------------------------------
# Default watchlist
# -----------------------------------------------------------------------

WATCHLIST = {
    # US Indices & ETFs
    "SPY":   {"label": "S&P 500 ETF",     "category": "ETF",    "currency": "USD"},
    "QQQ":   {"label": "Nasdaq ETF",       "category": "ETF",    "currency": "USD"},
    "IWM":   {"label": "Russell 2000 ETF", "category": "ETF",    "currency": "USD"},
    "GLD":   {"label": "Gold ETF",         "category": "ETF",    "currency": "USD"},
    "TLT":   {"label": "20yr Treasury ETF","category": "ETF",    "currency": "USD"},
    # Canadian
    "^GSPTSE": {"label": "TSX Composite",  "category": "Index",  "currency": "CAD"},
    "CAD=X":   {"label": "CAD/USD Rate",   "category": "FX",     "currency": "USD"},
    # Crypto
    "BTC-USD":  {"label": "Bitcoin",       "category": "Crypto", "currency": "USD"},
    "ETH-USD":  {"label": "Ethereum",      "category": "Crypto", "currency": "USD"},
    "XRP-USD":  {"label": "XRP",           "category": "Crypto", "currency": "USD"},
    "SOL-USD":  {"label": "Solana",        "category": "Crypto", "currency": "USD"},
    "DOGE-USD": {"label": "Dogecoin",      "category": "Crypto", "currency": "USD"},
    "BNB-USD":  {"label": "BNB",           "category": "Crypto", "currency": "USD"},
    # US Mega Cap
    "AAPL":  {"label": "Apple",            "category": "Equity", "currency": "USD"},
    "MSFT":  {"label": "Microsoft",        "category": "Equity", "currency": "USD"},
    "GOOGL": {"label": "Alphabet",         "category": "Equity", "currency": "USD"},
    "AMZN":  {"label": "Amazon",           "category": "Equity", "currency": "USD"},
    "NVDA":  {"label": "Nvidia",           "category": "Equity", "currency": "USD"},
    "META":  {"label": "Meta",             "category": "Equity", "currency": "USD"},
    "TSLA":  {"label": "Tesla",            "category": "Equity", "currency": "USD"},
    "NFLX":  {"label": "Netflix",          "category": "Equity", "currency": "USD"},
    "AMD":   {"label": "AMD",              "category": "Equity", "currency": "USD"},
    # US Large Cap — Financials
    "JPM":   {"label": "JPMorgan",         "category": "Equity", "currency": "USD"},
    "BAC":   {"label": "Bank of America",  "category": "Equity", "currency": "USD"},
    "GS":    {"label": "Goldman Sachs",    "category": "Equity", "currency": "USD"},
    "V":     {"label": "Visa",             "category": "Equity", "currency": "USD"},
    # US Large Cap — Energy
    "XOM":   {"label": "ExxonMobil",       "category": "Equity", "currency": "USD"},
    "CVX":   {"label": "Chevron",          "category": "Equity", "currency": "USD"},
    "OXY":   {"label": "Occidental",       "category": "Equity", "currency": "USD"},
    # US Large Cap — Health & Consumer
    "UNH":   {"label": "UnitedHealth",     "category": "Equity", "currency": "USD"},
    "JNJ":   {"label": "Johnson & Johnson","category": "Equity", "currency": "USD"},
    "WMT":   {"label": "Walmart",          "category": "Equity", "currency": "USD"},
    "COST":  {"label": "Costco",           "category": "Equity", "currency": "USD"},
    # US — Semis & Tech
    "TSM":   {"label": "TSMC",             "category": "Equity", "currency": "USD"},
    "INTC":  {"label": "Intel",            "category": "Equity", "currency": "USD"},
    "CRM":   {"label": "Salesforce",       "category": "Equity", "currency": "USD"},
    "PLTR":  {"label": "Palantir",         "category": "Equity", "currency": "USD"},
    "COIN":  {"label": "Coinbase",         "category": "Equity", "currency": "USD"},
    "MSTR":  {"label": "MicroStrategy",    "category": "Equity", "currency": "USD"},
    # TSX — Banks
    "RY.TO":  {"label": "Royal Bank",      "category": "Equity", "currency": "CAD"},
    "TD.TO":  {"label": "TD Bank",         "category": "Equity", "currency": "CAD"},
    "BNS.TO": {"label": "Scotiabank",      "category": "Equity", "currency": "CAD"},
    "BMO.TO": {"label": "Bank of Montreal","category": "Equity", "currency": "CAD"},
    "CM.TO":  {"label": "CIBC",            "category": "Equity", "currency": "CAD"},
    "NA.TO":  {"label": "National Bank",   "category": "Equity", "currency": "CAD"},
    # TSX — Energy
    "CNQ.TO": {"label": "Canadian Natural","category": "Equity", "currency": "CAD"},
    "SU.TO":  {"label": "Suncor",          "category": "Equity", "currency": "CAD"},
    "CVE.TO": {"label": "Cenovus",         "category": "Equity", "currency": "CAD"},
    "TOU.TO": {"label": "Tourmaline",      "category": "Equity", "currency": "CAD"},
    # TSX — Mining & Materials
    "ABX.TO": {"label": "Barrick Gold",    "category": "Equity", "currency": "CAD"},
    "AEM.TO": {"label": "Agnico Eagle",    "category": "Equity", "currency": "CAD"},
    "WPM.TO": {"label": "Wheaton Precious","category": "Equity", "currency": "CAD"},
    "FM.TO":  {"label": "First Quantum",   "category": "Equity", "currency": "CAD"},
    # TSX — Tech & Growth
    "SHOP.TO": {"label": "Shopify",        "category": "Equity", "currency": "CAD"},
    "CSU.TO":  {"label": "Constellation SW","category":"Equity", "currency": "CAD"},
    "KXS.TO":  {"label": "Kinaxis",        "category": "Equity", "currency": "CAD"},
    "OTEX.TO": {"label": "Open Text",      "category": "Equity", "currency": "CAD"},
    # TSX — Utilities & Infrastructure
    "ENB.TO":  {"label": "Enbridge",       "category": "Equity", "currency": "CAD"},
    "TRP.TO":  {"label": "TC Energy",      "category": "Equity", "currency": "CAD"},
    "FTS.TO":  {"label": "Fortis",         "category": "Equity", "currency": "CAD"},
}

# -----------------------------------------------------------------------
# Default pipeline config
# -----------------------------------------------------------------------

DEFAULT_CFG = {
    "window": 40, "atr_window": 14,
    "compressed_ratio_strong": 0.82, "compressed_ratio_mild": 0.95,
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

ASSET_OVERRIDES = {
    "SPY":     {"compressed_ratio_strong": 0.80, "energy_high": 1.10, "energy_breakout": 1.60, "volume_breakout": 1.30},
    "BTC-USD": {"compressed_ratio_strong": 0.85, "compressed_ratio_mild": 1.00, "energy_high": 1.20, "volume_high": 1.30, "volume_breakout": 1.50},
    "XRP-USD": {"energy_high": 1.10, "energy_breakout": 1.50, "volume_high": 1.30, "volume_breakout": 1.70},
    "ETH-USD": {"compressed_ratio_mild": 1.00, "energy_breakout": 1.55, "volume_breakout": 1.45},
    "SOL-USD": {"compressed_ratio_mild": 1.00, "energy_breakout": 1.60, "volume_breakout": 1.50},
}

def get_cfg(ticker: str) -> dict:
    cfg = DEFAULT_CFG.copy()
    cfg.update(ASSET_OVERRIDES.get(ticker, {}))
    return cfg

# -----------------------------------------------------------------------
# Database
# -----------------------------------------------------------------------

DB_PATH = Path.home() / "Downloads" / "tsunami.db"

def init_db() -> None:
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS scans (
            ticker TEXT, scan_date TEXT, as_of_date TEXT,
            price REAL, pct_5d REAL, pct_20d REAL,
            state TEXT, signal TEXT, stage INTEGER,
            compression REAL, energy REAL, volume REAL,
            cwt_cycle REAL, cwt_slope REAL, cwt_conc REAL, cwt_conc_3d REAL,
            exc_slope REAL, exc_reversal INTEGER,
            history_json TEXT, currency TEXT,
            phase_velocity REAL, ridge_sharpness REAL, ridge_delta REAL,
            compression_debt REAL, fisher_info REAL,
            PRIMARY KEY (ticker, scan_date)
        )
    """)
    con.commit()
    # Migration — add new columns if upgrading from older DB
    for col in [("phase_velocity","REAL"), ("ridge_sharpness","REAL"),
                ("ridge_delta","REAL"), ("compression_debt","REAL"), ("fisher_info","REAL")]:
        try:
            cur.execute(f"ALTER TABLE scans ADD COLUMN {col[0]} {col[1]}")
            con.commit()
        except Exception:
            pass  # column already exists
    con.close()

def save_result(row: dict) -> None:
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO scans (
            ticker, scan_date, as_of_date, price, pct_5d, pct_20d,
            state, signal, stage, compression, energy, volume,
            cwt_cycle, cwt_slope, cwt_conc, cwt_conc_3d,
            exc_slope, exc_reversal, history_json, currency,
            phase_velocity, ridge_sharpness, ridge_delta,
            compression_debt, fisher_info
        ) VALUES (
            :ticker,:scan_date,:as_of_date,:price,:pct_5d,:pct_20d,
            :state,:signal,:stage,:compression,:energy,:volume,
            :cwt_cycle,:cwt_slope,:cwt_conc,:cwt_conc_3d,
            :exc_slope,:exc_reversal,:history_json,:currency,
            :phase_velocity,:ridge_sharpness,:ridge_delta,
            :compression_debt,:fisher_info
        )
    """, row)
    con.commit()
    con.close()

def load_latest() -> list[dict]:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("""
        SELECT s.* FROM scans s
        INNER JOIN (
            SELECT ticker, MAX(scan_date) as max_date
            FROM scans GROUP BY ticker
        ) latest ON s.ticker=latest.ticker AND s.scan_date=latest.max_date
        ORDER BY s.stage DESC, s.energy DESC
    """)
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    return rows

def load_custom_tickers_from_db() -> dict:
    """Load custom tickers added by user."""
    try:
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT * FROM custom_tickers")
        rows = {r["ticker"]: {"label": r["label"], "category": r["category"],
                               "currency": "CAD" if r["ticker"].endswith(".TO") else "USD"}
                for r in cur.fetchall()}
        con.close()
        return rows
    except Exception:
        return {}

def get_full_watchlist() -> dict:
    """Merge default watchlist with custom tickers."""
    wl = WATCHLIST.copy()
    wl.update(load_custom_tickers_from_db())
    return wl

# -----------------------------------------------------------------------
# Pipeline helpers
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

def _download(ticker: str) -> pd.DataFrame | None:
    for attempt in range(2):  # max 2 attempts — don't hammer on failure
        try:
            end   = date.today()
            start = end - timedelta(days=LOOKBACK_DAYS)
            raw   = yf.download(ticker, start=str(start), end=str(end),
                                auto_adjust=True, progress=False, timeout=15)
            if raw is None or raw.empty:
                if attempt == 0:
                    time.sleep(3)
                    continue
                return None
            raw.columns = [col[0].lower() if isinstance(col, tuple) else str(col).lower()
                           for col in raw.columns]
            raw = raw.reset_index()
            raw.columns = [col[0].lower() if isinstance(col, tuple) else str(col).lower()
                           for col in raw.columns]
            df = raw[["date","open","high","low","close","volume"]].dropna()
            return df.sort_values("date").reset_index(drop=True)
        except Exception:
            if attempt == 0:
                time.sleep(3)
                continue
            return None
    return None

def _true_range(df):
    pc = df["close"].shift(1)
    return pd.concat([df["high"]-df["low"],(df["high"]-pc).abs(),(df["low"]-pc).abs()],axis=1).max(axis=1)

def _cwt_pywt(x, min_c, max_c):
    """Original CWT using PyWavelets — with phase velocity via numpy."""
    r = {"dominant_cycle": np.nan, "total_energy": np.nan, "energy_concentration": np.nan,
         "phase_velocity": np.nan, "ridge_sharpness": np.nan}
    if not HAS_PYWT or len(x) < 8 or not np.all(np.isfinite(x)): return r
    x = x - np.mean(x)
    if np.allclose(x, 0): return r
    cycles = np.arange(min_c, max_c+1, dtype=float)
    try:
        coeffs, _ = pywt.cwt(x, cycles/1.03, CWT_WAVELET)
    except: return r
    power = np.abs(coeffs)**2
    lp = power[:, -1]
    total = float(np.sum(lp))
    if total <= 0: return r
    mp = float(np.mean(lp))
    peak_idx = int(np.argmax(lp))
    r["dominant_cycle"]       = float(cycles[peak_idx])
    r["total_energy"]         = total
    r["energy_concentration"] = lp[peak_idx] / mp if mp > 0 else np.nan
    r["ridge_sharpness"]      = r["energy_concentration"]

    # ── Phase velocity via numpy phase derivative ──
    # Rate of change of instantaneous frequency at the dominant cycle
    # Low = steady organic cycle, High = external shock / false signal
    try:
        phase = np.angle(coeffs[peak_idx])  # phase of dominant scale over time
        dphase = np.diff(phase)
        # Unwrap jumps
        dphase = np.where(dphase > np.pi,  dphase - 2*np.pi, dphase)
        dphase = np.where(dphase < -np.pi, dphase + 2*np.pi, dphase)
        inst_freq = np.abs(dphase) / (2 * np.pi)
        # Phase velocity = std of instantaneous frequency over recent bars
        # Low std = steady cycle, High std = erratic / noise
        recent = inst_freq[-min(10, len(inst_freq)):]
        if len(recent) > 2 and np.all(np.isfinite(recent)):
            r["phase_velocity"] = float(np.std(recent))
    except Exception:
        pass

    return r


def _cwt_sswt(x, min_c, max_c):
    """
    Synchrosqueezed Wavelet Transform — sharper frequency localisation.
    Falls back to pywt if ssqueezepy call fails.
    """
    r = {"dominant_cycle": np.nan, "total_energy": np.nan, "energy_concentration": np.nan,
         "phase_velocity": np.nan, "ridge_sharpness": np.nan}
    if len(x) < 8 or not np.all(np.isfinite(x)): return r
    x = x - np.mean(x)
    if np.allclose(x, 0): return r
    try:
        Wx, scales, _ = ssq.cwt(x, wavelet="morlet", scales="log")
        Tx, _, ssq_freqs, *_ = ssq.ssqueeze(Wx, scales, wavelet="morlet")
        power = np.abs(Tx)**2
        lp    = power[:, -1]
        total = float(np.sum(lp))
        if total <= 0: return _cwt_pywt(x, min_c, max_c)
        n          = len(x)
        cycle_bars = np.where(ssq_freqs > 0, n / (ssq_freqs * n), np.inf)
        valid = (cycle_bars >= min_c) & (cycle_bars <= max_c)
        if not np.any(valid): return _cwt_pywt(x, min_c, max_c)
        lp_valid    = lp[valid]
        cycles_valid= cycle_bars[valid]
        dom_idx     = np.argmax(lp_valid)
        mp          = float(np.mean(lp_valid))
        ridge_sharp = float(lp_valid[dom_idx] / mp) if mp > 0 else np.nan
        r["dominant_cycle"]       = float(cycles_valid[dom_idx])
        r["total_energy"]         = total
        r["energy_concentration"] = ridge_sharp
        r["ridge_sharpness"]      = ridge_sharp
        return r
    except Exception:
        return _cwt_pywt(x, min_c, max_c)


def _cwt(x, min_c, max_c):
    """Main CWT entry point. Uses SSWT if available, falls back to pywt."""
    if HAS_SSQ:
        return _cwt_sswt(x, min_c, max_c)
    return _cwt_pywt(x, min_c, max_c)

def _fft(x, min_c, max_c):
    if len(x)<8 or not np.all(np.isfinite(x)): return np.nan, np.nan
    x = x - np.mean(x)
    if np.allclose(x,0): return np.nan, np.nan
    n = len(x); power = np.abs(np.fft.rfft(x))**2; freqs = np.fft.rfftfreq(n,d=1.0)
    valid = (freqs>0)&(freqs>=1/max_c)&(freqs<=1/min_c)
    if not np.any(valid): return np.nan, np.nan
    vp=power[valid]; vf=freqs[valid]; pf=vf[np.argmax(vp)]
    return (float(1.0/pf) if pf>0 else np.nan), float(np.sum(vp))

def run_pipeline(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    w=cfg["window"]; aw=cfg["atr_window"]; sl=cfg["sustained_lookback"]
    emw=cfg["excursion_max_window"]; esw=cfg["excursion_slope_window"]
    out=df.copy()
    out["log_return"]        = np.log(out["close"]/out["close"].shift(1))
    out["true_range"]        = _true_range(out)
    out["atr"]               = out["true_range"].rolling(aw,min_periods=aw).mean()
    out["atr_mean"]          = out["atr"].rolling(w,min_periods=w).mean()
    out["compression_ratio"] = out["atr"]/out["atr_mean"]
    out["vol_mean"]          = out["volume"].rolling(w,min_periods=w).mean()
    out["volume_ratio"]      = out["volume"]/out["vol_mean"]
    returns=out["log_return"].to_numpy()
    cycles=[np.nan]*len(out); energies=[np.nan]*len(out); concs=[np.nan]*len(out)
    sharpness=[np.nan]*len(out); phase_vels=[np.nan]*len(out)
    for i in range(w,len(out)):
        seg=returns[i-w+1:i+1]
        if not np.all(np.isfinite(seg)): continue
        if HAS_SSQ or HAS_PYWT:
            r=_cwt(seg,2,w)
            cycles[i]=r["dominant_cycle"]
            energies[i]=r["total_energy"]
            concs[i]=r["energy_concentration"]
            sharpness[i]=r.get("ridge_sharpness", np.nan)
            phase_vels[i]=r.get("phase_velocity", np.nan)
        else:
            dc,en=_fft(seg,2,w); cycles[i]=dc; energies[i]=en
    out["cwt_dominant_cycle"]=cycles
    out["spectral_energy"]=energies
    out["cwt_energy_concentration"]=concs
    out["ridge_sharpness"]=sharpness
    out["phase_velocity"]=phase_vels
    # ridge_delta — rate of change of ridge sharpness (3-bar slope)
    out["ridge_delta"]=(out["ridge_sharpness"].rolling(3,min_periods=3)
                        .apply(lambda x: np.polyfit(np.arange(len(x)),x,1)[0], raw=True))

    # ── Compression Debt ──────────────────────────────────────────────
    # Time-integral of compression: ∑(compression_ratio × days_compressed)
    # Measures stored potential energy — how long and how tight the spring is wound
    # Resets when compression_ratio >= 1.0 (compression released)
    # Higher debt = more stored energy = larger expected breakout magnitude
    comp = out["compression_ratio"].to_numpy()
    debt = np.zeros(len(comp))
    running = 0.0
    for i in range(len(comp)):
        if np.isfinite(comp[i]) and comp[i] < 1.0:
            running += (1.0 - comp[i])  # debt accrues as gap below 1.0
        else:
            running = 0.0  # reset when compression released
        debt[i] = running
    out["compression_debt"] = debt

    # ── Fisher Information ─────────────────────────────────────────────
    # Measures information content of the CWT scalogram
    # High FI = regime is about to shift (system becoming more ordered)
    # Computed as FI = sum((dp/dt)^2 / p) where p is normalised power distribution
    fi_vals = [np.nan] * len(out)
    fi_window = 5  # bars to compute FI over
    for i in range(w + fi_window, len(out)):
        try:
            seg = returns[i - w + 1:i + 1]
            if not np.all(np.isfinite(seg)): continue
            coeffs_fi, _ = pywt.cwt(seg, np.arange(2, w+1, dtype=float)/1.03, CWT_WAVELET)
            power_fi = np.abs(coeffs_fi[:, -fi_window:]) ** 2
            # Normalise to probability distribution per time step
            col_sums = power_fi.sum(axis=0, keepdims=True)
            col_sums = np.where(col_sums > 0, col_sums, 1)
            p = power_fi / col_sums  # shape: (scales, fi_window)
            # Fisher Information: FI = sum((dp)^2 / p) across scales
            dp = np.diff(p, axis=1)  # (scales, fi_window-1)
            p_mid = p[:, :-1]
            with np.errstate(divide='ignore', invalid='ignore'):
                fi_terms = np.where(p_mid > 1e-10, dp**2 / p_mid, 0)
            fi_vals[i] = float(np.sum(fi_terms))
        except Exception:
            pass
    out["fisher_info"] = fi_vals
    out["energy_mean"]=out["spectral_energy"].rolling(w,min_periods=w).mean()
    out["energy_ratio"]=np.where(out["energy_mean"]>0,out["spectral_energy"]/out["energy_mean"],np.nan)
    out["cwt_cycle_slope"]=(out["cwt_dominant_cycle"].rolling(CWT_SLOPE_BACK,min_periods=CWT_SLOPE_BACK)
                            .apply(lambda x:np.polyfit(np.arange(len(x)),x,1)[0],raw=True))
    out["cwt_conc_min_3"]=out["cwt_energy_concentration"].rolling(sl,min_periods=sl).min()
    out["energy_excursion_max"]=out["energy_ratio"].rolling(emw,min_periods=emw).max()
    out["excursion_slope"]=(out["energy_excursion_max"].rolling(esw,min_periods=esw)
                            .apply(lambda x:np.polyfit(np.arange(len(x)),x,1)[0],raw=True))
    slope=out["excursion_slope"]
    out["excursion_reversal"]=(slope>0)&(slope.shift(1)<=0)&(slope.shift(2)<=0)
    out["market_state"]="insufficient_data"
    ready=(out["compression_ratio"].notna()&out["energy_ratio"].notna()&out["volume_ratio"].notna()
           &(pd.Series(np.arange(len(out))>=(2*w-1),index=out.index)))
    has_cwt=out["cwt_cycle_slope"].notna()&out["cwt_energy_concentration"].notna()
    has_sus=out["cwt_conc_min_3"].notna(); has_exc=out["excursion_reversal"].notna()
    breakout=ready&(out["energy_ratio"]>cfg["energy_breakout"])&(out["volume_ratio"]>cfg["volume_breakout"])
    expanding=ready&~breakout&(out["compression_ratio"]>=1.0)&(out["energy_ratio"]>cfg["energy_high"])&(out["volume_ratio"]>cfg["volume_high"])
    pre_breakout=(ready&has_cwt&~breakout&~expanding&(out["energy_ratio"]>cfg["prebreakout_energy_min"])
                  &(out["cwt_cycle_slope"]<cfg["prebreakout_cycle_slope_max"])
                  &(out["cwt_energy_concentration"]>cfg["prebreakout_concentration_min"])
                  &(out["volume_ratio"]>cfg["prebreakout_volume_min"]))
    early_watch=(ready&has_cwt&~breakout&~expanding&~pre_breakout
                 &(out["energy_ratio"]>cfg["earlywatch_energy_min"])
                 &(out["cwt_cycle_slope"]<cfg["earlywatch_cycle_slope_max"])
                 &(out["cwt_energy_concentration"]>cfg["earlywatch_concentration_min"])
                 &(out["volume_ratio"]>cfg["earlywatch_volume_min"]))
    sustained_focus=(ready&has_sus&~breakout&~expanding&~pre_breakout&~early_watch
                     &(out["cwt_conc_min_3"]>cfg["sustained_concentration_min"])
                     &(out["energy_ratio"]>cfg["sustained_energy_min"])
                     &(out["volume_ratio"]>cfg["sustained_volume_min"]))
    excursion_reversal=(ready&has_exc&~breakout&~expanding&~pre_breakout&~early_watch&~sustained_focus
                        &out["excursion_reversal"])
    coiling=(ready&~breakout&~expanding&~pre_breakout&~early_watch&~sustained_focus&~excursion_reversal
             &(out["compression_ratio"]<cfg["compressed_ratio_mild"])
             &(out["energy_ratio"]>=cfg["energy_low"])&(out["energy_ratio"]<cfg["energy_high"])
             &(out["volume_ratio"]<cfg["volume_high"]))
    compressed=(ready&~breakout&~expanding&~pre_breakout&~early_watch&~sustained_focus&~excursion_reversal&~coiling
                &(out["compression_ratio"]<cfg["compressed_ratio_strong"])&(out["energy_ratio"]<cfg["energy_low"]))
    neutral=ready&~(compressed|coiling|excursion_reversal|sustained_focus|early_watch|pre_breakout|breakout|expanding)
    out.loc[neutral,"market_state"]="neutral"; out.loc[compressed,"market_state"]="compressed"
    out.loc[coiling,"market_state"]="coiling"; out.loc[excursion_reversal,"market_state"]="excursion_reversal"
    out.loc[sustained_focus,"market_state"]="sustained_focus"; out.loc[early_watch,"market_state"]="early_watch"
    out.loc[pre_breakout,"market_state"]="pre_breakout"; out.loc[expanding,"market_state"]="expanding"
    out.loc[breakout,"market_state"]="breakout_state"
    out["ma_fast"]=out["close"].rolling(10,min_periods=10).mean()
    out["ma_slow"]=out["close"].rolling(30,min_periods=30).mean()
    is_exp=out["market_state"].isin({"expanding","breakout_state"})
    out["signal"]="no_trade_zone"
    out.loc[out["market_state"]=="excursion_reversal","signal"]="excursion_reversal_alert"
    out.loc[out["market_state"]=="sustained_focus","signal"]="sustained_focus_alert"
    out.loc[out["market_state"]=="early_watch","signal"]="early_watch_alert"
    out.loc[out["market_state"]=="pre_breakout","signal"]="watch_pre_breakout"
    out.loc[is_exp&(out["ma_fast"]>out["ma_slow"]),"signal"]="bullish_breakout_bias"
    out.loc[is_exp&(out["ma_fast"]<out["ma_slow"]),"signal"]="bearish_breakout_bias"
    out.loc[is_exp&(out["ma_fast"]==out["ma_slow"]),"signal"]="expansion_no_bias"
    return out

# -----------------------------------------------------------------------
# Main scan
# -----------------------------------------------------------------------

def run_scan(tickers=None):
    init_db()
    try:
        from tsunami_trades import init_trades_tables
        init_trades_tables()
    except Exception:
        pass
    watchlist = get_full_watchlist()
    if tickers is None:
        tickers = list(watchlist.keys())

    today   = date.today().isoformat()
    results = []

    for ticker in tickers:
        info = watchlist.get(ticker, {"label": ticker, "category": "Other", "currency": "USD"})

        # Skip FX rate from full pipeline — just track price
        if info.get("category") == "FX":
            print(f"  Scanning {ticker:12} (FX rate)... ", end="", flush=True)
            try:
                raw = yf.download(ticker, period="2d", progress=False)
                if not raw.empty:
                    raw.columns = [col[0].lower() if isinstance(col,tuple) else str(col).lower() for col in raw.columns]
                    price = float(raw["close"].iloc[-1])
                    row = {"ticker":ticker,"scan_date":today,"as_of_date":today,"price":price,
                           "pct_5d":None,"pct_20d":None,"state":"neutral","signal":"no_trade_zone",
                           "stage":0,"compression":None,"energy":None,"volume":None,
                           "cwt_cycle":None,"cwt_slope":None,"cwt_conc":None,"cwt_conc_3d":None,
                           "exc_slope":None,"exc_reversal":0,"history_json":"[]",
                           "currency":info.get("currency","USD"),
                           "phase_velocity":None,"ridge_sharpness":None,"ridge_delta":None,
                           "compression_debt":None,"fisher_info":None}
                    save_result(row)
                    results.append(row)
                    print(f"CA$1 = US${price:.4f}")
            except Exception as e:
                print(f"❌ {e}")
            continue

        print(f"  Scanning {ticker:12} ({info['label']})...", end=" ", flush=True)
        df = _download(ticker)
        if df is None or len(df) < 100:
            print("❌ No data")
            continue

        cfg = get_cfg(ticker)
        out = run_pipeline(df, cfg)
        row = out.iloc[-1]

        def fv(v):
            try: f=float(v); return round(f,6) if np.isfinite(f) else None
            except: return None

        pct_5d  = (float(out["close"].iloc[-1])/float(out["close"].iloc[-6])-1)*100  if len(out)>=6  else None
        pct_20d = (float(out["close"].iloc[-1])/float(out["close"].iloc[-21])-1)*100 if len(out)>=21 else None

        history = out.tail(60)[["close","compression_ratio","cwt_cycle_slope",
                                 "energy_ratio","volume_ratio","market_state"]].copy()
        history["date"] = out["date"].tail(60).dt.strftime("%Y-%m-%d").values
        history_json    = history.to_json(orient="records")

        state  = str(row["market_state"])
        stage  = STATE_STAGE.get(state, 0)
        result = {
            "ticker":         ticker,
            "scan_date":      today,
            "as_of_date":     pd.to_datetime(row["date"]).strftime("%Y-%m-%d"),
            "price":          fv(row["close"]),
            "pct_5d":         round(pct_5d,2)  if pct_5d  is not None else None,
            "pct_20d":        round(pct_20d,2) if pct_20d is not None else None,
            "state":          state,
            "signal":          str(row["signal"]),
            "stage":           stage,
            "compression":     fv(row["compression_ratio"]),
            "energy":          fv(row["energy_ratio"]),
            "volume":          fv(row["volume_ratio"]),
            "cwt_cycle":       fv(row["cwt_dominant_cycle"]),
            "cwt_slope":       fv(row["cwt_cycle_slope"]),
            "cwt_conc":        fv(row["cwt_energy_concentration"]),
            "cwt_conc_3d":     fv(row["cwt_conc_min_3"]),
            "exc_slope":       fv(row["excursion_slope"]),
            "exc_reversal":    int(bool(row["excursion_reversal"])),
            "history_json":    history_json,
            "currency":        info.get("currency","USD"),
            "phase_velocity":   fv(row.get("phase_velocity", np.nan)),
            "ridge_sharpness":  fv(row.get("ridge_sharpness", np.nan)),
            "ridge_delta":      fv(row.get("ridge_delta", np.nan)),
            "compression_debt": fv(row.get("compression_debt", np.nan)),
            "fisher_info":      fv(row.get("fisher_info", np.nan)),
        }

        save_result(result)
        results.append(result)
        stage_str = ["·","·","🔄","👁 ","⚡","🚀"][min(stage,5)]
        print(f"{stage_str} Stage {stage} — {state}")
        time.sleep(1.0)  # throttle — prevents fd exhaustion with large watchlist

    print(f"\n✅ Scan complete — {len(results)}/{len(tickers)} assets processed")
    return results


if __name__ == "__main__":
    print(f"\n🌊 Tsunami Engine — {date.today().strftime('%A %B %d %Y')}")
    print("=" * 50)
    run_scan()
