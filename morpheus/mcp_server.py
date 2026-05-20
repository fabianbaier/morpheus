"""MCP server exposing morpheus state + a tightly-scoped action surface
to Claude Code / Codex CLI / any MCP client.

Runs as `morpheus mcp serve` (stdio JSON-RPC). To wire into Claude Code,
add this to your `~/.claude.json` or project `.mcp.json`:

    {
      "mcpServers": {
        "morpheus": {
          "command": "morpheus",
          "args": ["mcp", "serve"]
        }
      }
    }

Tools exposed (all read-only OR explicitly state-mutating notes — no
iTerm spawn/kill from MCP yet; that lives behind the CLI for v0.6):
  - list_sessions()
  - get_session(tab_prefix)
  - list_missions(include_archived)
  - get_mission(ref)
  - update_mission(ref, ...)
  - add_mission_event(ref, ...)
  - add_mission_artifact(ref, ...)
  - link_missions(from_ref, to_ref, ...)
  - get_context()
  - get_context_short()
  - post_note(text, tab_id, kind)
  - claim_path(path, tab_id)
  - daily_spend()
  - recent_actions(limit)
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from morpheus import context as ctx_mod
from morpheus import db, ledger, mission_graph as graph_mod, prd_runs

mcp = FastMCP("morpheus")


@mcp.tool()
def list_sessions() -> list[dict]:
    """List every morpheus mission session currently tracked.

    Returns each session's tab_id, goal, state (working/idle/blocked/
    finished/crashed), last_event, age in seconds, and any linked PR or
    worktree. Use this to know what other agents are doing before you
    start parallel work.
    """
    now = time.time()
    out: list[dict] = []
    for m in db.all_missions():
        out.append({
            "tab_id": m.tab_id,
            "mission_id": m.mission_id,
            "tab_short": (m.tab_id or "").split("-")[0],
            "goal": m.goal,
            "state": m.state,
            "last_event": m.last_event,
            "age_secs": max(0, now - m.buffer_changed_at),
            "linked_pr": m.linked_pr,
            "linked_worktree": m.linked_worktree,
            "cmd": m.cmd,
        })
    return out


@mcp.tool()
def get_session(tab_prefix: str) -> dict:
    """Get details on one session by tab_id prefix (e.g., 't1a2b3' or
    the full tab id). Returns the mission card plus its recent notes.
    """
    missions = db.all_missions()
    match = next(
        (m for m in missions if m.tab_id == tab_prefix or m.tab_id.startswith(tab_prefix)),
        None,
    )
    if match is None:
        return {"found": False, "error": f"no tab matching '{tab_prefix}'"}
    notes = db.notes_for_tab(match.tab_id, limit=10)
    memory = db.get_memory(match.mission_id) if match.mission_id else None
    events = db.recent_events(match.mission_id, limit=10) if match.mission_id else []
    artifacts = db.artifacts_for_mission(match.mission_id, limit=10) if match.mission_id else []
    return {
        "found": True,
        "tab_id": match.tab_id,
        "mission_id": match.mission_id,
        "goal": match.goal,
        "state": match.state,
        "last_event": match.last_event,
        "cmd": match.cmd,
        "linked_pr": match.linked_pr,
        "linked_worktree": match.linked_worktree,
        "age_secs": max(0, time.time() - match.buffer_changed_at),
        "memory": (
            {
                "title": memory.title,
                "why": memory.why,
                "done_definition": memory.done_definition,
                "acceptance_criteria": memory.acceptance_criteria,
                "phase": memory.phase,
                "next_step": memory.next_step,
                "blocked_on": memory.blocked_on,
                "confidence": memory.confidence,
                "source_kind": memory.source_kind,
                "source_ref": memory.source_ref,
            }
            if memory else None
        ),
        "events": [
            {
                "id": e.id, "kind": e.kind, "actor": e.actor,
                "summary": e.summary, "source_ref": e.source_ref, "ts": e.ts,
            }
            for e in events
        ],
        "artifacts": [
            {
                "id": a.id, "kind": a.kind, "path_or_url": a.path_or_url,
                "status": a.status, "summary": a.summary, "ts": a.created_at,
            }
            for a in artifacts
        ],
        "notes": [
            {"id": n.id, "text": n.text, "kind": n.kind, "ts": n.created_at}
            for n in notes
        ],
    }


@mcp.tool()
def list_missions(include_archived: bool = False) -> list[dict]:
    """List durable mission graph nodes.

    This includes archived mission memory when requested. Use this when a live
    tab may have disappeared but the durable mission still matters.
    """
    live_by_mission: dict[str, list[db.Mission]] = {}
    for mission in db.all_missions():
        live_by_mission.setdefault(mission.mission_id, []).append(mission)
    return [
        {
            **_memory_dict(memory),
            "short_id": graph_mod.short_id(memory.mission_id),
            "live": [_live_dict(m) for m in live_by_mission.get(memory.mission_id, [])],
        }
        for memory in db.all_memory(include_archived=include_archived)
    ]


@mcp.tool()
def get_mission(
    ref: str,
    event_limit: int = 10,
    artifact_limit: int = 10,
    edge_limit: int = 10,
) -> dict:
    """Get a durable mission graph card by mission id/prefix or tab id/prefix."""
    resolved = graph_mod.resolve(ref)
    if resolved is None:
        return {"found": False, "error": f"no mission matching '{ref}'"}

    return {
        "found": True,
        "mission_id": resolved.mission_id,
        "short_id": graph_mod.short_id(resolved.mission_id),
        "memory": _memory_dict(resolved.memory),
        "live": [_live_dict(m) for m in resolved.live],
        "events": [
            _event_dict(event)
            for event in db.recent_events(resolved.mission_id, limit=max(0, event_limit))
        ],
        "artifacts": [
            _artifact_dict(artifact)
            for artifact in db.artifacts_for_mission(resolved.mission_id, limit=max(0, artifact_limit))
        ],
        "edges": [
            _edge_dict(edge)
            for edge in db.edges_for_id(resolved.mission_id, limit=max(0, edge_limit))
        ],
    }


@mcp.tool()
def update_mission(
    ref: str,
    title: Optional[str] = None,
    why: Optional[str] = None,
    done_definition: Optional[str] = None,
    acceptance_criteria: Optional[str] = None,
    current_plan: Optional[str] = None,
    next_step: Optional[str] = None,
    last_decision: Optional[str] = None,
    last_summary: Optional[str] = None,
    blocked_on: Optional[str] = None,
    phase: Optional[str] = None,
    confidence: Optional[float] = None,
    source_kind: Optional[str] = None,
    source_ref: Optional[str] = None,
    epic_ref: Optional[str] = None,
    issue_ref: Optional[str] = None,
    claimed_paths_json: Optional[str] = None,
    topic: Optional[str] = None,
) -> dict:
    """Update safe mission-memory fields.

    `claimed_paths_json` may be a JSON list of path strings. Spawn, kill, close,
    push, merge, and external-message actions are intentionally not exposed.
    """
    resolved = graph_mod.resolve(ref)
    if resolved is None:
        return {"ok": False, "found": False, "error": f"no mission matching '{ref}'"}

    memory = resolved.memory
    updates: dict[str, Any] = {}
    for field, value in {
        "title": title,
        "why": why,
        "done_definition": done_definition,
        "acceptance_criteria": acceptance_criteria,
        "current_plan": current_plan,
        "next_step": next_step,
        "last_decision": last_decision,
        "last_summary": last_summary,
        "blocked_on": blocked_on,
        "phase": phase,
        "confidence": confidence,
        "source_kind": source_kind,
        "source_ref": source_ref,
        "epic_ref": epic_ref,
        "issue_ref": issue_ref,
        "topic": topic,
    }.items():
        if value is not None:
            setattr(memory, field, value)
            updates[field] = value

    if claimed_paths_json is not None:
        try:
            paths = json.loads(claimed_paths_json)
        except json.JSONDecodeError as e:
            return {"ok": False, "found": True, "error": f"claimed_paths_json is not JSON: {e}"}
        if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
            return {"ok": False, "found": True, "error": "claimed_paths_json must be a JSON list of strings"}
        memory.claimed_paths = json.dumps(paths)
        updates["claimed_paths"] = paths

    if not updates:
        return {"ok": True, "found": True, "mission_id": resolved.mission_id, "updated": []}

    db.upsert_memory(memory)
    event_id = db.add_event(
        resolved.mission_id,
        kind="mission_update",
        actor="mcp",
        summary=f"MCP updated mission fields: {', '.join(sorted(updates))}",
        source_ref="mcp:update_mission",
        metadata={"fields": sorted(updates)},
    )
    _log_action("mcp_update_mission", resolved, {"fields": sorted(updates)})
    _refresh_after_graph_write(resolved.mission_id)
    refreshed = graph_mod.resolve(resolved.mission_id)
    return {
        "ok": True,
        "found": True,
        "mission_id": resolved.mission_id,
        "event_id": event_id,
        "updated": sorted(updates),
        "memory": _memory_dict(refreshed.memory if refreshed else memory),
    }


@mcp.tool()
def add_mission_event(
    ref: str,
    summary: str,
    kind: str = "decision",
    actor: str = "mcp",
    source_ref: str = "mcp:add_mission_event",
    metadata_json: str = "",
) -> dict:
    """Append a mission graph event."""
    resolved = graph_mod.resolve(ref)
    if resolved is None:
        return {"ok": False, "found": False, "error": f"no mission matching '{ref}'"}
    try:
        metadata = json.loads(metadata_json) if metadata_json else {}
    except json.JSONDecodeError as e:
        return {"ok": False, "found": True, "error": f"metadata_json is not JSON: {e}"}
    if not isinstance(metadata, dict):
        return {"ok": False, "found": True, "error": "metadata_json must decode to an object"}

    event_id = db.add_event(
        resolved.mission_id,
        kind=kind,
        actor=actor,
        summary=summary,
        source_ref=source_ref,
        metadata=metadata,
    )
    _log_action("mcp_add_mission_event", resolved, {"kind": kind, "event_id": event_id})
    _refresh_after_graph_write(resolved.mission_id)
    return {"ok": True, "found": True, "mission_id": resolved.mission_id, "event_id": event_id}


@mcp.tool()
def add_mission_artifact(
    ref: str,
    path_or_url: str,
    kind: str = "proof",
    status: str = "unknown",
    summary: str = "",
) -> dict:
    """Attach a proof/output artifact to a mission."""
    resolved = graph_mod.resolve(ref)
    if resolved is None:
        return {"ok": False, "found": False, "error": f"no mission matching '{ref}'"}
    artifact_id = db.add_artifact(
        resolved.mission_id,
        kind=kind,
        path_or_url=path_or_url,
        status=status,
        summary=summary,
    )
    _log_action(
        "mcp_add_mission_artifact",
        resolved,
        {"kind": kind, "status": status, "artifact_id": artifact_id},
    )
    _refresh_after_graph_write(resolved.mission_id)
    return {"ok": True, "found": True, "mission_id": resolved.mission_id, "artifact_id": artifact_id}


@mcp.tool()
def link_missions(
    from_ref: str,
    to_ref: str,
    relation: str = "relates_to",
    reason: str = "",
) -> dict:
    """Create a mission graph edge between two existing missions."""
    from_resolved = graph_mod.resolve(from_ref)
    if from_resolved is None:
        return {"ok": False, "error": f"no mission matching '{from_ref}'"}
    to_resolved = graph_mod.resolve(to_ref)
    if to_resolved is None:
        return {"ok": False, "error": f"no mission matching '{to_ref}'"}

    edge_id = db.add_edge(
        from_resolved.mission_id,
        to_resolved.mission_id,
        relation=relation,
        reason=reason,
    )
    _log_action(
        "mcp_link_missions",
        from_resolved,
        {
            "edge_id": edge_id,
            "to_mission_id": to_resolved.mission_id,
            "relation": relation,
        },
    )
    _refresh_after_graph_write(from_resolved.mission_id)
    _refresh_after_graph_write(to_resolved.mission_id)
    return {
        "ok": True,
        "edge_id": edge_id,
        "from_id": from_resolved.mission_id,
        "to_id": to_resolved.mission_id,
        "relation": relation,
    }


@mcp.tool()
def get_context() -> str:
    """Return the full human-readable markdown context snapshot of every
    morpheus session. This is what ~/.morpheus/context.md contains."""
    return ctx_mod.build_markdown()


@mcp.tool()
def get_context_short() -> str:
    """One-line summary of current state — '12 sessions · 2 blocked · 7
    working · others blocked: PR #224, x402 review'. Cheap, prompt-sized."""
    return ctx_mod.build_short()


@mcp.tool()
def post_note(text: str, tab_id: Optional[str] = None, kind: str = "note") -> dict:
    """Post a cross-session note visible to every other agent.

    Args:
        text: The note body (one line, ~100 chars).
        tab_id: Attach to a specific tab (omit for unattached).
        kind: One of 'note' | 'claim' | 'broadcast'. Use 'claim' when
            asserting ownership of a path or worktree; 'broadcast' for
            findings that all sessions should know about.
    """
    nid = db.add_note(text=text, tab_id=tab_id, session_id=None, kind=kind)
    try:
        ctx_mod.write_context_file()
        ctx_mod.write_context_json()
    except Exception:
        pass
    ledger.log_action(
        "post_note_mcp", tab_id=tab_id,
        details={"kind": kind, "text": text[:160]},
    )
    return {"id": nid, "ok": True}


@mcp.tool()
def claim_path(path: str, tab_id: Optional[str] = None) -> dict:
    """Claim ownership of a file or directory. Other agents reading the
    context will see this and (if instructed) defer overlapping work.
    Convenience wrapper over post_note(kind='claim')."""
    return post_note(text=f"claiming {path}", tab_id=tab_id, kind="claim")


@mcp.tool()
def daily_spend() -> dict:
    """How much has morpheus spent on autonomous LLM calls today (in USD)?"""
    return {
        "dollars_today": round(ledger.daily_dollar_total(), 4),
    }


@mcp.tool()
def recent_actions(limit: int = 20) -> list[dict]:
    """List recent autonomous / scripted actions morpheus took on the
    user's behalf (spawn, kill, snapshot, note, prune, trigger_spawn)."""
    entries = ledger.recent_actions(limit=limit)
    return [
        {
            "id": e.id, "action": e.action, "tab_id": e.tab_id,
            "details": e.details, "ts": e.ts,
        }
        for e in entries
    ]


def _memory_dict(memory: db.MissionMemory) -> dict:
    return {
        "mission_id": memory.mission_id,
        "title": memory.title,
        "why": memory.why,
        "done_definition": memory.done_definition,
        "acceptance_criteria": memory.acceptance_criteria,
        "current_plan": memory.current_plan,
        "next_step": memory.next_step,
        "last_decision": memory.last_decision,
        "last_summary": memory.last_summary,
        "blocked_on": memory.blocked_on,
        "phase": memory.phase,
        "confidence": memory.confidence,
        "source_kind": memory.source_kind,
        "source_ref": memory.source_ref,
        "epic_ref": memory.epic_ref,
        "issue_ref": memory.issue_ref,
        "last_verified_at": memory.last_verified_at,
        "claimed_paths": _decode_claimed_paths(memory.claimed_paths),
        "topic": memory.topic,
        "created_at": memory.created_at,
        "updated_at": memory.updated_at,
        "archived_at": memory.archived_at,
    }


def _live_dict(mission: db.Mission) -> dict:
    return {
        "tab_id": mission.tab_id,
        "session_id": mission.session_id,
        "goal": mission.goal,
        "state": mission.state,
        "last_event": mission.last_event,
        "age_secs": max(0, time.time() - mission.buffer_changed_at),
        "cmd": mission.cmd,
        "linked_pr": mission.linked_pr,
        "linked_worktree": mission.linked_worktree,
    }


def _event_dict(event: db.MissionEvent) -> dict:
    return {
        "id": event.id,
        "mission_id": event.mission_id,
        "kind": event.kind,
        "actor": event.actor,
        "summary": event.summary,
        "source_ref": event.source_ref,
        "metadata": event.metadata,
        "ts": event.ts,
    }


def _artifact_dict(artifact: db.MissionArtifact) -> dict:
    return {
        "id": artifact.id,
        "mission_id": artifact.mission_id,
        "kind": artifact.kind,
        "path_or_url": artifact.path_or_url,
        "status": artifact.status,
        "summary": artifact.summary,
        "created_at": artifact.created_at,
    }


def _edge_dict(edge: db.MissionEdge) -> dict:
    return {
        "id": edge.id,
        "from_id": edge.from_id,
        "to_id": edge.to_id,
        "relation": edge.relation,
        "reason": edge.reason,
        "created_at": edge.created_at,
    }


def _decode_claimed_paths(raw: str) -> list[str]:
    try:
        decoded = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if isinstance(decoded, list):
        return [str(item) for item in decoded]
    return []


def _log_action(action: str, resolved: graph_mod.ResolvedMission, details: dict) -> None:
    tab_id = resolved.live[0].tab_id if resolved.live else None
    ledger.log_action(
        action,
        tab_id=tab_id,
        details={"mission_id": resolved.mission_id, **details},
    )


def _refresh_after_graph_write(mission_id: str) -> None:
    try:
        ctx_mod.write_context_file()
        ctx_mod.write_context_json()
    except Exception:
        pass
    try:
        prd_runs.update_status_for_mission(mission_id)
    except Exception:
        pass


def serve() -> None:
    """Start the MCP stdio server. Blocks until the client disconnects."""
    mcp.run()


if __name__ == "__main__":
    serve()
