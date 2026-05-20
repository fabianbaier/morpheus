"""SQLite-backed mission card store. Lives at ~/.morpheus/morpheus.db."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

DB_DIR = Path.home() / ".morpheus"
DB_PATH = DB_DIR / "morpheus.db"


@dataclass
class Mission:
    tab_id: str
    session_id: str = ""
    goal: str = ""
    state: str = "unknown"
    last_event: str = ""
    last_event_at: float = 0.0
    buffer_hash: str = ""
    buffer_changed_at: float = 0.0
    cmd: str = ""
    linked_pr: Optional[int] = None
    linked_worktree: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS missions (
    tab_id            TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL DEFAULT '',
    goal              TEXT NOT NULL DEFAULT '',
    state             TEXT NOT NULL DEFAULT 'unknown',
    last_event        TEXT NOT NULL DEFAULT '',
    last_event_at     REAL NOT NULL DEFAULT 0,
    buffer_hash       TEXT NOT NULL DEFAULT '',
    buffer_changed_at REAL NOT NULL DEFAULT 0,
    cmd               TEXT NOT NULL DEFAULT '',
    linked_pr         INTEGER,
    linked_worktree   TEXT NOT NULL DEFAULT '',
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS missions_state_idx ON missions(state);
CREATE INDEX IF NOT EXISTS missions_updated_idx ON missions(updated_at);

CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tab_id      TEXT,
    session_id  TEXT,
    text        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'note',   -- note | claim | broadcast
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS notes_tab_idx     ON notes(tab_id);
CREATE INDEX IF NOT EXISTS notes_created_idx ON notes(created_at DESC);
"""


def _connect() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def upsert(mission: Mission) -> None:
    mission.updated_at = time.time()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO missions (
                tab_id, session_id, goal, state, last_event, last_event_at,
                buffer_hash, buffer_changed_at, cmd, linked_pr, linked_worktree,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tab_id) DO UPDATE SET
                session_id        = excluded.session_id,
                goal              = COALESCE(NULLIF(excluded.goal, ''), missions.goal),
                state             = excluded.state,
                last_event        = excluded.last_event,
                last_event_at     = excluded.last_event_at,
                buffer_hash       = excluded.buffer_hash,
                buffer_changed_at = excluded.buffer_changed_at,
                cmd               = COALESCE(NULLIF(excluded.cmd, ''), missions.cmd),
                linked_pr         = COALESCE(excluded.linked_pr, missions.linked_pr),
                linked_worktree   = COALESCE(NULLIF(excluded.linked_worktree, ''), missions.linked_worktree),
                updated_at        = excluded.updated_at
            """,
            (
                mission.tab_id, mission.session_id, mission.goal, mission.state,
                mission.last_event, mission.last_event_at, mission.buffer_hash,
                mission.buffer_changed_at, mission.cmd, mission.linked_pr,
                mission.linked_worktree, mission.created_at, mission.updated_at,
            ),
        )


def get(tab_id: str) -> Optional[Mission]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM missions WHERE tab_id = ?", (tab_id,)).fetchone()
    return _row_to_mission(row) if row else None


def all_missions() -> list[Mission]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM missions ORDER BY updated_at DESC").fetchall()
    return [_row_to_mission(r) for r in rows]


def delete(tab_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM missions WHERE tab_id = ?", (tab_id,))


def reconcile_missing(known_tab_ids: Iterable[str]) -> int:
    """Delete mission rows for tab_ids no longer present. Returns count deleted."""
    known = list(known_tab_ids)
    with _connect() as conn:
        if not known:
            cur = conn.execute("DELETE FROM missions")
            return cur.rowcount
        placeholders = ",".join("?" * len(known))
        cur = conn.execute(
            f"DELETE FROM missions WHERE tab_id NOT IN ({placeholders})", known
        )
        return cur.rowcount


@dataclass
class Note:
    id: int
    tab_id: Optional[str]
    session_id: Optional[str]
    text: str
    kind: str
    created_at: float


def add_note(
    text: str,
    tab_id: Optional[str] = None,
    session_id: Optional[str] = None,
    kind: str = "note",
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO notes (tab_id, session_id, text, kind, created_at) VALUES (?, ?, ?, ?, ?)",
            (tab_id, session_id, text, kind, time.time()),
        )
        return cur.lastrowid


def recent_notes(limit: int = 20) -> list[Note]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM notes ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_note(r) for r in rows]


def notes_for_tab(tab_id: str, limit: int = 10) -> list[Note]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM notes WHERE tab_id = ? ORDER BY created_at DESC LIMIT ?",
            (tab_id, limit),
        ).fetchall()
    return [_row_to_note(r) for r in rows]


def _row_to_note(row: sqlite3.Row) -> Note:
    return Note(
        id=row["id"],
        tab_id=row["tab_id"],
        session_id=row["session_id"],
        text=row["text"],
        kind=row["kind"],
        created_at=row["created_at"],
    )


def _row_to_mission(row: sqlite3.Row) -> Mission:
    return Mission(
        tab_id=row["tab_id"],
        session_id=row["session_id"],
        goal=row["goal"],
        state=row["state"],
        last_event=row["last_event"],
        last_event_at=row["last_event_at"],
        buffer_hash=row["buffer_hash"],
        buffer_changed_at=row["buffer_changed_at"],
        cmd=row["cmd"],
        linked_pr=row["linked_pr"],
        linked_worktree=row["linked_worktree"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
