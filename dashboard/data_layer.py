"""
Read-only data layer for the Claude-Trader monitor dashboard.

Every function reads files the live bot already writes (bot_state.json,
market_analysis.json, HistoricalData.csv, LiveFeed.csv) plus reconstructs the
closed-trade ledger by parsing the bot log. Nothing here mutates bot state, so
the dashboard cannot interfere with the running system.
"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import pandas as pd

BOT_ROOT = Path(__file__).resolve().parent.parent
DATA = BOT_ROOT / "data"
LOG = BOT_ROOT / "logs" / "trading_agent.log"
CONFIG = BOT_ROOT / "config" / "agent_config.json"

# NQ full-size = $20/point. Overridden by config if a point_value is set.
DEFAULT_POINT_VALUE = 20.0


def _load_json(path: Path) -> dict:
    try:
        txt = path.read_text().strip()
        return json.loads(txt) if txt else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def read_config() -> dict:
    return _load_json(CONFIG)


def point_value() -> float:
    cfg = read_config().get("execution", {})
    return float(cfg.get("point_value", DEFAULT_POINT_VALUE))


def read_state() -> dict:
    """Current pending_entry / in_position the bot is tracking."""
    return _load_json(DATA / "bot_state.json")


def read_analysis() -> dict:
    """Latest LLM decision (overall bias + long/short assessments)."""
    return _load_json(DATA / "market_analysis.json")


def read_price() -> Optional[float]:
    """Last live price from LiveFeed.csv."""
    try:
        df = pd.read_csv(DATA / "LiveFeed.csv")
        if len(df) and "Last" in df.columns:
            return float(df.iloc[-1]["Last"])
    except Exception:
        pass
    return None


def read_bars(limit: int = 300) -> pd.DataFrame:
    """Hourly OHLC + EMAs from HistoricalData.csv (deduped, sorted)."""
    try:
        df = pd.read_csv(DATA / "HistoricalData.csv")
        df["DateTime"] = pd.to_datetime(df["DateTime"], format="mixed")
        df = df.drop_duplicates("DateTime").sort_values("DateTime").tail(limit)
        return df.reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


# Heartbeat older than this => the NT SecondHistoricalData strategy itself has
# stopped (chart closed / NT crashed), which is worse than a feed disconnect.
HEARTBEAT_DEAD_SEC = 120


def read_feed_status() -> dict:
    """Feed health from FeedStatus.csv, the heartbeat the NinjaTrader
    SecondHistoricalData strategy writes every few seconds regardless of bar
    flow. Lets the dashboard show *why* the feed froze (disconnected vs. strategy
    not running vs. quiet market).

    Returns a dict the UI can render directly:
        ok           : bool   - feed is connected AND heartbeat is fresh
        connected    : bool|None
        state        : str|None  (INIT|UP|DOWN|RECONNECTED|DISCONNECTED|TERMINATED)
        heartbeat_age_sec : float|None
        last_bar     : str|None
        label        : str    - short human status ("Connected", "Disconnected", ...)
        detail       : str    - one-line explanation for a tooltip/caption
    """
    status = {
        "ok": False, "connected": None, "state": None,
        "heartbeat_age_sec": None, "last_bar": None,
        "label": "Unknown", "detail": "No FeedStatus.csv heartbeat yet.",
    }
    try:
        df = pd.read_csv(DATA / "FeedStatus.csv")
        if df.empty:
            return status
        row = df.iloc[-1]
        state = str(row.get("State", "")) or None
        status["state"] = state

        connected = row.get("Connected")
        if connected is not None and not pd.isna(connected):
            status["connected"] = bool(int(connected))

        hb = pd.to_datetime(row.get("Heartbeat"), errors="coerce")
        if pd.notna(hb):
            status["heartbeat_age_sec"] = (pd.Timestamp.now() - hb).total_seconds()

        status["last_bar"] = str(row.get("LastBar", "")) or None
    except (FileNotFoundError, OSError, ValueError, KeyError):
        return status

    age = status["heartbeat_age_sec"]
    conn = status["connected"]
    heartbeat_dead = age is not None and age > HEARTBEAT_DEAD_SEC

    if heartbeat_dead:
        status["label"] = "Strategy down"
        status["detail"] = (f"NT heartbeat is {age:.0f}s old - the "
                            f"SecondHistoricalData strategy/chart is not running.")
    elif conn is False:
        status["label"] = "Disconnected"
        status["detail"] = "NinjaTrader data feed is disconnected (auto-reconnect in progress)."
    elif conn is True:
        status["ok"] = True
        status["label"] = "Connected"
        status["detail"] = "NinjaTrader data feed is connected and the heartbeat is fresh."
    else:
        status["label"] = "Unknown"
        status["detail"] = "Heartbeat present but connection state is unreadable."

    return status


# ---- closed-trade ledger reconstructed from the log -------------------------

@dataclass
class Trade:
    armed_at: str
    side: str            # LONG / SHORT
    order_type: str      # LIMIT / STOP
    entry: float
    stop: float
    target: float
    status: str          # FILLED / EXPIRED / OPEN
    fill_at: str = ""
    exit_at: str = ""
    exit_kind: str = ""  # STOP / TARGET
    exit_price: float = 0.0
    points: float = 0.0
    usd: float = 0.0
    dry_run: bool = False


_RE_PLACE_LIVE = re.compile(
    r"Resting (LIMIT|STOP) entry placed: (LONG|SHORT) @ ([\d.]+).*?SL ([\d.]+) \| TP ([\d.]+)")
_RE_PLACE_DRY = re.compile(
    r"\[DRY RUN\] Would place resting (LIMIT|STOP) entry: (LONG|SHORT) .*?@ ([\d.]+) \| SL ([\d.]+) \| TP ([\d.]+)")
_RE_FILL = re.compile(r"ENTRY FILL inferred: (LONG|SHORT) (LIMIT|STOP) @ ([\d.]+)")
_RE_EXIT = re.compile(
    r"POSITION EXIT inferred \((STOP|TARGET)\): (LONG|SHORT) entry ([\d.]+) \| SL ([\d.]+) \| TP ([\d.]+)")
_RE_EXPIRE = re.compile(r"RESTING ENTRY EXPIRED after \d+ bars \(unfilled @ ([\d.]+)\)")
_RE_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def parse_trades(max_lines: int = 60000) -> list[dict]:
    """Walk the log and stitch armed→fill→exit into a closed-trade ledger."""
    if not LOG.exists():
        return []
    pv = point_value()
    try:
        lines = LOG.read_text(errors="replace").splitlines()[-max_lines:]
    except Exception:
        return []

    trades: list[Trade] = []
    cur: Optional[Trade] = None

    def ts(line: str) -> str:
        m = _RE_TS.match(line)
        return m.group(1) if m else ""

    for ln in lines:
        m = _RE_PLACE_LIVE.search(ln) or _RE_PLACE_DRY.search(ln)
        if m:
            if cur is not None:        # previous never resolved -> drop as stale
                pass
            cur = Trade(armed_at=ts(ln), order_type=m.group(1), side=m.group(2),
                        entry=float(m.group(3)), stop=float(m.group(4)),
                        target=float(m.group(5)), status="ARMED",
                        dry_run="[DRY RUN]" in ln)
            continue
        if cur is None:
            continue
        m = _RE_FILL.search(ln)
        if m:
            cur.status = "OPEN"
            cur.fill_at = ts(ln)
            cur.entry = float(m.group(3))
            continue
        m = _RE_EXIT.search(ln)
        if m:
            cur.exit_kind = m.group(1)
            cur.exit_at = ts(ln)
            cur.exit_price = cur.stop if m.group(1) == "STOP" else cur.target
            sign = 1 if cur.side == "LONG" else -1
            cur.points = round(sign * (cur.exit_price - cur.entry), 2)
            cur.usd = round(cur.points * pv, 2)
            cur.status = "FILLED"
            trades.append(cur)
            cur = None
            continue
        m = _RE_EXPIRE.search(ln)
        if m:
            cur.status = "EXPIRED"
            trades.append(cur)
            cur = None
            continue

    if cur is not None:                # still working/open at end of log
        trades.append(cur)
    return [asdict(t) for t in trades]


def closed_trades(trades: list[dict]) -> list[dict]:
    return [t for t in trades if t["status"] == "FILLED"]


def pnl_summary(trades: list[dict]) -> dict:
    """Aggregate stats over FILLED (real, non-dry-run) trades."""
    real = [t for t in closed_trades(trades) if not t["dry_run"]]
    wins = [t for t in real if t["points"] > 0]
    losses = [t for t in real if t["points"] <= 0]
    total_pts = round(sum(t["points"] for t in real), 2)
    total_usd = round(sum(t["usd"] for t in real), 2)
    return {
        "n": len(real),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(100 * len(wins) / len(real), 1) if real else 0.0,
        "total_pts": total_pts,
        "total_usd": total_usd,
        "best": round(max((t["points"] for t in real), default=0.0), 2),
        "worst": round(min((t["points"] for t in real), default=0.0), 2),
    }


def min_gap_size() -> float:
    return float(read_config().get("trading_params", {}).get("min_gap_size", 5.0))


def compute_fvgs(df: pd.DataFrame) -> list[dict]:
    """Mirror FairValueGaps.find_fvgs_in_data + is_fvg_filled on the chart bars.

    3-candle window (c1=i-2, c3=i): bullish gap when c3.Low > c1.High, bearish
    when c3.High < c1.Low, each >= min_gap_size. A zone is 'filled' once a later
    bar trades back through its far edge.
    """
    if df is None or len(df) < 3:
        return []
    mn = min_gap_size()
    out = []
    n = len(df)
    for i in range(2, n):
        c1, c3 = df.iloc[i - 2], df.iloc[i]
        kind = top = bottom = None
        if c3["Low"] > c1["High"] and (c3["Low"] - c1["High"]) >= mn:
            kind, top, bottom = "bullish", c3["Low"], c1["High"]
        elif c3["High"] < c1["Low"] and (c1["Low"] - c3["High"]) >= mn:
            kind, top, bottom = "bearish", c1["Low"], c3["High"]
        if kind is None:
            continue
        filled, fill_dt = False, None
        for j in range(i + 1, n):
            ch = df.iloc[j]
            if (kind == "bullish" and ch["Low"] <= bottom) or \
               (kind == "bearish" and ch["High"] >= top):
                filled, fill_dt = True, ch["DateTime"]
                break
        out.append({
            "type": kind, "top": float(top), "bottom": float(bottom),
            "dt_start": c3["DateTime"], "dt_fill": fill_dt,
            "filled": filled, "gap_size": float(abs(top - bottom)),
        })
    return out


def equity_curve(trades: list[dict]) -> pd.DataFrame:
    real = [t for t in closed_trades(trades) if not t["dry_run"]]
    rows, cum = [], 0.0
    for i, t in enumerate(real, 1):
        cum += t["usd"]
        rows.append({"n": i, "exit_at": t["exit_at"], "cum_usd": round(cum, 2)})
    return pd.DataFrame(rows)
