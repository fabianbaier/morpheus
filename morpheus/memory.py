"""User-level relevance memory — a markdown file the user owns.

Omnipresence mode (docs/omnipresence-prd.md §3.3) judges candidate pushes
against ``~/.morpheus/memory.md``: sectioned, human-editable markdown that
Morpheus appends dated one-line facts to. Every change is a visible diff (the
file is git-friendly) and every append is also logged to ``memory.log`` so
``morpheus memory log`` can show what changed and when.

This is deliberately a *single user-level file*, not per-project overlays —
missions keep their own mission memory; omnipresence reads both.

The directory honours the same override the shared database uses
(``MORPHEUS_DB_DIR``, see db.py) so tests and alternate deployments can
repoint everything at once. Tests patch the module globals directly.
"""

from __future__ import annotations

import os
import time
from collections import deque
from pathlib import Path
from typing import Optional

MEMORY_DIR = Path(os.environ.get("MORPHEUS_DB_DIR") or (Path.home() / ".morpheus"))
MEMORY_FILENAME = "memory.md"
LOG_FILENAME = "memory.log"

SECTIONS = ("People", "Interests", "Current", "Never push")
# The section that must survive any prompt-budget truncation: it holds the
# user's do-not-push rules, and dropping it would silently re-enable pushes
# the user explicitly muted.
NEVER_PUSH_SECTION = "Never push"
# One fact is one line; anything longer belongs in a mission, not here.
ENTRY_MAX_CHARS = 500
# memory.log bounds: once the log passes LOG_MAX_LINES, an append trims it to
# the newest LOG_TRIM_TO lines so the file can never grow without bound.
LOG_MAX_LINES = 2000
LOG_TRIM_TO = 1000

_TEMPLATE = """# Morpheus user memory

Morpheus reads this file when judging what is worth pushing to your ambient
surfaces (G2 glasses, phone). Edit it freely — it is yours. Morpheus appends
dated one-line facts under the sections below; every change is a plain diff.

## People

## Interests

## Current

## Never push
"""


def memory_path() -> Path:
    return MEMORY_DIR / MEMORY_FILENAME


def log_path() -> Path:
    return MEMORY_DIR / LOG_FILENAME


def ensure_file() -> Path:
    """Create ~/.morpheus/memory.md with the section template if missing."""
    path = memory_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_TEMPLATE)
    return path


def read_memory() -> str:
    """Return the full memory file (creating the template on first read)."""
    return ensure_file().read_text()


def _clean_entry(text: str) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) > ENTRY_MAX_CHARS:
        cleaned = cleaned[: ENTRY_MAX_CHARS - 3].rstrip() + "..."
    return cleaned


def _clean_section(section: str) -> str:
    cleaned = " ".join(str(section or "").split()).lstrip("#").strip()
    if not cleaned:
        raise ValueError("section is required")
    # Keep user-visible headings tidy and match the template's capitalization
    # when the caller means one of the canonical sections.
    for known in SECTIONS:
        if cleaned.lower() == known.lower():
            return known
    if len(cleaned) > 64:
        raise ValueError("section name too long (max 64 chars)")
    return cleaned


def append_entry(section: str, text: str, now: Optional[float] = None) -> str:
    """Append a dated one-line fact under a section (created if missing).

    Returns the exact line written, e.g. ``- 2026-07-03: out of espresso beans``.
    Also appends one line to memory.log for `morpheus memory log`.
    """
    section = _clean_section(section)
    text = _clean_entry(text)
    if not text:
        raise ValueError("text is required")
    ts = time.time() if now is None else float(now)
    line = f"- {time.strftime('%Y-%m-%d', time.localtime(ts))}: {text}"

    content = read_memory()
    lines = content.splitlines()
    heading = f"## {section}"
    start = next(
        (i for i, ln in enumerate(lines) if ln.strip().lower() == heading.lower()),
        None,
    )
    if start is None:
        # New section at the end of the file.
        while lines and not lines[-1].strip():
            lines.pop()
        lines.extend(["", heading, line])
    else:
        # Insert at the end of the section block (before the next heading),
        # trimming trailing blank lines inside the block so entries stay
        # contiguous under their heading.
        end = len(lines)
        for i in range(start + 1, len(lines)):
            if lines[i].startswith("## "):
                end = i
                break
        insert_at = end
        while insert_at > start + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines.insert(insert_at, line)
    memory_path().write_text("\n".join(lines) + "\n")
    _log_change(ts, section, text)
    return line


def _log_change(ts: float, section: str, text: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(f"{stamp}\t{section}\t{text}\n")
    # Bound the log: past LOG_MAX_LINES, keep only the newest LOG_TRIM_TO.
    # The cap keeps this O(small); an hourly appender never re-reads much.
    lines = path.read_text().splitlines()
    if len(lines) > LOG_MAX_LINES:
        path.write_text("\n".join(lines[-LOG_TRIM_TO:]) + "\n")


def read_log(limit: int = 20) -> list[dict[str, str]]:
    """Return the newest ``limit`` memory changes, newest first.

    Reads only the tail (a bounded deque over the line iterator) instead of
    materializing the whole file, so a large log is cheap to query.
    """
    path = log_path()
    if not path.exists():
        return []
    limit = max(1, int(limit))
    with open(path) as f:
        tail = deque((raw.rstrip("\n") for raw in f if raw.strip()), maxlen=limit)
    entries: list[dict[str, str]] = []
    for raw in tail:
        parts = raw.split("\t", 2)
        while len(parts) < 3:
            parts.append("")
        entries.append({"ts": parts[0], "section": parts[1], "text": parts[2]})
    return list(reversed(entries))


def _never_push_line_indices(lines: list[str]) -> set[int]:
    """Indices of the '## Never push' heading and every line of its block."""
    indices: set[int] = set()
    in_never = False
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if stripped.startswith("## "):
            in_never = stripped[3:].strip().lower() == NEVER_PUSH_SECTION.lower()
        if in_never:
            indices.add(i)
    return indices


def top_entries(max_chars: int = 2000) -> str:
    """A bounded read for prompts: the memory file, truncated safely at a line
    boundary so a judge prompt never balloons past its budget.

    Section-aware: the '## Never push' block (the user's do-not-push rules)
    is *always* included in full — it is usually the last section, and a
    naive head-truncation would silently drop it as memory grows. The
    remaining budget is filled with the rest of the file, top down, with the
    original line order preserved.
    """
    text = read_memory()
    max_chars = max(0, int(max_chars))
    if len(text) <= max_chars:
        return text
    lines = text.splitlines()
    never = _never_push_line_indices(lines)
    keep = set(never)
    budget = max_chars - sum(len(lines[i]) + 1 for i in never)
    for i, ln in enumerate(lines):
        if i in never:
            continue
        cost = len(ln) + 1
        if cost > budget:
            break
        keep.add(i)
        budget -= cost
    return "\n".join(lines[i] for i in sorted(keep))
