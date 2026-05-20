"""The watch loop — polls all iTerm tabs, detects state, updates titles + DB."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from morpheus import context as ctx_mod
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

    return len(tabs)


async def watch_loop(poll_interval: float = 5.0, on_state_change=None) -> None:
    log = setup_logging()
    log.info("morpheus watch started (poll=%.1fs)", poll_interval)

    async def body(connection):
        while True:
            try:
                n = await _tick(connection, log, on_state_change=on_state_change)
                log.debug("tick: %d tabs", n)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("tick error: %s", e)
            await asyncio.sleep(poll_interval)

    try:
        iterm_client.run_app(body)
    except KeyboardInterrupt:
        log.info("morpheus watch stopped")
