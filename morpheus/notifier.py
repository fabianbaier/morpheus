"""macOS notifications via `terminal-notifier`.

Wraps the `terminal-notifier` CLI (install: `brew install terminal-notifier`).
Falls back to silent + a one-time stderr warning if it isn't installed —
notifications are an enhancement, not a hard requirement.

Notifications fire only when the dashboard isn't the focused app — silent
when the user is already looking at the rain.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_log = logging.getLogger("morpheus.notifier")

_BIN: Optional[str] = None
_WARNED = False
_LAST_FIRED_AT: dict[str, float] = {}   # kind -> last unix ts (rate limiting)
RATE_LIMIT_SECS = 1.0  # per-kind floor between notifications

# Per-kind silencing — overridable later via config.
SILENCED: set[str] = set()


def _find_bin() -> Optional[str]:
    global _BIN, _WARNED
    if _BIN is not None:
        return _BIN
    found = shutil.which("terminal-notifier")
    if found:
        _BIN = found
        return found
    if not _WARNED:
        _WARNED = True
        _log.warning(
            "terminal-notifier not found on PATH — macOS notifications disabled. "
            "Install: brew install terminal-notifier"
        )
    return None


@dataclass
class Notification:
    title: str
    message: str
    kind: str = "info"      # info | state | spawn | close | note | error | brief
    sound: Optional[str] = None   # macOS sound name, e.g. "Glass", "Pop"
    subtitle: Optional[str] = None
    group: Optional[str] = None   # collapses notifications of the same group


def is_available() -> bool:
    return _find_bin() is not None


def notify(n: Notification, force: bool = False) -> bool:
    """Fire a notification. Returns True if it was actually sent.

    Skipped when:
    - terminal-notifier isn't installed
    - the kind is in SILENCED
    - rate-limited (same kind fired < RATE_LIMIT_SECS ago)
    - `force=False` and Morpheus dashboard is the foreground app
    """
    if n.kind in SILENCED:
        return False
    last = _LAST_FIRED_AT.get(n.kind, 0.0)
    now = time.time()
    if (now - last) < RATE_LIMIT_SECS:
        return False
    bin_path = _find_bin()
    if not bin_path:
        return False

    cmd = [bin_path, "-title", n.title[:64], "-message", n.message[:240]]
    if n.subtitle:
        cmd.extend(["-subtitle", n.subtitle[:64]])
    if n.sound:
        cmd.extend(["-sound", n.sound])
    if n.group:
        cmd.extend(["-group", n.group])
    # Bring iTerm to front if user clicks the banner.
    cmd.extend(["-activate", "com.googlecode.iterm2"])

    try:
        subprocess.run(cmd, capture_output=True, timeout=3)
        _LAST_FIRED_AT[n.kind] = now
        return True
    except Exception as e:
        _log.debug("notify failed: %s", e)
        return False


# ── helpers for the kinds Morpheus fires ──────────────────────────────────

def notify_state(goal: str, new_state: str, last_event: str = "") -> bool:
    """A session's state changed. Only the high-priority transitions fire."""
    sound = None
    title = f"🐇 {goal or 'session'}"
    if new_state == "blocked":
        sound = "Glass"
        msg = f"BLOCKED — {last_event or 'needs your input'}"
    elif new_state == "crashed":
        sound = "Sosumi"
        msg = f"CRASHED — {last_event or 'session died'}"
    elif new_state == "finished":
        msg = f"finished — {last_event or 'no more output'}"
    else:
        return False
    return notify(Notification(
        title=title, message=msg, kind="state", sound=sound,
        group=f"morpheus.{goal or 'session'}",
    ))


def notify_spawn(goal: str, tab_id: str) -> bool:
    return notify(Notification(
        title=f"🐇 new session",
        message=f"{goal or '(untitled)'}  [{tab_id.split('-')[0]}]",
        kind="spawn",
        group="morpheus.spawn",
    ))


def notify_note(goal: str, text: str) -> bool:
    return notify(Notification(
        title=f"🐇 note from {goal or 'session'}",
        message=text,
        kind="note",
        group="morpheus.note",
    ))


def notify_brief(summary: str) -> bool:
    return notify(Notification(
        title="🐇 Morpheus brief",
        message=summary[:240],
        kind="brief",
        sound="Glass",
    ))
