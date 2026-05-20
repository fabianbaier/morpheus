import tempfile
import unittest
from pathlib import Path

from morpheus import prd_runs


class PRDRunsTest(unittest.TestCase):
    def test_find_prds_prefers_prd_named_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir()
            (root / "PRD.md").write_text("# Main PRD\n", encoding="utf-8")
            (root / "docs" / "feature-spec.md").write_text("# Feature Spec\n", encoding="utf-8")
            (root / "notes.md").write_text("# Notes\n", encoding="utf-8")

            candidates = prd_runs.find_prds(root)

        self.assertEqual([candidate.label for candidate in candidates], ["PRD.md", "docs/feature-spec.md"])

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
