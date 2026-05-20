import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from morpheus import activity, context as ctx_mod, db


class ActivitySnapshotTest(unittest.TestCase):
    def test_headline_skips_codex_chrome_and_uses_recent_substantive_output(self) -> None:
        buffer = "\n".join(
            [
                "• Ran tests/test_activity.py",
                "Implemented cached activity snapshots for instant context.",
                "• Working (43s • esc to interrupt) · 1 background terminal running",
                "› Use /skills to list available skills",
                "gpt-5.5 xhigh · ~",
            ]
        )

        headline = activity.session_headline(buffer, fallback="active output")

        self.assertEqual(headline, "Implemented cached activity snapshots for instant context.")

    def test_snapshot_contains_headline_tail_and_activity_metadata(self) -> None:
        mission = db.Mission(
            tab_id="tab-8",
            mission_id="m_activity",
            session_id="session-8",
            goal="activity cache",
            state="working",
            last_event="active output",
            last_event_at=90,
            buffer_changed_at=95,
            buffer_hash="abc123",
            cmd="codex",
            linked_worktree="/repo",
        )
        tab = type(
            "Tab",
            (),
            {
                "tab_id": "tab-8",
                "session_id": "session-8",
                "current_name": "codex",
                "cwd": "/repo",
                "buffer": "Progress: 50%.\nAdding focused tests now.",
            },
        )()

        snapshot = activity.build_snapshot(
            [activity.ActivityObservation.from_tab(tab, mission)],
            generated_at=100,
        )

        self.assertEqual(snapshot["session_count"], 1)
        item = snapshot["sessions"][0]
        self.assertEqual(item["tab_id"], "tab-8")
        self.assertEqual(item["headline"], "Progress: 50%.")
        self.assertEqual(item["tail_lines"], ["Progress: 50%.", "Adding focused tests now."])
        self.assertEqual(item["age_seconds"], 5)

    def test_write_and_read_snapshot_is_atomic_json_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "activity.json"
            payload = activity.write_snapshot([], path=path, generated_at=123)

            self.assertEqual(payload["generated_at"], 123)
            self.assertEqual(activity.read_snapshot(path)["sessions"], [])

    def test_context_json_and_markdown_include_cached_activity(self) -> None:
        mission = db.Mission(
            tab_id="tab-1",
            mission_id="m_one",
            goal="coordination",
            state="working",
            last_event="active output",
            buffer_changed_at=10,
            updated_at=20,
        )
        cached = {
            "tab-1": {
                "tab_id": "tab-1",
                "headline": "Reviewing sibling session output.",
                "tail_lines": ["Reviewing sibling session output."],
            }
        }

        with patch.object(ctx_mod.db, "all_missions", new=lambda: [mission]), patch.object(
            ctx_mod.db, "recent_notes", new=lambda limit=15: []
        ), patch.object(
            ctx_mod.db, "all_memory", new=lambda include_archived=True: []
        ), patch.object(
            ctx_mod.activity_mod, "activities_by_tab", new=lambda: cached
        ):
            payload = ctx_mod.build_json()
            markdown = ctx_mod.build_markdown()

        self.assertEqual(payload["sessions"][0]["activity"]["headline"], "Reviewing sibling session output.")
        self.assertIn("Reviewing sibling session output.", markdown)


if __name__ == "__main__":
    unittest.main()
