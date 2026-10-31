"""Internal / customer export with four gates (E-EXPORT-01..04).

D2 put the customer edition on day one, so trimming is not a later refinement —
it ships with V0. Two layers, because one layer means betting the whole risk on
the completeness of the rules:

1. *trimming* — a conclusion may only carry evidence the audience is allowed to
   see (``pm_evidence.visibility``); otherwise it degrades to ``uncovered`` or is
   rewritten with ``customer_text``;
2. *blacklist* — a final scan for source paths, table names, ticket ids and
   internal names. Hits block the export and are reported, never silently
   replaced.

The blacklist is a backstop, not a substitute for trimming.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from octop.infra.pm.domain.evidence import locator_label, visibility_allows

BLACKLIST: tuple[tuple[str, str, str], ...] = (
    ("E-EXPORT-02a", r"(/src/|\\src\\|\.java\b|\.vue\b|\.tsx\b|@Table\b|@Entity\b)", "源码路径"),
    ("E-EXPORT-02b", r"\b[A-Z][A-Z0-9]{1,}_[A-Z0-9_]{2,}\b", "内部表名/常量"),
    (
        "E-EXPORT-02c",
        r"\b(?:CARGO-\d+|[A-Z]{2,}-\d{3,})\b",
        "内部工单号",
    ),
)

_COMPILED = tuple((rule, re.compile(pattern), label) for rule, pattern, label in BLACKLIST)

# PM's own public identifiers are not internal tickets; excluding them keeps the
# ticket pattern from blocking every customer export.
_SAFE_ID_PREFIXES = ("REQ-", "PM-")


@dataclass(frozen=True)
class Hit:
    rule: str
    label: str
    match: str
    start: int

    def to_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "label": self.label, "match": self.match, "start": self.start}


@dataclass(frozen=True)
class ExportedConclusion:
    seq: int
    text: str
    customer_text: str | None
    status: str
    evidence_ids: tuple[int, ...]
    downgraded: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "text": self.text,
            "customer_text": self.customer_text,
            "status": self.status,
            "evidence_ids": list(self.evidence_ids),
            "downgraded": self.downgraded,
        }


@dataclass(frozen=True)
class ExportPlan:
    audience: str
    title: str
    body: str
    conclusions: tuple[ExportedConclusion, ...]
    hits: tuple[Hit, ...]
    downgraded: int
    dropped_evidence: int
    notes: tuple[str, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return not self.hits

    def to_dict(self) -> dict[str, Any]:
        return {
            "audience": self.audience,
            "title": self.title,
            "ok": self.ok,
            "hits": [hit.to_dict() for hit in self.hits],
            "downgraded": self.downgraded,
            "dropped_evidence": self.dropped_evidence,
            "conclusions": [item.to_dict() for item in self.conclusions],
            "notes": list(self.notes),
        }


def scan(text: str, *, internal_names: Sequence[str] = ()) -> list[Hit]:
    hits: list[Hit] = []
    for rule, pattern, label in _COMPILED:
        for match in pattern.finditer(text):
            token = match.group(0)
            if rule == "E-EXPORT-02c" and token.startswith(_SAFE_ID_PREFIXES):
                continue
            hits.append(Hit(rule=rule, label=label, match=token, start=match.start()))
    for name in internal_names:
        if not name:
            continue
        for match in re.finditer(re.escape(name), text):
            hits.append(Hit(rule="E-EXPORT-02d", label="内部人员名", match=name, start=match.start()))
    return hits


def build_plan(
    *,
    audience: str,
    title: str,
    body: str,
    conclusions: Sequence[Mapping[str, Any]],
    evidence: Mapping[int, Mapping[str, Any]],
    internal_names: Sequence[str] = (),
) -> ExportPlan:
    """Apply E-EXPORT-01 (trimming) then E-EXPORT-02 (scan)."""
    exported: list[ExportedConclusion] = []
    downgraded = 0
    dropped = 0
    rewritten_count = 0

    for item in conclusions:
        seq = int(item.get("seq") or 0)
        status = str(item.get("status") or "uncovered")
        evidence_ids = tuple(int(eid) for eid in item.get("evidence_ids") or ())
        text = str(item.get("text") or "")
        customer_text = item.get("customer_text")

        kept_ids: tuple[int, ...] = evidence_ids
        was_downgraded = False
        if audience == "customer":
            allowed = [
                eid
                for eid in evidence_ids
                if eid in evidence
                and visibility_allows(str(evidence[eid].get("visibility") or "internal"), "customer")
            ]
            dropped += len(evidence_ids) - len(allowed)
            kept_ids = tuple(allowed)
            if status == "evidenced" and not allowed:
                # E-EXPORT-01: either restate for the customer, or admit the gap.
                if customer_text:
                    rewritten_count += 1
                else:
                    status = "uncovered"
                    was_downgraded = True
                    downgraded += 1

        rendered = text
        if audience == "customer" and customer_text:
            rendered = str(customer_text)
        exported.append(
            ExportedConclusion(
                seq=seq,
                text=rendered,
                customer_text=str(customer_text) if customer_text else None,
                status=status,
                evidence_ids=kept_ids,
                downgraded=was_downgraded,
            )
        )

    payload = "\n".join(
        [
            title or "",
            body or "",
            *[item.text for item in exported],
        ]
    )
    hits = tuple(scan(payload, internal_names=internal_names))
    notes: list[str] = []
    if audience == "customer" and any(item.downgraded for item in exported):
        notes.append("部分结论因证据仅内部可见而降级为未覆盖")
    if audience == "customer" and rewritten_count:
        notes.append(f"{rewritten_count} 条结论改用客户版措辞（未暴露内部证据）")
    return ExportPlan(
        audience=audience,
        title=title,
        body=body,
        conclusions=tuple(exported),
        hits=hits,
        downgraded=downgraded,
        dropped_evidence=dropped,
        notes=tuple(notes),
    )


def render_markdown(plan: ExportPlan, *, include_evidence: bool = True) -> str:
    lines = [f"# {plan.title or '方案'}", ""]
    if plan.body:
        lines.extend([plan.body.strip(), ""])
    if plan.conclusions:
        lines.append("## 结论")
        for item in plan.conclusions:
            suffix = "" if item.status == "evidenced" else f"（{item.status}）"
            lines.append(f"{item.seq}. {item.text}{suffix}")
        lines.append("")
    if include_evidence and plan.audience == "internal":
        lines.append("## 证据索引")
        for item in plan.conclusions:
            for eid in item.evidence_ids:
                lines.append(f"- 结论 {item.seq} → 证据 #{eid}")
        lines.append("")
    if plan.notes:
        lines.append("## 导出说明")
        lines.extend(f"- {note}" for note in plan.notes)
    return "\n".join(lines).strip() + "\n"


def evidence_line(evidence_id: int, item: Mapping[str, Any]) -> str:
    locator = item.get("locator") or {}
    label = locator_label(locator if isinstance(locator, dict) else {})
    return f"#{evidence_id} [{item.get('source_type', '?')}] {label}"
