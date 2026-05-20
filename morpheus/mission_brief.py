"""Selected mission briefing helpers.

This is intentionally deterministic and local: pressing `b` in the dashboard
should answer "what is this, why does it matter, what happened, what proof
exists, and what should I do next?" without waiting on an LLM.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Optional

from morpheus import db, naming


ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class SelectedMissionBrief:
    title: str
    body: str


def build_selected_brief(
    mission: db.Mission,
    *,
    memory: Optional[db.MissionMemory] = None,
    events: Optional[list[db.MissionEvent]] = None,
    artifacts: Optional[list[db.MissionArtifact]] = None,
    transcript: str = "",
    generated_at: Optional[float] = None,
) -> SelectedMissionBrief:
    """Build a terse cited brief for one selected mission."""
    events = events or []
    artifacts = artifacts or []
    generated = generated_at or time.time()
    title = (memory.title if memory else "") or mission.goal or "(untitled)"
    mission_ref = mission.mission_id or "unlinked"
    tab_ref = f"tab:{mission.tab_id}" if mission.tab_id else "tab:unset"
    graph_ref = f"graph:{mission_ref}"
    source_ref = _source_ref(memory, graph_ref)
    phase = (memory.phase if memory else "") or "unset"
    blocked = memory.blocked_on if memory else ""
    next_step = _first_nonempty(
        memory.next_step if memory else "",
        "Attach to the session and ask it to state next step.",
    )
    age = naming.format_age(naming.now_minus(mission.buffer_changed_at))

    lines: list[str] = []
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(generated))
    lines.append(f"# Mission brief - {title}")
    lines.append("")
    lines.append(f"- Mission: `{mission_ref}` [{graph_ref}]")
    lines.append(f"- Tab: `{_short_tab(mission.tab_id)}` [{tab_ref}]")
    lines.append(f"- Generated: {when}")
    lines.append("")

    lines.append("## What")
    lines.append(f"{_first_nonempty(mission.goal, title, '(untitled mission)')} [{graph_ref}]")
    lines.append("")

    lines.append("## Why")
    lines.append(f"{_first_nonempty(memory.why if memory else '', 'Not recorded yet.')} [{source_ref}]")
    lines.append("")

    lines.append("## Status")
    lines.append(f"- State: {mission.state or 'unknown'}; phase: {phase}; age: {age} [{graph_ref}]")
    if blocked:
        lines.append(f"- Blocked on: {_compact(blocked)} [{graph_ref}]")
    if mission.last_event:
        lines.append(f"- Last event: {_compact(mission.last_event)} [{tab_ref}]")
    if memory and memory.last_decision:
        lines.append(f"- Last decision: {_compact(memory.last_decision)} [{source_ref}]")
    lines.append("")

    lines.append("## What Happened")
    if events:
        for event in events[:4]:
            when = time.strftime("%H:%M", time.localtime(event.ts))
            ref = event.source_ref or graph_ref
            lines.append(f"- {when} {event.kind}/{event.actor}: {_compact(event.summary)} [{ref}]")
    else:
        lines.append(f"- No mission events recorded yet. [{graph_ref}]")
    lines.append("")

    lines.append("## Proof")
    if artifacts:
        for artifact in artifacts[:4]:
            summary = f" - {_compact(artifact.summary)}" if artifact.summary else ""
            lines.append(
                f"- {artifact.status} {artifact.kind}: {artifact.path_or_url}{summary} "
                f"[artifact:{artifact.id}]"
            )
    else:
        lines.append(f"- No proof artifacts recorded yet. [{graph_ref}]")
    lines.append("")

    lines.append("## Next")
    lines.append(f"{_compact(next_step, limit=360)} [{source_ref if memory and memory.next_step else graph_ref}]")

    tail = _transcript_tail(transcript)
    if tail:
        lines.append("")
        lines.append("## Transcript Tail")
        for line in tail:
            lines.append(f"- {_compact(line, limit=180)} [{tab_ref}]")

    return SelectedMissionBrief(title=title, body="\n".join(lines).rstrip() + "\n")


def _source_ref(memory: Optional[db.MissionMemory], fallback: str) -> str:
    if memory is None:
        return fallback
    if memory.source_kind and memory.source_ref:
        return f"{memory.source_kind}:{memory.source_ref}"
    if memory.source_ref:
        return memory.source_ref
    if memory.source_kind:
        return memory.source_kind
    return fallback


def _first_nonempty(*values: str) -> str:
    for value in values:
        cleaned = " ".join((value or "").split())
        if cleaned:
            return cleaned
    return ""


def _short_tab(tab_id: str) -> str:
    return (tab_id or "?").split("-")[0]


def _compact(value: str, limit: int = 240) -> str:
    cleaned = " ".join((value or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "..."


def _transcript_tail(transcript: str, *, limit: int = 5) -> list[str]:
    lines: list[str] = []
    for raw in reversed(transcript.splitlines()):
        line = CONTROL_RE.sub("", ANSI_RE.sub("", raw)).strip()
        if not line:
            continue
        lines.append(line)
        if len(lines) >= limit:
            break
    return list(reversed(lines))
