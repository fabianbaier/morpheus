"""Localhost HTTP + Server-Sent-Events bridge for the Morpheus desktop app.

Built on the Python standard library only (no extra runtime dependency) so it
runs on exactly the same interpreter the CLI uses and is fully testable offline.

Security model (a localhost bridge that can spawn processes / send keystrokes is
an RCE surface for any local process or web page, so this is not optional):

* Binds ``127.0.0.1`` only — never ``0.0.0.0``.
* Requires a per-launch bearer token on every request (``Authorization: Bearer``
  header, or ``?token=`` for SSE/EventSource which cannot set headers).
* Validates the ``Host`` header (must be loopback) to blunt DNS-rebinding.
* No permissive CORS — the SPA is served same-origin, so cross-origin callers get
  nothing.

The request logic lives in :func:`dispatch`, a pure function over
``(method, path, query, headers, body)`` returning ``(status, headers, body)``.
That lets the whole routing/auth layer be unit-tested without opening a socket;
:class:`_Handler` is a thin :mod:`http.server` adapter on top.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from morpheus.desktop import agents, bridge

WEB_ROOT = Path(__file__).resolve().parent / "web"

# Caps so a misbehaving client can't pin every request thread on SSE forever.
_MAX_SSE_CLIENTS = 8
_SSE_HEARTBEAT_SECS = 15.0
_SSE_POLL_SECS = 2.0

# Limit concurrent live agent turns (each spawns a real claude/codex subprocess).
_MAX_AGENT_TURNS = 4
_agent_semaphore = threading.BoundedSemaphore(_MAX_AGENT_TURNS)

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".woff2": "font/woff2",
}


class Config:
    """Per-launch server configuration (token, bind address, etc.)."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0, token: Optional[str] = None):
        self.host = host
        self.port = port
        self.token = token or secrets.token_urlsafe(24)


def _json(status: int, payload: Any) -> tuple[int, dict[str, str], bytes]:
    body = json.dumps(payload).encode("utf-8")
    return status, {"Content-Type": "application/json; charset=utf-8"}, body


def _err(status: int, message: str) -> tuple[int, dict[str, str], bytes]:
    return _json(status, {"ok": False, "error": message})


def _host_is_loopback(host_header: str) -> bool:
    if not host_header:
        return False
    hostname = host_header.split(":", 1)[0].strip().lower()
    return hostname in ("127.0.0.1", "localhost", "::1", "[::1]")


def _authorized(cfg: Config, headers: dict[str, str], query: dict[str, list[str]]) -> bool:
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        if secrets.compare_digest(auth[7:].strip(), cfg.token):
            return True
    qtok = (query.get("token") or [""])[0]
    if qtok and secrets.compare_digest(qtok, cfg.token):
        return True
    return False


def _read_json_body(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}


def _serve_static(path: str) -> tuple[int, dict[str, str], bytes]:
    rel = "index.html" if path in ("", "/", "/index.html") else path.lstrip("/")
    target = (WEB_ROOT / rel).resolve()
    # Path traversal guard: resolved target must stay inside WEB_ROOT.
    try:
        target.relative_to(WEB_ROOT)
    except ValueError:
        return _err(403, "forbidden")
    if not target.is_file():
        return _err(404, "not found")
    ctype = _CONTENT_TYPES.get(target.suffix, "application/octet-stream")
    return 200, {"Content-Type": ctype}, target.read_bytes()


def dispatch(
    cfg: Config,
    method: str,
    raw_path: str,
    headers: dict[str, str],
    body: bytes = b"",
) -> tuple[int, dict[str, str], bytes]:
    """Pure request router. Returns ``(status, headers, body_bytes)``.

    ``headers`` keys are expected lowercased. The ``/api/stream`` SSE route is
    handled by the HTTP adapter, not here (it needs a live socket); this function
    only validates auth for it and returns 200 so tests can assert reachability.
    """
    headers = {k.lower(): v for k, v in headers.items()}
    parsed = urlparse(raw_path)
    path = parsed.path
    query = parse_qs(parsed.query)

    # Host-header check (DNS-rebinding mitigation) for everything.
    if not _host_is_loopback(headers.get("host", "127.0.0.1")):
        return _err(403, "bad host")

    # Health check is unauthenticated (used by the Electron parent for readiness).
    if path == "/healthz":
        return _json(200, {"ok": True, "service": "morpheus-desktop"})

    is_api = path.startswith("/api/")
    is_stream = path == "/api/stream"

    # Auth required for everything except the unauthenticated health check and
    # static assets (the index/app/css are not secret; the data behind /api is).
    if is_api and not _authorized(cfg, headers, query):
        return _err(401, "unauthorized")

    if method == "GET":
        if is_stream:
            return 200, {"Content-Type": "text/event-stream"}, b""  # adapter streams
        if is_api:
            return _route_api_get(path, query)
        return _serve_static(path)

    if method == "POST":
        if not is_api:
            return _err(404, "not found")
        return _route_api_post(path, _read_json_body(body))

    return _err(405, "method not allowed")


def _route_api_get(path: str, query: dict[str, list[str]]) -> tuple[int, dict[str, str], bytes]:
    def q(name: str, default: str = "") -> str:
        return (query.get(name) or [default])[0]

    if path == "/api/fleet":
        return _json(200, bridge.fleet(tenant_id=q("tenant") or None))
    if path == "/api/sessions":
        return _json(200, {"sessions": bridge.sessions(tenant_id=q("tenant") or None)})
    if path.startswith("/api/sessions/"):
        ref = path[len("/api/sessions/"):]
        detail = bridge.mission_detail(ref)
        if detail is None:
            return _err(404, "mission not found")
        return _json(200, detail)
    if path == "/api/goals":
        return _json(200, {"goals": bridge.goals(tenant_id=q("tenant") or None)})
    if path == "/api/loops":
        return _json(200, {"loops": bridge.loops(tenant_id=q("tenant", ""))})
    if path == "/api/notes":
        return _json(200, {"notes": bridge.notes()})
    if path == "/api/activity":
        return _json(200, {"activity": bridge.activity_feed()})
    if path == "/api/spend":
        return _json(200, bridge.spend())
    if path == "/api/projects":
        return _json(200, {"projects": bridge.projects()})
    if path == "/api/agents":
        return _json(200, {"agents": agents.available_agents(), "cwd": os.getcwd()})
    if path.startswith("/api/loops/"):
        rest = path[len("/api/loops/"):]
        # /api/loops/{id}/runs/{run_id}/output → captured run output (text)
        parts = rest.split("/")
        if len(parts) == 4 and parts[1] == "runs" and parts[3] == "output":
            try:
                loop_id, run_id = int(parts[0]), int(parts[2])
            except ValueError:
                return _err(400, "bad loop/run id")
            result = bridge.loop_run_output(loop_id, run_id)
            if not result.get("ok"):
                return _err(404, result.get("error", "not found"))
            return _json(200, result)
        try:
            loop_id = int(rest)
        except ValueError:
            return _err(400, "bad loop id")
        detail = bridge.loop_detail(loop_id)
        if detail is None:
            return _err(404, "loop not found")
        return _json(200, detail)
    if path == "/api/feed":
        try:
            limit = int(q("limit", "50"))
            since = int(q("since_id", "0"))
        except ValueError:
            return _err(400, "bad limit/since_id")
        return _json(200, {"items": bridge.feed_items(limit=limit, since_id=since)})
    if path == "/api/feed/text":
        # Ultra-condensed plain text — the endpoint a minimal client (AR glasses,
        # a watch, curl) polls. One line per item, newest first.
        try:
            limit = int(q("limit", "20"))
        except ValueError:
            return _err(400, "bad limit")
        text = bridge.feed_text(limit=limit)
        return 200, {"Content-Type": "text/plain; charset=utf-8"}, text.encode("utf-8")
    if path == "/api/feed/rules":
        return _json(200, {"rules": bridge.feed_rules_list()})
    return _err(404, "not found")


def _route_api_post(path: str, data: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
    if path == "/api/chat":
        return _json(200, bridge.chat(
            str(data.get("message", "")),
            use_llm=bool(data.get("use_llm", True)),
            include_gh=bool(data.get("include_gh", False)),
        ))
    if path == "/api/notes":
        return _json(200, bridge.post_note(
            str(data.get("text", "")),
            kind=str(data.get("kind", "note")),
            tab_id=data.get("tab_id"),
        ))
    if path == "/api/broadcast":
        return _json(200, bridge.broadcast(
            str(data.get("text", "")),
            submit=bool(data.get("submit", True)),
        ))
    if path == "/api/spawn":
        return _json(200, bridge.spawn_session(
            str(data.get("goal", "")),
            str(data.get("command", "")),
        ))
    if path == "/api/send":
        return _json(200, bridge.send_to_session(
            str(data.get("tab_id", "")),
            str(data.get("text", "")),
            submit=bool(data.get("submit", True)),
        ))
    if path == "/api/loops":
        return _json(200, bridge.loop_create(
            str(data.get("name", "")),
            str(data.get("prompt", "")),
            every=str(data.get("every", "30m")),
            command=str(data.get("command", "")),
            feed_policy=str(data.get("feed_policy", "")),
            feed_pattern=str(data.get("feed_pattern", "")),
        ))
    if path.startswith("/api/loops/") and path.endswith("/action"):
        try:
            loop_id = int(path[len("/api/loops/"):-len("/action")])
        except ValueError:
            return _err(400, "bad loop id")
        return _json(200, bridge.loop_action(loop_id, str(data.get("action", ""))))
    if path.startswith("/api/loops/") and path.endswith("/feed-rule"):
        try:
            loop_id = int(path[len("/api/loops/"):-len("/feed-rule")])
        except ValueError:
            return _err(400, "bad loop id")
        return _json(200, bridge.loop_set_feed_rule(
            loop_id, str(data.get("policy", "")), str(data.get("pattern", ""))))
    if path == "/api/goals":
        return _json(200, bridge.goal_create(
            str(data.get("objective", "")),
            done_definition=str(data.get("done_definition", "")),
            source=str(data.get("source", "")),
            autonomy_level=str(data.get("autonomy_level", "ask_to_spawn")),
            max_turns=int(data.get("max_turns", 20) or 20),
            max_workers=int(data.get("max_workers", 3) or 3),
        ))
    if path.startswith("/api/goals/") and path.endswith("/action"):
        goal_ref = path[len("/api/goals/"):-len("/action")]
        return _json(200, bridge.goal_action(
            goal_ref, str(data.get("action", "")), reason=str(data.get("reason", ""))))
    if path == "/api/feed":
        return _json(200, bridge.feed_post(
            str(data.get("title", "")),
            str(data.get("body", "")),
            priority=int(data.get("priority", 0) or 0),
        ))
    return _err(404, "not found")


# ───────────────────────── SSE stream ─────────────────────────


def fleet_signature(snapshot: dict[str, Any]) -> str:
    """A cheap fingerprint of the cockpit state so the stream only pushes on change."""
    parts = [str(snapshot.get("counts"))]
    for s in snapshot.get("sessions", []):
        parts.append(f"{s.get('tab_id')}:{s.get('state')}:{s.get('buffer_changed_at')}")
    for n in snapshot.get("notes", [])[:5]:
        parts.append(f"n{n.get('id')}")
    return "|".join(parts)


# ───────────────────────── HTTP adapter ─────────────────────────


def make_handler(cfg: Config) -> type[BaseHTTPRequestHandler]:
    sse_clients = {"count": 0}
    sse_lock = threading.Lock()

    class _Handler(BaseHTTPRequestHandler):
        server_version = "Morpheus/desktop"
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # silence default stderr logging
            pass

        def _send(self, status: int, hdrs: dict[str, str], payload: bytes):
            self.send_response(status)
            for k, v in hdrs.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        def _headers_dict(self) -> dict[str, str]:
            return {k: v for k, v in self.headers.items()}

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/api/stream":
                self._stream(parsed)
                return
            status, hdrs, payload = dispatch(cfg, "GET", self.path, self._headers_dict())
            self._send(status, hdrs, payload)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else b""
            parsed = urlparse(self.path)
            if parsed.path == "/api/agent/turn":
                self._agent_turn(body)
                return
            status, hdrs, payload = dispatch(cfg, "POST", self.path, self._headers_dict(), body)
            self._send(status, hdrs, payload)

        def _agent_turn(self, body: bytes):
            """Stream a live agent turn (claude/codex/gemini) as SSE events."""
            hdrs = {k.lower(): v for k, v in self.headers.items()}
            if not _host_is_loopback(hdrs.get("host", "127.0.0.1")):
                self._send(*_err(403, "bad host"))
                return
            if not _authorized(cfg, hdrs, {}):
                self._send(*_err(401, "unauthorized"))
                return
            if not _agent_semaphore.acquire(blocking=False):
                self._send(*_err(503, "too many concurrent agent turns"))
                return
            data = _read_json_body(body)
            proc_box: dict[str, Any] = {}
            gen = None
            # A turn is a finite stream; close the connection at the end so the
            # client's read returns instead of waiting on keep-alive for EOF.
            self.close_connection = True
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                gen = agents.run_turn(
                    kind=str(data.get("agent", "claude")),
                    message=str(data.get("message", "")),
                    cwd=(data.get("cwd") or None),
                    session_ref=str(data.get("session_ref", "")),
                    permission_mode=str(data.get("permission_mode", "default")),
                    allowed_tools=(data.get("allowed_tools") or None),
                    model=str(data.get("model", "")),
                    on_process=lambda p: proc_box.__setitem__("p", p),
                )
                for ev in gen:
                    frame = f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n"
                    self.wfile.write(frame.encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                p = proc_box.get("p")
                if p is not None:
                    agents._safe_kill(p)
            finally:
                if gen is not None:
                    gen.close()
                _agent_semaphore.release()

        def _stream(self, parsed):
            query = parse_qs(parsed.query)
            if not _host_is_loopback(self.headers.get("Host", "127.0.0.1")):
                self._send(*_err(403, "bad host"))
                return
            if not _authorized(cfg, {k.lower(): v for k, v in self.headers.items()}, query):
                self._send(*_err(401, "unauthorized"))
                return
            with sse_lock:
                if sse_clients["count"] >= _MAX_SSE_CLIENTS:
                    self._send(*_err(503, "too many stream clients"))
                    return
                sse_clients["count"] += 1
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                self._stream_loop()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with sse_lock:
                    sse_clients["count"] -= 1

        def _stream_loop(self):
            from morpheus import feeds

            last_sig = None
            last_beat = 0.0
            last_feed_id = feeds.latest_id()
            while True:
                snapshot = bridge.fleet()
                sig = fleet_signature(snapshot)
                now = time.time()
                if sig != last_sig:
                    last_sig = sig
                    chunk = f"event: fleet\ndata: {json.dumps(snapshot)}\n\n"
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
                    last_beat = now
                # Push newly arrived feed items as their own event so a feed
                # subscriber (desktop, glasses relay) gets them immediately.
                new_items = bridge.feed_items(limit=20, since_id=last_feed_id)
                if new_items:
                    last_feed_id = max(it["id"] for it in new_items)
                    chunk = f"event: feed\ndata: {json.dumps({'items': new_items})}\n\n"
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
                    last_beat = now
                if now - last_beat >= _SSE_HEARTBEAT_SECS:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    last_beat = now
                time.sleep(_SSE_POLL_SECS)

    return _Handler


class DesktopServer:
    """Owns the ThreadingHTTPServer lifecycle."""

    def __init__(self, cfg: Optional[Config] = None):
        self.cfg = cfg or Config()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        return f"http://{self.cfg.host}:{self.cfg.port}/?token={self.cfg.token}"

    def start(self) -> "DesktopServer":
        handler = make_handler(self.cfg)
        self._httpd = ThreadingHTTPServer((self.cfg.host, self.cfg.port), handler)
        self._httpd.daemon_threads = True
        # If port was 0 the OS picked one; record the real value.
        self.cfg.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def handshake(self) -> dict[str, Any]:
        """The single JSON line the Electron parent reads from stdout to learn
        where to point the window and which token to use."""
        return {
            "service": "morpheus-desktop",
            "host": self.cfg.host,
            "port": self.cfg.port,
            "token": self.cfg.token,
            "url": self.url,
        }

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def serve_forever_blocking(self):
        if self._thread is not None:
            self._thread.join()


def serve(
    host: str = "127.0.0.1",
    port: int = 0,
    token: Optional[str] = None,
    *,
    on_ready: Optional[Callable[[DesktopServer], None]] = None,
    block: bool = True,
) -> DesktopServer:
    """Start the desktop bridge server. Prints the handshake JSON line on ready."""
    server = DesktopServer(Config(host=host, port=port, token=token)).start()
    if on_ready is not None:
        on_ready(server)
    if block:
        try:
            server.serve_forever_blocking()
        except KeyboardInterrupt:
            server.stop()
    return server
