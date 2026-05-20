"""Terminal intro animation for the Morpheus cockpit.

The intro is intentionally self-contained and best-effort. It should never
block the dashboard if the terminal, network, or geolocation provider is slow.
"""

from __future__ import annotations

import json
import math
import os
import queue
import random
import select
import shutil
import sys
import termios
import threading
import time
import tty
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib import request

from morpheus import config as cfg_mod
from morpheus import rain as rain_mod

INTRO_GEO_CACHE_PATH = cfg_mod.CONFIG_DIR / "intro_geo.json"
INTRO_GEO_CACHE_SECONDS = 24 * 60 * 60
INTRO_GEO_ENDPOINT = "https://ipapi.co/json/"
INTRO_GEO_TIMEOUT_SECONDS = 0.8
DEFAULT_INTRO_SECONDS = 7.5
MIN_INTRO_SECONDS = 5.0
MAX_INTRO_SECONDS = 24.0
INTRO_FPS = 20.0
BASE_GLOBE_WIDTH = 58
BASE_GLOBE_HEIGHT = 26

_STYLE_RESET = "\x1b[0m"
_GREEN = "\x1b[38;5;46m"
_DIM_GREEN = "\x1b[38;5;22m"
_BRIGHT_GREEN = "\x1b[1;38;5;118m"
_CYAN = "\x1b[38;5;51m"
_WHITE = "\x1b[1;38;5;231m"
_BLUE = "\x1b[38;5;25m"
_LAND = "\x1b[38;5;35m"
_CLOUD = "\x1b[38;5;250m"
_AMBER = "\x1b[38;5;220m"

_LAND_POLYGONS: tuple[tuple[tuple[float, float], ...], ...] = (
    # North America, with a deliberately more detailed Pacific coast so the
    # California zoom reads correctly at terminal resolution.
    (
        (-168, 72), (-116, 73), (-60, 72), (-52, 56), (-64, 47),
        (-72, 41), (-76, 35), (-81, 28), (-80, 25), (-90, 29),
        (-97, 25), (-87, 18), (-83, 10), (-92, 8), (-101, 16),
        (-108, 21), (-111, 25), (-114, 28), (-117, 32.5),
        (-119.5, 34.3), (-121.0, 35.6), (-122.2, 37.2),
        (-122.8, 38.3), (-123.7, 40.4), (-124.3, 42.0),
        (-124.5, 48.5), (-130, 53), (-145, 58), (-160, 62),
        (-168, 72),
    ),
    # Greenland.
    ((-74, 60), (-46, 60), (-20, 74), (-42, 83), (-66, 78), (-74, 60)),
    # South America.
    (
        (-81, 12), (-66, 11), (-50, -1), (-35, -8), (-40, -22),
        (-52, -55), (-67, -55), (-76, -35), (-81, -18), (-81, 12),
    ),
    # Africa.
    (
        (-18, 36), (4, 37), (32, 31), (50, 10), (43, -35),
        (19, -35), (8, -21), (-16, 4), (-18, 36),
    ),
    # Eurasia, simplified but continent-shaped enough for a rotating globe.
    (
        (-11, 35), (4, 58), (30, 70), (75, 74), (145, 62),
        (156, 48), (132, 32), (118, 12), (102, 1), (78, 7),
        (58, 25), (42, 14), (29, 31), (12, 36), (-11, 35),
    ),
    # Southeast Asia / Indonesia massing.
    ((94, 22), (122, 20), (143, 2), (126, -10), (104, -7), (94, 22)),
    # Australia.
    ((112, -10), (154, -12), (153, -39), (116, -35), (112, -10)),
    # Antarctica edge.
    ((-180, -72), (-90, -78), (0, -74), (90, -78), (180, -72), (180, -90), (-180, -90), (-180, -72)),
)


@dataclass(frozen=True)
class IntroLocation:
    latitude: float
    longitude: float
    label: str = ""


@dataclass(frozen=True)
class IntroOptions:
    enabled: bool = True
    geolocation: bool = True
    duration_seconds: float = DEFAULT_INTRO_SECONDS
    location: Optional[IntroLocation] = None


def load_options(
    cfg: Optional[Mapping[str, Any]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> IntroOptions:
    """Return intro settings, with geolocation default-on and opt-out."""
    cfg = cfg if cfg is not None else cfg_mod.load()
    env = env if env is not None else os.environ
    intro_cfg = cfg.get("intro", {}) if isinstance(cfg, Mapping) else {}
    if not isinstance(intro_cfg, Mapping):
        intro_cfg = {}

    enabled = _as_bool(intro_cfg.get("enabled"), True)
    if _env_is_false(env.get("MORPHEUS_INTRO")) or _env_is_true(env.get("MORPHEUS_NO_INTRO")):
        enabled = False

    geolocation = _as_bool(intro_cfg.get("geolocation"), True)
    if "MORPHEUS_INTRO_GEO" in env:
        geolocation = _env_is_true(env.get("MORPHEUS_INTRO_GEO"))

    duration = _as_float(intro_cfg.get("duration_seconds"), DEFAULT_INTRO_SECONDS)
    mode = (env.get("MORPHEUS_INTRO_MODE") or str(intro_cfg.get("mode", ""))).lower()
    if mode == "short":
        duration = 5.0
    elif mode == "cinematic":
        duration = 12.0
    if "MORPHEUS_INTRO_SECONDS" in env:
        duration = _as_float(env.get("MORPHEUS_INTRO_SECONDS"), duration)
    duration = max(MIN_INTRO_SECONDS, min(MAX_INTRO_SECONDS, duration))

    location = parse_location(env.get("MORPHEUS_INTRO_LOCATION") or str(intro_cfg.get("location", "") or ""))
    return IntroOptions(
        enabled=enabled,
        geolocation=geolocation,
        duration_seconds=duration,
        location=location,
    )


def parse_location(value: str) -> Optional[IntroLocation]:
    value = value.strip()
    if not value:
        return None
    pieces = [piece.strip() for piece in value.split(",")]
    if len(pieces) < 2:
        return None
    try:
        latitude = float(pieces[0])
        longitude = float(pieces[1])
    except ValueError:
        return None
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        return None
    label = ", ".join(piece for piece in pieces[2:] if piece)
    return IntroLocation(latitude=latitude, longitude=longitude, label=label)


def maybe_play_intro() -> None:
    options = load_options()
    if not options.enabled:
        return
    if not sys.stdout.isatty() or os.environ.get("TERM") == "dumb":
        return

    width, height = shutil.get_terminal_size((100, 32))
    if width < 42 or height < 16:
        return

    player = _IntroPlayer(options, width=min(width, 160), height=min(height, 48))
    player.play()


def resolve_location(options: IntroOptions) -> Optional[IntroLocation]:
    if options.location is not None:
        return options.location
    cached = _read_cached_location()
    if cached is not None:
        return cached
    if not options.geolocation:
        return None
    location = _fetch_ip_location()
    if location is not None:
        _write_cached_location(location)
    return location


def _read_cached_location(now: Optional[float] = None) -> Optional[IntroLocation]:
    now = time.time() if now is None else now
    try:
        payload = json.loads(INTRO_GEO_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    try:
        if now - float(payload.get("fetched_at", 0)) > INTRO_GEO_CACHE_SECONDS:
            return None
        return IntroLocation(
            latitude=float(payload["latitude"]),
            longitude=float(payload["longitude"]),
            label=str(payload.get("label") or ""),
        )
    except Exception:
        return None


def _write_cached_location(location: IntroLocation) -> None:
    try:
        cfg_mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        INTRO_GEO_CACHE_PATH.write_text(
            json.dumps({
                "latitude": location.latitude,
                "longitude": location.longitude,
                "label": location.label,
                "fetched_at": time.time(),
            }),
            encoding="utf-8",
        )
    except Exception:
        pass


def _fetch_ip_location() -> Optional[IntroLocation]:
    req = request.Request(
        INTRO_GEO_ENDPOINT,
        headers={"User-Agent": "morpheus-mc intro geolocation"},
    )
    try:
        with request.urlopen(req, timeout=INTRO_GEO_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    try:
        latitude = float(payload["latitude"])
        longitude = float(payload["longitude"])
    except Exception:
        return None
    label_parts = [
        str(payload.get("city") or "").strip(),
        str(payload.get("region") or "").strip(),
        str(payload.get("country_name") or payload.get("country") or "").strip(),
    ]
    label = ", ".join(part for part in label_parts if part)
    return IntroLocation(latitude=latitude, longitude=longitude, label=label)


class _IntroPlayer:
    def __init__(self, options: IntroOptions, *, width: int, height: int):
        self.options = options
        self.width = width
        self.height = height
        self.location: Optional[IntroLocation] = options.location
        self._location_queue: queue.Queue[Optional[IntroLocation]] = queue.Queue(maxsize=1)
        self._old_termios = None

    def play(self) -> None:
        self._start_location_lookup()
        frame_count = max(1, int(self.options.duration_seconds * INTRO_FPS))
        interval = 1.0 / INTRO_FPS
        self._enter_terminal()
        try:
            for frame in range(frame_count):
                self._drain_location_queue()
                progress = frame / max(1, frame_count - 1)
                sys.stdout.write("\x1b[H")
                sys.stdout.write(self._render_frame(frame, progress))
                sys.stdout.flush()
                if self._key_pressed():
                    break
                time.sleep(interval)
        finally:
            self._leave_terminal()

    def _start_location_lookup(self) -> None:
        def worker() -> None:
            try:
                self._location_queue.put(resolve_location(self.options), block=False)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _drain_location_queue(self) -> None:
        try:
            location = self._location_queue.get_nowait()
        except queue.Empty:
            return
        if location is not None:
            self.location = location

    def _enter_terminal(self) -> None:
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[2J\x1b[H")
        sys.stdout.flush()
        if sys.stdin.isatty():
            try:
                self._old_termios = termios.tcgetattr(sys.stdin.fileno())
                tty.setcbreak(sys.stdin.fileno())
            except Exception:
                self._old_termios = None

    def _leave_terminal(self) -> None:
        if self._old_termios is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old_termios)
            except Exception:
                pass
        sys.stdout.write("\x1b[0m\x1b[2J\x1b[H\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()

    def _key_pressed(self) -> bool:
        if not sys.stdin.isatty():
            return False
        try:
            ready, _, _ = select.select([sys.stdin], [], [], 0)
            if not ready:
                return False
            os.read(sys.stdin.fileno(), 1)
            return True
        except Exception:
            return False

    def _render_frame(self, frame: int, progress: float) -> str:
        canvas = [[" " for _ in range(self.width)] for _ in range(self.height)]
        styles = [["" for _ in range(self.width)] for _ in range(self.height)]
        self._paint_rain(canvas, styles, frame, progress)

        if progress < 0.94:
            rotation = self._rotation_for(progress)
            globe_width, globe_height, gx, gy = self._globe_geometry(progress, rotation)
            globe = _render_globe(globe_width, globe_height, rotation, frame)
            _blit(canvas, styles, globe, gx, gy)
            if progress > 0.50:
                self._paint_rain(canvas, styles, frame, progress, foreground=True)
            if self.location is not None and progress > 0.50:
                _paint_location_lock(canvas, styles, self.location, rotation, gx, gy, globe_width, globe_height, frame)
        else:
            glasses = _sunglasses_frame(frame)
            gx = max(0, self.width // 2 - len(glasses[0]) // 2)
            gy = max(1, self.height // 2 - len(glasses) // 2)
            _blit(canvas, styles, glasses, gx, gy)

        self._paint_caption(canvas, styles, progress)
        return _canvas_to_ansi(canvas, styles)

    def _rotation_for(self, progress: float) -> float:
        free = progress * math.tau * 1.18
        if self.location is None or progress < 0.42:
            return free
        target = math.radians(self.location.longitude)
        lock = _smoothstep((progress - 0.42) / 0.28)
        return _lerp_angle(free, target, lock)

    def _globe_geometry(self, progress: float, rotation: float) -> tuple[int, int, int, int]:
        zoom = 1.0 + 1.85 * _smoothstep((progress - 0.54) / 0.36)
        globe_width = max(BASE_GLOBE_WIDTH, int(round(BASE_GLOBE_WIDTH * zoom)))
        globe_height = max(BASE_GLOBE_HEIGHT, int(round(BASE_GLOBE_HEIGHT * zoom)))
        base_left = self.width // 2 - globe_width // 2
        base_top = self.height // 2 - globe_height // 2
        if self.location is None:
            return globe_width, globe_height, base_left, base_top

        projected = project_location(
            self.location.latitude,
            self.location.longitude,
            rotation,
            globe_width,
            globe_height,
        )
        if projected is None:
            return globe_width, globe_height, base_left, base_top

        focus = _smoothstep((progress - 0.58) / 0.34)
        px, py = projected
        target_left = self.width // 2 - px
        target_top = self.height // 2 - py
        left = int(round(base_left + (target_left - base_left) * focus))
        top = int(round(base_top + (target_top - base_top) * focus))
        return globe_width, globe_height, left, top

    def _paint_rain(
        self,
        canvas: list[list[str]],
        styles: list[list[str]],
        frame: int,
        progress: float,
        *,
        foreground: bool = False,
    ) -> None:
        density = 0.04 + min(0.36, _smoothstep(progress / 0.78) * 0.42)
        center_pull = _smoothstep((progress - 0.78) / 0.16)
        if foreground:
            density = 0.08 + 0.22 * _smoothstep((progress - 0.50) / 0.30)
            center_pull = _smoothstep((progress - 0.82) / 0.12)
        rng = random.Random(frame // 2)
        for x in range(self.width):
            speed = 1 + (x % 5)
            head = (frame * speed + x * 7 + (3 if foreground else 0)) % (self.height + 14) - 7
            tail_len = 5 if foreground else 7
            for tail in range(tail_len):
                y = head - tail
                if not 0 <= y < self.height:
                    continue
                if rng.random() > density:
                    continue
                draw_x = int(round(x + (self.width // 2 - x) * center_pull * (tail / 10)))
                if not 0 <= draw_x < self.width:
                    continue
                canvas[y][draw_x] = random.choice(rain_mod.CHARS)
                if foreground:
                    styles[y][draw_x] = _WHITE if tail == 0 and progress > 0.78 else _BRIGHT_GREEN
                else:
                    styles[y][draw_x] = _BRIGHT_GREEN if tail == 0 else (_GREEN if tail < 3 else _DIM_GREEN)

    def _paint_caption(self, canvas: list[list[str]], styles: list[list[str]], progress: float) -> None:
        if progress < 0.18:
            caption = "signal acquired"
        elif progress < 0.48:
            caption = "rotating earth"
        elif progress < 0.60:
            caption = "matrix rain overlay"
        elif progress < 0.70:
            caption = f"operator location: {self.location.label or 'resolving'}" if self.location else "operator location: resolving"
        elif progress < 0.92:
            caption = "zooming to signal"
        elif progress < 0.95:
            caption = "entering the matrix"
        else:
            caption = "morpheus signal locked"
        _write_text(canvas, styles, self.width // 2 - len(caption) // 2, self.height - 3, caption, _BRIGHT_GREEN)


def _render_globe(width: int, height: int, rotation: float, frame: int) -> list[list[tuple[str, str]]]:
    rx = width / 2 - 2
    ry = height / 2 - 1
    cx = width / 2 - 0.5
    cy = height / 2 - 0.5
    rows: list[list[tuple[str, str]]] = []
    shade_chars = ".,:;ox%#@"
    for y in range(height):
        row: list[tuple[str, str]] = []
        for x in range(width):
            nx = (x - cx) / rx
            ny = (y - cy) / ry
            r2 = nx * nx + ny * ny
            if r2 > 1.0:
                row.append((" ", ""))
                continue
            z = math.sqrt(max(0.0, 1.0 - r2))
            lat = math.asin(-ny)
            lon = math.atan2(nx, z) + rotation
            light = max(0.12, min(1.0, 0.35 + 0.65 * (z * 0.78 - nx * 0.32 - ny * 0.16)))
            land = _is_land(lat, lon)
            clouds = _cloud_mask(lat, lon, frame)
            idx = max(0, min(len(shade_chars) - 1, int(light * (len(shade_chars) - 1))))
            if clouds:
                row.append((shade_chars[min(len(shade_chars) - 1, idx + 1)], _CLOUD))
            elif land:
                row.append((shade_chars[idx], _LAND))
            else:
                row.append((shade_chars[max(1, idx - 1)], _BLUE))
        rows.append(row)
    return rows


def _is_land(lat: float, lon: float) -> bool:
    lat_deg = math.degrees(lat)
    lon_deg = _normalize_lon(math.degrees(lon))
    if any(_point_in_polygon(lon_deg, lat_deg, polygon) for polygon in _LAND_POLYGONS):
        return True
    # A tiny procedural island/noise layer keeps the planet from looking too
    # vector-clean without overriding major coastline accuracy.
    island_noise = (
        0.5
        + 0.5 * math.sin(math.radians(lon_deg * 7.0) + math.sin(math.radians(lat_deg * 3.0)))
        * math.sin(math.radians(lon_deg * 3.5 - lat_deg * 4.0))
    )
    return island_noise > 0.985


def _cloud_mask(lat: float, lon: float, frame: int) -> bool:
    drift = frame * 0.045
    band = abs(math.sin(lat * 4.0 + lon * 1.8 + drift))
    swirl = abs(math.sin(lon * 5.5 - lat * 2.7 - drift * 1.3))
    return band > 0.88 and swirl > 0.52


def _paint_location_lock(
    canvas: list[list[str]],
    styles: list[list[str]],
    location: IntroLocation,
    rotation: float,
    gx: int,
    gy: int,
    globe_width: int,
    globe_height: int,
    frame: int,
) -> None:
    projected = project_location(location.latitude, location.longitude, rotation, globe_width, globe_height)
    if projected is None:
        return
    px, py = projected
    x = gx + px
    y = gy + py
    glyph = "◎" if frame % 2 == 0 else "◉"
    _write_text(canvas, styles, x, y, glyph, _AMBER)
    label = location.label or f"{location.latitude:.1f},{location.longitude:.1f}"
    label = label[: min(34, len(label))]
    _write_text(canvas, styles, max(0, x - len(label) // 2), min(len(canvas) - 1, y + 2), label, _AMBER)


def project_location(latitude: float, longitude: float, rotation: float, width: int, height: int) -> Optional[tuple[int, int]]:
    relative_lon = _angle_delta(math.radians(longitude), rotation)
    if abs(relative_lon) > math.pi / 2:
        return None
    rx = width / 2 - 2
    ry = height / 2 - 1
    cx = width / 2 - 0.5
    cy = height / 2 - 0.5
    lat = math.radians(latitude)
    x = int(round(cx + math.sin(relative_lon) * math.cos(lat) * rx))
    y = int(round(cy - math.sin(lat) * ry))
    return max(0, min(width - 1, x)), max(0, min(height - 1, y))


def _sunglasses_frame(frame: int) -> list[list[tuple[str, str]]]:
    style = _WHITE if frame % 4 in {0, 1} else _CYAN
    art = [
        "        ▄▄▄▄▄       ▄▄▄▄▄        ",
        "     ▄█████████▄ ▄█████████▄     ",
        "    ████████████ ████████████    ",
        "    ███████████▀ ▀███████████    ",
        "      ▀█████▀       ▀█████▀      ",
        "          ▀▀▄     ▄▀▀          ",
    ]
    return [[(ch, style if ch != " " else "") for ch in line] for line in art]


def _blit(
    canvas: list[list[str]],
    styles: list[list[str]],
    art: list[list[tuple[str, str]]],
    left: int,
    top: int,
) -> None:
    for y, row in enumerate(art):
        for x, (ch, style) in enumerate(row):
            if ch == " ":
                continue
            cy = top + y
            cx = left + x
            if 0 <= cy < len(canvas) and 0 <= cx < len(canvas[cy]):
                canvas[cy][cx] = ch
                styles[cy][cx] = style


def _write_text(canvas: list[list[str]], styles: list[list[str]], x: int, y: int, text: str, style: str) -> None:
    if y < 0 or y >= len(canvas):
        return
    for offset, ch in enumerate(text):
        pos = x + offset
        if 0 <= pos < len(canvas[y]):
            canvas[y][pos] = ch
            styles[y][pos] = style


def _canvas_to_ansi(canvas: list[list[str]], styles: list[list[str]]) -> str:
    lines: list[str] = []
    for y, row in enumerate(canvas):
        current = ""
        line = []
        for x, ch in enumerate(row):
            style = styles[y][x]
            if style != current:
                line.append(style or _STYLE_RESET)
                current = style
            line.append(ch)
        if current:
            line.append(_STYLE_RESET)
        lines.append("".join(line).rstrip())
    return "\n".join(lines)


def _angle_delta(a: float, b: float) -> float:
    return (a - b + math.pi) % math.tau - math.pi


def _normalize_lon(lon: float) -> float:
    return (lon + 180) % 360 - 180


def _point_in_polygon(x: float, y: float, polygon: tuple[tuple[float, float], ...]) -> bool:
    inside = False
    j = len(polygon) - 1
    for i, (xi, yi) in enumerate(polygon):
        xj, yj = polygon[j]
        if (yi > y) != (yj > y):
            x_at_y = (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi
            if x < x_at_y:
                inside = not inside
        j = i
    return inside


def _lerp_angle(a: float, b: float, amount: float) -> float:
    return a + _angle_delta(b, a) * amount


def _smoothstep(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return value * value * (3 - 2 * value)


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _env_is_true(value: Optional[str]) -> bool:
    return value is not None and str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _env_is_false(value: Optional[str]) -> bool:
    return value is not None and str(value).strip().lower() in {"0", "false", "no", "off", "disabled"}
