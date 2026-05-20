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

from morpheus import ask as ask_mod
from morpheus import brief as brief_mod
from morpheus import config as cfg_mod
from morpheus import context as ctx_mod
from morpheus import core, daemon as daemon_mod, db, iterm_client, ledger as ledger_mod, loops as loops_mod, mission_graph as graph_mod, naming, notifier as notifier_mod, prd_runs, trigger as trigger_mod, __version__

app = typer.Typer(
    name="morpheus",
    help="Mission control for your iTerm tabs.",
    no_args_is_help=False,
    add_completion=False,
)
console = Console()


# ───────── default entry: launch dashboard ─────────

@app.callback(invoke_without_command=True)
def _default(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        from morpheus import dashboard
        dashboard.run()


@app.command()
def dashboard():
    """Launch the Matrix-rain dashboard (same as running `morpheus` with no args)."""
    from morpheus import dashboard as dash_mod
    dash_mod.run()


# ───────── version ─────────

@app.command()
def version():
    """Print the morpheus version."""
    console.print(f"morpheus {__version__}")


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

    async def _do(connection):
        info = await iterm_client.spawn_tab(connection, command=command, goal=goal)
        if info is None:
            console.print("[red]failed to spawn tab — is iTerm focused?[/red]")
            raise typer.Exit(1)
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
    run = prd_runs.create_prd_run(prd, title=title)
    coordinator_goal = f"{run.title} coordinator"
    coordinator_cmd = prd_runs.coordinator_command(command, run)

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
            goal=coordinator_goal,
            state="working",
            cmd=coordinator_cmd,
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
    if target:
        resolved = graph_mod.resolve(target)
        if resolved is None:
            console.print(f"[red]no mission matching '{target}'[/red]")
            raise typer.Exit(1)
        target_mission_id = resolved.mission_id
        target_tab_id = resolved.live[0].tab_id if resolved.live else None

    loop = db.create_loop(
        name=name,
        prompt=prompt,
        interval_seconds=interval,
        command=command,
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
    table.add_column("LAST", overflow="fold")
    for loop in rows:
        target = loop.target_mission_id[:14] if loop.target_mission_id else "ticker"
        last = loop.last_summary or "—"
        table.add_row(
            str(loop.id),
            loop.status,
            loop.name,
            loops_mod.format_interval(loop.interval_seconds),
            loops_mod.format_due(loop.next_run_at),
            target,
            last,
        )
    console.print(table)


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


# ───────── list ─────────

@app.command("list")
def list_cmd(
    stale_hours: float = typer.Option(4.0, "--stale", help="Hours of idle before flagged stale."),
):
    """List every registered tab with state, goal, age, last event."""

    async def _do(connection):
        live_tabs = await iterm_client.enumerate_tabs(connection)
        live_ids = {t.tab_id for t in live_tabs}

        rows = db.all_missions()
        if not rows:
            console.print("[dim]no missions registered yet — start `morpheus watch` or spawn a tab.[/dim]")
            return

        table = Table(
            title=f"MORPHEUS — {len(rows)} mission(s)",
            header_style="bold green",
            show_lines=False,
            row_styles=["", "dim"],
        )
        table.add_column("ID", style="green", no_wrap=True)
        table.add_column("ST")
        table.add_column("GOAL")
        table.add_column("AGE", justify="right")
        table.add_column("LAST EVENT", overflow="fold")
        table.add_column("LIVE?", justify="center")

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
            table.add_row(tab_short, emoji, goal_disp + stale_marker, age, m.last_event, live)

        console.print(table)

    iterm_client.run(_do)


# ───────── prune ─────────

@app.command()
def prune(
    older_than_hours: float = typer.Option(4.0, "--older-than", "-o",
                                           help="Hours of idle to consider stale."),
    yes: bool = typer.Option(False, "--yes", "-y",
                             help="Close all candidates without prompting."),
):
    """Close stale iTerm tabs (idle/finished, idle > --older-than)."""

    async def _do(connection):
        live = await iterm_client.enumerate_tabs(connection)
        live_by_id = {t.tab_id: t for t in live}

        candidates = []
        for m in db.all_missions():
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


@app.command()
def context(
    fmt: str = typer.Option("md", "--format", "-f", help="md | json | short"),
    refresh: bool = typer.Option(False, "--refresh", "-r",
                                  help="Force re-poll iTerm before printing (slower)."),
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

    if fmt == "json":
        console.print_json(json.dumps(ctx_mod.build_json(self_tab, self_session)))
    elif fmt == "short":
        console.print(ctx_mod.build_short(self_tab))
    else:
        md = ctx_mod.build_markdown(self_tab, self_session)
        # Render with Rich's markdown for terminal display.
        console.print(Markdown(md))


@app.command()
def note(
    text: str = typer.Argument(..., help="The note text other sessions will see."),
    tab: Optional[str] = typer.Option(None, "--tab", "-t",
                                       help="Attach to this tab_id (default: current iTerm session)."),
    kind: str = typer.Option("note", "--kind", "-k",
                              help="note | claim | broadcast"),
):
    """Post a cross-session note that every other session can read in their context."""
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
def graph_status():
    """Show mission graph table counts and basic health checks."""
    health = graph_mod.graph_health()
    counts = health["counts"]

    table = Table(title="MORPHEUS mission graph", header_style="bold green")
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
