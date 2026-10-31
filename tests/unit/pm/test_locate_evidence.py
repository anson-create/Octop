"""What a ticket says about *where* it lives.

The bug these tests exist for: a long description used to be the only search
text, so a two-word Jira component ("供应商管理") was diluted into nothing and the
locator landed on whichever page happened to share a generic field name. These
pin the two halves of the fix — every source keeps its weight, and sources that
carry no text say so instead of being silently dropped.
"""

from __future__ import annotations

from octop.infra.pm.domain.locate_evidence import (
    WEIGHT_COMMENT,
    WEIGHT_COMPONENT,
    WEIGHT_DESCRIPTION,
    WEIGHT_SUMMARY,
    LocateEvidence,
    evidence_from_baseline,
)
from octop.infra.pm.ports.jira import issue_to_summary, locate_field_ids

ISSUE = {
    "key": "CCTOWER-3597",
    "fields": {
        "summary": "派单供应商列表新增就绪状态筛选",
        "description": "供应商列表需要按就绪状态筛选，未就绪的行禁用开关并显示原因。",
        "project": {"key": "CCTOWER"},
        "components": [{"name": "供应商管理"}, {"name": "智能派单"}],
        "labels": ["派单", "供应商"],
        "attachment": [
            {"filename": "供应商列表-筛选态.png", "mimeType": "image/png", "size": 2048},
            {"filename": "image (3).png", "mimeType": "image/png", "size": 1024},
        ],
        "comment": {
            "comments": [
                {"author": {"displayName": "王五"}, "body": "这个在供应商台账页面里加一列就行"},
            ]
        },
        "customfield_10231": "运营每天手工核对，经常漏掉未就绪的供应商，导致派单失败",
    },
}

CUSTOM_NAMES = {"customfield_10231": "业务痛点"}


def _summarised() -> dict:
    return issue_to_summary(ISSUE, custom_names=CUSTOM_NAMES)


# --- reading the ticket -----------------------------------------------------


def test_intake_reads_fields_the_picker_never_shows() -> None:
    issue = _summarised()

    assert issue["components"] == ["供应商管理", "智能派单"]
    assert [item["name"] for item in issue["custom_fields"]] == ["业务痛点"]
    assert issue["comments"][0]["author"] == "王五"
    assert [item["filename"] for item in issue["attachments"]] == [
        "供应商列表-筛选态.png",
        "image (3).png",
    ]


def test_a_custom_field_without_a_display_name_is_dropped() -> None:
    """An unnamed ``customfield_10999`` would reach the model as a bare key."""

    issue = issue_to_summary(ISSUE)

    assert issue["custom_fields"] == []


# --- field catalogue --------------------------------------------------------


def test_the_catalogue_picks_fields_by_meaning_not_by_id() -> None:
    """The field id is chosen per instance; the display name is the stable part."""

    catalog = [
        {"id": "customfield_10231", "name": "业务痛点", "schema": {"type": "string"}},
        {"id": "customfield_10232", "name": "建议方案", "schema": {"type": "string"}},
        {"id": "customfield_10233", "name": "需求优先级", "schema": {"type": "string"}},
        {"id": "customfield_10234", "name": "故事点", "schema": {"type": "number"}},
        {"id": "summary", "name": "摘要", "schema": {"type": "string"}},
    ]

    picked = {item["name"] for item in locate_field_ids(catalog)}

    assert picked == {"业务痛点", "建议方案"}


def test_a_priority_field_is_not_read_as_product_context() -> None:
    """`需求优先级` contains 需求; matching on substring alone would read it."""

    catalog = [{"id": "customfield_1", "name": "需求优先级", "schema": {"type": "string"}}]

    assert locate_field_ids(catalog) == []


def test_a_date_field_is_skipped_even_when_its_name_matches() -> None:
    catalog = [{"id": "customfield_2", "name": "期望完成", "schema": {"type": "date"}}]

    assert locate_field_ids(catalog) == []


# --- weighting --------------------------------------------------------------


def test_a_component_outranks_the_title_and_the_title_outranks_the_body() -> None:
    evidence = LocateEvidence.from_issue(_summarised())

    ordered = [item.kind for item in evidence.ranked()]

    assert ordered[0] == "component"
    assert ordered.index("summary") < ordered.index("description")
    assert evidence.strong_sources()[0].text == "供应商管理"


def test_the_body_cannot_outrank_the_component_it_contradicts() -> None:
    """This is the dilution bug, stated as an assertion."""

    evidence = LocateEvidence.from_issue(_summarised())
    weights = {item.kind: item.weight for item in evidence.sources}

    assert weights["component"] == WEIGHT_COMPONENT
    assert weights["summary"] == WEIGHT_SUMMARY
    assert weights["description"] == WEIGHT_DESCRIPTION
    assert weights["comment"] == WEIGHT_COMMENT
    assert WEIGHT_COMPONENT > WEIGHT_SUMMARY > WEIGHT_DESCRIPTION > WEIGHT_COMMENT


# --- attachment names -------------------------------------------------------


def test_a_screenshot_named_after_a_screen_becomes_a_signal() -> None:
    evidence = LocateEvidence.from_issue(_summarised())
    attachment = [item for item in evidence.sources if item.kind == "attachment"]

    assert len(attachment) == 1
    assert "供应商列表" in attachment[0].text


def test_a_worthless_screenshot_name_is_declared_unread_not_guessed() -> None:
    """`image (3).png` may well show the screen. We cannot see it, so we say so."""

    evidence = LocateEvidence.from_issue(_summarised())

    assert any("image (3).png" in note for note in evidence.unread)
    assert any("未做图内文字识别" in note for note in evidence.unread)


# --- storage round trip -----------------------------------------------------


def test_evidence_survives_the_baseline_snapshot_unchanged() -> None:
    """Later steps read the snapshot, so a Jira edit cannot move the page."""

    evidence = LocateEvidence.from_issue(_summarised())
    restored = evidence_from_baseline({"locate_evidence": evidence.as_dict()})

    assert [item.text for item in restored.ranked()] == [
        item.text for item in evidence.ranked()
    ]
    assert restored.unread == evidence.unread


def test_a_requirement_without_a_snapshot_yields_nothing() -> None:
    assert LocateEvidence.from_issue(None).has_content is False
    assert evidence_from_baseline({}).has_content is False
    assert evidence_from_baseline({"locate_evidence": "not a dict"}).has_content is False
