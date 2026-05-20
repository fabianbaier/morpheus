"""The watch loop — polls all iTerm tabs, detects state, updates titles + DB."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from morpheus import context as ctx_mod
from morpheus import daemon as daemon_mod
from morpheus import db, detect, iterm_client, naming

LOG_DIR = Path.home() / ".morpheus"
LOG_PATH = LOG_DIR / "morpheus.log"


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


async def _tick(connection, log: logging.Logger, on_state_change=None) -> int:
    """One observation cycle. Returns count of tabs processed."""
    tabs = await iterm_client.enumerate_tabs(connection)
    seen_ids = []
    now = time.time()

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

        prev.state = new_state
        db.upsert(prev)

        age = naming.now_minus(prev.buffer_changed_at)
        new_title = naming.build_tab_title(prev.goal, new_state, d.last_event, age)

        # Only push a tab name update if it actually differs from current.
        if new_title != tab.current_name:
            ok = await iterm_client.set_tab_name(connection, tab.session_id, new_title)
            if not ok:
                log.debug("set_tab_name failed for %s", tab.tab_id)

        if prev_state != new_state and on_state_change is not None:
            await on_state_change(prev, prev_state, new_state)

    # Drop missions for tabs that no longer exist.
    deleted = db.reconcile_missing(seen_ids)
    if deleted:
        log.info("reconciled: removed %d stale missions", deleted)

    # Refresh shared context snapshot so agents in other tabs can read it.
    try:
        ctx_mod.write_context_file()
        ctx_mod.write_context_json()
    except Exception as e:
        log.exception("context write failed: %s", e)

    # Heartbeat so `morpheus daemon-status` can tell we're alive.
    daemon_mod.write_beacon()

    return len(tabs)


def watch_loop(
    poll_interval: float = 5.0,
    on_state_change=None,
    on_new_mission=None,
    on_closed_mission=None,
    on_new_note=None,
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

    async def body(connection):
        nonlocal last_seen_tabs, last_note_id
        while True:
            try:
                n = await _tick(connection, log, on_state_change=on_state_change)
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
