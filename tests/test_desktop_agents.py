"""Tests for the agent runner that drives claude/codex/gemini CLIs.

`parse_line` is tested against a real captured `claude --output-format
stream-json` fixture, and `run_turn` is tested end-to-end against a fake agent
script that replays that fixture — so no network, credits, or real CLI needed.
"""

import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from morpheus import db
from morpheus.desktop import agents

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "claude_stream.jsonl"


class _TempDB:
    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._p = [patch.object(db, "DB_DIR", root),
                   patch.object(db, "DB_PATH", root / "morpheus.db")]
        for p in self._p:
            p.start()
        return root

    def __exit__(self, *a):
        for p in reversed(self._p):
            p.stop()
        self._tmp.cleanup()


class ClaudeParseTest(unittest.TestCase):
    def _events(self):
        adapter = agents.ClaudeAdapter()
        out = []
        for line in FIXTURE.read_text().splitlines():
            out.extend(adapter.parse_line(line))
        return out

    def test_fixture_yields_expected_event_sequence(self):
        types = [e["type"] for e in self._events()]
        self.assertEqual(types[0], "session")
        for expected in ("thinking", "text", "tool_use", "tool_result", "result"):
            self.assertIn(expected, types, expected)

    def test_session_and_result_carry_session_id_and_cost(self):
        evs = self._events()
        session = next(e for e in evs if e["type"] == "session")
        result = next(e for e in evs if e["type"] == "result")
        self.assertTrue(session["session_id"])
        self.assertEqual(result["session_id"], session["session_id"])
        self.assertGreater(result["cost_usd"], 0)

    def test_tool_use_has_name_and_summary(self):
        tu = next(e for e in self._events() if e["type"] == "tool_use")
        self.assertEqual(tu["name"], "Read")
        self.assertIn("sample.txt", tu["summary"])

    def test_blank_and_garbage_lines_ignored(self):
        adapter = agents.ClaudeAdapter()
        self.assertEqual(adapter.parse_line(""), [])
        self.assertEqual(adapter.parse_line("not json"), [])

    def test_web_search_tool_becomes_web_search_event(self):
        adapter = agents.ClaudeAdapter()
        line = ('{"type":"assistant","message":{"content":'
                '[{"type":"tool_use","id":"t1","name":"WebSearch","input":{"query":"morpheus iterm"}}]}}')
        evs = adapter.parse_line(line)
        self.assertEqual(evs[0]["type"], "web_search")
        self.assertEqual(evs[0]["query"], "morpheus iterm")


class CodexParseTest(unittest.TestCase):
    def test_maps_known_item_types(self):
        a = agents.CodexAdapter()
        self.assertEqual(a.parse_line('{"type":"thread.started","thread_id":"th_1"}')[0]["type"], "session")
        self.assertEqual(a.parse_line('{"item":{"type":"assistant_message","text":"hi"}}')[0]["type"], "text")
        self.assertEqual(a.parse_line('{"item":{"type":"reasoning","text":"hmm"}}')[0]["type"], "thinking")
        ev = a.parse_line('{"item":{"type":"command_execution","command":"ls -la"}}')[0]
        self.assertEqual(ev["name"], "Bash")

    def test_plain_text_line_degrades_to_text(self):
        a = agents.CodexAdapter()
        self.assertEqual(a.parse_line("Working on it...")[0]["type"], "text")


class RegistryTest(unittest.TestCase):
    def test_available_agents_lists_known_clis(self):
        kinds = {a["kind"] for a in agents.available_agents()}
        self.assertEqual(kinds, {"claude", "codex", "gemini"})

    def test_get_adapter_unknown(self):
        self.assertIsNone(agents.get_adapter("nope"))


class RunTurnTest(unittest.TestCase):
    def _fake_agent(self, tmp: Path) -> str:
        script = tmp / "fake_agent.py"
        script.write_text(textwrap.dedent(f"""
            import sys
            sys.stdout.write(open({str(FIXTURE)!r}).read())
        """))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return str(script)

    def test_run_turn_streams_events_from_fake_agent(self):
        with _TempDB():
            with tempfile.TemporaryDirectory() as tmp:
                argv = [sys.executable, self._fake_agent(Path(tmp))]
                evs = list(agents.run_turn("claude", "read the file", cwd=tmp, argv=argv))
                types = [e["type"] for e in evs]
                self.assertEqual(types[0], "session")
                self.assertEqual(types[-1], "result")
                self.assertIn("tool_use", types)

    def test_run_turn_logs_cost_to_ledger(self):
        from morpheus import ledger
        with _TempDB():
            with tempfile.TemporaryDirectory() as tmp:
                argv = [sys.executable, self._fake_agent(Path(tmp))]
                list(agents.run_turn("claude", "hi", cwd=tmp, argv=argv))
                self.assertGreater(ledger.daily_dollar_total(), 0)

    def test_unknown_agent_yields_error(self):
        evs = list(agents.run_turn("bogus", "hi"))
        self.assertEqual(evs[0]["type"], "error")

    def test_bad_cwd_yields_error(self):
        argv = [sys.executable, "-c", "print('x')"]
        evs = list(agents.run_turn("claude", "hi", cwd="/no/such/dir", argv=argv))
        self.assertEqual(evs[0]["type"], "error")

    def test_nonzero_exit_without_result_yields_error(self):
        argv = [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"]
        with tempfile.TemporaryDirectory() as tmp:
            evs = list(agents.run_turn("claude", "hi", cwd=tmp, argv=argv))
            self.assertEqual(evs[-1]["type"], "error")
            self.assertIn("boom", evs[-1]["message"])


if __name__ == "__main__":
    unittest.main()
