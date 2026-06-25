"""
Control channel writer for the monitor (Phase 3).

The dashboard only WRITES requests to data/bot_control.json. The bot reads it
each loop, executes (pause / cancel_entry / flatten), and clears the one-shot
command — so the bot stays the single owner of orders and state. Actions take
effect within one bot loop (~5s); they respect the bot's dry_run setting.
"""
from __future__ import annotations
import json
from datetime import datetime

from data_layer import DATA

CONTROL = DATA / "bot_control.json"


def read_control() -> dict:
    try:
        txt = CONTROL.read_text().strip()
        return json.loads(txt) if txt else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write(d: dict) -> None:
    CONTROL.parent.mkdir(parents=True, exist_ok=True)
    CONTROL.write_text(json.dumps(d, indent=2))


def set_paused(paused: bool) -> None:
    ctrl = read_control()
    ctrl["paused"] = bool(paused)
    _write(ctrl)


def request_command(cmd: str) -> None:
    """Queue a one-shot command ('cancel_entry' or 'flatten') for the bot."""
    ctrl = read_control()
    ctrl["command"] = cmd
    ctrl["requested_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _write(ctrl)


def is_paused() -> bool:
    return bool(read_control().get("paused", False))


def pending_command() -> str | None:
    return read_control().get("command")
