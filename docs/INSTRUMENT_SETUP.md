# Running on a different instrument

After the 2026-06-24 parameterization, switching instruments is **config-only** — no
code edits. The previously-hardcoded values (live FVG gap size, psychological-level
spacing, the "NQ" labels in the prompt) now all read from `config/agent_config.json`.

Run **one instrument at a time**: both NT strategies write the same
`data/HistoricalData.csv` / `data/LiveFeed.csv`. To run two instruments at once you'd
need separate file paths in the strategies **and** a second `main.py` with its own
config pointing at those paths.

## Step 1 — NinjaTrader
1. Add `SecondHistoricalData` + `SecondLifeFeed` (both **Strategies**) to a **60-minute**
   chart of the new instrument; enable both.
2. Set the chart **Days to load ≥ 20** (clears `SecondHistoricalData`'s 150-bar warmup
   gate; under ~7 days it writes nothing — see project memory).
3. Delete the old `data/HistoricalData.csv` so it rewrites clean (the strategy truncates
   on load), then confirm fresh hourly rows appear.

## Step 2 — `config/agent_config.json`
All point-based values are in **price points** and must be rescaled to the instrument.

| Key | What it is | Notes |
|---|---|---|
| `execution.instrument` | OIF contract string | Must match NT exactly, e.g. `"ES 09-26"`, `"GC 08-26"`, `"CL 08-26"` |
| `trading_params.min_gap_size` | min FVG size, points | Drives **live** FVG detection (`FairValueGaps.py`) |
| `levels.psychological_intervals` | round-number spacing (list) | Used by level_detector |
| `levels.agent_psych_interval` | level spacing the **LLM** reasons about, points | Was hardcoded 100 (NQ) |
| `levels.confluence_tolerance` | points | |
| `risk_management.stop_loss_min/default/max` | points | |
| `risk_management.stop_buffer` | points | |
| `risk_management.max_daily_loss` | points | |

### Rough starting points (TUNE before trading live)
Scale roughly with the instrument's typical hourly range; these are ballpark only:

| Instrument | min_gap_size | agent_psych_interval | stop default | 
|---|---|---|---|
| NQ (current) | 5 | 100 | 40 |
| ES | 2 | 25 | 12 |
| GC | 2 | 25 | 15 |
| CL | 0.15 | 1 | 0.40 |
| MNQ | 5 | 100 | 40 (same scale as NQ) |

## Step 3 — restart
Restart `python main.py --mode live`. Startup log should show
`TradingAgent initialized (instrument=<SYM> ...)` and `Loaded N active FVGs`.

## Watch-outs
- **Tick size:** no tick-rounding in code — entry/stop/target come straight from the LLM.
  Instruments with finer ticks (GC 0.1, CL 0.01) may get order rejects if the model emits
  an off-tick price. Verify on the first live (Sim) orders.
- **FVG semantics are FILL/MAGNET** (trade toward the gap), not rejection — same on every
  instrument. See `src/trading_agent.py` prompt section 1.
