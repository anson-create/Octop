"""Decision policy and evidence envelope rules."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from octop.infra.errors import OctopError
from octop.infra.pm.domain import decisions, evidence


def test_levels_and_labels() -> None:
    assert decisions.LEVELS == ("L1", "L2", "L3")
    for level in decisions.LEVELS:
        assert decisions.describe(level)
    with pytest.raises(OctopError):
        decisions.ensure_level("L9")


def test_only_l1_auto_answers_and_only_l1_l2_expire() -> None:
    assert decisions.should_auto_answer("L1") is True
    assert decisions.should_auto_answer("L2") is False
    assert decisions.expires("L1") is True
    assert decisions.expires("L2") is True
    assert decisions.expires("L3") is False


def test_l3_timeout_is_pushed_far_out() -> None:
    assert decisions.timeout_seconds("L1", 180) == 180
    assert decisions.timeout_seconds("L2", 86400) == 86400
    assert decisions.timeout_seconds("L3", 86400) > 86400 * 6


def test_timeout_at_is_iso_utc() -> None:
    stamp = decisions.timeout_at("L1", 60, now=datetime(2026, 10, 31, 2, 0, tzinfo=UTC))
    assert stamp == "2026-10-31T02:01:00Z"


def test_pick_auto_choice_prefers_recommendation_then_first() -> None:
    options = [{"key": "a"}, {"key": "b"}]
    assert decisions.pick_auto_choice(options, "b") == "b"
    assert decisions.pick_auto_choice(options, "zzz") == "a"
    with pytest.raises(OctopError):
        decisions.pick_auto_choice([], None)


def test_validate_answer_rules() -> None:
    options = [{"key": "yes"}, {"key": "no"}]
    selected, note = decisions.validate_answer(options, selected_key="yes", note="  范围已确认，可以推进  ")
    assert selected == "yes" and note == "范围已确认，可以推进"

    with pytest.raises(OctopError):
        decisions.validate_answer(options, selected_key="maybe", note="这个理由足够长了")
    with pytest.raises(OctopError):
        decisions.validate_answer(options, selected_key="yes", note="短")


def test_auto_note_is_self_describing() -> None:
    note = decisions.auto_note("L1", "yes")
    assert "自动决策" in note and "申诉" in note and len(note) >= decisions.NOTE_MIN_LEN


def test_envelope_requires_a_locator() -> None:
    problems = evidence.validate(
        {"source_type": "code", "locator": {}, "excerpt": "x", "visibility": "internal"}
    )
    assert any("locator" in item for item in problems)


def test_envelope_rejects_unknown_locator_kind() -> None:
    problems = evidence.validate(
        {
            "source_type": "code",
            "locator": {"kind": "carrier_pigeon", "x": 1},
            "excerpt": "x",
        }
    )
    assert any("locator kind" in item for item in problems)


def test_envelope_accepts_pm_record_and_clips_excerpt() -> None:
    envelope = evidence.normalise(
        {
            "source_type": "human",
            "locator": {"kind": "pm_record", "entity": "requirement", "ref": "REQ-1"},
            "excerpt": "x" * 5000,
            "visibility": "customer",
            "confidence": 0.5,
        },
        retrieved_at="2026-10-31T00:00:00Z",
        stale_at=None,
    )
    assert envelope.ok
    assert len(envelope.excerpt) == evidence.EXCERPT_LIMIT + 1  # ellipsis appended
    assert envelope.content_hash


def test_confidence_must_be_a_fraction() -> None:
    problems = evidence.validate(
        {
            "source_type": "kb",
            "locator": {"kind": "kb_section", "kb_id": "k", "doc_id": "d"},
            "excerpt": "x",
            "confidence": 1.5,
        }
    )
    assert any("confidence" in item for item in problems)


def test_staleness_and_locator_label() -> None:
    assert evidence.is_stale("2026-01-01T00:00:00Z", "2026-10-31T00:00:00Z") is True
    assert evidence.is_stale(None, "2026-10-31T00:00:00Z") is False
    assert (
        evidence.locator_label(
            {"kind": "code_span", "repo": "h5", "path": "a.vue", "line": [1, 9]}
        )
        == "h5/a.vue:1-9"
    )
    assert evidence.locator_label({"kind": "jira_field", "issue": "CARGO-1", "field": "f"}) == (
        "CARGO-1.f"
    )
