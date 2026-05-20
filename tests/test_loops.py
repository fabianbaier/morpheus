import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from morpheus import db, loops


class LoopsTest(unittest.TestCase):
    def test_parse_interval_supports_human_units(self) -> None:
        self.assertEqual(loops.parse_interval("15m"), 15 * 60)
        self.assertEqual(loops.parse_interval("2h"), 2 * 3600)
        self.assertEqual(loops.parse_interval("daily"), 86400)

    def test_parse_interval_rejects_runaway_seconds(self) -> None:
        with self.assertRaises(ValueError):
            loops.parse_interval("10s")

    def test_build_command_quotes_prompt_by_default(self) -> None:
        command = loops.build_command("codex exec", "what's new & why?")

        self.assertEqual(command, "codex exec 'what'\"'\"'s new & why?'")

    def test_run_loop_publishes_note_event_and_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch.object(db, "DB_DIR", tmp_path), patch.object(
                db, "DB_PATH", tmp_path / "morpheus.db"
            ), patch.object(
                loops.ctx_mod, "write_context_file", new=lambda: None
            ), patch.object(
                loops.ctx_mod, "write_context_json", new=lambda: None
            ):
                mission = db.Mission(
                    tab_id="tab-target",
                    mission_id="m_20260520000102_abcd1234",
                    goal="consume market scan",
                    state="working",
                )
                db.upsert(mission)
                loop = db.create_loop(
                    name="market scan",
                    prompt="ignored prompt",
                    interval_seconds=300,
                    command="printf 'Summary: WMT under 132 is the disciplined zone.'",
                    target_mission_id=mission.mission_id,
                    target_tab_id=mission.tab_id,
                    next_run_at=0,
                )

                run = loops.run_loop(loop, timeout=5)

                self.assertEqual(run.status, "success")
                self.assertIn("WMT under 132", run.summary)
                self.assertTrue(Path(run.output_path).exists())

                notes = db.recent_notes(limit=5)
                self.assertEqual(notes[0].kind, "loop")
                self.assertIn("loop [market scan]", notes[0].text)
                self.assertEqual(notes[0].tab_id, mission.tab_id)

                events = db.recent_events(mission.mission_id, limit=5)
                self.assertTrue(any(event.kind == "loop_output" for event in events))

                artifacts = db.artifacts_for_mission(mission.mission_id, limit=5)
                self.assertTrue(any(artifact.kind == "loop-output" for artifact in artifacts))

                refreshed = db.get_loop(loop.id)
                self.assertIsNotNone(refreshed)
                self.assertGreater(refreshed.next_run_at, run.finished_at)


if __name__ == "__main__":
    unittest.main()
