"""
tsunami_backtest.py
-------------------
Full trade lifecycle backtest for Tsunami.
"""
from __future__ import annotations
import argparse, sys
from datetime import date, timedelta
import numpy as np
import pandas as pd
import yfinance as yf

try:
    from tsunami_engine import run_pipeline, get_cfg, STATE_STAGE
except ImportError:
    print("❌ tsunami_engine.py not found — run this from your Downloads folder")
    sys.exit(1)

ATR_MULT        = 2.0
ATR_WINDOW      = 14
MAX_HOLD_DAYS   = 10    # fixed time stop
MIN_STAGE       = 3
MIN_HOLD_DAYS   = 3     # min days before stage collapse exit
LONG_ONLY       = True
MIN_ENERGY      = 1.0   # only enter if energy ratio is above this and rising
ENTRY_SIGNALS   = {"bullish_breakout_bias", "bearish_breakout_bias"}
RISK_BANDS      = [(86,100,0.015),(76,85,0.010),(65,75,0.005),(0,64,0.005)]

def conviction_score(r):
    s = 0
    s += int(r.get("stage") or 0) * 8
    if r.get("exc_reversal"): s += 20
    comp = r.get("compression")
    if comp is not None:
        try:
            c = float(comp)
            if 0 < c < 0.80: s += 15
            elif 0 < c < 0.88: s += 10
            elif 0 < c < 0.95: s += 5
        except: pass
    slope = r.get("cwt_slope")
    if slope is not None:
        try:
            sl = float(slope)
            if sl < -3.0: s += 15
            elif sl < -1.5: s += 8
            elif sl < 0: s += 3
        except: pass
    conc = r.get("cwt_conc_3d")
    if conc is not None:
        try:
            cn = float(conc)
            if cn > 5.0: s += 10
            elif cn > 3.0: s += 5
        except: pass
    return min(int(s), 100)

def risk_pct(conviction):
    for lo, hi, pct in RISK_BANDS:
        if lo <= conviction <= hi: return pct
    return 0.005

def calc_atr(df, window=ATR_WINDOW):
    try:
        pc = df["close"].shift(1)
        tr = pd.concat([df["high"]-df["low"],(df["high"]-pc).abs(),(df["low"]-pc).abs()],axis=1).max(axis=1)
        atr = float(tr.rolling(window).mean().iloc[-1])
        return atr if np.isfinite(atr) else None
    except: return None

def position_size(entry, stop, conviction, portfolio):
    r = risk_pct(conviction)
    dollar_risk = portfolio * r
    rps = abs(entry - stop)
    if rps <= 0: return {"shares":0,"dollar_risk":0,"position_value":0,"risk_pct":r}
    shares = dollar_risk / rps
    return {"shares":round(shares,2),"dollar_risk":round(dollar_risk,2),
            "position_value":round(shares*entry,2),"risk_pct":r}

def download_full(ticker, start, end):
    pre = (date.fromisoformat(start) - timedelta(days=365*3)).isoformat()
    print(f"  Downloading {ticker} ({pre} → {end})...")
    try:
        raw = yf.download(ticker, start=pre, end=end, auto_adjust=True, progress=False, timeout=30)
        if raw is None or raw.empty: return None
        raw.columns = [col[0].lower() if isinstance(col,tuple) else str(col).lower() for col in raw.columns]
        raw = raw.reset_index()
        raw.columns = [col[0].lower() if isinstance(col,tuple) else str(col).lower() for col in raw.columns]
        df = raw[["date","open","high","low","close","volume"]].dropna()
        df = df.sort_values("date").reset_index(drop=True)
        df["date"] = pd.to_datetime(df["date"])
        return df
    except Exception as e:
        print(f"  ❌ {e}"); return None

def run_backtest(ticker, start, end, min_conviction=0, portfolio=50000.0):
    cfg     = get_cfg(ticker)
    full_df = download_full(ticker, start, end)
    if full_df is None: return []

    start_dt = pd.Timestamp(start)
    end_dt   = pd.Timestamp(end)
    trading_days = full_df[(full_df["date"]>=start_dt)&(full_df["date"]<=end_dt)]["date"].tolist()
    if not trading_days:
        print("❌ No trading days in range."); return []

    all_dates   = full_df["date"].tolist()
    date_to_idx = {d:i for i,d in enumerate(all_dates)}
    opens       = dict(zip(full_df["date"], full_df["open"]))
    closes      = dict(zip(full_df["date"], full_df["close"]))
    lows        = dict(zip(full_df["date"], full_df["low"]))

    print(f"\n📅 Trade simulation: {ticker} | {start} → {end}")
    print(f"   Portfolio: ${portfolio:,.0f} | Min conviction: {min_conviction}")
    print(f"   Entry: next open | Stop: 2×ATR | Time stop: {MAX_HOLD_DAYS} days | Min energy: {MIN_ENERGY}")
    print("─" * 70)

    trades   = []
    in_trade = False
    trade    = {}

    for today in trading_days:

        # ── IN TRADE: check exits ──────────────────────────────────────
        if in_trade:
            close = closes.get(today)
            low   = lows.get(today)
            if close is None: continue

            days_held   = (today - pd.Timestamp(trade["entry_date"])).days
            direction   = trade["direction"]
            stop        = trade["stop_price"]
            exit_reason = None
            exit_price  = close

            # 1. Stop hit — check low for intraday
            if direction == "long" and low is not None and low <= stop:
                exit_reason = "🛑 Stop hit"
                exit_price  = stop

            # 2. Time stop
            if not exit_reason and days_held >= MAX_HOLD_DAYS:
                exit_reason = f"⏱ Time stop ({days_held}d)"

            # 3. Stage collapse — only after minimum hold period
            if not exit_reason and days_held >= MIN_HOLD_DAYS:
                try:
                    sl  = full_df[full_df["date"]<=today].copy().reset_index(drop=True)
                    out = run_pipeline(sl, cfg)
                    stage = STATE_STAGE.get(str(out.iloc[-1]["market_state"]), 0)
                    if stage < 2:
                        exit_reason = f"📉 Stage collapsed ({out.iloc[-1]['market_state']})"
                except: pass

            if exit_reason:
                entry  = trade["entry_price"]
                shares = trade["shares"]
                if direction == "long":
                    pnl     = (exit_price - entry) * shares
                    pnl_pct = (exit_price / entry - 1) * 100
                else:
                    pnl     = (entry - exit_price) * shares
                    pnl_pct = (entry / exit_price - 1) * 100

                trade.update({"exit_date":today.strftime("%Y-%m-%d"),"exit_price":round(exit_price,4),
                               "exit_reason":exit_reason,"days_held":days_held,
                               "pnl":round(pnl,2),"pnl_pct":round(pnl_pct,2)})
                trades.append(trade)
                in_trade = False
                trade    = {}
                result   = "✅ WIN " if pnl > 0 else "❌ LOSS"
                print(f"  EXIT  {today.strftime('%Y-%m-%d')}  {result}  {exit_reason:30}  "
                      f"${exit_price:>8,.2f}  P&L: {'+'if pnl>0 else ''}{pnl:>8,.2f}  ({pnl_pct:+.1f}%)")
            continue

        # ── NOT IN TRADE: scan for signal ─────────────────────────────
        slice_df = full_df[full_df["date"]<=today].copy().reset_index(drop=True)
        if len(slice_df) < 100: continue

        try:
            out    = run_pipeline(slice_df, cfg)
            row    = out.iloc[-1]
            state  = str(row["market_state"])
            stage  = STATE_STAGE.get(state, 0)
            signal = str(row["signal"])
        except: continue

        if stage < MIN_STAGE or signal not in ENTRY_SIGNALS: continue
        direction = "long" if "bullish" in signal else "short"
        if LONG_ONLY and direction == "short": continue

        def fv(v):
            try: f=float(v); return round(f,6) if np.isfinite(f) else None
            except: return None

        r = {"compression":fv(row["compression_ratio"]),"energy":fv(row["energy_ratio"]),
             "volume":fv(row["volume_ratio"]),"cwt_slope":fv(row["cwt_cycle_slope"]),
             "cwt_conc":fv(row["cwt_energy_concentration"]),"cwt_conc_3d":fv(row["cwt_conc_min_3"]),
             "exc_reversal":int(bool(row["excursion_reversal"])),"stage":stage}
        conv = conviction_score(r)
        if conv < min_conviction: continue

        # Energy filter — only enter if energy is above MIN_ENERGY and rising
        energy_now  = fv(row["energy_ratio"])
        if energy_now is None or energy_now < MIN_ENERGY: continue
        # Check energy is rising — compare to 3 bars ago
        if len(out) >= 4:
            energy_prev = fv(out.iloc[-4]["energy_ratio"])
            if energy_prev is not None and energy_now <= energy_prev: continue

        # Entry: next day open
        idx = date_to_idx.get(today)
        if idx is None or idx+1 >= len(all_dates): continue
        entry_date  = all_dates[idx+1]
        entry_price = opens.get(entry_date)
        if not entry_price or entry_price <= 0: continue

        atr        = calc_atr(slice_df) or entry_price * 0.02
        stop_price = (entry_price - atr*ATR_MULT) if direction=="long" else (entry_price + atr*ATR_MULT)
        sizing     = position_size(entry_price, stop_price, conv, portfolio)
        if sizing["shares"] <= 0: continue

        trade = {"ticker":ticker,"signal_date":today.strftime("%Y-%m-%d"),
                 "entry_date":entry_date.strftime("%Y-%m-%d"),"state":state,"stage":stage,
                 "signal":signal,"conviction":conv,"direction":direction,
                 "entry_price":round(entry_price,4),"stop_price":round(stop_price,4),
                 "atr":round(atr,4),"shares":sizing["shares"],"dollar_risk":sizing["dollar_risk"],
                 "position_value":sizing["position_value"]}
        in_trade = True
        print(f"  ENTER {entry_date.strftime('%Y-%m-%d')}  {direction.upper():5}  conv:{conv:3}  "
              f"${entry_price:>8,.2f}  stop:${stop_price:>8,.2f}  "
              f"shares:{sizing['shares']:>7,.1f}  risk:${sizing['dollar_risk']:>7,.0f}")

    # Close open trade at end of period
    if in_trade and trading_days:
        last   = trading_days[-1]
        ep     = closes.get(last, trade["entry_price"])
        entry  = trade["entry_price"]
        shares = trade["shares"]
        direction = trade["direction"]
        pnl     = (ep-entry)*shares if direction=="long" else (entry-ep)*shares
        pnl_pct = (ep/entry-1)*100 if direction=="long" else (entry/ep-1)*100
        days_held = (last - pd.Timestamp(trade["entry_date"])).days
        trade.update({"exit_date":last.strftime("%Y-%m-%d"),"exit_price":round(ep,4),
                      "exit_reason":"⏹ End of period","days_held":days_held,
                      "pnl":round(pnl,2),"pnl_pct":round(pnl_pct,2)})
        trades.append(trade)

    print(f"\n  → {len(trades)} trades completed")
    return trades

def print_scorecard(trades, ticker, start, end, min_conviction, portfolio):
    if not trades:
        print("\n⚠️  No trades."); return

    closed   = [t for t in trades if "pnl" in t]
    wins     = [t for t in closed if t["pnl"] > 0]
    losses   = [t for t in closed if t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in closed)
    pnl_pcts  = [t["pnl_pct"] for t in closed]
    win_rate  = len(wins)/len(closed)*100 if closed else 0
    gross_win = sum(t["pnl"] for t in wins) if wins else 0
    gross_loss= abs(sum(t["pnl"] for t in losses)) if losses else 1
    pf        = gross_win/gross_loss if gross_loss > 0 else float("inf")
    avg_hold  = sum(t.get("days_held",0) for t in closed)/len(closed) if closed else 0

    reasons = {}
    for t in closed:
        r = t.get("exit_reason","Unknown"); reasons[r] = reasons.get(r,0)+1

    print("\n"+"═"*70)
    print(f"  🌊 TSUNAMI TRADE SIMULATION — {ticker}")
    print(f"  {start} → {end} | Portfolio: ${portfolio:,.0f} | Min conviction: {min_conviction}")
    print("═"*70)
    print(f"\n  Total trades   : {len(closed)}")
    print(f"  Wins / Losses  : {len(wins)}W / {len(losses)}L")
    print(f"  Win rate       : {win_rate:.1f}%")
    print(f"  Total P&L      : {'+'if total_pnl>=0 else ''}{total_pnl:,.2f}")
    print(f"  Mean P&L/trade : {'+'if sum(t['pnl'] for t in closed)/len(closed)>=0 else ''}{sum(t['pnl'] for t in closed)/len(closed):,.2f}")
    print(f"  Mean return    : {sum(pnl_pcts)/len(pnl_pcts):+.2f}% per trade")
    print(f"  Best trade     : +{max(pnl_pcts):.2f}%")
    print(f"  Worst trade    : {min(pnl_pcts):.2f}%")
    print(f"  Profit factor  : {pf:.2f}")
    print(f"  Avg hold       : {avg_hold:.1f} days")

    print(f"\n  ── Exit Reasons ──")
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"      {reason:35} {count:3} trades")

    print(f"\n  ── Trade Log ──")
    print(f"  {'Signal':12} {'Entry':12} {'Exit':12} {'Dir':5} {'Conv':5} "
          f"{'Entry$':>9} {'Exit$':>9} {'Stop$':>9} {'Days':>5} {'P&L':>10} {'%':>7}  Reason")
    print("  "+"─"*115)
    for t in closed:
        pnl_s = f"{'+'if t['pnl']>=0 else ''}{t['pnl']:,.2f}"
        pct_s = f"{t['pnl_pct']:+.1f}%"
        print(f"  {t['signal_date']:12} {t['entry_date']:12} {t.get('exit_date','—'):12} "
              f"{t['direction'].upper():5} {t['conviction']:<5} "
              f"${t['entry_price']:>8,.2f} ${t.get('exit_price',0):>8,.2f} "
              f"${t['stop_price']:>8,.2f} {t.get('days_held',0):>5} "
              f"{pnl_s:>10} {pct_s:>7}  {t.get('exit_reason','')}")

    print("\n"+"═"*70)
    print("  ✅ Simulation complete")
    print("═"*70)

def main():
    parser = argparse.ArgumentParser(description="🌊 Tsunami Trade Simulator")
    parser.add_argument("--ticker",         required=True)
    parser.add_argument("--start",          required=True)
    parser.add_argument("--end",            required=True)
    parser.add_argument("--min-conviction", type=int,   default=0)
    parser.add_argument("--portfolio",      type=float, default=50000.0)
    args = parser.parse_args()

    trades = run_backtest(args.ticker.upper(), args.start, args.end,
                          args.min_conviction, args.portfolio)
    print_scorecard(trades, args.ticker.upper(), args.start, args.end,
                    args.min_conviction, args.portfolio)

if __name__ == "__main__":
    main()
