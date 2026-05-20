"""PRD-backed run helpers for coordinator-led agent work."""

from __future__ import annotations

import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from morpheus import db

RUNS_DIR = Path.home() / ".morpheus" / "runs"

PRD_NAME_HINTS = (
    "prd",
    "requirements",
    "spec",
    "plan",
    "roadmap",
)
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
}


@dataclass
class PRDCandidate:
    path: Path
    label: str


@dataclass
class PRDRun:
    parent_id: str
    title: str
    prd_path: Path
    status_path: Path
    prompt_path: Path


def find_prds(root: Path | str, limit: int = 30) -> list[PRDCandidate]:
    base = Path(root).expanduser().resolve()
    if not base.exists():
        return []

    candidates: list[Path] = []
    for path in _walk_files(base):
        if path.suffix.lower() not in {".md", ".markdown", ".txt"}:
            continue
        rel = path.relative_to(base)
        searchable = str(rel).lower()
        if any(hint in searchable for hint in PRD_NAME_HINTS):
            candidates.append(path)

    candidates.sort(key=lambda p: (_prd_score(base, p), str(p.relative_to(base)).lower()))
    return [
        PRDCandidate(path=path, label=str(path.relative_to(base)))
        for path in candidates[:limit]
    ]


def create_prd_run(prd_path: Path | str, title: Optional[str] = None) -> PRDRun:
    path = Path(prd_path).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"PRD not found: {path}")

    now = time.time()
    parent_id = db.new_mission_id(now)
    run_title = title or title_from_prd(path)
    run_dir = RUNS_DIR / parent_id
    run_dir.mkdir(parents=True, exist_ok=True)
    status_path = run_dir / "status.md"
    prompt_path = run_dir / "coordinator_prompt.md"

    run = PRDRun(
        parent_id=parent_id,
        title=run_title,
        prd_path=path,
        status_path=status_path,
        prompt_path=prompt_path,
    )

    db.upsert_memory(
        db.MissionMemory(
            mission_id=parent_id,
            title=run_title,
            why=f"Implement PRD run from {path.name}.",
            done_definition="PRD acceptance criteria are satisfied, verified, and recorded in Morpheus.",
            acceptance_criteria="Use the PRD as source of truth; record proof artifacts before declaring done.",
            current_plan="Coordinator owns the PRD, keeps run status in Morpheus, and manually splits work into safe child sessions.",
            next_step="Read the PRD, identify disjoint worker slices, and propose the first child sessions.",
            phase="planning",
            confidence=1.0,
            source_kind="prd",
            source_ref=str(path),
            topic="prd-run",
            created_at=now,
            updated_at=now,
        )
    )
    db.add_artifact(
        parent_id,
        kind="prd",
        path_or_url=str(path),
        status="source",
        summary=f"Source PRD for {run_title}",
    )
    db.add_event(
        parent_id,
        kind="run_created",
        actor="morpheus",
        summary=f"PRD run created from {path.name}",
        source_ref=str(path),
        metadata={"status_path": str(status_path), "prompt_path": str(prompt_path)},
    )
    write_status_file(run)
    write_coordinator_prompt(run)
    return run


def title_from_prd(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip() or path.stem
    except OSError:
        pass
    return path.stem.replace("_", " ").replace("-", " ").strip() or path.name


def coordinator_command(base_command: str, run: PRDRun) -> str:
    cmd = (base_command or "codex").strip()
    prompt = (
        f"You are the coordinator for Morpheus PRD run {run.parent_id}. "
        f"Read {run.prompt_path}, use {run.prd_path} as source of truth, "
        f"and keep status in Morpheus rather than rewriting the PRD."
    )
    return f"{cmd} {shlex.quote(prompt)}"


def attach_coordinator(run: PRDRun, mission: db.Mission) -> None:
    db.add_edge(
        run.parent_id,
        mission.mission_id,
        relation="coordinator",
        reason="Coordinator session for PRD run",
    )
    db.add_event(
        run.parent_id,
        kind="coordinator_spawned",
        actor="morpheus",
        summary=f"Coordinator spawned: {mission.goal or mission.tab_id}",
        source_ref=f"tab:{mission.tab_id}",
        metadata={"child_mission_id": mission.mission_id},
    )
    db.add_event(
        mission.mission_id,
        kind="assigned",
        actor="morpheus",
        summary=f"Assigned as coordinator for {run.title}",
        source_ref=str(run.prd_path),
        metadata={"parent_mission_id": run.parent_id, "role": "coordinator"},
    )
    write_status_file(run, coordinator=mission)


def write_status_file(run: PRDRun, coordinator: Optional[db.Mission] = None) -> None:
    run.status_path.parent.mkdir(parents=True, exist_ok=True)
    coordinator_line = "unset"
    if coordinator is not None:
        coordinator_line = f"{coordinator.goal or coordinator.tab_id} ({coordinator.mission_id})"
    run.status_path.write_text(
        "\n".join(
            [
                f"# {run.title}",
                "",
                f"- parent mission: `{run.parent_id}`",
                f"- source PRD: `{run.prd_path}`",
                f"- coordinator: {coordinator_line}",
                "- mode: coordinator-only",
                "- next: review PRD and propose manual worker slices",
                "",
                "Morpheus owns run status in the mission graph. Do not rewrite the PRD as a status log.",
                "",
            ]
        ),
        encoding="utf-8",
    )


def write_coordinator_prompt(run: PRDRun) -> None:
    run.prompt_path.parent.mkdir(parents=True, exist_ok=True)
    run.prompt_path.write_text(
        "\n".join(
            [
                f"# Coordinator Prompt: {run.title}",
                "",
                f"Parent mission: `{run.parent_id}`",
                f"Source PRD: `{run.prd_path}`",
                f"Status file: `{run.status_path}`",
                "",
                "You are the coordinator for this PRD run.",
                "",
                "Responsibilities:",
                "- Read the PRD and summarize the objective, acceptance criteria, risks, and proof needed.",
                "- Keep durable status in Morpheus mission events/artifacts, not by rewriting the PRD.",
                "- Propose child worker sessions only when their file/path ownership is disjoint.",
                "- Before asking for worker fan-out, name the write scope and verification for each worker.",
                "- Record decisions, blockers, and proof with `morpheus graph event` and `morpheus graph artifact`.",
                "",
                "Do not auto-spawn parallel workers yet. v0.8 starts coordinator-only unless the user explicitly asks for child sessions.",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _walk_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file():
            yield path


def _prd_score(root: Path, path: Path) -> tuple[int, int]:
    rel = path.relative_to(root)
    name = path.name.lower()
    if name == "prd.md":
        rank = 0
    elif name.startswith("prd"):
        rank = 1
    elif "prd" in name:
        rank = 2
    else:
        rank = 3
    return rank, len(rel.parts)
