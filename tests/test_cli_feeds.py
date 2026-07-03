"""CLI tests for the `morpheus feeds` group: routing the user's own loops
into ambient feeds (rules / route / unroute / recent)."""

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from morpheus import cli, config as cfg_mod, db, feeds


@contextmanager
def isolated_runtime():
    """Repoint the shared DB and config at a temp root (same pattern as
    tests/test_cli_omni.py) so CLI invocations never touch ~/.morpheus."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db_dir = root / "db"
        with patch.object(db, "DB_DIR", db_dir), patch.object(
            db, "DB_PATH", db_dir / "morpheus.db"
        ), patch.object(cfg_mod, "CONFIG_DIR", root), patch.object(
            cfg_mod, "CONFIG_PATH", root / "config.toml"
        ):
            yield root


runner = CliRunner()


def _loop(name="watcher"):
    return db.create_loop(name=name, prompt="scan", interval_seconds=60,
                          command="echo")


class FeedsRouteTest(unittest.TestCase):
    def test_route_defaults_to_on_threshold_and_omni_feed(self):
        with isolated_runtime() as root:
            (root / "config.toml").write_text('[omni]\nfeed = "glasses"\n')
            lp = _loop()
            result = runner.invoke(cli.app, ["feeds", "route", str(lp.id)])
            rules = feeds.rules(source_kind="loop", source_ref=str(lp.id), feed=None)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].policy, "on_threshold")
        self.assertEqual(rules[0].feed, "glasses")
        self.assertEqual(rules[0].threshold, 0.0)  # follow the [omni] default
        self.assertIn("glasses", result.output)
        self.assertIn("on_threshold", result.output)

    def test_route_with_explicit_policy_pattern_and_feed(self):
        with isolated_runtime():
            lp = _loop()
            result = runner.invoke(cli.app, [
                "feeds", "route", str(lp.id), "--policy", "on_match",
                "--pattern", "error|breaking", "--feed", "alerts"])
            rules = feeds.rules(source_kind="loop", source_ref=str(lp.id),
                                feed="alerts")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].policy, "on_match")
        self.assertEqual(rules[0].pattern, "error|breaking")

    def test_route_with_threshold(self):
        with isolated_runtime():
            lp = _loop()
            result = runner.invoke(cli.app, [
                "feeds", "route", str(lp.id), "--threshold", "0.8"])
            rule = feeds.rules(source_kind="loop", source_ref=str(lp.id))[0]

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(rule.threshold, 0.8)

    def test_route_replaces_existing_rule_for_the_loop(self):
        with isolated_runtime():
            lp = _loop()
            runner.invoke(cli.app, ["feeds", "route", str(lp.id), "--policy", "always"])
            runner.invoke(cli.app, ["feeds", "route", str(lp.id), "--policy", "on_change"])
            rules = feeds.rules(source_kind="loop", source_ref=str(lp.id))

        self.assertEqual([r.policy for r in rules], ["on_change"])

    def test_route_validates_loop_exists(self):
        with isolated_runtime():
            result = runner.invoke(cli.app, ["feeds", "route", "999"])
            self.assertEqual(feeds.rules(feed=None), [])

        self.assertEqual(result.exit_code, 1)
        self.assertIn("no loop #999", result.output)

    def test_route_rejects_bad_policy_threshold_and_pattern(self):
        with isolated_runtime():
            lp = _loop()
            bad_policy = runner.invoke(
                cli.app, ["feeds", "route", str(lp.id), "--policy", "sometimes"])
            bad_threshold = runner.invoke(
                cli.app, ["feeds", "route", str(lp.id), "--threshold", "1.5"])
            bad_pattern = runner.invoke(
                cli.app, ["feeds", "route", str(lp.id), "--policy", "on_match",
                          "--pattern", "(["])
            self.assertEqual(feeds.rules(feed=None), [])

        self.assertEqual(bad_policy.exit_code, 1)
        self.assertIn("policy", bad_policy.output)
        self.assertEqual(bad_threshold.exit_code, 1)
        self.assertEqual(bad_pattern.exit_code, 1)

    def test_routed_loop_actually_posts_to_the_feed(self):
        # End to end: a user-routed loop's runs land in the chosen feed.
        import time as _time
        with isolated_runtime():
            lp = _loop()
            runner.invoke(cli.app, ["feeds", "route", str(lp.id),
                                    "--policy", "always", "--feed", "alerts"])
            run = db.PromptLoopRun(
                id=1, loop_id=lp.id, started_at=_time.time(),
                finished_at=_time.time(), status="ok", exit_code=0,
                output_path="", summary="all green")
            feeds.route_loop_run(lp, run)
            self.assertEqual([it.title for it in feeds.recent(feed="alerts")],
                             ["all green"])


class FeedsRulesListTest(unittest.TestCase):
    def test_lists_rules_across_feeds_with_details(self):
        with isolated_runtime():
            lp = _loop("news-watcher")
            feeds.set_rule("loop", str(lp.id), policy="on_threshold",
                           threshold=0.8, feed="glasses")
            feeds.set_rule("loop", "12345", policy="on_match",
                           pattern="breaking", feed="main")
            result = runner.invoke(cli.app, ["feeds", "rules"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("news-watcher", result.output)
        self.assertIn("glasses", result.output)
        self.assertIn("on_threshold", result.output)
        self.assertIn("0.8", result.output)
        self.assertIn("on_match", result.output)
        self.assertIn("breaking", result.output)

    def test_feed_flag_filters(self):
        with isolated_runtime():
            feeds.set_rule("loop", "1", policy="always", feed="glasses")
            feeds.set_rule("loop", "2", policy="always", feed="main")
            result = runner.invoke(cli.app, ["feeds", "rules", "--feed", "main"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("loop:2", result.output)
        self.assertNotIn("loop:1", result.output)

    def test_empty_rules_hint(self):
        with isolated_runtime():
            result = runner.invoke(cli.app, ["feeds", "rules"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("no feed rules", result.output)
        self.assertIn("feeds route", result.output)


class FeedsUnrouteTest(unittest.TestCase):
    def test_unroute_deletes_the_rule(self):
        with isolated_runtime():
            lp = _loop()
            runner.invoke(cli.app, ["feeds", "route", str(lp.id)])
            result = runner.invoke(cli.app, ["feeds", "unroute", str(lp.id)])
            rules = feeds.rules(source_kind="loop", source_ref=str(lp.id), feed=None)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("removed", result.output)
        self.assertEqual(rules, [])

    def test_unroute_without_rule_reports_nothing_to_remove(self):
        with isolated_runtime():
            lp = _loop()
            result = runner.invoke(cli.app, ["feeds", "unroute", str(lp.id)])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("nothing to remove", result.output)

    def test_unroute_validates_loop_exists(self):
        with isolated_runtime():
            result = runner.invoke(cli.app, ["feeds", "unroute", "999"])

        self.assertEqual(result.exit_code, 1)
        self.assertIn("no loop #999", result.output)

    def test_unroute_can_target_one_feed(self):
        with isolated_runtime():
            lp = _loop()
            feeds.set_rule("loop", str(lp.id), policy="always", feed="glasses")
            feeds.set_rule("loop", str(lp.id), policy="always", feed="main")
            result = runner.invoke(
                cli.app, ["feeds", "unroute", str(lp.id), "--feed", "glasses"])
            remaining = feeds.rules(source_kind="loop", source_ref=str(lp.id),
                                    feed=None)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual([r.feed for r in remaining], ["main"])


class FeedsRecentTest(unittest.TestCase):
    def test_recent_shows_configured_feed_items(self):
        with isolated_runtime() as root:
            (root / "config.toml").write_text('[omni]\nfeed = "glasses"\n')
            feeds.post("glasses push", feed="glasses")
            feeds.post("main push")
            result = runner.invoke(cli.app, ["feeds", "recent"])
            override = runner.invoke(cli.app, ["feeds", "recent", "--feed", "main"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("glasses push", result.output)
        self.assertNotIn("main push", result.output)
        self.assertEqual(override.exit_code, 0, override.output)
        self.assertIn("main push", override.output)

    def test_recent_empty_feed_hint(self):
        with isolated_runtime():
            result = runner.invoke(cli.app, ["feeds", "recent"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("no items", result.output)


if __name__ == "__main__":
    unittest.main()
