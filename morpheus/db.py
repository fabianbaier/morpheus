"""SQLite-backed mission card and mission graph store.

The database lives at ~/.morpheus/morpheus.db. v0.6 stored live iTerm tabs in
`missions`; v0.7 adds durable mission graph tables so a mission can outlive the
tab/session currently attached to it.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

DB_DIR = Path.home() / ".morpheus"
DB_PATH = DB_DIR / "morpheus.db"


@dataclass
class Mission:
    tab_id: str
    mission_id: str = ""
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


@dataclass
class MissionMemory:
    mission_id: str
    title: str = ""
    why: str = ""
    done_definition: str = ""
    acceptance_criteria: str = ""
    current_plan: str = ""
    next_step: str = ""
    last_decision: str = ""
    last_summary: str = ""
    blocked_on: str = ""
    phase: str = "planning"
    confidence: float = 0.0
    source_kind: str = "imported"
    source_ref: str = ""
    epic_ref: str = ""
    issue_ref: str = ""
    last_verified_at: float = 0.0
    claimed_paths: str = "[]"
    topic: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    archived_at: Optional[float] = None


@dataclass
class MissionEvent:
    id: int
    mission_id: str
    ts: float
    kind: str
    actor: str
    summary: str
    source_ref: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MissionArtifact:
    id: int
    mission_id: str
    kind: str
    path_or_url: str
    status: str
    summary: str
    created_at: float


@dataclass
class MissionEdge:
    id: int
    from_id: str
    to_id: str
    relation: str
    reason: str
    created_at: float


@dataclass
class PromptLoop:
    id: int
    name: str
    prompt: str
    interval_seconds: float
    command: str
    target_mission_id: str = ""
    target_tab_id: Optional[str] = None
    status: str = "active"
    last_run_at: float = 0.0
    next_run_at: float = 0.0
    last_run_status: str = ""
    last_summary: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class PromptLoopRun:
    id: int
    loop_id: int
    started_at: float
    finished_at: float
    status: str
    exit_code: Optional[int]
    output_path: str
    summary: str
    target_mission_id: str = ""
    target_tab_id: Optional[str] = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS missions (
    tab_id            TEXT PRIMARY KEY,
    mission_id        TEXT NOT NULL DEFAULT '',
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

CREATE TABLE IF NOT EXISTS mission_memory (
    mission_id          TEXT PRIMARY KEY,
    title               TEXT NOT NULL DEFAULT '',
    why                 TEXT NOT NULL DEFAULT '',
    done_definition     TEXT NOT NULL DEFAULT '',
    acceptance_criteria TEXT NOT NULL DEFAULT '',
    current_plan        TEXT NOT NULL DEFAULT '',
    next_step           TEXT NOT NULL DEFAULT '',
    last_decision       TEXT NOT NULL DEFAULT '',
    last_summary        TEXT NOT NULL DEFAULT '',
    blocked_on          TEXT NOT NULL DEFAULT '',
    phase               TEXT NOT NULL DEFAULT 'planning',
    confidence          REAL NOT NULL DEFAULT 0,
    source_kind         TEXT NOT NULL DEFAULT 'imported',
    source_ref          TEXT NOT NULL DEFAULT '',
    epic_ref            TEXT NOT NULL DEFAULT '',
    issue_ref           TEXT NOT NULL DEFAULT '',
    last_verified_at    REAL NOT NULL DEFAULT 0,
    claimed_paths       TEXT NOT NULL DEFAULT '[]',
    topic               TEXT NOT NULL DEFAULT '',
    created_at          REAL NOT NULL DEFAULT 0,
    updated_at          REAL NOT NULL DEFAULT 0,
    archived_at         REAL
);

CREATE INDEX IF NOT EXISTS mission_memory_topic_idx ON mission_memory(topic);
CREATE INDEX IF NOT EXISTS mission_memory_phase_idx ON mission_memory(phase);
CREATE INDEX IF NOT EXISTS mission_memory_archived_idx ON mission_memory(archived_at);

CREATE TABLE IF NOT EXISTS mission_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id    TEXT NOT NULL,
    ts            REAL NOT NULL,
    kind          TEXT NOT NULL,
    actor         TEXT NOT NULL DEFAULT 'morpheus',
    summary       TEXT NOT NULL,
    source_ref    TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS mission_events_mission_idx ON mission_events(mission_id, ts DESC);
CREATE INDEX IF NOT EXISTS mission_events_kind_idx ON mission_events(kind);

CREATE TABLE IF NOT EXISTS mission_artifacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id  TEXT NOT NULL,
    kind        TEXT NOT NULL,
    path_or_url TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'unknown',
    summary     TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS mission_artifacts_mission_idx ON mission_artifacts(mission_id, created_at DESC);
CREATE INDEX IF NOT EXISTS mission_artifacts_kind_idx ON mission_artifacts(kind);

CREATE TABLE IF NOT EXISTS mission_edges (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    from_id    TEXT NOT NULL,
    to_id      TEXT NOT NULL,
    relation   TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS mission_edges_from_idx ON mission_edges(from_id);
CREATE INDEX IF NOT EXISTS mission_edges_to_idx ON mission_edges(to_id);
CREATE INDEX IF NOT EXISTS mission_edges_relation_idx ON mission_edges(relation);

CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tab_id      TEXT,
    session_id  TEXT,
    text        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'note',
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS notes_tab_idx     ON notes(tab_id);
CREATE INDEX IF NOT EXISTS notes_created_idx ON notes(created_at DESC);

CREATE TABLE IF NOT EXISTS prompt_loops (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL,
    prompt            TEXT NOT NULL,
    interval_seconds  REAL NOT NULL,
    command           TEXT NOT NULL,
    target_mission_id TEXT NOT NULL DEFAULT '',
    target_tab_id     TEXT,
    status            TEXT NOT NULL DEFAULT 'active',
    last_run_at       REAL NOT NULL DEFAULT 0,
    next_run_at       REAL NOT NULL,
    last_run_status   TEXT NOT NULL DEFAULT '',
    last_summary      TEXT NOT NULL DEFAULT '',
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS prompt_loops_status_next_idx ON prompt_loops(status, next_run_at);
CREATE INDEX IF NOT EXISTS prompt_loops_target_idx ON prompt_loops(target_mission_id);

CREATE TABLE IF NOT EXISTS prompt_loop_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    loop_id           INTEGER NOT NULL,
    started_at        REAL NOT NULL,
    finished_at       REAL NOT NULL,
    status            TEXT NOT NULL,
    exit_code         INTEGER,
    output_path       TEXT NOT NULL DEFAULT '',
    summary           TEXT NOT NULL DEFAULT '',
    target_mission_id TEXT NOT NULL DEFAULT '',
    target_tab_id     TEXT
);

CREATE INDEX IF NOT EXISTS prompt_loop_runs_loop_idx ON prompt_loop_runs(loop_id, started_at DESC);
"""


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        conn.close()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    _ensure_column(conn, "missions", "mission_id", "TEXT NOT NULL DEFAULT ''")
    conn.execute("CREATE INDEX IF NOT EXISTS missions_mission_id_idx ON missions(mission_id)")


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _new_mission_id(now: Optional[float] = None) -> str:
    ts = time.strftime("%Y%m%d%H%M%S", time.localtime(now or time.time()))
    return f"m_{ts}_{secrets.token_hex(4)}"


def new_mission_id(now: Optional[float] = None) -> str:
    return _new_mission_id(now)


def _ensure_mission_identity(conn: sqlite3.Connection, mission: Mission) -> None:
    if mission.mission_id:
        return
    row = conn.execute(
        "SELECT mission_id FROM missions WHERE tab_id = ?",
        (mission.tab_id,),
    ).fetchone()
    if row and row["mission_id"]:
        mission.mission_id = row["mission_id"]
    else:
        mission.mission_id = _new_mission_id(mission.created_at)


def _ensure_memory_row(conn: sqlite3.Connection, mission: Mission, now: float) -> None:
    title = mission.goal or mission.cmd or mission.tab_id.split("-")[0]
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO mission_memory (
            mission_id, title, source_kind, source_ref, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            mission.mission_id,
            title,
            "imported",
            f"tab:{mission.tab_id}",
            now,
            now,
        ),
    )
    if cur.rowcount:
        _insert_event(
            conn,
            mission.mission_id,
            kind="created",
            actor="morpheus",
            summary=f"Mission created: {title or mission.mission_id}",
            source_ref=f"tab:{mission.tab_id}",
            ts=now,
        )
    if title:
        conn.execute(
            """
            UPDATE mission_memory
               SET title = ?, updated_at = ?
             WHERE mission_id = ? AND title = ''
            """,
            (title, now, mission.mission_id),
        )


def upsert(mission: Mission) -> None:
    mission.updated_at = time.time()
    with _connect() as conn:
        _ensure_mission_identity(conn, mission)
        conn.execute(
            """
            INSERT INTO missions (
                tab_id, mission_id, session_id, goal, state, last_event,
                last_event_at, buffer_hash, buffer_changed_at, cmd, linked_pr,
                linked_worktree, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tab_id) DO UPDATE SET
                mission_id        = CASE
                                      WHEN missions.mission_id = '' THEN excluded.mission_id
                                      ELSE missions.mission_id
                                    END,
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
                mission.tab_id,
                mission.mission_id,
                mission.session_id,
                mission.goal,
                mission.state,
                mission.last_event,
                mission.last_event_at,
                mission.buffer_hash,
                mission.buffer_changed_at,
                mission.cmd,
                mission.linked_pr,
                mission.linked_worktree,
                mission.created_at,
                mission.updated_at,
            ),
        )
        row = conn.execute(
            "SELECT mission_id FROM missions WHERE tab_id = ?",
            (mission.tab_id,),
        ).fetchone()
        if row and row["mission_id"]:
            mission.mission_id = row["mission_id"]
        _ensure_memory_row(conn, mission, mission.updated_at)


def get(tab_id: str) -> Optional[Mission]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM missions WHERE tab_id = ?", (tab_id,)).fetchone()
    return _row_to_mission(row) if row else None


def update_mission_details(
    tab_id: str,
    *,
    goal: str,
    linked_pr: Optional[int],
    linked_worktree: str,
) -> bool:
    """Update user-editable live attachment fields exactly as supplied."""
    now = time.time()
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE missions
               SET goal = ?,
                   linked_pr = ?,
                   linked_worktree = ?,
                   updated_at = ?
             WHERE tab_id = ?
            """,
            (goal, linked_pr, linked_worktree, now, tab_id),
        )
    return cur.rowcount > 0


def all_missions() -> list[Mission]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM missions ORDER BY updated_at DESC").fetchall()
    return [_row_to_mission(r) for r in rows]


def delete(tab_id: str) -> None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT mission_id FROM missions WHERE tab_id = ?",
            (tab_id,),
        ).fetchone()
        if row and row["mission_id"]:
            _archive_mission(conn, row["mission_id"], f"tab {tab_id} deleted", f"tab:{tab_id}")
        conn.execute("DELETE FROM missions WHERE tab_id = ?", (tab_id,))


def reconcile_missing(known_tab_ids: Iterable[str]) -> int:
    """Delete live attachment rows for missing tabs. Durable memory survives."""
    known = list(known_tab_ids)
    with _connect() as conn:
        if not known:
            rows = conn.execute("SELECT tab_id, mission_id FROM missions").fetchall()
            for row in rows:
                if row["mission_id"]:
                    _archive_mission(
                        conn,
                        row["mission_id"],
                        f"tab {row['tab_id']} disappeared",
                        f"tab:{row['tab_id']}",
                    )
            cur = conn.execute("DELETE FROM missions")
            return cur.rowcount

        placeholders = ",".join("?" * len(known))
        rows = conn.execute(
            f"SELECT tab_id, mission_id FROM missions WHERE tab_id NOT IN ({placeholders})",
            known,
        ).fetchall()
        for row in rows:
            if row["mission_id"]:
                _archive_mission(
                    conn,
                    row["mission_id"],
                    f"tab {row['tab_id']} disappeared",
                    f"tab:{row['tab_id']}",
                )
        cur = conn.execute(
            f"DELETE FROM missions WHERE tab_id NOT IN ({placeholders})",
            known,
        )
        return cur.rowcount


def upsert_memory(memory: MissionMemory) -> None:
    now = time.time()
    memory.updated_at = now
    if not memory.created_at:
        memory.created_at = now
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO mission_memory (
                mission_id, title, why, done_definition, acceptance_criteria,
                current_plan, next_step, last_decision, last_summary,
                blocked_on, phase, confidence, source_kind, source_ref,
                epic_ref, issue_ref, last_verified_at, claimed_paths, topic,
                created_at, updated_at, archived_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mission_id) DO UPDATE SET
                title               = excluded.title,
                why                 = excluded.why,
                done_definition     = excluded.done_definition,
                acceptance_criteria = excluded.acceptance_criteria,
                current_plan        = excluded.current_plan,
                next_step           = excluded.next_step,
                last_decision       = excluded.last_decision,
                last_summary        = excluded.last_summary,
                blocked_on          = excluded.blocked_on,
                phase               = excluded.phase,
                confidence          = excluded.confidence,
                source_kind         = excluded.source_kind,
                source_ref          = excluded.source_ref,
                epic_ref            = excluded.epic_ref,
                issue_ref           = excluded.issue_ref,
                last_verified_at    = excluded.last_verified_at,
                claimed_paths       = excluded.claimed_paths,
                topic               = excluded.topic,
                updated_at          = excluded.updated_at,
                archived_at         = excluded.archived_at
            """,
            (
                memory.mission_id,
                memory.title,
                memory.why,
                memory.done_definition,
                memory.acceptance_criteria,
                memory.current_plan,
                memory.next_step,
                memory.last_decision,
                memory.last_summary,
                memory.blocked_on,
                memory.phase,
                memory.confidence,
                memory.source_kind,
                memory.source_ref,
                memory.epic_ref,
                memory.issue_ref,
                memory.last_verified_at,
                memory.claimed_paths,
                memory.topic,
                memory.created_at,
                memory.updated_at,
                memory.archived_at,
            ),
        )


def get_memory(mission_id: str) -> Optional[MissionMemory]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM mission_memory WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()
    return _row_to_memory(row) if row else None


def memory_for_tab(tab_id: str) -> Optional[MissionMemory]:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT mm.*
              FROM mission_memory mm
              JOIN missions m ON m.mission_id = mm.mission_id
             WHERE m.tab_id = ?
            """,
            (tab_id,),
        ).fetchone()
    return _row_to_memory(row) if row else None


def all_memory(include_archived: bool = False) -> list[MissionMemory]:
    query = "SELECT * FROM mission_memory"
    if not include_archived:
        query += " WHERE archived_at IS NULL"
    query += " ORDER BY updated_at DESC"
    with _connect() as conn:
        rows = conn.execute(query).fetchall()
    return [_row_to_memory(r) for r in rows]


def archive_memory(mission_id: str, summary: str = "mission archived") -> None:
    with _connect() as conn:
        _archive_mission(conn, mission_id, summary, "")


def add_event(
    mission_id: str,
    kind: str,
    summary: str,
    actor: str = "user",
    source_ref: str = "",
    metadata: Optional[dict[str, Any]] = None,
) -> int:
    with _connect() as conn:
        return _insert_event(
            conn,
            mission_id,
            kind=kind,
            actor=actor,
            summary=summary,
            source_ref=source_ref,
            metadata=metadata,
        )


def recent_events(mission_id: str, limit: int = 10) -> list[MissionEvent]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM mission_events
             WHERE mission_id = ?
             ORDER BY ts DESC
             LIMIT ?
            """,
            (mission_id, limit),
        ).fetchall()
    return [_row_to_event(r) for r in rows]


def add_artifact(
    mission_id: str,
    kind: str,
    path_or_url: str,
    status: str = "unknown",
    summary: str = "",
) -> int:
    with _connect() as conn:
        now = time.time()
        cur = conn.execute(
            """
            INSERT INTO mission_artifacts (
                mission_id, kind, path_or_url, status, summary, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (mission_id, kind, path_or_url, status, summary, now),
        )
        _insert_event(
            conn,
            mission_id,
            kind="artifact",
            actor="morpheus",
            summary=summary or f"{kind}: {path_or_url}",
            source_ref=path_or_url,
            ts=now,
            metadata={"artifact_id": cur.lastrowid, "status": status},
        )
        return cur.lastrowid


def artifacts_for_mission(mission_id: str, limit: int = 20) -> list[MissionArtifact]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM mission_artifacts
             WHERE mission_id = ?
             ORDER BY created_at DESC
             LIMIT ?
            """,
            (mission_id, limit),
        ).fetchall()
    return [_row_to_artifact(r) for r in rows]


def add_edge(
    from_id: str,
    to_id: str,
    relation: str,
    reason: str = "",
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO mission_edges (from_id, to_id, relation, reason, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (from_id, to_id, relation, reason, time.time()),
        )
        return cur.lastrowid


def edges_for_id(node_id: str, limit: int = 20) -> list[MissionEdge]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM mission_edges
             WHERE from_id = ? OR to_id = ?
             ORDER BY created_at DESC
             LIMIT ?
            """,
            (node_id, node_id, limit),
        ).fetchall()
    return [_row_to_edge(r) for r in rows]


def edges_from_id(node_id: str, relation: str = "", limit: int = 50) -> list[MissionEdge]:
    query = "SELECT * FROM mission_edges WHERE from_id = ?"
    params: list[Any] = [node_id]
    if relation:
        query += " AND relation = ?"
        params.append(relation)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_edge(r) for r in rows]


def edges_to_id(node_id: str, relation: str = "", limit: int = 50) -> list[MissionEdge]:
    query = "SELECT * FROM mission_edges WHERE to_id = ?"
    params: list[Any] = [node_id]
    if relation:
        query += " AND relation = ?"
        params.append(relation)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_edge(r) for r in rows]


def graph_counts() -> dict[str, int]:
    with _connect() as conn:
        return {
            "live_sessions": conn.execute("SELECT COUNT(*) AS n FROM missions").fetchone()["n"],
            "missions": conn.execute("SELECT COUNT(*) AS n FROM mission_memory").fetchone()["n"],
            "active_missions": conn.execute(
                "SELECT COUNT(*) AS n FROM mission_memory WHERE archived_at IS NULL"
            ).fetchone()["n"],
            "archived_missions": conn.execute(
                "SELECT COUNT(*) AS n FROM mission_memory WHERE archived_at IS NOT NULL"
            ).fetchone()["n"],
            "events": conn.execute("SELECT COUNT(*) AS n FROM mission_events").fetchone()["n"],
            "artifacts": conn.execute("SELECT COUNT(*) AS n FROM mission_artifacts").fetchone()["n"],
            "edges": conn.execute("SELECT COUNT(*) AS n FROM mission_edges").fetchone()["n"],
            "loops": conn.execute("SELECT COUNT(*) AS n FROM prompt_loops").fetchone()["n"],
            "loop_runs": conn.execute("SELECT COUNT(*) AS n FROM prompt_loop_runs").fetchone()["n"],
        }


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


def create_loop(
    name: str,
    prompt: str,
    interval_seconds: float,
    command: str,
    target_mission_id: str = "",
    target_tab_id: Optional[str] = None,
    status: str = "active",
    next_run_at: Optional[float] = None,
) -> PromptLoop:
    now = time.time()
    next_at = next_run_at if next_run_at is not None else now + interval_seconds
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO prompt_loops (
                name, prompt, interval_seconds, command, target_mission_id,
                target_tab_id, status, next_run_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                prompt,
                interval_seconds,
                command,
                target_mission_id,
                target_tab_id,
                status,
                next_at,
                now,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM prompt_loops WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row_to_prompt_loop(row)


def all_loops(include_paused: bool = True) -> list[PromptLoop]:
    query = "SELECT * FROM prompt_loops"
    params: tuple[Any, ...] = ()
    if not include_paused:
        query += " WHERE status = ?"
        params = ("active",)
    query += " ORDER BY next_run_at ASC, created_at DESC"
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_prompt_loop(row) for row in rows]


def get_loop(loop_id: int) -> Optional[PromptLoop]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM prompt_loops WHERE id = ?", (loop_id,)).fetchone()
    return _row_to_prompt_loop(row) if row else None


def due_loops(now: Optional[float] = None, limit: int = 10) -> list[PromptLoop]:
    ts = now or time.time()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM prompt_loops
             WHERE status = 'active' AND next_run_at <= ?
             ORDER BY next_run_at ASC
             LIMIT ?
            """,
            (ts, limit),
        ).fetchall()
    return [_row_to_prompt_loop(row) for row in rows]


def set_loop_status(loop_id: int, status: str) -> Optional[PromptLoop]:
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "UPDATE prompt_loops SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, loop_id),
        )
        row = conn.execute("SELECT * FROM prompt_loops WHERE id = ?", (loop_id,)).fetchone()
    return _row_to_prompt_loop(row) if row else None


def update_loop_after_run(
    loop_id: int,
    *,
    last_run_at: float,
    next_run_at: float,
    last_run_status: str,
    last_summary: str,
) -> Optional[PromptLoop]:
    now = time.time()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE prompt_loops
               SET last_run_at = ?,
                   next_run_at = ?,
                   last_run_status = ?,
                   last_summary = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            (last_run_at, next_run_at, last_run_status, last_summary, now, loop_id),
        )
        row = conn.execute("SELECT * FROM prompt_loops WHERE id = ?", (loop_id,)).fetchone()
    return _row_to_prompt_loop(row) if row else None


def record_loop_run(
    loop_id: int,
    *,
    started_at: float,
    finished_at: float,
    status: str,
    exit_code: Optional[int],
    output_path: str,
    summary: str,
    target_mission_id: str = "",
    target_tab_id: Optional[str] = None,
) -> PromptLoopRun:
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO prompt_loop_runs (
                loop_id, started_at, finished_at, status, exit_code, output_path,
                summary, target_mission_id, target_tab_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                loop_id,
                started_at,
                finished_at,
                status,
                exit_code,
                output_path,
                summary,
                target_mission_id,
                target_tab_id,
            ),
        )
        row = conn.execute("SELECT * FROM prompt_loop_runs WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row_to_prompt_loop_run(row)


def loop_runs(loop_id: int, limit: int = 20) -> list[PromptLoopRun]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM prompt_loop_runs
             WHERE loop_id = ?
             ORDER BY started_at DESC
             LIMIT ?
            """,
            (loop_id, limit),
        ).fetchall()
    return [_row_to_prompt_loop_run(row) for row in rows]


def update_loop_details(
    loop_id: int,
    *,
    name: Optional[str] = None,
    prompt: Optional[str] = None,
    interval_seconds: Optional[float] = None,
    command: Optional[str] = None,
) -> Optional[PromptLoop]:
    updates: list[str] = []
    params: list[Any] = []
    if name is not None:
        updates.append("name = ?")
        params.append(name)
    if prompt is not None:
        updates.append("prompt = ?")
        params.append(prompt)
    if interval_seconds is not None:
        updates.append("interval_seconds = ?")
        params.append(interval_seconds)
        updates.append("next_run_at = ?")
        params.append(time.time() + interval_seconds)
    if command is not None:
        updates.append("command = ?")
        params.append(command)
    if not updates:
        return get_loop(loop_id)

    updates.append("updated_at = ?")
    params.append(time.time())
    params.append(loop_id)
    with _connect() as conn:
        conn.execute(
            f"UPDATE prompt_loops SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        row = conn.execute("SELECT * FROM prompt_loops WHERE id = ?", (loop_id,)).fetchone()
    return _row_to_prompt_loop(row) if row else None


def set_loop_target(
    loop_id: int,
    *,
    target_mission_id: str = "",
    target_tab_id: Optional[str] = None,
) -> Optional[PromptLoop]:
    now = time.time()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE prompt_loops
               SET target_mission_id = ?,
                   target_tab_id = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            (target_mission_id, target_tab_id, now, loop_id),
        )
        row = conn.execute("SELECT * FROM prompt_loops WHERE id = ?", (loop_id,)).fetchone()
    return _row_to_prompt_loop(row) if row else None


def delete_loop(loop_id: int) -> Optional[PromptLoop]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM prompt_loops WHERE id = ?", (loop_id,)).fetchone()
        if row is None:
            return None
        loop = _row_to_prompt_loop(row)
        conn.execute("DELETE FROM prompt_loop_runs WHERE loop_id = ?", (loop_id,))
        conn.execute("DELETE FROM prompt_loops WHERE id = ?", (loop_id,))
    return loop


def _archive_mission(
    conn: sqlite3.Connection,
    mission_id: str,
    summary: str,
    source_ref: str,
) -> None:
    now = time.time()
    conn.execute(
        """
        UPDATE mission_memory
           SET phase = 'archived',
               archived_at = COALESCE(archived_at, ?),
               updated_at = ?
         WHERE mission_id = ?
        """,
        (now, now, mission_id),
    )
    _insert_event(
        conn,
        mission_id,
        kind="archive",
        actor="morpheus",
        summary=summary,
        source_ref=source_ref,
        ts=now,
    )


def _insert_event(
    conn: sqlite3.Connection,
    mission_id: str,
    kind: str,
    actor: str,
    summary: str,
    source_ref: str = "",
    metadata: Optional[dict[str, Any]] = None,
    ts: Optional[float] = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO mission_events (
            mission_id, ts, kind, actor, summary, source_ref, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            mission_id,
            ts or time.time(),
            kind,
            actor,
            summary,
            source_ref,
            json.dumps(metadata or {}, sort_keys=True),
        ),
    )
    return cur.lastrowid


def _row_to_note(row: sqlite3.Row) -> Note:
    return Note(
        id=row["id"],
        tab_id=row["tab_id"],
        session_id=row["session_id"],
        text=row["text"],
        kind=row["kind"],
        created_at=row["created_at"],
    )


def _row_to_prompt_loop(row: sqlite3.Row) -> PromptLoop:
    return PromptLoop(
        id=row["id"],
        name=row["name"],
        prompt=row["prompt"],
        interval_seconds=row["interval_seconds"],
        command=row["command"],
        target_mission_id=row["target_mission_id"],
        target_tab_id=row["target_tab_id"],
        status=row["status"],
        last_run_at=row["last_run_at"],
        next_run_at=row["next_run_at"],
        last_run_status=row["last_run_status"],
        last_summary=row["last_summary"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_prompt_loop_run(row: sqlite3.Row) -> PromptLoopRun:
    return PromptLoopRun(
        id=row["id"],
        loop_id=row["loop_id"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        status=row["status"],
        exit_code=row["exit_code"],
        output_path=row["output_path"],
        summary=row["summary"],
        target_mission_id=row["target_mission_id"],
        target_tab_id=row["target_tab_id"],
    )


def _row_to_mission(row: sqlite3.Row) -> Mission:
    return Mission(
        tab_id=row["tab_id"],
        mission_id=row["mission_id"],
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


def _row_to_memory(row: sqlite3.Row) -> MissionMemory:
    return MissionMemory(
        mission_id=row["mission_id"],
        title=row["title"],
        why=row["why"],
        done_definition=row["done_definition"],
        acceptance_criteria=row["acceptance_criteria"],
        current_plan=row["current_plan"],
        next_step=row["next_step"],
        last_decision=row["last_decision"],
        last_summary=row["last_summary"],
        blocked_on=row["blocked_on"],
        phase=row["phase"],
        confidence=row["confidence"],
        source_kind=row["source_kind"],
        source_ref=row["source_ref"],
        epic_ref=row["epic_ref"],
        issue_ref=row["issue_ref"],
        last_verified_at=row["last_verified_at"],
        claimed_paths=row["claimed_paths"],
        topic=row["topic"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        archived_at=row["archived_at"],
    )


def _row_to_event(row: sqlite3.Row) -> MissionEvent:
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except json.JSONDecodeError:
        metadata = {}
    return MissionEvent(
        id=row["id"],
        mission_id=row["mission_id"],
        ts=row["ts"],
        kind=row["kind"],
        actor=row["actor"],
        summary=row["summary"],
        source_ref=row["source_ref"],
        metadata=metadata,
    )


def _row_to_artifact(row: sqlite3.Row) -> MissionArtifact:
    return MissionArtifact(
        id=row["id"],
        mission_id=row["mission_id"],
        kind=row["kind"],
        path_or_url=row["path_or_url"],
        status=row["status"],
        summary=row["summary"],
        created_at=row["created_at"],
    )


def _row_to_edge(row: sqlite3.Row) -> MissionEdge:
    return MissionEdge(
        id=row["id"],
        from_id=row["from_id"],
        to_id=row["to_id"],
        relation=row["relation"],
        reason=row["reason"],
        created_at=row["created_at"],
    )
