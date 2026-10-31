"""Gate engine — the automatable part of "is this step good enough?".

Twelve rules, registered by id, evaluated per stage. A failing rule only changes
the *step* status, never the task: a bad PRD draft should not stop the task from
being resumed after a fix. Each rule has a negative fixture in
``tests/unit/pm/test_gates_negative.py`` — a rule without a counter-example is not
a rule.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from octop.infra.pm.domain import stages


@dataclass(frozen=True)
class GateResult:
    rule_id: str
    stage: str
    passed: bool
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "stage": self.stage,
            "passed": self.passed,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass
class GateContext:
    """Everything a rule may look at. Nothing here is a DB handle on purpose."""

    step_key: str
    stage: str
    evidence: Sequence[Mapping[str, Any]] = ()
    gaps: Sequence[str] = ()
    trace_sources: Sequence[str] = ()
    requirement_items: Sequence[Mapping[str, Any]] = ()
    conclusions: Sequence[Mapping[str, Any]] = ()
    prototype_html: str | None = None
    # "source" means the markup came out of the product's own page; "ai" means
    # a model drew it. Only the former may claim exact fidelity.
    prototype_origin: str | None = None
    fidelity: str | None = None
    fidelity_note: str | None = None
    decision: Mapping[str, Any] | None = None
    reviews: Mapping[str, str] = field(default_factory=dict)
    external_write: bool = False
    external_write_signed: bool = False


@dataclass(frozen=True)
class GateReport:
    stage: str
    step_key: str
    results: tuple[GateResult, ...]

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)

    @property
    def failures(self) -> tuple[GateResult, ...]:
        return tuple(result for result in self.results if not result.passed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "step_key": self.step_key,
            "passed": self.passed,
            "results": [result.to_dict() for result in self.results],
            "failures": [result.rule_id for result in self.failures],
        }


Rule = Callable[[GateContext], tuple[bool, str, dict[str, Any]]]
# rule_id → (rule, applicable step_keys or None for "every step in the stage")
_RULES: dict[str, list[tuple[str, Rule, frozenset[str] | None]]] = {}


def gate(
    stage_key: str, rule_id: str, *, steps: tuple[str, ...] | None = None
) -> Callable[[Rule], Rule]:
    """Register a rule. ``steps`` narrows it to specific steps in the stage.

    Without that narrowing a stage-level rule would judge every step of the
    stage — e.g. the prototype rules would fail the PRD step for not containing
    ``<form>``.
    """

    def _register(func: Rule) -> Rule:
        _RULES.setdefault(stage_key, []).append(
            (rule_id, func, frozenset(steps) if steps else None)
        )
        return func

    return _register


def rules_for(stage_key: str, step_key: str | None = None) -> list[tuple[str, Rule]]:
    out: list[tuple[str, Rule]] = []
    for rule_id, rule, scope in _RULES.get(stage_key, []):
        if scope is not None and step_key is not None and step_key not in scope:
            continue
        out.append((rule_id, rule))
    return out


def rule_ids() -> dict[str, list[str]]:
    return {stage_key: [rule_id for rule_id, _, _ in items] for stage_key, items in _RULES.items()}


def rule_scopes() -> dict[str, list[str] | None]:
    return {
        rule_id: (sorted(scope) if scope else None)
        for items in _RULES.values()
        for rule_id, _, scope in items
    }


def evaluate(ctx: GateContext) -> GateReport:
    results: list[GateResult] = []
    for rule_id, rule in rules_for(ctx.stage, ctx.step_key):
        passed, reason, detail = rule(ctx)
        results.append(
            GateResult(rule_id=rule_id, stage=ctx.stage, passed=passed, reason=reason, detail=detail)
        )
    return GateReport(stage=ctx.stage, step_key=ctx.step_key, results=tuple(results))


def evaluate_step(step_key: str, ctx: GateContext | None = None, **overrides: Any) -> GateReport:
    stage_key = stages.stage_of(step_key)
    if stage_key is None:
        return GateReport(stage="?", step_key=step_key, results=())
    if ctx is None:
        ctx = GateContext(step_key=step_key, stage=stage_key, **overrides)
    else:
        ctx.step_key = step_key
        ctx.stage = stage_key
    return evaluate(ctx)


# --- P1 fact gathering ------------------------------------------------------

_CHECKABLE_RE = re.compile(
    r"(given\s+.+\s+when\s+.+\s+then|假如.+那么|当.+时.+则|若.+则|"
    r"不超过|不少于|至少\s*\d|≤|>=|≤\s*\d)",
    re.IGNORECASE,
)

_ELEMENT_TAGS = ("<form", "<table", "<button", "<input", "<select", "<textarea")


@gate("P1", "G-P1-01")
def _p1_evidence_addressed(ctx: GateContext) -> tuple[bool, str, dict[str, Any]]:
    if not ctx.evidence:
        return False, "no evidence collected", {"count": 0}
    bad = [
        index
        for index, item in enumerate(ctx.evidence)
        if not item.get("locator") or not str(item.get("excerpt") or "").strip()
    ]
    if bad:
        return False, "evidence without locator or excerpt", {"indexes": bad}
    return True, "", {"count": len(ctx.evidence)}


@gate("P1", "G-P1-02")
def _p1_gaps_explicit(ctx: GateContext) -> tuple[bool, str, dict[str, Any]]:
    if not ctx.evidence and not ctx.gaps:
        return False, "no evidence and no explicit gap list", {"gaps": 0}
    return True, "", {"gaps": list(ctx.gaps)}


@gate("P1", "G-P1-03")
def _p1_trace_sources(ctx: GateContext) -> tuple[bool, str, dict[str, Any]]:
    sources = [str(item).strip() for item in ctx.trace_sources if str(item).strip()]
    if not sources:
        return False, "retrieval source breakdown missing", {"sources": []}
    return True, "", {"sources": sources}


# --- P2 structure and feasibility -------------------------------------------

@gate("P2", "G-P2-01", steps=("requirement_analysis",))
def _p2_requirement_items(ctx: GateContext) -> tuple[bool, str, dict[str, Any]]:
    if not ctx.requirement_items:
        return False, "no requirement items", {}
    missing: list[int] = []
    for index, item in enumerate(ctx.requirement_items, start=1):
        if not all(str(item.get(key) or "").strip() for key in ("title", "user_value", "acceptance")):
            missing.append(index)
    if missing:
        return False, "items missing title/user_value/acceptance", {"indexes": missing}
    return True, "", {"count": len(ctx.requirement_items)}


@gate("P2", "G-P2-02", steps=("requirement_analysis",))
def _p2_acceptance_checkable(ctx: GateContext) -> tuple[bool, str, dict[str, Any]]:
    unchecked = [
        index
        for index, item in enumerate(ctx.requirement_items, start=1)
        if not _CHECKABLE_RE.search(str(item.get("acceptance") or ""))
    ]
    if unchecked:
        return False, "acceptance criteria are not machine-checkable", {"indexes": unchecked}
    return True, "", {"count": len(ctx.requirement_items)}


# --- P3 decisions -----------------------------------------------------------

@gate("P3", "G-P3-01")
def _p3_decision_recorded(ctx: GateContext) -> tuple[bool, str, dict[str, Any]]:
    decision = ctx.decision
    if not decision:
        return False, "no decision recorded", {}
    selected = str(decision.get("selected_key") or "").strip()
    note = str(decision.get("note") or "").strip()
    if not selected:
        return False, "decision has no selected option", {}
    if len(note) < 10:
        return False, "decision reason missing or too short (<10 chars)", {"note_len": len(note)}
    auto = bool(decision.get("auto_decided"))
    if decision.get("decided_by") in (None, "", 0) and not auto:
        return False, "decision has no decider", {}
    if not decision.get("decided_at"):
        return False, "decision has no timestamp", {}
    return True, "", {"selected_key": selected, "auto_decided": auto}


@gate("P3", "G-P3-02")
def _p3_option_snapshot(ctx: GateContext) -> tuple[bool, str, dict[str, Any]]:
    decision = ctx.decision or {}
    snapshot = decision.get("option_snapshot") or decision.get("options") or []
    if not snapshot:
        return False, "option snapshot missing — the card cannot be reviewed later", {}
    return True, "", {"options": len(snapshot)}


# --- P4 production ----------------------------------------------------------

@gate("P4", "G-P4-01", steps=("prototype",))
def _p4_prototype_substance(ctx: GateContext) -> tuple[bool, str, dict[str, Any]]:
    html = (ctx.prototype_html or "").lower()
    if not html:
        return False, "prototype body is empty", {}
    kinds = [tag for tag in _ELEMENT_TAGS if tag in html]
    has_style = "<style" in html or "style=" in html
    if len(kinds) < 2:
        return False, "prototype has fewer than two real element kinds", {"kinds": kinds}
    if not has_style:
        return False, "prototype has no styling", {"kinds": kinds}
    return True, "", {"kinds": kinds}


@gate("P4", "G-P4-02", steps=("prototype",))
def _p4_prototype_framing(ctx: GateContext) -> tuple[bool, str, dict[str, Any]]:
    html = (ctx.prototype_html or "").lower()
    document_opens = html.count("<html")
    fences = (ctx.prototype_html or "").count("```")
    if document_opens > 1:
        return False, "more than one <html> document in one prototype", {"html": document_opens}
    if fences:
        return False, "markdown code fences inside prototype body", {"fences": fences}
    return True, "", {"html": document_opens}


@gate("P4", "G-P4-03")
def _p4_fidelity(ctx: GateContext) -> tuple[bool, str, dict[str, Any]]:
    if ctx.fidelity not in ("exact", "partial", "unlabeled"):
        return False, "fidelity is not declared", {"fidelity": ctx.fidelity}
    if ctx.fidelity == "unlabeled":
        return False, "unlabeled output may not pass", {"fidelity": ctx.fidelity}
    if ctx.fidelity == "partial" and not str(ctx.fidelity_note or "").strip():
        return False, "partial fidelity requires a difference note", {}
    return True, "", {"fidelity": ctx.fidelity}


@gate("P4", "G-P4-04")
def _p4_no_fabrication(ctx: GateContext) -> tuple[bool, str, dict[str, Any]]:
    if not ctx.conclusions:
        return False, "artifact has no conclusions to check", {}
    bad = [
        str(item.get("seq") or index)
        for index, item in enumerate(ctx.conclusions, start=1)
        if str(item.get("status") or "") not in ("evidenced", "uncovered", "undetermined")
    ]
    if bad:
        return False, "unknown conclusion status", {"seqs": bad}
    unsupported = [
        str(item.get("seq") or index)
        for index, item in enumerate(ctx.conclusions, start=1)
        if str(item.get("status")) == "evidenced" and not item.get("evidence_ids")
    ]
    if unsupported:
        return False, "evidenced conclusion without linked evidence", {"seqs": unsupported}
    return True, "", {
        "total": len(ctx.conclusions),
        "uncovered": sum(1 for item in ctx.conclusions if item.get("status") == "uncovered"),
    }


@gate("P4", "G-P4-05", steps=("prototype",))
def _p4_exact_requires_source(ctx: GateContext) -> tuple[bool, str, dict[str, Any]]:
    """``exact`` is reserved for markup that came out of the product's own page.

    A model that has never seen the product cannot produce a 1:1 result, so a
    model-drawn prototype claiming exact fidelity is a false statement about the
    artifact rather than a quality problem. Catching it here means the claim has
    to be backed by the derivation path, which records the page and the class
    overlap it measured.
    """

    origin = str(ctx.prototype_origin or "")
    fidelity = str(ctx.fidelity or "")
    if fidelity == "exact" and origin != "source":
        return (
            False,
            "exact fidelity requires a prototype derived from product source",
            {"origin": origin or "(unset)", "fidelity": fidelity},
        )
    return True, "", {"origin": origin or "(unset)", "fidelity": fidelity}


# --- P5 review and delivery -------------------------------------------------

@gate("P5", "G-P5-01")
def _p5_reviews_complete(ctx: GateContext) -> tuple[bool, str, dict[str, Any]]:
    missing = [item for item in ("business", "tech", "customer") if item not in ctx.reviews]
    if missing:
        return False, "missing review perspectives", {"missing": missing}
    rejected = [key for key, verdict in ctx.reviews.items() if verdict != "pass"]
    if rejected:
        return False, "a perspective rejected the deliverable", {"rejected": rejected}
    return True, "", {"reviews": dict(ctx.reviews)}


@gate("P5", "G-P5-02")
def _p5_external_write_signed(ctx: GateContext) -> tuple[bool, str, dict[str, Any]]:
    if ctx.external_write and not ctx.external_write_signed:
        return False, "external write requires an answered L3 card", {}
    return True, "", {"external_write": ctx.external_write}
