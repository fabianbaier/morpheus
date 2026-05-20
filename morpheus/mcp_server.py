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
  - get_context()
  - get_context_short()
  - post_note(text, tab_id, kind)
  - claim_path(path, tab_id)
  - daily_spend()
  - recent_actions(limit)
"""

from __future__ import annotations

import time
from typing import Optional

from mcp.server.fastmcp import FastMCP

from morpheus import context as ctx_mod
from morpheus import db, ledger

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
    return {
        "found": True,
        "tab_id": match.tab_id,
        "goal": match.goal,
        "state": match.state,
        "last_event": match.last_event,
        "cmd": match.cmd,
        "linked_pr": match.linked_pr,
        "linked_worktree": match.linked_worktree,
        "age_secs": max(0, time.time() - match.buffer_changed_at),
        "notes": [
            {"id": n.id, "text": n.text, "kind": n.kind, "ts": n.created_at}
            for n in notes
        ],
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


def serve() -> None:
    """Start the MCP stdio server. Blocks until the client disconnects."""
    mcp.run()


if __name__ == "__main__":
    serve()
