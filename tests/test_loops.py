import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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

        self.assertEqual(command, "codex exec --skip-git-repo-check 'what'\"'\"'s new & why?'")

    def test_build_command_preserves_existing_codex_skip_flag(self) -> None:
        command = loops.build_command("codex exec --skip-git-repo-check", "ok")

        self.assertEqual(command, "codex exec --skip-git-repo-check ok")

    def test_summarize_output_prefers_codex_assistant_headline_over_prompt(self) -> None:
        prompt = "Can you give me a brief headline summary on current worldwide news?"
        output = "\n".join(
            [
                "$ codex exec --skip-git-repo-check 'news'",
                "started: 2026-05-21 09:57:00",
                "Reading additional input from stdin...",
                "OpenAI Codex v0.132.0",
                "--------",
                "workdir: /tmp/project",
                "model: gpt-5.5",
                "provider: openai",
                "approval: never",
                "sandbox: danger-full-access",
                "reasoning effort: xhigh",
                "session id: 019e4824-ec9a-7ce0-bf54-0b29e9b42f86",
                "--------",
                "user",
                prompt,
                "assistant",
                "Headline: global markets are watching central-bank signals and AI earnings.",
                "Sources: https://example.com/news",
                "[loop success; exit=0]",
            ]
        )

        summary = loops.summarize_output(output, prompt=prompt)

        self.assertEqual(
            summary,
            "Headline: global markets are watching central-bank signals and AI earnings.",
        )
        self.assertNotIn("Can you give me", summary)

    def test_create_loop_defaults_first_run_due_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch.object(db, "DB_DIR", tmp_path), patch.object(
                db, "DB_PATH", tmp_path / "morpheus.db"
            ):
                loop = db.create_loop(
                    name="news",
                    prompt="summarize news",
                    interval_seconds=300,
                    command="printf ok",
                )

                self.assertLessEqual(loop.next_run_at, loop.created_at + 1)
                self.assertEqual(db.due_loops(limit=5), [loop])

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

    def test_run_loop_uses_loop_project_root_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            project = tmp_path / "project"
            project.mkdir()
            captured = {}

            def fake_run(*args, **kwargs):
                captured["cwd"] = kwargs.get("cwd")
                return SimpleNamespace(returncode=0, stdout="Summary: ok\n", stderr="")

            with patch.object(db, "DB_DIR", tmp_path), patch.object(
                db, "DB_PATH", tmp_path / "morpheus.db"
            ), patch.object(
                loops.subprocess, "run", new=fake_run
            ), patch.object(
                loops.ctx_mod, "write_context_file", new=lambda: None
            ), patch.object(
                loops.ctx_mod, "write_context_json", new=lambda: None
            ):
                loop = db.create_loop(
                    name="project loop",
                    prompt="ignored prompt",
                    interval_seconds=300,
                    command="printf ok",
                    project_root=str(project),
                )

                run = loops.run_loop(loop, timeout=5)

            self.assertEqual(run.status, "success")
            self.assertEqual(captured["cwd"], str(project))

    def test_run_loop_records_running_row_and_streams_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            observed = {}

            def fake_run(*args, **kwargs):
                running = db.loop_runs(observed["loop_id"], limit=1)[0]
                observed["running_status"] = running.status
                observed["output_exists_during_run"] = Path(running.output_path).exists()
                kwargs["stdout"].write("session id: 019e4824-ec9a-7ce0-bf54-0b29e9b42f86\n")
                kwargs["stdout"].write("Summary: streamed while running.\n")
                kwargs["stdout"].flush()
                return SimpleNamespace(returncode=0)

            with patch.object(db, "DB_DIR", tmp_path), patch.object(
                db, "DB_PATH", tmp_path / "morpheus.db"
            ), patch.object(
                loops.subprocess, "run", new=fake_run
            ), patch.object(
                loops.ctx_mod, "write_context_file", new=lambda: None
            ), patch.object(
                loops.ctx_mod, "write_context_json", new=lambda: None
            ):
                loop = db.create_loop(
                    name="streaming loop",
                    prompt="ignored prompt",
                    interval_seconds=300,
                    command="codex exec",
                )
                observed["loop_id"] = loop.id

                run = loops.run_loop(loop, timeout=5)

            text = Path(run.output_path).read_text(encoding="utf-8")
            self.assertEqual(observed["running_status"], "running")
            self.assertTrue(observed["output_exists_during_run"])
            self.assertEqual(run.mission_id, f"looprun_{loop.id}_{run.id}")
            self.assertEqual(run.resume_ref, "019e4824-ec9a-7ce0-bf54-0b29e9b42f86")
            self.assertEqual(run.resume_command, f"codex resume {run.resume_ref}")
            self.assertEqual(run.resume_confidence, "exact")
            self.assertEqual(run.status, "success")
            self.assertIn("Summary: streamed while running.", run.summary)
            self.assertIn("Summary: streamed while running.", text)
            self.assertIn("[loop success; exit=0]", text)

    def test_loop_lifecycle_helpers_update_target_history_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch.object(db, "DB_DIR", tmp_path), patch.object(
                db, "DB_PATH", tmp_path / "morpheus.db"
            ):
                loop = db.create_loop(
                    name="news",
                    prompt="summarize news",
                    interval_seconds=300,
                    command="printf ok",
                    next_run_at=0,
                )

                edited = db.update_loop_details(
                    loop.id,
                    name="market news",
                    interval_seconds=600,
                )
                self.assertIsNotNone(edited)
                self.assertEqual(edited.name, "market news")
                self.assertEqual(edited.interval_seconds, 600)
                self.assertGreater(edited.next_run_at, loop.next_run_at)

                joined = db.set_loop_target(
                    loop.id,
                    target_mission_id="m_target",
                    target_tab_id="tab-target",
                )
                self.assertIsNotNone(joined)
                self.assertEqual(joined.target_mission_id, "m_target")
                self.assertEqual(joined.target_tab_id, "tab-target")

                run = db.record_loop_run(
                    loop.id,
                    started_at=1,
                    finished_at=3,
                    status="success",
                    exit_code=0,
                    output_path="/tmp/out.txt",
                    summary="done",
                    target_mission_id="m_target",
                    target_tab_id="tab-target",
                )
                self.assertEqual(db.loop_runs(loop.id), [run])

                db.upsert_memory(db.MissionMemory(
                    mission_id=run.mission_id,
                    title="loop run memory",
                    topic="loop-run",
                    source_kind="loop-run",
                    archived_at=4,
                ))
                db.add_event(run.mission_id, "loop_run_joined", "joined")
                deleted_run = db.delete_loop_run(run.id)
                self.assertIsNotNone(deleted_run)
                self.assertEqual(deleted_run.id, run.id)
                self.assertEqual(db.loop_runs(loop.id), [])
                self.assertIsNone(db.get_memory(run.mission_id))

                deleted = db.delete_loop(loop.id)
                self.assertIsNotNone(deleted)
                self.assertEqual(deleted.name, "market news")
                self.assertIsNone(db.get_loop(loop.id))
                self.assertEqual(db.loop_runs(loop.id), [])


if __name__ == "__main__":
    unittest.main()
