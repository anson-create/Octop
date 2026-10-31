"""What a ticket tells us about *which part of the product* it is about.

A requirement's prose is the worst place to look for the name of a screen: the
description talks about behaviour, while the title, the Jira component field,
the label and the name of the attached screenshot talk about the area. Mixing
them into one string would let a long description drown a two-word component
name, so every source keeps its own weight and they are searched separately.

Nothing here decides anything. It only ranks the texts; the caller still has to
corroborate a page against the code graph before claiming a match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Weights are relative, not calibrated: they encode "a component field is worth
# more than a comment" and nothing finer. Tuned against real tickets, not theory.
WEIGHT_COMPONENT = 3.0
WEIGHT_LABEL = 2.0
WEIGHT_SUMMARY = 2.0
WEIGHT_ATTACHMENT = 1.5
WEIGHT_CUSTOM = 1.0
WEIGHT_DESCRIPTION = 1.0
WEIGHT_ENVIRONMENT = 0.8
WEIGHT_COMMENT = 0.6

# Screenshot names are written by people in a hurry: `image (3).png` carries
# nothing, `供应商列表-筛选.png` carries everything. Strip the decoration and
# drop names that end up empty rather than searching for "image".
_ATTACHMENT_NOISE = re.compile(
    r"^(image|screenshot|snipaste|wx_camera|wechatimg|img|pastedimage|"
    r"截屏|截图|屏幕截图|图片|未命名)[\s_\-()0-9]*$",
    re.IGNORECASE,
)
_ATTACHMENT_DATE = re.compile(r"^(19|20)\d{2}[-_.]?\d{4}[-_.]?")
_EXTENSION = re.compile(r"\.(png|jpe?g|gif|bmp|webp|heic|pdf|docx?|xlsx?|zip|mp4|mov)$", re.IGNORECASE)


def _attachment_hint(filename: str) -> str:
    stem = _EXTENSION.sub("", filename or "").strip()
    stem = _ATTACHMENT_DATE.sub("", stem).strip()
    if _ATTACHMENT_NOISE.match(stem):
        return ""
    # `供应商列表_v2` still names a screen; keep the readable half.
    return stem.replace("_", " ").replace("-", " ").strip()


@dataclass(frozen=True)
class EvidenceSource:
    """One piece of ticket text that may name a functional area."""

    kind: str
    field: str
    text: str
    weight: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "field": self.field,
            "text": self.text[:400],
            "weight": self.weight,
        }


@dataclass(frozen=True)
class LocateEvidence:
    """Weighted sources drawn from one Jira issue."""

    sources: tuple[EvidenceSource, ...] = ()
    # Things we could not read, stated rather than silently skipped. A screenshot
    # whose name carries nothing still may show the page — we just cannot see it.
    unread: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_issue(cls, issue: dict[str, Any] | None) -> LocateEvidence:
        if not isinstance(issue, dict):
            return cls()
        sources: list[EvidenceSource] = []
        unread: list[str] = []

        title = str(issue.get("title") or "").strip()
        if title:
            sources.append(
                EvidenceSource(kind="summary", field="标题", text=title, weight=WEIGHT_SUMMARY)
            )

        for name in issue.get("components") or []:
            text = str(name or "").strip()
            if text:
                sources.append(
                    EvidenceSource(
                        kind="component", field="Jira 组件", text=text, weight=WEIGHT_COMPONENT
                    )
                )

        for name in issue.get("labels") or []:
            text = str(name or "").strip()
            if text:
                sources.append(
                    EvidenceSource(kind="label", field="标签", text=text, weight=WEIGHT_LABEL)
                )

        for item in issue.get("custom_fields") or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            sources.append(
                EvidenceSource(
                    kind="custom",
                    field=str(item.get("name") or item.get("id") or "自定义字段"),
                    text=text,
                    weight=WEIGHT_CUSTOM,
                )
            )

        description = str(issue.get("description") or "").strip()
        if description:
            sources.append(
                EvidenceSource(
                    kind="description",
                    field="描述",
                    text=description,
                    weight=WEIGHT_DESCRIPTION,
                )
            )

        environment = str(issue.get("environment") or "").strip()
        if environment:
            sources.append(
                EvidenceSource(
                    kind="environment",
                    field="环境",
                    text=environment,
                    weight=WEIGHT_ENVIRONMENT,
                )
            )

        for item in issue.get("attachments") or []:
            if not isinstance(item, dict):
                continue
            filename = str(item.get("filename") or "")
            hint = _attachment_hint(filename)
            if hint:
                sources.append(
                    EvidenceSource(
                        kind="attachment",
                        field=f"附件名：{filename[:60]}",
                        text=hint,
                        weight=WEIGHT_ATTACHMENT,
                    )
                )
            else:
                unread.append(f"附件 {filename or '(无名)'} 的文件名不含页面信息，且未做图内文字识别")

        for item in issue.get("comments") or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if text:
                sources.append(
                    EvidenceSource(
                        kind="comment",
                        field=f"评论（{item.get('author') or '匿名'}）",
                        text=text,
                        weight=WEIGHT_COMMENT,
                    )
                )

        return cls(sources=tuple(sources), unread=tuple(unread))

    # --- queries ----------------------------------------------------------

    @property
    def has_content(self) -> bool:
        return any(item.text.strip() for item in self.sources)

    def ranked(self) -> tuple[EvidenceSource, ...]:
        """Sources strongest first. Ties keep insertion order (title before body)."""

        return tuple(sorted(self.sources, key=lambda item: -item.weight))

    def strong_sources(self, *, floor: float = WEIGHT_CUSTOM) -> tuple[EvidenceSource, ...]:
        """Sources at or above ``floor`` — the ones allowed to anchor a page."""

        return tuple(item for item in self.ranked() if item.weight >= floor)

    def merged_text(self, *, limit: int = 6000) -> str:
        """Everything joined, for callers that cannot weight. Avoid using this
        for location — it is the dilution this module exists to prevent."""

        parts: list[str] = []
        budget = limit
        for item in self.ranked():
            text = item.text.strip()
            if not text:
                continue
            chunk = f"[{item.field}] {text}"
            if len(chunk) > budget:
                break
            parts.append(chunk)
            budget -= len(chunk)
        return "\n".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sources": [item.as_dict() for item in self.ranked()],
            "unread": list(self.unread),
        }


def evidence_from_baseline(baseline: dict[str, Any] | None) -> LocateEvidence:
    """Rebuild evidence stored at intake time.

    Intake is the only moment we hold the ticket; every later step reads this
    snapshot rather than calling Jira again, so a ticket edited mid-flight
    cannot silently move the prototype to a different screen.
    """

    if not isinstance(baseline, dict):
        return LocateEvidence()
    raw = baseline.get("locate_evidence")
    if not isinstance(raw, dict):
        return LocateEvidence()
    sources: list[EvidenceSource] = []
    for item in raw.get("sources") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        try:
            weight = float(item.get("weight") or 0.0)
        except (TypeError, ValueError):
            weight = 0.0
        sources.append(
            EvidenceSource(
                kind=str(item.get("kind") or "unknown"),
                field=str(item.get("field") or ""),
                text=text,
                weight=weight,
            )
        )
    unread = tuple(str(item) for item in (raw.get("unread") or []))
    return LocateEvidence(sources=tuple(sources), unread=unread)
