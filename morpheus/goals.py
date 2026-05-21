"""Autonomous goal-run helpers.

Goal runs are a Morpheus-level controller primitive over PRD runs and durable
missions. Provider CLIs may run underneath, but the objective, budgets, status,
and proof live in the mission graph.
"""

from __future__ import annotations

import shlex
import posixpath
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from morpheus import db, mission_graph as graph_mod, prd_runs, tenant as tenant_mod

GOALS_DIR = Path.home() / ".morpheus" / "goals"
DEFAULT_MAX_TURNS = 20
DEFAULT_MAX_WORKERS = 3
DEFAULT_CONTINUATION_COOLDOWN_SECONDS = 120.0
CONTROLLER_CONTINUE_STATES = {"idle"}
ALLOWED_AGENT_COMMANDS = {"codex", "claude", "gemini", "opencode", "aider"}
BLOCKED_COMMAND_TOKENS = {";", "&&", "||", "|", ">", ">>", "<", "$(", "`"}
AGENT_VALUE_OPTIONS = {
    "-m",
    "-p",
    "-s",
    "--append-system-prompt",
    "--approval-policy",
    "--ask-for-approval",
    "--config",
    "--model",
    "--model-provider",
    "--mcp-config",
    "--output-schema",
    "--permission-prompt-tool",
    "--profile",
    "--sandbox",
}
AGENT_BLOCKED_OPTIONS = {"-C", "--add-dir", "--cd", "--cwd"}
GOAL_STATUSES = {"active", "paused", "done", "failed", "cleared"}
TASK_STATUSES = {"planned", "running", "blocked", "done", "failed", "ready_for_retry", "cancelled"}
AUTONOMY_LEVELS = {"observe_only", "ask_to_spawn", "bounded_fanout"}
ACTIVE_TASK_STATUSES = {"running", "blocked"}
FINAL_TASK_STATUSES = {"done", "failed", "cancelled"}


@dataclass
class GoalRunBundle:
    goal: db.GoalRun
    parent: db.MissionMemory
    status_path: Path
    prompt_path: Path


@dataclass
class GoalContinuationTarget:
    goal: db.GoalRun
    controller: db.Mission
    reason: str


def create_goal_run(
    source: str | Path,
    *,
    title: Optional[str] = None,
    objective: Optional[str] = None,
    done_definition: Optional[str] = None,
    project: Optional[db.ProjectTenant] = None,
    autonomy_level: str = "ask_to_spawn",
    max_turns: int = DEFAULT_MAX_TURNS,
    max_workers: int = DEFAULT_MAX_WORKERS,
    max_spend_usd: float = 0.0,
    judge_model: str = "",
) -> GoalRunBundle:
    """Create a durable goal run from a PRD path or existing mission ref."""
    autonomy_level = _validate_value("autonomy_level", autonomy_level, AUTONOMY_LEVELS)
    max_turns = _positive_int("max_turns", max_turns)
    max_workers = _positive_int("max_workers", max_workers)
    parent = _resolve_or_create_parent(source, title=title, project=project)
    project = _project_for_parent(parent, fallback=project)
    now = time.time()
    goal_id = db.new_goal_id(now)
    goal = db.GoalRun(
        goal_id=goal_id,
        parent_mission_id=parent.mission_id,
        tenant_id=project.tenant_id,
        project_root=project.root_path,
        source_kind=parent.source_kind or "mission",
        source_ref=parent.source_ref or parent.mission_id,
        objective=objective or default_objective(parent),
        done_definition=done_definition or parent.done_definition or default_done_definition(parent),
        status="active",
        autonomy_level=autonomy_level,
        max_turns=max_turns,
        max_workers=max_workers,
        max_spend_usd=max(0.0, float(max_spend_usd)),
        judge_model=judge_model,
        created_at=now,
        updated_at=now,
    )
    goal = db.upsert_goal_run(goal)
    bundle = bundle_for_goal(goal.goal_id)
    db.add_edge(
        parent.mission_id,
        goal.goal_id,
        relation="goal_run",
        reason="Autonomous goal run for this mission",
    )
    db.add_event(
        parent.mission_id,
        kind="goal_start",
        actor="morpheus",
        summary=f"Goal run started: {goal.objective}",
        source_ref=f"goal:{goal.goal_id}",
        metadata={
            "goal_id": goal.goal_id,
            "status_path": str(bundle.status_path),
            "prompt_path": str(bundle.prompt_path),
            "max_turns": goal.max_turns,
            "max_workers": goal.max_workers,
            "autonomy_level": goal.autonomy_level,
        },
    )
    db.add_artifact(
        parent.mission_id,
        kind="goal_status",
        path_or_url=str(bundle.status_path),
        status="active",
        summary=f"Autonomous goal status for {parent.title or parent.mission_id}",
    )
    write_controller_prompt(bundle)
    write_status_file(bundle)
    return bundle


def bundle_for_goal(goal_id: str) -> GoalRunBundle:
    goal = db.get_goal_run(goal_id)
    if goal is None:
        raise ValueError(f"goal run not found: {goal_id}")
    parent = db.get_memory(goal.parent_mission_id)
    if parent is None:
        raise ValueError(f"goal parent mission not found: {goal.parent_mission_id}")
    run_dir = GOALS_DIR / goal.goal_id
    return GoalRunBundle(
        goal=goal,
        parent=parent,
        status_path=run_dir / "status.md",
        prompt_path=run_dir / "controller_prompt.md",
    )


def resolve_goal(ref: str) -> Optional[db.GoalRun]:
    ref = ref.strip()
    if not ref:
        return None
    direct = db.get_goal_run(ref)
    if direct is not None:
        return direct
    matches = [
        goal for goal in db.all_goal_runs(include_finished=True)
        if goal.goal_id.startswith(ref)
        or goal.parent_mission_id == ref
        or goal.parent_mission_id.startswith(ref)
        or goal.controller_mission_id == ref
        or (goal.controller_mission_id and goal.controller_mission_id.startswith(ref))
    ]
    if matches:
        return matches[0]
    resolved = graph_mod.resolve(ref)
    if resolved is None:
        return None
    return db.goal_run_for_mission(resolved.mission_id)


def controller_command(base_command: str, bundle: GoalRunBundle) -> str:
    cmd = _safe_agent_command(base_command)
    prompt = (
        f"You are the Morpheus autonomous goal controller for goal run {bundle.goal.goal_id}. "
        f"Read {bundle.prompt_path}, use {bundle.status_path} for current run state, "
        f"and keep proof/status in Morpheus mission events and artifacts."
    )
    return f"{cmd} {shlex.quote(prompt)}"


def attach_controller(bundle: GoalRunBundle, mission: db.Mission) -> db.GoalRun:
    goal = db.attach_goal_controller(bundle.goal.goal_id, mission.mission_id)
    if goal is None:
        raise ValueError(f"goal run not found: {bundle.goal.goal_id}")
    db.add_edge(
        goal.parent_mission_id,
        mission.mission_id,
        relation="goal_controller",
        reason="Controller session for autonomous goal run",
    )
    db.add_event(
        goal.parent_mission_id,
        kind="goal_controller_spawned",
        actor="morpheus",
        summary=f"Goal controller spawned: {mission.goal or mission.tab_id}",
        source_ref=f"tab:{mission.tab_id}",
        metadata={"goal_id": goal.goal_id, "controller_mission_id": mission.mission_id},
    )
    db.add_event(
        mission.mission_id,
        kind="assigned",
        actor="morpheus",
        summary=f"Assigned as goal controller for {bundle.parent.title or goal.goal_id}",
        source_ref=f"goal:{goal.goal_id}",
        metadata={"goal_id": goal.goal_id, "parent_mission_id": goal.parent_mission_id, "role": "goal_controller"},
    )
    refreshed = bundle_for_goal(goal.goal_id)
    write_status_file(refreshed)
    return goal


def create_task(
    goal_ref: str,
    *,
    title: str,
    scope: str = "",
    verification: str = "",
    claimed_paths: Iterable[str] | str = (),
    status: str = "planned",
    metadata: Optional[dict] = None,
) -> db.GoalTask:
    goal = resolve_goal(goal_ref)
    if goal is None:
        raise ValueError(f"goal run not found: {goal_ref}")
    status = _validate_value("task status", status, TASK_STATUSES)
    normalized_paths = _normalize_claimed_paths(claimed_paths, project_root=goal.project_root)
    encoded_paths = _encode_claimed_paths(normalized_paths)
    path_conflicts = _claimed_path_conflicts(goal.goal_id, normalized_paths, project_root=goal.project_root)
    if path_conflicts:
        summary = "Goal task path conflict: " + "; ".join(path_conflicts)
        db.add_event(
            goal.parent_mission_id,
            kind="goal_path_conflict",
            actor="morpheus",
            summary=summary,
            source_ref=f"goal:{goal.goal_id}",
            metadata={"goal_id": goal.goal_id, "title": title, "conflicts": path_conflicts},
        )
        write_status_file(bundle_for_goal(goal.goal_id))
        raise ValueError(summary)
    task = db.create_goal_task(
        goal.goal_id,
        title=title,
        scope=scope,
        verification=verification,
        claimed_paths=encoded_paths,
        status=status,
        metadata=metadata,
    )
    db.add_event(
        goal.parent_mission_id,
        kind="goal_task_created",
        actor="morpheus",
        summary=f"Goal task created: {title}",
        source_ref=f"task:{task.task_id}",
        metadata={
            "goal_id": goal.goal_id,
            "task_id": task.task_id,
            "scope": scope,
            "verification": verification,
            "claimed_paths": _decode_claimed_paths(task.claimed_paths),
        },
    )
    db.add_note(
        text=f"goal {goal.goal_id} task created: {title}",
        tab_id=_controller_tab_id(goal),
        kind="goal",
    )
    bundle = bundle_for_goal(goal.goal_id)
    write_status_file(bundle)
    return task


def resolve_task(ref: str) -> Optional[db.GoalTask]:
    ref = ref.strip()
    if not ref:
        return None
    direct = db.goal_task(ref)
    if direct is not None:
        return direct
    for goal in db.all_goal_runs(include_finished=True):
        for task in db.goal_tasks(goal.goal_id):
            if (
                task.task_id.startswith(ref)
                or task.worker_mission_id == ref
                or (task.worker_mission_id and task.worker_mission_id.startswith(ref))
            ):
                return task
    resolved = graph_mod.resolve(ref)
    if resolved is None:
        return None
    return db.goal_task_for_worker(resolved.mission_id)


def worker_command(base_command: str, bundle: GoalRunBundle, task: db.GoalTask) -> str:
    cmd = _safe_agent_command(base_command)
    prompt = (
        f"You are a worker for Morpheus goal run {bundle.goal.goal_id}. "
        f"Task: {task.title} ({task.task_id}). "
        f"Objective: {bundle.goal.objective}. "
        f"Read {bundle.status_path} before acting and keep status/proof in Morpheus. "
        f"Owned scope: {task.scope or 'ask the controller/user to confirm write scope before editing'}. "
        f"Claimed paths: {', '.join(_decode_claimed_paths(task.claimed_paths)) or 'unset'}. "
        f"Verification required: {task.verification or 'record proof before declaring done'}. "
        f"Use `morpheus goal task-status {task.task_id} running --summary \"...\"` for heartbeats, "
        f"`morpheus goal task-status {task.task_id} blocked --summary \"...\"` for blockers, and "
        f"`morpheus goal task-status {task.task_id} done --summary \"...\"` when complete. "
        f"Do not revert unrelated edits or other workers' changes."
    )
    return f"{cmd} {shlex.quote(prompt)}"


def attach_worker(
    task_ref: str,
    mission: db.Mission,
    *,
    status: str = "running",
) -> db.GoalTask:
    status = _validate_value("task status", status, TASK_STATUSES)
    task = resolve_task(task_ref)
    if task is None:
        raise ValueError(f"goal task not found: {task_ref}")
    goal = db.get_goal_run(task.goal_id)
    if goal is None:
        raise ValueError(f"goal run not found: {task.goal_id}")
    if task.status not in ACTIVE_TASK_STATUSES and goal.active_workers >= goal.max_workers:
        raise ValueError(f"goal worker budget reached ({goal.active_workers}/{goal.max_workers})")

    updated = db.attach_goal_worker(task.task_id, mission.mission_id, status=status)
    if updated is None:
        raise ValueError(f"goal task not found: {task.task_id}")

    db.add_edge(
        goal.parent_mission_id,
        mission.mission_id,
        relation="goal_worker",
        reason=updated.scope or updated.title,
    )
    db.add_edge(
        goal.goal_id,
        mission.mission_id,
        relation="goal_task",
        reason=updated.title,
    )
    if goal.controller_mission_id:
        db.add_edge(
            goal.controller_mission_id,
            mission.mission_id,
            relation="goal_worker",
            reason=updated.title,
        )
    db.upsert_memory(
        db.MissionMemory(
            mission_id=mission.mission_id,
            tenant_id=mission.tenant_id or goal.tenant_id,
            project_root=mission.project_root or goal.project_root,
            title=mission.goal or updated.title,
            why=f"Worker for autonomous goal {goal.goal_id}.",
            done_definition=updated.verification or "Task scope is complete and proof is recorded.",
            acceptance_criteria=goal.done_definition,
            current_plan=updated.scope,
            next_step="Work the assigned task, heartbeat through Morpheus, and record proof.",
            phase="editing",
            confidence=1.0,
            source_kind="goal_task",
            source_ref=updated.task_id,
            claimed_paths=updated.claimed_paths,
            topic="goal-worker",
            created_at=mission.created_at,
            updated_at=mission.updated_at,
        )
    )
    db.add_event(
        goal.parent_mission_id,
        kind="goal_worker_spawned",
        actor="morpheus",
        summary=f"Goal worker spawned: {mission.goal or updated.title}",
        source_ref=f"tab:{mission.tab_id}",
        metadata={
            "goal_id": goal.goal_id,
            "task_id": updated.task_id,
            "worker_mission_id": mission.mission_id,
            "scope": updated.scope,
            "verification": updated.verification,
        },
    )
    db.add_event(
        mission.mission_id,
        kind="assigned",
        actor="morpheus",
        summary=f"Assigned goal task: {updated.title}",
        source_ref=f"goal:{goal.goal_id}",
        metadata={
            "goal_id": goal.goal_id,
            "task_id": updated.task_id,
            "parent_mission_id": goal.parent_mission_id,
            "role": "goal_worker",
            "scope": updated.scope,
            "verification": updated.verification,
        },
    )
    db.add_note(
        text=f"goal {goal.goal_id} worker spawned: {mission.goal or updated.title}",
        tab_id=mission.tab_id,
        session_id=mission.session_id,
        kind="goal",
    )
    bundle = bundle_for_goal(goal.goal_id)
    write_status_file(bundle)
    return updated


def set_task_status(
    task_ref: str,
    status: str,
    *,
    summary: str = "",
    metadata: Optional[dict] = None,
) -> db.GoalTask:
    status = _validate_value("task status", status, TASK_STATUSES)
    task = resolve_task(task_ref)
    if task is None:
        raise ValueError(f"goal task not found: {task_ref}")
    updated = db.update_goal_task(
        task.task_id,
        status=status,
        result_summary=summary if summary else None,
        metadata=metadata,
        heartbeat=status in ACTIVE_TASK_STATUSES or bool(summary),
    )
    if updated is None:
        raise ValueError(f"goal task not found: {task.task_id}")
    goal = db.get_goal_run(updated.goal_id)
    if goal is None:
        raise ValueError(f"goal run not found: {updated.goal_id}")

    event_kind = "goal_worker_heartbeat" if status == "running" else f"goal_worker_{status}"
    event_summary = summary or f"Goal task {updated.title} marked {status}"
    db.add_event(
        goal.parent_mission_id,
        kind=event_kind,
        actor="morpheus",
        summary=event_summary,
        source_ref=f"task:{updated.task_id}",
        metadata={
            "goal_id": goal.goal_id,
            "task_id": updated.task_id,
            "status": updated.status,
            "worker_mission_id": updated.worker_mission_id,
        },
    )
    if updated.worker_mission_id:
        db.add_event(
            updated.worker_mission_id,
            kind=event_kind,
            actor="morpheus",
            summary=event_summary,
            source_ref=f"task:{updated.task_id}",
            metadata={"goal_id": goal.goal_id, "task_id": updated.task_id, "status": updated.status},
        )
    live_worker = _live_for_mission(updated.worker_mission_id)
    db.add_note(
        text=f"goal {goal.goal_id} task {updated.status}: {updated.title}",
        tab_id=live_worker.tab_id if live_worker else None,
        session_id=live_worker.session_id if live_worker else None,
        kind="goal",
    )
    bundle = bundle_for_goal(goal.goal_id)
    write_status_file(bundle)
    return updated


def reserve_continuation(
    goal_ref: str,
    *,
    reason: str = "controller continuation queued",
    cooldown_seconds: float = 0.0,
) -> tuple[GoalRunBundle, str]:
    goal = resolve_goal(goal_ref)
    if goal is None:
        raise ValueError(f"goal run not found: {goal_ref}")
    updated, outcome = db.reserve_goal_continuation(
        goal.goal_id,
        reason=reason,
        cooldown_seconds=cooldown_seconds,
    )
    if updated is None:
        raise ValueError(f"goal run not found: {goal_ref}")
    bundle = bundle_for_goal(updated.goal_id)
    if outcome == "reserved":
        db.add_event(
            updated.parent_mission_id,
            kind="goal_continue",
            actor="morpheus",
            summary=f"Controller continuation {updated.turns_used}/{updated.max_turns}: {reason}",
            source_ref=f"goal:{updated.goal_id}",
            metadata={"goal_id": updated.goal_id, "turns_used": updated.turns_used, "max_turns": updated.max_turns},
        )
    elif outcome == "budget_exhausted":
        db.add_event(
            updated.parent_mission_id,
            kind="goal_budget_pause",
            actor="morpheus",
            summary=updated.last_judge_reason or f"Controller turn budget exhausted ({updated.turns_used}/{updated.max_turns})",
            source_ref=f"goal:{updated.goal_id}",
            metadata={"goal_id": updated.goal_id, "turns_used": updated.turns_used, "max_turns": updated.max_turns},
        )
    write_status_file(bundle)
    return bundle, outcome


def continuation_text(bundle: GoalRunBundle, *, reason: str = "scheduled continuation") -> str:
    goal = bundle.goal
    tasks = db.goal_tasks(goal.goal_id)
    task_summary = ", ".join(
        f"{task.task_id}:{task.status}:{task.title}"
        for task in tasks[:12]
    ) or "none"
    return " ".join(
        [
            "# MORPHEUS_GOAL_CONTINUATION",
            f"goal={goal.goal_id}",
            f"reason={_comment_field(reason, limit=120)}",
            f"turns={goal.turns_used}/{goal.max_turns}",
            f"workers={goal.active_workers}/{goal.max_workers}",
            f"status_file={bundle.status_path}",
            f"prompt_file={bundle.prompt_path}",
            f"tasks={_comment_field(task_summary, limit=240)}",
            "commands=morpheus goal task-add, morpheus goal task-spawn, morpheus goal done, morpheus goal pause",
            "action=read status, reconcile proof, create/spawn only disjoint bounded tasks, mark done or pause when proven",
            "never=push, merge, approve PRs, send external messages, spend money, or take account actions",
        ]
    )


def due_continuation_targets(
    *,
    now: Optional[float] = None,
    cooldown_seconds: float = DEFAULT_CONTINUATION_COOLDOWN_SECONDS,
    limit: int = 3,
    tenant_id: Optional[str] = None,
) -> list[GoalContinuationTarget]:
    ts = time.time() if now is None else now
    targets: list[GoalContinuationTarget] = []
    for goal in db.all_goal_runs(include_finished=False, tenant_id=tenant_id):
        if goal.status != "active" or not goal.controller_mission_id:
            continue
        if cooldown_seconds > 0 and goal.last_continued_at > 0:
            if ts - goal.last_continued_at < cooldown_seconds:
                continue
        if goal.turns_used >= goal.max_turns:
            continue
        controller = _live_for_mission(goal.controller_mission_id)
        if controller is None:
            continue
        if controller.state not in CONTROLLER_CONTINUE_STATES:
            continue
        targets.append(GoalContinuationTarget(
            goal=goal,
            controller=controller,
            reason=f"controller {controller.state} and continuation cooldown elapsed",
        ))
        if len(targets) >= limit:
            break
    return targets


def pause_budget_exhausted_goals(tenant_id: Optional[str] = None) -> list[db.GoalRun]:
    paused: list[db.GoalRun] = []
    for goal in db.all_goal_runs(include_finished=False, tenant_id=tenant_id):
        if goal.status != "active" or goal.turns_used < goal.max_turns:
            continue
        bundle, outcome = reserve_continuation(
            goal.goal_id,
            reason=f"controller turn budget exhausted ({goal.turns_used}/{goal.max_turns})",
            cooldown_seconds=0,
        )
        if outcome == "budget_exhausted":
            paused.append(bundle.goal)
    return paused


def set_status(
    goal_id: str,
    status: str,
    *,
    reason: str = "",
    reset_turns: bool = False,
) -> GoalRunBundle:
    status = _validate_value("goal status", status, GOAL_STATUSES)
    goal = db.set_goal_run_status(
        goal_id,
        status,
        reason=reason,
        reset_turns=reset_turns,
    )
    if goal is None:
        raise ValueError(f"goal run not found: {goal_id}")
    bundle = bundle_for_goal(goal.goal_id)
    db.add_event(
        goal.parent_mission_id,
        kind=f"goal_{status}",
        actor="morpheus",
        summary=reason or f"Goal run {status}",
        source_ref=f"goal:{goal.goal_id}",
        metadata={"goal_id": goal.goal_id, "status": status},
    )
    db.add_note(
        text=f"goal {goal.goal_id} {status}: {reason or status}",
        tab_id=_controller_tab_id(goal),
        kind="goal",
    )
    write_status_file(bundle)
    return bundle


def write_controller_prompt(bundle: GoalRunBundle) -> None:
    goal = bundle.goal
    parent = bundle.parent
    bundle.prompt_path.parent.mkdir(parents=True, exist_ok=True)
    bundle.prompt_path.write_text(
        "\n".join(
            [
                f"# Goal Controller Prompt: {parent.title or goal.goal_id}",
                "",
                f"Goal run: `{goal.goal_id}`",
                f"Parent mission: `{goal.parent_mission_id}`",
                f"Source: `{goal.source_ref or parent.source_ref}`",
                f"Status file: `{bundle.status_path}`",
                "",
                "## Objective",
                goal.objective,
                "",
                "## Done Definition",
                goal.done_definition,
                "",
                "## Autonomy Envelope",
                f"- status: `{goal.status}`",
                f"- autonomy level: `{goal.autonomy_level}`",
                f"- max controller turns: `{goal.max_turns}`",
                f"- max active workers: `{goal.max_workers}`",
                f"- max spend USD: `{goal.max_spend_usd:g}`",
                "",
                "## Responsibilities",
                "- Read the source PRD/mission and derive a bounded implementation plan.",
                "- Fan out only into worker scopes that can be owned independently.",
                "- Before spawning or requesting workers, name each worker's scope, verification, and expected proof.",
                "- Keep status in Morpheus mission events/artifacts; do not rewrite the source PRD as a status log.",
                "- Surface blockers, changed paths, verification commands, and residual risk.",
                "- Use deterministic checks when possible; model judgment is secondary to surfaced proof.",
                "- Use `morpheus goal task-add`, `task-spawn`, and `task-status` to create, fan out, heartbeat, block, and complete worker tasks.",
                "- Mark the run with `morpheus goal done`, `pause`, or `clear`; do not invent completion outside the control plane.",
                "",
                "## Safety Rules",
                "- Do not push, merge, approve PRs, send external messages, spend money, or take account actions.",
                "- Do not let two workers edit the same path unless the user approves or the work is serialized.",
                "- If the goal is blocked, pause and record a durable blocker instead of improvising around missing authority.",
                "",
            ]
        ),
        encoding="utf-8",
    )


def write_status_file(bundle: GoalRunBundle) -> Path:
    bundle.status_path.parent.mkdir(parents=True, exist_ok=True)
    bundle.status_path.write_text(render_status(bundle.goal.goal_id), encoding="utf-8")
    return bundle.status_path


def render_status(goal_id: str, *, generated_at: Optional[float] = None) -> str:
    bundle = bundle_for_goal(goal_id)
    goal = bundle.goal
    parent = bundle.parent
    now = generated_at if generated_at is not None else time.time()
    tasks = db.goal_tasks(goal.goal_id)
    controller = db.get_memory(goal.controller_mission_id) if goal.controller_mission_id else None
    controller_live = _live_for_mission(goal.controller_mission_id)
    events = _recent_events([goal.parent_mission_id, goal.controller_mission_id, *[t.worker_mission_id for t in tasks]], limit=12)
    artifacts = _recent_artifacts([goal.parent_mission_id, goal.controller_mission_id, *[t.worker_mission_id for t in tasks]], limit=12)

    lines = [
        f"# Goal Run: {parent.title or goal.goal_id}",
        "",
        f"- goal id: `{goal.goal_id}`",
        f"- parent mission: `{goal.parent_mission_id}`",
        f"- source: `{goal.source_ref or parent.source_ref}`",
        f"- status: `{goal.status}`",
        f"- autonomy: `{goal.autonomy_level}`",
        f"- generated: `{_format_ts(now)}`",
        f"- budget: `{goal.turns_used}/{goal.max_turns}` turns, `{goal.active_workers}/{goal.max_workers}` active workers, `${goal.spent_usd:g}/${goal.max_spend_usd:g}`",
        "",
        "Morpheus owns goal status in SQLite. This file is rendered from graph state; do not hand-edit it as the source of truth.",
        "",
        "## Objective",
        goal.objective or "- unset",
        "",
        "## Done Definition",
        goal.done_definition or parent.done_definition or "- unset",
        "",
        "## Controller",
    ]
    if goal.controller_mission_id:
        live = f" live tab `{controller_live.tab_id}`" if controller_live else ""
        title = (controller.title if controller else "") or goal.controller_mission_id
        lines.append(f"- {title} (`{goal.controller_mission_id}`){live}")
    else:
        lines.append("- not spawned")

    lines.extend(["", "## Workers"])
    if tasks:
        for task in tasks:
            worker = f" → `{task.worker_mission_id}`" if task.worker_mission_id else ""
            scope = f" scope: {task.scope}" if task.scope else ""
            verification = f" verify: {task.verification}" if task.verification else ""
            heartbeat = f" heartbeat: {_format_ts(task.last_heartbeat_at)}" if task.last_heartbeat_at else ""
            result = f" result: {_one_line(task.result_summary)}" if task.result_summary else ""
            paths = ", ".join(_decode_claimed_paths(task.claimed_paths))
            claims = f" paths: {paths}" if paths else ""
            lines.append(
                f"- `{task.status}` `{task.task_id}` {task.title}{worker}"
                f"{scope}{claims}{verification}{heartbeat}{result}"
            )
    else:
        lines.append("- none yet")

    lines.extend(["", "## Last Judge Reason"])
    lines.append(goal.last_judge_reason or "- none yet")

    lines.extend(["", "## Why Not Done Yet"])
    if goal.status in {"done", "cleared"}:
        lines.append("- goal is no longer active")
    elif not tasks:
        lines.append("- no worker tasks or proof have been recorded yet")
    elif any(task.status in {"planned", "running", "blocked", "ready_for_retry"} for task in tasks):
        pending = [task.title for task in tasks if task.status in {"planned", "running", "blocked", "ready_for_retry"}]
        lines.append(f"- pending tasks: {', '.join(pending[:5])}")
    else:
        lines.append("- controller still needs final proof/check agreement before marking done")

    lines.extend(["", "## Recent Events"])
    if events:
        for event in events:
            source = f" [{event.source_ref}]" if event.source_ref else ""
            lines.append(f"- {_format_ts(event.ts)} `{event.mission_id}` {event.kind}/{event.actor}: {_one_line(event.summary)}{source}")
    else:
        lines.append("- none")

    lines.extend(["", "## Proof And Artifacts"])
    if artifacts:
        for artifact in artifacts:
            summary = f" - {_one_line(artifact.summary)}" if artifact.summary else ""
            lines.append(f"- {_format_ts(artifact.created_at)} `{artifact.mission_id}` {artifact.status} {artifact.kind}: `{artifact.path_or_url}`{summary}")
    else:
        lines.append("- none")

    lines.append("")
    return "\n".join(lines)


def default_objective(parent: db.MissionMemory) -> str:
    title = parent.title or parent.mission_id
    if parent.source_kind == "prd":
        return f"Implement the PRD '{title}' until its acceptance criteria are verified."
    return f"Complete mission '{title}' until its done definition is verified."


def default_done_definition(parent: db.MissionMemory) -> str:
    if parent.acceptance_criteria:
        return f"All acceptance criteria are satisfied and proof artifacts are recorded.\n\n{parent.acceptance_criteria}"
    return "The objective is complete, deterministic checks pass where available, and Morpheus has recorded proof artifacts."


def _resolve_or_create_parent(
    source: str | Path,
    *,
    title: Optional[str],
    project: Optional[db.ProjectTenant],
) -> db.MissionMemory:
    source_text = str(source)
    path = Path(source_text).expanduser()
    if path.exists() and path.is_file():
        run = prd_runs.create_prd_run(path, title=title, project=project)
        memory = db.get_memory(run.parent_id)
        if memory is None:
            raise ValueError(f"PRD parent mission was not created: {run.parent_id}")
        return memory

    resolved = graph_mod.resolve(source_text)
    if resolved is None:
        raise ValueError(f"no PRD path or mission matching '{source_text}'")
    return resolved.memory


def _project_for_parent(
    parent: db.MissionMemory,
    *,
    fallback: Optional[db.ProjectTenant],
) -> db.ProjectTenant:
    if parent.tenant_id:
        project = db.get_project_tenant(parent.tenant_id)
        if project is not None:
            return project
    if parent.project_root:
        return tenant_mod.ensure_project_tenant(parent.project_root)
    if parent.source_ref:
        return tenant_mod.ensure_project_tenant(parent.source_ref)
    if fallback is not None:
        return fallback
    return tenant_mod.ensure_project_tenant(Path.cwd())


def _live_for_mission(mission_id: str) -> Optional[db.Mission]:
    if not mission_id:
        return None
    for mission in db.all_missions():
        if mission.mission_id == mission_id:
            return mission
    return None


def _controller_tab_id(goal: db.GoalRun) -> Optional[str]:
    controller = _live_for_mission(goal.controller_mission_id)
    return controller.tab_id if controller else None


def _recent_events(mission_ids: list[str], *, limit: int) -> list[db.MissionEvent]:
    events: list[db.MissionEvent] = []
    for mission_id in {m for m in mission_ids if m}:
        events.extend(db.recent_events(mission_id, limit=limit))
    events.sort(key=lambda event: event.ts, reverse=True)
    return events[:limit]


def _recent_artifacts(mission_ids: list[str], *, limit: int) -> list[db.MissionArtifact]:
    artifacts: list[db.MissionArtifact] = []
    for mission_id in {m for m in mission_ids if m}:
        artifacts.extend(db.artifacts_for_mission(mission_id, limit=limit))
    artifacts.sort(key=lambda artifact: artifact.created_at, reverse=True)
    return artifacts[:limit]


def _safe_agent_command(command: str) -> str:
    raw = (command or "codex").strip()
    if not raw:
        raw = "codex"
    if "\n" in raw or "\r" in raw or any(token in raw for token in BLOCKED_COMMAND_TOKENS):
        raise ValueError("agent command must be a direct provider command without shell control operators")
    try:
        parts = shlex.split(raw)
    except ValueError as exc:
        raise ValueError(f"agent command is not parseable: {exc}") from exc
    if not parts:
        return "codex"
    executable = Path(parts[0]).name.lower()
    if executable not in ALLOWED_AGENT_COMMANDS:
        allowed = ", ".join(sorted(ALLOWED_AGENT_COMMANDS))
        raise ValueError(f"agent command must start with one of: {allowed}")
    _validate_agent_options(parts[1:])
    return " ".join(shlex.quote(part) for part in parts)


def _validate_agent_options(parts: list[str]) -> None:
    index = 0
    while index < len(parts):
        part = parts[index]
        if part == "--" or not part.startswith("-"):
            raise ValueError("agent command may only include options, not positional subcommands")
        option_name = part.split("=", 1)[0]
        if option_name in AGENT_BLOCKED_OPTIONS:
            raise ValueError(f"agent command option {option_name} cannot override the goal project root")
        if "=" in part:
            index += 1
            continue
        if part in AGENT_VALUE_OPTIONS:
            if index + 1 >= len(parts) or parts[index + 1].startswith("-"):
                raise ValueError(f"agent command option {part} requires a value")
            index += 2
            continue
        index += 1


def _validate_value(label: str, value: str, allowed: set[str]) -> str:
    candidate = (value or "").strip()
    if candidate not in allowed:
        allowed_values = ", ".join(sorted(allowed))
        raise ValueError(f"invalid {label} '{value}'; expected one of: {allowed_values}")
    return candidate


def _positive_int(label: str, value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive integer") from exc
    if parsed < 1:
        raise ValueError(f"{label} must be a positive integer")
    return parsed


def _encode_claimed_paths(paths: Iterable[str] | str) -> str:
    if isinstance(paths, str):
        if not paths.strip():
            return "[]"
        items = [item.strip() for item in paths.split(",") if item.strip()]
    else:
        items = [str(item).strip() for item in paths if str(item).strip()]
    import json
    return json.dumps(items, sort_keys=True)


def _decode_claimed_paths(raw: str) -> list[str]:
    import json
    try:
        decoded = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list):
        return []
    return [str(item) for item in decoded if str(item)]


def _normalize_claimed_paths(paths: Iterable[str] | str, *, project_root: str = "") -> list[str]:
    if isinstance(paths, str):
        raw_items = [item.strip() for item in paths.split(",") if item.strip()]
    else:
        raw_items = [str(item).strip() for item in paths if str(item).strip()]

    normalized: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        path = _normalize_claimed_path(raw, project_root=project_root)
        if path and path not in seen:
            normalized.append(path)
            seen.add(path)
    return normalized


def _normalize_claimed_path(raw: str, *, project_root: str = "") -> str:
    value = str(raw).strip()
    if not value:
        return ""
    candidate = Path(value).expanduser()
    root = Path(project_root).expanduser().resolve() if project_root else None
    if candidate.is_absolute():
        if root is None:
            raise ValueError("absolute claimed paths require a project root")
        try:
            return _clean_claimed_path(candidate.resolve(strict=False).relative_to(root))
        except ValueError as exc:
            raise ValueError(f"claimed path must stay inside project root: {raw}") from exc
    return _clean_claimed_path(value)


def _clean_claimed_path(path: object) -> str:
    normalized = posixpath.normpath(str(path).replace("\\", "/").strip())
    if normalized in {"", "."}:
        return "."
    if normalized == ".." or normalized.startswith("../"):
        raise ValueError(f"claimed path must stay inside project root: {path}")
    return normalized.strip("/")


def _claimed_path_conflicts(goal_id: str, claimed_paths: list[str], *, project_root: str = "") -> list[str]:
    if not claimed_paths:
        return []
    conflicts: list[str] = []
    for task in db.goal_tasks(goal_id):
        if task.status in FINAL_TASK_STATUSES:
            continue
        existing = _normalize_claimed_paths(_decode_claimed_paths(task.claimed_paths), project_root=project_root)
        overlap = [
            claimed
            for claimed in claimed_paths
            for current in existing
            if _paths_overlap(claimed, current)
        ]
        if overlap:
            conflicts.append(f"{task.task_id} already claims overlapping paths: {', '.join(sorted(set(overlap)))}")
    return conflicts


def _paths_overlap(left: str, right: str) -> bool:
    if left == "." or right == ".":
        return True
    return left == right or left.startswith(f"{right}/") or right.startswith(f"{left}/")


def _comment_field(text: object, *, limit: int) -> str:
    compact = _one_line(str(text), limit=limit)
    compact = compact.replace("\r", " ").replace("\n", " ")
    compact = compact.replace("`", "'").replace("$(", "(")
    return compact


def _format_ts(ts: float) -> str:
    if not ts:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _one_line(text: str, limit: int = 180) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "..."
