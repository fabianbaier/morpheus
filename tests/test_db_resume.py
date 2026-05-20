import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from morpheus import db


class ResumeMetadataTest(unittest.TestCase):
    def test_delete_archives_resume_metadata_for_codex_mission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, "DB_DIR", root), patch.object(db, "DB_PATH", root / "morpheus.db"):
                mission = db.Mission(
                    tab_id="tab-codex",
                    session_id="session-codex",
                    goal="resume closed codex",
                    cmd="codex",
                    linked_worktree="/tmp/work",
                    state="working",
                )
                db.upsert(mission)

                live_memory = db.get_memory(mission.mission_id)
                self.assertEqual(live_memory.agent_kind, "codex")
                self.assertIn("codex resume --last", live_memory.resume_command)
                self.assertIn("cd /tmp/work", live_memory.resume_command)
                self.assertEqual(live_memory.resume_confidence, "fallback")

                db.delete(mission.tab_id)
                archived = db.get_memory(mission.mission_id)

        self.assertIsNotNone(archived.archived_at)
        self.assertGreater(archived.closed_at, 0)
        self.assertEqual(archived.last_tab_id, "tab-codex")
        self.assertEqual(archived.agent_kind, "codex")
        self.assertIn("codex resume --last", archived.resume_command)

    def test_buffer_resume_id_replaces_codex_fallback_and_survives_archive(self) -> None:
        resume_id = "019e466d-0fd8-7441-aa1f-32a5db211a73"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, "DB_DIR", root), patch.object(db, "DB_PATH", root / "morpheus.db"):
                mission = db.Mission(
                    tab_id="tab-codex",
                    session_id="session-codex",
                    goal="resume closed codex",
                    cmd="codex --yolo",
                    linked_worktree="/tmp/work",
                    state="working",
                )
                db.upsert(mission)

                changed = db.refresh_resume_metadata_from_buffer(
                    mission,
                    "To continue this session, run codex resume "
                    f"{resume_id}",
                )
                self.assertTrue(changed)
                live_memory = db.get_memory(mission.mission_id)
                self.assertEqual(live_memory.resume_ref, resume_id)
                self.assertEqual(live_memory.resume_confidence, "exact")
                self.assertIn(f"codex --yolo resume {resume_id}", live_memory.resume_command)

                db.delete(mission.tab_id)
                archived = db.get_memory(mission.mission_id)

        self.assertEqual(archived.resume_ref, resume_id)
        self.assertEqual(archived.resume_confidence, "exact")
        self.assertIn(f"codex --yolo resume {resume_id}", archived.resume_command)
        self.assertNotIn("--last", archived.resume_command)

    def test_dismiss_closed_resume_hides_archived_resumable_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(db, "DB_DIR", root), patch.object(db, "DB_PATH", root / "morpheus.db"):
                mission = db.Mission(
                    tab_id="tab-codex",
                    session_id="session-codex",
                    goal="resume closed codex",
                    cmd="codex",
                    state="working",
                )
                db.upsert(mission)
                db.delete(mission.tab_id)

                dismissed = db.dismiss_closed_resume(mission.mission_id)
                memory = db.get_memory(mission.mission_id)
                events = db.recent_events(mission.mission_id, limit=5)

        self.assertTrue(dismissed)
        self.assertEqual(memory.resume_command, "")
        self.assertEqual(memory.resume_confidence, "dismissed")
        self.assertTrue(any(event.summary == "closed resume dismissed" for event in events))
