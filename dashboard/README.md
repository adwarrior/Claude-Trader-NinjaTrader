# Claude-Trader Monitor

Read-only dashboard for the live bot. Reads the files the bot already writes
(`data/bot_state.json`, `data/market_analysis.json`, `data/HistoricalData.csv`,
`data/LiveFeed.csv`) and reconstructs the closed-trade ledger from the log. It
does **not** touch bot state, so it cannot interfere with the running system.

Styled to match the strategy-platform-v2 design system (dark terminal, amber/teal,
JetBrains Mono for numbers) via custom HTML/CSS through `st.html()` — native
Streamlit widgets are avoided, and auto-refresh uses `st.fragment(run_every=)`
so only data panels re-render (not the whole page).

## Run

From the bot root, in a **Windows** terminal (same machine as the bot):

```
streamlit run dashboard/app.py
```

Requires `streamlit`, `pandas` (already in the bot's environment).

## Panels (Phase 1)

- **Live status** — FLAT / PENDING / IN POSITION, working order, price, LIVE/DRY-RUN.
- **Performance** — net P&L, win rate, best/worst over real (non-dry-run) fills.
- **Latest decision** — bias + long/short assessments with reasoning, from `market_analysis.json`.
- **Trade history** — reconstructed from the log; dry-run trades shown but excluded from P&L.

## Phase 2 — chart

Themed Plotly: hourly candles, EMA21/75/150, FVG zones (bot's exact 3-candle rule,
unfilled solid / filled dimmed), active entry/SL/TP lines + live-price marker.
Refreshes every 15s (hourly bars change slowly).

## Phase 3 — controls

Request/execute design: the dashboard only writes `data/bot_control.json`; the bot
reads it each loop and executes, so there's no two-process race over orders/state.

- **Pause new entries** — bot keeps managing any open trade but arms nothing new.
- **Cancel order** / **Flatten** — confirm-gated popovers; queued as one-shot
  commands the bot actions within ~5s. Both respect `dry_run` (log-only in dry-run).

> Requires the updated `main.py` (control-channel hook) — **restart the bot** once
> after pulling these changes for the controls to take effect.

P&L assumes NQ full-size at $20/point; override with `execution.point_value` in `agent_config.json`.
