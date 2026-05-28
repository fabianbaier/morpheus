"""Morpheus desktop app — a chat-agent cockpit over the mission graph.

This package adds a desktop experience on top of the existing Morpheus CLI:

* ``bridge``  — an OS-agnostic domain layer that turns the SQLite-backed mission
  state (the same ``~/.morpheus/morpheus.db`` the CLI and daemon use) into plain
  JSON-serialisable dicts, plus a handful of write/control operations. Pure and
  fully unit-testable; no sockets, no iTerm2 required for the read surface.
* ``server`` — a dependency-light localhost HTTP + Server-Sent-Events bridge that
  exposes the ``bridge`` over REST/SSE and serves the static web UI in ``web/``.
* ``web``    — a vanilla HTML/CSS/JS single-page chat app styled after Claude
  Code / Codex, tailored to Morpheus (mission graph, sessions, goals, loops).
* ``electron`` (sibling dir) — a thin Electron shell that packages the above as a
  native macOS ``.app``/``.dmg``.

The desktop app is "compatible" with the CLI by construction: it reads and writes
the same database and ``config.toml``, so state stays in sync no matter which
front-end you use.
"""

from __future__ import annotations

__all__ = ["bridge", "server"]
