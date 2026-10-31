"""Decision policy: levels, timeouts, auto-answer, appeal.

D3 shipped defaults with a two-week calibration window, so the level lives in
``pm_step_handler.decision_level`` and changing it is a row update — not a deploy.
Defaults stay conservative: only reversible steps auto-answer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

from octop.infra.pm.errors import invalid

LEVELS = ("L1", "L2", "L3")

LEVEL_LABELS = {
    "L1": "可自动（可逆）",
    "L2": "需人拍板（阻塞）",
    "L3": "强制人签（不可撤）",
}

NOTE_MIN_LEN = 10
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_L3_TIMEOUT_MULTIPLIER = 7


def ensure_level(level: str) -> str:
    if level not in LEVELS:
        raise invalid(f"level must be one of {LEVELS}", level=level)
    return level


def timeout_seconds(level: str, base_seconds: int) -> int:
    """L3 cards never auto-expire in practice; the timestamp is only a display floor."""
    ensure_level(level)
    if level == "L3":
        return max(base_seconds, 1) * _L3_TIMEOUT_MULTIPLIER
    return max(base_seconds, 1)


def timeout_at(level: str, base_seconds: int, *, now: datetime | None = None) -> str:
    current = now or datetime.now(UTC)
    return (current + timedelta(seconds=timeout_seconds(level, base_seconds))).strftime(_TIME_FORMAT)


def should_auto_answer(level: str) -> bool:
    return level == "L1"


def expires(level: str) -> bool:
    """L3 cards stay open until a human signs; only L1/L2 sweep on timeout."""
    return level in ("L1", "L2")


def option_keys(options: Sequence[Mapping[str, object]]) -> list[str]:
    return [str(item.get("key")) for item in options if item.get("key") is not None]


def pick_auto_choice(
    options: Sequence[Mapping[str, object]], recommended_key: str | None
) -> str:
    keys = option_keys(options)
    if not keys:
        raise invalid("decision has no options to auto-pick")
    if recommended_key and recommended_key in keys:
        return recommended_key
    return keys[0]


def auto_note(level: str, selected_key: str) -> str:
    return (
        f"超时自动决策（{LEVEL_LABELS.get(level, level)}）：系统选择 {selected_key}，"
        "已留痕，可申诉。"
    )


def validate_answer(
    options: Sequence[Mapping[str, object]],
    *,
    selected_key: str,
    note: str,
) -> tuple[str, str]:
    keys = option_keys(options)
    if selected_key not in keys:
        raise invalid("selected option is not in the card", selected_key=selected_key, options=keys)
    cleaned = (note or "").strip()
    if len(cleaned) < NOTE_MIN_LEN:
        raise invalid(
            f"decision reason must be at least {NOTE_MIN_LEN} characters",
            length=len(cleaned),
        )
    return selected_key, cleaned


def describe(level: str) -> str:
    return LEVEL_LABELS.get(level, level)
