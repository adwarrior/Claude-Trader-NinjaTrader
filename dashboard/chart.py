"""Themed Plotly price + levels chart for the monitor (Phase 2)."""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

# Palette aligned with styles.css design tokens (hex approximations of the oklch).
BG       = "#0c0e13"
SURFACE  = "#161a22"
GRID     = "#252a36"
FG       = "#eff0f2"
MUTED    = "#8a929f"
GREEN    = "#22c55e"
RED      = "#ef4444"
AMBER    = "#f0a830"
TEAL     = "#2ec4b6"


def _line(fig, df, col, color, name, width=1.3, dash=None):
    if col in df.columns and df[col].notna().any():
        fig.add_trace(go.Scatter(
            x=df["DateTime"], y=df[col], mode="lines", name=name,
            line=dict(color=color, width=width, dash=dash), hoverinfo="skip"))


def price_chart(df: pd.DataFrame, fvgs: list[dict], state: dict, price=None) -> go.Figure:
    fig = go.Figure()
    if df is None or df.empty:
        fig.update_layout(template="plotly_dark", paper_bgcolor=BG, plot_bgcolor=BG,
                          height=520, annotations=[dict(text="No bar data",
                          showarrow=False, font=dict(color=MUTED))])
        return fig

    x0, x1 = df["DateTime"].iloc[0], df["DateTime"].iloc[-1]

    # FVG zones (drawn first, behind price).
    for f in fvgs:
        base = GREEN if f["type"] == "bullish" else RED
        opacity = 0.06 if f["filled"] else 0.16
        xend = f["dt_fill"] if f["filled"] and f["dt_fill"] is not None else x1
        fig.add_shape(type="rect", x0=f["dt_start"], x1=xend,
                      y0=f["bottom"], y1=f["top"], line=dict(width=0),
                      fillcolor=base, opacity=opacity, layer="below")

    # Candles.
    fig.add_trace(go.Candlestick(
        x=df["DateTime"], open=df["Open"], high=df["High"], low=df["Low"],
        close=df["Close"], name="NQ",
        increasing_line_color=GREEN, decreasing_line_color=RED,
        increasing_fillcolor=GREEN, decreasing_fillcolor=RED,
        line=dict(width=1)))

    # EMAs.
    _line(fig, df, "EMA21", AMBER, "EMA21")
    _line(fig, df, "EMA75", TEAL, "EMA75")
    _line(fig, df, "EMA150", MUTED, "EMA150")

    # Active order / position levels from bot_state.json.
    active = state.get("in_position") or state.get("pending_entry")
    if active:
        kind = "POSITION" if state.get("in_position") else "PENDING"
        for key, color, label in (("entry", AMBER, f"{kind} entry"),
                                  ("stop", RED, "stop"),
                                  ("target", GREEN, "target")):
            if active.get(key) is not None:
                fig.add_hline(y=float(active[key]), line=dict(color=color, width=1.2, dash="dash"),
                              annotation_text=f"{label} {float(active[key]):,.0f}",
                              annotation_position="right",
                              annotation_font=dict(color=color, size=10))

    # Live price marker.
    if price is not None:
        fig.add_hline(y=float(price), line=dict(color=FG, width=0.8, dash="dot"),
                      annotation_text=f"{float(price):,.0f}", annotation_position="left",
                      annotation_font=dict(color=FG, size=10))

    fig.update_layout(
        template="plotly_dark", paper_bgcolor=BG, plot_bgcolor=BG,
        height=540, margin=dict(l=10, r=70, t=10, b=10),
        font=dict(family="JetBrains Mono, monospace", size=11, color=FG),
        xaxis=dict(gridcolor=GRID, rangeslider=dict(visible=False)),
        yaxis=dict(gridcolor=GRID, side="right"),
        legend=dict(orientation="h", y=1.02, yanchor="bottom", x=0,
                    bgcolor="rgba(0,0,0,0)", font=dict(color=MUTED)),
        hovermode="x unified",
        dragmode="pan",
    )
    return fig
