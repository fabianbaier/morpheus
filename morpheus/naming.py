"""Tab-title formatting and goal inference."""

from __future__ import annotations

import re
import time

STATE_EMOJI = {
    "working":  "🟢",
    "idle":     "🟡",
    "blocked":  "🔴",
    "finished": "⚫",
    "crashed":  "💀",
    "unknown":  "⚪",
}

# Self-marker prefix so morpheus's own tabs don't get re-classified by the watcher.
MORPHEUS_TAB_PREFIX = "▶ MORPHEUS"

# Max characters we'll set as a tab name.
MAX_TITLE_LEN = 60


def is_morpheus_tab(name: str) -> bool:
    return name.startswith(MORPHEUS_TAB_PREFIX)


def format_age(secs: float) -> str:
    if secs < 60:
        return f"{int(secs)}s"
    if secs < 3600:
        return f"{int(secs / 60)}m"
    if secs < 86400:
        return f"{int(secs / 3600)}h"
    return f"{int(secs / 86400)}d"


def infer_goal_from_cmd(cmd: str) -> str:
    """Best-effort goal inference from a launched command."""
    if not cmd:
        return ""
    parts = cmd.strip().split()
    if not parts:
        return ""
    head = parts[0].rsplit("/", 1)[-1]

    # PR number anywhere?
    pr_match = re.search(r"#(\d+)|--pr[= ](\d+)|/pull/(\d+)", cmd)
    pr_num = next((g for g in (pr_match.groups() if pr_match else []) if g), None)

    label_map = {
        "codex": "codex",
        "claude": "claude",
        "gh": "gh",
        "git": "git",
        "make": "make",
        "npm": "npm",
        "pnpm": "pnpm",
        "yarn": "yarn",
        "fly": "fly",
        "vercel": "vercel",
        "docker": "docker",
    }
    label = label_map.get(head, head)

    if pr_num:
        return f"{label} PR #{pr_num}"
    return label


def build_tab_title(
    goal: str,
    state: str,
    last_event: str,
    age_secs: float,
    stale_after_hours: float = 4.0,
) -> str:
    emoji = STATE_EMOJI.get(state, STATE_EMOJI["unknown"])

    display_goal = goal.strip() if goal else "untitled"

    # Stale prefix when nothing has changed for a long time and the tab isn't blocked.
    stale_prefix = ""
    if state in ("idle", "finished") and age_secs >= stale_after_hours * 3600:
        stale_prefix = f"{format_age(age_secs)} • "

    if state == "blocked":
        title = f"{emoji} BLOCKED: {display_goal}"
    else:
        title = f"{emoji} {stale_prefix}{display_goal}"

    if len(title) > MAX_TITLE_LEN:
        title = title[: MAX_TITLE_LEN - 1] + "…"
    return title


def now_minus(ts: float) -> float:
    return max(0.0, time.time() - ts) if ts else 0.0
