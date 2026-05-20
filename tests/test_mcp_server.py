import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from morpheus import db, ledger, mcp_server, prd_runs


@contextmanager
def isolated_mcp_runtime():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db_dir = root / "db"
        runs_dir = root / "runs"
        with patch.object(db, "DB_DIR", db_dir), patch.object(
            db, "DB_PATH", db_dir / "morpheus.db"
        ), patch.object(prd_runs, "RUNS_DIR", runs_dir), patch.object(
            mcp_server.ctx_mod, "write_context_file", new=lambda: None
        ), patch.object(
            mcp_server.ctx_mod, "write_context_json", new=lambda: None
        ):
            yield root


class MCPServerTest(unittest.TestCase):
    def test_list_and_get_mission_return_graph_memory_live_events_artifacts_and_edges(self) -> None:
        with isolated_mcp_runtime():
            mission = db.Mission(
                tab_id="tab-alpha",
                mission_id="m_alpha",
                goal="ship MCP graph tools",
                state="working",
                last_event="editing",
                cmd="codex",
            )
            db.upsert(mission)
            db.upsert_memory(db.MissionMemory(
                mission_id=mission.mission_id,
                title="MCP graph tools",
                why="agents need durable mission context",
                phase="editing",
                claimed_paths='["morpheus/mcp_server.py"]',
            ))
            db.add_event(mission.mission_id, kind="decision", actor="user", summary="keep spawn ask-first")
            db.add_artifact(
                mission.mission_id,
                kind="test",
                path_or_url="tests/test_mcp_server.py",
                status="pass",
                summary="MCP tests",
            )
            db.add_edge(mission.mission_id, "topic:mcp", "relates_to", "MCP work")

            listed = mcp_server.list_missions()
            detail = mcp_server.get_mission("tab-alpha", event_limit=5, artifact_limit=5, edge_limit=5)

        self.assertEqual(listed[0]["mission_id"], "m_alpha")
        self.assertEqual(listed[0]["live"][0]["tab_id"], "tab-alpha")
        self.assertEqual(listed[0]["claimed_paths"], ["morpheus/mcp_server.py"])
        self.assertTrue(detail["found"])
        self.assertEqual(detail["memory"]["title"], "MCP graph tools")
        self.assertEqual(detail["live"][0]["state"], "working")
        self.assertTrue(any(event["summary"] == "keep spawn ask-first" for event in detail["events"]))
        self.assertTrue(any(artifact["kind"] == "test" for artifact in detail["artifacts"]))
        self.assertTrue(any(edge["to_id"] == "topic:mcp" for edge in detail["edges"]))

    def test_update_mission_changes_memory_and_logs_action(self) -> None:
        with isolated_mcp_runtime():
            mission = db.Mission(tab_id="tab-alpha", mission_id="m_alpha", goal="old", state="idle")
            db.upsert(mission)

            result = mcp_server.update_mission(
                "m_alpha",
                title="Updated title",
                why="MCP clients can correct mission memory",
                phase="reviewing",
                confidence=0.9,
                claimed_paths_json='["morpheus/mcp_server.py", "tests/test_mcp_server.py"]',
            )
            memory = db.get_memory("m_alpha")
            events = db.recent_events("m_alpha", limit=5)
            actions = ledger.recent_actions(limit=5)

        self.assertTrue(result["ok"])
        self.assertEqual(memory.title, "Updated title")
        self.assertEqual(memory.phase, "reviewing")
        self.assertEqual(memory.confidence, 0.9)
        self.assertEqual(memory.claimed_paths, '["morpheus/mcp_server.py", "tests/test_mcp_server.py"]')
        self.assertEqual(events[0].kind, "mission_update")
        self.assertEqual(events[0].actor, "mcp")
        self.assertEqual(actions[0].action, "mcp_update_mission")
        self.assertEqual(actions[0].details["mission_id"], "m_alpha")

    def test_event_and_artifact_tools_refresh_prd_run_status(self) -> None:
        with isolated_mcp_runtime() as root:
            prd = root / "PRD.md"
            prd.write_text("# MCP PRD\n", encoding="utf-8")
            run = prd_runs.create_prd_run(prd, title="MCP PRD")
            worker = db.Mission(
                tab_id="tab-worker",
                mission_id="m_worker",
                goal="add MCP graph tools",
                state="working",
            )
            db.upsert(worker)
            prd_runs.attach_worker(run, worker, scope="morpheus/mcp_server.py")

            event = mcp_server.add_mission_event(
                "m_worker",
                "MCP graph event tool works",
                kind="check",
                source_ref="tests/test_mcp_server.py",
            )
            artifact = mcp_server.add_mission_artifact(
                "m_worker",
                "tests/test_mcp_server.py",
                kind="test",
                status="pass",
                summary="MCP graph tests",
            )
            status = run.status_path.read_text(encoding="utf-8")

        self.assertTrue(event["ok"])
        self.assertTrue(artifact["ok"])
        self.assertIn("check/mcp: MCP graph event tool works", status)
        self.assertIn("pass test: `tests/test_mcp_server.py` - MCP graph tests", status)

    def test_link_missions_creates_edge(self) -> None:
        with isolated_mcp_runtime():
            db.upsert(db.Mission(tab_id="tab-a", mission_id="m_a", goal="A"))
            db.upsert(db.Mission(tab_id="tab-b", mission_id="m_b", goal="B"))

            result = mcp_server.link_missions("m_a", "m_b", relation="blocks", reason="B waits on A")
            edges = db.edges_for_id("m_a", limit=5)

        self.assertTrue(result["ok"])
        self.assertEqual(result["relation"], "blocks")
        self.assertTrue(any(edge.to_id == "m_b" and edge.relation == "blocks" for edge in edges))


if __name__ == "__main__":
    unittest.main()
