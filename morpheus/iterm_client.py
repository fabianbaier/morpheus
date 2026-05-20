"""Thin async wrapper around the iTerm2 Python API.

Every interactive call lives inside `async with iterm2.Connection.async_create()`.
For one-shot CLI commands use `run(coro)`; for the long-lived watcher use
`run_app(app_coro)` which yields the iterm2 App object on each tick.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

try:
    import iterm2
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "iterm2 Python package not installed. Run `pip install -e .` first."
    ) from e


@dataclass
class TabInfo:
    tab_id: str
    session_id: str
    window_id: str
    buffer: str
    current_name: str
    cwd: str = ""    # iTerm shell-integration "path" variable, empty if unavailable


async def _read_buffer(session: "iterm2.Session", max_lines: int = 200) -> str:
    """Read the trailing `max_lines` of the session's visible buffer."""
    try:
        contents = await session.async_get_screen_contents()
    except Exception:
        return ""
    n = min(contents.number_of_lines, max_lines)
    start = contents.number_of_lines - n
    lines = []
    for i in range(start, contents.number_of_lines):
        line = contents.line(i)
        if line is not None:
            lines.append(line.string)
    return "\n".join(lines)


async def enumerate_tabs(connection: "iterm2.Connection") -> list[TabInfo]:
    app = await iterm2.async_get_app(connection)
    if app is None:
        return []
    tabs: list[TabInfo] = []
    for window in app.windows:
        for tab in window.tabs:
            session = tab.current_session
            if session is None:
                continue
            buf = await _read_buffer(session)
            try:
                cwd = await session.async_get_variable("path") or ""
            except Exception:
                cwd = ""
            tabs.append(
                TabInfo(
                    tab_id=tab.tab_id,
                    session_id=session.session_id,
                    window_id=window.window_id,
                    buffer=buf,
                    current_name=session.name or "",
                    cwd=cwd,
                )
            )
    return tabs


async def set_tab_name(connection: "iterm2.Connection", session_id: str, name: str) -> bool:
    """Set the display name of the session in the given tab."""
    app = await iterm2.async_get_app(connection)
    if app is None:
        return False
    for window in app.windows:
        for tab in window.tabs:
            for session in tab.sessions:
                if session.session_id == session_id:
                    try:
                        await session.async_set_name(name)
                        return True
                    except Exception:
                        return False
    return False


async def spawn_tab(
    connection: "iterm2.Connection",
    command: str,
    goal: str = "",
    in_window_id: Optional[str] = None,
) -> Optional[TabInfo]:
    """Create a new tab in the active (or specified) window and run `command`."""
    app = await iterm2.async_get_app(connection)
    if app is None:
        return None

    target_window = None
    if in_window_id:
        target_window = next((w for w in app.windows if w.window_id == in_window_id), None)
    if target_window is None:
        target_window = app.current_terminal_window
    if target_window is None and app.windows:
        target_window = app.windows[0]
    if target_window is None:
        return None

    new_tab = await target_window.async_create_tab()
    if new_tab is None:
        return None
    session = new_tab.current_session
    if session is None:
        return None

    if goal:
        try:
            await session.async_set_name(f"NEW {goal}")
        except Exception:
            pass

    if command:
        await session.async_send_text(command.rstrip("\n") + "\n")

    return TabInfo(
        tab_id=new_tab.tab_id,
        session_id=session.session_id,
        window_id=target_window.window_id,
        buffer="",
        current_name=session.name or "",
    )


async def close_tab(connection: "iterm2.Connection", tab_id: str) -> bool:
    app = await iterm2.async_get_app(connection)
    if app is None:
        return False
    for window in app.windows:
        for tab in window.tabs:
            if tab.tab_id == tab_id:
                try:
                    await tab.async_close(force=True)
                    return True
                except Exception:
                    return False
    return False


def run(coro_factory: Callable[["iterm2.Connection"], Awaitable]):
    """Run a one-shot coroutine that needs an iTerm2 connection."""
    return iterm2.run_until_complete(coro_factory)


def run_app(loop_body: Callable[["iterm2.Connection"], Awaitable[None]]):
    """Run a long-lived loop body with a persistent iTerm2 connection.

    `loop_body` is expected to handle its own sleep/iteration and only return
    on shutdown.
    """
    iterm2.run_until_complete(loop_body)
