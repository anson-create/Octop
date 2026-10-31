"""Shared helpers for PM repos.

Timestamps are stored as ISO-8601 UTC strings (``2026-10-31T02:00:00Z``) on both
dialects. The host stores unix ints for its own tables; PM keeps ISO because
evidence windows, decision countdowns, and audit trails are read by humans and
exported verbatim.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def now_iso() -> str:
    return time.strftime(ISO_FORMAT, time.gmtime())


def iso_in(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).strftime(ISO_FORMAT)


def day_key() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def to_iso(value: Any) -> str | None:
    """Normalise a DB value to an ISO string (PostgreSQL may hand back datetime)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        stamp = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return stamp.astimezone(UTC).strftime(ISO_FORMAT)
    text = str(value)
    return text or None


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def is_past(value: str | None) -> bool:
    parsed = from_iso(value)
    return parsed is not None and parsed <= datetime.now(UTC)


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


def cell(row: Any, key: str, index: int = 0) -> Any:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return row[key]
    return row[index]


def insert_id(conn: Any, sql: str, params: Sequence[Any]) -> int:
    """``INSERT … RETURNING id`` — supported by SQLite 3.35+ and PostgreSQL."""
    row = conn.execute(f"{sql} RETURNING id", tuple(params)).fetchone()
    if row is None:
        raise RuntimeError("INSERT RETURNING id produced no row")
    return int(cell(row, "id"))


def count_rows(conn: Any, sql: str, params: Sequence[Any] = ()) -> int:
    row = conn.execute(sql, tuple(params)).fetchone()
    return int(cell(row, "n", 0) or 0)


def placeholders(count: int) -> str:
    return ", ".join("?" for _ in range(count))
