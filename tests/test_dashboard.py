import asyncio
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from textual.widgets import Input

from morpheus import dashboard, mission_brief
from morpheus.dashboard import (
    BriefScreenContent,
    EditMissionScreen,
    LiveBuffer,
    LoopActionRequest,
    LoopManagerScreen,
    LoopRequest,
    LoopScreen,
    MissionCardWidget,
    MorpheusApp,
    NewSessionRequest,
    NewSessionScreen,
    NoteScreen,
    SelectedBriefScreen,
    WorkerRequest,
    WorkerScreen,
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
def isolated_dashboard_runtime(missions=None, memories=None, edges=None):
    async def fake_async_create():
        return object()

    mission_rows = list(missions or [])
    memory_rows = list(memories or [])
    edge_rows = list(edges or [])

    with (
        patch.object(dashboard.iterm2.Connection, "async_create", new=fake_async_create),
        patch.object(dashboard.core, "setup_logging", new=lambda: FakeLogger()),
        patch.object(dashboard.db, "recent_notes", new=lambda limit=1: []),
        patch.object(dashboard.db, "all_missions", new=lambda: list(mission_rows)),
        patch.object(dashboard.db, "all_memory", new=lambda include_archived=False: list(memory_rows)),
        patch.object(
            dashboard.db,
            "edges_from_id",
            new=lambda node_id, relation="", limit=50: [
                edge for edge in edge_rows
                if edge.from_id == node_id and (not relation or edge.relation == relation)
            ][:limit],
        ),
    ):
        yield


class DashboardTest(unittest.IsolatedAsyncioTestCase):
    def test_selected_brief_builder_cites_graph_events_artifacts_and_tail(self) -> None:
        mission = dashboard.db.Mission(
            tab_id="tab-123456",
            mission_id="m_20260520000102_abcd1234",
            goal="ship selected brief",
            state="working",
            last_event="tests are running",
            buffer_changed_at=0,
        )
        memory = dashboard.db.MissionMemory(
            mission_id=mission.mission_id,
            title="Selected brief",
            why="recover intent before attaching",
            current_plan="summarize graph first",
            next_step="verify the b key modal",
            phase="testing",
            source_kind="prd",
            source_ref="PRD.md",
        )
        event = dashboard.db.MissionEvent(
            id=1,
            mission_id=mission.mission_id,
            ts=0,
            kind="decision",
            actor="user",
            summary="brief must be cited",
            source_ref="PRD.md:934",
        )
        artifact = dashboard.db.MissionArtifact(
            id=7,
            mission_id=mission.mission_id,
            kind="test",
            path_or_url="tests/test_dashboard.py",
            status="pass",
            summary="dashboard coverage",
            created_at=0,
        )

        brief = mission_brief.build_selected_brief(
            mission,
            memory=memory,
            events=[event],
            artifacts=[artifact],
            transcript="old line\nlatest terminal detail",
            generated_at=0,
        )

        self.assertEqual(brief.title, "Selected brief")
        self.assertIn("recover intent before attaching [prd:PRD.md]", brief.body)
        self.assertIn("State: working; phase: testing", brief.body)
        self.assertIn("decision/user: brief must be cited [PRD.md:934]", brief.body)
        self.assertIn("pass test: tests/test_dashboard.py", brief.body)
        self.assertIn("verify the b key modal [prd:PRD.md]", brief.body)
        self.assertIn("latest terminal detail [tab:tab-123456]", brief.body)

    def test_mission_card_render_prioritizes_latest_output_when_collapsed(self) -> None:
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
            buffer="\n".join(f"line {i}" for i in range(12)),
            observed_at=0,
        )

        rendered = card._render_card(mission, memory, [event], [artifact], live).plain

        self.assertIn("Graph card", rendered)
        self.assertIn("tab tab", rendered)
        self.assertIn("LATEST OUTPUT", rendered)
        self.assertIn("line 0", rendered)
        self.assertIn("line 11", rendered)
        self.assertNotIn("why: recover stale agent intent", rendered)
        self.assertNotIn("decision build card before edit flow", rendered)

        card.toggle_details()
        expanded = card._render_card(mission, memory, [event], [artifact], live).plain

        self.assertIn("phase: planning", expanded)
        self.assertIn("why: recover stale agent intent", expanded)
        self.assertIn("next: wire edit flow", expanded)
        self.assertIn("decision build card before edit flow", expanded)
        self.assertIn("pass test tests/test_dashboard.py", expanded)

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

    def test_edit_mission_screen_parses_claims_and_pr(self) -> None:
        memory = dashboard.db.MissionMemory(
            mission_id="m_20260520000102_abcd1234",
            title="Old title",
            why="old why",
            phase="planning",
            source_kind="user",
            claimed_paths='["src/old.py"]',
        )
        self.assertEqual(dashboard._parse_optional_pr("#225"), 225)
        self.assertEqual(
            dashboard._normalize_claimed_paths("src/a.py, tests/test_a.py"),
            '["src/a.py", "tests/test_a.py"]',
        )
        self.assertEqual(dashboard._display_claimed_paths(memory.claimed_paths), "src/old.py")

    def test_resume_command_uses_agent_command_or_codex_fallback(self) -> None:
        self.assertTrue(dashboard._resume_command("codex --yolo", "hello").startswith("codex --yolo "))
        self.assertTrue(dashboard._resume_command("npm test", "hello").startswith("codex "))

    def test_prd_tree_state_helpers_round_trip_parent_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "tree-state.json"

            self.assertTrue(dashboard._save_prd_collapsed_ids({"m_parent_b", "m_parent_a"}, state_path))

            self.assertEqual(
                dashboard._load_prd_collapsed_ids(state_path),
                {"m_parent_a", "m_parent_b"},
            )

    def test_prd_tree_state_helper_rejects_corrupt_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "tree-state.json"
            state_path.write_text("{not json", encoding="utf-8")

            with self.assertRaises(ValueError):
                dashboard._load_prd_collapsed_ids(state_path)

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

    def test_session_headline_scans_recent_tail_of_large_buffers(self) -> None:
        rendered = _session_headline(
            ("old noise\n" * 5000) + "The recent answer is what matters here.",
            fallback="idle",
        )

        self.assertEqual(rendered, "The recent answer is what matters here.")

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

    def test_live_stream_update_buffers_can_skip_render(self) -> None:
        stream = dashboard.LiveStreamWidget()

        with (
            patch.object(stream, "_sync_shards_if_needed") as sync_shards,
            patch.object(stream, "_render_live") as render_live,
        ):
            stream.update_buffers({}, None, render=False)

        sync_shards.assert_called_once()
        render_live.assert_not_called()

    def test_live_stream_update_buffers_can_skip_sync_and_render(self) -> None:
        stream = dashboard.LiveStreamWidget()

        with (
            patch.object(stream, "_sync_shards_if_needed") as sync_shards,
            patch.object(stream, "_render_live") as render_live,
        ):
            stream.update_buffers({}, None, render=False, sync=False)

        sync_shards.assert_not_called()
        render_live.assert_not_called()

    def test_refresh_live_stream_can_update_without_rendering_rain(self) -> None:
        app = DashboardHarness()
        stream = dashboard.LiveStreamWidget()

        with (
            patch.object(app, "query_one", return_value=stream),
            patch.object(app, "_selected_tab_id", return_value="tab-123456"),
            patch.object(stream, "update_buffers") as update_buffers,
        ):
            app._refresh_live_stream(render=False)

        update_buffers.assert_called_once_with(
            app.live_buffers,
            "tab-123456",
            render=False,
            sync=True,
        )

    def test_refresh_table_does_not_repaint_rain(self) -> None:
        app = DashboardHarness()
        fake_table = SimpleNamespace(refresh_rows=lambda *args: None)

        with (
            patch.object(dashboard.db, "all_missions", return_value=[]),
            patch.object(app, "_prd_tree_context", return_value=([], [])),
            patch.object(app, "query_one", return_value=fake_table),
            patch.object(app, "_refresh_mission_card"),
            patch.object(app, "_refresh_live_stream") as refresh_live_stream,
        ):
            app._refresh_table()

        refresh_live_stream.assert_called_once_with(render=False, sync=False)

    def test_rain_cadence_is_low_fps_by_default(self) -> None:
        self.assertGreaterEqual(dashboard.RAIN_INTERVAL_SECONDS, 2.0)

    async def test_zoomed_terminal_uses_compact_in_bounds_layout(self) -> None:
        app = DashboardHarness()

        with isolated_dashboard_runtime():
            async with app.run_test(size=(70, 20)) as pilot:
                await pilot.pause()

                self.assertTrue(app.screen.has_class("compact"))
                header = app.query_one("#header")
                body = app.query_one("#body")
                rain = app.query_one("#rain-panel")
                missions = app.query_one("#missions-panel")
                card = app.query_one("#mission-card-panel")
                alerts = app.query_one("#alerts-panel")

                self.assertGreaterEqual(header.region.y, 0)
                self.assertGreaterEqual(body.region.height, 6)
                self.assertEqual(rain.region.height, body.region.height)
                self.assertEqual(missions.region.height, body.region.height)
                self.assertEqual(card.region.height, body.region.height)
                self.assertLessEqual(alerts.region.y + alerts.region.height, app.size.height)

    async def test_standard_terminal_panels_fill_body_region(self) -> None:
        app = DashboardHarness()

        with isolated_dashboard_runtime():
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()

                self.assertFalse(app.screen.has_class("compact"))
                body = app.query_one("#body")
                self.assertEqual(app.query_one("#rain-panel").region.height, body.region.height)
                self.assertEqual(app.query_one("#missions-panel").region.height, body.region.height)
                self.assertEqual(app.query_one("#mission-card-panel").region.height, body.region.height)

    def test_sync_shards_only_when_buffer_signature_changes(self) -> None:
        stream = dashboard.LiveStreamWidget()
        stream.buffers = {
            "tab-123456": LiveBuffer(
                tab_id="tab-123456",
                goal="current debate on X",
                state="working",
                last_event="active output",
                buffer="Searching the web\nTwo debate clusters are driving the timeline.",
                observed_at=1,
            )
        }

        with patch.object(stream, "_sync_shards") as sync_shards:
            stream._sync_shards_if_needed()
            stream._sync_shards_if_needed()

        sync_shards.assert_called_once()

    def test_new_stream_update_replaces_top_line_and_drops_old_line(self) -> None:
        stream = dashboard.LiveStreamWidget()
        stream.buffers = {
            "tab-123456": LiveBuffer(
                tab_id="tab-123456",
                goal="X",
                state="working",
                last_event="active output",
                buffer="First useful status.",
                observed_at=1,
            )
        }

        with patch.object(dashboard.time, "monotonic", return_value=1.0):
            stream._sync_shards()

        stream.buffers["tab-123456"] = LiveBuffer(
            tab_id="tab-123456",
            goal="X",
            state="working",
            last_event="active output",
            buffer="Second useful status.",
            observed_at=2,
        )
        with patch.object(dashboard.time, "monotonic", return_value=2.0):
            stream._sync_shards()

        current = stream.shards["tab-123456"]
        self.assertEqual(current.x, 0)
        self.assertEqual(current.y, 0)
        self.assertIn("Second usefu", current.text)
        self.assertEqual(len(stream.falling_shards), 1)
        self.assertEqual(stream.falling_shards[0].x, 0)
        self.assertGreaterEqual(stream.falling_shards[0].y, 1)
        self.assertIn("First useful", stream.falling_shards[0].text)

    def test_idle_stream_adds_capped_matrix_pulses_without_replacing_status(self) -> None:
        stream = dashboard.LiveStreamWidget()
        stream.buffers = {
            "tab-123456": LiveBuffer(
                tab_id="tab-123456",
                goal="current debate on X",
                state="working",
                last_event="active output",
                buffer="Latest real status.",
                observed_at=1,
            )
        }

        with patch.object(dashboard.time, "monotonic", return_value=1.0):
            stream._sync_shards_if_needed()
        current_text = stream.shards["tab-123456"].text

        for step in range(dashboard.MAX_FALLING_STREAM_SHARDS + 4):
            with patch.object(dashboard.time, "monotonic", return_value=3.0 + step):
                stream._sync_shards_if_needed()

        self.assertEqual(stream.shards["tab-123456"].text, current_text)
        self.assertEqual(len(stream.falling_shards), dashboard.MAX_FALLING_STREAM_SHARDS)
        self.assertTrue(all(shard.ambient for shard in stream.falling_shards))

    def test_header_shows_project_root_and_hidden_count(self) -> None:
        project = dashboard.db.ProjectTenant(
            tenant_id="p_current",
            name="current",
            root_path="/Users/fabianbaier/current",
        )
        app = DashboardHarness(project=project)
        current = dashboard.db.Mission(tab_id="tab-current", tenant_id="p_current")
        hidden = dashboard.db.Mission(tab_id="tab-hidden", tenant_id="p_hidden")

        def fake_all_missions(tenant_id=None):
            if tenant_id == "p_current":
                return [current]
            return [current, hidden]

        with patch.object(dashboard.db, "all_missions", new=fake_all_missions):
            rendered = app._header_text(compact=True).plain

        self.assertIn("project: current", rendered)
        self.assertIn("~/current", rendered)
        self.assertIn("1 hidden elsewhere", rendered)
        self.assertIn("press t to switch", rendered)

    def test_project_switch_result_updates_scope(self) -> None:
        project_a = dashboard.db.ProjectTenant(
            tenant_id="p_a",
            name="a",
            root_path="/tmp/a",
        )
        project_b = dashboard.db.ProjectTenant(
            tenant_id="p_b",
            name="b",
            root_path="/tmp/b",
        )
        mission_b = dashboard.db.Mission(tab_id="tab-b", tenant_id="p_b")
        app = DashboardHarness(project=project_a)
        app.live_buffers = {
            "tab-a": dashboard.LiveBuffer("tab-a", "a", "working", "", "", 0),
            "tab-b": dashboard.LiveBuffer("tab-b", "b", "working", "", "", 0),
        }

        def fake_all_missions(tenant_id=None):
            if tenant_id == "p_b":
                return [mission_b]
            return []

        with (
            patch.object(dashboard.db, "get_project_tenant", new=lambda tenant_id: project_b),
            patch.object(dashboard.db, "all_missions", new=fake_all_missions),
            patch.object(app, "_refresh_table", new=lambda: None),
        ):
            app._handle_project_switch_result(dashboard.ProjectSwitchRequest("p_b"))

        self.assertEqual(app.project, project_b)
        self.assertFalse(app.show_all)
        self.assertEqual(app.tenant_id, "p_b")
        self.assertEqual(set(app.live_buffers), {"tab-b"})

    def test_project_cleanup_result_purges_current_scope_and_switches_global(self) -> None:
        project = dashboard.db.ProjectTenant(
            tenant_id="p_old",
            name="old",
            root_path="/tmp/old",
        )
        app = DashboardHarness(project=project)
        captured = {}
        cleanup = dashboard.db.ProjectCleanupResult(
            tenant_id=project.tenant_id,
            name=project.name,
            root_path=project.root_path,
            deleted={"project_tenants": 1, "mission_memory": 2},
        )

        def fake_log_action(action, tab_id=None, details=None):
            captured["ledger"] = (action, tab_id, details)
            return 1

        with (
            patch.object(dashboard.db, "delete_project_tenant", new=lambda tenant_id, allow_live=False: cleanup),
            patch.object(dashboard.ledger_mod, "log_action", new=fake_log_action),
            patch.object(dashboard.db, "all_missions", new=lambda tenant_id=None: []),
            patch.object(app, "_refresh_table", new=lambda: None),
        ):
            app._handle_project_switch_result(dashboard.ProjectSwitchRequest("p_old", action="delete"))

        self.assertIsNone(app.project)
        self.assertTrue(app.show_all)
        self.assertEqual(app.tenant_id, "")
        self.assertEqual(captured["ledger"][0], "project_delete")
        self.assertEqual(captured["ledger"][2]["deleted"], cleanup.deleted)
        self.assertIn("deleted project", app.alerts[-1].text)

    async def test_project_switch_key_opens_switcher(self) -> None:
        project = dashboard.db.ProjectTenant(
            tenant_id="p_current",
            name="current",
            root_path="/tmp/current",
        )
        app = DashboardHarness(project=project)

        with (
            isolated_dashboard_runtime(),
            patch.object(dashboard.tenant_mod, "backfill_known_tenants", new=lambda: 0),
            patch.object(dashboard.db, "all_project_tenants", new=lambda include_archived=False: [project]),
            patch.object(dashboard.db, "all_missions", new=lambda tenant_id=None: []),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.press("t")
                await pilot.pause()

                self.assertIsInstance(app.screen, dashboard.ProjectSwitchScreen)

    async def test_project_switcher_shows_action_legend(self) -> None:
        project = dashboard.db.ProjectTenant(
            tenant_id="p_current",
            name="current",
            root_path="/tmp/current",
        )
        app = DashboardHarness(project=project)

        with (
            isolated_dashboard_runtime(),
            patch.object(dashboard.tenant_mod, "backfill_known_tenants", new=lambda: 0),
            patch.object(dashboard.db, "all_project_tenants", new=lambda include_archived=False: [project]),
            patch.object(dashboard.db, "all_missions", new=lambda tenant_id=None: []),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.press("t")
                await pilot.pause()

                legend = app.screen.query_one("#project_legend", dashboard.Static).content

        self.assertIn("p prune empty", str(legend))
        self.assertIn("d delete stored graph", str(legend))
        self.assertIn("n nuke active", str(legend))

    async def test_project_nuke_closes_live_tabs_and_purges_project(self) -> None:
        project = dashboard.db.ProjectTenant(
            tenant_id="p_active",
            name="active",
            root_path="/tmp/active",
        )
        mission_a = dashboard.db.Mission(tab_id="tab-a", tenant_id=project.tenant_id)
        mission_b = dashboard.db.Mission(tab_id="tab-b", tenant_id=project.tenant_id)
        app = DashboardHarness(project=project)
        app.iterm_conn = object()
        app.live_buffers = {
            "tab-a": dashboard.LiveBuffer("tab-a", "a", "working", "", "", 0),
            "tab-b": dashboard.LiveBuffer("tab-b", "b", "working", "", "", 0),
        }
        closed: list[str] = []
        captured = {}
        cleanup = dashboard.db.ProjectCleanupResult(
            tenant_id=project.tenant_id,
            name=project.name,
            root_path=project.root_path,
            deleted={"project_tenants": 1, "missions": 2, "mission_memory": 2},
        )

        async def fake_close_tab(connection, tab_id):
            closed.append(tab_id)
            return True

        def fake_all_missions(tenant_id=None):
            if tenant_id == project.tenant_id:
                return [mission_a, mission_b]
            return []

        def fake_delete_project_tenant(tenant_id, allow_live=False):
            captured["delete"] = (tenant_id, allow_live)
            return cleanup

        def fake_log_action(action, tab_id=None, details=None):
            captured["ledger"] = (action, tab_id, details)
            return 1

        with (
            patch.object(dashboard.db, "get_project_tenant", new=lambda tenant_id: project),
            patch.object(dashboard.db, "all_missions", new=fake_all_missions),
            patch.object(dashboard.iterm_client, "close_tab", new=fake_close_tab),
            patch.object(dashboard.db, "delete_project_tenant", new=fake_delete_project_tenant),
            patch.object(dashboard.ledger_mod, "log_action", new=fake_log_action),
            patch.object(app, "_refresh_table", new=lambda: None),
        ):
            await app._handle_project_nuke_result(dashboard.ProjectSwitchRequest("p_active", action="nuke"))

        self.assertEqual(closed, ["tab-a", "tab-b"])
        self.assertEqual(captured["delete"], ("p_active", True))
        self.assertEqual(captured["ledger"][0], "project_nuke")
        self.assertEqual(captured["ledger"][2]["closed_tabs"], 2)
        self.assertIsNone(app.project)
        self.assertTrue(app.show_all)
        self.assertEqual(app.tenant_id, "")
        self.assertEqual(app.live_buffers, {})
        self.assertIn("nuked project", app.alerts[-1].text)

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

    async def test_observed_idle_session_reconciles_ready_ticker_if_transition_was_missed(self) -> None:
        app = DashboardHarness()
        events = []
        now = time.time()
        mission = dashboard.db.Mission(
            tab_id="tab-123456",
            mission_id="m_20260520000102_abcd1234",
            goal="workday coordinator",
            state="idle",
            last_event="idle 44s",
            buffer_hash="hash-ready",
            buffer_changed_at=now - 44,
        )
        tab = dashboard.iterm_client.TabInfo(
            tab_id=mission.tab_id,
            session_id="session-123456",
            window_id="window-123456",
            current_name="workday1 coordinator",
            buffer="\n".join(
                [
                    "Checked the dashboard and tests.",
                    "Bottom line: The repo is clean and aligned with origin/main.",
                    "› Use /skills to list available skills",
                ]
            ),
        )
        detection = SimpleNamespace(last_event="idle 44s")

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

        with patch.object(dashboard.time, "time", return_value=now), patch.object(
            dashboard.db, "add_event", new=fake_add_event
        ):
            await app._on_tab_observed(tab, mission, detection)
            await app._on_tab_observed(tab, mission, detection)

        self.assertEqual(len(app.alerts), 1)
        self.assertEqual(
            app.alerts[0].text,
            "ready [workday coordinator] — Bottom line: The repo is clean and aligned with origin/main.",
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["metadata"]["summary_kind"], "ready")

    async def test_stale_observed_idle_session_does_not_replay_ready_ticker(self) -> None:
        app = DashboardHarness()
        now = time.time()
        mission = dashboard.db.Mission(
            tab_id="tab-123456",
            mission_id="m_20260520000102_abcd1234",
            goal="old idle",
            state="idle",
            last_event="idle 10m",
            buffer_hash="hash-old",
            buffer_changed_at=now - dashboard.READY_RECONCILE_SECONDS - 1,
        )
        tab = dashboard.iterm_client.TabInfo(
            tab_id=mission.tab_id,
            session_id="session-123456",
            window_id="window-123456",
            current_name="old idle",
            buffer="This finished a while ago.",
        )

        with patch.object(dashboard.time, "time", return_value=now):
            await app._on_tab_observed(tab, mission, SimpleNamespace(last_event="idle 10m"))

        self.assertEqual(list(app.alerts), [])

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

                await app.pop_screen()
                await pilot.pause()

                await app.push_screen(LoopManagerScreen(
                    loops=[
                        dashboard.db.PromptLoop(
                            id=1,
                            name="news",
                            prompt="summarize news",
                            interval_seconds=300,
                            command="printf ok",
                            next_run_at=0,
                        )
                    ],
                    runs_by_loop={},
                    join_target_label="ticker/context only",
                ))
                await pilot.pause()
                self.assertIsInstance(app.screen, LoopManagerScreen)

                await app.pop_screen()
                await pilot.pause()

                await app.push_screen(SelectedBriefScreen(BriefScreenContent("Brief", "body")))
                await pilot.pause()
                self.assertIsInstance(app.screen, SelectedBriefScreen)

                await app.pop_screen()
                await pilot.pause()

                mission = dashboard.db.Mission(
                    tab_id="tab-123456",
                    mission_id="m_20260520000102_abcd1234",
                    goal="edit mission",
                )
                memory = dashboard.db.MissionMemory(
                    mission_id=mission.mission_id,
                    title="Edit mission",
                )
                await app.push_screen(EditMissionScreen(mission, memory))
                await pilot.pause()
                self.assertIsInstance(app.screen, EditMissionScreen)

                await app.pop_screen()
                await pilot.pause()

                await app.push_screen(WorkerScreen(parent_id="m_parent", run_title="PRD Run"))
                await pilot.pause()
                self.assertIsInstance(app.screen, WorkerScreen)

    async def test_new_session_key_opens_modal_without_worker_crash(self) -> None:
        app = DashboardHarness()

        with isolated_dashboard_runtime():
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.press("n")
                await pilot.pause()

                self.assertIsInstance(app.screen, NewSessionScreen)

    async def test_edit_mission_key_opens_modal_for_selected_mission(self) -> None:
        app = DashboardHarness()
        mission = dashboard.db.Mission(
            tab_id="tab-123456",
            mission_id="m_20260520000102_abcd1234",
            goal="edit this mission",
            state="working",
            buffer_changed_at=1,
        )
        memory = dashboard.db.MissionMemory(
            mission_id=mission.mission_id,
            title="Edit this mission",
            why="make stale intent recoverable",
        )

        with isolated_dashboard_runtime([mission]), patch.object(
            dashboard.db, "get", new=lambda tab_id: mission if tab_id == mission.tab_id else None
        ), patch.object(
            dashboard.db, "get_memory", new=lambda mission_id: memory if mission_id == memory.mission_id else None
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                app._refresh_table()
                await pilot.pause()
                await pilot.press("e")
                await pilot.pause()

                self.assertIsInstance(app.screen, EditMissionScreen)
                self.assertEqual(app.screen.query_one("#goal_input", Input).value, "edit this mission")
                self.assertEqual(app.screen.query_one("#why_input", Input).value, "make stale intent recoverable")

    async def test_brief_selected_key_opens_modal_for_selected_mission(self) -> None:
        app = DashboardHarness()
        mission = dashboard.db.Mission(
            tab_id="tab-123456",
            mission_id="m_20260520000102_abcd1234",
            goal="brief this mission",
            state="working",
            last_event="waiting for tests",
            buffer_changed_at=1,
        )
        memory = dashboard.db.MissionMemory(
            mission_id=mission.mission_id,
            title="Brief this mission",
            why="show stale context fast",
            next_step="attach only if the brief says so",
            phase="reviewing",
            source_kind="user",
            source_ref="tab:tab-123456",
        )
        event = dashboard.db.MissionEvent(
            id=1,
            mission_id=mission.mission_id,
            ts=0,
            kind="summary",
            actor="morpheus",
            summary="ready for review",
            source_ref="tab:tab-123456",
        )

        with isolated_dashboard_runtime([mission]), patch.object(
            dashboard.db, "get", new=lambda tab_id: mission if tab_id == mission.tab_id else None
        ), patch.object(
            dashboard.db, "get_memory", new=lambda mission_id: memory if mission_id == memory.mission_id else None
        ), patch.object(
            dashboard.db, "recent_events", new=lambda mission_id, limit=5: [event]
        ), patch.object(
            dashboard.db, "artifacts_for_mission", new=lambda mission_id, limit=5: []
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                app.live_buffers[mission.tab_id] = LiveBuffer(
                    tab_id=mission.tab_id,
                    goal=mission.goal,
                    state=mission.state,
                    last_event=mission.last_event,
                    buffer="first\nlatest output",
                    observed_at=0,
                )
                app._refresh_table()
                await pilot.pause()
                await pilot.press("b")
                await pilot.pause()

                self.assertIsInstance(app.screen, SelectedBriefScreen)
                self.assertIn("show stale context fast", app.screen.brief.body)
                self.assertIn("ready for review", app.screen.brief.body)
                self.assertIn("latest output", app.screen.brief.body)

    async def test_brief_selected_without_selection_pushes_alert(self) -> None:
        app = DashboardHarness()

        with isolated_dashboard_runtime():
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.press("b")
                await pilot.pause()

        self.assertEqual(app.alerts[0].kind, "error")
        self.assertIn("no mission selected", app.alerts[0].text)

    async def test_prd_parent_enter_and_space_toggle_children_and_persist_state(self) -> None:
        app = DashboardHarness()
        parent = dashboard.db.MissionMemory(
            mission_id="m_parent",
            title="PRD tree state",
            topic="prd-run",
            source_kind="prd",
            updated_at=100,
        )
        child = dashboard.db.Mission(
            tab_id="tab-child",
            mission_id="m_child",
            goal="coordinator",
            state="working",
            buffer_changed_at=90,
        )
        edge = dashboard.db.MissionEdge(
            id=1,
            from_id=parent.mission_id,
            to_id=child.mission_id,
            relation="coordinator",
            reason="coordinator",
            created_at=95,
        )

        with tempfile.TemporaryDirectory() as tmpdir, isolated_dashboard_runtime(
            [child], [parent], [edge]
        ), patch.object(
            dashboard, "PRD_TREE_STATE_PATH", new=Path(tmpdir) / "tree-state.json"
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                app._refresh_table()
                await pilot.pause()

                table = app.query_one(dashboard.MissionsTable)
                self.assertEqual([ref.role for ref in table.row_refs], ["prd", "coordinator"])
                self.assertEqual(table.get_row_at(0)[1].plain, "▾")

                await pilot.press("enter")
                await pilot.pause()

                self.assertEqual([ref.role for ref in table.row_refs], ["prd"])
                self.assertEqual(table.get_row_at(0)[1].plain, "▸")
                self.assertEqual(dashboard._load_prd_collapsed_ids(), {"m_parent"})
                self.assertIn("collapsed PRD run", app.alerts[0].text)

                await pilot.press("o")
                await pilot.pause()

                self.assertEqual([ref.role for ref in table.row_refs], ["prd", "coordinator"])
                self.assertEqual(table.get_row_at(0)[1].plain, "▾")
                self.assertEqual(dashboard._load_prd_collapsed_ids(), set())
                self.assertIn("expanded PRD run", app.alerts[0].text)

                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(dashboard._load_prd_collapsed_ids(), {"m_parent"})

            restarted = DashboardHarness()
            restarted.prd_collapsed_ids = dashboard._load_prd_collapsed_ids()
            async with restarted.run_test(size=(120, 40)) as restart_pilot:
                restarted._refresh_table()
                await restart_pilot.pause()
                restarted_table = restarted.query_one(dashboard.MissionsTable)
                self.assertEqual([ref.role for ref in restarted_table.row_refs], ["prd"])

    async def test_refresh_table_preserves_prd_child_selection(self) -> None:
        app = DashboardHarness()
        parent = dashboard.db.MissionMemory(
            mission_id="m_parent",
            title="kms implementation",
            topic="prd-run",
            source_kind="prd",
            updated_at=100,
        )
        coordinator = dashboard.db.Mission(
            tab_id="tab-coordinator",
            mission_id="m_coordinator",
            goal="kms implementation coordinator",
            state="working",
            buffer_changed_at=100,
        )
        worker = dashboard.db.Mission(
            tab_id="tab-worker",
            mission_id="m_worker",
            goal="tester",
            state="working",
            buffer_changed_at=100,
        )
        edges = [
            dashboard.db.MissionEdge(
                id=1,
                from_id=parent.mission_id,
                to_id=coordinator.mission_id,
                relation="coordinator",
                reason="coordinator",
                created_at=100,
            ),
            dashboard.db.MissionEdge(
                id=2,
                from_id=parent.mission_id,
                to_id=worker.mission_id,
                relation="worker",
                reason="worker",
                created_at=101,
            ),
        ]

        with tempfile.TemporaryDirectory() as tmpdir, isolated_dashboard_runtime(
            [coordinator, worker], [parent], edges
        ), patch.object(
            dashboard, "PRD_TREE_STATE_PATH", new=Path(tmpdir) / "tree-state.json"
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                app._refresh_table()
                await pilot.pause()
                table = app.query_one(dashboard.MissionsTable)
                self.assertEqual([ref.role for ref in table.row_refs], ["prd", "coordinator", "worker"])

                await pilot.press("j")
                await pilot.press("j")
                await pilot.pause()
                self.assertEqual(table.selected_ref().mission_id, worker.mission_id)

                app._refresh_table()
                await pilot.pause()

                self.assertEqual(table.cursor_row, 2)
                self.assertEqual(table.selected_ref().mission_id, worker.mission_id)

    async def test_prd_tree_toggle_reports_persistence_failure_without_mutating_state(self) -> None:
        app = DashboardHarness()
        app.prd_collapsed_ids = set()
        parent = dashboard.db.MissionMemory(
            mission_id="m_parent",
            title="PRD tree state",
            topic="prd-run",
            source_kind="prd",
            updated_at=100,
        )

        with tempfile.TemporaryDirectory() as tmpdir, isolated_dashboard_runtime(
            [], [parent], []
        ), patch.object(
            dashboard, "PRD_TREE_STATE_PATH", new=Path(tmpdir) / "tree-state.json"
        ), patch.object(
            dashboard, "_toggle_prd_collapsed_id", side_effect=OSError("disk full")
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                app._refresh_table()
                await pilot.pause()

                await pilot.press("enter")
                await pilot.pause()

                self.assertEqual(app.prd_collapsed_ids, set())
                self.assertIn("PRD tree state save failed", app.alerts[0].text)

    async def test_kill_prd_parent_archives_run_and_closes_child_tabs(self) -> None:
        app = DashboardHarness()
        parent = dashboard.db.MissionMemory(
            mission_id="m_parent",
            title="new1",
            topic="prd-run",
            source_kind="prd",
            updated_at=100,
        )
        child = dashboard.db.Mission(
            tab_id="tab-child",
            mission_id="m_child",
            goal="new1 coordinator",
            state="idle",
            buffer_changed_at=90,
        )
        edge = dashboard.db.MissionEdge(
            id=1,
            from_id=parent.mission_id,
            to_id=child.mission_id,
            relation="coordinator",
            reason="coordinator",
            created_at=95,
        )
        closed: list[str] = []
        deleted: list[str] = []
        archived: list[str] = []

        async def fake_close_tab(connection, tab_id):
            closed.append(tab_id)
            return True

        with isolated_dashboard_runtime([child], [parent], [edge]), patch.object(
            dashboard.iterm_client, "close_tab", new=fake_close_tab
        ), patch.object(
            dashboard.db, "delete", new=lambda tab_id: deleted.append(tab_id)
        ), patch.object(
            dashboard.db, "archive_memory", new=lambda mission_id, summary="": archived.append(mission_id)
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                app._refresh_table()
                await pilot.pause()
                await pilot.press("d")
                await pilot.pause()

        self.assertEqual(closed, ["tab-child"])
        self.assertEqual(deleted, ["tab-child"])
        self.assertEqual(archived, ["m_parent"])
        self.assertIn("killed PRD run", app.alerts[0].text)

    async def test_focus_selected_session_does_not_pollute_ticker(self) -> None:
        app = DashboardHarness()
        mission = dashboard.db.Mission(
            tab_id="tab-live",
            mission_id="m_live",
            goal="live codex",
            state="working",
            buffer_changed_at=1,
        )
        actions: list[str] = []

        class FakeWindow:
            window_id = "window-live"

            async def async_activate(self):
                actions.append("window")

        class FakeTab:
            tab_id = "tab-live"

            async def async_select(self):
                actions.append("tab")

        window = FakeWindow()
        window.tabs = [FakeTab()]

        async def fake_get_app(connection):
            return SimpleNamespace(windows=[window])

        with isolated_dashboard_runtime(missions=[mission], memories=[]), patch.object(
            dashboard.iterm2, "async_get_app", new=fake_get_app
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                app._refresh_table()
                await pilot.pause()
                app.alerts.clear()
                await pilot.press("enter")
                await pilot.pause()

        self.assertEqual(actions, ["window", "tab"])
        self.assertEqual(list(app.alerts), [])

    async def test_kill_closed_row_dismisses_it_from_dashboard(self) -> None:
        app = DashboardHarness()
        memory = dashboard.db.MissionMemory(
            mission_id="m_closed",
            title="closed codex",
            phase="archived",
            agent_kind="codex",
            resume_command="codex resume 019e466d-0fd8-7441-aa1f-32a5db211a73",
            resume_confidence="exact",
            archived_at=10,
            closed_at=10,
        )
        dismissed: list[str] = []
        final_row_count = 0

        def fake_dismiss(mission_id, summary=""):
            dismissed.append(mission_id)
            memory.resume_command = ""
            return True

        with isolated_dashboard_runtime(missions=[], memories=[memory]), patch.object(
            dashboard.db, "dismiss_closed_resume", new=fake_dismiss
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                app._refresh_table()
                await pilot.pause()
                table = app.query_one(dashboard.MissionsTable)
                self.assertEqual(table.selected_ref().role, "closed")
                app.alerts.clear()
                await pilot.press("d")
                await pilot.pause()
                final_row_count = table.row_count

        self.assertEqual(dismissed, ["m_closed"])
        self.assertEqual(list(app.alerts), [])
        self.assertEqual(final_row_count, 0)

    async def test_prune_archives_orphan_prd_parent_rows(self) -> None:
        app = DashboardHarness()
        orphan_parent = dashboard.db.MissionMemory(
            mission_id="m_orphan",
            title="old run",
            topic="prd-run",
            source_kind="prd",
            updated_at=1,
        )
        active_parent = dashboard.db.MissionMemory(
            mission_id="m_active",
            title="active run",
            topic="prd-run",
            source_kind="prd",
            updated_at=1,
        )
        child = dashboard.db.Mission(
            tab_id="tab-child",
            mission_id="m_child",
            goal="active coordinator",
            state="working",
            buffer_changed_at=1,
        )
        edge = dashboard.db.MissionEdge(
            id=1,
            from_id=active_parent.mission_id,
            to_id=child.mission_id,
            relation="coordinator",
            reason="coordinator",
            created_at=1,
        )
        archived: list[str] = []

        async def fake_enumerate_tabs(connection):
            return [SimpleNamespace(tab_id="tab-child")]

        with isolated_dashboard_runtime([child], [orphan_parent, active_parent], [edge]), patch.object(
            dashboard.iterm_client, "enumerate_tabs", new=fake_enumerate_tabs
        ), patch.object(
            dashboard.db, "archive_memory", new=lambda mission_id, summary="": archived.append(mission_id)
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                app._refresh_table()
                await pilot.pause()
                await pilot.press("p")
                await pilot.pause()

        self.assertEqual(archived, ["m_orphan"])
        self.assertIn("archived 1 orphan PRD runs", app.alerts[0].text)

    async def test_prune_closed_row_dismisses_it_from_dashboard(self) -> None:
        app = DashboardHarness()
        memory = dashboard.db.MissionMemory(
            mission_id="m_closed",
            title="closed codex",
            phase="archived",
            agent_kind="codex",
            resume_command="codex resume 019e466d-0fd8-7441-aa1f-32a5db211a73",
            resume_confidence="exact",
            archived_at=10,
            closed_at=10,
        )
        dismissed: list[str] = []
        final_row_count = 0

        def fake_dismiss(mission_id, summary=""):
            dismissed.append(mission_id)
            memory.resume_command = ""
            return True

        with isolated_dashboard_runtime(missions=[], memories=[memory]), patch.object(
            dashboard.db, "dismiss_closed_resume", new=fake_dismiss
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                app._refresh_table()
                await pilot.pause()
                table = app.query_one(dashboard.MissionsTable)
                self.assertEqual(table.selected_ref().role, "closed")
                app.alerts.clear()
                await pilot.press("p")
                await pilot.pause()
                final_row_count = table.row_count

        self.assertEqual(dismissed, ["m_closed"])
        self.assertEqual(list(app.alerts), [])
        self.assertEqual(final_row_count, 0)

    async def test_edit_mission_without_selection_pushes_alert(self) -> None:
        app = DashboardHarness()

        with isolated_dashboard_runtime():
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.press("e")
                await pilot.pause()

        self.assertEqual(app.alerts[0].kind, "error")
        self.assertIn("no mission selected", app.alerts[0].text)

    async def test_edit_mission_submit_updates_memory_live_fields_and_event(self) -> None:
        app = DashboardHarness()
        done = asyncio.Event()
        captured = {}
        mission = dashboard.db.Mission(
            tab_id="tab-123456",
            mission_id="m_20260520000102_abcd1234",
            goal="old goal",
            state="working",
            linked_pr=224,
            linked_worktree="/tmp/old",
            buffer_changed_at=1,
        )
        memory = dashboard.db.MissionMemory(
            mission_id=mission.mission_id,
            title="Old title",
            why="old why",
            phase="planning",
            source_kind="imported",
            source_ref="tab:old",
        )

        def fake_upsert_memory(updated):
            captured["memory"] = updated

        project = dashboard.db.ProjectTenant(
            tenant_id="p_edit",
            name="edit",
            root_path="/tmp/new",
        )

        def fake_update_mission_details(
            tab_id,
            *,
            goal,
            linked_pr,
            linked_worktree,
            tenant_id="",
            project_root="",
        ):
            captured["live"] = {
                "tab_id": tab_id,
                "goal": goal,
                "linked_pr": linked_pr,
                "linked_worktree": linked_worktree,
                "tenant_id": tenant_id,
                "project_root": project_root,
            }
            return True

        def fake_add_event(mission_id, kind, summary, actor="user", source_ref="", metadata=None):
            captured["event"] = {
                "mission_id": mission_id,
                "kind": kind,
                "summary": summary,
                "actor": actor,
                "source_ref": source_ref,
                "metadata": metadata,
            }
            done.set()
            return 1

        with isolated_dashboard_runtime([mission]), patch.object(
            dashboard.db, "get", new=lambda tab_id: mission if tab_id == mission.tab_id else None
        ), patch.object(
            dashboard.db, "get_memory", new=lambda mission_id: memory if mission_id == memory.mission_id else None
        ), patch.object(
            dashboard.db, "upsert_memory", new=fake_upsert_memory
        ), patch.object(
            dashboard.db, "update_mission_details", new=fake_update_mission_details
        ), patch.object(
            dashboard.db, "add_event", new=fake_add_event
        ), patch.object(
            dashboard.tenant_mod, "ensure_project_tenant", new=lambda path=None: project
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                app._refresh_table()
                await pilot.pause()
                await pilot.press("e")
                await pilot.pause()

                screen = app.screen
                self.assertIsInstance(screen, EditMissionScreen)
                screen.query_one("#goal_input", Input).value = "new goal"
                screen.query_one("#title_input", Input).value = "New title"
                screen.query_one("#why_input", Input).value = "new why"
                screen.query_one("#done_input", Input).value = "done means verified"
                screen.query_one("#criteria_input", Input).value = "criterion one"
                screen.query_one("#plan_input", Input).value = "plan next"
                screen.query_one("#next_input", Input).value = "write tests"
                screen.query_one("#linked_pr_input", Input).value = "#225"
                screen.query_one("#worktree_input", Input).value = "/tmp/new"
                screen.query_one("#claimed_paths_input", Input).value = "src/a.py, tests/test_a.py"
                screen.query_one("#topic_input", Input).value = "mission-edit"
                screen.action_submit()

                await asyncio.wait_for(done.wait(), timeout=1)

        self.assertEqual(captured["memory"].title, "New title")
        self.assertEqual(captured["memory"].why, "new why")
        self.assertEqual(captured["memory"].done_definition, "done means verified")
        self.assertEqual(captured["memory"].acceptance_criteria, "criterion one")
        self.assertEqual(captured["memory"].current_plan, "plan next")
        self.assertEqual(captured["memory"].next_step, "write tests")
        self.assertEqual(captured["memory"].claimed_paths, '["src/a.py", "tests/test_a.py"]')
        self.assertEqual(captured["memory"].topic, "mission-edit")
        self.assertEqual(captured["memory"].tenant_id, "p_edit")
        self.assertEqual(captured["memory"].project_root, "/tmp/new")
        self.assertEqual(
            captured["live"],
            {
                "tab_id": mission.tab_id,
                "goal": "new goal",
                "linked_pr": 225,
                "linked_worktree": "/tmp/new",
                "tenant_id": "p_edit",
                "project_root": "/tmp/new",
            },
        )
        self.assertEqual(captured["event"]["kind"], "mission_edit")
        self.assertEqual(captured["event"]["actor"], "user")
        self.assertEqual(captured["event"]["metadata"]["phase"], "planning")

    async def test_resume_fresh_snapshots_spawns_links_and_archives_old(self) -> None:
        app = DashboardHarness()
        done = asyncio.Event()
        captured = {"events": [], "edges": []}
        mission = dashboard.db.Mission(
            tab_id="tab-old",
            session_id="session-old",
            mission_id="m_old",
            goal="resume this mission",
            state="working",
            cmd="codex",
            linked_pr=224,
            linked_worktree="/tmp/work",
            last_event="needs fresh context",
            buffer_changed_at=1,
        )
        memory = dashboard.db.MissionMemory(
            mission_id=mission.mission_id,
            title="Resume this mission",
            why="avoid token blowup",
            current_plan="snapshot then continue",
            next_step="spawn fresh",
            phase="testing",
            source_kind="user",
            source_ref="tab:tab-old",
        )

        async def fake_enumerate_tabs(connection):
            return [SimpleNamespace(tab_id=mission.tab_id, buffer="old terminal buffer")]

        async def fake_spawn_tab(connection, *, command, goal):
            captured["spawn"] = {"connection": connection, "command": command, "goal": goal}
            return SimpleNamespace(tab_id="tab-new", session_id="session-new")

        async def fake_close_tab(connection, tab_id):
            captured["closed"] = tab_id
            return True

        def fake_upsert(updated):
            if updated.tab_id == "tab-new":
                updated.mission_id = "m_new"
                captured["new_mission"] = updated

        def fake_upsert_memory(updated):
            captured["new_memory"] = updated

        def fake_add_artifact(mission_id, kind, path_or_url, status="unknown", summary=""):
            captured["artifact"] = {
                "mission_id": mission_id,
                "kind": kind,
                "path": path_or_url,
                "summary": summary,
            }
            return 7

        def fake_add_edge(from_id, to_id, relation, reason=""):
            captured["edges"].append((from_id, to_id, relation, reason))
            return 1

        def fake_add_event(mission_id, kind, summary, actor="user", source_ref="", metadata=None):
            captured["events"].append(
                {
                    "mission_id": mission_id,
                    "kind": kind,
                    "summary": summary,
                    "actor": actor,
                    "source_ref": source_ref,
                    "metadata": metadata,
                }
            )
            return len(captured["events"])

        def fake_delete(tab_id):
            captured["deleted"] = tab_id
            done.set()

        with tempfile.TemporaryDirectory() as tmpdir, isolated_dashboard_runtime([mission]), patch.object(
            dashboard, "SNAPSHOT_DIR", new=Path(tmpdir)
        ), patch.object(
            dashboard.iterm_client, "enumerate_tabs", new=fake_enumerate_tabs
        ), patch.object(
            dashboard.iterm_client, "spawn_tab", new=fake_spawn_tab
        ), patch.object(
            dashboard.iterm_client, "close_tab", new=fake_close_tab
        ), patch.object(
            dashboard.db, "get", new=lambda tab_id: mission if tab_id == mission.tab_id else None
        ), patch.object(
            dashboard.db, "get_memory", new=lambda mission_id: memory if mission_id == memory.mission_id else None
        ), patch.object(
            dashboard.db, "recent_events", new=lambda mission_id, limit=5: []
        ), patch.object(
            dashboard.db, "artifacts_for_mission", new=lambda mission_id, limit=5: []
        ), patch.object(
            dashboard.db, "upsert", new=fake_upsert
        ), patch.object(
            dashboard.db, "upsert_memory", new=fake_upsert_memory
        ), patch.object(
            dashboard.db, "add_artifact", new=fake_add_artifact
        ), patch.object(
            dashboard.db, "add_edge", new=fake_add_edge
        ), patch.object(
            dashboard.db, "add_event", new=fake_add_event
        ), patch.object(
            dashboard.db, "delete", new=fake_delete
        ), patch.object(
            dashboard.ledger_mod, "log_action", new=lambda action, tab_id, details: captured.setdefault("ledger", (action, tab_id, details))
        ), patch.object(
            dashboard.ctx_mod, "write_context_file", new=lambda: None
        ), patch.object(
            dashboard.ctx_mod, "write_context_json", new=lambda: None
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                app._refresh_table()
                await pilot.pause()
                await pilot.press("r")
                await asyncio.wait_for(done.wait(), timeout=1)

            snapshot_path = Path(captured["artifact"]["path"])
            self.assertTrue(snapshot_path.exists())
            self.assertIn("old terminal buffer", snapshot_path.read_text())
            self.assertIn("Snapshot file:", captured["spawn"]["command"])
            self.assertIn(str(snapshot_path), captured["spawn"]["command"])
            self.assertEqual(captured["spawn"]["goal"], "resume this mission")
            self.assertEqual(captured["new_mission"].linked_pr, 224)
            self.assertEqual(captured["new_mission"].linked_worktree, "/tmp/work")
            self.assertEqual(captured["new_memory"].mission_id, "m_new")
            self.assertEqual(captured["new_memory"].source_kind, "snapshot")
            self.assertEqual(captured["new_memory"].source_ref, str(snapshot_path))
            self.assertEqual(captured["edges"][0][0:3], ("m_new", "m_old", "spawned_from"))
            self.assertEqual(captured["closed"], "tab-old")
            self.assertEqual(captured["deleted"], "tab-old")
            self.assertEqual(captured["ledger"][0], "resume_fresh")
            self.assertTrue(captured["ledger"][2]["closed_old_tab"])

    async def test_resume_closed_mission_spawns_provider_resume_and_reattaches_memory(self) -> None:
        app = DashboardHarness()
        done = asyncio.Event()
        captured = {}
        resume_id = "019e466d-0fd8-7441-aa1f-32a5db211a73"
        memory = dashboard.db.MissionMemory(
            mission_id="m_closed",
            title="Closed Codex mission",
            why="tab disappeared",
            current_plan="continue provider session",
            next_step="run tests",
            phase="archived",
            agent_kind="codex",
            resume_ref=resume_id,
            resume_command=f"cd /tmp/work && codex --yolo resume {resume_id}",
            resume_confidence="exact",
            last_tab_id="tab-old",
            closed_at=10,
            archived_at=10,
        )

        async def fake_spawn_tab(connection, *, command, goal):
            captured["spawn"] = {"connection": connection, "command": command, "goal": goal}
            return SimpleNamespace(tab_id="tab-new", session_id="session-new")

        async def fake_send_text_to_tabs(connection, tab_ids, text):
            captured["send_text"] = {
                "connection": connection,
                "tab_ids": tab_ids,
                "text": text,
            }
            return []

        def fake_upsert(mission):
            captured["mission"] = mission

        def fake_upsert_memory(updated):
            captured["memory"] = updated

        def fake_add_event(mission_id, kind, summary, actor="user", source_ref="", metadata=None):
            captured["event"] = {
                "mission_id": mission_id,
                "kind": kind,
                "summary": summary,
                "actor": actor,
                "source_ref": source_ref,
                "metadata": metadata,
            }
            done.set()
            return 1

        with isolated_dashboard_runtime(missions=[], memories=[memory]), patch.object(
            dashboard.db, "get_memory", new=lambda mission_id: memory if mission_id == memory.mission_id else None
        ), patch.object(
            dashboard.iterm_client, "spawn_tab", new=fake_spawn_tab
        ), patch.object(
            dashboard.iterm_client, "send_text_to_tabs", new=fake_send_text_to_tabs
        ), patch.object(
            dashboard.db, "upsert", new=fake_upsert
        ), patch.object(
            dashboard.db, "upsert_memory", new=fake_upsert_memory
        ), patch.object(
            dashboard.db, "add_event", new=fake_add_event
        ), patch.object(
            dashboard.ledger_mod, "log_action", new=lambda *args, **kwargs: captured.setdefault("ledger", (args, kwargs))
        ), patch.object(
            dashboard.ctx_mod, "write_context_file", new=lambda: None
        ), patch.object(
            dashboard.ctx_mod, "write_context_json", new=lambda: None
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                app._refresh_table()
                await pilot.pause()
                table = app.query_one(dashboard.MissionsTable)
                self.assertEqual(table.selected_ref().role, "closed")
                await pilot.press("r")
                await asyncio.wait_for(done.wait(), timeout=1)

        self.assertEqual(captured["spawn"]["goal"], "Closed Codex mission")
        self.assertEqual(
            captured["spawn"]["command"],
            f"cd /tmp/work && codex --yolo resume {resume_id}",
        )
        self.assertIn("Resume this Morpheus mission", captured["send_text"]["text"])
        self.assertTrue(captured["send_text"]["text"].endswith("\r"))
        self.assertEqual(captured["send_text"]["tab_ids"], ["tab-new"])
        self.assertEqual(captured["mission"].mission_id, "m_closed")
        self.assertEqual(captured["mission"].tab_id, "tab-new")
        self.assertIsNone(captured["memory"].archived_at)
        self.assertEqual(captured["memory"].phase, "working")
        self.assertEqual(captured["event"]["kind"], "resume")
        self.assertEqual(captured["event"]["metadata"]["agent_kind"], "codex")

    def test_post_spawn_resume_text_submits_gemini_resume_and_prompt(self) -> None:
        memory = dashboard.db.MissionMemory(
            mission_id="m_closed",
            title="Closed Gemini mission",
            next_step="answer follow-up",
            agent_kind="gemini",
            resume_ref="gemini-session",
        )

        text = dashboard._post_spawn_resume_text(memory)

        self.assertIn("/chat resume gemini-session\r", text)
        self.assertIn("Resume this Morpheus mission", text)
        self.assertTrue(text.endswith("\r"))

    async def test_new_session_submit_spawns_tab_and_records_mission(self) -> None:
        app = DashboardHarness()
        done = asyncio.Event()
        captured = {}
        project = dashboard.db.ProjectTenant(
            tenant_id="p_test",
            name="test",
            root_path="/tmp/test-project",
        )

        async def fake_spawn_tab(connection, *, command, goal):
            captured["spawn"] = (connection, command, goal)
            return SimpleNamespace(tab_id="tab-123456", session_id="session-123456")

        def fake_upsert(mission):
            captured["mission"] = mission
            done.set()

        with isolated_dashboard_runtime(), patch.object(
            dashboard.iterm_client, "spawn_tab", new=fake_spawn_tab
        ), patch.object(dashboard.db, "upsert", new=fake_upsert), patch.object(
            dashboard.tenant_mod, "ensure_project_tenant", new=lambda path=None: project
        ):
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
        self.assertEqual(command, "cd /tmp/test-project && codex")
        self.assertEqual(goal, "review a PR")
        self.assertEqual(captured["mission"].tab_id, "tab-123456")
        self.assertEqual(captured["mission"].session_id, "session-123456")
        self.assertEqual(captured["mission"].goal, "review a PR")
        self.assertEqual(captured["mission"].cmd, "cd /tmp/test-project && codex")
        self.assertEqual(captured["mission"].tenant_id, "p_test")
        self.assertEqual(captured["mission"].project_root, "/tmp/test-project")

    async def test_new_session_result_refreshes_visible_missions_immediately(self) -> None:
        app = DashboardHarness()
        app.iterm_conn = object()
        missions = []
        project = dashboard.db.ProjectTenant(
            tenant_id="p_test",
            name="test",
            root_path="/tmp/test-project",
        )

        async def fake_spawn_tab(connection, *, command, goal):
            return SimpleNamespace(tab_id="tab-123456", session_id="session-123456")

        def fake_upsert(mission):
            missions.append(mission)

        with (
            patch.object(dashboard.iterm_client, "spawn_tab", new=fake_spawn_tab),
            patch.object(dashboard.db, "upsert", new=fake_upsert),
            patch.object(dashboard.db, "all_missions", new=lambda: list(missions)),
            patch.object(dashboard.tenant_mod, "ensure_project_tenant", new=lambda path=None: project),
            patch.object(app, "_refresh_table") as refresh_table,
        ):
            await app._handle_new_session_result(NewSessionRequest(
                goal="review a PR",
                command="codex",
            ))

        self.assertEqual([m.tab_id for m in app.current_missions], ["tab-123456"])
        self.assertEqual(app.last_seen_tabs, {"tab-123456"})
        self.assertIn("new session [review a PR]", app.alerts[0].text)
        refresh_table.assert_called_once()

    async def test_new_session_with_prd_creates_run_and_coordinator(self) -> None:
        app = DashboardHarness()
        app.iterm_conn = object()
        done = asyncio.Event()
        captured = {}
        app.project = dashboard.db.ProjectTenant(
            tenant_id="p_parent",
            name="bkeyID",
            root_path="/tmp/bkeyID",
        )
        app.tenant_id = app.project.tenant_id
        project = dashboard.db.ProjectTenant(
            tenant_id="p_nested",
            name="bkey-devkit",
            root_path="/tmp/bkeyID/bkey-devkit",
        )
        run = SimpleNamespace(
            parent_id="m_parent",
            title="PRD Runs",
            prd_path=Path("/tmp/bkeyID/bkey-devkit/PRD.md"),
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

        def fake_create_prd_run(path, title=None, *, project=None):
            captured["prd_run"] = (path, title, project)
            return run

        with patch.object(
            dashboard.prd_runs, "create_prd_run", new=fake_create_prd_run
        ), patch.object(
            dashboard.prd_runs, "coordinator_command", new=lambda cmd, run: f"{cmd} --coordinator"
        ), patch.object(
            dashboard.prd_runs, "attach_coordinator", new=fake_attach
        ), patch.object(
            dashboard.iterm_client, "spawn_tab", new=fake_spawn_tab
        ), patch.object(
            dashboard.tenant_mod, "ensure_project_tenant", new=lambda path=None: project
        ), patch.object(dashboard.db, "upsert", new=fake_upsert):
            await app._handle_new_session_result(
                NewSessionRequest(goal="", command="codex", prd_path="/tmp/bkeyID/bkey-devkit/PRD.md")
            )
            await asyncio.wait_for(done.wait(), timeout=1)

        connection, command, goal = captured["spawn"]
        self.assertIs(connection, app.iterm_conn)
        self.assertEqual(command, "cd /tmp/bkeyID && codex --coordinator")
        self.assertEqual(goal, "PRD Runs coordinator")
        self.assertEqual(captured["mission"].goal, "PRD Runs coordinator")
        self.assertEqual(captured["mission"].cmd, "cd /tmp/bkeyID && codex --coordinator")
        self.assertEqual(captured["mission"].tenant_id, "p_parent")
        self.assertEqual(captured["mission"].project_root, "/tmp/bkeyID")
        self.assertEqual(
            captured["prd_run"],
            ("/tmp/bkeyID/bkey-devkit/PRD.md", None, app.project),
        )
        self.assertEqual(captured["attach"], (run, captured["mission"]))

    async def test_worker_result_spawns_child_under_prd_parent(self) -> None:
        app = DashboardHarness()
        app.iterm_conn = object()
        captured = {}
        project = dashboard.db.ProjectTenant(
            tenant_id="p_parent",
            name="bkeyID",
            root_path="/tmp/bkeyID",
        )
        run = SimpleNamespace(
            parent_id="m_parent",
            title="PRD Runs",
            prd_path=Path("/tmp/bkeyID/bkey-devkit/PRD.md"),
            status_path="/tmp/status.md",
            prompt_path="/tmp/prompt.md",
            tenant_id="p_parent",
            project_root="/tmp/bkeyID",
        )

        async def fake_spawn_tab(connection, *, command, goal):
            captured["spawn"] = (connection, command, goal)
            return SimpleNamespace(tab_id="tab-worker", session_id="session-worker")

        def fake_upsert(mission):
            mission.mission_id = "m_worker"
            captured["mission"] = mission

        def fake_attach(created_run, mission, *, scope="", verification=""):
            captured["attach"] = (created_run, mission, scope, verification)

        with patch.object(
            dashboard.prd_runs, "run_from_parent", new=lambda parent_id: run
        ), patch.object(
            dashboard.prd_runs, "worker_command", new=lambda cmd, run, worker_goal, scope="", verification="": f"{cmd} --worker"
        ), patch.object(
            dashboard.prd_runs, "attach_worker", new=fake_attach
        ), patch.object(
            dashboard.iterm_client, "spawn_tab", new=fake_spawn_tab
        ), patch.object(
            dashboard.db, "get_project_tenant", new=lambda tenant_id: project if tenant_id == "p_parent" else None
        ), patch.object(
            dashboard.tenant_mod,
            "ensure_project_tenant",
            side_effect=AssertionError("worker spawn should use the PRD run owner tenant"),
        ), patch.object(
            dashboard.db, "upsert", new=fake_upsert
        ), patch.object(
            dashboard.ledger_mod, "log_action", new=lambda *args, **kwargs: 1
        ), patch.object(
            dashboard.ctx_mod, "write_context_file", new=lambda: None
        ), patch.object(
            dashboard.ctx_mod, "write_context_json", new=lambda: None
        ):
            await app._handle_worker_result(WorkerRequest(
                parent_id="m_parent",
                goal="implement worker tree",
                command="codex",
                scope="morpheus/dashboard.py",
                verification="pytest tests/test_dashboard.py",
            ))

        connection, command, goal = captured["spawn"]
        self.assertIs(connection, app.iterm_conn)
        self.assertEqual(command, "cd /tmp/bkeyID && codex --worker")
        self.assertEqual(goal, "implement worker tree")
        self.assertEqual(captured["mission"].mission_id, "m_worker")
        self.assertEqual(captured["mission"].tenant_id, "p_parent")
        self.assertEqual(captured["mission"].project_root, "/tmp/bkeyID")
        self.assertEqual(captured["attach"][0], run)
        self.assertEqual(captured["attach"][2], "morpheus/dashboard.py")
        self.assertEqual(captured["attach"][3], "pytest tests/test_dashboard.py")

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

    async def test_manage_loops_action_opens_manager_with_history(self) -> None:
        app = DashboardHarness()
        loop = dashboard.db.PromptLoop(
            id=7,
            name="market scan",
            prompt="summarize catalysts",
            interval_seconds=900,
            command="codex exec",
            target_mission_id="m_20260520000102_abcd1234",
            target_tab_id="tab-123456",
            last_summary="WMT disciplined zone",
            next_run_at=0,
        )
        run = dashboard.db.PromptLoopRun(
            id=3,
            loop_id=loop.id,
            started_at=1,
            finished_at=2,
            status="success",
            exit_code=0,
            output_path="/tmp/loop.txt",
            summary="WMT disciplined zone",
            target_mission_id=loop.target_mission_id,
            target_tab_id=loop.target_tab_id,
        )

        with isolated_dashboard_runtime(), patch.object(
            dashboard.db, "all_loops", new=lambda include_paused=True: [loop]
        ), patch.object(
            dashboard.db, "loop_runs", new=lambda loop_id, limit=5: [run]
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                app.action_manage_loops()
                await pilot.pause()

                self.assertIsInstance(app.screen, LoopManagerScreen)
                detail = app.screen._detail(loop)
                self.assertIn("market scan", str(detail))
                self.assertIn("WMT disciplined zone", str(detail))

    async def test_loop_manager_join_action_updates_target_and_records_event(self) -> None:
        app = DashboardHarness()
        captured = {}
        loop = dashboard.db.PromptLoop(
            id=7,
            name="market scan",
            prompt="summarize catalysts",
            interval_seconds=900,
            command="codex exec",
        )
        updated = dashboard.db.PromptLoop(
            id=7,
            name="market scan",
            prompt="summarize catalysts",
            interval_seconds=900,
            command="codex exec",
            target_mission_id="m_target",
            target_tab_id="tab-target",
        )

        def fake_set_loop_target(loop_id, *, target_mission_id="", target_tab_id=None):
            captured["target"] = (loop_id, target_mission_id, target_tab_id)
            return updated

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

        with patch.object(
            dashboard.db, "get_loop", new=lambda loop_id: loop if loop_id == loop.id else None
        ), patch.object(
            dashboard.db, "set_loop_target", new=fake_set_loop_target
        ), patch.object(
            dashboard.db, "add_event", new=fake_add_event
        ), patch.object(
            dashboard.ledger_mod, "log_action", new=lambda action, tab_id, details: captured.setdefault("ledger", (action, tab_id, details))
        ), patch.object(
            dashboard.ctx_mod, "write_context_file", new=lambda: None
        ), patch.object(
            dashboard.ctx_mod, "write_context_json", new=lambda: None
        ):
            app._handle_loop_action_result(LoopActionRequest(
                action="join",
                loop_id=loop.id,
                target_mission_id="m_target",
                target_tab_id="tab-target",
            ))

        self.assertEqual(captured["target"], (7, "m_target", "tab-target"))
        self.assertEqual(captured["event"]["kind"], "loop_joined")
        self.assertEqual(captured["event"]["actor"], "morpheus")
        self.assertEqual(captured["ledger"][0], "loop_join")
        self.assertIn("joined loop", app.alerts[0].text)

    async def test_space_toggles_mission_card_details(self) -> None:
        app = DashboardHarness()

        with isolated_dashboard_runtime():
            async with app.run_test(size=(120, 40)) as pilot:
                card = app.query_one(MissionCardWidget)
                self.assertFalse(card.details_expanded)

                await pilot.press("space")
                await pilot.pause()

                self.assertTrue(card.details_expanded)


if __name__ == "__main__":
    unittest.main()
