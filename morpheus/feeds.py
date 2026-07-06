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
* ``on_threshold``— scored 0-1 by the omnipresence relevance judge (LLM via
                    provider CLI, see judge.py) against the user's memory file
                    and current context signals; posted only when the score
                    clears the rule's threshold (or the [omni] default when the
                    rule threshold is 0). Guarded by the omni enable flag,
                    quiet hours, a per-feed hourly push cap, and 6h dedupe.

Sources are identified by ``(source_kind, source_ref)`` — today ``loop``/loop-id
and ``manual``; the schema is deliberately generic so future sources (``email``,
``sensor``, ``agent``, ``state``) plug in without migration.

Storage lives in the same SQLite file as everything else (``db._connect``), so
the CLI, daemon, loop-runner, and desktop bridge all see one feed.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from morpheus import db as _db
from morpheus.db import _connect

_log = logging.getLogger("morpheus.feeds")

DEFAULT_FEED = "main"
POLICIES = ("always", "on_change", "on_match", "on_failure", "on_threshold")
# on_threshold guard windows (PRD §3.5): the push budget is per trailing hour,
# dedupe looks back six hours.
PUSH_CAP_WINDOW_SECONDS = 3600.0
DEDUPE_WINDOW_SECONDS = 6 * 3600.0
# A judged loop that found nothing prints exactly this (see omni_templates);
# it is a healthy no-op, never a candidate — skipping it keeps the run free.
NOTHING_SENTINEL = "NOTHING"
# Agents sometimes narrate the sentinel instead of printing it ("no
# 'location' signals yet.") — a meta-summary, not a find. Judging it every
# 5 minutes wastes tokens, so summaries that *start* with a no-X-yet shape
# are treated like NOTHING and skipped before the judge.
_NO_FIND_RE = re.compile(
    r"^no\b.{0,40}\b(signals?|finds?|updates?|results?)\b", re.IGNORECASE)
# Push acknowledgements from a display client (G2 glasses tap/double-tap):
# "expanded" is a positive relevance signal, "dismissed" a negative one. The
# omnipresence memory-updater loop mines these via recent_acks().
ACK_ACTIONS = ("expanded", "dismissed")
# feed_acks is pruned to this many newest rows on every insert, so a chatty
# (or hostile) display client can never grow the table without bound.
ACKS_MAX_ROWS = 5000
# Stored titles are stripped and truncated; evaluate() must normalize candidate
# summaries with the *same* recipe or a long unchanged summary would look
# "changed" on every run. One constant + one helper so they cannot drift.
TITLE_MAX_CHARS = 200

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
    threshold   REAL NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS feed_rules_src_idx ON feed_rules(source_kind, source_ref);

CREATE TABLE IF NOT EXISTS feed_acks (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL,
    action  TEXT NOT NULL,
    ts      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS feed_acks_item_idx ON feed_acks(item_id, ts DESC);
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
    # on_threshold only: judge score needed to post. 0 = "use the [omni]
    # default", so tuning the config threshold retunes untouched rules.
    threshold: float = 0.0


@dataclass
class FeedAck:
    id: int
    item_id: int
    action: str
    ts: float


_initialized_db_paths: set[str] = set()


def _init() -> None:
    # The DDL is idempotent but not free, and _init() runs on hot paths
    # (per-evaluate, per-SSE-tick). Memoize per resolved database path — not
    # globally — because tests repoint db.DB_PATH at per-case temp dirs and
    # each fresh file still needs its schema created.
    key = str(_db.DB_PATH)
    if key in _initialized_db_paths:
        return
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        # CREATE TABLE IF NOT EXISTS never alters an existing table, so
        # databases created before the on_threshold policy lack the threshold
        # column. Same one-shot migration idea as db._ensure_column, and it
        # rides the per-path memoization above so it costs one PRAGMA per
        # process per database.
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(feed_rules)")}
        if "threshold" not in cols:
            conn.execute("ALTER TABLE feed_rules ADD COLUMN threshold REAL NOT NULL DEFAULT 0")
    _initialized_db_paths.add(key)


def _normalize_title(text: str) -> str:
    """The canonical shape of a stored feed-item title: stripped, truncated to
    ``TITLE_MAX_CHARS``, re-stripped (truncation can expose trailing space).
    post() stores this shape; on_change comparisons must use the same one."""
    return (text or "").strip()[:TITLE_MAX_CHARS].strip()


# ── items ────────────────────────────────────────────────────────────────


def post(title: str, body: str = "", *, source_kind: str = "manual",
         source_ref: str = "", priority: int = 0, feed: str = DEFAULT_FEED,
         metadata: Optional[dict] = None) -> int:
    """Append one condensed item to a feed. Returns the item id."""
    title = _normalize_title(title)
    if not title:
        raise ValueError("feed item needs a title")
    _init()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO feed_items (feed, ts, title, body, source_kind, source_ref, priority, metadata)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (feed, time.time(), title, body, source_kind, source_ref,
             int(priority), json.dumps(metadata or {})),
        )
        return cur.lastrowid


# Filter applied when a consumer asks to hide items the user already
# dismissed on a display client: any 'dismissed' ack hides the item (a later
# 'expanded' does not resurrect it — the user has already seen it).
_EXCLUDE_DISMISSED_SQL = (
    " AND id NOT IN (SELECT item_id FROM feed_acks WHERE action = 'dismissed')"
)


def recent(limit: int = 50, *, feed: str = DEFAULT_FEED,
           since_id: int = 0, exclude_dismissed: bool = False) -> list[FeedItem]:
    _init()
    q = "SELECT * FROM feed_items WHERE feed = ? AND id > ?"
    if exclude_dismissed:
        q += _EXCLUDE_DISMISSED_SQL
    with _connect() as conn:
        rows = conn.execute(
            q + " ORDER BY ts DESC, id DESC LIMIT ?",
            (feed, since_id, limit),
        ).fetchall()
    return [_item(r) for r in rows]


def recent_after(since_id: int, limit: int = 50, *, feed: str = DEFAULT_FEED,
                 exclude_dismissed: bool = False) -> list[FeedItem]:
    """Cursor fetch for streaming consumers: the *oldest* ``limit`` items with
    id > ``since_id``, ascending. A burst larger than ``limit`` then arrives
    across successive polls instead of being skipped forever; ``recent()``
    stays newest-first for display callers."""
    _init()
    q = "SELECT * FROM feed_items WHERE feed = ? AND id > ?"
    if exclude_dismissed:
        q += _EXCLUDE_DISMISSED_SQL
    with _connect() as conn:
        rows = conn.execute(
            q + " ORDER BY id ASC LIMIT ?",
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


# ── acks ─────────────────────────────────────────────────────────────────


def record_ack(item_id: int, action: str) -> int:
    """Record a display client's reaction to one pushed item. Returns the ack id.

    ``expanded`` (single tap) and ``dismissed`` (double tap) are the only
    actions; anything else is a bug in the client, so it raises.
    """
    action = (action or "").strip().lower()
    if action not in ACK_ACTIONS:
        raise ValueError(f"action must be one of {ACK_ACTIONS}")
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        raise ValueError("item_id must be an integer feed item id")
    if item_id <= 0:
        raise ValueError("item_id must be a positive feed item id")
    _init()
    with _connect() as conn:
        # Bound the id against reality: a client can only ack items that
        # exist, so a buggy/hostile bridge cannot seed acks for future ids.
        row = conn.execute("SELECT MAX(id) AS m FROM feed_items").fetchone()
        latest = int(row["m"] or 0)
        if item_id > latest:
            raise ValueError(
                f"item_id {item_id} does not exist (latest feed item is {latest})")
        cur = conn.execute(
            "INSERT INTO feed_acks (item_id, action, ts) VALUES (?, ?, ?)",
            (item_id, action, time.time()),
        )
        ack_id = cur.lastrowid
        # Keep only the newest ACKS_MAX_ROWS acks. Ids are AUTOINCREMENT, so
        # "newest 5000" is a single indexed range delete.
        conn.execute("DELETE FROM feed_acks WHERE id <= ?", (ack_id - ACKS_MAX_ROWS,))
        return ack_id


def recent_acks(limit: int = 50) -> list[FeedAck]:
    """Newest acks first — the memory-updater loop's relevance-signal input."""
    _init()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM feed_acks ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    return [FeedAck(id=r["id"], item_id=r["item_id"], action=r["action"], ts=r["ts"])
            for r in rows]


# ── rules ────────────────────────────────────────────────────────────────


def set_rule(source_kind: str, source_ref: str, *, policy: str = "always",
             pattern: str = "", threshold: float = 0.0,
             feed: str = DEFAULT_FEED) -> FeedRule:
    """Create or replace the rule for one source. One rule per source keeps the
    mental model simple: 'this loop pushes to the feed when <policy>'."""
    if policy not in POLICIES:
        raise ValueError(f"policy must be one of {POLICIES}")
    if policy == "on_match":
        re.compile(pattern)  # validate now, not at evaluation time
    if policy == "on_threshold":
        try:
            threshold = float(threshold)
        except (TypeError, ValueError):
            raise ValueError("threshold must be a number in [0, 1]")
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be in [0, 1] (0 = use the [omni] default)")
    else:
        threshold = 0.0
    _init()
    with _connect() as conn:
        conn.execute(
            "DELETE FROM feed_rules WHERE feed = ? AND source_kind = ? AND source_ref = ?",
            (feed, source_kind, source_ref),
        )
        cur = conn.execute(
            "INSERT INTO feed_rules (feed, source_kind, source_ref, policy, pattern, threshold, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (feed, source_kind, source_ref, policy, pattern, threshold, time.time()),
        )
        rid = cur.lastrowid
    return FeedRule(id=rid, feed=feed, source_kind=source_kind,
                    source_ref=source_ref, policy=policy, pattern=pattern,
                    threshold=threshold, created_at=time.time())


def rules(*, source_kind: str = "", source_ref: str = "",
          feed: Optional[str] = DEFAULT_FEED) -> list[FeedRule]:
    """List rules, filtered by source and feed. ``feed=None`` means *all*
    feeds (used by the `morpheus feeds` CLI); the default stays the main feed
    so existing callers see no behavior change."""
    _init()
    q = "SELECT * FROM feed_rules WHERE 1=1"
    args: list[Any] = []
    if feed is not None:
        q += " AND feed = ?"
        args.append(feed)
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
                     pattern=r["pattern"], threshold=float(r["threshold"] or 0.0),
                     created_at=r["created_at"]) for r in rows]


def delete_rule(rule_id: int) -> bool:
    _init()
    with _connect() as conn:
        cur = conn.execute("DELETE FROM feed_rules WHERE id = ?", (rule_id,))
        return cur.rowcount > 0


# ── routing ──────────────────────────────────────────────────────────────


def _last_posted_title(source_kind: str, source_ref: str, feed: str) -> str:
    _init()
    with _connect() as conn:
        row = conn.execute(
            "SELECT title FROM feed_items WHERE feed = ? AND source_kind = ?"
            " AND source_ref = ? ORDER BY id DESC LIMIT 1",
            (feed, source_kind, source_ref),
        ).fetchone()
    return row["title"] if row else ""


def evaluate(rule: FeedRule, *, summary: str, failed: bool) -> bool:
    """Does this result pass the rule's threshold?

    Pure and cheap by contract: ``on_threshold`` is deliberately *not*
    evaluated here (it needs config, guards, and an LLM call) — it returns
    False and route_loop_run() sends it through the judged path instead.
    """
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
        # Compare the candidate in the exact shape post() stores titles in
        # (normalizing the stored side too, for rows written by older code).
        candidate = _normalize_title(summary)
        return candidate != _normalize_title(_last_posted_title(
            rule.source_kind, rule.source_ref, rule.feed))
    return False


def route_loop_run(loop, run) -> Optional[int]:
    """Called after every loop run: push the result into feeds whose rules pass.

    For the classic policies, failures are always posted with priority 1 when
    a rule exists, so a broken watcher never goes silently quiet. Judged
    (``on_threshold``) rules feed an ambient surface instead — they never
    force-post failures (fail closed, logged) and route through
    ``_route_on_threshold``. Returns the posted item id (last one if multiple
    feeds), or None if nothing matched.
    """
    _init()
    summary = (run.summary or "").strip() or f"loop run {run.status}"
    failed = (run.status or "").lower() not in ("ok", "success", "succeeded", "0", "")
    posted: Optional[int] = None
    # feed=None: consult rules on EVERY feed — a rule routing this loop to a
    # non-default feed (e.g. the configured [omni] feed) must still fire.
    for rule in rules(source_kind="loop", source_ref=str(loop.id), feed=None):
        if rule.policy == "on_threshold":
            item_id = _route_on_threshold(rule, loop, run, summary, failed)
            posted = item_id if item_id is not None else posted
            continue
        if failed or evaluate(rule, summary=summary, failed=failed):
            # Deliberately NO phone escalation on this path — including the
            # priority-1 failure force-posts: a flapping watcher retrying
            # every minute would spam the phone. Escalation is judged-path
            # only (see _escalate_if_urgent).
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


# ── judged routing (on_threshold, PRD §3.5) ──────────────────────────────


# Sources whose last judge call failed — so the failure is logged once per
# streak, not once per run (a broken judge CLI would otherwise log every
# minute). Per-process state: a fresh process re-logs once, which is fine.
_judge_failing_sources: set[tuple[str, str, str]] = set()


def _omni_settings() -> dict:
    """Resolved [omni] settings. A wrapper so tests can inject settings
    without touching ~/.morpheus/config.toml."""
    from morpheus import config
    return config.omni_settings()


def _judge_item(title: str, body: str, *, memory_text: str,
                context_lines: list[str], judge_command: str):
    """Run the relevance judge (judge.score_item). A module-level seam so
    tests can fake the judge without spawning any subprocess."""
    from morpheus import judge
    return judge.score_item(title, body, memory_text=memory_text,
                            context_lines=context_lines,
                            judge_command=judge_command)


def _context_lines() -> list[str]:
    """Compact one-line-per-kind rendering of the latest context signals."""
    from morpheus import signals
    lines: list[str] = []
    for sig in signals.latest_per_kind():
        age_min = max(0, int((time.time() - sig.ts) / 60))
        payload = {k: v for k, v in sig.payload.items() if k != "ts"}
        body = " ".join(
            f"{k}={json.dumps(payload[k], ensure_ascii=False)}"
            for k in sorted(payload)
        )
        lines.append(f"- {sig.kind} ({age_min}m ago): {body[:200] or '(empty)'}")
    return lines


def _posted_count_since(feed: str, since_ts: float) -> int:
    _init()
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM feed_items WHERE feed = ? AND ts > ?",
            (feed, since_ts),
        ).fetchone()
    return int(row["n"] or 0)


def _is_duplicate_title(feed: str, title: str, since_ts: float) -> bool:
    _init()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT title FROM feed_items WHERE feed = ? AND ts > ?",
            (feed, since_ts),
        ).fetchall()
    return any(_normalize_title(r["title"]) == title for r in rows)


def _route_on_threshold(rule: FeedRule, loop, run, summary: str,
                        failed: bool) -> Optional[int]:
    """The judged path: cheap guards first, then the LLM judge, then post.

    Returns the posted item id or None. Judge failure = no push (fail
    closed), logged on the first failure of a streak so a broken judge CLI
    does not spam the log every run.
    """
    source_key = (rule.feed, "loop", str(loop.id))
    if failed:
        # An ambient feed never force-posts loop failures; the failure stays
        # visible in `morpheus loops list` / notes like any other run.
        return None
    candidate = _normalize_title(summary)
    if (not candidate or candidate.upper() == NOTHING_SENTINEL
            or _NO_FIND_RE.match(candidate)):
        return None  # healthy "nothing relevant" run — free by design
    settings = _omni_settings()
    if not settings.get("enabled"):
        return None
    from morpheus import config
    if config.is_quiet_now(settings.get("quiet_hours")):
        return None
    now = time.time()
    try:
        cap = int(settings.get("push_per_hour") or 0)
    except (TypeError, ValueError):
        cap = 0
    if cap <= 0:
        # push_per_hour = 0 means ZERO pushes (mute), never "unlimited".
        return None
    if _posted_count_since(rule.feed, now - PUSH_CAP_WINDOW_SECONDS) >= cap:
        return None  # push budget spent; drop quietly
    if _is_duplicate_title(rule.feed, candidate, now - DEDUPE_WINDOW_SECONDS):
        return None

    from morpheus import memory
    try:
        memory_text = memory.top_entries()
    except Exception:
        memory_text = ""
    try:
        context_lines = _context_lines()
    except Exception:
        context_lines = []
    body = f"loop [{loop.name}] · status={run.status}"
    try:
        verdict = _judge_item(candidate, body, memory_text=memory_text,
                              context_lines=context_lines,
                              judge_command=str(settings.get("judge_command") or ""))
    except Exception:
        verdict = None
    if verdict is None:
        if source_key not in _judge_failing_sources:
            _judge_failing_sources.add(source_key)
            _log.warning(
                "relevance judge failed for loop #%s (feed %s); "
                "failing closed — nothing pushed", loop.id, rule.feed)
        return None
    _judge_failing_sources.discard(source_key)
    threshold = rule.threshold if rule.threshold > 0 else float(settings.get("threshold") or 0.0)
    if verdict.score < threshold:
        return None
    posted_priority = 0
    item_id = post(
        candidate,
        body=body,
        source_kind="loop",
        source_ref=str(loop.id),
        priority=posted_priority,
        feed=rule.feed,
        metadata={
            "loop_id": loop.id,
            "run_id": run.id,
            "status": run.status,
            "judge": {"score": verdict.score, "rationale": verdict.rationale},
        },
    )
    # Escalation strictly FOLLOWS the successful feed post and never blocks
    # it: a failed phone push changes nothing about the item above.
    _escalate_if_urgent(candidate, verdict.score, posted_priority, settings)
    return item_id


def _escalate_if_urgent(title: str, score: float, priority: int,
                        settings: Mapping) -> None:
    """Phone-push escalation (PRD §3.1 notification mirroring).

    THE RULE, deliberately conservative: a phone push fires ONLY after a
    successful judged (on_threshold) feed post, and only when the judge
    score clears ``[omni].escalate_score`` OR the posted item carries
    priority > 0. Nothing else escalates — in particular the classic-policy
    failure force-posts in route_loop_run never reach the phone, because a
    flapping watcher retrying every minute would spam push notifications.
    Send failures are swallowed inside push.send_push (and belt-and-braces
    here), so escalation can never break routing.
    """
    if not str(settings.get("ntfy_topic") or "").strip():
        return  # escalation off — do not even import/call the sender
    try:
        escalate_score = min(1.0, max(0.0, float(settings.get("escalate_score"))))
    except (TypeError, ValueError):
        return  # unset/garbage escalate_score: fail closed, no escalation
    if score < escalate_score and priority <= 0:
        return
    from morpheus import push
    try:
        push.send_push(title, settings=settings)
    except Exception:  # pragma: no cover — send_push never raises by contract
        _log.debug("escalation send_push raised; ignored", exc_info=True)
