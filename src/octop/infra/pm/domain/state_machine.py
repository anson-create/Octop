"""State machines for task / step / decision.

Rule from the build guide: status is written by the state machine and the gate
engine only. Model output never reaches a status column, and every transition is
checked here so an illegal jump raises instead of silently corrupting the board.
"""

from __future__ import annotations

from collections.abc import Iterable

from octop.infra.pm.errors import conflict

TASK_STATES = (
    "queued",
    "running",
    "waiting_human",
    "blocked",
    "paused",
    "delivered",
    "archived",
)

STEP_STATES = ("pending", "running", "passed", "failed", "waiting_card", "skipped")

DECISION_STATES = ("pending", "answered", "auto_answered", "expired", "void")

_TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "paused", "blocked", "archived"}),
    "running": frozenset({"running", "waiting_human", "blocked", "paused", "delivered"}),
    "waiting_human": frozenset({"running", "blocked", "paused", "archived"}),
    "blocked": frozenset({"running", "paused", "archived"}),
    "paused": frozenset({"queued", "running", "archived"}),
    "delivered": frozenset({"archived"}),
    "archived": frozenset(),
}

_STEP_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "skipped"}),
    "running": frozenset({"passed", "failed", "waiting_card"}),
    "waiting_card": frozenset({"running", "failed", "passed", "skipped"}),
    "failed": frozenset({"pending", "running", "skipped"}),
    "passed": frozenset({"pending", "passed"}),
    "skipped": frozenset({"pending"}),
}

_DECISION_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"answered", "auto_answered", "expired", "void"}),
    "answered": frozenset(),
    "auto_answered": frozenset(),
    "expired": frozenset({"answered", "void"}),
    "void": frozenset(),
}


def _table(kind: str) -> dict[str, frozenset[str]]:
    if kind == "task":
        return _TASK_TRANSITIONS
    if kind == "step":
        return _STEP_TRANSITIONS
    if kind == "decision":
        return _DECISION_TRANSITIONS
    raise ValueError(f"unknown state machine: {kind}")


def can_transition(kind: str, source: str, target: str) -> bool:
    return target in _table(kind).get(source, frozenset())


def ensure_transition(kind: str, source: str, target: str) -> None:
    if source == target and kind != "decision":
        return
    if not can_transition(kind, source, target):
        raise conflict(
            f"illegal {kind} transition: {source} -> {target}",
            kind=kind,
            source=source,
            target=target,
        )


def task_status_from_steps(statuses: Iterable[str]) -> str:
    """Derive the task status from its step statuses (gate-driven, not model-driven)."""
    items = list(statuses)
    if not items:
        return "queued"
    if "waiting_card" in items:
        return "waiting_human"
    if all(status in ("passed", "skipped") for status in items):
        return "delivered"
    if "running" in items:
        return "running"
    if "failed" in items:
        return "blocked"
    return "queued"


def stage_from_steps(steps: Iterable[object], default: str = "P5") -> str:
    """Current stage = stage of the first step that is not passed/skipped."""
    for step in sorted(steps, key=lambda item: getattr(item, "seq", 0)):
        if getattr(step, "status", "pending") not in ("passed", "skipped"):
            return str(getattr(step, "stage", "P1"))
    return default
