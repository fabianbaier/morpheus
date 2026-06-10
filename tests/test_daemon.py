import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from morpheus import daemon


class DaemonTest(unittest.TestCase):
    def test_loop_runner_install_writes_launch_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plist = root / "com.morpheus.loop-runner.plist"
            calls = []

            def fake_run(args):
                calls.append(args)
                if args == ["launchctl", "list", daemon.LOOP_RUNNER_LABEL]:
                    return 1, "", "not loaded"
                return 0, "", ""

            with patch.object(daemon, "LAUNCH_AGENT_DIR", root), patch.object(
                daemon, "LOOP_RUNNER_PATH", plist
            ), patch.object(
                daemon, "MORPHEUS_DIR", root / ".morpheus"
            ), patch.object(
                daemon, "LOOP_RUNNER_LOG", root / ".morpheus" / "loop-runner.log"
            ), patch.object(
                daemon, "_run", new=fake_run
            ):
                ok, message = daemon.install_loop_runner(
                    interval=60,
                    limit=7,
                    timeout=42,
                    morpheus_path="/tmp/morpheus",
                )

            self.assertTrue(ok, message)
            text = plist.read_text()
            self.assertIn("<string>loops</string>", text)
            self.assertIn("<string>run-due</string>", text)
            self.assertIn("<string>--limit</string>", text)
            self.assertIn("<string>7</string>", text)
            self.assertIn("<string>--timeout</string>", text)
            self.assertIn("<string>42</string>", text)
            self.assertIn("<string>--all-projects</string>", text)
            self.assertIn("<key>StartInterval</key>", text)
            self.assertIn("<integer>60</integer>", text)
            self.assertIn(["launchctl", "load", "-w", str(plist)], calls)

    def test_loop_runner_status_parses_plist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plist = root / "com.morpheus.loop-runner.plist"
            plist.write_text(
                daemon._loop_runner_plist_xml(
                    "/tmp/morpheus",
                    interval=120,
                    limit=3,
                    timeout=99,
                )
            )

            def fake_run(args):
                if args == ["launchctl", "list", daemon.LOOP_RUNNER_LABEL]:
                    return 0, f"123\t0\t{daemon.LOOP_RUNNER_LABEL}", ""
                return 0, "", ""

            with patch.object(daemon, "LOOP_RUNNER_PATH", plist), patch.object(
                daemon, "LOOP_RUNNER_BEACON_PATH", root / "loop-runner.beacon"
            ), patch.object(
                daemon, "LOOP_RUNNER_LOG", root / "loop-runner.log"
            ), patch.object(
                daemon, "_run", new=fake_run
            ):
                status = daemon.loop_runner_status()

            self.assertTrue(status.plist_installed)
            self.assertTrue(status.launchctl_loaded)
            self.assertEqual(status.pid, 123)
            self.assertEqual(status.program_path, "/tmp/morpheus")
            self.assertEqual(status.interval_secs, 120)
            self.assertEqual(status.limit, 3)
            self.assertEqual(status.timeout_secs, 99)


if __name__ == "__main__":
    unittest.main()
