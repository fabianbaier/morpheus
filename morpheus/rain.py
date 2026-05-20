"""Matrix-style digital rain renderer.

One vertical stream per active mission. Stream speed / brightness / color
encode the session's state — the rain is not decoration, it carries
information you can read at a glance.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from rich.text import Text

from morpheus import db

# Matrix-flavored character set: katakana + digits + a sprinkle of symbols.
KATAKANA = (
    "ァアィイゥウェエォオカガキギクグケゲコゴサザシジスズセゼソゾタダチヂッ"
    "ツヅテデトドナニヌネノハバパヒビピフブプヘベペホボポマミムメモャヤュユ"
    "ョヨラリルレロワヲン"
)
DIGITS = "0123456789"
SYMBOLS = "<>[]{}/\\|=+-*&^%$#@!?:;.,"
CHARS = KATAKANA + DIGITS + SYMBOLS

# How many frames per drop-down for each state. Lower = faster.
SPEED_FOR_STATE = {
    "working":  1,
    "blocked":  2,
    "crashed":  1,
    "idle":     5,
    "finished": 9,
    "unknown":  6,
}

# Empty/decoration columns should still feel alive when session output is quiet.
DECO_SPEED_MIN = 2
DECO_SPEED_MAX = 5
AMBIENT_DENSITY = 0.18
ACTIVE_AMBIENT_DENSITY = 0.26
AMBIENT_CHURN = 0.22
AMBIENT_MOD = 100


@dataclass
class _Column:
    x: int
    height: int
    state: str = "unknown"
    goal: str = ""
    head_y: int = 0
    length: int = 12
    speed_ticks: int = 6
    tick_counter: int = 0
    chars: list[str] | None = None      # populated in __post_init__
    ambient: list[str] | None = None
    ambient_phase: int = 0
    decorative: bool = False     # not tied to a real mission

    def __post_init__(self):
        if self.chars is None:
            self._reset_chars()
        if self.ambient is None:
            self._reset_ambient()
        self.length = self._new_length()
        self._reset_head(initial=True)

    def _reset_chars(self) -> None:
        self.chars = [random.choice(CHARS) for _ in range(max(self.height, 1))]

    def _reset_ambient(self) -> None:
        self.ambient = [random.choice(CHARS) for _ in range(max(self.height, 1))]
        self.ambient_phase = random.randint(0, AMBIENT_MOD - 1)

    def _new_length(self) -> int:
        return random.randint(8, max(10, self.height // 2 + 4))

    def _reset_head(self, *, initial: bool = False) -> None:
        if self.decorative:
            if initial:
                self.head_y = random.randint(0, max(0, self.height - 1))
                return
            self.head_y = -random.randint(0, max(1, self.height // 5))
            return
        self.head_y = (
            random.randint(-self.height, 0)
            if initial
            else -random.randint(0, max(1, self.height // 2))
        )

    # ── state changes ──────────────────────────────────────────────────────

    def update_state(self, state: str, goal: str) -> None:
        if state != self.state:
            self.state = state
            self.speed_ticks = SPEED_FOR_STATE.get(state, 6)
        self.goal = goal

    # ── per-frame advance ──────────────────────────────────────────────────

    def tick(self) -> None:
        self.ambient_phase = (self.ambient_phase + 1) % AMBIENT_MOD
        if self.decorative and self.chars and self.ambient and random.random() < AMBIENT_CHURN:
            pos = random.randrange(len(self.chars))
            self.chars[pos] = random.choice(CHARS)
            self.ambient[pos % len(self.ambient)] = random.choice(CHARS)
        self.tick_counter += 1
        if self.tick_counter < self.speed_ticks:
            return
        self.tick_counter = 0
        self.head_y += 1
        if 0 <= self.head_y < self.height:
            # As the head moves, swap in a new character.
            self.chars[self.head_y] = random.choice(CHARS)
        if self.head_y - self.length > self.height:
            # Restart with a random delay before the next drop.
            self._reset_head()
            self.length = self._new_length()
            self._reset_chars()
            self._reset_ambient()

    # ── per-cell rendering ─────────────────────────────────────────────────

    def get_cell(self, y: int) -> tuple[str, str]:
        if y > self.head_y or y < self.head_y - self.length or y < 0 or y >= self.height:
            return self._ambient_cell(y)
        offset = self.head_y - y
        ch = self.chars[y] if self.chars else random.choice(CHARS)

        # Crashed: erratic red glitch.
        if self.state == "crashed":
            if random.random() < 0.35:
                return random.choice(CHARS), "bold red"
            return ch, "red"

        # Blocked: yellow, head extra bright.
        if self.state == "blocked":
            if offset == 0:
                return ch, "bold bright_yellow"
            if offset < 4:
                return ch, "bright_yellow"
            return ch, "yellow"

        # Green spectrum for working / idle / finished / unknown / decorative.
        if self.decorative:
            return ch, "bold bright_green" if offset == 0 else ""
        if offset == 0:
            # Bright white head for active, bright green head otherwise.
            return ch, "bold white" if self.state == "working" else "bold bright_green"
        if offset < 3:
            return ch, "bright_green" if self.state != "finished" else "green"
        if offset < 8:
            return ch, "green"
        return ch, "color(22)"  # dim dark green

    def _ambient_cell(self, y: int) -> tuple[str, str]:
        density = (
            ACTIVE_AMBIENT_DENSITY
            if self.state in {"working", "blocked", "crashed"}
            else AMBIENT_DENSITY
        )
        if self.decorative:
            density = AMBIENT_DENSITY
        threshold = max(0, min(AMBIENT_MOD, int(density * AMBIENT_MOD)))
        gate = (self.x * 37 + y * 17 + self.ambient_phase * 23) % AMBIENT_MOD
        if gate >= threshold:
            return " ", ""
        ch = self.ambient[y % len(self.ambient)] if self.ambient else CHARS[(self.x + y) % len(CHARS)]
        if self.state == "crashed":
            return ch, "red"
        if self.state == "blocked":
            return ch, "yellow"
        if self.state == "working":
            return ch, "color(28)"
        return ch, ""


class Rain:
    """Owns a grid of columns + the per-frame tick + render."""

    def __init__(self, cols: int, rows: int):
        self.cols = max(1, cols)
        self.rows = max(1, rows)
        self.columns: list[_Column] = []
        self._mission_xs: dict[str, int] = {}
        self._mission_signature: tuple[tuple[str, str, str, float, float], ...] | None = None
        self._fill_decorative_columns()

    # ── geometry ───────────────────────────────────────────────────────────

    def resize(self, cols: int, rows: int) -> None:
        if cols == self.cols and rows == self.rows:
            return
        self.cols = max(1, cols)
        self.rows = max(1, rows)
        # Reset columns to match new geometry.
        for col in self.columns:
            col.height = self.rows
            col.length = col._new_length()
            col._reset_chars()
            col._reset_ambient()
            col._reset_head(initial=True)
        self.columns = [col for col in self.columns if 0 <= col.x < self.cols]
        self._mission_signature = None
        self._fill_decorative_columns()

    def _new_decorative_column(self, x: int) -> _Column:
        return _Column(
            x=x, height=self.rows, state="unknown", goal="",
            decorative=True,
            speed_ticks=random.randint(DECO_SPEED_MIN, DECO_SPEED_MAX),
        )

    def _fill_decorative_columns(self) -> None:
        used_xs = {col.x for col in self.columns if 0 <= col.x < self.cols}
        for x in range(self.cols):
            if x not in used_xs:
                self.columns.append(self._new_decorative_column(x))
        self.columns.sort(key=lambda c: c.x)

    # ── reconcile mission columns ──────────────────────────────────────────

    def update_missions(self, missions: list[db.Mission]) -> None:
        # Sort: blocked > crashed > working > idle > finished > unknown.
        state_order = {"blocked": 0, "crashed": 1, "working": 2,
                       "idle": 3, "finished": 4, "unknown": 5}
        sorted_m = sorted(
            [m for m in missions if m.state != "unknown" or m.goal],
            key=lambda m: (state_order.get(m.state, 9), -m.updated_at),
        )
        signature = tuple(
            (m.tab_id, m.state, m.goal, m.updated_at, m.buffer_changed_at)
            for m in sorted_m
        )
        if signature == self._mission_signature:
            return

        # Reserve every-other column for missions so the rain has visual breathing room.
        spacing = max(2, self.cols // max(1, len(sorted_m) or 1))
        spacing = min(spacing, 4)

        new_cols: list[_Column] = []
        used_xs: set[int] = set()
        new_mission_xs: dict[str, int] = {}
        previous_mission_cols = {
            tab_id: col
            for tab_id, old_x in self._mission_xs.items()
            for col in self.columns
            if not col.decorative and col.x == old_x
        }
        decorative_by_x = {col.x: col for col in self.columns if col.decorative}

        for i, m in enumerate(sorted_m):
            x = (i * spacing) % self.cols
            while x in used_xs:
                x = (x + 1) % self.cols
            used_xs.add(x)
            new_mission_xs[m.tab_id] = x

            col = previous_mission_cols.get(m.tab_id)
            if col is None:
                col = _Column(x=x, height=self.rows, state=m.state, goal=m.goal)
            else:
                col.x = x
                col.update_state(m.state, m.goal)
            new_cols.append(col)

        # Fill rest with decorative streams.
        for x in range(self.cols):
            if x in used_xs:
                continue
            # Reuse existing decorative col if at same x.
            deco = decorative_by_x.get(x)
            if deco is None:
                deco = self._new_decorative_column(x)
            new_cols.append(deco)

        new_cols.sort(key=lambda c: c.x)
        self.columns = new_cols
        self._mission_xs = new_mission_xs
        self._mission_signature = signature

    # ── per-frame advance ──────────────────────────────────────────────────

    def tick(self) -> None:
        for col in self.columns:
            col.tick()

    # ── render to a Rich Text ──────────────────────────────────────────────

    def render(self) -> Text:
        out = Text()
        # Pre-index columns by x for fast lookup.
        col_at_x: list[_Column | None] = [None] * self.cols
        for col in self.columns:
            if 0 <= col.x < self.cols:
                col_at_x[col.x] = col

        for y in range(self.rows):
            for x in range(self.cols):
                col = col_at_x[x]
                if col is None:
                    out.append(" ")
                    continue
                ch, style = col.get_cell(y)
                out.append(ch, style=style)
            if y < self.rows - 1:
                out.append("\n")
        return out
