"""Runs the front-end JS unit tests (app.test.mjs) and a syntax check via Node.

Skipped automatically when Node.js is not on PATH, so the Python-only test
environments still pass `make test`.
"""

import shutil
import subprocess
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "morpheus" / "desktop" / "web"
NODE = shutil.which("node")


@unittest.skipIf(NODE is None, "node.js not installed")
class DesktopWebJsTest(unittest.TestCase):
    def test_app_js_syntax(self):
        r = subprocess.run([NODE, "--check", str(WEB / "app.js")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_pure_helpers(self):
        r = subprocess.run([NODE, str(WEB / "app.test.mjs")],
                           capture_output=True, text=True, cwd=str(WEB))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("passed", r.stdout)


if __name__ == "__main__":
    unittest.main()
