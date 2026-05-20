"""Cross-session awareness — builds a live snapshot of every other session
that other agents can read to know what's going on around them.

Auto-written to ~/.morpheus/context.md every tick. Also exposed via
`morpheus context` (text/json) and `morpheus note <text>` for posting
cross-session messages.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from morpheus import db, naming

CONTEXT_PATH = Path.home() / ".morpheus" / "context.md"
CONTEXT_JSON_PATH = Path.home() / ".morpheus" / "context.json"


def _state_counts(missions: list[db.Mission]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for m in missions:
        counts[m.state] = counts.get(m.state, 0) + 1
    return counts


def _format_note_line(n: db.Note, missions_by_tab: dict[str, db.Mission]) -> str:
    ts = time.strftime("%H:%M", time.localtime(n.created_at))
    tab_short = (n.tab_id or "?").split("-")[0]
    goal = missions_by_tab.get(n.tab_id, db.Mission(tab_id="")).goal or "?"
    kind_marker = {"note": "•", "claim": "⚑", "broadcast": "📡"}.get(n.kind, "•")
    return f"- [{ts}] {kind_marker} **{tab_short}** ({goal}): {n.text}"


def build_markdown(self_tab_id: Optional[str] = None, self_session_id: Optional[str] = None) -> str:
    """Build the markdown snapshot. Marks `self_*` so the reader can tell which
    session is themselves vs others."""
    missions = db.all_missions()
    notes = db.recent_notes(limit=15)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    counts = _state_counts(missions)
    missions_by_tab = {m.tab_id: m for m in missions}

    def _is_self(m: db.Mission) -> bool:
        if self_tab_id and m.tab_id == self_tab_id:
            return True
        if self_session_id and m.session_id == self_session_id:
            return True
        return False

    lines: list[str] = []
    lines.append(f"# Morpheus Context — {now}")
    lines.append("")
    summary = f"{len(missions)} session(s)"
    for emoji, key in [("🔴", "blocked"), ("💀", "crashed"), ("🟢", "working"),
                       ("🟡", "idle"), ("⚫", "finished")]:
        c = counts.get(key, 0)
        if c:
            summary += f" | {emoji} {c} {key}"
    lines.append(summary)
    lines.append("")
    lines.append("Every other agent session running on this machine, with current state.")
    lines.append("If you are one of these sessions, find your own row marked **[YOU]** and ignore it.")
    lines.append("")

    # Sort: blocked first, then crashed, then working, then idle, then finished.
    state_order = {"blocked": 0, "crashed": 1, "working": 2, "idle": 3, "finished": 4, "unknown": 5}
    sorted_missions = sorted(missions, key=lambda m: (state_order.get(m.state, 9), -m.updated_at))

    lines.append("## Sessions")
    lines.append("")
    lines.append("| State | ID | Goal | Age | Last event | |")
    lines.append("|-------|-----|------|-----|------------|---|")
    for m in sorted_missions:
        emoji = naming.STATE_EMOJI.get(m.state, "⚪")
        age = naming.format_age(naming.now_minus(m.buffer_changed_at))
        you = "**[YOU]**" if _is_self(m) else ""
        goal = m.goal or "(untitled)"
        last = (m.last_event or "—").replace("|", "\\|")
        tab_short = (m.tab_id or "?").split("-")[0]
        lines.append(f"| {emoji} {m.state} | `{tab_short}` | {goal} | {age} | {last} | {you} |")

    lines.append("")
    lines.append("## Recent cross-session notes")
    lines.append("")
    if not notes:
        lines.append("_no notes yet — post one with `morpheus note \"text\"`_")
    else:
        for n in notes:
            lines.append(_format_note_line(n, missions_by_tab))

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("**Usage from inside an agent session:**")
    lines.append("- Read this file (`cat ~/.morpheus/context.md`) to see the world around you.")
    lines.append("- Run `morpheus note \"text\"` to post a note other sessions will see.")
    lines.append("- Run `morpheus context --json` for a parseable snapshot.")
    lines.append("- Run `morpheus context --short` for a one-line summary suitable for prompts.")
    return "\n".join(lines) + "\n"


def build_json(self_tab_id: Optional[str] = None, self_session_id: Optional[str] = None) -> dict:
    missions = db.all_missions()
    notes = db.recent_notes(limit=15)

    def _is_self(m: db.Mission) -> bool:
        if self_tab_id and m.tab_id == self_tab_id:
            return True
        if self_session_id and m.session_id == self_session_id:
            return True
        return False

    return {
        "generated_at": time.time(),
        "counts": _state_counts(missions),
        "sessions": [
            {
                "tab_id": m.tab_id,
                "session_id": m.session_id,
                "goal": m.goal,
                "state": m.state,
                "last_event": m.last_event,
                "last_event_at": m.last_event_at,
                "buffer_changed_at": m.buffer_changed_at,
                "cmd": m.cmd,
                "linked_pr": m.linked_pr,
                "linked_worktree": m.linked_worktree,
                "is_self": _is_self(m),
            }
            for m in missions
        ],
        "notes": [
            {
                "id": n.id,
                "tab_id": n.tab_id,
                "session_id": n.session_id,
                "text": n.text,
                "kind": n.kind,
                "created_at": n.created_at,
            }
            for n in notes
        ],
    }


def build_short(self_tab_id: Optional[str] = None) -> str:
    """One-line summary suitable for an agent prompt."""
    missions = db.all_missions()
    counts = _state_counts(missions)
    parts = [f"{len(missions)} sessions"]
    for emoji, key in [("🔴", "blocked"), ("🟢", "working"), ("🟡", "idle"), ("⚫", "finished")]:
        c = counts.get(key, 0)
        if c:
            parts.append(f"{c} {key}")
    others = [m for m in missions if m.tab_id != self_tab_id and m.state == "blocked"]
    summary = " · ".join(parts)
    if others:
        names = ", ".join(m.goal or m.tab_id.split("-")[0] for m in others[:3])
        summary += f" — others blocked: {names}"
    return summary


def write_context_file() -> None:
    """Write the markdown context to ~/.morpheus/context.md (atomic)."""
    CONTEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
    content = build_markdown()
    tmp = CONTEXT_PATH.with_suffix(".md.tmp")
    tmp.write_text(content)
    tmp.replace(CONTEXT_PATH)


def write_context_json() -> None:
    CONTEXT_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONTEXT_JSON_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(build_json(), indent=2))
    tmp.replace(CONTEXT_JSON_PATH)
