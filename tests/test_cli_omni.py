"""CLI tests for the omnipresence surface: the g2-bridge remote contract
(`remote feed/feed-ack/context-add/omni-status`) and the human-facing
`context`, `memory`, and `omni` command groups."""

import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from morpheus import cli, config as cfg_mod, db, feeds, memory, omni_templates, signals


@contextmanager
def isolated_omni_runtime():
    """Repoint the shared DB, the config file, and the memory dir at a temp
    root so CLI invocations never touch the real ~/.morpheus."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db_dir = root / "db"
        with patch.object(db, "DB_DIR", db_dir), patch.object(
            db, "DB_PATH", db_dir / "morpheus.db"
        ), patch.object(cfg_mod, "CONFIG_DIR", root), patch.object(
            cfg_mod, "CONFIG_PATH", root / "config.toml"
        ), patch.object(memory, "MEMORY_DIR", root), patch.dict(
            "os.environ", {}, clear=False
        ) as env:
            env.pop("MORPHEUS_OMNI_ENABLED", None)
            yield root


runner = CliRunner()


def _invoke_json(args):
    result = runner.invoke(cli.app, args)
    return result, (json.loads(result.output) if result.output.strip() else None)


class RemoteFeedContractTest(unittest.TestCase):
    def test_feed_without_cursor_returns_newest_items_ascending(self):
        with isolated_omni_runtime():
            ids = [feeds.post(f"item {i}", body=f"body {i}") for i in range(5)]
            result, payload = _invoke_json(["remote", "feed", "--compact", "--limit", "3"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(set(payload), {"items", "latest_id"})
        self.assertEqual([it["id"] for it in payload["items"]], ids[2:])
        self.assertEqual(payload["latest_id"], ids[-1])
        item = payload["items"][0]
        self.assertEqual(
            set(item),
            {"id", "ts", "title", "body", "priority", "source_kind", "source_ref", "metadata"},
        )
        self.assertIsInstance(item["id"], int)
        self.assertIsInstance(item["ts"], float)
        self.assertIsInstance(item["priority"], int)
        self.assertIsInstance(item["metadata"], dict)

    def test_feed_cursor_returns_strictly_greater_ids_ascending(self):
        with isolated_omni_runtime():
            ids = [feeds.post(f"item {i}") for i in range(5)]
            result, payload = _invoke_json(
                ["remote", "feed", "--compact", "--after", str(ids[1]), "--limit", "2"]
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual([it["id"] for it in payload["items"]], ids[2:4])
        self.assertEqual(payload["latest_id"], ids[-1])

    def test_feed_empty_database(self):
        with isolated_omni_runtime():
            result, payload = _invoke_json(["remote", "feed", "--compact"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(payload, {"items": [], "latest_id": 0})

    def test_feed_items_carry_metadata_and_bounded_body(self):
        with isolated_omni_runtime():
            feeds.post("judged", body="x" * 5000,
                       metadata={"score": 0.86, "rationale": "espresso beans"})
            result, payload = _invoke_json(["remote", "feed", "--compact"])

        self.assertEqual(result.exit_code, 0, result.output)
        item = payload["items"][0]
        self.assertEqual(item["metadata"]["score"], 0.86)
        self.assertLessEqual(len(item["body"]), cli.FEED_BODY_MAX_CHARS)


class RemoteFeedNameTest(unittest.TestCase):
    """Item 1: remote feed serves the configured [omni] feed, not a
    hardwired 'main', so a non-default feed actually reaches the glasses."""

    def test_feed_defaults_to_configured_omni_feed(self):
        with isolated_omni_runtime() as root:
            (root / "config.toml").write_text('[omni]\nfeed = "glasses"\n')
            gid = feeds.post("glasses item", feed="glasses")
            feeds.post("main item")  # different feed — must not leak in
            result, payload = _invoke_json(["remote", "feed", "--compact"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual([it["id"] for it in payload["items"]], [gid])
        self.assertEqual(payload["items"][0]["title"], "glasses item")
        self.assertEqual(payload["latest_id"], gid)

    def test_feed_flag_overrides_configured_feed(self):
        with isolated_omni_runtime() as root:
            (root / "config.toml").write_text('[omni]\nfeed = "glasses"\n')
            feeds.post("glasses item", feed="glasses")
            mid = feeds.post("main item")
            result, payload = _invoke_json(
                ["remote", "feed", "--compact", "--feed", "main"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual([it["id"] for it in payload["items"]], [mid])


class RemoteFeedDismissedTest(unittest.TestCase):
    """Item 4: dismissed items never resurface on bridge/simulator restart."""

    def test_dismissed_items_are_excluded_by_default(self):
        with isolated_omni_runtime():
            kept = feeds.post("keep")
            dismissed = feeds.post("already dismissed")
            feeds.record_ack(dismissed, "dismissed")
            result, payload = _invoke_json(["remote", "feed", "--compact"])
            cursor, cursor_payload = _invoke_json(
                ["remote", "feed", "--compact", "--after", "0"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual([it["id"] for it in payload["items"]], [kept])
        self.assertEqual(cursor.exit_code, 0, cursor.output)
        self.assertEqual([it["id"] for it in cursor_payload["items"]], [kept])

    def test_include_dismissed_opts_out(self):
        with isolated_omni_runtime():
            kept = feeds.post("keep")
            dismissed = feeds.post("already dismissed")
            feeds.record_ack(dismissed, "dismissed")
            result, payload = _invoke_json(
                ["remote", "feed", "--compact", "--include-dismissed"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual([it["id"] for it in payload["items"]], [kept, dismissed])


class RemoteFeedMetadataCapTest(unittest.TestCase):
    """Item 8: one huge metadata blob cannot blow the bridge's output cap."""

    def test_oversized_metadata_is_stubbed_keeping_judge_score(self):
        with isolated_omni_runtime():
            feeds.post("big one", metadata={
                "judge": {"score": 0.91, "rationale": "fine"},
                "blob": "x" * (cli.FEED_METADATA_MAX_CHARS + 100),
            })
            result, payload = _invoke_json(["remote", "feed", "--compact"])

        self.assertEqual(result.exit_code, 0, result.output)
        meta = payload["items"][0]["metadata"]
        self.assertEqual(meta, {"truncated": True, "judge": {"score": 0.91}})

    def test_normal_metadata_passes_through_unchanged(self):
        with isolated_omni_runtime():
            feeds.post("small one", metadata={"loop_id": 3, "judge": {"score": 0.8}})
            result, payload = _invoke_json(["remote", "feed", "--compact"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(payload["items"][0]["metadata"],
                         {"loop_id": 3, "judge": {"score": 0.8}})


class RemoteFeedAckTest(unittest.TestCase):
    def test_ack_records_and_returns_contract_shape(self):
        with isolated_omni_runtime():
            item = feeds.post("push")
            result, payload = _invoke_json(
                ["remote", "feed-ack", "--compact", "--item", str(item), "--action", "expanded"]
            )
            acks = feeds.recent_acks()

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(payload, {"ok": True, "item": item, "action": "expanded"})
        self.assertEqual((acks[0].item_id, acks[0].action), (item, "expanded"))

    def test_ack_rejects_unknown_action(self):
        with isolated_omni_runtime():
            item = feeds.post("push")
            result, payload = _invoke_json(
                ["remote", "feed-ack", "--compact", "--item", str(item), "--action", "swiped"]
            )
            self.assertEqual(feeds.recent_acks(), [])

        self.assertEqual(result.exit_code, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("action", payload["error"])


class RemoteContextAddTest(unittest.TestCase):
    def test_context_add_stores_location(self):
        with isolated_omni_runtime():
            result, payload = _invoke_json(
                ["remote", "context-add", "--compact", "--kind", "location",
                 "--data", '{"lat": 52.52, "lon": 13.405, "accuracy": 12}']
            )
            latest = signals.latest("location")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(payload, {"ok": True, "id": latest.id})
        self.assertEqual(latest.payload["lat"], 52.52)

    def test_context_add_rejects_bad_lat(self):
        with isolated_omni_runtime():
            result, payload = _invoke_json(
                ["remote", "context-add", "--compact", "--kind", "location",
                 "--data", '{"lat": "not-a-number", "lon": 13.4}']
            )
            self.assertIsNone(signals.latest("location"))

        self.assertEqual(result.exit_code, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("lat", payload["error"])

    def test_context_add_rejects_non_object_json(self):
        with isolated_omni_runtime():
            result, payload = _invoke_json(
                ["remote", "context-add", "--compact", "--kind", "location", "--data", "[1,2]"]
            )

        self.assertEqual(result.exit_code, 1)
        self.assertFalse(payload["ok"])

    def test_context_add_rejects_oversized_data_before_parsing(self):
        # Item 13: the length gate fires BEFORE json.loads, so a huge string
        # is rejected cheaply instead of being parsed first.
        huge = '{"pad": "' + "x" * (2 * signals.PAYLOAD_MAX_CHARS) + '"}'
        with isolated_omni_runtime():
            # cli.json IS the stdlib module; a poisoned loads proves the guard
            # rejects before any parse. Parse the CLI's own output afterwards.
            with patch.object(cli.json, "loads",
                              side_effect=AssertionError("must not parse")) as loads:
                result = runner.invoke(
                    cli.app,
                    ["remote", "context-add", "--compact", "--kind", "location",
                     "--data", huge])
                loads.assert_not_called()
            self.assertIsNone(signals.latest("location"))

        payload = json.loads(result.output)
        self.assertEqual(result.exit_code, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("too large", payload["error"])

    def test_context_add_survives_deeply_nested_json(self):
        # Item 13: json.loads raises RecursionError on pathological nesting;
        # the command must fail cleanly, not crash with a traceback.
        bomb = "[" * 3000 + "]" * 3000
        with isolated_omni_runtime():
            result, payload = _invoke_json(
                ["remote", "context-add", "--compact", "--kind", "location",
                 "--data", bomb])

        self.assertEqual(result.exit_code, 1)
        self.assertFalse(payload["ok"])


class RemoteOmniStatusTest(unittest.TestCase):
    def test_defaults_shape(self):
        with isolated_omni_runtime():
            result, payload = _invoke_json(["remote", "omni-status", "--compact"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            payload,
            {
                "enabled": False,
                "threshold": 0.7,
                "push_per_hour": 6,
                "quiet_hours": None,
                "feed": "main",
            },
        )

    def test_reflects_configured_quiet_hours_and_toggle(self):
        with isolated_omni_runtime() as root:
            (root / "config.toml").write_text(
                '[omni]\nenabled = true\nquiet_hours = "22:00-07:00"\nthreshold = 0.5\n'
            )
            result, payload = _invoke_json(["remote", "omni-status", "--compact"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["threshold"], 0.5)
        self.assertEqual(payload["quiet_hours"], {"start": "22:00", "end": "07:00"})


class OmniToggleTest(unittest.TestCase):
    def test_on_off_persist_to_config_and_preserve_content(self):
        with isolated_omni_runtime() as root:
            config = root / "config.toml"
            config.write_text("# my precious comments\n[intro]\nenabled = false\n")

            self.assertEqual(runner.invoke(cli.app, ["omni", "on"]).exit_code, 0)
            self.assertTrue(cfg_mod.omni_settings()["enabled"])
            text = config.read_text()
            self.assertIn("# my precious comments", text)
            self.assertIn("[intro]", text)

            self.assertEqual(runner.invoke(cli.app, ["omni", "off"]).exit_code, 0)
            self.assertFalse(cfg_mod.omni_settings()["enabled"])
            # Toggling edits the existing [omni] section instead of appending another.
            self.assertEqual(config.read_text().count("[omni]"), 1)

    def test_status_prints_resolved_settings(self):
        with isolated_omni_runtime():
            result = runner.invoke(cli.app, ["omni", "status"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("threshold", result.output)
        self.assertIn("push_per_hour", result.output)


class OmniEscalationCliTest(unittest.TestCase):
    """`omni status` masks the ntfy topic (a capability URL) and
    `omni test-push` exercises the escalation channel end to end."""

    def test_status_masks_topic_and_shows_server_and_score(self):
        topic = "supersecrettopic123"
        with isolated_omni_runtime() as root:
            (root / "config.toml").write_text(
                f'[omni]\nntfy_topic = "{topic}"\n'
                'ntfy_server = "https://push.example.com"\n'
                "escalate_score = 0.9\n"
            )
            result = runner.invoke(cli.app, ["omni", "status"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn(topic, result.output)       # never the full topic
        self.assertIn(topic[-4:], result.output)     # …but recognizable
        self.assertIn("set", result.output)
        self.assertIn("push.example.com", result.output)
        self.assertIn("escalate_score", result.output)
        self.assertIn("0.9", result.output)

    def test_status_does_not_leak_a_short_topic(self):
        topic = "short1"  # <= 8 chars: no tail shown at all
        with isolated_omni_runtime() as root:
            (root / "config.toml").write_text(f'[omni]\nntfy_topic = "{topic}"\n')
            result = runner.invoke(cli.app, ["omni", "status"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn(topic, result.output)
        self.assertIn("set", result.output)

    def test_status_reports_unset_topic_as_escalation_off(self):
        with isolated_omni_runtime():
            result = runner.invoke(cli.app, ["omni", "status"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("unset", result.output)
        self.assertIn("ntfy_topic", result.output)

    def test_test_push_success_hints_at_ntfy_and_even_whitelist(self):
        with isolated_omni_runtime() as root:
            (root / "config.toml").write_text('[omni]\nntfy_topic = "t-abc"\n')
            with patch("morpheus.push.send_push", return_value=True) as send:
                result = runner.invoke(cli.app, ["omni", "test-push"])

        self.assertEqual(result.exit_code, 0, result.output)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], "Morpheus test push")
        self.assertEqual(send.call_args.kwargs["settings"]["ntfy_topic"], "t-abc")
        self.assertIn("ntfy app", result.output)
        self.assertIn("Even app", result.output)
        self.assertIn("whitelist", result.output)

    def test_test_push_failure_exits_nonzero(self):
        with isolated_omni_runtime() as root:
            (root / "config.toml").write_text('[omni]\nntfy_topic = "t-abc"\n')
            with patch("morpheus.push.send_push", return_value=False) as send:
                result = runner.invoke(cli.app, ["omni", "test-push"])

        self.assertEqual(result.exit_code, 1)
        send.assert_called_once()
        self.assertIn("failed", result.output)

    def test_test_push_without_topic_exits_without_sending(self):
        with isolated_omni_runtime():
            with patch("morpheus.push.send_push", return_value=True) as send:
                result = runner.invoke(cli.app, ["omni", "test-push"])

        self.assertEqual(result.exit_code, 1)
        send.assert_not_called()
        self.assertIn("ntfy_topic", result.output)


class MemoryCliTest(unittest.TestCase):
    def test_add_show_log_roundtrip(self):
        with isolated_omni_runtime():
            added = runner.invoke(
                cli.app, ["memory", "add", "out of espresso beans", "--section", "Current"]
            )
            shown = runner.invoke(cli.app, ["memory", "show"])
            logged = runner.invoke(cli.app, ["memory", "log"])

        self.assertEqual(added.exit_code, 0, added.output)
        self.assertEqual(shown.exit_code, 0, shown.output)
        self.assertIn("## Current", shown.output)
        self.assertIn("out of espresso beans", shown.output)
        self.assertEqual(logged.exit_code, 0, logged.output)
        self.assertIn("espresso", logged.output)

    def test_path_points_into_memory_dir(self):
        with isolated_omni_runtime() as root:
            result = runner.invoke(cli.app, ["memory", "path"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn(str(root / "memory.md"), result.output)
            self.assertTrue((root / "memory.md").exists())

    def test_add_rejects_empty_text(self):
        with isolated_omni_runtime():
            result = runner.invoke(cli.app, ["memory", "add", "   "])

        self.assertEqual(result.exit_code, 1)

    def test_show_never_wraps_long_facts(self):
        # Item 3: the omni-memory agent reads `memory show`; rich would
        # hard-wrap at ~80 cols off a TTY and split one fact into fragments,
        # breaking the agent's never-duplicate contract.
        fact = "the user prefers the long way around the park " * 4  # >120 chars
        fact = fact.strip()
        self.assertGreater(len(fact), 120)
        with isolated_omni_runtime():
            added = runner.invoke(cli.app, ["memory", "add", fact, "--section", "Current"])
            shown = runner.invoke(cli.app, ["memory", "show"])
            capped = runner.invoke(cli.app, ["memory", "show", "--max-chars", "2000"])

        self.assertEqual(added.exit_code, 0, added.output)
        for result in (shown, capped):
            self.assertEqual(result.exit_code, 0, result.output)
            matching = [ln for ln in result.output.splitlines() if fact in ln]
            self.assertEqual(len(matching), 1, result.output)

    def test_add_rejects_non_canonical_section_by_default(self):
        # Item 11: the omni-memory agent runs this command; free-form section
        # names would let mined feed content sprawl the memory file.
        with isolated_omni_runtime():
            rejected = runner.invoke(
                cli.app, ["memory", "add", "likes the canal", "--section", "Places"])
            self.assertEqual(rejected.exit_code, 1)
            self.assertIn("--custom-section", rejected.output)
            self.assertNotIn("## Places", memory.read_memory())

            allowed = runner.invoke(
                cli.app, ["memory", "add", "likes the canal",
                          "--section", "Places", "--custom-section"])
            self.assertEqual(allowed.exit_code, 0, allowed.output)
            self.assertIn("## Places", memory.read_memory())

    def test_add_accepts_canonical_sections_case_insensitively(self):
        with isolated_omni_runtime():
            for section in ("People", "interests", "CURRENT", "never push"):
                result = runner.invoke(
                    cli.app, ["memory", "add", f"fact for {section}", "--section", section])
                self.assertEqual(result.exit_code, 0, result.output)


class OmniInitTest(unittest.TestCase):
    def _loops_by_name(self):
        return {lp.name: lp for lp in db.all_loops(include_paused=True)}

    def test_init_creates_template_loops_and_location_rule(self):
        with isolated_omni_runtime():
            result = runner.invoke(cli.app, ["omni", "init"])
            self.assertEqual(result.exit_code, 0, result.output)
            by_name = self._loops_by_name()

            self.assertIn("omni-location", by_name)
            self.assertIn("omni-memory", by_name)
            loc = by_name["omni-location"]
            mem = by_name["omni-memory"]
            self.assertEqual(loc.interval_seconds, 300)
            self.assertEqual(mem.interval_seconds, 3600)
            self.assertEqual(loc.status, "active")
            self.assertEqual(mem.status, "active")
            self.assertIn("morpheus context latest --kind location", loc.prompt)
            self.assertIn("NOTHING", loc.prompt)
            self.assertIn("morpheus memory show", mem.prompt)
            self.assertIn("morpheus memory add", mem.prompt)

            loc_rules = feeds.rules(source_kind="loop", source_ref=str(loc.id))
            self.assertEqual(len(loc_rules), 1)
            self.assertEqual(loc_rules[0].policy, "on_threshold")
            self.assertEqual(loc_rules[0].threshold, 0.0)  # follow [omni] default
            self.assertEqual(loc_rules[0].feed, "main")
            # The memory loop feeds memory.md, not the glasses: no rule.
            self.assertEqual(feeds.rules(source_kind="loop", source_ref=str(mem.id)), [])
            self.assertIn("created", result.output)

    def test_init_is_idempotent(self):
        with isolated_omni_runtime():
            first = runner.invoke(cli.app, ["omni", "init"])
            self.assertEqual(first.exit_code, 0, first.output)
            ids = {name: lp.id for name, lp in self._loops_by_name().items()}
            second = runner.invoke(cli.app, ["omni", "init"])
            self.assertEqual(second.exit_code, 0, second.output)

            by_name = self._loops_by_name()
            self.assertEqual(len(by_name), 2)  # no duplicates
            self.assertEqual({n: lp.id for n, lp in by_name.items()}, ids)
            self.assertIn("exists", second.output)
            # still exactly one rule, pointing at the same location loop
            loc_rules = feeds.rules(source_kind="loop",
                                    source_ref=str(ids["omni-location"]))
            self.assertEqual(len(loc_rules), 1)

    def test_init_does_not_resume_paused_loops(self):
        with isolated_omni_runtime():
            runner.invoke(cli.app, ["omni", "init"])
            loc = self._loops_by_name()["omni-location"]
            db.set_loop_status(loc.id, "paused")
            runner.invoke(cli.app, ["omni", "init"])
            self.assertEqual(db.get_loop(loc.id).status, "paused")

    def test_force_recreates_from_current_templates(self):
        with isolated_omni_runtime():
            runner.invoke(cli.app, ["omni", "init"])
            old = self._loops_by_name()
            db.update_loop_details(old["omni-location"].id, prompt="user-mangled")
            forced = runner.invoke(cli.app, ["omni", "init", "--force"])
            self.assertEqual(forced.exit_code, 0, forced.output)
            self.assertIn("recreated", forced.output)

            by_name = self._loops_by_name()
            self.assertEqual(len(by_name), 2)
            new_loc = by_name["omni-location"]
            self.assertNotEqual(new_loc.id, old["omni-location"].id)
            self.assertEqual(
                new_loc.prompt,
                omni_templates.render_prompt(omni_templates.LOCATION_PROMPT),
            )
            rules = feeds.rules(source_kind="loop", source_ref=str(new_loc.id))
            self.assertEqual([r.policy for r in rules], ["on_threshold"])

    def test_status_shows_templates_rules_and_recent_items(self):
        with isolated_omni_runtime():
            before = runner.invoke(cli.app, ["omni", "status"])
            self.assertEqual(before.exit_code, 0, before.output)
            self.assertIn("missing", before.output)

            runner.invoke(cli.app, ["omni", "init"])
            feeds.post("Beans on promo")
            after = runner.invoke(cli.app, ["omni", "status"])
            self.assertEqual(after.exit_code, 0, after.output)
            self.assertIn("omni-location", after.output)
            self.assertIn("omni-memory", after.output)
            self.assertNotIn("missing", after.output)
            self.assertIn("active", after.output)
            self.assertIn("feed rules", after.output)
            self.assertIn("judge_command", after.output)
            self.assertIn("Beans on promo", after.output)


class MemoryCandidatesTest(unittest.TestCase):
    def test_candidates_show_reactions(self):
        with isolated_omni_runtime():
            liked = feeds.post("espresso beans promo nearby")
            hated = feeds.post("crypto dip alert")
            quiet_item = feeds.post("unnoticed update")
            feeds.record_ack(liked, "expanded")
            feeds.record_ack(hated, "dismissed")
            del quiet_item
            result = runner.invoke(cli.app, ["memory", "candidates"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("[expanded]", result.output)
        self.assertIn("espresso beans promo nearby", result.output)
        self.assertIn("[dismissed]", result.output)
        self.assertIn("[no-ack]", result.output)

    def test_candidates_empty_feed(self):
        with isolated_omni_runtime():
            result = runner.invoke(cli.app, ["memory", "candidates"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("no feed items", result.output)

    def test_candidates_read_the_configured_omni_feed(self):
        # Item 1: mining must look at the feed omnipresence pushes to.
        with isolated_omni_runtime() as root:
            (root / "config.toml").write_text('[omni]\nfeed = "glasses"\n')
            feeds.post("glasses push", feed="glasses")
            feeds.post("main push")
            result = runner.invoke(cli.app, ["memory", "candidates"])
            override = runner.invoke(cli.app, ["memory", "candidates", "--feed", "main"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("glasses push", result.output)
        self.assertNotIn("main push", result.output)
        self.assertEqual(override.exit_code, 0, override.output)
        self.assertIn("main push", override.output)

    def test_candidates_never_wrap_long_titles(self):
        # Item 3: same raw-stdout contract as `memory show`.
        title = ("promo at the corner store on the long avenue with a very "
                 "detailed description that keeps going " * 2).strip()[:190]
        self.assertGreater(len(title), 120)
        with isolated_omni_runtime():
            feeds.post(title)
            result = runner.invoke(cli.app, ["memory", "candidates"])

        self.assertEqual(result.exit_code, 0, result.output)
        matching = [ln for ln in result.output.splitlines() if title in ln]
        self.assertEqual(len(matching), 1, result.output)


class TemplatePromptConstraintsTest(unittest.TestCase):
    """Item 7: the omni loop prompts carry an explicit command allowlist and
    treat feed/web content as untrusted data (prompt-level defense; the
    enforced bounds live CLI-side)."""

    def test_memory_prompt_enumerates_allowed_commands(self):
        prompt = omni_templates.MEMORY_PROMPT
        self.assertIn("ONLY commands", prompt)
        for command in ("morpheus memory show",
                        "morpheus remote feed --compact --limit 20",
                        "morpheus memory candidates",
                        "morpheus memory add"):
            self.assertIn(command, prompt)
        self.assertIn("Never run any", prompt)
        self.assertIn("untrusted DATA", prompt)
        self.assertIn("ignore", prompt)
        self.assertIn("at most 200 characters", prompt)
        for section in ("People", "Interests", "Current", "Never push"):
            self.assertIn(section, prompt)

    def test_location_prompt_enumerates_allowed_commands(self):
        prompt = omni_templates.LOCATION_PROMPT
        self.assertIn("ONLY commands", prompt)
        self.assertIn("morpheus context latest --kind location", prompt)
        self.assertIn("morpheus memory show --max-chars 2000", prompt)
        self.assertIn("Never run any", prompt)
        self.assertIn("untrusted DATA", prompt)
        self.assertIn("at most 200", prompt)
        # No fabrication: finds must come from a real search, else NOTHING.
        self.assertIn("NEVER invent, guess, or fabricate", prompt)

    def test_module_documents_prompt_level_defense(self):
        self.assertIn("prompt-level", omni_templates.__doc__)


class ContextCliTest(unittest.TestCase):
    def test_add_and_latest(self):
        with isolated_omni_runtime():
            added = runner.invoke(
                cli.app,
                ["context", "add", "--kind", "location", "--data", '{"lat": 1, "lon": 2}'],
            )
            latest = runner.invoke(cli.app, ["context", "latest", "--kind", "location"])
            listed = runner.invoke(cli.app, ["context", "list", "--kind", "location"])

        self.assertEqual(added.exit_code, 0, added.output)
        self.assertIn("stored", added.output)
        self.assertEqual(latest.exit_code, 0, latest.output)
        self.assertIn("location", latest.output)
        self.assertEqual(listed.exit_code, 0, listed.output)

    def test_add_rejects_bad_payload(self):
        with isolated_omni_runtime():
            result = runner.invoke(
                cli.app,
                ["context", "add", "--kind", "location", "--data", '{"lat": 200, "lon": 2}'],
            )

        self.assertEqual(result.exit_code, 1)

    def test_latest_without_signals_reports_empty(self):
        with isolated_omni_runtime():
            result = runner.invoke(cli.app, ["context", "latest"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("no context signals", result.output)

    def test_add_rejects_oversized_data_before_parsing(self):
        # Item 13: same pre-parse length gate as `remote context-add`.
        huge = '{"pad": "' + "x" * (2 * signals.PAYLOAD_MAX_CHARS) + '"}'
        with isolated_omni_runtime():
            with patch.object(cli.json, "loads",
                              side_effect=AssertionError("must not parse")) as loads:
                result = runner.invoke(
                    cli.app, ["context", "add", "--kind", "location", "--data", huge])
                loads.assert_not_called()
            self.assertIsNone(signals.latest("location"))

        self.assertEqual(result.exit_code, 1)
        self.assertIn("too large", result.output)

    def test_add_survives_deeply_nested_json(self):
        bomb = "[" * 3000 + "]" * 3000
        with isolated_omni_runtime():
            result = runner.invoke(
                cli.app, ["context", "add", "--kind", "location", "--data", bomb])
            self.assertIsNone(signals.latest("location"))

        self.assertEqual(result.exit_code, 1)


if __name__ == "__main__":
    unittest.main()


class TemplateBinPathTest(unittest.TestCase):
    """Loop agents run in shells whose PATH often lacks the morpheus shim, so
    the rendered prompts must carry the resolved absolute CLI path."""

    def test_render_prompt_substitutes_backticked_commands(self):
        rendered = omni_templates.render_prompt(
            omni_templates.LOCATION_PROMPT, "/opt/venv/bin/morpheus")
        self.assertIn("`/opt/venv/bin/morpheus context latest --kind location`", rendered)
        self.assertIn("`/opt/venv/bin/morpheus memory show --max-chars 2000`", rendered)
        self.assertNotIn("`morpheus ", rendered)

    def test_render_prompt_keeps_bare_name_without_resolution(self):
        self.assertEqual(
            omni_templates.render_prompt(omni_templates.MEMORY_PROMPT, "morpheus"),
            omni_templates.MEMORY_PROMPT,
        )
