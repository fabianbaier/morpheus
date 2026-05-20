import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from morpheus import cli, db, recall_eval


class RecallEvalLogicTest(unittest.TestCase):
    def test_stale_mission_with_required_recall_fields_passes(self) -> None:
        now = 1_000_000.0
        memory = db.MissionMemory(
            mission_id="m_recall",
            title="Recall eval",
            why="dogfood stale mission recall",
            done_definition="user can recover next action from the brief",
            acceptance_criteria="- why visible\n- next step visible",
            next_step="run the focused recall eval test",
            last_decision="score graph data before adding UI polish",
            source_kind="prd",
            source_ref="PRD.md",
            updated_at=now - recall_eval.DEFAULT_STALE_SECONDS - 60,
        )
        events = [
            db.MissionEvent(
                id=1,
                mission_id=memory.mission_id,
                ts=now - 30,
                kind="check",
                actor="codex",
                summary="focused recall eval test passed",
                source_ref="python -m unittest tests.test_recall_eval",
            )
        ]
        artifacts = [
            db.MissionArtifact(
                id=2,
                mission_id=memory.mission_id,
                kind="test",
                path_or_url="tests/test_recall_eval.py",
                status="pass",
                summary="recall eval coverage",
                created_at=now - 20,
            )
        ]

        result = recall_eval.evaluate_mission(
            memory,
            events=events,
            artifacts=artifacts,
            now=now,
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.score, 100)
        self.assertEqual(result.missing_labels, [])

    def test_missing_recall_fields_fail_with_actionable_labels(self) -> None:
        now = 1_000_000.0
        memory = db.MissionMemory(
            mission_id="m_sparse",
            title="Sparse mission",
            updated_at=now - recall_eval.DEFAULT_STALE_SECONDS - 60,
        )

        result = recall_eval.evaluate_mission(memory, now=now)

        self.assertFalse(result.passed)
        self.assertIn("why", result.missing_labels)
        self.assertIn("done definition", result.missing_labels)
        self.assertIn("acceptance criteria", result.missing_labels)
        self.assertIn("next step", result.missing_labels)
        self.assertIn("recent decision", result.missing_labels)
        self.assertIn("recent check", result.missing_labels)
        self.assertIn("proof artifact", result.missing_labels)
        self.assertNotIn("stale age", result.missing_labels)

    def test_recent_live_activity_fails_stale_age_even_when_context_is_complete(self) -> None:
        now = 1_000_000.0
        memory = db.MissionMemory(
            mission_id="m_fresh",
            title="Fresh mission",
            why="keep fresh sessions out of the 48-hour dogfood gate",
            done_definition="fresh mission fails stale age",
            acceptance_criteria="age must be at least 48 hours",
            next_step="wait until the mission is stale",
            last_decision="use live buffer activity as the freshest age source",
            updated_at=now - recall_eval.DEFAULT_STALE_SECONDS - 60,
        )
        live = [
            db.Mission(
                tab_id="tab-fresh",
                mission_id=memory.mission_id,
                buffer_changed_at=now - 60,
            )
        ]
        events = [
            db.MissionEvent(
                id=1,
                mission_id=memory.mission_id,
                ts=now - 30,
                kind="check",
                actor="codex",
                summary="context check passed",
            )
        ]
        artifacts = [
            db.MissionArtifact(
                id=1,
                mission_id=memory.mission_id,
                kind="proof",
                path_or_url="proof.md",
                status="pass",
                summary="context proof",
                created_at=now - 20,
            )
        ]

        result = recall_eval.evaluate_mission(
            memory,
            live=live,
            events=events,
            artifacts=artifacts,
            now=now,
        )

        self.assertFalse(result.passed)
        self.assertEqual(result.missing_labels, ["stale age"])
        self.assertEqual(result.age_source, "live buffer")

    def test_failed_check_and_artifact_do_not_pass_recall_eval(self) -> None:
        now = 1_000_000.0
        memory = db.MissionMemory(
            mission_id="m_failed_proof",
            title="Failed proof",
            why="avoid false confidence",
            done_definition="failed tests cannot satisfy recall proof",
            acceptance_criteria="failures must be visible",
            next_step="fix failing tests",
            last_decision="require pass-like verification",
            updated_at=now - recall_eval.DEFAULT_STALE_SECONDS - 60,
        )
        events = [
            db.MissionEvent(
                id=1,
                mission_id=memory.mission_id,
                ts=now,
                kind="check",
                actor="codex",
                summary="tests failed",
                metadata={"status": "fail"},
            )
        ]
        artifacts = [
            db.MissionArtifact(
                id=1,
                mission_id=memory.mission_id,
                kind="test",
                path_or_url="tests/test_recall_eval.py",
                status="fail",
                summary="failing tests",
                created_at=now,
            )
        ]

        result = recall_eval.evaluate_mission(
            memory,
            events=events,
            artifacts=artifacts,
            now=now,
        )

        self.assertFalse(result.passed)
        self.assertIn("recent check", result.missing_labels)
        self.assertIn("proof artifact", result.missing_labels)

    def test_negated_success_words_do_not_pass_check_events(self) -> None:
        for summary in (
            "not verified",
            "not ok",
            "did not succeed",
            "not successful",
            "unverified",
            "10 passed, 2 errors",
            "tests passed with 2 failures",
        ):
            with self.subTest(summary=summary):
                event = db.MissionEvent(
                    id=1,
                    mission_id="m_negated",
                    ts=1,
                    kind="check",
                    actor="codex",
                    summary=summary,
                )
                self.assertFalse(recall_eval._event_passed(event))

        for summary in (
            "verified",
            "ok",
            "tests passed",
            "build succeeded",
            "10 passed, 0 errors",
            "tests passed with no failures",
            "tests passed with no failures or errors",
            "Tests: 0 failed, 12 passed",
            "no tests failed",
            "error handling tests passed",
            "error-handling tests passed",
        ):
            with self.subTest(summary=summary):
                event = db.MissionEvent(
                    id=1,
                    mission_id="m_positive",
                    ts=1,
                    kind="check",
                    actor="codex",
                    summary=summary,
                )
                self.assertTrue(recall_eval._event_passed(event))

    def test_newer_failed_check_and_artifact_override_older_passes(self) -> None:
        now = 1_000_000.0
        memory = db.MissionMemory(
            mission_id="m_newer_failure",
            title="Newer failure",
            why="latest proof controls readiness",
            done_definition="new failures must block recall readiness",
            acceptance_criteria="latest check and proof must pass",
            next_step="fix the newer failure",
            last_decision="require latest relevant status",
            updated_at=now - recall_eval.DEFAULT_STALE_SECONDS - 60,
        )
        events = [
            db.MissionEvent(
                id=1,
                mission_id=memory.mission_id,
                ts=now - 20,
                kind="check",
                actor="codex",
                summary="tests passed",
            ),
            db.MissionEvent(
                id=2,
                mission_id=memory.mission_id,
                ts=now - 10,
                kind="check",
                actor="codex",
                summary="tests did not pass",
            ),
        ]
        artifacts = [
            db.MissionArtifact(
                id=1,
                mission_id=memory.mission_id,
                kind="test",
                path_or_url="old.log",
                status="pass",
                summary="old passing proof",
                created_at=now - 20,
            ),
            db.MissionArtifact(
                id=2,
                mission_id=memory.mission_id,
                kind="test",
                path_or_url="new.log",
                status="fail",
                summary="new failing proof",
                created_at=now - 10,
            ),
        ]

        result = recall_eval.evaluate_mission(
            memory,
            events=events,
            artifacts=artifacts,
            now=now,
        )

        self.assertFalse(result.passed)
        self.assertIn("recent check", result.missing_labels)
        self.assertIn("proof artifact", result.missing_labels)

    def test_tighter_than_supported_target_fails_proxy_gate(self) -> None:
        now = 1_000_000.0
        memory = db.MissionMemory(
            mission_id="m_fast_target",
            title="Fast target",
            why="document proxy limits",
            done_definition="unsupported target cannot pass",
            acceptance_criteria="target must be supported by deterministic proxy",
            next_step="use the default 10 second target",
            last_decision="target is a required check",
            updated_at=now - recall_eval.DEFAULT_STALE_SECONDS - 60,
        )
        events = [
            db.MissionEvent(
                id=1,
                mission_id=memory.mission_id,
                ts=now,
                kind="check",
                actor="codex",
                summary="tests passed",
            )
        ]
        artifacts = [
            db.MissionArtifact(
                id=1,
                mission_id=memory.mission_id,
                kind="test",
                path_or_url="tests/test_recall_eval.py",
                status="pass",
                summary="passing tests",
                created_at=now,
            )
        ]

        result = recall_eval.evaluate_mission(
            memory,
            events=events,
            artifacts=artifacts,
            now=now,
            target_seconds=0.001,
        )

        self.assertFalse(result.passed)
        self.assertIn("target seconds", result.missing_labels)


class RecallEvalCliTest(unittest.TestCase):
    def test_graph_recall_eval_outputs_status_and_can_record_event(self) -> None:
        runner = CliRunner()
        project = db.ProjectTenant("p_cli", "cli", "/tmp/cli")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(db, "DB_DIR", root),
                patch.object(db, "DB_PATH", root / "morpheus.db"),
                patch.object(cli.tenant_mod, "backfill_known_tenants", new=lambda: 0),
                patch.object(cli.tenant_mod, "ensure_project_tenant", new=lambda path=None: project),
            ):
                mission = db.Mission(
                    tab_id="tab-recall",
                    mission_id="m_cli_recall",
                    tenant_id=project.tenant_id,
                    goal="CLI recall eval",
                    state="idle",
                    buffer_changed_at=time.time() - recall_eval.DEFAULT_STALE_SECONDS - 60,
                    cmd="codex",
                )
                db.upsert(mission)
                db.upsert_memory(
                    db.MissionMemory(
                        mission_id=mission.mission_id,
                        tenant_id=project.tenant_id,
                        title="CLI recall eval",
                        why="prove the command can score stale recall data",
                        done_definition="CLI prints PASS and records an event on request",
                        acceptance_criteria="- PASS visible\n- recall_eval event written",
                        next_step="commit the scoped implementation",
                        last_decision="keep recall eval under graph commands",
                        source_kind="test",
                        source_ref="tests/test_recall_eval.py",
                    )
                )
                db.add_event(
                    mission.mission_id,
                    kind="check",
                    actor="codex",
                    summary="CLI recall eval test passed",
                    source_ref="python -m unittest tests.test_recall_eval",
                )
                db.add_artifact(
                    mission.mission_id,
                    kind="test",
                    path_or_url="tests/test_recall_eval.py",
                    status="pass",
                    summary="CLI recall eval coverage",
                )

                result = runner.invoke(
                    cli.app,
                    ["graph", "recall-eval", mission.mission_id, "--record-event"],
                )
                events = db.recent_events(mission.mission_id, limit=10)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("PASS", result.output)
        self.assertTrue(
            any(event.kind == "recall_eval" and "PASS" in event.summary for event in events)
        )

    def test_graph_recall_eval_json_reports_missing_fields(self) -> None:
        runner = CliRunner()
        project = db.ProjectTenant("p_cli", "cli", "/tmp/cli")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(db, "DB_DIR", root),
                patch.object(db, "DB_PATH", root / "morpheus.db"),
                patch.object(cli.tenant_mod, "backfill_known_tenants", new=lambda: 0),
                patch.object(cli.tenant_mod, "ensure_project_tenant", new=lambda path=None: project),
            ):
                mission = db.Mission(
                    tab_id="tab-sparse",
                    mission_id="m_cli_sparse",
                    tenant_id=project.tenant_id,
                    goal="Sparse recall eval",
                    state="idle",
                    buffer_changed_at=time.time() - recall_eval.DEFAULT_STALE_SECONDS - 60,
                )
                db.upsert(mission)

                result = runner.invoke(
                    cli.app,
                    ["graph", "recall-eval", mission.mission_id, "--json"],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload[0]["status"], "FAIL")
        self.assertIn("why", payload[0]["missing"])

    def test_graph_recall_eval_defaults_to_cwd_project_scope(self) -> None:
        runner = CliRunner()
        now = time.time()
        current_project = db.ProjectTenant(
            tenant_id="p_current",
            name="current",
            root_path="/tmp/current",
        )

        def add_ready_mission(mission_id: str, tab_id: str, tenant_id: str) -> None:
            db.upsert(
                db.Mission(
                    tab_id=tab_id,
                    mission_id=mission_id,
                    tenant_id=tenant_id,
                    goal=mission_id,
                    state="idle",
                    buffer_changed_at=now - recall_eval.DEFAULT_STALE_SECONDS - 60,
                )
            )
            db.upsert_memory(
                db.MissionMemory(
                    mission_id=mission_id,
                    tenant_id=tenant_id,
                    title=mission_id,
                    why="prove project scoped recall eval",
                    done_definition="only current project is evaluated by default",
                    acceptance_criteria="--all includes every project",
                    next_step="keep project boundaries intact",
                    last_decision="default no-ref recall eval to cwd project",
                    updated_at=now - recall_eval.DEFAULT_STALE_SECONDS - 60,
                )
            )
            db.add_event(mission_id, kind="check", actor="codex", summary="tests passed")
            db.add_artifact(
                mission_id,
                kind="test",
                path_or_url=f"{mission_id}.log",
                status="pass",
                summary="passing project-scoped proof",
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(db, "DB_DIR", root),
                patch.object(db, "DB_PATH", root / "morpheus.db"),
                patch.object(cli.tenant_mod, "backfill_known_tenants", new=lambda: 0),
                patch.object(cli.tenant_mod, "ensure_project_tenant", new=lambda path=None: current_project),
            ):
                add_ready_mission("m_current", "tab-current", "p_current")
                add_ready_mission("m_other", "tab-other", "p_other")

                scoped = runner.invoke(cli.app, ["graph", "recall-eval", "--json"])
                global_result = runner.invoke(cli.app, ["graph", "recall-eval", "--all", "--json"])
                scoped_ref = runner.invoke(cli.app, ["graph", "recall-eval", "m_other", "--json"])
                global_ref = runner.invoke(cli.app, ["graph", "recall-eval", "--all", "m_other", "--json"])

        self.assertEqual(scoped.exit_code, 0, scoped.output)
        scoped_payload = json.loads(scoped.output)
        self.assertEqual([item["mission_id"] for item in scoped_payload], ["m_current"])

        self.assertEqual(global_result.exit_code, 0, global_result.output)
        global_ids = {item["mission_id"] for item in json.loads(global_result.output)}
        self.assertEqual(global_ids, {"m_current", "m_other"})
        self.assertNotEqual(scoped_ref.exit_code, 0)
        self.assertIn("no mission matching", scoped_ref.output)
        self.assertEqual(global_ref.exit_code, 0, global_ref.output)
        self.assertEqual(json.loads(global_ref.output)[0]["mission_id"], "m_other")

    def test_graph_recall_eval_rejects_ambiguous_prefix(self) -> None:
        runner = CliRunner()
        project = db.ProjectTenant("p_cli", "cli", "/tmp/cli")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(db, "DB_DIR", root),
                patch.object(db, "DB_PATH", root / "morpheus.db"),
                patch.object(cli.tenant_mod, "backfill_known_tenants", new=lambda: 0),
                patch.object(cli.tenant_mod, "ensure_project_tenant", new=lambda path=None: project),
            ):
                for mission_id, tab_id in (
                    ("m_shared_alpha", "tab-alpha"),
                    ("m_shared_beta", "tab-beta"),
                ):
                    db.upsert(
                        db.Mission(
                            tab_id=tab_id,
                            mission_id=mission_id,
                            tenant_id=project.tenant_id,
                            goal=mission_id,
                        )
                    )
                    db.upsert_memory(
                        db.MissionMemory(
                            mission_id=mission_id,
                            tenant_id=project.tenant_id,
                            title=mission_id,
                        )
                    )

                result = runner.invoke(cli.app, ["graph", "recall-eval", "m_shared"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("ambiguous mission ref", result.output)


if __name__ == "__main__":
    unittest.main()
