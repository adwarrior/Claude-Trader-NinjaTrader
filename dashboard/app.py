"""
Claude-Trader monitor — read-only dashboard (Phase 1).

Run (Windows-native, alongside the bot):
    streamlit run dashboard/app.py

Decoupled from the live bot: it only reads files the bot writes. Auto-refreshing
panels use st.fragment(run_every=...) so only the data refreshes, not the whole
page — keeps it responsive (the lag fix vs full native-widget reruns).
"""
from pathlib import Path

import streamlit as st

import data_layer as dl
import components as ui

st.set_page_config(page_title="Claude-Trader Monitor", layout="wide",
                   initial_sidebar_state="collapsed")

# Load design tokens + component CSS once.
st.html(f"<style>{(Path(__file__).parent / 'styles.css').read_text()}</style>")

REFRESH_SECS = 5

st.html("<h1 style='font-family:var(--font-display);color:var(--fg-primary);"
        "margin:0 0 4px'>Claude-Trader <span style='color:var(--accent-amber)'>Monitor</span></h1>"
        "<div class='ct-muted' style='margin-bottom:14px;font-size:12px'>NQ 09-26 · hourly · read-only</div>")


def _render():
    cfg = dl.read_config()
    dry = bool(cfg.get("execution", {}).get("dry_run", False))

    state = dl.read_state()
    analysis = dl.read_analysis()
    price = dl.read_price()
    trades = dl.parse_trades()
    summary = dl.pnl_summary(trades)

    st.html(ui.status_card(state, price, dry))
    st.html(ui.metrics_strip(summary))
    st.html(ui.decision_panel(analysis))
    st.html(ui.trades_table(trades))

    st.caption(f"Auto-refreshing every {REFRESH_SECS}s · "
               f"reads data/ + logs/ · last price {price if price else '—'}")


# Fragment isolates the refresh to just this block (Streamlit >= 1.33).
if hasattr(st, "fragment"):
    _render = st.fragment(run_every=REFRESH_SECS)(_render)
    _render()
else:  # very old Streamlit fallback: manual refresh button
    _render()
    if st.button("Refresh"):
        st.rerun()
