"""`morpheus brief` — a digest of the world right now.

Gathers state from local mission DB + `gh` review queue + recent notes,
optionally pipes it through `claude -p` (or `codex exec` as fallback) to
produce a human-readable markdown digest.

Designed for morning/evening review. Can be scheduled via launchd /
`scheduled-tasks` / cron — but defaults to on-demand.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from morpheus import db, naming


DEFAULT_STALE_HOURS = 4.0
GH_QUEUE_LIMIT = 20
LLM_TIMEOUT_SECS = 60


@dataclass
class PR:
    number: int
    title: str
    url: str
    repo: str
    updated_at: str = ""


@dataclass
class BriefState:
    generated_at: float
    total_missions: int
    counts: dict[str, int]
    blocked: list[db.Mission]
    stale: list[db.Mission]
    crashed: list[db.Mission]
    recent_notes: list[db.Note]
    gh_review_queue: list[PR] = field(default_factory=list)
    gh_error: Optional[str] = None


# ── state gathering ──────────────────────────────────────────────────────

def gather_state(stale_hours: float = DEFAULT_STALE_HOURS,
                 gh_repos: Optional[list[str]] = None,
                 include_gh: bool = True) -> BriefState:
    missions = db.all_missions()
    notes = db.recent_notes(limit=15)
    counts: dict[str, int] = {}
    for m in missions:
        counts[m.state] = counts.get(m.state, 0) + 1
    blocked = [m for m in missions if m.state == "blocked"]
    crashed = [m for m in missions if m.state == "crashed"]
    now = time.time()
    stale = [
        m for m in missions
        if m.state in ("idle", "finished")
        and (now - m.buffer_changed_at) >= stale_hours * 3600
    ]
    state = BriefState(
        generated_at=now,
        total_missions=len(missions),
        counts=counts,
        blocked=blocked,
        stale=stale,
        crashed=crashed,
        recent_notes=notes,
    )
    if include_gh:
        try:
            state.gh_review_queue = fetch_gh_review_queue(gh_repos)
        except Exception as e:
            state.gh_error = str(e)
    return state


def fetch_gh_review_queue(repos: Optional[list[str]] = None) -> list[PR]:
    """Run `gh pr list --search "review-requested:@me"` and parse."""
    if shutil.which("gh") is None:
        raise RuntimeError("gh CLI not on PATH (install: brew install gh)")
    if repos:
        # gh's --search syntax handles repo filters globally
        repo_qs = " ".join(f"repo:{r}" for r in repos)
        search = f"review-requested:@me {repo_qs}"
    else:
        search = "review-requested:@me"
    cmd = [
        "gh", "search", "prs", search,
        "--state", "open",
        "--json", "number,title,url,repository,updatedAt",
        "--limit", str(GH_QUEUE_LIMIT),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        raise RuntimeError("gh search timed out (>15s)")
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"gh exit {proc.returncode}")
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"gh JSON parse failed: {e}")
    out: list[PR] = []
    for row in data:
        repo_info = row.get("repository") or {}
        repo_name = repo_info.get("nameWithOwner") or repo_info.get("name") or "?"
        out.append(PR(
            number=row.get("number") or 0,
            title=row.get("title") or "(no title)",
            url=row.get("url") or "",
            repo=repo_name,
            updated_at=row.get("updatedAt") or "",
        ))
    return out


# ── prompt assembly ──────────────────────────────────────────────────────

def build_template_brief(state: BriefState) -> str:
    """A no-LLM brief — pure formatting of the state."""
    when = time.strftime("%a %Y-%m-%d %H:%M", time.localtime(state.generated_at))
    lines: list[str] = []
    lines.append(f"# 🐇 Morpheus brief — {when}\n")
    if state.total_missions == 0:
        lines.append("_no sessions registered yet — spawn one with `morpheus spawn` or `n` in the dashboard._\n")
    else:
        emo_line = []
        for emoji, key in [("🔴", "blocked"), ("💀", "crashed"), ("🟢", "working"),
                           ("🟡", "idle"), ("⚫", "finished")]:
            c = state.counts.get(key, 0)
            if c:
                emo_line.append(f"{emoji} {c} {key}")
        lines.append(
            f"**{state.total_missions} session(s)**  ·  " + "  ·  ".join(emo_line) + "\n"
        )

    if state.blocked:
        lines.append("## 🔴 Blocked — need your eyes")
        for m in state.blocked:
            age = naming.format_age(naming.now_minus(m.buffer_changed_at))
            lines.append(
                f"- **{m.goal or '(untitled)'}** "
                f"(`{(m.tab_id or '?').split('-')[0]}`, blocked {age}) "
                f"— last: _{m.last_event}_"
            )
        lines.append("")

    if state.crashed:
        lines.append("## 💀 Crashed")
        for m in state.crashed:
            lines.append(f"- **{m.goal or '(untitled)'}** — last: _{m.last_event}_")
        lines.append("")

    if state.stale:
        lines.append(f"## ⚫ Stale (idle/finished > {int(DEFAULT_STALE_HOURS)}h)")
        for m in state.stale[:10]:
            age = naming.format_age(naming.now_minus(m.buffer_changed_at))
            lines.append(
                f"- {m.goal or '(untitled)'} "
                f"(`{(m.tab_id or '?').split('-')[0]}`, {age}) — _{m.last_event}_"
            )
        if len(state.stale) > 10:
            lines.append(f"- _…and {len(state.stale) - 10} more_")
        lines.append("")
        lines.append("Run `morpheus prune` to clean these up.\n")

    if state.gh_review_queue:
        lines.append(f"## 📥 PR review queue ({len(state.gh_review_queue)})")
        for pr in state.gh_review_queue:
            lines.append(f"- [{pr.repo}#{pr.number}]({pr.url}) — {pr.title}")
        lines.append("")
    elif state.gh_error:
        lines.append(f"## 📥 PR review queue")
        lines.append(f"_couldn't fetch: {state.gh_error}_\n")

    if state.recent_notes:
        lines.append("## 📝 Recent cross-session notes")
        for n in state.recent_notes[:10]:
            ts = time.strftime("%H:%M", time.localtime(n.created_at))
            marker = {"note": "•", "claim": "⚑", "broadcast": "📡"}.get(n.kind, "•")
            lines.append(f"- [{ts}] {marker} {n.text}")
        lines.append("")

    lines.append("---")
    lines.append(f"_generated by morpheus brief at {when}_")
    return "\n".join(lines) + "\n"


def build_llm_prompt(state: BriefState) -> str:
    """Build the prompt we'll feed to claude/codex for a richer brief."""
    raw = build_template_brief(state)
    return (
        "You are Morpheus, an assistant that summarizes a software engineer's "
        "concurrent agent sessions and PR review queue into a tight, "
        "actionable morning/evening brief.\n\n"
        "Below is the raw state. Produce a markdown digest under 25 lines that:\n"
        " 1. Opens with the single most important thing to look at right now.\n"
        " 2. Lists action items in priority order (blocked > crashed > stale > review queue).\n"
        " 3. Suggests concrete next steps where obvious (kill stale X, attach to tab Y, etc.).\n"
        " 4. Omits sections that have no items. No filler. No hedging.\n"
        " 5. Ends with a one-line 'focus' suggestion for the next 2 hours.\n"
        " 6. Uses 🐇 only for the single most-urgent item.\n\n"
        "RAW STATE:\n\n"
        f"{raw}"
    )


# ── LLM invocation ────────────────────────────────────────────────────────

def _run_claude(prompt: str) -> Optional[str]:
    if shutil.which("claude") is None:
        return None
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=LLM_TIMEOUT_SECS,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        return proc.stdout.strip()
    except Exception:
        return None


def _run_codex(prompt: str) -> Optional[str]:
    if shutil.which("codex") is None:
        return None
    try:
        proc = subprocess.run(
            ["codex", "exec", prompt, "-s", "read-only"],
            capture_output=True, text=True, timeout=LLM_TIMEOUT_SECS,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        return proc.stdout.strip()
    except Exception:
        return None


def generate(use_llm: bool = True, stale_hours: float = DEFAULT_STALE_HOURS,
             gh_repos: Optional[list[str]] = None,
             include_gh: bool = True) -> str:
    state = gather_state(stale_hours=stale_hours, gh_repos=gh_repos,
                          include_gh=include_gh)
    if not use_llm:
        return build_template_brief(state)
    prompt = build_llm_prompt(state)
    out = _run_claude(prompt)
    if out is None:
        out = _run_codex(prompt)
    if out is None:
        # Fallback: template brief with a note that LLM was unavailable.
        body = build_template_brief(state)
        return ("> _(neither `claude -p` nor `codex exec` available — "
                "using template brief)_\n\n" + body)
    return out
