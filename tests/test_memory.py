"""Tests for the user-level relevance memory file (~/.morpheus/memory.md)."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from morpheus import memory


class _TempMemoryDir:
    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._p = patch.object(memory, "MEMORY_DIR", root)
        self._p.start()
        return root

    def __exit__(self, *a):
        self._p.stop()
        self._tmp.cleanup()


class EnsureFileTest(unittest.TestCase):
    def test_creates_template_with_all_sections(self):
        with _TempMemoryDir() as root:
            path = memory.ensure_file()
            self.assertEqual(path, root / "memory.md")
            text = path.read_text()
            for section in memory.SECTIONS:
                self.assertIn(f"## {section}", text)

    def test_does_not_clobber_existing_file(self):
        with _TempMemoryDir() as root:
            (root / "memory.md").write_text("## Current\n- my own note\n")
            memory.ensure_file()
            self.assertIn("my own note", memory.read_memory())


class AppendEntryTest(unittest.TestCase):
    def test_appends_dated_line_under_section(self):
        with _TempMemoryDir():
            now = time.time()
            line = memory.append_entry("Current", "out of espresso beans", now=now)
            date = time.strftime("%Y-%m-%d", time.localtime(now))
            self.assertEqual(line, f"- {date}: out of espresso beans")
            text = memory.read_memory()
            current = text.split("## Current", 1)[1].split("## ", 1)[0]
            self.assertIn("out of espresso beans", current)

    def test_entries_accumulate_in_order_within_section(self):
        with _TempMemoryDir():
            memory.append_entry("Interests", "espresso")
            memory.append_entry("Interests", "climbing")
            section = memory.read_memory().split("## Interests", 1)[1].split("## ", 1)[0]
            self.assertLess(section.index("espresso"), section.index("climbing"))

    def test_missing_section_is_created(self):
        with _TempMemoryDir():
            memory.append_entry("Places", "likes the canal route")
            text = memory.read_memory()
            self.assertIn("## Places", text)
            self.assertIn("likes the canal route", text)

    def test_section_matching_is_case_insensitive_and_canonical(self):
        with _TempMemoryDir():
            memory.append_entry("never push", "crypto price spam")
            text = memory.read_memory()
            # No duplicate "## never push" heading — the template's section is used.
            self.assertEqual(text.count("## Never push"), 1)
            self.assertNotIn("## never push\n", text)
            self.assertIn("crypto price spam", text)

    def test_entry_is_single_line_and_bounded(self):
        with _TempMemoryDir():
            line = memory.append_entry("Current", "multi\nline\n" + "x" * 1000)
            self.assertNotIn("\n", line)
            self.assertLessEqual(len(line), memory.ENTRY_MAX_CHARS + 20)

    def test_rejects_empty_inputs(self):
        with _TempMemoryDir():
            with self.assertRaises(ValueError):
                memory.append_entry("", "fact")
            with self.assertRaises(ValueError):
                memory.append_entry("Current", "   ")


class LogTest(unittest.TestCase):
    def test_every_append_writes_one_log_line(self):
        with _TempMemoryDir() as root:
            memory.append_entry("Current", "first fact")
            memory.append_entry("People", "second fact")
            raw = (root / "memory.log").read_text().strip().splitlines()
            self.assertEqual(len(raw), 2)
            self.assertIn("\tCurrent\tfirst fact", raw[0])
            entries = memory.read_log(limit=10)
            self.assertEqual([e["text"] for e in entries], ["second fact", "first fact"])
            self.assertEqual(entries[0]["section"], "People")

    def test_read_log_respects_limit_and_missing_file(self):
        with _TempMemoryDir():
            self.assertEqual(memory.read_log(), [])
            for i in range(5):
                memory.append_entry("Current", f"fact {i}")
            entries = memory.read_log(limit=2)
            self.assertEqual([e["text"] for e in entries], ["fact 4", "fact 3"])


class TopEntriesTest(unittest.TestCase):
    def test_short_file_is_returned_whole(self):
        with _TempMemoryDir():
            memory.append_entry("Current", "small fact")
            self.assertEqual(memory.top_entries(10_000), memory.read_memory())

    def test_truncates_at_line_boundary_within_budget(self):
        with _TempMemoryDir():
            for i in range(50):
                memory.append_entry("Current", f"fact number {i} with some padding text")
            top = memory.top_entries(max_chars=400)
            self.assertLessEqual(len(top), 400)
            # Safe truncation: never cuts mid-line.
            full_lines = set(memory.read_memory().splitlines())
            for line in top.splitlines():
                self.assertIn(line, full_lines)

    def test_never_push_section_survives_truncation(self):
        # Item 6: 'Never push' is the LAST section, so naive head-truncation
        # would silently drop the user's do-not-push rules as memory grows.
        with _TempMemoryDir():
            memory.append_entry("Never push", "crypto price spam")
            memory.append_entry("Never push", "sports scores")
            for i in range(60):
                memory.append_entry("Current", f"fact number {i} with plenty of padding text here")
            self.assertGreater(len(memory.read_memory()), 2000)

            top = memory.top_entries(max_chars=2000)
            self.assertLessEqual(len(top), 2000)
            self.assertIn("## Never push", top)
            self.assertIn("crypto price spam", top)
            self.assertIn("sports scores", top)
            # The rest of the budget is filled top-down, order preserved.
            self.assertLess(top.index("# Morpheus user memory"),
                            top.index("## Never push"))
            full_lines = set(memory.read_memory().splitlines())
            for line in top.splitlines():
                self.assertIn(line, full_lines)

    def test_never_push_kept_even_when_it_dominates_the_budget(self):
        with _TempMemoryDir():
            for i in range(10):
                memory.append_entry("Never push", f"muted topic {i} " + "x" * 60)
            for i in range(40):
                memory.append_entry("Current", f"current fact {i} " + "y" * 60)
            top = memory.top_entries(max_chars=600)
            for i in range(10):
                self.assertIn(f"muted topic {i}", top)


class LogBoundsTest(unittest.TestCase):
    def test_log_is_trimmed_to_newest_1000_past_2000_lines(self):
        # Item 11: an hourly appender must never grow memory.log unboundedly.
        with _TempMemoryDir() as root:
            log = root / "memory.log"
            log.write_text("".join(
                f"2026-01-01 00:00:00\tCurrent\told fact {i}\n" for i in range(2500)))
            memory.append_entry("Current", "the newest fact")
            lines = log.read_text().splitlines()
            self.assertEqual(len(lines), memory.LOG_TRIM_TO)
            self.assertIn("the newest fact", lines[-1])
            self.assertNotIn("\told fact 0\n", log.read_text())

    def test_log_under_cap_is_left_alone(self):
        with _TempMemoryDir() as root:
            for i in range(3):
                memory.append_entry("Current", f"fact {i}")
            lines = (root / "memory.log").read_text().splitlines()
            self.assertEqual(len(lines), 3)

    def test_read_log_tail_matches_full_read(self):
        with _TempMemoryDir():
            for i in range(30):
                memory.append_entry("Current", f"fact {i}")
            entries = memory.read_log(limit=5)
            self.assertEqual([e["text"] for e in entries],
                             [f"fact {i}" for i in range(29, 24, -1)])


if __name__ == "__main__":
    unittest.main()
