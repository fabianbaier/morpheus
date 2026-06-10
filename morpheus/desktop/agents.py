"""Drive real agent CLIs (claude / codex / gemini) and stream their output.

This is what makes the desktop chat *feel* like Claude Code / Codex while under
the hood it's translating to and from those CLIs. Each adapter spawns the agent
in its headless/streaming mode, reads its line-delimited output, and normalises
it into a single event schema the UI can render as native-feeling tool use, web
search, file edits, and streamed text.

Normalised event ``type`` values (all dicts, JSON-serialisable):

* ``session``     {session_id, model, tools, cwd}        — turn started
* ``thinking``    {text}                                  — model reasoning
* ``text``        {text}                                  — assistant prose
* ``tool_use``    {id, name, input, summary}              — a tool call started
* ``web_search``  {query}                                 — server-side web search
* ``web_fetch``   {url}                                   — server-side web fetch
* ``tool_result`` {tool_use_id, is_error, content}        — a tool returned
* ``summary``     {text}                                  — post-turn summary
* ``result``      {text, cost_usd, web_searches, session_id, is_error}
* ``error``       {message}

Multi-turn: capture ``session_id`` from the ``session``/``result`` events and pass
it back as ``session_ref`` on the next turn so the conversation continues with
full context (``claude --resume``, ``codex exec resume``).

The read surface here is OS-agnostic and testable: ``parse_line`` is pure, and
``run_turn`` accepts an ``argv`` override so tests can point it at a fake agent
script that replays a captured fixture — no network, credits, or real CLI needed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from morpheus import ledger

DEFAULT_TURN_TIMEOUT = 900.0  # agents can run long; the client can abort sooner.

# Tools that are server-side web actions get their own event type so the UI can
# render them as a "searching the web" affordance like Claude Code / Codex.
_WEB_SEARCH_NAMES = {"WebSearch", "web_search", "web.search"}
_WEB_FETCH_NAMES = {"WebFetch", "web_fetch", "web.fetch"}


def _summarize_tool_input(name: str, inp: dict[str, Any]) -> str:
    """A one-line human summary of a tool call, à la Claude Code's tool headers."""
    if not isinstance(inp, dict):
        return ""
    for key in ("file_path", "path", "command", "query", "url", "pattern", "prompt", "description"):
        if key in inp and inp[key]:
            val = str(inp[key])
            return val if len(val) <= 120 else val[:119] + "…"
    return ", ".join(f"{k}={v}" for k, v in list(inp.items())[:2])[:120]


# ───────────────────────── adapters ─────────────────────────


@dataclass
class AgentAdapter:
    """Base adapter. Subclasses implement build_command + parse_line."""

    kind: str = ""
    executable: str = ""
    label: str = ""
    supports_resume: bool = False
    structured: bool = True  # emits parseable tool events vs. plain text

    def is_available(self) -> bool:
        return shutil.which(self.executable) is not None

    def build_command(self, message: str, *, session_ref: str = "",
                      permission_mode: str = "default",
                      allowed_tools: Optional[list[str]] = None,
                      model: str = "") -> list[str]:
        raise NotImplementedError

    def parse_line(self, raw: str) -> list[dict[str, Any]]:
        raise NotImplementedError


class ClaudeAdapter(AgentAdapter):
    def __init__(self):
        super().__init__(kind="claude", executable="claude", label="Claude Code",
                         supports_resume=True, structured=True)

    def build_command(self, message, *, session_ref="", permission_mode="default",
                      allowed_tools=None, model=""):
        cmd = [self.executable, "-p", message,
               "--output-format", "stream-json", "--verbose"]
        if session_ref:
            cmd += ["--resume", session_ref]
        if permission_mode:
            cmd += ["--permission-mode", permission_mode]
        if allowed_tools:
            cmd += ["--allowedTools", *allowed_tools]
        if model:
            cmd += ["--model", model]
        return cmd

    def parse_line(self, raw):
        raw = raw.strip()
        if not raw:
            return []
        try:
            o = json.loads(raw)
        except ValueError:
            return []
        t = o.get("type")
        events: list[dict[str, Any]] = []
        if t == "system" and o.get("subtype") == "init":
            events.append({"type": "session", "session_id": o.get("session_id", ""),
                           "model": o.get("model", ""), "tools": o.get("tools", []),
                           "cwd": o.get("cwd", "")})
        elif t == "system" and o.get("subtype") == "post_turn_summary":
            if o.get("status_detail"):
                events.append({"type": "summary", "text": o["status_detail"]})
        elif t == "assistant":
            for b in o.get("message", {}).get("content", []):
                bt = b.get("type")
                if bt == "text" and b.get("text"):
                    events.append({"type": "text", "text": b["text"]})
                elif bt == "thinking":
                    events.append({"type": "thinking", "text": b.get("thinking") or b.get("text", "")})
                elif bt == "tool_use":
                    name = b.get("name", "")
                    inp = b.get("input", {}) or {}
                    if name in _WEB_SEARCH_NAMES:
                        events.append({"type": "web_search", "query": str(inp.get("query", ""))})
                    elif name in _WEB_FETCH_NAMES:
                        events.append({"type": "web_fetch", "url": str(inp.get("url", ""))})
                    else:
                        events.append({"type": "tool_use", "id": b.get("id", ""), "name": name,
                                       "input": inp, "summary": _summarize_tool_input(name, inp)})
        elif t == "user":
            content = o.get("message", {}).get("content", [])
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        c = b.get("content")
                        events.append({"type": "tool_result", "tool_use_id": b.get("tool_use_id", ""),
                                       "is_error": bool(b.get("is_error")), "content": _flatten_content(c)})
        elif t == "result":
            usage = o.get("usage", {}) or {}
            stu = usage.get("server_tool_use", {}) or {}
            events.append({"type": "result", "text": o.get("result", ""),
                           "cost_usd": float(o.get("total_cost_usd") or 0.0),
                           "web_searches": int(stu.get("web_search_requests") or 0),
                           "session_id": o.get("session_id", ""),
                           "is_error": bool(o.get("is_error"))})
        return events


class CodexAdapter(AgentAdapter):
    """Best-effort adapter for `codex exec --json`. Codex's event schema has
    shifted across versions, so this maps the known shapes and otherwise degrades
    any line with visible text to a ``text`` event rather than dropping it."""

    def __init__(self):
        super().__init__(kind="codex", executable="codex", label="Codex",
                         supports_resume=True, structured=True)

    def build_command(self, message, *, session_ref="", permission_mode="default",
                      allowed_tools=None, model=""):
        cmd = [self.executable, "exec", "--json"]
        if session_ref:
            cmd += ["resume", session_ref]
        if model:
            cmd += ["-m", model]
        cmd += ["--skip-git-repo-check", message]
        return cmd

    def parse_line(self, raw):
        raw = raw.strip()
        if not raw:
            return []
        try:
            o = json.loads(raw)
        except ValueError:
            # Non-JSON lines from codex are human-readable progress; surface them.
            return [{"type": "text", "text": raw}] if not raw.startswith(("[", "{")) else []
        # newer schema: {"type":"item.completed","item":{...}} / {"type":"thread.started",...}
        if o.get("type") == "thread.started" or "thread_id" in o:
            return [{"type": "session", "session_id": o.get("thread_id", ""), "model": "", "tools": [], "cwd": ""}]
        item = o.get("item") or o.get("msg") or {}
        itype = item.get("type") or o.get("type", "")
        text = item.get("text") or item.get("message") or item.get("content") or ""
        if itype in ("assistant_message", "agent_message"):
            return [{"type": "text", "text": _flatten_content(text)}] if text else []
        if itype in ("reasoning", "agent_reasoning"):
            return [{"type": "thinking", "text": _flatten_content(text)}] if text else []
        if itype in ("command_execution", "exec_command_begin"):
            cmd = item.get("command") or item.get("cmd") or ""
            return [{"type": "tool_use", "id": item.get("id", ""), "name": "Bash",
                     "input": {"command": cmd}, "summary": _flatten_content(cmd)[:120]}]
        if itype in ("web_search", "web_search_begin"):
            return [{"type": "web_search", "query": _flatten_content(item.get("query", ""))}]
        if itype in ("file_change", "patch_apply_begin"):
            return [{"type": "tool_use", "id": item.get("id", ""), "name": "Edit",
                     "input": item, "summary": _flatten_content(str(item.get("path", "")))[:120]}]
        if o.get("type") in ("turn.completed", "task_complete"):
            usage = o.get("usage") or item.get("usage") or {}
            return [{"type": "result", "text": _flatten_content(text), "cost_usd": 0.0,
                     "web_searches": 0, "session_id": "", "is_error": False}]
        return []


class TextAgentAdapter(AgentAdapter):
    """Fallback for agents without structured streaming (e.g. gemini). Streams
    stdout lines as ``text`` events and synthesises a final ``result``."""

    def __init__(self, kind="gemini", executable="gemini", label="Gemini"):
        super().__init__(kind=kind, executable=executable, label=label,
                         supports_resume=False, structured=False)
        self._buf: list[str] = []

    def build_command(self, message, *, session_ref="", permission_mode="default",
                      allowed_tools=None, model=""):
        cmd = [self.executable, "-p", message]
        if model:
            cmd += ["-m", model]
        return cmd

    def parse_line(self, raw):
        line = raw.rstrip("\n")
        self._buf.append(line)
        return [{"type": "text", "text": line + "\n"}] if line else []


def _flatten_content(c: Any) -> str:
    """Tool results / content can be a string or a list of content blocks."""
    if c is None:
        return ""
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for b in c:
            if isinstance(b, dict):
                parts.append(b.get("text") or b.get("content") or json.dumps(b))
            else:
                parts.append(str(b))
        return "\n".join(parts)
    if isinstance(c, dict):
        return c.get("text") or json.dumps(c)
    return str(c)


# ───────────────────────── registry + runner ─────────────────────────

_ADAPTERS = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "gemini": lambda: TextAgentAdapter("gemini", "gemini", "Gemini"),
}


def get_adapter(kind: str) -> Optional[AgentAdapter]:
    factory = _ADAPTERS.get(kind)
    return factory() if factory else None


def available_agents() -> list[dict[str, Any]]:
    """Which agent CLIs are installed, for the UI's agent picker."""
    out = []
    for kind, factory in _ADAPTERS.items():
        a = factory()
        out.append({"kind": kind, "label": a.label, "available": a.is_available(),
                    "supports_resume": a.supports_resume, "structured": a.structured})
    return out


def run_turn(
    kind: str,
    message: str,
    *,
    cwd: Optional[str] = None,
    session_ref: str = "",
    permission_mode: str = "default",
    allowed_tools: Optional[list[str]] = None,
    model: str = "",
    timeout: float = DEFAULT_TURN_TIMEOUT,
    argv: Optional[list[str]] = None,
    on_process: Optional[Any] = None,
) -> Iterator[dict[str, Any]]:
    """Run one agent turn, yielding normalised events as they stream in.

    ``argv`` overrides the spawned command (for tests / a fake agent). ``cwd`` is
    the working directory the agent operates in. ``on_process`` (if given) is
    called with the Popen so a caller can terminate the turn early.
    """
    adapter = get_adapter(kind)
    if adapter is None:
        yield {"type": "error", "message": f"unknown agent '{kind}'"}
        return
    if argv is None:
        if not adapter.is_available():
            yield {"type": "error",
                   "message": f"{adapter.label} CLI ('{adapter.executable}') is not installed or not on PATH."}
            return
        argv = adapter.build_command(message, session_ref=session_ref,
                                     permission_mode=permission_mode,
                                     allowed_tools=allowed_tools, model=model)

    workdir = cwd or os.getcwd()
    if not os.path.isdir(workdir):
        yield {"type": "error", "message": f"working directory does not exist: {workdir}"}
        return

    try:
        proc = subprocess.Popen(
            argv, cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
    except (OSError, ValueError) as e:
        yield {"type": "error", "message": str(e)}
        return

    if on_process is not None:
        on_process(proc)

    # Watchdog kills the process on timeout so a hung agent can't stream forever.
    timer = threading.Timer(timeout, _safe_kill, args=(proc,))
    timer.daemon = True
    timer.start()

    saw_result = False
    session_id = ""
    try:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                for ev in adapter.parse_line(line):
                    if ev["type"] in ("session", "result") and ev.get("session_id"):
                        session_id = ev["session_id"]
                    if ev["type"] == "result":
                        saw_result = True
                        if ev.get("cost_usd"):
                            try:
                                ledger.log_cost(kind=f"agent:{kind}",
                                                description=message[:120],
                                                dollars=float(ev["cost_usd"]))
                            except Exception:
                                pass
                    yield ev
        except (BrokenPipeError, ConnectionResetError):
            _safe_kill(proc)
        # Normal completion: emit a terminal event if the agent didn't.
        rc = proc.wait()
        stderr = (proc.stderr.read() if proc.stderr else "") or ""
        if not saw_result:
            if rc != 0:
                yield {"type": "error", "message": stderr.strip()[:400] or f"agent exited with code {rc}"}
            else:
                # text-only agents (gemini) never emit a structured result
                yield {"type": "result", "text": "", "cost_usd": 0.0, "web_searches": 0,
                       "session_id": session_id, "is_error": False}
    finally:
        # Runs on normal exit AND on early generator close (client disconnect):
        # cancel the watchdog, reap the process, and close the pipes (no FD leak).
        timer.cancel()
        if proc.poll() is None:
            _safe_kill(proc)
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass


def _safe_kill(proc: subprocess.Popen) -> None:
    try:
        if proc.poll() is None:
            proc.terminate()
    except Exception:
        pass
