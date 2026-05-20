"""SQLite-backed mission card and mission graph store.

The database lives at ~/.morpheus/morpheus.db. v0.6 stored live iTerm tabs in
`missions`; v0.7 adds durable mission graph tables so a mission can outlive the
tab/session currently attached to it.
"""

from __future__ import annotations

import json
import re
import secrets
import shlex
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

DB_DIR = Path.home() / ".morpheus"
DB_PATH = DB_DIR / "morpheus.db"
CODEX_SESSION_ID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
CODEX_RESUME_LINE_RE = re.compile(
    r"\bcodex\b[^\r\n]*?\bresume\s+(?P<ref>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\b"
)
CODEX_VALUE_OPTIONS = {
    "-c",
    "-C",
    "-m",
    "-p",
    "-s",
    "--approval-policy",
    "--ask-for-approval",
    "--cd",
    "--config",
    "--cwd",
    "--model",
    "--model-provider",
    "--output-schema",
    "--profile",
    "--sandbox",
}
CLAUDE_VALUE_OPTIONS = {
    "--add-dir",
    "--append-system-prompt",
    "--model",
    "--mcp-config",
    "--permission-prompt-tool",
    "--resume",
    "--session-id",
}


@dataclass
class Mission:
    tab_id: str
    mission_id: str = ""
    tenant_id: str = ""
    project_root: str = ""
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
    tenant_id: str = ""
    project_root: str = ""
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
    agent_kind: str = ""
    resume_ref: str = ""
    resume_command: str = ""
    resume_confidence: str = ""
    last_tab_id: str = ""
    closed_at: float = 0.0
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
class ProjectTenant:
    tenant_id: str
    name: str
    root_path: str
    root_kind: str = "cwd"
    created_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)
    archived_at: Optional[float] = None


@dataclass
class ProjectTenantUsage:
    tenant_id: str
    live_sessions: int = 0
    memories: int = 0
    active_memories: int = 0
    archived_memories: int = 0
    events: int = 0
    artifacts: int = 0
    edges: int = 0
    notes: int = 0
    loops: int = 0
    loop_runs: int = 0

    @property
    def graph_rows(self) -> int:
        return (
            self.live_sessions
            + self.memories
            + self.events
            + self.artifacts
            + self.edges
            + self.notes
            + self.loops
            + self.loop_runs
        )

    @property
    def is_empty(self) -> bool:
        return self.graph_rows == 0


@dataclass
class ProjectCleanupResult:
    tenant_id: str
    name: str = ""
    root_path: str = ""
    deleted: dict[str, int] = field(default_factory=dict)
    blocked_reason: str = ""

    @property
    def total_deleted(self) -> int:
        return sum(self.deleted.values())


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
    tenant_id: str = ""
    project_root: str = ""
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
CREATE TABLE IF NOT EXISTS project_tenants (
    tenant_id    TEXT PRIMARY KEY,
    name         TEXT NOT NULL DEFAULT '',
    root_path    TEXT NOT NULL DEFAULT '',
    root_kind    TEXT NOT NULL DEFAULT 'cwd',
    created_at   REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    archived_at  REAL
);

CREATE UNIQUE INDEX IF NOT EXISTS project_tenants_root_idx ON project_tenants(root_path);
CREATE INDEX IF NOT EXISTS project_tenants_seen_idx ON project_tenants(last_seen_at DESC);

CREATE TABLE IF NOT EXISTS missions (
    tab_id            TEXT PRIMARY KEY,
    mission_id        TEXT NOT NULL DEFAULT '',
    tenant_id         TEXT NOT NULL DEFAULT '',
    project_root      TEXT NOT NULL DEFAULT '',
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
    tenant_id           TEXT NOT NULL DEFAULT '',
    project_root        TEXT NOT NULL DEFAULT '',
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
    agent_kind          TEXT NOT NULL DEFAULT '',
    resume_ref          TEXT NOT NULL DEFAULT '',
    resume_command      TEXT NOT NULL DEFAULT '',
    resume_confidence   TEXT NOT NULL DEFAULT '',
    last_tab_id         TEXT NOT NULL DEFAULT '',
    closed_at           REAL NOT NULL DEFAULT 0,
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
    tenant_id         TEXT NOT NULL DEFAULT '',
    project_root      TEXT NOT NULL DEFAULT '',
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
    _ensure_column(conn, "missions", "tenant_id", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "missions", "project_root", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "mission_memory", "tenant_id", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "mission_memory", "project_root", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "mission_memory", "agent_kind", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "mission_memory", "resume_ref", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "mission_memory", "resume_command", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "mission_memory", "resume_confidence", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "mission_memory", "last_tab_id", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "mission_memory", "closed_at", "REAL NOT NULL DEFAULT 0")
    _ensure_column(conn, "prompt_loops", "tenant_id", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "prompt_loops", "project_root", "TEXT NOT NULL DEFAULT ''")
    conn.execute("CREATE INDEX IF NOT EXISTS missions_mission_id_idx ON missions(mission_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS missions_tenant_idx ON missions(tenant_id, updated_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS missions_project_root_idx ON missions(project_root)")
    conn.execute("CREATE INDEX IF NOT EXISTS mission_memory_tenant_idx ON mission_memory(tenant_id, updated_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS mission_memory_project_root_idx ON mission_memory(project_root)")
    conn.execute("CREATE INDEX IF NOT EXISTS prompt_loops_tenant_idx ON prompt_loops(tenant_id, next_run_at)")


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _new_mission_id(now: Optional[float] = None) -> str:
    ts = time.strftime("%Y%m%d%H%M%S", time.localtime(now or time.time()))
    return f"m_{ts}_{secrets.token_hex(4)}"


def _infer_agent_kind(command: str) -> str:
    try:
        parts = shlex.split(command or "")
    except ValueError:
        parts = (command or "").split()
    for part in parts:
        exe = Path(part).name.lower()
        if exe in {"codex", "claude", "gemini"}:
            return exe
    return ""


def _command_parts(command: str) -> list[str]:
    try:
        return shlex.split(command or "")
    except ValueError:
        return (command or "").split()


def _agent_command_parts(command: str, agent_kind: str) -> list[str]:
    parts = _command_parts(command)
    for idx, part in enumerate(parts):
        if Path(part).name.lower() == agent_kind:
            return parts[idx:]
    return parts


def _option_value(parts: list[str], *names: str) -> str:
    for idx, part in enumerate(parts):
        for name in names:
            if part == name and idx + 1 < len(parts):
                return parts[idx + 1]
            if part.startswith(f"{name}="):
                return part.split("=", 1)[1]
    return ""


def _resume_ref_from_command(command: str, agent_kind: str) -> tuple[str, str]:
    parts = _agent_command_parts(command, agent_kind)
    if not parts:
        return "", "unavailable"
    if agent_kind == "codex":
        if "resume" in parts:
            idx = parts.index("resume")
            for value in parts[idx + 1:]:
                if value == "--last":
                    return "", "fallback"
                if value.startswith("-"):
                    continue
                if CODEX_SESSION_ID_RE.fullmatch(value):
                    return value, "exact"
                return "", "fallback"
        return "", "fallback"
    if agent_kind == "claude":
        ref = _option_value(parts, "--resume", "-r", "--session-id")
        return ref, "exact" if ref else "fallback"
    if agent_kind == "gemini":
        joined = " ".join(parts)
        if "/chat resume" in joined:
            tail = joined.split("/chat resume", 1)[1].strip()
            return tail.split()[0] if tail else "", "exact" if tail else "fallback"
        return "", "fallback"
    return "", "unavailable"


def _with_worktree(command: str, linked_worktree: str) -> str:
    if not linked_worktree:
        return command
    return f"cd {shlex.quote(linked_worktree)} && {command}"


def _leading_agent_options(parts: list[str], value_options: set[str]) -> list[str]:
    if not parts:
        return []
    kept = [parts[0]]
    idx = 1
    while idx < len(parts):
        part = parts[idx]
        if part == "--" or not part.startswith("-"):
            break
        kept.append(part)
        option_name = part.split("=", 1)[0]
        if "=" not in part and option_name in value_options and idx + 1 < len(parts):
            idx += 1
            kept.append(parts[idx])
        idx += 1
    return kept


def _codex_resume_command(command: str, resume_ref: str) -> str:
    parts = _agent_command_parts(command, "codex") or ["codex"]
    kept = _leading_agent_options(parts, CODEX_VALUE_OPTIONS) or ["codex"]
    return shlex.join(kept + ["resume", resume_ref or "--last"])


def _claude_resume_command(command: str, resume_ref: str) -> str:
    parts = _agent_command_parts(command, "claude") or ["claude"]
    kept = _leading_agent_options(parts, CLAUDE_VALUE_OPTIONS) or ["claude"]
    if resume_ref:
        return shlex.join(kept + ["--resume", resume_ref])
    return shlex.join(kept + ["--continue"])


def _codex_resume_ref_from_buffer(buffer: str) -> str:
    found = ""
    for match in CODEX_RESUME_LINE_RE.finditer(buffer or ""):
        found = match.group("ref")
    return found


def _resume_command_for_mission(mission: Mission) -> tuple[str, str, str, str]:
    agent_kind = _infer_agent_kind(mission.cmd)
    resume_ref, confidence = _resume_ref_from_command(mission.cmd, agent_kind)
    if agent_kind == "codex":
        base = _codex_resume_command(mission.cmd, resume_ref)
    elif agent_kind == "claude":
        base = _claude_resume_command(mission.cmd, resume_ref)
    elif agent_kind == "gemini":
        base = "gemini"
    else:
        return "", "", "", "unavailable"
    return agent_kind, resume_ref, _with_worktree(base, mission.linked_worktree), confidence


def _persist_resume_metadata(
    conn: sqlite3.Connection,
    mission: Mission,
    *,
    closed_at: float = 0.0,
) -> None:
    if not mission.mission_id:
        return
    agent_kind, resume_ref, resume_command, confidence = _resume_command_for_mission(mission)
    existing = conn.execute(
        "SELECT resume_confidence FROM mission_memory WHERE mission_id = ?",
        (mission.mission_id,),
    ).fetchone()
    if existing and existing["resume_confidence"] == "exact" and confidence != "exact":
        resume_command = ""
        confidence = ""
    conn.execute(
        """
        UPDATE mission_memory
           SET agent_kind = CASE WHEN ? != '' THEN ? ELSE agent_kind END,
               resume_ref = CASE WHEN ? != '' THEN ? ELSE resume_ref END,
               resume_command = CASE WHEN ? != '' THEN ? ELSE resume_command END,
               resume_confidence = CASE WHEN ? != '' THEN ? ELSE resume_confidence END,
               last_tab_id = CASE WHEN ? != '' THEN ? ELSE last_tab_id END,
               closed_at = CASE WHEN ? > 0 THEN ? ELSE closed_at END,
               updated_at = ?
         WHERE mission_id = ?
        """,
        (
            agent_kind,
            agent_kind,
            resume_ref,
            resume_ref,
            resume_command,
            resume_command,
            confidence,
            confidence,
            mission.tab_id,
            mission.tab_id,
            closed_at,
            closed_at,
            time.time(),
            mission.mission_id,
        ),
    )


def refresh_resume_metadata_from_buffer(mission: Mission, buffer: str) -> bool:
    """Persist exact provider resume metadata found in a live terminal buffer."""
    if not mission.mission_id:
        return False
    agent_kind = _infer_agent_kind(mission.cmd)
    if agent_kind != "codex":
        return False
    resume_ref = _codex_resume_ref_from_buffer(buffer)
    if not resume_ref:
        return False
    resume_command = _with_worktree(
        _codex_resume_command(mission.cmd, resume_ref),
        mission.linked_worktree,
    )
    now = time.time()
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE mission_memory
               SET agent_kind = ?,
                   resume_ref = ?,
                   resume_command = ?,
                   resume_confidence = ?,
                   last_tab_id = CASE WHEN ? != '' THEN ? ELSE last_tab_id END,
                   updated_at = ?
             WHERE mission_id = ?
            """,
            (
                agent_kind,
                resume_ref,
                resume_command,
                "exact",
                mission.tab_id,
                mission.tab_id,
                now,
                mission.mission_id,
            ),
        )
    return cur.rowcount > 0


def new_mission_id(now: Optional[float] = None) -> str:
    return _new_mission_id(now)


def upsert_project_tenant(tenant: ProjectTenant) -> ProjectTenant:
    now = time.time()
    if not tenant.created_at:
        tenant.created_at = now
    tenant.last_seen_at = now
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO project_tenants (
                tenant_id, name, root_path, root_kind, created_at, last_seen_at, archived_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id) DO UPDATE SET
                name         = excluded.name,
                root_path    = excluded.root_path,
                root_kind    = excluded.root_kind,
                last_seen_at = excluded.last_seen_at,
                archived_at  = excluded.archived_at
            """,
            (
                tenant.tenant_id,
                tenant.name,
                tenant.root_path,
                tenant.root_kind,
                tenant.created_at,
                tenant.last_seen_at,
                tenant.archived_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM project_tenants WHERE tenant_id = ?",
            (tenant.tenant_id,),
        ).fetchone()
    return _row_to_project_tenant(row)


def get_project_tenant(tenant_id: str) -> Optional[ProjectTenant]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM project_tenants WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()
    return _row_to_project_tenant(row) if row else None


def all_project_tenants(include_archived: bool = False) -> list[ProjectTenant]:
    query = "SELECT * FROM project_tenants"
    if not include_archived:
        query += " WHERE archived_at IS NULL"
    query += " ORDER BY last_seen_at DESC, name ASC"
    with _connect() as conn:
        rows = conn.execute(query).fetchall()
    return [_row_to_project_tenant(row) for row in rows]


def project_tenant_usage(tenant_id: str) -> ProjectTenantUsage:
    with _connect() as conn:
        return _project_tenant_usage(conn, tenant_id)


def empty_project_tenants(include_archived: bool = False) -> list[ProjectTenant]:
    with _connect() as conn:
        query = "SELECT * FROM project_tenants"
        if not include_archived:
            query += " WHERE archived_at IS NULL"
        query += " ORDER BY last_seen_at DESC, name ASC"
        tenants = [_row_to_project_tenant(row) for row in conn.execute(query).fetchall()]
        return [
            tenant
            for tenant in tenants
            if _project_tenant_usage(conn, tenant.tenant_id).is_empty
        ]


def prune_empty_project_tenant(tenant_id: str) -> ProjectCleanupResult:
    with _connect() as conn:
        tenant = conn.execute(
            "SELECT * FROM project_tenants WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()
        if tenant is None:
            return ProjectCleanupResult(
                tenant_id=tenant_id,
                blocked_reason="project tenant not found",
            )
        project = _row_to_project_tenant(tenant)
        usage = _project_tenant_usage(conn, tenant_id)
        if not usage.is_empty:
            return ProjectCleanupResult(
                tenant_id=project.tenant_id,
                name=project.name,
                root_path=project.root_path,
                blocked_reason="project has related mission graph rows",
            )
        cur = conn.execute("DELETE FROM project_tenants WHERE tenant_id = ?", (tenant_id,))
        return ProjectCleanupResult(
            tenant_id=project.tenant_id,
            name=project.name,
            root_path=project.root_path,
            deleted={"project_tenants": cur.rowcount},
        )


def prune_empty_project_tenants(include_archived: bool = False) -> list[ProjectCleanupResult]:
    results: list[ProjectCleanupResult] = []
    with _connect() as conn:
        query = "SELECT * FROM project_tenants"
        if not include_archived:
            query += " WHERE archived_at IS NULL"
        query += " ORDER BY last_seen_at DESC, name ASC"
        tenants = [_row_to_project_tenant(row) for row in conn.execute(query).fetchall()]
        for tenant in tenants:
            if not _project_tenant_usage(conn, tenant.tenant_id).is_empty:
                continue
            cur = conn.execute(
                "DELETE FROM project_tenants WHERE tenant_id = ?",
                (tenant.tenant_id,),
            )
            results.append(ProjectCleanupResult(
                tenant_id=tenant.tenant_id,
                name=tenant.name,
                root_path=tenant.root_path,
                deleted={"project_tenants": cur.rowcount},
            ))
    return results


def delete_project_tenant(
    tenant_id: str,
    *,
    allow_live: bool = False,
) -> ProjectCleanupResult:
    """Remove a project tenant and all Morpheus-owned graph rows for it."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM project_tenants WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()
        if row is None:
            return ProjectCleanupResult(
                tenant_id=tenant_id,
                blocked_reason="project tenant not found",
            )
        tenant = _row_to_project_tenant(row)
        usage = _project_tenant_usage(conn, tenant_id)
        if usage.live_sessions and not allow_live:
            return ProjectCleanupResult(
                tenant_id=tenant.tenant_id,
                name=tenant.name,
                root_path=tenant.root_path,
                blocked_reason="project still has live session rows",
            )

        mission_ids, tab_ids, session_ids = _project_tenant_related_ids(conn, tenant_id)
        loop_ids = _project_tenant_loop_ids(conn, mission_ids, tab_ids, tenant_id=tenant_id)
        deleted: dict[str, int] = {}

        deleted["prompt_loop_runs"] = _delete_project_loop_runs(
            conn,
            loop_ids,
            mission_ids,
            tab_ids,
        )
        deleted["prompt_loops"] = _delete_where_ids(conn, "prompt_loops", "id", loop_ids)
        deleted["notes"] = _delete_project_notes(conn, tab_ids, session_ids)
        deleted["mission_edges"] = _delete_project_edges(conn, mission_ids)
        deleted["mission_artifacts"] = _delete_where_ids(
            conn,
            "mission_artifacts",
            "mission_id",
            mission_ids,
        )
        deleted["mission_events"] = _delete_where_ids(
            conn,
            "mission_events",
            "mission_id",
            mission_ids,
        )
        deleted["missions"] = _delete_where_ids(conn, "missions", "tab_id", tab_ids)
        deleted["mission_memory"] = _delete_where_ids(
            conn,
            "mission_memory",
            "mission_id",
            mission_ids,
        )
        deleted["action_ledger"] = _delete_project_action_entries(
            conn,
            tenant,
            mission_ids,
            tab_ids,
        )
        deleted["project_tenants"] = conn.execute(
            "DELETE FROM project_tenants WHERE tenant_id = ?",
            (tenant_id,),
        ).rowcount

    return ProjectCleanupResult(
        tenant_id=tenant.tenant_id,
        name=tenant.name,
        root_path=tenant.root_path,
        deleted={key: value for key, value in deleted.items() if value},
    )


def _project_tenant_usage(conn: sqlite3.Connection, tenant_id: str) -> ProjectTenantUsage:
    mission_ids, tab_ids, session_ids = _project_tenant_related_ids(conn, tenant_id)
    loop_ids = _project_tenant_loop_ids(conn, mission_ids, tab_ids, tenant_id=tenant_id)
    return ProjectTenantUsage(
        tenant_id=tenant_id,
        live_sessions=_count(conn, "missions", "tenant_id = ?", (tenant_id,)),
        memories=_count(conn, "mission_memory", "tenant_id = ?", (tenant_id,)),
        active_memories=_count(
            conn,
            "mission_memory",
            "tenant_id = ? AND archived_at IS NULL",
            (tenant_id,),
        ),
        archived_memories=_count(
            conn,
            "mission_memory",
            "tenant_id = ? AND archived_at IS NOT NULL",
            (tenant_id,),
        ),
        events=_count_where_ids(conn, "mission_events", "mission_id", mission_ids),
        artifacts=_count_where_ids(conn, "mission_artifacts", "mission_id", mission_ids),
        edges=_count_project_edges(conn, mission_ids),
        notes=_count_project_notes(conn, tab_ids, session_ids),
        loops=len(loop_ids),
        loop_runs=_count_project_loop_runs(conn, loop_ids, mission_ids, tab_ids),
    )


def _project_tenant_related_ids(
    conn: sqlite3.Connection,
    tenant_id: str,
) -> tuple[list[str], list[str], list[str]]:
    mission_rows = conn.execute(
        "SELECT tab_id, mission_id, session_id FROM missions WHERE tenant_id = ?",
        (tenant_id,),
    ).fetchall()
    memory_rows = conn.execute(
        "SELECT mission_id FROM mission_memory WHERE tenant_id = ?",
        (tenant_id,),
    ).fetchall()
    mission_ids = sorted({
        row["mission_id"]
        for row in [*mission_rows, *memory_rows]
        if row["mission_id"]
    })
    tab_ids = sorted({row["tab_id"] for row in mission_rows if row["tab_id"]})
    session_ids = sorted({row["session_id"] for row in mission_rows if row["session_id"]})
    return mission_ids, tab_ids, session_ids


def _project_tenant_loop_ids(
    conn: sqlite3.Connection,
    mission_ids: list[str],
    tab_ids: list[str],
    *,
    tenant_id: str = "",
) -> list[int]:
    where, params = _or_in_conditions([
        ("tenant_id", [tenant_id] if tenant_id else []),
        ("target_mission_id", mission_ids),
        ("target_tab_id", tab_ids),
    ])
    if not where:
        return []
    rows = conn.execute(
        f"SELECT id FROM prompt_loops WHERE {where}",
        params,
    ).fetchall()
    return sorted({int(row["id"]) for row in rows})


def _count(
    conn: sqlite3.Connection,
    table: str,
    where: str = "",
    params: Iterable[Any] = (),
) -> int:
    query = f"SELECT COUNT(*) AS n FROM {table}"
    if where:
        query += f" WHERE {where}"
    row = conn.execute(query, tuple(params)).fetchone()
    return int(row["n"] or 0)


def _placeholders(values: Iterable[Any]) -> str:
    return ",".join("?" for _ in values)


def _count_where_ids(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    values: list[Any],
) -> int:
    if not values:
        return 0
    placeholders = _placeholders(values)
    return _count(conn, table, f"{column} IN ({placeholders})", values)


def _delete_where_ids(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    values: list[Any],
) -> int:
    if not values:
        return 0
    placeholders = _placeholders(values)
    return conn.execute(
        f"DELETE FROM {table} WHERE {column} IN ({placeholders})",
        values,
    ).rowcount


def _or_in_conditions(pairs: Iterable[tuple[str, list[Any]]]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for column, values in pairs:
        if not values:
            continue
        clauses.append(f"{column} IN ({_placeholders(values)})")
        params.extend(values)
    return " OR ".join(clauses), params


def _count_project_edges(conn: sqlite3.Connection, mission_ids: list[str]) -> int:
    where, params = _or_in_conditions([
        ("from_id", mission_ids),
        ("to_id", mission_ids),
    ])
    if not where:
        return 0
    return _count(conn, "mission_edges", where, params)


def _delete_project_edges(conn: sqlite3.Connection, mission_ids: list[str]) -> int:
    where, params = _or_in_conditions([
        ("from_id", mission_ids),
        ("to_id", mission_ids),
    ])
    if not where:
        return 0
    return conn.execute(f"DELETE FROM mission_edges WHERE {where}", params).rowcount


def _count_project_notes(
    conn: sqlite3.Connection,
    tab_ids: list[str],
    session_ids: list[str],
) -> int:
    where, params = _or_in_conditions([
        ("tab_id", tab_ids),
        ("session_id", session_ids),
    ])
    if not where:
        return 0
    return _count(conn, "notes", where, params)


def _delete_project_notes(
    conn: sqlite3.Connection,
    tab_ids: list[str],
    session_ids: list[str],
) -> int:
    where, params = _or_in_conditions([
        ("tab_id", tab_ids),
        ("session_id", session_ids),
    ])
    if not where:
        return 0
    return conn.execute(f"DELETE FROM notes WHERE {where}", params).rowcount


def _count_project_loop_runs(
    conn: sqlite3.Connection,
    loop_ids: list[int],
    mission_ids: list[str],
    tab_ids: list[str],
) -> int:
    where, params = _or_in_conditions([
        ("loop_id", loop_ids),
        ("target_mission_id", mission_ids),
        ("target_tab_id", tab_ids),
    ])
    if not where:
        return 0
    return _count(conn, "prompt_loop_runs", where, params)


def _delete_project_loop_runs(
    conn: sqlite3.Connection,
    loop_ids: list[int],
    mission_ids: list[str],
    tab_ids: list[str],
) -> int:
    where, params = _or_in_conditions([
        ("loop_id", loop_ids),
        ("target_mission_id", mission_ids),
        ("target_tab_id", tab_ids),
    ])
    if not where:
        return 0
    return conn.execute(f"DELETE FROM prompt_loop_runs WHERE {where}", params).rowcount


def _delete_project_action_entries(
    conn: sqlite3.Connection,
    tenant: ProjectTenant,
    mission_ids: list[str],
    tab_ids: list[str],
) -> int:
    if not _table_exists(conn, "action_ledger"):
        return 0
    deleted = _delete_where_ids(conn, "action_ledger", "tab_id", tab_ids)
    for token in [tenant.tenant_id, tenant.root_path, *mission_ids]:
        if not token:
            continue
        deleted += conn.execute(
            "DELETE FROM action_ledger WHERE details LIKE ?",
            (f"%{token}%",),
        ).rowcount
    return deleted


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


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
            mission_id, tenant_id, project_root, title, source_kind, source_ref,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            mission.mission_id,
            mission.tenant_id,
            mission.project_root,
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
               SET title = ?,
                   tenant_id = CASE WHEN ? != '' AND tenant_id = '' THEN ? ELSE tenant_id END,
                   project_root = CASE WHEN ? != '' AND project_root = '' THEN ? ELSE project_root END,
                   updated_at = ?
             WHERE mission_id = ? AND title = ''
            """,
            (
                title,
                mission.tenant_id,
                mission.tenant_id,
                mission.project_root,
                mission.project_root,
                now,
                mission.mission_id,
            ),
        )
    if mission.tenant_id or mission.project_root:
        conn.execute(
            """
            UPDATE mission_memory
               SET tenant_id = CASE WHEN ? != '' THEN ? ELSE tenant_id END,
                   project_root = CASE WHEN ? != '' THEN ? ELSE project_root END,
                   updated_at = ?
             WHERE mission_id = ?
            """,
            (
                mission.tenant_id,
                mission.tenant_id,
                mission.project_root,
                mission.project_root,
                now,
                mission.mission_id,
            ),
        )


def upsert(mission: Mission) -> None:
    mission.updated_at = time.time()
    with _connect() as conn:
        _ensure_mission_identity(conn, mission)
        conn.execute(
            """
            INSERT INTO missions (
                tab_id, mission_id, tenant_id, project_root, session_id, goal,
                state, last_event, last_event_at, buffer_hash,
                buffer_changed_at, cmd, linked_pr, linked_worktree,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tab_id) DO UPDATE SET
                mission_id        = CASE
                                      WHEN missions.mission_id = '' THEN excluded.mission_id
                                      ELSE missions.mission_id
                                    END,
                tenant_id         = COALESCE(NULLIF(excluded.tenant_id, ''), missions.tenant_id),
                project_root      = COALESCE(NULLIF(excluded.project_root, ''), missions.project_root),
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
                mission.tenant_id,
                mission.project_root,
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
            "SELECT * FROM missions WHERE tab_id = ?",
            (mission.tab_id,),
        ).fetchone()
        if row and row["mission_id"]:
            mission.mission_id = row["mission_id"]
            mission.tenant_id = row["tenant_id"]
            mission.project_root = row["project_root"]
        _ensure_memory_row(conn, mission, mission.updated_at)
        _persist_resume_metadata(conn, mission)


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
    tenant_id: str = "",
    project_root: str = "",
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
                   tenant_id = CASE WHEN ? != '' THEN ? ELSE tenant_id END,
                   project_root = CASE WHEN ? != '' THEN ? ELSE project_root END,
                   updated_at = ?
             WHERE tab_id = ?
            """,
            (
                goal,
                linked_pr,
                linked_worktree,
                tenant_id,
                tenant_id,
                project_root,
                project_root,
                now,
                tab_id,
            ),
        )
        row = conn.execute("SELECT * FROM missions WHERE tab_id = ?", (tab_id,)).fetchone()
        if row is not None:
            mission = _row_to_mission(row)
            if mission.mission_id and (mission.tenant_id or mission.project_root):
                conn.execute(
                    """
                    UPDATE mission_memory
                       SET tenant_id = CASE WHEN ? != '' THEN ? ELSE tenant_id END,
                           project_root = CASE WHEN ? != '' THEN ? ELSE project_root END,
                           updated_at = ?
                     WHERE mission_id = ?
                    """,
                    (
                        mission.tenant_id,
                        mission.tenant_id,
                        mission.project_root,
                        mission.project_root,
                        now,
                        mission.mission_id,
                    ),
                )
            _persist_resume_metadata(conn, mission)
    return cur.rowcount > 0


def all_missions(tenant_id: Optional[str] = None) -> list[Mission]:
    query = "SELECT * FROM missions"
    params: list[Any] = []
    if tenant_id:
        query += " WHERE tenant_id = ?"
        params.append(tenant_id)
    query += " ORDER BY updated_at DESC"
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_mission(r) for r in rows]


def delete(tab_id: str) -> None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM missions WHERE tab_id = ?",
            (tab_id,),
        ).fetchone()
        if row and row["mission_id"]:
            mission = _row_to_mission(row)
            _persist_resume_metadata(conn, mission, closed_at=time.time())
            _archive_mission(conn, row["mission_id"], f"tab {tab_id} deleted", f"tab:{tab_id}")
        conn.execute("DELETE FROM missions WHERE tab_id = ?", (tab_id,))


def reconcile_missing(known_tab_ids: Iterable[str]) -> int:
    """Delete live attachment rows for missing tabs. Durable memory survives."""
    known = list(known_tab_ids)
    with _connect() as conn:
        if not known:
            rows = conn.execute("SELECT * FROM missions").fetchall()
            for row in rows:
                if row["mission_id"]:
                    mission = _row_to_mission(row)
                    _persist_resume_metadata(conn, mission, closed_at=time.time())
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
            f"SELECT * FROM missions WHERE tab_id NOT IN ({placeholders})",
            known,
        ).fetchall()
        for row in rows:
            if row["mission_id"]:
                mission = _row_to_mission(row)
                _persist_resume_metadata(conn, mission, closed_at=time.time())
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
                mission_id, tenant_id, project_root, title, why,
                done_definition, acceptance_criteria, current_plan, next_step,
                last_decision, last_summary, blocked_on, phase, confidence,
                source_kind, source_ref, epic_ref, issue_ref, last_verified_at,
                claimed_paths, topic, agent_kind, resume_ref, resume_command,
                resume_confidence, last_tab_id, closed_at, created_at,
                updated_at, archived_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mission_id) DO UPDATE SET
                tenant_id           = CASE WHEN excluded.tenant_id != '' THEN excluded.tenant_id ELSE mission_memory.tenant_id END,
                project_root        = CASE WHEN excluded.project_root != '' THEN excluded.project_root ELSE mission_memory.project_root END,
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
                agent_kind          = excluded.agent_kind,
                resume_ref          = excluded.resume_ref,
                resume_command      = excluded.resume_command,
                resume_confidence   = excluded.resume_confidence,
                last_tab_id         = excluded.last_tab_id,
                closed_at           = excluded.closed_at,
                updated_at          = excluded.updated_at,
                archived_at         = excluded.archived_at
            """,
            (
                memory.mission_id,
                memory.tenant_id,
                memory.project_root,
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
                memory.agent_kind,
                memory.resume_ref,
                memory.resume_command,
                memory.resume_confidence,
                memory.last_tab_id,
                memory.closed_at,
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


def all_memory(
    include_archived: bool = False,
    tenant_id: Optional[str] = None,
) -> list[MissionMemory]:
    query = "SELECT * FROM mission_memory"
    clauses: list[str] = []
    params: list[Any] = []
    if not include_archived:
        clauses.append("archived_at IS NULL")
    if tenant_id:
        clauses.append("tenant_id = ?")
        params.append(tenant_id)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY updated_at DESC"
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_memory(r) for r in rows]


def archive_memory(mission_id: str, summary: str = "mission archived") -> None:
    with _connect() as conn:
        _archive_mission(conn, mission_id, summary, "")


def dismiss_closed_resume(mission_id: str, summary: str = "closed resume dismissed") -> bool:
    """Hide an archived resumable mission from the closed-session dashboard rows."""
    now = time.time()
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE mission_memory
               SET resume_command = '',
                   resume_confidence = CASE
                                         WHEN resume_confidence = 'dismissed' THEN resume_confidence
                                         ELSE 'dismissed'
                                       END,
                   updated_at = ?
             WHERE mission_id = ?
               AND archived_at IS NOT NULL
               AND resume_command != ''
            """,
            (now, mission_id),
        )
        if cur.rowcount:
            _insert_event(
                conn,
                mission_id,
                kind="archive",
                actor="morpheus",
                summary=summary,
                ts=now,
            )
    return cur.rowcount > 0


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


def graph_counts(tenant_id: Optional[str] = None) -> dict[str, int]:
    mission_filter = " WHERE tenant_id = ?" if tenant_id else ""
    memory_filter = " WHERE tenant_id = ?" if tenant_id else ""
    active_filter = " WHERE archived_at IS NULL"
    archived_filter = " WHERE archived_at IS NOT NULL"
    if tenant_id:
        active_filter += " AND tenant_id = ?"
        archived_filter += " AND tenant_id = ?"
    mission_params: tuple[Any, ...] = (tenant_id,) if tenant_id else ()
    with _connect() as conn:
        return {
            "live_sessions": conn.execute(
                f"SELECT COUNT(*) AS n FROM missions{mission_filter}",
                mission_params,
            ).fetchone()["n"],
            "missions": conn.execute(
                f"SELECT COUNT(*) AS n FROM mission_memory{memory_filter}",
                mission_params,
            ).fetchone()["n"],
            "active_missions": conn.execute(
                f"SELECT COUNT(*) AS n FROM mission_memory{active_filter}",
                mission_params,
            ).fetchone()["n"],
            "archived_missions": conn.execute(
                f"SELECT COUNT(*) AS n FROM mission_memory{archived_filter}",
                mission_params,
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


def recent_notes(limit: int = 20, tenant_id: Optional[str] = None) -> list[Note]:
    with _connect() as conn:
        if tenant_id:
            rows = conn.execute(
                """
                SELECT n.*
                  FROM notes n
                  LEFT JOIN missions m ON m.tab_id = n.tab_id
                 WHERE n.tab_id IS NULL OR m.tenant_id = ?
                 ORDER BY n.created_at DESC
                 LIMIT ?
                """,
                (tenant_id, limit),
            ).fetchall()
        else:
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
    tenant_id: str = "",
    project_root: str = "",
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
                name, prompt, interval_seconds, command, tenant_id, project_root,
                target_mission_id, target_tab_id, status, next_run_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                prompt,
                interval_seconds,
                command,
                tenant_id,
                project_root,
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


def all_loops(include_paused: bool = True, tenant_id: str = "") -> list[PromptLoop]:
    with _connect() as conn:
        params: list[Any] = []
        conditions: list[str] = []
        if not include_paused:
            conditions.append("status = ?")
            params.append("active")
        if tenant_id:
            mission_ids, tab_ids, _session_ids = _project_tenant_related_ids(conn, tenant_id)
            loop_ids = _project_tenant_loop_ids(conn, mission_ids, tab_ids, tenant_id=tenant_id)
            loop_scope, loop_params = _or_in_conditions([
                ("tenant_id", [tenant_id]),
                ("id", loop_ids),
            ])
            legacy_scope = "(tenant_id = '' AND target_mission_id = '' AND target_tab_id IS NULL)"
            if loop_scope:
                conditions.append(f"({loop_scope} OR {legacy_scope})")
                params.extend(loop_params)
            else:
                conditions.append(legacy_scope)
        query = "SELECT * FROM prompt_loops"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY next_run_at ASC, created_at DESC"
        rows = conn.execute(query, tuple(params)).fetchall()
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
        tenant_id=row["tenant_id"],
        project_root=row["project_root"],
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


def _row_to_project_tenant(row: sqlite3.Row) -> ProjectTenant:
    return ProjectTenant(
        tenant_id=row["tenant_id"],
        name=row["name"],
        root_path=row["root_path"],
        root_kind=row["root_kind"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
        archived_at=row["archived_at"],
    )


def _row_to_mission(row: sqlite3.Row) -> Mission:
    return Mission(
        tab_id=row["tab_id"],
        mission_id=row["mission_id"],
        tenant_id=row["tenant_id"],
        project_root=row["project_root"],
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
        tenant_id=row["tenant_id"],
        project_root=row["project_root"],
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
        agent_kind=row["agent_kind"],
        resume_ref=row["resume_ref"],
        resume_command=row["resume_command"],
        resume_confidence=row["resume_confidence"],
        last_tab_id=row["last_tab_id"],
        closed_at=row["closed_at"],
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
