"""PRD-backed run helpers for coordinator-led agent work."""

from __future__ import annotations

import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from morpheus import db, tenant as tenant_mod

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
    "Library",
    "Applications",
    "Downloads",
    "Desktop",
    "Documents",
}
PROJECT_MARKERS = {
    ".git",
    ".jj",
    ".hg",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "Gemfile",
}
MAX_SCAN_ENTRIES = 2500
MAX_SCAN_SECONDS = 0.25


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
    tenant_id: str = ""
    project_root: str = ""


def find_prds(
    root: Path | str,
    limit: Optional[int] = None,
    max_entries: int = MAX_SCAN_ENTRIES,
    max_seconds: float = MAX_SCAN_SECONDS,
) -> list[PRDCandidate]:
    base = Path(root).expanduser().resolve()
    if not base.exists():
        return []
    if _is_broad_root(base):
        return []

    candidates: list[Path] = []
    deadline = time.monotonic() + max_seconds if max_seconds else None
    for path in _walk_files(base, max_entries=max_entries, deadline=deadline):
        if path.suffix.lower() not in {".md", ".markdown"}:
            continue
        candidates.append(path)

    candidates.sort(key=lambda p: (_prd_score(base, p), str(p.relative_to(base)).lower()))
    selected = candidates if limit is None else candidates[:limit]
    return [
        PRDCandidate(path=path, label=str(path.relative_to(base)))
        for path in selected
    ]


def scan_root_for_worktree(root: Path | str, fallback: Optional[Path | str] = None) -> Path:
    """Return a bounded PRD-scan root for a selected tab/worktree.

    iTerm/Codex sessions can report `$HOME` as their cwd. Scanning that from the
    TUI would block the cockpit, so broad roots fall back to the dashboard cwd.
    """
    base = Path(root).expanduser().resolve()
    if base.is_file():
        base = base.parent
    candidate = _nearest_project_root(base) or base
    if not _is_broad_root(candidate):
        return candidate

    fallback_base = Path(fallback or Path.cwd()).expanduser().resolve()
    if fallback_base.is_file():
        fallback_base = fallback_base.parent
    fallback_candidate = _nearest_project_root(fallback_base) or fallback_base
    if _is_broad_root(fallback_candidate):
        return fallback_base
    return fallback_candidate


def create_prd_run(
    prd_path: Path | str,
    title: Optional[str] = None,
    *,
    project: Optional[db.ProjectTenant] = None,
) -> PRDRun:
    path = Path(prd_path).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"PRD not found: {path}")

    now = time.time()
    parent_id = db.new_mission_id(now)
    run_title = title or title_from_prd(path)
    project = project or tenant_mod.ensure_project_tenant(path)
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
        tenant_id=project.tenant_id,
        project_root=project.root_path,
    )

    db.upsert_memory(
        db.MissionMemory(
            mission_id=parent_id,
            tenant_id=project.tenant_id,
            project_root=project.root_path,
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
    update_status_from_graph(parent_id)
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
    update_status_from_graph(run.parent_id)


def attach_worker(
    run: PRDRun,
    mission: db.Mission,
    *,
    scope: str = "",
    verification: str = "",
) -> None:
    db.add_edge(
        run.parent_id,
        mission.mission_id,
        relation="worker",
        reason=scope or "Worker session for PRD run",
    )
    db.add_event(
        run.parent_id,
        kind="worker_spawned",
        actor="morpheus",
        summary=f"Worker spawned: {mission.goal or mission.tab_id}",
        source_ref=f"tab:{mission.tab_id}",
        metadata={
            "child_mission_id": mission.mission_id,
            "scope": scope,
            "verification": verification,
        },
    )
    db.add_event(
        mission.mission_id,
        kind="assigned",
        actor="morpheus",
        summary=f"Assigned as worker for {run.title}",
        source_ref=str(run.prd_path),
        metadata={
            "parent_mission_id": run.parent_id,
            "role": "worker",
            "scope": scope,
            "verification": verification,
        },
    )
    update_status_from_graph(run.parent_id)


def parent_for_child(mission_id: str) -> Optional[str]:
    for edge in db.edges_to_id(mission_id, limit=20):
        if edge.relation in {"coordinator", "worker"}:
            return edge.from_id
    return None


def prd_parent_for_mission(mission_id: str) -> Optional[str]:
    """Return the PRD parent for a parent or child mission id."""
    memory = db.get_memory(mission_id)
    if memory and (memory.topic == "prd-run" or memory.source_kind == "prd"):
        return mission_id
    return parent_for_child(mission_id)


def update_status_for_mission(
    mission_id: str,
    *,
    record_event: bool = False,
) -> Optional[PRDRun]:
    """Refresh the containing PRD run status file for a parent or child."""
    parent_id = prd_parent_for_mission(mission_id)
    if not parent_id:
        return None
    update_status_from_graph(parent_id, record_event=record_event)
    return run_from_parent(parent_id)


def run_from_parent(parent_id: str) -> PRDRun:
    memory = db.get_memory(parent_id)
    if memory is None:
        raise ValueError(f"PRD parent mission not found: {parent_id}")
    prd_path = Path(memory.source_ref).expanduser() if memory.source_ref else Path("")
    run_dir = RUNS_DIR / parent_id
    return PRDRun(
        parent_id=parent_id,
        title=memory.title or parent_id,
        prd_path=prd_path,
        status_path=run_dir / "status.md",
        prompt_path=run_dir / "coordinator_prompt.md",
        tenant_id=memory.tenant_id,
        project_root=memory.project_root,
    )


def project_for_run(run: PRDRun) -> db.ProjectTenant:
    """Return the owning Morpheus project for a PRD run.

    A PRD file can live inside a nested repo, but the run belongs to the
    cockpit/command project that launched it. Fall back to the PRD path for
    older rows that predate explicit PRD-run tenancy.
    """
    tenant_id = getattr(run, "tenant_id", "")
    project_root = getattr(run, "project_root", "")
    prd_path = getattr(run, "prd_path")
    if tenant_id:
        project = db.get_project_tenant(tenant_id)
        if project is not None:
            return project
    if project_root:
        return tenant_mod.ensure_project_tenant(project_root)
    return tenant_mod.ensure_project_tenant(prd_path)


def worker_command(
    base_command: str,
    run: PRDRun,
    *,
    worker_goal: str,
    scope: str = "",
    verification: str = "",
) -> str:
    cmd = (base_command or "codex").strip()
    prompt = (
        f"You are a worker for Morpheus PRD run {run.parent_id}. "
        f"Goal: {worker_goal}. "
        f"Read {run.prd_path} as source of truth and inspect {run.status_path} for current run status. "
        f"Coordinate through Morpheus mission events/artifacts. "
        f"Owned scope: {scope or 'ask the coordinator/user to confirm write scope before editing'}. "
        f"Verification required: {verification or 'record proof before declaring done'}. "
        f"Do not revert unrelated edits or other workers' changes."
    )
    return f"{cmd} {shlex.quote(prompt)}"


def update_status_from_graph(
    parent_id: str,
    *,
    record_event: bool = False,
) -> Path:
    """Render a PRD run status file from mission graph state.

    The status file is a derived artifact. The mission graph remains the source
    of truth for coordinators, workers, events, and proof.
    """
    memory = db.get_memory(parent_id)
    if memory is None:
        raise ValueError(f"PRD parent mission not found: {parent_id}")
    if memory.source_kind != "prd" and memory.topic != "prd-run":
        raise ValueError(f"mission is not a PRD run parent: {parent_id}")

    run = run_from_parent(parent_id)
    if record_event:
        db.add_event(
            parent_id,
            kind="status_refreshed",
            actor="morpheus",
            summary="Rendered PRD run status from mission graph",
            source_ref=str(run.status_path),
        )

    run.status_path.parent.mkdir(parents=True, exist_ok=True)
    run.status_path.write_text(render_status_from_graph(parent_id), encoding="utf-8")
    return run.status_path


def render_status_from_graph(parent_id: str, *, generated_at: Optional[float] = None) -> str:
    memory = db.get_memory(parent_id)
    if memory is None:
        raise ValueError(f"PRD parent mission not found: {parent_id}")
    run = run_from_parent(parent_id)
    now = generated_at if generated_at is not None else time.time()
    children = _run_children(parent_id)
    child_ids = [child.mission_id for child in children]
    coordinators = [child for child in children if child.role == "coordinator"]
    workers = [child for child in children if child.role == "worker"]
    events = _recent_run_events([parent_id, *child_ids], limit=12)
    artifacts = _recent_run_artifacts([parent_id, *child_ids], limit=12)

    lines = [
        f"# {memory.title or run.title}",
        "",
        f"- parent mission: `{parent_id}`",
        f"- source PRD: `{run.prd_path}`",
        f"- phase: `{memory.phase or 'unknown'}`",
        "- mode: `graph-synced`",
        f"- generated: `{_format_ts(now)}`",
        "",
        "Morpheus owns run status in the mission graph. This file is rendered from graph state; do not hand-edit it as a status log.",
        "",
        "## Parent Mission",
    ]
    _append_field(lines, "why", memory.why)
    _append_field(lines, "done", memory.done_definition)
    _append_field(lines, "criteria", memory.acceptance_criteria)
    _append_field(lines, "plan", memory.current_plan)
    _append_field(lines, "next", memory.next_step)
    _append_field(lines, "blocked", memory.blocked_on)

    lines.extend(["", "## Coordinator"])
    if coordinators:
        for child in coordinators:
            _append_child(lines, child)
    else:
        lines.append("- unset")

    lines.extend(["", "## Workers"])
    if workers:
        for child in workers:
            _append_child(lines, child)
    else:
        lines.append("- none")

    lines.extend(["", "## Recent Events"])
    if events:
        for event in events:
            source = f" [{event.source_ref}]" if event.source_ref else ""
            lines.append(
                f"- {_format_ts(event.ts)} `{event.mission_id}` {event.kind}/{event.actor}: "
                f"{_one_line(event.summary)}{source}"
            )
    else:
        lines.append("- none")

    lines.extend(["", "## Proof And Artifacts"])
    if artifacts:
        for artifact in artifacts:
            summary = f" - {_one_line(artifact.summary)}" if artifact.summary else ""
            lines.append(
                f"- {_format_ts(artifact.created_at)} `{artifact.mission_id}` "
                f"{artifact.status} {artifact.kind}: `{artifact.path_or_url}`{summary}"
            )
    else:
        lines.append("- none")

    lines.append("")
    return "\n".join(lines)


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


def _walk_files(
    root: Path,
    *,
    max_entries: int = MAX_SCAN_ENTRIES,
    deadline: Optional[float] = None,
) -> Iterable[Path]:
    stack = [root]
    visited = 0
    while stack:
        if deadline is not None and time.monotonic() > deadline:
            return
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        dirs: list[Path] = []
        for entry in entries:
            visited += 1
            if visited > max_entries:
                return
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if entry.name in SKIP_DIRS or entry.name.startswith("."):
                        continue
                    dirs.append(entry)
                elif entry.is_file():
                    yield entry
            except OSError:
                continue
        stack.extend(reversed(dirs))


def _nearest_project_root(path: Path) -> Optional[Path]:
    current = path.resolve()
    if current.is_file():
        current = current.parent
    for candidate in [current, *current.parents]:
        if any((candidate / marker).exists() for marker in PROJECT_MARKERS):
            return candidate
    return None


def _is_broad_root(path: Path) -> bool:
    base = path.expanduser().resolve()
    home = Path.home().resolve()
    broad = {Path("/").resolve(), home, home.parent.resolve()}
    return base in broad


def _prd_score(root: Path, path: Path) -> tuple[int, int]:
    rel = path.relative_to(root)
    name = path.name.lower()
    if name == "prd.md":
        rank = 0
    elif name.startswith("prd"):
        rank = 1
    elif "prd" in name:
        rank = 2
    elif any(hint in str(rel).lower() for hint in PRD_NAME_HINTS):
        rank = 3
    else:
        rank = 4
    return rank, len(rel.parts)


@dataclass
class _RunChild:
    role: str
    mission_id: str
    edge: db.MissionEdge
    mission: Optional[db.Mission]
    memory: Optional[db.MissionMemory]
    scope: str = ""
    verification: str = ""


def _run_children(parent_id: str) -> list[_RunChild]:
    live_by_mission = {m.mission_id: m for m in db.all_missions() if m.mission_id}
    edges = [
        edge for edge in db.edges_from_id(parent_id, limit=200)
        if edge.relation in {"coordinator", "worker"}
    ]
    edges.sort(key=lambda edge: (0 if edge.relation == "coordinator" else 1, edge.created_at))
    children: list[_RunChild] = []
    for edge in edges:
        metadata = _assignment_metadata(edge.to_id)
        children.append(
            _RunChild(
                role=edge.relation,
                mission_id=edge.to_id,
                edge=edge,
                mission=live_by_mission.get(edge.to_id),
                memory=db.get_memory(edge.to_id),
                scope=str(metadata.get("scope") or (edge.reason if edge.relation == "worker" else "")),
                verification=str(metadata.get("verification") or ""),
            )
        )
    return children


def _assignment_metadata(mission_id: str) -> dict[str, object]:
    for event in db.recent_events(mission_id, limit=25):
        if event.kind == "assigned":
            return event.metadata
    return {}


def _append_child(lines: list[str], child: _RunChild) -> None:
    title = ""
    if child.memory and child.memory.title:
        title = child.memory.title
    elif child.mission and child.mission.goal:
        title = child.mission.goal
    else:
        title = child.mission_id

    if child.mission is not None:
        live = f"live tab `{child.mission.tab_id}` state `{child.mission.state}`"
    elif child.memory and child.memory.archived_at:
        live = f"archived `{_format_ts(child.memory.archived_at)}`"
    else:
        live = "no live tab attachment"

    lines.append(f"- {child.role}: {_one_line(title)} (`{child.mission_id}`) {live}")
    if child.scope:
        lines.append(f"  - scope: {_one_line(child.scope)}")
    if child.verification:
        lines.append(f"  - verification: {_one_line(child.verification)}")
    if child.memory:
        _append_field(lines, "phase", child.memory.phase, indent="  ")
        _append_field(lines, "next", child.memory.next_step, indent="  ")
        _append_field(lines, "blocked", child.memory.blocked_on, indent="  ")
        claims = _one_line(child.memory.claimed_paths)
        if claims and claims != "[]":
            lines.append(f"  - claimed paths: {claims}")


def _append_field(lines: list[str], label: str, value: str, *, indent: str = "") -> None:
    cleaned = _one_line(value)
    if cleaned:
        lines.append(f"{indent}- {label}: {cleaned}")


def _recent_run_events(mission_ids: list[str], *, limit: int) -> list[db.MissionEvent]:
    events: list[db.MissionEvent] = []
    for mission_id in mission_ids:
        events.extend(db.recent_events(mission_id, limit=limit))
    events.sort(key=lambda event: event.ts, reverse=True)
    return events[:limit]


def _recent_run_artifacts(mission_ids: list[str], *, limit: int) -> list[db.MissionArtifact]:
    artifacts: list[db.MissionArtifact] = []
    for mission_id in mission_ids:
        artifacts.extend(db.artifacts_for_mission(mission_id, limit=limit))
    artifacts.sort(key=lambda artifact: artifact.created_at, reverse=True)
    return artifacts[:limit]


def _one_line(value: object) -> str:
    return " ".join(str(value or "").split())


def _format_ts(ts: float) -> str:
    if not ts:
        return "unknown"
    return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(ts))
