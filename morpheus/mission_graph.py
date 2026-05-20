"""Helpers for resolving and inspecting the v0.7 mission graph."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from morpheus import db


@dataclass
class ResolvedMission:
    mission_id: str
    memory: db.MissionMemory
    live: list[db.Mission]


def resolve(ref: str) -> Optional[ResolvedMission]:
    """Resolve a mission id, mission id prefix, tab id, or tab id prefix."""
    ref = ref.strip()
    if not ref:
        return None

    live = db.all_missions()
    live_matches = [
        m for m in live
        if m.tab_id == ref
        or m.tab_id.startswith(ref)
        or m.mission_id == ref
        or m.mission_id.startswith(ref)
    ]
    if live_matches:
        mission_id = live_matches[0].mission_id
        memory = db.get_memory(mission_id)
        if memory:
            return ResolvedMission(
                mission_id=mission_id,
                memory=memory,
                live=[m for m in live if m.mission_id == mission_id],
            )

    memories = db.all_memory(include_archived=True)
    memory_matches = [
        m for m in memories
        if m.mission_id == ref or m.mission_id.startswith(ref)
    ]
    if not memory_matches:
        return None
    memory = memory_matches[0]
    return ResolvedMission(
        mission_id=memory.mission_id,
        memory=memory,
        live=[m for m in live if m.mission_id == memory.mission_id],
    )


def short_id(mission_id: str) -> str:
    """A readable mission id prefix for terminal tables."""
    parts = mission_id.split("_")
    if len(parts) >= 3:
        return f"{parts[1][-6:]}-{parts[2][:4]}"
    return mission_id[:12]


def graph_health() -> dict[str, object]:
    """Return simple health facts for `morpheus graph status`."""
    live = db.all_missions()
    memories = db.all_memory(include_archived=True)
    memory_ids = {m.mission_id for m in memories}
    live_without_memory = [
        m for m in live
        if not m.mission_id or m.mission_id not in memory_ids
    ]
    active = [m for m in memories if m.archived_at is None]
    active_without_live = [
        m for m in active
        if not any(l.mission_id == m.mission_id for l in live)
    ]
    return {
        "counts": db.graph_counts(),
        "live_without_memory": live_without_memory,
        "active_without_live": active_without_live,
    }
