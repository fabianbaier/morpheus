import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from morpheus import db, goals, prd_runs


@contextmanager
def isolated_goal_runtime():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db_dir = root / "db"
        runs_dir = root / "runs"
        goals_dir = root / "goals"
        with patch.object(db, "DB_DIR", db_dir), patch.object(
            db, "DB_PATH", db_dir / "morpheus.db"
        ), patch.object(prd_runs, "RUNS_DIR", runs_dir), patch.object(
            goals, "GOALS_DIR", goals_dir
        ):
            yield root


class GoalRunsTest(unittest.TestCase):
    def test_create_goal_run_from_prd_writes_prompt_status_and_edges(self) -> None:
        with isolated_goal_runtime() as root:
            prd = root / "PRD.md"
            prd.write_text("# Autonomous PRD\n\n- ship it\n", encoding="utf-8")

            bundle = goals.create_goal_run(
                prd,
                objective="Implement the PRD fully",
                max_turns=7,
                max_workers=2,
            )

            stored = db.get_goal_run(bundle.goal.goal_id)
            parent = db.get_memory(bundle.goal.parent_mission_id)
            edges = db.edges_from_id(bundle.goal.parent_mission_id, limit=10)
            status = bundle.status_path.read_text(encoding="utf-8")
            prompt = bundle.prompt_path.read_text(encoding="utf-8")

        self.assertIsNotNone(stored)
        self.assertIsNotNone(parent)
        self.assertEqual(stored.max_turns, 7)
        self.assertEqual(stored.max_workers, 2)
        self.assertEqual(parent.source_kind, "prd")
        self.assertIn("Implement the PRD fully", status)
        self.assertIn("Safety Rules", prompt)
        self.assertTrue(any(edge.relation == "goal_run" and edge.to_id == stored.goal_id for edge in edges))

    def test_attach_controller_links_live_controller_and_refreshes_status(self) -> None:
        with isolated_goal_runtime() as root:
            prd = root / "PRD.md"
            prd.write_text("# Autonomous PRD\n", encoding="utf-8")
            bundle = goals.create_goal_run(prd)
            controller = db.Mission(
                tab_id="tab-controller",
                mission_id="m_controller",
                goal="autonomous prd goal controller",
                state="working",
                cmd="codex",
            )
            db.upsert(controller)

            attached = goals.attach_controller(bundle, controller)
            edges = db.edges_from_id(bundle.goal.parent_mission_id, limit=10)
            status = goals.bundle_for_goal(attached.goal_id).status_path.read_text(encoding="utf-8")

        self.assertEqual(attached.controller_mission_id, "m_controller")
        self.assertTrue(any(edge.relation == "goal_controller" and edge.to_id == "m_controller" for edge in edges))
        self.assertIn("autonomous prd goal controller", status)
        self.assertIn("live tab `tab-controller`", status)

    def test_goal_status_controls_preserve_history_and_reset_resume_turns(self) -> None:
        with isolated_goal_runtime() as root:
            prd = root / "PRD.md"
            prd.write_text("# Autonomous PRD\n", encoding="utf-8")
            bundle = goals.create_goal_run(prd)
            run = bundle.goal
            run.turns_used = 5
            db.upsert_goal_run(run)

            paused = goals.set_status(run.goal_id, "paused", reason="need user input")
            resumed = goals.set_status(run.goal_id, "active", reason="continue", reset_turns=True)
            cleared = goals.set_status(run.goal_id, "cleared", reason="done testing")
            events = db.recent_events(run.parent_mission_id, limit=10)
            status = cleared.status_path.read_text(encoding="utf-8")

        self.assertEqual(paused.goal.status, "paused")
        self.assertEqual(resumed.goal.status, "active")
        self.assertEqual(resumed.goal.turns_used, 0)
        self.assertEqual(cleared.goal.status, "cleared")
        self.assertTrue(any(event.kind == "goal_paused" for event in events))
        self.assertTrue(any(event.kind == "goal_cleared" for event in events))
        self.assertIn("done testing", status)

    def test_resolve_goal_from_parent_or_controller_mission(self) -> None:
        with isolated_goal_runtime() as root:
            prd = root / "PRD.md"
            prd.write_text("# Autonomous PRD\n", encoding="utf-8")
            bundle = goals.create_goal_run(prd)
            controller = db.Mission(
                tab_id="tab-controller",
                mission_id="m_controller",
                goal="autonomous prd goal controller",
                state="working",
                cmd="codex",
            )
            db.upsert(controller)
            goals.attach_controller(bundle, controller)

            by_parent = goals.resolve_goal(bundle.goal.parent_mission_id)
            by_controller = goals.resolve_goal("m_controller")

        self.assertIsNotNone(by_parent)
        self.assertIsNotNone(by_controller)
        self.assertEqual(by_parent.goal_id, bundle.goal.goal_id)
        self.assertEqual(by_controller.goal_id, bundle.goal.goal_id)

    def test_goal_task_attach_and_completion_rolls_up_active_workers(self) -> None:
        with isolated_goal_runtime() as root:
            prd = root / "PRD.md"
            prd.write_text("# Autonomous PRD\n", encoding="utf-8")
            bundle = goals.create_goal_run(prd, max_workers=1)
            task = goals.create_task(
                bundle.goal.goal_id,
                title="implement status rollup",
                scope="morpheus/goals.py",
                verification="python -m unittest tests.test_goals",
                claimed_paths=["morpheus/goals.py"],
            )
            worker = db.Mission(
                tab_id="tab-worker",
                mission_id="m_worker",
                goal="status rollup worker",
                state="working",
                cmd="codex",
            )
            db.upsert(worker)

            attached = goals.attach_worker(task.task_id, worker)
            running_goal = db.get_goal_run(bundle.goal.goal_id)
            with self.assertRaises(ValueError):
                goals.create_task(
                    bundle.goal.goal_id,
                    title="conflicting task",
                    claimed_paths=["morpheus/goals.py"],
                )
            done = goals.set_task_status(task.task_id, "done", summary="rollup implemented and tested")
            finished_goal = db.get_goal_run(bundle.goal.goal_id)
            edges = db.edges_from_id(bundle.goal.parent_mission_id, limit=20)
            status = goals.bundle_for_goal(bundle.goal.goal_id).status_path.read_text(encoding="utf-8")

        self.assertEqual(attached.worker_mission_id, "m_worker")
        self.assertEqual(running_goal.active_workers, 1)
        self.assertEqual(done.status, "done")
        self.assertEqual(finished_goal.active_workers, 0)
        self.assertTrue(any(edge.relation == "goal_worker" and edge.to_id == "m_worker" for edge in edges))
        self.assertIn("rollup implemented and tested", status)
        self.assertIn("morpheus/goals.py", status)

    def test_controller_continuation_respects_cooldown_and_turn_budget(self) -> None:
        with isolated_goal_runtime() as root:
            prd = root / "PRD.md"
            prd.write_text("# Autonomous PRD\n", encoding="utf-8")
            bundle = goals.create_goal_run(prd, max_turns=1)
            controller = db.Mission(
                tab_id="tab-controller",
                mission_id="m_controller",
                goal="autonomous prd goal controller",
                state="idle",
                cmd="codex",
            )
            db.upsert(controller)
            goals.attach_controller(bundle, controller)

            due = goals.due_continuation_targets(cooldown_seconds=0, limit=5)
            reserved, outcome = goals.reserve_continuation(
                bundle.goal.goal_id,
                reason="test tick",
                cooldown_seconds=0,
            )
            text = goals.continuation_text(reserved, reason="test tick")
            exhausted, exhausted_outcome = goals.reserve_continuation(
                bundle.goal.goal_id,
                reason="budget check",
                cooldown_seconds=0,
            )
            events = db.recent_events(bundle.goal.parent_mission_id, limit=10)

        self.assertEqual([target.goal.goal_id for target in due], [bundle.goal.goal_id])
        self.assertEqual(outcome, "reserved")
        self.assertIn("morpheus goal task-add", text)
        self.assertIn("Turn budget: 1/1", text)
        self.assertEqual(exhausted_outcome, "budget_exhausted")
        self.assertEqual(exhausted.goal.status, "paused")
        self.assertTrue(any(event.kind == "goal_continue" for event in events))
        self.assertTrue(any(event.kind == "goal_budget_pause" for event in events))


if __name__ == "__main__":
    unittest.main()
