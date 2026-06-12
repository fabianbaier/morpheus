"""Recurring prompt loops routed through the Morpheus mission graph.

Morpheus stores loop intent and routing. A cron/launchd entry can call
`morpheus loops run-due` every minute; this module decides which prompts are
due, runs them, captures output, and publishes summaries back into ticker notes
and mission graph events.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Optional

from morpheus import context as ctx_mod
from morpheus import db, ledger

DEFAULT_COMMAND = "codex exec --skip-git-repo-check"
DEFAULT_TIMEOUT_SECONDS = 20 * 60
MIN_INTERVAL_SECONDS = 60

DURATION_RE = re.compile(
    r"^\s*(?P<num>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>s|sec|secs|second|seconds|m|min|mins|minute|minutes|"
    r"h|hr|hrs|hour|hours|d|day|days)?\s*$",
    re.IGNORECASE,
)
CONCLUSION_RE = re.compile(
    r"\b(answer|summary|headline|bottom line|recommend|done|fixed|shipped|implemented|next step)\b",
    re.IGNORECASE,
)
NOISE_PREFIXES = (
    "reading additional input from stdin",
    "openai codex",
    "searching the web",
    "thinking",
    "working",
    "web search:",
    "sources:",
    "source:",
    "use /skills to list",
    "› use /skills to list",
    "> use /skills to list",
    "$ ",
    "started:",
    "cwd:",
    "[loop ",
)
CODEX_CHROME_PREFIXES = (
    "workdir:",
    "model:",
    "provider:",
    "approval:",
    "sandbox:",
    "reasoning effort:",
    "reasoning summaries:",
    "session id:",
    "token usage:",
)
CODEX_ROLE_MARKERS = {"assistant", "developer", "system", "user"}
CODEX_EXEC_RE = re.compile(r"^codex\s+exec(?=\s|$)")


def parse_interval(value: str) -> float:
    """Parse human loop intervals like `15m`, `2h`, `daily`, or bare minutes."""
    normalized = value.strip().lower()
    aliases = {
        "hourly": 3600,
        "daily": 86400,
        "weekly": 7 * 86400,
    }
    if normalized in aliases:
        return float(aliases[normalized])

    match = DURATION_RE.match(value)
    if not match:
        raise ValueError("interval must look like 15m, 2h, daily, or weekly")
    amount = float(match.group("num"))
    unit = (match.group("unit") or "m").lower()
    multiplier = 60
    if unit.startswith("s"):
        multiplier = 1
    elif unit.startswith("h"):
        multiplier = 3600
    elif unit.startswith("d"):
        multiplier = 86400
    seconds = amount * multiplier
    if seconds < MIN_INTERVAL_SECONDS:
        raise ValueError("loop interval must be at least 60 seconds")
    return seconds


def format_interval(seconds: float) -> str:
    seconds_i = int(seconds)
    if seconds_i % 86400 == 0:
        days = seconds_i // 86400
        return f"{days}d"
    if seconds_i % 3600 == 0:
        hours = seconds_i // 3600
        return f"{hours}h"
    if seconds_i % 60 == 0:
        minutes = seconds_i // 60
        return f"{minutes}m"
    return f"{seconds_i}s"


def format_due(ts: float, now: Optional[float] = None) -> str:
    now_ts = now or time.time()
    delta = int(ts - now_ts)
    if delta <= 0:
        return "due"
    return f"in {format_interval(delta)}"


def build_command(command: str, prompt: str) -> str:
    """Build a shell command. `{prompt}` opt-in lets users place the prompt."""
    command = normalize_command(command)
    quoted = shlex.quote(prompt)
    if "{prompt}" in command:
        return command.replace("{prompt}", quoted)
    return f"{command} {quoted}"


def normalize_command(command: str) -> str:
    command = command.strip() or DEFAULT_COMMAND
    if CODEX_EXEC_RE.search(command) and "--skip-git-repo-check" not in command:
        return CODEX_EXEC_RE.sub(DEFAULT_COMMAND, command, count=1)
    return command


def summarize_output(stdout: str, stderr: str = "", width: int = 160, prompt: str = "") -> str:
    lines = visible_output_lines(stdout, prompt=prompt) or visible_output_lines(stderr, prompt=prompt)
    candidates: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        score = index
        if CONCLUSION_RE.search(line):
            score += 100
        if line.endswith((".", "!", "?")):
            score += 5
        candidates.append((score, line))
    if not candidates:
        return "loop completed with no visible output" if stdout.strip() else "loop produced no output"
    _score, line = max(candidates, key=lambda item: item[0])
    line = _first_sentence(line)
    return line if len(line) <= width else line[: width - 1] + "…"


def visible_output_lines(output: str, *, prompt: str = "") -> list[str]:
    """Return human-visible result lines from a loop output transcript."""
    prompt_text = _clean_line(prompt)
    assistant_lines = _latest_codex_assistant_lines(output.splitlines(), prompt=prompt_text)
    if assistant_lines:
        return assistant_lines
    lines: list[str] = []
    for raw in output.splitlines():
        line = _clean_line(raw)
        if not line or _is_noise(line, prompt=prompt_text):
            continue
        lines.append(line)
    return lines


def run_due(
    *,
    limit: int = 5,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    now: Optional[float] = None,
    cwd: Optional[Path] = None,
    tenant_id: str = "",
) -> list[db.PromptLoopRun]:
    runs: list[db.PromptLoopRun] = []
    for loop in db.due_loops(now=now, limit=limit, tenant_id=tenant_id):
        runs.append(run_loop(loop, timeout=timeout, cwd=cwd))
    return runs


def run_loop(
    loop: db.PromptLoop,
    *,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    cwd: Optional[Path] = None,
) -> db.PromptLoopRun:
    started = time.time()
    db.mark_loop_running(
        loop.id,
        started_at=started,
        next_run_at=started + loop.interval_seconds,
    )
    command = build_command(loop.command, loop.prompt)
    run_cwd = cwd or _loop_cwd(loop)
    output_path = _output_path(loop.id, started)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_output_header(output_path, command, run_cwd, started)
    run = db.start_loop_run(
        loop.id,
        started_at=started,
        output_path=str(output_path),
        target_mission_id=loop.target_mission_id,
        target_tab_id=loop.target_tab_id,
    )

    status = "success"
    exit_code: Optional[int] = 0
    try:
        with output_path.open("a", encoding="utf-8") as out:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=str(run_cwd) if run_cwd else None,
                text=True,
                stdout=out,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        exit_code = completed.returncode
        if completed.returncode != 0:
            status = "failed"
        summary = summarize_output(_read_output(output_path), prompt=loop.prompt)
    except subprocess.TimeoutExpired:
        status = "timeout"
        exit_code = None
        summary = f"loop timed out after {timeout}s"
        _append_output_footer(output_path, status=status, exit_code=exit_code, timed_out=True)
    except Exception as exc:
        status = "failed"
        exit_code = None
        summary = str(exc)
        _append_output_line(output_path, f"\n[loop runner error] {exc}\n")
    finished = time.time()
    if status != "timeout":
        _append_output_footer(output_path, status=status, exit_code=exit_code)
    run = db.finish_loop_run(
        run.id,
        finished_at=finished,
        status=status,
        exit_code=exit_code,
        summary=summary,
    )
    run = _persist_resume_metadata(loop, run, command, run_cwd) or run
    db.update_loop_after_run(
        loop.id,
        last_run_at=finished,
        next_run_at=finished + loop.interval_seconds,
        last_run_status=status,
        last_summary=summary,
    )
    publish_run(loop, run)
    try:
        ctx_mod.write_context_file()
        ctx_mod.write_context_json()
    except Exception:
        pass
    ledger.log_action(
        "loop_run",
        tab_id=loop.target_tab_id,
        details={
            "loop_id": loop.id,
            "run_id": run.id,
            "status": status,
            "target_mission_id": loop.target_mission_id,
        },
    )
    return run


def _persist_resume_metadata(
    loop: db.PromptLoop,
    run: db.PromptLoopRun,
    command: str,
    run_cwd: Optional[Path],
) -> Optional[db.PromptLoopRun]:
    agent_kind, resume_ref, resume_command, confidence = db.resume_metadata_from_text(
        command,
        _read_output(Path(run.output_path)),
        linked_worktree=str(run_cwd or loop.project_root or ""),
    )
    if confidence != "exact" or not resume_command:
        return None
    return db.update_loop_run_resume_metadata(
        run.id,
        agent_kind=agent_kind,
        resume_ref=resume_ref,
        resume_command=resume_command,
        resume_confidence=confidence,
    )


def publish_run(loop: db.PromptLoop, run: db.PromptLoopRun) -> None:
    note = f"loop [{loop.name}] {run.summary}"
    db.add_note(
        text=note,
        tab_id=loop.target_tab_id,
        kind="loop",
    )
    # Route into subscribed feeds (per-loop rules with thresholds). Best-effort:
    # a feed problem must never break the loop runner itself.
    try:
        from morpheus import feeds
        feeds.route_loop_run(loop, run)
    except Exception:
        pass
    if not loop.target_mission_id:
        return
    db.add_event(
        loop.target_mission_id,
        kind="loop_output",
        actor="morpheus",
        summary=note,
        source_ref=run.output_path,
        metadata={
            "loop_id": loop.id,
            "run_id": run.id,
            "status": run.status,
            "target_tab_id": loop.target_tab_id,
        },
    )
    db.add_artifact(
        loop.target_mission_id,
        kind="loop-output",
        path_or_url=run.output_path,
        status=run.status,
        summary=note,
    )


def _output_path(loop_id: int, ts: float) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(ts))
    return db.DB_DIR / "loops" / str(loop_id) / f"{stamp}.txt"


def _write_output_header(output_path: Path, command: str, cwd: Optional[Path], started: float) -> None:
    lines = [
        f"$ {command}",
        f"started: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started))}",
    ]
    if cwd:
        lines.append(f"cwd: {cwd}")
    lines.append("")
    output_path.write_text("\n".join(lines), encoding="utf-8")


def _append_output_footer(
    output_path: Path,
    *,
    status: str,
    exit_code: Optional[int],
    timed_out: bool = False,
) -> None:
    code = "timeout" if timed_out else ("none" if exit_code is None else str(exit_code))
    _append_output_line(output_path, f"\n\n[loop {status}; exit={code}]\n")


def _append_output_line(output_path: Path, text: str) -> None:
    with output_path.open("a", encoding="utf-8") as out:
        out.write(text)


def _read_output(output_path: Path) -> str:
    try:
        return output_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _loop_cwd(loop: db.PromptLoop) -> Optional[Path]:
    if not loop.project_root:
        return None
    path = Path(loop.project_root).expanduser()
    return path if path.exists() else None


def _clean_line(value: str) -> str:
    return " ".join(value.strip().split())


def _latest_codex_assistant_lines(raw_lines: list[str], *, prompt: str = "") -> list[str]:
    sections: list[list[str]] = []
    current: Optional[list[str]] = None
    for raw in raw_lines:
        line = _clean_line(raw)
        lowered = line.lower()
        if lowered == "assistant":
            if current:
                sections.append(current)
            current = []
            continue
        if lowered in CODEX_ROLE_MARKERS:
            if current:
                sections.append(current)
            current = None
            continue
        if current is not None and not _is_noise(line, prompt=prompt):
            current.append(line)
    if current:
        sections.append(current)
    return sections[-1] if sections else []


def _is_noise(line: str, *, prompt: str = "") -> bool:
    lowered = line.lower()
    if len(line) < 3:
        return True
    if lowered in CODEX_ROLE_MARKERS:
        return True
    if any(lowered.startswith(prefix) for prefix in NOISE_PREFIXES):
        return True
    if any(lowered.startswith(prefix) for prefix in CODEX_CHROME_PREFIXES):
        return True
    if prompt:
        prompt_lower = prompt.lower()
        if len(line) > 20 and (lowered in prompt_lower or prompt_lower in lowered):
            return True
    if lowered.startswith("searched "):
        return True
    if "http://" in lowered or "https://" in lowered:
        return True
    if set(line) <= {"-", "=", "_", " ", "─"}:
        return True
    return False


def _first_sentence(value: str) -> str:
    match = re.search(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“])", value)
    if not match:
        return value
    return value[: match.start()].strip()
