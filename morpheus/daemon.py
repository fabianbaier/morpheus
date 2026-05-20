"""launchd integration — make morpheus an always-on background watcher.

Renders a LaunchAgent plist at ~/Library/LaunchAgents/com.morpheus.watch.plist
that runs `morpheus watch` continuously. Auto-starts at login, auto-restarts
on crash. Logs to ~/.morpheus/daemon.log. Writes a beacon file every tick
so we can detect a hung daemon.

The daemon and the foreground dashboard can coexist — both share state via
SQLite. The daemon's job is to keep things ticking when the dashboard isn't
open.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

LAUNCH_AGENT_LABEL = "com.morpheus.watch"
LAUNCH_AGENT_DIR = Path.home() / "Library" / "LaunchAgents"
LAUNCH_AGENT_PATH = LAUNCH_AGENT_DIR / f"{LAUNCH_AGENT_LABEL}.plist"

MORPHEUS_DIR = Path.home() / ".morpheus"
BEACON_PATH = MORPHEUS_DIR / "daemon.beacon"
DAEMON_LOG = MORPHEUS_DIR / "daemon.log"


@dataclass
class DaemonStatus:
    plist_installed: bool
    launchctl_loaded: bool
    pid: Optional[int]
    beacon_exists: bool
    beacon_age_secs: Optional[float]
    log_size_bytes: int
    program_path: Optional[str]


def write_beacon() -> None:
    """Call from the tick loop to prove the daemon is alive."""
    MORPHEUS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        BEACON_PATH.write_text(str(time.time()))
    except Exception:
        pass


def find_morpheus_binary() -> Optional[str]:
    """Locate the `morpheus` CLI executable, preferring the active venv."""
    # 1. Active venv's bin/
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        cand = Path(venv) / "bin" / "morpheus"
        if cand.exists():
            return str(cand)
    # 2. PATH lookup
    found = shutil.which("morpheus")
    if found:
        return found
    return None


def _plist_xml(morpheus_path: str, poll: float = 5.0) -> str:
    home = str(Path.home())
    log_path = str(DAEMON_LOG)
    # Make sure the PATH includes common locations for terminal-notifier, gh, etc.
    path_env = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTD/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCH_AGENT_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{morpheus_path}</string>
        <string>watch</string>
        <string>--poll</string>
        <string>{poll:g}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
    <key>WorkingDirectory</key>
    <string>{home}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{path_env}</string>
        <key>HOME</key>
        <string>{home}</string>
    </dict>
</dict>
</plist>
"""


def _run(args: list[str]) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=10)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


def install(poll: float = 5.0, morpheus_path: Optional[str] = None) -> tuple[bool, str]:
    """Install + load the LaunchAgent. Returns (ok, message)."""
    path = morpheus_path or find_morpheus_binary()
    if not path:
        return False, (
            "couldn't locate the `morpheus` binary. Activate your venv first "
            "(or pip install -e . in this dir) so `which morpheus` resolves."
        )
    LAUNCH_AGENT_DIR.mkdir(parents=True, exist_ok=True)
    MORPHEUS_DIR.mkdir(parents=True, exist_ok=True)

    # If already loaded, unload first to pick up new path/poll.
    if _is_loaded():
        _run(["launchctl", "unload", str(LAUNCH_AGENT_PATH)])

    LAUNCH_AGENT_PATH.write_text(_plist_xml(path, poll=poll))

    rc, _, err = _run(["launchctl", "load", "-w", str(LAUNCH_AGENT_PATH)])
    if rc != 0:
        return False, f"launchctl load failed: {err or '(no message)'}"
    return True, (
        f"installed → {LAUNCH_AGENT_PATH}\n"
        f"watching every {poll:g}s · logs at {DAEMON_LOG}\n"
        f"check with `morpheus daemon-status`"
    )


def uninstall() -> tuple[bool, str]:
    """Unload + remove the LaunchAgent."""
    if not LAUNCH_AGENT_PATH.exists() and not _is_loaded():
        return True, "daemon not installed (nothing to do)."
    if _is_loaded():
        rc, _, err = _run(["launchctl", "unload", str(LAUNCH_AGENT_PATH)])
        if rc != 0 and "not find" not in err.lower():
            return False, f"launchctl unload failed: {err or '(no message)'}"
    if LAUNCH_AGENT_PATH.exists():
        try:
            LAUNCH_AGENT_PATH.unlink()
        except Exception as e:
            return False, f"couldn't remove plist: {e}"
    return True, "daemon uninstalled."


def _is_loaded() -> bool:
    rc, out, _ = _run(["launchctl", "list", LAUNCH_AGENT_LABEL])
    return rc == 0 and LAUNCH_AGENT_LABEL in out


def _get_pid() -> Optional[int]:
    rc, out, _ = _run(["launchctl", "list", LAUNCH_AGENT_LABEL])
    if rc != 0:
        return None
    # Output format: "PID	STATUS	LABEL" then a verbose dict; PID is the first
    # whitespace-separated token of the first line.
    first = (out.splitlines() or [""])[0]
    parts = first.split()
    if parts and parts[0].isdigit():
        return int(parts[0])
    # Fallback: parse the dict-style output.
    for line in out.splitlines():
        line = line.strip()
        if line.startswith('"PID"') and "=" in line:
            try:
                return int(line.split("=", 1)[1].strip().rstrip(";").strip())
            except Exception:
                pass
    return None


def status() -> DaemonStatus:
    pid = _get_pid() if _is_loaded() else None
    beacon_age = None
    if BEACON_PATH.exists():
        try:
            ts = float(BEACON_PATH.read_text().strip())
            beacon_age = max(0.0, time.time() - ts)
        except Exception:
            pass
    program_path: Optional[str] = None
    if LAUNCH_AGENT_PATH.exists():
        text = LAUNCH_AGENT_PATH.read_text()
        # Cheap extraction of first <string>…</string> after ProgramArguments.
        marker = "<key>ProgramArguments</key>"
        if marker in text:
            tail = text.split(marker, 1)[1]
            for chunk in tail.split("<string>")[1:]:
                program_path = chunk.split("</string>")[0]
                break
    log_size = 0
    if DAEMON_LOG.exists():
        try:
            log_size = DAEMON_LOG.stat().st_size
        except Exception:
            pass
    return DaemonStatus(
        plist_installed=LAUNCH_AGENT_PATH.exists(),
        launchctl_loaded=_is_loaded(),
        pid=pid,
        beacon_exists=BEACON_PATH.exists(),
        beacon_age_secs=beacon_age,
        log_size_bytes=log_size,
        program_path=program_path,
    )
