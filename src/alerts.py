"""
Email alerts for critical bot failures.

Sends via Gmail SMTP using GMAIL_USER / GMAIL_APP_PASSWORD from .env.
Per-key cooldown (default 6h, ALERT_COOLDOWN_HOURS) so a recurring hourly
failure produces a handful of emails, not sixty. Never raises — an alert
failure must not take down the trading loop.
"""

import json
import logging
import os
import smtplib
import time
from email.message import EmailMessage
from pathlib import Path

logger = logging.getLogger(__name__)

_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "alert_state.json"


def _cooldown_seconds() -> float:
    return float(os.getenv("ALERT_COOLDOWN_HOURS", "6")) * 3600


def _load_state() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text())
    except Exception:
        return {}


def send_alert(subject: str, body: str, key: str = "default") -> bool:
    """Email an alert to ALERT_EMAIL (falls back to GMAIL_USER).

    key groups related alerts for cooldown purposes. Returns True if sent.
    """
    try:
        state = _load_state()
        now = time.time()
        if now - state.get(key, 0) < _cooldown_seconds():
            logger.info(f"Alert '{key}' suppressed (cooldown active)")
            return False

        user = os.getenv("GMAIL_USER", "").strip()
        password = os.getenv("GMAIL_APP_PASSWORD", "").strip().replace(" ", "")
        to_addr = os.getenv("ALERT_EMAIL", user).strip()
        if not user or not password:
            logger.error("Alert NOT sent: GMAIL_USER / GMAIL_APP_PASSWORD missing from .env")
            return False

        msg = EmailMessage()
        msg["Subject"] = f"[Claude-Trader ALERT] {subject}"
        msg["From"] = user
        msg["To"] = to_addr
        msg.set_content(body)

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(user, password)
            server.send_message(msg)

        state[key] = now
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(state))
        logger.info(f"Alert email sent to {to_addr}: {subject}")
        return True
    except Exception as e:
        logger.error(f"Failed to send alert email: {e}")
        return False
