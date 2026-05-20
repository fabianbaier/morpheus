import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from typer.testing import CliRunner

from morpheus import cli, db, iterm_client


class BroadcastCliTest(unittest.TestCase):
    def test_broadcast_payload_stages_by_default_and_submits_with_enter(self) -> None:
        self.assertEqual(
            cli._broadcast_payload("check remote commits", submit=False),
            "[morpheus broadcast] check remote commits",
        )
        self.assertEqual(
            cli._broadcast_payload("check remote commits", submit=True),
            "[morpheus broadcast] check remote commits\r",
        )

    def test_resolve_broadcast_targets_excludes_self_by_default(self) -> None:
        missions = [
            db.Mission(tab_id="tab-self", session_id="session-self", mission_id="m_self", goal="self"),
            db.Mission(tab_id="tab-other", session_id="session-other", mission_id="m_other", goal="other"),
        ]

        with patch.object(cli.db, "all_missions", new=lambda: list(missions)):
            targets, errors = cli._resolve_broadcast_targets(
                None,
                include_self=False,
                self_session_id="session-self",
            )

        self.assertEqual(errors, [])
        self.assertEqual([mission.tab_id for mission in targets], ["tab-other"])

    def test_broadcast_note_sends_to_iterm_and_records_note(self) -> None:
        runner = CliRunner()
        missions = [
            db.Mission(tab_id="tab-self", session_id="session-self", mission_id="m_self", goal="self"),
            db.Mission(tab_id="tab-other", session_id="session-other", mission_id="m_other", goal="other"),
        ]
        captured = {}

        def fake_run(coro_factory):
            return asyncio.run(coro_factory(object()))

        async def fake_send_text_to_tabs(connection, tab_ids, text):
            captured["send"] = {"tab_ids": tab_ids, "text": text}
            return [
                iterm_client.SendTextResult(tab_id=tab_id, session_id=f"session-{tab_id}", ok=True)
                for tab_id in tab_ids
            ]

        def fake_add_note(**kwargs):
            captured["note"] = kwargs
            return 1

        with patch.dict(os.environ, {"ITERM_SESSION_ID": "session-self"}), patch.object(
            cli.db, "all_missions", new=lambda: list(missions)
        ), patch.object(
            cli.iterm_client, "run", new=fake_run
        ), patch.object(
            cli.iterm_client, "send_text_to_tabs", new=fake_send_text_to_tabs
        ), patch.object(
            cli.db, "add_note", new=fake_add_note
        ), patch.object(
            cli.ledger_mod, "log_action", new=lambda *args, **kwargs: 1
        ), patch.object(
            cli.ctx_mod, "write_context_file", new=lambda: None
        ), patch.object(
            cli.ctx_mod, "write_context_json", new=lambda: None
        ):
            result = runner.invoke(cli.app, ["note", "--kind", "broadcast", "verify commits"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(captured["send"]["tab_ids"], ["tab-other"])
        self.assertEqual(captured["send"]["text"], "[morpheus broadcast] verify commits\r")
        self.assertEqual(captured["note"]["text"], "verify commits")
        self.assertEqual(captured["note"]["tab_id"], "tab-self")
        self.assertEqual(captured["note"]["kind"], "broadcast")

    def test_regular_note_does_not_send_to_iterm(self) -> None:
        runner = CliRunner()
        captured = {}

        def fail_run(coro_factory):
            raise AssertionError("regular notes should not touch iTerm")

        def fake_add_note(**kwargs):
            captured["note"] = kwargs
            return 1

        with patch.dict(os.environ, {}, clear=True), patch.object(
            cli.iterm_client, "run", new=fail_run
        ), patch.object(
            cli.db, "add_note", new=fake_add_note
        ), patch.object(
            cli.ctx_mod, "write_context_file", new=lambda: None
        ), patch.object(
            cli.ctx_mod, "write_context_json", new=lambda: None
        ):
            result = runner.invoke(cli.app, ["note", "context only"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(captured["note"]["kind"], "note")
