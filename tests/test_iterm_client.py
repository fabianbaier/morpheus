import unittest
from types import SimpleNamespace
from unittest.mock import patch

from morpheus import iterm_client


class FakeSession:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.sent: list[str] = []

    async def async_send_text(self, text: str) -> None:
        self.sent.append(text)


class ItermClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_send_text_to_tabs_sends_exact_text_and_reports_missing(self) -> None:
        session = FakeSession("session-a")
        app = SimpleNamespace(windows=[
            SimpleNamespace(tabs=[
                SimpleNamespace(tab_id="tab-a", current_session=session),
            ]),
        ])

        async def fake_get_app(connection):
            return app

        with patch.object(iterm_client.iterm2, "async_get_app", new=fake_get_app):
            results = await iterm_client.send_text_to_tabs(
                object(),
                ["tab-a", "tab-missing"],
                "[morpheus broadcast] hello\n",
            )

        self.assertEqual(session.sent, ["[morpheus broadcast] hello\n"])
        self.assertTrue(results[0].ok)
        self.assertEqual(results[0].session_id, "session-a")
        self.assertFalse(results[1].ok)
        self.assertEqual(results[1].error, "tab not found")
