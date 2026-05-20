"""Live dashboard — runs inside one dedicated iTerm tab.

Drives the same tick logic as `morpheus watch` (so every other tab's title
stays current) and renders a three-pane TUI:

  ┌── banner + summary ─────────────────────────────────┐
  │                                                     │
  │  ┌── matrix rain ─────┐  ┌── mission table ──────┐  │
  │  │                    │  │                        │  │
  │  │   katakana drops   │  │   tab | st | goal …    │  │
  │  │                    │  │                        │  │
  │  └────────────────────┘  └────────────────────────┘  │
  │                                                     │
  │  ┌── 🐇 alerts (recent state changes + notes) ────┐  │
  │  └─────────────────────────────────────────────────┘  │
  └─────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass

import iterm2
from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from morpheus import core, db, iterm_client, naming, rain as rain_mod
from morpheus import __version__

console = Console()

# ── style + content constants ─────────────────────────────────────────────

BANNER = r"""
 ███╗   ███╗ ██████╗ ██████╗ ██████╗ ██╗  ██╗███████╗██╗   ██╗███████╗
 ████╗ ████║██╔═══██╗██╔══██╗██╔══██╗██║  ██║██╔════╝██║   ██║██╔════╝
 ██╔████╔██║██║   ██║██████╔╝██████╔╝███████║█████╗  ██║   ██║███████╗
 ██║╚██╔╝██║██║   ██║██╔══██╗██╔═══╝ ██╔══██║██╔══╝  ██║   ██║╚════██║
 ██║ ╚═╝ ██║╚██████╔╝██║  ██║██║     ██║  ██║███████╗╚██████╔╝███████║
 ╚═╝     ╚═╝ ╚═════╝ ╚═╝  ╚═╝╚═╝     ╚═╝  ╚═╝╚══════╝ ╚═════╝ ╚══════╝
""".strip("\n")

# White rabbit — used for every "new / push-to-user" event. (Follow it.)
RABBIT = "🐇"
ALERTS_MAX = 12

STATE_ORDER = {"blocked": 0, "crashed": 1, "working": 2,
               "idle": 3, "finished": 4, "unknown": 5}
STATE_STYLE = {
    "blocked":  "bold red",
    "crashed":  "bold magenta",
    "working":  "bright_green",
    "idle":     "yellow",
    "finished": "dim",
    "unknown":  "white",
}


@dataclass
class Alert:
    ts: float
    kind: str   # state | note | spawn | close
    text: str

    def render(self) -> Text:
        t = Text(time.strftime("%H:%M:%S", time.localtime(self.ts)), style="dim")
        t.append(f"  {RABBIT}  ", style="white")
        style = {
            "state": "bold",
            "note": "bright_green",
            "spawn": "cyan",
            "close": "dim",
        }.get(self.kind, "")
        t.append(self.text, style=style)
        return t


class DashboardState:
    """Mutable state held across frames."""

    def __init__(self):
        self.alerts: deque[Alert] = deque(maxlen=ALERTS_MAX)
        self.last_seen_note_id: int = 0
        self.last_seen_tabs: set[str] = set()
        self.rain: rain_mod.Rain | None = None
        self.last_layout_size: tuple[int, int] = (0, 0)
        # Initial watermarks so we don't replay existing state as fresh alerts.
        try:
            recent = db.recent_notes(limit=1)
            self.last_seen_note_id = recent[0].id if recent else 0
        except Exception:
            pass
        try:
            self.last_seen_tabs = {m.tab_id for m in db.all_missions()}
        except Exception:
            pass

    def push(self, alert: Alert) -> None:
        self.alerts.appendleft(alert)


# ── renderers ─────────────────────────────────────────────────────────────

def _summary_line(missions: list[db.Mission]) -> Text:
    counts: dict[str, int] = {}
    for m in missions:
        counts[m.state] = counts.get(m.state, 0) + 1

    t = Text(f"◉ {len(missions)} mission(s)   ", style="bold")
    for emoji, key in [("🔴", "blocked"), ("💀", "crashed"), ("🟢", "working"),
                       ("🟡", "idle"), ("⚫", "finished")]:
        c = counts.get(key, 0)
        if c:
            t.append(f"{emoji} {c} {key}   ", style=STATE_STYLE.get(key, ""))
    return t


def _render_header() -> Group:
    return Group(
        Align.center(Text(BANNER, style="bold green")),
        Align.center(Text(
            f"mission control v{__version__}  •  {RABBIT} follow the white rabbit  •  Ctrl-C to exit",
            style="dim",
        )),
    )


def _render_mission_table(missions: list[db.Mission], live_ids: set[str],
                          stale_after_hours: float = 4.0) -> Table:
    table = Table(
        header_style="bold green",
        border_style="green",
        show_header=True,
        expand=True,
        padding=(0, 1),
    )
    table.add_column("ID",         style="green", no_wrap=True, width=8)
    table.add_column("ST",         width=3, justify="center")
    table.add_column("GOAL",       ratio=3)
    table.add_column("AGE",        justify="right", width=6)
    table.add_column("LAST EVENT", ratio=3, overflow="fold")

    sorted_m = sorted(missions, key=lambda m: (STATE_ORDER.get(m.state, 9), -m.updated_at))
    for m in sorted_m:
        emoji = naming.STATE_EMOJI.get(m.state, "⚪")
        age_secs = naming.now_minus(m.buffer_changed_at)
        age = naming.format_age(age_secs)
        style = STATE_STYLE.get(m.state, "white")
        goal_disp = m.goal or "(untitled)"
        if (m.state in ("idle", "finished")) and age_secs >= stale_after_hours * 3600:
            goal_disp = f"[yellow]({age})[/yellow] {goal_disp}"
        tab_short = (m.tab_id or "?").split("-")[0]
        table.add_row(
            tab_short, emoji, Text(goal_disp, style=style),
            age, m.last_event or "—",
        )
    return table


def _render_alerts(state: DashboardState) -> Group:
    if not state.alerts:
        empty = Text(
            f"{RABBIT}  no incoming. waiting for sessions to do interesting things…",
            style="dim",
        )
        return Group(empty)
    lines = [a.render() for a in list(state.alerts)]
    return Group(*lines)


def _build_layout(rain: rain_mod.Rain, missions: list[db.Mission],
                  live_ids: set[str], state: DashboardState) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=10),
        Layout(name="body", ratio=1),
        Layout(name="alerts", size=ALERTS_MAX + 2),
    )
    layout["body"].split_row(
        Layout(name="rain", ratio=2),
        Layout(name="missions", ratio=3),
    )
    layout["header"].update(_render_header())

    layout["rain"].update(Panel(
        rain.render(),
        title=f"[bold green]rain[/bold green]   [dim](state: speed · brightness · color)[/dim]",
        border_style="green",
        padding=(0, 0),
    ))
    layout["missions"].update(Panel(
        Group(_summary_line(missions), Text(""), _render_mission_table(missions, live_ids)),
        title="[bold green]missions[/bold green]",
        border_style="green",
    ))
    layout["alerts"].update(Panel(
        _render_alerts(state),
        title=f"[bold white]{RABBIT}  alerts  (state changes · new notes · spawns)[/bold white]",
        border_style="bright_black",
    ))
    return layout


# ── self-tab management ───────────────────────────────────────────────────

async def _claim_self_tab(connection) -> None:
    """Rename this tab so the watcher excludes it."""
    app = await iterm2.async_get_app(connection)
    if app is None:
        return
    window = app.current_terminal_window
    if window is None:
        return
    tab = window.current_tab
    if tab is None or tab.current_session is None:
        return
    try:
        await tab.current_session.async_set_name(naming.MORPHEUS_TAB_PREFIX)
    except Exception:
        pass


# ── alert sources ─────────────────────────────────────────────────────────

def _scan_new_missions(state: DashboardState, missions: list[db.Mission]) -> None:
    current = {m.tab_id for m in missions}
    new_tabs = current - state.last_seen_tabs
    closed_tabs = state.last_seen_tabs - current
    by_id = {m.tab_id: m for m in missions}
    for tab in new_tabs:
        m = by_id.get(tab)
        if m is None:
            continue
        state.push(Alert(
            ts=time.time(),
            kind="spawn",
            text=f"new session [{m.goal or '(untitled)'}] {tab.split('-')[0]}",
        ))
    for tab in closed_tabs:
        state.push(Alert(
            ts=time.time(),
            kind="close",
            text=f"session [{tab.split('-')[0]}] closed",
        ))
    state.last_seen_tabs = current


def _scan_new_notes(state: DashboardState) -> None:
    recent = db.recent_notes(limit=ALERTS_MAX)
    fresh = [n for n in recent if n.id > state.last_seen_note_id]
    if not fresh:
        return
    # Map tab_id -> goal for nicer alert text.
    goals = {m.tab_id: (m.goal or "(untitled)") for m in db.all_missions()}
    for n in sorted(fresh, key=lambda n: n.created_at):
        goal = goals.get(n.tab_id or "", "unknown")
        marker = {"note": "•", "claim": "⚑", "broadcast": "📡"}.get(n.kind, "•")
        state.push(Alert(
            ts=n.created_at,
            kind="note",
            text=f"{marker} note from [{goal}]: {n.text}",
        ))
    state.last_seen_note_id = max(n.id for n in fresh)


def _make_state_hook(state: DashboardState):
    async def on_state_change(mission: db.Mission, old: str, new: str) -> None:
        emoji_new = naming.STATE_EMOJI.get(new, "⚪")
        emoji_old = naming.STATE_EMOJI.get(old, "⚪")
        goal = mission.goal or (mission.tab_id or "?").split("-")[0]
        # Only blocked/crashed/finished transitions get alerts to reduce noise.
        important_new = new in ("blocked", "crashed", "finished")
        important_old = old in ("blocked", "crashed")
        if not (important_new or important_old):
            return
        state.push(Alert(
            ts=time.time(),
            kind="state",
            text=f"{emoji_old} → {emoji_new}  [{goal}] is now {new}",
        ))
    return on_state_change


# ── main loop ─────────────────────────────────────────────────────────────

async def _loop(connection):
    log = core.setup_logging()
    await _claim_self_tab(connection)
    log.info("dashboard started")

    state = DashboardState()
    on_state_change = _make_state_hook(state)
    state.push(Alert(ts=time.time(), kind="spawn",
                     text="morpheus dashboard online — follow the white rabbit."))

    last_tick = 0.0
    last_size = (0, 0)

    with Live(console=console, refresh_per_second=12, screen=True,
              transient=False) as live:
        while True:
            try:
                now = time.time()

                # Heavy tick: poll iTerm, detect state, write DB + titles + context.
                if now - last_tick >= 2.0:
                    await core._tick(connection, log, on_state_change=on_state_change)
                    fresh_missions = db.all_missions()
                    _scan_new_missions(state, fresh_missions)
                    _scan_new_notes(state)
                    last_tick = now

                missions = db.all_missions()
                live_tabs = await iterm_client.enumerate_tabs(connection)

                # Compute the inner area of the rain panel: console - misc framing.
                # Width of rain pane ≈ (2/5) * console.width minus borders.
                # Height ≈ console.height minus header(10) minus alerts(14) minus borders.
                w = max(20, int(console.size.width * 2 / 5) - 4)
                h = max(8, console.size.height - 10 - (ALERTS_MAX + 2) - 4)

                if state.rain is None:
                    state.rain = rain_mod.Rain(cols=w, rows=h)
                elif (w, h) != last_size:
                    state.rain.resize(cols=w, rows=h)
                    last_size = (w, h)

                state.rain.update_missions(missions)
                state.rain.tick()

                live.update(_build_layout(
                    state.rain, missions, {t.tab_id for t in live_tabs}, state,
                ))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("dashboard tick error: %s", e)
            await asyncio.sleep(0.08)  # ~12 fps


def run() -> None:
    console.print(
        f"[bold green]▶ MORPHEUS[/bold green] — launching dashboard "
        f"(this tab is now the command center; titles sync every 2s)\n"
        f"[dim]{RABBIT} follow the white rabbit — Ctrl-C to exit.[/dim]"
    )
    try:
        iterm_client.run_app(_loop)
    except KeyboardInterrupt:
        console.print("\n[dim]dashboard closed.[/dim]")
