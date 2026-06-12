"""Remote-control surface helpers for ChatGPT Apps and compact devices.

This module intentionally stays transport-neutral. The same functions can back
an MCP Apps server, a web dashboard, a phone shortcut, or a glasses SDK bridge.
The public shape is small, crisp, and avoids raw terminal buffers by default.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

from morpheus import __version__
from morpheus import context as ctx_mod
from morpheus import db, ledger

WIDGET_URI = "ui://morpheus/live-card.html"
VOICE_BODY_LIMIT = 112
VOICE_TITLE_LIMIT = 56

_STATE_RANK = {
    "blocked": 0,
    "crashed": 1,
    "working": 2,
    "idle": 3,
    "finished": 4,
    "unknown": 5,
}
_PRIORITY_RANK = {"urgent": 0, "normal": 1, "low": 2}
_FINISHED_GOAL_STATES = {"done", "failed", "cleared"}
_REMOTE_VISIBLE_FINISHED_GOAL_STATES = {"done", "cleared"}
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_OUTPUT_NOISE_PREFIXES = (
    "Last login:",
    "OpenAI Codex",
    "Update available!",
    "Run brew upgrade",
    "See full release notes:",
    "https://github.com/openai/codex/releases/latest",
    "model:",
    "directory:",
    "permissions:",
    "Tip:",
    "gpt-",
    "cd ",
)
_BOX_ONLY_RE = re.compile(r"^[\s─━═┄┈┉╴╶╾╼\-_=]{8,}$")


@dataclass(frozen=True)
class AttentionCard:
    id: str
    priority: str
    kind: str
    title: str
    body: str
    actions: list[str]
    source: dict[str, Any]
    created_at: float
    ttl_seconds: int = 300

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "priority": self.priority,
            "kind": self.kind,
            "title": self.title,
            "body": self.body,
            "actions": list(self.actions),
            "source": dict(self.source),
            "created_at": self.created_at,
            "ttl_seconds": self.ttl_seconds,
        }


def _clean_line(value: object) -> str:
    return " ".join(str(value or "").replace("\n", " ").split())


def _shorten(value: object, limit: int) -> str:
    text = _clean_line(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _clean_terminal_line(value: object) -> str:
    cleaned = _ANSI_RE.sub("", str(value or ""))
    cleaned = _CONTROL_RE.sub("", cleaned)
    cleaned = cleaned.replace("\t", "    ")
    return cleaned.strip()


def clean_terminal_output(buffer: str, *, line_limit: int = 10, char_limit: int = 1400) -> dict[str, Any]:
    """Return a compact, display-safe tail of terminal output."""
    lines: list[str] = []
    for raw in str(buffer or "").splitlines():
        line = _clean_terminal_line(raw)
        if not line:
            continue
        if any(line.startswith(prefix) for prefix in _OUTPUT_NOISE_PREFIXES):
            continue
        if _BOX_ONLY_RE.match(line):
            continue
        if line.startswith(("│", "╭", "╰", "╮", "╯")):
            continue
        if "@" in line and " % " in line:
            continue
        if "esc to interrupt" in line or "Working(" in line:
            continue
        if line.startswith(">_") or line.startswith("> ") or line.startswith("›"):
            continue
        lines.append(line)
    tail = lines[-max(1, line_limit):]
    text = "\n".join(tail).strip()
    if len(text) > char_limit:
        text = text[-char_limit:].lstrip()
        tail = text.splitlines()
    return {
        "text": text,
        "lines": tail,
        "line_count": len(tail),
        "char_count": len(text),
    }


def _tab_ref(tab_id: str) -> str:
    return (tab_id or "?").split("-")[0][:12] or "?"


def _mission_ref(mission_id: str) -> str:
    return (mission_id or "?")[:12] or "?"


def _state_counts(missions: list[db.Mission]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for mission in missions:
        counts[mission.state] = counts.get(mission.state, 0) + 1
    return dict(sorted(counts.items()))


def _memory_by_mission(tenant_id: Optional[str] = None) -> dict[str, db.MissionMemory]:
    return {
        memory.mission_id: memory
        for memory in db.all_memory(include_archived=True, tenant_id=tenant_id)
    }


def _mission_title(mission: db.Mission, memory: Optional[db.MissionMemory]) -> str:
    return _shorten(
        (memory.title if memory and memory.title else "") or mission.goal or mission.tab_id,
        VOICE_TITLE_LIMIT,
    )


def _mission_body(mission: db.Mission, memory: Optional[db.MissionMemory]) -> str:
    if memory and memory.blocked_on:
        return _shorten(memory.blocked_on, VOICE_BODY_LIMIT)
    if mission.last_event:
        return _shorten(mission.last_event, VOICE_BODY_LIMIT)
    if memory and memory.next_step:
        return _shorten(memory.next_step, VOICE_BODY_LIMIT)
    return "No recent detail."


def _goal_title(goal: db.GoalRun) -> str:
    return _shorten(goal.objective or goal.source_ref or goal.goal_id, VOICE_TITLE_LIMIT)


def _goal_body(goal: db.GoalRun) -> str:
    if goal.status == "paused" and goal.last_judge_reason:
        return _shorten(goal.last_judge_reason, VOICE_BODY_LIMIT)
    if goal.status == "active" and goal.max_turns:
        remaining = max(0, goal.max_turns - goal.turns_used)
        return f"{remaining} controller turn(s) left; {goal.active_workers}/{goal.max_workers} workers active."
    if goal.last_judge_reason:
        return _shorten(goal.last_judge_reason, VOICE_BODY_LIMIT)
    return f"Status: {goal.status}."


def _card_sort_key(card: AttentionCard) -> tuple[int, float, str]:
    return (_PRIORITY_RANK.get(card.priority, 9), -card.created_at, card.id)


def attention_cards(
    *,
    limit: int = 8,
    tenant_id: Optional[str] = None,
    now: Optional[float] = None,
) -> list[dict[str, Any]]:
    """Return the cards Morpheus should consider pushing to a remote surface."""
    ts = time.time() if now is None else now
    cards: list[AttentionCard] = []
    missions = db.all_missions(tenant_id=tenant_id)
    memories = _memory_by_mission(tenant_id=tenant_id)

    sorted_missions = sorted(
        missions,
        key=lambda m: (_STATE_RANK.get(m.state, 9), -m.updated_at, m.tab_id),
    )
    for mission in sorted_missions:
        memory = memories.get(mission.mission_id)
        source = {
            "type": "session",
            "tab_ref": _tab_ref(mission.tab_id),
            "mission_ref": _mission_ref(mission.mission_id),
            "state": mission.state,
        }
        if mission.state in {"blocked", "crashed"}:
            cards.append(
                AttentionCard(
                    id=f"session:{source['tab_ref']}:{mission.state}",
                    priority="urgent",
                    kind=f"session_{mission.state}",
                    title=f"{mission.state.title()}: {_mission_title(mission, memory)}",
                    body=_mission_body(mission, memory),
                    actions=["brief_session", "send_operator_note"],
                    source=source,
                    created_at=mission.last_event_at or mission.updated_at or ts,
                    ttl_seconds=120,
                )
            )
        elif mission.state == "idle" and ts - (mission.updated_at or 0.0) <= 900:
            cards.append(
                AttentionCard(
                    id=f"session:{source['tab_ref']}:ready",
                    priority="normal",
                    kind="session_ready",
                    title=f"Ready: {_mission_title(mission, memory)}",
                    body=_mission_body(mission, memory),
                    actions=["brief_session", "send_operator_note"],
                    source=source,
                    created_at=mission.updated_at or ts,
                    ttl_seconds=300,
                )
            )

    goals = _remote_goal_runs(tenant_id=tenant_id)
    for goal in goals:
        source = {
            "type": "goal",
            "goal_ref": _mission_ref(goal.goal_id),
            "status": goal.status,
        }
        if goal.status in {"paused", "failed"}:
            cards.append(
                AttentionCard(
                    id=f"goal:{source['goal_ref']}:{goal.status}",
                    priority="urgent" if goal.status == "failed" else "normal",
                    kind=f"goal_{goal.status}",
                    title=f"Goal {goal.status}: {_goal_title(goal)}",
                    body=_goal_body(goal),
                    actions=["brief_goal", "send_operator_note"],
                    source=source,
                    created_at=goal.updated_at or ts,
                    ttl_seconds=300,
                )
            )
        elif goal.status == "active" and goal.max_turns and goal.max_turns - goal.turns_used <= 2:
            cards.append(
                AttentionCard(
                    id=f"goal:{source['goal_ref']}:budget",
                    priority="normal",
                    kind="goal_budget",
                    title=f"Budget tight: {_goal_title(goal)}",
                    body=_goal_body(goal),
                    actions=["brief_goal", "send_operator_note"],
                    source=source,
                    created_at=goal.updated_at or ts,
                    ttl_seconds=300,
                )
            )

    for note in db.recent_notes(limit=10, tenant_id=tenant_id):
        if note.kind not in {"broadcast", "goal"}:
            continue
        cards.append(
            AttentionCard(
                id=f"note:{note.id}",
                priority="low",
                kind=f"note_{note.kind}",
                title=f"{note.kind.title()}: {_shorten(note.text, VOICE_TITLE_LIMIT)}",
                body=_shorten(note.text, VOICE_BODY_LIMIT),
                actions=["acknowledge", "send_operator_note"],
                source={"type": "note", "note_id": note.id, "tab_ref": _tab_ref(note.tab_id or "")},
                created_at=note.created_at,
                ttl_seconds=600,
            )
        )

    return [card.to_dict() for card in sorted(cards, key=_card_sort_key)[: max(0, limit)]]


def _session_rows(missions: list[db.Mission], memories: dict[str, db.MissionMemory]) -> list[dict[str, Any]]:
    ordered = sorted(
        missions,
        key=lambda m: (_STATE_RANK.get(m.state, 9), -m.updated_at, m.tab_id),
    )
    rows: list[dict[str, Any]] = []
    now = time.time()
    for mission in ordered:
        memory = memories.get(mission.mission_id)
        rows.append(
            {
                "tab_ref": _tab_ref(mission.tab_id),
                "mission_ref": _mission_ref(mission.mission_id),
                "tenant_id": mission.tenant_id,
                "project_root": _shorten(mission.project_root, 120),
                "state": mission.state,
                "goal": _mission_title(mission, memory),
                "phase": memory.phase if memory else "unknown",
                "next_step": _shorten(memory.next_step if memory else "", 96),
                "blocked_on": _shorten(memory.blocked_on if memory else "", 96),
                "last_event": _shorten(mission.last_event, 96),
                "age_secs": max(0, int(now - (mission.buffer_changed_at or mission.updated_at or now))),
                "linked_pr": mission.linked_pr,
                # Exact codex thread id for `codex ... resume <id>` tabs so the
                # G2 bridge can re-attach mirror tabs after a bridge restart.
                "resume_ref": db.codex_resume_ref_from_command(mission.cmd),
            }
        )
    return rows


def _goal_rows(goals: list[db.GoalRun]) -> list[dict[str, Any]]:
    return [
        {
            "goal_ref": _mission_ref(goal.goal_id),
            "status": goal.status,
            "objective": _goal_title(goal),
            "turns": f"{goal.turns_used}/{goal.max_turns}",
            "workers": f"{goal.active_workers}/{goal.max_workers}",
            "autonomy_level": goal.autonomy_level,
            "last_reason": _shorten(goal.last_judge_reason, 96),
        }
        for goal in goals
    ]


def _remote_goal_runs(tenant_id: Optional[str] = None) -> list[db.GoalRun]:
    return [
        goal
        for goal in db.all_goal_runs(include_finished=True, tenant_id=tenant_id)
        if goal.status not in _REMOTE_VISIBLE_FINISHED_GOAL_STATES
    ]


def _voice_summary(counts: dict[str, int], cards: list[dict[str, Any]], active_goal_count: int) -> str:
    session_total = sum(counts.values())
    parts = [f"{session_total} sessions"]
    for key in ("blocked", "crashed", "working", "idle", "finished"):
        if counts.get(key):
            parts.append(f"{counts[key]} {key}")
    if active_goal_count:
        parts.append(f"{active_goal_count} active goals")
    summary = ". ".join(parts) + "."
    if cards:
        top = cards[0]
        summary += f" Top: {top['title']}. {top['body']}"
    return _shorten(summary, 240)


def fleet_snapshot(*, limit: int = 8, tenant_id: Optional[str] = None) -> dict[str, Any]:
    """Return a compact model/device-friendly fleet snapshot."""
    missions = db.all_missions(tenant_id=tenant_id)
    memories = _memory_by_mission(tenant_id=tenant_id)
    goals = _remote_goal_runs(tenant_id=tenant_id)
    counts = _state_counts(missions)
    cards = attention_cards(limit=limit, tenant_id=tenant_id)
    return {
        "generated_at": time.time(),
        "version": __version__,
        "summary": _voice_summary(counts, cards, len(goals)),
        "counts": counts,
        "active_goal_count": len([goal for goal in goals if goal.status not in _FINISHED_GOAL_STATES]),
        "cards": cards,
        "sessions": _session_rows(missions, memories),
        "goals": _goal_rows(goals),
        "policy": {
            "mode": "operator_confirmed_remote_control",
            "raw_terminal_buffers": False,
            "external_side_effects": "not_exposed",
            "destructive_actions": "not_exposed",
        },
    }


def _find_mission(ref: str, *, tenant_id: Optional[str] = None) -> tuple[Optional[db.Mission], str]:
    needle = (ref or "").strip()
    if not needle:
        return None, "missing session or mission reference"
    matches = [
        mission
        for mission in db.all_missions(tenant_id=tenant_id)
        if mission.tab_id == needle
        or mission.mission_id == needle
        or mission.tab_id.startswith(needle)
        or mission.mission_id.startswith(needle)
    ]
    if not matches:
        return None, f"no session or mission matching '{needle}'"
    exact = [
        mission
        for mission in matches
        if mission.tab_id == needle or mission.mission_id == needle
    ]
    if len(exact) == 1:
        return exact[0], ""
    if len(matches) > 1:
        refs = ", ".join(_tab_ref(mission.tab_id) for mission in matches[:5])
        return None, f"ambiguous reference '{needle}' ({refs})"
    return matches[0], ""


def session_brief(
    ref: str,
    *,
    tenant_id: Optional[str] = None,
    event_limit: int = 5,
) -> dict[str, Any]:
    """Return a targeted brief without raw terminal buffers."""
    mission, error = _find_mission(ref, tenant_id=tenant_id)
    if mission is None:
        return {"found": False, "error": error}

    memory = db.get_memory(mission.mission_id) if mission.mission_id else None
    events = db.recent_events(mission.mission_id, limit=event_limit) if mission.mission_id else []
    notes = db.notes_for_tab(mission.tab_id, limit=5)
    return {
        "found": True,
        "tab_ref": _tab_ref(mission.tab_id),
        "mission_ref": _mission_ref(mission.mission_id),
        "state": mission.state,
        "goal": _mission_title(mission, memory),
        "last_event": _shorten(mission.last_event, 160),
        "linked_pr": mission.linked_pr,
        "linked_worktree": _shorten(mission.linked_worktree, 120),
        "memory": (
            {
                "title": _shorten(memory.title, 120),
                "why": _shorten(memory.why, 240),
                "phase": memory.phase,
                "next_step": _shorten(memory.next_step, 160),
                "blocked_on": _shorten(memory.blocked_on, 160),
                "confidence": memory.confidence,
            }
            if memory
            else None
        ),
        "recent_events": [
            {
                "kind": event.kind,
                "actor": event.actor,
                "summary": _shorten(event.summary, 160),
                "source_ref": _shorten(event.source_ref, 120),
                "ts": event.ts,
            }
            for event in events
        ],
        "recent_notes": [
            {
                "id": note.id,
                "kind": note.kind,
                "text": _shorten(note.text, 160),
                "created_at": note.created_at,
            }
            for note in notes
        ],
        "policy": {"raw_terminal_buffers": False},
    }


def stage_operator_note(
    text: str,
    *,
    target_ref: Optional[str] = None,
    kind: str = "note",
    tenant_id: Optional[str] = None,
) -> dict[str, Any]:
    """Record a bounded operator note from a remote app/tool call."""
    note_text = _shorten(text, 240)
    if not note_text:
        return {"ok": False, "error": "text is required"}
    if kind not in {"note", "broadcast", "claim"}:
        return {"ok": False, "error": "kind must be note, broadcast, or claim"}

    mission: Optional[db.Mission] = None
    if target_ref:
        mission, error = _find_mission(target_ref, tenant_id=tenant_id)
        if mission is None:
            return {"ok": False, "error": error}

    tab_id = mission.tab_id if mission else None
    note_id = db.add_note(text=note_text, tab_id=tab_id, session_id=None, kind=kind)
    try:
        ctx_mod.write_context_file()
        ctx_mod.write_context_json()
    except Exception:
        pass
    ledger.log_action(
        "remote_operator_note",
        tab_id=tab_id,
        details={"kind": kind, "text": note_text, "target_ref": target_ref or ""},
    )
    return {
        "ok": True,
        "id": note_id,
        "kind": kind,
        "text": note_text,
        "target": (
            {
                "tab_ref": _tab_ref(mission.tab_id),
                "mission_ref": _mission_ref(mission.mission_id),
            }
            if mission
            else None
        ),
    }


def tool_descriptors() -> list[dict[str, Any]]:
    """Return MCP Apps-compatible tool descriptors for a ChatGPT app server."""
    return [
        {
            "name": "get_fleet_snapshot",
            "title": "Get Morpheus snapshot",
            "description": (
                "Return a compact Morpheus fleet snapshot with attention cards. "
                "Use this first for voice or mobile status checks."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 12, "default": 8}
                },
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": True,
                "openWorldHint": False,
                "destructiveHint": False,
            },
            "_meta": {
                "openai/toolInvocation/invoking": "Reading Morpheus...",
                "openai/toolInvocation/invoked": "Morpheus snapshot ready.",
            },
        },
        {
            "name": "get_attention_cards",
            "title": "Get attention cards",
            "description": "Return only the short cards Morpheus may push to remote devices.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 12, "default": 8}
                },
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": True,
                "openWorldHint": False,
                "destructiveHint": False,
            },
            "_meta": {
                "openai/toolInvocation/invoking": "Checking attention cards...",
                "openai/toolInvocation/invoked": "Attention cards ready.",
            },
        },
        {
            "name": "get_session_brief",
            "title": "Get session brief",
            "description": (
                "Return a focused brief for one Morpheus session by tab_ref or mission_ref. "
                "Does not return raw terminal buffers."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "minLength": 1},
                    "event_limit": {"type": "integer", "minimum": 0, "maximum": 12, "default": 5},
                },
                "required": ["ref"],
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": True,
                "openWorldHint": False,
                "destructiveHint": False,
            },
            "_meta": {
                "openai/toolInvocation/invoking": "Briefing session...",
                "openai/toolInvocation/invoked": "Session brief ready.",
            },
        },
        {
            "name": "stage_operator_note",
            "title": "Send operator note",
            "description": (
                "Record a short operator note in Morpheus. This changes Morpheus state but "
                "does not send external messages, publish, push, merge, spawn, or kill."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "minLength": 1, "maxLength": 240},
                    "target_ref": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["note", "broadcast", "claim"],
                        "default": "note",
                    },
                },
                "required": ["text"],
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": False,
                "openWorldHint": False,
                "destructiveHint": False,
            },
            "_meta": {
                "openai/toolInvocation/invoking": "Writing Morpheus note...",
                "openai/toolInvocation/invoked": "Morpheus note written.",
            },
        },
        {
            "name": "render_morpheus_live_card",
            "title": "Render Morpheus live card",
            "description": (
                "Render the compact Morpheus live-card widget from a snapshot. "
                "Call get_fleet_snapshot first, then pass through its structured content."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "counts": {"type": "object"},
                    "cards": {"type": "array", "items": {"type": "object"}},
                    "sessions": {"type": "array", "items": {"type": "object"}},
                    "goals": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["summary", "counts", "cards"],
                "additionalProperties": True,
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "counts": {"type": "object"},
                    "cards": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["summary", "counts", "cards"],
                "additionalProperties": True,
            },
            "annotations": {
                "readOnlyHint": True,
                "openWorldHint": False,
                "destructiveHint": False,
            },
            "_meta": {
                "ui": {"resourceUri": WIDGET_URI},
                "openai/outputTemplate": WIDGET_URI,
                "openai/toolInvocation/invoking": "Rendering Morpheus...",
                "openai/toolInvocation/invoked": "Morpheus rendered.",
            },
        },
    ]


def app_manifest() -> dict[str, Any]:
    """Return a draft manifest for the future ChatGPT Apps/MCP host."""
    return {
        "name": "morpheus",
        "title": "Morpheus Remote",
        "version": __version__,
        "description": "Compact remote control and attention cards for Morpheus.",
        "transport_target": {
            "production_shape": "streamable_http_mcp_server",
            "developer_mode": "Expose /mcp from a local or tunneled HTTPS server.",
        },
        "resources": [
            {
                "uri": WIDGET_URI,
                "mimeType": "text/html;profile=mcp-app",
                "description": "Compact live-card widget for ChatGPT, phone, or glasses surfaces.",
            }
        ],
        "tools": tool_descriptors(),
        "control_policy": {
            "read_tools": ["get_fleet_snapshot", "get_attention_cards", "get_session_brief"],
            "write_tools": ["stage_operator_note"],
            "not_exposed": ["spawn", "kill", "push", "merge", "approve", "send_external_message"],
            "confirmation": "required by host for all write tools",
        },
    }


def widget_html() -> str:
    """Return the MCP Apps widget template used by render_morpheus_live_card."""
    return """
<div id="root" class="morpheus-card">
  <header>
    <div>
      <strong>Morpheus</strong>
      <span id="summary">Waiting for snapshot...</span>
    </div>
    <button id="refresh" type="button" aria-label="Refresh">Refresh</button>
  </header>
  <main id="cards"></main>
</div>
<style>
  :root {
    color-scheme: light dark;
    font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }
  .morpheus-card {
    border: 1px solid color-mix(in srgb, currentColor 18%, transparent);
    border-radius: 8px;
    padding: 12px;
    background: Canvas;
    color: CanvasText;
    max-width: 720px;
  }
  header {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    align-items: flex-start;
    margin-bottom: 10px;
  }
  strong {
    display: block;
    font-size: 15px;
    line-height: 1.2;
  }
  #summary {
    display: block;
    margin-top: 2px;
    font-size: 12px;
    line-height: 1.35;
    opacity: .78;
  }
  button {
    border: 1px solid color-mix(in srgb, currentColor 22%, transparent);
    border-radius: 6px;
    background: ButtonFace;
    color: ButtonText;
    padding: 6px 9px;
    font: inherit;
    font-size: 12px;
  }
  #cards {
    display: grid;
    gap: 8px;
  }
  .card {
    border-left: 3px solid color-mix(in srgb, currentColor 35%, transparent);
    padding: 7px 8px;
    background: color-mix(in srgb, currentColor 5%, transparent);
    border-radius: 6px;
  }
  .urgent { border-left-color: #c2410c; }
  .normal { border-left-color: #0f766e; }
  .low { border-left-color: #64748b; }
  .title {
    font-size: 13px;
    font-weight: 650;
    line-height: 1.25;
    margin-bottom: 2px;
  }
  .body {
    font-size: 12px;
    line-height: 1.35;
    opacity: .82;
  }
  .meta {
    margin-top: 5px;
    font-size: 11px;
    opacity: .62;
  }
</style>
<script>
  const summaryEl = document.getElementById("summary");
  const cardsEl = document.getElementById("cards");
  const refreshButton = document.getElementById("refresh");

  function escapeText(value) {
    return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
    }[ch]));
  }

  function render(data) {
    const snapshot = data || window.openai?.toolOutput || {};
    summaryEl.textContent = snapshot.summary || "No active Morpheus snapshot.";
    const cards = Array.isArray(snapshot.cards) ? snapshot.cards : [];
    if (!cards.length) {
      cardsEl.innerHTML = '<div class="card low"><div class="title">Clear</div><div class="body">No urgent cards.</div></div>';
      return;
    }
    cardsEl.innerHTML = cards.slice(0, 6).map((card) => {
      const source = card.source || {};
      const ref = source.tab_ref || source.goal_ref || source.note_id || "";
      return `<section class="card ${escapeText(card.priority || "low")}">
        <div class="title">${escapeText(card.title)}</div>
        <div class="body">${escapeText(card.body)}</div>
        <div class="meta">${escapeText(card.kind)} ${escapeText(ref)}</div>
      </section>`;
    }).join("");
  }

  async function refresh() {
    const result = await window.openai?.callTool?.("get_fleet_snapshot", { limit: 6 });
    if (result?.structuredContent) render(result.structuredContent);
  }

  refreshButton.onclick = refresh;
  render(window.openai?.toolOutput);

  window.addEventListener("openai:set_globals", (event) => {
    render(event.detail?.globals?.toolOutput ?? window.openai?.toolOutput);
  }, { passive: true });

  window.addEventListener("message", (event) => {
    if (event.source !== window.parent) return;
    const message = event.data;
    if (!message || message.jsonrpc !== "2.0") return;
    if (message.method !== "ui/notifications/tool-result") return;
    render(message.params?.structuredContent);
  }, { passive: true });
</script>
""".strip()


def tool_result(data: dict[str, Any], *, text: Optional[str] = None) -> dict[str, Any]:
    """Wrap data in the Apps SDK/MCP tool-result shape."""
    return {
        "structuredContent": data,
        "content": [{"type": "text", "text": text or data.get("summary", "Morpheus ready.")}],
    }


def render_widget_result(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return a render-tool result for render_morpheus_live_card."""
    summary = _shorten(snapshot.get("summary", "Morpheus snapshot."), 240)
    return tool_result(snapshot, text=summary)


def html_preview(snapshot: Optional[dict[str, Any]] = None) -> str:
    """Standalone preview page for local testing outside ChatGPT."""
    payload = json.dumps(snapshot or fleet_snapshot(limit=6), ensure_ascii=False)
    payload = payload.replace("<", "\\u003c").replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Morpheus Live Card</title>
</head>
<body>
{widget_html()}
<script>
  window.openai = window.openai || {{}};
  window.openai.toolOutput = {payload};
  window.dispatchEvent(new CustomEvent("openai:set_globals", {{
    detail: {{ globals: {{ toolOutput: window.openai.toolOutput }} }}
  }}));
</script>
</body>
</html>
"""
