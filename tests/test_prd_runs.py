import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from morpheus import db, prd_runs


@contextmanager
def isolated_prd_runtime():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db_dir = root / "db"
        runs_dir = root / "runs"
        with patch.object(db, "DB_DIR", db_dir), patch.object(
            db, "DB_PATH", db_dir / "morpheus.db"
        ), patch.object(prd_runs, "RUNS_DIR", runs_dir):
            yield root


class PRDRunsTest(unittest.TestCase):
    def test_find_prds_lists_all_markdown_with_prd_files_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir()
            (root / "PRD.md").write_text("# Main PRD\n", encoding="utf-8")
            (root / "docs" / "feature-spec.md").write_text("# Feature Spec\n", encoding="utf-8")
            (root / "docs" / "reference.md").write_text("# Reference\n", encoding="utf-8")
            (root / "notes.txt").write_text("not markdown\n", encoding="utf-8")
            (root / "README.md").write_text("# Readme\n", encoding="utf-8")
            (root / "notes.md").write_text("# Notes\n", encoding="utf-8")

            candidates = prd_runs.find_prds(root)

        self.assertEqual(
            [candidate.label for candidate in candidates],
            ["PRD.md", "docs/feature-spec.md", "notes.md", "README.md", "docs/reference.md"],
        )

    def test_find_prds_default_does_not_truncate_markdown_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(40):
                (root / f"note-{i:02}.md").write_text("# note\n", encoding="utf-8")

            candidates = prd_runs.find_prds(root, max_seconds=1)

        self.assertEqual(len(candidates), 40)

    def test_find_prds_refuses_home_sized_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            (home / "PRD.md").write_text("# Should not scan home\n", encoding="utf-8")

            with patch.object(prd_runs.Path, "home", return_value=home):
                candidates = prd_runs.find_prds(home)

        self.assertEqual(candidates, [])

    def test_scan_root_falls_back_from_home_to_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            home = base / "home"
            repo = base / "repo"
            home.mkdir()
            repo.mkdir()
            (repo / ".git").mkdir()

            with patch.object(prd_runs.Path, "home", return_value=home):
                scan_root = prd_runs.scan_root_for_worktree(home, fallback=repo / "subdir")

        self.assertEqual(scan_root, repo)

    def test_find_prds_respects_entry_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(20):
                (root / f"file-{i}.md").write_text("# note\n", encoding="utf-8")
            (root / "PRD.md").write_text("# Too late\n", encoding="utf-8")

            candidates = prd_runs.find_prds(root, max_entries=5, max_seconds=1)

        self.assertLessEqual(len(candidates), 5)
        self.assertNotIn("PRD.md", [candidate.label for candidate in candidates])

    def test_title_from_prd_uses_first_heading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "PRD.md"
            path.write_text("\n# PRD Runs\nbody\n", encoding="utf-8")

            title = prd_runs.title_from_prd(path)

        self.assertEqual(title, "PRD Runs")

    def test_coordinator_command_points_to_prompt_and_prd(self) -> None:
        run = prd_runs.PRDRun(
            parent_id="m_20260520000102_abcd1234",
            title="PRD Runs",
            prd_path=Path("/tmp/PRD.md"),
            status_path=Path("/tmp/status.md"),
            prompt_path=Path("/tmp/coordinator_prompt.md"),
        )

        command = prd_runs.coordinator_command("codex", run)

        self.assertTrue(command.startswith("codex "))
        self.assertIn("coordinator_prompt.md", command)
        self.assertIn("PRD.md", command)

    def test_attach_worker_updates_status_file_from_graph(self) -> None:
        with isolated_prd_runtime() as root:
            prd = root / "PRD.md"
            prd.write_text("# Sync PRD\n", encoding="utf-8")
            run = prd_runs.create_prd_run(prd, title="Sync PRD")
            coordinator = db.Mission(
                tab_id="tab-coordinator",
                mission_id="m_coordinator",
                goal="sync coordinator",
                state="working",
                cmd="codex",
            )
            db.upsert(coordinator)
            prd_runs.attach_coordinator(run, coordinator)
            worker = db.Mission(
                tab_id="tab-worker",
                mission_id="m_worker",
                goal="implement status updater",
                state="working",
                cmd="codex",
            )
            db.upsert(worker)

            prd_runs.attach_worker(
                run,
                worker,
                scope="morpheus/prd_runs.py",
                verification="python -m unittest tests.test_prd_runs",
            )

            text = run.status_path.read_text(encoding="utf-8")

        self.assertIn("- mode: `graph-synced`", text)
        self.assertNotIn("coordinator-only", text)
        self.assertIn("coordinator: sync coordinator (`m_coordinator`) live tab `tab-coordinator`", text)
        self.assertIn("worker: implement status updater (`m_worker`) live tab `tab-worker`", text)
        self.assertIn("scope: morpheus/prd_runs.py", text)
        self.assertIn("verification: python -m unittest tests.test_prd_runs", text)

    def test_create_prd_run_can_belong_to_launching_project(self) -> None:
        with isolated_prd_runtime() as root:
            parent = root / "bkeyID"
            nested = parent / "bkey-devkit"
            nested.mkdir(parents=True)
            (parent / ".git").mkdir()
            (nested / ".git").mkdir()
            prd = nested / "PRD.md"
            prd.write_text("# Devkit PRD\n", encoding="utf-8")
            parent_project = prd_runs.tenant_mod.ensure_project_tenant(parent)
            nested_project = prd_runs.tenant_mod.ensure_project_tenant(prd)

            run = prd_runs.create_prd_run(prd, project=parent_project)
            memory = db.get_memory(run.parent_id)
            owner = prd_runs.project_for_run(run)

        self.assertNotEqual(parent_project.tenant_id, nested_project.tenant_id)
        self.assertIsNotNone(memory)
        self.assertEqual(run.tenant_id, parent_project.tenant_id)
        self.assertEqual(run.project_root, parent_project.root_path)
        self.assertEqual(memory.tenant_id, parent_project.tenant_id)
        self.assertEqual(memory.project_root, parent_project.root_path)
        self.assertEqual(memory.source_ref, str(prd.resolve()))
        self.assertEqual(owner.tenant_id, parent_project.tenant_id)

    def test_status_refresh_for_child_includes_events_and_artifacts(self) -> None:
        with isolated_prd_runtime() as root:
            prd = root / "PRD.md"
            prd.write_text("# Sync PRD\n", encoding="utf-8")
            run = prd_runs.create_prd_run(prd, title="Sync PRD")
            worker = db.Mission(
                tab_id="tab-worker",
                mission_id="m_worker",
                goal="prove status updater",
                state="working",
                cmd="codex",
            )
            db.upsert(worker)
            prd_runs.attach_worker(run, worker, scope="tests/test_prd_runs.py")
            run.status_path.write_text("stale coordinator-only status", encoding="utf-8")
            db.add_event(
                worker.mission_id,
                kind="check",
                actor="codex",
                summary="status updater test passed",
                source_ref="python -m unittest tests.test_prd_runs",
            )
            db.add_artifact(
                worker.mission_id,
                kind="test",
                path_or_url="tests/test_prd_runs.py",
                status="pass",
                summary="PRD run status tests",
            )

            refreshed = prd_runs.update_status_for_mission(worker.mission_id)
            text = run.status_path.read_text(encoding="utf-8")

        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed.parent_id, run.parent_id)
        self.assertNotIn("stale coordinator-only status", text)
        self.assertIn("check/codex: status updater test passed", text)
        self.assertIn("pass test: `tests/test_prd_runs.py` - PRD run status tests", text)


if __name__ == "__main__":
    unittest.main()
