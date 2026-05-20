import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from morpheus import cli


class ActivityCliTest(unittest.TestCase):
    def test_activity_short_reads_cached_snapshot_without_refreshing_iterm(self) -> None:
        runner = CliRunner()
        snapshot = {
            "generated_at": 123,
            "session_count": 1,
            "sessions": [
                {
                    "tab_id": "8",
                    "goal": "multi-tenancy",
                    "state": "working",
                    "headline": "Adding focused tests now.",
                    "tail_lines": ["Progress: 50%.", "Adding focused tests now."],
                }
            ],
        }

        def fail_run(coro_factory):
            raise AssertionError("cached activity should not connect to iTerm")

        with patch.object(cli.activity_mod, "read_snapshot", new=lambda: snapshot), patch.object(
            cli.iterm_client, "run", new=fail_run
        ):
            result = runner.invoke(cli.app, ["activity", "--format", "short"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("8 multi-tenancy: Adding focused tests now.", result.output)


if __name__ == "__main__":
    unittest.main()
