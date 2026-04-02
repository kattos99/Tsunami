"""
tsunami_ridge_debug.py
Show historical ridge_sharpness and ridge_delta for a ticker
around specific entry dates.

Usage:
  python3 tsunami_ridge_debug.py --ticker NVDA --start 2024-01-01 --end 2024-07-01
"""
import argparse
import numpy as np
import pandas as pd
import yfinance as yf
import sys

try:
    from tsunami_engine import run_pipeline, get_cfg
except ImportError:
    print("❌ Run this from your Downloads folder")
    sys.exit(1)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--start",  required=True)
    parser.add_argument("--end",    required=True)
    args = parser.parse_args()

    ticker = args.ticker.upper()
    print(f"\n🔬 Ridge Debug — {ticker} | {args.start} → {args.end}")
    print("─" * 70)

    # Download with extra history for pipeline warmup
    dl_start = pd.to_datetime(args.start) - pd.DateOffset(years=3)
    raw = yf.download(ticker, start=dl_start.strftime("%Y-%m-%d"),
                      end=args.end, progress=False, auto_adjust=True)
    if raw.empty:
        print("❌ No data"); return

    raw.columns = [c[0].lower() if isinstance(c, tuple) else str(c).lower()
                   for c in raw.columns]
    raw = raw.reset_index()
    raw.columns = [str(c).lower() for c in raw.columns]
    if "date" not in raw.columns and "datetime" in raw.columns:
        raw = raw.rename(columns={"datetime":"date"})
    raw["date"] = pd.to_datetime(raw["date"])

    cfg = get_cfg(ticker)
    out = run_pipeline(raw, cfg)

    # Filter to requested date range
    mask = (out["date"] >= args.start) & (out["date"] <= args.end)
    sub  = out[mask].copy()

    print(f"{'Date':12} {'Stage':>5} {'State':20} {'Ridge':>7} {'Delta':>8} {'PhaseVel':>10} {'C.Debt':>8} {'Fisher':>8}")
    print("─" * 85)
    for _, row in sub.iterrows():
        date_str = pd.to_datetime(row["date"]).strftime("%Y-%m-%d")
        state    = str(row.get("market_state","—"))[:18]
        ridge    = row.get("ridge_sharpness", np.nan)
        delta    = row.get("ridge_delta", np.nan)
        pvel     = row.get("phase_velocity", np.nan)
        cdebt    = row.get("compression_debt", np.nan)
        fisher   = row.get("fisher_info", np.nan)

        ridge_s  = f"{ridge:7.2f}" if ridge is not None and np.isfinite(float(ridge)) else "    n/a"
        delta_s  = f"{delta:+8.3f}" if delta is not None and np.isfinite(float(delta)) else "     n/a"
        pvel_s   = f"{pvel:10.5f}" if pvel is not None and np.isfinite(float(pvel)) else "       n/a"
        cdebt_s  = f"{cdebt:8.2f}" if cdebt is not None and np.isfinite(float(cdebt)) else "     n/a"
        fisher_s = f"{fisher:8.4f}" if fisher is not None and np.isfinite(float(fisher)) else "     n/a"

        from tsunami_engine import STATE_STAGE
        s = STATE_STAGE.get(state, 0)
        flag = " ◀ ENTRY" if s >= 5 else ""

        print(f"{date_str:12} {s:>5} {state:20} {ridge_s} {delta_s} {pvel_s} {cdebt_s} {fisher_s}{flag}")

    print("\n✅ Done — look for ridge_delta on the ◀ ENTRY rows")

if __name__ == "__main__":
    main()
