"""Morpheus CLI — typer-based entry points."""

from __future__ import annotations

import json
import os
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
from morpheus import core, daemon as daemon_mod, db, iterm_client, ledger as ledger_mod, loops as loops_mod, mission_graph as graph_mod, naming, notifier as notifier_mod, prd_runs, recall_eval, tenant as tenant_mod, trigger as trigger_mod, __version__

app = typer.Typer(
    name="morpheus",
    help="Mission control for your iTerm tabs.",
    no_args_is_help=False,
    add_completion=False,
)
console = Console()
projects_app = typer.Typer(help="List, prune, and delete project tenants.")
app.add_typer(projects_app, name="projects")


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
):
    """List configured prompt loops."""
    rows = db.all_loops(include_paused=all_statuses)
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
        run_count = len(db.loop_runs(loop.id, limit=1000))
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
):
    """Run due loops once. Put this command behind cron/launchd."""
    due = db.due_loops(limit=limit)
    if dry_run:
        if not due:
            console.print("[dim]no loops due[/dim]")
            return
        for loop in due:
            console.print(f"#{loop.id} {loop.name} due now")
        return
    runs = loops_mod.run_due(limit=limit, timeout=timeout)
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
    run = loops_mod.run_loop(loop, timeout=timeout)
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


@app.command()
def context(
    fmt: str = typer.Option("md", "--format", "-f", help="md | json | short"),
    refresh: bool = typer.Option(False, "--refresh", "-r",
                                  help="Force re-poll iTerm before printing (slower)."),
    all_projects: bool = typer.Option(False, "--all", help="Show every project instead of the cwd project."),
):
    """Print the shared cross-session snapshot.

    Default reads ~/.morpheus/context.md which the watch loop maintains every
    few seconds. --refresh forces a live re-poll (use sparingly).
    """
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
