"""Gate rules: one positive and one negative fixture per rule id.

A rule without a counter-example is not a rule — this file is the counter-example
set required by the build guide.
"""

from __future__ import annotations

from typing import Any

import pytest

from octop.infra.pm.domain import gates

_LONG_NOTE = "这个方案会影响客户对外承诺，需要先确认范围再推进"


def _ctx(**kwargs: Any) -> gates.GateContext:
    base: dict[str, Any] = {
        "step_key": "intake",
        "stage": "P1",
        "evidence": ({"source_type": "kb", "locator": {"kind": "kb_section", "kb_id": "k", "doc_id": "d"}, "excerpt": "x", "visibility": "internal"},),
        "gaps": ("缺字段",),
        "trace_sources": ("kb",),
    }
    step_key = kwargs.pop("step_key", base["step_key"])
    base.pop("step_key")
    base.pop("stage")
    stage = kwargs.pop("stage", gates.stages.stage_of(step_key) or "P1")
    base.update(kwargs)
    return gates.GateContext(step_key=step_key, stage=stage, **base)


def _result(ctx: gates.GateContext, rule_id: str) -> gates.GateResult:
    report = gates.evaluate(ctx)
    matches = [item for item in report.results if item.rule_id == rule_id]
    assert matches, f"{rule_id} did not run for step {ctx.step_key}"
    return matches[0]


POSITIVE = {
    "G-P1-01": _ctx(),
    "G-P1-02": _ctx(evidence=(), gaps=("缺口",)),
    "G-P1-03": _ctx(),
    "G-P2-01": _ctx(step_key="requirement_analysis", requirement_items=({"title": "t", "user_value": "v", "acceptance": "当提交时则不超过 200 票"},)),
    "G-P2-02": _ctx(step_key="requirement_analysis", requirement_items=({"title": "t", "user_value": "v", "acceptance": "当提交时则不超过 200 票"},)),
    "G-P3-01": _ctx(step_key="decision", decision={"selected_key": "yes", "note": _LONG_NOTE, "decided_by": 1, "decided_at": "2026-10-31T00:00:00Z", "option_snapshot": [{"key": "yes"}]}),
    "G-P3-02": _ctx(step_key="decision", decision={"selected_key": "yes", "note": _LONG_NOTE, "decided_by": 1, "decided_at": "2026-10-31T00:00:00Z", "option_snapshot": [{"key": "yes"}]}),
    "G-P4-01": _ctx(step_key="prototype", prototype_html='<html><style>.a{}</style><form><input></form><table></table></html>', fidelity="exact", conclusions=({"status": "uncovered"},)),
    "G-P4-02": _ctx(step_key="prototype", prototype_html="<html><form><table></table></form></html>", fidelity="exact", conclusions=({"status": "uncovered"},)),
    "G-P4-03": _ctx(step_key="prototype", prototype_html="<html></html>", fidelity="partial", fidelity_note="列表未做分页", conclusions=({"status": "uncovered"},)),
    "G-P4-04": _ctx(step_key="prd", conclusions=({"seq": 1, "status": "evidenced", "evidence_ids": [1]}, {"seq": 2, "status": "uncovered"})),
    "G-P5-01": _ctx(step_key="review", reviews={"business": "pass", "tech": "pass", "customer": "pass"}),
    "G-P5-02": _ctx(step_key="deliver", external_write=True, external_write_signed=True),
}

NEGATIVE = {
    "G-P1-01": _ctx(evidence=()),
    "G-P1-02": _ctx(evidence=(), gaps=()),
    "G-P1-03": _ctx(trace_sources=()),
    "G-P2-01": _ctx(step_key="requirement_analysis", requirement_items=({"title": "t"},)),
    "G-P2-02": _ctx(step_key="requirement_analysis", requirement_items=({"title": "t", "user_value": "v", "acceptance": "做得好看一点"},)),
    "G-P3-01": _ctx(step_key="decision", decision={"selected_key": "yes", "note": "太短", "decided_by": 1, "decided_at": "x"}),
    "G-P3-02": _ctx(step_key="decision", decision={"selected_key": "yes", "note": _LONG_NOTE, "decided_by": 1, "decided_at": "x", "option_snapshot": []}),
    "G-P4-01": _ctx(step_key="prototype", prototype_html="<div>空</div>", fidelity="exact", conclusions=({"status": "uncovered"},)),
    "G-P4-02": _ctx(step_key="prototype", prototype_html="<html><html>", fidelity="exact", conclusions=({"status": "uncovered"},)),
    "G-P4-03": _ctx(step_key="prototype", prototype_html="<html></html>", fidelity=None, conclusions=({"status": "uncovered"},)),
    "G-P4-04": _ctx(step_key="prd", conclusions=({"seq": 1, "status": "evidenced", "evidence_ids": []},)),
    "G-P5-01": _ctx(step_key="review", reviews={"business": "pass"}),
    "G-P5-02": _ctx(step_key="deliver", external_write=True, external_write_signed=False),
}


def test_every_rule_has_both_fixtures() -> None:
    registered = {rule_id for ids in gates.rule_ids().values() for rule_id in ids}
    assert registered == set(POSITIVE) == set(NEGATIVE)


@pytest.mark.parametrize("rule_id", sorted(POSITIVE))
def test_positive_fixture_passes(rule_id: str) -> None:
    result = _result(POSITIVE[rule_id], rule_id)
    assert result.passed, f"{rule_id} unexpectedly failed: {result.reason}"


@pytest.mark.parametrize("rule_id", sorted(NEGATIVE))
def test_negative_fixture_fails(rule_id: str) -> None:
    result = _result(NEGATIVE[rule_id], rule_id)
    assert not result.passed
    assert result.reason, f"{rule_id} failed without a reason"


def test_report_aggregates_failures() -> None:
    report = gates.evaluate(_ctx(evidence=(), gaps=(), trace_sources=()))
    assert report.passed is False
    assert {item.rule_id for item in report.failures} == {"G-P1-01", "G-P1-02", "G-P1-03"}
    assert report.to_dict()["failures"] == ["G-P1-01", "G-P1-02", "G-P1-03"]


def test_unknown_step_has_no_rules() -> None:
    report = gates.evaluate_step("not_a_step")
    assert report.results == ()


def test_scope_narrows_rules_to_steps() -> None:
    """P4 prototype rules must not judge the PRD step."""
    prd = gates.evaluate_step("prd", prototype_html="<div/>")
    assert "G-P4-01" not in {item.rule_id for item in prd.results}
    prototype = gates.evaluate_step("prototype", prototype_html="<div/>")
    assert "G-P4-01" in {item.rule_id for item in prototype.results}
