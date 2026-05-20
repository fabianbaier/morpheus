"""The watch loop — polls all iTerm tabs, detects state, updates titles + DB."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from morpheus import config as cfg_mod
from morpheus import context as ctx_mod
from morpheus import daemon as daemon_mod
from morpheus import db, detect, iterm_client, naming

LOG_DIR = Path.home() / ".morpheus"
LOG_PATH = LOG_DIR / "morpheus.log"

# In-memory per-process tracking (rebuilt on restart):
#   _working_since[tab_id] = unix ts when this tab entered the 'working' state
#   _token_warned[tab_id]  = highest threshold (in minutes) already alerted for
#   _collisions_seen       = set of frozenset({tab_id, tab_id, ...}) already alerted
_working_since: dict[str, float] = {}
_token_warned: dict[str, int] = {}
_collisions_seen: set[frozenset] = set()


def setup_logging(verbose: bool = False) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("morpheus")
    if log.handlers:
        return log
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.FileHandler(LOG_PATH)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(fh)
    return log


async def _tick(connection, log: logging.Logger,
                on_state_change=None, on_alert=None, on_tab_observed=None) -> int:
    """One observation cycle. Returns count of tabs processed.

    `on_alert(kind, mission, text)` fires for v0.4 derived alerts (token guard,
    worktree collision). Callers wire it to push to UI + system notifications.
    """
    tabs = await iterm_client.enumerate_tabs(connection)
    seen_ids = []
    now = time.time()
    cfg = cfg_mod.load()

    # tab_id -> cwd  for worktree-collision detection at end of tick.
    cwd_by_tab: dict[str, str] = {}

    for tab in tabs:
        seen_ids.append(tab.tab_id)
        # Skip the Morpheus tab itself.
        if naming.is_morpheus_tab(tab.current_name):
            continue

        prev = db.get(tab.tab_id) or db.Mission(
            tab_id=tab.tab_id,
            session_id=tab.session_id,
            buffer_changed_at=now,
            created_at=now,
        )
        prev.session_id = tab.session_id

        d = detect.detect(
            tab.buffer,
            prev.buffer_hash,
            prev.buffer_changed_at,
            now=now,
        )
        new_state = d.state
        prev_state = prev.state

        if d.changed:
            prev.buffer_changed_at = now
        prev.buffer_hash = d.buffer_hash
        prev.last_event = d.last_event
        prev.last_event_at = now

        if not prev.goal:
            # Best-effort initial goal from the current tab name (if it isn't blank).
            cand = tab.current_name.strip()
            if cand and cand.lower() not in ("zsh", "bash", "sh", "fish"):
                prev.goal = cand

        # Worktree tracking from iTerm shell-integration `path` variable.
        if tab.cwd:
            prev.linked_worktree = tab.cwd
            cwd_by_tab[tab.tab_id] = tab.cwd

        prev.state = new_state
        db.upsert(prev)

        if on_tab_observed is not None:
            await on_tab_observed(tab, prev, d)

        age = naming.now_minus(prev.buffer_changed_at)
        new_title = naming.build_tab_title(prev.goal, new_state, d.last_event, age)

        # Only push a tab name update if it actually differs from current.
        if new_title != tab.current_name:
            ok = await iterm_client.set_tab_name(connection, tab.session_id, new_title)
            if not ok:
                log.debug("set_tab_name failed for %s", tab.tab_id)

        if prev_state != new_state and on_state_change is not None:
            await on_state_change(prev, prev_state, new_state)

        # ── token-budget guard ────────────────────────────────────────────
        if cfg["token_guard"].get("enabled", True) and on_alert is not None:
            await _check_token_guard(prev, prev_state, new_state, cfg, on_alert, now)

    # ── worktree-collision detection (per-tick) ───────────────────────────
    if cfg["worktree"].get("warn_on_collision", True) and on_alert is not None:
        await _check_worktree_collisions(cwd_by_tab, on_alert)

    # Drop missions for tabs that no longer exist.
    deleted = db.reconcile_missing(seen_ids)
    if deleted:
        log.info("reconciled: removed %d stale missions", deleted)
        # Clean per-process tracking for vanished tabs.
        for k in list(_working_since.keys()):
            if k not in seen_ids:
                _working_since.pop(k, None)
                _token_warned.pop(k, None)

    # Refresh shared context snapshot so agents in other tabs can read it.
    try:
        ctx_mod.write_context_file()
        ctx_mod.write_context_json()
    except Exception as e:
        log.exception("context write failed: %s", e)

    # Heartbeat so `morpheus daemon-status` can tell we're alive.
    daemon_mod.write_beacon()

    return len(tabs)


async def _check_token_guard(mission: db.Mission, prev_state: str, new_state: str,
                              cfg: dict, on_alert, now: float) -> None:
    """Fire warn / snapshot alerts when a session has been working too long."""
    warn_min = int(cfg["token_guard"].get("warn_minutes", 60))
    snap_min = int(cfg["token_guard"].get("snapshot_minutes", 120))

    if new_state == "working":
        if mission.tab_id not in _working_since:
            _working_since[mission.tab_id] = now
        elapsed_min = (now - _working_since[mission.tab_id]) / 60.0
        already_warned = _token_warned.get(mission.tab_id, 0)
        if elapsed_min >= snap_min and already_warned < snap_min:
            _token_warned[mission.tab_id] = snap_min
            await on_alert(
                "token_snapshot", mission,
                f"[{mission.goal or mission.tab_id.split('-')[0]}] working "
                f"{int(elapsed_min)}min straight — SNAPSHOT NOW: `morpheus snapshot {mission.tab_id.split('-')[0]}`",
            )
        elif elapsed_min >= warn_min and already_warned < warn_min:
            _token_warned[mission.tab_id] = warn_min
            await on_alert(
                "token_warn", mission,
                f"[{mission.goal or mission.tab_id.split('-')[0]}] working "
                f"{int(elapsed_min)}min — consider snapshotting before token blowup",
            )
    elif prev_state == "working":
        # Left the working state; reset tracking so a fresh working stretch starts clean.
        _working_since.pop(mission.tab_id, None)
        _token_warned.pop(mission.tab_id, None)


async def _check_worktree_collisions(cwd_by_tab: dict[str, str], on_alert) -> None:
    """Push a 🐇 alert when two LIVE tabs share the same cwd."""
    by_cwd: dict[str, list[str]] = {}
    for tab_id, cwd in cwd_by_tab.items():
        if not cwd:
            continue
        by_cwd.setdefault(cwd, []).append(tab_id)
    for cwd, tabs in by_cwd.items():
        if len(tabs) < 2:
            continue
        key = frozenset(tabs)
        if key in _collisions_seen:
            continue
        _collisions_seen.add(key)
        # Look up goals for nicer message.
        goals = []
        for tab_id in tabs:
            m = db.get(tab_id)
            label = m.goal if (m and m.goal) else tab_id.split("-")[0]
            goals.append(label)
        await on_alert(
            "worktree_collision", None,
            f"COLLISION in {cwd}: {' & '.join(goals)} — resolve before pushing.",
        )


def watch_loop(
    poll_interval: float = 5.0,
    on_state_change=None,
    on_new_mission=None,
    on_closed_mission=None,
    on_new_note=None,
    on_alert=None,
    gh_poll_secs: float = 0.0,
) -> None:
    """Headless watch loop. Synchronous wrapper around an asyncio event loop.

    All callbacks are async. `on_state_change(mission, old, new)` fires when a
    mission's classified state changes between ticks. `on_new_mission(mission)`
    fires when a tab that wasn't in the DB before appears. `on_closed_mission(
    tab_id)` fires for tabs we lose. `on_new_note(note)` fires for any note
    rows that weren't present at startup or last tick.
    """
    log = setup_logging()
    log.info("morpheus watch started (poll=%.1fs)", poll_interval)

    try:
        last_seen_tabs = {m.tab_id for m in db.all_missions()}
    except Exception:
        last_seen_tabs = set()
    try:
        recent = db.recent_notes(limit=1)
        last_note_id = recent[0].id if recent else 0
    except Exception:
        last_note_id = 0
    last_gh_poll = 0.0

    async def body(connection):
        nonlocal last_seen_tabs, last_note_id, last_gh_poll
        while True:
            try:
                n = await _tick(connection, log,
                                 on_state_change=on_state_change,
                                 on_alert=on_alert)

                # Periodic GH poll → spawn-from-trigger.
                if gh_poll_secs > 0:
                    now_t = time.time()
                    if now_t - last_gh_poll >= gh_poll_secs:
                        last_gh_poll = now_t
                        try:
                            from morpheus import trigger as trigger_mod
                            await trigger_mod.poll_and_handle(connection, on_alert)
                        except Exception as e:
                            log.exception("gh poll error: %s", e)
                log.debug("tick: %d tabs", n)

                # Detect new + closed missions for notification hooks.
                if on_new_mission or on_closed_mission:
                    try:
                        missions = db.all_missions()
                        current = {m.tab_id for m in missions}
                        new_tabs = current - last_seen_tabs
                        closed_tabs = last_seen_tabs - current
                        by_id = {m.tab_id: m for m in missions}
                        if on_new_mission:
                            for t in new_tabs:
                                m = by_id.get(t)
                                if m:
                                    await on_new_mission(m)
                        if on_closed_mission:
                            for t in closed_tabs:
                                await on_closed_mission(t)
                        last_seen_tabs = current
                    except Exception as e:
                        log.exception("new/closed-mission hook failed: %s", e)

                # Detect new notes.
                if on_new_note:
                    try:
                        recent_notes = db.recent_notes(limit=12)
                        fresh = [nn for nn in recent_notes if nn.id > last_note_id]
                        for nn in sorted(fresh, key=lambda x: x.created_at):
                            await on_new_note(nn)
                        if fresh:
                            last_note_id = max(nn.id for nn in fresh)
                    except Exception as e:
                        log.exception("new-note hook failed: %s", e)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("tick error: %s", e)
            await asyncio.sleep(poll_interval)

    try:
        iterm_client.run_app(body)
    except KeyboardInterrupt:
        log.info("morpheus watch stopped")
