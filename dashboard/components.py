"""HTML component builders for the monitor (rendered via st.html)."""
from __future__ import annotations
from html import escape


def _fmt(x, nd=2):
    try:
        return f"{float(x):,.{nd}f}"
    except (TypeError, ValueError):
        return "—"


def _feed_pill(feed: dict | None) -> str:
    """Small colored pill showing NinjaTrader data-feed health from FeedStatus.csv.

    Green = connected & fresh heartbeat, amber = disconnected (NT auto-reconnecting),
    red = strategy/chart not running, grey = no heartbeat file yet. The full
    explanation rides along as a hover tooltip.
    """
    if not feed:
        return ""
    label = escape(str(feed.get("label", "Unknown")))
    detail = escape(str(feed.get("detail", "")))
    if feed.get("ok"):
        color = "var(--color-success, #3fb950)"
    elif feed.get("connected") is False:
        color = "var(--accent-amber, #d29922)"
    elif feed.get("connected") is None:
        color = "var(--ct-muted, #8b949e)"
    else:
        color = "var(--color-warning, #f85149)"
    age = feed.get("heartbeat_age_sec")
    age_txt = f" · {age:.0f}s" if isinstance(age, (int, float)) else ""
    return (f"<span class='ct-feed-pill' title='{detail}' "
            f"style='display:inline-flex;align-items:center;gap:5px;font-size:11px;"
            f"font-family:var(--font-mono);white-space:nowrap'>"
            f"<span style='width:8px;height:8px;border-radius:50%;background:{color};"
            f"display:inline-block'></span>Feed: {label}{age_txt}</span>")


def status_card(state: dict, price, dry_run: bool, feed: dict | None = None) -> str:
    pending = state.get("pending_entry")
    pos = state.get("in_position")
    if pos:
        side = pos["side"] if "side" in pos else pos.get("direction", "")
        cls = "ct-state-long" if side == "LONG" else "ct-state-short"
        pill = f"IN POSITION · {side}"
        detail = (f"<div class='ct-kv'><span class='k'>Entry</span><span class='v'>{_fmt(pos['entry'])}</span></div>"
                  f"<div class='ct-kv'><span class='k'>Stop</span><span class='v ct-loss'>{_fmt(pos['stop'])}</span></div>"
                  f"<div class='ct-kv'><span class='k'>Target</span><span class='v ct-profit'>{_fmt(pos['target'])}</span></div>")
    elif pending:
        side = pending.get("direction", pending.get("side", ""))
        cls = "ct-state-pending"
        pill = f"PENDING · {side} {pending.get('order_type','')}"
        age = pending.get("bars_alive", 0)
        detail = (f"<div class='ct-kv'><span class='k'>Entry</span><span class='v'>{_fmt(pending['entry'])}</span></div>"
                  f"<div class='ct-kv'><span class='k'>Stop</span><span class='v ct-loss'>{_fmt(pending['stop'])}</span></div>"
                  f"<div class='ct-kv'><span class='k'>Target</span><span class='v ct-profit'>{_fmt(pending['target'])}</span></div>"
                  f"<div class='ct-kv'><span class='k'>Age</span><span class='v'>{age} bars</span></div>")
    else:
        cls = "ct-state-flat"
        pill = "FLAT"
        detail = "<div class='ct-kv'><span class='k'>Working order</span><span class='v ct-muted'>none</span></div>"

    mode_cls = "ct-mode-dry" if dry_run else "ct-mode-live"
    mode_txt = "DRY-RUN" if dry_run else "LIVE · Sim101"
    px = f"<div class='ct-kv'><span class='k'>Price</span><span class='v'>{_fmt(price)}</span></div>"
    return (f"<div class='ct-card'><div class='ct-status-row'>"
            f"<span class='ct-state-pill {cls}'>{pill}</span>{detail}{px}"
            f"{_feed_pill(feed)}"
            f"<span class='ct-mode {mode_cls}'>{mode_txt}</span>"
            f"</div></div>")


def metrics_strip(summary: dict) -> str:
    def cell(lbl, val, cls=""):
        return (f"<div class='ct-metric'><div class='lbl'>{lbl}</div>"
                f"<div class='val {cls}'>{val}</div></div>")
    pts = summary["total_pts"]
    usd = summary["total_usd"]
    pcls = "ct-profit" if pts > 0 else ("ct-loss" if pts < 0 else "")
    return (f"<div class='ct-card'><div class='ct-h4'>Performance (live fills)</div>"
            f"<div class='ct-metrics'>"
            + cell("Net P&amp;L", f"${usd:,.0f}", pcls)
            + cell("Net pts", f"{pts:+.1f}", pcls)
            + cell("Trades", summary["n"])
            + cell("Win rate", f"{summary['win_rate']:.0f}%")
            + cell("Best", f"{summary['best']:+.0f}", "ct-profit")
            + cell("Worst", f"{summary['worst']:+.0f}", "ct-loss")
            + "</div></div>")


def trades_table(trades: list[dict], limit: int = 25) -> str:
    rows = list(reversed(trades))[:limit]
    body = []
    for t in rows:
        side_tag = f"<span class='ct-tag ct-tag-{t['side'].lower()}'>{t['side']}</span>"
        if t["status"] == "FILLED":
            res_cls = "ct-tag-win" if t["points"] > 0 else "ct-tag-loss"
            res = f"<span class='ct-tag {res_cls}'>{t['exit_kind']}</span>"
            pts = f"<span class='{'ct-profit' if t['points']>0 else 'ct-loss'}'>{t['points']:+.1f}</span>"
            usd = f"<span class='{'ct-profit' if t['usd']>0 else 'ct-loss'}'>${t['usd']:+,.0f}</span>"
            exit_p = _fmt(t["exit_price"])
        elif t["status"] == "OPEN":
            res = "<span class='ct-tag ct-tag-flat'>OPEN</span>"; pts = usd = exit_p = "—"
        else:
            res = "<span class='ct-tag ct-tag-flat'>EXPIRED</span>"; pts = usd = exit_p = "—"
        dry = " <span class='ct-muted'>(dry)</span>" if t.get("dry_run") else ""
        when = (t.get("fill_at") or t.get("armed_at") or "")[5:16]
        body.append(
            f"<tr><td>{when}{dry}</td><td>{side_tag}</td><td>{t['order_type']}</td>"
            f"<td>{_fmt(t['entry'])}</td><td>{exit_p}</td><td>{res}</td>"
            f"<td>{pts}</td><td>{usd}</td></tr>")
    if not body:
        body = ["<tr><td colspan='8' class='ct-muted' style='text-align:center;padding:18px'>No trades yet</td></tr>"]
    return (f"<div class='ct-card'><div class='ct-h4'>Trade history</div>"
            f"<table class='ct-table'><thead><tr>"
            f"<th>Time</th><th>Side</th><th>Type</th><th>Entry</th><th>Exit</th>"
            f"<th>Result</th><th>Pts</th><th>P&amp;L</th></tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table></div>")


def _assess_block(title: str, a: dict) -> str:
    if not isinstance(a, dict):
        a = {}
    status = (a.get("status") or "none").lower()
    badge = {"ready": "ct-badge-ready", "waiting": "ct-badge-waiting"}.get(status, "ct-badge-none")
    conf = float(a.get("confidence") or 0)
    setup = a.get("setup_type") or "—"
    otype = a.get("order_type") or "—"
    entry = _fmt(a.get("entry_plan")); stop = _fmt(a.get("stop_plan")); tgt = _fmt(a.get("target_plan"))
    rr = a.get("risk_reward")
    rr_s = f"{float(rr):.2f}:1" if isinstance(rr, (int, float)) else "—"
    reason = escape(str(a.get("reasoning") or "")) or "—"
    return (f"<div class='side'><h5>{title} "
            f"<span class='ct-badge {badge}'>{status}</span></h5>"
            f"<div class='ct-muted' style='font-size:11px'>{setup} · {otype}</div>"
            f"<div style='font-family:var(--font-mono);font-size:12px;margin-top:6px'>"
            f"E {entry} · SL {stop} · TP {tgt} · {rr_s}</div>"
            f"<div class='ct-conf-bar'><div style='width:{conf*100:.0f}%'></div></div>"
            f"<div class='ct-reason'>{reason}</div></div>")


def decision_panel(analysis: dict) -> str:
    bias = (analysis.get("overall_bias") or "neutral").upper()
    waiting = escape(str(analysis.get("waiting_for") or ""))
    updated = analysis.get("last_updated", "")
    longa = analysis.get("long_assessment", {})
    shorta = analysis.get("short_assessment", {})
    return (f"<div class='ct-card'><div class='ct-h4'>Latest decision "
            f"<span class='ct-muted' style='text-transform:none;letter-spacing:0'>· {updated}</span></div>"
            f"<div style='margin-bottom:10px'><span class='ct-muted'>Bias</span> "
            f"<b style='font-family:var(--font-display)'>{bias}</b> "
            f"<span class='ct-muted'>· {waiting}</span></div>"
            f"<div class='ct-assess'>{_assess_block('LONG', longa)}{_assess_block('SHORT', shorta)}</div></div>")
