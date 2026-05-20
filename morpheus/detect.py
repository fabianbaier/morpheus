"""State detection from terminal pane buffer.

Returns one of: working, idle, blocked, finished, crashed, unknown.

Detection is intentionally pattern-based and conservative — false positives on
"blocked" are very expensive (alert fatigue), so we only flag known prompts.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass

# How long without a buffer change before a tab is considered "idle".
IDLE_THRESHOLD_SECS = 30.0

# How long with no buffer change at all before "finished" (process likely done).
FINISHED_THRESHOLD_SECS = 60 * 30  # 30 min

# Look at this much of the trailing buffer when matching prompts.
TAIL_CHARS = 800


# Patterns for "blocked waiting for user input". Compile once.
# Each pattern is a tuple of (compiled_regex, label_for_event).
BLOCKED_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Codex
    (re.compile(r"Would you like to make the following edits\?", re.IGNORECASE), "codex: edit prompt"),
    (re.compile(r"Yes, proceed \(y\)", re.IGNORECASE), "codex: y/proceed prompt"),
    (re.compile(r"Yes, and don't ask again", re.IGNORECASE), "codex: don't-ask prompt"),
    # Claude Code permission prompts
    (re.compile(r"Do you want to proceed\?", re.IGNORECASE), "claude: proceed prompt"),
    (re.compile(r"\bAllow this command\?", re.IGNORECASE), "claude: permission prompt"),
    (re.compile(r"\b1\.\s*Yes\b.*\b2\.\s*No", re.DOTALL), "claude: yes/no menu"),
    # Generic shell
    (re.compile(r"\[y/N\]\s*$"), "shell: y/N prompt"),
    (re.compile(r"\[Y/n\]\s*$"), "shell: Y/n prompt"),
    (re.compile(r"\(y/n\)\s*[:?]?\s*$", re.IGNORECASE), "shell: y/n prompt"),
    (re.compile(r"Press\s+(any\s+key|Enter|RETURN)\s+to\s+continue", re.IGNORECASE), "shell: press-enter prompt"),
    (re.compile(r"Are you sure\?", re.IGNORECASE), "shell: confirm prompt"),
    # sudo
    (re.compile(r"^\s*Password:\s*$", re.MULTILINE), "sudo: password prompt"),
]

# Patterns indicating session ended.
FINISHED_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\[Process completed\]", re.IGNORECASE), "process completed"),
    (re.compile(r"^\s*Session ended\.?\s*$", re.MULTILINE), "session ended"),
]

# Patterns indicating crash / error termination.
CRASHED_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^\s*Segmentation fault", re.MULTILINE), "segfault"),
    (re.compile(r"^\s*panic:", re.MULTILINE), "go panic"),
    (re.compile(r"Traceback \(most recent call last\):.*Error", re.DOTALL), "python traceback"),
    (re.compile(r"\bzsh: killed\b"), "zsh killed"),
]


@dataclass
class Detection:
    state: str           # working | idle | blocked | finished | crashed | unknown
    last_event: str      # short human-readable summary
    buffer_hash: str     # for change tracking
    changed: bool        # did buffer change since last hash?


def buffer_hash(buffer: str) -> str:
    return hashlib.sha1(buffer.encode("utf-8", errors="replace")).hexdigest()[:16]


def _match_first(patterns: list[tuple[re.Pattern, str]], tail: str) -> str | None:
    for pat, label in patterns:
        if pat.search(tail):
            return label
    return None


def detect(
    buffer: str,
    prev_hash: str,
    prev_buffer_changed_at: float,
    now: float | None = None,
) -> Detection:
    """Classify the current state from a pane buffer.

    `prev_buffer_changed_at` is the unix ts of the last detected buffer change.
    On first call, pass 0.0 to indicate unknown.
    """
    now = now if now is not None else time.time()
    tail = buffer[-TAIL_CHARS:] if buffer else ""
    h = buffer_hash(buffer)
    changed = (h != prev_hash) if prev_hash else True

    # Crashed beats everything else.
    crashed = _match_first(CRASHED_PATTERNS, tail)
    if crashed:
        return Detection("crashed", crashed, h, changed)

    # Blocked beats finished/idle — a prompt is on screen.
    blocked = _match_first(BLOCKED_PATTERNS, tail)
    if blocked:
        return Detection("blocked", blocked, h, changed)

    # Explicit "finished" markers.
    finished = _match_first(FINISHED_PATTERNS, tail)
    if finished:
        return Detection("finished", finished, h, changed)

    # Time-based: working if buffer changed recently, else idle, else finished.
    last_change = prev_buffer_changed_at if not changed else now
    secs_since_change = now - last_change if last_change > 0 else 0.0

    if changed:
        return Detection("working", "active output", h, True)
    if secs_since_change >= FINISHED_THRESHOLD_SECS:
        return Detection("finished", "idle >30min", h, False)
    if secs_since_change >= IDLE_THRESHOLD_SECS:
        return Detection("idle", f"idle {int(secs_since_change)}s", h, False)
    return Detection("working", "recent output", h, False)
