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
from morpheus.desktop import server


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
                         "/api/spend", "/api/projects", "/api/sessions"):
                self.assertEqual(_get(path, AUTH)[0], 200, path)

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

    def test_stream_requires_auth(self):
        # dispatch returns 200 placeholder for an authorized stream request
        self.assertEqual(_get("/api/stream")[0], 401)
        self.assertEqual(_get("/api/stream?token=secret-token")[0], 200)


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
