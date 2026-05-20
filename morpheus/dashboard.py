"""Interactive mission control TUI built on Textual.

Live in this tab. Spawn, browse, focus, prune, snapshot — all from keybindings.
The morpheus tab is your command center; the iTerm tabs are your workers.

  ┌─ MORPHEUS banner ─────────────────────────────────────────────────────┐
  │                                                                       │
  │  ┌── stream rain ─────┐  ┌── missions (sorted newest-active first) ┐  │
  │  │  Matrix rain with  │  │  cursor-navigable, ticker-flash on      │  │
  │  │  live output shards│  │  state change (green/yellow/red row)    │  │
  │  └────────────────────┘  └────────────────────────────────────────┘   │
  │  ┌── 🐇 alerts ──────────────────────────────────────────────────┐    │
  │  └────────────────────────────────────────────────────────────────┘   │
  │  [j/k] nav  [enter] focus  [n] new  [d] kill  [p] prune  …            │
  └───────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import shlex
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import iterm2
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Input, Label, RichLog, Select, Static

from morpheus import context as ctx_mod
from morpheus import core, db, iterm_client, mission_brief, naming, rain as rain_mod
from morpheus import ledger as ledger_mod
from morpheus import loops as loops_mod
from morpheus import prd_runs
from morpheus import __version__

# ── content / palette ─────────────────────────────────────────────────────

RABBIT = "🐇"

BANNER = r"""
 ███╗   ███╗ ██████╗ ██████╗ ██████╗ ██╗  ██╗███████╗██╗   ██╗███████╗
 ████╗ ████║██╔═══██╗██╔══██╗██╔══██╗██║  ██║██╔════╝██║   ██║██╔════╝
 ██╔████╔██║██║   ██║██████╔╝██████╔╝███████║█████╗  ██║   ██║███████╗
 ██║╚██╔╝██║██║   ██║██╔══██╗██╔═══╝ ██╔══██║██╔══╝  ██║   ██║╚════██║
 ██║ ╚═╝ ██║╚██████╔╝██║  ██║██║     ██║  ██║███████╗╚██████╔╝███████║
 ╚═╝     ╚═╝ ╚═════╝ ╚═╝  ╚═╝╚═╝     ╚═╝  ╚═╝╚══════╝ ╚═════╝ ╚══════╝
""".strip("\n")

COL_MUTED   = "color(244)"
COL_DIMMER  = "bright_black"
COL_BODY    = "color(252)"
COL_ACCENT  = "bright_cyan"

STATE_TEXT_STYLE = {
    "blocked":  "bold bright_red",
    "crashed":  "bold bright_magenta",
    "working":  "bright_green",
    "idle":     "bright_yellow",
    "finished": "color(244)",
    "unknown":  "color(250)",
}

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
SEPARATOR_RE = re.compile(r"^[\s\-_=~*·•.┄─━═╍╎│|┆┊┉┈—–]+$")
LEADING_MARKER_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“])")
CONCLUSION_RE = re.compile(
    r"\b("
    r"answer|bottom line|tl;dr|summary|recommend|recommendation|"
    r"practical answer|my pick|i['’]?d focus|i would focus|i(?:'|’)d use|"
    r"focus on|looks like|next step|done|fixed|shipped|implemented"
    r")\b",
    re.IGNORECASE,
)
TRAILING_DISCLAIMER_RE = re.compile(
    r"\s+(?:This is )?not (?:financial|legal|medical) advice\.?$",
    re.IGNORECASE,
)

# Stock-ticker row background colors (entire row paints this color for ~3s
# after a state change, then settles back to default).
FLASH_BG = {
    "working":  "color(22)",   # dark green — "gaining"
    "idle":     "color(58)",   # dark yellow
    "blocked":  "color(94)",   # dark amber — needs you
    "crashed":  "color(52)",   # dark red — "losing"
    "finished": "color(53)",   # dark magenta — done
    "unknown":  "color(238)",
}
FLASH_DURATION = 3.0  # seconds
ORPHAN_PRD_PRUNE_SECONDS = 60.0
MISSION_CARD_OUTPUT_LINES = 18
MISSION_CARD_EXPANDED_OUTPUT_LINES = 10
SNAPSHOT_DIR = Path.home() / ".morpheus" / "snapshots"

# Sort order for the missions table when there are no flashes pulling
# things up — newest activity first.
def _sort_key(m: db.Mission):
    return -m.buffer_changed_at


@dataclass
class Alert:
    ts: float
    kind: str   # state | note | spawn | close | error
    text: str

    def render(self) -> Text:
        t = Text(time.strftime("%H:%M:%S", time.localtime(self.ts)), style=COL_DIMMER)
        t.append(f"  {RABBIT}  ", style="bright_white")
        style = {
            "state": "bold bright_white",
            "summary": "bold bright_green",
            "note":  "bright_green",
            "spawn": COL_ACCENT,
            "close": COL_MUTED,
            "error": "bold bright_red",
        }.get(self.kind, COL_BODY)
        t.append(self.text, style=style)
        return t


@dataclass
class LiveBuffer:
    tab_id: str
    goal: str
    state: str
    last_event: str
    buffer: str
    observed_at: float


@dataclass
class NewSessionRequest:
    goal: str
    command: str
    prd_path: str = ""


@dataclass
class LoopRequest:
    name: str
    prompt: str
    interval: str
    command: str
    target_mission_id: str = ""
    target_tab_id: Optional[str] = None


@dataclass
class LoopActionRequest:
    action: str
    loop_id: int
    target_mission_id: str = ""
    target_tab_id: Optional[str] = None


@dataclass
class EditMissionRequest:
    tab_id: str
    mission_id: str
    goal: str
    title: str
    why: str
    done_definition: str
    acceptance_criteria: str
    current_plan: str
    next_step: str
    phase: str
    blocked_on: str
    source_kind: str
    source_ref: str
    issue_ref: str
    linked_pr: Optional[int]
    linked_worktree: str
    claimed_paths: str
    topic: str


@dataclass
class BriefScreenContent:
    title: str
    body: str


@dataclass
class WorkerRequest:
    parent_id: str
    goal: str
    command: str
    scope: str = ""
    verification: str = ""


@dataclass
class MissionRowRef:
    tab_id: str = ""
    mission_id: str = ""
    parent_id: str = ""
    role: str = ""
    virtual: bool = False


@dataclass
class StreamShard:
    tab_id: str
    text: str
    x: int
    y: int
    speed_ticks: int
    tick_counter: int = 0

    def tick(self, height: int) -> bool:
        self.tick_counter += 1
        if self.tick_counter < self.speed_ticks:
            return True
        self.tick_counter = 0
        self.y += 1
        return self.y < height


# ── rain widget ───────────────────────────────────────────────────────────

class LiveStreamWidget(Static):
    """Matrix rain with live terminal output embedded as falling shards."""

    def __init__(self, **kw):
        super().__init__("", **kw)
        self.rain: Optional[rain_mod.Rain] = None
        self.buffers: dict[str, LiveBuffer] = {}
        self.selected_tab_id: Optional[str] = None
        self.shards: dict[str, StreamShard] = {}
        self._has_active_rain = False
        self._idle_placeholder_rendered = False

    def on_show(self) -> None:
        self._ensure_rain()

    def on_resize(self, event) -> None:
        if self.rain is None:
            self._ensure_rain()
        else:
            cols, rows = self._inner_size()
            self.rain.resize(cols=cols, rows=rows)

    def _inner_size(self) -> tuple[int, int]:
        # Subtract a few for the panel border + padding.
        w = max(8, self.size.width - 2)
        h = max(4, self.size.height - 2)
        return w, h

    def _ensure_rain(self) -> None:
        if self.rain is None:
            cols, rows = self._inner_size()
            self.rain = rain_mod.Rain(cols=cols, rows=rows)

    def update_buffers(
        self,
        buffers: dict[str, LiveBuffer],
        selected_tab_id: Optional[str],
        *,
        render: bool = True,
    ) -> None:
        self.buffers = buffers
        self.selected_tab_id = selected_tab_id
        self._idle_placeholder_rendered = False
        self._sync_shards()
        if render:
            self._render_live()

    def tick_rain(self, missions: list[db.Mission]) -> None:
        if self.rain is None:
            self._ensure_rain()
        if self.rain is None:
            return
        self._has_active_rain = bool(missions)
        if not missions and not self.buffers:
            if not self._idle_placeholder_rendered:
                self.update(Text("awaiting live streams", style=COL_DIMMER))
                self._idle_placeholder_rendered = True
            return
        self._idle_placeholder_rendered = False
        self.rain.update_missions(missions)
        self.rain.tick()
        self._sync_shards()
        self._tick_shards()
        self._render_live()

    def _render_live(self) -> None:
        ordered = self._ordered_buffers()
        if not ordered and not self._has_active_rain:
            self.update(Text("awaiting live streams", style=COL_DIMMER))
            return

        width = max(24, self.size.width - 4)
        height = max(6, self.size.height - 2)
        grid = self._rain_grid(width, height)

        if not ordered:
            self._overlay_text(grid, 0, 0, "awaiting live streams", COL_DIMMER)
            self.update(self._grid_to_text(grid))
            return

        for shard in self._ordered_shards():
            if 0 <= shard.y < height:
                live = self.buffers.get(shard.tab_id)
                style = self._shard_style(live)
                self._overlay_text(grid, shard.x, shard.y, shard.text, style)
                if shard.y + 1 < height:
                    self._overlay_text(grid, shard.x, shard.y + 1, self._ghost_text(shard.text), "color(29)")

        self.update(self._grid_to_text(grid))

    def _rain_grid(self, width: int, height: int) -> list[list[tuple[str, str]]]:
        if self.rain is None:
            self.rain = rain_mod.Rain(cols=width, rows=height)
        elif self.rain.cols != width or self.rain.rows != height:
            self.rain.resize(cols=width, rows=height)

        col_at_x: list[object | None] = [None] * width
        for col in self.rain.columns:
            if 0 <= col.x < width:
                col_at_x[col.x] = col

        grid: list[list[tuple[str, str]]] = []
        for y in range(height):
            row: list[tuple[str, str]] = []
            for x in range(width):
                col = col_at_x[x]
                if col is None:
                    row.append((" ", ""))
                else:
                    row.append(col.get_cell(y))
            grid.append(row)
        return grid

    def _grid_to_text(self, grid: list[list[tuple[str, str]]]) -> Text:
        out = Text()
        for y, row in enumerate(grid):
            run = ""
            run_style = row[0][1] if row else ""
            for ch, style in row:
                if style != run_style:
                    if run:
                        out.append(run, style=run_style)
                    run = ch
                    run_style = style
                else:
                    run += ch
            if run:
                out.append(run, style=run_style)
            if y < len(grid) - 1:
                out.append("\n")
        return out

    def _overlay_text(
        self,
        grid: list[list[tuple[str, str]]],
        x: int,
        y: int,
        value: str,
        style: str,
    ) -> None:
        if not grid or y < 0 or y >= len(grid):
            return
        width = len(grid[y])
        if width <= 0:
            return
        x = max(0, min(x, width - 1))
        value = _truncate(value, max(1, width - x), strip=False)
        for offset, ch in enumerate(value):
            pos = x + offset
            if pos >= width:
                break
            grid[y][pos] = (ch, style)

    def _ordered_buffers(self) -> list[LiveBuffer]:
        items = list(self.buffers.values())
        if not items:
            return []
        state_order = {"blocked": 0, "crashed": 1, "working": 2, "idle": 3, "finished": 4, "unknown": 5}
        return sorted(
            items,
            key=lambda item: (
                0 if item.tab_id == self.selected_tab_id else 1,
                state_order.get(item.state, 9),
                -item.observed_at,
            ),
        )

    def _ordered_shards(self) -> list[StreamShard]:
        ordered_tabs = [live.tab_id for live in self._ordered_buffers()]
        index = {tab_id: i for i, tab_id in enumerate(ordered_tabs)}
        return sorted(self.shards.values(), key=lambda shard: index.get(shard.tab_id, 99), reverse=True)

    def _sync_shards(self) -> None:
        width = max(24, self.size.width - 4)
        height = max(6, self.size.height - 2)
        ordered = self._ordered_buffers()[:8]
        active = {live.tab_id for live in ordered}
        for tab_id in list(self.shards.keys()):
            if tab_id not in active:
                self.shards.pop(tab_id, None)

        for index, live in enumerate(ordered):
            text = _stream_shard_text(live, width=max(12, width - 2))
            if not text:
                continue
            existing = self.shards.get(live.tab_id)
            if existing is not None and existing.text == text:
                existing.speed_ticks = self._shard_speed(live)
                continue
            self.shards[live.tab_id] = StreamShard(
                tab_id=live.tab_id,
                text=text,
                x=self._shard_x(live.tab_id, index, width, len(text)),
                y=random.randint(0, max(0, height // 3)),
                speed_ticks=self._shard_speed(live),
            )

    def _tick_shards(self) -> None:
        height = max(6, self.size.height - 2)
        for tab_id, shard in list(self.shards.items()):
            if shard.tick(height):
                continue
            live = self.buffers.get(tab_id)
            if live is None:
                self.shards.pop(tab_id, None)
                continue
            text = _stream_shard_text(live, width=max(12, self.size.width - 6))
            shard.text = text
            shard.y = -random.randint(0, max(1, height // 2))
            shard.x = self._shard_x(tab_id, 0, max(24, self.size.width - 4), len(text))

    def _shard_x(self, tab_id: str, index: int, width: int, text_len: int) -> int:
        if width <= text_len + 1:
            return 0
        band = max(1, width // max(1, min(8, len(self.buffers) or 1)))
        base = (index * band + (abs(hash(tab_id)) % max(1, band))) % width
        return min(base, max(0, width - text_len - 1))

    def _shard_speed(self, live: LiveBuffer) -> int:
        if live.tab_id == self.selected_tab_id:
            return 1
        return {"working": 1, "blocked": 2, "crashed": 1, "idle": 3, "finished": 5}.get(live.state, 3)

    def _shard_style(self, live: Optional[LiveBuffer]) -> str:
        if live is None:
            return "bright_green"
        if live.tab_id == self.selected_tab_id:
            return "bold bright_white"
        return {
            "blocked": "bold bright_yellow",
            "crashed": "bold bright_red",
            "working": "bright_cyan",
            "idle": "bright_green",
            "finished": "green",
        }.get(live.state, "bright_green")

    def _ghost_text(self, value: str) -> str:
        return "".join(ch if ch == " " or random.random() < 0.16 else random.choice(rain_mod.CHARS) for ch in value)


RainWidget = LiveStreamWidget


def _tail_lines(buffer: str, limit: int, width: int) -> list[str]:
    lines = [_clean_terminal_line(line, strip=False).rstrip() for line in buffer.splitlines()]
    lines = [line for line in lines if line.strip()]
    if not lines:
        return ["(no visible output yet)"]
    return [_truncate(line, width, strip=False) for line in lines[-limit:]]


def _truncate(value: str, width: int, strip: bool = True) -> str:
    cleaned = _clean_terminal_line(value, strip=strip)
    if len(cleaned) <= width:
        return cleaned
    return cleaned[: max(0, width - 1)] + "…"


def _clean_terminal_line(value: str, strip: bool = True) -> str:
    cleaned = ANSI_RE.sub("", value)
    cleaned = CONTROL_RE.sub("", cleaned)
    cleaned = cleaned.replace("\t", "    ")
    return cleaned.strip() if strip else cleaned


def _session_headline(
    buffer: str,
    fallback: str = "",
    width: int = 120,
) -> str:
    lines = _latest_response_lines(buffer)
    headline = _response_headline(lines)
    if headline:
        return _truncate(headline, width)
    return _truncate(fallback, width) if fallback else ""


def _latest_response_lines(buffer: str) -> list[str]:
    lines: list[str] = []
    for raw in reversed(buffer.splitlines()):
        line = _clean_terminal_line(raw)
        if not line:
            continue
        if _is_response_boundary(line):
            if lines:
                break
            continue
        if _is_headline_noise(line):
            continue
        lines.append(line)
    return list(reversed(lines))


def _response_headline(lines: list[str]) -> str:
    candidates: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        candidate = _headline_candidate(line)
        if not candidate:
            continue
        score = index
        if CONCLUSION_RE.search(candidate):
            score += 100
        if candidate.endswith((".", "!", "?")):
            score += 5
        candidates.append((score, candidate))

    if not candidates:
        return ""
    _score, candidate = max(candidates, key=lambda item: item[0])
    return _first_sentence(candidate)


def _headline_candidate(line: str) -> str:
    line = LEADING_MARKER_RE.sub("", line).strip()
    if _is_summary_candidate_noise(line):
        return ""
    line = TRAILING_DISCLAIMER_RE.sub("", line)
    return " ".join(line.split()).strip()


def _first_sentence(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    parts = [part.strip() for part in SENTENCE_SPLIT_RE.split(value) if part.strip()]
    for part in parts:
        if _is_summary_candidate_noise(part):
            continue
        return TRAILING_DISCLAIMER_RE.sub("", part).strip()
    return value


def _stream_shard_text(live: LiveBuffer, width: int) -> str:
    tab_short = (live.tab_id or "?").split("-")[0]
    goal = live.goal or tab_short
    label = _truncate(goal, 22)
    snippet = _session_headline(live.buffer, fallback=live.last_event, width=max(8, width - len(label) - 8))
    if not snippet:
        return ""
    emoji = naming.STATE_EMOJI.get(live.state, "⚪")
    return _truncate(f"{emoji} {label} :: {snippet}", width)


def _summary_alert_key(mission: db.Mission, headline: str, verb: str) -> str:
    source = mission.buffer_hash or headline
    return f"{mission.tab_id}:{source}:{verb}:{headline}"


def _is_headline_noise(line: str) -> bool:
    if len(line) < 3:
        return True
    if _is_separator_line(line):
        return True
    lowered = line.lower()
    if lowered.startswith("use /skills to list"):
        return True
    if lowered.startswith("› use /skills to list"):
        return True
    if lowered.startswith("> use /skills to list"):
        return True
    if lowered.startswith("gpt-") and ("·" in lowered or "~" in lowered):
        return True
    if lowered in {"searching the web", "thinking", "working"}:
        return True
    if lowered.startswith("searched "):
        return True
    return False


def _is_response_boundary(line: str) -> bool:
    if _is_separator_line(line):
        return True
    lowered = line.lower()
    if lowered.startswith("› ") and not lowered.startswith("› use /skills"):
        return True
    if lowered.startswith("> ") and not lowered.startswith("> use /skills"):
        return True
    if lowered.startswith("❯ "):
        return True
    return False


def _is_separator_line(line: str) -> bool:
    return bool(SEPARATOR_RE.fullmatch(line))


def _is_summary_candidate_noise(line: str) -> bool:
    lowered = line.lower()
    if _is_headline_noise(line):
        return True
    if lowered.startswith(("sources:", "source:")):
        return True
    if lowered.startswith(("caveat:", "disclaimer:")):
        return True
    if lowered in {"not financial advice.", "not legal advice.", "not medical advice."}:
        return True
    if "http://" in lowered or "https://" in lowered:
        return True
    return False


# ── missions table ────────────────────────────────────────────────────────

class MissionsTable(DataTable):
    """Sortable, navigable missions list with row-flash on state change."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.cursor_type = "row"
        self.zebra_stripes = False
        self.row_tab_ids: list[str] = []
        self.row_refs: list[MissionRowRef] = []

    def on_mount(self) -> None:
        self.add_columns("ID", "ST", "GOAL", "AGE", "LAST EVENT")

    def refresh_rows(
        self,
        missions: list[db.Mission],
        flashing: dict[str, tuple[float, str]],
        prd_parents: Optional[list[db.MissionMemory]] = None,
        prd_edges: Optional[list[db.MissionEdge]] = None,
    ) -> None:
        # Preserve cursor position by live tab or virtual mission across refreshes.
        prior_ref = self.row_refs[self.cursor_row] if (
            self.row_refs and 0 <= self.cursor_row < len(self.row_refs)
        ) else None

        self.clear()
        self.row_tab_ids = []
        self.row_refs = []

        sorted_m = sorted(missions, key=_sort_key)
        missions_by_id = {m.mission_id: m for m in missions if m.mission_id}
        child_ids: set[str] = set()
        rows: list[tuple[MissionRowRef, Optional[db.Mission], Optional[db.MissionMemory], str]] = []

        parents = sorted(
            prd_parents or [],
            key=lambda mem: mem.updated_at,
            reverse=True,
        )
        edges = prd_edges or []
        children_by_parent: dict[str, list[db.MissionEdge]] = {}
        for edge in edges:
            if edge.relation in {"coordinator", "worker"}:
                children_by_parent.setdefault(edge.from_id, []).append(edge)

        for parent in parents:
            rows.append((
                MissionRowRef(mission_id=parent.mission_id, role="prd", virtual=True),
                None,
                parent,
                "",
            ))
            children = sorted(
                children_by_parent.get(parent.mission_id, []),
                key=lambda edge: (0 if edge.relation == "coordinator" else 1, edge.created_at),
            )
            for edge in children:
                child = missions_by_id.get(edge.to_id)
                if child is None:
                    continue
                child_ids.add(child.mission_id)
                rows.append((
                    MissionRowRef(
                        tab_id=child.tab_id,
                        mission_id=child.mission_id,
                        parent_id=parent.mission_id,
                        role=edge.relation,
                    ),
                    child,
                    None,
                    "  └ " if edge == children[-1] else "  ├ ",
                ))

        for mission in sorted_m:
            if mission.mission_id and mission.mission_id in child_ids:
                continue
            rows.append((
                MissionRowRef(tab_id=mission.tab_id, mission_id=mission.mission_id),
                mission,
                None,
                "",
            ))

        now = time.time()

        for row_ref, mission, parent, prefix in rows:
            self.row_refs.append(row_ref)
            self.row_tab_ids.append(row_ref.tab_id)
            if parent is not None:
                emoji = "▣"
                age = naming.format_age(naming.now_minus(parent.updated_at))
                tab_short = "PRD"
                goal_disp = parent.title or parent.mission_id
                last_evt = parent.next_step or "PRD run"
                cell_style = "bold bright_green"
            else:
                assert mission is not None
                emoji = naming.STATE_EMOJI.get(mission.state, "⚪")
                age = naming.format_age(naming.now_minus(mission.buffer_changed_at))
                tab_short = (mission.tab_id or "?").split("-")[0]
                role = f"{row_ref.role}: " if row_ref.role else ""
                goal_disp = f"{prefix}{role}{mission.goal or '(untitled)'}"
                last_evt = mission.last_event or "—"

                flash = flashing.get(mission.tab_id)
                if flash and flash[0] > now:
                    bg = FLASH_BG.get(flash[1], "color(238)")
                    cell_style = f"bold bright_white on {bg}"
                else:
                    cell_style = STATE_TEXT_STYLE.get(mission.state, COL_BODY)

            flash = flashing.get(row_ref.tab_id)
            if flash and flash[0] > now:
                bg = FLASH_BG.get(flash[1], "color(238)")
                cell_style = f"bold bright_white on {bg}"

            self.add_row(
                Text(tab_short, style=cell_style),
                Text(emoji),
                Text(goal_disp, style=cell_style),
                Text(age, style=cell_style),
                Text(last_evt, style=cell_style),
            )

        # Restore cursor to the same tab/mission if it still exists.
        if prior_ref:
            for idx, row_ref in enumerate(self.row_refs):
                if prior_ref.tab_id and row_ref.tab_id == prior_ref.tab_id:
                    self.move_cursor(row=idx)
                    break
                if prior_ref.virtual and row_ref.virtual and row_ref.mission_id == prior_ref.mission_id:
                    self.move_cursor(row=idx)
                    break

    def selected_tab_id(self) -> Optional[str]:
        ref = self.selected_ref()
        return ref.tab_id if ref and ref.tab_id else None

    def selected_mission_id(self) -> Optional[str]:
        ref = self.selected_ref()
        return ref.mission_id if ref and ref.mission_id else None

    def selected_ref(self) -> Optional[MissionRowRef]:
        if not self.row_refs:
            return None
        if self.cursor_row is None or self.cursor_row < 0:
            return None
        if self.cursor_row >= len(self.row_refs):
            return None
        return self.row_refs[self.cursor_row]


# ── selected mission card ─────────────────────────────────────────────────

class MissionCardWidget(Static):
    """Right-side durable mission graph card for the selected session."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.details_expanded = False

    def on_mount(self) -> None:
        self.update(self._empty())

    def toggle_details(self) -> None:
        self.details_expanded = not self.details_expanded

    def update_card(self, mission: Optional[db.Mission], live: Optional[LiveBuffer] = None) -> None:
        if mission is None:
            self.update(self._empty())
            return

        memory = db.get_memory(mission.mission_id) if mission.mission_id else None
        events = db.recent_events(mission.mission_id, limit=5) if mission.mission_id else []
        artifacts = db.artifacts_for_mission(mission.mission_id, limit=5) if mission.mission_id else []
        self.update(self._render_card(mission, memory, events, artifacts, live))

    def _empty(self) -> Text:
        text = Text()
        text.append("MISSION CARD\n", style="bold bright_green")
        text.append("select a session", style=COL_DIMMER)
        return text

    def _render_card(
        self,
        mission: db.Mission,
        memory: Optional[db.MissionMemory],
        events: list[db.MissionEvent],
        artifacts: list[db.MissionArtifact],
        live: Optional[LiveBuffer] = None,
    ) -> Text:
        text = Text()
        title = (memory.title if memory else "") or mission.goal or "(untitled)"
        text.append("MISSION CARD\n", style="bold bright_green")
        text.append(f"{title}\n", style="bold white")
        compact = [
            f"tab {(mission.tab_id or '?').split('-')[0]}",
            mission.state or "unknown",
        ]
        if memory and memory.phase:
            compact.append(memory.phase)
        text.append(" · ".join(compact), style=STATE_TEXT_STYLE.get(mission.state, COL_DIMMER))
        text.append("\n")

        self._render_latest_output(text, live)

        if not self.details_expanded:
            return text

        text.append("\n")
        self._field(text, "mission", mission.mission_id or "unset", muted=not mission.mission_id)
        self._field(text, "tab", (mission.tab_id or "?").split("-")[0])
        self._field(text, "state", mission.state, style=STATE_TEXT_STYLE.get(mission.state, COL_BODY))
        self._field(text, "phase", memory.phase if memory else "unset", muted=not (memory and memory.phase))
        self._field(text, "cmd", mission.cmd or "unset", muted=not mission.cmd)
        if mission.linked_worktree:
            self._field(text, "worktree", mission.linked_worktree)
        if mission.linked_pr:
            self._field(text, "pr", f"#{mission.linked_pr}")

        text.append("\n")
        if memory is None:
            text.append("graph memory: unset\n", style=COL_DIMMER)
            return text

        self._section_field(text, "why", memory.why)
        self._section_field(text, "done", memory.done_definition)
        self._section_field(text, "criteria", memory.acceptance_criteria)
        self._section_field(text, "plan", memory.current_plan)
        self._section_field(text, "next", memory.next_step)
        self._section_field(text, "blocked", memory.blocked_on)
        self._section_field(text, "decision", memory.last_decision)

        text.append("\n")
        self._field(text, "source", _join_nonempty(memory.source_kind, memory.source_ref))
        self._field(text, "confidence", f"{memory.confidence:.2f}")
        if memory.topic:
            self._field(text, "topic", memory.topic)

        text.append("\nEVENTS\n", style="bold bright_green")
        if events:
            for event in events:
                when = time.strftime("%H:%M", time.localtime(event.ts))
                text.append(f"{when} ", style=COL_DIMMER)
                text.append(f"{event.kind}", style=COL_ACCENT)
                text.append(f" {event.summary}\n", style=COL_BODY)
        else:
            text.append("unset\n", style=COL_DIMMER)

        text.append("\nARTIFACTS\n", style="bold bright_green")
        if artifacts:
            for artifact in artifacts:
                status_style = {
                    "pass": "bright_green",
                    "fail": "bright_red",
                    "pending": "bright_yellow",
                }.get(artifact.status, COL_DIMMER)
                text.append(f"{artifact.status}", style=status_style)
                text.append(f" {artifact.kind} ", style=COL_ACCENT)
                text.append(f"{artifact.path_or_url}\n", style=COL_BODY)
        else:
            text.append("unset\n", style=COL_DIMMER)

        return text

    def _render_latest_output(self, text: Text, live: Optional[LiveBuffer]) -> None:
        text.append("\nLATEST OUTPUT\n", style="bold bright_green")
        if live and live.buffer:
            limit = MISSION_CARD_EXPANDED_OUTPUT_LINES if self.details_expanded else MISSION_CARD_OUTPUT_LINES
            for line in _tail_lines(live.buffer, limit=limit, width=110):
                text.append("  ", style=COL_DIMMER)
                text.append(line, style=COL_BODY)
                text.append("\n")
        else:
            text.append("unset\n", style=COL_DIMMER)

    def _field(
        self,
        text: Text,
        label: str,
        value: str,
        style: str = COL_BODY,
        muted: bool = False,
    ) -> None:
        text.append(f"{label}: ", style="bold bright_green")
        text.append(value or "unset", style=COL_DIMMER if muted or not value else style)
        text.append("\n")

    def _section_field(self, text: Text, label: str, value: str) -> None:
        text.append(f"{label}: ", style="bold bright_green")
        if value:
            text.append(_single_line(value), style=COL_BODY)
        else:
            text.append("unset", style=COL_DIMMER)
        text.append("\n")


def _single_line(value: str, limit: int = 140) -> str:
    cleaned = " ".join(value.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


def _join_nonempty(*parts: str) -> str:
    return " ".join(part for part in parts if part).strip() or "unset"


def _loop_target_label(loop: db.PromptLoop) -> str:
    if not loop.target_mission_id:
        return "ticker"
    target = loop.target_mission_id[:14]
    if loop.target_tab_id:
        target += f"/{loop.target_tab_id.split('-')[0]}"
    return target


def _format_dashboard_ts(ts: float) -> str:
    if not ts:
        return "—"
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def _snapshot_markdown(
    mission: db.Mission,
    *,
    buffer: str,
    ts: str,
    memory: Optional[db.MissionMemory] = None,
) -> str:
    lines = [
        f"# Morpheus snapshot - {ts}",
        "",
        f"- **Tab**: `{mission.tab_id}`",
        f"- **Mission**: `{mission.mission_id or 'unset'}`",
        f"- **Goal**: {mission.goal or '(untitled)'}",
        f"- **State**: {mission.state}",
        f"- **Last event**: {mission.last_event}",
        f"- **Cmd**: `{mission.cmd or '?'}`",
    ]
    if mission.linked_worktree:
        lines.append(f"- **Worktree**: `{mission.linked_worktree}`")
    if mission.linked_pr:
        lines.append(f"- **PR**: #{mission.linked_pr}")
    if memory is not None:
        lines.extend(
            [
                "",
                "## Mission Card",
                "",
                f"- **Title**: {memory.title or mission.goal or '(untitled)'}",
                f"- **Why**: {memory.why or 'unset'}",
                f"- **Done**: {memory.done_definition or 'unset'}",
                f"- **Criteria**: {memory.acceptance_criteria or 'unset'}",
                f"- **Plan**: {memory.current_plan or 'unset'}",
                f"- **Next**: {memory.next_step or 'unset'}",
                f"- **Blocked**: {memory.blocked_on or 'unset'}",
                f"- **Phase**: {memory.phase or 'unset'}",
                f"- **Source**: {_join_nonempty(memory.source_kind, memory.source_ref)}",
            ]
        )
    lines.extend(["", "## Buffer", "", "```", buffer, "```", ""])
    return "\n".join(lines)


def _write_snapshot_file(
    mission: db.Mission,
    *,
    buffer: str,
    memory: Optional[db.MissionMemory] = None,
) -> Path:
    ts = time.strftime("%Y-%m-%dT%H-%M-%S")
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SNAPSHOT_DIR / f"{ts}-{mission.tab_id.split('-')[0]}.md"
    out_path.write_text(
        _snapshot_markdown(mission, buffer=buffer, ts=ts, memory=memory)
    )
    return out_path


def _resume_base_command(cmd: str) -> str:
    try:
        parts = shlex.split(cmd or "")
    except ValueError:
        parts = []
    if parts and Path(parts[0]).name in {"codex", "claude", "opencode", "aider"}:
        return " ".join(shlex.quote(part) for part in parts)
    return "codex"


def _resume_command(base_cmd: str, prompt: str) -> str:
    return f"{_resume_base_command(base_cmd)} {shlex.quote(prompt)}"


def _resume_prompt(
    mission: db.Mission,
    *,
    snapshot_path: Path,
    brief: str,
) -> str:
    return (
        "You are resuming a Morpheus mission in a fresh terminal session.\n\n"
        f"Original mission id: {mission.mission_id or 'unset'}\n"
        f"Original tab id: {mission.tab_id}\n"
        f"Goal: {mission.goal or '(untitled)'}\n"
        f"Snapshot file: {snapshot_path}\n\n"
        "Mission brief:\n"
        f"{brief}\n"
        "First read the snapshot file, then restate the current plan and next "
        "step before editing. Preserve unrelated changes and coordinate through "
        "Morpheus events/artifacts when you discover new proof or blockers."
    )


def _memory_for_resumed_mission(
    old_mission: db.Mission,
    *,
    new_mission_id: str,
    snapshot_path: Path,
    old_memory: Optional[db.MissionMemory],
) -> db.MissionMemory:
    if old_memory is None:
        return db.MissionMemory(
            mission_id=new_mission_id,
            title=old_mission.goal,
            source_kind="snapshot",
            source_ref=str(snapshot_path),
        )
    return db.MissionMemory(
        mission_id=new_mission_id,
        title=old_memory.title,
        why=old_memory.why,
        done_definition=old_memory.done_definition,
        acceptance_criteria=old_memory.acceptance_criteria,
        current_plan=old_memory.current_plan,
        next_step=old_memory.next_step,
        last_decision=old_memory.last_decision,
        last_summary=old_memory.last_summary,
        blocked_on=old_memory.blocked_on,
        phase=old_memory.phase if old_memory.phase != "archived" else "planning",
        confidence=old_memory.confidence,
        source_kind="snapshot",
        source_ref=str(snapshot_path),
        epic_ref=old_memory.epic_ref,
        issue_ref=old_memory.issue_ref,
        last_verified_at=old_memory.last_verified_at,
        claimed_paths=old_memory.claimed_paths,
        topic=old_memory.topic,
    )


# ── modal: spawn new session ──────────────────────────────────────────────

class NewSessionScreen(ModalScreen[Optional[NewSessionRequest]]):
    """Modal form to spawn a new iTerm tab + register a mission card."""

    CSS = """
    NewSessionScreen {
        align: center middle;
    }
    #dialog {
        width: 70;
        height: 20;
        border: round ansi_bright_green;
        background: black;
        padding: 1 2;
    }
    #dialog Label.title {
        color: ansi_bright_green;
        text-style: bold;
        margin-bottom: 1;
    }
    #dialog Label.hint {
        color: $text-muted;
    }
    Input {
        background: black;
        color: ansi_bright_green;
        border: round green;
        margin: 0 0 1 0;
    }
    Input:focus {
        border: round ansi_bright_green;
    }
    Select {
        background: black;
        color: ansi_bright_green;
        border: round green;
        margin: 0 0 1 0;
    }
    #buttons {
        height: 3;
        align: center middle;
    }
    Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
        Binding("ctrl+enter", "submit", "spawn"),
    ]

    def __init__(
        self,
        prd_candidates: Optional[list[prd_runs.PRDCandidate]] = None,
        root: Optional[Path] = None,
    ):
        super().__init__()
        self.prd_candidates = prd_candidates or []
        self.root = root or Path.cwd()

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Label(f"{RABBIT}  SPAWN NEW SESSION", classes="title")
            yield Input(placeholder="goal — one line, e.g. 'PR #224 review'", id="goal_input")
            yield Input(placeholder="command — e.g. 'codex' or 'claude'", id="cmd_input")
            options = [("no PRD/source file", "")]
            options.extend((candidate.label, str(candidate.path)) for candidate in self.prd_candidates)
            yield Select(options, prompt="PRD/source file (optional)", allow_blank=False, value="", id="prd_select")
            yield Label("enter to spawn · esc to cancel", classes="hint")
            with Horizontal(id="buttons"):
                yield Button("spawn", id="spawn_btn", variant="success")
                yield Button("cancel", id="cancel_btn", variant="default")

    def on_mount(self) -> None:
        self.query_one("#goal_input", Input).focus()

    def action_submit(self) -> None:
        goal = self.query_one("#goal_input", Input).value
        cmd = self.query_one("#cmd_input", Input).value
        prd_value = self.query_one("#prd_select", Select).value
        prd_path = prd_value if isinstance(prd_value, str) else ""
        if cmd:
            self.dismiss(NewSessionRequest(goal=goal.strip(), command=cmd.strip(), prd_path=prd_path))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "goal_input":
            self.query_one("#cmd_input", Input).focus()
        elif event.input.id == "cmd_input":
            self.action_submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "spawn_btn":
            self.action_submit()
        else:
            self.action_cancel()


# ── modal: edit selected mission ───────────────────────────────────────────

PHASE_OPTIONS = (
    "planning",
    "editing",
    "testing",
    "reviewing",
    "blocked",
    "done_needs_human",
    "archived",
)

SOURCE_KIND_OPTIONS = (
    "user",
    "transcript",
    "inferred",
    "imported",
    "prd",
    "issue",
)

EDIT_INPUT_ORDER = (
    "goal_input",
    "title_input",
    "why_input",
    "done_input",
    "criteria_input",
    "plan_input",
    "next_input",
    "blocked_input",
    "source_ref_input",
    "issue_ref_input",
    "linked_pr_input",
    "worktree_input",
    "claimed_paths_input",
    "topic_input",
)


class EditMissionScreen(ModalScreen[Optional[EditMissionRequest]]):
    """Edit the durable mission card fields for the selected session."""

    CSS = """
    EditMissionScreen { align: center middle; }
    #dialog {
        width: 92;
        height: 28;
        border: round ansi_bright_green;
        background: black;
        padding: 1 2;
    }
    #dialog Label.title {
        color: ansi_bright_green;
        text-style: bold;
        margin-bottom: 1;
    }
    #dialog Label.hint {
        color: $text-muted;
    }
    EditMissionScreen Input {
        background: black;
        color: ansi_bright_green;
        border: round green;
        margin: 0 0 1 0;
    }
    EditMissionScreen Select {
        background: black;
        color: ansi_bright_green;
        border: round green;
        margin: 0 0 1 0;
    }
    #buttons {
        height: 3;
        align: center middle;
    }
    Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
        Binding("ctrl+enter", "submit", "save"),
    ]

    def __init__(self, mission: db.Mission, memory: db.MissionMemory):
        super().__init__()
        self.mission = mission
        self.memory = memory

    def compose(self) -> ComposeResult:
        phase_options = _select_options(PHASE_OPTIONS, self.memory.phase)
        source_options = _select_options(SOURCE_KIND_OPTIONS, self.memory.source_kind)
        with Container(id="dialog"):
            yield Label(
                f"{RABBIT}  EDIT MISSION  {self.mission.tab_id.split('-')[0]}",
                classes="title",
            )
            yield Input(value=self.mission.goal, placeholder="goal", id="goal_input")
            yield Input(value=self.memory.title, placeholder="card title", id="title_input")
            yield Input(value=self.memory.why, placeholder="why this exists", id="why_input")
            yield Input(value=self.memory.done_definition, placeholder="done definition", id="done_input")
            yield Input(value=self.memory.acceptance_criteria, placeholder="acceptance criteria", id="criteria_input")
            yield Input(value=self.memory.current_plan, placeholder="current plan", id="plan_input")
            yield Input(value=self.memory.next_step, placeholder="next step", id="next_input")
            with Horizontal():
                yield Select(phase_options, value=self.memory.phase or "planning", id="phase_select")
                yield Select(source_options, value=self.memory.source_kind or "user", id="source_kind_select")
            yield Input(value=self.memory.blocked_on, placeholder="blocked on", id="blocked_input")
            yield Input(value=self.memory.source_ref, placeholder="source ref", id="source_ref_input")
            yield Input(value=self.memory.issue_ref, placeholder="issue / PR / task ref", id="issue_ref_input")
            yield Input(value=_format_optional_pr(self.mission.linked_pr), placeholder="linked PR number", id="linked_pr_input")
            yield Input(value=self.mission.linked_worktree, placeholder="linked worktree", id="worktree_input")
            yield Input(value=_display_claimed_paths(self.memory.claimed_paths), placeholder="claimed paths, comma-separated", id="claimed_paths_input")
            yield Input(value=self.memory.topic, placeholder="topic", id="topic_input")
            yield Label("ctrl+enter to save · esc to cancel", classes="hint", id="hint_label")
            with Horizontal(id="buttons"):
                yield Button("save", id="save_btn", variant="success")
                yield Button("cancel", id="cancel_btn", variant="default")

    def on_mount(self) -> None:
        self.query_one("#goal_input", Input).focus()

    def action_submit(self) -> None:
        try:
            linked_pr = _parse_optional_pr(self.query_one("#linked_pr_input", Input).value)
            claimed_paths = _normalize_claimed_paths(self.query_one("#claimed_paths_input", Input).value)
        except ValueError as e:
            self.query_one("#hint_label", Label).update(str(e))
            return

        phase_value = self.query_one("#phase_select", Select).value
        source_kind_value = self.query_one("#source_kind_select", Select).value
        self.dismiss(
            EditMissionRequest(
                tab_id=self.mission.tab_id,
                mission_id=self.memory.mission_id,
                goal=self.query_one("#goal_input", Input).value.strip(),
                title=self.query_one("#title_input", Input).value.strip(),
                why=self.query_one("#why_input", Input).value.strip(),
                done_definition=self.query_one("#done_input", Input).value.strip(),
                acceptance_criteria=self.query_one("#criteria_input", Input).value.strip(),
                current_plan=self.query_one("#plan_input", Input).value.strip(),
                next_step=self.query_one("#next_input", Input).value.strip(),
                phase=str(phase_value or "planning"),
                blocked_on=self.query_one("#blocked_input", Input).value.strip(),
                source_kind=str(source_kind_value or "user"),
                source_ref=self.query_one("#source_ref_input", Input).value.strip(),
                issue_ref=self.query_one("#issue_ref_input", Input).value.strip(),
                linked_pr=linked_pr,
                linked_worktree=self.query_one("#worktree_input", Input).value.strip(),
                claimed_paths=claimed_paths,
                topic=self.query_one("#topic_input", Input).value.strip(),
            )
        )

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        current_id = event.input.id
        if current_id not in EDIT_INPUT_ORDER:
            return
        current_index = EDIT_INPUT_ORDER.index(current_id)
        if current_index == len(EDIT_INPUT_ORDER) - 1:
            self.action_submit()
            return
        self.query_one(f"#{EDIT_INPUT_ORDER[current_index + 1]}", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save_btn":
            self.action_submit()
        else:
            self.action_cancel()


def _select_options(values: tuple[str, ...], current: str) -> list[tuple[str, str]]:
    options = [(value.replace("_", " "), value) for value in values]
    if current and current not in values:
        options.append((current.replace("_", " "), current))
    return options


def _format_optional_pr(value: Optional[int]) -> str:
    return "" if value is None else str(value)


def _parse_optional_pr(value: str) -> Optional[int]:
    cleaned = value.strip().lstrip("#")
    if not cleaned:
        return None
    if not cleaned.isdigit():
        raise ValueError("linked PR must be a number or blank")
    return int(cleaned)


def _display_claimed_paths(value: str) -> str:
    try:
        loaded = json.loads(value or "[]")
    except json.JSONDecodeError:
        return value
    if isinstance(loaded, list):
        return ", ".join(str(item) for item in loaded)
    return value


def _normalize_claimed_paths(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return "[]"
    if cleaned.startswith("["):
        try:
            loaded = json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise ValueError(f"claimed paths JSON is invalid: {e.msg}") from e
        if not isinstance(loaded, list) or not all(isinstance(item, str) for item in loaded):
            raise ValueError("claimed paths JSON must be a list of strings")
        return json.dumps([item.strip() for item in loaded if item.strip()])
    paths = [part.strip() for part in cleaned.split(",") if part.strip()]
    return json.dumps(paths)


# ── modal: post note ──────────────────────────────────────────────────────

class NoteScreen(ModalScreen[Optional[tuple[str, str, Optional[str]]]]):
    """Post a cross-session note. Returns (kind, text, tab_id_or_None)."""

    CSS = """
    NoteScreen { align: center middle; }
    #dialog {
        width: 70;
        height: 12;
        border: round ansi_bright_yellow;
        background: black;
        padding: 1 2;
    }
    #dialog Label.title {
        color: ansi_bright_yellow;
        text-style: bold;
        margin-bottom: 1;
    }
    Input {
        background: black;
        color: ansi_bright_yellow;
        border: round yellow;
    }
    Input:focus { border: round ansi_bright_yellow; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
    ]

    def __init__(self, attach_tab_id: Optional[str] = None):
        super().__init__()
        self.attach_tab_id = attach_tab_id

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            title = f"{RABBIT}  NEW NOTE"
            if self.attach_tab_id:
                title += f"  (attached to {self.attach_tab_id.split('-')[0]})"
            yield Label(title, classes="title")
            yield Input(placeholder="note text · enter to post · esc to cancel", id="text_input")

    def on_mount(self) -> None:
        self.query_one("#text_input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if text:
            self.dismiss(("note", text, self.attach_tab_id))
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class LoopScreen(ModalScreen[Optional[LoopRequest]]):
    """Create a recurring prompt loop routed to ticker/context or a mission."""

    CSS = """
    LoopScreen { align: center middle; }
    #dialog {
        width: 82;
        height: 20;
        border: round ansi_bright_yellow;
        background: black;
        padding: 1 2;
    }
    #dialog Label.title {
        color: ansi_bright_yellow;
        text-style: bold;
        margin-bottom: 1;
    }
    #dialog Label.hint {
        color: grey;
        margin-bottom: 1;
    }
    Input {
        background: black;
        color: ansi_bright_yellow;
        border: round yellow;
        margin-bottom: 1;
    }
    Input:focus { border: round ansi_bright_yellow; }
    Button { margin-right: 2; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
    ]

    def __init__(
        self,
        *,
        target_label: str,
        target_mission_id: str = "",
        target_tab_id: Optional[str] = None,
    ):
        super().__init__()
        self.target_label = target_label
        self.target_mission_id = target_mission_id
        self.target_tab_id = target_tab_id

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Label(f"{RABBIT}  NEW LOOP", classes="title")
            yield Label(f"target: {self.target_label}", classes="hint")
            yield Input(placeholder="name, e.g. morning market scan", id="loop_name")
            yield Input(placeholder="prompt to run on every loop tick", id="loop_prompt")
            yield Input(value="30m", placeholder="interval, e.g. 15m, 2h, daily", id="loop_interval")
            yield Input(value=loops_mod.DEFAULT_COMMAND, placeholder="command, e.g. codex exec", id="loop_command")
            with Horizontal():
                yield Button("create", variant="primary", id="loop_create")
                yield Button("cancel", id="loop_cancel")

    def on_mount(self) -> None:
        self.query_one("#loop_name", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        order = ["loop_name", "loop_prompt", "loop_interval", "loop_command"]
        if event.input.id in order:
            idx = order.index(event.input.id)
            if idx < len(order) - 1:
                self.query_one(f"#{order[idx + 1]}", Input).focus()
            else:
                self.action_submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "loop_create":
            self.action_submit()
        else:
            self.action_cancel()

    def action_submit(self) -> None:
        name = self.query_one("#loop_name", Input).value.strip()
        prompt = self.query_one("#loop_prompt", Input).value.strip()
        interval = self.query_one("#loop_interval", Input).value.strip() or "30m"
        command = self.query_one("#loop_command", Input).value.strip() or loops_mod.DEFAULT_COMMAND
        if not prompt:
            self.query_one("#loop_prompt", Input).focus()
            return
        if not name:
            name = prompt[:48] + ("…" if len(prompt) > 48 else "")
        self.dismiss(LoopRequest(
            name=name,
            prompt=prompt,
            interval=interval,
            command=command,
            target_mission_id=self.target_mission_id,
            target_tab_id=self.target_tab_id,
        ))

    def action_cancel(self) -> None:
        self.dismiss(None)


class LoopManagerScreen(ModalScreen[Optional[LoopActionRequest]]):
    """Manage configured prompt loops without running long commands in the TUI."""

    CSS = """
    LoopManagerScreen { align: center middle; }
    #loop-dialog {
        width: 104;
        height: 31;
        border: round ansi_bright_yellow;
        background: black;
        padding: 1 2;
    }
    #loop-dialog Label.title {
        color: ansi_bright_yellow;
        text-style: bold;
        margin-bottom: 1;
    }
    #loop-dialog Label.hint {
        color: grey;
        margin-bottom: 1;
    }
    #loops_table {
        height: 10;
        margin-bottom: 1;
    }
    #loop_detail {
        height: 11;
        border: round yellow;
        padding: 0 1;
        color: ansi_bright_yellow;
        margin-bottom: 1;
    }
    Button { margin-right: 2; }
    """

    BINDINGS = [
        Binding("escape", "close", "close"),
        Binding("q", "close", "close"),
        Binding("j", "cursor_down", "next"),
        Binding("k", "cursor_up", "prev"),
        Binding("down", "cursor_down", "next", show=False),
        Binding("up", "cursor_up", "prev", show=False),
        Binding("p", "toggle_selected", "pause/resume"),
        Binding("t", "join_selected", "join"),
        Binding("d", "delete_selected", "delete"),
    ]

    def __init__(
        self,
        *,
        loops: list[db.PromptLoop],
        runs_by_loop: dict[int, list[db.PromptLoopRun]],
        join_target_label: str,
        join_target_mission_id: str = "",
        join_target_tab_id: Optional[str] = None,
    ):
        super().__init__()
        self.loops = loops
        self.runs_by_loop = runs_by_loop
        self.join_target_label = join_target_label
        self.join_target_mission_id = join_target_mission_id
        self.join_target_tab_id = join_target_tab_id
        self.confirm_delete_id: Optional[int] = None

    def compose(self) -> ComposeResult:
        with Container(id="loop-dialog"):
            yield Label(f"{RABBIT}  LOOPS", classes="title")
            yield Label(f"join target: {self.join_target_label}", classes="hint")
            yield DataTable(id="loops_table")
            yield Static("", id="loop_detail")
            with Horizontal():
                yield Button("join target", variant="primary", id="loop_join")
                yield Button("pause/resume", id="loop_toggle")
                yield Button("delete", variant="error", id="loop_delete")
                yield Button("close", id="loop_close")

    def on_mount(self) -> None:
        table = self.query_one("#loops_table", DataTable)
        table.cursor_type = "row"
        table.add_columns("ID", "ST", "NAME", "EVERY", "NEXT", "TARGET", "LAST")
        for loop in self.loops:
            table.add_row(
                str(loop.id),
                loop.status,
                loop.name,
                loops_mod.format_interval(loop.interval_seconds),
                loops_mod.format_due(loop.next_run_at),
                _loop_target_label(loop),
                loop.last_summary or "—",
            )
        table.focus()
        self._refresh_detail()

    def on_data_table_row_highlighted(self, event) -> None:
        self.confirm_delete_id = None
        self._refresh_detail()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "loop_join":
            self.action_join_selected()
        elif event.button.id == "loop_toggle":
            self.action_toggle_selected()
        elif event.button.id == "loop_delete":
            self.action_delete_selected()
        else:
            self.action_close()

    def action_cursor_down(self) -> None:
        table = self.query_one("#loops_table", DataTable)
        table.action_cursor_down()
        self.confirm_delete_id = None
        self._refresh_detail()

    def action_cursor_up(self) -> None:
        table = self.query_one("#loops_table", DataTable)
        table.action_cursor_up()
        self.confirm_delete_id = None
        self._refresh_detail()

    def action_toggle_selected(self) -> None:
        loop = self._selected_loop()
        if loop is None:
            return
        action = "pause" if loop.status == "active" else "resume"
        self.dismiss(LoopActionRequest(action=action, loop_id=loop.id))

    def action_join_selected(self) -> None:
        loop = self._selected_loop()
        if loop is None:
            return
        if not self.join_target_mission_id:
            self.query_one("#loop_detail", Static).update(
                "Select a mission row before opening loops to join a loop to it."
            )
            return
        self.dismiss(LoopActionRequest(
            action="join",
            loop_id=loop.id,
            target_mission_id=self.join_target_mission_id,
            target_tab_id=self.join_target_tab_id,
        ))

    def action_delete_selected(self) -> None:
        loop = self._selected_loop()
        if loop is None:
            return
        if self.confirm_delete_id != loop.id:
            self.confirm_delete_id = loop.id
            self.query_one("#loop_detail", Static).update(
                self._detail(loop, confirm_delete=True)
            )
            return
        self.dismiss(LoopActionRequest(action="delete", loop_id=loop.id))

    def action_close(self) -> None:
        self.dismiss(None)

    def _selected_loop(self) -> Optional[db.PromptLoop]:
        if not self.loops:
            return None
        table = self.query_one("#loops_table", DataTable)
        row = table.cursor_row or 0
        if row < 0 or row >= len(self.loops):
            return None
        return self.loops[row]

    def _refresh_detail(self) -> None:
        loop = self._selected_loop()
        detail = "no loops configured yet" if loop is None else self._detail(loop)
        self.query_one("#loop_detail", Static).update(detail)

    def _detail(self, loop: db.PromptLoop, *, confirm_delete: bool = False) -> str:
        lines = [
            f"#{loop.id} {loop.name}",
            f"status {loop.status} · every {loops_mod.format_interval(loop.interval_seconds)} · next {loops_mod.format_due(loop.next_run_at)}",
            f"target {_loop_target_label(loop)} · command {loop.command}",
            f"prompt {loop.prompt}",
        ]
        runs = self.runs_by_loop.get(loop.id, [])
        if runs:
            lines.append("recent runs:")
            for run in runs[:4]:
                lines.append(
                    f"  #{run.id} {_format_dashboard_ts(run.started_at)} {run.status} "
                    f"{run.summary or run.output_path or 'no summary'}"
                )
        else:
            lines.append("recent runs: none")
        if confirm_delete:
            lines.append("")
            lines.append("Press delete again to remove this loop. Output files remain on disk.")
        return "\n".join(lines)


class SelectedBriefScreen(ModalScreen[None]):
    """Read-only selected mission brief."""

    CSS = """
    SelectedBriefScreen { align: center middle; }
    #brief-dialog {
        width: 94;
        height: 32;
        border: round ansi_bright_green;
        background: black;
        padding: 1 2;
    }
    #brief-dialog Label.title {
        color: ansi_bright_green;
        text-style: bold;
        margin-bottom: 1;
    }
    #brief-body {
        height: 1fr;
        color: white;
        background: black;
    }
    #brief-buttons {
        height: 3;
        align: center middle;
    }
    Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "close", "close"),
        Binding("q", "close", "close"),
    ]

    def __init__(self, brief: BriefScreenContent):
        super().__init__()
        self.brief = brief

    def compose(self) -> ComposeResult:
        with Container(id="brief-dialog"):
            yield Label(f"{RABBIT}  BRIEF  {self.brief.title}", classes="title")
            yield Static(self.brief.body, id="brief-body")
            with Horizontal(id="brief-buttons"):
                yield Button("close", id="brief_close", variant="success")

    def action_close(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.action_close()


class WorkerScreen(ModalScreen[Optional[WorkerRequest]]):
    """Spawn a manual child worker under a PRD run."""

    CSS = """
    WorkerScreen { align: center middle; }
    #dialog {
        width: 82;
        height: 20;
        border: round ansi_bright_green;
        background: black;
        padding: 1 2;
    }
    #dialog Label.title {
        color: ansi_bright_green;
        text-style: bold;
        margin-bottom: 1;
    }
    #dialog Label.hint {
        color: grey;
        margin-bottom: 1;
    }
    Input {
        background: black;
        color: ansi_bright_green;
        border: round green;
        margin-bottom: 1;
    }
    Input:focus { border: round ansi_bright_green; }
    Button { margin-right: 2; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
    ]

    def __init__(self, *, parent_id: str, run_title: str):
        super().__init__()
        self.parent_id = parent_id
        self.run_title = run_title

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Label(f"{RABBIT}  NEW PRD WORKER", classes="title")
            yield Label(f"parent: {self.run_title}", classes="hint")
            yield Input(placeholder="worker goal, e.g. implement loop run table", id="worker_goal")
            yield Input(value="codex", placeholder="command, e.g. codex", id="worker_command")
            yield Input(placeholder="owned scope/files, e.g. morpheus/dashboard.py only", id="worker_scope")
            yield Input(placeholder="verification, e.g. pytest tests/test_dashboard.py", id="worker_verify")
            with Horizontal():
                yield Button("spawn", variant="primary", id="worker_spawn")
                yield Button("cancel", id="worker_cancel")

    def on_mount(self) -> None:
        self.query_one("#worker_goal", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        order = ["worker_goal", "worker_command", "worker_scope", "worker_verify"]
        if event.input.id in order:
            idx = order.index(event.input.id)
            if idx < len(order) - 1:
                self.query_one(f"#{order[idx + 1]}", Input).focus()
            else:
                self.action_submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "worker_spawn":
            self.action_submit()
        else:
            self.action_cancel()

    def action_submit(self) -> None:
        goal = self.query_one("#worker_goal", Input).value.strip()
        command = self.query_one("#worker_command", Input).value.strip() or "codex"
        scope = self.query_one("#worker_scope", Input).value.strip()
        verification = self.query_one("#worker_verify", Input).value.strip()
        if not goal:
            self.query_one("#worker_goal", Input).focus()
            return
        self.dismiss(WorkerRequest(
            parent_id=self.parent_id,
            goal=goal,
            command=command,
            scope=scope,
            verification=verification,
        ))

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── main app ──────────────────────────────────────────────────────────────

FOOTER_BINDINGS = [
    Binding("j", "cursor_down", "↓"),
    Binding("k", "cursor_up", "↑"),
    Binding("down", "cursor_down", "next", show=False),
    Binding("up", "cursor_up", "prev", show=False),
    Binding("enter", "focus_session", "focus tab"),
    Binding("n", "new_session", "new"),
    Binding("b", "brief_selected", "brief"),
    Binding("e", "edit_mission", "edit"),
    Binding("space", "toggle_card_details", "details"),
    Binding("d", "kill_session", "kill"),
    Binding("p", "prune_stale", "prune"),
    Binding("s", "snapshot_session", "snapshot"),
    Binding("slash", "post_note", "note"),
    Binding("l", "new_loop", "loop"),
    Binding("shift+l", "manage_loops", "loops"),
    Binding("w", "new_worker", "worker"),
    Binding("r", "resume_fresh", "resume"),
    Binding("ctrl+r", "refresh_now", "refresh", show=False),
    Binding("q", "quit", "quit"),
    Binding("ctrl+c", "quit", "quit", show=False),
]


class MorpheusApp(App):
    """The interactive Matrix mission control."""

    TITLE = "▶ MORPHEUS"
    SUB_TITLE = f"mission control v{__version__}"

    BINDINGS = FOOTER_BINDINGS

    CSS = """
    Screen {
        background: black;
        color: white;
    }
    #header {
        height: 9;
        background: black;
        color: ansi_bright_green;
        content-align: center middle;
    }
    #body {
        height: 1fr;
    }
    #rain-panel {
        width: 28%;
        border: round green;
        background: black;
        color: green;
        padding: 0 0;
    }
    #missions-panel {
        width: 42%;
        border: round green;
        background: black;
    }
    #mission-card-panel {
        width: 30%;
        border: round green;
        background: black;
        color: white;
        padding: 0 1;
    }
    #alerts-panel {
        height: 14;
        border: round ansi_bright_yellow;
        background: black;
        color: white;
    }
    DataTable {
        background: black;
    }
    DataTable > .datatable--header {
        background: black;
        color: ansi_bright_green;
        text-style: bold;
    }
    DataTable > .datatable--cursor {
        background: ansi_bright_green 25%;
    }
    DataTable > .datatable--hover {
        background: green 15%;
    }
    Footer {
        background: black;
        color: ansi_bright_green;
    }
    """

    def __init__(self):
        super().__init__()
        self.iterm_conn = None
        self.alerts: deque = deque(maxlen=12)
        self.flashing: dict[str, tuple[float, str]] = {}
        self.last_seen_tabs: set[str] = set()
        self.last_seen_note_id: int = 0
        self.live_buffers: dict[str, LiveBuffer] = {}
        self.summary_alert_hashes: dict[str, str] = {}
        self.self_tab_id: Optional[str] = None
        self.self_session_id: Optional[str] = None
        self.current_missions: list[db.Mission] = []
        self.log_handle = None

    # ── compose ────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        banner = Text(BANNER, style="bold bright_green", justify="center")
        sub = Text(
            f"\nmission control v{__version__}  •  {RABBIT} follow the white rabbit",
            style=COL_MUTED, justify="center",
        )
        yield Static(banner + sub, id="header")
        with Horizontal(id="body"):
            yield RainWidget(id="rain-panel")
            yield MissionsTable(id="missions-panel")
            yield MissionCardWidget(id="mission-card-panel")
        yield RichLog(id="alerts-panel", markup=False, wrap=False, highlight=False)
        yield Footer()

    # ── mount + intervals ──────────────────────────────────────────────────

    async def on_mount(self) -> None:
        self.log_handle = core.setup_logging()

        # Watermarks so we don't replay existing notes/sessions as fresh alerts.
        try:
            recent = db.recent_notes(limit=1)
            self.last_seen_note_id = recent[0].id if recent else 0
        except Exception:
            pass
        try:
            self.last_seen_tabs = {m.tab_id for m in db.all_missions()}
        except Exception:
            pass

        # Connect to iTerm2 (async — runs inside Textual's event loop).
        try:
            self.iterm_conn = await iterm2.Connection.async_create()
        except Exception as e:
            self._push_alert(Alert(time.time(), "error", f"iTerm2 connect failed: {e}"))
            return

        await self._claim_self_tab()
        self._push_alert(Alert(
            time.time(), "spawn",
            f"morpheus dashboard online — follow the white rabbit."
        ))

        # Heavy tick: enumerate iTerm tabs, detect state, write DB + titles + context.
        self.set_interval(2.0, self._do_tick)
        # Light tick: animate rain.
        self.set_interval(0.12, self._do_rain_animate)
        # Table re-render (catches flash-expiry without waiting for next heavy tick).
        self.set_interval(0.5, self._refresh_table)

    async def _claim_self_tab(self) -> None:
        app = await iterm2.async_get_app(self.iterm_conn)
        if app is None:
            return
        window = app.current_terminal_window
        if window is None:
            return
        tab = window.current_tab
        if tab is None or tab.current_session is None:
            return
        self.self_tab_id = tab.tab_id
        self.self_session_id = tab.current_session.session_id
        try:
            await tab.current_session.async_set_name(naming.MORPHEUS_TAB_PREFIX)
        except Exception:
            pass

    # ── tick loop ──────────────────────────────────────────────────────────

    async def _do_tick(self) -> None:
        if self.iterm_conn is None:
            return
        try:
            await core._tick(
                self.iterm_conn, self.log_handle,
                on_state_change=self._on_state_change,
                on_alert=self._on_alert,
                on_tab_observed=self._on_tab_observed,
                ignored_tab_ids={self.self_tab_id} if self.self_tab_id else set(),
                ignored_session_ids={self.self_session_id} if self.self_session_id else set(),
            )
            missions = db.all_missions()
            self.current_missions = missions
            self._scan_new_missions(missions)
            self._scan_new_notes()
        except Exception as e:
            self.log_handle.exception("tick error: %s", e)

    async def _on_alert(self, kind: str, mission, text: str) -> None:
        """v0.4 derived alerts (token guard, worktree collision)."""
        alert_kind = "state" if kind.startswith("token") else "error"
        self._push_alert(Alert(time.time(), alert_kind, text))

    async def _on_tab_observed(self, tab: iterm_client.TabInfo, mission: db.Mission, detection) -> None:
        self.live_buffers[tab.tab_id] = LiveBuffer(
            tab_id=tab.tab_id,
            goal=mission.goal,
            state=mission.state,
            last_event=detection.last_event,
            buffer=tab.buffer,
            observed_at=time.time(),
        )

    def _do_rain_animate(self) -> None:
        try:
            rain_widget = self.query_one(RainWidget)
            rain_widget.update_buffers(
                self.live_buffers,
                self._selected_tab_id(),
                render=False,
            )
            rain_widget.tick_rain(self.current_missions)
        except Exception:
            pass

    def _refresh_table(self) -> None:
        try:
            missions = db.all_missions()
            prd_parents, prd_edges = self._prd_tree_context()
        except Exception:
            return
        self.current_missions = missions
        # Expire old flashes.
        now = time.time()
        self.flashing = {k: v for k, v in self.flashing.items() if v[0] > now}
        try:
            table = self.query_one(MissionsTable)
            table.refresh_rows(missions, self.flashing, prd_parents, prd_edges)
            self._refresh_mission_card(missions)
            self._refresh_live_stream()
        except Exception:
            pass

    def _prd_tree_context(self) -> tuple[list[db.MissionMemory], list[db.MissionEdge]]:
        parents = [
            mem for mem in db.all_memory()
            if mem.topic == "prd-run" or mem.source_kind == "prd"
        ]
        edges: list[db.MissionEdge] = []
        for parent in parents:
            edges.extend(db.edges_from_id(parent.mission_id, limit=50))
        return parents, edges

    def _selected_tab_id(self) -> Optional[str]:
        try:
            return self.query_one(MissionsTable).selected_tab_id()
        except Exception:
            return None

    def _refresh_live_stream(self) -> None:
        try:
            stream = self.query_one(RainWidget)
        except Exception:
            return
        stream.update_buffers(self.live_buffers, self._selected_tab_id())

    def _refresh_mission_card(self, missions: Optional[list[db.Mission]] = None) -> None:
        try:
            table = self.query_one(MissionsTable)
            card = self.query_one(MissionCardWidget)
        except Exception:
            return

        tab_id = table.selected_tab_id()
        mission_id = table.selected_mission_id()

        mission = None
        if tab_id and missions is not None:
            mission = next((m for m in missions if m.tab_id == tab_id), None)
        if tab_id and mission is None:
            mission = db.get(tab_id)
        if mission is None and mission_id:
            memory = db.get_memory(mission_id)
            if memory is not None:
                mission = db.Mission(
                    tab_id="",
                    mission_id=memory.mission_id,
                    goal=memory.title,
                    state=memory.phase or "planning",
                    cmd="prd-run",
                    buffer_changed_at=memory.updated_at,
                    last_event=memory.next_step or "PRD run",
                )
        if mission is None:
            card.update_card(None)
            return
        card.update_card(mission, self.live_buffers.get(tab_id))

    # ── change detection / alerts ──────────────────────────────────────────

    async def _on_state_change(self, mission: db.Mission, old: str, new: str) -> None:
        # Start a flash for this row.
        self.flashing[mission.tab_id] = (time.time() + FLASH_DURATION, new)
        if new == "finished":
            self._push_session_summary_alert(
                mission,
                verb="completed",
                fallback=mission.last_event or "session ended",
            )
            return
        if new == "idle" and old in ("working", "blocked"):
            pushed = self._push_session_summary_alert(
                mission,
                verb="ready",
                fallback="",
            )
            if pushed:
                return
            return

        # Push an alert for notable transitions.
        if new in ("blocked", "crashed", "finished") or old in ("blocked", "crashed"):
            emoji_old = naming.STATE_EMOJI.get(old, "⚪")
            emoji_new = naming.STATE_EMOJI.get(new, "⚪")
            goal = mission.goal or (mission.tab_id or "?").split("-")[0]
            self._push_alert(Alert(
                time.time(), "state",
                f"{emoji_old} → {emoji_new}  [{goal}] is now {new}",
            ))

    def _push_session_summary_alert(
        self,
        mission: db.Mission,
        *,
        verb: str,
        fallback: str = "",
    ) -> bool:
        goal = mission.goal or (mission.tab_id or "?").split("-")[0]
        live = self.live_buffers.get(mission.tab_id)
        headline = _session_headline(
            live.buffer if live else "",
            fallback=fallback,
        )
        if not headline:
            return False
        summary_key = _summary_alert_key(mission, headline, verb)
        if self.summary_alert_hashes.get(mission.tab_id) == summary_key:
            return False
        self.summary_alert_hashes[mission.tab_id] = summary_key
        detail = f" — {headline}" if headline else ""
        text = f"{verb} [{goal}]{detail}"
        self._push_alert(Alert(time.time(), "summary", text))
        if mission.mission_id:
            try:
                db.add_event(
                    mission.mission_id,
                    kind="summary",
                    actor="morpheus",
                    summary=text,
                    source_ref=f"tab:{mission.tab_id}",
                    metadata={
                        "state": mission.state,
                        "last_event": mission.last_event,
                        "summary_kind": verb,
                    },
                )
            except Exception:
                pass
        return True

    def _scan_new_missions(self, missions: list[db.Mission]) -> None:
        current = {m.tab_id for m in missions}
        new_tabs = current - self.last_seen_tabs
        closed_tabs = self.last_seen_tabs - current
        by_id = {m.tab_id: m for m in missions}
        for t in new_tabs:
            m = by_id.get(t)
            if m is None:
                continue
            self.flashing[t] = (time.time() + FLASH_DURATION, m.state or "working")
            self._push_alert(Alert(
                time.time(), "spawn",
                f"new session [{m.goal or '(untitled)'}] {t.split('-')[0]}",
            ))
        for t in closed_tabs:
            if self._is_self_tab_id(t):
                self.live_buffers.pop(t, None)
                self.summary_alert_hashes.pop(t, None)
                continue
            live = self.live_buffers.pop(t, None)
            self.summary_alert_hashes.pop(t, None)
            goal = (live.goal if live else "") or t.split("-")[0]
            headline = _session_headline(live.buffer if live else "")
            detail = f" — {headline}" if headline else ""
            self._push_alert(Alert(
                time.time(), "close",
                f"closed [{goal}]{detail}",
            ))
        self.last_seen_tabs = current

    def _is_self_tab_id(self, tab_id: str) -> bool:
        return bool(self.self_tab_id and tab_id == self.self_tab_id)

    def _scan_new_notes(self) -> None:
        recent = db.recent_notes(limit=12)
        fresh = [n for n in recent if n.id > self.last_seen_note_id]
        if not fresh:
            return
        goals = {m.tab_id: (m.goal or "(untitled)") for m in db.all_missions()}
        for n in sorted(fresh, key=lambda n: n.created_at):
            goal = goals.get(n.tab_id or "", "unknown")
            marker = {"note": "•", "claim": "⚑", "broadcast": "📡", "loop": "↻"}.get(n.kind, "•")
            self._push_alert(Alert(
                n.created_at, "note",
                f"{marker} note from [{goal}]: {n.text}",
            ))
        self.last_seen_note_id = max(n.id for n in fresh)

    def _push_alert(self, alert: Alert) -> None:
        self.alerts.appendleft(alert)
        self._redraw_alerts()

    def _redraw_alerts(self) -> None:
        try:
            rich_log = self.query_one("#alerts-panel", RichLog)
            rich_log.clear()
            for alert in self.alerts:
                rich_log.write(alert.render())
        except Exception:
            pass

    # ── actions (keybindings) ──────────────────────────────────────────────

    def action_cursor_down(self) -> None:
        try:
            self.query_one(MissionsTable).action_cursor_down()
            self._refresh_mission_card()
        except Exception:
            pass

    def action_cursor_up(self) -> None:
        try:
            self.query_one(MissionsTable).action_cursor_up()
            self._refresh_mission_card()
        except Exception:
            pass

    async def action_focus_session(self) -> None:
        if self.iterm_conn is None:
            return
        table = self.query_one(MissionsTable)
        tab_id = table.selected_tab_id()
        if not tab_id:
            return
        app = await iterm2.async_get_app(self.iterm_conn)
        if app is None:
            return
        for window in app.windows:
            for tab in window.tabs:
                if tab.tab_id == tab_id:
                    try:
                        await window.async_activate()
                    except Exception:
                        pass
                    try:
                        await tab.async_select()
                    except Exception:
                        try:
                            await tab.async_activate()
                        except Exception:
                            pass
                    self._push_alert(Alert(
                        time.time(), "spawn",
                        f"focused [{tab_id.split('-')[0]}]"
                    ))
                    return

    def action_new_session(self) -> None:
        if self.iterm_conn is None:
            return
        root = prd_runs.scan_root_for_worktree(
            self._selected_worktree_or_cwd(),
            fallback=Path.cwd(),
        )
        try:
            candidates = prd_runs.find_prds(root)
        except Exception:
            candidates = []
        self.push_screen(NewSessionScreen(prd_candidates=candidates, root=root), self._handle_new_session_result)

    def action_brief_selected(self) -> None:
        try:
            table = self.query_one(MissionsTable)
        except Exception:
            self._push_alert(Alert(time.time(), "error", "no mission selected to brief"))
            return

        tab_id = table.selected_tab_id()
        mission_id = table.selected_mission_id()
        mission = None
        live = None
        if tab_id:
            mission = db.get(tab_id)
            live = self.live_buffers.get(tab_id)
        elif mission_id:
            memory = db.get_memory(mission_id)
            if memory is not None:
                mission = db.Mission(
                    tab_id="",
                    mission_id=memory.mission_id,
                    goal=memory.title,
                    state=memory.phase or "planning",
                    cmd="prd-run",
                    buffer_changed_at=memory.updated_at,
                    last_event=memory.next_step or "PRD run",
                )

        if mission is None:
            self._push_alert(Alert(time.time(), "error", "no mission selected to brief"))
            return

        memory = db.get_memory(mission.mission_id) if mission.mission_id else None
        events = db.recent_events(mission.mission_id, limit=5) if mission.mission_id else []
        artifacts = db.artifacts_for_mission(mission.mission_id, limit=5) if mission.mission_id else []
        brief = mission_brief.build_selected_brief(
            mission,
            memory=memory,
            events=events,
            artifacts=artifacts,
            transcript=live.buffer if live else "",
        )
        self.push_screen(SelectedBriefScreen(BriefScreenContent(brief.title, brief.body)))

    def _loop_target_for_selection(self) -> tuple[str, str, Optional[str]]:
        target_tab_id = self._selected_tab_id()
        target_mission_id = ""
        target_label = "ticker/context only"
        if target_tab_id:
            mission = db.get(target_tab_id)
            if mission and mission.mission_id:
                target_mission_id = mission.mission_id
                target_label = f"{mission.goal or target_tab_id.split('-')[0]} ({target_tab_id.split('-')[0]})"
        else:
            try:
                mission_id = self.query_one(MissionsTable).selected_mission_id()
            except Exception:
                mission_id = None
            if mission_id:
                memory = db.get_memory(mission_id)
                if memory is not None:
                    target_mission_id = memory.mission_id
                    target_label = memory.title or memory.mission_id
        return target_label, target_mission_id, target_tab_id if target_mission_id else None

    def action_new_loop(self) -> None:
        target_label, target_mission_id, target_tab_id = self._loop_target_for_selection()
        self.push_screen(
            LoopScreen(
                target_label=target_label,
                target_mission_id=target_mission_id,
                target_tab_id=target_tab_id,
            ),
            self._handle_loop_result,
        )

    def action_manage_loops(self) -> None:
        target_label, target_mission_id, target_tab_id = self._loop_target_for_selection()
        loops = db.all_loops(include_paused=True)
        runs_by_loop = {loop.id: db.loop_runs(loop.id, limit=5) for loop in loops}
        self.push_screen(
            LoopManagerScreen(
                loops=loops,
                runs_by_loop=runs_by_loop,
                join_target_label=target_label,
                join_target_mission_id=target_mission_id,
                join_target_tab_id=target_tab_id,
            ),
            self._handle_loop_action_result,
        )

    def action_toggle_card_details(self) -> None:
        try:
            card = self.query_one(MissionCardWidget)
            card.toggle_details()
            self._refresh_mission_card(db.all_missions())
        except Exception:
            return

    def action_new_worker(self) -> None:
        if self.iterm_conn is None:
            return
        parent_id = self._selected_prd_parent_id()
        if not parent_id:
            self._push_alert(Alert(
                time.time(),
                "error",
                "select a PRD run parent/coordinator/worker before spawning a worker",
            ))
            return
        try:
            run = prd_runs.run_from_parent(parent_id)
        except Exception as e:
            self._push_alert(Alert(time.time(), "error", f"worker spawn failed: {e}"))
            return
        self.push_screen(
            WorkerScreen(parent_id=run.parent_id, run_title=run.title),
            self._handle_worker_result,
        )

    def _selected_prd_parent_id(self) -> Optional[str]:
        try:
            ref = self.query_one(MissionsTable).selected_ref()
        except Exception:
            return None
        if ref is None:
            return None
        if ref.virtual and ref.role == "prd":
            return ref.mission_id
        if ref.parent_id:
            return ref.parent_id
        if ref.mission_id:
            return prd_runs.parent_for_child(ref.mission_id)
        return None

    def _handle_loop_result(self, result: Optional[LoopRequest]) -> None:
        if result is None:
            return
        try:
            interval = loops_mod.parse_interval(result.interval)
            loop = db.create_loop(
                name=result.name,
                prompt=result.prompt,
                interval_seconds=interval,
                command=result.command,
                target_mission_id=result.target_mission_id,
                target_tab_id=result.target_tab_id,
            )
            if result.target_mission_id:
                db.add_event(
                    result.target_mission_id,
                    kind="loop_created",
                    actor="morpheus",
                    summary=f"Loop created: {loop.name} every {loops_mod.format_interval(interval)}",
                    source_ref=f"loop:{loop.id}",
                    metadata={"loop_id": loop.id, "target_tab_id": result.target_tab_id},
                )
            ledger_mod.log_action(
                "loop_create",
                tab_id=result.target_tab_id,
                details={
                    "loop_id": loop.id,
                    "name": loop.name,
                    "interval_seconds": loop.interval_seconds,
                    "target_mission_id": result.target_mission_id,
                },
            )
            ctx_mod.write_context_file()
            ctx_mod.write_context_json()
        except Exception as e:
            self._push_alert(Alert(time.time(), "error", f"loop create failed: {e}"))
            return

        target = f" → {result.target_tab_id.split('-')[0]}" if result.target_tab_id else " → ticker"
        self._push_alert(Alert(
            time.time(),
            "spawn",
            f"loop [{loop.name}] every {loops_mod.format_interval(loop.interval_seconds)}{target}",
        ))

    def _handle_loop_action_result(self, result: Optional[LoopActionRequest]) -> None:
        if result is None:
            return
        loop = db.get_loop(result.loop_id)
        if loop is None:
            self._push_alert(Alert(time.time(), "error", f"loop #{result.loop_id} not found"))
            return

        try:
            if result.action in {"pause", "resume"}:
                status = "paused" if result.action == "pause" else "active"
                updated = db.set_loop_status(loop.id, status)
                if updated is None:
                    raise ValueError(f"loop #{loop.id} not found")
                ledger_mod.log_action(
                    f"loop_{result.action}",
                    tab_id=updated.target_tab_id,
                    details={"loop_id": updated.id, "target_mission_id": updated.target_mission_id},
                )
                if updated.target_mission_id:
                    db.add_event(
                        updated.target_mission_id,
                        kind=f"loop_{result.action}d",
                        actor="morpheus",
                        summary=f"Loop {result.action}d: {updated.name}",
                        source_ref=f"loop:{updated.id}",
                        metadata={"loop_id": updated.id, "target_tab_id": updated.target_tab_id},
                    )
                message = f"{result.action}d loop [{updated.name}]"
            elif result.action == "join":
                updated = db.set_loop_target(
                    loop.id,
                    target_mission_id=result.target_mission_id,
                    target_tab_id=result.target_tab_id,
                )
                if updated is None:
                    raise ValueError(f"loop #{loop.id} not found")
                ledger_mod.log_action(
                    "loop_join",
                    tab_id=updated.target_tab_id,
                    details={"loop_id": updated.id, "target_mission_id": updated.target_mission_id},
                )
                db.add_event(
                    updated.target_mission_id,
                    kind="loop_joined",
                    actor="morpheus",
                    summary=f"Loop joined: {updated.name}",
                    source_ref=f"loop:{updated.id}",
                    metadata={"loop_id": updated.id, "target_tab_id": updated.target_tab_id},
                )
                message = f"joined loop [{updated.name}] → {_loop_target_label(updated)}"
            elif result.action == "delete":
                deleted = db.delete_loop(loop.id)
                if deleted is None:
                    raise ValueError(f"loop #{loop.id} not found")
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
                message = f"deleted loop [{deleted.name}]"
            else:
                return
            ctx_mod.write_context_file()
            ctx_mod.write_context_json()
        except Exception as e:
            self._push_alert(Alert(time.time(), "error", f"loop action failed: {e}"))
            return

        self._push_alert(Alert(time.time(), "summary", message))

    async def _handle_worker_result(self, result: Optional[WorkerRequest]) -> None:
        if result is None:
            return
        if self.iterm_conn is None:
            return
        try:
            run = prd_runs.run_from_parent(result.parent_id)
            cmd = prd_runs.worker_command(
                result.command,
                run,
                worker_goal=result.goal,
                scope=result.scope,
                verification=result.verification,
            )
        except Exception as e:
            self._push_alert(Alert(time.time(), "error", f"worker spawn failed: {e}"))
            return
        try:
            info = await iterm_client.spawn_tab(self.iterm_conn, command=cmd, goal=result.goal)
        except Exception as e:
            self._push_alert(Alert(time.time(), "error", f"worker spawn failed: {e}"))
            return
        if info is None:
            self._push_alert(Alert(time.time(), "error", "worker spawn failed — is iTerm focused?"))
            return

        now = time.time()
        mission = db.Mission(
            tab_id=info.tab_id,
            session_id=info.session_id,
            goal=result.goal,
            state="working",
            cmd=cmd,
            linked_worktree=str(run.prd_path.parent) if run.prd_path else "",
            buffer_changed_at=now,
            last_event_at=now,
            created_at=now,
        )
        db.upsert(mission)
        prd_runs.attach_worker(
            run,
            mission,
            scope=result.scope,
            verification=result.verification,
        )
        ledger_mod.log_action(
            "worker_spawn",
            tab_id=mission.tab_id,
            details={
                "mission_id": mission.mission_id,
                "parent_mission_id": run.parent_id,
                "scope": result.scope,
                "verification": result.verification,
            },
        )
        try:
            ctx_mod.write_context_file()
            ctx_mod.write_context_json()
        except Exception:
            pass
        self._push_alert(Alert(
            time.time(),
            "spawn",
            f"worker [{result.goal}] spawned under {run.title} {info.tab_id.split('-')[0]}",
        ))

    async def _handle_new_session_result(self, result: Optional[NewSessionRequest]) -> None:
        if not result:
            return
        goal = result.goal
        cmd = result.command
        if not cmd:
            return
        if self.iterm_conn is None:
            return
        run = None
        if result.prd_path:
            try:
                run = prd_runs.create_prd_run(result.prd_path, title=goal or None)
                goal = f"{run.title} coordinator"
                cmd = prd_runs.coordinator_command(cmd, run)
            except Exception as e:
                self._push_alert(Alert(time.time(), "error", f"PRD run failed: {e}"))
                return
        try:
            info = await iterm_client.spawn_tab(self.iterm_conn, command=cmd, goal=goal)
        except Exception as e:
            self._push_alert(Alert(time.time(), "error", f"spawn failed: {e}"))
            return
        if info is None:
            self._push_alert(Alert(time.time(), "error", "spawn failed — is iTerm focused?"))
            return
        now = time.time()
        m = db.Mission(
            tab_id=info.tab_id, session_id=info.session_id,
            goal=goal or naming.infer_goal_from_cmd(cmd) or "(untitled)",
            state="working", cmd=cmd,
            buffer_changed_at=now, last_event_at=now, created_at=now,
        )
        db.upsert(m)
        if run is not None:
            prd_runs.attach_coordinator(run, m)
            self._push_alert(Alert(
                time.time(), "spawn",
                f"PRD run [{run.title}] coordinator spawned {info.tab_id.split('-')[0]}",
            ))
        # Alert will fire on next _scan_new_missions.

    def _selected_worktree_or_cwd(self) -> Path:
        try:
            table = self.query_one(MissionsTable)
            tab_id = table.selected_tab_id()
            if tab_id:
                mission = db.get(tab_id)
                if mission and mission.linked_worktree:
                    return Path(mission.linked_worktree)
            mission_id = table.selected_mission_id()
            if mission_id:
                memory = db.get_memory(mission_id)
                if memory and memory.source_ref:
                    source = Path(memory.source_ref).expanduser()
                    if source.exists():
                        return source.parent if source.is_file() else source
        except Exception:
            pass
        return Path.cwd()

    def action_edit_mission(self) -> None:
        table = self.query_one(MissionsTable)
        tab_id = table.selected_tab_id()
        if not tab_id:
            self._push_alert(Alert(time.time(), "error", "no mission selected to edit"))
            return
        mission = db.get(tab_id)
        if mission is None:
            self._push_alert(Alert(time.time(), "error", f"mission [{tab_id.split('-')[0]}] not found"))
            return
        if not mission.mission_id:
            db.upsert(mission)
            mission = db.get(tab_id) or mission
        memory = db.get_memory(mission.mission_id) if mission.mission_id else None
        if memory is None:
            memory = db.MissionMemory(
                mission_id=mission.mission_id,
                title=mission.goal or mission.cmd or tab_id.split("-")[0],
                source_kind="user",
                source_ref=f"tab:{tab_id}",
            )
        self.push_screen(EditMissionScreen(mission, memory), self._handle_edit_mission_result)

    async def _handle_edit_mission_result(self, result: Optional[EditMissionRequest]) -> None:
        if result is None:
            return

        existing = db.get_memory(result.mission_id)
        memory = existing or db.MissionMemory(mission_id=result.mission_id)
        memory.title = result.title
        memory.why = result.why
        memory.done_definition = result.done_definition
        memory.acceptance_criteria = result.acceptance_criteria
        memory.current_plan = result.current_plan
        memory.next_step = result.next_step
        memory.blocked_on = result.blocked_on
        memory.phase = result.phase
        memory.confidence = 1.0
        memory.source_kind = result.source_kind
        memory.source_ref = result.source_ref
        memory.issue_ref = result.issue_ref
        memory.claimed_paths = result.claimed_paths
        memory.topic = result.topic
        db.upsert_memory(memory)
        db.update_mission_details(
            result.tab_id,
            goal=result.goal,
            linked_pr=result.linked_pr,
            linked_worktree=result.linked_worktree,
        )
        db.add_event(
            result.mission_id,
            kind="mission_edit",
            actor="user",
            summary="Mission card edited",
            source_ref=f"tab:{result.tab_id}",
            metadata={
                "tab_id": result.tab_id,
                "phase": result.phase,
                "linked_pr": result.linked_pr,
                "linked_worktree": result.linked_worktree,
            },
        )
        self._push_alert(Alert(
            time.time(),
            "summary",
            f"edited [{result.title or result.goal or result.tab_id.split('-')[0]}]",
        ))
        self._refresh_table()

    async def _kill_prd_run(self, parent_id: str) -> None:
        child_ids = {
            edge.to_id
            for edge in db.edges_from_id(parent_id, limit=100)
            if edge.relation in {"coordinator", "worker"}
        }
        live_children = [
            mission for mission in db.all_missions()
            if mission.mission_id and mission.mission_id in child_ids
        ]
        closed = 0
        for mission in live_children:
            ok = await iterm_client.close_tab(self.iterm_conn, mission.tab_id)
            if ok:
                db.delete(mission.tab_id)
                closed += 1

        db.archive_memory(parent_id, "PRD run killed from dashboard")
        self._push_alert(Alert(
            time.time(),
            "close",
            f"killed PRD run [{parent_id.split('_')[-1]}] and {closed}/{len(live_children)} child tabs",
        ))
        self._refresh_table()

    def _prune_orphan_prd_runs(self, live_missions: list[db.Mission], now: float) -> int:
        live_mission_ids = {mission.mission_id for mission in live_missions if mission.mission_id}
        parents = [
            memory for memory in db.all_memory()
            if memory.topic == "prd-run" or memory.source_kind == "prd"
        ]
        archived = 0
        for parent in parents:
            if (now - parent.updated_at) < ORPHAN_PRD_PRUNE_SECONDS:
                continue
            child_ids = {
                edge.to_id
                for edge in db.edges_from_id(parent.mission_id, limit=100)
                if edge.relation in {"coordinator", "worker"}
            }
            if child_ids & live_mission_ids:
                continue
            db.archive_memory(parent.mission_id, "orphan PRD run pruned from dashboard")
            archived += 1
        return archived

    async def action_kill_session(self) -> None:
        if self.iterm_conn is None:
            return
        table = self.query_one(MissionsTable)
        ref = table.selected_ref()
        if ref and ref.virtual and ref.role == "prd" and ref.mission_id:
            await self._kill_prd_run(ref.mission_id)
            return
        tab_id = ref.tab_id if ref and ref.tab_id else None
        if not tab_id:
            self._push_alert(Alert(time.time(), "error", "selected row has no live tab to kill"))
            return
        ok = await iterm_client.close_tab(self.iterm_conn, tab_id)
        if ok:
            db.delete(tab_id)
            self._push_alert(Alert(
                time.time(), "close",
                f"killed [{tab_id.split('-')[0]}]"
            ))

    async def action_prune_stale(self) -> None:
        if self.iterm_conn is None:
            return
        now = time.time()
        stale_threshold = 4 * 3600
        live = await iterm_client.enumerate_tabs(self.iterm_conn)
        live_ids = {t.tab_id for t in live}
        missions = db.all_missions()
        candidates = []
        for m in missions:
            if m.tab_id not in live_ids:
                continue
            if m.state not in ("idle", "finished"):
                continue
            if (now - m.buffer_changed_at) < stale_threshold:
                continue
            candidates.append(m)
        closed = 0
        for m in candidates:
            ok = await iterm_client.close_tab(self.iterm_conn, m.tab_id)
            if ok:
                db.delete(m.tab_id)
                closed += 1
        archived_prd = self._prune_orphan_prd_runs(
            [m for m in missions if m.tab_id in live_ids],
            now,
        )
        self._push_alert(Alert(
            time.time(), "close",
            f"pruned {closed}/{len(candidates)} stale tabs, archived {archived_prd} orphan PRD runs",
        ))
        if closed or archived_prd:
            self._refresh_table()

    async def action_snapshot_session(self) -> None:
        if self.iterm_conn is None:
            return
        table = self.query_one(MissionsTable)
        tab_id = table.selected_tab_id()
        if not tab_id:
            return
        live = await iterm_client.enumerate_tabs(self.iterm_conn)
        tab = next((t for t in live if t.tab_id == tab_id), None)
        if tab is None:
            return
        m = db.get(tab_id) or db.Mission(tab_id=tab_id)
        memory = db.get_memory(m.mission_id) if m.mission_id else None
        out_path = _write_snapshot_file(m, buffer=tab.buffer, memory=memory)
        if m.mission_id:
            db.add_artifact(
                m.mission_id,
                kind="snapshot",
                path_or_url=str(out_path),
                status="unknown",
                summary=f"Snapshot for {m.goal or tab_id}",
            )
        self._push_alert(Alert(
            time.time(), "spawn",
            f"snapshot → {out_path.name}",
        ))

    async def action_resume_fresh(self) -> None:
        if self.iterm_conn is None:
            return
        table = self.query_one(MissionsTable)
        old_tab_id = table.selected_tab_id()
        if not old_tab_id:
            self._push_alert(Alert(time.time(), "error", "no live mission selected to resume"))
            return
        live = await iterm_client.enumerate_tabs(self.iterm_conn)
        old_tab = next((t for t in live if t.tab_id == old_tab_id), None)
        if old_tab is None:
            self._push_alert(Alert(time.time(), "error", f"tab [{old_tab_id.split('-')[0]}] not found"))
            return

        old_mission = db.get(old_tab_id) or db.Mission(tab_id=old_tab_id)
        if not old_mission.mission_id:
            db.upsert(old_mission)
            old_mission = db.get(old_tab_id) or old_mission
        old_memory = db.get_memory(old_mission.mission_id) if old_mission.mission_id else None
        snapshot_path = _write_snapshot_file(old_mission, buffer=old_tab.buffer, memory=old_memory)
        if old_mission.mission_id:
            db.add_artifact(
                old_mission.mission_id,
                kind="snapshot",
                path_or_url=str(snapshot_path),
                status="unknown",
                summary=f"Resume snapshot for {old_mission.goal or old_tab_id}",
            )

        brief = mission_brief.build_selected_brief(
            old_mission,
            memory=old_memory,
            events=db.recent_events(old_mission.mission_id, limit=5) if old_mission.mission_id else [],
            artifacts=db.artifacts_for_mission(old_mission.mission_id, limit=5) if old_mission.mission_id else [],
            transcript=old_tab.buffer,
        )
        prompt = _resume_prompt(old_mission, snapshot_path=snapshot_path, brief=brief.body)
        cmd = _resume_command(old_mission.cmd, prompt)
        goal = old_mission.goal or naming.infer_goal_from_cmd(old_mission.cmd) or "resumed mission"
        try:
            info = await iterm_client.spawn_tab(self.iterm_conn, command=cmd, goal=goal)
        except Exception as e:
            self._push_alert(Alert(time.time(), "error", f"resume failed: {e}"))
            return
        if info is None:
            self._push_alert(Alert(time.time(), "error", "resume failed — is iTerm focused?"))
            return

        now = time.time()
        new_mission = db.Mission(
            tab_id=info.tab_id,
            session_id=info.session_id,
            goal=goal,
            state="working",
            cmd=cmd,
            linked_pr=old_mission.linked_pr,
            linked_worktree=old_mission.linked_worktree,
            buffer_changed_at=now,
            last_event_at=now,
            created_at=now,
        )
        db.upsert(new_mission)
        new_memory = _memory_for_resumed_mission(
            old_mission,
            new_mission_id=new_mission.mission_id,
            snapshot_path=snapshot_path,
            old_memory=old_memory,
        )
        db.upsert_memory(new_memory)
        if old_mission.mission_id and new_mission.mission_id:
            db.add_edge(
                new_mission.mission_id,
                old_mission.mission_id,
                relation="spawned_from",
                reason=f"Fresh resume from {snapshot_path}",
            )
            db.add_event(
                old_mission.mission_id,
                kind="resume",
                actor="morpheus",
                summary=f"Fresh session spawned: {info.tab_id.split('-')[0]}",
                source_ref=str(snapshot_path),
                metadata={"new_mission_id": new_mission.mission_id, "new_tab_id": info.tab_id},
            )
            db.add_event(
                new_mission.mission_id,
                kind="resume",
                actor="morpheus",
                summary=f"Resumed from {old_mission.mission_id}",
                source_ref=str(snapshot_path),
                metadata={"old_mission_id": old_mission.mission_id, "old_tab_id": old_tab_id},
            )

        closed_old = await iterm_client.close_tab(self.iterm_conn, old_tab_id)
        if closed_old:
            db.delete(old_tab_id)
        ledger_mod.log_action(
            "resume_fresh",
            tab_id=old_tab_id,
            details={
                "old_mission_id": old_mission.mission_id,
                "new_mission_id": new_mission.mission_id,
                "new_tab_id": info.tab_id,
                "snapshot_path": str(snapshot_path),
                "closed_old_tab": closed_old,
            },
        )
        try:
            ctx_mod.write_context_file()
            ctx_mod.write_context_json()
        except Exception:
            pass
        status = "resumed" if closed_old else "resumed; old tab still open"
        self._push_alert(Alert(
            time.time(),
            "spawn",
            f"{status} [{goal}] → {info.tab_id.split('-')[0]}",
        ))
        self._refresh_table()

    def action_post_note(self) -> None:
        table = self.query_one(MissionsTable)
        attach_tab = table.selected_tab_id()
        self.push_screen(NoteScreen(attach_tab_id=attach_tab), self._handle_note_result)

    async def _handle_note_result(self, result: Optional[tuple[str, str, Optional[str]]]) -> None:
        if not result:
            return
        kind, text, tab_id = result
        db.add_note(text=text, tab_id=tab_id, session_id=None, kind=kind)
        try:
            ctx_mod.write_context_file()
            ctx_mod.write_context_json()
        except Exception:
            pass
        # Will surface on next _scan_new_notes.

    async def action_refresh_now(self) -> None:
        await self._do_tick()
        self._refresh_table()


# ── public entry ──────────────────────────────────────────────────────────

def run() -> None:
    MorpheusApp().run()
