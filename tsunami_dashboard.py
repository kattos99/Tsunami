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
from dash import Dash, Input, Output, State, dcc, html, ctx
from dash.dependencies import ALL
from dash.exceptions import PreventUpdate
from tsunami_engine import get_full_watchlist, load_latest, run_scan, init_db, DB_PATH
from tsunami_validation import (init_validation_tables, log_signals, check_pending_outcomes,
    load_pending, load_resolved, load_scorecard, HORIZONS)
from tsunami_universe import (
    init_universe_table, run_universe_scan, load_universe_latest, get_universe_scan_date,
    init_tsx_table, run_tsx_scan, load_tsx_latest, get_tsx_scan_date, TSX_SECTORS
)
from tsunami_trades import (init_trades_tables, get_portfolio_value, set_portfolio_value,
    add_custom_ticker, load_custom_tickers, get_entry_signals, check_exit_signals,
    load_open_trades, load_closed_trades, log_trade, trade_summary,
    get_cadusd_rate, currency_symbol, is_cad, MIN_CONVICTION, MIN_STAGE)

BG_DEEP="#080b12";BG_CARD="#0f1420";BG_PANEL="#141927";BORDER="#1e2740"
TEXT_PRI="#e8eaf6";TEXT_SEC="#7986cb";TEXT_DIM="#424870";ACCENT="#5c6bc0"

STATE_COLOR={"compressed":"#37474f","coiling":"#f57c00","excursion_reversal":"#66bb6a",
    "sustained_focus":"#ab47bc","early_watch":"#26c6da","pre_breakout":"#ec407a",
    "expanding":"#ff7043","breakout_state":"#ef5350","neutral":"#455a64","insufficient_data":"#263238"}
STATE_EMOJI={"compressed":"😴","coiling":"🌀","excursion_reversal":"🔄","sustained_focus":"🔭",
    "early_watch":"👁","pre_breakout":"⚡","expanding":"💥","breakout_state":"🚀","neutral":"😐","insufficient_data":"⏳"}
STATE_BUCKET={"breakout_state":"🚀 READY","expanding":"🚀 READY","pre_breakout":"⚡ BUILDING",
    "early_watch":"⚡ BUILDING","sustained_focus":"🔭 BUILDING","excursion_reversal":"🔄 EARLY WARNING",
    "coiling":"👀 WATCH","compressed":"👀 WATCH","neutral":"😐 QUIET","insufficient_data":"😐 QUIET"}
BUCKET_ORDER=["🚀 READY","⚡ BUILDING","🔭 BUILDING","🔄 EARLY WARNING","👀 WATCH","😐 QUIET"]
BUCKET_COLOR={"🚀 READY":"#ef5350","⚡ BUILDING":"#ec407a","🔭 BUILDING":"#ab47bc",
    "🔄 EARLY WARNING":"#66bb6a","👀 WATCH":"#f57c00","😐 QUIET":"#455a64"}

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
    try:hist=pd.DataFrame(json.loads(history_json))
    except:return go.Figure()
    hist=hist.dropna(subset=["compression_ratio","cwt_cycle_slope","energy_ratio"])
    if len(hist)<5:return go.Figure()
    n=len(hist);fig=go.Figure()
    fig.add_trace(go.Scatter3d(x=hist["compression_ratio"],y=hist["cwt_cycle_slope"],z=hist["energy_ratio"],
        mode="lines",line=dict(color="rgba(92,107,192,0.3)",width=2),hoverinfo="skip",name="path",showlegend=True))
    fig.add_trace(go.Scatter3d(x=hist["compression_ratio"],y=hist["cwt_cycle_slope"],z=hist["energy_ratio"],
        mode="markers",marker=dict(size=3,color=list(range(n)),colorscale=[[0,"#1a237e"],[0.5,"#5c6bc0"],[1,"#e8eaf6"]],opacity=0.8),
        text=hist.get("date",pd.Series([""]*n)),hovertemplate="%{text}<extra></extra>",name="days",showlegend=True))
    last=hist.iloc[-1];last_color=STATE_COLOR.get(str(last.get("market_state","neutral")),"#ef5350")
    z_off=float(last["energy_ratio"])+0.3
    fig.add_trace(go.Scatter3d(x=[last["compression_ratio"]],y=[last["cwt_cycle_slope"]],z=[z_off],
        mode="markers+text",marker=dict(size=12,color=last_color,symbol="diamond",line=dict(color="white",width=2)),
        text=[f"NOW\n{ticker}"],textfont=dict(color="white",size=11,family="monospace"),
        textposition="top center",name="today",showlegend=True,hovertemplate=f"<b>TODAY — {ticker}</b><extra></extra>"))
    fig.update_layout(height=height,margin=dict(l=0,r=0,t=30 if show_title else 10,b=0),paper_bgcolor=BG_DEEP,
        scene=dict(bgcolor=BG_PANEL,
            xaxis=dict(title="Compression",color=TEXT_SEC,backgroundcolor=BG_PANEL,gridcolor=BORDER),
            yaxis=dict(title="CWT Slope",color=TEXT_SEC,backgroundcolor=BG_PANEL,gridcolor=BORDER),
            zaxis=dict(title="Energy",color=TEXT_SEC,backgroundcolor=BG_PANEL,gridcolor=BORDER)),
        title=dict(text=f"{ticker} — Phase Space" if show_title else "",font=dict(color=TEXT_SEC,size=12)),
        legend=dict(bgcolor=BG_PANEL,font=dict(color=TEXT_SEC),itemsizing="constant"))
    return fig

def get_ai_commentary(r):
    ticker=r["ticker"];wl=get_full_watchlist();info=wl.get(ticker,{"label":ticker})
    state=r.get("state","neutral");stage=r.get("stage",0);score=conviction_score(r)
    prompt=f"You are Tsunami, a market regime detection system. Analyze {info['label']} ({ticker}) in 3-4 sentences. State {state} Stage {stage}, conviction {score}/100. Price: {fmt_price(r.get('price'),ticker)}. Compression: {r.get('compression','n/a')}, Energy: {r.get('energy','n/a')}, CWT slope: {r.get('cwt_slope','n/a')}, Conc: {r.get('cwt_conc','n/a')}, Exc reversal: {'YES' if r.get('exc_reversal') else 'NO'}. End with what to watch for next. No bullets."
    try:
        cfg=json.loads((Path.home()/".claude_config.json").read_text())
        key=cfg.get("anthropic_api_key","")
        resp=requests.post("https://api.anthropic.com/v1/messages",
            headers={"Content-Type":"application/json","x-api-key":key,"anthropic-version":"2023-06-01"},
            json={"model":"claude-sonnet-4-20250514","max_tokens":300,"messages":[{"role":"user","content":prompt}]},timeout=30)
        data=resp.json()
        if "content" in data and data["content"]:return data["content"][0].get("text","").strip()
        return "Analysis unavailable."
    except Exception as e:return f"Analysis unavailable: {str(e)[:60]}"

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
    "TSLA":    ("🌊🌊🌊", "ELITE",        "#ef5350"),
    "NVDA":    ("🌊🌊🌊", "ELITE",        "#ef5350"),
    "XRP-USD": ("🌊🌊",   "COMPATIBLE",   "#66bb6a"),
    "BNB-USD": ("🌊🌊",   "COMPATIBLE",   "#66bb6a"),
    "BTC-USD": ("🌊🌊",   "COMPATIBLE",   "#66bb6a"),
    "SOL-USD": ("🌊🌊",   "COMPATIBLE",   "#66bb6a"),
    "AAPL":    ("🌊🌊",   "COMPATIBLE",   "#66bb6a"),
    "XOM":     ("🌊🌊",   "COMPATIBLE",   "#66bb6a"),
    "ETH-USD": ("⚠️",     "INCOMPATIBLE", "#f57c00"),
    "META":    ("⚠️",     "INCOMPATIBLE", "#f57c00"),
    "MSFT":    ("⚠️",     "INCOMPATIBLE", "#f57c00"),
}

def compatibility_badge(ticker):
    if ticker not in TSUNAMI_RATINGS: return html.Span()
    icon, label, color = TSUNAMI_RATINGS[ticker]
    return html.Span(f"{icon}",
        title=f"Tsunami {label}",
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
    return html.Div(id={"type":"card","index":idx},children=[
        html.Div([
            html.Div([html.Span(f"{emoji} ",style={"fontSize":"15px"}),
                html.Span(info["label"],style={"fontWeight":"700","color":TEXT_PRI,"fontSize":"13px"}),
                html.Span(f" {ticker}",style={"color":TEXT_DIM,"fontSize":"10px","marginLeft":"3px"}),exc_badge,cad_badge,compat_badge]),
            html.Div([html.Span(str(score),style={"fontSize":"22px","fontWeight":"900","color":sc,"marginRight":"10px"}),
                html.Span(state.replace("_"," ").upper(),style={"background":color,"color":"white","fontSize":"9px","padding":"3px 8px","borderRadius":"8px","fontWeight":"700"})],
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
            html.Div(val,style={"fontSize":"12px","fontWeight":"600","color":TEXT_PRI}),
        ],style={"background":BG_DEEP,"borderRadius":"6px","padding":"5px 7px","textAlign":"center"})
          for label,val in [("Compress",fmt_val(r.get("compression"))),("Energy",fmt_val(r.get("energy"))),
            ("Volume",fmt_val(r.get("volume"))),("Slope",fmt_val(r.get("cwt_slope"),2)),
            ("Conc",fmt_val(r.get("cwt_conc"),2)),("Exc Sl",fmt_val(r.get("exc_slope"),3))]]],
        style={"display":"grid","gridTemplateColumns":"repeat(3,1fr)","gap":"5px","marginTop":"10px"}),
        html.Div("Click to expand →",style={"textAlign":"right","fontSize":"9px","color":TEXT_DIM,"marginTop":"8px"}),
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
                html.Div(f"Stage {stage} · {state.replace('_',' ').upper()}",style={"color":color,"fontSize":"13px","fontWeight":"600","marginTop":"4px"})]),
            html.Div([conviction_widget(score,"large"),
                html.Button("✕",id="close-btn",n_clicks=0,style={"background":"none","border":f"1px solid {BORDER}",
                    "color":TEXT_SEC,"borderRadius":"6px","padding":"8px 14px","cursor":"pointer","fontSize":"13px","marginLeft":"12px"})],
                style={"display":"flex","alignItems":"center"}),
        ],style={"display":"flex","justifyContent":"space-between","alignItems":"flex-start","marginBottom":"20px"}),
        html.Div([*[html.Div([
            html.Div(label,style={"fontSize":"10px","color":TEXT_DIM,"textTransform":"uppercase","marginBottom":"3px"}),
            html.Div(val,style={"fontSize":"15px","fontWeight":"700","color":vc}),
        ],style={"background":BG_DEEP,"borderRadius":"8px","padding":"10px 12px","textAlign":"center"})
          for label,val,vc in [
            ("Price",fmt_price(r.get("price"),ticker),TEXT_PRI),("5d Return",pct5_str,pct5c),("20d Return",pct20_str,pct20c),
            ("Compression",fmt_val(r.get("compression")),TEXT_PRI),("Energy",fmt_val(r.get("energy")),TEXT_PRI),
            ("Volume",fmt_val(r.get("volume")),TEXT_PRI),("CWT Cycle",fmt_val(r.get("cwt_cycle"),0)+" bars",TEXT_PRI),
            ("CWT Slope",fmt_val(r.get("cwt_slope"),2),TEXT_PRI),("Conc",fmt_val(r.get("cwt_conc"),2),TEXT_PRI),
            ("Conc 3d",fmt_val(r.get("cwt_conc_3d"),2),TEXT_PRI),("Exc Slope",fmt_val(r.get("exc_slope"),4),TEXT_PRI),
            ("Exc Reversal","✅ YES" if r.get("exc_reversal") else "—",TEXT_PRI)]],
        ],style={"display":"grid","gridTemplateColumns":"repeat(auto-fill,minmax(110px,1fr))","gap":"8px","marginBottom":"20px"}),
        dcc.Graph(figure=phase_fig,config={"displayModeBar":True}),
    ],style={"background":BG_CARD,"border":f"2px solid {color}","borderRadius":"12px","padding":"24px","marginBottom":"20px"})

def intelligence_card(r,commentary):
    ticker=r["ticker"];wl=get_full_watchlist();info=wl.get(ticker,{"label":ticker})
    state=r.get("state","neutral");stage=r.get("stage",0);color=STATE_COLOR.get(state,BORDER)
    emoji=STATE_EMOJI.get(state,"");score=conviction_score(r);pct5_str,pct5c=fmt_pct(r.get("pct_5d"))
    phase_fig=make_phase_chart(ticker,r.get("history_json","[]"),height=280,show_title=False)
    return html.Div([
        html.Div([
            html.Div([html.Span(f"{emoji} ",style={"fontSize":"18px"}),
                html.Span(info["label"],style={"fontWeight":"700","color":TEXT_PRI,"fontSize":"17px"}),
                html.Span(f" {ticker}",style={"color":TEXT_DIM,"fontSize":"12px","marginLeft":"4px"})]),
            html.Div([html.Span(f"Stage {stage}",style={"color":color,"fontWeight":"700","fontSize":"13px","marginRight":"10px"}),
                html.Span(state.replace("_"," ").upper(),style={"background":color,"color":"white","fontSize":"9px","padding":"3px 8px","borderRadius":"8px","fontWeight":"700"}),
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
            html.Div([dcc.Graph(figure=phase_fig,config={"displayModeBar":False},style={"width":"320px"})],style={"flexShrink":"0"}),
        ],style={"display":"flex","alignItems":"flex-start"}),
    ],style={"background":BG_CARD,"border":f"1px solid {color}60","borderLeft":f"5px solid {color}","borderRadius":"12px","padding":"20px","marginBottom":"16px"})

def build_intelligence_tab(rows):
    active=sorted([r for r in rows if r.get("stage",0)>=2],key=lambda r:conviction_score(r),reverse=True)
    if not active:
        return html.Div("All quiet.",style={"color":TEXT_DIM,"textAlign":"center","padding":"60px","fontSize":"16px"})
    return html.Div([
        html.Div([html.Div("🧠 Tsunami Intelligence",style={"fontSize":"16px","fontWeight":"700","color":TEXT_PRI}),
            html.Div(f"{len(active)} assets · sorted by conviction",style={"color":TEXT_DIM,"fontSize":"12px","marginTop":"3px"})],
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
    cadusd = get_cadusd_rate()
    custom = load_custom_tickers()

    # Custom ticker rows with remove button
    custom_rows = []
    for c in custom:
        flag = "🍁" if c["ticker"].endswith(".TO") else "🇺🇸"
        safe_id = c["ticker"].replace(".", "_").replace("^", "_").replace("=", "_")
        custom_rows.append(html.Div([
            html.Div([
                html.Span(flag, style={"marginRight":"8px","fontSize":"14px"}),
                html.Span(c["ticker"], style={"fontWeight":"700","color":TEXT_PRI,"fontSize":"13px","marginRight":"6px"}),
                html.Span(f"— {c['label']}", style={"color":TEXT_DIM,"fontSize":"12px"}),
            ], style={"display":"flex","alignItems":"center"}),
            html.Button("✕ Remove",
                id={"type":"remove-ticker","index":safe_id,"ticker":c["ticker"]},
                n_clicks=0,
                style={"background":"none","border":f"1px solid #ef535040","color":"#ef5350",
                       "borderRadius":"6px","padding":"4px 10px","cursor":"pointer",
                       "fontSize":"11px","fontWeight":"600"}),
        ], style={"display":"flex","justifyContent":"space-between","alignItems":"center",
                  "padding":"10px 0","borderBottom":f"1px solid {BORDER}"}))

    return html.Div([
        # Header
        html.Div([
            html.Div("⚡ Watchlist", style={"fontSize":"16px","fontWeight":"700","color":TEXT_PRI}),
            html.Div(f"CA$1 = US${cadusd:.4f}  ·  {len(custom)} custom tickers",
                style={"color":TEXT_DIM,"fontSize":"12px","marginTop":"3px"}),
        ], style={"marginBottom":"24px","paddingBottom":"16px","borderBottom":f"1px solid {BORDER}"}),

        # Add ticker
        html.Div([
            html.Div("Add a ticker to your watchlist",
                style={"fontSize":"13px","fontWeight":"700","color":TEXT_PRI,"marginBottom":"4px"}),
            html.Div("US stocks: MSFT, AAPL · Canadian (TSX): RY.TO, TD.TO, SHOP.TO",
                style={"fontSize":"11px","color":TEXT_DIM,"marginBottom":"12px"}),
            html.Div([
                dcc.Input(id="ticker-input", type="text", placeholder="e.g. RY.TO or MSFT",
                    debounce=True,
                    style={"background":BG_DEEP,"color":TEXT_PRI,"border":f"1px solid {BORDER}",
                           "borderRadius":"6px","padding":"9px 14px","fontSize":"13px",
                           "width":"160px","outline":"none"}),
                dcc.Input(id="ticker-label", type="text", placeholder="Nickname (optional)",
                    style={"background":BG_DEEP,"color":TEXT_PRI,"border":f"1px solid {BORDER}",
                           "borderRadius":"6px","padding":"9px 14px","fontSize":"13px",
                           "width":"180px","marginLeft":"8px","outline":"none"}),
                html.Button("+ Add",id="add-ticker-btn",n_clicks=0,
                    style={"background":"#66bb6a","color":"white","border":"none","borderRadius":"6px",
                           "padding":"9px 18px","cursor":"pointer","fontSize":"13px",
                           "marginLeft":"8px","fontWeight":"700"}),
                html.Span(id="ticker-status",
                    style={"color":TEXT_DIM,"fontSize":"12px","marginLeft":"12px"}),
            ], style={"display":"flex","alignItems":"center"}),
        ], style={"background":BG_CARD,"borderRadius":"10px","padding":"20px","marginBottom":"20px"}),

        # Custom ticker list
        html.Div([
            html.Div(f"Custom Tickers ({len(custom)})",
                style={"fontSize":"13px","fontWeight":"700","color":TEXT_PRI,"marginBottom":"4px"}),
            html.Div("These are scanned daily alongside the default watchlist",
                style={"fontSize":"11px","color":TEXT_DIM,"marginBottom":"16px"}),
            *(custom_rows if custom_rows else [
                html.Div("No custom tickers added yet.",
                    style={"color":TEXT_DIM,"fontSize":"13px","fontStyle":"italic","padding":"12px 0"}),
            ]),
        ], style={"background":BG_CARD,"borderRadius":"10px","padding":"20px"}),
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
                html.Span(state.replace("_"," ").upper(),
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
                        html.Span(state.replace("_"," ").upper(),
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
    dcc.Interval(id="price-poll",interval=15*60*1000,n_intervals=0),  # 15-min live price poll
    dcc.Store(id="selected-ticker",data=None),
    dcc.Store(id="all-rows",data=[]),
    dcc.Store(id="live-prices",data={}),  # ticker -> live price (float)
    dcc.Store(id="alert-signals",data=[]),  # current GET IN signals
    dcc.Store(id="show-all-grid",data=False),  # toggle quiet stocks
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
        dcc.Tab(label="⚡ Trades",value="trades",style=TAB_STYLE,selected_style=TAB_SEL),
        dcc.Tab(label="🔭 Universe",value="universe",style=TAB_STYLE,selected_style=TAB_SEL),
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
    all-rows every 3 minutes. Writes results into _live_price_cache.
    Never raises — failures are silently skipped so the grid is never broken.
    """
    import yfinance as yf
    while True:
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
        except Exception:
            pass
        time.sleep(15 * 60)  # 15-minute cadence — matches price-poll interval

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

@app.callback(
    Output("bucket-row","children"),Output("last-updated","children"),Output("all-rows","data"),
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
    return brow,upd,rows

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

@app.callback(Output("live-prices","data"),Input("price-poll","n_intervals"))
def update_live_prices(_):
    """Drain the background thread's cache into the Dash store every 3 min."""
    return get_live_price_snapshot()

@app.callback(
    Output("tab-content","children"),
    Input("main-tabs","value"),Input("all-rows","data"),Input("selected-ticker","data"),
    Input("live-prices","data"),Input("show-all-grid","data"),
)
def render_tab(tab,rows,selected_ticker,live_prices,show_all):
    if not rows:rows=load_latest()
    if tab=="intelligence":return build_intelligence_tab(rows)
    if tab=="validation":return build_validation_tab()
    if tab=="universe":return build_universe_tab()
    if tab=="alerts":
        if live_prices:
            rows=[{**r,"price":live_prices[r["ticker"]]} if r["ticker"] in live_prices else r for r in rows]
        portfolio   = get_portfolio_value()
        entry_sigs  = get_entry_signals(rows, portfolio)
        open_trades = load_open_trades()
        exit_sigs   = check_exit_signals(open_trades, rows, live_prices or {})
        return build_alerts_tab(entry_sigs, exit_sigs)
    if tab=="trades":
        fresh_rows=load_latest()
        return build_trades_tab(fresh_rows if fresh_rows else rows)
    detail=html.Div()
    if selected_ticker:
        row=next((r for r in rows if r["ticker"]==selected_ticker),None)
        if row:detail=detail_panel(row)
    # Hide FX rates and indices from grid — reference data only
    wl=get_full_watchlist()
    grid_rows=[r for r in rows if wl.get(r["ticker"],{}).get("category") not in ("FX","Index")]
    # Overlay live prices
    if live_prices:
        grid_rows=[{**r,"price":live_prices[r["ticker"]]} if r["ticker"] in live_prices else r
                   for r in grid_rows]
    # Filter quiet/neutral unless show-all toggled
    QUIET_STATES = {"neutral","insufficient_data","compressed","coiling"}
    active_rows = [r for r in grid_rows if r.get("state","neutral") not in QUIET_STATES]
    quiet_rows  = [r for r in grid_rows if r.get("state","neutral") in QUIET_STATES]
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
    idx=json.loads(triggered["prop_id"].split(".")[0])["index"]
    if rows and idx<len(rows):return rows[idx]["ticker"]
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
    # Check if already in default watchlist
    wl=get_full_watchlist()
    if ticker in wl and ticker not in [c["ticker"] for c in load_custom_tickers()]:
        return f"ℹ️ {ticker} is already in the default watchlist", []
    success=add_custom_ticker(ticker,label or ticker)
    if success:
        try:
            run_scan([ticker])
        except Exception:
            pass
        rows=load_latest()
        return f"✅ {ticker} added and scanned", rows
    return f"❌ Could not add {ticker}", []

@app.callback(
    Output("all-rows","data",allow_duplicate=True),
    Input({"type":"remove-ticker","index":ALL,"ticker":ALL},"n_clicks"),
    prevent_initial_call=True,
)
def remove_ticker_cb(n_clicks_list):
    if not ctx.triggered or not any((v for v in n_clicks_list if v)):raise PreventUpdate
    triggered=ctx.triggered[0]
    if not triggered["value"]:raise PreventUpdate
    # Get ticker from the triggered component's id dict
    prop_id=triggered["prop_id"]
    # ctx.triggered_id is cleaner
    if ctx.triggered_id and isinstance(ctx.triggered_id, dict):
        ticker=ctx.triggered_id.get("ticker","")
    else:
        raise PreventUpdate
    if not ticker:raise PreventUpdate
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

# ---------------------------------------------------------------------------
# Alert page — standalone HTML served at /alert/<ticker>
# ---------------------------------------------------------------------------

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
  <div class="label">{label} · Stage {stage} · {state.replace("_"," ").upper()}</div>

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
                html.Div(f"Stage {stage} · {state.replace('_',' ').upper()} · Conviction {conviction}/100",
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
                html.A("🚨 Open Full Alert →", href=f"/alert/{ticker}", target="_blank",
                    style={"display":"block","background":ac,"color":"#080b12","fontWeight":"900",
                        "fontSize":"14px","textAlign":"center","padding":"12px","borderRadius":"8px",
                        "textDecoration":"none","letterSpacing":"1px"}),
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
    if live_prices:
        rows = [{**r,"price":live_prices[r["ticker"]]} if r["ticker"] in live_prices else r
                for r in rows]
    portfolio    = get_portfolio_value()
    entry_sigs   = get_entry_signals(rows, portfolio)
    open_trades  = load_open_trades()
    exit_sigs    = check_exit_signals(open_trades, rows, live_prices or {})
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
