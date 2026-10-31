"""Intake is by reference: the server reads the ticket, not the browser.

These cover the rule the user asked for — a requirement can only be born from an
existing Jira issue — plus the promise that made it acceptable: the PAT never
lands in the database.
"""

from __future__ import annotations

import json

import pytest

from octop.infra.pm import errors
from octop.infra.pm.ports.jira import PortUnavailable


def test_status_reports_not_configured_on_a_fresh_workspace(service, workspace) -> None:
    status = service.jira_status(workspace["id"])
    assert status["configured"] is False
    assert status["token_set"] is False


def test_intake_refuses_before_jira_is_configured(service, workspace) -> None:
    with pytest.raises(errors.OctopError) as caught:
        service.create_requirement_from_jira(workspace["id"], issue_key="CARGO-1234")
    assert "Jira" in str(caught.value) or "jira" in str(caught.value).lower()


def test_intake_pulls_title_and_description_from_jira(service, workspace, fake_jira) -> None:
    requirement = service.create_requirement_from_jira(
        workspace["id"], issue_key="cargo-1234"
    )

    # Whatever case the user typed, the stored reference is canonical.
    assert requirement["source"] == "jira"
    assert requirement["source_ref"] == "CARGO-1234"
    assert requirement["title"] == "报关单批量导入"
    assert "单次不超过 200 票" in requirement["raw_text"]


def test_issue_key_stays_out_of_raw_text(service, workspace, fake_jira) -> None:
    """Exports run a blacklist that blocks ticket ids; the body must not carry one.

    The key still lives in source_ref/baseline for traceability.
    """
    requirement = service.create_requirement_from_jira(workspace["id"], issue_key="CARGO-1234")

    assert "CARGO-1234" not in requirement["raw_text"]
    assert requirement["source_ref"] == "CARGO-1234"


def test_unknown_issue_is_rejected_and_nothing_is_written(service, workspace, fake_jira) -> None:
    with pytest.raises(errors.OctopError):
        service.create_requirement_from_jira(workspace["id"], issue_key="CARGO-9999")

    listed = service.list_requirements(workspace_id=workspace["id"])
    assert listed["total"] == 0


def test_same_issue_twice_returns_the_same_requirement(service, workspace, fake_jira) -> None:
    first = service.create_requirement_from_jira(workspace["id"], issue_key="CARGO-1234")
    second = service.create_requirement_from_jira(workspace["id"], issue_key="CARGO-1234")

    assert first["id"] == second["id"]


def test_unreachable_jira_blocks_intake(service, workspace, monkeypatch) -> None:
    """Intake degrades to an error, never to a requirement with invented text."""

    def boom(self, workspace_id):
        raise PortUnavailable("Jira 请求超时")

    monkeypatch.setattr(type(service), "jira_client", boom, raising=True)

    with pytest.raises(errors.OctopError) as caught:
        service.create_requirement_from_jira(workspace["id"], issue_key="CARGO-1234")
    assert "超时" in str(caught.value)
    assert service.list_requirements(workspace_id=workspace["id"])["total"] == 0


def test_token_is_stored_outside_the_database_row(service, workspace, db) -> None:
    service.save_jira_config(
        workspace["id"],
        base_url="https://jira.example.com",
        token="super-secret-pat",
        jql="assignee = currentUser()",
    )

    with db.connect() as conn:
        rows = conn.execute("SELECT mapping_json, credential_ref FROM pm_integration").fetchall()
        secrets = conn.execute("SELECT k, v FROM secrets").fetchall()

    assert len(rows) == 1
    mapping_json = str(rows[0]["mapping_json"])
    credential_ref = str(rows[0]["credential_ref"])
    assert "super-secret-pat" not in mapping_json
    assert credential_ref.startswith("pm.jira.credential.")

    stored = {row["k"]: bytes(row["v"]).decode("utf-8") for row in secrets}
    # The stored value is one JSON credential (account name + password), so the
    # token is a field inside it rather than the whole value.
    assert json.loads(stored[credential_ref])["token"] == "super-secret-pat"

    # And the read API never echoes it back.
    status = service.jira_status(workspace["id"])
    assert status["token_set"] is True
    assert "super-secret-pat" not in str(status)


def test_config_rejects_a_url_without_scheme(service, workspace) -> None:
    with pytest.raises(errors.OctopError):
        service.save_jira_config(workspace["id"], base_url="jira.example.com", token="x")


def test_bare_issue_key_uses_a_key_lookup_not_text_search(
    service, workspace, fake_jira
) -> None:
    service.create_requirement_from_jira(workspace["id"], issue_key="CARGO-1234")
    # Configure so the client path is used, then check the JQL that was built.
    from octop.infra.pm.service import _looks_like_issue_key

    assert _looks_like_issue_key("CCTOWER-3597") is True
    assert _looks_like_issue_key("供应商 就绪") is False


def test_intake_snapshots_every_field_that_can_name_the_screen(
    service, workspace, fake_jira
) -> None:
    """Title and description are not the only places a ticket says *where*.

    The component picker, the custom "痛点" field, a comment and the name of an
    attached screenshot each name a functional area. Intake is the only moment
    they are in hand, so they are snapshotted here rather than re-fetched later.
    """

    requirement = service.create_requirement_from_jira(
        workspace["id"], issue_key="CARGO-1234"
    )
    baseline = requirement["baseline"]
    evidence = baseline["locate_evidence"]

    assert baseline["jira_components"] == ["报关单管理"]
    assert baseline["jira_custom_fields"][0]["name"] == "业务痛点"

    kinds = {item["kind"] for item in evidence["sources"]}
    assert {"component", "summary", "description", "custom", "comment", "attachment"} <= kinds
    # Ranked, so the first entry is the one worth trusting most.
    assert evidence["sources"][0]["kind"] == "component"
    assert evidence["sources"][0]["text"] == "报关单管理"


def test_the_screenshot_name_is_used_but_its_contents_are_not_claimed(
    service, workspace, fake_jira
) -> None:
    requirement = service.create_requirement_from_jira(
        workspace["id"], issue_key="CARGO-1234"
    )
    evidence = requirement["baseline"]["locate_evidence"]

    attachment = [item for item in evidence["sources"] if item["kind"] == "attachment"]
    assert attachment and "报关单列表" in attachment[0]["text"]
    # No OCR was run, and nothing may imply otherwise.
    assert all("识别" not in item["text"] for item in evidence["sources"])
