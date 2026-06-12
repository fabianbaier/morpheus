"""Feeds — a condensed, subscribable stream of high-level updates.

A feed is an aggregator: loops (and later notes, emails, sensors, awareness
signals) push short headline-style items into it, and any client — the desktop
app, a phone, AR glasses — subscribes to one terminal stream of "what matters
right now" instead of watching every session.

What routes into a feed is decided by **rules** with thresholds:

* ``always``      — every result from the source is posted.
* ``on_change``   — posted only when the summary differs from the last posted
                    item for that source (quiet when nothing new).
* ``on_match``    — posted only when the result matches a regex pattern
                    (e.g. ``error|breaking|>\\s*9000``).
* ``on_failure``  — posted only when the source run failed.

Sources are identified by ``(source_kind, source_ref)`` — today ``loop``/loop-id
and ``manual``; the schema is deliberately generic so future sources (``email``,
``sensor``, ``agent``, ``state``) plug in without migration.

Storage lives in the same SQLite file as everything else (``db._connect``), so
the CLI, daemon, loop-runner, and desktop bridge all see one feed.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from morpheus.db import _connect

DEFAULT_FEED = "main"
POLICIES = ("always", "on_change", "on_match", "on_failure")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS feed_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    feed        TEXT NOT NULL DEFAULT 'main',
    ts          REAL NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT 'manual',
    source_ref  TEXT NOT NULL DEFAULT '',
    priority    INTEGER NOT NULL DEFAULT 0,
    metadata    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS feed_items_ts_idx ON feed_items(feed, ts DESC);
CREATE INDEX IF NOT EXISTS feed_items_src_idx ON feed_items(source_kind, source_ref, ts DESC);

CREATE TABLE IF NOT EXISTS feed_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    feed        TEXT NOT NULL DEFAULT 'main',
    source_kind TEXT NOT NULL,
    source_ref  TEXT NOT NULL DEFAULT '',
    policy      TEXT NOT NULL DEFAULT 'always',
    pattern     TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS feed_rules_src_idx ON feed_rules(source_kind, source_ref);
"""


@dataclass
class FeedItem:
    id: int
    feed: str
    ts: float
    title: str
    body: str
    source_kind: str
    source_ref: str
    priority: int
    metadata: dict = field(default_factory=dict)


@dataclass
class FeedRule:
    id: int
    feed: str
    source_kind: str
    source_ref: str
    policy: str
    pattern: str
    created_at: float


def _init() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)


# ── items ────────────────────────────────────────────────────────────────


def post(title: str, body: str = "", *, source_kind: str = "manual",
         source_ref: str = "", priority: int = 0, feed: str = DEFAULT_FEED,
         metadata: Optional[dict] = None) -> int:
    """Append one condensed item to a feed. Returns the item id."""
    title = (title or "").strip()
    if not title:
        raise ValueError("feed item needs a title")
    _init()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO feed_items (feed, ts, title, body, source_kind, source_ref, priority, metadata)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (feed, time.time(), title[:200], body, source_kind, source_ref,
             int(priority), json.dumps(metadata or {})),
        )
        return cur.lastrowid


def recent(limit: int = 50, *, feed: str = DEFAULT_FEED,
           since_id: int = 0) -> list[FeedItem]:
    _init()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM feed_items WHERE feed = ? AND id > ?"
            " ORDER BY ts DESC, id DESC LIMIT ?",
            (feed, since_id, limit),
        ).fetchall()
    return [_item(r) for r in rows]


def latest_id(*, feed: str = DEFAULT_FEED) -> int:
    _init()
    with _connect() as conn:
        row = conn.execute(
            "SELECT MAX(id) AS m FROM feed_items WHERE feed = ?", (feed,)
        ).fetchone()
    return int(row["m"] or 0)


def render_text(limit: int = 20, *, feed: str = DEFAULT_FEED) -> str:
    """Ultra-condensed plain-text view — one line per item, newest first.

    This is the format a minimal client (AR glasses, a watch, `curl`) consumes:
    no JSON parsing required, just lines.
    """
    lines = []
    for it in recent(limit, feed=feed):
        stamp = time.strftime("%H:%M", time.localtime(it.ts))
        prefix = "! " if it.priority > 0 else ""
        lines.append(f"{stamp} {prefix}[{it.source_kind}] {it.title}")
    return "\n".join(lines)


def _item(r) -> FeedItem:
    try:
        meta = json.loads(r["metadata"] or "{}")
    except Exception:
        meta = {}
    return FeedItem(id=r["id"], feed=r["feed"], ts=r["ts"], title=r["title"],
                    body=r["body"], source_kind=r["source_kind"],
                    source_ref=r["source_ref"], priority=r["priority"], metadata=meta)


# ── rules ────────────────────────────────────────────────────────────────


def set_rule(source_kind: str, source_ref: str, *, policy: str = "always",
             pattern: str = "", feed: str = DEFAULT_FEED) -> FeedRule:
    """Create or replace the rule for one source. One rule per source keeps the
    mental model simple: 'this loop pushes to the feed when <policy>'."""
    if policy not in POLICIES:
        raise ValueError(f"policy must be one of {POLICIES}")
    if policy == "on_match":
        re.compile(pattern)  # validate now, not at evaluation time
    _init()
    with _connect() as conn:
        conn.execute(
            "DELETE FROM feed_rules WHERE feed = ? AND source_kind = ? AND source_ref = ?",
            (feed, source_kind, source_ref),
        )
        cur = conn.execute(
            "INSERT INTO feed_rules (feed, source_kind, source_ref, policy, pattern, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (feed, source_kind, source_ref, policy, pattern, time.time()),
        )
        rid = cur.lastrowid
    return FeedRule(id=rid, feed=feed, source_kind=source_kind,
                    source_ref=source_ref, policy=policy, pattern=pattern,
                    created_at=time.time())


def rules(*, source_kind: str = "", source_ref: str = "",
          feed: str = DEFAULT_FEED) -> list[FeedRule]:
    _init()
    q = "SELECT * FROM feed_rules WHERE feed = ?"
    args: list[Any] = [feed]
    if source_kind:
        q += " AND source_kind = ?"
        args.append(source_kind)
    if source_ref:
        q += " AND source_ref = ?"
        args.append(source_ref)
    with _connect() as conn:
        rows = conn.execute(q + " ORDER BY id", args).fetchall()
    return [FeedRule(id=r["id"], feed=r["feed"], source_kind=r["source_kind"],
                     source_ref=r["source_ref"], policy=r["policy"],
                     pattern=r["pattern"], created_at=r["created_at"]) for r in rows]


def delete_rule(rule_id: int) -> bool:
    _init()
    with _connect() as conn:
        cur = conn.execute("DELETE FROM feed_rules WHERE id = ?", (rule_id,))
        return cur.rowcount > 0


# ── routing ──────────────────────────────────────────────────────────────


def _last_posted_title(source_kind: str, source_ref: str, feed: str) -> str:
    with _connect() as conn:
        row = conn.execute(
            "SELECT title FROM feed_items WHERE feed = ? AND source_kind = ?"
            " AND source_ref = ? ORDER BY id DESC LIMIT 1",
            (feed, source_kind, source_ref),
        ).fetchone()
    return row["title"] if row else ""


def evaluate(rule: FeedRule, *, summary: str, failed: bool) -> bool:
    """Does this result pass the rule's threshold?"""
    if rule.policy == "always":
        return True
    if rule.policy == "on_failure":
        return failed
    if rule.policy == "on_match":
        try:
            return bool(re.search(rule.pattern, summary, re.IGNORECASE))
        except re.error:
            return False
    if rule.policy == "on_change":
        return summary.strip() != _last_posted_title(
            rule.source_kind, rule.source_ref, rule.feed).strip()
    return False


def route_loop_run(loop, run) -> Optional[int]:
    """Called after every loop run: push the result into feeds whose rules pass.

    Failures are always posted with priority 1 when a rule exists, so a broken
    watcher never goes silently quiet. Returns the posted item id (last one if
    multiple feeds), or None if nothing matched.
    """
    _init()
    summary = (run.summary or "").strip() or f"loop run {run.status}"
    failed = (run.status or "").lower() not in ("ok", "success", "succeeded", "0", "")
    posted: Optional[int] = None
    for rule in rules(source_kind="loop", source_ref=str(loop.id)):
        if failed or evaluate(rule, summary=summary, failed=failed):
            posted = post(
                summary,
                body=f"loop [{loop.name}] · status={run.status}",
                source_kind="loop",
                source_ref=str(loop.id),
                priority=1 if failed else 0,
                feed=rule.feed,
                metadata={"loop_id": loop.id, "run_id": run.id, "status": run.status},
            )
    return posted
