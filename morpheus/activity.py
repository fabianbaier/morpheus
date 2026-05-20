"""Cached live activity snapshots for fast cross-session awareness.

The iTerm screen buffer is expensive and environment-sensitive to inspect from
an arbitrary agent tab. The watch loop already has those buffers every tick, so
this module turns them into a small JSON cache that other agents can read in a
single file operation.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from morpheus import db

ACTIVITY_JSON_PATH = Path.home() / ".morpheus" / "activity.json"

DEFAULT_TAIL_LINES = 6
DEFAULT_TAIL_WIDTH = 220
DEFAULT_HEADLINE_WIDTH = 180
TERMINAL_TAIL_SCAN_CHARS = 16_000
HEADLINE_SCAN_LINES = 18

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
SEPARATOR_RE = re.compile(r"^[\s\-_=─━═]{3,}$")
LEADING_MARKER_RE = re.compile(r"^[\s•*+\-└│╰╭├┤┬┴┌┐┘>›❯]+")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
CONCLUSION_RE = re.compile(
    r"\b(done|fixed|implemented|verified|pass(?:ed)?|failing|blocked|"
    r"finding|issue|recommend|summary|progress|next)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ActivityObservation:
    tab_id: str
    mission_id: str
    session_id: str
    goal: str
    state: str
    last_event: str
    last_event_at: float
    buffer_changed_at: float
    buffer_hash: str
    cmd: str
    linked_worktree: str
    current_name: str
    cwd: str
    buffer: str

    @classmethod
    def from_tab(cls, tab, mission: db.Mission) -> "ActivityObservation":
        return cls(
            tab_id=mission.tab_id or tab.tab_id,
            mission_id=mission.mission_id,
            session_id=mission.session_id or tab.session_id,
            goal=mission.goal,
            state=mission.state,
            last_event=mission.last_event,
            last_event_at=mission.last_event_at,
            buffer_changed_at=mission.buffer_changed_at,
            buffer_hash=mission.buffer_hash,
            cmd=mission.cmd,
            linked_worktree=mission.linked_worktree,
            current_name=tab.current_name,
            cwd=tab.cwd,
            buffer=tab.buffer,
        )


def build_snapshot(
    observations: Iterable[ActivityObservation],
    *,
    generated_at: float | None = None,
    tail_limit: int = DEFAULT_TAIL_LINES,
) -> dict[str, Any]:
    """Build a compact, cacheable activity snapshot from live observations."""
    now = time.time() if generated_at is None else generated_at
    sessions = [_session_payload(obs, now=now, tail_limit=tail_limit) for obs in observations]
    state_order = {"blocked": 0, "crashed": 1, "working": 2, "idle": 3, "finished": 4}
    sessions.sort(
        key=lambda item: (
            state_order.get(str(item.get("state") or ""), 9),
            -float(item.get("buffer_changed_at") or 0),
            str(item.get("tab_id") or ""),
        )
    )
    return {
        "generated_at": now,
        "session_count": len(sessions),
        "sessions": sessions,
    }


def write_snapshot(
    observations: Iterable[ActivityObservation],
    *,
    path: Path = ACTIVITY_JSON_PATH,
    generated_at: float | None = None,
) -> dict[str, Any]:
    """Write the activity snapshot atomically and return the payload."""
    snapshot = build_snapshot(observations, generated_at=generated_at)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    tmp.replace(path)
    return snapshot


def read_snapshot(path: Path = ACTIVITY_JSON_PATH) -> dict[str, Any]:
    """Read the cached snapshot, returning an empty payload if it is absent."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty_snapshot()
    except (OSError, json.JSONDecodeError):
        return empty_snapshot(error="unreadable activity cache")
    if not isinstance(payload, dict):
        return empty_snapshot(error="invalid activity cache")
    sessions = payload.get("sessions")
    if not isinstance(sessions, list):
        payload["sessions"] = []
    payload.setdefault("generated_at", 0.0)
    payload.setdefault("session_count", len(payload["sessions"]))
    return payload


def empty_snapshot(error: str = "") -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "generated_at": 0.0,
        "session_count": 0,
        "sessions": [],
    }
    if error:
        snapshot["error"] = error
    return snapshot


def activities_by_tab(snapshot: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    snapshot = read_snapshot() if snapshot is None else snapshot
    result: dict[str, dict[str, Any]] = {}
    for item in snapshot.get("sessions", []):
        if isinstance(item, dict) and item.get("tab_id"):
            result[str(item["tab_id"])] = item
    return result


def render_tail_lines(buffer: str, *, limit: int = DEFAULT_TAIL_LINES, width: int = DEFAULT_TAIL_WIDTH) -> list[str]:
    lines = [_clean_terminal_line(line, strip=False).rstrip() for line in _terminal_tail(buffer).splitlines()]
    lines = [line for line in lines if line.strip()]
    if not lines:
        return []
    return [_truncate(line, width, strip=False) for line in lines[-limit:]]


def session_headline(
    buffer: str,
    *,
    fallback: str = "",
    width: int = DEFAULT_HEADLINE_WIDTH,
) -> str:
    lines = _latest_activity_lines(buffer)
    headline = _response_headline(lines)
    if headline:
        return _truncate(headline, width)
    return _truncate(fallback, width) if fallback else ""


def _session_payload(obs: ActivityObservation, *, now: float, tail_limit: int) -> dict[str, Any]:
    tail_lines = render_tail_lines(obs.buffer, limit=tail_limit)
    headline = session_headline(obs.buffer, fallback=obs.last_event)
    return {
        "tab_id": obs.tab_id,
        "mission_id": obs.mission_id,
        "session_id": obs.session_id,
        "goal": obs.goal,
        "state": obs.state,
        "headline": headline,
        "last_substantive_output": headline,
        "tail_lines": tail_lines,
        "observed_at": now,
        "last_event": obs.last_event,
        "last_event_at": obs.last_event_at,
        "buffer_changed_at": obs.buffer_changed_at,
        "age_seconds": max(0.0, now - obs.buffer_changed_at) if obs.buffer_changed_at else 0.0,
        "buffer_hash": obs.buffer_hash,
        "cmd": obs.cmd,
        "linked_worktree": obs.linked_worktree,
        "current_name": obs.current_name,
        "cwd": obs.cwd,
    }


def _latest_activity_lines(buffer: str) -> list[str]:
    lines: list[str] = []
    for raw in reversed(_terminal_tail(buffer).splitlines()):
        line = _clean_terminal_line(raw)
        if not line:
            continue
        if _is_response_boundary(line):
            if lines:
                break
            continue
        if _is_headline_noise(line):
            continue
        lines.append(line)
        if len(lines) >= HEADLINE_SCAN_LINES:
            break
    return list(reversed(lines))


def _response_headline(lines: list[str]) -> str:
    candidates: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        candidate = _headline_candidate(line)
        if not candidate:
            continue
        score = index
        if CONCLUSION_RE.search(candidate):
            score += 100
        if candidate.endswith((".", "!", "?")):
            score += 5
        candidates.append((score, candidate))
    if not candidates:
        return ""
    _score, candidate = max(candidates, key=lambda item: item[0])
    return _first_sentence(candidate)


def _headline_candidate(line: str) -> str:
    line = LEADING_MARKER_RE.sub("", line).strip()
    if _is_summary_candidate_noise(line):
        return ""
    return " ".join(line.split()).strip()


def _first_sentence(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    parts = [part.strip() for part in SENTENCE_SPLIT_RE.split(value) if part.strip()]
    for part in parts:
        if not _is_summary_candidate_noise(part):
            return part
    return value


def _terminal_tail(buffer: str, limit: int = TERMINAL_TAIL_SCAN_CHARS) -> str:
    return buffer if len(buffer) <= limit else buffer[-limit:]


def _truncate(value: str, width: int, strip: bool = True) -> str:
    cleaned = _clean_terminal_line(value, strip=strip)
    if len(cleaned) <= width:
        return cleaned
    return cleaned[: max(0, width - 1)] + "…"


def _clean_terminal_line(value: str, strip: bool = True) -> str:
    cleaned = ANSI_RE.sub("", value)
    cleaned = CONTROL_RE.sub("", cleaned)
    cleaned = cleaned.replace("\t", "    ")
    return cleaned.strip() if strip else cleaned


def _is_response_boundary(line: str) -> bool:
    if _is_separator_line(line):
        return True
    lowered = line.lower()
    if lowered.startswith(("› ", "> ", "❯ ")) and "use /skills" not in lowered:
        return True
    return False


def _is_separator_line(line: str) -> bool:
    return bool(SEPARATOR_RE.fullmatch(line))


def _is_headline_noise(line: str) -> bool:
    if len(line) < 3:
        return True
    if _is_separator_line(line):
        return True
    lowered = line.lower()
    if lowered.isdigit():
        return True
    if lowered.startswith(("use /skills to list", "› use /skills to list", "> use /skills to list")):
        return True
    if lowered.startswith("gpt-") and ("·" in lowered or "~" in lowered):
        return True
    if lowered.startswith(("openai codex", "token usage:")):
        return True
    if "esc to interrupt" in lowered or "/ps to view" in lowered or "ctrl + t to view transcript" in lowered:
        return True
    if lowered in {"searching the web", "thinking", "working"}:
        return True
    return False


def _is_summary_candidate_noise(line: str) -> bool:
    lowered = line.lower()
    if _is_headline_noise(line):
        return True
    if lowered.startswith(("sources:", "source:", "caveat:", "disclaimer:")):
        return True
    if "http://" in lowered or "https://" in lowered:
        return True
    return False
