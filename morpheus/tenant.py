"""Project tenant resolution for cwd-scoped Morpheus cockpits."""

from __future__ import annotations

import hashlib
import shlex
import subprocess
from pathlib import Path
from typing import Optional

from morpheus import db

PROJECT_MARKERS = (
    ".git",
    ".jj",
    ".hg",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "Gemfile",
)


def resolve_project_root(path: Path | str | None = None) -> tuple[Path, str]:
    """Resolve a durable project root for a cwd or file path."""
    base = Path(path or Path.cwd()).expanduser()
    try:
        base = base.resolve()
    except OSError:
        base = base.absolute()
    if base.is_file() or (not base.exists() and base.suffix):
        base = base.parent

    git_root = _git_toplevel(base)
    if git_root is not None:
        return git_root, "git"

    marker_root = _nearest_marker_root(base)
    if marker_root is not None:
        return marker_root, "marker"

    return base, "cwd"


def resolve_project_tenant(path: Path | str | None = None) -> db.ProjectTenant:
    root, root_kind = resolve_project_root(path)
    return db.ProjectTenant(
        tenant_id=tenant_id_for_root(root),
        name=_project_name(root),
        root_path=str(root),
        root_kind=root_kind,
    )


def ensure_project_tenant(path: Path | str | None = None) -> db.ProjectTenant:
    tenant = resolve_project_tenant(path)
    return db.upsert_project_tenant(tenant)


def apply_to_mission(mission: db.Mission, path: Path | str | None) -> db.ProjectTenant:
    tenant = ensure_project_tenant(path)
    mission.tenant_id = tenant.tenant_id
    mission.project_root = tenant.root_path
    return tenant


def command_in_project(command: str, project_root: str) -> str:
    if not project_root:
        return command
    return f"cd {shlex.quote(project_root)} && {command}"


def backfill_known_tenants() -> int:
    """Assign tenants to older rows when a known path already exists."""
    changed = 0
    for mission in db.all_missions():
        if mission.tenant_id or not mission.linked_worktree:
            continue
        project = ensure_project_tenant(mission.linked_worktree)
        if db.update_mission_details(
            mission.tab_id,
            goal=mission.goal,
            linked_pr=mission.linked_pr,
            linked_worktree=mission.linked_worktree,
            tenant_id=project.tenant_id,
            project_root=project.root_path,
        ):
            changed += 1

    for memory in db.all_memory(include_archived=True):
        if memory.tenant_id or not memory.source_ref:
            continue
        path = Path(memory.source_ref).expanduser()
        if not path.is_absolute() or not path.exists():
            continue
        project = ensure_project_tenant(path)
        memory.tenant_id = project.tenant_id
        memory.project_root = project.root_path
        db.upsert_memory(memory)
        changed += 1
    return changed


def tenant_id_for_root(root: Path | str) -> str:
    normalized = str(Path(root).expanduser().resolve())
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"p_{digest}"


def _git_toplevel(path: Path) -> Optional[Path]:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=0.5,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    if not output:
        return None
    try:
        return Path(output).expanduser().resolve()
    except OSError:
        return Path(output).expanduser().absolute()


def _nearest_marker_root(path: Path) -> Optional[Path]:
    current = path
    for candidate in [current, *current.parents]:
        if any((candidate / marker).exists() for marker in PROJECT_MARKERS):
            return candidate
    return None


def _project_name(root: Path) -> str:
    if root.name:
        return root.name
    return str(root)
