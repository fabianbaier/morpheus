"""Morpheus CLI — typer-based entry points."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.markdown import Markdown
from rich.table import Table

from morpheus import activity as activity_mod
from morpheus import ask as ask_mod
from morpheus import brief as brief_mod
from morpheus import config as cfg_mod
from morpheus import context as ctx_mod
from morpheus import core, daemon as daemon_mod, db, goals as goals_mod, iterm_client, ledger as ledger_mod, loops as loops_mod, mission_graph as graph_mod, naming, notifier as notifier_mod, prd_runs, recall_eval, tenant as tenant_mod, trigger as trigger_mod, __version__

app = typer.Typer(
    name="morpheus",
    help="Mission control for your iTerm tabs.",
    no_args_is_help=False,
    add_completion=False,
)
console = Console()
projects_app = typer.Typer(help="List, prune, and delete project tenants.")
app.add_typer(projects_app, name="projects")
remote_app = typer.Typer(help="ChatGPT Apps / remote-device bridge helpers.")
app.add_typer(remote_app, name="remote")


# ───────── default entry: launch dashboard ─────────

@app.callback(invoke_without_command=True)
def _default(
    ctx: typer.Context,
    all_projects: bool = typer.Option(False, "--all", help="Show the global fleet instead of the cwd project."),
):
    if ctx.invoked_subcommand is None:
        from morpheus import dashboard
        dashboard.run(show_all=all_projects)


@app.command()
def dashboard(
    all_projects: bool = typer.Option(False, "--all", help="Show the global fleet instead of the cwd project."),
):
    """Launch the Matrix-rain dashboard (same as running `morpheus` with no args)."""
    from morpheus import dashboard as dash_mod
    dash_mod.run(show_all=all_projects)


# ───────── version ─────────

@app.command()
def version():
    """Print the morpheus version."""
    console.print(f"morpheus {__version__}")


def _display_path(path: str) -> str:
    if not path:
        return ""
    try:
        home = Path.home().resolve()
        resolved = Path(path).expanduser().resolve()
        return "~/" + str(resolved.relative_to(home)) if resolved != home and home in resolved.parents else str(resolved)
    except Exception:
        return path


def _resolve_project_ref(ref: str) -> tuple[Optional[db.ProjectTenant], str]:
    tenants = db.all_project_tenants(include_archived=True)
    if not tenants:
        return None, "no project tenants recorded"
    lowered = ref.lower()
    resolved_path = ""
    try:
        resolved_path = str(Path(ref).expanduser().resolve())
    except Exception:
        resolved_path = ""

    exact = [
        tenant for tenant in tenants
        if tenant.tenant_id == ref or tenant.root_path == ref or tenant.root_path == resolved_path
    ]
    if len(exact) == 1:
        return exact[0], ""
    if len(exact) > 1:
        return None, f"ambiguous project ref '{ref}'"

    named = [tenant for tenant in tenants if tenant.name.lower() == lowered]
    if len(named) == 1:
        return named[0], ""
    if len(named) > 1:
        names = ", ".join(f"{tenant.name}:{tenant.tenant_id}" for tenant in named)
        return None, f"ambiguous project name '{ref}' ({names})"

    prefixed = [tenant for tenant in tenants if tenant.tenant_id.startswith(ref)]
    if len(prefixed) == 1:
        return prefixed[0], ""
    if len(prefixed) > 1:
        ids = ", ".join(tenant.tenant_id for tenant in prefixed)
        return None, f"ambiguous project id prefix '{ref}' ({ids})"

    return None, f"unknown project '{ref}'"


def _remote_tenant_id(all_projects: bool = False, project_ref: Optional[str] = None) -> Optional[str]:
    tenant_mod.backfill_known_tenants()
    if project_ref:
        project, error = _resolve_project_ref(project_ref)
        if project is None:
            console.print(f"[red]{escape(error)}[/red]")
            raise typer.Exit(1)
        return project.tenant_id
    if all_projects:
        return None
    return tenant_mod.ensure_project_tenant(Path.cwd()).tenant_id


def _remote_iterm_run(coro) -> Optional[dict]:
    """Run an iTerm coroutine for a remote-contract command.

    Remote commands promise JSON on stdout; an iTerm connection failure must
    become an {"ok": false} payload, never a traceback.
    """
    try:
        iterm_client.run(coro)
        return None
    except Exception as exc:
        return {
            "ok": False,
            "error": f"iTerm unavailable: {type(exc).__name__}: {exc}"[:300],
            "hint": "Is iTerm2 running with the Python API enabled? iTerm2 -> Settings -> General -> Magic.",
        }


def _project_usage_dict(usage: db.ProjectTenantUsage) -> dict:
    return {
        "live_sessions": usage.live_sessions,
        "memories": usage.memories,
        "active_memories": usage.active_memories,
        "archived_memories": usage.archived_memories,
        "events": usage.events,
        "artifacts": usage.artifacts,
        "edges": usage.edges,
        "notes": usage.notes,
        "goal_runs": usage.goal_runs,
        "goal_tasks": usage.goal_tasks,
        "loops": usage.loops,
        "loop_runs": usage.loop_runs,
        "graph_rows": usage.graph_rows,
    }


def _remote_project_row(project: db.ProjectTenant) -> dict:
    usage = db.project_tenant_usage(project.tenant_id)
    return {
        "id": project.tenant_id,
        "tenant_id": project.tenant_id,
        "name": project.name or project.tenant_id,
        "root_path": project.root_path,
        "root_kind": project.root_kind,
        "created_at": project.created_at,
        "last_seen_at": project.last_seen_at,
        "archived": bool(project.archived_at),
        "usage": _project_usage_dict(usage),
    }


def _usage_cell(usage: db.ProjectTenantUsage) -> str:
    return (
        f"{usage.live_sessions} live, "
        f"{usage.active_memories} active, "
        f"{usage.archived_memories} archived"
    )


# ───────── watch ─────────

@app.command()
def watch(
    poll: float = typer.Option(5.0, "--poll", "-p", help="Seconds between polls."),
    no_notify: bool = typer.Option(False, "--no-notify",
                                    help="Disable macOS notifications."),
):
    """Headless watch loop. Updates tab titles + context.md every --poll seconds.

    When run from a real terminal (interactive), Ctrl-C stops it. When run by
    launchd, this is the long-lived background process.
    """
    console.print(f"[bold green]▶ MORPHEUS watching[/bold green] (poll={poll:.1f}s) — Ctrl-C to stop.")
    console.print(f"  log:    {core.LOG_PATH}")
    console.print(f"  db:     {db.DB_PATH}")
    console.print(f"  beacon: {daemon_mod.BEACON_PATH}")

    if no_notify:
        on_state = on_spawn = on_note = on_alert = None
    else:
        async def on_state(m: db.Mission, old: str, new: str):
            notifier_mod.notify_state(m.goal or "(untitled)", new, m.last_event)

        async def on_spawn(m: db.Mission):
            notifier_mod.notify_spawn(m.goal or "(untitled)", m.tab_id)

        async def on_note(n: db.Note):
            owner = db.get(n.tab_id) if n.tab_id else None
            goal = owner.goal if owner else "?"
            notifier_mod.notify_note(goal, n.text)

        async def on_alert(kind: str, mission, text: str):
            # Map v0.4 alerts to notifications.
            from morpheus.notifier import Notification, notify
            sound = "Glass" if kind == "token_snapshot" else None
            notify(Notification(
                title="🐇 morpheus",
                message=text,
                kind=kind,
                sound=sound,
            ))

    # GH-poll interval comes from config; 0 disables.
    gh_poll = float(cfg_mod.load().get("trigger", {}).get("gh_poll_secs", 0) or 0)

    try:
        core.watch_loop(
            poll_interval=poll,
            on_state_change=on_state,
            on_new_mission=on_spawn,
            on_new_note=on_note,
            on_alert=on_alert,
            gh_poll_secs=gh_poll,
        )
    except KeyboardInterrupt:
        console.print("\n[bright_black]stopped.[/bright_black]")


# ───────── spawn ─────────

@app.command()
def spawn(
    goal: str = typer.Argument(..., help="One-line goal for the session."),
    command: str = typer.Argument(..., help="Shell command to run in the new tab."),
):
    """Open a new iTerm tab, run COMMAND, register a mission card with GOAL."""
    project = tenant_mod.ensure_project_tenant(Path.cwd())
    launch_command = tenant_mod.command_in_project(command, project.root_path)

    async def _do(connection):
        info = await iterm_client.spawn_tab(connection, command=launch_command, goal=goal)
        if info is None:
            console.print("[red]failed to spawn tab — is iTerm focused?[/red]")
            raise typer.Exit(1)
        now = time.time()
        m = db.Mission(
            tab_id=info.tab_id,
            session_id=info.session_id,
            tenant_id=project.tenant_id,
            project_root=project.root_path,
            goal=goal,
            state="working",
            cmd=launch_command,
            linked_worktree=project.root_path,
            buffer_changed_at=now,
            last_event_at=now,
            created_at=now,
        )
        db.upsert(m)
        console.print(f"[green]spawned[/green] tab {info.tab_id} — goal: [bold]{goal}[/bold]")
        console.print(f"  mission: [cyan]{m.mission_id}[/cyan]")
        console.print(f"  cmd: [dim]{command}[/dim]")

    iterm_client.run(_do)


# ───────── PRD runs (v0.8 foundation) ─────────

run_app = typer.Typer(help="Start and inspect PRD-backed coordinator runs.")
app.add_typer(run_app, name="run")


@run_app.command("start")
def run_start(
    prd: Path = typer.Argument(..., help="Markdown source file to use as the parent mission."),
    command: str = typer.Option("codex", "--cmd", "-c", help="Coordinator command, e.g. 'codex' or 'claude'."),
    title: Optional[str] = typer.Option(None, "--title", "-t", help="Override the PRD title."),
):
    """Create a PRD parent mission, spawn one coordinator tab, and link it."""
    project = tenant_mod.ensure_project_tenant(Path.cwd())
    run = prd_runs.create_prd_run(prd, title=title, project=project)
    coordinator_goal = f"{run.title} coordinator"
    coordinator_cmd = tenant_mod.command_in_project(
        prd_runs.coordinator_command(command, run),
        project.root_path,
    )

    async def _do(connection):
        info = await iterm_client.spawn_tab(
            connection,
            command=coordinator_cmd,
            goal=coordinator_goal,
        )
        if info is None:
            console.print("[red]failed to spawn coordinator tab — is iTerm focused?[/red]")
            raise typer.Exit(1)

        now = time.time()
        mission = db.Mission(
            tab_id=info.tab_id,
            session_id=info.session_id,
            tenant_id=project.tenant_id,
            project_root=project.root_path,
            goal=coordinator_goal,
            state="working",
            cmd=coordinator_cmd,
            linked_worktree=project.root_path,
            buffer_changed_at=now,
            last_event_at=now,
            created_at=now,
        )
        db.upsert(mission)
        prd_runs.attach_coordinator(run, mission)
        console.print(f"[green]PRD run created[/green] [bold]{run.title}[/bold]")
        console.print(f"  parent:      [cyan]{run.parent_id}[/cyan]")
        console.print(f"  coordinator: [cyan]{mission.mission_id}[/cyan] tab {info.tab_id}")
        console.print(f"  PRD:         {run.prd_path}")
        console.print(f"  status:      {run.status_path}")
        console.print(f"  prompt:      {run.prompt_path}")

    iterm_client.run(_do)


@run_app.command("find-prds")
def run_find_prds(
    root: Path = typer.Argument(Path.cwd(), help="Worktree/root directory to scan."),
):
    """List Markdown source files Morpheus can use for PRD runs."""
    candidates = prd_runs.find_prds(root)
    if not candidates:
        console.print("[yellow]no Markdown files found[/yellow]")
        return
    table = Table(title=f"Markdown sources in {Path(root).resolve()}", header_style="bold green")
    table.add_column("#", style="cyan")
    table.add_column("path")
    for i, candidate in enumerate(candidates, start=1):
        table.add_row(str(i), candidate.label)
    console.print(table)


@run_app.command("status")
def run_status(
    ref: str = typer.Argument(..., help="PRD parent mission id/prefix, or a coordinator/worker mission id/prefix."),
    record: bool = typer.Option(True, "--record/--no-record", help="Record a status_refreshed graph event."),
):
    """Refresh and print the graph-rendered status file for a PRD run."""
    resolved = graph_mod.resolve(ref)
    if resolved is None:
        console.print(f"[red]no mission matching '{ref}'[/red]")
        raise typer.Exit(1)

    try:
        parent_id = prd_runs.prd_parent_for_mission(resolved.mission_id)
        if not parent_id:
            console.print(f"[red]mission is not part of a PRD run: {resolved.mission_id}[/red]")
            raise typer.Exit(1)
        status_path = prd_runs.update_status_from_graph(parent_id, record_event=record)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    console.print(Markdown(status_path.read_text(encoding="utf-8")))
    console.print(f"\n[green]status refreshed:[/green] {status_path}")


# ───────── autonomous goal runs (v0.9 foundation) ─────────

goal_app = typer.Typer(help="Start and control autonomous PRD/mission goal runs.")
app.add_typer(goal_app, name="goal")


@goal_app.command("start")
def goal_start(
    source: str = typer.Argument(..., help="PRD path or mission id/prefix to promote into a goal."),
    command: str = typer.Option("codex", "--cmd", "-c", help="Controller command, e.g. 'codex' or 'claude'."),
    workers: str = typer.Option("auto", "--workers", "-w", help="'auto' or maximum active worker count."),
    title: Optional[str] = typer.Option(None, "--title", "-t", help="Title for a new PRD parent mission."),
    objective: Optional[str] = typer.Option(None, "--objective", "-o", help="Override the standing objective."),
    done_definition: Optional[str] = typer.Option(None, "--done", help="Override the completion condition."),
    autonomy_level: str = typer.Option("ask_to_spawn", "--autonomy", help="observe_only, ask_to_spawn, or bounded_fanout."),
    max_turns: int = typer.Option(goals_mod.DEFAULT_MAX_TURNS, "--max-turns", help="Controller continuation turn budget."),
    max_spend_usd: float = typer.Option(0.0, "--max-spend-usd", help="Optional spend budget; 0 means unset."),
    judge_model: str = typer.Option("", "--judge-model", help="Optional cheap evaluator model/provider label."),
):
    """Create a goal run, spawn one visible controller tab, and link it."""
    try:
        max_workers = _parse_goal_workers(workers)
        project = tenant_mod.ensure_project_tenant(Path.cwd())
        bundle = goals_mod.create_goal_run(
            source,
            title=title,
            objective=objective,
            done_definition=done_definition,
            project=project,
            autonomy_level=autonomy_level,
            max_turns=max_turns,
            max_workers=max_workers,
            max_spend_usd=max_spend_usd,
            judge_model=judge_model,
        )
        owner = _goal_project(bundle.goal) or project
        controller_goal = f"{bundle.parent.title or bundle.goal.goal_id} goal controller"
        controller_cmd = tenant_mod.command_in_project(
            goals_mod.controller_command(command, bundle),
            owner.root_path,
        )
    except Exception as e:
        console.print(f"[red]goal start failed: {e}[/red]")
        raise typer.Exit(1)

    async def _do(connection):
        info = await iterm_client.spawn_tab(
            connection,
            command=controller_cmd,
            goal=controller_goal,
        )
        if info is None:
            console.print("[red]failed to spawn goal controller tab — is iTerm focused?[/red]")
            raise typer.Exit(1)

        now = time.time()
        mission = db.Mission(
            tab_id=info.tab_id,
            session_id=info.session_id,
            tenant_id=owner.tenant_id,
            project_root=owner.root_path,
            goal=controller_goal,
            state="working",
            cmd=controller_cmd,
            linked_worktree=owner.root_path,
            buffer_changed_at=now,
            last_event_at=now,
            created_at=now,
        )
        db.upsert(mission)
        goal = goals_mod.attach_controller(bundle, mission)
        ledger_mod.log_action(
            "goal_start",
            tab_id=mission.tab_id,
            details={
                "goal_id": goal.goal_id,
                "parent_mission_id": goal.parent_mission_id,
                "controller_mission_id": mission.mission_id,
                "max_turns": goal.max_turns,
                "max_workers": goal.max_workers,
                "autonomy_level": goal.autonomy_level,
            },
        )
        _refresh_context_files()
        console.print(f"[green]goal run created[/green] [bold]{bundle.parent.title or goal.goal_id}[/bold]")
        console.print(f"  goal:       [cyan]{goal.goal_id}[/cyan]")
        console.print(f"  parent:     [cyan]{goal.parent_mission_id}[/cyan]")
        console.print(f"  controller: [cyan]{mission.mission_id}[/cyan] tab {info.tab_id}")
        console.print(f"  status:     {goals_mod.bundle_for_goal(goal.goal_id).status_path}")
        console.print(f"  prompt:     {goals_mod.bundle_for_goal(goal.goal_id).prompt_path}")

    iterm_client.run(_do)


@goal_app.command("status")
def goal_status(
    ref: str = typer.Argument(..., help="Goal id/prefix, parent mission, controller, or worker mission."),
):
    """Print the graph-rendered status for a goal run."""
    goal = goals_mod.resolve_goal(ref)
    if goal is None:
        console.print(f"[red]no goal run matching '{ref}'[/red]")
        raise typer.Exit(1)
    bundle = goals_mod.bundle_for_goal(goal.goal_id)
    goals_mod.write_status_file(bundle)
    console.print(Markdown(bundle.status_path.read_text(encoding="utf-8")))
    console.print(f"\n[green]status refreshed:[/green] {bundle.status_path}")


@goal_app.command("list")
def goal_list(
    all_statuses: bool = typer.Option(False, "--all", "-a", help="Include done/failed/cleared goals."),
):
    """List autonomous goal runs."""
    project = tenant_mod.ensure_project_tenant(Path.cwd())
    rows = db.all_goal_runs(include_finished=all_statuses, tenant_id=project.tenant_id)
    if not rows and not all_statuses:
        rows = db.all_goal_runs(include_finished=False)
    if not rows:
        console.print("[dim]no goal runs yet[/dim]")
        return
    table = Table(title=f"MORPHEUS GOALS — {len(rows)}", header_style="bold green")
    table.add_column("GOAL", style="cyan", no_wrap=True)
    table.add_column("ST", no_wrap=True)
    table.add_column("TURNS", no_wrap=True)
    table.add_column("WORKERS", no_wrap=True)
    table.add_column("OBJECTIVE")
    for goal in rows:
        table.add_row(
            graph_mod.short_id(goal.goal_id),
            goal.status,
            f"{goal.turns_used}/{goal.max_turns}",
            f"{goal.active_workers}/{goal.max_workers}",
            goal.objective or goal.parent_mission_id,
        )
    console.print(table)


@goal_app.command("continue")
def goal_continue_cmd(
    ref: str = typer.Argument(..., help="Goal id/prefix, parent, controller, or worker."),
    reason: str = typer.Option("manual continuation", "--reason", "-r", help="Why this continuation is being queued."),
    cooldown: float = typer.Option(0.0, "--cooldown", help="Minimum seconds since the last continuation."),
    stage: bool = typer.Option(False, "--stage", help="Type the continuation without pressing Enter."),
):
    """Queue one bounded continuation turn into the live controller tab."""
    goal = _resolve_goal_or_exit(ref)
    controller = _goal_controller_or_exit(goal)

    async def _do(connection):
        return await _send_goal_continuation(
            connection,
            goal.goal_id,
            controller,
            reason=reason,
            cooldown_seconds=cooldown,
            submit=not stage,
        )

    sent = iterm_client.run(_do)
    if not sent:
        raise typer.Exit(1)


@goal_app.command("run-due")
def goal_run_due(
    limit: int = typer.Option(2, "--limit", "-n", help="Maximum goal controllers to nudge."),
    cooldown: float = typer.Option(goals_mod.DEFAULT_CONTINUATION_COOLDOWN_SECONDS, "--cooldown", help="Minimum seconds between continuation turns."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show due controllers without sending text."),
):
    """Nudge due idle goal controllers once. The watcher/cockpit also does this when enabled."""
    paused = [] if dry_run else goals_mod.pause_budget_exhausted_goals()
    targets = goals_mod.due_continuation_targets(cooldown_seconds=cooldown, limit=limit)
    if dry_run:
        for goal in paused:
            console.print(f"{goal.goal_id} paused: controller turn budget exhausted")
        if not targets:
            console.print("[dim]no goal controllers due[/dim]")
            return
        for target in targets:
            console.print(f"{target.goal.goal_id} → {target.controller.tab_id.split('-')[0]} ({target.reason})")
        return
    if not targets:
        console.print("[dim]no goal controllers due[/dim]")
        return

    async def _do(connection):
        sent = 0
        for target in targets:
            ok = await _send_goal_continuation(
                connection,
                target.goal.goal_id,
                target.controller,
                reason=target.reason,
                cooldown_seconds=cooldown,
                submit=True,
            )
            if ok:
                sent += 1
        return sent

    sent = iterm_client.run(_do)
    console.print(f"[green]queued[/green] {sent}/{len(targets)} goal continuation(s)")
    if sent != len(targets):
        raise typer.Exit(1)


@goal_app.command("pause")
def goal_pause(
    ref: str = typer.Argument(..., help="Goal id/prefix to pause."),
    reason: str = typer.Option("Paused by user", "--reason", "-r", help="Pause reason."),
):
    """Pause future goal continuation work without deleting history."""
    _set_goal_status_from_cli(ref, "paused", reason=reason)


@goal_app.command("resume")
def goal_resume(
    ref: str = typer.Argument(..., help="Goal id/prefix to resume."),
    reason: str = typer.Option("Resumed by user", "--reason", "-r", help="Resume reason."),
):
    """Resume a goal run and reset the controller turn window."""
    _set_goal_status_from_cli(ref, "active", reason=reason, reset_turns=True)


@goal_app.command("done")
def goal_done(
    ref: str = typer.Argument(..., help="Goal id/prefix to mark done."),
    reason: str = typer.Option("Goal done; proof recorded", "--reason", "-r", help="Completion summary."),
):
    """Mark a goal run done while preserving graph history."""
    _set_goal_status_from_cli(ref, "done", reason=reason)


@goal_app.command("clear")
def goal_clear(
    ref: str = typer.Argument(..., help="Goal id/prefix to clear."),
    reason: str = typer.Option("Cleared by user", "--reason", "-r", help="Clear reason."),
):
    """Clear the active goal loop while preserving graph history."""
    _set_goal_status_from_cli(ref, "cleared", reason=reason)


@goal_app.command("task-add")
def goal_task_add(
    ref: str = typer.Argument(..., help="Goal id/prefix, parent, controller, or worker."),
    title: str = typer.Argument(..., help="Short task title."),
    scope: str = typer.Option("", "--scope", "-s", help="Owned scope/files for this task."),
    verification: str = typer.Option("", "--verify", "-v", help="Verification required before done."),
    path: Optional[list[str]] = typer.Option(None, "--path", "-p", help="Claimed path. Repeat for multiple paths."),
):
    """Create a bounded goal worker task without spawning it yet."""
    try:
        task = goals_mod.create_task(
            ref,
            title=title,
            scope=scope,
            verification=verification,
            claimed_paths=path or (),
        )
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    ledger_mod.log_action("goal_task_add", details={"goal_id": task.goal_id, "task_id": task.task_id})
    _refresh_context_files()
    console.print(f"[green]task created[/green] {task.task_id}")


@goal_app.command("tasks")
def goal_tasks_cmd(
    ref: str = typer.Argument(..., help="Goal id/prefix, parent, controller, or worker."),
):
    """List tasks for a goal run."""
    goal = _resolve_goal_or_exit(ref)
    tasks = db.goal_tasks(goal.goal_id)
    if not tasks:
        console.print("[dim]no tasks yet[/dim]")
        return
    table = Table(title=f"GOAL TASKS — {goal.goal_id}", header_style="bold green")
    table.add_column("TASK", style="cyan", no_wrap=True)
    table.add_column("ST", no_wrap=True)
    table.add_column("WORKER", no_wrap=True)
    table.add_column("TITLE")
    table.add_column("SCOPE")
    for task in tasks:
        table.add_row(
            graph_mod.short_id(task.task_id),
            task.status,
            graph_mod.short_id(task.worker_mission_id) if task.worker_mission_id else "—",
            task.title,
            task.scope or "—",
        )
    console.print(table)


@goal_app.command("task-spawn")
def goal_task_spawn(
    task_ref: str = typer.Argument(..., help="Goal task id/prefix to spawn as a live worker."),
    command: str = typer.Option("codex", "--cmd", "-c", help="Worker command, e.g. 'codex' or 'claude'."),
    worker_goal: Optional[str] = typer.Option(None, "--goal", "-g", help="Override the iTerm tab goal label."),
):
    """Spawn a live worker tab for a planned goal task."""
    task = goals_mod.resolve_task(task_ref)
    if task is None:
        console.print(f"[red]goal task not found: {task_ref}[/red]")
        raise typer.Exit(1)
    goal = db.get_goal_run(task.goal_id)
    if goal is None:
        console.print(f"[red]goal run not found: {task.goal_id}[/red]")
        raise typer.Exit(1)
    if goal.autonomy_level == "observe_only":
        console.print("[red]goal autonomy is observe_only; worker spawn is disabled[/red]")
        raise typer.Exit(1)
    bundle = goals_mod.bundle_for_goal(goal.goal_id)
    owner = _goal_project(goal) or tenant_mod.ensure_project_tenant(Path.cwd())
    label = worker_goal or f"{task.title} goal worker"
    worker_cmd = tenant_mod.command_in_project(
        goals_mod.worker_command(command, bundle, task),
        owner.root_path,
    )

    async def _do(connection):
        info = await iterm_client.spawn_tab(connection, command=worker_cmd, goal=label)
        if info is None:
            console.print("[red]failed to spawn goal worker tab — is iTerm focused?[/red]")
            raise typer.Exit(1)
        now = time.time()
        mission = db.Mission(
            tab_id=info.tab_id,
            session_id=info.session_id,
            tenant_id=owner.tenant_id,
            project_root=owner.root_path,
            goal=label,
            state="working",
            cmd=worker_cmd,
            linked_worktree=owner.root_path,
            buffer_changed_at=now,
            last_event_at=now,
            created_at=now,
        )
        db.upsert(mission)
        updated = goals_mod.attach_worker(task.task_id, mission)
        ledger_mod.log_action(
            "goal_worker_spawn",
            tab_id=mission.tab_id,
            details={"goal_id": goal.goal_id, "task_id": updated.task_id, "worker_mission_id": mission.mission_id},
        )
        _refresh_context_files()
        console.print(f"[green]worker spawned[/green] {mission.mission_id} tab {info.tab_id}")
        console.print(f"  task:   {updated.task_id}")
        console.print(f"  status: {goals_mod.bundle_for_goal(goal.goal_id).status_path}")

    iterm_client.run(_do)


@goal_app.command("task-status")
def goal_task_status_cmd(
    task_ref: str = typer.Argument(..., help="Goal task id/prefix or worker mission id/prefix."),
    status: str = typer.Argument(..., help="planned | running | blocked | done | failed | ready_for_retry | cancelled"),
    summary: str = typer.Option("", "--summary", "-s", help="Heartbeat, blocker, or completion summary."),
):
    """Update a goal task heartbeat/status and roll it up to the goal status file."""
    try:
        task = goals_mod.set_task_status(task_ref, status, summary=summary)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    ledger_mod.log_action(
        f"goal_task_{status}",
        details={"goal_id": task.goal_id, "task_id": task.task_id, "summary": summary[:160]},
    )
    _refresh_context_files()
    console.print(f"[green]{status}[/green] task {task.task_id}")


# ───────── prompt loops ─────────

loops_app = typer.Typer(help="Configure recurring prompt loops.")
app.add_typer(loops_app, name="loops")


@loops_app.command("add")
def loops_add(
    name: str = typer.Argument(..., help="Short name for the loop."),
    prompt: str = typer.Argument(..., help="Prompt to run on each loop tick."),
    every: str = typer.Option("30m", "--every", "-e", help="Interval, e.g. 15m, 2h, daily."),
    command: str = typer.Option(loops_mod.DEFAULT_COMMAND, "--cmd", "-c", help="Command prefix or template. Use {prompt} to place the prompt."),
    target: Optional[str] = typer.Option(None, "--target", "-t", help="Mission/tab prefix to receive loop output."),
):
    """Create a recurring prompt loop. Use `morpheus loops run-due` from cron/launchd."""
    try:
        interval = loops_mod.parse_interval(every)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    target_mission_id = ""
    target_tab_id: Optional[str] = None
    project = tenant_mod.ensure_project_tenant(Path.cwd())
    if target:
        resolved = graph_mod.resolve(target)
        if resolved is None:
            console.print(f"[red]no mission matching '{target}'[/red]")
            raise typer.Exit(1)
        target_mission_id = resolved.mission_id
        target_tab_id = resolved.live[0].tab_id if resolved.live else None
        if resolved.memory.tenant_id:
            project = db.get_project_tenant(resolved.memory.tenant_id) or project

    loop = db.create_loop(
        name=name,
        prompt=prompt,
        interval_seconds=interval,
        command=command,
        tenant_id=project.tenant_id,
        project_root=project.root_path,
        target_mission_id=target_mission_id,
        target_tab_id=target_tab_id,
    )
    ledger_mod.log_action(
        "loop_create",
        tab_id=target_tab_id,
        details={
            "loop_id": loop.id,
            "name": loop.name,
            "interval_seconds": loop.interval_seconds,
            "target_mission_id": target_mission_id,
        },
    )
    if target_mission_id:
        db.add_event(
            target_mission_id,
            kind="loop_created",
            actor="morpheus",
            summary=f"Loop created: {loop.name} every {loops_mod.format_interval(interval)}",
            source_ref=f"loop:{loop.id}",
            metadata={"loop_id": loop.id, "target_tab_id": target_tab_id},
        )
    console.print(f"[green]loop #{loop.id} created[/green] {loop.name}")
    console.print(f"  every: [cyan]{loops_mod.format_interval(loop.interval_seconds)}[/cyan]")
    console.print(f"  next:  {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(loop.next_run_at))}")
    console.print(f"  cmd:   [dim]{loop.command}[/dim]")
    if target_mission_id:
        console.print(f"  target mission: [cyan]{target_mission_id}[/cyan]")
    else:
        console.print("  target: ticker/context only")


@loops_app.command("list")
def loops_list(
    all_statuses: bool = typer.Option(False, "--all", "-a", help="Include paused loops."),
    all_projects: bool = typer.Option(False, "--all-projects", help="Show loops from every project tenant."),
):
    """List configured prompt loops."""
    project = None if all_projects else tenant_mod.ensure_project_tenant(Path.cwd())
    tenant_id = "" if project is None else project.tenant_id
    rows = db.all_loops(include_paused=all_statuses, tenant_id=tenant_id)
    if not rows:
        console.print("[dim]no loops configured yet[/dim]")
        return
    table = Table(title=f"MORPHEUS LOOPS — {len(rows)}", header_style="bold green")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("ST")
    table.add_column("NAME")
    table.add_column("EVERY", no_wrap=True)
    table.add_column("NEXT", no_wrap=True)
    table.add_column("TARGET")
    table.add_column("RUNS", justify="right", no_wrap=True)
    table.add_column("LAST", overflow="fold")
    for loop in rows:
        run_count = db.loop_run_count(loop.id)
        last = loop.last_summary or "—"
        table.add_row(
            str(loop.id),
            loop.status,
            loop.name,
            loops_mod.format_interval(loop.interval_seconds),
            loops_mod.format_due(loop.next_run_at),
            _loop_target_label(loop),
            str(run_count),
            last,
        )
    console.print(table)


@loops_app.command("show")
def loops_show(
    loop_id: int = typer.Argument(..., help="Loop id to inspect."),
    history: int = typer.Option(5, "--history", "-n", help="Number of recent runs to show."),
):
    """Show loop configuration and recent run history."""
    loop = db.get_loop(loop_id)
    if loop is None:
        console.print(f"[red]no loop #{loop_id}[/red]")
        raise typer.Exit(1)
    _print_loop_detail(loop, history=history)


@loops_app.command("history")
def loops_history(
    loop_id: int = typer.Argument(..., help="Loop id to inspect."),
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum run rows to print."),
):
    """Show prior runs for one prompt loop."""
    loop = db.get_loop(loop_id)
    if loop is None:
        console.print(f"[red]no loop #{loop_id}[/red]")
        raise typer.Exit(1)
    _print_loop_history(loop, limit=limit)


@loops_app.command("run-due")
def loops_run_due(
    limit: int = typer.Option(5, "--limit", "-n", help="Maximum due loops to run."),
    timeout: int = typer.Option(loops_mod.DEFAULT_TIMEOUT_SECONDS, "--timeout", help="Seconds before one loop run times out."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show due loops without running them."),
    all_projects: bool = typer.Option(False, "--all-projects", help="Run due loops from every project tenant."),
):
    """Run due loops once. Put this command behind cron/launchd."""
    project = None if all_projects else tenant_mod.ensure_project_tenant(Path.cwd())
    tenant_id = "" if project is None else project.tenant_id
    due = db.due_loops(limit=limit, tenant_id=tenant_id)
    if dry_run:
        if not due:
            console.print("[dim]no loops due[/dim]")
            return
        for loop in due:
            console.print(f"#{loop.id} {loop.name} due now")
        return
    daemon_mod.write_loop_runner_beacon()
    runs = loops_mod.run_due(limit=limit, timeout=timeout, tenant_id=tenant_id)
    daemon_mod.write_loop_runner_beacon()
    if not runs:
        console.print("[dim]no loops due[/dim]")
        return
    for run in runs:
        status_style = "green" if run.status == "success" else "red"
        console.print(f"[{status_style}]{run.status}[/{status_style}] loop #{run.loop_id}: {run.summary}")
        console.print(f"  output: {run.output_path}")


@loops_app.command("run")
def loops_run(
    loop_id: int = typer.Argument(..., help="Loop id to run immediately."),
    timeout: int = typer.Option(loops_mod.DEFAULT_TIMEOUT_SECONDS, "--timeout", help="Seconds before the loop run times out."),
):
    """Run one loop immediately, regardless of its next due time."""
    loop = db.get_loop(loop_id)
    if loop is None:
        console.print(f"[red]no loop #{loop_id}[/red]")
        raise typer.Exit(1)
    try:
        run = loops_mod.run_loop(loop, timeout=timeout)
    except loops_mod.LoopAlreadyRunning:
        console.print(f"[yellow]loop #{loop.id} is already running[/yellow]")
        raise typer.Exit(1)
    status_style = "green" if run.status == "success" else "red"
    console.print(f"[{status_style}]{run.status}[/{status_style}] loop #{loop.id}: {run.summary}")
    console.print(f"  output: {run.output_path}")


@loops_app.command("pause")
def loops_pause(loop_id: int = typer.Argument(..., help="Loop id to pause.")):
    """Pause a prompt loop."""
    loop = db.set_loop_status(loop_id, "paused")
    if loop is None:
        console.print(f"[red]no loop #{loop_id}[/red]")
        raise typer.Exit(1)
    ledger_mod.log_action("loop_pause", tab_id=loop.target_tab_id, details={"loop_id": loop.id})
    console.print(f"[yellow]paused[/yellow] loop #{loop.id} {loop.name}")


@loops_app.command("resume")
def loops_resume(loop_id: int = typer.Argument(..., help="Loop id to resume.")):
    """Resume a prompt loop."""
    loop = db.set_loop_status(loop_id, "active")
    if loop is None:
        console.print(f"[red]no loop #{loop_id}[/red]")
        raise typer.Exit(1)
    ledger_mod.log_action("loop_resume", tab_id=loop.target_tab_id, details={"loop_id": loop.id})
    console.print(f"[green]active[/green] loop #{loop.id} {loop.name}")


@loops_app.command("edit")
def loops_edit(
    loop_id: int = typer.Argument(..., help="Loop id to update."),
    name: Optional[str] = typer.Option(None, "--name", help="New loop name."),
    prompt: Optional[str] = typer.Option(None, "--prompt", help="New prompt."),
    every: Optional[str] = typer.Option(None, "--every", "-e", help="New interval, e.g. 15m, 2h, daily."),
    command: Optional[str] = typer.Option(None, "--cmd", "-c", help="New command prefix/template."),
):
    """Edit loop name, prompt, interval, or command."""
    existing = db.get_loop(loop_id)
    if existing is None:
        console.print(f"[red]no loop #{loop_id}[/red]")
        raise typer.Exit(1)
    interval = None
    if every is not None:
        try:
            interval = loops_mod.parse_interval(every)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
    if name is None and prompt is None and interval is None and command is None:
        _print_loop_detail(existing)
        return
    loop = db.update_loop_details(
        loop_id,
        name=name,
        prompt=prompt,
        interval_seconds=interval,
        command=command,
    )
    if loop is None:
        console.print(f"[red]no loop #{loop_id}[/red]")
        raise typer.Exit(1)
    ledger_mod.log_action(
        "loop_edit",
        tab_id=loop.target_tab_id,
        details={"loop_id": loop.id, "name": loop.name},
    )
    if loop.target_mission_id:
        db.add_event(
            loop.target_mission_id,
            kind="loop_updated",
            actor="morpheus",
            summary=f"Loop updated: {loop.name}",
            source_ref=f"loop:{loop.id}",
            metadata={"loop_id": loop.id, "target_tab_id": loop.target_tab_id},
        )
    _refresh_context_files()
    console.print(f"[green]updated[/green] loop #{loop.id} {loop.name}")


@loops_app.command("join")
def loops_join(
    loop_id: int = typer.Argument(..., help="Loop id to attach."),
    target: str = typer.Argument(..., help="Mission/tab prefix to receive loop output."),
):
    """Attach a loop to a mission so future runs write events/artifacts there."""
    loop = db.get_loop(loop_id)
    if loop is None:
        console.print(f"[red]no loop #{loop_id}[/red]")
        raise typer.Exit(1)
    target_mission_id, target_tab_id = _resolve_loop_target(target)
    updated = db.set_loop_target(
        loop_id,
        target_mission_id=target_mission_id,
        target_tab_id=target_tab_id,
    )
    if updated is None:
        console.print(f"[red]no loop #{loop_id}[/red]")
        raise typer.Exit(1)
    _record_loop_join(updated)
    console.print(f"[green]joined[/green] loop #{updated.id} {updated.name} → {_loop_target_label(updated)}")


@loops_app.command("unjoin")
def loops_unjoin(loop_id: int = typer.Argument(..., help="Loop id to detach from its target.")):
    """Detach a loop back to ticker/context only."""
    loop = db.get_loop(loop_id)
    if loop is None:
        console.print(f"[red]no loop #{loop_id}[/red]")
        raise typer.Exit(1)
    old_target = loop.target_mission_id
    updated = db.set_loop_target(loop_id, target_mission_id="", target_tab_id=None)
    if updated is None:
        console.print(f"[red]no loop #{loop_id}[/red]")
        raise typer.Exit(1)
    ledger_mod.log_action(
        "loop_unjoin",
        tab_id=loop.target_tab_id,
        details={"loop_id": loop.id, "old_target_mission_id": old_target},
    )
    if old_target:
        db.add_event(
            old_target,
            kind="loop_unjoined",
            actor="morpheus",
            summary=f"Loop detached: {loop.name}",
            source_ref=f"loop:{loop.id}",
            metadata={"loop_id": loop.id, "old_target_tab_id": loop.target_tab_id},
        )
    _refresh_context_files()
    console.print(f"[yellow]unjoined[/yellow] loop #{updated.id} {updated.name} → ticker/context")


@loops_app.command("delete")
def loops_delete(
    loop_id: int = typer.Argument(..., help="Loop id to delete."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
):
    """Delete a prompt loop and its stored run rows. Output files are left on disk."""
    loop = db.get_loop(loop_id)
    if loop is None:
        console.print(f"[red]no loop #{loop_id}[/red]")
        raise typer.Exit(1)
    if not yes and not typer.confirm(f"Delete loop #{loop.id} {loop.name}? Run output files will remain on disk."):
        raise typer.Exit(1)
    deleted = db.delete_loop(loop_id)
    if deleted is None:
        console.print(f"[red]no loop #{loop_id}[/red]")
        raise typer.Exit(1)
    ledger_mod.log_action(
        "loop_delete",
        tab_id=deleted.target_tab_id,
        details={"loop_id": deleted.id, "target_mission_id": deleted.target_mission_id},
    )
    if deleted.target_mission_id:
        db.add_event(
            deleted.target_mission_id,
            kind="loop_deleted",
            actor="morpheus",
            summary=f"Loop deleted: {deleted.name}",
            source_ref=f"loop:{deleted.id}",
            metadata={"loop_id": deleted.id, "target_tab_id": deleted.target_tab_id},
        )
    _refresh_context_files()
    console.print(f"[red]deleted[/red] loop #{deleted.id} {deleted.name}")


def _resolve_loop_target(target: str) -> tuple[str, Optional[str]]:
    resolved = graph_mod.resolve(target)
    if resolved is None:
        console.print(f"[red]no mission matching '{target}'[/red]")
        raise typer.Exit(1)
    target_tab_id = resolved.live[0].tab_id if resolved.live else None
    return resolved.mission_id, target_tab_id


def _record_loop_join(loop: db.PromptLoop) -> None:
    ledger_mod.log_action(
        "loop_join",
        tab_id=loop.target_tab_id,
        details={"loop_id": loop.id, "target_mission_id": loop.target_mission_id},
    )
    if loop.target_mission_id:
        db.add_event(
            loop.target_mission_id,
            kind="loop_joined",
            actor="morpheus",
            summary=f"Loop joined: {loop.name}",
            source_ref=f"loop:{loop.id}",
            metadata={"loop_id": loop.id, "target_tab_id": loop.target_tab_id},
        )
    _refresh_context_files()


def _refresh_context_files() -> None:
    try:
        ctx_mod.write_context_file()
        ctx_mod.write_context_json()
    except Exception:
        pass


def _parse_goal_workers(value: str) -> int:
    raw = (value or "auto").strip().lower()
    if raw == "auto":
        return goals_mod.DEFAULT_MAX_WORKERS
    try:
        parsed = int(raw)
    except ValueError:
        console.print(f"[red]invalid --workers value '{value}'; use 'auto' or an integer[/red]")
        raise typer.Exit(1)
    if parsed < 1:
        console.print("[red]--workers must be at least 1[/red]")
        raise typer.Exit(1)
    return parsed


def _resolve_goal_or_exit(ref: str) -> db.GoalRun:
    goal = goals_mod.resolve_goal(ref)
    if goal is None:
        console.print(f"[red]no goal run matching '{ref}'[/red]")
        raise typer.Exit(1)
    return goal


def _goal_controller_or_exit(goal: db.GoalRun) -> db.Mission:
    if not goal.controller_mission_id:
        console.print(f"[red]goal has no controller session: {goal.goal_id}[/red]")
        raise typer.Exit(1)
    live = [
        mission for mission in db.all_missions()
        if mission.mission_id == goal.controller_mission_id
    ]
    if not live:
        console.print(f"[red]goal controller is not live: {goal.controller_mission_id}[/red]")
        raise typer.Exit(1)
    return live[0]


async def _send_goal_continuation(
    connection,
    goal_ref: str,
    controller: db.Mission,
    *,
    reason: str,
    cooldown_seconds: float,
    submit: bool,
) -> bool:
    try:
        bundle, outcome = goals_mod.reserve_continuation(
            goal_ref,
            reason=reason,
            cooldown_seconds=cooldown_seconds,
        )
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        return False
    if outcome != "reserved":
        style = "yellow" if outcome in {"too_soon", "inactive", "budget_exhausted"} else "red"
        console.print(f"[{style}]not queued[/{style}] goal {bundle.goal.goal_id}: {outcome}")
        console.print(f"  status: {bundle.status_path}")
        return False

    payload = goals_mod.continuation_text(bundle, reason=reason)
    text = iterm_client.text_with_enter(payload) if submit else payload
    results = await iterm_client.send_text_to_tabs(connection, [controller.tab_id], text)
    result = results[0] if results else None
    if result is None or not result.ok:
        error = result.error if result else "send returned no result"
        db.add_event(
            bundle.goal.parent_mission_id,
            kind="goal_continue_failed",
            actor="morpheus",
            summary=error,
            source_ref=f"goal:{bundle.goal.goal_id}",
            metadata={"goal_id": bundle.goal.goal_id, "controller_tab_id": controller.tab_id},
        )
        console.print(f"[red]send failed[/red] {controller.tab_id.split('-')[0]}: {error}")
        return False
    db.add_note(
        text=f"goal {bundle.goal.goal_id} continuation {bundle.goal.turns_used}/{bundle.goal.max_turns} queued",
        tab_id=controller.tab_id,
        session_id=controller.session_id,
        kind="goal",
    )
    ledger_mod.log_action(
        "goal_continue",
        tab_id=controller.tab_id,
        details={"goal_id": bundle.goal.goal_id, "turns_used": bundle.goal.turns_used, "submit": submit},
    )
    _refresh_context_files()
    mode = "queued" if submit else "staged"
    console.print(f"[green]{mode}[/green] goal {bundle.goal.goal_id} continuation {bundle.goal.turns_used}/{bundle.goal.max_turns}")
    console.print(f"  controller: {controller.tab_id.split('-')[0]}")
    console.print(f"  status:     {bundle.status_path}")
    return True


def _goal_project(goal: db.GoalRun) -> Optional[db.ProjectTenant]:
    if goal.tenant_id:
        project = db.get_project_tenant(goal.tenant_id)
        if project is not None:
            return project
    if goal.project_root:
        return tenant_mod.ensure_project_tenant(goal.project_root)
    return None


def _set_goal_status_from_cli(
    ref: str,
    status: str,
    *,
    reason: str,
    reset_turns: bool = False,
) -> None:
    goal = goals_mod.resolve_goal(ref)
    if goal is None:
        console.print(f"[red]no goal run matching '{ref}'[/red]")
        raise typer.Exit(1)
    try:
        bundle = goals_mod.set_status(
            goal.goal_id,
            status,
            reason=reason,
            reset_turns=reset_turns,
        )
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    ledger_mod.log_action(
        f"goal_{status}",
        details={"goal_id": bundle.goal.goal_id, "reason": reason},
    )
    _refresh_context_files()
    style = "green" if status == "active" else "yellow" if status == "paused" else "red"
    console.print(f"[{style}]{status}[/{style}] goal {bundle.goal.goal_id}")
    console.print(f"  status: {bundle.status_path}")


def _loop_target_label(loop: db.PromptLoop) -> str:
    if not loop.target_mission_id:
        return "ticker"
    suffix = f"/{loop.target_tab_id.split('-')[0]}" if loop.target_tab_id else ""
    return f"{graph_mod.short_id(loop.target_mission_id)}{suffix}"


def _print_loop_detail(loop: db.PromptLoop, history: int = 5) -> None:
    console.print(f"[bold green]loop #{loop.id} {escape(loop.name)}[/bold green]")
    console.print(f"  status:  {loop.status}")
    console.print(f"  every:   [cyan]{loops_mod.format_interval(loop.interval_seconds)}[/cyan]")
    console.print(f"  next:    {loops_mod.format_due(loop.next_run_at)} ({_format_loop_ts(loop.next_run_at)})")
    console.print(f"  target:  {_loop_target_label(loop)}")
    console.print(f"  command: [dim]{escape(loop.command)}[/dim]")
    console.print(f"  prompt:  {escape(loop.prompt)}")
    console.print(f"  created: {_format_loop_ts(loop.created_at)}")
    console.print(f"  updated: {_format_loop_ts(loop.updated_at)}")
    if loop.last_run_at:
        console.print(f"  last:    {loop.last_run_status or 'unknown'} at {_format_loop_ts(loop.last_run_at)}")
        if loop.last_summary:
            console.print(f"           {escape(loop.last_summary)}")
    _print_loop_history(loop, limit=history)


def _print_loop_history(loop: db.PromptLoop, limit: int = 20) -> None:
    runs = db.loop_runs(loop.id, limit=limit)
    if not runs:
        console.print("[dim]no runs recorded yet[/dim]")
        return
    table = Table(title=f"loop #{loop.id} run history", header_style="bold green")
    table.add_column("RUN", style="cyan", no_wrap=True)
    table.add_column("STARTED", no_wrap=True)
    table.add_column("DUR", no_wrap=True)
    table.add_column("STATUS", no_wrap=True)
    table.add_column("EXIT", no_wrap=True)
    table.add_column("OUTPUT")
    table.add_column("SUMMARY", overflow="fold")
    for run in runs:
        table.add_row(
            str(run.id),
            _format_loop_ts(run.started_at),
            _format_loop_duration(run.started_at, run.finished_at),
            run.status,
            str(run.exit_code) if run.exit_code is not None else "—",
            run.output_path or "—",
            run.summary or "—",
        )
    console.print(table)


def _format_loop_ts(ts: float) -> str:
    if not ts:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _format_loop_duration(started_at: float, finished_at: float) -> str:
    seconds = max(0, int(finished_at - started_at))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


# ───────── list ─────────

@app.command("list")
def list_cmd(
    stale_hours: float = typer.Option(4.0, "--stale", help="Hours of idle before flagged stale."),
    all_projects: bool = typer.Option(False, "--all", help="Show every project instead of the cwd project."),
):
    """List every registered tab with state, goal, age, last event."""
    tenant_mod.backfill_known_tenants()
    project = None if all_projects else tenant_mod.ensure_project_tenant(Path.cwd())

    async def _do(connection):
        live_tabs = await iterm_client.enumerate_tabs(connection)
        live_ids = {t.tab_id for t in live_tabs}

        rows = db.all_missions(tenant_id=project.tenant_id if project else None)
        if not rows:
            scope = "global" if project is None else project.name
            console.print(f"[dim]no missions registered for {scope} — start `morpheus watch` or spawn a tab.[/dim]")
            return

        title_scope = "global" if project is None else project.name
        table = Table(
            title=f"MORPHEUS — {title_scope} — {len(rows)} mission(s)",
            header_style="bold green",
            show_lines=False,
            row_styles=["", "dim"],
        )
        table.add_column("ID", style="green", no_wrap=True)
        table.add_column("ST")
        table.add_column("GOAL")
        if project is None:
            table.add_column("PROJECT")
        table.add_column("AGE", justify="right")
        table.add_column("LAST EVENT", overflow="fold")
        table.add_column("LIVE?", justify="center")
        tenants = {
            item.tenant_id: item.name or item.root_path
            for item in db.all_project_tenants(include_archived=True)
        }

        for m in rows:
            emoji = naming.STATE_EMOJI.get(m.state, "⚪")
            age = naming.format_age(naming.now_minus(m.buffer_changed_at))
            live = "✓" if m.tab_id in live_ids else "[red]✗[/red]"
            tab_short = m.tab_id.split("-")[0] if m.tab_id else "?"
            goal_disp = m.goal or "[dim]untitled[/dim]"
            stale_marker = ""
            age_secs = naming.now_minus(m.buffer_changed_at)
            if age_secs >= stale_hours * 3600 and m.state in ("idle", "finished"):
                stale_marker = " [yellow](stale)[/yellow]"
            row = [tab_short, emoji, goal_disp + stale_marker]
            if project is None:
                row.append(tenants.get(m.tenant_id, m.project_root or "unknown"))
            row.extend([age, m.last_event, live])
            table.add_row(*row)

        console.print(table)

    iterm_client.run(_do)


# ───────── projects ─────────

@projects_app.command("list")
def projects_list(
    include_archived: bool = typer.Option(False, "--include-archived", help="Include archived project tenants."),
):
    """List known project tenants and their related mission graph rows."""
    tenant_mod.backfill_known_tenants()
    tenants = db.all_project_tenants(include_archived=include_archived)
    if not tenants:
        console.print("[dim]no project tenants recorded yet.[/dim]")
        return

    table = Table(
        title=f"MORPHEUS PROJECTS — {len(tenants)} tenant(s)",
        header_style="bold green",
        show_lines=False,
        row_styles=["", "dim"],
    )
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("NAME", no_wrap=True)
    table.add_column("USAGE")
    table.add_column("ROWS", justify="right")
    table.add_column("ROOT")

    for tenant in tenants:
        usage = db.project_tenant_usage(tenant.tenant_id)
        status = " [red](archived)[/red]" if tenant.archived_at else ""
        table.add_row(
            tenant.tenant_id,
            f"{tenant.name or '(unnamed)'}{status}",
            _usage_cell(usage),
            str(usage.graph_rows),
            _display_path(tenant.root_path),
        )
    console.print(table)


@projects_app.command("prune")
def projects_prune(
    yes: bool = typer.Option(False, "--yes", "-y", help="Delete empty project tenants without prompting."),
    include_archived: bool = typer.Option(False, "--include-archived", help="Also prune archived empty tenants."),
):
    """Delete project tenant rows that have no related mission graph state."""
    tenant_mod.backfill_known_tenants()
    candidates = db.empty_project_tenants(include_archived=include_archived)
    if not candidates:
        console.print("[dim]no empty project tenants to prune.[/dim]")
        return

    console.print("[bold]Empty project tenants:[/bold]")
    for tenant in candidates:
        console.print(f"  • [cyan]{tenant.tenant_id}[/cyan] {tenant.name or '(unnamed)'}  [dim]{_display_path(tenant.root_path)}[/dim]")

    if not yes:
        ans = typer.prompt("\nDelete these empty project tenants? [y/N]", default="N")
        if ans.strip().lower() != "y":
            console.print("[dim]aborted.[/dim]")
            return

    results = db.prune_empty_project_tenants(include_archived=include_archived)
    ledger_mod.log_action(
        "project_prune",
        details={
            "deleted": [result.tenant_id for result in results],
            "count": len(results),
        },
    )
    console.print(f"[green]pruned {len(results)} empty project tenant(s)[/green]")


@projects_app.command("delete")
def projects_delete(
    project: str = typer.Argument(..., help="Project tenant id, id prefix, name, or root path."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Delete without prompting."),
    close_live: bool = typer.Option(False, "--close-live", help="Close live iTerm tabs for this project before purging DB rows."),
):
    """Delete one project tenant and all related Morpheus-owned DB rows."""
    tenant_mod.backfill_known_tenants()
    tenant, error = _resolve_project_ref(project)
    if tenant is None:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(1)

    usage = db.project_tenant_usage(tenant.tenant_id)
    console.print(
        f"[bold]Project:[/bold] {tenant.name or tenant.tenant_id} "
        f"[dim]{tenant.tenant_id}[/dim]\n"
        f"[bold]Root:[/bold] {_display_path(tenant.root_path)}\n"
        f"[bold]Usage:[/bold] {_usage_cell(usage)}; {usage.graph_rows} related DB row(s)"
    )
    if usage.live_sessions and not close_live:
        console.print(
            "[red]project still has live session rows; use --close-live or switch to it and close/prune sessions first[/red]"
        )
        raise typer.Exit(1)

    if not yes:
        ans = typer.prompt("\nDelete this project tenant and related DB rows? [y/N]", default="N")
        if ans.strip().lower() != "y":
            console.print("[dim]aborted.[/dim]")
            return

    if close_live and usage.live_sessions:
        async def _close(connection):
            closed = 0
            failed: list[str] = []
            for mission in db.all_missions(tenant_id=tenant.tenant_id):
                ok = await iterm_client.close_tab(connection, mission.tab_id)
                if ok:
                    closed += 1
                else:
                    failed.append(mission.tab_id)
            if failed:
                console.print(f"[red]failed to close live tabs: {', '.join(failed)}[/red]")
                raise typer.Exit(1)
            console.print(f"[green]closed {closed} live tab(s)[/green]")

        iterm_client.run(_close)

    result = db.delete_project_tenant(
        tenant.tenant_id,
        allow_live=close_live or usage.live_sessions == 0,
    )
    if result.blocked_reason:
        console.print(f"[red]{result.blocked_reason}[/red]")
        raise typer.Exit(1)

    ledger_mod.log_action(
        "project_delete",
        details={
            "tenant_id": tenant.tenant_id,
            "root_path": tenant.root_path,
            "deleted": result.deleted,
        },
    )
    console.print(
        f"[green]deleted project tenant {tenant.name or tenant.tenant_id}; "
        f"removed {result.total_deleted} DB row(s)[/green]"
    )


@projects_app.command("nuke")
def projects_nuke(
    project: str = typer.Argument(..., help="Project tenant id, id prefix, name, or root path."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Nuke without prompting."),
):
    """Close live tabs for one project, then purge all related Morpheus DB rows."""
    projects_delete(project=project, yes=yes, close_live=True)


# ───────── prune ─────────

@app.command()
def prune(
    older_than_hours: float = typer.Option(4.0, "--older-than", "-o",
                                           help="Hours of idle to consider stale."),
    yes: bool = typer.Option(False, "--yes", "-y",
                             help="Close all candidates without prompting."),
    all_projects: bool = typer.Option(False, "--all", help="Prune every project instead of the cwd project."),
):
    """Close stale iTerm tabs (idle/finished, idle > --older-than)."""
    project = None if all_projects else tenant_mod.ensure_project_tenant(Path.cwd())

    async def _do(connection):
        live = await iterm_client.enumerate_tabs(connection)
        live_by_id = {t.tab_id: t for t in live}

        candidates = []
        for m in db.all_missions(tenant_id=project.tenant_id if project else None):
            if m.tab_id not in live_by_id:
                continue
            if m.state not in ("idle", "finished"):
                continue
            age_secs = naming.now_minus(m.buffer_changed_at)
            if age_secs < older_than_hours * 3600:
                continue
            candidates.append((m, age_secs))

        if not candidates:
            console.print("[dim]no stale tabs.[/dim]")
            return

        console.print(f"[bold]Stale candidates (idle >{older_than_hours:g}h):[/bold]")
        for m, age_secs in candidates:
            console.print(
                f"  • {naming.STATE_EMOJI.get(m.state, '⚪')} "
                f"[green]{m.tab_id.split('-')[0]}[/green]  "
                f"{m.goal or '(untitled)'}  "
                f"[dim]{naming.format_age(age_secs)}[/dim]"
            )

        if not yes:
            ans = typer.prompt("\nClose all? [y/N]", default="N")
            if ans.strip().lower() != "y":
                console.print("[dim]aborted.[/dim]")
                return

        closed = 0
        for m, _ in candidates:
            ok = await iterm_client.close_tab(connection, m.tab_id)
            if ok:
                db.delete(m.tab_id)
                closed += 1
        console.print(f"[green]closed {closed}/{len(candidates)} tabs[/green]")

    iterm_client.run(_do)


# ───────── snapshot ─────────

@app.command()
def snapshot(
    tab_id: str = typer.Argument(..., help="Tab ID (or short prefix, e.g. 'tab1')."),
    out: Optional[Path] = typer.Option(None, "--out", "-o",
                                        help="Output file (default: ~/.morpheus/snapshots/{ts}-{id}.md)."),
):
    """Dump a tab's mission + buffer to markdown — useful before closing a token-heavy session."""

    async def _do(connection):
        live = await iterm_client.enumerate_tabs(connection)
        candidates = [t for t in live if t.tab_id == tab_id or t.tab_id.startswith(tab_id)]
        if not candidates:
            console.print(f"[red]no tab matching '{tab_id}'[/red]")
            raise typer.Exit(1)
        if len(candidates) > 1:
            console.print(f"[red]ambiguous — '{tab_id}' matches {len(candidates)} tabs[/red]")
            raise typer.Exit(1)

        tab = candidates[0]
        m = db.get(tab.tab_id) or db.Mission(tab_id=tab.tab_id)

        ts = time.strftime("%Y-%m-%dT%H-%M-%S")
        out_path = out
        if out_path is None:
            snap_dir = Path.home() / ".morpheus" / "snapshots"
            snap_dir.mkdir(parents=True, exist_ok=True)
            short_id = tab.tab_id.split("-")[0]
            out_path = snap_dir / f"{ts}-{short_id}.md"

        body = (
            f"# Morpheus snapshot — {ts}\n\n"
            f"- **Tab**: `{tab.tab_id}`\n"
            f"- **Goal**: {m.goal or '(untitled)'}\n"
            f"- **State**: {m.state}\n"
            f"- **Last event**: {m.last_event}\n"
            f"- **Cmd**: `{m.cmd or '?'}`\n"
            f"- **Buffer-changed-at**: {time.ctime(m.buffer_changed_at) if m.buffer_changed_at else '?'}\n\n"
            f"## Buffer (tail)\n\n```\n{tab.buffer}\n```\n"
        )
        out_path.write_text(body)
        if m.mission_id:
            db.add_artifact(
                m.mission_id,
                kind="snapshot",
                path_or_url=str(out_path),
                status="unknown",
                summary=f"Snapshot for {m.goal or tab.tab_id}",
            )
        console.print(f"[green]snapshot written:[/green] {out_path}")

    iterm_client.run(_do)


# ───────── context (cross-session awareness) ─────────


def _current_iterm_session_id() -> Optional[str]:
    """Best-effort: read the iTerm session ID for the tab this command runs in."""
    return os.environ.get("ITERM_SESSION_ID")


def _tab_id_for_session(session_id: str) -> Optional[str]:
    """Look up the mission tab_id that owns a given iTerm session_id."""
    for m in db.all_missions():
        if m.session_id == session_id:
            return m.tab_id
    return None


def _broadcast_payload(text: str, *, submit: bool) -> str:
    payload = f"[morpheus broadcast] {text}"
    return iterm_client.text_with_enter(payload) if submit else payload


def _resolve_broadcast_targets(
    refs: Optional[list[str]],
    *,
    include_self: bool,
    self_session_id: Optional[str] = None,
) -> tuple[list[db.Mission], list[str]]:
    live = db.all_missions()
    self_session_id = self_session_id if self_session_id is not None else _current_iterm_session_id()
    self_tab_id = _tab_id_for_session(self_session_id) if self_session_id else None
    errors: list[str] = []
    targets: list[db.Mission] = []
    seen: set[str] = set()

    def add(mission: db.Mission) -> None:
        if not mission.tab_id or mission.tab_id in seen:
            return
        if not include_self and (
            (self_tab_id and mission.tab_id == self_tab_id)
            or (self_session_id and mission.session_id == self_session_id)
        ):
            return
        seen.add(mission.tab_id)
        targets.append(mission)

    if refs:
        for ref in refs:
            ref = ref.strip()
            matches = [
                mission for mission in live
                if mission.tab_id == ref
                or mission.tab_id.startswith(ref)
                or mission.mission_id == ref
                or mission.mission_id.startswith(ref)
            ]
            if not matches:
                errors.append(f"no live mission matching '{ref}'")
                continue
            for mission in matches:
                add(mission)
    else:
        for mission in sorted(live, key=lambda m: (m.goal or "", m.tab_id)):
            add(mission)

    return targets, errors


def _print_broadcast_targets(selected: list[db.Mission]) -> None:
    table = Table(title="Broadcast targets", header_style="bold green")
    table.add_column("tab", style="cyan")
    table.add_column("state")
    table.add_column("goal")
    for mission in selected:
        table.add_row(mission.tab_id.split("-")[0], mission.state or "unknown", mission.goal or "(untitled)")
    console.print(table)


context_app = typer.Typer(help="Cross-session snapshot and ambient context signals.")
app.add_typer(context_app, name="context")


@context_app.callback(invoke_without_command=True)
def context(
    ctx: typer.Context,
    fmt: str = typer.Option("md", "--format", "-f", help="md | json | short"),
    refresh: bool = typer.Option(False, "--refresh", "-r",
                                  help="Force re-poll iTerm before printing (slower)."),
    all_projects: bool = typer.Option(False, "--all", help="Show every project instead of the cwd project."),
):
    """Print the shared cross-session snapshot.

    Default reads ~/.morpheus/context.md which the watch loop maintains every
    few seconds. --refresh forces a live re-poll (use sparingly).
    Subcommands (add/latest/list) manage ambient context signals instead.
    """
    if ctx.invoked_subcommand is not None:
        return
    if refresh:
        # Live re-poll — pulls fresh tabs + state, writes file, then continues.
        async def _do(connection):
            log = core.setup_logging()
            await core._tick(connection, log)
        try:
            iterm_client.run(_do)
        except Exception as e:
            console.print(f"[yellow]warning: live refresh failed ({e}); falling back to cached.[/yellow]")

    self_session = _current_iterm_session_id()
    self_tab = _tab_id_for_session(self_session) if self_session else None
    tenant_mod.backfill_known_tenants()
    project = None if all_projects else tenant_mod.ensure_project_tenant(Path.cwd())
    tenant_id = project.tenant_id if project else None

    if fmt == "json":
        console.print_json(json.dumps(ctx_mod.build_json(self_tab, self_session, tenant_id=tenant_id)))
    elif fmt == "short":
        console.print(ctx_mod.build_short(self_tab, tenant_id=tenant_id))
    else:
        md = ctx_mod.build_markdown(self_tab, self_session, tenant_id=tenant_id)
        # Render with Rich's markdown for terminal display.
        console.print(Markdown(md))


def _signal_row(signal) -> tuple[str, str, str, str]:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(signal.ts))
    payload = json.dumps(signal.payload, separators=(",", ":"))
    if len(payload) > 120:
        payload = payload[:117] + "..."
    return (str(signal.id), signal.kind, stamp, payload)


@context_app.command("add")
def context_add(
    kind: str = typer.Option(..., "--kind", help="Signal kind, e.g. location."),
    data: str = typer.Option(..., "--data", help="Signal payload as a JSON object."),
):
    """Store one ambient context signal (phone/glasses sensor reading)."""
    from morpheus import signals as signals_mod

    try:
        # Length check BEFORE json.loads: parsing a huge/deep string costs
        # memory and can hit the recursion limit; anything over twice the
        # stored payload cap can never be accepted anyway.
        if len(data) > 2 * signals_mod.PAYLOAD_MAX_CHARS:
            raise ValueError(
                f"data too large (> {2 * signals_mod.PAYLOAD_MAX_CHARS} chars)")
        payload = json.loads(data)
        if not isinstance(payload, dict):
            raise ValueError("data must be a JSON object")
        signal_id = signals_mod.add_signal(kind, payload)
    except (ValueError, json.JSONDecodeError, RecursionError) as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]stored[/green] context signal #{signal_id} ({escape(kind)})")


@context_app.command("latest")
def context_latest(
    kind: Optional[str] = typer.Option(None, "--kind", help="Signal kind; omit for latest per kind."),
):
    """Show the newest context signal (per kind, or for one kind)."""
    from morpheus import signals as signals_mod

    if kind:
        signal = signals_mod.latest(kind)
        if signal is None:
            console.print(f"[yellow]no '{escape(kind)}' signals yet.[/yellow]")
            raise typer.Exit(1)
        signals = [signal]
    else:
        signals = signals_mod.latest_per_kind()
        if not signals:
            console.print("[yellow]no context signals yet.[/yellow]")
            return
    table = Table(title="latest context signals")
    for column in ("id", "kind", "ts", "payload"):
        table.add_column(column)
    for signal in signals:
        table.add_row(*_signal_row(signal))
    console.print(table)


@context_app.command("list")
def context_list(
    kind: str = typer.Option(..., "--kind", help="Signal kind, e.g. location."),
    limit: int = typer.Option(20, "--limit", min=1, max=1000, help="Maximum signals to show."),
):
    """List recent context signals of one kind, newest first."""
    from morpheus import signals as signals_mod

    signals = signals_mod.recent(kind, limit=limit)
    if not signals:
        console.print(f"[yellow]no '{escape(kind)}' signals yet.[/yellow]")
        return
    table = Table(title=f"context signals · {kind}")
    for column in ("id", "kind", "ts", "payload"):
        table.add_column(column)
    for signal in signals:
        table.add_row(*_signal_row(signal))
    console.print(table)


@remote_app.command("snapshot")
def remote_snapshot(
    limit: int = typer.Option(8, "--limit", min=1, max=12, help="Maximum attention cards to include."),
    all_projects: bool = typer.Option(False, "--all", help="Show the global fleet instead of the cwd project."),
    project: Optional[str] = typer.Option(None, "--project", help="Project tenant id, name, or path."),
    compact: bool = typer.Option(False, "--compact", help="Print compact JSON."),
):
    """Print the compact remote snapshot ChatGPT/mobile/glasses surfaces should read."""
    from morpheus import remote as remote_mod

    tenant_id = _remote_tenant_id(all_projects=all_projects, project_ref=project)
    snapshot = remote_mod.fleet_snapshot(limit=limit, tenant_id=tenant_id)
    if compact:
        sys.stdout.write(json.dumps(snapshot, separators=(",", ":")) + "\n")
    else:
        console.print_json(json.dumps(snapshot))


@remote_app.command("projects")
def remote_projects(
    limit: int = typer.Option(12, "--limit", min=1, max=50, help="Maximum projects to include."),
    include_archived: bool = typer.Option(False, "--include-archived", help="Include archived project tenants."),
    compact: bool = typer.Option(False, "--compact", help="Print compact JSON."),
):
    """Print project tenants for compact remote/glasses navigation."""
    tenant_mod.backfill_known_tenants()
    current = tenant_mod.ensure_project_tenant(Path.cwd())
    projects = db.all_project_tenants(include_archived=include_archived)[:limit]
    result = {
        "current_project_id": current.tenant_id,
        "projects": [_remote_project_row(project) for project in projects],
    }
    if compact:
        sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
    else:
        console.print_json(json.dumps(result))


@remote_app.command("cards")
def remote_cards(
    limit: int = typer.Option(8, "--limit", min=1, max=12, help="Maximum attention cards to include."),
    all_projects: bool = typer.Option(False, "--all", help="Show the global fleet instead of the cwd project."),
    project: Optional[str] = typer.Option(None, "--project", help="Project tenant id, name, or path."),
    compact: bool = typer.Option(False, "--compact", help="Print compact JSON."),
):
    """Print only push-worthy attention cards."""
    from morpheus import remote as remote_mod

    tenant_id = _remote_tenant_id(all_projects=all_projects, project_ref=project)
    cards = remote_mod.attention_cards(limit=limit, tenant_id=tenant_id)
    if compact:
        sys.stdout.write(json.dumps(cards, separators=(",", ":")) + "\n")
    else:
        console.print_json(json.dumps(cards))


@remote_app.command("brief")
def remote_brief(
    ref: str = typer.Argument(..., help="Session tab_ref or mission_ref."),
    event_limit: int = typer.Option(5, "--events", min=0, max=12, help="Recent graph events to include."),
    all_projects: bool = typer.Option(False, "--all", help="Search the global fleet instead of the cwd project."),
    project: Optional[str] = typer.Option(None, "--project", help="Project tenant id, name, or path."),
):
    """Print a raw-buffer-free brief for one remote-visible session."""
    from morpheus import remote as remote_mod

    tenant_id = _remote_tenant_id(all_projects=all_projects, project_ref=project)
    result = remote_mod.session_brief(ref, tenant_id=tenant_id, event_limit=event_limit)
    if not result.get("found"):
        console.print(f"[red]{escape(str(result.get('error', 'not found')))}[/red]")
        raise typer.Exit(1)
    console.print_json(json.dumps(result))


@remote_app.command("spawn")
def remote_spawn(
    goal: str = typer.Argument(..., help="One-line goal for the new remote-started session."),
    command: str = typer.Option("codex", "--cmd", "-c", help="Command to run, e.g. codex."),
    prompt: Optional[str] = typer.Option(
        None,
        "--prompt",
        help="Initial user prompt to pass to prompt-aware commands like codex.",
    ),
    project: Optional[str] = typer.Option(None, "--project", help="Project tenant id, name, or path."),
    compact: bool = typer.Option(False, "--compact", help="Print compact JSON."),
):
    """Spawn a new Morpheus/iTerm session for a remote surface."""
    tenant_mod.backfill_known_tenants()
    if project:
        resolved, error = _resolve_project_ref(project)
        if resolved is None:
            console.print(f"[red]{escape(error)}[/red]")
            raise typer.Exit(1)
    else:
        resolved = tenant_mod.ensure_project_tenant(Path.cwd())

    initial_prompt = goal if prompt is None else prompt
    launch_command = tenant_mod.command_with_prompt_in_project(command, resolved.root_path, initial_prompt)
    payload: dict = {}

    async def _do(connection):
        nonlocal payload
        info = await iterm_client.spawn_tab(connection, command=launch_command, goal=goal)
        if info is None:
            payload = {"ok": False, "error": "failed to spawn tab"}
            return
        now = time.time()
        mission = db.Mission(
            tab_id=info.tab_id,
            session_id=info.session_id,
            tenant_id=resolved.tenant_id,
            project_root=resolved.root_path,
            goal=goal,
            state="working",
            cmd=launch_command,
            linked_worktree=resolved.root_path,
            buffer_changed_at=now,
            last_event_at=now,
            created_at=now,
        )
        db.upsert(mission)
        ledger_mod.log_action(
            "remote_spawn_session",
            tab_id=mission.tab_id,
            details={
                "goal": goal,
                "cmd": command,
                "tenant_id": resolved.tenant_id,
                "project_root": resolved.root_path,
                "prompt_chars": len(initial_prompt),
            },
        )
        payload = {
            "ok": True,
            "session": {
                "tab_ref": mission.tab_id.split("-")[0],
                "mission_ref": mission.mission_id[:12] if mission.mission_id else "",
                "tab_id": mission.tab_id,
                "mission_id": mission.mission_id,
                "session_id": mission.session_id,
                "state": mission.state,
                "goal": mission.goal,
                "cmd": command,
                "launch_cmd": launch_command,
                "prompt_chars": len(initial_prompt),
                "project": _remote_project_row(resolved),
            },
        }

    failure = _remote_iterm_run(_do)
    if failure is not None:
        payload = failure
    if not payload.get("ok"):
        if compact:
            sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
        else:
            console.print_json(json.dumps(payload))
        raise typer.Exit(1)
    if compact:
        sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    else:
        console.print_json(json.dumps(payload))


@remote_app.command("note")
def remote_note(
    text: str = typer.Argument(..., help="Short operator note text."),
    target: Optional[str] = typer.Option(None, "--target", help="Optional session tab_ref or mission_ref."),
    kind: str = typer.Option("note", "--kind", help="note | broadcast | claim"),
    all_projects: bool = typer.Option(False, "--all", help="Search the global fleet instead of the cwd project."),
    project: Optional[str] = typer.Option(None, "--project", help="Project tenant id, name, or path."),
):
    """Stage a bounded operator note through the remote-control surface."""
    from morpheus import remote as remote_mod

    tenant_id = _remote_tenant_id(all_projects=all_projects, project_ref=project)
    result = remote_mod.stage_operator_note(text, target_ref=target, kind=kind, tenant_id=tenant_id)
    if not result.get("ok"):
        console.print(f"[red]{escape(str(result.get('error', 'note failed')))}[/red]")
        raise typer.Exit(1)
    console.print_json(json.dumps(result))


def _remote_ref_to_mission(ref: str, *, tenant_id: Optional[str] = None) -> tuple[Optional[db.Mission], str]:
    from morpheus import remote as remote_mod

    return remote_mod._find_mission(ref, tenant_id=tenant_id)


def _remote_prompt_allowed(mission: db.Mission) -> bool:
    cmd = (mission.cmd or "").lower()
    return "codex" in cmd


@remote_app.command("prompt")
def remote_prompt(
    text: str = typer.Argument(..., help="Bounded prompt text to submit to a managed Codex session."),
    target: str = typer.Option(..., "--target", help="Session tab_ref or mission_ref."),
    project: Optional[str] = typer.Option(None, "--project", help="Project tenant id, name, or path."),
    compact: bool = typer.Option(False, "--compact", help="Print compact JSON."),
):
    """Submit prompt text to an existing Morpheus-managed Codex/iTerm session."""
    from morpheus import remote as remote_mod

    clean_text = " ".join(str(text or "").split())
    if not clean_text:
        payload = {"ok": False, "error": "empty prompt"}
        if compact:
            sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
        else:
            console.print_json(json.dumps(payload))
        raise typer.Exit(1)
    if len(clean_text) > 2000:
        payload = {"ok": False, "error": "prompt too long"}
        if compact:
            sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
        else:
            console.print_json(json.dumps(payload))
        raise typer.Exit(1)

    tenant_id = _remote_tenant_id(project_ref=project)
    mission, error = _remote_ref_to_mission(target, tenant_id=tenant_id)
    if mission is None:
        payload = {"ok": False, "error": error}
        if compact:
            sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
        else:
            console.print_json(json.dumps(payload))
        raise typer.Exit(1)
    if not _remote_prompt_allowed(mission):
        payload = {"ok": False, "error": "target is not a managed Codex session"}
        if compact:
            sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
        else:
            console.print_json(json.dumps(payload))
        raise typer.Exit(1)

    submitted: dict = {}

    async def _do(connection):
        nonlocal submitted
        results = await iterm_client.send_text_to_tabs(
            connection,
            [mission.tab_id],
            iterm_client.text_with_enter(clean_text),
        )
        result = results[0] if results else None
        submitted = {
            "ok": bool(result and result.ok),
            "error": "" if result and result.ok else (result.error if result else "send returned no result"),
        }

    failure = _remote_iterm_run(_do)
    if failure is not None:
        submitted = failure
    if not submitted.get("ok"):
        payload = {"ok": False, "error": submitted.get("error", "send failed")}
        if compact:
            sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
        else:
            console.print_json(json.dumps(payload))
        raise typer.Exit(1)

    note_id = db.add_note(
        text=remote_mod._shorten(clean_text, 240),
        tab_id=mission.tab_id,
        session_id=mission.session_id,
        kind="prompt",
    )
    ledger_mod.log_action(
        "remote_prompt_sent",
        tab_id=mission.tab_id,
        details={"target": target, "text_chars": len(clean_text), "note_id": note_id},
    )
    payload = {
        "ok": True,
        "target": {
            "tab_ref": mission.tab_id.split("-")[0],
            "mission_ref": mission.mission_id[:12] if mission.mission_id else "",
            "state": mission.state,
        },
        "text_chars": len(clean_text),
        "note_id": note_id,
    }
    if compact:
        sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    else:
        console.print_json(json.dumps(payload))


@remote_app.command("output")
def remote_output(
    ref: str = typer.Argument(..., help="Session tab_ref or mission_ref."),
    project: Optional[str] = typer.Option(None, "--project", help="Project tenant id, name, or path."),
    lines: int = typer.Option(10, "--lines", min=1, max=30, help="Clean output lines to return."),
    compact: bool = typer.Option(False, "--compact", help="Print compact JSON."),
):
    """Return a cleaned latest-output view for one remote-visible session."""
    from morpheus import remote as remote_mod

    tenant_id = _remote_tenant_id(project_ref=project)
    mission, error = _remote_ref_to_mission(ref, tenant_id=tenant_id)

    payload: dict = {}

    async def _do(connection):
        nonlocal payload
        tabs = await iterm_client.enumerate_tabs(connection)
        ref_text = str(ref or "").strip()
        target_tab_id = mission.tab_id if mission is not None else ref_text
        tab = next((candidate for candidate in tabs if candidate.tab_id == target_tab_id), None)
        if tab is None and ref_text:
            tab = next(
                (
                    candidate
                    for candidate in tabs
                    if candidate.tab_id.startswith(ref_text)
                    or ref_text in (candidate.current_name or "")
                    or ref_text == candidate.session_id
                ),
                None,
            )
        if tab is None:
            payload = {"ok": False, "error": error if mission is None else "tab not found"}
            return
        cleaned = remote_mod.clean_terminal_output(tab.buffer, line_limit=lines)
        payload = {
            "ok": True,
            "session": {
                "tab_ref": (mission.tab_id if mission is not None else tab.tab_id).split("-")[0],
                "mission_ref": mission.mission_id[:12] if mission is not None and mission.mission_id else "",
                "state": mission.state if mission is not None else "unknown",
                "goal": mission.goal if mission is not None else tab.current_name,
            },
            "output": cleaned,
        }

    failure = _remote_iterm_run(_do)
    if failure is not None:
        payload = failure
    if not payload.get("ok"):
        if compact:
            sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
        else:
            console.print_json(json.dumps(payload))
        raise typer.Exit(1)
    if compact:
        sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    else:
        console.print_json(json.dumps(payload))


@remote_app.command("manifest")
def remote_manifest(
    compact: bool = typer.Option(False, "--compact", help="Print compact JSON."),
):
    """Print the draft Apps/MCP manifest and tool descriptors."""
    from morpheus import remote as remote_mod

    manifest = remote_mod.app_manifest()
    if compact:
        sys.stdout.write(json.dumps(manifest, separators=(",", ":")) + "\n")
    else:
        console.print_json(json.dumps(manifest))


@remote_app.command("widget")
def remote_widget(
    out: Optional[Path] = typer.Option(None, "--out", help="Write HTML to this path."),
    preview: bool = typer.Option(False, "--preview", help="Render a standalone preview page."),
):
    """Print or write the ChatGPT Apps live-card widget template."""
    from morpheus import remote as remote_mod

    html_text = remote_mod.html_preview() if preview else remote_mod.widget_html()
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html_text, encoding="utf-8")
        console.print(f"[green]wrote[/green] {out}")
        return
    console.print(html_text, markup=False)


# ── omnipresence remote contract (consumed by the g2-bridge) ─────────────
#
# These four commands are the exact CLI contract the Node bridge shells out
# to; shapes are frozen — change them only together with plugins/g2-bridge.

FEED_BODY_MAX_CHARS = 2000
FEED_REF_MAX_CHARS = 200
# Serialized metadata larger than this is replaced by a stub (keeping the
# judge score when extractable) so one huge item cannot blow the bridge's
# per-command output cap and take down /api/feed for every item after it.
FEED_METADATA_MAX_CHARS = 2048


def _bounded_metadata(metadata: dict) -> dict:
    try:
        encoded = json.dumps(metadata)
    except (TypeError, ValueError, RecursionError):
        return {"truncated": True}
    if len(encoded) <= FEED_METADATA_MAX_CHARS:
        return metadata
    stub: dict = {"truncated": True}
    judge_meta = metadata.get("judge")
    if isinstance(judge_meta, dict):
        score = judge_meta.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            stub["judge"] = {"score": float(score)}
    return stub


def _remote_feed_item(item) -> dict:
    """Bounded, display-safe feed item for remote surfaces (no raw buffers)."""
    metadata = item.metadata if isinstance(item.metadata, dict) else {}
    return {
        "id": int(item.id),
        "ts": float(item.ts),
        "title": str(item.title or "")[:200],
        "body": str(item.body or "")[:FEED_BODY_MAX_CHARS],
        "priority": int(item.priority or 0),
        "source_kind": str(item.source_kind or "")[:64],
        "source_ref": str(item.source_ref or "")[:FEED_REF_MAX_CHARS],
        "metadata": _bounded_metadata(metadata),
    }


def _emit_remote_payload(payload: dict, compact: bool) -> None:
    if compact:
        sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    else:
        console.print_json(json.dumps(payload))


@remote_app.command("feed")
def remote_feed(
    after: int = typer.Option(0, "--after", min=0, help="Cursor: return items with id > after."),
    limit: int = typer.Option(20, "--limit", min=1, max=200, help="Maximum items to return."),
    feed: Optional[str] = typer.Option(None, "--feed", help="Feed name (default: the configured [omni] feed)."),
    include_dismissed: bool = typer.Option(
        False, "--include-dismissed",
        help="Also return items the user already dismissed (excluded by default)."),
    compact: bool = typer.Option(False, "--compact", help="Print compact JSON."),
):
    """Print feed items for the glasses bridge (ascending by id).

    Serves the feed omnipresence routes to ([omni].feed) unless --feed
    overrides it, and hides items the user dismissed so a bridge/simulator
    restart never resurrects them.
    """
    from morpheus import feeds as feeds_mod

    feed_name = feed or cfg_mod.omni_settings()["feed"]
    exclude_dismissed = not include_dismissed
    if after > 0:
        items = feeds_mod.recent_after(after, limit, feed=feed_name,
                                       exclude_dismissed=exclude_dismissed)
    else:
        # No cursor yet: hand the client the newest `limit` items, still in
        # ascending order so it can render and adopt the last id as cursor.
        items = list(reversed(feeds_mod.recent(limit, feed=feed_name,
                                               exclude_dismissed=exclude_dismissed)))
    payload = {
        "items": [_remote_feed_item(item) for item in items],
        "latest_id": feeds_mod.latest_id(feed=feed_name),
    }
    _emit_remote_payload(payload, compact)


@remote_app.command("feed-ack")
def remote_feed_ack(
    item: int = typer.Option(..., "--item", help="Feed item id being acknowledged."),
    action: str = typer.Option(..., "--action", help="expanded | dismissed"),
    compact: bool = typer.Option(False, "--compact", help="Print compact JSON."),
):
    """Record a glasses-side expand/dismiss ack for one pushed feed item."""
    from morpheus import feeds as feeds_mod

    try:
        feeds_mod.record_ack(item, action)
    except ValueError as exc:
        _emit_remote_payload({"ok": False, "error": str(exc)}, compact)
        raise typer.Exit(1)
    _emit_remote_payload({"ok": True, "item": int(item), "action": action.strip().lower()}, compact)


@remote_app.command("context-add")
def remote_context_add(
    kind: str = typer.Option(..., "--kind", help="Signal kind, e.g. location."),
    data: str = typer.Option(..., "--data", help="Signal payload as a JSON object."),
    compact: bool = typer.Option(False, "--compact", help="Print compact JSON."),
):
    """Store one context signal posted through the bridge (/api/context)."""
    from morpheus import signals as signals_mod

    try:
        # Length check BEFORE json.loads (see context_add): bound parse cost
        # and recursion for bridge-supplied payloads too.
        if len(data) > 2 * signals_mod.PAYLOAD_MAX_CHARS:
            raise ValueError(
                f"data too large (> {2 * signals_mod.PAYLOAD_MAX_CHARS} chars)")
        payload = json.loads(data)
        if not isinstance(payload, dict):
            raise ValueError("data must be a JSON object")
        signal_id = signals_mod.add_signal(kind, payload)
    except (ValueError, json.JSONDecodeError, RecursionError) as exc:
        _emit_remote_payload({"ok": False, "error": str(exc)[:200]}, compact)
        raise typer.Exit(1)
    _emit_remote_payload({"ok": True, "id": signal_id}, compact)


@remote_app.command("omni-status")
def remote_omni_status(
    compact: bool = typer.Option(False, "--compact", help="Print compact JSON."),
):
    """Print the resolved omnipresence settings the bridge advertises."""
    settings = cfg_mod.omni_settings()
    payload = {
        "enabled": bool(settings["enabled"]),
        "threshold": float(settings["threshold"]),
        "push_per_hour": int(settings["push_per_hour"]),
        "quiet_hours": settings["quiet_hours"],
        "feed": settings["feed"],
    }
    _emit_remote_payload(payload, compact)


# ── user memory (~/.morpheus/memory.md) ──────────────────────────────────

memory_app = typer.Typer(help="User-level relevance memory (~/.morpheus/memory.md).")
app.add_typer(memory_app, name="memory")


@memory_app.command("show")
def memory_show(
    max_chars: int = typer.Option(0, "--max-chars", min=0, help="Truncate safely to N chars (0 = full file)."),
):
    """Print the user memory file."""
    from morpheus import memory as memory_mod

    text = memory_mod.top_entries(max_chars) if max_chars else memory_mod.read_memory()
    # Raw write, never console.print: rich hard-wraps at ~80 cols when stdout
    # is not a TTY, and the omni-memory agent reads this output — wrapped
    # fragments would break its never-duplicate-a-fact contract.
    if text and not text.endswith("\n"):
        text += "\n"
    sys.stdout.write(text)


@memory_app.command("path")
def memory_path():
    """Print the memory file path (creating the template if missing)."""
    from morpheus import memory as memory_mod

    console.print(str(memory_mod.ensure_file()), markup=False)


@memory_app.command("add")
def memory_add(
    text: str = typer.Argument(..., help="One-line fact to remember."),
    section: str = typer.Option("Current", "--section", help="People | Interests | Current | Never push."),
    custom_section: bool = typer.Option(
        False, "--custom-section",
        help="Allow a non-canonical section name (off by default so agents cannot invent sections)."),
):
    """Append a dated one-line fact under a section of memory.md.

    --section is restricted to the canonical sections unless --custom-section
    is passed; the omni-memory agent runs this command, and free-form section
    names would let mined feed content sprawl the memory file.
    """
    from morpheus import memory as memory_mod

    canonical = {name.lower() for name in memory_mod.SECTIONS}
    if not custom_section and " ".join(str(section or "").split()).lstrip("#").strip().lower() not in canonical:
        console.print(
            f"[red]--section must be one of {' | '.join(memory_mod.SECTIONS)} "
            "(pass --custom-section to use a custom name)[/red]")
        raise typer.Exit(1)
    try:
        line = memory_mod.append_entry(section, text)
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]added under ## {escape(section)}:[/green] {escape(line)}")


@memory_app.command("candidates")
def memory_candidates(
    limit: int = typer.Option(20, "--limit", min=1, max=100, help="Maximum recent feed items to review."),
    feed: Optional[str] = typer.Option(None, "--feed", help="Feed name (default: the configured [omni] feed)."),
):
    """Recent pushes + how the user reacted — raw material for the
    omni-memory loop (expanded = relevant signal, dismissed = negative)."""
    from morpheus import feeds as feeds_mod

    # Mine the feed omnipresence actually pushes to ([omni].feed), not a
    # hardwired 'main' — otherwise a non-default feed is never mined.
    feed_name = feed or cfg_mod.omni_settings()["feed"]
    items = feeds_mod.recent(limit, feed=feed_name)
    if not items:
        console.print("[dim]no feed items yet — nothing to mine.[/dim]")
        return
    reactions: dict[int, str] = {}
    for ack in reversed(feeds_mod.recent_acks(limit * 5)):
        reactions[ack.item_id] = ack.action  # newest ack per item wins
    lines = []
    for item in items:
        action = reactions.get(item.id, "no-ack")
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(item.ts))
        lines.append(f"[{action}] {stamp} [{item.source_kind}] {item.title}")
    # Raw write (see memory_show): the omni-memory agent parses these lines;
    # rich would hard-wrap long titles into fragments off a TTY.
    sys.stdout.write("\n".join(lines) + "\n")


@memory_app.command("log")
def memory_log(
    limit: int = typer.Option(20, "--limit", min=1, max=500, help="Maximum log entries to show."),
):
    """Show recent memory changes (what was appended, where, when)."""
    from morpheus import memory as memory_mod

    entries = memory_mod.read_log(limit)
    if not entries:
        console.print("[yellow]no memory changes logged yet.[/yellow]")
        return
    table = Table(title="memory changes (newest first)")
    for column in ("ts", "section", "text"):
        table.add_column(column)
    for entry in entries:
        table.add_row(entry["ts"], entry["section"], entry["text"])
    console.print(table)


# ── feeds: route your own loops into ambient feeds ──────────────────────

feeds_app = typer.Typer(help="Route loop output into ambient feeds (rules, recent items).")
app.add_typer(feeds_app, name="feeds")


def _require_loop(loop_id: int) -> db.PromptLoop:
    loop = db.get_loop(loop_id)
    if loop is None:
        console.print(f"[red]no loop #{loop_id}[/red]")
        raise typer.Exit(1)
    return loop


@feeds_app.command("rules")
def feeds_rules(
    feed: Optional[str] = typer.Option(None, "--feed", help="Only rules for this feed (default: all feeds)."),
):
    """List feed routing rules (which sources push into which feed, and when)."""
    from morpheus import feeds as feeds_mod

    rules = feeds_mod.rules(feed=feed)
    if not rules:
        console.print("[dim]no feed rules yet — route a loop with `morpheus feeds route <loop-id>`.[/dim]")
        return
    table = Table(title=f"FEED RULES — {len(rules)}", header_style="bold green")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("FEED")
    table.add_column("SOURCE")
    table.add_column("POLICY")
    table.add_column("THRESHOLD", justify="right")
    table.add_column("PATTERN")
    for rule in rules:
        source = f"{rule.source_kind}:{rule.source_ref}"
        if rule.source_kind == "loop":
            try:
                loop = db.get_loop(int(rule.source_ref))
            except (TypeError, ValueError):
                loop = None
            if loop is not None:
                source = f"loop #{loop.id} {loop.name}"
        threshold = ""
        if rule.policy == "on_threshold":
            threshold = f"{rule.threshold:g}" if rule.threshold > 0 else "(omni default)"
        table.add_row(str(rule.id), rule.feed, source, rule.policy,
                      threshold, rule.pattern or "")
    console.print(table)


@feeds_app.command("route")
def feeds_route(
    loop_id: int = typer.Argument(..., help="Loop id whose output should be routed."),
    policy: str = typer.Option("on_threshold", "--policy",
                               help="always | on_change | on_match | on_failure | on_threshold"),
    threshold: float = typer.Option(0.0, "--threshold",
                                    help="on_threshold only: judge score in [0,1] needed to push (0 = [omni] default)."),
    pattern: str = typer.Option("", "--pattern", help="on_match only: regex the summary must match."),
    feed: Optional[str] = typer.Option(None, "--feed", help="Feed name (default: the configured [omni] feed)."),
):
    """Create or replace the feed rule for one loop (one rule per source)."""
    from morpheus import feeds as feeds_mod

    loop = _require_loop(loop_id)
    feed_name = feed or cfg_mod.omni_settings()["feed"]
    try:
        rule = feeds_mod.set_rule("loop", str(loop.id), policy=policy,
                                  pattern=pattern, threshold=threshold,
                                  feed=feed_name)
    except (ValueError, re.error) as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1)
    detail = f"policy [cyan]{rule.policy}[/cyan]"
    if rule.policy == "on_threshold":
        detail += f" · threshold {rule.threshold:g}" if rule.threshold > 0 else " · threshold (omni default)"
    if rule.pattern:
        detail += f" · pattern {escape(rule.pattern)}"
    console.print(
        f"[green]rule #{rule.id}[/green] loop #{loop.id} {escape(loop.name)}"
        f" → feed '{escape(rule.feed)}' · {detail}")


@feeds_app.command("unroute")
def feeds_unroute(
    loop_id: int = typer.Argument(..., help="Loop id to stop routing."),
    feed: Optional[str] = typer.Option(None, "--feed", help="Only remove the rule for this feed (default: all feeds)."),
):
    """Delete the feed rule(s) for one loop."""
    from morpheus import feeds as feeds_mod

    loop = _require_loop(loop_id)
    rules = feeds_mod.rules(source_kind="loop", source_ref=str(loop.id), feed=feed)
    if not rules:
        console.print(f"[yellow]loop #{loop.id} has no feed rule — nothing to remove.[/yellow]")
        return
    for rule in rules:
        feeds_mod.delete_rule(rule.id)
        console.print(
            f"[green]removed[/green] rule #{rule.id}"
            f" (loop #{loop.id} {escape(loop.name)} → feed '{escape(rule.feed)}')")


@feeds_app.command("recent")
def feeds_recent(
    limit: int = typer.Option(20, "--limit", min=1, max=200, help="Maximum items to show."),
    feed: Optional[str] = typer.Option(None, "--feed", help="Feed name (default: the configured [omni] feed)."),
):
    """Show recent feed items, newest first."""
    from morpheus import feeds as feeds_mod

    feed_name = feed or cfg_mod.omni_settings()["feed"]
    items = feeds_mod.recent(limit, feed=feed_name)
    if not items:
        console.print(f"[dim]no items in feed '{escape(feed_name)}' yet.[/dim]")
        return
    table = Table(title=f"FEED '{feed_name}' — {len(items)} items", header_style="bold green")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("WHEN", no_wrap=True)
    table.add_column("!", no_wrap=True)
    table.add_column("SOURCE", no_wrap=True)
    table.add_column("TITLE", overflow="fold")
    for item in items:
        stamp = time.strftime("%m-%d %H:%M", time.localtime(item.ts))
        source = item.source_kind
        if item.source_ref:
            source += f":{item.source_ref}"
        table.add_row(str(item.id), stamp, "!" if item.priority > 0 else "",
                      source, item.title)
    console.print(table)


# ── omnipresence mode controls ───────────────────────────────────────────

omni_app = typer.Typer(help="Omnipresence mode — ambient pushes to connected glasses.")
app.add_typer(omni_app, name="omni")


def _print_omni_status() -> None:
    from morpheus import feeds as feeds_mod
    from morpheus import omni_templates

    settings = cfg_mod.omni_settings()
    quiet = settings["quiet_hours"]
    table = Table(title="omnipresence")
    table.add_column("setting")
    table.add_column("value")
    table.add_row("enabled", "[green]on[/green]" if settings["enabled"] else "[red]off[/red]")
    table.add_row("threshold", f"{settings['threshold']:g}")
    table.add_row("push_per_hour", str(settings["push_per_hour"]))
    table.add_row("quiet_hours", f"{quiet['start']}-{quiet['end']}" if quiet else "none")
    table.add_row("feed", settings["feed"])
    table.add_row(
        "judge_command",
        settings["judge_command"] or f"(default: {loops_mod.DEFAULT_COMMAND})",
    )
    # Phone-push escalation channel. The topic is a capability URL — never
    # print it in full; the last 6 chars are enough to recognize it.
    topic = settings["ntfy_topic"]
    # Show a recognizable tail only when the topic is long enough that the
    # tail is not the whole secret; short topics show no tail at all.
    if not topic:
        topic_display = "[dim]unset — escalation off[/dim]"
    elif len(topic) > 8:
        topic_display = f"[green]set[/green] (…{topic[-4:]})"
    else:
        topic_display = "[green]set[/green]"
    table.add_row("ntfy_topic", topic_display)
    table.add_row("ntfy_server", settings["ntfy_server"])
    table.add_row("escalate_score", f"{settings['escalate_score']:g}")
    for tpl in omni_templates.template_status():
        label = f"#{tpl['loop_id']} {tpl['status']}" if tpl["present"] else "[yellow]missing — run `morpheus omni init`[/yellow]"
        table.add_row(f"loop {tpl['name']}", label)
    table.add_row("feed rules", str(len(feeds_mod.rules(feed=settings["feed"]))))
    console.print(table)
    items = feeds_mod.recent(3, feed=settings["feed"])
    if items:
        console.print("[bold]last feed items[/bold]")
        for item in items:
            stamp = time.strftime("%H:%M", time.localtime(item.ts))
            console.print(f"  {stamp} [{item.source_kind}] {item.title}", markup=False)
    else:
        console.print("[dim]no feed items yet[/dim]")


@omni_app.command("init")
def omni_init(
    force: bool = typer.Option(False, "--force", help="Recreate the template loops from the current templates."),
):
    """Create the omnipresence template loops (idempotent, PRD §3.4).

    omni-location (5m, on_threshold feed rule) and omni-memory (hourly, no
    feed rule) are ordinary loops: visible in `morpheus loops list`,
    editable, pausable, deletable. Re-running reports existing loops instead
    of duplicating them; nothing is paused.
    """
    from morpheus import omni_templates

    settings = cfg_mod.omni_settings()
    project = tenant_mod.ensure_project_tenant(Path.cwd())
    results = omni_templates.ensure_templates(
        tenant_id=project.tenant_id,
        project_root=project.root_path,
        feed=settings["feed"],
        force=force,
    )
    for res in results:
        color = {"created": "green", "recreated": "green"}.get(res.action, "yellow")
        line = f"[{color}]{res.action}[/{color}] loop #{res.loop_id} {res.name}"
        if res.rule_id is not None:
            line += f" · feed rule #{res.rule_id} on_threshold → feed '{settings['feed']}'"
        else:
            line += " · no feed rule (feeds memory.md, not the glasses)"
        console.print(line)
    if not settings["enabled"]:
        console.print("[dim]omnipresence is off — enable pushes with `morpheus omni on`.[/dim]")


@omni_app.command("on")
def omni_on():
    """Enable omnipresence mode (persists [omni].enabled in config.toml)."""
    path = cfg_mod.set_omni_enabled(True)
    console.print(f"[green]omnipresence enabled[/green] ({path})")
    _print_omni_status()


@omni_app.command("off")
def omni_off():
    """Disable omnipresence mode (persists [omni].enabled in config.toml)."""
    path = cfg_mod.set_omni_enabled(False)
    console.print(f"[yellow]omnipresence disabled[/yellow] ({path})")
    _print_omni_status()


@omni_app.command("status")
def omni_status():
    """Show the resolved omnipresence settings."""
    _print_omni_status()


@omni_app.command("test-push")
def omni_test_push():
    """Send a test phone push through the ntfy escalation channel.

    Escalated headlines reach the glasses because the Even app mirrors phone
    notifications; this verifies the whole chain end to end.
    """
    from morpheus import push as push_mod

    settings = cfg_mod.omni_settings()
    if not settings["ntfy_topic"]:
        console.print("[yellow]escalation is off[/yellow] — no ntfy_topic set.")
        console.print(f"Set ntfy_topic in the omni section of {cfg_mod.CONFIG_PATH}")
        console.print("(pick a hard-to-guess topic; it acts as a capability URL).")
        raise typer.Exit(1)
    ok = push_mod.send_push(
        "Morpheus test push",
        "If you can read this, the escalation channel works.",
        settings=settings,
    )
    if ok:
        console.print("[green]test push sent[/green] — check the ntfy app on your phone.")
        console.print("[dim]glasses tip: whitelist ntfy in the Even app notification settings.[/dim]")
    else:
        console.print("[red]test push failed[/red] — check ntfy_server / ntfy_topic and network.")
        console.print("[dim]details: see the morpheus.push log warning.[/dim]")
        raise typer.Exit(1)


@app.command("activity")
def activity_snapshot(
    fmt: str = typer.Option("table", "--format", "-f", help="table | json | short"),
    refresh: bool = typer.Option(
        False,
        "--refresh",
        "-r",
        help="Force a live iTerm poll before reading the cached snapshot.",
    ),
    all_sessions: bool = typer.Option(
        False,
        "--all",
        help="Show all observed sessions. Reserved for tenant-aware builds; current snapshots are global.",
    ),
    tail: int = typer.Option(3, "--tail", min=0, max=10, help="Tail lines to show in table output."),
):
    """Show the cached live activity snapshot.

    The default path reads ~/.morpheus/activity.json and does not connect to
    iTerm, so agents can answer "what is everyone doing?" without reconstructing
    state from the graph or terminal API.
    """
    del all_sessions
    if refresh:
        async def _do(connection):
            log = core.setup_logging()
            await core._tick(connection, log)
        try:
            iterm_client.run(_do)
        except Exception as e:
            console.print(f"[yellow]warning: live refresh failed ({e}); falling back to cached.[/yellow]")

    snapshot = activity_mod.read_snapshot()
    sessions = [item for item in snapshot.get("sessions", []) if isinstance(item, dict)]
    fmt = fmt.lower()
    if fmt == "json":
        console.print_json(json.dumps(snapshot))
        return
    if fmt == "short":
        if not sessions:
            console.print("0 sessions — no activity snapshot yet")
            return
        parts = []
        for item in sessions:
            tab = str(item.get("tab_id") or "?").split("-")[0]
            goal = item.get("goal") or tab
            headline = item.get("headline") or item.get("last_event") or "no visible activity"
            parts.append(f"{tab} {goal}: {headline}")
        console.print(" | ".join(parts))
        return
    if fmt != "table":
        console.print("[red]--format must be table, json, or short[/red]")
        raise typer.Exit(1)

    generated_at = float(snapshot.get("generated_at") or 0)
    age = max(0.0, time.time() - generated_at) if generated_at else 0.0
    title = f"MORPHEUS activity ({len(sessions)} sessions"
    title += f", {age:.1f}s old)" if generated_at else ", no cache yet)"
    table = Table(title=title, header_style="bold green", border_style="green")
    table.add_column("tab", style="cyan", no_wrap=True)
    table.add_column("state", no_wrap=True)
    table.add_column("goal")
    table.add_column("activity")
    if tail:
        table.add_column("tail")
    for item in sessions:
        tab = str(item.get("tab_id") or "?").split("-")[0]
        tail_lines = item.get("tail_lines") if isinstance(item.get("tail_lines"), list) else []
        row = [
            tab,
            str(item.get("state") or "unknown"),
            str(item.get("goal") or "(untitled)"),
            str(item.get("headline") or item.get("last_event") or "—"),
        ]
        if tail:
            row.append("\n".join(str(line) for line in tail_lines[-tail:]))
        table.add_row(*row)
    console.print(table)


@app.command()
def note(
    text: str = typer.Argument(..., help="The note text other sessions will see."),
    tab: Optional[str] = typer.Option(None, "--tab", "-t",
                                       help="Attach to this tab_id (default: current iTerm session)."),
    kind: str = typer.Option("note", "--kind", "-k",
                              help="note | claim | broadcast"),
    targets: Optional[list[str]] = typer.Option(
        None,
        "--target",
        help="For broadcasts: target live tab/mission id or prefix. Repeat for multiple. Default: all live sessions except this one.",
    ),
    submit: bool = typer.Option(
        True,
        "--submit/--stage",
        help="For broadcasts: press Enter in target sessions. Use --stage to type without submitting.",
    ),
    direct: bool = typer.Option(
        True,
        "--direct/--context-only",
        help="For broadcasts: also type into live iTerm sessions. Use --context-only for passive context only.",
    ),
    include_self: bool = typer.Option(
        False,
        "--include-self",
        help="For broadcasts: also send to the iTerm session running this command.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="For broadcasts: show target sessions without typing anything.",
    ),
):
    """Post a cross-session note.

    `--kind broadcast` records the note and also types it into live sessions
    by default. Use `--context-only` to keep it passive.
    """
    session_id = _current_iterm_session_id()
    tab_id = tab or (_tab_id_for_session(session_id) if session_id else None)

    if not tab_id and not session_id:
        console.print(
            "[yellow]note: couldn't detect your current iTerm session "
            "($ITERM_SESSION_ID not set, no --tab given).[/yellow]\n"
            "  → posting as an unattached note."
        )

    nid = db.add_note(text=text, tab_id=tab_id, session_id=session_id, kind=kind)
    # Refresh the context file immediately so siblings see it on next read.
    try:
        ctx_mod.write_context_file()
        ctx_mod.write_context_json()
    except Exception:
        pass

    marker = {"note": "•", "claim": "⚑", "broadcast": "📡"}.get(kind, "•")
    where = tab_id.split("-")[0] if tab_id else "unattached"
    console.print(f"[green]{marker} note #{nid}[/green] [dim]({where})[/dim]: {text}")
    if kind != "broadcast" or not direct:
        return

    selected, errors = _resolve_broadcast_targets(
        targets,
        include_self=include_self,
        self_session_id=session_id,
    )
    for error in errors:
        console.print(f"[yellow]{error}[/yellow]")
    if not selected:
        console.print("[yellow]broadcast note recorded, but no live target sessions were found[/yellow]")
        return

    _print_broadcast_targets(selected)
    if dry_run:
        console.print("[yellow]dry run:[/yellow] broadcast note recorded, no text sent")
        return

    payload = _broadcast_payload(text, submit=submit)
    tab_ids = [mission.tab_id for mission in selected]

    async def _do(connection):
        return await iterm_client.send_text_to_tabs(connection, tab_ids, payload)

    results = iterm_client.run(_do)
    ok = [result for result in results if result.ok]
    failed = [result for result in results if not result.ok]
    for result in failed:
        console.print(f"[red]failed[/red] {result.tab_id.split('-')[0]}: {result.error}")

    ledger_mod.log_action(
        "broadcast_direct",
        tab_id=tab_id,
        details={
            "note_id": nid,
            "targets": [result.tab_id for result in ok],
            "submit": submit,
            "text": text[:160],
        },
    )
    mode = "submitted" if submit else "staged"
    console.print(f"[green]{mode} broadcast[/green] to {len(ok)}/{len(results)} session(s)")
    if failed:
        raise typer.Exit(1)


@app.command()
def notes(
    limit: int = typer.Option(15, "--limit", "-n", help="How many recent notes to show."),
    tab: Optional[str] = typer.Option(None, "--tab", "-t",
                                       help="Filter to a specific tab_id (or prefix)."),
):
    """List recent cross-session notes."""
    if tab:
        # Resolve prefix to a full tab_id.
        matches = [m for m in db.all_missions() if m.tab_id.startswith(tab)]
        if not matches:
            console.print(f"[red]no tab matching '{tab}'[/red]")
            raise typer.Exit(1)
        all_notes = []
        for m in matches:
            all_notes.extend(db.notes_for_tab(m.tab_id, limit=limit))
        all_notes.sort(key=lambda n: -n.created_at)
        all_notes = all_notes[:limit]
    else:
        all_notes = db.recent_notes(limit=limit)

    if not all_notes:
        console.print("[dim]no notes yet.[/dim]")
        return

    by_tab = {m.tab_id: m for m in db.all_missions()}
    for n in all_notes:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(n.created_at))
        tab_short = (n.tab_id or "?").split("-")[0]
        goal = by_tab.get(n.tab_id, db.Mission(tab_id="")).goal or "(unknown)"
        marker = {"note": "•", "claim": "⚑", "broadcast": "📡"}.get(n.kind, "•")
        console.print(f"[dim]{ts}[/dim]  {marker}  [green]{tab_short}[/green]  [bold]{goal}[/bold]  {n.text}")


# ───────── brief ─────────

@app.command()
def brief(
    out: Optional[Path] = typer.Option(None, "--out", "-o",
                                        help="Write the brief to this file (also prints)."),
    notify: bool = typer.Option(False, "--notify", "-n",
                                 help="Push a macOS notification with the brief summary."),
    no_llm: bool = typer.Option(False, "--no-llm",
                                 help="Skip the claude-p call; print the template-only brief."),
    no_gh: bool = typer.Option(False, "--no-gh",
                                help="Skip the GH review-queue lookup."),
):
    """Generate a brief digest of current state (sessions + GH queue + notes)."""
    body = brief_mod.generate(use_llm=not no_llm, include_gh=not no_gh)
    # Render as markdown to the terminal.
    console.print(Markdown(body))
    if out:
        out.write_text(body)
        console.print(f"\n[green]wrote {len(body):d} bytes →[/green] {out}")
    if notify:
        # Just send the first non-empty line of the brief as the notification body.
        summary = next((ln for ln in body.splitlines() if ln.strip() and not ln.startswith("#")),
                       "morpheus brief ready — see your terminal.")
        ok = notifier_mod.notify_brief(summary)
        if not ok:
            console.print("[yellow]notification not delivered (terminal-notifier missing?)[/yellow]")


# ───────── ask ─────────

@app.command()
def ask(
    query: str = typer.Argument(..., help="Question to ask Morpheus about current state."),
    no_llm: bool = typer.Option(False, "--no-llm",
                                 help="Skip claude/codex — print state snapshot only."),
):
    """Ask morpheus a question about your current mission state."""
    out = ask_mod.ask(query, use_llm=not no_llm)
    console.print(Markdown(out))


# ───────── trigger (one-shot GH poll) ─────────

@app.command("poll-prs")
def poll_prs():
    """One-shot GH review-queue poll. Respects config.trigger.spawn_from_gh_pr.

    Normally the watch daemon polls automatically on config.trigger.gh_poll_secs;
    this command runs a single cycle on demand.
    """
    async def _do(connection):
        async def _alert(kind, mission, text):
            color = "yellow" if kind == "new_pr" else ("red" if "error" in kind else "green")
            console.print(f"[{color}]🐇 {kind}:[/{color}] {text}")
        n = await trigger_mod.poll_and_handle(connection, on_alert=_alert)
        console.print(f"[green]{n} new PR(s) discovered[/green]")
    iterm_client.run(_do)


# ───────── ledger ─────────

ledger_app = typer.Typer(help="Show recent costs / actions / today's spend.")
app.add_typer(ledger_app, name="ledger")


@ledger_app.command("costs")
def ledger_costs(
    limit: int = typer.Option(50, "--limit", "-n"),
):
    """Recent LLM cost ledger entries."""
    rows = ledger_mod.recent_costs(limit=limit)
    if not rows:
        console.print("[dim]no cost entries yet.[/dim]")
        return
    table = Table(header_style="bold green", border_style="green")
    table.add_column("when", style="bright_black", no_wrap=True)
    table.add_column("kind", style="cyan")
    table.add_column("description")
    table.add_column("tokens", justify="right")
    table.add_column("$", justify="right", style="bright_yellow")
    total = 0.0
    for r in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.ts))
        table.add_row(when, r.kind, r.description, f"{r.tokens_estimate:,}", f"{r.dollars:.4f}")
        total += r.dollars
    console.print(table)
    console.print(f"\n[bold]today so far:[/bold] [bright_yellow]${ledger_mod.daily_dollar_total():.4f}[/bright_yellow]   "
                   f"[dim]({len(rows)} most-recent entries totaled ${total:.4f})[/dim]")


@ledger_app.command("actions")
def ledger_actions(
    limit: int = typer.Option(50, "--limit", "-n"),
):
    """Recent action ledger entries (every spawn/kill/note/etc)."""
    rows = ledger_mod.recent_actions(limit=limit)
    if not rows:
        console.print("[dim]no action entries yet.[/dim]")
        return
    table = Table(header_style="bold green", border_style="green")
    table.add_column("when", style="bright_black", no_wrap=True)
    table.add_column("action", style="cyan")
    table.add_column("tab", style="green")
    table.add_column("details")
    for r in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.ts))
        tab = (r.tab_id or "—").split("-")[0]
        details = ", ".join(f"{k}={v}" for k, v in r.details.items())
        table.add_row(when, r.action, tab, details[:80])
    console.print(table)


# ───────── mission graph (v0.7 foundation) ─────────

graph_app = typer.Typer(help="Inspect and annotate the v0.7 mission graph.")
app.add_typer(graph_app, name="graph")


@graph_app.command("status")
def graph_status(
    all_projects: bool = typer.Option(False, "--all", help="Show every project instead of the cwd project."),
):
    """Show mission graph table counts and basic health checks."""
    tenant_mod.backfill_known_tenants()
    project = None if all_projects else tenant_mod.ensure_project_tenant(Path.cwd())
    health = graph_mod.graph_health(tenant_id=project.tenant_id if project else None)
    counts = health["counts"]

    scope = "global" if project is None else project.name
    table = Table(title=f"MORPHEUS mission graph — {scope}", header_style="bold green")
    table.add_column("table / check")
    table.add_column("count", justify="right", style="cyan")
    for key in (
        "live_sessions", "missions", "active_missions", "archived_missions",
        "events", "artifacts", "edges",
    ):
        table.add_row(key, str(counts[key]))
    table.add_row("live_without_memory", str(len(health["live_without_memory"])))
    table.add_row("active_without_live", str(len(health["active_without_live"])))
    console.print(table)

    if health["live_without_memory"]:
        console.print("[yellow]live sessions missing memory rows:[/yellow]")
        for m in health["live_without_memory"]:
            console.print(f"  - {m.tab_id.split('-')[0]} {m.goal or '(untitled)'}")


@graph_app.command("show")
def graph_show(
    ref: str = typer.Argument(..., help="Mission id, mission id prefix, tab id, or tab id prefix."),
):
    """Show one durable mission graph card."""
    resolved = graph_mod.resolve(ref)
    if resolved is None:
        console.print(f"[red]no mission matching '{ref}'[/red]")
        raise typer.Exit(1)

    mem = resolved.memory
    console.print(f"[bold green]{escape(mem.title or mem.mission_id)}[/bold green]")
    console.print(f"  mission:   [cyan]{mem.mission_id}[/cyan]")
    console.print(f"  short:     [cyan]{graph_mod.short_id(mem.mission_id)}[/cyan]")
    console.print(f"  phase:     {escape(mem.phase)}")
    source_line = f"  source:    {mem.source_kind} {mem.source_ref or ''}".rstrip()
    console.print(source_line, markup=False)
    console.print(f"  confidence:{mem.confidence:.2f}")
    if resolved.live:
        live_bits = ", ".join(f"{m.tab_id.split('-')[0]}:{m.state}" for m in resolved.live)
        console.print(f"  live:      {live_bits}")
    else:
        console.print("  live:      [dim]no live tab attachment[/dim]")

    fields = [
        ("why", mem.why),
        ("done", mem.done_definition),
        ("criteria", mem.acceptance_criteria),
        ("plan", mem.current_plan),
        ("next", mem.next_step),
        ("blocked", mem.blocked_on),
        ("decision", mem.last_decision),
        ("summary", mem.last_summary),
    ]
    for label, value in fields:
        if value:
            console.print(f"\n[bold]{label}[/bold]\n{escape(value)}")

    events = db.recent_events(mem.mission_id, limit=8)
    if events:
        console.print("\n[bold]recent events[/bold]")
        for e in events:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(e.ts))
            source = f" [{e.source_ref}]" if e.source_ref else ""
            console.print(f"  {when} {e.kind}/{e.actor}: {e.summary}{source}", markup=False)

    artifacts = db.artifacts_for_mission(mem.mission_id, limit=8)
    if artifacts:
        console.print("\n[bold]artifacts[/bold]")
        for a in artifacts:
            console.print(
                f"  {a.status} {a.kind}: {a.path_or_url} {a.summary}",
                markup=False,
            )

    edges = db.edges_for_id(mem.mission_id, limit=8)
    if edges:
        console.print("\n[bold]edges[/bold]")
        for e in edges:
            console.print(
                f"  {e.from_id} -[{e.relation}]-> {e.to_id} {e.reason}",
                markup=False,
            )


@graph_app.command("recall-eval")
def graph_recall_eval(
    refs: Optional[list[str]] = typer.Argument(
        None,
        help="Mission ids/prefixes or tab ids/prefixes. Default: all stale active missions.",
    ),
    all_projects: bool = typer.Option(False, "--all", help="Evaluate every project instead of the cwd project."),
    stale_hours: float = typer.Option(
        48.0,
        "--stale-hours",
        help="Minimum idle/closed age for the 48-hour recall gate.",
    ),
    target_seconds: float = typer.Option(
        10.0,
        "--target-seconds",
        help="Human recall target this readiness score is proxying.",
    ),
    include_archived: bool = typer.Option(
        False,
        "--include-archived",
        help="When no refs are given, include archived mission memory rows.",
    ),
    include_fresh: bool = typer.Option(
        False,
        "--include-fresh",
        help="When no refs are given, include missions younger than --stale-hours.",
    ),
    record_event: bool = typer.Option(
        False,
        "--record-event",
        help="Append a recall_eval graph event to each evaluated mission.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
):
    """Evaluate stale mission recall readiness from graph-backed brief fields."""
    if stale_hours <= 0:
        console.print("[red]--stale-hours must be positive[/red]")
        raise typer.Exit(1)
    if target_seconds <= 0:
        console.print("[red]--target-seconds must be positive[/red]")
        raise typer.Exit(1)

    tenant_mod.backfill_known_tenants()
    project = None if all_projects else tenant_mod.ensure_project_tenant(Path.cwd())

    try:
        results = _recall_eval_results(
            refs or [],
            stale_seconds=stale_hours * 3600,
            target_seconds=target_seconds,
            include_archived=include_archived,
            include_fresh=include_fresh,
            tenant_id=project.tenant_id if project else None,
        )
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    if json_out:
        console.print_json(json.dumps([result.to_dict() for result in results]))
    else:
        _print_recall_eval_results(results, stale_hours=stale_hours)

    if record_event:
        for result in results:
            db.add_event(
                result.mission_id,
                kind="recall_eval",
                actor="morpheus",
                summary=result.event_summary(),
                source_ref="morpheus graph recall-eval",
                metadata=result.to_dict(),
            )
        if not json_out:
            console.print(f"[green]recorded recall_eval event(s):[/green] {len(results)}")


def _recall_eval_results(
    refs: list[str],
    *,
    stale_seconds: float,
    target_seconds: float,
    include_archived: bool,
    include_fresh: bool,
    tenant_id: Optional[str] = None,
) -> list[recall_eval.RecallEvaluation]:
    live_by_mission: dict[str, list[db.Mission]] = {}
    for mission in db.all_missions(tenant_id=tenant_id):
        if mission.mission_id:
            live_by_mission.setdefault(mission.mission_id, []).append(mission)

    memories: list[db.MissionMemory] = []
    if refs:
        for ref in refs:
            resolved = _resolve_recall_ref(ref, tenant_id=tenant_id)
            if resolved is None:
                raise ValueError(f"no mission matching '{ref}'")
            memories.append(resolved.memory)
            if resolved.live:
                live_by_mission[resolved.mission_id] = resolved.live
    else:
        memories = db.all_memory(include_archived=include_archived, tenant_id=tenant_id)

    results: list[recall_eval.RecallEvaluation] = []
    seen: set[str] = set()
    for memory in memories:
        if memory.mission_id in seen:
            continue
        seen.add(memory.mission_id)
        result = recall_eval.evaluate_mission(
            memory,
            live=live_by_mission.get(memory.mission_id, []),
            events=db.recent_events(memory.mission_id, limit=20),
            artifacts=db.artifacts_for_mission(memory.mission_id, limit=20),
            stale_seconds=stale_seconds,
            target_seconds=target_seconds,
        )
        if refs or include_fresh or (
            result.age_seconds is not None and result.age_seconds >= stale_seconds
        ):
            results.append(result)
    return results


def _resolve_recall_ref(
    ref: str,
    tenant_id: Optional[str] = None,
) -> Optional[graph_mod.ResolvedMission]:
    """Resolve one ref for recall eval, rejecting ambiguous prefixes."""
    needle = ref.strip()
    if not needle:
        return None

    live = db.all_missions(tenant_id=tenant_id)
    memories = db.all_memory(include_archived=True, tenant_id=tenant_id)
    memory_by_id = {memory.mission_id: memory for memory in memories}

    exact_ids = {
        mission.mission_id for mission in live
        if mission.mission_id and (mission.tab_id == needle or mission.mission_id == needle)
    }
    exact_ids.update(
        memory.mission_id for memory in memories
        if memory.mission_id == needle
    )
    candidate_ids = exact_ids
    if not candidate_ids:
        candidate_ids = {
            mission.mission_id for mission in live
            if mission.mission_id and (
                mission.tab_id.startswith(needle)
                or mission.mission_id.startswith(needle)
            )
        }
        candidate_ids.update(
            memory.mission_id for memory in memories
            if memory.mission_id.startswith(needle)
        )

    if not candidate_ids:
        return None
    if len(candidate_ids) > 1:
        choices = ", ".join(graph_mod.short_id(mission_id) for mission_id in sorted(candidate_ids))
        raise ValueError(f"ambiguous mission ref '{ref}' matches: {choices}")

    mission_id = next(iter(candidate_ids))
    memory = memory_by_id.get(mission_id)
    if memory is None and tenant_id is None:
        memory = db.get_memory(mission_id)
    if memory is None:
        return None
    return graph_mod.ResolvedMission(
        mission_id=mission_id,
        memory=memory,
        live=[mission for mission in live if mission.mission_id == mission_id],
    )


def _print_recall_eval_results(
    results: list[recall_eval.RecallEvaluation],
    *,
    stale_hours: float,
) -> None:
    if not results:
        console.print(f"[dim]no missions at or beyond {stale_hours:g}h stale[/dim]")
        return

    table = Table(title="MORPHEUS 48-hour recall eval", header_style="bold green")
    table.add_column("RESULT", no_wrap=True)
    table.add_column("SCORE", justify="right", no_wrap=True)
    table.add_column("AGE", justify="right", no_wrap=True)
    table.add_column("MISSION", style="cyan", no_wrap=True)
    table.add_column("TITLE", overflow="fold")
    table.add_column("MISSING", overflow="fold")
    for result in results:
        status = "[green]PASS[/green]" if result.passed else "[red]FAIL[/red]"
        missing = ", ".join(result.missing_labels) if result.missing_labels else "none"
        table.add_row(
            status,
            f"{result.score}%",
            recall_eval.format_age(result.age_seconds),
            graph_mod.short_id(result.mission_id),
            escape(result.title),
            escape(missing),
        )
    console.print(table)


@graph_app.command("event")
def graph_event(
    ref: str = typer.Argument(..., help="Mission id/prefix or tab id/prefix."),
    summary: str = typer.Argument(..., help="Event summary to attach."),
    kind: str = typer.Option("decision", "--kind", "-k",
                              help="decision | blocker | check | summary | note"),
    actor: str = typer.Option("user", "--actor", "-a",
                               help="user | morpheus | codex | claude | shell"),
    source_ref: str = typer.Option("", "--source-ref", "-s",
                                    help="Transcript span, file path, command, or URL."),
):
    """Append a provenance-friendly event to a mission."""
    resolved = graph_mod.resolve(ref)
    if resolved is None:
        console.print(f"[red]no mission matching '{ref}'[/red]")
        raise typer.Exit(1)
    event_id = db.add_event(
        resolved.mission_id,
        kind=kind,
        actor=actor,
        summary=summary,
        source_ref=source_ref,
    )
    console.print(f"[green]event #{event_id}[/green] → {resolved.mission_id}")
    _refresh_prd_run_status_for_mission(resolved.mission_id)


@graph_app.command("artifact")
def graph_artifact(
    ref: str = typer.Argument(..., help="Mission id/prefix or tab id/prefix."),
    path_or_url: str = typer.Argument(..., help="Local path or external URL."),
    kind: str = typer.Option("proof", "--kind", "-k",
                              help="snapshot | diff | test | build | pr | issue | doc | log | proof"),
    status: str = typer.Option("unknown", "--status", "-s",
                                help="pending | pass | fail | unknown"),
    summary: str = typer.Option("", "--summary", help="Short artifact summary."),
):
    """Attach a proof/output artifact to a mission."""
    resolved = graph_mod.resolve(ref)
    if resolved is None:
        console.print(f"[red]no mission matching '{ref}'[/red]")
        raise typer.Exit(1)
    artifact_id = db.add_artifact(
        resolved.mission_id,
        kind=kind,
        path_or_url=path_or_url,
        status=status,
        summary=summary,
    )
    console.print(f"[green]artifact #{artifact_id}[/green] → {resolved.mission_id}")
    _refresh_prd_run_status_for_mission(resolved.mission_id)


def _refresh_prd_run_status_for_mission(mission_id: str) -> None:
    try:
        run = prd_runs.update_status_for_mission(mission_id)
    except Exception as e:
        console.print(f"[yellow]warning: PRD run status refresh failed: {e}[/yellow]")
        return
    if run is not None:
        console.print(f"  status: {run.status_path}")


# ───────── daemon (launchd) ─────────

@app.command("install-daemon")
def install_daemon(
    poll: float = typer.Option(5.0, "--poll", "-p",
                                help="Seconds between polls."),
):
    """Install the launchd LaunchAgent so morpheus runs always (RunAtLoad, KeepAlive)."""
    ok, msg = daemon_mod.install(poll=poll)
    if ok:
        console.print(f"[green]✓ {msg}[/green]")
    else:
        console.print(f"[red]✗ {msg}[/red]")
        raise typer.Exit(1)


@app.command("uninstall-daemon")
def uninstall_daemon():
    """Stop and remove the launchd LaunchAgent."""
    ok, msg = daemon_mod.uninstall()
    if ok:
        console.print(f"[green]✓ {msg}[/green]")
    else:
        console.print(f"[red]✗ {msg}[/red]")
        raise typer.Exit(1)


@app.command("install-loop-runner")
def install_loop_runner(
    interval: int = typer.Option(60, "--interval", "-i", help="Seconds between loop runner wakeups."),
    limit: int = typer.Option(5, "--limit", "-n", help="Maximum due loops per wakeup."),
    timeout: int = typer.Option(loops_mod.DEFAULT_TIMEOUT_SECONDS, "--timeout", help="Seconds before one loop run times out."),
):
    """Install the launchd LaunchAgent that executes due prompt loops."""
    ok, msg = daemon_mod.install_loop_runner(
        interval=interval,
        limit=limit,
        timeout=timeout,
    )
    if ok:
        console.print(f"[green]✓ {msg}[/green]")
    else:
        console.print(f"[red]✗ {msg}[/red]")
        raise typer.Exit(1)


@app.command("uninstall-loop-runner")
def uninstall_loop_runner():
    """Stop and remove the loop runner LaunchAgent."""
    ok, msg = daemon_mod.uninstall_loop_runner()
    if ok:
        console.print(f"[green]✓ {msg}[/green]")
    else:
        console.print(f"[red]✗ {msg}[/red]")
        raise typer.Exit(1)


mcp_app = typer.Typer(help="MCP server (Model Context Protocol) for Claude Code / Codex.")
app.add_typer(mcp_app, name="mcp")


@mcp_app.command("serve")
def mcp_serve():
    """Run morpheus's MCP stdio server. Wire into ~/.claude.json or .mcp.json.

    Example MCP client config (drop into ~/.claude.json or a project .mcp.json):

        {
          "mcpServers": {
            "morpheus": {
              "command": "morpheus",
              "args": ["mcp", "serve"]
            }
          }
        }
    """
    from morpheus import mcp_server
    mcp_server.serve()


# ───────── desktop app (chat-agent cockpit) ─────────

desktop_app = typer.Typer(help="Morpheus desktop app — a Claude-Code-style chat cockpit.")
app.add_typer(desktop_app, name="desktop")


@desktop_app.callback(invoke_without_command=True)
def _desktop_default(
    ctx: typer.Context,
    host: str = typer.Option("127.0.0.1", help="Bind address (loopback only)."),
    port: int = typer.Option(0, help="Port (0 = OS-assigned)."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't open a browser window."),
):
    """Launch the desktop cockpit: start the bridge server and open the UI.

    Same SQLite DB and config.toml as the CLI, so state stays in sync. On macOS
    this is the window you keep open; the served SPA is a chat agent tailored to
    Morpheus (mission graph, sessions, goals, loops, autonomous goals).
    """
    if ctx.invoked_subcommand is not None:
        return
    import webbrowser
    from morpheus.desktop import server as desktop_server

    def _on_ready(srv: "desktop_server.DesktopServer"):
        console.print(f"[green]Morpheus desktop[/green] serving at [bold]{srv.url.split('?')[0]}[/bold]")
        console.print(f"  token: [dim]{srv.cfg.token}[/dim]")
        console.print("  (Ctrl-C to stop)")
        if not no_browser:
            try:
                webbrowser.open(srv.url)
            except Exception:
                pass

    desktop_server.serve(host=host, port=port, on_ready=_on_ready, block=True)


@desktop_app.command("serve")
def desktop_serve(
    host: str = typer.Option("127.0.0.1", help="Bind address (loopback only)."),
    port: int = typer.Option(0, help="Port (0 = OS-assigned)."),
    handshake: bool = typer.Option(
        False, "--handshake",
        help="Print a single JSON line {host,port,token,url} on ready (for the Electron shell), then serve.",
    ),
    parent_watchdog: bool = typer.Option(
        False, "--parent-watchdog",
        help="Exit automatically when the launching parent process dies (prevents orphaned servers if the shell is force-quit).",
    ),
):
    """Run the desktop bridge server (REST + SSE) without opening a browser.

    The Electron shell launches this with --handshake, reads the JSON line from
    stdout to learn the port + auth token, then points its window at the URL.
    """
    from morpheus.desktop import server as desktop_server

    def _on_ready(srv: "desktop_server.DesktopServer"):
        if handshake:
            print(json.dumps(srv.handshake()), flush=True)
        else:
            console.print(f"[green]bridge[/green] at [bold]{srv.url.split('?')[0]}[/bold]  token: [dim]{srv.cfg.token}[/dim]")

    desktop_server.serve(host=host, port=port, on_ready=_on_ready, block=True,
                         parent_watchdog=parent_watchdog)


@app.command("daemon-status")
def daemon_status():
    """Show launchd daemon status (loaded? PID? beacon age? log size?)."""
    s = daemon_mod.status()

    def yes(b: bool) -> str:
        return "[green]✓[/green]" if b else "[red]✗[/red]"

    console.print(f"[bold]morpheus daemon[/bold]")
    console.print(f"  plist installed:   {yes(s.plist_installed)}  {daemon_mod.LAUNCH_AGENT_PATH}")
    console.print(f"  launchctl loaded:  {yes(s.launchctl_loaded)}")
    console.print(f"  PID:               {s.pid if s.pid else '[dim]—[/dim]'}")
    if s.program_path:
        console.print(f"  program:           {s.program_path}")
    if s.beacon_exists:
        age = s.beacon_age_secs or 0.0
        color = "green" if age < 30 else ("yellow" if age < 120 else "red")
        console.print(f"  last beacon:       [{color}]{naming.format_age(age)} ago[/{color}]  "
                       f"({daemon_mod.BEACON_PATH})")
    else:
        console.print(f"  last beacon:       [yellow]never (daemon may have just started or be hung)[/yellow]")
    console.print(f"  log size:          {s.log_size_bytes:,} bytes  ({daemon_mod.DAEMON_LOG})")
    if not s.launchctl_loaded:
        console.print("\n  [yellow]Install:[/yellow] morpheus install-daemon")
    elif s.beacon_age_secs is None or s.beacon_age_secs > 120:
        console.print("\n  [yellow]Daemon looks unhealthy — check the log:[/yellow]")
        console.print(f"    tail -f {daemon_mod.DAEMON_LOG}")


@app.command("loop-runner-status")
def loop_runner_status():
    """Show launchd loop runner status (loaded? last wake? log size?)."""
    s = daemon_mod.loop_runner_status()

    def yes(b: bool) -> str:
        return "[green]✓[/green]" if b else "[red]✗[/red]"

    console.print(f"[bold]morpheus loop runner[/bold]")
    console.print(f"  plist installed:   {yes(s.plist_installed)}  {daemon_mod.LOOP_RUNNER_PATH}")
    console.print(f"  launchctl loaded:  {yes(s.launchctl_loaded)}")
    console.print(f"  PID:               {s.pid if s.pid else '[dim]—[/dim]'}")
    if s.program_path:
        console.print(f"  program:           {s.program_path}")
    if s.interval_secs:
        console.print(f"  interval:          {s.interval_secs}s")
    if s.limit is not None:
        console.print(f"  limit:             {s.limit}")
    if s.timeout_secs is not None:
        console.print(f"  timeout:           {s.timeout_secs}s")
    if s.beacon_exists:
        age = s.beacon_age_secs or 0.0
        healthy_window = max((s.interval_secs or 60) * 3, 180)
        color = "green" if age < healthy_window else "yellow"
        console.print(f"  last wake:         [{color}]{naming.format_age(age)} ago[/{color}]  "
                       f"({daemon_mod.LOOP_RUNNER_BEACON_PATH})")
    else:
        console.print("  last wake:         [yellow]never[/yellow]")
    console.print(f"  log size:          {s.log_size_bytes:,} bytes  ({daemon_mod.LOOP_RUNNER_LOG})")
    if not s.launchctl_loaded:
        console.print("\n  [yellow]Install:[/yellow] morpheus install-loop-runner")
    elif not s.beacon_exists:
        console.print("\n  [yellow]Runner is loaded but has not reported a wake yet; wait one interval or check the log.[/yellow]")


# ───────── doctor ─────────

def _iterm2_running() -> bool:
    """Best-effort check whether the iTerm2 app is running on this machine.

    Tries several patterns because the process name varies across macOS
    versions and how the app was launched.
    """
    import subprocess
    patterns = [
        ["pgrep", "-x", "iTerm2"],
        ["pgrep", "-x", "iTerm"],
        ["pgrep", "-if", "iTerm2"],
        ["pgrep", "-if", "Contents/MacOS/iTerm"],
    ]
    for cmd in patterns:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
            if out.returncode == 0 and out.stdout.strip():
                return True
        except Exception:
            continue
    return False


def _running_in_iterm() -> bool:
    """Are we *executing* inside an iTerm2 session? (vs Terminal.app, etc.)"""
    return bool(os.environ.get("ITERM_SESSION_ID")) or os.environ.get("TERM_PROGRAM") == "iTerm.app"


@app.command()
def doctor():
    """Diagnose iTerm2 + Python API connectivity."""
    console.print("[bold]morpheus doctor[/bold]")

    try:
        import iterm2
        console.print("  ✓ iterm2 package importable")
    except Exception as e:
        console.print(f"  ✗ iterm2 import failed: {e}")
        raise typer.Exit(1)

    # Where is the user running this?
    if _running_in_iterm():
        console.print("  ✓ running inside an iTerm2 session")
    else:
        term_prog = os.environ.get("TERM_PROGRAM") or "unknown"
        console.print(
            f"  [yellow]⚠ not running inside iTerm2 (TERM_PROGRAM={term_prog}) — "
            f"morpheus can still connect to iTerm2 if it's running, but the dashboard "
            f"will render in this terminal, not in an iTerm tab.[/yellow]"
        )
    # NB: deliberately do NOT print an "iTerm2 not running" warning here —
    # the connection attempt below is the only reliable signal. pgrep checks
    # were producing false negatives that confused users (the connection
    # would succeed but the pre-check would say "not running").

    # iterm2's run_until_complete prints its own help on connection failure and
    # may sys.exit rather than raise — so we use a success flag and catch
    # BaseException (covering SystemExit) to detect non-success reliably.
    success = {"ok": False, "windows": 0, "tabs": 0}

    async def _do(connection):
        try:
            app = await iterm2.async_get_app(connection)
        except Exception as e:
            console.print(f"  ✗ async_get_app failed: {e}")
            return
        if app is None:
            console.print("  ✗ connected, but no App returned (is iTerm running?)")
            return
        success["windows"] = len(app.windows)
        success["tabs"] = sum(len(w.tabs) for w in app.windows)
        success["ok"] = True

    try:
        iterm_client.run(_do)
    except SystemExit:
        pass
    except BaseException as e:
        console.print(f"  ✗ connect failed: {type(e).__name__}: {e}")

    if success["ok"]:
        console.print(f"  ✓ connected — windows={success['windows']}  tabs={success['tabs']}")
        console.print("\n[green bold]✓ all checks passed.[/green bold] Run [bold]morpheus[/bold] to launch the dashboard.")
        return

    # Now that we know the connection actually failed, give the diagnostic
    # for "is iTerm2 even running?" — this is the right time to surface it.
    if not _iterm2_running():
        console.print(
            "  [red]✗ iTerm2 app does not appear to be running — launch it first "
            "(CMD+SPACE → 'iTerm' → enter, or `open -a iTerm`).[/red]"
        )

    console.print("\n[yellow bold]→ iTerm2 Python API setup needed (this lives INSIDE iTerm2, not in Terminal.app):[/yellow bold]")
    console.print("  1. [bold]Switch to iTerm2[/bold] (CMD+TAB to it, or launch from /Applications/iTerm.app)")
    console.print("  2. Top-left menubar: click [bold]\"iTerm2\"[/bold] → [bold]\"Settings…\"[/bold]  (shortcut: CMD+,)")
    console.print("  3. In the Settings window: click the [bold]\"General\"[/bold] icon in the top toolbar")
    console.print("  4. In General's sub-tabs, click [bold]\"Magic\"[/bold]")
    console.print("  5. Check [bold]☑ Enable Python API[/bold]")
    console.print("  6. Set [bold]Require Authentication[/bold] dropdown to [bold]\"Allow all apps to connect\"[/bold]")
    console.print("  7. [yellow]Quit iTerm2 entirely (CMD+Q) and re-open[/yellow]  ← most people miss this")
    console.print("  8. Re-run: [bold]morpheus doctor[/bold]")
    console.print("\n  Note: morpheus can be launched from any terminal (Terminal.app, iTerm, etc.) —")
    console.print("  it just needs iTerm2 to be running with the Python API enabled. For the dashboard")
    console.print("  to appear inside an iTerm tab, run `morpheus` from a tab in iTerm2 itself.")
    console.print("\n  Alternative: set [bold]$ITERM2_COOKIE[/bold] env var to a valid cookie.")
    raise typer.Exit(1)


if __name__ == "__main__":
    app()
