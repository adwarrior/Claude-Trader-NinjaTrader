"""
Claude NQ Trading Agent - Main Orchestrator
Coordinates all system components for live trading, backtesting, and monitoring
"""

import argparse
import json
import logging
import sys
import time
import os
import pandas as pd
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

# Import modules
from src.fvg_analyzer import FVGAnalyzer
from src.level_detector import LevelDetector
from src.trading_agent import TradingAgent
from src.memory_manager import MemoryManager
from src.signal_generator import SignalGenerator
from src.backtest_engine import BacktestEngine
from src.market_analysis_manager import MarketAnalysisManager

# Load environment variables
load_dotenv()

# Configure logging
def setup_logging(log_level: str = "INFO", log_file: str = None):
    """Setup logging configuration"""
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'

    # Configure stdout handler with UTF-8 encoding for Windows
    import io
    stdout_handler = logging.StreamHandler(io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace'))
    handlers = [stdout_handler]

    if log_file:
        # log_file may already include a directory (e.g. "logs/trading_agent.log");
        # honour it as-is and just ensure the parent directory exists.
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # File handler with UTF-8 encoding
        handlers.append(logging.FileHandler(log_path, encoding='utf-8', errors='replace'))

    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format=log_format,
        handlers=handlers
    )

logger = logging.getLogger(__name__)


class TradingOrchestrator:
    """Main orchestrator for trading system"""

    def __init__(self, config_path: str = "config/agent_config.json"):
        """
        Initialize Trading Orchestrator

        Args:
            config_path: Path to configuration file
        """
        # Load configuration
        with open(config_path, 'r') as f:
            self.config = json.load(f)

        # Setup logging
        log_level = self.config.get('logging', {}).get('level', 'INFO')
        log_file = self.config.get('logging', {}).get('log_file', 'trading_agent.log')
        setup_logging(log_level, log_file)

        # Initializing silently
        pass

        # Initialize components
        self.fvg_analyzer = FVGAnalyzer(
            min_gap_size=self.config['trading_params']['min_gap_size'],
            max_gap_age=self.config['trading_params']['max_gap_age_bars']
        )

        self.level_detector = LevelDetector(
            level_intervals=self.config['levels']['psychological_intervals']
        )

        self.memory_manager = MemoryManager()

        exec_cfg = self.config.get('execution', {})
        self.signal_generator = SignalGenerator(
            account=exec_cfg.get('account', 'Sim101'),
            instrument=exec_cfg.get('instrument', 'NQ 06-26'),
            quantity=exec_cfg.get('quantity',
                                  self.config['trading_params'].get('position_size', 1)),
            host=exec_cfg.get('host', 'localhost'),
            port=exec_cfg.get('port', 36973),
            dry_run=exec_cfg.get('dry_run', False),
        )
        self.analysis_manager = MarketAnalysisManager()

        # Trading agent (requires LLM API key from the env var named in llm config)
        key_env = self.config.get('llm', {}).get('api_key_env', 'NOUS_API_KEY')
        api_key = os.getenv(key_env) or os.getenv('ANTHROPIC_API_KEY')
        if api_key:
            self.trading_agent = TradingAgent(self.config, api_key=api_key)
        else:
            self.trading_agent = None
            logger.warning(f"No API key found (set {key_env}) - trading agent not initialized")

        # State tracking
        self.daily_trades = 0
        self.daily_pnl = 0.0
        self.consecutive_losses = 0
        self.trading_paused = False

        # Resting-entry state machine (MVP: fills/exits inferred from bars).
        # pending_entry: a resting LIMIT/STOP entry working but not yet filled.
        # in_position:   a setup whose entry was inferred filled; SL/TP are live.
        self.pending_entry = None
        self.in_position = None
        self.max_pending_bars = self.config.get('execution', {}).get('max_pending_bars', 3)

        # Persist the state machine so a restart mid-trade recovers instead of
        # thinking it is flat (OIF is write-only -> no fill read-back from NT).
        self.state_file = Path(self.config.get('execution', {}).get('state_file', 'data/bot_state.json'))
        self._load_state()

        # Dashboard control channel: the monitor only WRITES requests here; the
        # bot is the sole executor (reads each loop, acts, clears) so there is no
        # two-process race over orders or state.
        self.control_file = Path(self.config.get('execution', {}).get('control_file', 'data/bot_control.json'))
        self.paused = False

        # Initialization complete

    def _read_feed_status(self) -> dict:
        """Read FeedStatus.csv, the heartbeat the NinjaTrader SecondHistoricalData
        strategy writes every few seconds regardless of bar flow.

        Returns a dict with keys: connected (bool|None), state (str|None),
        heartbeat_age_sec (float|None), last_bar (str|None). All None when the
        file is absent/unreadable (e.g. an older NT script without the heartbeat),
        in which case callers fall back to the timestamp-only stale guard.
        """
        status = {'connected': None, 'state': None,
                  'heartbeat_age_sec': None, 'last_bar': None}
        try:
            df = pd.read_csv('data/FeedStatus.csv')
            if df.empty:
                return status
            row = df.iloc[-1]
            status['state'] = str(row.get('State', '')) or None
            connected = row.get('Connected')
            if connected is not None and not pd.isna(connected):
                status['connected'] = bool(int(connected))
            hb = pd.to_datetime(row.get('Heartbeat'), errors='coerce')
            if pd.notna(hb):
                status['heartbeat_age_sec'] = (pd.Timestamp.now() - hb).total_seconds()
            status['last_bar'] = str(row.get('LastBar', '')) or None
        except (FileNotFoundError, OSError, ValueError, KeyError):
            pass
        return status

    def _read_control(self) -> None:
        """Honour pause + one-shot commands (cancel_entry / flatten) from the dashboard."""
        if not self.control_file.exists():
            self.paused = False
            return
        try:
            with open(self.control_file, 'r') as f:
                ctrl = json.load(f)
        except (json.JSONDecodeError, OSError):
            return

        self.paused = bool(ctrl.get('paused', False))
        cmd = ctrl.get('command')
        if not cmd:
            return

        if cmd == 'cancel_entry' and self.pending_entry:
            logger.warning("CONTROL: cancel_entry requested from dashboard")
            self.signal_generator.cancel_entry(self.pending_entry)
            self.pending_entry = None
            self._save_state()
        elif cmd == 'flatten':
            logger.warning("CONTROL: flatten requested from dashboard")
            if self.pending_entry:
                self.signal_generator.cancel_entry(self.pending_entry)
                self.pending_entry = None
            self.signal_generator.close_position()
            self.in_position = None
            self._save_state()

        # Clear the one-shot command, keep the paused flag.
        try:
            with open(self.control_file, 'w') as f:
                json.dump({'paused': self.paused, 'command': None}, f, indent=2)
        except OSError as e:
            logger.error(f"Failed to clear control command: {e}")

    def _save_state(self) -> None:
        """Persist pending_entry / in_position to disk (datetimes -> str)."""
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.state_file, 'w') as f:
                json.dump({'pending_entry': self.pending_entry,
                           'in_position': self.in_position}, f, default=str, indent=2)
        except Exception as e:
            logger.error(f"Failed to save bot state to {self.state_file}: {e}")

    def _load_state(self) -> None:
        """Restore pending_entry / in_position from disk on startup."""
        if not self.state_file.exists():
            return
        try:
            with open(self.state_file, 'r') as f:
                state = json.load(f)
            self.pending_entry = state.get('pending_entry')
            self.in_position = state.get('in_position')
            if self.in_position:
                pos = self.in_position
                logger.warning(f"RECOVERED OPEN POSITION from state file: {pos['direction']} "
                               f"entry {pos['entry']} | SL {pos['stop']} | TP {pos['target']}. "
                               f"NT still manages the live OCO; no new entries until it resolves.")
            if self.pending_entry:
                pend = self.pending_entry
                logger.warning(f"RECOVERED PENDING ENTRY from state file: {pend['direction']} "
                               f"{pend['order_type']} @ {pend['entry']} (id={pend.get('order_id')}).")
        except Exception as e:
            logger.error(f"Failed to load bot state from {self.state_file}: {e}")

    def _check_entry_fill(self, pend: dict, bar_high: float, bar_low: float) -> bool:
        """Infer whether a resting entry would have filled within this bar's range."""
        entry = pend['entry']
        if pend['direction'] == 'LONG':
            # LONG LIMIT rests below market (fills on dip); LONG STOP rests above (fills on break up)
            return bar_low <= entry if pend['order_type'] == 'LIMIT' else bar_high >= entry
        else:  # SHORT
            # SHORT LIMIT rests above market (fills on pop); SHORT STOP rests below (fills on break down)
            return bar_high >= entry if pend['order_type'] == 'LIMIT' else bar_low <= entry

    def _check_position_exit(self, pos: dict, bar_high: float, bar_low: float):
        """Infer SL/TP hit within the bar. Returns 'STOP', 'TARGET', or None.
        If both are touched in one bar, assume STOP first (conservative)."""
        if pos['direction'] == 'LONG':
            hit_stop = bar_low <= pos['stop']
            hit_target = bar_high >= pos['target']
        else:  # SHORT
            hit_stop = bar_high >= pos['stop']
            hit_target = bar_low <= pos['target']
        if hit_stop:
            return 'STOP'
        if hit_target:
            return 'TARGET'
        return None

    def _status_banner(self, current_price=None) -> str:
        """One-line at-a-glance state line shown above the detailed display."""
        if self.in_position:
            p = self.in_position
            state = (f"IN POSITION {p['direction']} @ {p['entry']:.2f} "
                     f"| SL {p['stop']:.2f} TP {p['target']:.2f}")
        elif self.pending_entry:
            p = self.pending_entry
            state = (f"PENDING {p['direction']} {p['order_type']} @ {p['entry']:.2f} "
                     f"| SL {p['stop']:.2f} TP {p['target']:.2f} "
                     f"| age {p.get('bars_alive', 0)}/{self.max_pending_bars} bars")
        else:
            state = "FLAT - no working order"
        px = f"{current_price:.2f}" if current_price is not None else "n/a"
        mode = "DRY-RUN" if self.signal_generator.dry_run else "LIVE"
        return (f"[{mode}] {state} | px {px} | trades today {self.daily_trades}")

    def check_risk_limits(self) -> tuple[bool, str]:
        """
        Check if risk management limits allow trading

        Returns:
            Tuple of (can_trade, reason)
        """
        max_daily_trades = self.config['risk_management']['max_daily_trades']
        max_daily_loss = self.config['risk_management']['max_daily_loss']
        max_consecutive_losses = self.config['risk_management']['max_consecutive_losses']

        if self.trading_paused:
            return False, "Trading is paused (manual intervention required)"

        if self.daily_trades >= max_daily_trades:
            return False, f"Daily trade limit reached ({max_daily_trades})"

        if abs(self.daily_pnl) >= max_daily_loss and self.daily_pnl < 0:
            return False, f"Daily loss limit reached ({max_daily_loss} points)"

        if self.consecutive_losses >= max_consecutive_losses:
            return False, f"Consecutive loss limit reached ({max_consecutive_losses})"

        return True, ""

    def run_live_mode(self):
        """Run in live trading mode"""
        # Starting live mode silently

        if not self.trading_agent:
            logger.error("Trading agent not initialized - API key required")
            return

        # Import FairValueGaps display to access its state
        import sys
        import pandas as pd
        import os
        sys.path.insert(0, str(Path.cwd()))
        from FairValueGaps import FVGDisplay

        # Create FVG display instance (but don't run its main loop)
        fvg_display = FVGDisplay(
            min_gap_size=self.config['trading_params']['min_gap_size']
        )

        # Load historical FVGs
        fvg_display.load_historical_fvgs()

        logger.info(f"Loaded {len(fvg_display.active_fvgs)} active FVGs")
        logger.info("="*60)

        # Track last processed bar and result
        last_bar_time = None
        last_result = None

        # Stale-feed guard: HistoricalData.csv should advance once per bar interval.
        # If it stops (NT SecondHistoricalData blocked by its 150-bar warmup gate,
        # chart closed, wrong data folder, etc.) the loop would otherwise silently
        # replay the last setup forever and look like it "found one setup and stopped".
        # Warn loudly and stop presenting the stale decision as if it were live.
        stale_feed_minutes = self.config.get('monitoring', {}).get('stale_feed_minutes', 90)
        last_new_bar_wallclock = time.time()
        last_stale_warning_wallclock = 0.0

        try:
            while True:
                # Honour dashboard pause / one-shot commands every iteration.
                self._read_control()

                # Reload historical data to check for updates
                historical_df = pd.read_csv('data/HistoricalData.csv')
                # NT writes ISO timestamps (2026-06-11 20:37:00) while older
                # recorded rows use US format (06/11/2026 20:37:00); 'mixed'
                # infers each row so both parse.
                historical_df['DateTime'] = pd.to_datetime(historical_df['DateTime'], format='mixed')

                # Get latest bar timestamp
                current_bar_time = historical_df.iloc[-1]['DateTime']

                # Check if new bar arrived
                if current_bar_time != last_bar_time:
                    # NEW BAR DETECTED - Run full analysis
                    logger.info(f"\n{'='*60}")
                    logger.info(f"NEW BAR: {current_bar_time}")
                    logger.info(f"{'='*60}")

                    # Update last processed time
                    last_bar_time = current_bar_time
                    last_new_bar_wallclock = time.time()

                    # Check for new hourly bars
                    if fvg_display.check_historical_updated():
                        fvg_display.process_historical_bars()

                    # Get current price
                    current_price = fvg_display.read_current_price()

                    if current_price is None:
                        logger.warning("No current price available")
                        time.sleep(5)
                        continue

                    # Check live FVG fills
                    fvg_display.check_live_fvg_fills(current_price)

                    # --- Resting-entry lifecycle (MVP: infer from this bar's range) ---
                    bar_high = float(historical_df.iloc[-1]['High'])
                    bar_low = float(historical_df.iloc[-1]['Low'])

                    if self.in_position:
                        pos = self.in_position
                        # Never infer an exit from the entry bar itself: the SL/TP
                        # bracket only exists in NT from that bar's CLOSE, so intrabar
                        # excursions there predate the orders. A restart re-processing
                        # the entry bar once inferred a phantom stop-out this way.
                        if str(current_bar_time) == str(pos.get('entry_bar_time')):
                            logger.info("Exit check skipped: still on entry bar "
                                        f"({current_bar_time}) — bracket was placed at its close")
                        else:
                            exit_kind = self._check_position_exit(pos, bar_high, bar_low)
                            if exit_kind:
                                logger.info(f"POSITION EXIT inferred ({exit_kind}): {pos['direction']} "
                                            f"entry {pos['entry']:.2f} | SL {pos['stop']:.2f} | TP {pos['target']:.2f}")
                                self.in_position = None
                                self._save_state()
                    elif self.pending_entry:
                        pend = self.pending_entry
                        if self._check_entry_fill(pend, bar_high, bar_low):
                            logger.info(f"ENTRY FILL inferred: {pend['direction']} {pend['order_type']} "
                                        f"@ {pend['entry']:.2f} -> placing SL/TP bracket")
                            pend['entry_bar_time'] = str(current_bar_time)
                            self.signal_generator.place_exits(pend)
                            self.in_position = pend
                            self.pending_entry = None
                            self.daily_trades += 1
                            self._save_state()
                        else:
                            pend['bars_alive'] += 1
                            if pend['bars_alive'] >= self.max_pending_bars:
                                logger.info(f"RESTING ENTRY EXPIRED after {pend['bars_alive']} bars "
                                            f"(unfilled @ {pend['entry']:.2f}) - cancelling")
                                self.signal_generator.cancel_entry(pend)
                                self.pending_entry = None
                            self._save_state()

                    # Get active FVGs
                    active_fvgs = [fvg for fvg in fvg_display.active_fvgs if not fvg.get('filled', False)]

                    # Debug logging
                    total_fvgs = len(fvg_display.active_fvgs)
                    unfilled_fvgs = len(active_fvgs)
                    bullish_count = len([f for f in active_fvgs if f['type'] == 'bullish'])
                    bearish_count = len([f for f in active_fvgs if f['type'] == 'bearish'])

                    logger.info(f"FVG Status: Total={total_fvgs}, Unfilled={unfilled_fvgs} (Bullish={bullish_count}, Bearish={bearish_count})")

                    if active_fvgs:
                        # Show details of each FVG
                        logger.info("Active FVGs:")
                        for i, fvg in enumerate(active_fvgs[:5], 1):  # Show first 5
                            logger.info(f"  {i}. {fvg['type'].upper()}: {fvg['bottom']:.2f}-{fvg['top']:.2f} | "
                                      f"Current Price: {current_price:.2f} | "
                                      f"Relative: {'ABOVE' if fvg['bottom'] > current_price else 'BELOW' if fvg['top'] < current_price else 'AT'}")

                    if not active_fvgs:
                        logger.info("No active FVGs - waiting...")
                        time.sleep(5)
                        continue

                    # Analyze market context
                    fvg_context = self.fvg_analyzer.analyze_market_context(current_price, active_fvgs)

                    # Debug: Show filtering results
                    logger.info(f"After filtering - Nearest Bullish: {fvg_context['nearest_bullish_fvg'] is not None}, "
                              f"Nearest Bearish: {fvg_context['nearest_bearish_fvg'] is not None}")

                    # Get latest bar from historical data for EMA/Stochastic values
                    current_bar = historical_df.iloc[-1]

                    # Extract market data (EMA and Stochastic indicators)
                    market_data = {
                        'ema21': current_bar.get('EMA21', 0),
                        'ema75': current_bar.get('EMA75', 0),
                        'ema150': current_bar.get('EMA150', 0),
                        'stochastic': current_bar.get('StochD', 50)
                    }

                    # Check risk limits
                    can_trade, reason = self.check_risk_limits()
                    if not can_trade:
                        logger.warning(f"Trading blocked: {reason}")
                        time.sleep(60)
                        continue

                    # Get memory context
                    memory_context = self.memory_manager.get_memory_context()

                    # Get previous analysis for incremental updates
                    previous_analysis = self.analysis_manager.format_previous_analysis_for_prompt()

                    # Analyze with Claude (only on new bar)
                    try:
                        result = self.trading_agent.analyze_setup(
                            fvg_context,
                            market_data,
                            memory_context,
                            previous_analysis
                        )
                        last_result = result

                        # Check if we have a tradeable decision
                        if result['success']:
                            decision_data = result['decision']

                            # Save updated analysis state
                            if 'long_assessment' in decision_data and 'short_assessment' in decision_data:
                                # Build analysis update from decision
                                analysis_update = {
                                    'current_bar_index': decision_data.get('current_bar_index', 0),
                                    'overall_bias': decision_data.get('overall_bias', 'neutral'),
                                    'waiting_for': decision_data.get('waiting_for', 'Analyzing market'),
                                    'long_assessment': decision_data['long_assessment'],
                                    'short_assessment': decision_data['short_assessment'],
                                    'bars_since_last_update': 0
                                }
                                self.analysis_manager.update_analysis(analysis_update)
                                logger.info(f"Analysis state saved: {decision_data.get('waiting_for', 'N/A')}")

                            primary = decision_data['primary_decision']

                            if primary != 'NONE':
                                # Only arm a new entry when flat, with nothing working, and not paused.
                                if self.paused:
                                    logger.info(f"NO NEW ENTRY: PAUSED by dashboard - skipping {primary} setup")
                                elif self.in_position or self.pending_entry:
                                    state = 'in position' if self.in_position else 'resting entry working'
                                    logger.info(f"NO NEW ENTRY: {state} - skipping {primary} setup")
                                else:
                                    # Get the chosen setup
                                    chosen_setup = decision_data['long_setup'] if primary == 'LONG' else decision_data['short_setup']

                                    # Build signal format for the signal generator
                                    signal = {
                                        'decision': primary,
                                        'order_type': chosen_setup.get('order_type', 'LIMIT'),
                                        'entry': chosen_setup['entry'],
                                        'stop': chosen_setup['stop'],
                                        'target': chosen_setup['target'],
                                        'risk_reward': chosen_setup['risk_reward'],
                                        'confidence': chosen_setup['confidence'],
                                        'reasoning': decision_data['overall_reasoning'],
                                        'setup_type': chosen_setup.get('setup_type', 'fvg_only')
                                    }

                                    logger.info(f"ARMING RESTING ENTRY: {primary} {signal['order_type']} @ {signal['entry']:.0f}")
                                    logger.info(f"R:R {signal['risk_reward']:.2f}:1 | Confidence: {signal['confidence']:.2f}")

                                    try:
                                        pending = self.signal_generator.place_entry(signal)
                                        if pending:
                                            pending['bar_time'] = current_bar_time
                                            pending['bars_alive'] = 0
                                            self.pending_entry = pending
                                            self._save_state()
                                            # Mark trade as executed in analysis manager
                                            self.analysis_manager.mark_trade_executed(primary)
                                            logger.info(f"RESTING ENTRY ARMED: {primary} order working at {signal['entry']:.2f}")
                                        else:
                                            logger.warning("ENTRY PLACEMENT FAILED: validation or bridge error")
                                    except Exception as e:
                                        logger.error(f"ERROR PLACING ENTRY: {e}")
                                        import traceback
                                        logger.error(traceback.format_exc())
                            else:
                                logger.info("NO TRADE: Primary decision is NONE")
                        else:
                            logger.error(f"VALIDATION FAILED: {result.get('validation_error', 'Unknown error')}")
                            logger.error(f"Full result: {result}")

                    except Exception as e:
                        logger.error(f"ERROR IN ANALYSIS: {e}")
                        import traceback
                        logger.error(traceback.format_exc())
                        # Create error result so display doesn't crash
                        last_result = {
                            'success': False,
                            'error': str(e),
                            'decision': {}
                        }

                    # Clear screen and show response
                    os.system('cls' if os.name == 'nt' else 'clear')
                    print(self._status_banner(current_price))
                    print("-" * 60)
                    print(self.trading_agent.format_decision_display(result, current_price))
                    print("\nWaiting for next bar")

                    # Brief pause to show result
                    time.sleep(2)

                else:
                    # WAITING FOR NEW BAR - Show live updates
                    current_price = fvg_display.read_current_price()

                    # Stale-feed detection: minutes since HistoricalData.csv last advanced.
                    stale_minutes = (time.time() - last_new_bar_wallclock) / 60.0
                    feed_stale = stale_minutes >= stale_feed_minutes

                    # Clear screen
                    os.system('cls' if os.name == 'nt' else 'clear')
                    print(self._status_banner(current_price))
                    print("-" * 60)

                    # Pull the NT heartbeat so we can distinguish a dropped feed
                    # from a merely quiet market, and notice an auto-reconnect.
                    feed_status = self._read_feed_status()
                    connected = feed_status['connected']
                    hb_age = feed_status['heartbeat_age_sec']
                    # Heartbeat present but old => the NT strategy itself stopped
                    # (chart closed / NT crashed), which is worse than a feed drop.
                    heartbeat_dead = hb_age is not None and hb_age > 120

                    if feed_stale:
                        # Build a cause string from the heartbeat when available.
                        if connected is False:
                            cause = ("NinjaTrader data feed is DISCONNECTED "
                                     "(NT auto-reconnect in progress)")
                        elif heartbeat_dead:
                            cause = (f"NT heartbeat is {hb_age:.0f}s old - the "
                                     f"SecondHistoricalData strategy/chart is not running")
                        elif connected is True:
                            cause = ("feed CONNECTED but no new bar - quiet market or "
                                     "150-bar warmup / Days-to-load / data path issue")
                        else:
                            cause = ("no heartbeat file - check the SecondHistoricalData "
                                     "strategy (150-bar warmup gate / Days-to-load / data path)")

                        # Throttle the WARNING log to once/min so it's loud in the log
                        # file without spamming on every 5s refresh.
                        if time.time() - last_stale_warning_wallclock >= 60:
                            logger.warning(
                                f"STALE FEED: HistoricalData.csv has not advanced for "
                                f"{stale_minutes:.0f} min (last bar {last_bar_time}). "
                                f"Analysis is PAUSED - {cause}."
                            )
                            last_stale_warning_wallclock = time.time()

                        # Do NOT replay the old decision as if it were live - show a
                        # stale banner so a frozen feed can't masquerade as one setup.
                        print("=" * 60)
                        print("  /!\\  STALE DATA FEED - analysis paused")
                        print(f"  HistoricalData.csv last advanced {stale_minutes:.0f} min ago")
                        print(f"  Last bar: {last_bar_time}")
                        if connected is not None:
                            hb_txt = f"{hb_age:.0f}s ago" if hb_age is not None else "n/a"
                            print(f"  NT feed: {'CONNECTED' if connected else 'DISCONNECTED'} "
                                  f"(heartbeat {hb_txt})")
                        print(f"  Cause: {cause}")
                        if current_price is not None:
                            print(f"  Live price (LiveFeed.csv): {current_price:.2f}")
                        print("  NT auto-reconnects the feed; analysis resumes on the next new bar.")
                        print("=" * 60)
                    elif connected is True and last_stale_warning_wallclock > 0:
                        # We were stale and the feed has come back with fresh bars.
                        logger.info("FEED RECOVERED: new bars flowing again, analysis resumed.")
                        last_stale_warning_wallclock = 0.0
                    else:
                        # Show last decision with current price
                        if last_result:
                            print(self.trading_agent.format_decision_display(last_result, current_price))

                        # Static waiting message
                        print("\nWaiting for next bar")

                    # Wait 5 seconds before refreshing
                    time.sleep(5)

        except KeyboardInterrupt:
            logger.info("\nLive trading stopped by user")
        except Exception as e:
            logger.error(f"Error in live trading: {e}")
            import traceback
            logger.error(traceback.format_exc())

    def run_backtest_mode(self, days: int = 30, output_file: str = "backtest_results.json"):
        """
        Run in backtest mode

        Args:
            days: Number of days to backtest
            output_file: Output file for results
        """
        logger.info(f"Starting BACKTEST mode ({days} days)")

        key_env = self.config.get('llm', {}).get('api_key_env', 'NOUS_API_KEY')
        api_key = os.getenv(key_env) or os.getenv('ANTHROPIC_API_KEY')
        use_claude = api_key is not None

        if not use_claude:
            logger.warning(f"No API key (set {key_env}) - running backtest with simple logic")

        engine = BacktestEngine(self.config)
        results = engine.run_backtest(days=days, use_claude=use_claude, api_key=api_key)

        # Print summary
        logger.info("="*60)
        logger.info("BACKTEST RESULTS")
        logger.info("="*60)
        logger.info(f"Period: {results['backtest_period']}")
        logger.info(f"Total Bars: {results['total_bars']}")
        logger.info(f"Total Trades: {results['total_trades']}")
        logger.info(f"Wins: {results['wins']} | Losses: {results['losses']} | Breakeven: {results['breakeven']}")
        logger.info(f"Win Rate: {results['win_rate']:.1%}")
        logger.info(f"Total P&L: {results['total_pnl']:+.2f} points")
        logger.info(f"Average P&L: {results['avg_pnl']:+.2f} points")
        logger.info(f"Max Win: {results['max_win']:+.2f} points")
        logger.info(f"Max Loss: {results['max_loss']:+.2f} points")
        logger.info(f"Average Bars Held: {results['avg_bars_held']:.1f}")

        if results.get('by_setup_type'):
            logger.info("\nBy Setup Type:")
            for setup_type, stats in results['by_setup_type'].items():
                logger.info(f"  {setup_type}: {stats['trades']} trades, {stats['win_rate']:.1%} win rate, "
                          f"{stats['avg_pnl']:+.2f}pts avg")

        logger.info("="*60)

        # Export results
        engine.export_results(results, output_file)

    def run_monitor_mode(self):
        """Run in monitoring/dashboard mode"""
        logger.info("Starting MONITOR mode")

        # Display performance summary
        print("\n" + "="*60)
        print(self.memory_manager.get_performance_summary())
        print("="*60)

        # Display current signals
        recent_signals = self.signal_generator.get_recent_signals(10)
        if recent_signals:
            print("\nRECENT SIGNALS:")
            print("-"*60)
            for signal in recent_signals:
                print(f"{signal['DateTime']} | {signal['Direction']:<5} | "
                      f"Entry: {signal['Entry_Price']:<8} | "
                      f"Stop: {signal['Stop_Loss']:<8} | "
                      f"Target: {signal['Target']}")
            print("="*60)

        print(f"\nSignals today: {self.signal_generator.count_signals_today()}")


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description='Claude NQ Trading Agent')
    parser.add_argument('--mode', choices=['live', 'backtest', 'monitor'],
                       default='monitor', help='Operating mode')
    parser.add_argument('--days', type=int, default=30,
                       help='Number of days for backtest (default: 30)')
    parser.add_argument('--config', type=str, default='config/agent_config.json',
                       help='Path to configuration file')
    parser.add_argument('--output', type=str, default='backtest_results.json',
                       help='Output file for backtest results')

    args = parser.parse_args()

    # Initialize orchestrator
    orchestrator = TradingOrchestrator(config_path=args.config)

    # Run in selected mode
    if args.mode == 'live':
        orchestrator.run_live_mode()
    elif args.mode == 'backtest':
        orchestrator.run_backtest_mode(days=args.days, output_file=args.output)
    elif args.mode == 'monitor':
        orchestrator.run_monitor_mode()


if __name__ == "__main__":
    main()
