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

# Empty/decoration columns drop very slowly.
DECO_SPEED_MIN = 4
DECO_SPEED_MAX = 10


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
    chars: list[str] = None      # populated in __post_init__
    decorative: bool = False     # not tied to a real mission

    def __post_init__(self):
        if self.chars is None:
            self.chars = [random.choice(CHARS) for _ in range(max(self.height, 1))]
        self.head_y = random.randint(-self.height, 0)
        self.length = random.randint(8, max(10, self.height // 2 + 4))

    # ── state changes ──────────────────────────────────────────────────────

    def update_state(self, state: str, goal: str) -> None:
        if state != self.state:
            self.state = state
            self.speed_ticks = SPEED_FOR_STATE.get(state, 6)
        self.goal = goal

    # ── per-frame advance ──────────────────────────────────────────────────

    def tick(self) -> None:
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
            self.head_y = -random.randint(0, max(1, self.height // 2))
            self.length = random.randint(8, max(10, self.height // 2 + 4))
            self.chars = [random.choice(CHARS) for _ in range(self.height)]

    # ── per-cell rendering ─────────────────────────────────────────────────

    def get_cell(self, y: int) -> tuple[str, str]:
        # Outside the visible tail = blank.
        if y > self.head_y or y < self.head_y - self.length or y < 0 or y >= self.height:
            return " ", ""
        offset = self.head_y - y
        ch = self.chars[y]

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
        if offset == 0:
            # Bright white head for active, bright green head otherwise.
            return ch, "bold white" if self.state == "working" else "bold bright_green"
        if offset < 3:
            return ch, "bright_green" if self.state != "finished" else "green"
        if offset < 8:
            return ch, "green"
        return ch, "color(22)"  # dim dark green


class Rain:
    """Owns a grid of columns + the per-frame tick + render."""

    def __init__(self, cols: int, rows: int):
        self.cols = max(1, cols)
        self.rows = max(1, rows)
        self.columns: list[_Column] = []
        self._mission_xs: dict[str, int] = {}

    # ── geometry ───────────────────────────────────────────────────────────

    def resize(self, cols: int, rows: int) -> None:
        if cols == self.cols and rows == self.rows:
            return
        self.cols = max(1, cols)
        self.rows = max(1, rows)
        # Reset columns to match new geometry.
        for col in self.columns:
            col.height = self.rows
            col.chars = [random.choice(CHARS) for _ in range(self.rows)]
            col.head_y = random.randint(-self.rows, 0)

    # ── reconcile mission columns ──────────────────────────────────────────

    def update_missions(self, missions: list[db.Mission]) -> None:
        # Sort: blocked > crashed > working > idle > finished > unknown.
        state_order = {"blocked": 0, "crashed": 1, "working": 2,
                       "idle": 3, "finished": 4, "unknown": 5}
        sorted_m = sorted(
            [m for m in missions if m.state != "unknown" or m.goal],
            key=lambda m: (state_order.get(m.state, 9), -m.updated_at),
        )

        # Reserve every-other column for missions so the rain has visual breathing room.
        spacing = max(2, self.cols // max(1, len(sorted_m) or 1))
        spacing = min(spacing, 4)

        existing_by_tab = {tab_id: col for tab_id, col in zip(self._mission_xs.keys(), self.columns)
                           if tab_id in self._mission_xs}
        new_cols: list[_Column] = []
        used_xs: set[int] = set()
        new_mission_xs: dict[str, int] = {}

        for i, m in enumerate(sorted_m):
            x = (i * spacing) % self.cols
            while x in used_xs:
                x = (x + 1) % self.cols
            used_xs.add(x)
            new_mission_xs[m.tab_id] = x

            col = next((c for c in self.columns
                        if not c.decorative and c.x == self._mission_xs.get(m.tab_id, -1)), None)
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
            deco = next((c for c in self.columns if c.decorative and c.x == x), None)
            if deco is None:
                deco = _Column(
                    x=x, height=self.rows, state="unknown", goal="",
                    decorative=True,
                    speed_ticks=random.randint(DECO_SPEED_MIN, DECO_SPEED_MAX),
                )
            new_cols.append(deco)

        new_cols.sort(key=lambda c: c.x)
        self.columns = new_cols
        self._mission_xs = new_mission_xs

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
