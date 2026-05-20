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
from rich.markdown import Markdown
from rich.table import Table

from morpheus import context as ctx_mod
from morpheus import core, db, iterm_client, naming, __version__

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
):
    """Run the watch loop in the foreground. Updates tab titles every --poll seconds."""
    console.print(f"[bold green]▶ MORPHEUS watching[/bold green] (poll={poll:.1f}s) — Ctrl-C to stop.")
    console.print(f"  log: {core.LOG_PATH}")
    console.print(f"  db:  {db.DB_PATH}")
    try:
        import asyncio
        asyncio.run(core.watch_loop(poll_interval=poll))
    except KeyboardInterrupt:
        console.print("\n[dim]stopped.[/dim]")


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
        console.print(f"  cmd: [dim]{command}[/dim]")

    iterm_client.run(_do)


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


# ───────── doctor ─────────

def _iterm2_running() -> bool:
    """Best-effort check whether the iTerm2 app is running on this machine."""
    import subprocess
    try:
        out = subprocess.run(
            ["pgrep", "-x", "iTerm2"],
            capture_output=True, text=True, timeout=2,
        )
        return out.returncode == 0
    except Exception:
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

    # Up-front context: where is the user running this, and is iTerm2 alive?
    if _running_in_iterm():
        console.print("  ✓ running inside an iTerm2 session")
    else:
        term_prog = os.environ.get("TERM_PROGRAM") or "unknown"
        console.print(
            f"  [yellow]⚠ not running inside iTerm2 (TERM_PROGRAM={term_prog}) — "
            f"morpheus can still connect to iTerm2 if it's running, but the dashboard "
            f"will render in this terminal, not in an iTerm tab.[/yellow]"
        )
    if _iterm2_running():
        console.print("  ✓ iTerm2 app appears to be running")
    else:
        console.print(
            "  [red]✗ iTerm2 app is NOT running — launch iTerm2 first "
            "(CMD+SPACE → 'iTerm' → enter, or open from /Applications/iTerm.app)[/red]"
        )

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
