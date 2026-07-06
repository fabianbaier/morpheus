"""Tests for the ntfy phone-push escalation sender (morpheus/push.py,
PRD §3.1 notification mirroring)."""

import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from morpheus import push


def _settings(**over):
    base = {"ntfy_topic": "morpheus-abc123", "ntfy_server": "https://ntfy.sh",
            "escalate_score": 0.85}
    base.update(over)
    return base


class SendPushTest(unittest.TestCase):
    def setUp(self):
        push._send_failing = False

    def _urlopen_ok(self):
        m = MagicMock()
        m.return_value.__enter__.return_value = MagicMock(status=200)
        return m

    def test_posts_correct_url_headers_and_body(self):
        urlopen = self._urlopen_ok()
        with patch("morpheus.push.urllib.request.urlopen", urlopen):
            ok = push.send_push("Beans on promo", "loop [omni-location]",
                                settings=_settings())
        self.assertTrue(ok)
        urlopen.assert_called_once()
        req = urlopen.call_args.args[0]
        self.assertEqual(urlopen.call_args.kwargs["timeout"],
                         push.SEND_TIMEOUT_SECONDS)
        self.assertEqual(req.full_url, "https://ntfy.sh/morpheus-abc123")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.data.decode("utf-8"),
                         "Beans on promo\nloop [omni-location]")
        self.assertEqual(req.get_header("Title"), "Morpheus")
        self.assertEqual(req.get_header("Priority"), "high")

    def test_server_trailing_slash_is_normalized(self):
        urlopen = self._urlopen_ok()
        with patch("morpheus.push.urllib.request.urlopen", urlopen):
            push.send_push("hi", settings=_settings(
                ntfy_server="https://push.example.com/"))
        req = urlopen.call_args.args[0]
        self.assertEqual(req.full_url, "https://push.example.com/morpheus-abc123")

    def test_non_positive_priority_sends_without_high_header(self):
        urlopen = self._urlopen_ok()
        with patch("morpheus.push.urllib.request.urlopen", urlopen):
            push.send_push("calm one", settings=_settings(), priority=0)
        req = urlopen.call_args.args[0]
        self.assertIsNone(req.get_header("Priority"))

    def test_empty_topic_is_a_quiet_noop(self):
        urlopen = self._urlopen_ok()
        with patch("morpheus.push.urllib.request.urlopen", urlopen):
            with self.assertNoLogs("morpheus.push", level="WARNING"):
                ok = push.send_push("anything", settings=_settings(ntfy_topic=""))
        self.assertFalse(ok)
        urlopen.assert_not_called()

    def test_network_failure_returns_false_and_warns_once_per_streak(self):
        boom = MagicMock(side_effect=urllib.error.URLError("no route"))
        with patch("morpheus.push.urllib.request.urlopen", boom):
            with self.assertLogs("morpheus.push", level="WARNING") as cm:
                self.assertFalse(push.send_push("one", settings=_settings()))
                self.assertFalse(push.send_push("two", settings=_settings()))
        self.assertEqual(len(cm.output), 1)  # once per streak, not per send
        # The warning names the server, never the (capability-URL) topic.
        self.assertIn("ntfy.sh", cm.output[0])
        self.assertNotIn("morpheus-abc123", cm.output[0])

        # A success resets the streak so the next failure warns again.
        with patch("morpheus.push.urllib.request.urlopen", self._urlopen_ok()):
            self.assertTrue(push.send_push("back", settings=_settings()))
        with patch("morpheus.push.urllib.request.urlopen", boom):
            with self.assertLogs("morpheus.push", level="WARNING") as cm:
                self.assertFalse(push.send_push("down again", settings=_settings()))
        self.assertEqual(len(cm.output), 1)

    def test_http_error_returns_false_without_raising(self):
        boom = MagicMock(side_effect=urllib.error.HTTPError(
            "https://ntfy.sh/morpheus-abc123", 403, "forbidden", {}, None))
        with patch("morpheus.push.urllib.request.urlopen", boom):
            with self.assertLogs("morpheus.push", level="WARNING"):
                self.assertFalse(push.send_push("nope", settings=_settings()))

    def test_none_settings_resolve_from_config(self):
        urlopen = self._urlopen_ok()
        with patch("morpheus.config.omni_settings",
                   return_value=_settings(ntfy_topic="from-config")), \
                patch("morpheus.push.urllib.request.urlopen", urlopen):
            self.assertTrue(push.send_push("hello"))
        req = urlopen.call_args.args[0]
        self.assertEqual(req.full_url, "https://ntfy.sh/from-config")


if __name__ == "__main__":
    unittest.main()
