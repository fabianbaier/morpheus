"""Spawn-from-trigger — watch GitHub for review-requested PRs, optionally
spawn a draft codex session in iTerm for each new one.

The daemon's watch loop calls `poll_and_handle()` every N seconds (config:
trigger.gh_poll_secs). Each new PR (not in seen_prs table) fires a 🐇 alert.
If config.trigger.spawn_from_gh_pr is true, the daemon also opens a new
iTerm tab pre-loaded with codex + the diff and registers a mission.

The autonomy gate consults ledger.is_within_daily_cap() before any paid
auto-spawn; if you're past your daily cap, the spawn is skipped (but the
alert still fires so you can act manually).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from morpheus import brief, db, iterm_client, ledger
from morpheus import config as cfg_mod
from morpheus.db import _connect


_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_prs (
    url             TEXT PRIMARY KEY,
    number          INTEGER NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    repo            TEXT NOT NULL DEFAULT '',
    spawned_tab_id  TEXT,
    seen_at         REAL NOT NULL
);
"""


def _init() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)


def list_seen() -> set[str]:
    _init()
    with _connect() as conn:
        rows = conn.execute("SELECT url FROM seen_prs").fetchall()
    return {r["url"] for r in rows}


def mark_seen(pr: brief.PR, spawned_tab_id: Optional[str] = None) -> None:
    _init()
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO seen_prs "
            "(url, number, title, repo, spawned_tab_id, seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (pr.url, pr.number, pr.title, pr.repo, spawned_tab_id, time.time()),
        )


def _build_codex_cmd(pr: brief.PR, draft_prompt: str) -> str:
    """Best-effort: produce a shell command that lands codex in the right
    repo at the right PR. We `gh pr checkout` into a worktree-ish directory
    then start codex with the draft prompt."""
    # The user is expected to be authenticated via gh.
    repo_root = f"~/github/{pr.repo}"
    branch_dir = f".claude/worktrees/pr-{pr.number}"
    return (
        f'cd {repo_root} 2>/dev/null && '
        f'(git worktree add {branch_dir} 2>/dev/null || true) && '
        f'cd {branch_dir} 2>/dev/null && '
        f'gh pr checkout {pr.number} 2>/dev/null; '
        f'codex'
    )


async def poll_and_handle(connection, on_alert=None) -> int:
    """One polling cycle. Returns count of NEW PRs discovered."""
    cfg = cfg_mod.load()
    if not cfg.get("trigger", {}).get("gh_poll_secs", 0):
        return 0

    spawn_draft = bool(cfg["trigger"].get("spawn_from_gh_pr", False))
    draft_prompt = cfg["trigger"].get("draft_prompt", "/adversarial-review")
    repos = cfg["trigger"].get("gh_repos", [])
    cap_dollars = float(cfg["autonomy"].get("daily_dollar_cap", 0.0))

    try:
        prs = brief.fetch_gh_review_queue(repos)
    except Exception as e:
        if on_alert:
            await on_alert("trigger_error", None, f"gh poll failed: {e}")
        return 0

    seen = list_seen()
    new_prs = [p for p in prs if p.url not in seen]
    spawned_count = 0

    for pr in new_prs:
        msg = f"NEW review-requested PR: {pr.repo}#{pr.number} — {pr.title}"
        if on_alert:
            await on_alert("new_pr", None, msg)

        spawned_tab: Optional[str] = None

        # Autonomy gate: only auto-spawn if config says so AND we're within budget.
        within_budget, today_spend = ledger.is_within_daily_cap(cap_dollars)
        if spawn_draft and connection is not None and within_budget:
            try:
                cmd = _build_codex_cmd(pr, draft_prompt)
                info = await iterm_client.spawn_tab(
                    connection, command=cmd,
                    goal=f"PR #{pr.number} {pr.title[:40]}",
                )
                if info is not None:
                    spawned_tab = info.tab_id
                    now = time.time()
                    m = db.Mission(
                        tab_id=info.tab_id, session_id=info.session_id,
                        goal=f"PR #{pr.number} {pr.title[:40]}",
                        state="working", cmd=cmd, linked_pr=pr.number,
                        buffer_changed_at=now, last_event_at=now, created_at=now,
                    )
                    db.upsert(m)
                    ledger.log_action(
                        "spawn_from_trigger",
                        tab_id=info.tab_id,
                        details={"pr_url": pr.url, "pr_number": pr.number, "cmd": cmd},
                    )
                    spawned_count += 1
                    if on_alert:
                        await on_alert(
                            "trigger_spawn", None,
                            f"draft session spawned for {pr.repo}#{pr.number}",
                        )
            except Exception as e:
                if on_alert:
                    await on_alert(
                        "trigger_error", None,
                        f"spawn failed for #{pr.number}: {e}",
                    )
        elif spawn_draft and not within_budget and on_alert:
            await on_alert(
                "trigger_capped", None,
                f"would auto-spawn for #{pr.number} but daily cap ${cap_dollars:.2f} hit "
                f"(spent ${today_spend:.2f}) — handle manually.",
            )

        mark_seen(pr, spawned_tab_id=spawned_tab)

    return len(new_prs)
