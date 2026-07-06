"""Tests for the feeds aggregator: items, rules/thresholds, loop routing,
and the judged on_threshold path (omnipresence, PRD §3.5)."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from morpheus import config as cfg_mod, db, feeds, loops
from morpheus.judge import JudgeResult


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

    def test_recent_after_pages_backlog_in_ascending_order(self):
        with _TempDB():
            ids = [feeds.post(f"item {i}") for i in range(5)]
            first = feeds.recent_after(0, limit=2)
            self.assertEqual([it.id for it in first], ids[:2])
            # an overflowing burst arrives on the next poll, not never
            second = feeds.recent_after(first[-1].id, limit=2)
            self.assertEqual([it.id for it in second], ids[2:4])
            # display callers keep the newest-first view of recent()
            self.assertEqual([it.id for it in feeds.recent(limit=2)], [ids[4], ids[3]])

    def test_init_memoizes_per_database_path_not_globally(self):
        # _init() skips re-running the DDL for a database it already set up,
        # but a repointed DB_PATH (the test-isolation pattern) is a *new*
        # database that still needs its schema created.
        with _TempDB():
            feeds.post("first db item")
            self.assertEqual(len(feeds.recent()), 1)
        with _TempDB():
            self.assertEqual(feeds.recent(), [])  # fresh DB, fresh schema
            feeds.post("second db item")
            self.assertEqual([it.title for it in feeds.recent()], ["second db item"])

    def test_post_stores_normalized_title(self):
        # Truncation at TITLE_MAX_CHARS can expose trailing whitespace; the
        # stored title is the canonical normalized shape.
        raw = "  " + "x" * 199 + "  tail  "
        with _TempDB():
            feeds.post(raw)
            stored = feeds.recent()[0].title
            self.assertEqual(stored, feeds._normalize_title(raw))
            self.assertEqual(stored, "x" * 199)
            self.assertLessEqual(len(stored), feeds.TITLE_MAX_CHARS)

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


class FeedAcksTest(unittest.TestCase):
    def test_record_and_recent_acks_newest_first(self):
        with _TempDB():
            a = feeds.post("push one")
            b = feeds.post("push two")
            feeds.record_ack(a, "expanded")
            feeds.record_ack(b, "dismissed")
            acks = feeds.recent_acks()
            self.assertEqual([(k.item_id, k.action) for k in acks],
                             [(b, "dismissed"), (a, "expanded")])
            self.assertGreater(acks[0].ts, 0)

    def test_action_is_normalized_and_validated(self):
        with _TempDB():
            item = feeds.post("push")
            feeds.record_ack(item, "  Dismissed ")
            self.assertEqual(feeds.recent_acks()[0].action, "dismissed")
            for bad in ("", "swiped", None):
                with self.assertRaises(ValueError):
                    feeds.record_ack(item, bad)

    def test_item_id_must_be_positive_integer(self):
        with _TempDB():
            for bad in (0, -3, "abc", None):
                with self.assertRaises(ValueError):
                    feeds.record_ack(bad, "expanded")

    def test_recent_acks_respects_limit(self):
        with _TempDB():
            item = feeds.post("push")
            for _ in range(5):
                feeds.record_ack(item, "expanded")
            self.assertEqual(len(feeds.recent_acks(limit=3)), 3)


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

    def test_on_change_ignores_storage_truncation_of_long_summaries(self):
        with _TempDB():
            lp = self._loop()
            feeds.set_rule("loop", str(lp.id), policy="on_change")
            long_summary = "market scan: " + "x" * 300  # post() stores title[:200]
            feeds.route_loop_run(lp, _run(lp, summary=long_summary))
            feeds.route_loop_run(lp, _run(lp, summary=long_summary))  # same → skipped
            self.assertEqual(len(feeds.recent()), 1)

    def test_on_change_evaluates_on_fresh_database(self):
        with _TempDB():
            # evaluate() must init the schema itself: routing can hit a brand-new
            # DB before any post()/set_rule() call has created the feed tables.
            rule = feeds.FeedRule(id=1, feed="main", source_kind="loop",
                                  source_ref="1", policy="on_change", pattern="",
                                  created_at=0.0)
            self.assertTrue(feeds.evaluate(rule, summary="first result", failed=False))

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

    def test_rules_on_non_default_feeds_still_route(self):
        # Item 1/5 regression: route_loop_run must consult rules on EVERY
        # feed, or a loop routed to a non-default feed never posts anywhere.
        with _TempDB():
            lp = self._loop()
            feeds.set_rule("loop", str(lp.id), policy="always", feed="glasses")
            self.assertIsNotNone(feeds.route_loop_run(lp, _run(lp)))
            self.assertEqual(feeds.recent(feed="glasses")[0].title, "all green")
            self.assertEqual(feeds.recent(feed="main"), [])


def _omni(**over):
    """A resolved [omni] settings dict like config.omni_settings() returns."""
    base = {"enabled": True, "threshold": 0.7, "push_per_hour": 6,
            "quiet_hours": None, "feed": "main", "judge_command": "",
            "ntfy_topic": "", "ntfy_server": "https://ntfy.sh",
            "escalate_score": 0.85}
    base.update(over)
    return base


class ThresholdRuleTest(unittest.TestCase):
    def test_set_rule_stores_threshold_for_on_threshold(self):
        with _TempDB():
            rule = feeds.set_rule("loop", "3", policy="on_threshold", threshold=0.8)
            self.assertEqual(rule.threshold, 0.8)
            stored = feeds.rules(source_kind="loop", source_ref="3")[0]
            self.assertEqual((stored.policy, stored.threshold), ("on_threshold", 0.8))

    def test_set_rule_validates_threshold_range(self):
        with _TempDB():
            for bad in (-0.1, 1.5, "high"):
                with self.assertRaises(ValueError):
                    feeds.set_rule("loop", "3", policy="on_threshold", threshold=bad)

    def test_threshold_zeroed_for_other_policies(self):
        with _TempDB():
            rule = feeds.set_rule("loop", "3", policy="always", threshold=0.9)
            self.assertEqual(rule.threshold, 0.0)

    def test_evaluate_stays_false_for_on_threshold(self):
        # evaluate() is pure/cheap by contract; the judged path lives in
        # route_loop_run, so evaluate must never pass an on_threshold rule.
        rule = feeds.FeedRule(id=1, feed="main", source_kind="loop",
                              source_ref="1", policy="on_threshold",
                              pattern="", created_at=0.0, threshold=0.5)
        self.assertFalse(feeds.evaluate(rule, summary="anything", failed=False))

    def test_migration_adds_threshold_column_to_old_database(self):
        old_rules_ddl = """
        CREATE TABLE feed_rules (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            feed        TEXT NOT NULL DEFAULT 'main',
            source_kind TEXT NOT NULL,
            source_ref  TEXT NOT NULL DEFAULT '',
            policy      TEXT NOT NULL DEFAULT 'always',
            pattern     TEXT NOT NULL DEFAULT '',
            created_at  REAL NOT NULL
        );
        """
        with _TempDB():
            # A database created by the pre-on_threshold schema, with a row.
            with db._connect() as conn:
                conn.executescript(old_rules_ddl)
                conn.execute(
                    "INSERT INTO feed_rules (feed, source_kind, source_ref,"
                    " policy, pattern, created_at) VALUES"
                    " ('main', 'loop', '1', 'always', '', 0)")
            # First feeds touch migrates: the column appears, old rows read
            # back with threshold 0.0, and new on_threshold rules store theirs.
            rule = feeds.set_rule("loop", "2", policy="on_threshold", threshold=0.6)
            self.assertEqual(rule.threshold, 0.6)
            with db._connect() as conn:
                cols = {r["name"] for r in conn.execute("PRAGMA table_info(feed_rules)")}
            self.assertIn("threshold", cols)
            old = feeds.rules(source_kind="loop", source_ref="1")[0]
            self.assertEqual((old.policy, old.threshold), ("always", 0.0))


class OnThresholdRoutingTest(unittest.TestCase):
    """End-to-end judged routing with an injected fake judge (no subprocess)."""

    def setUp(self):
        feeds._judge_failing_sources.clear()

    def _loop(self):
        return db.create_loop(name="omni-location", prompt="scout",
                              interval_seconds=300, command="echo")

    def _route(self, lp, summary, *, settings=None, judge=None, status="ok"):
        judge = judge if judge is not None else MagicMock(
            return_value=JudgeResult(score=0.9, rationale="relevant"))
        with patch.object(feeds, "_omni_settings", return_value=settings or _omni()), \
                patch.object(feeds, "_judge_item", judge):
            item_id = feeds.route_loop_run(lp, _run(lp, status=status, summary=summary))
        return item_id, judge

    def test_score_above_rule_threshold_posts_with_judge_metadata(self):
        with _TempDB():
            lp = self._loop()
            feeds.set_rule("loop", str(lp.id), policy="on_threshold", threshold=0.5)
            judge = MagicMock(return_value=JudgeResult(score=0.86, rationale="espresso beans"))
            item_id, _ = self._route(lp, "Supermarket 50m left: beans on promo", judge=judge)
            self.assertIsNotNone(item_id)
            item = feeds.recent()[0]
            self.assertEqual(item.title, "Supermarket 50m left: beans on promo")
            self.assertEqual(item.metadata["judge"],
                             {"score": 0.86, "rationale": "espresso beans"})
            self.assertEqual(item.source_ref, str(lp.id))

    def test_score_below_rule_threshold_posts_nothing(self):
        with _TempDB():
            lp = self._loop()
            feeds.set_rule("loop", str(lp.id), policy="on_threshold", threshold=0.5)
            judge = MagicMock(return_value=JudgeResult(score=0.4, rationale="meh"))
            item_id, _ = self._route(lp, "weak find", judge=judge)
            self.assertIsNone(item_id)
            self.assertEqual(feeds.recent(), [])

    def test_zero_rule_threshold_falls_back_to_omni_default(self):
        with _TempDB():
            lp = self._loop()
            feeds.set_rule("loop", str(lp.id), policy="on_threshold", threshold=0.0)
            judge = MagicMock(return_value=JudgeResult(score=0.65, rationale="close"))
            item_id, _ = self._route(lp, "borderline", settings=_omni(threshold=0.7), judge=judge)
            self.assertIsNone(item_id)  # 0.65 < omni default 0.7
            judge = MagicMock(return_value=JudgeResult(score=0.75, rationale="good"))
            item_id, _ = self._route(lp, "clears default", settings=_omni(threshold=0.7), judge=judge)
            self.assertIsNotNone(item_id)

    def test_rule_threshold_overrides_omni_default(self):
        with _TempDB():
            lp = self._loop()
            feeds.set_rule("loop", str(lp.id), policy="on_threshold", threshold=0.9)
            judge = MagicMock(return_value=JudgeResult(score=0.8, rationale="ok"))
            item_id, _ = self._route(lp, "not enough for this rule",
                                     settings=_omni(threshold=0.7), judge=judge)
            self.assertIsNone(item_id)  # 0.8 clears the config 0.7 but not the rule 0.9

    def test_omni_disabled_never_calls_judge(self):
        with _TempDB():
            lp = self._loop()
            feeds.set_rule("loop", str(lp.id), policy="on_threshold")
            item_id, judge = self._route(lp, "anything", settings=_omni(enabled=False))
            self.assertIsNone(item_id)
            judge.assert_not_called()

    def test_push_cap_blocks_before_judge(self):
        with _TempDB():
            lp = self._loop()
            feeds.set_rule("loop", str(lp.id), policy="on_threshold")
            feeds.post("earlier one")
            feeds.post("earlier two")
            item_id, judge = self._route(lp, "over budget",
                                         settings=_omni(push_per_hour=2))
            self.assertIsNone(item_id)
            judge.assert_not_called()
            # A roomier budget lets the same candidate through.
            item_id, _ = self._route(lp, "over budget", settings=_omni(push_per_hour=5))
            self.assertIsNotNone(item_id)

    def test_dedupe_against_recent_titles_blocks_before_judge(self):
        with _TempDB():
            lp = self._loop()
            feeds.set_rule("loop", str(lp.id), policy="on_threshold")
            feeds.post("Beans on promo")
            item_id, judge = self._route(lp, "  Beans on promo  ")
            self.assertIsNone(item_id)
            judge.assert_not_called()
            item_id, _ = self._route(lp, "A different find")
            self.assertIsNotNone(item_id)

    def test_quiet_hours_window_blocks_judge(self):
        with _TempDB():
            lp = self._loop()
            feeds.set_rule("loop", str(lp.id), policy="on_threshold")
            now = time.localtime()
            quiet = {"start": f"{(now.tm_hour - 1) % 24:02d}:00",
                     "end": f"{(now.tm_hour + 1) % 24:02d}:00"}
            item_id, judge = self._route(lp, "quiet please",
                                         settings=_omni(quiet_hours=quiet))
            self.assertIsNone(item_id)
            judge.assert_not_called()

    def test_nothing_sentinel_skips_judge(self):
        with _TempDB():
            lp = self._loop()
            feeds.set_rule("loop", str(lp.id), policy="on_threshold")
            for summary in ("NOTHING", "nothing", "  Nothing  "):
                item_id, judge = self._route(lp, summary)
                self.assertIsNone(item_id)
                judge.assert_not_called()

    def test_no_find_meta_summaries_skip_judge(self):
        # Agents sometimes narrate the sentinel ("no 'location' signals
        # yet.") — a healthy meta-summary that must not burn a judge call
        # every 5 minutes.
        with _TempDB():
            lp = self._loop()
            feeds.set_rule("loop", str(lp.id), policy="on_threshold")
            for summary in ("no 'location' signals yet.",
                            "No new finds this run",
                            "NO relevant results nearby",
                            "no location updates"):
                item_id, judge = self._route(lp, summary)
                self.assertIsNone(item_id, summary)
                judge.assert_not_called()
            self.assertEqual(feeds.recent(), [])

    def test_no_find_matcher_leaves_real_headlines_alone(self):
        with _TempDB():
            lp = self._loop()
            feeds.set_rule("loop", str(lp.id), policy="on_threshold")
            for summary in ("Nothing beats this café: 2-for-1 espresso today",
                            "Noodle bar around the corner has signals night",
                            "North exit closed: use the west entrance"):
                item_id, judge = self._route(lp, summary)
                self.assertIsNotNone(item_id, summary)
                judge.assert_called()

    def test_judge_failure_fails_closed_and_logs_once_per_streak(self):
        with _TempDB():
            lp = self._loop()
            feeds.set_rule("loop", str(lp.id), policy="on_threshold")
            failing = MagicMock(return_value=None)
            with self.assertLogs("morpheus.feeds", level="WARNING") as cm:
                self._route(lp, "first failure", judge=failing)
                self._route(lp, "second failure", judge=failing)
            self.assertEqual(len(cm.output), 1)  # logged once, not per run
            self.assertEqual(feeds.recent(), [])
            # A success resets the streak so the next failure logs again.
            self._route(lp, "back to life")
            with self.assertLogs("morpheus.feeds", level="WARNING") as cm:
                self._route(lp, "fails again", judge=failing)
            self.assertEqual(len(cm.output), 1)

    def test_failed_run_is_not_force_posted_for_on_threshold(self):
        with _TempDB():
            lp = self._loop()
            feeds.set_rule("loop", str(lp.id), policy="on_threshold")
            item_id, judge = self._route(lp, "loop crashed", status="error")
            self.assertIsNone(item_id)
            judge.assert_not_called()
            self.assertEqual(feeds.recent(), [])

    def test_failure_force_posting_unchanged_for_other_policies(self):
        with _TempDB():
            lp = self._loop()
            feeds.set_rule("loop", str(lp.id), policy="on_match", pattern="never-xyz")
            with patch.object(feeds, "_omni_settings", return_value=_omni()):
                feeds.route_loop_run(lp, _run(lp, status="error", summary="loop crashed"))
            items = feeds.recent()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].priority, 1)

    def test_judge_receives_memory_and_context(self):
        with _TempDB():
            from morpheus import signals
            lp = self._loop()
            feeds.set_rule("loop", str(lp.id), policy="on_threshold")
            signals.add_signal("location", {"lat": 52.52, "lon": 13.405})
            with patch("morpheus.memory.top_entries", return_value="## Current\n- beans"):
                _, judge = self._route(lp, "candidate title")
            judge.assert_called_once()
            kwargs = judge.call_args.kwargs
            self.assertIn("beans", kwargs["memory_text"])
            joined = "\n".join(kwargs["context_lines"])
            self.assertIn("location", joined)
            self.assertIn("52.52", joined)


class EscalationTest(unittest.TestCase):
    """Phone-push escalation (PRD §3.1): a successful judged post ALSO fires
    an ntfy push when the score clears [omni].escalate_score (or the posted
    priority is > 0); escalation never blocks or alters the feed post."""

    def setUp(self):
        feeds._judge_failing_sources.clear()

    def _loop(self):
        return db.create_loop(name="omni-location", prompt="scout",
                              interval_seconds=300, command="echo")

    def _route(self, lp, summary, *, settings, score=0.9, status="ok",
               send=None):
        judge = MagicMock(return_value=JudgeResult(score=score, rationale="r"))
        send = send if send is not None else MagicMock(return_value=True)
        with patch.object(feeds, "_omni_settings", return_value=settings), \
                patch.object(feeds, "_judge_item", judge), \
                patch("morpheus.push.send_push", send):
            item_id = feeds.route_loop_run(lp, _run(lp, status=status,
                                                    summary=summary))
        return item_id, send

    def test_score_at_or_above_escalate_score_fires_push_with_title(self):
        with _TempDB():
            lp = self._loop()
            feeds.set_rule("loop", str(lp.id), policy="on_threshold")
            item_id, send = self._route(
                lp, "Beans on promo", score=0.9,
                settings=_omni(ntfy_topic="t-abc", escalate_score=0.85))
            self.assertIsNotNone(item_id)
            send.assert_called_once()
            self.assertEqual(send.call_args.args[0], "Beans on promo")
            self.assertEqual(
                send.call_args.kwargs["settings"]["ntfy_topic"], "t-abc")

    def test_score_below_escalate_score_posts_without_push(self):
        with _TempDB():
            lp = self._loop()
            feeds.set_rule("loop", str(lp.id), policy="on_threshold")
            item_id, send = self._route(
                lp, "Mild find", score=0.75,  # clears threshold 0.7 only
                settings=_omni(ntfy_topic="t-abc", escalate_score=0.85))
            self.assertIsNotNone(item_id)  # still posted to the feed
            send.assert_not_called()

    def test_priority_positive_judged_item_escalates_via_rule_helper(self):
        # Judged posts carry priority 0 today; the rule still covers
        # priority > 0 items should the judged path ever set one.
        settings = _omni(ntfy_topic="t-abc", escalate_score=0.85)
        send = MagicMock(return_value=True)
        with patch("morpheus.push.send_push", send):
            feeds._escalate_if_urgent("urgent", 0.2, 1, settings)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], "urgent")

    def test_low_score_zero_priority_does_not_escalate(self):
        send = MagicMock(return_value=True)
        with patch("morpheus.push.send_push", send):
            feeds._escalate_if_urgent(
                "meh", 0.2, 0, _omni(ntfy_topic="t-abc", escalate_score=0.85))
        send.assert_not_called()

    def test_empty_topic_never_calls_sender(self):
        with _TempDB():
            lp = self._loop()
            feeds.set_rule("loop", str(lp.id), policy="on_threshold")
            item_id, send = self._route(
                lp, "Great find", score=0.99,
                settings=_omni(ntfy_topic="", escalate_score=0.85))
            self.assertIsNotNone(item_id)  # feed post unaffected
            send.assert_not_called()

    def test_send_failure_does_not_affect_the_feed_post(self):
        with _TempDB():
            lp = self._loop()
            feeds.set_rule("loop", str(lp.id), policy="on_threshold")
            for send in (MagicMock(return_value=False),
                         MagicMock(side_effect=RuntimeError("boom"))):
                feeds.set_rule("loop", str(lp.id), policy="on_threshold")
                item_id, _ = self._route(
                    lp, f"Find {id(send)}", score=0.95,
                    settings=_omni(ntfy_topic="t-abc"), send=send)
                self.assertIsNotNone(item_id)
                send.assert_called_once()
            self.assertEqual(len(feeds.recent()), 2)

    def test_failure_force_posts_never_escalate(self):
        # Deliberate: a flapping watcher force-posting priority-1 failures
        # must not spam the phone; escalation is judged-path only.
        with _TempDB():
            lp = self._loop()
            feeds.set_rule("loop", str(lp.id), policy="on_match",
                           pattern="never-xyz")
            send = MagicMock(return_value=True)
            with patch.object(feeds, "_omni_settings",
                              return_value=_omni(ntfy_topic="t-abc")), \
                    patch("morpheus.push.send_push", send):
                feeds.route_loop_run(lp, _run(lp, status="error",
                                              summary="loop crashed"))
            self.assertEqual(feeds.recent()[0].priority, 1)  # posted as before
            send.assert_not_called()


class QuietHoursHelperTest(unittest.TestCase):
    def _tm(self, hour, minute):
        return time.struct_time((2026, 7, 3, hour, minute, 0, 4, 184, -1))

    def test_overnight_range_wraps_midnight(self):
        quiet = {"start": "22:00", "end": "08:00"}
        self.assertTrue(cfg_mod.is_quiet_now(quiet, self._tm(23, 0)))
        self.assertTrue(cfg_mod.is_quiet_now(quiet, self._tm(2, 30)))
        self.assertTrue(cfg_mod.is_quiet_now(quiet, self._tm(7, 59)))
        self.assertFalse(cfg_mod.is_quiet_now(quiet, self._tm(8, 0)))
        self.assertFalse(cfg_mod.is_quiet_now(quiet, self._tm(12, 0)))
        self.assertFalse(cfg_mod.is_quiet_now(quiet, self._tm(21, 59)))

    def test_daytime_range(self):
        quiet = {"start": "09:00", "end": "17:30"}
        self.assertTrue(cfg_mod.is_quiet_now(quiet, self._tm(9, 0)))
        self.assertTrue(cfg_mod.is_quiet_now(quiet, self._tm(17, 29)))
        self.assertFalse(cfg_mod.is_quiet_now(quiet, self._tm(17, 30)))
        self.assertFalse(cfg_mod.is_quiet_now(quiet, self._tm(8, 59)))

    def test_none_and_zero_width_are_off(self):
        self.assertFalse(cfg_mod.is_quiet_now(None, self._tm(3, 0)))
        self.assertFalse(cfg_mod.is_quiet_now({}, self._tm(3, 0)))
        self.assertFalse(cfg_mod.is_quiet_now({"start": "10:00", "end": "10:00"},
                                              self._tm(10, 0)))

    def test_malformed_input_is_off(self):
        self.assertFalse(cfg_mod.is_quiet_now({"start": "late", "end": "early"},
                                              self._tm(3, 0)))


class PushPerHourZeroTest(unittest.TestCase):
    """Item 2: push_per_hour = 0 means ZERO pushes, never 'unlimited'."""

    def setUp(self):
        feeds._judge_failing_sources.clear()

    def _loop(self):
        return db.create_loop(name="omni-location", prompt="scout",
                              interval_seconds=300, command="echo")

    def test_zero_cap_blocks_everything_before_the_judge(self):
        with _TempDB():
            lp = self._loop()
            feeds.set_rule("loop", str(lp.id), policy="on_threshold")
            judge = MagicMock(return_value=JudgeResult(score=0.99, rationale="great"))
            with patch.object(feeds, "_omni_settings",
                              return_value=_omni(push_per_hour=0)), \
                    patch.object(feeds, "_judge_item", judge):
                item_id = feeds.route_loop_run(lp, _run(lp, summary="urgent find"))
            self.assertIsNone(item_id)
            judge.assert_not_called()
            self.assertEqual(feeds.recent(), [])

    def test_positive_cap_still_pushes(self):
        with _TempDB():
            lp = self._loop()
            feeds.set_rule("loop", str(lp.id), policy="on_threshold")
            judge = MagicMock(return_value=JudgeResult(score=0.99, rationale="great"))
            with patch.object(feeds, "_omni_settings",
                              return_value=_omni(push_per_hour=1)), \
                    patch.object(feeds, "_judge_item", judge):
                item_id = feeds.route_loop_run(lp, _run(lp, summary="urgent find"))
            self.assertIsNotNone(item_id)


class DismissedExclusionTest(unittest.TestCase):
    """Item 4: consumers can hide items the user already dismissed."""

    def test_recent_and_recent_after_can_exclude_dismissed(self):
        with _TempDB():
            kept = feeds.post("keep me")
            dismissed = feeds.post("dismiss me")
            expanded = feeds.post("expand me")
            feeds.record_ack(dismissed, "dismissed")
            feeds.record_ack(expanded, "expanded")

            # default: everything (memory mining needs the dismissed ones)
            self.assertEqual({it.id for it in feeds.recent()},
                             {kept, dismissed, expanded})
            self.assertEqual({it.id for it in feeds.recent(exclude_dismissed=True)},
                             {kept, expanded})
            self.assertEqual(
                [it.id for it in feeds.recent_after(0, exclude_dismissed=True)],
                [kept, expanded])

    def test_dismiss_wins_even_after_a_later_expand(self):
        # The user has already seen it; a later expand must not resurrect it.
        with _TempDB():
            item = feeds.post("seen once")
            feeds.record_ack(item, "dismissed")
            feeds.record_ack(item, "expanded")
            self.assertEqual(feeds.recent(exclude_dismissed=True), [])


class AckBoundsTest(unittest.TestCase):
    """Item 10: acks are bounded — no future ids, table pruned to newest 5000."""

    def test_rejects_item_id_beyond_latest_feed_item(self):
        with _TempDB():
            item = feeds.post("real item")
            with self.assertRaises(ValueError):
                feeds.record_ack(item + 1, "expanded")
            self.assertEqual(feeds.recent_acks(), [])
            # the real item still acks fine
            feeds.record_ack(item, "expanded")
            self.assertEqual(feeds.recent_acks()[0].item_id, item)

    def test_rejects_any_ack_on_empty_feed(self):
        with _TempDB():
            with self.assertRaises(ValueError):
                feeds.record_ack(1, "expanded")

    def test_acks_pruned_to_newest_rows_on_insert(self):
        with _TempDB():
            item = feeds.post("busy item")
            with patch.object(feeds, "ACKS_MAX_ROWS", 10):
                for _ in range(25):
                    feeds.record_ack(item, "expanded")
                with db._connect() as conn:
                    row = conn.execute("SELECT COUNT(*) AS n, MIN(id) AS lo,"
                                       " MAX(id) AS hi FROM feed_acks").fetchone()
                self.assertEqual(row["n"], 10)
                self.assertEqual(row["hi"] - row["lo"] + 1, 10)  # the newest block


if __name__ == "__main__":
    unittest.main()
