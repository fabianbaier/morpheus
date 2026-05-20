"""Deterministic 48-hour mission recall readiness checks.

The evaluator is a local proxy for the PRD dogfood test: after a mission has
gone stale, the graph-backed brief should contain enough durable context to
recover purpose, done criteria, recent state, proof, and next action quickly.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Optional, Sequence

from morpheus import db


DEFAULT_STALE_SECONDS = 48 * 60 * 60
DEFAULT_TARGET_SECONDS = 10.0
CHECK_EVENT_KINDS = {"check", "test", "build", "verification", "status"}
PASS_STATUSES = {"pass", "passed", "success", "succeeded", "successful", "ok"}
FAIL_STATUSES = {"fail", "failed", "failure", "error", "errored", "timeout", "timed out"}
PROOF_ARTIFACT_STATUSES = {"pass"}
PROOF_ARTIFACT_KINDS = {"test", "build", "proof", "diff", "pr", "log", "screenshot"}
PASS_RE = re.compile(r"\b(pass(?:ed)?|success(?:ful)?|succeeded|ok|verified)\b")
FAIL_RE = re.compile(
    r"\b(fail(?:ed|ure|ing)?|error(?:ed)?|timeout|timed out)\b"
    r"|\b(?:did\s+not|does\s+not|do\s+not|is\s+not|are\s+not|was\s+not|were\s+not|not|never|no)\s+"
    r"(?:pass(?:ed|ing)?|succeed(?:ed)?|success(?:ful)?|ok|verified)\b"
    r"|\bunverified\b"
)


@dataclass(frozen=True)
class RecallCheck:
    key: str
    label: str
    passed: bool
    detail: str
    source_ref: str = ""
    required: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "passed": self.passed,
            "detail": self.detail,
            "source_ref": self.source_ref,
            "required": self.required,
        }


@dataclass(frozen=True)
class RecallEvaluation:
    mission_id: str
    title: str
    passed: bool
    score: int
    age_seconds: Optional[float]
    age_source: str
    stale_seconds: float
    target_seconds: float
    checks: tuple[RecallCheck, ...]

    @property
    def status(self) -> str:
        return "PASS" if self.passed else "FAIL"

    @property
    def missing_labels(self) -> list[str]:
        return [check.label for check in self.checks if check.required and not check.passed]

    def to_dict(self) -> dict[str, object]:
        return {
            "mission_id": self.mission_id,
            "title": self.title,
            "status": self.status,
            "passed": self.passed,
            "score": self.score,
            "age_seconds": self.age_seconds,
            "age": format_age(self.age_seconds),
            "age_source": self.age_source,
            "stale_seconds": self.stale_seconds,
            "target_seconds": self.target_seconds,
            "missing": self.missing_labels,
            "checks": [check.to_dict() for check in self.checks],
        }

    def event_summary(self) -> str:
        missing = ", ".join(self.missing_labels) if self.missing_labels else "none"
        return (
            f"48-hour recall eval {self.status}: score {self.score}%, "
            f"age {format_age(self.age_seconds)}, target {self.target_seconds:g}s, "
            f"missing: {missing}"
        )


def evaluate_mission(
    memory: db.MissionMemory,
    *,
    live: Sequence[db.Mission] = (),
    events: Sequence[db.MissionEvent] = (),
    artifacts: Sequence[db.MissionArtifact] = (),
    now: Optional[float] = None,
    stale_seconds: float = DEFAULT_STALE_SECONDS,
    target_seconds: float = DEFAULT_TARGET_SECONDS,
) -> RecallEvaluation:
    """Evaluate whether one mission has enough graph context for stale recall."""
    if stale_seconds <= 0:
        raise ValueError("stale_seconds must be positive")
    if target_seconds <= 0:
        raise ValueError("target_seconds must be positive")

    ts = time.time() if now is None else now
    age_seconds, age_source = mission_age(memory, live=live, now=ts)
    source_ref = memory_source_ref(memory)
    checks = [
        _stale_check(age_seconds, stale_seconds, age_source),
        _target_check(target_seconds),
        _text_check("why", "why", memory.why, source_ref),
        _text_check("done_definition", "done definition", memory.done_definition, source_ref),
        _text_check(
            "acceptance_criteria",
            "acceptance criteria",
            memory.acceptance_criteria,
            source_ref,
        ),
        _text_check("next_step", "next step", memory.next_step, source_ref),
        _decision_check(memory, events),
        _check_event_check(events),
        _proof_artifact_check(artifacts),
    ]
    required = [check for check in checks if check.required]
    passed_count = sum(1 for check in required if check.passed)
    score = int(round(100 * passed_count / len(required))) if required else 100
    passed = passed_count == len(required)
    title = _first_nonempty(memory.title, memory.mission_id)
    return RecallEvaluation(
        mission_id=memory.mission_id,
        title=title,
        passed=passed,
        score=score,
        age_seconds=age_seconds,
        age_source=age_source,
        stale_seconds=stale_seconds,
        target_seconds=target_seconds,
        checks=tuple(checks),
    )


def mission_age(
    memory: db.MissionMemory,
    *,
    live: Sequence[db.Mission] = (),
    now: Optional[float] = None,
) -> tuple[Optional[float], str]:
    """Return age since latest live activity, close time, or memory update."""
    ts = time.time() if now is None else now
    live_changed = [mission.buffer_changed_at for mission in live if mission.buffer_changed_at > 0]
    if live_changed:
        return max(0.0, ts - max(live_changed)), "live buffer"
    if memory.closed_at > 0:
        return max(0.0, ts - memory.closed_at), "closed"
    if memory.updated_at > 0:
        return max(0.0, ts - memory.updated_at), "memory updated"
    if memory.created_at > 0:
        return max(0.0, ts - memory.created_at), "created"
    return None, "unknown"


def memory_source_ref(memory: db.MissionMemory) -> str:
    if memory.source_kind and memory.source_ref:
        return f"{memory.source_kind}:{memory.source_ref}"
    if memory.source_ref:
        return memory.source_ref
    if memory.source_kind:
        return memory.source_kind
    return f"graph:{memory.mission_id}"


def format_age(age_seconds: Optional[float]) -> str:
    if age_seconds is None:
        return "unknown"
    seconds = max(0, int(age_seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 72:
        return f"{hours}h"
    days = hours // 24
    remainder = hours % 24
    if remainder:
        return f"{days}d{remainder}h"
    return f"{days}d"


def _stale_check(age_seconds: Optional[float], stale_seconds: float, source: str) -> RecallCheck:
    stale_age = format_age(stale_seconds)
    if age_seconds is None:
        return RecallCheck(
            key="stale_age",
            label="stale age",
            passed=False,
            detail=f"age unknown; need >= {stale_age}",
            source_ref=source,
        )
    passed = age_seconds >= stale_seconds
    comparator = ">=" if passed else "<"
    return RecallCheck(
        key="stale_age",
        label="stale age",
        passed=passed,
        detail=f"age {format_age(age_seconds)} {comparator} {stale_age}",
        source_ref=source,
    )


def _target_check(target_seconds: float) -> RecallCheck:
    passed = target_seconds >= DEFAULT_TARGET_SECONDS
    return RecallCheck(
        key="target_seconds",
        label="target seconds",
        passed=passed,
        detail=(
            f"target {target_seconds:g}s supported by deterministic proxy"
            if passed
            else f"target {target_seconds:g}s is below supported proxy floor {DEFAULT_TARGET_SECONDS:g}s"
        ),
        source_ref="morpheus graph recall-eval",
    )


def _text_check(key: str, label: str, value: str, source_ref: str) -> RecallCheck:
    text = _clean(value)
    return RecallCheck(
        key=key,
        label=label,
        passed=bool(text),
        detail=_compact(text) if text else "missing",
        source_ref=source_ref,
    )


def _decision_check(
    memory: db.MissionMemory,
    events: Sequence[db.MissionEvent],
) -> RecallCheck:
    text = _clean(memory.last_decision)
    if text:
        return RecallCheck(
            key="recent_decision",
            label="recent decision",
            passed=True,
            detail=_compact(text),
            source_ref=memory_source_ref(memory),
        )
    event = _latest_event(events, {"decision"})
    if event is None:
        return RecallCheck(
            key="recent_decision",
            label="recent decision",
            passed=False,
            detail="missing",
        )
    return RecallCheck(
        key="recent_decision",
        label="recent decision",
        passed=True,
        detail=_compact(event.summary),
        source_ref=event.source_ref or f"event:{event.id}",
    )


def _check_event_check(events: Sequence[db.MissionEvent]) -> RecallCheck:
    event = _latest_event(events, CHECK_EVENT_KINDS)
    if event is None:
        return RecallCheck(
            key="recent_check",
            label="recent check",
            passed=False,
            detail="missing passing check",
        )
    if not _event_passed(event):
        return RecallCheck(
            key="recent_check",
            label="recent check",
            passed=False,
            detail=f"latest check not passing: {event.kind}: {_compact(event.summary)}",
            source_ref=event.source_ref or f"event:{event.id}",
        )
    return RecallCheck(
        key="recent_check",
        label="recent check",
        passed=True,
        detail=f"{event.kind}: {_compact(event.summary)}",
        source_ref=event.source_ref or f"event:{event.id}",
    )


def _proof_artifact_check(artifacts: Sequence[db.MissionArtifact]) -> RecallCheck:
    artifact = _latest_proof_artifact(artifacts)
    if artifact is None:
        return RecallCheck(
            key="proof_artifact",
            label="proof artifact",
            passed=False,
            detail="missing passing proof artifact",
        )
    status = _clean(artifact.status).lower()
    if status not in PROOF_ARTIFACT_STATUSES:
        return RecallCheck(
            key="proof_artifact",
            label="proof artifact",
            passed=False,
            detail=f"latest proof not passing: {status or 'unknown'} {artifact.kind}: {artifact.path_or_url}",
            source_ref=f"artifact:{artifact.id}",
        )
    detail = f"{artifact.status} {artifact.kind}: {artifact.path_or_url}"
    if artifact.summary:
        detail += f" - {_compact(artifact.summary)}"
    return RecallCheck(
        key="proof_artifact",
        label="proof artifact",
        passed=True,
        detail=detail,
        source_ref=f"artifact:{artifact.id}",
    )


def _latest_event(
    events: Sequence[db.MissionEvent],
    kinds: set[str],
) -> Optional[db.MissionEvent]:
    matches = [event for event in events if event.kind in kinds and _clean(event.summary)]
    if not matches:
        return None
    return max(matches, key=lambda event: (event.ts, event.id))


def _event_passed(event: db.MissionEvent) -> bool:
    status = _clean(str(event.metadata.get("status", ""))).lower()
    if status:
        return status in PASS_STATUSES
    summary = _clean(event.summary).lower()
    if FAIL_RE.search(summary):
        return False
    return bool(PASS_RE.search(summary))


def _latest_proof_artifact(
    artifacts: Sequence[db.MissionArtifact],
) -> Optional[db.MissionArtifact]:
    matches = [
        artifact for artifact in artifacts
        if _clean(artifact.path_or_url)
        and artifact.kind in PROOF_ARTIFACT_KINDS
    ]
    if not matches:
        return None
    return max(matches, key=lambda artifact: (artifact.created_at, artifact.id))


def _first_nonempty(*values: str) -> str:
    for value in values:
        text = _clean(value)
        if text:
            return text
    return ""


def _clean(value: str) -> str:
    return " ".join((value or "").split())


def _compact(value: str, limit: int = 160) -> str:
    text = _clean(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "..."
