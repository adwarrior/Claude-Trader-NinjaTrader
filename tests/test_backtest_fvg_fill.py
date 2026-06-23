"""
Regression test for the backtest FVG-fill bug.

Bug: BacktestEngine.update_fvg_status inverted the fill condition relative to
the live FairValueGaps implementation, so every FVG was marked 'filled' on the
bar it formed. Result: get_active_fvgs always returned [], the LLM was never
queried, and the backtest took 0 trades regardless of data window.

Correct (live) semantics (FairValueGaps.is_fvg_filled / check_live_fvg_fills):
  - Bullish FVG fills when price returns DOWN to the bottom:  Low  <= bottom
  - Bearish FVG fills when price returns UP   to the top:     High >= top
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import json
import pandas as pd
from src.backtest_engine import BacktestEngine


def _engine():
    cfg = json.load(open(Path(__file__).parent.parent / 'config' / 'agent_config.json'))
    return BacktestEngine(cfg)


def test_bullish_fvg_not_filled_on_formation():
    """A freshly formed bullish FVG must NOT be filled by its own formation bar."""
    eng = _engine()
    # 3 bars forming a clear bullish gap: candle3.Low (110) > candle1.High (100)
    df = pd.DataFrame([
        {'DateTime': '2025-01-01 00:00:00', 'Open': 95,  'High': 100, 'Low': 90,  'Close': 99,  'EMA21': 95, 'EMA75': 95, 'EMA150': 95, 'StochD': 50},
        {'DateTime': '2025-01-01 01:00:00', 'Open': 100, 'High': 108, 'Low': 99,  'Close': 107, 'EMA21': 95, 'EMA75': 95, 'EMA150': 95, 'StochD': 50},
        {'DateTime': '2025-01-01 02:00:00', 'Open': 112, 'High': 120, 'Low': 110, 'Close': 118, 'EMA21': 95, 'EMA75': 95, 'EMA150': 95, 'StochD': 50},
    ])
    fvgs = eng.detect_fvgs_historical(df)
    assert len(fvgs) == 1 and fvgs[0]['type'] == 'bullish', f"expected 1 bullish FVG, got {fvgs}"

    # Replay status updates through the formation bar (index 2)
    for i in range(len(df)):
        eng.update_fvg_status(fvgs, df.iloc[i], i)

    assert fvgs[0]['filled'] is False, "bullish FVG self-filled on its formation bar (BUG)"
    assert len(eng.get_active_fvgs(fvgs, 2)) == 1, "FVG should still be active after forming"


def test_bullish_fvg_fills_only_when_price_returns_to_bottom():
    """Bullish FVG fills when a later bar's Low drops to/below the gap bottom (100)."""
    eng = _engine()
    df = pd.DataFrame([
        {'DateTime': '2025-01-01 00:00:00', 'Open': 95,  'High': 100, 'Low': 90,  'Close': 99,  'EMA21': 95, 'EMA75': 95, 'EMA150': 95, 'StochD': 50},
        {'DateTime': '2025-01-01 01:00:00', 'Open': 100, 'High': 108, 'Low': 99,  'Close': 107, 'EMA21': 95, 'EMA75': 95, 'EMA150': 95, 'StochD': 50},
        {'DateTime': '2025-01-01 02:00:00', 'Open': 112, 'High': 120, 'Low': 110, 'Close': 118, 'EMA21': 95, 'EMA75': 95, 'EMA150': 95, 'StochD': 50},
        # later bar that dips back into the gap bottom (Low 98 <= bottom 100)
        {'DateTime': '2025-01-01 03:00:00', 'Open': 112, 'High': 115, 'Low': 98,  'Close': 101, 'EMA21': 95, 'EMA75': 95, 'EMA150': 95, 'StochD': 50},
    ])
    fvgs = eng.detect_fvgs_historical(df)
    for i in range(3):
        eng.update_fvg_status(fvgs, df.iloc[i], i)
    assert fvgs[0]['filled'] is False, "should still be unfilled before the retrace"

    eng.update_fvg_status(fvgs, df.iloc[3], 3)
    assert fvgs[0]['filled'] is True, "bullish FVG should fill when price returns to bottom"


def test_real_window_has_active_fvgs():
    """On real data, at least some bars must have active FVGs (else LLM never runs)."""
    eng = _engine()
    df = pd.read_csv(Path(__file__).parent.parent / 'data' / 'HistoricalData.csv')
    df['DateTime'] = pd.to_datetime(df['DateTime'])
    df = df.tail(300).reset_index(drop=True)
    fvgs = eng.detect_fvgs_historical(df)
    bars_with_active = 0
    for i in range(3, len(df)):
        eng.update_fvg_status(fvgs, df.iloc[i], i)
        if eng.get_active_fvgs(fvgs, i):
            bars_with_active += 1
    assert bars_with_active > 0, "no bar had an active FVG — LLM would never be queried (BUG)"


def test_simple_logic_levels_bracket_entry_correctly():
    """Every simple-logic trade must have stop/target on the correct side of entry."""
    eng = _engine()
    res = eng.run_backtest(days=30, use_claude=False, api_key=None)
    assert res['total_trades'] > 0, "simple-logic path produced no trades"
    for t in res['trades']:
        if t['direction'] == 'LONG':
            assert t['stop'] < t['entry'] < t['target'], \
                f"LONG levels wrong: stop {t['stop']} entry {t['entry']} target {t['target']}"
        else:  # SHORT
            assert t['target'] < t['entry'] < t['stop'], \
                f"SHORT levels wrong: stop {t['stop']} entry {t['entry']} target {t['target']}"
        # a 'target_hit' exit must be a win, a 'stop_loss' exit must be a loss
        if t['exit_reason'] == 'target_hit':
            assert t['profit_loss'] > 0, f"target_hit but P/L {t['profit_loss']}"
        elif t['exit_reason'] == 'stop_loss':
            assert t['profit_loss'] < 0, f"stop_loss but P/L {t['profit_loss']}"


def test_backtest_survives_llm_failures():
    """A failing/erroring LLM on some bars must not abort the backtest."""
    eng = _engine()

    class FlakyAgent:
        """Alternates between raising, returning an error dict, and NONE."""
        def __init__(self): self.n = 0
        def analyze_setup(self, *a, **k):
            self.n += 1
            if self.n % 3 == 0:
                raise RuntimeError("simulated API outage")
            if self.n % 3 == 1:
                return {'success': False, 'error': 'parse failed'}  # no 'decision' key
            return {'success': True, 'decision': {'primary_decision': 'NONE'}}

    import src.backtest_engine as be
    orig = be.TradingAgent
    be.TradingAgent = lambda *a, **k: FlakyAgent()
    try:
        res = eng.run_backtest(days=10, use_claude=True, api_key='dummy')
    finally:
        be.TradingAgent = orig
    # Should complete and return a valid stats dict (0 trades is fine here)
    assert 'total_trades' in res and res['total_trades'] == 0


if __name__ == '__main__':
    import traceback
    passed = failed = 0
    for name, fn in list(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn(); print(f"PASS {name}"); passed += 1
            except Exception:
                print(f"FAIL {name}"); traceback.print_exc(); failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
