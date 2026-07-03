"""Config loader for ~/.morpheus/config.toml.

Defaults are exhaustive — the on-disk file is purely overrides. First read
writes the defaults so the user sees the schema.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Optional

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore

CONFIG_DIR = Path.home() / ".morpheus"
CONFIG_PATH = CONFIG_DIR / "config.toml"


DEFAULTS: dict[str, Any] = {
    "general": {
        "poll_interval": 5.0,
        "stale_after_hours": 4.0,
        "log_level": "info",
    },
    "detection": {
        # User-defined patterns to ALSO match as blocked. (Plain Python regex.)
        "extra_blocked_patterns": [],
    },
    "notifications": {
        "enabled": True,
        # Any of: state, note, spawn, close, error, brief
        "silence_kinds": [],
        # 24h quiet ranges, e.g. ["22:00-07:00"].
        "quiet_hours": [],
    },
    "brief": {
        "schedule": ["08:00", "18:00"],
        "include_gh_queue": True,
        # Optional repo whitelist; empty = all repos you're a reviewer on.
        "gh_repos": [],
        "include_calendar": False,
    },
    "autonomy": {
        # Daily LLM spend cap in USD. Crossing it disables autonomy until tomorrow.
        "daily_dollar_cap": 5.00,
        # off | soft | full
        "permissions": "soft",
        # Action classes:
        "allowed_actions": ["poll", "summarize", "research", "draft"],
        "ask_first_actions": ["spawn", "kill", "delete"],
        "denied_actions": ["merge", "push", "approve", "external-message"],
    },
    "worktree": {
        # Warn when two or more LIVE sessions share the same cwd.
        "warn_on_collision": True,
    },
    "token_guard": {
        # When a session has been working continuously for warn_minutes,
        # fire a heads-up. snapshot_minutes is the louder "snapshot now" alert.
        "enabled": True,
        "warn_minutes": 60,
        "snapshot_minutes": 120,
    },
    "trigger": {
        # Auto-spawn draft codex sessions for new review-requested PRs.
        "spawn_from_gh_pr": False,
        "gh_poll_secs": 300,         # 5 min
        # Pre-loaded prompt for codex (or empty for a bare session).
        "draft_prompt": "/adversarial-review",
        "gh_repos": [],
    },
    "goal_loop": {
        "enabled": True,
        "cooldown_seconds": 120,
        "max_per_tick": 2,
    },
    "topic_watchers": {
        # List of {"name": "...", "query": "...", "interval_minutes": N}
        "watchers": [],
    },
    "colors": {
        "state_working":  "bright_green",
        "state_blocked":  "bold bright_red",
        "state_idle":     "bright_yellow",
        "state_crashed":  "bold bright_magenta",
        "state_finished": "color(244)",
        "flash_duration_secs": 3.0,
    },
    "intro": {
        "enabled": True,
        "mode": "default",            # default | short | cinematic
        "duration_seconds": 7.5,      # clamped to 5-24 seconds
        "geolocation": True,          # default-on; opt out with false or MORPHEUS_INTRO_GEO=0
        "location": "",              # optional "lat,lon,label" override
    },
    "omni": {
        # Omnipresence mode: ambient pushes to connected glasses (PRD §3.6).
        "enabled": False,             # opt-in; toggle with `morpheus omni on|off`
        "threshold": 0.7,             # relevance-judge score needed to push
        "push_per_hour": 6,           # push budget; 0 = zero pushes (mute), not unlimited
        "quiet_hours": "",           # "HH:MM-HH:MM"; empty = none (the default)
        "feed": "main",              # which feed omnipresence delivers
        "judge_command": "",         # empty = default `codex exec` (wave 2)
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict[str, Any]:
    """Load config from disk, merged on top of DEFAULTS.

    On first run, writes a commented default file so the schema is visible.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        _write_default()
        return DEFAULTS
    try:
        with open(CONFIG_PATH, "rb") as f:
            user = tomllib.load(f)
    except Exception:
        return DEFAULTS
    return _deep_merge(DEFAULTS, user)


def _write_default() -> None:
    """Render a TOML file with comments explaining each section."""
    text = f"""# Morpheus config — written by morpheus on first run.
# All keys are optional; defaults are baked in. Override only what you want.
# Reload by restarting the daemon (`morpheus uninstall-daemon && install-daemon`)
# or restarting `morpheus` dashboard.

[general]
poll_interval = {DEFAULTS['general']['poll_interval']}
stale_after_hours = {DEFAULTS['general']['stale_after_hours']}
log_level = "{DEFAULTS['general']['log_level']}"

[detection]
# Add Python regex strings matched against the trailing pane buffer to
# classify additional prompts as BLOCKED.
extra_blocked_patterns = []

[notifications]
enabled = true
# Silence specific kinds. Any of: state, note, spawn, close, error, brief
silence_kinds = []
# Time windows during which we suppress all notifications, e.g. ["22:00-07:00"]
quiet_hours = []

[brief]
# Times at which the daemon will run `morpheus brief --notify` automatically (v0.5+).
schedule = ["08:00", "18:00"]
include_gh_queue = true
# Optional: restrict GH queue to these repos. Empty = all repos you can review.
gh_repos = []

[autonomy]
# Daily LLM-spend cap (USD). Crossing it disables autonomous actions for the day.
daily_dollar_cap = 5.00
# off | soft | full
permissions = "soft"
allowed_actions   = ["poll", "summarize", "research", "draft"]
ask_first_actions = ["spawn", "kill", "delete"]
denied_actions    = ["merge", "push", "approve", "external-message"]

[worktree]
# Push a 🐇 alert + notification when two LIVE sessions share the same cwd.
warn_on_collision = true

[token_guard]
enabled = true
# After a session has been working continuously for N min, fire a warning.
warn_minutes = 60
# Louder "snapshot now" alert at N min.
snapshot_minutes = 120

[trigger]
# Auto-spawn a paused draft codex session for every new review-requested PR.
spawn_from_gh_pr = false
gh_poll_secs = 300
draft_prompt = "/adversarial-review"
gh_repos = []

[goal_loop]
# When enabled, the watcher/cockpit nudges idle autonomous goal controllers.
enabled = true
# Minimum seconds between continuation turns per goal controller.
cooldown_seconds = 120
# Maximum goal controllers to nudge on one watcher tick.
max_per_tick = 2

[topic_watchers]
# List of tables: each runs `claude -p` with web search on its interval.
# Example:
#   [[topic_watchers.watchers]]
#   name = "x402 protocol news"
#   query = "what's new with x402 protocol this week?"
#   interval_minutes = 1440
watchers = []

[intro]
# Cinematic Matrix boot animation before the dashboard.
enabled = true
# default | short | cinematic
mode = "default"
# Duration is clamped to 5-24 seconds. Env override: MORPHEUS_INTRO_SECONDS.
duration_seconds = 7.5
# Default-on IP geolocation for the Earth lock-on effect.
# Opt out with geolocation = false or MORPHEUS_INTRO_GEO=0.
geolocation = true
# Optional manual override: "lat,lon,label". Env override: MORPHEUS_INTRO_LOCATION.
location = ""

[omni]
# Omnipresence mode: ambient pushes to connected G2 glasses.
# Opt-in; toggle with `morpheus omni on|off`. Env override: MORPHEUS_OMNI_ENABLED.
enabled = false
# Relevance-judge score (0-1) a candidate item needs to be pushed.
threshold = 0.7
# Maximum pushes per hour. 0 means zero pushes (mute), NOT unlimited.
push_per_hour = 6
# Quiet hours as "HH:MM-HH:MM", e.g. "22:00-07:00". Empty = none (default off).
quiet_hours = ""
# Which feed omnipresence delivers to the glasses.
feed = "main"
# Judge runner command. Empty = default `codex exec`; e.g. "claude -p".
judge_command = ""
"""
    CONFIG_PATH.write_text(text)


# ── omnipresence ([omni]) ────────────────────────────────────────────────
#
# Same resolver pattern as intro.load_options(): config value on top of the
# baked-in default, with an env override for the boolean knob.

_QUIET_HOURS_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")
_TRUE_VALUES = {"1", "true", "yes", "on"}


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_VALUES
    return default


def _as_float(value: Any, default: float) -> float:
    try:
        if isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        if isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_quiet_hours(value: Any) -> Optional[dict[str, str]]:
    """Parse "HH:MM-HH:MM" into {"start", "end"}; empty/invalid → None (off)."""
    text = str(value or "").strip()
    if not text:
        return None
    match = _QUIET_HOURS_RE.match(text)
    if not match:
        return None
    h1, m1, h2, m2 = (int(g) for g in match.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        return None
    return {"start": f"{h1:02d}:{m1:02d}", "end": f"{h2:02d}:{m2:02d}"}


def is_quiet_now(quiet_hours: Optional[Mapping[str, str]],
                 now: Optional[time.struct_time] = None) -> bool:
    """Is the local time inside the parsed [omni] quiet-hours window?

    ``quiet_hours`` is the shape ``parse_quiet_hours`` returns: None (off) or
    ``{"start": "HH:MM", "end": "HH:MM"}``. Overnight ranges wrap midnight
    (22:00-08:00 covers 23:00 *and* 06:30). A zero-width range (start == end)
    is off, not always-on — that is what an untouched picker produces.
    """
    if not quiet_hours:
        return False
    try:
        sh, sm = (int(x) for x in str(quiet_hours["start"]).split(":"))
        eh, em = (int(x) for x in str(quiet_hours["end"]).split(":"))
    except (KeyError, TypeError, ValueError):
        return False
    start = sh * 60 + sm
    end = eh * 60 + em
    if start == end:
        return False
    if now is None:
        now = time.localtime()
    now_min = now.tm_hour * 60 + now.tm_min
    if start < end:
        return start <= now_min < end
    return now_min >= start or now_min < end


def omni_settings(
    cfg: Optional[Mapping[str, Any]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Return the resolved [omni] settings (PRD §3.6 controls).

    quiet_hours resolves to None (off, the default) or {"start", "end"}.
    """
    cfg = cfg if cfg is not None else load()
    env = env if env is not None else os.environ
    omni_cfg = cfg.get("omni", {}) if isinstance(cfg, Mapping) else {}
    if not isinstance(omni_cfg, Mapping):
        omni_cfg = {}
    defaults = DEFAULTS["omni"]

    enabled = _as_bool(omni_cfg.get("enabled"), defaults["enabled"])
    if "MORPHEUS_OMNI_ENABLED" in env:
        enabled = str(env.get("MORPHEUS_OMNI_ENABLED") or "").strip().lower() in _TRUE_VALUES

    threshold = _as_float(omni_cfg.get("threshold"), defaults["threshold"])
    threshold = min(1.0, max(0.0, threshold))
    push_per_hour = max(0, _as_int(omni_cfg.get("push_per_hour"), defaults["push_per_hour"]))
    return {
        "enabled": enabled,
        "threshold": threshold,
        "push_per_hour": push_per_hour,
        "quiet_hours": parse_quiet_hours(omni_cfg.get("quiet_hours", defaults["quiet_hours"])),
        "feed": str(omni_cfg.get("feed") or defaults["feed"]),
        "judge_command": str(omni_cfg.get("judge_command") or defaults["judge_command"]),
    }


def set_omni_enabled(enabled: bool) -> Path:
    """Persist [omni].enabled to config.toml with a minimal, content-preserving
    edit (there is no general config writer; the rest of the file — including
    the user's comments and other keys — is left byte-for-byte intact)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        _write_default()
    text = CONFIG_PATH.read_text()
    value = "true" if enabled else "false"
    lines = text.splitlines()
    section = ""
    omni_start = None
    omni_end = len(lines)
    for i, line in enumerate(lines):
        header = re.match(r"^\s*\[(.+?)\]\s*(#.*)?$", line)
        if header:
            if section == "omni" and omni_start is not None:
                omni_end = i
                break
            section = header.group(1).strip()
            if section == "omni":
                omni_start = i
    if omni_start is None:
        # No [omni] section yet — append one.
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["[omni]", f"enabled = {value}"])
    else:
        for i in range(omni_start + 1, omni_end):
            if re.match(r"^\s*enabled\s*=", lines[i]):
                lines[i] = f"enabled = {value}"
                break
        else:
            lines.insert(omni_start + 1, f"enabled = {value}")
    CONFIG_PATH.write_text("\n".join(lines) + ("\n" if text.endswith("\n") or lines else ""))
    return CONFIG_PATH


def in_quiet_hours(now_local: time.struct_time = None) -> bool:
    """Return True if the current local time falls inside any quiet-hours range."""
    cfg = load()
    ranges = cfg["notifications"].get("quiet_hours", [])
    if not ranges:
        return False
    if now_local is None:
        now_local = time.localtime()
    now_min = now_local.tm_hour * 60 + now_local.tm_min
    for r in ranges:
        try:
            lo, hi = r.split("-")
            lh, lm = (int(x) for x in lo.split(":"))
            hh, hm = (int(x) for x in hi.split(":"))
            lo_min = lh * 60 + lm
            hi_min = hh * 60 + hm
        except Exception:
            continue
        if lo_min <= hi_min:
            if lo_min <= now_min < hi_min:
                return True
        else:
            # Wraps midnight (e.g. 22:00-07:00).
            if now_min >= lo_min or now_min < hi_min:
                return True
    return False
