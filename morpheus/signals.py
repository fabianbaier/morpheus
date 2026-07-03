"""Context signal store — ambient sensor readings from phone/glasses couriers.

Omnipresence mode (see docs/omnipresence-prd.md §3.2) ingests short context
signals — today ``location``; later ``activity``, ``battery``,
``calendar_window`` — POSTed by the G2 bridge and stored here so the location
loop and the relevance judge can read "where is the user right now" without
talking to any device directly.

Storage lives in the same SQLite file as everything else (``db._connect``), in
its own ``context_signals`` table (this module owns its schema, same style as
``feeds.py``). Growth is bounded: at most ``MAX_PER_KIND`` rows are kept per
kind; the oldest rows are pruned on insert.

Privacy: signals never leave the local database; payloads are bounded JSON.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from morpheus import db as _db
from morpheus.db import _connect

# Keep at most this many signals per kind — enough history for a "did the user
# move materially" check without letting a per-minute courier grow the DB
# forever.
MAX_PER_KIND = 1000
# One signal is a compact sensor reading, not a document.
PAYLOAD_MAX_CHARS = 8192
KIND_MAX_CHARS = 32

_SCHEMA = """
CREATE TABLE IF NOT EXISTS context_signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    ts           REAL NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS context_signals_kind_idx ON context_signals(kind, id DESC);
"""


@dataclass
class ContextSignal:
    id: int
    kind: str
    ts: float
    payload: dict = field(default_factory=dict)


_initialized_db_paths: set[str] = set()


def _init() -> None:
    # Same memoization recipe as feeds._init(): idempotent DDL, skipped per
    # resolved database path so tests that repoint db.DB_PATH still get a
    # schema in each fresh file.
    key = str(_db.DB_PATH)
    if key in _initialized_db_paths:
        return
    with _connect() as conn:
        conn.executescript(_SCHEMA)
    _initialized_db_paths.add(key)


def _number(value: Any) -> Optional[float]:
    """Return the value as a float if it is a real number (bools excluded)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):  # NaN/inf
        return None
    return number


def _validate_location(payload: dict, now: float) -> dict:
    """Normalize a location payload: numeric lat/lon required, optional
    numeric accuracy, ts defaulting to now. Extra keys are preserved."""
    lat = _number(payload.get("lat"))
    lon = _number(payload.get("lon"))
    if lat is None or not -90.0 <= lat <= 90.0:
        raise ValueError("location payload needs a numeric lat in [-90, 90]")
    if lon is None or not -180.0 <= lon <= 180.0:
        raise ValueError("location payload needs a numeric lon in [-180, 180]")
    out = dict(payload)
    out["lat"] = lat
    out["lon"] = lon
    if "accuracy" in payload:
        accuracy = _number(payload.get("accuracy"))
        if accuracy is None or accuracy < 0:
            raise ValueError("location accuracy must be a non-negative number")
        out["accuracy"] = accuracy
    ts = _number(payload.get("ts")) if "ts" in payload else None
    out["ts"] = ts if ts is not None and ts > 0 else now
    return out


def _normalize_kind(kind: str) -> str:
    normalized = (kind or "").strip().lower()
    if not normalized or len(normalized) > KIND_MAX_CHARS:
        raise ValueError(f"kind must be 1-{KIND_MAX_CHARS} characters")
    if not all(ch.isalnum() or ch == "_" for ch in normalized):
        raise ValueError("kind must be alphanumeric/underscore")
    return normalized


def add_signal(kind: str, payload: dict, ts: Optional[float] = None) -> int:
    """Store one context signal; returns the row id.

    ``location`` payloads are validated (numeric lat/lon; optional accuracy;
    ts defaults to now). Keeps at most MAX_PER_KIND rows per kind.
    """
    kind = _normalize_kind(kind)
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    now = time.time()
    if kind == "location":
        payload = _validate_location(payload, now)
    if ts is None:
        payload_ts = _number(payload.get("ts")) if "ts" in payload else None
        ts = payload_ts if payload_ts is not None and payload_ts > 0 else now
    encoded = json.dumps(payload, ensure_ascii=False)
    if len(encoded) > PAYLOAD_MAX_CHARS:
        raise ValueError(f"payload too large (> {PAYLOAD_MAX_CHARS} chars)")
    _init()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO context_signals (kind, ts, payload_json) VALUES (?, ?, ?)",
            (kind, float(ts), encoded),
        )
        # Bound growth: prune the oldest rows beyond MAX_PER_KIND for this kind.
        conn.execute(
            "DELETE FROM context_signals WHERE kind = ? AND id NOT IN ("
            " SELECT id FROM context_signals WHERE kind = ? ORDER BY id DESC LIMIT ?)",
            (kind, kind, MAX_PER_KIND),
        )
        return cur.lastrowid


def latest(kind: str) -> Optional[ContextSignal]:
    """Return the newest signal of one kind, or None."""
    _init()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM context_signals WHERE kind = ? ORDER BY id DESC LIMIT 1",
            ((kind or "").strip().lower(),),
        ).fetchone()
    return _signal(row) if row else None


def recent(kind: str, limit: int = 20) -> list[ContextSignal]:
    """Return the newest ``limit`` signals of one kind, newest first."""
    _init()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM context_signals WHERE kind = ? ORDER BY id DESC LIMIT ?",
            ((kind or "").strip().lower(), max(1, int(limit))),
        ).fetchall()
    return [_signal(r) for r in rows]


def kinds() -> list[str]:
    """Return the distinct kinds present, alphabetically."""
    _init()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT kind FROM context_signals ORDER BY kind"
        ).fetchall()
    return [r["kind"] for r in rows]


def latest_per_kind() -> list[ContextSignal]:
    """Return the newest signal for every kind present (latest-per-kind view)."""
    _init()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT cs.* FROM context_signals cs JOIN ("
            " SELECT kind, MAX(id) AS max_id FROM context_signals GROUP BY kind"
            ") newest ON cs.id = newest.max_id ORDER BY cs.kind"
        ).fetchall()
    return [_signal(r) for r in rows]


def _signal(r) -> ContextSignal:
    try:
        payload = json.loads(r["payload_json"] or "{}")
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return ContextSignal(id=r["id"], kind=r["kind"], ts=r["ts"], payload=payload)
