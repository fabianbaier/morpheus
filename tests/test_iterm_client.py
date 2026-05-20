import unittest
from types import SimpleNamespace
from unittest.mock import patch

from morpheus import iterm_client


class FakeSession:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.name = ""
        self.sent: list[str] = []

    async def async_send_text(self, text: str) -> None:
        self.sent.append(text)

    async def async_set_name(self, name: str) -> None:
        self.name = name


class FakeWindow:
    def __init__(self, tab) -> None:
        self.window_id = "window-a"
        self.tabs = []
        self._tab = tab

    async def async_create_tab(self):
        self.tabs.append(self._tab)
        return self._tab


class ItermClientTest(unittest.IsolatedAsyncioTestCase):
    def test_text_with_enter_uses_carriage_return(self) -> None:
        self.assertEqual(iterm_client.text_with_enter("uptime"), "uptime\r")
        self.assertEqual(iterm_client.text_with_enter("uptime\n"), "uptime\r")
        self.assertEqual(iterm_client.text_with_enter("uptime\r"), "uptime\r")

    async def test_spawn_tab_submits_command_with_carriage_return(self) -> None:
        session = FakeSession("session-new")
        tab = SimpleNamespace(tab_id="tab-new", current_session=session)
        window = FakeWindow(tab)
        app = SimpleNamespace(windows=[window], current_terminal_window=window)

        async def fake_get_app(connection):
            return app

        with patch.object(iterm_client.iterm2, "async_get_app", new=fake_get_app):
            info = await iterm_client.spawn_tab(object(), "uptime\n", goal="check load")

        self.assertIsNotNone(info)
        self.assertEqual(session.name, "NEW check load")
        self.assertEqual(session.sent, ["uptime\r"])

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
