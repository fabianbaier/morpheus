"""Tests for the desktop bridge domain layer.

All reads/writes go through a temporary SQLite DB via the standard
`patch.object(db, "DB_PATH"/"DB_DIR", ...)` convention, so nothing touches the
real ~/.morpheus database. The iTerm2 control ops are exercised on their
degrade-gracefully (non-mac) path.
"""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from morpheus import db, ledger
from morpheus.desktop import bridge


class _TempDB:
    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._p = [
            patch.object(db, "DB_DIR", root),
            patch.object(db, "DB_PATH", root / "morpheus.db"),
            # ensure no stray iTerm cookie makes control ops attempt a connection
            patch.dict("os.environ", {}, clear=False),
        ]
        for p in self._p:
            p.start()
        import os
        os.environ.pop("ITERM2_COOKIE", None)
        return root

    def __exit__(self, *a):
        for p in reversed(self._p):
            p.stop()
        self._tmp.cleanup()


def _seed():
    now = time.time()
    blocked = db.Mission(tab_id="tab-block", session_id="s1", goal="fix auth",
                         state="blocked", cmd="codex", buffer_changed_at=now, last_event_at=now)
    working = db.Mission(tab_id="tab-work", session_id="s2", goal="build UI",
                         state="working", cmd="claude", buffer_changed_at=now, last_event_at=now)
    db.upsert(blocked)
    db.upsert(working)
    db.add_note("watch out for src/auth", kind="broadcast")
    db.add_event(blocked.mission_id, "decision", "chose JWT", actor="codex")
    db.add_artifact(blocked.mission_id, "proof", "tests/test_auth.py", status="pass", summary="green")
    ledger.log_cost(kind="ask", description="q", tokens=100, dollars=0.03)
    return blocked, working


class BridgeReadTest(unittest.TestCase):
    def test_fleet_shape_and_ordering(self):
        with _TempDB():
            _seed()
            f = bridge.fleet()
            self.assertIn("sessions", f)
            self.assertIn("health", f)
            self.assertEqual(f["health"]["total"], 2)
            self.assertEqual(f["health"]["blocked"], 1)
            # blocked session sorts first (attention)
            self.assertEqual(f["sessions"][0]["state"], "blocked")
            self.assertEqual(f["sessions"][0]["emoji"], "🔴")
            self.assertGreaterEqual(f["spend"]["today_usd"], 0.03)
            self.assertFalse(f["iterm_available"])

    def test_mission_detail_by_mission_id_and_tab_id(self):
        with _TempDB():
            blocked, _ = _seed()
            by_mid = bridge.mission_detail(blocked.mission_id)
            self.assertIsNotNone(by_mid)
            self.assertEqual(by_mid["mission_id"], blocked.mission_id)
            self.assertTrue(any(e["summary"] == "chose JWT" for e in by_mid["events"]))
            self.assertTrue(any(a["path_or_url"] == "tests/test_auth.py" for a in by_mid["artifacts"]))
            # resolvable by tab_id too
            by_tab = bridge.mission_detail("tab-block")
            self.assertEqual(by_tab["mission_id"], blocked.mission_id)

    def test_mission_detail_missing_returns_none(self):
        with _TempDB():
            self.assertIsNone(bridge.mission_detail("nonexistent"))

    def test_notes_and_activity_feed(self):
        with _TempDB():
            _seed()
            notes = bridge.notes()
            self.assertTrue(any(n["kind"] == "broadcast" for n in notes))
            feed = bridge.activity_feed()
            self.assertTrue(any("src/auth" in it["text"] for it in feed))

    def test_spend(self):
        with _TempDB():
            _seed()
            s = bridge.spend()
            self.assertGreaterEqual(s["today_usd"], 0.03)
            self.assertTrue(len(s["recent"]) >= 1)


class BridgeWriteTest(unittest.TestCase):
    def test_post_note_writes(self):
        with _TempDB():
            r = bridge.post_note("hello fleet", kind="note")
            self.assertTrue(r["ok"])
            self.assertTrue(any(n["text"] == "hello fleet" for n in bridge.notes()))

    def test_post_note_rejects_empty(self):
        with _TempDB():
            self.assertFalse(bridge.post_note("   ")["ok"])

    def test_chat_without_llm_is_deterministic(self):
        with _TempDB():
            _seed()
            r = bridge.chat("what is blocked?", use_llm=False, include_gh=False)
            self.assertTrue(r["ok"])
            self.assertIn("what is blocked?", r["answer"])
            self.assertIn("No LLM", r["answer"])


class BridgeControlDegradeTest(unittest.TestCase):
    def test_iterm_unavailable(self):
        with _TempDB():
            self.assertFalse(bridge.iterm_available())

    def test_spawn_degrades_with_hint(self):
        with _TempDB():
            r = bridge.spawn_session("review PR", "codex")
            self.assertFalse(r["ok"])
            self.assertIn("morpheus spawn", r["hint"])

    def test_send_degrades(self):
        with _TempDB():
            self.assertFalse(bridge.send_to_session("tab-x", "hi")["ok"])

    def test_broadcast_records_note_even_without_iterm(self):
        with _TempDB():
            r = bridge.broadcast("freeze main")
            self.assertTrue(r["ok"])  # note recorded
            self.assertFalse(r["delivery"]["available"])
            self.assertTrue(any(n["text"] == "freeze main" and n["kind"] == "broadcast"
                                for n in bridge.notes()))

    def test_spawn_requires_command(self):
        with _TempDB():
            self.assertFalse(bridge.spawn_session("goal only", "")["ok"])


if __name__ == "__main__":
    unittest.main()
