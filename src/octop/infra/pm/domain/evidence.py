"""Evidence envelope: the single shape every retrieval port must produce.

An envelope without a locator is not evidence — it is a quote with no address.
G-P1-01 rejects it, and ``EXCERPT_LIMIT`` keeps the row small enough that the
table does not become the slowest thing on the task page.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

EXCERPT_LIMIT = 2048

SOURCE_TYPES = ("jira", "code", "datadict", "kb", "human", "web")
VISIBILITIES = ("internal", "customer")

# Required keys per locator kind. "An address you can click" only means something
# if the shape is enforced.
LOCATOR_REQUIRED: dict[str, tuple[str, ...]] = {
    "jira_field": ("issue", "field"),
    "code_span": ("repo", "path"),
    "dict_field": ("table",),
    "kb_section": ("kb_id", "doc_id"),
    "url": ("url",),
    # A record inside PM itself (requirement, workspace, decision). Needed so a
    # step can cite "this requirement says X" without faking an external source.
    "pm_record": ("entity", "ref"),
}

# Default staleness windows, in seconds, by source type (§8.3).
STALE_WINDOWS_S: dict[str, int] = {
    "code": 7 * 86400,
    "datadict": 30 * 86400,
    "jira": 30 * 86400,
    "kb": 30 * 86400,
    "web": 7 * 86400,
    "human": 180 * 86400,
}


def content_hash(excerpt: str) -> str:
    return hashlib.sha256(excerpt.encode("utf-8")).hexdigest()


def clip_excerpt(excerpt: str, limit: int = EXCERPT_LIMIT) -> str:
    text = (excerpt or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


@dataclass(frozen=True)
class Envelope:
    source_type: str
    port: str
    locator: dict[str, Any]
    excerpt: str
    retrieved_at: str
    content_hash: str
    stale_at: str | None = None
    visibility: str = "internal"
    confidence: float | None = None
    problems: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.problems


def validate(payload: dict[str, Any]) -> tuple[str, ...]:
    """Return validation problems (empty means the envelope is usable)."""
    problems: list[str] = []
    source_type = str(payload.get("source_type") or "")
    if source_type not in SOURCE_TYPES:
        problems.append(f"source_type must be one of {SOURCE_TYPES}")
    port = str(payload.get("port") or source_type)
    if port not in SOURCE_TYPES:
        problems.append(f"port must be one of {SOURCE_TYPES}")

    locator = payload.get("locator")
    if not isinstance(locator, dict) or not locator:
        problems.append("locator is required")
    else:
        kind = str(locator.get("kind") or "")
        required = LOCATOR_REQUIRED.get(kind)
        if required is None:
            problems.append(f"unknown locator kind: {kind or '(missing)'}")
        else:
            missing = [key for key in required if not str(locator.get(key) or "").strip()]
            if missing:
                problems.append(f"locator.{kind} missing keys: {', '.join(missing)}")

    excerpt = str(payload.get("excerpt") or "").strip()
    if not excerpt:
        problems.append("excerpt is required")

    visibility = str(payload.get("visibility") or "internal")
    if visibility not in VISIBILITIES:
        problems.append(f"visibility must be one of {VISIBILITIES}")

    confidence = payload.get("confidence")
    if confidence is not None:
        try:
            value = float(confidence)
        except (TypeError, ValueError):
            problems.append("confidence must be a number")
        else:
            if not 0.0 <= value <= 1.0:
                problems.append("confidence must be within 0..1")
    return tuple(problems)


def normalise(payload: dict[str, Any], *, retrieved_at: str, stale_at: str | None) -> Envelope:
    excerpt = clip_excerpt(str(payload.get("excerpt") or ""))
    source_type = str(payload.get("source_type") or "")
    return Envelope(
        source_type=source_type,
        port=str(payload.get("port") or source_type),
        locator=dict(payload.get("locator") or {}),
        excerpt=excerpt,
        retrieved_at=retrieved_at,
        content_hash=content_hash(excerpt),
        stale_at=stale_at,
        visibility=str(payload.get("visibility") or "internal"),
        confidence=(
            float(payload["confidence"]) if payload.get("confidence") is not None else None
        ),
        problems=validate({**payload, "excerpt": excerpt}),
    )


def visibility_allows(evidence_visibility: str, audience: str) -> bool:
    """``customer`` audience may only carry ``customer``-visible evidence."""
    if audience == "customer":
        return evidence_visibility == "customer"
    return True


def is_stale(stale_at: str | None, now_iso: str) -> bool:
    if not stale_at:
        return False
    return stale_at <= now_iso


def locator_label(locator: dict[str, Any]) -> str:
    """Human-readable address used in exports and evidence badges."""
    kind = str(locator.get("kind") or "")
    if kind == "code_span":
        path = locator.get("path")
        line = locator.get("line")
        if isinstance(line, (list, tuple)) and len(line) == 2:
            return f"{locator.get('repo', '')}/{path}:{line[0]}-{line[1]}"
        return f"{locator.get('repo', '')}/{path}"
    if kind == "jira_field":
        return f"{locator.get('issue', '')}.{locator.get('field', '')}"
    if kind == "dict_field":
        column = locator.get("column")
        return f"{locator.get('table', '')}.{column}" if column else str(locator.get("table", ""))
    if kind == "kb_section":
        return f"{locator.get('doc_id', '')}{locator.get('anchor', '')}"
    if kind == "url":
        return str(locator.get("url", ""))
    if kind == "pm_record":
        return f"pm://{locator.get('entity', '')}/{locator.get('ref', '')}"
    return kind or "unknown"
