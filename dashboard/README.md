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

## Roadmap

- **Phase 2** — price + levels chart (hourly candles, EMAs, FVGs, active entry/SL/TP) via themed Plotly.
- **Phase 3** — controls: pause/resume, cancel working order, flatten (confirm-gated; respects `dry_run`).

P&L assumes NQ full-size at $20/point; override with `execution.point_value` in `agent_config.json`.
