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
