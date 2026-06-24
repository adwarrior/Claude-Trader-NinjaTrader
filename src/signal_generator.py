"""
Signal Generator Module
Submits trade signals directly to NinjaTrader 8 via the ATI socket bridge.

No CSV files: signals are sent straight to NT8 over the Automated Trading
Interface (see nt_rest_bridge.NTBridge). Signal history is kept in memory for
the dashboard/summary helpers.

The decision dict contract is unchanged and LLM-agnostic — any model that
produces {decision, entry, stop, target} works here.
"""

import logging
import sys
import uuid
from typing import Dict, Any, Optional
from datetime import datetime
from pathlib import Path

# nt_rest_bridge.py lives at the project root, one level above src/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nt_rest_bridge import NTBridge, NTBridgeError

logger = logging.getLogger(__name__)


class SignalGenerator:
    """Generates trade signals and submits them to NinjaTrader via ATI."""

    def __init__(
        self,
        account: str = "Sim101",
        instrument: str = "NQ 06-26",
        quantity: int = 1,
        host: str = "localhost",
        port: int = 36973,
        dry_run: bool = False,
        bridge: Optional[NTBridge] = None,
    ):
        """
        Initialize Signal Generator.

        Args:
            account:    NT8 account name (e.g. "Sim101")
            instrument: NT8 instrument string (e.g. "NQ 06-26")
            quantity:   Contracts per trade
            host/port:  ATI socket endpoint (NT8 default port is 36973)
            dry_run:    If True, validate and log but do NOT submit orders
            bridge:     Optional pre-built NTBridge (mainly for testing)
        """
        self.account = account
        self.instrument = instrument
        self.quantity = quantity
        self.dry_run = dry_run

        self.bridge = bridge or NTBridge(account=account, host=host, port=port)

        # In-memory signal history (replaces the old CSV log)
        self._history: list[Dict[str, Any]] = []

        logger.info(
            f"SignalGenerator initialized (account={account}, "
            f"instrument={instrument}, qty={quantity}, dry_run={dry_run})"
        )

    def validate_decision(self, decision: Dict[str, Any]) -> tuple[bool, str]:
        """
        Validate decision data before generating signal

        Args:
            decision: Decision dictionary from the trading agent

        Returns:
            Tuple of (is_valid, error_message)
        """
        # Check decision type
        if decision.get('decision') not in ['LONG', 'SHORT']:
            return False, f"Invalid decision type: {decision.get('decision')}"

        # Check required fields
        required_fields = ['entry', 'stop', 'target']
        for field in required_fields:
            if field not in decision:
                return False, f"Missing required field: {field}"
            if not isinstance(decision[field], (int, float)):
                return False, f"Invalid {field} value: {decision[field]}"

        # Validate price relationships
        entry = decision['entry']
        stop = decision['stop']
        target = decision['target']

        if decision['decision'] == 'LONG':
            if stop >= entry:
                return False, f"LONG stop ({stop}) must be below entry ({entry})"
            if target <= entry:
                return False, f"LONG target ({target}) must be above entry ({entry})"

        elif decision['decision'] == 'SHORT':
            if stop <= entry:
                return False, f"SHORT stop ({stop}) must be above entry ({entry})"
            if target >= entry:
                return False, f"SHORT target ({target}) must be below entry ({entry})"

        # Validate 5pt buffer was applied correctly (if raw_target exists)
        if 'raw_target' in decision and decision['raw_target'] is not None:
            raw_target = decision['raw_target']

            if decision['decision'] == 'LONG':
                # LONG: Final target should be 5pts BELOW raw target
                expected_target = raw_target - 5
                if abs(target - expected_target) > 0.1:  # Allow 0.1pt tolerance
                    return False, f"LONG buffer error: target ({target}) should be raw_target - 5 ({expected_target})"

            elif decision['decision'] == 'SHORT':
                # SHORT: Final target should be 5pts ABOVE raw target
                expected_target = raw_target + 5
                if abs(target - expected_target) > 0.1:  # Allow 0.1pt tolerance
                    return False, f"SHORT buffer error: target ({target}) should be raw_target + 5 ({expected_target})"

        # Calculate and validate R:R ratio with final target (after buffer)
        risk = abs(entry - stop)
        reward = abs(target - entry)

        if risk == 0:
            return False, "Risk cannot be zero (entry == stop)"

        rr_ratio = reward / risk

        # Minimum R:R requirement: 1.3:1
        min_rr = 1.3
        if rr_ratio < min_rr:
            return False, f"R:R too low: {rr_ratio:.2f}:1 (minimum {min_rr}:1 required after 5pt buffer)"

        logger.info(f"Validation passed: {decision['decision']} | R:R = {rr_ratio:.2f}:1 | "
                    f"Risk: {risk:.2f}pts | Reward: {reward:.2f}pts")

        return True, ""

    def generate_signal(self, decision: Dict[str, Any], timestamp: datetime = None) -> bool:
        """
        Validate a decision and submit it to NinjaTrader as a bracket order.

        Args:
            decision:  Decision dictionary from the trading agent
            timestamp: Optional timestamp (defaults to now)

        Returns:
            True if the order was submitted successfully (or accepted in dry_run)
        """
        # Validate decision
        is_valid, error_msg = self.validate_decision(decision)
        if not is_valid:
            logger.error(f"Signal validation failed: {error_msg}")
            return False

        if timestamp is None:
            timestamp = datetime.now()

        direction = decision['decision']          # "LONG" / "SHORT"
        entry = decision['entry']
        stop = decision['stop']
        target = decision['target']

        if self.dry_run:
            logger.info(
                f"[DRY RUN] Would submit {direction} {self.quantity}x {self.instrument} "
                f"@ {entry:.2f} | SL {stop:.2f} | TP {target:.2f}"
            )
            self._record(decision, timestamp, status="dry_run")
            return True

        # Submit market entry + SL stop + TP limit via ATI
        try:
            responses = self.bridge.bracket_order(
                instrument=self.instrument,
                direction=direction,
                quantity=self.quantity,
                stop_loss=stop,
                take_profit=target,
            )

            logger.info(f"Signal submitted to NT8: {direction} @ {entry:.2f}")
            logger.info(f"  Stop: {stop:.2f} | Target: {target:.2f} | Qty: {self.quantity}")
            logger.info(f"  ATI responses: {responses}")

            self._record(decision, timestamp, status="submitted", responses=responses)
            return True

        except NTBridgeError as e:
            logger.error(f"Error submitting signal to NT8 ATI: {e}")
            return False

    def place_entry(self, decision: Dict[str, Any],
                    timestamp: datetime = None) -> Optional[Dict[str, Any]]:
        """
        Validate a decision and place a RESTING entry order (LIMIT or STOP) at
        the planned price instead of a market order. Returns a pending-order
        descriptor that the main loop tracks until fill, or None on failure.

        The SL/TP are NOT sent here - they are placed via place_exits() once the
        entry is inferred to have filled (see main loop fill-inference).
        """
        is_valid, error_msg = self.validate_decision(decision)
        if not is_valid:
            logger.error(f"Signal validation failed: {error_msg}")
            return None

        if timestamp is None:
            timestamp = datetime.now()

        direction = decision['decision']          # "LONG" / "SHORT"
        entry = decision['entry']
        stop = decision['stop']
        target = decision['target']
        order_type = decision.get('order_type', 'LIMIT')

        pending = {
            'direction': direction,
            'order_type': order_type,
            'entry': entry,
            'stop': stop,
            'target': target,
            'instrument': self.instrument,
            'quantity': self.quantity,
            'placed_at': timestamp,
            'order_id': None,
        }

        if self.dry_run:
            logger.info(
                f"[DRY RUN] Would place resting {order_type} entry: {direction} "
                f"{self.quantity}x {self.instrument} @ {entry:.2f} | SL {stop:.2f} | TP {target:.2f}"
            )
            pending['order_id'] = f"dry-{uuid.uuid4().hex[:12]}"
            self._record(decision, timestamp, status="dry_run_entry")
            return pending

        try:
            order_id = self.bridge.entry_order(
                instrument=self.instrument,
                direction=direction,
                quantity=self.quantity,
                order_type=order_type,
                price=entry,
            )
            pending['order_id'] = order_id
            logger.info(
                f"Resting {order_type} entry placed: {direction} @ {entry:.2f} "
                f"(id={order_id}) | SL {stop:.2f} | TP {target:.2f}"
            )
            self._record(decision, timestamp, status="entry_placed", responses=order_id)
            return pending
        except NTBridgeError as e:
            logger.error(f"Error placing resting entry to NT8 ATI: {e}")
            return None

    def place_exits(self, pending: Dict[str, Any]) -> bool:
        """Fire the SL/TP OCO bracket after the resting entry is inferred filled."""
        if self.dry_run:
            logger.info(
                f"[DRY RUN] Would place exits: SL {pending['stop']:.2f} / "
                f"TP {pending['target']:.2f} (OCO) for {pending['direction']} position"
            )
            return True
        try:
            resp = self.bridge.bracket_exits(
                instrument=pending['instrument'],
                direction=pending['direction'],
                quantity=pending['quantity'],
                stop_loss=pending['stop'],
                take_profit=pending['target'],
            )
            logger.info(f"Bracket exits placed (OCO): {resp}")
            return True
        except NTBridgeError as e:
            logger.error(f"Error placing bracket exits to NT8 ATI: {e}")
            return False

    def cancel_entry(self, pending: Dict[str, Any]) -> bool:
        """Cancel an unfilled resting entry order (setup invalidated / expired)."""
        order_id = pending.get('order_id')
        if self.dry_run:
            logger.info(f"[DRY RUN] Would cancel resting entry {order_id}")
            return True
        if not order_id:
            return False
        try:
            self.bridge.cancel_order(order_id)
            logger.info(f"Resting entry cancelled: {order_id}")
            return True
        except NTBridgeError as e:
            logger.error(f"Error cancelling resting entry {order_id}: {e}")
            return False

    def close_position(self) -> bool:
        """Flatten the configured instrument and cancel its working orders."""
        if self.dry_run:
            logger.info(f"[DRY RUN] Would close position on {self.instrument}")
            return True
        try:
            resp = self.bridge.close_position(self.instrument)
            logger.info(f"Close position on {self.instrument}: {resp}")
            return True
        except NTBridgeError as e:
            logger.error(f"Error closing position: {e}")
            return False

    def _record(self, decision: Dict[str, Any], timestamp: datetime,
                status: str, responses: Any = None) -> None:
        """Append a submitted signal to the in-memory history."""
        self._history.append({
            'DateTime': timestamp.strftime('%m/%d/%Y %H:%M:%S'),
            'Direction': decision['decision'],
            'Entry_Price': f"{decision['entry']:.2f}",
            'Stop_Loss': f"{decision['stop']:.2f}",
            'Target': f"{decision['target']:.2f}",
            'Status': status,
            'Responses': responses,
        })

    def get_signal_summary(self, decision: Dict[str, Any]) -> str:
        """
        Generate human-readable signal summary

        Args:
            decision: Decision dictionary

        Returns:
            Summary string
        """
        entry = decision['entry']
        stop = decision['stop']
        target = decision['target']

        risk = abs(entry - stop)
        reward = abs(target - entry)
        rr_ratio = reward / risk if risk > 0 else 0

        lines = []
        lines.append(f"=== TRADE SIGNAL GENERATED ===")
        lines.append(f"Direction: {decision['decision']}")

        # Show setup type if available
        if 'setup_type' in decision and decision['setup_type']:
            lines.append(f"Setup Type: {decision['setup_type']}")

        lines.append(f"Entry: {entry:.2f}")
        lines.append(f"Stop Loss: {stop:.2f} ({risk:.2f}pts risk)")

        # Show raw target and buffer calculation if available
        if 'raw_target' in decision and decision['raw_target'] is not None:
            raw_target = decision['raw_target']
            buffer_direction = "+" if decision['decision'] == 'SHORT' else "-"
            lines.append(f"Raw Target: {raw_target:.2f}")
            lines.append(f"Final Target: {target:.2f} ({raw_target:.2f} {buffer_direction} 5pt buffer)")
        else:
            lines.append(f"Target: {target:.2f} ({reward:.2f}pts reward)")

        lines.append(f"Risk/Reward: {rr_ratio:.2f}:1")

        # Show confidence if available
        if 'confidence' in decision:
            lines.append(f"Confidence: {decision['confidence']:.0%}")

        # Show reasoning if available
        if 'reasoning' in decision and decision['reasoning']:
            lines.append(f"\nReasoning: {decision['reasoning']}")

        lines.append(f"\nSignal routed to NT8: {self.instrument} (account {self.account})")

        return "\n".join(lines)

    def count_signals_today(self) -> int:
        """
        Count number of signals generated today

        Returns:
            Number of signals today
        """
        today = datetime.now().strftime('%m/%d/%Y')
        return sum(1 for s in self._history if s['DateTime'].startswith(today))

    def get_recent_signals(self, limit: int = 10) -> list:
        """
        Get most recent signals

        Args:
            limit: Number of signals to retrieve

        Returns:
            List of signal dictionaries
        """
        return self._history[-limit:] if len(self._history) > limit else list(self._history)

    def clear_signals(self):
        """Clear in-memory signal history."""
        self._history.clear()
        logger.info("Trade signal history cleared")


# Example usage
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # dry_run avoids needing a live NT8 connection for the smoke test
    generator = SignalGenerator(
        account="Sim101",
        instrument="NQ 06-26",
        quantity=1,
        dry_run=True,
    )

    # Sample decision
    sample_decision = {
        'decision': 'SHORT',
        'entry': 14712.00,
        'stop': 14730.00,
        'target': 14650.00,
        'risk_reward': 3.44,
        'confidence': 0.78
    }

    # Generate signal
    success = generator.generate_signal(sample_decision)

    if success:
        print(generator.get_signal_summary(sample_decision))
        print(f"\nSignals today: {generator.count_signals_today()}")
        print(f"Recent: {generator.get_recent_signals()}")
