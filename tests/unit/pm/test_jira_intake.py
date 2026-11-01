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


# --- everything the ticket says, not just its description -------------------
#
# Intake used to store only `description`. The custom fields, the environment,
# the comments and the attachment names were read for page location and then
# dropped, so the analysis step wrote about a ticket it had only partly read.
# These cover the composition rule, the budget, and the key that may not leak.


def test_intake_keeps_the_whole_ticket_not_just_the_description(
    service, workspace, fake_jira
) -> None:
    requirement = service.create_requirement_from_jira(workspace["id"], issue_key="CARGO-1234")
    text = requirement["raw_text"]

    assert "单次不超过 200 票" in text  # description
    assert "关务每天手工录入 200 票" in text  # custom field the team calls 痛点
    assert "业务痛点" in text  # and the field's own name
    assert "报关单列表页加个导入按钮" in text  # comment
    assert "报关单管理" in text  # component
    assert "报关单列表-导入入口.png" in text  # attachment name


def test_the_comment_is_labelled_so_it_is_not_read_as_a_requirement(
    service, workspace, fake_jira
) -> None:
    """A model that cannot tell a comment from the ask will quote the wrong one."""

    requirement = service.create_requirement_from_jira(workspace["id"], issue_key="CARGO-1234")
    text = requirement["raw_text"]

    assert "## 讨论记录" in text
    assert "产品经理" in text
    assert text.index("单次不超过 200 票") < text.index("## 讨论记录")


def test_baseline_records_how_the_text_was_assembled(service, workspace, fake_jira) -> None:
    """A truncated body must not look like a short ticket."""

    baseline = service.create_requirement_from_jira(
        workspace["id"], issue_key="CARGO-1234"
    )["baseline"]

    assert "需求描述" in baseline["jira_text_sections"]
    assert "讨论记录" in baseline["jira_text_sections"]
    assert baseline["jira_comment_count"] == 1
    assert baseline["jira_attachments"][0]["filename"] == "报关单列表-导入入口.png"
    assert baseline["jira_text_truncated"] == []


def test_an_issue_with_no_content_still_says_so(service, workspace, fake_jira) -> None:
    fake_jira.issues["CARGO-7777"] = {
        "key": "CARGO-7777",
        "title": "只有标题的工单",
        "description": "",
        "status": "To Do",
        "project": "CARGO",
    }

    requirement = service.create_requirement_from_jira(workspace["id"], issue_key="CARGO-7777")

    assert "未填写描述" in requirement["raw_text"]
    # Only the absence is stated; nothing is invented to fill the gap.
    assert "## 讨论记录" not in requirement["raw_text"]


def test_a_comment_naming_another_ticket_is_reported_not_rewritten(
    service, workspace, fake_jira
) -> None:
    """The export gate blocks ticket ids; the operator has to hear it at intake.

    The alternative — editing the customer's words on the way in — makes the
    stored requirement differ from the ticket it claims to come from.
    """

    fake_jira.issues["CARGO-8888"] = {
        "key": "CARGO-8888",
        "title": "含其他工单号的评论",
        "description": "正文",
        "status": "To Do",
        "project": "CARGO",
        "comments": [{"author": "业务", "text": "和 CARGO-9999 是同一个问题", "created": ""}],
    }

    requirement = service.create_requirement_from_jira(workspace["id"], issue_key="CARGO-8888")

    assert "CARGO-9999" in requirement["raw_text"]
    assert requirement["raw_text"].count("CARGO-8888") == 0
    assert requirement["baseline"]["jira_export_blockers"]
    assert "CARGO-9999" in requirement["baseline"]["jira_export_blockers"][0]


def test_a_huge_ticket_is_cut_on_a_line_boundary_and_says_where() -> None:
    from octop.infra.pm.domain.jira_intake import compose

    issue = {
        "key": "CARGO-1",
        "title": "很长的工单",
        "description": "\n".join(f"第 {index} 条验收标准" for index in range(400)),
        "comments": [{"author": "业务", "text": "评论", "created": ""}],
    }

    composed = compose(issue, max_chars=1200)

    assert len(composed.text) < 1600
    assert composed.truncated
    assert "截断" in composed.text
    # Cut mid-line would read as a typo rather than a truncation.
    assert not composed.text.rstrip().endswith("验收标")


# --- preview: what the dialog shows must equal what intake writes -----------


def test_preview_returns_the_same_text_intake_will_store(service, workspace, fake_jira) -> None:
    preview = service.preview_jira_issue(workspace["id"], issue_key="cargo-1234")
    requirement = service.create_requirement_from_jira(workspace["id"], issue_key="CARGO-1234")

    assert preview["key"] == "CARGO-1234"
    assert preview["requirement_text"] == requirement["raw_text"]
    assert preview["existing"] is None
    assert preview["issue"]["comments"][0]["author"] == "产品经理"


def test_preview_flags_a_ticket_that_is_already_a_requirement(
    service, workspace, fake_jira
) -> None:
    created = service.create_requirement_from_jira(workspace["id"], issue_key="CARGO-1234")

    preview = service.preview_jira_issue(workspace["id"], issue_key="CARGO-1234")

    assert preview["existing"]["id"] == created["id"]
    assert preview["existing"]["public_id"] == created["public_id"]


def test_preview_rejects_a_typed_string_that_is_not_a_key(service, workspace, fake_jira) -> None:
    with pytest.raises(errors.OctopError) as caught:
        service.preview_jira_issue(workspace["id"], issue_key="报关单 批量导入")
    assert "工单号" in str(caught.value)


def test_preview_of_an_unknown_key_fails_without_writing(service, workspace, fake_jira) -> None:
    with pytest.raises(errors.OctopError):
        service.preview_jira_issue(workspace["id"], issue_key="CARGO-4040")

    assert service.list_requirements(workspace_id=workspace["id"])["total"] == 0


# --- one fuzzy box, defaulted to the design queue ---------------------------
#
# The dialog used to open onto nothing: a key field with no results, plus a
# separate search list hidden behind a toggle. Two boxes for one question
# ("which ticket?") meant every operator had to know which one to use, and the
# default queue — mine, waiting to be designed, P0/P1 — was never shown at all.


def test_the_default_queue_is_the_design_backlog_not_everything_assigned(
    service, workspace, fake_jira
) -> None:
    result = service.list_jira_issues(workspace["id"])

    assert 'assignee = currentUser()' in result["jql"]
    assert 'status in ("待设计")' in result["jql"]
    assert 'priority in ("P0", "P1")' in result["jql"]
    assert result["note"] == ""


def test_a_mistyped_key_reads_as_no_match_instead_of_an_error(
    service, workspace, fake_jira
) -> None:
    """A search box that 404s on a typo is not a search box.

    The key is OR-ed with a text search rather than looked up exactly, so a
    wrong digit returns an empty list the operator can see and correct.
    """

    result = service.list_jira_issues(workspace["id"], q="cargo-9999")

    assert 'key = "CARGO-9999"' in result["jql"]
    assert "text ~" in result["jql"]


def test_a_keyword_search_is_a_fuzzy_text_match(service, workspace, fake_jira) -> None:
    result = service.list_jira_issues(workspace["id"], q="报关单 批量导入")

    assert 'text ~ "报关单 批量导入"' in result["jql"]
    assert "key =" not in result["jql"]


def test_a_pool_filter_the_site_rejects_falls_back_and_says_so(
    service, workspace, fake_jira
) -> None:
    """A stored guess about status names must not open the dialog onto a 400.

    "待设计" is this product's guess about someone else's Jira vocabulary. On a
    site that calls it something else, the useful behaviour is to show the
    tickets and state the fallback — not to hand the operator an error they
    cannot fix from that dialog.
    """

    fake_jira.reject_jql = "待设计"

    result = service.list_jira_issues(workspace["id"])

    assert result["items"]
    assert "resolution = Unresolved" in result["jql"]
    assert "退回" in result["note"]
    assert "does not exist for the field" in result["note"]


def test_a_query_the_operator_typed_is_never_swapped_for_a_fallback(
    service, workspace, fake_jira
) -> None:
    """Silently replacing their search would report success for a query that
    never ran."""

    fake_jira.reject_jql = "报关单"

    with pytest.raises(errors.OctopError):
        service.list_jira_issues(workspace["id"], q="报关单")


def test_meta_reports_which_default_names_this_site_actually_has(
    service, workspace, fake_jira
) -> None:
    meta = service.jira_meta(workspace["id"])

    assert meta["user"] == "测试员"
    assert meta["statuses"] == ["待设计", "开发中", "已完成"]
    assert meta["priorities"] == ["P0", "P1", "P2"]
    assert meta["status_hits"] == ["待设计"]
    assert meta["priority_hits"] == ["P0", "P1"]
    assert meta["ok"] is True
    assert meta["error"] == ""


def test_meta_flags_a_site_that_calls_the_status_something_else(
    service, workspace, fake_jira
) -> None:
    """The whole point of the call: name the mismatch instead of failing silently."""

    fake_jira.status_names = ["To Do", "In Progress", "Done"]

    meta = service.jira_meta(workspace["id"])

    assert meta["status_hits"] == []
    assert meta["ok"] is False
    assert "To Do" in meta["statuses"]


def test_meta_tolerates_a_suffixed_workflow_name(service, workspace, fake_jira) -> None:
    """Real statuses carry suffixes; strict equality would cry wolf."""

    fake_jira.status_names = ["待设计(评审中)", "开发中"]

    meta = service.jira_meta(workspace["id"])

    assert meta["status_hits"] == ["待设计"]
    assert meta["ok"] is True
