"""
NT8 OIF Bridge
==============
Submits orders to NinjaTrader 8 via the documented Order Instruction File (OIF)
interface: write a uniquely-named `oif*.txt` into NT8's `incoming` folder and
NT processes it immediately, then deletes it.

  Operations > Automated Trading > ATI > File Interface > Order Instruction Files

NT8 setup (one-time):
  Tools → Options → Automated Trading Interface → ✓ AT Interface enabled
  The incoming folder is:  <Documents>/NinjaTrader 8/incoming

Why files, not the socket: NT8's ATI socket (port 36973) is a state/data stream,
NOT an order-entry channel — sending PLACE to it does nothing. Orders MUST go
through OIF files (or the NtDirect DLL). Verified empirically + per the help guide.

Documented PLACE field order (semicolon-delimited, positional):
  PLACE;<ACCOUNT>;<INSTRUMENT>;<ACTION>;<QTY>;<ORDERTYPE>;[LIMIT];[STOP];
        <TIF>;[OCOID];[ORDERID];[STRATEGY];[STRATEGYID]

Usage:
    from nt_rest_bridge import NTBridge, NTBridgeError

    nt = NTBridge(account="Sim101")              # auto-detects incoming dir
    nt.market_order("NQ 09-26", "BUY", 1)
    nt.bracket_order("NQ 09-26", "LONG", qty=1, stop_loss=18200.0, take_profit=18300.0)
    nt.close_position("NQ 09-26")
"""

import os
import uuid
from pathlib import Path
from typing import Literal, Optional

# Default NT8 incoming folder (under the user's Windows Documents).
# Under WSL, Path.home() is the Linux home — NT8 lives on the Windows side,
# so probe the /mnt/c mount too. NT8_INCOMING_DIR env var overrides everything.
def _default_incoming_dir() -> str:
    override = os.getenv("NT8_INCOMING_DIR")
    if override:
        return override
    candidates = [Path.home() / "Documents" / "NinjaTrader 8" / "incoming"]
    candidates += sorted(Path("/mnt/c/Users").glob("*/Documents/NinjaTrader 8/incoming"))
    for c in candidates:
        if c.is_dir():
            return str(c)
    return str(candidates[0])


class NTBridgeError(Exception):
    pass


class NTBridge:
    def __init__(
        self,
        account: str,
        host: str = "localhost",      # kept for config compatibility (unused by OIF)
        port: int = 36973,            # kept for config compatibility (unused by OIF)
        timeout: float = 5.0,         # kept for config compatibility (unused by OIF)
        incoming_dir: Optional[str] = None,
    ):
        self.account = account
        self.incoming_dir = Path(incoming_dir or _default_incoming_dir())

    # ------------------------------------------------------------------
    # Orders  (field order per the documented OIF PLACE format)
    # ------------------------------------------------------------------

    def market_order(
        self,
        instrument: str,
        action: Literal["BUY", "SELL"],
        quantity: int,
        tif: str = "DAY",
    ) -> str:
        return self._place(instrument, action, quantity, "MARKET", tif=tif)

    def limit_order(
        self,
        instrument: str,
        action: Literal["BUY", "SELL"],
        quantity: int,
        limit_price: float,
        tif: str = "DAY",
        oco: Optional[str] = None,
    ) -> str:
        return self._place(instrument, action, quantity, "LIMIT",
                           limit=limit_price, tif=tif, oco=oco)

    def stop_market_order(
        self,
        instrument: str,
        action: Literal["BUY", "SELL"],
        quantity: int,
        stop_price: float,
        tif: str = "DAY",
        oco: Optional[str] = None,
    ) -> str:
        return self._place(instrument, action, quantity, "STOPMARKET",
                           stop=stop_price, tif=tif, oco=oco)

    def _place(
        self,
        instrument: str,
        action: str,
        quantity: int,
        order_type: str,
        limit: float = 0,
        stop: float = 0,
        tif: str = "DAY",
        oco: str = "",
        order_id: str = "",
    ) -> str:
        # PLACE;ACCOUNT;INSTRUMENT;ACTION;QTY;ORDERTYPE;LIMIT;STOP;TIF;OCO;ORDERID;STRATEGY;STRATEGYID
        cmd = (
            f"PLACE;{self.account};{instrument};{action};{quantity};{order_type};"
            f"{limit};{stop};{tif};{oco or ''};{order_id or ''};;"
        )
        return self._write_oif(cmd)

    def cancel_order(self, order_id: str) -> str:
        # CANCEL;;;;;;;;;;ORDERID;;STRATEGYID
        return self._write_oif(f"CANCEL;;;;;;;;;;{order_id};;")

    def close_position(self, instrument: str) -> str:
        """Flatten position for this instrument (CLOSEPOSITION)."""
        # CLOSEPOSITION;ACCOUNT;INSTRUMENT;;;;;;;;;;
        return self._write_oif(f"CLOSEPOSITION;{self.account};{instrument};;;;;;;;;;")

    def flatten_everything(self) -> str:
        """Flatten ALL positions and cancel ALL orders on the account."""
        return self._write_oif("FLATTENEVERYTHING;;;;;;;;;;;;")

    # ------------------------------------------------------------------
    # Convenience: entry + bracket
    # ------------------------------------------------------------------

    def entry_order(
        self,
        instrument: str,
        direction: Literal["LONG", "SHORT", "BUY", "SELL"],
        quantity: int,
        order_type: Literal["LIMIT", "STOP"],
        price: float,
        tif: str = "GTC",
    ) -> str:
        """
        Place a *resting* entry order (LIMIT or STOP) at `price` and return a
        caller-supplied ORDERID so the order can be cancelled later.

        NO bracket is attached here. Unlike a market entry, a resting entry
        fills at an unknown future time, so the SL/TP cannot be sent up front
        (before the fill they have no position to protect and would act as
        wrong-way entries). Send them via bracket_exits() once the entry is
        known/inferred to have filled.

        order_type semantics:
          LIMIT - rest at a better price than market (pullback/rejection entries)
          STOP  - trigger as price trades through `price` (breakout entries)
        """
        action = "BUY" if direction in ("LONG", "BUY") else "SELL"
        order_id = f"entry-{uuid.uuid4().hex[:12]}"
        if order_type == "LIMIT":
            self._place(instrument, action, quantity, "LIMIT",
                        limit=price, tif=tif, order_id=order_id)
        else:
            self._place(instrument, action, quantity, "STOPMARKET",
                        stop=price, tif=tif, order_id=order_id)
        return order_id

    def bracket_exits(
        self,
        instrument: str,
        direction: Literal["LONG", "SHORT", "BUY", "SELL"],
        quantity: int,
        stop_loss: float,
        take_profit: float,
    ) -> dict:
        """
        Submit the protective SL stop + TP limit as an OCO pair *after* an entry
        has filled. `direction` is the position's direction (LONG/SHORT); the
        exits are placed on the opposite side. The SL and TP share an OCO id so
        NT cancels the surviving leg when either fills.
        """
        action = "BUY" if direction in ("LONG", "BUY") else "SELL"
        exit_action = "SELL" if action == "BUY" else "BUY"
        oco = f"oco-{uuid.uuid4().hex[:12]}"
        sl_id = f"sl-{uuid.uuid4().hex[:12]}"
        tp_id = f"tp-{uuid.uuid4().hex[:12]}"

        self._place(instrument, exit_action, quantity, "STOPMARKET",
                    stop=stop_loss, tif="GTC", oco=oco, order_id=sl_id)
        self._place(instrument, exit_action, quantity, "LIMIT",
                    limit=take_profit, tif="GTC", oco=oco, order_id=tp_id)
        return {"sl_id": sl_id, "tp_id": tp_id, "oco": oco}

    def bracket_order(
        self,
        instrument: str,
        direction: Literal["LONG", "SHORT", "BUY", "SELL"],
        quantity: int,
        stop_loss: float,
        take_profit: float,
    ) -> dict:
        """
        Market entry + SL stop + TP limit. The SL and TP share an OCO id so NT
        cancels the surviving leg when either fills (both directions). Retained
        as a fallback / for callers that want an immediate market fill.
        """
        action = "BUY" if direction in ("LONG", "BUY") else "SELL"
        entry = self.market_order(instrument, action, quantity, tif="DAY")
        exits = self.bracket_exits(instrument, direction, quantity, stop_loss, take_profit)
        return {"entry": entry, **exits}

    # ------------------------------------------------------------------
    # Read-back: NT's outgoing reply files
    # ------------------------------------------------------------------
    # For every PLACE that carries an ORDERID, NT maintains
    #   outgoing/<ACCOUNT>_<ORDERID>.txt  ->  <STATUS>;<FILLED QTY>;<AVG PRICE>
    # and per instrument
    #   outgoing/<FULL INSTRUMENT>_<ACCOUNT>_position.txt  ->  <POS>;<QTY>;<AVG PRICE>
    # These persist across NT/bot restarts, so a fill that happened while this
    # process was down is still readable afterwards.

    _MONTH_CODES = {"01": "JAN", "02": "FEB", "03": "MAR", "04": "APR",
                    "05": "MAY", "06": "JUN", "07": "JUL", "08": "AUG",
                    "09": "SEP", "10": "OCT", "11": "NOV", "12": "DEC"}

    @property
    def outgoing_dir(self) -> Path:
        return self.incoming_dir.parent / "outgoing"

    def order_status(self, order_id: str) -> Optional[tuple]:
        """Return (status, filled_qty, avg_fill_price) for an order we placed
        with an ORDERID, or None while NT has not written a reply yet."""
        if not order_id:
            return None
        path = self.outgoing_dir / f"{self.account}_{order_id}.txt"
        try:
            parts = path.read_text().strip().split(";")
            return parts[0].upper(), int(float(parts[1] or 0)), float(parts[2] or 0)
        except (OSError, IndexError, ValueError):
            return None

    def position_status(self, instrument: str) -> Optional[tuple]:
        """Return (direction, qty, avg_price) for e.g. 'NQ 09-26' from NT's
        position file ('LONG'/'SHORT'/'FLAT'), or None if unreadable. NT names
        the file with the full instrument name, e.g. 'NQ SEP26 Globex'."""
        try:
            symbol, expiry = instrument.rsplit(" ", 1)
            month, year = expiry.split("-")
            full_name = f"{symbol} {self._MONTH_CODES[month]}{year} Globex"
        except (ValueError, KeyError):
            return None
        path = self.outgoing_dir / f"{full_name}_{self.account}_position.txt"
        try:
            parts = path.read_text().strip().split(";")
            return parts[0].upper(), int(float(parts[1] or 0)), float(parts[2] or 0)
        except (OSError, IndexError, ValueError):
            return None

    # ------------------------------------------------------------------
    # Internal: write the OIF file
    # ------------------------------------------------------------------

    def _write_oif(self, line: str) -> str:
        """
        Write a single instruction line to a uniquely-named oif*.txt in the
        incoming folder. NT processes and deletes it. Write directly into the
        folder (not copy) to avoid the file-locking issue the docs warn about.
        """
        if not self.incoming_dir.exists():
            raise NTBridgeError(
                f"NT8 incoming folder not found: {self.incoming_dir}\n"
                "Is NinjaTrader installed and the AT Interface enabled? "
                "(Tools → Options → Automated Trading Interface)"
            )
        fname = f"oif_{uuid.uuid4().hex}.txt"
        path = self.incoming_dir / fname
        try:
            # Write atomically-ish: NT polls the folder, so write the full
            # content in one go directly into the watched folder.
            with open(path, "w", encoding="ascii", newline="\r\n") as f:
                f.write(line + "\r\n")
            return f"OIF written: {fname}"
        except OSError as e:
            raise NTBridgeError(f"Failed to write OIF file {path}: {e}") from e


# ------------------------------------------------------------------
# Smoke-test  (python nt_rest_bridge.py)
# ------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    nt = NTBridge(account="Sim101")
    print(f"Incoming folder: {nt.incoming_dir}")
    print(f"Folder exists:   {nt.incoming_dir.exists()}")

    if "--place" in sys.argv:
        # Live test: places 1 contract MARKET BUY on Sim101. Flatten after!
        instrument = "NQ 09-26"
        print(f"Placing TEST market BUY: 1 {instrument} on Sim101 ...")
        print(nt.market_order(instrument, "BUY", 1))
        print("Check NT Orders/Positions tab. Flatten the test position when done.")
    else:
        print("Dry check only. Re-run with --place to send a 1-contract test order:")
        print("    python nt_rest_bridge.py --place")
