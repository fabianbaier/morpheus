"""Config loader for ~/.morpheus/config.toml.

Defaults are exhaustive — the on-disk file is purely overrides. First read
writes the defaults so the user sees the schema.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

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
"""
    CONFIG_PATH.write_text(text)


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
