"""OS-agnostic domain layer for the Morpheus desktop app.

Every function here returns plain JSON-serialisable Python (dicts/lists/str/num)
so the HTTP layer can ``json.dumps`` it directly. The read surface and the
note/chat writes work anywhere (Linux CI included) because they only touch the
SQLite database via :mod:`morpheus.db`. The iTerm2 *control* operations
(spawn/send/broadcast keystrokes) are macOS-only; they degrade explicitly with a
structured ``{"ok": False, "error": ...}`` result instead of raising when iTerm2
is unavailable, so a request thread never crashes on a non-mac host.

Design notes (from the architecture review):
* We never cache a DB connection or the DB path — every call goes through the
  ``morpheus.db`` public functions, which re-read ``db.DB_PATH`` on each call.
  That keeps the ``patch.object(db, "DB_PATH", ...)`` test convention working.
* iTerm2 control ops are serialized by the caller (the server holds a lock); here
  we keep them small and best-effort.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

from morpheus import activity as activity_mod
from morpheus import ask as ask_mod
from morpheus import context as context_mod
from morpheus import db
from morpheus import ledger
from morpheus import naming

# ───────────────────────── helpers ─────────────────────────


def _emoji(state: str) -> str:
    return naming.STATE_EMOJI.get(state, naming.STATE_EMOJI["unknown"])


def _age(ts: float, now: Optional[float] = None) -> str:
    if not ts:
        return ""
    return naming.format_age((now or time.time()) - ts)


def _decode_paths(raw: str) -> list[str]:
    try:
        value = json.loads(raw or "[]")
        return [str(p) for p in value] if isinstance(value, list) else []
    except (ValueError, TypeError):
        return []


def attention_rank(state: str) -> int:
    """Sort key so blocked/crashed bubble to the top of the sidebar."""
    order = {"blocked": 0, "crashed": 1, "working": 2, "idle": 3, "unknown": 4, "finished": 5}
    return order.get(state, 4)


# ───────────────────────── read surface ─────────────────────────


def fleet(tenant_id: Optional[str] = None) -> dict[str, Any]:
    """The full cockpit snapshot: counts, sessions, goals, notes, spend.

    Reuses :func:`morpheus.context.build_json` for the session/goal/note shape so
    the desktop app and the existing context layer never disagree, then enriches
    each session with display fields (emoji, age, headline) the UI needs.
    """
    snapshot = context_mod.build_json(tenant_id=tenant_id)
    now = snapshot.get("generated_at", time.time())

    sessions = []
    for s in snapshot.get("sessions", []):
        act = s.get("activity") or {}
        headline = act.get("headline") or s.get("last_event") or ""
        sessions.append(
            {
                **s,
                "emoji": _emoji(s.get("state", "unknown")),
                "age": _age(s.get("buffer_changed_at") or s.get("last_event_at") or 0, now),
                "headline": headline,
                "attention": attention_rank(s.get("state", "unknown")),
            }
        )
    sessions.sort(key=lambda s: (s["attention"], -(s.get("buffer_changed_at") or 0)))

    counts = snapshot.get("counts", {})
    return {
        "generated_at": now,
        "counts": counts,
        "health": {
            "working": counts.get("working", 0),
            "idle": counts.get("idle", 0),
            "blocked": counts.get("blocked", 0),
            "crashed": counts.get("crashed", 0),
            "finished": counts.get("finished", 0),
            "total": sum(counts.values()),
        },
        "sessions": sessions,
        "goals": snapshot.get("goals", []),
        "notes": snapshot.get("notes", []),
        "spend": spend(),
        "iterm_available": iterm_available(),
    }


def sessions(tenant_id: Optional[str] = None) -> list[dict[str, Any]]:
    return fleet(tenant_id=tenant_id)["sessions"]


def _resolve_memory(ref: str) -> Optional[db.MissionMemory]:
    """Resolve a mission by mission_id, tab_id, or unambiguous prefix."""
    if not ref:
        return None
    mem = db.get_memory(ref)
    if mem is not None:
        return mem
    # Maybe it's a tab_id of a live session → map to its mission_id.
    live = db.get(ref)
    if live is not None and live.mission_id:
        mem = db.get_memory(live.mission_id)
        if mem is not None:
            return mem
    # Prefix match against all memories.
    candidates = [
        m
        for m in db.all_memory(include_archived=True)
        if m.mission_id.startswith(ref) or m.last_tab_id.startswith(ref)
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def mission_detail(ref: str, *, event_limit: int = 25, artifact_limit: int = 25,
                   edge_limit: int = 25) -> Optional[dict[str, Any]]:
    """Full Mission Card: durable memory + events + artifacts + graph edges."""
    mem = _resolve_memory(ref)
    if mem is None:
        return None
    mid = mem.mission_id
    live = next((m for m in db.all_missions() if m.mission_id == mid), None)
    events = db.recent_events(mid, limit=event_limit)
    artifacts = db.artifacts_for_mission(mid, limit=artifact_limit)
    edges = db.edges_for_id(mid, limit=edge_limit)

    return {
        "mission_id": mid,
        "title": mem.title,
        "why": mem.why,
        "done_definition": mem.done_definition,
        "acceptance_criteria": mem.acceptance_criteria,
        "current_plan": mem.current_plan,
        "next_step": mem.next_step,
        "last_decision": mem.last_decision,
        "last_summary": mem.last_summary,
        "blocked_on": mem.blocked_on,
        "phase": mem.phase,
        "confidence": mem.confidence,
        "topic": mem.topic,
        "agent_kind": mem.agent_kind,
        "source_kind": mem.source_kind,
        "source_ref": mem.source_ref,
        "claimed_paths": _decode_paths(mem.claimed_paths),
        "resume_command": mem.resume_command,
        "resume_confidence": mem.resume_confidence,
        "archived": mem.archived_at is not None,
        "created_at": mem.created_at,
        "updated_at": mem.updated_at,
        "live": _live_session_dict(live) if live else None,
        "events": [
            {
                "id": e.id,
                "ts": e.ts,
                "age": _age(e.ts),
                "kind": e.kind,
                "actor": e.actor,
                "summary": e.summary,
                "source_ref": e.source_ref,
            }
            for e in events
        ],
        "artifacts": [
            {
                "id": a.id,
                "kind": a.kind,
                "path_or_url": a.path_or_url,
                "status": a.status,
                "summary": a.summary,
                "created_at": a.created_at,
            }
            for a in artifacts
        ],
        "edges": [
            {
                "id": ed.id,
                "from_id": ed.from_id,
                "to_id": ed.to_id,
                "relation": ed.relation,
                "reason": ed.reason,
            }
            for ed in edges
        ],
    }


def _live_session_dict(m: db.Mission) -> dict[str, Any]:
    act = activity_mod.activities_by_tab().get(m.tab_id) or {}
    return {
        "tab_id": m.tab_id,
        "session_id": m.session_id,
        "state": m.state,
        "emoji": _emoji(m.state),
        "goal": m.goal,
        "cmd": m.cmd,
        "headline": act.get("headline", ""),
        "tail": act.get("tail", []),
    }


def goals(include_finished: bool = True, tenant_id: Optional[str] = None) -> list[dict[str, Any]]:
    out = []
    for g in db.all_goal_runs(include_finished=include_finished, tenant_id=tenant_id):
        tasks = db.goal_tasks(g.goal_id)
        out.append(
            {
                "goal_id": g.goal_id,
                "objective": g.objective,
                "done_definition": g.done_definition,
                "status": g.status,
                "autonomy_level": g.autonomy_level,
                "turns_used": g.turns_used,
                "max_turns": g.max_turns,
                "active_workers": g.active_workers,
                "max_workers": g.max_workers,
                "spent_usd": g.spent_usd,
                "max_spend_usd": g.max_spend_usd,
                "last_judge_reason": g.last_judge_reason,
                "updated_at": g.updated_at,
                "tasks": [
                    {
                        "task_id": t.task_id,
                        "title": t.title,
                        "scope": t.scope,
                        "status": t.status,
                        "verification": t.verification,
                        "claimed_paths": _decode_paths(t.claimed_paths),
                        "result_summary": t.result_summary,
                        "worker_mission_id": t.worker_mission_id,
                    }
                    for t in tasks
                ],
            }
        )
    return out


def _due_in(next_run_at: float, now: float) -> str:
    """Human countdown to the next run — '' when unset, 'due' when past."""
    if not next_run_at:
        return ""
    delta = next_run_at - now
    if delta <= 0:
        return "due"
    return naming.format_age(delta)


def loops(include_paused: bool = True, tenant_id: str = "") -> list[dict[str, Any]]:
    now = time.time()
    out = []
    for lp in db.all_loops(include_paused=include_paused, tenant_id=tenant_id):
        out.append(
            {
                "id": lp.id,
                "name": lp.name,
                "prompt": lp.prompt,
                "command": lp.command,
                "interval_seconds": lp.interval_seconds,
                "status": lp.status,
                "next_run_at": lp.next_run_at,
                "next_due": _due_in(lp.next_run_at, now),
                "due_now": bool(lp.next_run_at and lp.next_run_at <= now and lp.status == "active"),
                "running": lp.last_run_status == "running",
                "last_run_at": lp.last_run_at,
                "last_run_status": lp.last_run_status,
                "last_summary": lp.last_summary,
                "target_mission_id": lp.target_mission_id,
            }
        )
    return out


def notes(limit: int = 30, tenant_id: Optional[str] = None) -> list[dict[str, Any]]:
    return [
        {
            "id": n.id,
            "tab_id": n.tab_id,
            "session_id": n.session_id,
            "text": n.text,
            "kind": n.kind,
            "created_at": n.created_at,
            "age": _age(n.created_at),
        }
        for n in db.recent_notes(limit=limit, tenant_id=tenant_id)
    ]


def _action_text(action: str, details: Any) -> str:
    """Flatten a ledger action's details (a dict) into one readable line.

    ledger.recent_actions() json-decodes the details column into a dict; sending
    that raw to the browser renders as "[object Object]". Pick the most
    informative fields, fall back to compact JSON, else just the action name.
    """
    if isinstance(details, str):
        return details or action
    if isinstance(details, dict) and details:
        for key in ("summary", "goal", "title", "command", "prompt", "reason", "text", "name"):
            val = details.get(key)
            if val:
                return f"{action}: {val}" if action else str(val)
        flat = ", ".join(f"{k}={v}" for k, v in details.items()
                         if isinstance(v, (str, int, float)) and str(v))
        return f"{action}: {flat}"[:200] if flat else action
    return action


def activity_feed(limit: int = 40) -> list[dict[str, Any]]:
    """The 🐇 ticker: recent notes + recent autonomous actions, newest first."""
    items: list[dict[str, Any]] = []
    for n in db.recent_notes(limit=limit):
        items.append(
            {
                "ts": n.created_at,
                "kind": n.kind,
                "text": n.text,
                "source": "note",
                "tab_id": n.tab_id,
            }
        )
    for a in ledger.recent_actions(limit=limit):
        items.append(
            {
                "ts": a.ts,
                "kind": a.action,
                "text": _action_text(a.action, a.details),
                "source": "action",
                "tab_id": a.tab_id,
            }
        )
    items.sort(key=lambda x: x["ts"], reverse=True)
    for it in items:
        it["age"] = _age(it["ts"])
    return items[:limit]


def spend() -> dict[str, Any]:
    return {
        "today_usd": round(ledger.daily_dollar_total(), 4),
        "recent": [
            {
                "kind": c.kind,
                "description": c.description,
                "dollars": c.dollars,
                "ts": c.ts,
            }
            for c in ledger.recent_costs(limit=20)
        ],
    }


def projects(include_archived: bool = False) -> list[dict[str, Any]]:
    return [
        {
            "tenant_id": p.tenant_id,
            "name": p.name,
            "root_path": p.root_path,
            "last_seen_at": p.last_seen_at,
            "archived": p.archived_at is not None,
        }
        for p in db.all_project_tenants(include_archived=include_archived)
    ]


# ───────────────────────── write surface (DB, OS-agnostic) ─────────────────────────


def post_note(text: str, kind: str = "note", tab_id: Optional[str] = None) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty note"}
    if kind not in ("note", "claim", "broadcast"):
        kind = "note"
    nid = db.add_note(text=text, tab_id=tab_id, kind=kind)
    try:
        context_mod.write_context_json()
    except Exception:
        pass
    return {"ok": True, "id": nid, "kind": kind, "text": text}


def chat(query: str, *, use_llm: bool = True, include_gh: bool = False) -> dict[str, Any]:
    """Route a chat message to ``ask.ask`` — conversational Q&A over fleet state.

    ``include_gh`` defaults to False so hermetic tests never shell out to ``gh``.
    When no ``claude``/``codex`` CLI is present, ``ask`` returns the raw state
    snapshot, so this works (deterministically) without any LLM.
    """
    query = (query or "").strip()
    if not query:
        return {"ok": False, "error": "empty message"}
    # ask.ask uses include_gh=True internally; mirror that knob by gathering
    # state ourselves only when we want to skip GitHub. The simplest hermetic
    # path is use_llm-aware delegation to ask.ask.
    if include_gh:
        answer = ask_mod.ask(query, use_llm=use_llm)
    else:
        from morpheus import brief

        state = brief.gather_state(include_gh=False)
        snapshot = brief.build_template_brief(state)
        if not use_llm:
            answer = f"## Question\n\n> {query}\n\n## (No LLM — raw state)\n\n{snapshot}"
        else:
            prompt = (
                "You are Morpheus, the mission control for a solo developer's "
                "agent sessions. Answer the user's question using ONLY the state "
                "snapshot below. Be concise (≤12 lines). If the user is asking for "
                "an action, tell them the exact morpheus command to run.\n\n"
                f"QUESTION: {query}\n\nCURRENT STATE:\n\n{snapshot}"
            )
            answer = brief._run_claude(prompt) or brief._run_codex(prompt)
            if answer is None:
                answer = (
                    f"## Question\n\n> {query}\n\n"
                    f"## (no LLM available — raw state)\n\n{snapshot}"
                )
            else:
                answer = f"## Question\n\n> {query}\n\n## Morpheus says\n\n{answer}\n"
    return {"ok": True, "query": query, "answer": answer}


# ───────────────────────── control surface (iTerm2, macOS-only) ─────────────────────────


def iterm_available() -> bool:
    """True only if we can plausibly drive iTerm2 (mac + a live API cookie)."""
    if not os.environ.get("ITERM2_COOKIE"):
        return False
    try:
        import iterm2  # noqa: F401
    except Exception:
        return False
    return True


_UNAVAILABLE = {
    "ok": False,
    "error": "iTerm2 control is unavailable here (requires macOS + iTerm2 Python API). "
    "The note was still recorded so live sessions will see it via the shared context.",
}


def send_to_session(tab_id: str, text: str, *, submit: bool = True) -> dict[str, Any]:
    """Type ``text`` into a live iTerm2 session. macOS-only; degrades gracefully."""
    if not iterm_available():
        return dict(_UNAVAILABLE, ok=False)
    from morpheus import iterm_client

    payload = iterm_client.text_with_enter(text) if submit else text

    def _do(conn):
        return iterm_client.send_text_to_tabs(conn, [tab_id], payload)

    try:
        results = iterm_client.run(_do)
        r = results[0] if results else None
        return {"ok": bool(r and r.ok), "tab_id": tab_id, "error": (r.error if r else "no result")}
    except Exception as e:  # pragma: no cover - mac-only path
        return {"ok": False, "error": str(e)}


def broadcast(text: str, *, submit: bool = True) -> dict[str, Any]:
    """Record a broadcast note (durable, cross-session) and best-effort type it
    into every live session. The note always succeeds; the keystroke delivery is
    macOS-only and reported separately."""
    note_result = post_note(text, kind="broadcast")
    delivery: dict[str, Any] = {"attempted": False, "sent": [], "available": iterm_available()}
    if iterm_available():
        from morpheus import iterm_client

        tab_ids = [m.tab_id for m in db.all_missions() if m.state not in ("finished", "crashed")]
        payload = iterm_client.text_with_enter(text) if submit else text

        def _do(conn):
            return iterm_client.send_text_to_tabs(conn, tab_ids, payload)

        try:
            results = iterm_client.run(_do)
            delivery["attempted"] = True
            delivery["sent"] = [{"tab_id": r.tab_id, "ok": r.ok, "error": r.error} for r in results]
        except Exception as e:  # pragma: no cover - mac-only path
            delivery["error"] = str(e)
    return {"ok": note_result.get("ok", False), "note": note_result, "delivery": delivery}


def spawn_session(goal: str, command: str) -> dict[str, Any]:
    """Open a new iTerm2 tab running ``command`` and register a mission card.

    macOS-only. On other hosts this returns a structured unavailable result; the
    UI surfaces the exact CLI command to run instead."""
    goal = (goal or "").strip()
    command = (command or "").strip()
    if not command:
        return {"ok": False, "error": "command is required"}
    if not iterm_available():
        return dict(
            _UNAVAILABLE,
            ok=False,
            hint=f'morpheus spawn "{goal}" "{command}"',
        )
    from morpheus import iterm_client

    def _do(conn):
        return iterm_client.spawn_tab(conn, command=command, goal=goal)

    try:
        info = iterm_client.run(_do)
        if info is None:
            return {"ok": False, "error": "failed to spawn tab — is iTerm focused?"}
        now = time.time()
        m = db.Mission(
            tab_id=info.tab_id,
            session_id=info.session_id,
            goal=goal,
            state="working",
            cmd=command,
            buffer_changed_at=now,
            last_event_at=now,
            created_at=now,
        )
        db.upsert(m)
        return {"ok": True, "tab_id": info.tab_id, "mission_id": m.mission_id, "goal": goal}
    except Exception as e:  # pragma: no cover - mac-only path
        return {"ok": False, "error": str(e)}


# ───────────────────────── loops management (TUI parity) ─────────────────────────


def _loop_dict(lp: db.PromptLoop, now: Optional[float] = None) -> dict[str, Any]:
    from morpheus import loops as loops_mod

    now = now or time.time()
    return {
        "id": lp.id,
        "name": lp.name,
        "prompt": lp.prompt,
        "command": lp.command,
        "interval_seconds": lp.interval_seconds,
        "interval": loops_mod.format_interval(lp.interval_seconds),
        "status": lp.status,
        "next_run_at": lp.next_run_at,
        "next_due": _due_in(lp.next_run_at, now),
        "due_now": bool(lp.next_run_at and lp.next_run_at <= now and lp.status == "active"),
        "running": lp.last_run_status == "running",
        "last_run_at": lp.last_run_at,
        "last_run_status": lp.last_run_status,
        "last_summary": lp.last_summary,
        "target_mission_id": lp.target_mission_id,
    }


def loop_detail(loop_id: int) -> Optional[dict[str, Any]]:
    """One loop + its run history + its feed rule (if any)."""
    from morpheus import feeds

    lp = db.get_loop(loop_id)
    if lp is None:
        return None
    out = _loop_dict(lp)
    out["runs"] = [
        {
            "id": r.id,
            "started_at": r.started_at,
            "age": _age(r.started_at),
            "finished_at": r.finished_at,
            "status": r.status,
            "exit_code": r.exit_code,
            "summary": r.summary,
        }
        for r in db.loop_runs(loop_id, limit=15)
    ]
    rule = next(iter(feeds.rules(source_kind="loop", source_ref=str(loop_id))), None)
    out["feed_rule"] = (
        {"id": rule.id, "policy": rule.policy, "pattern": rule.pattern} if rule else None
    )
    return out


def loop_create(name: str, prompt: str, every: str = "30m",
                command: str = "", *, feed_policy: str = "",
                feed_pattern: str = "") -> dict[str, Any]:
    """Create a recurring prompt loop; optionally subscribe it to the feed."""
    from morpheus import feeds
    from morpheus import loops as loops_mod

    name = (name or "").strip()
    prompt = (prompt or "").strip()
    if not name or not prompt:
        return {"ok": False, "error": "name and prompt are required"}
    try:
        interval = loops_mod.parse_interval(every)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    lp = db.create_loop(
        name=name,
        prompt=prompt,
        interval_seconds=interval,
        command=loops_mod.normalize_command(command),
    )
    result: dict[str, Any] = {"ok": True, "loop": _loop_dict(lp)}
    if feed_policy:
        try:
            rule = feeds.set_rule("loop", str(lp.id), policy=feed_policy, pattern=feed_pattern)
            result["feed_rule"] = {"id": rule.id, "policy": rule.policy, "pattern": rule.pattern}
        except ValueError as e:
            result["feed_rule_error"] = str(e)
    return result


def loop_action(loop_id: int, action: str, *, timeout: int = 0,
                wait: bool = False) -> dict[str, Any]:
    """pause | resume | delete | run_now — the TUI's loop verbs.

    ``run_now`` starts the run in a background thread and returns immediately
    (agent runs can take many minutes; blocking the HTTP request was both bad UX
    and a thread hog). The UI watches the loop's ``running`` flag / run history
    for completion. ``wait=True`` keeps the old synchronous behaviour for tests.
    """
    from morpheus import loops as loops_mod

    lp = db.get_loop(loop_id)
    if lp is None:
        return {"ok": False, "error": f"loop {loop_id} not found"}
    if action == "pause":
        db.set_loop_status(loop_id, "paused")
        return {"ok": True, "status": "paused"}
    if action == "resume":
        db.set_loop_status(loop_id, "active")
        return {"ok": True, "status": "active"}
    if action == "delete":
        db.delete_loop(loop_id)
        return {"ok": True, "deleted": True}
    if action == "run_now":
        if lp.last_run_status == "running":
            return {"ok": False, "error": "a run is already in progress"}
        run_timeout = timeout or loops_mod.DEFAULT_TIMEOUT_SECONDS
        if wait:
            try:
                # run_loop publishes the result itself (notes + feed routing).
                run = loops_mod.run_loop(lp, timeout=run_timeout)
                return {"ok": run.status in ("ok", "success"), "status": run.status,
                        "summary": run.summary, "exit_code": run.exit_code}
            except Exception as e:
                return {"ok": False, "error": str(e)}
        import threading

        def _bg():
            try:
                loops_mod.run_loop(lp, timeout=run_timeout)
            except Exception:
                pass

        threading.Thread(target=_bg, daemon=True,
                         name=f"loop-run-{loop_id}").start()
        return {"ok": True, "started": True, "status": "running"}
    return {"ok": False, "error": f"unknown action '{action}'"}


def loop_run_output(loop_id: int, run_id: int, *, tail_chars: int = 20000) -> dict[str, Any]:
    """The captured output of one loop run — how you 'look inside' a headless run."""
    from morpheus import loops as loops_mod
    from pathlib import Path

    run = db.get_loop_run(run_id)
    if run is None or run.loop_id != loop_id:
        return {"ok": False, "error": "run not found"}
    text = loops_mod._read_output(Path(run.output_path)) if run.output_path else ""
    if len(text) > tail_chars:
        text = "…(truncated)…\n" + text[-tail_chars:]
    return {"ok": True, "run_id": run_id, "status": run.status,
            "output": text or "(no output captured)"}


def loop_set_feed_rule(loop_id: int, policy: str, pattern: str = "") -> dict[str, Any]:
    """Subscribe/adjust how this loop pushes into the feed ('' clears the rule)."""
    from morpheus import feeds

    if db.get_loop(loop_id) is None:
        return {"ok": False, "error": f"loop {loop_id} not found"}
    existing = feeds.rules(source_kind="loop", source_ref=str(loop_id))
    if not policy:
        for r in existing:
            feeds.delete_rule(r.id)
        return {"ok": True, "feed_rule": None}
    try:
        rule = feeds.set_rule("loop", str(loop_id), policy=policy, pattern=pattern)
    except (ValueError, Exception) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "feed_rule": {"id": rule.id, "policy": rule.policy, "pattern": rule.pattern}}


# ───────────────────────── goals management (TUI parity) ─────────────────────────


def goal_create(objective: str, *, done_definition: str = "", source: str = "",
                autonomy_level: str = "ask_to_spawn", max_turns: int = 20,
                max_workers: int = 3) -> dict[str, Any]:
    """Create a durable goal run. `source` may be a PRD path or mission ref; when
    empty, a fresh mission memory node is created from the objective so goals can
    be started straight from the desktop without a PRD file."""
    from morpheus import goals as goals_mod

    objective = (objective or "").strip()
    if not objective:
        return {"ok": False, "error": "objective is required"}
    try:
        if not source:
            now = time.time()
            mem = db.MissionMemory(
                mission_id=db.new_mission_id(now),
                title=objective[:120],
                why=objective,
                done_definition=done_definition,
                phase="planning",
                source_kind="desktop",
            )
            db.upsert_memory(mem)
            source = mem.mission_id
        bundle = goals_mod.create_goal_run(
            source,
            objective=objective,
            done_definition=done_definition or None,
            autonomy_level=autonomy_level,
            max_turns=max_turns,
            max_workers=max_workers,
        )
    except (ValueError, Exception) as e:
        return {"ok": False, "error": str(e)}
    g = bundle.goal
    return {"ok": True, "goal_id": g.goal_id, "objective": g.objective,
            "status": g.status, "autonomy_level": g.autonomy_level}


def goal_action(goal_id: str, action: str, *, reason: str = "") -> dict[str, Any]:
    """pause | resume | done | clear — goal lifecycle controls."""
    from morpheus import goals as goals_mod

    status_map = {"pause": "paused", "resume": "active", "done": "done", "clear": "cleared"}
    status = status_map.get(action)
    if status is None:
        return {"ok": False, "error": f"unknown action '{action}'"}
    goal = goals_mod.resolve_goal(goal_id)
    if goal is None:
        return {"ok": False, "error": f"goal '{goal_id}' not found"}
    try:
        goals_mod.set_status(goal.goal_id, status, reason=reason or f"{action} from desktop")
    except (ValueError, Exception) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "goal_id": goal.goal_id, "status": status}


# ───────────────────────── feed (the aggregator) ─────────────────────────


def feed_items(limit: int = 50, since_id: int = 0) -> list[dict[str, Any]]:
    from morpheus import feeds

    return [
        {
            "id": it.id,
            "ts": it.ts,
            "age": _age(it.ts),
            "title": it.title,
            "body": it.body,
            "source_kind": it.source_kind,
            "source_ref": it.source_ref,
            "priority": it.priority,
        }
        for it in feeds.recent(limit, since_id=since_id)
    ]


def feed_post(title: str, body: str = "", priority: int = 0) -> dict[str, Any]:
    from morpheus import feeds

    try:
        item_id = feeds.post(title, body, priority=priority)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "id": item_id}


def feed_rules_list() -> list[dict[str, Any]]:
    from morpheus import feeds

    loops_by_id = {str(lp.id): lp.name for lp in db.all_loops()}
    return [
        {
            "id": r.id,
            "source_kind": r.source_kind,
            "source_ref": r.source_ref,
            "source_name": loops_by_id.get(r.source_ref, r.source_ref)
            if r.source_kind == "loop" else r.source_ref,
            "policy": r.policy,
            "pattern": r.pattern,
        }
        for r in feeds.rules()
    ]


def feed_text(limit: int = 20) -> str:
    from morpheus import feeds

    return feeds.render_text(limit)
