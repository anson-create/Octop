"""Export gates: trimming by visibility, blacklist scanning, safe identifiers."""

from __future__ import annotations

import pytest

from octop.infra.pm.domain import export


def _conclusions() -> list[dict]:
    return [
        {
            "seq": 1,
            "text": "批量导入需要限制单次 200 票，依据代码 src/importer.vue:120-138",
            "customer_text": "单次导入上限为 200 票",
            "status": "evidenced",
            "evidence_ids": [1],
        },
        {
            "seq": 2,
            "text": "失败原因可回显给操作员",
            "status": "evidenced",
            "evidence_ids": [2],
        },
        {
            "seq": 3,
            "text": "暂不支持供应商侧导入",
            "status": "uncovered",
            "evidence_ids": [],
        },
    ]


EVIDENCE = {
    1: {"visibility": "internal", "source_type": "code"},
    2: {"visibility": "customer", "source_type": "kb"},
}


def test_internal_export_keeps_internal_evidence() -> None:
    plan = export.build_plan(
        audience="internal",
        title="方案",
        body="正文",
        conclusions=_conclusions(),
        evidence=EVIDENCE,
    )
    assert plan.downgraded == 0
    assert plan.dropped_evidence == 0
    assert plan.conclusions[0].evidence_ids == (1,)


def test_customer_export_rewrites_and_trims() -> None:
    plan = export.build_plan(
        audience="customer",
        title="方案",
        body="正文",
        conclusions=_conclusions(),
        evidence=EVIDENCE,
    )
    first, second, third = plan.conclusions
    # seq 1: internal-only evidence, but a customer_text exists → restated, no evidence exposed
    assert first.text == "单次导入上限为 200 票"
    assert first.evidence_ids == ()
    # seq 2: customer-visible evidence survives
    assert second.evidence_ids == (2,)
    # seq 3 was already a declared gap
    assert third.status == "uncovered"
    assert plan.dropped_evidence == 1
    assert any("客户版措辞" in note for note in plan.notes)


def test_customer_export_downgrades_without_customer_text() -> None:
    conclusions = [
        {
            "seq": 1,
            "text": "依赖 OMS_BOOKING 表",
            "status": "evidenced",
            "evidence_ids": [1],
        }
    ]
    plan = export.build_plan(
        audience="customer",
        title="t",
        body="",
        conclusions=conclusions,
        evidence={1: {"visibility": "internal"}},
    )
    assert plan.conclusions[0].status == "uncovered"
    assert plan.downgraded == 1
    assert any("降级" in note for note in plan.notes)


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("见 src/service/Importer.java 第 120 行", "E-EXPORT-02a"),
        ("读取 OMS_BOOKING 的字段", "E-EXPORT-02b"),
        ("关联工单 CARGO-1234", "E-EXPORT-02c"),
        ("由 张三 确认口径", "E-EXPORT-02d"),
    ],
)
def test_blacklist_hits(text: str, rule: str) -> None:
    hits = export.scan(text, internal_names=["张三"])
    assert any(hit.rule == rule for hit in hits), hits


def test_pm_public_ids_are_not_blocked() -> None:
    hits = export.scan("需求 REQ-20261031-001 对应任务 PM-20261031-002")
    assert hits == []


def test_hits_block_the_plan_and_report_clean_matches() -> None:
    plan = export.build_plan(
        audience="customer",
        title="方案",
        body="详情见 src/order.vue",
        conclusions=[],
        evidence={},
    )
    assert plan.ok is False
    payload = plan.to_dict()
    assert payload["ok"] is False
    assert payload["hits"][0]["label"] == "源码路径"


def test_render_markdown_marks_gaps_and_omits_evidence_for_customers() -> None:
    plan = export.build_plan(
        audience="customer",
        title="方案",
        body="正文",
        conclusions=_conclusions(),
        evidence=EVIDENCE,
    )
    rendered = export.render_markdown(plan)
    assert "（uncovered）" in rendered
    assert "证据索引" not in rendered


def test_visibility_helper() -> None:
    assert export.visibility_allows("customer", "customer") is True
    assert export.visibility_allows("internal", "customer") is False
    assert export.visibility_allows("internal", "internal") is True
