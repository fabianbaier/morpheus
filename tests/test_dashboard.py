import asyncio
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from textual.widgets import Input

from morpheus import dashboard
from morpheus.dashboard import MorpheusApp, NewSessionScreen, NoteScreen


class DashboardHarness(MorpheusApp):
    async def _claim_self_tab(self) -> None:
        pass

    async def on_mount(self) -> None:
        self.iterm_conn = object()


class FakeLogger:
    def exception(self, *args, **kwargs) -> None:
        pass


@contextmanager
def isolated_dashboard_runtime():
    async def fake_async_create():
        return object()

    with (
        patch.object(dashboard.iterm2.Connection, "async_create", new=fake_async_create),
        patch.object(dashboard.core, "setup_logging", new=lambda: FakeLogger()),
        patch.object(dashboard.db, "recent_notes", new=lambda limit=1: []),
        patch.object(dashboard.db, "all_missions", new=lambda: []),
    ):
        yield


class DashboardTest(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_and_modal_css_mounts(self) -> None:
        app = DashboardHarness()

        with isolated_dashboard_runtime():
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()

                await app.push_screen(NewSessionScreen())
                await pilot.pause()
                self.assertIsInstance(app.screen, NewSessionScreen)

                await app.pop_screen()
                await pilot.pause()

                await app.push_screen(NoteScreen())
                await pilot.pause()
                self.assertIsInstance(app.screen, NoteScreen)

    async def test_new_session_key_opens_modal_without_worker_crash(self) -> None:
        app = DashboardHarness()

        with isolated_dashboard_runtime():
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.press("n")
                await pilot.pause()

                self.assertIsInstance(app.screen, NewSessionScreen)

    async def test_new_session_submit_spawns_tab_and_records_mission(self) -> None:
        app = DashboardHarness()
        done = asyncio.Event()
        captured = {}

        async def fake_spawn_tab(connection, *, command, goal):
            captured["spawn"] = (connection, command, goal)
            return SimpleNamespace(tab_id="tab-123456", session_id="session-123456")

        def fake_upsert(mission):
            captured["mission"] = mission
            done.set()

        with isolated_dashboard_runtime(), patch.object(
            dashboard.iterm_client, "spawn_tab", new=fake_spawn_tab
        ), patch.object(dashboard.db, "upsert", new=fake_upsert):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.press("n")
                await pilot.pause()

                screen = app.screen
                self.assertIsInstance(screen, NewSessionScreen)
                screen.query_one("#goal_input", Input).value = "review a PR"
                screen.query_one("#cmd_input", Input).value = "codex"
                screen.action_submit()

                await asyncio.wait_for(done.wait(), timeout=1)

        connection, command, goal = captured["spawn"]
        self.assertIs(connection, app.iterm_conn)
        self.assertEqual(command, "codex")
        self.assertEqual(goal, "review a PR")
        self.assertEqual(captured["mission"].tab_id, "tab-123456")
        self.assertEqual(captured["mission"].session_id, "session-123456")
        self.assertEqual(captured["mission"].goal, "review a PR")
        self.assertEqual(captured["mission"].cmd, "codex")

    async def test_post_note_key_opens_modal_and_records_note(self) -> None:
        app = DashboardHarness()
        done = asyncio.Event()
        captured = {}

        def fake_add_note(*, text, tab_id, session_id, kind):
            captured["note"] = {
                "text": text,
                "tab_id": tab_id,
                "session_id": session_id,
                "kind": kind,
            }
            done.set()

        with isolated_dashboard_runtime(), patch.object(
            dashboard.db, "add_note", new=fake_add_note
        ), patch.object(
            dashboard.ctx_mod, "write_context_file", new=lambda: None
        ), patch.object(
            dashboard.ctx_mod, "write_context_json", new=lambda: None
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.press("slash")
                await pilot.pause()

                screen = app.screen
                self.assertIsInstance(screen, NoteScreen)
                screen.dismiss(("note", "handoff detail", None))

                await asyncio.wait_for(done.wait(), timeout=1)

        self.assertEqual(
            captured["note"],
            {
                "text": "handoff detail",
                "tab_id": None,
                "session_id": None,
                "kind": "note",
            },
        )


if __name__ == "__main__":
    unittest.main()
