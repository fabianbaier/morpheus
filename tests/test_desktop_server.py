"""Tests for the desktop HTTP/SSE bridge server.

The routing + auth layer is exercised through the pure `dispatch()` function (no
socket needed), plus one real end-to-end test that binds a loopback socket on an
OS-assigned port and drives it with urllib.
"""

import json
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from morpheus import db
from morpheus.desktop import agents, server


class _TempDB:
    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._p = [
            patch.object(db, "DB_DIR", root),
            patch.object(db, "DB_PATH", root / "morpheus.db"),
        ]
        for p in self._p:
            p.start()
        # seed a mission so /api/fleet has content
        db.upsert(db.Mission(tab_id="tab-1", session_id="s1", goal="demo",
                             state="working", cmd="codex"))
        return root

    def __exit__(self, *a):
        for p in reversed(self._p):
            p.stop()
        self._tmp.cleanup()


CFG = server.Config(host="127.0.0.1", port=12345, token="secret-token")
LOOPBACK = {"Host": "127.0.0.1:12345"}


def _get(path, headers=None):
    h = dict(LOOPBACK)
    if headers:
        h.update(headers)
    return server.dispatch(CFG, "GET", path, h)


def _post(path, body, headers=None):
    h = dict(LOOPBACK)
    if headers:
        h.update(headers)
    return server.dispatch(CFG, "POST", path, h, json.dumps(body).encode())


AUTH = {"Authorization": "Bearer secret-token"}


class DispatchAuthTest(unittest.TestCase):
    def test_healthz_no_auth(self):
        status, hdrs, body = _get("/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_api_requires_token(self):
        with _TempDB():
            self.assertEqual(_get("/api/fleet")[0], 401)

    def test_api_with_bearer_token(self):
        with _TempDB():
            status, hdrs, body = _get("/api/fleet", AUTH)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["health"]["total"], 1)

    def test_api_with_query_token(self):
        with _TempDB():
            self.assertEqual(_get("/api/fleet?token=secret-token")[0], 200)

    def test_wrong_token_rejected(self):
        with _TempDB():
            self.assertEqual(_get("/api/fleet", {"Authorization": "Bearer nope"})[0], 401)

    def test_bad_host_rejected(self):
        status, _, _ = server.dispatch(CFG, "GET", "/healthz", {"Host": "evil.com"})
        self.assertEqual(status, 403)


class DispatchRouteTest(unittest.TestCase):
    def test_static_index_served(self):
        status, hdrs, body = _get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", hdrs["Content-Type"])
        self.assertIn(b"Morpheus", body)

    def test_static_app_js_served(self):
        status, hdrs, body = _get("/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", hdrs["Content-Type"])

    def test_path_traversal_blocked(self):
        status, _, _ = _get("/../../../etc/passwd")
        self.assertIn(status, (403, 404))

    def test_unknown_api_404(self):
        with _TempDB():
            self.assertEqual(_get("/api/nope", AUTH)[0], 404)

    def test_method_not_allowed(self):
        self.assertEqual(server.dispatch(CFG, "PUT", "/api/fleet", dict(LOOPBACK, **AUTH))[0], 405)

    def test_sessions_detail_404_when_missing(self):
        with _TempDB():
            self.assertEqual(_get("/api/sessions/ghost", AUTH)[0], 404)

    def test_goals_loops_notes_activity_routes(self):
        with _TempDB():
            for path in ("/api/goals", "/api/loops", "/api/notes", "/api/activity",
                         "/api/spend", "/api/projects", "/api/sessions", "/api/agents"):
                self.assertEqual(_get(path, AUTH)[0], 200, path)

    def test_agents_route_lists_clis(self):
        with _TempDB():
            status, _, body = _get("/api/agents", AUTH)
            self.assertEqual(status, 200)
            data = json.loads(body)
            kinds = {a["kind"] for a in data["agents"]}
            self.assertEqual(kinds, {"claude", "codex", "gemini"})
            self.assertIn("cwd", data)

    def test_post_note_route(self):
        with _TempDB():
            status, _, body = _post("/api/notes", {"text": "via server"}, AUTH)
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["ok"])

    def test_post_chat_route(self):
        with _TempDB():
            status, _, body = _post("/api/chat", {"message": "status?", "use_llm": False}, AUTH)
            self.assertEqual(status, 200)
            self.assertIn("status?", json.loads(body)["answer"])

    def test_post_spawn_degrades(self):
        with _TempDB():
            status, _, body = _post("/api/spawn", {"goal": "g", "command": "codex"}, AUTH)
            self.assertEqual(status, 200)
            self.assertFalse(json.loads(body)["ok"])

    def test_loop_and_goal_and_feed_routes(self):
        with _TempDB():
            # create loop with a feed rule
            status, _, body = _post("/api/loops", {
                "name": "news", "prompt": "scan", "every": "10m",
                "feed_policy": "always"}, AUTH)
            self.assertEqual(status, 200)
            created = json.loads(body)
            self.assertTrue(created["ok"], created)
            lid = created["loop"]["id"]
            # loop detail
            status, _, body = _get(f"/api/loops/{lid}", AUTH)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["name"], "news")
            # pause via action
            status, _, body = _post(f"/api/loops/{lid}/action", {"action": "pause"}, AUTH)
            self.assertEqual(json.loads(body)["status"], "paused")
            # feed rule update
            status, _, body = _post(f"/api/loops/{lid}/feed-rule",
                                    {"policy": "on_change"}, AUTH)
            self.assertEqual(json.loads(body)["feed_rule"]["policy"], "on_change")
            # goal create + action
            status, _, body = _post("/api/goals", {"objective": "test obj"}, AUTH)
            gid = json.loads(body)["goal_id"]
            status, _, body = _post(f"/api/goals/{gid}/action", {"action": "pause"}, AUTH)
            self.assertEqual(json.loads(body)["status"], "paused")
            # feed post + read + rules + text
            _post("/api/feed", {"title": "hello feed"}, AUTH)
            status, _, body = _get("/api/feed", AUTH)
            self.assertEqual(json.loads(body)["items"][0]["title"], "hello feed")
            status, hdrs, body = _get("/api/feed/text", AUTH)
            self.assertIn("text/plain", hdrs["Content-Type"])
            self.assertIn("hello feed", body.decode())
            status, _, body = _get("/api/feed/rules", AUTH)
            self.assertEqual(len(json.loads(body)["rules"]), 1)

    def test_loop_routes_validate_ids(self):
        with _TempDB():
            self.assertEqual(_get("/api/loops/abc", AUTH)[0], 400)
            self.assertEqual(_get("/api/loops/999", AUTH)[0], 404)
            self.assertEqual(_post("/api/loops/xyz/action", {"action": "pause"}, AUTH)[0], 400)

    def test_loop_run_output_route(self):
        from morpheus.desktop import bridge
        with _TempDB():
            lid = bridge.loop_create("echoer", "served-output", command="echo")["loop"]["id"]
            bridge.loop_action(lid, "run_now", wait=True)
            rid = bridge.loop_detail(lid)["runs"][0]["id"]
            status, _, body = _get(f"/api/loops/{lid}/runs/{rid}/output", AUTH)
            self.assertEqual(status, 200)
            self.assertIn("served-output", json.loads(body)["output"])
            self.assertEqual(_get(f"/api/loops/{lid}/runs/9999/output", AUTH)[0], 404)
            self.assertEqual(_get(f"/api/loops/{lid}/runs/abc/output", AUTH)[0], 400)

    def test_stream_requires_auth(self):
        # dispatch returns 200 placeholder for an authorized stream request
        self.assertEqual(_get("/api/stream")[0], 401)
        self.assertEqual(_get("/api/stream?token=secret-token")[0], 200)


class ParentWatchdogTest(unittest.TestCase):
    def test_fires_when_parent_changes(self):
        import threading
        fired = threading.Event()
        ppids = iter([42, 42, 1])  # parent died → reparented to init
        t = server.start_parent_watchdog(
            poll_secs=0.01, getppid=lambda: next(ppids, 1),
            on_orphaned=fired.set)
        self.assertTrue(fired.wait(timeout=2))
        t.join(timeout=2)

    def test_quiet_while_parent_alive(self):
        import threading
        fired = threading.Event()
        server.start_parent_watchdog(
            poll_secs=0.01, getppid=lambda: 42, initial_ppid=42,
            on_orphaned=fired.set)
        self.assertFalse(fired.wait(timeout=0.2))


class FleetSignatureTest(unittest.TestCase):
    def test_signature_changes_with_state(self):
        a = {"counts": {"working": 1}, "sessions": [{"tab_id": "t", "state": "working", "buffer_changed_at": 1}], "notes": []}
        b = {"counts": {"blocked": 1}, "sessions": [{"tab_id": "t", "state": "blocked", "buffer_changed_at": 2}], "notes": []}
        self.assertNotEqual(server.fleet_signature(a), server.fleet_signature(b))
        self.assertEqual(server.fleet_signature(a), server.fleet_signature(dict(a)))


class LiveServerTest(unittest.TestCase):
    def test_end_to_end_loopback(self):
        with _TempDB():
            srv = server.DesktopServer(server.Config(host="127.0.0.1", port=0, token="tk")).start()
            try:
                base = f"http://127.0.0.1:{srv.cfg.port}"
                # health, no auth
                with urllib.request.urlopen(base + "/healthz", timeout=5) as r:
                    self.assertEqual(r.status, 200)
                # fleet needs token
                req = urllib.request.Request(base + "/api/fleet", headers={"Authorization": "Bearer tk"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    payload = json.loads(r.read())
                    self.assertEqual(payload["health"]["total"], 1)
                # unauthorized
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(base + "/api/fleet", timeout=5)
                self.assertEqual(ctx.exception.code, 401)
                # handshake shape
                hs = srv.handshake()
                self.assertEqual(hs["port"], srv.cfg.port)
                self.assertEqual(hs["token"], "tk")
                self.assertIn("url", hs)
            finally:
                srv.stop()

    def test_agent_turn_streams_sse_events(self):
        def fake_run_turn(**kwargs):
            yield {"type": "session", "session_id": "sess-1", "model": "m", "tools": [], "cwd": "/"}
            yield {"type": "text", "text": "hello"}
            yield {"type": "result", "text": "hello", "cost_usd": 0.01, "web_searches": 0,
                   "session_id": "sess-1", "is_error": False}

        with _TempDB(), patch.object(agents, "run_turn", fake_run_turn):
            srv = server.DesktopServer(server.Config(host="127.0.0.1", port=0, token="tk")).start()
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{srv.cfg.port}/api/agent/turn",
                    data=json.dumps({"agent": "claude", "message": "hi"}).encode(),
                    headers={"Authorization": "Bearer tk", "Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=5) as r:
                    raw = r.read().decode()
                self.assertIn("event: session", raw)
                self.assertIn("event: text", raw)
                self.assertIn("event: result", raw)
                self.assertIn("sess-1", raw)
            finally:
                srv.stop()

    def test_agent_turn_requires_auth(self):
        with _TempDB():
            srv = server.DesktopServer(server.Config(host="127.0.0.1", port=0, token="tk")).start()
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{srv.cfg.port}/api/agent/turn",
                    data=b"{}", headers={"Content-Type": "application/json"})
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req, timeout=5)
                self.assertEqual(ctx.exception.code, 401)
            finally:
                srv.stop()

    def test_agent_turn_concurrency_cap(self):
        import threading
        busy = threading.BoundedSemaphore(1)
        busy.acquire()  # fully consumed → next turn must be rejected
        with _TempDB(), patch.object(server, "_agent_semaphore", busy):
            srv = server.DesktopServer(server.Config(host="127.0.0.1", port=0, token="tk")).start()
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{srv.cfg.port}/api/agent/turn",
                    data=json.dumps({"agent": "claude", "message": "hi"}).encode(),
                    headers={"Authorization": "Bearer tk", "Content-Type": "application/json"})
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req, timeout=5)
                self.assertEqual(ctx.exception.code, 503)
            finally:
                srv.stop()

    def test_sse_stream_emits_fleet_event(self):
        with _TempDB():
            srv = server.DesktopServer(server.Config(host="127.0.0.1", port=0, token="tk")).start()
            try:
                url = f"http://127.0.0.1:{srv.cfg.port}/api/stream?token=tk"
                with urllib.request.urlopen(url, timeout=5) as r:
                    # read the first SSE frame
                    deadline = time.time() + 5
                    buf = b""
                    while b"event: fleet" not in buf and time.time() < deadline:
                        chunk = r.read(64)
                        if not chunk:
                            break
                        buf += chunk
                    self.assertIn(b"event: fleet", buf)
            finally:
                srv.stop()


if __name__ == "__main__":
    unittest.main()
