import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from morpheus import prd_runs


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


if __name__ == "__main__":
    unittest.main()
