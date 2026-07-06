"""Phone-push escalation via ntfy (PRD §3.1 notification mirroring).

The Even app mirrors phone notifications onto the G2 glasses as wake-capable
pop-ups, so a high-priority feed push should ALSO fire a phone push — the
glasses then light up even when the display sleeps. Everything else stays
laptop-local (notifier.py owns the macOS desktop banners; this module owns
only the ntfy escalation channel).

Privacy: escalated headlines leave the tailnet via the configured ntfy
server and the Apple/Google push infrastructure. The config template carries
the matching warning; keep escalation reserved for genuinely urgent items or
self-host ntfy.

Design contract: :func:`send_push` NEVER raises and never blocks the caller
for long (5s timeout). Failures are swallowed to a warning on the
``morpheus.push`` logger — once per consecutive-failure streak, same pattern
as the judge-failure logging in feeds.py — and reported as ``False``.
"""

from __future__ import annotations

import logging
import urllib.request
from typing import Mapping, Optional

_log = logging.getLogger("morpheus.push")

SEND_TIMEOUT_SECONDS = 5.0

# True while the last send failed, so a broken network/server logs one
# warning per streak instead of one per escalation. Per-process state: a
# fresh process re-logs once, which is fine.
_send_failing = False


def send_push(title: str, body: str = "", *, priority: int = 3,
              settings: Optional[Mapping] = None) -> bool:
    """POST one phone push to the configured ntfy topic. Returns True on
    success, False otherwise — it NEVER raises.

    ntfy conventions: the notification text travels as the request body, the
    app identity as the ``Title`` header, and ``priority > 0`` (the default)
    maps to the ntfy ``Priority: high`` header so the Even app treats the
    mirrored notification as wake-worthy. A non-positive priority sends a
    normal-priority push.

    No-op returning False when ``[omni].ntfy_topic`` is empty (escalation
    off) — that is a configuration state, not a failure, so it never warns.
    """
    global _send_failing
    if settings is None:
        try:
            from morpheus import config
            settings = config.omni_settings()
        except Exception:
            return False
    topic = str(settings.get("ntfy_topic") or "").strip()
    if not topic:
        return False  # escalation off by configuration — quiet no-op
    server = str(settings.get("ntfy_server") or "").strip() or "https://ntfy.sh"
    url = f"{server.rstrip('/')}/{topic}"
    try:
        message = "\n".join(
            part for part in ((title or "").strip(), (body or "").strip()) if part)
        headers = {"Title": "Morpheus"}
        if int(priority) > 0:
            headers["Priority"] = "high"
        req = urllib.request.Request(
            url,
            data=(message or "Morpheus").encode("utf-8"),
            headers=headers,
            method="POST",
        )
        # urlopen raises on HTTP >= 400, so reaching here means delivered.
        with urllib.request.urlopen(req, timeout=SEND_TIMEOUT_SECONDS):
            pass
    except Exception as exc:
        if not _send_failing:
            _send_failing = True
            # Log the server, never the topic — it is a capability URL.
            _log.warning(
                "phone push via %s failed (%s); staying quiet until a send "
                "succeeds", server, exc)
        return False
    _send_failing = False
    return True
