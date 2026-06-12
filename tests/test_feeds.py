"""Tests for the feeds aggregator: items, rules/thresholds, loop routing."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from morpheus import db, feeds, loops


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


def _run(loop, status="ok", summary="all green"):
    return db.PromptLoopRun(
        id=1, loop_id=loop.id, started_at=time.time(), finished_at=time.time(),
        status=status, exit_code=0 if status == "ok" else 1,
        output_path="", summary=summary,
    )


class FeedItemsTest(unittest.TestCase):
    def test_post_and_recent(self):
        with _TempDB():
            fid = feeds.post("BTC crossed 100k", "details here", priority=1)
            items = feeds.recent()
            self.assertEqual(items[0].id, fid)
            self.assertEqual(items[0].title, "BTC crossed 100k")
            self.assertEqual(items[0].priority, 1)

    def test_post_rejects_empty_title(self):
        with _TempDB():
            with self.assertRaises(ValueError):
                feeds.post("   ")

    def test_since_id_and_latest_id(self):
        with _TempDB():
            a = feeds.post("first")
            b = feeds.post("second")
            self.assertEqual(feeds.latest_id(), b)
            newer = feeds.recent(since_id=a)
            self.assertEqual([it.id for it in newer], [b])

    def test_render_text_is_plain_lines(self):
        with _TempDB():
            feeds.post("quiet update")
            feeds.post("ALERT thing", priority=1)
            text = feeds.render_text()
            lines = text.splitlines()
            self.assertEqual(len(lines), 2)
            self.assertIn("! [manual] ALERT thing", lines[0])
            self.assertIn("[manual] quiet update", lines[1])


class FeedRulesTest(unittest.TestCase):
    def test_set_rule_replaces_existing(self):
        with _TempDB():
            feeds.set_rule("loop", "7", policy="always")
            feeds.set_rule("loop", "7", policy="on_failure")
            rs = feeds.rules(source_kind="loop", source_ref="7")
            self.assertEqual(len(rs), 1)
            self.assertEqual(rs[0].policy, "on_failure")

    def test_invalid_policy_and_pattern_rejected(self):
        with _TempDB():
            with self.assertRaises(ValueError):
                feeds.set_rule("loop", "1", policy="sometimes")
            with self.assertRaises(Exception):
                feeds.set_rule("loop", "1", policy="on_match", pattern="([")

    def test_delete_rule(self):
        with _TempDB():
            r = feeds.set_rule("loop", "9", policy="always")
            self.assertTrue(feeds.delete_rule(r.id))
            self.assertEqual(feeds.rules(source_kind="loop", source_ref="9"), [])


class RoutingTest(unittest.TestCase):
    def _loop(self):
        return db.create_loop(name="news", prompt="scan news", interval_seconds=60,
                              command="echo")

    def test_always_posts(self):
        with _TempDB():
            lp = self._loop()
            feeds.set_rule("loop", str(lp.id), policy="always")
            self.assertIsNotNone(feeds.route_loop_run(lp, _run(lp)))
            self.assertEqual(feeds.recent()[0].title, "all green")
            self.assertEqual(feeds.recent()[0].source_kind, "loop")

    def test_no_rule_posts_nothing(self):
        with _TempDB():
            lp = self._loop()
            self.assertIsNone(feeds.route_loop_run(lp, _run(lp)))
            self.assertEqual(feeds.recent(), [])

    def test_on_match_threshold(self):
        with _TempDB():
            lp = self._loop()
            feeds.set_rule("loop", str(lp.id), policy="on_match", pattern=r"breaking|>\s*100")
            self.assertIsNone(feeds.route_loop_run(lp, _run(lp, summary="nothing new")))
            self.assertIsNotNone(feeds.route_loop_run(lp, _run(lp, summary="BREAKING: thing")))
            self.assertEqual(len(feeds.recent()), 1)

    def test_on_change_threshold(self):
        with _TempDB():
            lp = self._loop()
            feeds.set_rule("loop", str(lp.id), policy="on_change")
            feeds.route_loop_run(lp, _run(lp, summary="state A"))
            feeds.route_loop_run(lp, _run(lp, summary="state A"))  # same → skipped
            feeds.route_loop_run(lp, _run(lp, summary="state B"))
            titles = [it.title for it in feeds.recent()]
            self.assertEqual(titles, ["state B", "state A"])

    def test_failures_always_post_with_priority(self):
        with _TempDB():
            lp = self._loop()
            feeds.set_rule("loop", str(lp.id), policy="on_match", pattern="never-matches-xyz")
            feeds.route_loop_run(lp, _run(lp, status="error", summary="loop crashed"))
            items = feeds.recent()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].priority, 1)

    def test_publish_run_routes_into_feed(self):
        with _TempDB():
            lp = self._loop()
            feeds.set_rule("loop", str(lp.id), policy="always")
            loops.publish_run(lp, _run(lp, summary="published via hook"))
            self.assertEqual(feeds.recent()[0].title, "published via hook")


if __name__ == "__main__":
    unittest.main()
