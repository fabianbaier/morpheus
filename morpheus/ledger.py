"""Cost + action ledgers — SQLite tables tracking every autonomous LLM call
and every operation morpheus took on the user's behalf.

Two tables, both in ~/.morpheus/morpheus.db (same file as missions / notes):
  cost_ledger    — one row per LLM invocation (claude -p, codex exec, web search)
  action_ledger  — one row per spawn / kill / note / snapshot / prune / etc.

`daily_dollar_total()` is what the autonomy gate consults to decide whether
to keep firing claude/codex on the user's behalf.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Optional

from morpheus.db import _connect


_SCHEMA = """
CREATE TABLE IF NOT EXISTS cost_ledger (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    tokens_estimate INTEGER NOT NULL DEFAULT 0,
    dollars         REAL NOT NULL DEFAULT 0,
    ts              REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS cost_ts_idx   ON cost_ledger(ts DESC);
CREATE INDEX IF NOT EXISTS cost_kind_idx ON cost_ledger(kind);

CREATE TABLE IF NOT EXISTS action_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    action      TEXT NOT NULL,
    tab_id      TEXT,
    details     TEXT NOT NULL DEFAULT '{}',
    ts          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS action_ts_idx  ON action_ledger(ts DESC);
CREATE INDEX IF NOT EXISTS action_kind_idx ON action_ledger(action);
"""


def _init() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)


@dataclass
class CostEntry:
    id: int
    kind: str
    description: str
    tokens_estimate: int
    dollars: float
    ts: float


@dataclass
class ActionEntry:
    id: int
    action: str
    tab_id: Optional[str]
    details: dict[str, Any]
    ts: float


# ── cost ledger ───────────────────────────────────────────────────────────

def log_cost(kind: str, description: str = "", tokens: int = 0,
             dollars: float = 0.0) -> int:
    _init()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO cost_ledger (kind, description, tokens_estimate, dollars, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (kind, description, int(tokens), float(dollars), time.time()),
        )
        return cur.lastrowid


def daily_dollar_total() -> float:
    """Sum of dollars spent today (local midnight → now)."""
    _init()
    midnight = _today_midnight()
    with _connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(dollars), 0) AS total FROM cost_ledger WHERE ts >= ?",
            (midnight,),
        ).fetchone()
    return float(row["total"] or 0.0)


def recent_costs(limit: int = 50) -> list[CostEntry]:
    _init()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM cost_ledger ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [CostEntry(**dict(r)) for r in rows]


# ── action ledger ────────────────────────────────────────────────────────

def log_action(action: str, tab_id: Optional[str] = None,
               details: Optional[dict] = None) -> int:
    _init()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO action_ledger (action, tab_id, details, ts) VALUES (?, ?, ?, ?)",
            (action, tab_id, json.dumps(details or {}), time.time()),
        )
        return cur.lastrowid


def recent_actions(limit: int = 50) -> list[ActionEntry]:
    _init()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM action_ledger ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    out: list[ActionEntry] = []
    for r in rows:
        try:
            details = json.loads(r["details"] or "{}")
        except Exception:
            details = {}
        out.append(ActionEntry(
            id=r["id"], action=r["action"], tab_id=r["tab_id"],
            details=details, ts=r["ts"],
        ))
    return out


# ── autonomy gate ────────────────────────────────────────────────────────

def is_within_daily_cap(cap_dollars: float) -> tuple[bool, float]:
    """Return (within_cap, current_total). The autonomy code consults this
    before firing another paid LLM call."""
    if cap_dollars <= 0:
        return True, 0.0
    total = daily_dollar_total()
    return total < cap_dollars, total


# ── helpers ──────────────────────────────────────────────────────────────

def _today_midnight() -> float:
    lt = time.localtime()
    midnight_struct = time.struct_time((
        lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0,
        lt.tm_wday, lt.tm_yday, lt.tm_isdst,
    ))
    return time.mktime(midnight_struct)
