import asyncio
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from textual.widgets import Input

from morpheus import dashboard
from morpheus.dashboard import (
    LiveBuffer,
    LoopRequest,
    LoopScreen,
    MissionCardWidget,
    MorpheusApp,
    NewSessionRequest,
    NewSessionScreen,
    NoteScreen,
    _session_headline,
    _stream_shard_text,
    _tail_lines,
)


class DashboardHarness(MorpheusApp):
    async def _claim_self_tab(self) -> None:
        pass

    async def on_mount(self) -> None:
        self.iterm_conn = object()


class FakeLogger:
    def exception(self, *args, **kwargs) -> None:
        pass


class FakeRichLog:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.clear_count = 0

    def clear(self) -> None:
        self.clear_count += 1
        self.lines = []

    def write(self, value) -> None:
        self.lines.append(value.plain if hasattr(value, "plain") else str(value))


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
    def test_mission_card_render_includes_graph_memory(self) -> None:
        card = MissionCardWidget()
        mission = dashboard.db.Mission(
            tab_id="tab-123456",
            mission_id="m_20260520000102_abcd1234",
            goal="ship the graph card",
            state="working",
            cmd="codex",
        )
        memory = dashboard.db.MissionMemory(
            mission_id=mission.mission_id,
            title="Graph card",
            why="recover stale agent intent",
            done_definition="card shows durable context",
            acceptance_criteria="- phase visible\n- next step visible",
            current_plan="render selected mission",
            next_step="wire edit flow",
            blocked_on="",
            phase="planning",
            confidence=0.75,
            source_kind="user",
            source_ref="PRD.md",
        )
        event = dashboard.db.MissionEvent(
            id=1,
            mission_id=mission.mission_id,
            ts=0,
            kind="decision",
            actor="user",
            summary="build card before edit flow",
        )
        artifact = dashboard.db.MissionArtifact(
            id=1,
            mission_id=mission.mission_id,
            kind="test",
            path_or_url="tests/test_dashboard.py",
            status="pass",
            summary="dashboard test",
            created_at=0,
        )
        live = LiveBuffer(
            tab_id=mission.tab_id,
            goal=mission.goal,
            state=mission.state,
            last_event="active output",
            buffer="alpha\nSearching the web\nfinal response line",
            observed_at=0,
        )

        rendered = card._render_card(mission, memory, [event], [artifact], live).plain

        self.assertIn("Graph card", rendered)
        self.assertIn("phase: planning", rendered)
        self.assertIn("why: recover stale agent intent", rendered)
        self.assertIn("next: wire edit flow", rendered)
        self.assertIn("LATEST OUTPUT", rendered)
        self.assertIn("Searching the web", rendered)
        self.assertIn("final response line", rendered)
        self.assertIn("decision build card before edit flow", rendered)
        self.assertIn("pass test tests/test_dashboard.py", rendered)

    def test_tail_lines_prefers_recent_non_empty_output(self) -> None:
        rendered = _tail_lines("old\n\nmiddle\nlatest and very long", limit=2, width=12)

        self.assertEqual(rendered, ["middle", "latest and …"])

    def test_alert_panel_renders_newest_first(self) -> None:
        app = DashboardHarness()
        rich_log = FakeRichLog()

        with patch.object(app, "query_one", return_value=rich_log):
            app._push_alert(dashboard.Alert(1, "summary", "older ready headline"))
            app._push_alert(dashboard.Alert(2, "summary", "newer ready headline"))

        self.assertEqual(len(app.alerts), 2)
        self.assertIn("newer ready headline", rich_log.lines[0])
        self.assertIn("older ready headline", rich_log.lines[1])
        self.assertGreaterEqual(rich_log.clear_count, 2)

    def test_session_headline_prefers_final_substantive_line(self) -> None:
        rendered = _session_headline(
            "\nSearching the web\nAssuming you mean X/Twitter: two debate clusters.\n› Use /skills to list available skills",
            fallback="process completed",
        )

        self.assertEqual(rendered, "Assuming you mean X/Twitter: two debate clusters.")

    def test_session_headline_ignores_codex_chrome_and_summarizes_answer(self) -> None:
        rendered = _session_headline(
            "\n".join(
                [
                    "› and what do you think is a great new stockprice for tomorrow",
                    "• Searching the web",
                    "• Searched finance: WMT",
                    "────────────────────────────────────────────────────────────",
                    "For tomorrow, I’d focus on WMT.",
                    "",
                    "Current price: $134.20",
                    "My “good entry” zone: $130–$132",
                    "I would avoid chasing it above: $136+",
                    "",
                    "So my practical answer: WMT under $132 looks like the better disciplined buy zone for tomorrow. Not financial advice.",
                    "────────────────────────────────────────────────────────────",
                    "› Use /skills to list available skills",
                    "gpt-5.5 xhigh · ~",
                ]
            ),
            fallback="idle",
        )

        self.assertEqual(
            rendered,
            "So my practical answer: WMT under $132 looks like the better disciplined buy zone for tomorrow.",
        )

    def test_session_headline_skips_sources_after_answer(self) -> None:
        rendered = _session_headline(
            "\n".join(
                [
                    "The fix is to store run state in the mission graph.",
                    "Sources: https://example.com/docs",
                    "› Use /skills to list available skills",
                ]
            ),
            fallback="idle",
        )

        self.assertEqual(rendered, "The fix is to store run state in the mission graph.")

    def test_stream_shard_text_embeds_live_output_in_mission_label(self) -> None:
        live = LiveBuffer(
            tab_id="tab-123456",
            goal="current debate on X",
            state="working",
            last_event="active output",
            buffer="Searching the web\nTwo debate clusters are driving the timeline.",
            observed_at=0,
        )

        rendered = _stream_shard_text(live, width=80)

        self.assertIn("current debate on X", rendered)
        self.assertIn("Two debate clusters", rendered)
        self.assertNotIn("Searching the web", rendered)

    async def test_finished_session_pushes_summary_ticker_and_event(self) -> None:
        app = DashboardHarness()
        captured = {}
        mission = dashboard.db.Mission(
            tab_id="tab-123456",
            mission_id="m_20260520000102_abcd1234",
            goal="current debate on X",
            state="finished",
            last_event="process completed",
        )
        app.live_buffers[mission.tab_id] = LiveBuffer(
            tab_id=mission.tab_id,
            goal=mission.goal,
            state=mission.state,
            last_event=mission.last_event,
            buffer="Searching the web\nTwo debate clusters are driving the timeline.",
            observed_at=0,
        )

        def fake_add_event(mission_id, kind, summary, actor="user", source_ref="", metadata=None):
            captured["event"] = {
                "mission_id": mission_id,
                "kind": kind,
                "summary": summary,
                "actor": actor,
                "source_ref": source_ref,
                "metadata": metadata,
            }
            return 1

        with patch.object(dashboard.db, "add_event", new=fake_add_event):
            await app._on_state_change(mission, "working", "finished")

        alert = app.alerts[0]
        self.assertEqual(alert.kind, "summary")
        self.assertEqual(
            alert.text,
            "completed [current debate on X] — Two debate clusters are driving the timeline.",
        )
        self.assertEqual(captured["event"]["kind"], "summary")
        self.assertEqual(captured["event"]["actor"], "morpheus")
        self.assertEqual(captured["event"]["summary"], alert.text)

    async def test_idle_after_working_pushes_ready_ticker_once(self) -> None:
        app = DashboardHarness()
        events = []
        mission = dashboard.db.Mission(
            tab_id="tab-123456",
            mission_id="m_20260520000102_abcd1234",
            goal="latest news on X",
            state="idle",
            last_event="idle 38s",
            buffer_hash="hash-ready",
        )
        app.live_buffers[mission.tab_id] = LiveBuffer(
            tab_id=mission.tab_id,
            goal=mission.goal,
            state=mission.state,
            last_event=mission.last_event,
            buffer="\n".join(
                [
                    "Searching the web",
                    "The latest X debate is split across policy and sports.",
                    "────────────────────────────────────────────────────────────",
                    "› Use /skills to list available skills",
                ]
            ),
            observed_at=0,
        )

        def fake_add_event(mission_id, kind, summary, actor="user", source_ref="", metadata=None):
            events.append(
                {
                    "mission_id": mission_id,
                    "kind": kind,
                    "summary": summary,
                    "actor": actor,
                    "source_ref": source_ref,
                    "metadata": metadata,
                }
            )
            return 1

        with patch.object(dashboard.db, "add_event", new=fake_add_event):
            await app._on_state_change(mission, "working", "idle")
            await app._on_state_change(mission, "working", "idle")

        self.assertEqual(len(app.alerts), 1)
        alert = app.alerts[0]
        self.assertEqual(alert.kind, "summary")
        self.assertEqual(
            alert.text,
            "ready [latest news on X] — The latest X debate is split across policy and sports.",
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["metadata"]["summary_kind"], "ready")

    def test_scan_new_missions_silently_drops_self_tab(self) -> None:
        app = MorpheusApp()
        app.self_tab_id = "self-tab"
        app.last_seen_tabs = {"self-tab"}

        app._scan_new_missions([])

        self.assertEqual(len(app.alerts), 0)
        self.assertEqual(app.last_seen_tabs, set())

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

                await app.pop_screen()
                await pilot.pause()

                await app.push_screen(LoopScreen(target_label="ticker/context only"))
                await pilot.pause()
                self.assertIsInstance(app.screen, LoopScreen)

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

    async def test_new_session_with_prd_creates_run_and_coordinator(self) -> None:
        app = DashboardHarness()
        app.iterm_conn = object()
        done = asyncio.Event()
        captured = {}
        run = SimpleNamespace(
            parent_id="m_parent",
            title="PRD Runs",
            prd_path="/tmp/PRD.md",
            status_path="/tmp/status.md",
            prompt_path="/tmp/prompt.md",
        )

        async def fake_spawn_tab(connection, *, command, goal):
            captured["spawn"] = (connection, command, goal)
            return SimpleNamespace(tab_id="tab-123456", session_id="session-123456")

        def fake_upsert(mission):
            mission.mission_id = "m_child"
            captured["mission"] = mission

        def fake_attach(created_run, mission):
            captured["attach"] = (created_run, mission)
            done.set()

        with patch.object(
            dashboard.prd_runs, "create_prd_run", new=lambda path, title=None: run
        ), patch.object(
            dashboard.prd_runs, "coordinator_command", new=lambda cmd, run: f"{cmd} --coordinator"
        ), patch.object(
            dashboard.prd_runs, "attach_coordinator", new=fake_attach
        ), patch.object(
            dashboard.iterm_client, "spawn_tab", new=fake_spawn_tab
        ), patch.object(dashboard.db, "upsert", new=fake_upsert):
            await app._handle_new_session_result(
                NewSessionRequest(goal="", command="codex", prd_path="/tmp/PRD.md")
            )
            await asyncio.wait_for(done.wait(), timeout=1)

        connection, command, goal = captured["spawn"]
        self.assertIs(connection, app.iterm_conn)
        self.assertEqual(command, "codex --coordinator")
        self.assertEqual(goal, "PRD Runs coordinator")
        self.assertEqual(captured["mission"].goal, "PRD Runs coordinator")
        self.assertEqual(captured["mission"].cmd, "codex --coordinator")
        self.assertEqual(captured["attach"], (run, captured["mission"]))

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

    async def test_loop_key_opens_modal_and_records_loop(self) -> None:
        app = DashboardHarness()
        done = asyncio.Event()
        captured = {}

        def fake_create_loop(**kwargs):
            captured["loop"] = kwargs
            done.set()
            return SimpleNamespace(id=7, name=kwargs["name"], interval_seconds=kwargs["interval_seconds"])

        with isolated_dashboard_runtime(), patch.object(
            dashboard.db, "create_loop", new=fake_create_loop
        ), patch.object(
            dashboard.ledger_mod, "log_action", new=lambda *args, **kwargs: 1
        ), patch.object(
            dashboard.ctx_mod, "write_context_file", new=lambda: None
        ), patch.object(
            dashboard.ctx_mod, "write_context_json", new=lambda: None
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.press("l")
                await pilot.pause()

                screen = app.screen
                self.assertIsInstance(screen, LoopScreen)
                screen.dismiss(LoopRequest(
                    name="market scan",
                    prompt="summarize tomorrow's market catalysts",
                    interval="15m",
                    command="codex exec",
                ))

                await asyncio.wait_for(done.wait(), timeout=1)

        self.assertEqual(captured["loop"]["name"], "market scan")
        self.assertEqual(captured["loop"]["prompt"], "summarize tomorrow's market catalysts")
        self.assertEqual(captured["loop"]["interval_seconds"], 15 * 60)
        self.assertEqual(captured["loop"]["command"], "codex exec")
        self.assertEqual(captured["loop"]["target_mission_id"], "")
        self.assertIsNone(captured["loop"]["target_tab_id"])


if __name__ == "__main__":
    unittest.main()
