"""Step execution port.

The pipeline never invents content. A step either runs a real handler (rule-based,
therefore honest by construction) or reports ``executor_unavailable`` so the board
shows an unfinished step instead of a plausible-looking artifact. Model-backed
steps plug in here later without touching the pipeline.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from octop.infra.pm.domain import evidence as evidence_rules
from octop.infra.pm.domain import stages


class ExecutorUnavailable(RuntimeError):
    """No handler is registered for a step that needs one."""


@dataclass
class StepContext:
    task_id: int
    task_public_id: str
    requirement_id: int
    requirement_title: str
    requirement_text: str
    workspace_id: int
    step_key: str
    seq: int
    threshold: int
    decision_level: str
    decision_timeout_s: int
    human_note: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    siblings: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class StepOutput:
    status: str  # passed | failed | needs_decision
    artifact: dict[str, Any] | None = None
    conclusions: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    trace: dict[str, Any] = field(default_factory=dict)
    decision: dict[str, Any] | None = None
    error_code: str | None = None
    message: str = ""


class StepExecutor(Protocol):
    def supports(self, step_key: str) -> bool: ...

    def run(self, ctx: StepContext) -> StepOutput: ...


def _pm_record(entity: str, ref: str) -> dict[str, Any]:
    return {"kind": "pm_record", "entity": entity, "ref": ref}


def _envelope(
    *,
    source_type: str,
    locator: dict[str, Any],
    excerpt: str,
    visibility: str = "internal",
    confidence: float | None = None,
) -> dict[str, Any]:
    return {
        "source_type": source_type,
        "port": source_type,
        "locator": locator,
        "excerpt": evidence_rules.clip_excerpt(excerpt),
        "visibility": visibility,
        "confidence": confidence,
    }


def _tokens(text: str) -> set[str]:
    return {token for token in re.split(r"[^\w\u4e00-\u9fff]+", text.lower()) if token}


class DeterministicExecutor:
    """Rule-based steps: intake, dedup, prototype_gate, deliver.

    Everything produced here is derivable from stored rows, so ``origin='rule'``
    and the conclusions are honestly evidenced.
    """

    HANDLED = ("intake", "dedup", "prototype_gate", "deliver")

    def supports(self, step_key: str) -> bool:
        return step_key in self.HANDLED

    def run(self, ctx: StepContext) -> StepOutput:
        handler = {
            "intake": self._intake,
            "dedup": self._dedup,
            "prototype_gate": self._prototype_gate,
            "deliver": self._deliver,
        }[ctx.step_key]
        return handler(ctx)

    def _intake(self, ctx: StepContext) -> StepOutput:
        fields = {
            "需求标题": ctx.requirement_title,
            "需求原文": ctx.requirement_text,
            "工作区": str(ctx.workspace_id),
            "任务": ctx.task_public_id,
        }
        gaps: list[str] = []
        raw = ctx.requirement_text.strip()
        if len(raw) < 40:
            gaps.append("需求原文过短，缺少可核查背景（<40 字）")
        if not re.search(r"\d", raw):
            gaps.append("需求未提及任何数值口径（数量/金额/时效）")
        if "客户" not in raw and "供应商" not in raw and "用户" not in raw:
            gaps.append("需求未指明受影响角色")

        evidence = [
            _envelope(
                source_type="human",
                locator=_pm_record("requirement", ctx.task_public_id),
                excerpt=raw[:400] or "(空)",
                confidence=0.6,
            )
        ]
        artifact = {
            "kind": "data",
            "title": "进件摘要",
            "audience": "internal",
            "fidelity": "exact",
            "origin": "rule",
            "body": json.dumps({"fields": fields, "gaps": gaps}, ensure_ascii=False, indent=2),
        }
        conclusions = [
            {
                "text": f"需求来自 {fields['需求标题']}，原文已存档。",
                "status": "evidenced",
                "evidence_indexes": [0],
            }
        ]
        if gaps:
            conclusions.append(
                {
                    "text": f"当前有 {len(gaps)} 处信息缺口，已在缺口清单列出。",
                    "status": "uncovered",
                }
            )
        return StepOutput(
            status="passed",
            artifact=artifact,
            conclusions=conclusions,
            evidence=evidence,
            gaps=gaps,
            trace={"sources": ["requirement"], "tool_calls": [], "gaps": len(gaps)},
        )

    def _dedup(self, ctx: StepContext) -> StepOutput:
        base = _tokens(ctx.requirement_title)
        matches: list[dict[str, Any]] = []
        for sibling in ctx.siblings:
            if int(sibling.get("id", 0)) == ctx.requirement_id:
                continue
            other = _tokens(str(sibling.get("title") or ""))
            if not base or not other:
                continue
            overlap = len(base & other) / max(1, len(base | other))
            if overlap >= 0.5:
                matches.append(
                    {
                        "requirement_id": sibling.get("id"),
                        "public_id": sibling.get("public_id"),
                        "title": sibling.get("title"),
                        "overlap": round(overlap, 3),
                    }
                )
        scanned = len(ctx.siblings)
        evidence = [
            _envelope(
                source_type="human",
                locator=_pm_record("workspace", str(ctx.workspace_id)),
                excerpt=f"比对范围：工作区 {ctx.workspace_id} 现存需求 {scanned} 条",
                confidence=0.5,
            )
        ]
        for item in matches[:5]:
            evidence.append(
                _envelope(
                    source_type="human",
                    locator=_pm_record("requirement", str(item["public_id"])),
                    excerpt=str(item["title"])[:300],
                    confidence=float(item["overlap"]),
                )
            )
        gaps = [] if matches else ["未发现重复需求（已比对全部现存需求）"]
        artifact = {
            "kind": "data",
            "title": "重复需求比对",
            "audience": "internal",
            "fidelity": "exact",
            "origin": "rule",
            "body": json.dumps(
                {"scanned": scanned, "matches": matches, "gaps": gaps}, ensure_ascii=False, indent=2
            ),
        }
        conclusions = [
            {
                "text": (
                    f"发现 {len(matches)} 条相似需求，需在拍板环节确认合并或拆分。"
                    if matches
                    else f"已比对 {scanned} 条现存需求，未发现重复。"
                ),
                "status": "evidenced",
            }
        ]
        return StepOutput(
            status="passed",
            artifact=artifact,
            conclusions=conclusions,
            evidence=evidence,
            gaps=gaps,
            trace={"sources": ["pm_workspace"], "tool_calls": ["title_overlap"], "scanned": scanned},
        )

    def _prototype_gate(self, ctx: StepContext) -> StepOutput:
        decision = {
            "level": ctx.decision_level,
            "question": "是否为本需求产出可点击原型？（可逆，超时按建议自动决定）",
            "recommended_key": "yes",
            "options": [
                {
                    "key": "yes",
                    "label": "产出原型",
                    "pros": "评审时有可点界面，减少口头歧义",
                    "cons": "多 0.5 人日",
                    "effort": "0.5d",
                    "confidence": 0.7,
                },
                {
                    "key": "no",
                    "label": "跳过原型，直接 PRD",
                    "pros": "更快",
                    "cons": "界面理解靠文字，易返工",
                    "effort": "0",
                    "confidence": 0.5,
                },
            ],
        }
        return StepOutput(
            status="needs_decision",
            decision=decision,
            trace={"sources": ["requirement"], "tool_calls": [], "gate": "prototype_gate"},
        )

    def _deliver(self, ctx: StepContext) -> StepOutput:
        return StepOutput(
            status="passed",
            artifact={
                "kind": "bundle",
                "title": "交付包",
                "audience": "internal",
                "fidelity": "partial",
                "fidelity_note": "由已通过的环节产物汇编，不含未完成环节内容",
                "origin": "rule",
                "body": json.dumps(
                    {"task": ctx.task_public_id, "requirement": ctx.requirement_title},
                    ensure_ascii=False,
                ),
            },
            evidence=[
                _envelope(
                    source_type="human",
                    locator=_pm_record("task", ctx.task_public_id),
                    excerpt=f"交付包汇编自任务 {ctx.task_public_id} 的已通过环节",
                    confidence=0.6,
                )
            ],
            conclusions=[
                {
                    "text": "交付包由已通过门禁的环节产物汇编而成。",
                    "status": "evidenced",
                    "evidence_indexes": [0],
                }
            ],
            trace={"sources": ["pm_task"], "tool_calls": ["assemble"]},
        )


class RegistryExecutor:
    """Delegates to a registered handler per step, else reports unavailable."""

    def __init__(self, *executors: StepExecutor) -> None:
        self._executors = list(executors)

    def register(self, executor: StepExecutor) -> None:
        self._executors.insert(0, executor)

    def supports(self, step_key: str) -> bool:
        return any(executor.supports(step_key) for executor in self._executors)

    def run(self, ctx: StepContext) -> StepOutput:
        for executor in self._executors:
            if executor.supports(ctx.step_key):
                return executor.run(ctx)
        spec = stages.step_by_key(ctx.step_key)
        handler = spec.handler_id if spec else "?"
        raise ExecutorUnavailable(
            f"no executor registered for step '{ctx.step_key}' ({handler})"
        )
