import json
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from morpheus import db, ledger, remote


@contextmanager
def isolated_remote_runtime():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db_dir = root / "db"
        with patch.object(db, "DB_DIR", db_dir), patch.object(
            db, "DB_PATH", db_dir / "morpheus.db"
        ), patch.object(remote.ctx_mod, "write_context_file", new=lambda: None), patch.object(
            remote.ctx_mod, "write_context_json", new=lambda: None
        ):
            yield root


class RemoteSurfaceTest(unittest.TestCase):
    def test_snapshot_prioritizes_blocked_cards_without_raw_ids(self) -> None:
        with isolated_remote_runtime():
            now = time.time()
            blocked = db.Mission(
                tab_id="abc123-blocked-tab",
                mission_id="m_blocked",
                goal="finish checkout flow",
                state="blocked",
                last_event="waiting for approval",
                last_event_at=now - 10,
                buffer_changed_at=now - 20,
            )
            working = db.Mission(
                tab_id="def456-working-tab",
                mission_id="m_working",
                goal="write docs",
                state="working",
                last_event="editing",
                buffer_changed_at=now - 5,
            )
            db.upsert(blocked)
            db.upsert(working)
            db.upsert_memory(
                db.MissionMemory(
                    mission_id="m_blocked",
                    title="Checkout flow",
                    phase="reviewing",
                    blocked_on="Need approval before running payment tests.",
                    next_step="Run the payment tests after approval.",
                )
            )

            snapshot = remote.fleet_snapshot(limit=4)

        self.assertEqual(snapshot["counts"]["blocked"], 1)
        self.assertEqual(snapshot["cards"][0]["priority"], "urgent")
        self.assertEqual(snapshot["cards"][0]["kind"], "session_blocked")
        self.assertIn("Need approval", snapshot["cards"][0]["body"])
        encoded = json.dumps(snapshot)
        self.assertNotIn("abc123-blocked-tab", encoded)
        self.assertNotIn("session_id", encoded)
        self.assertFalse(snapshot["policy"]["raw_terminal_buffers"])

    def test_session_brief_resolves_short_ref_and_stays_compact(self) -> None:
        with isolated_remote_runtime():
            db.upsert(
                db.Mission(
                    tab_id="abc123-session",
                    mission_id="m_alpha",
                    goal="ship remote bridge",
                    state="idle",
                    last_event="ready for review",
                )
            )
            db.upsert_memory(
                db.MissionMemory(
                    mission_id="m_alpha",
                    title="Remote bridge",
                    why="voice needs small state packets",
                    next_step="Review manifest and widget.",
                    phase="testing",
                )
            )
            db.add_event("m_alpha", kind="check", actor="codex", summary="remote tests pass")

            brief = remote.session_brief("abc123")

        self.assertTrue(brief["found"])
        self.assertEqual(brief["tab_ref"], "abc123")
        self.assertEqual(brief["memory"]["phase"], "testing")
        self.assertEqual(brief["recent_events"][0]["summary"], "remote tests pass")
        self.assertFalse(brief["policy"]["raw_terminal_buffers"])

    def test_stage_operator_note_is_bounded_and_logged(self) -> None:
        with isolated_remote_runtime():
            db.upsert(
                db.Mission(
                    tab_id="abc123-session",
                    mission_id="m_alpha",
                    goal="ship remote bridge",
                    state="blocked",
                )
            )
            result = remote.stage_operator_note("x" * 400, target_ref="abc123", kind="broadcast")
            notes = db.recent_notes(limit=5)
            actions = ledger.recent_actions(limit=5)

        self.assertTrue(result["ok"])
        self.assertLessEqual(len(result["text"]), 240)
        self.assertEqual(notes[0].kind, "broadcast")
        self.assertLessEqual(len(notes[0].text), 240)
        self.assertEqual(actions[0].action, "remote_operator_note")
        self.assertEqual(actions[0].details["kind"], "broadcast")

    def test_tool_descriptors_have_required_annotations(self) -> None:
        tools = remote.tool_descriptors()
        by_name = {tool["name"]: tool for tool in tools}

        for tool in tools:
            annotations = tool.get("annotations", {})
            self.assertIsInstance(annotations.get("readOnlyHint"), bool)
            self.assertIsInstance(annotations.get("openWorldHint"), bool)
            self.assertIsInstance(annotations.get("destructiveHint"), bool)

        self.assertTrue(by_name["get_fleet_snapshot"]["annotations"]["readOnlyHint"])
        self.assertFalse(by_name["stage_operator_note"]["annotations"]["readOnlyHint"])
        self.assertFalse(by_name["stage_operator_note"]["annotations"]["destructiveHint"])
        self.assertEqual(
            by_name["render_morpheus_live_card"]["_meta"]["ui"]["resourceUri"],
            remote.WIDGET_URI,
        )
        self.assertNotIn("spawn", by_name)
        self.assertNotIn("kill", by_name)
        self.assertNotIn("push", by_name)

    def test_html_preview_embeds_json_without_html_entities(self) -> None:
        html = remote.html_preview({"summary": "<clear>", "counts": {}, "cards": []})

        self.assertIn("window.openai.toolOutput =", html)
        self.assertIn("\\u003cclear>", html)
        assignment = html.split("window.openai.toolOutput =", 1)[1].split(";", 1)[0]
        self.assertNotIn("&quot;", assignment)

    def test_codex_resume_ref_from_command_parses_quoted_resume_ids(self) -> None:
        thread = "019ebd79-adb0-7982-ad1a-ddd9d877a89e"
        mirror_cmd = (
            "cd '/Users/fab/github/morpheus' && codex --remote 'ws://127.0.0.1:8765' "
            f"-C '/Users/fab/github/morpheus' resume '{thread}'"
        )

        self.assertEqual(db.codex_resume_ref_from_command(mirror_cmd), thread)
        self.assertEqual(db.codex_resume_ref_from_command("codex resume --last"), "")
        self.assertEqual(db.codex_resume_ref_from_command("codex"), "")
        self.assertEqual(db.codex_resume_ref_from_command(""), "")

    def test_snapshot_sessions_expose_codex_resume_ref_for_mirror_tabs(self) -> None:
        thread = "019ebd79-adb0-7982-ad1a-ddd9d877a89e"
        with isolated_remote_runtime():
            now = time.time()
            db.upsert(
                db.Mission(
                    tab_id="mirror1-tab",
                    mission_id="m_mirror",
                    goal="G2: hello",
                    state="working",
                    cmd=(
                        "cd '/tmp/proj' && codex --remote 'ws://127.0.0.1:8765' "
                        f"-C '/tmp/proj' resume '{thread}'"
                    ),
                    buffer_changed_at=now,
                )
            )
            db.upsert(
                db.Mission(
                    tab_id="plain1-tab",
                    mission_id="m_plain",
                    goal="write docs",
                    state="working",
                    cmd="codex",
                    buffer_changed_at=now,
                )
            )

            snapshot = remote.fleet_snapshot(limit=4)

        by_ref = {row["tab_ref"]: row for row in snapshot["sessions"]}
        self.assertEqual(by_ref["mirror1"]["resume_ref"], thread)
        self.assertEqual(by_ref["plain1"]["resume_ref"], "")


if __name__ == "__main__":
    unittest.main()
