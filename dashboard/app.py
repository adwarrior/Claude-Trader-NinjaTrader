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
import chart as ch
import controls as ctl

st.set_page_config(page_title="Claude-Trader Monitor", layout="wide",
                   initial_sidebar_state="collapsed")

# Load design tokens + component CSS once.
st.html(f"<style>{(Path(__file__).parent / 'styles.css').read_text()}</style>")

REFRESH_SECS = 5
CHART_REFRESH_SECS = 15  # hourly bars change slowly; redraw less often

st.html("<h1 style='font-family:var(--font-display);color:var(--fg-primary);"
        "margin:0 0 4px'>Claude-Trader <span style='color:var(--accent-amber)'>Monitor</span></h1>"
        "<div class='ct-muted' style='margin-bottom:14px;font-size:12px'>NQ 09-26 · hourly · read-only</div>")


def _render_controls():
    cfg = dl.read_config()
    dry = bool(cfg.get("execution", {}).get("dry_run", False))
    state = dl.read_state()
    has_pending = bool(state.get("pending_entry"))
    has_pos = bool(state.get("in_position"))

    c1, c2, c3, c4 = st.columns([1.4, 1, 1, 2])

    def _toggle_pause():
        ctl.set_paused(st.session_state.pause_toggle)
    c1.toggle("Pause new entries", value=ctl.is_paused(), key="pause_toggle",
              on_change=_toggle_pause,
              help="Bot keeps managing any open trade but arms no new entries.")

    with c2.popover("Cancel order", disabled=not has_pending, use_container_width=True):
        st.caption("Cancel the working resting order on the next bot loop?")
        if st.button("Confirm cancel", key="confirm_cancel", use_container_width=True):
            ctl.request_command("cancel_entry")
            st.toast("Cancel requested — bot will action within ~5s")

    with c3.popover("Flatten", disabled=not has_pos, use_container_width=True):
        st.caption("Flatten the open position and cancel any working order?")
        if st.button("Confirm flatten", key="confirm_flatten", type="primary",
                     use_container_width=True):
            ctl.request_command("flatten")
            st.toast("Flatten requested — bot will action within ~5s")

    pend = ctl.pending_command()
    bits = []
    if ctl.is_paused():
        bits.append("<span style='color:var(--accent-amber)'>PAUSED</span>")
    if pend:
        bits.append(f"<span style='color:var(--color-warning)'>queued: {pend}</span>")
    bits.append("<span class='ct-muted'>dry-run: actions log only</span>" if dry
                else "<span class='ct-muted'>live: actions hit Sim101</span>")
    c4.html("<div style='padding-top:8px;font-size:11px;font-family:var(--font-mono)'>"
            + " · ".join(bits) + "</div>")


def _render_top():
    cfg = dl.read_config()
    dry = bool(cfg.get("execution", {}).get("dry_run", False))
    state = dl.read_state()
    price = dl.read_price()
    trades = dl.parse_trades()
    st.html(ui.status_card(state, price, dry))
    st.html(ui.metrics_strip(dl.pnl_summary(trades)))


def _render_chart():
    df = dl.read_bars()
    state = dl.read_state()
    price = dl.read_price()
    all_fvgs = dl.compute_fvgs(df)
    # show every unfilled zone + the few most recent filled ones (context, dimmed)
    fvgs = [f for f in all_fvgs if not f["filled"]] + \
           [f for f in all_fvgs if f["filled"]][-6:]
    st.html("<div class='ct-h4' style='margin:6px 0 0'>Price &amp; levels</div>")
    st.plotly_chart(ch.price_chart(df, fvgs, state, price),
                    use_container_width=True, config={"displayModeBar": False})


def _render_bottom():
    st.html(ui.decision_panel(dl.read_analysis()))
    st.html(ui.trades_table(dl.parse_trades()))
    st.caption(f"Panels refresh {REFRESH_SECS}s · chart {CHART_REFRESH_SECS}s · reads data/ + logs/")


# Controls render once (they rerun on interaction); data panels auto-refresh via
# fragments so only that block reruns (Streamlit >= 1.33).
_render_controls()
if hasattr(st, "fragment"):
    st.fragment(run_every=REFRESH_SECS)(_render_top)()
    st.fragment(run_every=CHART_REFRESH_SECS)(_render_chart)()
    st.fragment(run_every=REFRESH_SECS)(_render_bottom)()
else:  # very old Streamlit fallback
    _render_top(); _render_chart(); _render_bottom()
    if st.button("Refresh"):
        st.rerun()
