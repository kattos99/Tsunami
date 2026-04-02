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
MAX_HOLD_DAYS   = 10
MIN_STAGE       = 3
MIN_HOLD_DAYS   = 3
LONG_ONLY       = True
MIN_ENERGY      = 1.0
MIN_RIDGE       = 0.0   # min ridge sharpness to enter (0 = disabled)
MAX_PHASE_VEL   = 0.0   # max phase velocity to enter (0 = disabled)
MIN_RIDGE_DELTA = 0.0   # min ridge delta (0 = disabled)
AVOID_MATURE_COLLAPSE = False  # skip entry when ridge > 6 AND delta < 0
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
             "exc_reversal":int(bool(row["excursion_reversal"])),"stage":stage,
             "ridge_sharpness":fv(row.get("ridge_sharpness")),"phase_velocity":fv(row.get("phase_velocity"))}
        conv = conviction_score(r)
        if conv < min_conviction: continue

        # Energy filter — only enter if energy is above MIN_ENERGY and rising
        energy_now  = fv(row["energy_ratio"])
        if energy_now is None or energy_now < MIN_ENERGY: continue
        # Check energy is rising — compare to 3 bars ago
        if len(out) >= 4:
            energy_prev = fv(out.iloc[-4]["energy_ratio"])
            if energy_prev is not None and energy_now <= energy_prev: continue

        # Ridge sharpness filter — skip if cycle is noisy (optional)
        if MIN_RIDGE > 0:
            ridge = fv(row.get("ridge_sharpness"))
            if ridge is not None and ridge < MIN_RIDGE: continue

        # Phase velocity filter — skip if cycle is erratic (optional)
        if MAX_PHASE_VEL > 0:
            pv = fv(row.get("phase_velocity"))
            if pv is not None and pv > MAX_PHASE_VEL: continue

        # Ridge delta filter — only enter if cycle is actively sharpening (optional)
        if MIN_RIDGE_DELTA != 0:
            rd = fv(row.get("ridge_delta"))
            if rd is not None and rd < MIN_RIDGE_DELTA: continue

        # Avoid mature collapse — skip when ridge is high AND falling (universal avoid)
        if AVOID_MATURE_COLLAPSE:
            ridge = fv(row.get("ridge_sharpness"))
            rd    = fv(row.get("ridge_delta"))
            if ridge is not None and rd is not None and ridge > 6.0 and rd < 0:
                continue

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

def print_scorecard(trades, ticker, start, end, min_conviction, portfolio, label=""):
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

    tag = f" [{label}]" if label else ""
    print("\n"+"═"*70)
    print(f"  🌊 TSUNAMI TRADE SIMULATION — {ticker}{tag}")
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
    return {"win_rate": win_rate, "profit_factor": pf, "total_pnl": total_pnl,
            "mean_pct": sum(pnl_pcts)/len(pnl_pcts) if pnl_pcts else 0,
            "n_trades": len(closed)}


# -----------------------------------------------------------------------
# DSR — Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014)
# -----------------------------------------------------------------------

def calc_dsr(trades: list[dict], n_trials: int = 1) -> float | None:
    """
    Deflated Sharpe Ratio — adjusts observed Sharpe for the number of
    strategy variants tested. Accounts for multiple testing bias.
    DSR >= 0.95 is the institutional gate.

    n_trials: number of parameter combinations tested (default 1 for single run,
              9 for perturbation test)
    """
    closed = [t for t in trades if "pnl" in t]
    if len(closed) < 5:
        return None

    rets = np.array([t["pnl_pct"] / 100 for t in closed])
    n    = len(rets)
    mu   = float(np.mean(rets))
    sig  = float(np.std(rets, ddof=1))
    if sig == 0:
        return None

    sr_obs = mu / sig * np.sqrt(252 / max(1, np.mean([t.get("days_held",5) for t in closed])))

    # Expected maximum Sharpe under null (no skill) across n_trials
    # Approximation: E[max SR] ≈ (1 - euler_gamma) * Z^-1(1 - 1/n_trials) + euler_gamma * Z^-1(1 - 1/(n_trials*e))
    from scipy import stats
    euler_gamma = 0.5772
    if n_trials <= 1:
        sr_benchmark = 0.0
    else:
        z1 = stats.norm.ppf(1 - 1/n_trials)
        z2 = stats.norm.ppf(1 - 1/(n_trials * np.e))
        sr_benchmark = (1 - euler_gamma) * z1 + euler_gamma * z2

    # Adjust for skewness and kurtosis
    skew = float(pd.Series(rets).skew()) if n > 3 else 0
    kurt = float(pd.Series(rets).kurtosis()) if n > 3 else 0

    sr_adj = sr_obs * np.sqrt((1 - skew*sr_obs + (kurt-1)/4 * sr_obs**2) / (n-1))

    # DSR = P(SR_true > SR_benchmark)
    dsr = float(stats.norm.cdf((sr_adj - sr_benchmark) / np.sqrt(1/n)))
    return round(dsr, 4)


# -----------------------------------------------------------------------
# Perturbation stability test
# -----------------------------------------------------------------------

def run_perturbation_test(ticker: str, start: str, end: str,
                          min_conviction: int, portfolio: float) -> dict:
    """
    Run 9 parameter combinations (3 ATR × 3 energy thresholds).
    Reports how many are profitable — gate is 5/9.
    """
    global ATR_MULT, MIN_ENERGY

    atr_variants    = [1.5, 2.0, 2.5]
    energy_variants = [0.9, 1.0, 1.1]

    print(f"\n  🔬 Perturbation Stability Test — {ticker}")
    print(f"  Testing 9 parameter combinations (3 ATR × 3 energy threshold)")
    print("  " + "─"*55)
    print(f"  {'ATR':>6} {'Energy':>8} {'Trades':>7} {'Win%':>7} {'PF':>7} {'P&L':>10}  Pass")
    print("  " + "─"*55)

    orig_atr    = ATR_MULT
    orig_energy = MIN_ENERGY
    passes      = 0
    results     = []

    for atr_v in atr_variants:
        for energy_v in energy_variants:
            ATR_MULT   = atr_v
            MIN_ENERGY = energy_v
            try:
                trades = run_backtest(ticker, start, end, min_conviction, portfolio)
                closed = [t for t in trades if "pnl" in t]
                if not closed:
                    print(f"  {atr_v:>6.1f}× {energy_v:>8.1f} {'0':>7} {'—':>7} {'—':>7} {'—':>10}  ✗")
                    results.append(False)
                    continue
                wins  = [t for t in closed if t["pnl"] > 0]
                losses= [t for t in closed if t["pnl"] <= 0]
                wr    = len(wins)/len(closed)*100
                gw    = sum(t["pnl"] for t in wins) if wins else 0
                gl    = abs(sum(t["pnl"] for t in losses)) if losses else 0
                pf    = gw/gl if gl > 0 else float("inf")
                pnl   = sum(t["pnl"] for t in closed)
                passed= pnl > 0 and pf > 1.0
                if passed: passes += 1
                results.append(passed)
                p_str = "✅" if passed else "✗"
                pf_str= f"{pf:.2f}" if pf != float("inf") else "∞"
                print(f"  {atr_v:>6.1f}× {energy_v:>8.1f} {len(closed):>7} {wr:>6.1f}% {pf_str:>7} {pnl:>+10.0f}  {p_str}")
            except Exception as e:
                print(f"  {atr_v:>6.1f}× {energy_v:>8.1f}  ERROR: {e}")
                results.append(False)

    # Restore originals
    ATR_MULT   = orig_atr
    MIN_ENERGY = orig_energy

    gate_pass = passes >= 5
    print(f"\n  Passed: {passes}/9  |  Gate (5/9): {'✅ PASS' if gate_pass else '❌ FAIL'}")
    return {"passes": passes, "total": 9, "gate": gate_pass, "results": results}


# -----------------------------------------------------------------------
# OOS comparison summary
# -----------------------------------------------------------------------

def print_oos_comparison(is_result: dict, oos_result: dict, ticker: str, oos_split: str) -> None:
    """Print a clean IS vs OOS comparison table."""
    print("\n" + "═"*70)
    print(f"  🔬 WALK-FORWARD OOS ANALYSIS — {ticker}")
    print(f"  OOS split: {oos_split}  (IS = before, OOS = after)")
    print("═"*70)

    metrics = [
        ("Trades",       f"{is_result['n_trades']}",               f"{oos_result['n_trades']}"),
        ("Win Rate",     f"{is_result['win_rate']:.1f}%",          f"{oos_result['win_rate']:.1f}%"),
        ("Profit Factor",f"{is_result['profit_factor']:.2f}",      f"{oos_result['profit_factor']:.2f}"),
        ("Total P&L",    f"${is_result['total_pnl']:+,.2f}",       f"${oos_result['total_pnl']:+,.2f}"),
        ("Mean Return",  f"{is_result['mean_pct']:+.2f}%",         f"{oos_result['mean_pct']:+.2f}%"),
    ]

    if "dsr" in is_result:
        metrics.append(("DSR",  f"{is_result['dsr']:.4f}" if is_result['dsr'] else "—",
                                 f"{oos_result['dsr']:.4f}" if oos_result.get('dsr') else "—"))

    print(f"\n  {'Metric':20} {'IN-SAMPLE (IS)':>20} {'OUT-OF-SAMPLE (OOS)':>20}")
    print("  " + "─"*62)
    for metric, is_val, oos_val in metrics:
        print(f"  {metric:20} {is_val:>20} {oos_val:>20}")

    # Verdict
    print("\n  ── Verdict ──")
    oos_pf  = oos_result["profit_factor"]
    is_pf   = is_result["profit_factor"]
    oos_pnl = oos_result["total_pnl"]

    if oos_pf >= 1.5 and oos_pnl > 0:
        verdict = "✅ STRONG — OOS edge confirmed. Profit factor holds out-of-sample."
    elif oos_pf >= 1.0 and oos_pnl > 0:
        verdict = "⚠️  MARGINAL — OOS profitable but weaker than IS. Monitor closely."
    elif oos_pnl > 0:
        verdict = "⚠️  WEAK — OOS profitable but profit factor < 1. May be noise."
    else:
        verdict = "❌ FAILED — OOS not profitable. IS results likely overfitted."

    ratio = oos_pf / is_pf if is_pf > 0 else 0
    print(f"  {verdict}")
    print(f"  OOS/IS profit factor ratio: {ratio:.2f}  (>0.5 = acceptable degradation)")
    print("═"*70)


def main():
    parser = argparse.ArgumentParser(description="🌊 Tsunami Trade Simulator")
    parser.add_argument("--ticker",         required=True)
    parser.add_argument("--start",          required=True)
    parser.add_argument("--end",            required=True)
    parser.add_argument("--min-conviction", type=int,   default=0)
    parser.add_argument("--portfolio",      type=float, default=50000.0)
    parser.add_argument("--oos-split",      type=str,   default=None,
                        help="Date to split IS/OOS e.g. 2024-01-01")
    parser.add_argument("--perturbation",   action="store_true")
    parser.add_argument("--dsr",            action="store_true")
    parser.add_argument("--min-ridge",       type=float, default=0.0,
                        help="Minimum ridge sharpness to enter. 0 = disabled.")
    parser.add_argument("--max-phase-vel",   type=float, default=0.0,
                        help="Maximum phase velocity to enter. 0 = disabled.")
    parser.add_argument("--min-ridge-delta",       type=float, default=0.0,
                        help="Minimum ridge delta to enter. 0 = disabled.")
    parser.add_argument("--avoid-mature-collapse", action="store_true",
                        help="Skip entry when ridge > 6 AND delta < 0 (mature cycle collapsing).")
    parser.add_argument("--max-hold",              type=int,   default=0,
                        help="Override time stop in days. 0 = use default (10).")
    args = parser.parse_args()
    ticker = args.ticker.upper()

    # Apply optional filters
    global MIN_RIDGE, MAX_PHASE_VEL, MAX_HOLD_DAYS, MIN_RIDGE_DELTA, AVOID_MATURE_COLLAPSE
    MIN_RIDGE             = args.min_ridge
    MAX_PHASE_VEL         = args.max_phase_vel
    MIN_RIDGE_DELTA       = args.min_ridge_delta
    AVOID_MATURE_COLLAPSE = args.avoid_mature_collapse
    if args.max_hold > 0:
        MAX_HOLD_DAYS = args.max_hold

    if args.oos_split:
        # ── Walk-forward OOS mode ──
        print(f"\n🔬 Walk-Forward OOS Test: {ticker}")
        print(f"   IS:  {args.start} → {args.oos_split}")
        print(f"   OOS: {args.oos_split} → {args.end}")

        print(f"\n{'─'*70}")
        print(f"  Phase 1: IN-SAMPLE training period")
        is_trades = run_backtest(ticker, args.start, args.oos_split,
                                 args.min_conviction, args.portfolio)
        is_result = print_scorecard(is_trades, ticker, args.start, args.oos_split,
                                    args.min_conviction, args.portfolio, label="IN-SAMPLE")
        if is_result and args.dsr:
            dsr = calc_dsr(is_trades, n_trials=9 if args.perturbation else 1)
            is_result["dsr"] = dsr
            print(f"\n  DSR (in-sample): {dsr:.4f}  {'✅ ≥0.95' if dsr and dsr>=0.95 else '⚠️  <0.95'}" if dsr else "\n  DSR: insufficient data")

        print(f"\n{'─'*70}")
        print(f"  Phase 2: OUT-OF-SAMPLE blind test (no parameter changes)")
        oos_trades = run_backtest(ticker, args.oos_split, args.end,
                                  args.min_conviction, args.portfolio)
        oos_result = print_scorecard(oos_trades, ticker, args.oos_split, args.end,
                                     args.min_conviction, args.portfolio, label="OUT-OF-SAMPLE")
        if oos_result and args.dsr:
            dsr = calc_dsr(oos_trades, n_trials=1)
            oos_result["dsr"] = dsr

        if is_result and oos_result:
            print_oos_comparison(is_result, oos_result, ticker, args.oos_split)

    else:
        # ── Standard mode ──
        trades = run_backtest(ticker, args.start, args.end,
                              args.min_conviction, args.portfolio)
        result = print_scorecard(trades, ticker, args.start, args.end,
                                 args.min_conviction, args.portfolio)

        if args.dsr and result:
            try:
                dsr = calc_dsr(trades, n_trials=9 if args.perturbation else 1)
                print(f"\n  DSR: {dsr:.4f}  {'✅ ≥0.95' if dsr and dsr>=0.95 else '⚠️  <0.95'}" if dsr else "\n  DSR: insufficient data")
            except ImportError:
                print("\n  DSR: install scipy to enable (pip install scipy)")

    if args.perturbation:
        run_perturbation_test(ticker, args.start, args.end,
                              args.min_conviction, args.portfolio)

if __name__ == "__main__":
    main()
