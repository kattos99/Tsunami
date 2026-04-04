"""
tsunami_dashboard.py  (v6 — four tabs)
"""
from __future__ import annotations
import json, sqlite3, threading, time
from datetime import date, timedelta
from pathlib import Path
import numpy as np, pandas as pd
import plotly.graph_objects as go
import requests
import dash
from dash import Dash, Input, Output, State, dcc, html, ctx
from dash.dependencies import ALL
from dash.exceptions import PreventUpdate
from tsunami_engine import get_full_watchlist, load_latest, run_scan, init_db, DB_PATH
from tsunami_validation import (init_validation_tables, log_signals, check_pending_outcomes,
    load_pending, load_resolved, load_scorecard, HORIZONS)
from tsunami_universe import (
    init_universe_table, run_universe_scan, load_universe_latest, get_universe_scan_date,
    init_tsx_table, run_tsx_scan, load_tsx_latest, get_tsx_scan_date, TSX_SECTORS,
    init_nightly_table, run_nightly_scan, load_nightly_best, get_nightly_scan_date,
    start_nightly_scheduler, TSX_FULL, NYSE_FULL, CRYPTO_FULL_TICKERS
)
from tsunami_trades import (init_trades_tables, get_portfolio_value, set_portfolio_value,
    get_daily_target, set_daily_target, get_pnl_tally,
    get_questrade_accounts, load_questrade_holdings, save_questrade_holdings,
    add_custom_ticker, load_custom_tickers, get_entry_signals, check_exit_signals,
    load_open_trades, load_closed_trades, log_trade, trade_summary,
    get_cadusd_rate, currency_symbol, is_cad, MIN_CONVICTION, MIN_STAGE,
    open_paper_trade, close_paper_trade, load_open_paper_trades,
    load_closed_paper_trades, paper_trade_scorecard, get_atr, position_size,
    ATR_MULT, DEFAULT_PORT)

BG_DEEP="#080b12";BG_CARD="#0f1420";BG_PANEL="#141927";BORDER="#1e2740"
TEXT_PRI="#e8eaf6";TEXT_SEC="#7986cb";TEXT_DIM="#424870";ACCENT="#5c6bc0"

STATE_COLOR={"compressed":"#37474f","coiling":"#f57c00","excursion_reversal":"#66bb6a",
    "sustained_focus":"#ab47bc","early_watch":"#26c6da","pre_breakout":"#ec407a",
    "expanding":"#ef5350","breakout_state":"#ef5350","neutral":"#455a64","insufficient_data":"#263238"}
STATE_EMOJI={"compressed":"😴","coiling":"🌀","excursion_reversal":"⚡","sustained_focus":"🔭",
    "early_watch":"👁","pre_breakout":"🎯","expanding":"🚀","breakout_state":"🚀","neutral":"😐","insufficient_data":"⏳"}

# Human-readable state labels — no internal jargon
STATE_LABEL={
    "compressed":         "Quiet",
    "coiling":            "Winding Up",
    "excursion_reversal": "Early Signal",
    "sustained_focus":    "Building",
    "early_watch":        "Worth Watching",
    "pre_breakout":       "Setup Loading",
    "expanding":          "Breaking Out",
    "breakout_state":     "Breaking Out",
    "neutral":            "Nothing Yet",
    "insufficient_data":  "Not Enough Data",
}

def plain_english(r: dict) -> str:
    """One plain-English sentence describing what this asset is doing right now."""
    state  = r.get("state","neutral")
    ticker = r.get("ticker","")
    signal = r.get("signal","")
    energy = r.get("energy")
    days   = ""

    if state == "compressed":
        return f"{ticker} is quiet. Volatility is low and cycles are tightening. Nothing to act on yet — just keep watching."
    if state == "coiling":
        return f"{ticker} is winding up. Pressure is building under the surface. Not ready yet, but worth keeping on your radar."
    if state == "excursion_reversal":
        return f"{ticker} just showed an early signal — energy peaked and reversed. This is a warning shot. Something may be shifting."
    if state == "sustained_focus":
        return f"{ticker} has been holding its compression for an extended period. The setup is maturing. Patience — this one is building toward something."
    if state == "early_watch":
        return f"{ticker} is starting to show the right conditions. Energy is rising and cycles are shortening. Worth watching closely now."
    if state == "pre_breakout":
        return f"{ticker} looks loaded. All the conditions are aligning — tight cycles, rising energy, increasing concentration. A move could be imminent."
    if state in ("expanding","breakout_state"):
        if "bullish" in signal:
            return f"{ticker} is breaking out to the upside. Energy has released and momentum is pointing higher. The move is underway."
        elif "bearish" in signal:
            return f"{ticker} is breaking out to the downside. Energy has released with bearish bias. The move is underway."
        return f"{ticker} is in breakout mode. Energy has released strongly. Watch for follow-through."
    return f"{ticker} is in a neutral state. No clear regime signal right now."

STATE_BUCKET={"breakout_state":"🚀 Breaking Out","expanding":"🚀 Breaking Out","pre_breakout":"🎯 Setup Loading",
    "early_watch":"👁 Worth Watching","sustained_focus":"🔭 Building","excursion_reversal":"⚡ Early Signal",
    "coiling":"🌀 Winding Up","compressed":"😴 Quiet","neutral":"😐 Nothing Yet","insufficient_data":"😐 Nothing Yet"}
BUCKET_ORDER=["🚀 Breaking Out","🎯 Setup Loading","🔭 Building","⚡ Early Signal","👁 Worth Watching","🌀 Winding Up","😴 Quiet","😐 Nothing Yet"]
BUCKET_COLOR={"🚀 Breaking Out":"#ef5350","🎯 Setup Loading":"#ec407a","🔭 Building":"#ab47bc",
    "⚡ Early Signal":"#66bb6a","👁 Worth Watching":"#26c6da","🌀 Winding Up":"#f57c00",
    "😴 Quiet":"#455a64","😐 Nothing Yet":"#455a64"}

def conviction_score(r):
    s = 0
    stage = r.get("stage") or 0
    s += int(stage) * 8
    if r.get("exc_reversal"): s += 20
    # Compression — only score if below 1.0 (compressing)
    comp = r.get("compression")
    if comp is not None:
        try:
            c = float(comp)
            if 0 < c < 0.80:   s += 15
            elif 0 < c < 0.88: s += 10
            elif 0 < c < 0.95: s += 5
        except: pass
    # CWT slope — only score if negative (cycles compressing)
    slope = r.get("cwt_slope")
    if slope is not None:
        try:
            sl = float(slope)
            if sl < -3.0:   s += 15
            elif sl < -1.5: s += 8
            elif sl < 0:    s += 3
        except: pass
    # Concentration — 3-day minimum
    conc = r.get("cwt_conc_3d")
    if conc is not None:
        try:
            cn = float(conc)
            if cn > 5.0:   s += 10
            elif cn > 3.0: s += 5
        except: pass
    return min(int(s), 100)

def score_color(s):
    if s>=90:return "#ef5350"
    if s>=76:return "#ec407a"
    if s>=61:return "#f57c00"
    if s>=41:return "#66bb6a"
    return "#455a64"

def score_label(s):
    if s>=90:return "EXTREME"
    if s>=76:return "HIGH"
    if s>=61:return "BUILDING"
    if s>=41:return "EARLY"
    return "QUIET"

def conviction_widget(score,size="normal"):
    color=score_color(score);label=score_label(score)
    bar="█"*int(score/10)+"░"*(10-int(score/10))
    ns,ls,bs,pad=("28px","9px","11px","8px 10px") if size=="normal" else ("42px","11px","14px","14px 16px")
    return html.Div([
        html.Div("CONVICTION",style={"fontSize":ls,"color":TEXT_DIM,"textTransform":"uppercase","letterSpacing":"1px","marginBottom":"4px"}),
        html.Div(str(score),style={"fontSize":ns,"fontWeight":"900","color":color,"lineHeight":"1","marginBottom":"4px"}),
        html.Div(bar,style={"fontSize":bs,"color":color,"letterSpacing":"1px","fontFamily":"monospace"}),
        html.Div(label,style={"fontSize":ls,"color":color,"fontWeight":"700","marginTop":"4px"}),
    ],style={"background":BG_DEEP,"borderRadius":"8px","padding":pad,"textAlign":"center","border":f"1px solid {color}40"})

def fmt_price(p,ticker=""):
    if p is None:return "n/a"
    try:
        f=float(p);sym=currency_symbol(ticker)
        # Crypto and very low price assets get 4 decimals, everything else 2
        if "USD" in ticker and "-" in ticker:return f"{sym}{f:,.4f}" if f<10 else f"{sym}{f:,.2f}"
        return f"{sym}{f:,.2f}"
    except:return "n/a"

def fmt_pct(v):
    if v is None:return "n/a",TEXT_DIM
    try:
        f=float(v);c="#66bb6a" if f>0 else "#ef5350" if f<0 else TEXT_DIM
        return f"{'+' if f>0 else ''}{f:.2f}%",c
    except:return "n/a",TEXT_DIM

def fmt_val(v,dec=3):
    if v is None:return "n/a"
    try:f=float(v);return str(round(f,dec)) if np.isfinite(f) else "n/a"
    except:return "n/a"

def stage_bar(stage):
    steps=[("1","🔄","#66bb6a"),("2","🔭","#ab47bc"),("3","👁","#26c6da"),("4","⚡","#ec407a"),("5","🚀","#ef5350")]
    dots=[]
    for i,(num,emoji,color) in enumerate(steps):
        active=(i+1)<=stage
        dots.append(html.Div(emoji if active else num,style={"width":"22px","height":"22px","borderRadius":"50%",
            "background":color if active else BG_DEEP,"border":f"2px solid {color if active else BORDER}",
            "display":"flex","alignItems":"center","justifyContent":"center","fontSize":"10px",
            "color":TEXT_PRI if active else TEXT_DIM,"fontWeight":"700"}))
        if i<4:dots.append(html.Div(style={"flex":"1","height":"2px","background":color if active else BORDER,"margin":"0 2px"}))
    return html.Div(dots,style={"display":"flex","alignItems":"center","width":"100%","marginTop":"10px"})

def make_phase_chart(ticker,history_json,height=380,show_title=True):
    def empty_fig(msg="No data"):
        fig=go.Figure()
        fig.add_annotation(text=msg,xref="paper",yref="paper",x=0.5,y=0.5,
                          showarrow=False,font=dict(size=12,color="#424870"))
        fig.update_layout(height=height,
                         paper_bgcolor=BG_DEEP,
                         plot_bgcolor=BG_DEEP,
                         margin=dict(l=0,r=0,t=0,b=0))
        return fig
    try:hist=pd.DataFrame(json.loads(history_json))
    except:return empty_fig("Invalid history data")
    if hist.empty:return empty_fig("No history data")
    # Track whether we needed the slope fallback for chart annotation
    slope_was_missing = False
    # Try full dropna first, fall back to partial if needed
    hist_full=hist.dropna(subset=["compression_ratio","cwt_cycle_slope","energy_ratio"])
    if len(hist_full)>=3:
        hist=hist_full
    else:
        # Partial fallback — fill missing CWT columns so we can still draw the path
        hist=hist.copy()
        if "cwt_cycle_slope" not in hist.columns or hist["cwt_cycle_slope"].isna().all():
            hist["cwt_cycle_slope"]=0.0
            slope_was_missing = True
        else:
            hist["cwt_cycle_slope"]=hist["cwt_cycle_slope"].fillna(0.0)
        if "energy_ratio" not in hist.columns or hist["energy_ratio"].isna().all():
            hist["energy_ratio"]=1.0
        else:
            hist["energy_ratio"]=hist["energy_ratio"].fillna(1.0)
        hist=hist.dropna(subset=["compression_ratio"])
    if len(hist)<3:return empty_fig(f"Only {len(hist)} valid rows")
    n=len(hist);fig=go.Figure()

    # ── Coloured regime zones ──
    # Breakout zone — high energy, any compression, any slope
    fig.add_trace(go.Scatter3d(
        x=[0.8,2.0,2.0,0.8,0.8], y=[-15,-15,15,15,-15],
        z=[1.55,1.55,1.55,1.55,1.55],
        mode="lines", line=dict(color="rgba(239,83,80,0.6)",width=3),
        name="🚀 Breaking Out", showlegend=True, hoverinfo="skip"))
    # Pre-breakout zone
    fig.add_trace(go.Scatter3d(
        x=[0.7,1.1,1.1,0.7,0.7], y=[-15,-15,0,0,-15],
        z=[1.15,1.15,1.15,1.15,1.15],
        mode="lines", line=dict(color="rgba(236,64,122,0.5)",width=2),
        name="🎯 Setup Loading", showlegend=True, hoverinfo="skip"))
    # Compressed zone
    fig.add_trace(go.Scatter3d(
        x=[0.5,0.85,0.85,0.5,0.5], y=[-5,-5,5,5,-5],
        z=[0.6,0.6,0.6,0.6,0.6],
        mode="lines", line=dict(color="rgba(55,71,79,0.5)",width=2),
        name="😴 Quiet/Compressed", showlegend=True, hoverinfo="skip"))

    # Trail coloured by stage
    stage_colors = {0:"#455a64",1:"#f57c00",2:"#66bb6a",3:"#ab47bc",4:"#ec407a",5:"#ef5350"}
    if "market_state" in hist.columns:
        from tsunami_engine import STATE_STAGE
        trail_colors = [stage_colors.get(STATE_STAGE.get(str(s),0),"#5c6bc0")
                       for s in hist["market_state"]]
    else:
        trail_colors = [f"rgba(92,107,192,{0.3+0.7*i/n})" for i in range(n)]

    fig.add_trace(go.Scatter3d(x=hist["compression_ratio"],y=hist["cwt_cycle_slope"],z=hist["energy_ratio"],
        mode="lines+markers",
        line=dict(color=list(range(n)),colorscale=[[0,"#1a237e"],[0.5,"#5c6bc0"],[1,"#e8eaf6"]],width=3),
        marker=dict(size=2,color=trail_colors),
        text=hist.get("date",pd.Series([""]*n)),
        hovertemplate="%{text}<extra></extra>",name="Path",showlegend=True))

    last=hist.iloc[-1]
    state_str=str(last.get("market_state","neutral"))
    last_color=STATE_COLOR.get(state_str,"#ef5350")
    last_label=STATE_LABEL.get(state_str,"Now")
    z_off=float(last["energy_ratio"])+0.3
    fig.add_trace(go.Scatter3d(x=[last["compression_ratio"]],y=[last["cwt_cycle_slope"]],z=[z_off],
        mode="markers+text",marker=dict(size=12,color=last_color,symbol="diamond",line=dict(color="white",width=2)),
        text=[f"{ticker}\n{last_label}"],textfont=dict(color="white",size=11,family="monospace"),
        textposition="top center",name="Now",showlegend=True,
        hovertemplate=f"<b>NOW — {ticker} — {last_label}</b><extra></extra>"))

    fig.update_layout(height=height,margin=dict(l=0,r=0,t=30 if show_title else 10,b=0),paper_bgcolor=BG_DEEP,
        scene=dict(bgcolor=BG_PANEL,
            xaxis=dict(title="Compression",color=TEXT_SEC,backgroundcolor=BG_PANEL,gridcolor=BORDER),
            yaxis=dict(title="CWT Slope",color=TEXT_SEC,backgroundcolor=BG_PANEL,gridcolor=BORDER),
            zaxis=dict(title="Energy",color=TEXT_SEC,backgroundcolor=BG_PANEL,gridcolor=BORDER)),
        title=dict(text=(f"{ticker} — Phase Space (CWT slope missing — rescan to populate)" if slope_was_missing
                         else f"{ticker} — Phase Space (coloured by regime)") if show_title else "",
            font=dict(color=TEXT_SEC,size=12)),
        legend=dict(bgcolor=BG_PANEL,font=dict(color=TEXT_SEC,size=10),itemsizing="constant"))
    return fig

def _load_anthropic_key() -> str:
    """Try several locations for the Anthropic API key. Returns '' if not found."""
    # 1. Environment variable (most portable)
    import os
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return key
    # 2. ~/.claude_config.json  (original location)
    for fname in (".claude_config.json", "claude_config.json"):
        cfg_path = Path.home() / fname
        try:
            cfg = json.loads(cfg_path.read_text())
            key = cfg.get("anthropic_api_key", "") or cfg.get("api_key", "")
            if key:
                return key
        except Exception:
            pass
    # 3. ~/Downloads/claude_config.json  (common drop location on macOS)
    try:
        cfg = json.loads((Path.home() / "Downloads" / "claude_config.json").read_text())
        key = cfg.get("anthropic_api_key", "") or cfg.get("api_key", "")
        if key:
            return key
    except Exception:
        pass
    return ""

def get_ai_commentary(r):
    ticker=r["ticker"];wl=get_full_watchlist();info=wl.get(ticker,{"label":ticker})
    state=r.get("state","neutral");stage=r.get("stage",0);score=conviction_score(r)
    prompt=f"You are Tsunami, a market regime detection system. Analyze {info['label']} ({ticker}) in 3-4 sentences. State {state} Stage {stage}, conviction {score}/100. Price: {fmt_price(r.get('price'),ticker)}. Compression: {r.get('compression','n/a')}, Energy: {r.get('energy','n/a')}, CWT slope: {r.get('cwt_slope','n/a')}, Conc: {r.get('cwt_conc','n/a')}, Exc reversal: {'YES' if r.get('exc_reversal') else 'NO'}. End with what to watch for next. No bullets."
    key = _load_anthropic_key()
    if not key:
        return (f"{info['label']} ({ticker}) is in {STATE_LABEL.get(state, state)} at Stage {stage} "
                f"with conviction {score}/100. "
                f"Compression: {fmt_val(r.get('compression'))}, Energy: {fmt_val(r.get('energy'))}, "
                f"Slope: {fmt_val(r.get('cwt_slope'),2)}. "
                f"To enable AI commentary, set ANTHROPIC_API_KEY in your environment "
                f"or save your key to ~/claude_config.json as {{\"anthropic_api_key\": \"sk-ant-...\"}}.")
    try:
        resp=requests.post("https://api.anthropic.com/v1/messages",
            headers={"Content-Type":"application/json","x-api-key":key,"anthropic-version":"2023-06-01"},
            json={"model":"claude-haiku-4-5-20251001","max_tokens":300,"messages":[{"role":"user","content":prompt}]},timeout=30)
        data=resp.json()
        if "content" in data and data["content"]:return data["content"][0].get("text","").strip()
        error=data.get("error",{}).get("message","unknown error")
        return f"AI analysis unavailable — API error: {error[:80]}"
    except Exception as e:
        return f"AI analysis unavailable — {type(e).__name__}: {str(e)[:80]}"

def save_commentary(ticker,scan_date,text):
    con=sqlite3.connect(DB_PATH);cur=con.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS commentary (ticker TEXT,scan_date TEXT,text TEXT,PRIMARY KEY(ticker,scan_date))")
    cur.execute("INSERT OR REPLACE INTO commentary VALUES(?,?,?)",(ticker,scan_date,text))
    con.commit();con.close()

def load_commentary(ticker,scan_date):
    con=sqlite3.connect(DB_PATH);cur=con.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS commentary (ticker TEXT,scan_date TEXT,text TEXT,PRIMARY KEY(ticker,scan_date))")
    cur.execute("SELECT text FROM commentary WHERE ticker=? AND scan_date=?",(ticker,scan_date))
    row=cur.fetchone();con.close()
    return row[0] if row else None

def get_or_generate(r):
    ticker=r["ticker"];today=r.get("scan_date",date.today().isoformat())
    cached=load_commentary(ticker,today)
    if cached:return cached
    text=get_ai_commentary(r);save_commentary(ticker,today,text)
    return text

# Tsunami Compatibility Ratings — derived from backtest results
TSUNAMI_RATINGS = {
    "TSLA":    ("🌊🌊🌊", "Best Fit",   "#ef5350"),
    "NVDA":    ("🌊🌊🌊", "Best Fit",   "#ef5350"),
    "XRP-USD": ("🌊🌊",   "Works Well", "#66bb6a"),
    "BNB-USD": ("🌊🌊",   "Works Well", "#66bb6a"),
    "BTC-USD": ("🌊🌊",   "Works Well", "#66bb6a"),
    "SOL-USD": ("🌊🌊",   "Works Well", "#66bb6a"),
    "AAPL":    ("🌊🌊",   "Works Well", "#66bb6a"),
    "XOM":     ("🌊🌊",   "Works Well", "#66bb6a"),
    "ETH-USD": ("⚠️",     "Poor Fit",   "#f57c00"),
    "META":    ("⚠️",     "Poor Fit",   "#f57c00"),
    "MSFT":    ("⚠️",     "Poor Fit",   "#f57c00"),
}

def compatibility_badge(ticker):
    if ticker not in TSUNAMI_RATINGS: return html.Span()
    icon, label, color = TSUNAMI_RATINGS[ticker]
    return html.Span(f"{icon}",
        title=f"Tsunami {label} — based on backtest results",
        style={"fontSize":"11px","marginLeft":"5px","cursor":"help"})

def asset_card(r,idx):
    ticker=r["ticker"];wl=get_full_watchlist();info=wl.get(ticker,{"label":ticker,"category":""})
    state=r.get("state","neutral");stage=r.get("stage",0)
    color=STATE_COLOR.get(state,"#455a64");emoji=STATE_EMOJI.get(state,"")
    score=conviction_score(r);sc=score_color(score)
    price_str=fmt_price(r.get("price"),ticker)
    pct5_str,pct5c=fmt_pct(r.get("pct_5d"))
    pct20_str,pct20c=fmt_pct(r.get("pct_20d"))
    exc_badge=html.Span("🔄",style={"marginLeft":"4px","fontSize":"12px"}) if r.get("exc_reversal") else html.Span()
    cad_badge=html.Span("🍁",style={"marginLeft":"4px","fontSize":"10px"}) if is_cad(ticker) else html.Span()
    compat_badge=compatibility_badge(ticker)
    ticker=r["ticker"]
    safe_ticker = ticker.replace(".","__").replace("^","__").replace("=","__")
    return html.Div(id={"type":"card","index":safe_ticker},children=[
        html.Div([
            html.Div([html.Span(f"{emoji} ",style={"fontSize":"15px"}),
                html.Span(info["label"],style={"fontWeight":"700","color":TEXT_PRI,"fontSize":"13px"}),
                html.Span(f" {ticker}",style={"color":TEXT_DIM,"fontSize":"10px","marginLeft":"3px"}),exc_badge,cad_badge,compat_badge]),
            html.Div([html.Span(str(score),style={"fontSize":"22px","fontWeight":"900","color":sc,"marginRight":"10px"}),
                html.Span(STATE_LABEL.get(state, state.replace('_',' ').title()),style={"background":color,"color":"white","fontSize":"9px","padding":"3px 8px","borderRadius":"8px","fontWeight":"700"})],
                style={"display":"flex","alignItems":"center"}),
        ],style={"display":"flex","justifyContent":"space-between","alignItems":"center","marginBottom":"8px"}),
        html.Div([html.Span(price_str,style={"fontSize":"20px","fontWeight":"700","color":"white"}),
            html.Div([html.Span("5d ",style={"color":TEXT_DIM,"fontSize":"11px"}),
                html.Span(pct5_str,style={"color":pct5c,"fontWeight":"600","fontSize":"12px"}),
                html.Span("  20d ",style={"color":TEXT_DIM,"fontSize":"11px","marginLeft":"6px"}),
                html.Span(pct20_str,style={"color":pct20c,"fontWeight":"600","fontSize":"12px"})]),
        ],style={"display":"flex","justifyContent":"space-between","alignItems":"center","marginBottom":"4px"}),
        stage_bar(stage),
        html.Div([*[html.Div([
            html.Div(label,style={"fontSize":"9px","color":TEXT_DIM,"textTransform":"uppercase","marginBottom":"2px"}),
            html.Div(val,style={"fontSize":"12px","fontWeight":"600","color":vc}),
        ],style={"background":BG_DEEP,"borderRadius":"6px","padding":"5px 7px","textAlign":"center"})
          for label,val,vc in [
            ("Compress",fmt_val(r.get("compression")),TEXT_PRI),
            ("Energy",fmt_val(r.get("energy")),TEXT_PRI),
            ("Volume",fmt_val(r.get("volume")),TEXT_PRI),
            ("Slope",fmt_val(r.get("cwt_slope"),2),TEXT_PRI),
            ("Conc",fmt_val(r.get("cwt_conc"),2),TEXT_PRI),
            ("Exc Sl",fmt_val(r.get("exc_slope"),3),TEXT_PRI),
            ("Ridge",fmt_val(r.get("ridge_sharpness"),1),
             "#66bb6a" if (r.get("ridge_sharpness") or 0)>8 else "#f57c00" if (r.get("ridge_sharpness") or 0)>5 else TEXT_DIM),
            ("Phase Vel",fmt_val(r.get("phase_velocity"),4),
             "#66bb6a" if (r.get("phase_velocity") or 1)<0.01 else "#f57c00" if (r.get("phase_velocity") or 1)<0.03 else "#ef5350"),
            ("Rdg Δ",fmt_val(r.get("ridge_delta"),2),
             "#66bb6a" if (r.get("ridge_delta") or 0)>0 else "#ef5350"),
            ("C.Debt",fmt_val(r.get("compression_debt"),1),
             "#ef5350" if (r.get("compression_debt") or 0)>10 else "#f57c00" if (r.get("compression_debt") or 0)>5 else TEXT_DIM),
            ("Fisher",fmt_val(r.get("fisher_info"),3),
             "#66bb6a" if (r.get("fisher_info") or 0)>0.5 else TEXT_DIM),
          ]]],
        style={"display":"grid","gridTemplateColumns":"repeat(4,1fr)","gap":"5px","marginTop":"10px"}),
        html.Div("Click to expand →",style={"textAlign":"right","fontSize":"9px","color":TEXT_DIM,"marginTop":"8px"}),
        html.Div(plain_english(r),style={"fontSize":"11px","color":TEXT_DIM,"marginTop":"6px",
            "fontStyle":"italic","borderTop":f"1px solid {BORDER}","paddingTop":"6px","lineHeight":"1.5"}),
    ],n_clicks=0,style={"background":BG_CARD,"border":f"1px solid {color}40","borderLeft":f"4px solid {color}",
                         "borderRadius":"10px","padding":"14px","cursor":"pointer","userSelect":"none"})

def bucket_summary(rows):
    counts={}
    for r in rows:
        b=STATE_BUCKET.get(r.get("state","neutral"),"😐 QUIET");counts[b]=counts.get(b,0)+1
    cards=[]
    for bucket in BUCKET_ORDER:
        n=counts.get(bucket,0);color=BUCKET_COLOR.get(bucket,TEXT_DIM)
        cards.append(html.Div([
            html.Div(bucket,style={"fontSize":"10px","color":color,"fontWeight":"700","marginBottom":"4px"}),
            html.Div(str(n),style={"fontSize":"28px","fontWeight":"800","color":color if n>0 else TEXT_DIM}),
            html.Div("assets",style={"fontSize":"10px","color":TEXT_DIM}),
        ],style={"background":BG_CARD,"border":f"1px solid {color}30","borderTop":f"3px solid {color if n>0 else BORDER}",
                 "borderRadius":"8px","padding":"12px 16px","textAlign":"center","minWidth":"100px"}))
    return html.Div(cards,style={"display":"flex","gap":"10px","overflowX":"auto","paddingBottom":"4px"})

def detail_panel(r):
    ticker=r["ticker"];wl=get_full_watchlist();info=wl.get(ticker,{"label":ticker})
    state=r.get("state","neutral");color=STATE_COLOR.get(state,BORDER);stage=r.get("stage",0);score=conviction_score(r)
    pct5_str,pct5c=fmt_pct(r.get("pct_5d"));pct20_str,pct20c=fmt_pct(r.get("pct_20d"))
    phase_fig=make_phase_chart(ticker,r.get("history_json","[]"))
    return html.Div([
        html.Div([
            html.Div([html.H2(f"{STATE_EMOJI.get(state,'')} {info['label']}",style={"margin":"0","color":TEXT_PRI,"fontSize":"20px"}),
                html.Div(f"Stage {stage} · {STATE_LABEL.get(state, state.replace('_',' ').title())}",style={"color":color,"fontSize":"13px","fontWeight":"600","marginTop":"4px"})]),
            html.Div([conviction_widget(score,"large"),
                html.Button("✕",id="close-btn",n_clicks=0,style={"background":"none","border":f"1px solid {BORDER}",
                    "color":TEXT_SEC,"borderRadius":"6px","padding":"8px 14px","cursor":"pointer","fontSize":"13px","marginLeft":"12px"})],
                style={"display":"flex","alignItems":"center"}),
        ],style={"display":"flex","justifyContent":"space-between","alignItems":"flex-start","marginBottom":"20px"}),
        html.Div([
            html.Div([
                html.Div(label,style={"fontSize":"10px","color":TEXT_DIM,"textTransform":"uppercase","marginBottom":"3px"}),
                html.Div(val,style={"fontSize":"15px","fontWeight":"700","color":vc}),
            ],style={"background":BG_DEEP,"borderRadius":"8px","padding":"10px 12px","textAlign":"center"})
            for label,val,vc in [
                ("Price",fmt_price(r.get("price"),ticker),TEXT_PRI),
                ("5d Return",pct5_str,pct5c),
                ("20d Return",pct20_str,pct20c),
                ("Compression",fmt_val(r.get("compression")),TEXT_PRI),
                ("Energy",fmt_val(r.get("energy")),TEXT_PRI),
                ("Volume",fmt_val(r.get("volume")),TEXT_PRI),
                ("CWT Cycle",fmt_val(r.get("cwt_cycle"),0)+" bars",TEXT_PRI),
                ("CWT Slope",fmt_val(r.get("cwt_slope"),2),TEXT_PRI),
                ("Conc",fmt_val(r.get("cwt_conc"),2),TEXT_PRI),
                ("Conc 3d",fmt_val(r.get("cwt_conc_3d"),2),TEXT_PRI),
                ("Exc Slope",fmt_val(r.get("exc_slope"),4),TEXT_PRI),
                ("Exc Reversal","✅ YES" if r.get("exc_reversal") else "—",TEXT_PRI),
                ("Ridge Sharp",fmt_val(r.get("ridge_sharpness"),2),
                 "#66bb6a" if (r.get("ridge_sharpness") or 0)>8 else "#f57c00" if (r.get("ridge_sharpness") or 0)>5 else TEXT_DIM),
                ("Phase Vel",fmt_val(r.get("phase_velocity"),5),
                 "#66bb6a" if (r.get("phase_velocity") or 1)<0.01 else "#f57c00" if (r.get("phase_velocity") or 1)<0.03 else "#ef5350"),
                ("Ridge Δ",fmt_val(r.get("ridge_delta"),3),
                 "#66bb6a" if (r.get("ridge_delta") or 0)>0 else "#ef5350"),
                ("Compress Debt",fmt_val(r.get("compression_debt"),1),
                 "#ef5350" if (r.get("compression_debt") or 0)>10 else "#f57c00" if (r.get("compression_debt") or 0)>5 else TEXT_DIM),
                ("Fisher Info",fmt_val(r.get("fisher_info"),4),
                 "#66bb6a" if (r.get("fisher_info") or 0)>0.5 else TEXT_DIM),
            ]
        ],style={"display":"grid","gridTemplateColumns":"repeat(auto-fill,minmax(110px,1fr))","gap":"8px","marginBottom":"20px"}),
        dcc.Graph(figure=phase_fig,config={"displayModeBar":True}),
        # Plain English summary
        html.Div([
            html.Div("📖 What's happening",style={"fontSize":"10px","color":TEXT_DIM,
                "textTransform":"uppercase","letterSpacing":"1px","marginBottom":"8px"}),
            html.P(plain_english(r),style={"color":TEXT_PRI,"fontSize":"14px",
                "lineHeight":"1.75","margin":"0","fontStyle":"italic"}),
        ],style={"background":BG_DEEP,"borderRadius":"8px","padding":"16px","marginTop":"16px"}),
        html.Div([
            html.Button("📝 Paper Trade This — Review & Confirm",
                id={"type":"paper-from-detail","ticker":ticker},
                n_clicks=0,
                style={"background":"#1a3a2a","color":"#00e676","border":"1px solid #00e67640",
                    "borderRadius":"8px","padding":"10px 20px","cursor":"pointer","fontWeight":"700",
                    "fontSize":"13px","marginTop":"16px","width":"100%"}),
            html.Div(id={"type":"paper-detail-status","ticker":ticker},
                style={"color":TEXT_DIM,"fontSize":"11px","marginTop":"6px","textAlign":"center"}),
        ]),
    ],style={"background":BG_CARD,"border":f"2px solid {color}","borderRadius":"12px","padding":"24px","marginBottom":"20px"})

def _make_sparkline(history_json: str, width: int = 300, height: int = 90) -> str:
    """
    Build a lightweight SVG sparkline showing Energy and Compression over time.
    No WebGL — pure SVG, zero browser GPU cost. One per intelligence card is fine.
    """
    try:
        import json as _json
        hist = pd.DataFrame(_json.loads(history_json))
        if hist.empty: raise ValueError("empty")
        energy = hist["energy_ratio"].dropna().tolist()[-40:]
        comp   = hist["compression_ratio"].dropna().tolist()[-40:]
        if len(energy) < 3: raise ValueError("too short")
    except Exception:
        return f'''<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">
            <rect width="100%" height="100%" fill="{BG_DEEP}" rx="6"/>
            <text x="50%" y="50%" fill="#424870" font-size="11" text-anchor="middle" dominant-baseline="middle">No history</text>
        </svg>'''

    def _points(vals, w, h, lo, hi):
        rng = hi - lo or 1
        n   = len(vals)
        pts = []
        for i, v in enumerate(vals):
            x = int(i / (n - 1) * (w - 16)) + 8
            y = int(h - 8 - (v - lo) / rng * (h - 16))
            pts.append(f"{x},{y}")
        return " ".join(pts)

    e_lo, e_hi = min(energy), max(energy)
    c_lo, c_hi = min(comp),   max(comp)
    e_pts = _points(energy, width, height, e_lo, e_hi)
    c_pts = _points(comp,   width, height, c_lo, c_hi)

    # Colour last point by level
    last_e = energy[-1]
    e_col  = "#ef5350" if last_e > 1.55 else "#66bb6a" if last_e > 1.15 else "#f57c00"

    return f'''<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">
        <rect width="100%" height="100%" fill="{BG_DEEP}" rx="6"/>
        <polyline points="{c_pts}" fill="none" stroke="#37474f" stroke-width="1.5" stroke-dasharray="3,2" opacity="0.6"/>
        <polyline points="{e_pts}" fill="none" stroke="{e_col}" stroke-width="2"/>
        <circle cx="{e_pts.split()[-1].split(",")[0]}" cy="{e_pts.split()[-1].split(",")[1]}"
                r="4" fill="{e_col}" stroke="{BG_DEEP}" stroke-width="1.5"/>
        <text x="8" y="12" fill="#424870" font-size="9" font-family="monospace">Energy</text>
        <text x="{width-8}" y="12" fill="#424870" font-size="9" font-family="monospace"
              text-anchor="end">{last_e:.2f}</text>
    </svg>'''

def intelligence_card(r,commentary):
    ticker=r["ticker"];wl=get_full_watchlist();info=wl.get(ticker,{"label":ticker})
    state=r.get("state","neutral");stage=r.get("stage",0);color=STATE_COLOR.get(state,BORDER)
    emoji=STATE_EMOJI.get(state,"");score=conviction_score(r);pct5_str,pct5c=fmt_pct(r.get("pct_5d"))
    sparkline_svg = _make_sparkline(r.get("history_json","[]"))
    return html.Div([
        html.Div([
            html.Div([html.Span(f"{emoji} ",style={"fontSize":"18px"}),
                html.Span(info["label"],style={"fontWeight":"700","color":TEXT_PRI,"fontSize":"17px"}),
                html.Span(f" {ticker}",style={"color":TEXT_DIM,"fontSize":"12px","marginLeft":"4px"})]),
            html.Div([html.Span(f"Stage {stage}",style={"color":color,"fontWeight":"700","fontSize":"13px","marginRight":"10px"}),
                html.Span(STATE_LABEL.get(state, state.replace('_'," ").title()),style={"background":color,"color":"white","fontSize":"9px","padding":"3px 8px","borderRadius":"8px","fontWeight":"700"}),
                html.Span(f"  {pct5_str}",style={"color":pct5c,"fontSize":"13px","fontWeight":"600","marginLeft":"10px"})]),
        ],style={"display":"flex","justifyContent":"space-between","alignItems":"center","marginBottom":"16px"}),
        html.Div([
            html.Div([conviction_widget(score,"large"),html.Div(style={"height":"16px"}),
                html.Div("🧠 TSUNAMI INTELLIGENCE",style={"fontSize":"10px","color":TEXT_DIM,"textTransform":"uppercase","letterSpacing":"1px","marginBottom":"10px"}),
                html.P(commentary,style={"color":TEXT_PRI,"fontSize":"14px","lineHeight":"1.75","margin":"0"}),
                html.Div([*[html.Div([html.Span(label+" ",style={"color":TEXT_DIM,"fontSize":"11px"}),
                    html.Span(val,style={"color":TEXT_PRI,"fontWeight":"600","fontSize":"12px"})],
                    style={"marginRight":"16px","display":"inline-block"})
                  for label,val in [("Compression",fmt_val(r.get("compression"))),("Energy",fmt_val(r.get("energy"))),
                    ("Slope",fmt_val(r.get("cwt_slope"),2)),("Conc",fmt_val(r.get("cwt_conc"),2)),
                    ("Exc Rev","✅" if r.get("exc_reversal") else "—")]]],
                style={"marginTop":"14px","paddingTop":"12px","borderTop":f"1px solid {BORDER}"}),
            ],style={"flex":"1","marginRight":"24px","minWidth":"0"}),
            # Lightweight SVG sparkline — no WebGL, no context limit
            html.Div([
                html.Div("Energy vs Compression — 40 days",
                    style={"fontSize":"9px","color":TEXT_DIM,"textTransform":"uppercase",
                           "letterSpacing":"0.5px","marginBottom":"6px"}),
                html.Img(src=f"data:image/svg+xml;utf8,{sparkline_svg}",
                    style={"width":"300px","height":"90px","borderRadius":"6px","display":"block"}),
                html.Div([
                    html.Span("━ Energy  ", style={"color":"#f57c00","fontSize":"10px","fontFamily":"monospace"}),
                    html.Span("╌ Compression", style={"color":"#37474f","fontSize":"10px","fontFamily":"monospace"}),
                ],style={"marginTop":"6px"}),
                html.Div("Full 3D phase chart available in Grid → click asset",
                    style={"fontSize":"9px","color":TEXT_DIM,"marginTop":"8px","fontStyle":"italic"}),
            ],style={"flexShrink":"0","width":"320px","background":BG_DEEP,"borderRadius":"8px","padding":"12px"}),
        ],style={"display":"flex","alignItems":"flex-start"}),
    ],style={"background":BG_CARD,"border":f"1px solid {color}60","borderLeft":f"5px solid {color}","borderRadius":"12px","padding":"20px","marginBottom":"16px"})

# ---------------------------------------------------------------------------
# Scotia Portfolio helpers
# ---------------------------------------------------------------------------

def _parse_scotia_csv(filepath: str) -> list[dict]:
    """Parse Scotia iTrade account_details CSV into clean holdings list."""
    import csv, os
    if not os.path.exists(filepath):
        return []
    rows = []
    with open(filepath, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                symbol      = row.get("Symbol","").strip()
                name        = row.get("Security name","").strip()
                asset_class = row.get("Asset class","").strip()
                industry    = row.get("Industry category","").strip()
                currency    = row.get("Currency","CAD").strip()
                qty         = float(row.get("Quantity","0") or 0)
                avg_cost    = float(row.get("Average cost ($)","0") or 0)
                mkt_price   = float(row.get("Market price ($)","0") or 0)
                book_val    = float(row.get("Book value ($)","0") or 0)
                mkt_val     = float(row.get("Market value ($)","0") or 0)
                chg_pct     = float(row.get("Change (%)","0") or 0)
                chg_dol     = float(row.get("Change ($)","0") or 0)
                ann_income  = float(row.get("Projected Annual income ($)","0") or 0)
                ann_yield   = float(row.get("Projected Annual yield (%)","0") or 0)
                rows.append({
                    "symbol": symbol, "name": name, "asset_class": asset_class,
                    "industry": industry, "currency": currency, "qty": qty,
                    "avg_cost": avg_cost, "mkt_price": mkt_price, "book_val": book_val,
                    "mkt_val": mkt_val, "chg_pct": chg_pct, "chg_dol": chg_dol,
                    "ann_income": ann_income, "ann_yield": ann_yield,
                })
            except Exception:
                continue
    return rows

def _get_latest_portfolio_csv() -> str:
    """Find the most recent account_details CSV in ~/Downloads."""
    import glob, os
    pattern = str(Path.home() / "Downloads" / "account_details_*.csv")
    files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    return files[0] if files else ""

def _match_scan_row(symbol: str, scan_rows: list[dict]) -> dict | None:
    """Match a Scotia symbol to a Tsunami scan row. Handles .TO suffix."""
    if not symbol or symbol == "N/A":
        return None
    # Try direct match first
    for r in scan_rows:
        if r["ticker"].upper() == symbol.upper():
            return r
    # Try with .TO suffix (Scotia lists TSX stocks without it sometimes)
    for r in scan_rows:
        if r["ticker"].upper() == symbol.upper() + ".TO":
            return r
    # Try without .TO
    for r in scan_rows:
        if r["ticker"].upper().replace(".TO","") == symbol.upper().replace(".TO",""):
            return r
    return None

def build_portfolio_tab(scan_rows: list[dict]) -> html.Div:
    """Scotia portfolio tab — holdings with Tsunami regime overlay."""
    from pathlib import Path as _Path

    csv_path = _get_latest_portfolio_csv()
    holdings = _parse_scotia_csv(csv_path) if csv_path else []

    # ── Summary numbers ──
    total_mkt    = sum(h["mkt_val"] for h in holdings)
    total_book   = sum(h["book_val"] for h in holdings)
    total_gain   = total_mkt - total_book
    total_gain_p = (total_gain / total_book * 100) if total_book else 0
    total_income = sum(h["ann_income"] for h in holdings)
    total_yield  = (total_income / total_mkt * 100) if total_mkt else 0

    # Group by asset class
    by_class = {}
    for h in holdings:
        ac = h["asset_class"] or "Other"
        by_class.setdefault(ac, []).append(h)

    class_order = ["Equities", "Fixed Income", "Real Estate", "Cash & Short Term", "Other"]

    def gc(v): return "#00e676" if v >= 0 else "#ff1744"
    def fmt_cad(v): return f"CA${v:,.0f}"
    def fmt_pct(v): return f"{'+'if v>=0 else ''}{v:.2f}%"

    # ── Upload / refresh section ──
    import os
    csv_name = os.path.basename(csv_path) if csv_path else "None found"
    last_updated = ""
    if csv_path:
        import datetime
        mtime = os.path.getmtime(csv_path)
        last_updated = datetime.datetime.fromtimestamp(mtime).strftime("%b %d, %Y %I:%M %p")

    header_section = html.Div([
        html.Div([
            html.Div([
                html.Div("🏦 Scotia Portfolio", style={"fontSize":"16px","fontWeight":"700","color":TEXT_PRI}),
                html.Div(f"File: {csv_name}  ·  Updated: {last_updated or 'never'}",
                    style={"color":TEXT_DIM,"fontSize":"11px","marginTop":"3px"}),
            ]),
            html.Div([
                html.Div("To refresh: export Holdings CSV from Scotia iTrade → save to Downloads",
                    style={"color":TEXT_DIM,"fontSize":"11px","fontStyle":"italic"}),
            ]),
        ], style={"display":"flex","justifyContent":"space-between","alignItems":"flex-start",
                  "marginBottom":"20px","paddingBottom":"16px","borderBottom":f"1px solid {BORDER}"}),
    ])

    if not holdings:
        return html.Div([
            header_section,
            html.Div([
                html.Div("📂 No portfolio file found", style={"fontSize":"16px","fontWeight":"700","color":TEXT_PRI,"marginBottom":"8px"}),
                html.Div("Export your holdings from Scotia iTrade and save to your Downloads folder.",
                    style={"color":TEXT_DIM,"fontSize":"13px","marginBottom":"16px"}),
                html.Div("In Scotia iTrade: Accounts → Holdings → Export → CSV",
                    style={"color":TEXT_SEC,"fontSize":"12px","fontStyle":"italic"}),
            ], style={"textAlign":"center","padding":"60px"}),
        ])

    # ── Portfolio summary cards ──
    summary = html.Div([
        *[html.Div([
            html.Div(label, style={"fontSize":"10px","color":TEXT_DIM,"textTransform":"uppercase","letterSpacing":"0.5px","marginBottom":"6px"}),
            html.Div(value, style={"fontSize":"20px","fontWeight":"800","color":color}),
        ], style={"background":BG_DEEP,"borderRadius":"10px","padding":"14px 18px","textAlign":"center","flex":"1"})
        for label, value, color in [
            ("Total Market Value",  fmt_cad(total_mkt),    TEXT_PRI),
            ("Total Book Value",    fmt_cad(total_book),   TEXT_PRI),
            ("Total Gain / Loss",   f"{fmt_cad(total_gain)} ({fmt_pct(total_gain_p)})", gc(total_gain)),
            ("Annual Income",       fmt_cad(total_income), "#66bb6a"),
            ("Portfolio Yield",     f"{total_yield:.2f}%", "#66bb6a"),
        ]],
    ], style={"display":"flex","gap":"10px","marginBottom":"24px","flexWrap":"wrap"})

    # ── Asset class breakdown ──
    class_cards = []
    for ac in class_order:
        items = by_class.get(ac, [])
        if not items: continue
        ac_val  = sum(h["mkt_val"] for h in items)
        ac_gain = sum(h["chg_dol"] for h in items)
        ac_pct  = ac_val / total_mkt * 100 if total_mkt else 0
        class_cards.append(html.Div([
            html.Div(ac, style={"fontSize":"10px","color":TEXT_DIM,"fontWeight":"700","textTransform":"uppercase","marginBottom":"4px"}),
            html.Div(fmt_cad(ac_val), style={"fontSize":"16px","fontWeight":"800","color":TEXT_PRI}),
            html.Div(f"{ac_pct:.1f}% of portfolio", style={"fontSize":"10px","color":TEXT_DIM,"marginTop":"2px"}),
            html.Div(fmt_pct(ac_gain/sum(h["book_val"] for h in items)*100) if sum(h["book_val"] for h in items) else "—",
                style={"fontSize":"12px","color":gc(ac_gain),"fontWeight":"600","marginTop":"4px"}),
        ], style={"background":BG_DEEP,"borderRadius":"8px","padding":"12px 14px","textAlign":"center","flex":"1","minWidth":"120px"}))

    class_row = html.Div(class_cards, style={"display":"flex","gap":"10px","marginBottom":"24px","flexWrap":"wrap"})

    # ── Holdings sections by asset class ──
    th_s = {"padding":"8px 10px","color":TEXT_DIM,"fontSize":"10px","textTransform":"uppercase",
            "fontWeight":"700","borderBottom":f"1px solid {BORDER}","textAlign":"left",
            "background":BG_DEEP,"position":"sticky","top":"0"}

    sections = []
    for ac in class_order:
        items = by_class.get(ac, [])
        if not items: continue

        # Sort equities by market value desc
        items = sorted(items, key=lambda h: h["mkt_val"], reverse=True)

        rows_html = []
        for h in items:
            symbol     = h["symbol"]
            scan_row   = _match_scan_row(symbol, scan_rows)
            gain_c     = gc(h["chg_dol"])
            unrealised = h["mkt_val"] - h["book_val"]
            unrealised_pct = (unrealised / h["book_val"] * 100) if h["book_val"] else 0

            # Tsunami regime badge
            if scan_row:
                state  = scan_row.get("state","neutral")
                stage  = scan_row.get("stage",0)
                score  = conviction_score(scan_row)
                color  = STATE_COLOR.get(state,"#455a64")
                emoji  = STATE_EMOJI.get(state,"")
                label  = STATE_LABEL.get(state, state.replace("_"," ").title())
                regime_badge = html.Td([
                    html.Span(f"{emoji} {label}",
                        style={"background":color,"color":"white","fontSize":"9px",
                               "padding":"2px 7px","borderRadius":"6px","fontWeight":"700",
                               "marginRight":"4px"}),
                    html.Span(f"S{stage}",
                        style={"color":color,"fontSize":"10px","fontWeight":"700","marginRight":"4px"}),
                    html.Span(str(score),
                        style={"color":score_color(score),"fontSize":"11px","fontWeight":"900"}),
                ], style={"padding":"8px 10px","whiteSpace":"nowrap"})
            elif symbol == "N/A":
                regime_badge = html.Td("—", style={"padding":"8px 10px","color":TEXT_DIM,"fontSize":"11px"})
            else:
                regime_badge = html.Td("Not scanned",
                    style={"padding":"8px 10px","color":TEXT_DIM,"fontSize":"10px","fontStyle":"italic"})

            rows_html.append(html.Tr([
                html.Td([
                    html.Div(h["name"][:45], style={"fontWeight":"700","color":TEXT_PRI,"fontSize":"12px"}),
                    html.Div(symbol if symbol != "N/A" else h["industry"],
                        style={"color":TEXT_DIM,"fontSize":"10px","marginTop":"2px"}),
                ], style={"padding":"8px 10px","minWidth":"200px"}),
                html.Td(f"{h['qty']:,.2f}", style={"padding":"8px 10px","color":TEXT_DIM,"fontSize":"11px","textAlign":"right"}),
                html.Td(f"CA${h['avg_cost']:,.2f}", style={"padding":"8px 10px","color":TEXT_DIM,"fontSize":"11px","textAlign":"right"}),
                html.Td(f"CA${h['mkt_price']:,.2f}", style={"padding":"8px 10px","color":TEXT_PRI,"fontSize":"11px","fontWeight":"600","textAlign":"right"}),
                html.Td(fmt_cad(h["mkt_val"]), style={"padding":"8px 10px","color":TEXT_PRI,"fontSize":"12px","fontWeight":"700","textAlign":"right"}),
                html.Td([
                    html.Div(f"{fmt_pct(unrealised_pct)}", style={"color":gain_c,"fontWeight":"700","fontSize":"12px"}),
                    html.Div(f"{fmt_cad(unrealised)}", style={"color":gain_c,"fontSize":"10px"}),
                ], style={"padding":"8px 10px","textAlign":"right"}),
                html.Td([
                    html.Div(f"{h['ann_yield']:.2f}%", style={"color":"#66bb6a","fontWeight":"600","fontSize":"11px"}),
                    html.Div(f"{fmt_cad(h['ann_income'])}/yr", style={"color":TEXT_DIM,"fontSize":"10px"}),
                ], style={"padding":"8px 10px","textAlign":"right"}),
                regime_badge,
            ], style={"borderBottom":f"1px solid {BORDER}","background":BG_CARD}))

        ac_total = sum(h["mkt_val"] for h in items)
        ac_unrealised = sum(h["mkt_val"] - h["book_val"] for h in items)
        ac_unrealised_pct = ac_unrealised / sum(h["book_val"] for h in items) * 100 if sum(h["book_val"] for h in items) else 0

        sections.append(html.Div([
            html.Div([
                html.Span(ac, style={"fontSize":"14px","fontWeight":"700","color":TEXT_PRI}),
                html.Span(f"  {fmt_cad(ac_total)}",
                    style={"fontSize":"13px","color":TEXT_SEC,"marginLeft":"10px"}),
                html.Span(f"  {fmt_pct(ac_unrealised_pct)}",
                    style={"fontSize":"12px","color":gc(ac_unrealised),"marginLeft":"8px","fontWeight":"600"}),
            ], style={"marginBottom":"10px"}),
            html.Div([
                html.Table([
                    html.Thead(html.Tr([
                        html.Th(c, style=th_s) for c in
                        ["Security","Qty","Avg Cost","Price","Market Value","Gain / Loss","Yield","Tsunami Signal"]
                    ])),
                    html.Tbody(rows_html),
                ], style={"width":"100%","borderCollapse":"collapse","minWidth":"900px"}),
            ], style={"overflowX":"auto"}),
        ], style={"background":BG_CARD,"borderRadius":"10px","padding":"16px 20px","marginBottom":"16px"}))

    # ── Tsunami alerts on your holdings ──
    held_symbols = {h["symbol"].upper() for h in holdings if h["symbol"] != "N/A"}
    held_to      = {s + ".TO" for s in held_symbols} | {s.replace(".TO","") for s in held_symbols}
    all_held     = held_symbols | held_to

    portfolio_signals = [r for r in scan_rows
                         if r.get("ticker","").upper() in all_held and r.get("stage",0) >= 3]
    portfolio_signals = sorted(portfolio_signals, key=lambda r: conviction_score(r), reverse=True)

    signal_cards = []
    for r in portfolio_signals:
        ticker  = r["ticker"]
        state   = r.get("state","neutral")
        stage   = r.get("stage",0)
        score   = conviction_score(r)
        color   = STATE_COLOR.get(state,"#455a64")
        emoji   = STATE_EMOJI.get(state,"")
        label   = STATE_LABEL.get(state, state.replace("_"," ").title())
        holding = next((h for h in holdings
                        if h["symbol"].upper() in (ticker.upper(), ticker.upper().replace(".TO",""))), None)
        mkt_val = holding["mkt_val"] if holding else 0
        unrealised = (holding["mkt_val"] - holding["book_val"]) if holding else 0

        signal_cards.append(html.Div([
            html.Div([
                html.Span(f"{emoji} ", style={"fontSize":"16px"}),
                html.Span(ticker, style={"fontWeight":"900","color":TEXT_PRI,"fontSize":"15px","marginRight":"8px"}),
                html.Span(STATE_LABEL.get(state,label),
                    style={"background":color,"color":"white","fontSize":"9px",
                           "padding":"3px 8px","borderRadius":"6px","fontWeight":"700","marginRight":"8px"}),
                html.Span(f"Stage {stage}", style={"color":color,"fontWeight":"700","fontSize":"12px"}),
            ], style={"marginBottom":"8px"}),
            html.Div([
                html.Div([
                    html.Div("Conviction", style={"fontSize":"9px","color":TEXT_DIM,"marginBottom":"2px"}),
                    html.Div(str(score), style={"fontSize":"20px","fontWeight":"900","color":score_color(score)}),
                ], style={"background":BG_DEEP,"borderRadius":"6px","padding":"8px 12px","textAlign":"center","flex":"1"}),
                html.Div([
                    html.Div("You Hold", style={"fontSize":"9px","color":TEXT_DIM,"marginBottom":"2px"}),
                    html.Div(fmt_cad(mkt_val), style={"fontSize":"14px","fontWeight":"700","color":TEXT_PRI}),
                ], style={"background":BG_DEEP,"borderRadius":"6px","padding":"8px 12px","textAlign":"center","flex":"1"}),
                html.Div([
                    html.Div("Unrealised", style={"fontSize":"9px","color":TEXT_DIM,"marginBottom":"2px"}),
                    html.Div(fmt_cad(unrealised), style={"fontSize":"14px","fontWeight":"700","color":gc(unrealised)}),
                ], style={"background":BG_DEEP,"borderRadius":"6px","padding":"8px 12px","textAlign":"center","flex":"1"}),
                html.Div([
                    html.Div("Signal", style={"fontSize":"9px","color":TEXT_DIM,"marginBottom":"2px"}),
                    html.Div(plain_english(r), style={"fontSize":"10px","color":TEXT_SEC,"lineHeight":"1.4"}),
                ], style={"background":BG_DEEP,"borderRadius":"6px","padding":"8px 12px","flex":"3"}),
            ], style={"display":"flex","gap":"8px"}),
        ], style={"background":BG_CARD,"border":f"1px solid {color}50","borderLeft":f"4px solid {color}",
                  "borderRadius":"8px","padding":"14px","marginBottom":"10px"}))

    signals_section = html.Div([
        html.Div(f"⚡ Tsunami Signals on Your Holdings ({len(portfolio_signals)})",
            style={"fontSize":"14px","fontWeight":"700","color":TEXT_PRI,"marginBottom":"12px"}),
        *(signal_cards if signal_cards else [
            html.Div("No Stage 3+ signals on your current holdings — all quiet.",
                style={"color":TEXT_DIM,"fontSize":"13px","fontStyle":"italic","padding":"16px 0"})
        ]),
    ], style={"background":BG_CARD,"borderRadius":"10px","padding":"20px","marginBottom":"20px"})

    # ── Questrade Accounts ──
    questrade_sections = []
    for acct_id, acct in get_questrade_accounts().items():
        holdings_qt = load_questrade_holdings(acct_id)
        live_prices = get_live_price_snapshot()
        cadusd      = get_cadusd_rate()

        qt_rows = []
        total_qt_val  = 0.0
        total_qt_cost = 0.0
        for h in holdings_qt:
            sym      = h["symbol"]
            qty      = h.get("qty") or 0
            avg_p    = h.get("avg_price") or 0
            curr     = h.get("currency","CAD")
            name     = h.get("name", sym)
            # Get live price from cache or fall back to avg
            live_p   = live_prices.get(sym) or live_prices.get(sym.replace(".TO","")) or avg_p
            cost_val = qty * avg_p
            mkt_val  = qty * live_p
            gain     = mkt_val - cost_val
            gain_pct = (gain / cost_val * 100) if cost_val else 0
            total_qt_val  += mkt_val
            total_qt_cost += cost_val

            scan_row = _match_scan_row(sym, scan_rows)
            if scan_row:
                state  = scan_row.get("state","neutral")
                stage  = scan_row.get("stage",0)
                score  = conviction_score(scan_row)
                color  = STATE_COLOR.get(state,"#455a64")
                emoji  = STATE_EMOJI.get(state,"")
                regime_cell = html.Td([
                    html.Span(f"{emoji} {STATE_LABEL.get(state,state)}",
                        style={"background":color,"color":"white","fontSize":"9px",
                               "padding":"2px 7px","borderRadius":"6px","fontWeight":"700","marginRight":"4px"}),
                    html.Span(f"S{stage} · {score}",
                        style={"color":score_color(score),"fontSize":"11px","fontWeight":"900"}),
                ], style={"padding":"8px 10px","whiteSpace":"nowrap"})
            else:
                regime_cell = html.Td("Scanning...",
                    style={"padding":"8px 10px","color":TEXT_DIM,"fontSize":"10px","fontStyle":"italic"})

            gc_fn = lambda v: "#00e676" if v >= 0 else "#ff1744"
            qt_rows.append(html.Tr([
                html.Td([
                    html.Div(name, style={"fontWeight":"700","color":TEXT_PRI,"fontSize":"12px"}),
                    html.Div(f"{sym} · {curr}", style={"color":TEXT_DIM,"fontSize":"10px","marginTop":"2px"}),
                ], style={"padding":"8px 10px"}),
                html.Td(f"{qty:,.0f}", style={"padding":"8px 10px","color":TEXT_DIM,"fontSize":"11px","textAlign":"right"}),
                html.Td(f"${avg_p:,.4f}" if avg_p < 10 else f"${avg_p:,.2f}",
                    style={"padding":"8px 10px","color":TEXT_DIM,"fontSize":"11px","textAlign":"right"}),
                html.Td(f"${live_p:,.4f}" if live_p < 10 else f"${live_p:,.2f}",
                    style={"padding":"8px 10px","color":TEXT_PRI,"fontWeight":"600","fontSize":"11px","textAlign":"right"}),
                html.Td(f"${mkt_val:,.2f}",
                    style={"padding":"8px 10px","color":TEXT_PRI,"fontWeight":"700","fontSize":"12px","textAlign":"right"}),
                html.Td([
                    html.Div(f"{'+'if gain_pct>=0 else ''}{gain_pct:.1f}%",
                        style={"color":gc_fn(gain),"fontWeight":"700","fontSize":"12px"}),
                    html.Div(f"{'+'if gain>=0 else ''}${gain:,.2f}",
                        style={"color":gc_fn(gain),"fontSize":"10px"}),
                ], style={"padding":"8px 10px","textAlign":"right"}),
                regime_cell,
            ], style={"borderBottom":f"1px solid {BORDER}","background":BG_CARD}))

        total_gain_qt     = total_qt_val - total_qt_cost
        total_gain_pct_qt = (total_gain_qt / total_qt_cost * 100) if total_qt_cost else 0
        gc_fn = lambda v: "#00e676" if v >= 0 else "#ff1744"

        th_s2 = {"padding":"8px 10px","color":TEXT_DIM,"fontSize":"10px","textTransform":"uppercase",
                 "fontWeight":"700","borderBottom":f"1px solid {BORDER}","textAlign":"left","background":BG_DEEP}

        questrade_sections.append(html.Div([
            html.Div([
                html.Div([
                    html.Span("💳 ", style={"fontSize":"16px"}),
                    html.Span(acct["label"], style={"fontWeight":"700","color":TEXT_PRI,"fontSize":"15px"}),
                    html.Span(f"  {acct['type']} · #{acct['account']}",
                        style={"color":TEXT_DIM,"fontSize":"11px","marginLeft":"8px"}),
                ]),
                html.Div([
                    html.Span(f"${total_qt_val:,.2f}",
                        style={"fontSize":"18px","fontWeight":"800","color":TEXT_PRI,"marginRight":"12px"}),
                    html.Span(f"{'+'if total_gain_qt>=0 else ''}${total_gain_qt:,.2f}",
                        style={"fontSize":"14px","fontWeight":"700","color":gc_fn(total_gain_qt),"marginRight":"6px"}),
                    html.Span(f"({total_gain_pct_qt:+.1f}%)",
                        style={"fontSize":"13px","color":gc_fn(total_gain_qt),"fontWeight":"600"}),
                ]),
            ], style={"display":"flex","justifyContent":"space-between","alignItems":"center","marginBottom":"14px"}),
            html.Div([
                html.Table([
                    html.Thead(html.Tr([html.Th(c, style=th_s2) for c in
                        ["Security","Qty","Avg Price","Live Price","Market Value","Gain / Loss","Tsunami Signal"]])),
                    html.Tbody(qt_rows),
                ], style={"width":"100%","borderCollapse":"collapse","minWidth":"700px"}),
            ], style={"overflowX":"auto"}),
        ], style={"background":BG_CARD,"borderRadius":"10px","padding":"20px","marginBottom":"16px",
                  "border":f"1px solid #5c6bc040","borderLeft":"4px solid #5c6bc0"}))

    return html.Div([
        header_section,
        summary,
        class_row,
        # Questrade accounts
        html.Div([
            html.Div("💳 Questrade Accounts",
                style={"fontSize":"14px","fontWeight":"700","color":TEXT_PRI,"marginBottom":"12px"}),
            *questrade_sections,
        ], style={"marginBottom":"8px"}) if questrade_sections else html.Div(),
        signals_section,
        *sections,
    ])


# Company name lookup for nightly scan results
NAME_MAP = {
    "RY.TO":"Royal Bank","TD.TO":"TD Bank","BNS.TO":"Scotiabank","BMO.TO":"Bank of Montreal",
    "CM.TO":"CIBC","NA.TO":"National Bank","MFC.TO":"Manulife","SLF.TO":"Sun Life",
    "IFC.TO":"Intact Financial","GWO.TO":"Great-West Lifeco","CNQ.TO":"Canadian Natural",
    "SU.TO":"Suncor","CVE.TO":"Cenovus","IMO.TO":"Imperial Oil","TOU.TO":"Tourmaline",
    "ARX.TO":"Arc Resources","ENB.TO":"Enbridge","TRP.TO":"TC Energy","PPL.TO":"Pembina",
    "ABX.TO":"Barrick Gold","AEM.TO":"Agnico Eagle","WPM.TO":"Wheaton Precious",
    "FM.TO":"First Quantum","TECK-B.TO":"Teck Resources","NTR.TO":"Nutrien",
    "SHOP.TO":"Shopify","CSU.TO":"Constellation SW","BB.TO":"BlackBerry",
    "OTEX.TO":"Open Text","KXS.TO":"Kinaxis","FTS.TO":"Fortis","H.TO":"Hydro One",
    "CNR.TO":"CN Rail","CP.TO":"CP Kansas City","WCN.TO":"Waste Connections",
    "L.TO":"Loblaw","MRU.TO":"Metro","ATD.TO":"Couche-Tard","DOL.TO":"Dollarama",
    "BCE.TO":"BCE Inc","RCI-B.TO":"Rogers","T.TO":"Telus","REI-UN.TO":"RioCan REIT",
    "BN.TO":"Brookfield Corp","BAM.TO":"Brookfield AM","POW.TO":"Power Corp",
    "TVE.TO":"Tamarack Valley","MEG.TO":"MEG Energy","BEP-UN.TO":"Brookfield Renewable",
    "BIP-UN.TO":"Brookfield Infra","TIH.TO":"Toromont","WSP.TO":"WSP Global",
    "KEY.TO":"Keyera","GEI.TO":"Gibson Energy","ERF.TO":"Enerplus","PEY.TO":"Peyto",
    "K.TO":"Kinross Gold","AGI.TO":"Alamos Gold","OR.TO":"Osisko Royalties",
    "IVN.TO":"Ivanhoe Mines","MG.TO":"Magna Intl","CCL-B.TO":"CCL Industries",
    "LSPD.TO":"Lightspeed","STN.TO":"Stantec","ATRL.TO":"AtkinsRealis","TFI.TO":"TFI Intl",
    "EMP-A.TO":"Empire Co","MTY.TO":"MTY Food","QSR.TO":"Restaurant Brands",
    "CRT-UN.TO":"CT REIT","AP-UN.TO":"Allied REIT","HR-UN.TO":"H&R REIT",
    "SRU-UN.TO":"SmartCentres","DIR-UN.TO":"Dream Industrial","QBR-B.TO":"Quebecor",
    "MBT.TO":"Manitoba Telecom","CWB.TO":"CWB Financial","X.TO":"TMX Group",
    "FFH.TO":"Fairfax Financial","AQN.TO":"Algonquin Power","EMA.TO":"Emera",
    "INE.TO":"Innergex","NVEI.TO":"Nuvei","DND.TO":"Dye & Durham","BB.TO":"BlackBerry",
    "AAPL":"Apple","MSFT":"Microsoft","GOOGL":"Alphabet","AMZN":"Amazon",
    "NVDA":"Nvidia","META":"Meta","TSLA":"Tesla","NFLX":"Netflix","AMD":"AMD",
    "INTC":"Intel","TSM":"TSMC","AVGO":"Broadcom","QCOM":"Qualcomm","MU":"Micron",
    "AMAT":"Applied Materials","LRCX":"Lam Research","KLAC":"KLA Corp","ARM":"Arm Holdings",
    "SMCI":"SuperMicro","JPM":"JPMorgan","BAC":"Bank of America","GS":"Goldman Sachs",
    "MS":"Morgan Stanley","WFC":"Wells Fargo","C":"Citigroup","BLK":"BlackRock",
    "SCHW":"Charles Schwab","AXP":"Amex","V":"Visa","MA":"Mastercard","PYPL":"PayPal",
    "COF":"Capital One","USB":"US Bancorp","TFC":"Truist","XOM":"ExxonMobil",
    "CVX":"Chevron","OXY":"Occidental","COP":"ConocoPhillips","EOG":"EOG Resources",
    "SLB":"Schlumberger","HAL":"Halliburton","MPC":"Marathon Petroleum","PSX":"Phillips 66",
    "VLO":"Valero","DVN":"Devon Energy","FANG":"Diamondback","UNH":"UnitedHealth",
    "JNJ":"J&J","PFE":"Pfizer","ABBV":"AbbVie","MRK":"Merck","LLY":"Eli Lilly",
    "BMY":"Bristol-Myers","AMGN":"Amgen","GILD":"Gilead","REGN":"Regeneron",
    "BIIB":"Biogen","VRTX":"Vertex","ISRG":"Intuitive Surgical","WMT":"Walmart",
    "COST":"Costco","TGT":"Target","HD":"Home Depot","LOW":"Lowe's","MCD":"McDonald's",
    "SBUX":"Starbucks","NKE":"Nike","LULU":"Lululemon","TJX":"TJX Companies",
    "ROST":"Ross Stores","DG":"Dollar General","DLTR":"Dollar Tree","GE":"GE Aerospace",
    "HON":"Honeywell","MMM":"3M","CAT":"Caterpillar","DE":"John Deere","BA":"Boeing",
    "RTX":"Raytheon","LMT":"Lockheed Martin","NOC":"Northrop Grumman","GD":"General Dynamics",
    "DIS":"Disney","CMCSA":"Comcast","CHTR":"Charter","T":"AT&T","VZ":"Verizon",
    "TMUS":"T-Mobile","SNAP":"Snap","PINS":"Pinterest","SPOT":"Spotify",
    "PLTR":"Palantir","COIN":"Coinbase","MSTR":"MicroStrategy","HOOD":"Robinhood",
    "SOFI":"SoFi","RBLX":"Roblox","DKNG":"DraftKings","ABNB":"Airbnb",
    "UBER":"Uber","LYFT":"Lyft","RIVN":"Rivian","LCID":"Lucid","NIO":"NIO",
    "XPEV":"XPeng","LI":"Li Auto","F":"Ford","GM":"General Motors","BTBT":"Bit Digital",
    "SPY":"S&P 500 ETF","QQQ":"Nasdaq ETF","IWM":"Russell 2000","GLD":"Gold ETF",
    "SLV":"Silver ETF","TLT":"20yr Treasury","HYG":"High Yield Bond","XLE":"Energy ETF",
    "XLF":"Financials ETF","XLK":"Tech ETF","XLV":"Health ETF","ARKK":"ARK Innovation",
    "MARA":"Marathon Digital","RIOT":"Riot Platforms","HUT":"Hut 8","CLSK":"CleanSpark",
    "CIFR":"Cipher Mining","BTC-USD":"Bitcoin","ETH-USD":"Ethereum","BNB-USD":"BNB",
    "XRP-USD":"XRP","SOL-USD":"Solana","ADA-USD":"Cardano","DOGE-USD":"Dogecoin",
    "AVAX-USD":"Avalanche","SHIB-USD":"Shiba Inu","DOT-USD":"Polkadot","LINK-USD":"Chainlink",
    "MATIC-USD":"Polygon","LTC-USD":"Litecoin","BCH-USD":"Bitcoin Cash","UNI-USD":"Uniswap",
    "ATOM-USD":"Cosmos","XLM-USD":"Stellar","ALGO-USD":"Algorand","INJ-USD":"Injective",
    "NEAR-USD":"NEAR Protocol","AAVE-USD":"Aave","ARB-USD":"Arbitrum","OP-USD":"Optimism",
    "SUI-USD":"Sui","APT-USD":"Aptos","STX-USD":"Stacks","HBAR-USD":"Hedera",
    "VET-USD":"VeChain","EGLD-USD":"MultiversX","THETA-USD":"Theta","IMX-USD":"Immutable",
    "SAND-USD":"The Sandbox","MANA-USD":"Decentraland","GRT-USD":"The Graph",
    "CHZ-USD":"Chiliz","CRO-USD":"Cronos","FTM-USD":"Fantom","GALA-USD":"Gala",
    "ENS-USD":"ENS","CAKE-USD":"PancakeSwap","COMP-USD":"Compound","TWT-USD":"Trust Wallet",
    "SUSHI-USD":"SushiSwap","YFI-USD":"Yearn Finance","SNX-USD":"Synthetix",
    "BAL-USD":"Balancer","FIL-USD":"Filecoin","ICP-USD":"Internet Computer",
}


def build_nightly_tab() -> html.Div:
    """Nightly full-market scan results — best signals from TSX + NYSE + Crypto."""
    scan_date  = get_nightly_scan_date()
    today      = date.today().isoformat()
    is_fresh   = scan_date == today
    best       = load_nightly_best(min_stage=2, min_conviction=40, limit=60)

    # Enrich names
    for r in best:
        if r.get("name","") == r.get("ticker","") or not r.get("name"):
            r["name"] = NAME_MAP.get(r["ticker"], r["ticker"])

    # Split by market
    tsx_rows    = [r for r in best if r.get("market") == "tsx"]
    nyse_rows   = [r for r in best if r.get("market") == "nyse"]
    crypto_rows = [r for r in best if r.get("market") == "crypto"]

    total_scanned = len(TSX_FULL) + len(NYSE_FULL) + len(CRYPTO_FULL_TICKERS)

    # Which tickers are already on the grid
    from tsunami_engine import WATCHLIST
    wl          = get_full_watchlist()
    on_grid     = set(wl.keys())
    custom_list = {c["ticker"] for c in load_custom_tickers()}

    # Status header
    if scan_date:
        status_color = "#00e676" if is_fresh else "#f57c00"
        status_text  = f"Scanned {today} — {len(best)} signals found" if is_fresh else f"Last scan: {scan_date} — run tonight or manually"
    else:
        status_color = "#455a64"
        status_text  = "No scan yet — click Scan Now or wait for midnight"

    def build_signal_row(r: dict) -> html.Tr:
        ticker   = r["ticker"]
        state    = r.get("state","neutral")
        stage    = r.get("stage", 0)
        conv     = r.get("conviction", 0)
        color    = STATE_COLOR.get(state, "#455a64")
        emoji    = STATE_EMOJI.get(state, "")
        price    = r.get("price")
        pct5     = r.get("pct_5d")
        pct20    = r.get("pct_20d")
        pct5_str, pct5c   = fmt_pct(pct5)
        pct20_str,pct20c  = fmt_pct(pct20)
        price_str = fmt_price(price, ticker) if price else "—"
        name      = r.get("name", ticker)

        # Direction — 20d trend is the primary signal
        # Negative 20d = falling regime (bearish) even if state says "Breaking Out"
        pct20_val = r.get("pct_20d") or 0
        pct5_val  = r.get("pct_5d")  or 0
        if pct20_val < -5:
            is_bullish = False
        elif pct20_val > 5:
            is_bullish = True
        else:
            is_bullish = pct5_val >= 0
        dir_arrow = html.Span("▲", style={"color":"#00e676","fontWeight":"700","fontSize":"12px","marginRight":"4px"}) if is_bullish else                     html.Span("▼", style={"color":"#ff1744","fontWeight":"700","fontSize":"12px","marginRight":"4px"})

        # Add to Grid button
        already_on = ticker in on_grid
        safe_id    = ticker.replace(".","__").replace("^","__").replace("=","__")
        if already_on:
            add_btn = html.Span("✓ On Grid",
                style={"color":"#00e676","fontSize":"10px","fontWeight":"600","padding":"3px 8px",
                       "border":"1px solid #00e67640","borderRadius":"4px"})
        else:
            add_btn = html.Button("+ Grid",
                id={"type":"nightly-add","ticker":ticker,"name":name},
                n_clicks=0,
                style={"background":"#1a237e","color":"#7986cb","border":"1px solid #3949ab",
                       "borderRadius":"4px","padding":"3px 10px","cursor":"pointer",
                       "fontSize":"10px","fontWeight":"700"})

        return html.Tr([
            html.Td([
                dir_arrow,
                html.Span(ticker, style={"fontWeight":"700","color":TEXT_PRI,"fontSize":"13px","marginRight":"6px"}),
                html.Div(name[:25], style={"color":TEXT_DIM,"fontSize":"10px","marginTop":"1px"}),
            ], style={"padding":"8px 10px","minWidth":"140px"}),
            html.Td(price_str,
                style={"padding":"8px 10px","color":TEXT_PRI,"fontSize":"12px","textAlign":"right","whiteSpace":"nowrap"}),
            html.Td(html.Span(pct5_str, style={"color":pct5c,"fontWeight":"600"}),
                style={"padding":"8px 10px","textAlign":"right","fontSize":"12px"}),
            html.Td(html.Span(pct20_str, style={"color":pct20c,"fontWeight":"600"}),
                style={"padding":"8px 10px","textAlign":"right","fontSize":"12px"}),
            html.Td([
                html.Span(f"{emoji} {STATE_LABEL.get(state,state)}",
                    style={"background":color,"color":"white","fontSize":"9px",
                           "padding":"2px 7px","borderRadius":"4px","fontWeight":"700"}),
            ], style={"padding":"8px 10px","whiteSpace":"nowrap"}),
            html.Td(f"S{stage}",
                style={"padding":"8px 10px","color":color,"fontWeight":"700","fontSize":"12px","textAlign":"center"}),
            html.Td(str(conv),
                style={"padding":"8px 10px","color":score_color(conv),"fontWeight":"900","fontSize":"13px","textAlign":"center"}),
            html.Td(add_btn, style={"padding":"8px 10px","textAlign":"center","whiteSpace":"nowrap"}),
        ], style={"borderBottom":f"1px solid {BORDER}","background":f"{color}08"})

    th_s = {"padding":"8px 10px","color":TEXT_DIM,"fontSize":"10px","textTransform":"uppercase",
            "fontWeight":"700","borderBottom":f"1px solid {BORDER}","background":BG_DEEP,"textAlign":"left"}
    th_r = {**th_s,"textAlign":"right"}
    th_c = {**th_s,"textAlign":"center"}

    def market_table(rows: list[dict], label: str, icon: str) -> html.Div:
        if not rows:
            return html.Div([
                html.Div(f"{icon} {label}",
                    style={"fontSize":"13px","fontWeight":"700","color":TEXT_PRI,"marginBottom":"8px"}),
                html.Div("No signals above threshold in last scan.",
                    style={"color":TEXT_DIM,"fontSize":"12px","fontStyle":"italic","padding":"12px 0"}),
            ], style={"background":BG_CARD,"borderRadius":"10px","padding":"16px 20px","marginBottom":"12px"})

        already_count = sum(1 for r in rows if r["ticker"] in on_grid)
        new_count     = len(rows) - already_count

        return html.Div([
            html.Div([
                html.Span(f"{icon} {label}",
                    style={"fontSize":"13px","fontWeight":"700","color":TEXT_PRI}),
                html.Span(f"  {len(rows)} signals",
                    style={"fontSize":"11px","color":TEXT_DIM,"marginLeft":"8px"}),
                html.Span(f"  · {new_count} new",
                    style={"fontSize":"11px","color":"#7986cb","marginLeft":"4px"}) if new_count else html.Span(),
            ], style={"marginBottom":"12px"}),
            html.Div([
                html.Table([
                    html.Thead(html.Tr([
                        html.Th("Ticker / Name", style=th_s),
                        html.Th("Price",  style=th_r),
                        html.Th("5d",     style=th_r),
                        html.Th("20d",    style=th_r),
                        html.Th("Regime", style=th_s),
                        html.Th("Stage",  style=th_c),
                        html.Th("Conv",   style=th_c),
                        html.Th("Grid",   style=th_c),
                    ])),
                    html.Tbody([build_signal_row(r) for r in rows]),
                ], style={"width":"100%","borderCollapse":"collapse","minWidth":"650px"}),
            ], style={"overflowX":"auto"}),
        ], style={"background":BG_CARD,"borderRadius":"10px","padding":"16px 20px","marginBottom":"12px"})

    # Auto-promote section
    auto_candidates = [r for r in best if r.get("stage",0) >= 4
                       and r.get("conviction",0) >= 50
                       and r["ticker"] not in on_grid]

    auto_section = html.Div()
    if auto_candidates:
        auto_section = html.Div([
            html.Div([
                html.Div([
                    html.Span("⚡ Auto-Promote Candidates",
                        style={"fontSize":"13px","fontWeight":"700","color":"#ec407a"}),
                    html.Span(f"  {len(auto_candidates)} stocks at Stage 4+ · conviction 50+",
                        style={"fontSize":"11px","color":TEXT_DIM,"marginLeft":"8px"}),
                ]),
                html.Button("+ Add All to Grid",
                    id="nightly-promote-all-btn", n_clicks=0,
                    style={"background":"#1a237e","color":"white","border":"1px solid #3949ab",
                           "borderRadius":"6px","padding":"7px 16px","cursor":"pointer",
                           "fontSize":"12px","fontWeight":"700"}),
            ], style={"display":"flex","justifyContent":"space-between","alignItems":"center","marginBottom":"10px"}),
            html.Div([
                html.Span(f"{NAME_MAP.get(r['ticker'],r['ticker'])} ({r['ticker']}) S{r['stage']} · {r['conviction']}",
                    style={"background":"#1a237e","color":"#7986cb","fontSize":"10px",
                           "padding":"3px 8px","borderRadius":"4px","marginRight":"6px","marginBottom":"4px",
                           "display":"inline-block"})
                for r in auto_candidates
            ]),
            html.Div(id="nightly-promote-status",
                style={"color":TEXT_DIM,"fontSize":"11px","marginTop":"6px"}),
        ], style={"background":"#0d1330","border":"1px solid #3949ab","borderRadius":"10px",
                  "padding":"16px 20px","marginBottom":"16px"})

    return html.Div([
        html.Div([
            html.Div([
                html.Div("🌙 Nightly Universe Scan",
                    style={"fontSize":"16px","fontWeight":"700","color":TEXT_PRI}),
                html.Div(f"{total_scanned} stocks + crypto scanned · midnight auto-scan enabled",
                    style={"color":TEXT_DIM,"fontSize":"12px","marginTop":"3px"}),
                html.Div(status_text,
                    style={"color":status_color,"fontSize":"11px","marginTop":"4px"}),
            ]),
            html.Div([
                html.Div([
                    html.Button("🌙 Scan Now", id="nightly-scan-btn", n_clicks=0,
                        style={"background":"#1a237e","color":"white","border":"1px solid #3949ab",
                               "borderRadius":"8px","padding":"9px 18px","cursor":"pointer",
                               "fontWeight":"600","fontSize":"13px","marginRight":"8px"}),
                    html.Button("🍁 TSX", id="nightly-tsx-btn", n_clicks=0,
                        style={"background":BG_DEEP,"color":TEXT_SEC,"border":f"1px solid {BORDER}",
                               "borderRadius":"8px","padding":"9px 12px","cursor":"pointer","fontSize":"12px","marginRight":"4px"}),
                    html.Button("🇺🇸 US", id="nightly-nyse-btn", n_clicks=0,
                        style={"background":BG_DEEP,"color":TEXT_SEC,"border":f"1px solid {BORDER}",
                               "borderRadius":"8px","padding":"9px 12px","cursor":"pointer","fontSize":"12px","marginRight":"4px"}),
                    html.Button("₿ Crypto", id="nightly-crypto-btn", n_clicks=0,
                        style={"background":BG_DEEP,"color":TEXT_SEC,"border":f"1px solid {BORDER}",
                               "borderRadius":"8px","padding":"9px 12px","cursor":"pointer","fontSize":"12px"}),
                ], style={"display":"flex","alignItems":"center"}),
                html.Div(id="nightly-scan-status",
                    style={"color":TEXT_DIM,"fontSize":"11px","marginTop":"6px","textAlign":"right"}),
            ]),
        ], style={"display":"flex","justifyContent":"space-between","alignItems":"flex-start",
                  "marginBottom":"16px","paddingBottom":"16px","borderBottom":f"1px solid {BORDER}"}),

        html.Div([
            *[html.Div([
                html.Div(label, style={"fontSize":"10px","color":TEXT_DIM,"textTransform":"uppercase","marginBottom":"4px"}),
                html.Div(str(count), style={"fontSize":"22px","fontWeight":"800","color":color}),
                html.Div("signals", style={"fontSize":"10px","color":TEXT_DIM}),
            ], style={"background":BG_DEEP,"borderRadius":"8px","padding":"12px 16px","textAlign":"center","flex":"1"})
            for label, count, color in [
                ("🍁 TSX",   len(tsx_rows),    "#ef5350"),
                ("🇺🇸 US",    len(nyse_rows),   "#42a5f5"),
                ("₿ Crypto", len(crypto_rows), "#f57c00"),
                ("Total",    len(best),         TEXT_PRI),
            ]],
        ], style={"display":"flex","gap":"10px","marginBottom":"16px"}),

        auto_section,

        market_table(tsx_rows,    "TSX",           "🍁"),
        market_table(nyse_rows,   "NYSE / NASDAQ",  "🇺🇸"),
        market_table(crypto_rows, "Crypto",         "₿"),
    ])


def build_intelligence_tab(rows):
    all_active = sorted([r for r in rows if r.get("stage",0)>=2],key=lambda r:conviction_score(r),reverse=True)
    # Only show assets with conviction > 40 — below that there's nothing actionable
    active = [r for r in all_active if conviction_score(r) > 40]
    if not active:
        return html.Div([
            html.Div("🧠 Tsunami Intelligence",style={"fontSize":"16px","fontWeight":"700","color":TEXT_PRI,"marginBottom":"8px"}),
            html.Div("No assets above conviction 40 right now — all signals are early or weak.",
                style={"color":TEXT_DIM,"textAlign":"center","padding":"60px","fontSize":"14px","fontStyle":"italic"}),
        ])
    return html.Div([
        html.Div([
            html.Div("🧠 Tsunami Intelligence",style={"fontSize":"16px","fontWeight":"700","color":TEXT_PRI}),
            html.Div(f"{len(active)} assets · conviction > 40 · sorted by conviction"
                     + (f" · {len(all_active)-len(active)} below threshold hidden" if len(all_active)>len(active) else ""),
                style={"color":TEXT_DIM,"fontSize":"12px","marginTop":"3px"})],
            style={"marginBottom":"20px","paddingBottom":"16px","borderBottom":f"1px solid {BORDER}"}),
        *[intelligence_card(r,get_or_generate(r)) for r in active],
    ])

def metric_box(label,value,color=None):
    return html.Div([
        html.Div(label,style={"fontSize":"10px","color":TEXT_DIM,"textTransform":"uppercase","letterSpacing":"0.5px","marginBottom":"4px"}),
        html.Div(value,style={"fontSize":"18px","fontWeight":"700","color":color or TEXT_PRI}),
    ],style={"background":BG_DEEP,"borderRadius":"8px","padding":"12px 14px","textAlign":"center"})

def build_validation_tab():
    sc=load_scorecard();pending=load_pending();resolved=load_resolved()
    days=sc.get("days_running",0);total=sc.get("total_signals",0);first=sc.get("first_date","—")
    horizon_cards=[]
    for h in HORIZONS:
        hd=sc.get("horizons",{}).get(h,{});n=hd.get("n",0);wr=hd.get("win_rate");mr=hd.get("mean_ret")
        wr_str="pending" if n==0 else f"{wr}%"
        mr_str="pending" if n==0 else f"{'+' if mr and mr>0 else ''}{mr}%"
        wrc=TEXT_DIM if n==0 else "#66bb6a" if wr and wr>=55 else "#ef5350" if wr and wr<50 else "#f57c00"
        horizon_cards.append(html.Div([
            html.Div(f"{h}-Day Hold",style={"fontSize":"11px","color":TEXT_DIM,"textTransform":"uppercase","marginBottom":"8px","fontWeight":"600"}),
            html.Div(f"n={n}",style={"fontSize":"11px","color":TEXT_DIM,"marginBottom":"4px"}),
            html.Div(wr_str,style={"fontSize":"26px","fontWeight":"800","color":wrc}),
            html.Div("win rate",style={"fontSize":"10px","color":TEXT_DIM,"marginBottom":"6px"}),
            html.Div(mr_str,style={"fontSize":"16px","fontWeight":"700","color":"#66bb6a" if n>0 and mr and mr>0 else TEXT_DIM}),
            html.Div("mean return",style={"fontSize":"10px","color":TEXT_DIM}),
        ],style={"background":BG_DEEP,"borderRadius":"10px","padding":"16px","textAlign":"center","border":f"1px solid {BORDER}","flex":"1"}))
    th_s={"padding":"8px 10px","color":TEXT_DIM,"fontSize":"10px","textTransform":"uppercase","borderBottom":f"1px solid {BORDER}"}
    pending_rows=[]
    for s in pending[:20]:
        fired=date.fromisoformat(s["date_fired"]);color=STATE_COLOR.get(s.get("state","neutral"),"#455a64")
        from tsunami_validation import add_trading_days
        checks=[f"{h}td: {add_trading_days(fired,h).strftime('%b %d')}" for h in HORIZONS]
        pending_rows.append(html.Tr([
            html.Td(html.Span(s["ticker"],style={"fontWeight":"700","color":TEXT_PRI}),style={"padding":"8px 10px"}),
            html.Td(s["date_fired"],style={"padding":"8px 10px","color":TEXT_DIM,"fontSize":"12px"}),
            html.Td(html.Span(f"Stage {s['stage']}",style={"color":color,"fontWeight":"600","fontSize":"12px"}),style={"padding":"8px 10px"}),
            html.Td(html.Span(str(s.get("conviction",0)),style={"color":score_color(s.get("conviction",0)),"fontWeight":"700"}),style={"padding":"8px 10px","textAlign":"center"}),
            html.Td(f"${s['price_at_signal']:,.2f}" if s.get("price_at_signal") else "—",style={"padding":"8px 10px","color":TEXT_DIM,"fontSize":"12px"}),
            html.Td(" · ".join(checks),style={"padding":"8px 10px","color":TEXT_DIM,"fontSize":"11px"}),
        ],style={"borderBottom":f"1px solid {BORDER}"}))
    resolved_rows=[]
    for s in resolved[:30]:
        ret=s.get("return_pct",0) or 0;retc="#66bb6a" if ret>0 else "#ef5350"
        color=STATE_COLOR.get(s.get("state","neutral"),"#455a64")
        ob=html.Span("WIN" if s.get("outcome")=="win" else "LOSS",
            style={"background":"#1b5e20" if s.get("outcome")=="win" else "#b71c1c",
                   "color":"#66bb6a" if s.get("outcome")=="win" else "#ef9a9a",
                   "padding":"2px 8px","borderRadius":"6px","fontSize":"10px","fontWeight":"700"})
        resolved_rows.append(html.Tr([
            html.Td(html.Span(s["ticker"],style={"fontWeight":"700","color":TEXT_PRI}),style={"padding":"8px 10px"}),
            html.Td(s["date_fired"],style={"padding":"8px 10px","color":TEXT_DIM,"fontSize":"12px"}),
            html.Td(html.Span(f"Stage {s['stage']}",style={"color":color,"fontWeight":"600","fontSize":"12px"}),style={"padding":"8px 10px"}),
            html.Td(html.Span(str(s.get("conviction",0)),style={"color":score_color(s.get("conviction",0)),"fontWeight":"700"}),style={"padding":"8px 10px","textAlign":"center"}),
            html.Td(f"{s['horizon_days']}d",style={"padding":"8px 10px","color":TEXT_DIM,"textAlign":"center"}),
            html.Td(f"{'+' if ret>0 else ''}{ret:.2f}%",style={"padding":"8px 10px","color":retc,"fontWeight":"700","textAlign":"center"}),
            html.Td(ob,style={"padding":"8px 10px","textAlign":"center"}),
        ],style={"borderBottom":f"1px solid {BORDER}"}))
    return html.Div([
        html.Div([html.Div("📋 Forward Validation — 90 Day Tracker",style={"fontSize":"16px","fontWeight":"700","color":TEXT_PRI}),
            html.Div("All Stage 2+ signals logged automatically. No adjustments.",style={"color":TEXT_DIM,"fontSize":"12px","marginTop":"3px"})],
            style={"marginBottom":"20px","paddingBottom":"16px","borderBottom":f"1px solid {BORDER}"}),
        html.Div([metric_box("Days Running",str(days)),metric_box("Signals Logged",str(total)),
            metric_box("Started",first),metric_box("Target","90 days")],
            style={"display":"grid","gridTemplateColumns":"repeat(4,1fr)","gap":"10px","marginBottom":"20px"}),
        html.Div(horizon_cards,style={"display":"flex","gap":"12px","marginBottom":"20px"}),
        html.Div([
            html.Div([html.Div(f"⏳ Pending ({len(pending)})",style={"fontSize":"13px","fontWeight":"700","color":TEXT_PRI,"marginBottom":"12px"}),
                html.Table([html.Tr([html.Th(c,style=th_s) for c in ["Ticker","Fired","Stage","Conv","Entry","Check Dates"]])]+
                    (pending_rows if pending_rows else [html.Tr([html.Td("No pending signals",style={"padding":"20px","color":TEXT_DIM,"textAlign":"center","fontStyle":"italic"})])]),
                    style={"width":"100%","borderCollapse":"collapse"})],
                style={"background":BG_CARD,"borderRadius":"10px","padding":"16px","marginBottom":"12px"}),
            html.Div([html.Div(f"✅ Resolved ({len(resolved)})",style={"fontSize":"13px","fontWeight":"700","color":TEXT_PRI,"marginBottom":"12px"}),
                html.Table([html.Tr([html.Th(c,style=th_s) for c in ["Ticker","Fired","Stage","Conv","Hold","Return","Outcome"]])]+
                    (resolved_rows if resolved_rows else [html.Tr([html.Td("First outcomes due April 3 (5 trading days)",style={"padding":"20px","color":TEXT_DIM,"textAlign":"center","fontStyle":"italic"})])]),
                    style={"width":"100%","borderCollapse":"collapse"})],
                style={"background":BG_CARD,"borderRadius":"10px","padding":"16px"}),
        ]),
    ])

def build_trades_tab(rows):
    """Clean watchlist manager — add/remove tickers, see what's being tracked."""
    cadusd = get_cadusd_rate()
    custom = load_custom_tickers()
    wl     = get_full_watchlist()

    # All currently tracked tickers with their scan state
    tracked = []
    for ticker, info in wl.items():
        if info.get("category") in ("FX","Index"): continue
        scan = next((r for r in rows if r["ticker"]==ticker), None)
        state = scan.get("state","neutral") if scan else "—"
        stage = scan.get("stage",0) if scan else 0
        price = scan.get("price") if scan else None
        is_custom = any(c["ticker"]==ticker for c in custom)
        tracked.append({"ticker":ticker,"label":info["label"],"state":state,
                        "stage":stage,"price":price,"custom":is_custom,
                        "category":info.get("category","")})
    tracked = sorted(tracked, key=lambda r: r["stage"], reverse=True)

    # Build compact ticker rows — one per line, clean
    ticker_rows = []
    for t in tracked:
        color  = STATE_COLOR.get(t["state"],"#455a64")
        emoji  = STATE_EMOJI.get(t["state"],"")
        flag   = "🍁" if t["ticker"].endswith(".TO") else ""
        price_str = fmt_price(t["price"], t["ticker"]) if t["price"] else "—"
        safe_id = t["ticker"].replace(".","_").replace("^","_").replace("=","_")
        remove_btn = html.Button("✕",
            id={"type":"remove-ticker","index":safe_id}, n_clicks=0,
            style={"background":"none","border":"none","color":"#ef535070",
                   "cursor":"pointer","fontSize":"13px","padding":"2px 6px",
                   "borderRadius":"4px"}) if t["custom"] else html.Span()

        ticker_rows.append(html.Div([
            html.Div([
                html.Span(f"{emoji} ", style={"fontSize":"12px","width":"20px","display":"inline-block"}),
                html.Span(t["ticker"], style={"fontWeight":"700","color":TEXT_PRI,
                    "fontSize":"13px","width":"100px","display":"inline-block"}),
                html.Span(t["label"][:28], style={"color":TEXT_DIM,"fontSize":"12px",
                    "width":"200px","display":"inline-block"}),
                html.Span(flag, style={"fontSize":"11px","width":"20px","display":"inline-block"}),
            ], style={"display":"flex","alignItems":"center","flex":"1"}),
            html.Div([
                html.Span(price_str, style={"color":TEXT_PRI,"fontWeight":"600",
                    "fontSize":"13px","width":"80px","display":"inline-block","textAlign":"right"}),
                html.Span(STATE_LABEL.get(t["state"],t["state"]) if t["state"]!="—" else "—",
                    style={"background":color if t["state"]!="—" else "transparent",
                           "color":"white" if t["state"]!="—" else TEXT_DIM,
                           "fontSize":"9px","padding":"2px 7px","borderRadius":"4px",
                           "fontWeight":"700","marginLeft":"10px","width":"90px",
                           "display":"inline-block","textAlign":"center"}),
                html.Span(remove_btn, style={"marginLeft":"10px","width":"30px"}),
            ], style={"display":"flex","alignItems":"center"}),
        ], style={"display":"flex","justifyContent":"space-between","alignItems":"center",
                  "padding":"7px 12px","borderBottom":f"1px solid {BORDER}",
                  "background":BG_CARD if t["custom"] else "transparent",
                  "borderLeft":f"3px solid {color}" if t["custom"] else "3px solid transparent",
                  "borderRadius":"4px","marginBottom":"2px"}))

    return html.Div([
        # Header
        html.Div([
            html.Div([
                html.Div("📡 Watchlist", style={"fontSize":"16px","fontWeight":"700","color":TEXT_PRI}),
                html.Div(f"{len(tracked)} assets tracked  ·  CA$1 = US${cadusd:.4f}",
                    style={"color":TEXT_DIM,"fontSize":"12px","marginTop":"3px"}),
            ]),
            # Add ticker inline
            html.Div([
                dcc.Input(id="ticker-input", type="text", placeholder="MSFT or RY.TO",
                    debounce=True,
                    style={"background":BG_DEEP,"color":TEXT_PRI,"border":f"1px solid {BORDER}",
                           "borderRadius":"6px 0 0 6px","padding":"8px 12px","fontSize":"13px",
                           "width":"130px","outline":"none"}),
                dcc.Input(id="ticker-label", type="text", placeholder="Nickname",
                    style={"background":BG_DEEP,"color":TEXT_PRI,"border":f"1px solid {BORDER}",
                           "borderLeft":"none","padding":"8px 12px","fontSize":"13px",
                           "width":"120px","outline":"none"}),
                html.Button("+ Add", id="add-ticker-btn", n_clicks=0,
                    style={"background":ACCENT,"color":"white","border":"none",
                           "borderRadius":"0 6px 6px 0","padding":"8px 14px",
                           "cursor":"pointer","fontSize":"13px","fontWeight":"700"}),
                html.Span(id="ticker-status",
                    style={"color":TEXT_DIM,"fontSize":"11px","marginLeft":"10px"}),
            ], style={"display":"flex","alignItems":"center"}),
        ], style={"display":"flex","justifyContent":"space-between","alignItems":"center",
                  "marginBottom":"16px","paddingBottom":"16px","borderBottom":f"1px solid {BORDER}"}),

        # Column headers
        html.Div([
            html.Span("Ticker / Name", style={"color":TEXT_DIM,"fontSize":"10px",
                "textTransform":"uppercase","flex":"1"}),
            html.Div([
                html.Span("Price", style={"color":TEXT_DIM,"fontSize":"10px",
                    "textTransform":"uppercase","width":"80px","textAlign":"right","display":"inline-block"}),
                html.Span("Regime", style={"color":TEXT_DIM,"fontSize":"10px",
                    "textTransform":"uppercase","width":"90px","textAlign":"center",
                    "display":"inline-block","marginLeft":"10px"}),
                html.Span("", style={"width":"40px","display":"inline-block"}),
            ]),
        ], style={"display":"flex","justifyContent":"space-between","padding":"4px 12px",
                  "marginBottom":"6px"}),

        # Ticker list
        html.Div(ticker_rows, style={"maxHeight":"70vh","overflowY":"auto"}),

        html.Div("🍁 Highlighted = your custom tickers  ·  Click ✕ to remove from watchlist",
            style={"color":TEXT_DIM,"fontSize":"10px","marginTop":"12px",
                   "fontStyle":"italic","textAlign":"center"}),
    ])


def conviction_score_uni(r):
    """Conviction score for universe scan rows (different field names)."""
    s = 0
    stage = r.get("stage") or 0
    s += int(stage) * 8
    if r.get("exc_reversal"): s += 20
    comp = r.get("compression")
    if comp is not None:
        try:
            c = float(comp)
            if 0 < c < 0.80:   s += 15
            elif 0 < c < 0.88: s += 10
            elif 0 < c < 0.95: s += 5
        except: pass
    slope = r.get("cwt_slope")
    if slope is not None:
        try:
            sl = float(slope)
            if sl < -3.0:   s += 15
            elif sl < -1.5: s += 8
            elif sl < 0:    s += 3
        except: pass
    conc = r.get("cwt_conc_3d")
    if conc is not None:
        try:
            cn = float(conc)
            if cn > 5.0:   s += 10
            elif cn > 3.0: s += 5
        except: pass
    return min(int(s), 100)

def universe_card(r):
    ticker  = r.get("yahoo_ticker","")
    name    = r.get("name", ticker)
    symbol  = r.get("symbol", "")
    rank    = r.get("rank", 0)
    state   = r.get("state","neutral")
    stage   = r.get("stage",0)
    color   = STATE_COLOR.get(state,"#455a64")
    emoji   = STATE_EMOJI.get(state,"")
    score   = conviction_score_uni(r)
    sc      = score_color(score)
    pct5_str,pct5c   = fmt_pct(r.get("pct_5d"))
    pct20_str,pct20c = fmt_pct(r.get("pct_20d"))
    price   = r.get("price")
    price_str = f"${float(price):,.4f}" if price and float(price)<10 else f"${float(price):,.2f}" if price else "n/a"
    exc_badge = html.Span("🔄",style={"marginLeft":"4px","fontSize":"11px"}) if r.get("exc_reversal") else html.Span()

    return html.Div([
        html.Div([
            html.Div([
                html.Span(f"#{rank} ",style={"color":TEXT_DIM,"fontSize":"11px"}),
                html.Span(f"{emoji} {name}",style={"fontWeight":"700","color":TEXT_PRI,"fontSize":"13px"}),
                html.Span(f" {symbol}",style={"color":TEXT_DIM,"fontSize":"10px","marginLeft":"3px"}),
                exc_badge,
            ]),
            html.Div([
                html.Span(str(score),style={"fontSize":"20px","fontWeight":"900","color":sc,"marginRight":"8px"}),
                html.Span(STATE_LABEL.get(state, state.replace('_',' ').title()),
                    style={"background":color,"color":"white","fontSize":"9px",
                           "padding":"3px 8px","borderRadius":"8px","fontWeight":"700"}),
            ],style={"display":"flex","alignItems":"center"}),
        ],style={"display":"flex","justifyContent":"space-between","alignItems":"center","marginBottom":"8px"}),

        html.Div([
            html.Span(price_str,style={"fontSize":"18px","fontWeight":"700","color":"white"}),
            html.Div([
                html.Span("5d ",style={"color":TEXT_DIM,"fontSize":"11px"}),
                html.Span(pct5_str,style={"color":pct5c,"fontWeight":"600","fontSize":"12px"}),
                html.Span("  20d ",style={"color":TEXT_DIM,"fontSize":"11px","marginLeft":"6px"}),
                html.Span(pct20_str,style={"color":pct20c,"fontWeight":"600","fontSize":"12px"}),
            ]),
        ],style={"display":"flex","justifyContent":"space-between","alignItems":"center","marginBottom":"8px"}),

        stage_bar(stage),

        html.Div([*[html.Div([
            html.Div(label,style={"fontSize":"9px","color":TEXT_DIM,"textTransform":"uppercase","marginBottom":"2px"}),
            html.Div(val,style={"fontSize":"11px","fontWeight":"600","color":TEXT_PRI}),
        ],style={"background":BG_DEEP,"borderRadius":"5px","padding":"4px 6px","textAlign":"center"})
          for label,val in [
            ("Compress",fmt_val(r.get("compression"))),
            ("Energy",  fmt_val(r.get("energy"))),
            ("Volume",  fmt_val(r.get("volume"))),
            ("Slope",   fmt_val(r.get("cwt_slope"),2)),
            ("Conc",    fmt_val(r.get("cwt_conc"),2)),
            ("Exc Sl",  fmt_val(r.get("exc_slope"),3)),
        ]],
        ],style={"display":"grid","gridTemplateColumns":"repeat(3,1fr)","gap":"4px","marginTop":"8px"}),

        # Promote button
        html.Div([
            html.Button("+ Watch",
                id={"type":"promote-ticker","ticker":ticker,"label":name},
                n_clicks=0,
                style={"background":"none","border":f"1px solid {ACCENT}40","color":TEXT_SEC,
                       "borderRadius":"6px","padding":"4px 10px","cursor":"pointer",
                       "fontSize":"11px","marginTop":"8px"})
        ]) if score >= 20 else html.Div(),
    ],style={"background":BG_CARD,"border":f"1px solid {color}40","borderLeft":f"4px solid {color}",
             "borderRadius":"10px","padding":"14px"})


def tsx_sector_section(rows):
    """Build TSX sector breakdown grouped by sector."""
    by_sector = {}
    for r in rows:
        s = r.get("sector","Other")
        if s not in by_sector:
            by_sector[s] = []
        r["_conv"] = conviction_score_uni(r)
        by_sector[s].append(r)

    sections = []
    for sector in TSX_SECTORS.keys():
        stocks = sorted(by_sector.get(sector,[]), key=lambda r: r["_conv"], reverse=True)
        if not stocks:
            continue
        cards = []
        for r in stocks:
            ticker = r.get("yahoo_ticker","")
            state  = r.get("state","neutral")
            stage  = r.get("stage",0)
            color  = STATE_COLOR.get(state,"#455a64")
            emoji  = STATE_EMOJI.get(state,"")
            score  = r["_conv"]
            sc     = score_color(score)
            price  = r.get("price")
            price_str = f"CA${float(price):,.2f}" if price else "n/a"
            pct5_str,pct5c = fmt_pct(r.get("pct_5d"))
            exc_badge = html.Span("🔄",style={"marginLeft":"4px","fontSize":"10px"}) if r.get("exc_reversal") else html.Span()

            cards.append(html.Div([
                html.Div([
                    html.Div([
                        html.Span(f"{emoji} ",style={"fontSize":"13px"}),
                        html.Span(r.get("name",ticker),style={"fontWeight":"700","color":TEXT_PRI,"fontSize":"12px"}),
                        html.Span(f" {r.get('symbol','')}",style={"color":TEXT_DIM,"fontSize":"10px","marginLeft":"2px"}),
                        exc_badge,
                    ]),
                    html.Div([
                        html.Span(str(score),style={"fontSize":"18px","fontWeight":"900","color":sc,"marginRight":"6px"}),
                        html.Span(STATE_LABEL.get(state, state.replace('_',' ').title()),
                            style={"background":color,"color":"white","fontSize":"8px",
                                   "padding":"2px 6px","borderRadius":"6px","fontWeight":"700"}),
                    ],style={"display":"flex","alignItems":"center"}),
                ],style={"display":"flex","justifyContent":"space-between","alignItems":"center","marginBottom":"6px"}),
                html.Div([
                    html.Span(price_str,style={"fontSize":"15px","fontWeight":"700","color":"white"}),
                    html.Span(pct5_str,style={"color":pct5c,"fontSize":"12px","fontWeight":"600","marginLeft":"8px"}),
                ],style={"marginBottom":"6px"}),
                stage_bar(stage),
                html.Div([
                    *[html.Div([
                        html.Div(label,style={"fontSize":"8px","color":TEXT_DIM,"textTransform":"uppercase","marginBottom":"1px"}),
                        html.Div(val,style={"fontSize":"11px","fontWeight":"600","color":TEXT_PRI}),
                    ],style={"background":BG_DEEP,"borderRadius":"4px","padding":"4px 6px","textAlign":"center"})
                      for label,val in [
                        ("Compress",fmt_val(r.get("compression"))),
                        ("Energy",  fmt_val(r.get("energy"))),
                        ("Slope",   fmt_val(r.get("cwt_slope"),2)),
                    ]],
                ],style={"display":"grid","gridTemplateColumns":"repeat(3,1fr)","gap":"4px","marginTop":"6px"}),
                html.Button("+ Watch",
                    id={"type":"promote-ticker","ticker":ticker,"label":r.get("name",ticker)},
                    n_clicks=0,
                    style={"background":"none","border":f"1px solid {ACCENT}40","color":TEXT_SEC,
                           "borderRadius":"6px","padding":"3px 8px","cursor":"pointer",
                           "fontSize":"10px","marginTop":"6px"}) if score >= 20 else html.Div(),
            ],style={"background":BG_CARD,"border":f"1px solid {color}40","borderLeft":f"3px solid {color}",
                     "borderRadius":"8px","padding":"10px"}))

        sections.append(html.Div([
            html.Div(sector,style={"fontSize":"13px","fontWeight":"700","color":TEXT_PRI,"marginBottom":"12px"}),
            html.Div(cards,style={"display":"grid","gridTemplateColumns":"repeat(auto-fill,minmax(220px,1fr))","gap":"8px"}),
        ],style={"marginBottom":"20px"}))

    return sections


def build_universe_tab():
    rows      = load_universe_latest()
    scan_date = get_universe_scan_date()
    today     = date.today().isoformat()
    is_stale  = scan_date != today if scan_date else True

    # Sort by conviction descending
    for r in rows:
        r["_conv"] = conviction_score_uni(r)
    rows = sorted(rows, key=lambda r: r["_conv"], reverse=True)

    # Bucket counts
    active   = [r for r in rows if r.get("stage",0) >= 2]
    watching = [r for r in rows if r.get("stage",0) == 1]
    quiet    = [r for r in rows if r.get("stage",0) == 0]

    # Split into active signals vs rest
    active_cards  = [universe_card(r) for r in active]
    watch_cards   = [universe_card(r) for r in watching]

    stale_banner = html.Div(
        f"⚠️ Last scanned {scan_date or 'never'} — hit Scan Universe to update",
        style={"background":"rgba(245,124,0,0.15)","border":"1px solid rgba(245,124,0,0.4)",
               "borderRadius":"8px","padding":"10px 16px","color":"#f57c00",
               "fontSize":"13px","marginBottom":"16px"}
    ) if is_stale else html.Div()

    return html.Div([
        # Header
        html.Div([
            html.Div([
                html.Div("🔭 Universe Scan — Top 25 Crypto",
                    style={"fontSize":"16px","fontWeight":"700","color":TEXT_PRI}),
                html.Div(
                    f"Last scanned: {scan_date or 'never'} · {len(rows)} assets · "
                    f"{len(active)} active signals · {len(watching)} watching · {len(quiet)} quiet",
                    style={"color":TEXT_DIM,"fontSize":"12px","marginTop":"3px"}),
            ]),
            html.Button("🔭 Scan Universe", id="universe-scan-btn", n_clicks=0,
                style={"background":"#ab47bc","color":"white","border":"none","borderRadius":"8px",
                       "padding":"9px 18px","cursor":"pointer","fontWeight":"600","fontSize":"13px"}),
        ],style={"display":"flex","justifyContent":"space-between","alignItems":"flex-start",
                 "marginBottom":"20px","paddingBottom":"16px","borderBottom":f"1px solid {BORDER}"}),

        html.Div(id="universe-scan-status",style={"color":TEXT_DIM,"fontSize":"12px","marginBottom":"12px"}),

        stale_banner,

        # Bucket summary
        html.Div([
            *[html.Div([
                html.Div(label,style={"fontSize":"10px","color":color,"fontWeight":"700","marginBottom":"4px"}),
                html.Div(str(count),style={"fontSize":"28px","fontWeight":"800","color":color if count>0 else TEXT_DIM}),
                html.Div("assets",style={"fontSize":"10px","color":TEXT_DIM}),
            ],style={"background":BG_CARD,"border":f"1px solid {color}30",
                     "borderTop":f"3px solid {color if count>0 else BORDER}",
                     "borderRadius":"8px","padding":"12px 16px","textAlign":"center","minWidth":"100px"})
              for label,count,color in [
                ("🚀 Active Signals", len(active),  "#ec407a"),
                ("👀 Watching",       len(watching), "#f57c00"),
                ("😐 Quiet",          len(quiet),    "#455a64"),
            ]],
        ],style={"display":"flex","gap":"10px","marginBottom":"24px"}),

        # Active signals
        html.Div([
            html.Div("🚀 Active Signals",
                style={"fontSize":"14px","fontWeight":"700","color":TEXT_PRI,"marginBottom":"16px"}),
            html.Div(active_cards if active_cards else [
                html.Div("No active signals in top 25 crypto right now.",
                    style={"color":TEXT_DIM,"textAlign":"center","padding":"30px","fontStyle":"italic"})
            ],style={"display":"grid","gridTemplateColumns":"repeat(auto-fill,minmax(280px,1fr))","gap":"12px"}),
        ],style={"marginBottom":"24px"}),

        # Watching
        html.Div([
            html.Div(f"👀 Stage 1 — Watching ({len(watching)})",
                style={"fontSize":"13px","fontWeight":"700","color":TEXT_PRI,"marginBottom":"16px"}),
            html.Div(watch_cards if watch_cards else [
                html.Div("Nothing coiling or compressed.",
                    style={"color":TEXT_DIM,"textAlign":"center","padding":"20px","fontStyle":"italic"})
            ],style={"display":"grid","gridTemplateColumns":"repeat(auto-fill,minmax(280px,1fr))","gap":"12px"}),
        ]),

        # TSX Sector Scan
        html.Div([
            html.Div(style={"height":"8px"}),
            html.Div(style={"borderTop":f"1px solid {BORDER}","marginBottom":"20px"}),
            html.Div([
                html.Div([
                    html.Div("🍁 TSX Sector Scan",
                        style={"fontSize":"16px","fontWeight":"700","color":TEXT_PRI}),
                    html.Div(
                        f"Last scanned: {get_tsx_scan_date() or 'never'} · Banks · Energy · Mining · Tech",
                        style={"color":TEXT_DIM,"fontSize":"12px","marginTop":"3px"}),
                ]),
                html.Button("🍁 Scan TSX", id="tsx-scan-btn", n_clicks=0,
                    style={"background":"#d32f2f","color":"white","border":"none","borderRadius":"8px",
                           "padding":"9px 18px","cursor":"pointer","fontWeight":"600","fontSize":"13px"}),
            ],style={"display":"flex","justifyContent":"space-between","alignItems":"flex-start",
                     "marginBottom":"16px"}),
            html.Div(id="tsx-scan-status",style={"color":TEXT_DIM,"fontSize":"12px","marginBottom":"12px"}),
            *tsx_sector_section(load_tsx_latest()),
        ]),
    ])

TAB_STYLE={"background":BG_PANEL,"color":TEXT_DIM,"border":f"1px solid {BORDER}","borderRadius":"8px 8px 0 0","padding":"8px 20px","fontWeight":"600","fontSize":"13px"}
TAB_SEL={**TAB_STYLE,"background":ACCENT,"color":"white","border":f"1px solid {ACCENT}"}

app=Dash(__name__,title="🌊 Tsunami")
app.config.suppress_callback_exceptions=True

app.layout=html.Div([
    dcc.Interval(id="active-refresh",interval=30*60*1000,n_intervals=0),
    dcc.Interval(id="passive-refresh",interval=60*60*1000,n_intervals=0),
    dcc.Interval(id="price-poll",interval=15*60*1000,n_intervals=0),  # 15-min full price poll
    dcc.Interval(id="position-poll",interval=60*1000,n_intervals=0),     # 60-sec open position refresh
    dcc.Store(id="selected-ticker",data=None),
    dcc.Store(id="all-rows",data=[]),
    dcc.Store(id="live-prices",data={}),  # ticker -> live price (float)
    dcc.Store(id="alert-signals",data=[]),  # current GET IN signals
    dcc.Store(id="show-all-grid",data=False),  # toggle quiet stocks
    dcc.Store(id="pending-trade",data=None),   # trade ticket awaiting confirmation
    dcc.Store(id="ticket-mode",data="shares"),  # "shares" or "dollars"
    dcc.Store(id="notification-data",data=None),
    html.Div(id="notification-trigger",style={"display":"none"}),
    html.Div([
        html.Div([
            html.H1("🌊 Tsunami",style={"margin":"0","fontSize":"26px","fontWeight":"800","color":TEXT_PRI}),
            html.Div(id="last-updated",style={"color":TEXT_DIM,"fontSize":"12px","marginTop":"2px"}),
        ]),
        html.Div([
            html.Button("↻ Scan All",id="scan-btn",n_clicks=0,
                style={"background":ACCENT,"color":"white","border":"none","borderRadius":"8px",
                       "padding":"9px 18px","cursor":"pointer","fontWeight":"600","fontSize":"13px"}),
            html.Div(id="scan-status",style={"color":TEXT_DIM,"fontSize":"11px","marginTop":"4px","textAlign":"right"}),
        ]),
    ],style={"display":"flex","justifyContent":"space-between","alignItems":"flex-start",
             "marginBottom":"20px","paddingBottom":"16px","borderBottom":f"1px solid {BORDER}"}),
    html.Div(id="bucket-row",style={"marginBottom":"20px"}),
    dcc.Tabs(id="main-tabs",value="grid",children=[
        dcc.Tab(label="📊 Grid",value="grid",style=TAB_STYLE,selected_style=TAB_SEL),
        dcc.Tab(label="🧠 Intelligence",value="intelligence",style=TAB_STYLE,selected_style=TAB_SEL),
        dcc.Tab(label="📋 Validation",value="validation",style=TAB_STYLE,selected_style=TAB_SEL),
        dcc.Tab(label="➕ Add Assets",value="trades",style=TAB_STYLE,selected_style=TAB_SEL),
        dcc.Tab(label="📝 Paper",value="paper",style=TAB_STYLE,selected_style=TAB_SEL),
        dcc.Tab(label="🔭 Universe",value="universe",style=TAB_STYLE,selected_style=TAB_SEL),
        dcc.Tab(label="🌙 Nightly",value="nightly",style=TAB_STYLE,selected_style=TAB_SEL),
        dcc.Tab(label="🏦 Portfolio",value="portfolio",style=TAB_STYLE,selected_style=TAB_SEL),
        dcc.Tab(id="alerts-tab",label="🚨 Alerts",value="alerts",style=TAB_STYLE,selected_style={**TAB_SEL,"background":"#b71c1c","borderColor":"#ef5350"}),
    ],style={"marginBottom":"0"},colors={"border":BORDER,"primary":ACCENT,"background":BG_PANEL}),
    html.Div(id="tab-content",style={"paddingTop":"20px"}),
],style={"background":BG_DEEP,"minHeight":"100vh","padding":"24px",
         "fontFamily":"-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif","color":TEXT_PRI})

# ---------------------------------------------------------------------------
# Live price background thread
# ---------------------------------------------------------------------------
_live_price_cache: dict[str, float] = {}   # ticker -> last_price
_price_cache_lock = threading.Lock()
_price_thread_started = False

def _price_worker() -> None:
    """
    Background daemon: fetches fast_info.last_price for every ticker in
    all-rows every 15 minutes. Writes results into _live_price_cache.
    Also monitors open paper trades for exit conditions.
    Never raises — failures are silently skipped so the grid is never broken.
    Runs immediately on first start, then every 15 minutes.
    """
    import yfinance as yf
    first_run = True
    while True:
        if not first_run:
            time.sleep(15 * 60)
        first_run = False
        try:
            rows = load_latest() or []
            tickers = [r["ticker"] for r in rows if r.get("ticker")]
            for ticker in tickers:
                try:
                    price = yf.Ticker(ticker).fast_info.last_price
                    if price and float(price) > 0:
                        with _price_cache_lock:
                            _live_price_cache[ticker] = float(price)
                except Exception:
                    pass  # leave stale value in cache; never crash the thread

            # Monitor open paper trades for exit conditions
            try:
                open_trades = load_open_paper_trades()
                state_map   = {r["ticker"]: r for r in rows}
                today       = date.today()
                with _price_cache_lock:
                    live_snapshot = dict(_live_price_cache)

                for trade in open_trades:
                    ticker     = trade["ticker"]
                    price      = live_snapshot.get(ticker) or 0
                    stop       = trade["stop_price"]
                    direction  = trade["direction"]
                    entry_date = date.fromisoformat(trade["entry_date"])
                    days_held  = (today - entry_date).days
                    stage      = state_map.get(ticker, {}).get("stage", 0)
                    exit_reason = None

                    if direction == "long" and price > 0 and price <= stop:
                        exit_reason = f"🛑 Price hit stop loss ({price:.2f})"
                    elif days_held >= 10:
                        exit_reason = f"⏱ 10-day limit reached"
                    elif stage < 2 and days_held >= 3:
                        exit_reason = "📉 Regime ended"

                    if exit_reason and price > 0:
                        close_paper_trade(trade["id"], price, exit_reason)
            except Exception:
                pass

        except Exception:
            pass

def _start_price_thread() -> None:
    global _price_thread_started
    if not _price_thread_started:
        t = threading.Thread(target=_price_worker, daemon=True, name="tsunami-price-worker")
        t.start()
        _price_thread_started = True

def get_live_price_snapshot() -> dict[str, float]:
    """Return a safe copy of the current live price cache."""
    with _price_cache_lock:
        return dict(_live_price_cache)

_start_price_thread()  # safe here — function is now defined
start_nightly_scheduler()  # midnight full market scan

@app.callback(
    Output("bucket-row","children"),Output("last-updated","children"),
    Output("all-rows","data"),Output("live-prices","data"),
    Output("notification-data","data"),
    Input("active-refresh","n_intervals"),Input("passive-refresh","n_intervals"),Input("scan-btn","n_clicks"),
)
def refresh_data(active_n,passive_n,scan_clicks):
    triggered=ctx.triggered_id
    if triggered=="scan-btn" and scan_clicks and scan_clicks>0:
        run_scan()
    rows=load_latest()
    if rows:
        for r in rows:r["conviction"]=conviction_score(r)
        log_signals(rows);check_pending_outcomes()
    brow=bucket_summary(rows)
    active_count=sum(1 for r in rows if r.get("stage",0)>=2)
    refresh_note="30min" if active_count>0 else "1hr"
    upd=f"Updated: {date.today().strftime('%A %B %d, %Y')} · {len(rows)} assets · refresh {refresh_note}"
    # Bundle the latest price cache with every data refresh.
    # This is the primary delivery path — price-poll is a secondary top-up.
    prices=get_live_price_snapshot()
    # Build notification payload for client-side target check
    try:
        tally = get_pnl_tally()
        daily_target = get_daily_target()
        notif = json.dumps({"target_hit": daily_target>0 and tally["today"]>=daily_target,
                            "today": tally["today"], "target": daily_target})
    except Exception:
        notif = None
    return brow,upd,rows,prices,notif

@app.callback(Output("scan-status","children"),Input("scan-btn","n_clicks"),prevent_initial_call=True)
def scan_status(n):
    if not n:raise PreventUpdate
    return "✅ Scan complete"

@app.callback(
    Output("show-all-grid","data"),
    Input("show-all-btn","n_clicks"),
    State("show-all-grid","data"),
    prevent_initial_call=True,
)
def toggle_show_all(n, current):
    if not n: raise PreventUpdate
    return not current

@app.callback(
    Output("pending-trade","data"),
    Output("main-tabs","value"),
    Input({"type":"paper-enter","ticker":ALL},"n_clicks"),
    State("alert-signals","data"),
    prevent_initial_call=True,
)
def enter_paper_trade(n_clicks_list, alert_data):
    """Stage a paper trade from alert — shows trade ticket."""
    if not ctx.triggered or not any(v for v in n_clicks_list if v):
        raise PreventUpdate
    tid = ctx.triggered_id
    if not tid or not isinstance(tid, dict): raise PreventUpdate
    ticker = tid.get("ticker","")
    if not ticker: raise PreventUpdate
    entry_signals = []
    if isinstance(alert_data, dict): entry_signals = alert_data.get("entry", [])
    elif isinstance(alert_data, list): entry_signals = alert_data
    sig = next((s for s in entry_signals if s["ticker"] == ticker), None)
    if not sig: raise PreventUpdate
    live  = get_live_price_snapshot()
    price = live.get(ticker) or sig.get("price") or 0
    if price <= 0: raise PreventUpdate
    try: atr = get_atr(ticker) or price * 0.02
    except: atr = price * 0.02
    direction  = sig.get("direction","long")
    conviction = sig.get("conviction", 50)
    portfolio  = get_portfolio_value()
    stop_price = (price - atr*ATR_MULT) if direction=="long" else (price + atr*ATR_MULT)
    sizing     = position_size(price, stop_price, conviction, portfolio)
    pending = {"ticker":ticker,"direction":direction,"stage":sig.get("stage",0),
               "conviction":conviction,"entry_price":round(price,4),"stop_price":round(stop_price,4),
               "atr":round(atr,4),"shares":sizing["shares"],"dollar_risk":sizing["dollar_risk"],
               "position_value":sizing["position_value"],"risk_pct":sizing["risk_pct"]}
    return pending, "paper"

@app.callback(
    Output("pending-trade","data", allow_duplicate=True),
    Output("main-tabs","value", allow_duplicate=True),
    Input({"type":"paper-from-detail","ticker":ALL},"n_clicks"),
    State("all-rows","data"),
    prevent_initial_call=True,
)
def paper_trade_from_detail(n_clicks_list, rows):
    """Stage a paper trade from detail panel — shows trade ticket."""
    if not ctx.triggered or not any(v for v in n_clicks_list if v):
        raise PreventUpdate
    tid = ctx.triggered_id
    if not tid or not isinstance(tid, dict): raise PreventUpdate
    ticker = tid.get("ticker","")
    if not ticker: raise PreventUpdate
    row = next((r for r in (rows or []) if r["ticker"] == ticker), None)
    if not row:
        rows = load_latest()
        row  = next((r for r in rows if r["ticker"] == ticker), None)
    if not row: raise PreventUpdate
    live  = get_live_price_snapshot()
    price = live.get(ticker) or row.get("price") or 0
    if price <= 0: raise PreventUpdate
    try: atr = get_atr(ticker) or price * 0.02
    except: atr = price * 0.02
    conviction = conviction_score(row)
    portfolio  = get_portfolio_value()
    stop_price = price - atr * ATR_MULT
    sizing     = position_size(price, stop_price, conviction, portfolio)
    pending = {"ticker":ticker,"direction":"long","stage":row.get("stage",0),
               "conviction":conviction,"entry_price":round(price,4),"stop_price":round(stop_price,4),
               "atr":round(atr,4),"shares":sizing["shares"],"dollar_risk":sizing["dollar_risk"],
               "position_value":sizing["position_value"],"risk_pct":sizing["risk_pct"]}
    return pending, "paper"

@app.callback(
    Output("ticket-live-calc","children"),
    Input("ticket-price","value"),
    Input("ticket-shares","value"),
    Input("ticket-dollars","value"),
    Input("ticket-stop","value"),
    Input("ticket-mode","data"),
    State("ticket-data","data"),
    prevent_initial_call=True,
)
def update_ticket_calc(price, shares, dollars, stop, mode, ticket_data):
    """Live calc — supports both share qty and dollar amount entry."""
    if not price or not stop or not ticket_data:
        raise PreventUpdate
    try:
        p  = float(str(price).replace(",",""))
        st = float(str(stop).replace(",",""))
        ticker    = ticket_data.get("ticker","")
        direction = ticket_data.get("direction","long")
        is_long   = direction == "long"

        # Derive shares from whichever input is active
        if mode == "dollars" and dollars:
            sh = float(dollars) / p if p else 0
        elif shares:
            sh = float(shares)
        else:
            raise PreventUpdate

        pos_val     = p * sh
        risk_amt    = abs(p - st) * sh
        pnl_at_stop = (st - p) * sh if is_long else (p - st) * sh
        risk_pct    = abs(p - st) / p * 100 if p else 0
        pnl_color   = "#00e676" if pnl_at_stop >= 0 else "#ef5350"

        # Determine display shares
        display_sh = sh if mode=="dollars" else sh
        return html.Div([
            html.Div([
                *[html.Div([
                    html.Div(lbl, style={"fontSize":"9px","color":TEXT_DIM,
                        "textTransform":"uppercase","letterSpacing":"0.5px","marginBottom":"4px"}),
                    html.Div(val, style={"fontSize":"15px","fontWeight":"700","color":clr}),
                ], style={"textAlign":"center","flex":"1","padding":"10px 8px",
                           "background":BG_DEEP,"borderRadius":"6px"})
                for lbl, val, clr in [
                    ("Shares",        f"{display_sh:,.1f}",                        TEXT_PRI),
                    ("Position",      fmt_price(pos_val, ticker),                   TEXT_PRI),
                    ("Max Loss",      f"-{fmt_price(risk_amt, ticker)}",            "#ef5350"),
                    ("Risk %",        f"{risk_pct:.1f}%",                           "#f57c00"),
                    ("P&L at Stop",   f"{'+'if pnl_at_stop>=0 else ''}{fmt_price(abs(pnl_at_stop),ticker)}", pnl_color),
                ]],
            ], style={"display":"flex","gap":"6px"}),
        ])
    except Exception:
        raise PreventUpdate

@app.callback(
    Output("tab-content","children", allow_duplicate=True),
    Input({"type":"close-paper-trade","trade_id":ALL},"n_clicks"),
    prevent_initial_call=True,
)
def close_paper_trade_cb(n_clicks_list):
    if not ctx.triggered or not any(v for v in n_clicks_list if v):
        raise PreventUpdate
    tid = ctx.triggered_id
    if not tid or not isinstance(tid, dict): raise PreventUpdate
    trade_id = tid.get("trade_id")
    if not trade_id: raise PreventUpdate
    live  = get_live_price_snapshot()
    # Get trade to find ticker
    open_trades = load_open_paper_trades()
    trade = next((t for t in open_trades if t["id"] == trade_id), None)
    if not trade: raise PreventUpdate
    price = live.get(trade["ticker"]) or trade["entry_price"]
    close_paper_trade(trade_id, price, "👤 Manually closed")
    return build_paper_tab(None)

@app.callback(
    Output("pending-trade","data", allow_duplicate=True),
    Output("tab-content","children", allow_duplicate=True),
    Output("ticket-status","children"),
    Input("ticket-confirm-btn","n_clicks"),
    Input("ticket-cancel-btn","n_clicks"),
    State("ticket-data","data"),
    State("ticket-price","value"),
    State("ticket-shares","value"),
    State("ticket-dollars","value"),
    State("ticket-stop","value"),
    State("ticket-note","value"),
    State("ticket-mode","data"),
    prevent_initial_call=True,
)
def handle_ticket(confirm_clicks, cancel_clicks, ticket_data,
                  price, shares, dollars, stop, note, mode):
    """Handle confirm or cancel on the trade ticket."""
    triggered = ctx.triggered_id
    if triggered == "ticket-cancel-btn":
        return None, build_paper_tab(None), ""

    if triggered == "ticket-confirm-btn" and ticket_data:
        if not price or not stop:
            return ticket_data, build_paper_tab(ticket_data), "❌ Enter price and stop"
        try:
            p  = float(str(price).replace(",",""))
            st = float(str(stop).replace(",",""))
            # Resolve shares from dollars or qty
            if dollars:
                sh = float(dollars) / p if p else 0
            elif shares:
                sh = float(shares)
            else:
                return ticket_data, build_paper_tab(ticket_data), "❌ Enter quantity or dollar amount"
            if sh <= 0:
                return ticket_data, build_paper_tab(ticket_data), "❌ Quantity must be > 0"
            atr = ticket_data.get("atr", abs(p - st) / ATR_MULT)
            open_paper_trade(
                ticker         = ticket_data["ticker"],
                direction      = ticket_data["direction"],
                stage          = ticket_data.get("stage",0),
                conviction     = ticket_data.get("conviction",50),
                entry_price    = round(p,4),
                stop_price     = round(st,4),
                atr            = round(float(atr),4),
                shares         = round(sh,4),
                dollar_risk    = round(sh * abs(p-st),2),
                position_value = round(sh * p,2),
            )
            return None, build_paper_tab(None), ""
        except Exception as e:
            return ticket_data, build_paper_tab(ticket_data), f"❌ {str(e)[:60]}"
    raise PreventUpdate

@app.callback(
    Output("ticket-mode","data"),
    Output("ticket-shares","style"),
    Output("ticket-dollars","style"),
    Output("ticket-mode-shares","style"),
    Output("ticket-mode-dollars","style"),
    Input("ticket-mode-shares","n_clicks"),
    Input("ticket-mode-dollars","n_clicks"),
    prevent_initial_call=True,
)
def toggle_ticket_mode(shares_clicks, dollars_clicks):
    """Switch between shares and dollar-amount entry."""
    triggered = ctx.triggered_id
    share_style_base = {"background":BG_DEEP,"color":"#f57c00","border":f"1px solid {BORDER}",
                        "borderRadius":"6px","padding":"10px","fontSize":"20px","fontWeight":"700",
                        "width":"120px","outline":"none"}
    dollar_style_base= {"background":BG_DEEP,"color":"#f57c00","border":f"1px solid {BORDER}",
                        "borderRadius":"6px","padding":"10px","fontSize":"20px","fontWeight":"700",
                        "width":"140px","outline":"none"}
    btn_on  = {"background":ACCENT,"color":"white","border":"none","fontSize":"12px","fontWeight":"700","cursor":"pointer","padding":"10px 14px"}
    btn_off = {"background":BG_DEEP,"color":TEXT_DIM,"border":f"1px solid {BORDER}","fontSize":"12px","fontWeight":"700","cursor":"pointer","padding":"10px 14px"}
    if triggered == "ticket-mode-dollars":
        return ("dollars",
                {**share_style_base, "display":"none"},
                {**dollar_style_base,"display":"block"},
                {**btn_off,"borderRadius":"6px 0 0 6px"},
                {**btn_on, "borderRadius":"0 6px 6px 0"})
    return ("shares",
            {**share_style_base,"display":"block"},
            {**dollar_style_base,"display":"none"},
            {**btn_on, "borderRadius":"6px 0 0 6px"},
            {**btn_off,"borderRadius":"0 6px 6px 0"})


# Browser notification when daily target is hit
app.clientside_callback(
    """
    function(tally_json) {
        if (!tally_json) return '';
        try {
            var data = JSON.parse(tally_json);
            if (data.target_hit) {
                if ('Notification' in window) {
                    if (Notification.permission === 'granted') {
                        new Notification('🎯 Daily Target Hit!', {
                            body: 'You\'ve hit your $' + data.target + ' target. Lock in profits.',
                            icon: ''
                        });
                    } else if (Notification.permission !== 'denied') {
                        Notification.requestPermission().then(function(p) {
                            if (p === 'granted') {
                                new Notification('🎯 Daily Target Hit!', {
                                    body: 'You\'ve hit your $' + data.target + ' target.',
                                });
                            }
                        });
                    }
                }
            }
        } catch(e) {}
        return '';
    }
    """,
    Output("notification-trigger","children"),
    Input("notification-data","data"),
)


@app.callback(
    Output("tab-content","children", allow_duplicate=True),
    Input("position-poll","n_intervals"),
    State("main-tabs","value"),
    State("pending-trade","data"),
    prevent_initial_call=True,
)
def refresh_open_positions(_, tab, pending):
    """Refresh paper tab every 60 sec to show updated live prices on open positions."""
    if tab != "paper":
        raise PreventUpdate
    # Re-fetch prices for open positions only — fast, targeted
    import yfinance as yf
    open_trades = load_open_paper_trades()
    if not open_trades:
        raise PreventUpdate
    tickers = list({t["ticker"] for t in open_trades})
    for ticker in tickers:
        try:
            price = yf.Ticker(ticker).fast_info.last_price
            if price and float(price) > 0:
                with _price_cache_lock:
                    _live_price_cache[ticker] = float(price)
        except Exception:
            pass
    return build_paper_tab(pending)


@app.callback(
    Output("live-prices","data", allow_duplicate=True),
    Input("price-poll","n_intervals"),
    prevent_initial_call=True,
)
def update_live_prices_poll(_):
    """Secondary top-up: drain background cache into store every 15 min.
    Primary delivery is via refresh_data which fires on load and active-refresh.
    """
    return get_live_price_snapshot()

@app.callback(
    Output("tab-content","children"),
    Input("main-tabs","value"),Input("all-rows","data"),Input("selected-ticker","data"),
    Input("live-prices","data"),Input("show-all-grid","data"),Input("pending-trade","data"),
)
def render_tab(tab,rows,selected_ticker,live_prices,show_all,pending_trade):
    if not rows:rows=load_latest()
    if tab=="portfolio":return build_portfolio_tab(rows)
    if tab=="nightly":return build_nightly_tab()
    if tab=="intelligence":return build_intelligence_tab(rows)
    if tab=="validation":return build_validation_tab()
    if tab=="universe":return build_universe_tab()
    # Fall back to the in-process cache when the Dash store is still empty
    # (this covers the cold-start window before the first price-poll fires)
    effective_prices = live_prices or get_live_price_snapshot()
    if tab=="alerts":
        if effective_prices:
            rows=[{**r,"price":effective_prices[r["ticker"]]} if r["ticker"] in effective_prices else r for r in rows]
        portfolio   = get_portfolio_value()
        entry_sigs  = get_entry_signals(rows, portfolio)
        open_trades = load_open_trades()
        exit_sigs   = check_exit_signals(open_trades, rows, effective_prices or {})
        return build_alerts_tab(entry_sigs, exit_sigs)
    if tab=="trades":
        fresh_rows=load_latest()
        return build_trades_tab(fresh_rows if fresh_rows else rows)
    if tab=="paper":
        return build_paper_tab(pending_trade)
    detail=html.Div()
    if selected_ticker:
        row=next((r for r in rows if r["ticker"]==selected_ticker),None)
        if row:detail=detail_panel(row)
    # Hide FX rates and indices from grid — reference data only
    wl=get_full_watchlist()
    grid_rows=[r for r in rows if wl.get(r["ticker"],{}).get("category") not in ("FX","Index")]
    # Overlay live prices — use effective_prices (store OR in-process cache fallback)
    if effective_prices:
        grid_rows=[{**r,"price":effective_prices[r["ticker"]]} if r["ticker"] in effective_prices else r
                   for r in grid_rows]
    # Filter quiet/neutral unless show-all toggled
    QUIET_STATES   = {"neutral","insufficient_data","compressed","coiling"}
    custom_tickers = {c["ticker"] for c in load_custom_tickers()}
    # Custom stocks always show — quiet filter only applies to default watchlist
    active_rows  = [r for r in grid_rows if r.get("state","neutral") not in QUIET_STATES or r["ticker"] in custom_tickers]
    quiet_rows   = [r for r in grid_rows if r.get("state","neutral") in QUIET_STATES and r["ticker"] not in custom_tickers]
    display_rows = grid_rows if show_all else active_rows
    # Toggle button
    quiet_count = len(quiet_rows)
    toggle_btn = html.Div([
        html.Button(
            f"{'Hide' if show_all else 'Show'} {quiet_count} quiet assets",
            id="show-all-btn", n_clicks=0,
            style={"background":"none","border":f"1px solid {BORDER}","color":TEXT_DIM,
                   "borderRadius":"6px","padding":"5px 12px","cursor":"pointer",
                   "fontSize":"11px","marginBottom":"12px"}),
    ]) if quiet_count > 0 else html.Div()
    cards=[asset_card(r,i) for i,r in enumerate(display_rows)]
    if not cards:
        cards=[html.Div("No active signals right now — all assets are quiet.",
            style={"color":TEXT_DIM,"fontStyle":"italic","padding":"40px","textAlign":"center",
                   "gridColumn":"1/-1"})]
    grid=html.Div(cards,style={"display":"grid","gridTemplateColumns":"repeat(auto-fill, minmax(300px, 1fr))","gap":"12px"})
    return html.Div([detail,toggle_btn,grid])

@app.callback(
    Output("selected-ticker","data"),
    Input({"type":"card","index":ALL},"n_clicks"),State("all-rows","data"),prevent_initial_call=True,
)
def select_ticker(n_clicks_list,rows):
    if not ctx.triggered or not any(n_clicks_list):raise PreventUpdate
    triggered=ctx.triggered[0]
    if not triggered["value"]:raise PreventUpdate
    safe_ticker=json.loads(triggered["prop_id"].split(".")[0])["index"]
    # Reverse sanitization — __  was used to replace . ^ =
    # Try direct match first, then try restoring dots
    if rows:
        for r in rows:
            st = r["ticker"].replace(".","__").replace("^","__").replace("=","__")
            if st == safe_ticker:
                return r["ticker"]
    raise PreventUpdate

@app.callback(
    Output("selected-ticker","data",allow_duplicate=True),Input("close-btn","n_clicks"),prevent_initial_call=True,
)
def close_detail(n):
    if not n:raise PreventUpdate
    return None

# portfolio callback removed

@app.callback(
    Output("ticker-status","children"),
    Output("all-rows","data",allow_duplicate=True),
    Input("add-ticker-btn","n_clicks"),
    State("ticker-input","value"),
    State("ticker-label","value"),
    prevent_initial_call=True,
)
def add_ticker(n,ticker,label):
    if not n or not ticker:raise PreventUpdate
    ticker=ticker.upper().strip()
    import yfinance as yf
    try:
        test=yf.download(ticker,period="5d",progress=False,timeout=8)
        if test.empty:
            return f"❌ {ticker} not found — check the symbol", []
    except Exception:
        return f"❌ Could not verify {ticker}", []
    # Check if already added as custom
    custom_tickers = [c["ticker"] for c in load_custom_tickers()]
    if ticker in custom_tickers:
        return f"ℹ️ {ticker} is already in your custom list", []
    # Add it — even if it's in the default watchlist, adding as custom pins it to grid
    success=add_custom_ticker(ticker,label or ticker)
    if success:
        try:
            run_scan([ticker])
        except Exception:
            pass
        rows=load_latest()
        wl=get_full_watchlist()
        if ticker in wl:
            return f"✅ {ticker} pinned to your grid (was already being scanned)", rows
        return f"✅ {ticker} added and scanned", rows
    return f"❌ Could not add {ticker}", []

@app.callback(
    Output("all-rows","data",allow_duplicate=True),
    Input({"type":"remove-ticker","index":ALL},"n_clicks"),
    prevent_initial_call=True,
)
def remove_ticker_cb(n_clicks_list):
    if not ctx.triggered or not any((v for v in n_clicks_list if v)):raise PreventUpdate
    triggered=ctx.triggered[0]
    if not triggered["value"]:raise PreventUpdate
    if ctx.triggered_id and isinstance(ctx.triggered_id, dict):
        # safe_id has dots replaced with _ — recover original ticker from custom list
        safe_id = ctx.triggered_id.get("index","")
        custom  = load_custom_tickers()
        ticker  = next((c["ticker"] for c in custom
                        if c["ticker"].replace(".","_").replace("^","_").replace("=","_") == safe_id), None)
        if not ticker:
            raise PreventUpdate
    else:
        raise PreventUpdate
    from tsunami_trades import remove_custom_ticker
    remove_custom_ticker(ticker)
    rows=load_latest()
    return rows

@app.callback(
    Output("universe-scan-status","children"),
    Output("tab-content","children",allow_duplicate=True),
    Input("universe-scan-btn","n_clicks"),
    prevent_initial_call=True,
)
def trigger_universe_scan(n):
    if not n: raise PreventUpdate
    run_universe_scan()
    return (f"✅ Crypto scan complete — {date.today().strftime('%B %d, %Y')}",
            build_universe_tab())

@app.callback(
    Output("tsx-scan-status","children"),
    Output("tab-content","children",allow_duplicate=True),
    Input("tsx-scan-btn","n_clicks"),
    prevent_initial_call=True,
)
def trigger_tsx_scan(n):
    if not n: raise PreventUpdate
    run_tsx_scan()
    return (f"✅ TSX scan complete — {date.today().strftime('%B %d, %Y')}",
            build_universe_tab())

@app.callback(
    Output("tab-content","children",allow_duplicate=True),
    Input({"type":"promote-ticker","ticker":ALL,"label":ALL},"n_clicks"),
    prevent_initial_call=True,
)
def promote_to_watchlist(n_clicks_list):
    if not ctx.triggered or not any(v for v in n_clicks_list if v):
        raise PreventUpdate
    triggered = ctx.triggered[0]
    if not triggered["value"]:
        raise PreventUpdate
    tid = ctx.triggered_id
    if not tid or not isinstance(tid, dict):
        raise PreventUpdate
    ticker = tid.get("ticker","")
    label  = tid.get("label", ticker)
    if not ticker:
        raise PreventUpdate
    # Add to custom watchlist
    add_custom_ticker(ticker, label)
    # Scan it immediately so it appears in grid
    try:
        run_scan([ticker])
    except Exception:
        pass
    # Return updated universe tab with confirmation
    tab = build_universe_tab()
    return tab

def build_trade_ticket(pending: dict) -> html.Div:
    """Editable trade ticket shown at top of Paper tab when a trade is pending."""
    if not pending:
        return html.Div()

    ticker    = pending.get("ticker","")
    direction = pending.get("direction","long")
    price     = pending.get("entry_price", 0)
    stop      = pending.get("stop_price", 0)
    shares    = pending.get("shares", 0)
    risk      = pending.get("dollar_risk", 0)
    pos_val   = pending.get("position_value", 0)
    conviction= pending.get("conviction", 0)
    wl        = get_full_watchlist()
    label     = wl.get(ticker,{}).get("label", ticker)
    is_long   = direction == "long"
    ac        = "#00e676" if is_long else "#ff1744"

    return html.Div([
        html.Div("📋 New Trade Ticket", style={"fontSize":"16px","fontWeight":"700",
            "color":"#00e676","marginBottom":"4px"}),
        html.Div("Review and adjust before confirming. All fields are editable.",
            style={"color":TEXT_DIM,"fontSize":"12px","marginBottom":"20px"}),

        # Asset + direction header
        html.Div([
            html.Div([
                html.Span(ticker, style={"fontSize":"22px","fontWeight":"900","color":TEXT_PRI,"marginRight":"10px"}),
                html.Span(label,  style={"fontSize":"14px","color":TEXT_SEC}),
            ]),
            html.Div([
                html.Button("Buying",
                    id="ticket-long-btn", n_clicks=0,
                    style={"background":"#00e676" if is_long else BG_DEEP,
                           "color":"#080b12" if is_long else TEXT_DIM,
                           "border":"1px solid #00e676","borderRadius":"6px 0 0 6px",
                           "padding":"8px 18px","cursor":"pointer","fontWeight":"700","fontSize":"13px"}),
                html.Button("Selling",
                    id="ticket-short-btn", n_clicks=0,
                    style={"background":"#ff1744" if not is_long else BG_DEEP,
                           "color":"white" if not is_long else TEXT_DIM,
                           "border":"1px solid #ff1744","borderRadius":"0 6px 6px 0",
                           "padding":"8px 18px","cursor":"pointer","fontWeight":"700","fontSize":"13px"}),
            ], style={"display":"flex"}),
        ], style={"display":"flex","justifyContent":"space-between","alignItems":"center","marginBottom":"20px"}),

        # Editable fields grid
        html.Div([
            # Entry price
            html.Div([
                html.Div("Entry Price", style={"fontSize":"11px","color":TEXT_DIM,"marginBottom":"6px","textTransform":"uppercase"}),
                dcc.Input(id="ticket-price", type="number", value=round(price,4),
                    style={"background":BG_DEEP,"color":TEXT_PRI,"border":f"1px solid {BORDER}",
                           "borderRadius":"6px","padding":"10px 12px","fontSize":"16px",
                           "fontWeight":"700","width":"100%","outline":"none"}),
            ], style={"flex":"1"}),

            # Shares
            html.Div([
                html.Div("Shares", style={"fontSize":"11px","color":TEXT_DIM,"marginBottom":"6px","textTransform":"uppercase"}),
                dcc.Input(id="ticket-shares", type="number", value=round(shares,2),
                    style={"background":BG_DEEP,"color":"#f57c00","border":f"1px solid {BORDER}",
                           "borderRadius":"6px","padding":"10px 12px","fontSize":"16px",
                           "fontWeight":"700","width":"100%","outline":"none"}),
            ], style={"flex":"1"}),

            # Stop price
            html.Div([
                html.Div("Stop Price", style={"fontSize":"11px","color":TEXT_DIM,"marginBottom":"6px","textTransform":"uppercase"}),
                dcc.Input(id="ticket-stop", type="number", value=round(stop,4),
                    style={"background":BG_DEEP,"color":"#ef5350","border":f"1px solid {BORDER}",
                           "borderRadius":"6px","padding":"10px 12px","fontSize":"16px",
                           "fontWeight":"700","width":"100%","outline":"none"}),
            ], style={"flex":"1"}),
        ], style={"display":"flex","gap":"12px","marginBottom":"16px"}),

        # Calculated summary row
        html.Div([
            html.Div([
                html.Div("Position Value", style={"fontSize":"10px","color":TEXT_DIM,"marginBottom":"4px"}),
                html.Div(fmt_price(pos_val, ticker), style={"fontSize":"16px","fontWeight":"700","color":TEXT_PRI}),
            ], style={"background":BG_DEEP,"borderRadius":"8px","padding":"12px","textAlign":"center","flex":"1"}),
            html.Div([
                html.Div("Dollar Risk", style={"fontSize":"10px","color":TEXT_DIM,"marginBottom":"4px"}),
                html.Div(fmt_price(risk, ticker), style={"fontSize":"16px","fontWeight":"700","color":"#ab47bc"}),
            ], style={"background":BG_DEEP,"borderRadius":"8px","padding":"12px","textAlign":"center","flex":"1"}),
            html.Div([
                html.Div("Conviction", style={"fontSize":"10px","color":TEXT_DIM,"marginBottom":"4px"}),
                html.Div(str(conviction), style={"fontSize":"16px","fontWeight":"700","color":score_color(conviction)}),
            ], style={"background":BG_DEEP,"borderRadius":"8px","padding":"12px","textAlign":"center","flex":"1"}),
            html.Div([
                html.Div("Risk %", style={"fontSize":"10px","color":TEXT_DIM,"marginBottom":"4px"}),
                html.Div(f"{pending.get('risk_pct',0)*100:.1f}%",
                    style={"fontSize":"16px","fontWeight":"700","color":TEXT_PRI}),
            ], style={"background":BG_DEEP,"borderRadius":"8px","padding":"12px","textAlign":"center","flex":"1"}),
        ], style={"display":"flex","gap":"10px","marginBottom":"12px"}),

        # Live calculated totals — updates as you type
        html.Div(id="ticket-live-calc", style={"marginBottom":"16px"}),

        # Note field
        html.Div([
            html.Div("Trade Note (optional)", style={"fontSize":"11px","color":TEXT_DIM,"marginBottom":"6px","textTransform":"uppercase"}),
            dcc.Input(id="ticket-note", type="text", placeholder="e.g. Post-earnings compression play...",
                style={"background":BG_DEEP,"color":TEXT_PRI,"border":f"1px solid {BORDER}",
                       "borderRadius":"6px","padding":"10px 12px","fontSize":"13px",
                       "width":"100%","outline":"none"}),
        ], style={"marginBottom":"20px"}),

        # Action buttons
        html.Div([
            html.Button("✅ Confirm Paper Trade",
                id="ticket-confirm-btn", n_clicks=0,
                style={"background":"#00e676","color":"#080b12","border":"none","borderRadius":"8px",
                       "padding":"12px 28px","cursor":"pointer","fontWeight":"900",
                       "fontSize":"15px","flex":"2"}),
            html.Button("✕ Cancel",
                id="ticket-cancel-btn", n_clicks=0,
                style={"background":"none","color":TEXT_DIM,"border":f"1px solid {BORDER}",
                       "borderRadius":"8px","padding":"12px 20px","cursor":"pointer",
                       "fontWeight":"600","fontSize":"13px","marginLeft":"10px","flex":"1"}),
        ], style={"display":"flex"}),

        html.Div(id="ticket-status", style={"color":TEXT_DIM,"fontSize":"12px","marginTop":"8px","textAlign":"center"}),

        # Hidden store with original trade data
        dcc.Store(id="ticket-data", data=pending),

    ], style={"background":BG_CARD,"border":f"2px solid #00e67640","borderLeft":"4px solid #00e676",
              "borderRadius":"12px","padding":"24px","marginBottom":"24px"})

def build_paper_tab(pending: dict = None) -> html.Div:
    """Clean trading blotter — ticket, open positions, closed history, P&L summary."""
    open_trades   = load_open_paper_trades()
    closed_trades = load_closed_paper_trades()
    scorecard     = paper_trade_scorecard()
    live          = get_live_price_snapshot()
    tally         = get_pnl_tally()
    daily_target  = get_daily_target()

    today_pnl    = tally["today"]
    week_pnl     = tally["week"]
    month_pnl    = tally["month"]
    target_hit   = daily_target > 0 and today_pnl >= daily_target
    target_pct   = min(today_pnl / daily_target * 100, 100) if daily_target > 0 else 0

    def pc(v): return "#00e676" if v >= 0 else "#ff1744"
    def pf(v): return f"{'+'if v>=0 else ''}{v:,.2f}"

    # ── Trade ticket (shown when entering a trade) ──
    if pending:
        ticker    = pending.get("ticker","")
        direction = pending.get("direction","long")
        price     = pending.get("entry_price", 0)
        stop      = pending.get("stop_price", 0)
        shares    = pending.get("shares", 0)
        is_long   = direction == "long"
        ac        = "#00e676" if is_long else "#ff1744"
        wl        = get_full_watchlist()
        name      = wl.get(ticker,{}).get("label", ticker)
        live_p    = live.get(ticker) or price

        ticket = html.Div([
            # Ticker header with live price
            html.Div([
                html.Div([
                    html.Span(ticker, style={"fontSize":"26px","fontWeight":"900","color":TEXT_PRI,"marginRight":"10px"}),
                    html.Span(name, style={"fontSize":"14px","color":TEXT_DIM}),
                ]),
                html.Div([
                    html.Div(fmt_price(live_p, ticker),
                        style={"fontSize":"28px","fontWeight":"900","color":ac,"lineHeight":"1"}),
                    html.Div("LIVE", style={"fontSize":"9px","color":TEXT_DIM,
                        "textTransform":"uppercase","letterSpacing":"1px","textAlign":"right"}),
                ]),
            ], style={"display":"flex","justifyContent":"space-between","alignItems":"center",
                      "marginBottom":"20px","paddingBottom":"16px","borderBottom":f"1px solid {BORDER}"}),

            # Buy / Sell toggle
            html.Div([
                html.Button("▲ BUY",  id="ticket-long-btn",  n_clicks=0,
                    style={"background":"#00e676" if is_long else BG_DEEP,
                           "color":"#080b12" if is_long else TEXT_DIM,
                           "border":"2px solid #00e676","borderRadius":"6px 0 0 6px",
                           "padding":"10px 24px","cursor":"pointer","fontWeight":"900","fontSize":"14px"}),
                html.Button("▼ SELL", id="ticket-short-btn", n_clicks=0,
                    style={"background":"#ff1744" if not is_long else BG_DEEP,
                           "color":"white" if not is_long else TEXT_DIM,
                           "border":"2px solid #ff1744","borderRadius":"0 6px 6px 0",
                           "padding":"10px 24px","cursor":"pointer","fontWeight":"900","fontSize":"14px"}),
            ], style={"display":"flex","marginBottom":"16px"}),

            # Price + Stop row
            html.Div([
                html.Div([
                    html.Div([
                        html.Span("PRICE", style={"fontSize":"10px","color":TEXT_DIM,
                            "textTransform":"uppercase","letterSpacing":"1px"}),
                        html.Span(" · live", style={"fontSize":"10px","color":"#00e676"}),
                    ], style={"marginBottom":"6px"}),
                    dcc.Input(id="ticket-price", type="text",
                        value=f"{live_p:.4f}" if live_p < 5 else f"{live_p:.2f}",
                        style={"background":BG_DEEP,"color":TEXT_PRI,
                               "border":f"2px solid {ac}","borderRadius":"8px",
                               "padding":"10px 14px","fontSize":"16px","fontWeight":"700",
                               "width":"100%","outline":"none"}),
                ], style={"flex":"1"}),
                html.Div([
                    html.Div("STOP LOSS", style={"fontSize":"10px","color":TEXT_DIM,
                        "textTransform":"uppercase","letterSpacing":"1px","marginBottom":"6px"}),
                    dcc.Input(id="ticket-stop", type="text",
                        value=f"{stop:.4f}" if stop < 5 else f"{stop:.2f}",
                        style={"background":BG_DEEP,"color":"#ef5350",
                               "border":f"1px solid {BORDER}","borderRadius":"8px",
                               "padding":"10px 14px","fontSize":"16px","fontWeight":"700",
                               "width":"100%","outline":"none"}),
                ], style={"flex":"1"}),
            ], style={"display":"flex","gap":"12px","marginBottom":"12px"}),

            # Quantity — shares or dollar amount toggle
            html.Div([
                html.Div("QUANTITY", style={"fontSize":"10px","color":TEXT_DIM,
                    "textTransform":"uppercase","letterSpacing":"1px","marginBottom":"6px"}),
                html.Div([
                    html.Button("# Shares",
                        id="ticket-mode-shares", n_clicks=0,
                        style={"background":ACCENT,"color":"white",
                               "border":"none","borderRadius":"6px 0 0 6px",
                               "padding":"11px 16px","cursor":"pointer",
                               "fontSize":"12px","fontWeight":"700","whiteSpace":"nowrap"}),
                    html.Button("$ Amount",
                        id="ticket-mode-dollars", n_clicks=0,
                        style={"background":BG_DEEP,"color":TEXT_DIM,
                               "border":f"1px solid {BORDER}","borderRadius":"0 6px 6px 0",
                               "padding":"11px 16px","cursor":"pointer",
                               "fontSize":"12px","fontWeight":"700","whiteSpace":"nowrap"}),
                    dcc.Input(id="ticket-shares", type="number",
                        value=round(shares,0) if shares else None,
                        placeholder="0",
                        style={"background":BG_DEEP,"color":"#f57c00",
                               "border":f"1px solid {BORDER}","borderRadius":"6px",
                               "padding":"10px 14px","fontSize":"16px","fontWeight":"700",
                               "width":"140px","outline":"none","marginLeft":"8px"}),
                    dcc.Input(id="ticket-dollars", type="number",
                        value=None, placeholder="0.00",
                        style={"background":BG_DEEP,"color":"#f57c00",
                               "border":f"1px solid {BORDER}","borderRadius":"6px",
                               "padding":"10px 14px","fontSize":"16px","fontWeight":"700",
                               "width":"160px","outline":"none","marginLeft":"8px",
                               "display":"none"}),
                ], style={"display":"flex","alignItems":"center"}),
            ], style={"marginBottom":"12px"}),

            # Live calc row
            html.Div(id="ticket-live-calc", style={"marginBottom":"12px"}),

            # Note + confirm
            html.Div([
                dcc.Input(id="ticket-note", type="text", placeholder="Notes (optional)",
                    style={"background":BG_DEEP,"color":TEXT_PRI,"border":f"1px solid {BORDER}",
                           "borderRadius":"6px","padding":"10px","fontSize":"13px",
                           "flex":"1","outline":"none"}),
                html.Button("✓ Confirm", id="ticket-confirm-btn", n_clicks=0,
                    style={"background":ac,"color":"#080b12" if is_long else "white",
                           "border":"none","borderRadius":"6px","padding":"10px 28px",
                           "cursor":"pointer","fontWeight":"900","fontSize":"15px",
                           "marginLeft":"10px","whiteSpace":"nowrap"}),
                html.Button("✕", id="ticket-cancel-btn", n_clicks=0,
                    style={"background":"none","color":TEXT_DIM,"border":f"1px solid {BORDER}",
                           "borderRadius":"6px","padding":"10px 14px","cursor":"pointer",
                           "fontSize":"14px","marginLeft":"8px"}),
            ], style={"display":"flex","alignItems":"center"}),

            html.Div(id="ticket-status",
                style={"color":TEXT_DIM,"fontSize":"11px","marginTop":"8px","textAlign":"center"}),
            dcc.Store(id="ticket-data", data=pending),
        ], style={"background":BG_CARD,"border":f"2px solid {ac}40","borderLeft":f"4px solid {ac}",
                  "borderRadius":"10px","padding":"20px","marginBottom":"20px"})
    else:
        ticket = html.Div([dcc.Store(id="ticket-data", data=None)])

    # ── P&L strip ──
    bar_w = f"{target_pct:.0f}%"
    bar_color = pc(today_pnl)
    pnl_strip = html.Div([
        # Left: today with progress bar
        html.Div([
            html.Div([
                html.Span("TODAY  ", style={"fontSize":"10px","color":TEXT_DIM,"textTransform":"uppercase"}),
                html.Span(pf(today_pnl), style={"fontSize":"22px","fontWeight":"900","color":pc(today_pnl),"marginRight":"8px"}),
                html.Span(f"/ ${daily_target:,.0f} target", style={"fontSize":"11px","color":TEXT_DIM}),
                html.Span(" 🎯" if target_hit else "", style={"fontSize":"14px","marginLeft":"4px"}),
            ], style={"marginBottom":"6px"}),
            html.Div([
                html.Div(style={"width":bar_w,"height":"4px","background":bar_color,
                    "borderRadius":"2px"}),
            ], style={"background":BG_DEEP,"borderRadius":"2px","height":"4px","marginBottom":"4px"}),
        ], style={"flex":"2","paddingRight":"20px","borderRight":f"1px solid {BORDER}"}),
        # Right: week / month / all time
        *[html.Div([
            html.Div(label, style={"fontSize":"10px","color":TEXT_DIM,"textTransform":"uppercase","marginBottom":"3px"}),
            html.Div(pf(val), style={"fontSize":"16px","fontWeight":"700","color":pc(val)}),
        ], style={"flex":"1","textAlign":"center"})
        for label, val in [("Week", week_pnl), ("Month", month_pnl), ("All Time", tally["total"])]],
        # Settings gear
        html.Div([
            html.Div("Daily Target", style={"fontSize":"9px","color":TEXT_DIM,"marginBottom":"4px"}),
            html.Div([
                dcc.Input(id="daily-target-input", type="number", value=daily_target,
                    min=0, step=50,
                    style={"background":BG_DEEP,"color":TEXT_PRI,"border":f"1px solid {BORDER}",
                           "borderRadius":"4px 0 0 4px","padding":"4px 8px",
                           "fontSize":"13px","width":"70px","outline":"none"}),
                html.Button("Set", id="set-target-btn", n_clicks=0,
                    style={"background":ACCENT,"color":"white","border":"none",
                           "borderRadius":"0 4px 4px 0","padding":"4px 8px",
                           "cursor":"pointer","fontSize":"12px","fontWeight":"700"}),
            ], style={"display":"flex"}),
            html.Span(id="target-status", style={"fontSize":"10px","color":TEXT_DIM}),
        ], style={"flex":"1","textAlign":"center"}),
    ], style={"display":"flex","alignItems":"center","gap":"16px","background":BG_CARD,
              "borderRadius":"10px","padding":"14px 20px","marginBottom":"16px"})

    # ── Open positions — compact blotter style ──
    open_section_rows = []
    for t in open_trades:
        ticker     = t["ticker"]
        live_price = live.get(ticker) or get_live_price_snapshot().get(ticker) or t["entry_price"]
        entry      = t["entry_price"]
        shares     = t["shares"]
        stop       = t["stop_price"]
        direction  = t["direction"]
        atr        = t.get("atr") or abs(entry - stop) / 2
        is_long    = direction == "long"

        if is_long:
            upnl     = (live_price - entry) * shares
            upnl_pct = (live_price / entry - 1) * 100
            risk_pct_live = (live_price - stop) / entry * 100
            # Trailing stop hint
            if atr and live_price >= entry + 2 * atr:
                trail_hint = f"Move stop → ${entry + atr:,.2f}"
                trail_color = "#00e676"
            elif atr and live_price >= entry + atr:
                trail_hint = f"Move stop → ${entry:,.2f} (breakeven)"
                trail_color = "#f57c00"
            else:
                trail_hint = None
                trail_color = TEXT_DIM
        else:
            upnl     = (entry - live_price) * shares
            upnl_pct = (entry / live_price - 1) * 100
            trail_hint = None
            trail_color = TEXT_DIM

        pnl_c     = pc(upnl)
        days_held = (__import__("datetime").date.today() -
                     __import__("datetime").date.fromisoformat(t["entry_date"])).days
        dir_color = "#00e676" if is_long else "#ff1744"
        dir_label = "▲ LONG" if is_long else "▼ SHORT"

        open_section_rows.append(html.Div([
            # Single compact row
            html.Div([
                # Left: ticker + direction
                html.Div([
                    html.Span(ticker, style={"fontSize":"16px","fontWeight":"900",
                        "color":TEXT_PRI,"marginRight":"8px"}),
                    html.Span(dir_label, style={"fontSize":"10px","fontWeight":"700",
                        "color":dir_color,"marginRight":"12px"}),
                    html.Span(f"Day {days_held}", style={"fontSize":"11px","color":TEXT_DIM}),
                ], style={"display":"flex","alignItems":"center","flex":"1"}),

                # Middle: price data
                html.Div([
                    html.Div([
                        html.Span("Entry ", style={"fontSize":"10px","color":TEXT_DIM}),
                        html.Span(fmt_price(entry,ticker),
                            style={"fontSize":"14px","fontWeight":"700","color":TEXT_PRI}),
                    ], style={"textAlign":"center","marginRight":"20px"}),
                    html.Div([
                        html.Span("Live ", style={"fontSize":"10px","color":TEXT_DIM}),
                        html.Span(fmt_price(live_price,ticker),
                            style={"fontSize":"14px","fontWeight":"700","color":pnl_c}),
                    ], style={"textAlign":"center","marginRight":"20px"}),
                    html.Div([
                        html.Span("Stop ", style={"fontSize":"10px","color":TEXT_DIM}),
                        html.Span(fmt_price(stop,ticker),
                            style={"fontSize":"14px","fontWeight":"700","color":"#ef5350"}),
                    ], style={"textAlign":"center","marginRight":"20px"}),
                    html.Div([
                        html.Span(f"{shares:,.0f} shares", style={"fontSize":"10px","color":TEXT_DIM}),
                    ], style={"textAlign":"center","marginRight":"20px"}),
                ], style={"display":"flex","alignItems":"center"}),

                # Right: P&L + close
                html.Div([
                    html.Div([
                        html.Span(pf(upnl), style={"fontSize":"18px","fontWeight":"900","color":pnl_c}),
                        html.Span(f"  {upnl_pct:+.1f}%",
                            style={"fontSize":"12px","color":pnl_c,"marginLeft":"4px"}),
                    ], style={"marginRight":"16px"}),
                    html.Button("Close",
                        id={"type":"close-paper-trade","trade_id":t["id"]},
                        n_clicks=0,
                        style={"background":"#3a1010","border":"1px solid #ef535060",
                               "color":"#ef5350","borderRadius":"6px","padding":"6px 14px",
                               "cursor":"pointer","fontSize":"12px","fontWeight":"700"}),
                ], style={"display":"flex","alignItems":"center"}),
            ], style={"display":"flex","alignItems":"center","justifyContent":"space-between"}),

            # Trailing stop hint (only shown when relevant)
            html.Div(f"🔒 {trail_hint}",
                style={"fontSize":"11px","color":trail_color,"fontWeight":"600",
                       "marginTop":"6px","paddingTop":"6px",
                       "borderTop":f"1px solid {BORDER}"}) if trail_hint else html.Div(),

        ], style={"background":BG_CARD,
                  "borderLeft":f"4px solid {pnl_c}",
                  "borderRadius":"8px","padding":"12px 16px","marginBottom":"6px"}))

    # ── Stats bar ──
    stats_bar = html.Div()
    if scorecard["closed"] > 0:
        pf_str = f"{scorecard['profit_factor']}" if scorecard["profit_factor"] else "—"
        stats_bar = html.Div([
            *[html.Div([
                html.Div(label, style={"fontSize":"9px","color":TEXT_DIM,"textTransform":"uppercase","marginBottom":"2px"}),
                html.Div(val, style={"fontSize":"13px","fontWeight":"700","color":TEXT_PRI}),
            ], style={"textAlign":"center"})
            for label, val in [
                ("Closed", str(scorecard["closed"])),
                ("Open",   str(scorecard["open"])),
                ("Win Rate", f"{scorecard['win_rate']}%"),
                ("Profit Factor", pf_str),
                ("Avg Return", f"{scorecard['mean_pct']:+.2f}%"),
            ]],
        ], style={"display":"flex","justifyContent":"space-around","background":BG_DEEP,
                  "borderRadius":"8px","padding":"10px","marginBottom":"12px"})

    # ── Closed trades — compact table ──
    def clean_exit(r):
        if not r: return "—"
        rl = r.lower()
        if "stop" in rl:    return "🛑 Stop"
        if "stage" in rl:   return "📉 Regime"
        if "time" in rl:    return "⏱ Time"
        if "manual" in rl:  return "👤 Manual"
        return r[:20]

    th_s = {"padding":"6px 10px","color":TEXT_DIM,"fontSize":"10px","textTransform":"uppercase",
            "fontWeight":"700","borderBottom":f"1px solid {BORDER}","background":BG_DEEP}

    closed_rows = []
    for t in closed_trades[:50]:
        pnl = t.get("pnl") or 0
        pct = t.get("pnl_pct") or 0
        bc  = "#00e676" if pnl >= 0 else "#ff1744"
        closed_rows.append(html.Tr([
            html.Td(t.get("exit_date","—")[-5:],
                style={"padding":"6px 10px","color":TEXT_DIM,"fontSize":"11px"}),
            html.Td(t["ticker"],
                style={"padding":"6px 10px","color":TEXT_PRI,"fontWeight":"700","fontSize":"12px"}),
            html.Td("▲" if t["direction"]=="long" else "▼",
                style={"padding":"6px 10px","color":"#00e676" if t["direction"]=="long" else "#ff1744",
                       "fontSize":"13px","fontWeight":"900","textAlign":"center"}),
            html.Td(fmt_price(t["entry_price"],t["ticker"]),
                style={"padding":"6px 10px","color":TEXT_DIM,"fontSize":"11px","textAlign":"right"}),
            html.Td(fmt_price(t.get("exit_price"),t["ticker"]),
                style={"padding":"6px 10px","color":TEXT_PRI,"fontSize":"11px","textAlign":"right"}),
            html.Td(f"{pf(pnl)}", style={"padding":"6px 10px","color":bc,"fontWeight":"700",
                "fontSize":"12px","textAlign":"right"}),
            html.Td(f"{pct:+.1f}%", style={"padding":"6px 10px","color":bc,"fontSize":"11px","textAlign":"right"}),
            html.Td(clean_exit(t.get("exit_reason","")),
                style={"padding":"6px 10px","color":TEXT_DIM,"fontSize":"10px"}),
        ], style={"borderBottom":f"1px solid {BORDER}"}))

    return html.Div([
        ticket,
        pnl_strip,

        # Open positions
        html.Div([
            html.Div(f"Open  {len(open_trades)}",
                style={"fontSize":"13px","fontWeight":"700","color":TEXT_PRI,"marginBottom":"10px"}),
            *(open_section_rows if open_section_rows else [
                html.Div("No open trades — signals appear in 🚨 Alerts",
                    style={"color":TEXT_DIM,"fontSize":"13px","fontStyle":"italic","padding":"16px 0",
                           "textAlign":"center"})
            ]),
        ], style={"marginBottom":"16px"}),

        stats_bar,

        # Closed trades
        html.Div([
            html.Div(f"History  {len(closed_trades)} trades",
                style={"fontSize":"13px","fontWeight":"700","color":TEXT_PRI,"marginBottom":"10px"}),
            html.Div([
                html.Table([
                    html.Thead(html.Tr([html.Th(c, style=th_s)
                        for c in ["Date","Ticker","","Entry","Exit","P&L","%","Exit Reason"]])),
                    html.Tbody(closed_rows if closed_rows else [
                        html.Tr([html.Td("No closed trades yet",
                            style={"padding":"20px","color":TEXT_DIM,"textAlign":"center",
                                   "fontStyle":"italic"})])
                    ]),
                ], style={"width":"100%","borderCollapse":"collapse"}),
            ], style={"overflowX":"auto"}),
        ], style={"background":BG_CARD,"borderRadius":"10px","padding":"16px 20px"}),
    ])


def _alert_page_html(sig: dict) -> str:
    """Generate a self-contained HTML alert page for one trade signal."""
    ticker    = sig["ticker"]
    direction = sig.get("direction","long").upper()
    conviction= sig.get("conviction",0)
    price     = sig.get("price") or 0
    stop      = sig.get("stop_price") or 0
    shares    = sig.get("shares") or 0
    risk_amt  = sig.get("dollar_risk") or 0
    pos_val   = sig.get("position_value") or 0
    state     = sig.get("state","")
    stage     = sig.get("stage",0)
    time_stop = sig.get("time_stop_date","10 days")
    curr      = sig.get("currency","$")
    wl        = get_full_watchlist()
    label     = wl.get(ticker,{}).get("label", ticker)
    is_long   = direction == "LONG"
    action_color = "#00e676" if is_long else "#ff1744"
    action_text  = "🟢 GET IN — LONG" if is_long else "🔴 GET IN — SHORT"
    risk_pct_val = sig.get("risk_pct",0)

    exit_stop_color = "#ef5350"
    fmt_p = lambda p: f"${p:,.2f}" if p >= 1 else f"${p:,.4f}"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>🚨 {ticker} — Tsunami Alert</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#080b12;color:#e8eaf6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}}
  .card{{background:#0f1420;border:2px solid {action_color};border-radius:16px;padding:40px;max-width:560px;width:100%}}
  .action{{font-size:42px;font-weight:900;color:{action_color};text-align:center;letter-spacing:2px;margin-bottom:8px}}
  .ticker{{font-size:22px;font-weight:700;text-align:center;color:#e8eaf6;margin-bottom:4px}}
  .label{{font-size:14px;color:#7986cb;text-align:center;margin-bottom:32px}}
  .grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:32px}}
  .cell{{background:#080b12;border-radius:10px;padding:14px;text-align:center}}
  .cell-label{{font-size:10px;color:#424870;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px}}
  .cell-value{{font-size:22px;font-weight:800;color:#e8eaf6}}
  .cell-value.green{{color:#00e676}}
  .cell-value.red{{color:#ff1744}}
  .cell-value.amber{{color:#f57c00}}
  .section{{margin-bottom:24px}}
  .section-title{{font-size:11px;color:#424870;text-transform:uppercase;letter-spacing:1px;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid #1e2740}}
  .exit-row{{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid #1e2740}}
  .exit-row:last-child{{border-bottom:none}}
  .exit-label{{font-size:13px;color:#7986cb}}
  .exit-value{{font-size:13px;font-weight:700;color:#ef5350}}
  .conviction-bar{{background:#1e2740;border-radius:4px;height:8px;margin-top:8px;overflow:hidden}}
  .conviction-fill{{height:100%;border-radius:4px;background:{action_color};width:{conviction}%}}
  .footer{{font-size:11px;color:#424870;text-align:center;margin-top:24px}}
  .pulse{{animation:pulse 2s infinite}}
  @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:0.6}}}}
</style>
</head>
<body>
<div class="card">
  <div class="action pulse">{action_text}</div>
  <div class="ticker">{ticker}</div>
  <div class="label">{label} · Stage {stage} · {STATE_LABEL.get(state, state.replace('_',' ').title())}</div>

  <div class="grid">
    <div class="cell">
      <div class="cell-label">Entry Price</div>
      <div class="cell-value">{fmt_p(price)}</div>
    </div>
    <div class="cell">
      <div class="cell-label">Direction</div>
      <div class="cell-value {'green' if is_long else 'red'}">{direction}</div>
    </div>
    <div class="cell">
      <div class="cell-label">Shares</div>
      <div class="cell-value amber">{shares:,.1f}</div>
    </div>
    <div class="cell">
      <div class="cell-label">Position Value</div>
      <div class="cell-value">{fmt_p(pos_val)}</div>
    </div>
  </div>

  <div class="section">
    <div class="section-title">Conviction — {conviction}/100</div>
    <div class="conviction-bar"><div class="conviction-fill"></div></div>
  </div>

  <div class="section">
    <div class="section-title">🚪 Exit Rules — First One Wins</div>
    <div class="exit-row">
      <span class="exit-label">🛑 Stop Loss</span>
      <span class="exit-value">Price ≤ {fmt_p(stop)} ({curr}{abs(price-stop):,.2f} risk/share)</span>
    </div>
    <div class="exit-row">
      <span class="exit-label">📉 Stage Collapse</span>
      <span class="exit-value">If Stage drops below 2</span>
    </div>
    <div class="exit-row">
      <span class="exit-label">⏱ Time Stop</span>
      <span class="exit-value">Exit by {time_stop}</span>
    </div>
  </div>

  <div class="section">
    <div class="section-title">💰 Risk</div>
    <div class="exit-row">
      <span class="exit-label">Dollar Risk</span>
      <span class="exit-value">{fmt_p(risk_amt)} ({risk_pct_val*100:.1f}% of portfolio)</span>
    </div>
    <div class="exit-row">
      <span class="exit-label">Stop Price</span>
      <span class="exit-value">{fmt_p(stop)}</span>
    </div>
  </div>

  <div class="footer">Generated by Tsunami · {date.today().strftime('%A %B %d, %Y')} · Close this tab when done</div>
</div>
</body>
</html>"""


# Store current alert signals so the route can read them
_current_alert_signals: list[dict] = []
_alert_signals_lock = threading.Lock()

def _set_alert_signals(signals: list[dict]) -> None:
    global _current_alert_signals
    with _alert_signals_lock:
        _current_alert_signals = signals

def _get_alert_signal(ticker: str) -> dict | None:
    with _alert_signals_lock:
        return next((s for s in _current_alert_signals if s["ticker"] == ticker), None)


def _get_out_page_html(trade: dict) -> str:
    """Standalone GET OUT alert page for an open trade."""
    ticker      = trade["ticker"]
    direction   = trade.get("direction","long").upper()
    exit_reason = trade.get("exit_reason","Exit triggered")
    exit_price  = trade.get("suggested_exit") or 0
    entry_price = trade.get("entry_price") or 0
    stop        = trade.get("stop_price") or 0
    shares      = trade.get("shares") or 0
    entry_date  = trade.get("entry_date","")
    wl          = get_full_watchlist()
    label       = wl.get(ticker,{}).get("label", ticker)
    fmt_p       = lambda p: f"${p:,.2f}" if p >= 1 else f"${p:,.4f}"
    pnl         = (exit_price - entry_price) * shares if direction=="LONG" else (entry_price - exit_price) * shares
    pnl_pct     = (exit_price/entry_price - 1)*100 if entry_price else 0
    pnl_color   = "#00e676" if pnl >= 0 else "#ff1744"
    pnl_sign    = "+" if pnl >= 0 else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>🚨 GET OUT — {ticker}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#080b12;color:#e8eaf6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}}
  .card{{background:#0f1420;border:2px solid #ff1744;border-radius:16px;padding:40px;max-width:560px;width:100%}}
  .action{{font-size:42px;font-weight:900;color:#ff1744;text-align:center;letter-spacing:2px;margin-bottom:8px;animation:pulse 1.5s infinite}}
  .ticker{{font-size:22px;font-weight:700;text-align:center;margin-bottom:4px}}
  .reason{{font-size:15px;color:#ef5350;text-align:center;font-weight:700;margin-bottom:32px;padding:10px;background:#ff174420;border-radius:8px}}
  .grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:24px}}
  .cell{{background:#080b12;border-radius:10px;padding:14px;text-align:center}}
  .cell-label{{font-size:10px;color:#424870;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px}}
  .cell-value{{font-size:22px;font-weight:800;color:#e8eaf6}}
  .pnl{{font-size:32px;font-weight:900;color:{pnl_color};text-align:center;margin-bottom:24px}}
  .footer{{font-size:11px;color:#424870;text-align:center;margin-top:24px}}
  @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:0.5}}}}
</style>
</head>
<body>
<div class="card">
  <div class="action">🔴 GET OUT NOW</div>
  <div class="ticker">{label} ({ticker}) — {direction}</div>
  <div class="reason">{exit_reason}</div>
  <div class="pnl">{pnl_sign}{fmt_p(pnl)}  ({pnl_sign}{pnl_pct:.2f}%)</div>
  <div class="grid">
    <div class="cell">
      <div class="cell-label">Entry Price</div>
      <div class="cell-value">{fmt_p(entry_price)}</div>
    </div>
    <div class="cell">
      <div class="cell-label">Exit Price (now)</div>
      <div class="cell-value" style="color:#ff1744">{fmt_p(exit_price)}</div>
    </div>
    <div class="cell">
      <div class="cell-label">Stop Was</div>
      <div class="cell-value">{fmt_p(stop)}</div>
    </div>
    <div class="cell">
      <div class="cell-label">Shares</div>
      <div class="cell-value">{shares:,.1f}</div>
    </div>
  </div>
  <div class="footer">Entered {entry_date} · Generated by Tsunami · {date.today().strftime('%A %B %d, %Y')}</div>
</div>
</body>
</html>"""


from flask import Response as FlaskResponse

# Flask route — GET IN alert page
@app.server.route("/alert/<ticker>")
def alert_page(ticker):
    sig = _get_alert_signal(ticker.upper())
    if not sig:
        return FlaskResponse("<h2 style='font-family:sans-serif;color:#ef5350;padding:40px'>No active alert for " + ticker + "</h2>", status=404)
    return FlaskResponse(_alert_page_html(sig), mimetype="text/html")

# Dev test route — injects a fake GET IN signal using real scan data
# Visit http://localhost:8050/test-alert to activate, /test-alert/clear to remove
@app.server.route("/test-alert")
def test_alert():
    import yfinance as yf
    rows = load_latest()
    if not rows:
        return FlaskResponse("No scan data found", status=500)
    r = next((x for x in rows), rows[0])
    ticker = r["ticker"]
    price  = r.get("price") or 100
    try:
        atr = get_atr(ticker) or price * 0.02
    except Exception:
        atr = price * 0.02
    from tsunami_trades import ATR_MULT, DEFAULT_PORT, position_size
    stop   = price - atr * ATR_MULT
    sizing = position_size(price, stop, 70, DEFAULT_PORT)
    fake   = {**r, "conviction":70, "direction":"long",
              "stop_price":sizing["stop_price"], "shares":sizing["shares"],
              "dollar_risk":sizing["dollar_risk"], "position_value":sizing["position_value"],
              "risk_pct":sizing["risk_pct"], "time_stop_date":"2026-04-09", "currency":"$"}
    _set_alert_signals([fake])
    return FlaskResponse(
        f"<html><body style='background:#080b12;color:#e8eaf6;font-family:sans-serif;padding:40px'>"
        f"<h2>✅ Fake signal injected for <b>{ticker}</b></h2>"
        f"<p style='margin-top:16px'><a href='/alert/{ticker}' target='_blank' "
        f"style='color:#00e676;font-size:18px'>→ Open alert page for {ticker}</a></p>"
        f"<p style='margin-top:12px'><a href='/test-alert/clear' style='color:#ef5350'>Clear test signal</a></p>"
        f"</body></html>", mimetype="text/html")

@app.server.route("/test-alert/clear")
def clear_test_alert():
    _set_alert_signals([])
    return FlaskResponse(
        "<html><body style='background:#080b12;color:#e8eaf6;font-family:sans-serif;padding:40px'>"
        "<h2>✅ Test signal cleared</h2>"
        "<p style='margin-top:12px'><a href='/' style='color:#7986cb'>← Back to dashboard</a></p>"
        "</body></html>", mimetype="text/html")

# Flask route for GET OUT page
@app.server.route("/getout/<ticker>")
def getout_page(ticker):
    ticker = ticker.upper()
    rows   = load_latest()
    for r in rows: r["conviction"] = conviction_score(r)
    live   = get_live_price_snapshot()
    open_trades = load_open_trades()
    exits  = check_exit_signals(open_trades, rows, live)
    trade  = next((t for t in exits if t["ticker"] == ticker), None)
    if not trade:
        # show page even if not currently triggered — find the open trade
        trade = next((t for t in open_trades if t["ticker"] == ticker), None)
        if trade:
            price = live.get(ticker) or trade.get("entry_price",0)
            trade = {**trade, "exit_reason":"Manual review", "suggested_exit": price}
    if not trade:
        return FlaskResponse(f"<h2 style='font-family:sans-serif;color:#ef5350;padding:40px'>No open trade for {ticker}</h2>", status=404)
    return FlaskResponse(_get_out_page_html(trade), mimetype="text/html")


# ---------------------------------------------------------------------------
# Alerts tab builder
# ---------------------------------------------------------------------------

def build_alerts_tab(entry_signals: list[dict], exit_signals: list[dict]) -> html.Div:
    total = len(entry_signals) + len(exit_signals)

    if total == 0:
        return html.Div([
            html.Div("🚨 Trade Alerts", style={"fontSize":"18px","fontWeight":"700","color":TEXT_PRI,"marginBottom":"8px"}),
            html.Div("No actionable signals right now.",
                style={"color":TEXT_DIM,"fontSize":"14px","marginBottom":"4px"}),
            html.Div("Watching for: Stage 5 + conviction ≥ 65 + breakout bias (GET IN) · Stop / stage collapse / time stop (GET OUT)",
                style={"color":TEXT_DIM,"fontSize":"12px","fontStyle":"italic","padding":"30px 0","textAlign":"center"}),
        ])

    sections = []

    # ---- GET OUT (most urgent — show first) ----
    if exit_signals:
        exit_cards = []
        for trade in exit_signals:
            ticker      = trade["ticker"]
            exit_reason = trade.get("exit_reason","Exit triggered")
            exit_price  = trade.get("suggested_exit") or 0
            entry_price = trade.get("entry_price") or 0
            shares      = trade.get("shares") or 0
            direction   = trade.get("direction","long").upper()
            wl          = get_full_watchlist()
            label       = wl.get(ticker,{}).get("label", ticker)
            pnl         = (exit_price-entry_price)*shares if direction=="LONG" else (entry_price-exit_price)*shares
            pnl_pct     = (exit_price/entry_price-1)*100 if entry_price else 0
            pnl_color   = "#00e676" if pnl>=0 else "#ff1744"
            fmt_p       = lambda p: f"${p:,.2f}" if p>=1 else f"${p:,.4f}"

            exit_cards.append(html.Div([
                html.Div("🔴 GET OUT NOW", style={"fontSize":"26px","fontWeight":"900","color":"#ff1744",
                    "letterSpacing":"1px","marginBottom":"6px"}),
                html.Div(f"{label} ({ticker}) — {direction}",
                    style={"fontSize":"16px","fontWeight":"700","color":TEXT_PRI,"marginBottom":"6px"}),
                html.Div(exit_reason, style={"fontSize":"13px","color":"#ef5350","fontWeight":"700",
                    "background":"#ff174418","borderRadius":"6px","padding":"8px 12px","marginBottom":"16px"}),
                html.Div([
                    html.Div([html.Div("Entry",style={"fontSize":"10px","color":TEXT_DIM,"marginBottom":"4px"}),
                        html.Div(fmt_p(entry_price),style={"fontSize":"18px","fontWeight":"800","color":TEXT_PRI})],
                        style={"background":BG_DEEP,"borderRadius":"8px","padding":"10px","textAlign":"center","flex":"1"}),
                    html.Div([html.Div("Exit Now",style={"fontSize":"10px","color":TEXT_DIM,"marginBottom":"4px"}),
                        html.Div(fmt_p(exit_price),style={"fontSize":"18px","fontWeight":"800","color":"#ff1744"})],
                        style={"background":BG_DEEP,"borderRadius":"8px","padding":"10px","textAlign":"center","flex":"1"}),
                    html.Div([html.Div("P&L",style={"fontSize":"10px","color":TEXT_DIM,"marginBottom":"4px"}),
                        html.Div(f"{'+' if pnl>=0 else ''}{fmt_p(pnl)} ({'+' if pnl_pct>=0 else ''}{pnl_pct:.1f}%)",
                            style={"fontSize":"16px","fontWeight":"800","color":pnl_color})],
                        style={"background":BG_DEEP,"borderRadius":"8px","padding":"10px","textAlign":"center","flex":"1"}),
                ],style={"display":"flex","gap":"10px","marginBottom":"14px"}),
                html.A("🔴 Open Full Exit Alert →", href=f"/getout/{ticker}", target="_blank",
                    style={"display":"block","background":"#ff1744","color":"white","fontWeight":"900",
                        "fontSize":"14px","textAlign":"center","padding":"12px","borderRadius":"8px",
                        "textDecoration":"none","letterSpacing":"1px"}),
            ],style={"background":BG_CARD,"border":"2px solid #ff1744","borderRadius":"12px",
                "padding":"24px","marginBottom":"14px"}))

        sections.append(html.Div([
            html.Div(f"🔴 GET OUT — {len(exit_signals)} Open Trade{'s' if len(exit_signals)>1 else ''} Need Action",
                style={"fontSize":"15px","fontWeight":"700","color":"#ff1744","marginBottom":"14px"}),
            *exit_cards,
        ], style={"marginBottom":"28px"}))

    # ---- GET IN ----
    if entry_signals:
        entry_cards = []
        for sig in entry_signals:
            ticker     = sig["ticker"]
            direction  = sig.get("direction","long").upper()
            conviction = sig.get("conviction",0)
            price      = sig.get("price") or 0
            stop       = sig.get("stop_price") or 0
            shares     = sig.get("shares") or 0
            risk_amt   = sig.get("dollar_risk") or 0
            stage      = sig.get("stage",0)
            state      = sig.get("state","")
            wl         = get_full_watchlist()
            label      = wl.get(ticker,{}).get("label", ticker)
            is_long    = direction=="LONG"
            ac         = "#00e676" if is_long else "#ff1744"
            action     = "🟢 GET IN — LONG" if is_long else "🔴 GET IN — SHORT"
            fmt_p      = lambda p: f"${p:,.2f}" if p>=1 else f"${p:,.4f}"

            entry_cards.append(html.Div([
                html.Div(action, style={"fontSize":"26px","fontWeight":"900","color":ac,
                    "letterSpacing":"1px","marginBottom":"6px"}),
                html.Div(f"{label} ({ticker})",
                    style={"fontSize":"16px","fontWeight":"700","color":TEXT_PRI,"marginBottom":"2px"}),
                html.Div(f"Stage {stage} · {STATE_LABEL.get(state, state.replace('_',' ').title())} · Conviction {conviction}/100",
                    style={"fontSize":"12px","color":TEXT_SEC,"marginBottom":"18px"}),
                html.Div([
                    html.Div([html.Div("Entry",style={"fontSize":"10px","color":TEXT_DIM,"marginBottom":"4px"}),
                        html.Div(fmt_p(price),style={"fontSize":"18px","fontWeight":"800","color":TEXT_PRI})],
                        style={"background":BG_DEEP,"borderRadius":"8px","padding":"10px","textAlign":"center","flex":"1"}),
                    html.Div([html.Div("Stop",style={"fontSize":"10px","color":TEXT_DIM,"marginBottom":"4px"}),
                        html.Div(fmt_p(stop),style={"fontSize":"18px","fontWeight":"800","color":"#ef5350"})],
                        style={"background":BG_DEEP,"borderRadius":"8px","padding":"10px","textAlign":"center","flex":"1"}),
                    html.Div([html.Div("Shares",style={"fontSize":"10px","color":TEXT_DIM,"marginBottom":"4px"}),
                        html.Div(f"{shares:,.1f}",style={"fontSize":"18px","fontWeight":"800","color":"#f57c00"})],
                        style={"background":BG_DEEP,"borderRadius":"8px","padding":"10px","textAlign":"center","flex":"1"}),
                    html.Div([html.Div("$ Risk",style={"fontSize":"10px","color":TEXT_DIM,"marginBottom":"4px"}),
                        html.Div(fmt_p(risk_amt),style={"fontSize":"18px","fontWeight":"800","color":"#ab47bc"})],
                        style={"background":BG_DEEP,"borderRadius":"8px","padding":"10px","textAlign":"center","flex":"1"}),
                ],style={"display":"flex","gap":"10px","marginBottom":"14px"}),
                html.Div([
                html.A("🚨 Open Full Alert →", href=f"/alert/{ticker}", target="_blank",
                    style={"display":"block","background":ac,"color":"#080b12","fontWeight":"900",
                        "fontSize":"14px","textAlign":"center","padding":"12px","borderRadius":"8px",
                        "textDecoration":"none","letterSpacing":"1px","flex":"1"}),
                html.Button("📝 Paper Trade",
                    id={"type":"paper-enter","ticker":ticker},
                    n_clicks=0,
                    style={"background":"#1a3a2a","color":"#00e676","border":"1px solid #00e67640",
                        "borderRadius":"8px","padding":"12px","cursor":"pointer","fontWeight":"700",
                        "fontSize":"13px","marginLeft":"8px"}),
                ], style={"display":"flex","gap":"8px"}),
            ],style={"background":BG_CARD,"border":f"2px solid {ac}","borderRadius":"12px",
                "padding":"24px","marginBottom":"14px"}))

        sections.append(html.Div([
            html.Div(f"🟢 GET IN — {len(entry_signals)} New Signal{'s' if len(entry_signals)>1 else ''}",
                style={"fontSize":"15px","fontWeight":"700","color":"#00e676","marginBottom":"14px"}),
            *entry_cards,
        ]))

    return html.Div([
        html.Div(f"🚨 {total} Active Alert{'s' if total>1 else ''}",
            style={"fontSize":"18px","fontWeight":"700","color":"#ef5350","marginBottom":"4px"}),
        html.Div("Click any alert to open a full trade card in a new tab.",
            style={"color":TEXT_DIM,"fontSize":"13px","marginBottom":"24px"}),
        *sections,
    ])


# ---------------------------------------------------------------------------
# Nightly scan callbacks
# ---------------------------------------------------------------------------

@app.callback(
    Output("tab-content","children", allow_duplicate=True),
    Output("all-rows","data", allow_duplicate=True),
    Output("bucket-row","children", allow_duplicate=True),
    Input({"type":"nightly-add","ticker":ALL,"name":ALL},"n_clicks"),
    prevent_initial_call=True,
)
def nightly_add_to_grid(n_clicks_list):
    """Add a single nightly signal, scan it immediately, refresh grid."""
    if not ctx.triggered or not any(v for v in n_clicks_list if v):
        raise PreventUpdate
    tid = ctx.triggered_id
    if not tid or not isinstance(tid, dict): raise PreventUpdate
    ticker = tid.get("ticker","")
    name   = tid.get("name", ticker)
    if not ticker: raise PreventUpdate
    # Add to watchlist and scan immediately so it appears on grid now
    add_custom_ticker(ticker, name)
    try:
        run_scan([ticker])
    except Exception:
        pass
    # Reload all rows so grid updates without needing Scan All
    rows = load_latest()
    if rows:
        for r in rows: r["conviction"] = conviction_score(r)
    return build_nightly_tab(), rows, bucket_summary(rows)


@app.callback(
    Output("nightly-promote-status","children"),
    Output("tab-content","children", allow_duplicate=True),
    Output("all-rows","data", allow_duplicate=True),
    Output("bucket-row","children", allow_duplicate=True),
    Input("nightly-promote-all-btn","n_clicks"),
    prevent_initial_call=True,
)
def nightly_promote_all(n):
    if not n: raise PreventUpdate
    best = load_nightly_best(min_stage=4, min_conviction=50, limit=30)
    wl   = get_full_watchlist()
    added = []
    for r in best:
        if r["ticker"] not in wl:
            name = NAME_MAP.get(r["ticker"], r["ticker"])
            add_custom_ticker(r["ticker"], name)
            added.append(r["ticker"])
    if added:
        try:
            run_scan(added)
        except Exception:
            pass
        status = f"✅ Added {len(added)} to grid: {', '.join(added)}"
    else:
        status = "ℹ️ All candidates already on grid"
    rows = load_latest()
    if rows:
        for r in rows: r["conviction"] = conviction_score(r)
    return status, build_nightly_tab(), rows, bucket_summary(rows)

# ---------------------------------------------------------------------------
# Nightly scan callbacks
# ---------------------------------------------------------------------------

@app.callback(
    Output("nightly-scan-status","children"),
    Output("tab-content","children", allow_duplicate=True),
    Input("nightly-scan-btn","n_clicks"),
    Input("nightly-tsx-btn","n_clicks"),
    Input("nightly-nyse-btn","n_clicks"),
    Input("nightly-crypto-btn","n_clicks"),
    prevent_initial_call=True,
)
def trigger_nightly_scan(full, tsx, nyse, crypto):
    if not any([full, tsx, nyse, crypto]):
        raise PreventUpdate
    triggered = ctx.triggered_id
    markets = {
        "nightly-scan-btn":   ["tsx","nyse","crypto"],
        "nightly-tsx-btn":    ["tsx"],
        "nightly-nyse-btn":   ["nyse"],
        "nightly-crypto-btn": ["crypto"],
    }.get(triggered, ["tsx","nyse","crypto"])
    labels = {"tsx":"🍁 TSX","nyse":"🇺🇸 US","crypto":"₿ Crypto"}
    running = " + ".join(labels[m] for m in markets)
    try:
        summary = run_nightly_scan(markets=markets)
        status  = (f"✅ Done — {summary['total']} scanned · "
                   f"{summary['signals']} signals · "
                   f"TSX:{summary['tsx']} US:{summary['nyse']} Crypto:{summary['crypto']}")
    except Exception as e:
        status = f"❌ Scan error: {str(e)[:60]}"
    return status, build_nightly_tab()


# ---------------------------------------------------------------------------
# Paper tab — profit target callback
# ---------------------------------------------------------------------------

@app.callback(
    Output("target-status","children"),
    Output("tab-content","children", allow_duplicate=True),
    Input("set-target-btn","n_clicks"),
    State("daily-target-input","value"),
    prevent_initial_call=True,
)
def save_daily_target(n, value):
    if not n or value is None:
        raise PreventUpdate
    try:
        v = float(value)
        if v < 0:
            return "❌ Target must be positive", dash.no_update
        set_daily_target(v)
        return f"✅ Target set to ${v:,.0f}", build_paper_tab(None)
    except Exception as e:
        return f"❌ {str(e)[:40]}", dash.no_update

# ---------------------------------------------------------------------------
# Alert callbacks
# ---------------------------------------------------------------------------

@app.callback(
    Output("alert-signals","data"),
    Output("alerts-tab","label"),
    Input("all-rows","data"),
    Input("live-prices","data"),
)
def update_alert_signals(rows, live_prices):
    """Recompute entry + exit signals whenever rows or live prices update."""
    if not rows:
        return [], "🚨 Alerts"
    # Fall back to in-process cache if Dash store is still empty (cold-start)
    effective_prices = live_prices or get_live_price_snapshot()
    if effective_prices:
        rows = [{**r,"price":effective_prices[r["ticker"]]} if r["ticker"] in effective_prices else r
                for r in rows]
    portfolio    = get_portfolio_value()
    entry_sigs   = get_entry_signals(rows, portfolio)
    open_trades  = load_open_trades()
    exit_sigs    = check_exit_signals(open_trades, rows, effective_prices or {})
    _set_alert_signals(entry_sigs)
    total = len(entry_sigs) + len(exit_sigs)
    label = f"🚨 Alerts  ●{total}●" if total > 0 else "🚨 Alerts"
    # Store both in data as a dict
    return {"entry": entry_sigs, "exit": exit_sigs}, label


if __name__=="__main__":
    init_db();init_validation_tables();init_trades_tables()
    existing=load_latest()
    if not existing:
        print("No data — running initial scan...")
        run_scan()
    print("\n🌊 Tsunami Dashboard")
    print("   http://localhost:8050\n")
    app.run(debug=False,port=8050)
