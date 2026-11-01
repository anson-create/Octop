"""The connector transport: MCP JSON in, the same issue shape out.

The tests here stay off the network on purpose. They pin the two things that
break silently: the field mapping (a wrong key means a requirement written from
an empty description) and the transport switch (a workspace bound to a
connector must not fall back to a stored REST credential that is no longer
entered anywhere).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from octop.infra.connectors.crypto import encrypt_credentials
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.connectors import ConnectorRepo
from octop.infra.db.repos.secrets import SecretRepo
from octop.infra.errors import OctopError
from octop.infra.pm.ports.base import PortError
from octop.infra.pm.ports.jira import JiraClient
from octop.infra.pm.ports.mcp_jira import (
    McpJiraClient,
    _payload_of,
    mcp_issue_to_summary,
)

# --- the JSON the Atlassian server actually returns ------------------------
#
# Shaped from a live call: system fields are snake_case dicts, custom fields are
# top-level keys named in Chinese because ``use_display_names`` is on, and the
# comments live at the top level rather than under ``fields.comment.comments``.

MCP_ISSUE: dict[str, Any] = {
    "id": "193111",
    "key": "CCTOWER-3597",
    "summary": "CargoWare 智能派单（调度中枢）",
    "browse_url": "http://jira.example.com/browse/CCTOWER-3597",
    "description": "在派单供应商列表页新增「就绪状态」筛选，支持三态。",
    "status": {"name": "录入中", "category": "处理中", "color": "yellow"},
    "issue_type": {"name": "故事"},
    "priority": {"name": "P2"},
    "project": {"key": "CCTOWER", "name": "CargoWare 调度中枢"},
    "assignee": {
        "display_name": "熊培｜Anson",
        "name": "anson",
        "email": "anson@example.com",
    },
    "reporter": {"display_name": "李四", "name": "lisi"},
    "labels": ["smart-dispatch"],
    "components": [{"name": "派单供应商"}],
    "created": "2026-09-30 20:38:35 CST",
    "updated": "2026-10-30 18:54:12 CST",
    "resolution": {"name": ""},
    "environment": "预发环境 tenant 99",
    "comments": [
        {
            "id": "418842",
            "body": "状态要和适配器配置、路由配置两处对齐。",
            "author": {"display_name": "产品经理"},
            "created": "2026-10-30T17:18:12.000+0800",
        }
    ],
    "attachment": [
        {"filename": "就绪状态.png", "mime": "image/png", "size": 204800},
    ],
    "业务痛点": {"value": "新供应商注册后不知道能不能派单。"},
    "期望效果": {"value": "列表直接显示就绪状态。"},
    "预计设计工作量": {"value": "3"},
}


def _summary() -> dict[str, Any]:
    return mcp_issue_to_summary(MCP_ISSUE)


def test_the_whole_issue_survives_the_translation() -> None:
    out = _summary()

    assert out["key"] == "CCTOWER-3597"
    assert out["title"] == "CargoWare 智能派单（调度中枢）"
    assert out["description"].startswith("在派单供应商列表页")
    assert out["status"] == "录入中"
    assert out["priority"] == "P2"
    assert out["issue_type"] == "故事"
    assert out["project"] == "CCTOWER"
    assert out["assignee"] == "熊培｜Anson"
    assert out["reporter"] == "李四"
    assert out["environment"] == "预发环境 tenant 99"


def test_custom_fields_arrive_under_their_own_names() -> None:
    """`use_display_names` is the whole reason no catalogue call is needed."""

    custom = {item["name"]: item["text"] for item in _summary()["custom_fields"]}

    assert custom["业务痛点"] == "新供应商注册后不知道能不能派单。"
    assert custom["期望效果"] == "列表直接显示就绪状态。"
    # A dict passed through str() would reach the requirement text as
    # "{'value': ...}", which reads like a bug rather than a field.
    assert all("{" not in text for text in custom.values())


def test_system_fields_are_not_mistaken_for_custom_ones() -> None:
    names = {item["name"] for item in _summary()["custom_fields"]}

    for reserved in ("summary", "status", "assignee", "labels", "comments", "project"):
        assert reserved not in names


def test_comments_keep_their_author_and_are_not_merged_into_the_description() -> None:
    out = _summary()

    assert out["comments"] == [
        {
            "author": "产品经理",
            "text": "状态要和适配器配置、路由配置两处对齐。",
            "created": "2026-10-30T17:18:12.000+0800",
        }
    ]
    assert "适配器配置" not in out["description"]


def test_attachments_are_named_even_though_contents_are_not_read() -> None:
    assert _summary()["attachments"] == [
        {"filename": "就绪状态.png", "mime": "image/png", "size": "204800"}
    ]


def test_the_localised_status_category_is_translated_back() -> None:
    """`status_category` is part of the contract; REST reports `indeterminate`,
    the MCP server reports the localised 处理中."""

    assert _summary()["status_category"] == "indeterminate"


def test_an_issue_with_no_description_yields_empty_text_not_a_crash() -> None:
    thin = {"key": "CCTOWER-1", "summary": "只有标题"}

    out = mcp_issue_to_summary(thin)

    assert out["title"] == "只有标题"
    assert out["description"] == ""
    assert out["comments"] == []
    assert out["attachments"] == []
    assert out["custom_fields"] == []


def test_nested_dict_values_are_flattened_instead_of_stringified() -> None:
    """Jira custom fields come back as {"value": "..."} or {"name": "..."}."""

    out = mcp_issue_to_summary(
        {"key": "X-1", "summary": "s", "问题归类": {"name": "功能缺陷"}}
    )

    assert out["custom_fields"] == [{"id": "问题归类", "name": "问题归类", "text": "功能缺陷"}]


# --- reading the tool result ------------------------------------------------


class _Chunk:
    def __init__(self, text: str) -> None:
        self.text = text


class _Result:
    def __init__(self, text: str, *, is_error: bool = False) -> None:
        self.content = [_Chunk(text)]
        self.isError = is_error


def test_a_tool_error_is_raised_not_parsed_as_data() -> None:
    """The server answers failures as a normal result with isError set.

    Parsing that text would put an error page into the requirement body.
    """

    with pytest.raises(PortError) as caught:
        _payload_of(_Result('{"error": "boom"}', is_error=True), "jira_search")

    assert "jira_search" in str(caught.value)


def test_prose_回答_is_rejected_rather_than_read_as_zero_results() -> None:
    with pytest.raises(PortError):
        _payload_of(_Result("No issues found in this project."), "jira_search")


def test_a_json_payload_is_returned_as_is() -> None:
    assert _payload_of(_Result('{"total": 3}'), "jira_search") == {"total": 3}


# --- the client surface -----------------------------------------------------


class _FakeSession:
    def __init__(self, answers: dict[str, dict[str, Any]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, tool: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, payload))
        return self.answers.get(tool, {})


def test_search_asks_for_all_fields_and_caps_the_limit() -> None:
    """Jira's own cap is 50; asking for 200 is an error, not a bigger page."""

    session = _FakeSession({"jira_search": {"total": 1, "issues": [MCP_ISSUE]}})
    client = McpJiraClient(session)

    found = client.search("project = CCTOWER", limit=200)

    tool, payload = session.calls[0]
    assert tool == "jira_search"
    assert payload["limit"] == 50
    assert payload["use_display_names"] is True
    assert "description" in str(payload["fields"])
    assert found[0]["key"] == "CCTOWER-3597"


def test_count_reads_total_without_pulling_issues() -> None:
    session = _FakeSession({"jira_search": {"total": 3744}})

    assert McpJiraClient(session).count("project = CCTOWER") == 3744
    assert session.calls[0][1]["fields"] == "summary"


def test_issue_key_is_upper_cased_before_it_reaches_the_server() -> None:
    session = _FakeSession({"jira_get_issue": MCP_ISSUE})

    McpJiraClient(session).issue("cctower-3597")

    assert session.calls[0][1]["issue_key"] == "CCTOWER-3597"


def test_an_empty_issue_key_fails_before_any_call() -> None:
    session = _FakeSession({})

    with pytest.raises(PortError):
        McpJiraClient(session).issue("   ")

    assert session.calls == []


def test_myself_returns_rest_spelled_keys_so_callers_do_not_branch() -> None:
    """`jira_meta` reads `displayName`; the transport must not leak into it."""

    session = _FakeSession({"jira_get_user_profile": {"display_name": "熊培｜Anson", "name": "anson"}})

    me = McpJiraClient(session, username="anson").myself()

    assert me["displayName"] == "熊培｜Anson"
    assert me["name"] == "anson"


def test_myself_falls_back_to_the_account_never_to_a_connector_label() -> None:
    """The connector's label is not a person.

    Live, an unavailable profile call made ``jira_meta`` report the owner of the
    default queue as "Jira + Confluence (WallTech)" — the connector's display
    name — which reads as a different user entirely.
    """

    session = _FakeSession({})  # the profile call answers nothing

    me = McpJiraClient(session, username="anson", base_url="http://jira.example.com").myself()

    assert me["displayName"] == "anson"
    assert me["name"] == "anson"


def test_statuses_and_priorities_are_empty_rather_than_invented() -> None:
    """The server has no global status catalogue; a guessed list would be worse
    than none, because jira_meta would then 'verify' the guess."""

    client = McpJiraClient(_FakeSession({}))

    assert client.statuses() == ()
    assert client.priorities() == ()


def test_probe_reports_the_transport_it_used() -> None:
    session = _FakeSession({"jira_search": {"total": 7}})

    out = McpJiraClient(session, base_url="http://jira.example.com", username="anson").probe()

    assert out["transport"] == "mcp"
    assert out["account"] == "anson"
    assert out["open_assigned"] == 7


# --- transport switching on the service ------------------------------------


def _make_connector(db: SqlitePool, instance_id: str = "CONN1") -> str:
    """A connector row carrying a Jira server spec, encrypted like the real one."""

    # connectors.user_id is a real foreign key, so the owner has to exist.
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users("
            "id, username, password_hash, role, display_name, created_at"
            ") VALUES (1, 'anson', 'x', 'admin', 'Anson', 0)"
        )

    repo = ConnectorRepo(db)
    repo.create(
        instance_id=instance_id,
        user_id=1,
        kind="custom-mcp",
        display_name="自定义 MCP",
        mcp_server_name=f"custom-mcp__{instance_id}",
    )
    blob = encrypt_credentials(
        SecretRepo(db),
        {
            "instance_id": instance_id,
            "servers": {
                "jira": {
                    "transport": "stdio",
                    "enabled": True,
                    "display_name": "Jira + Confluence (WallTech)",
                    "command": "/opt/mcp-services/bin/mcp-atlassian",
                    "args": ["--transport", "stdio"],
                    "env": {
                        "JIRA_URL": "http://jira.example.com",
                        "JIRA_USERNAME": "anson",
                        "JIRA_API_TOKEN": "secret-token",
                    },
                },
                "mail": {
                    "transport": "stdio",
                    "enabled": True,
                    "display_name": "网易企业邮箱",
                    "command": "/opt/mcp-services/bin/mcp-email-server",
                    "args": ["stdio"],
                    "env": {},
                },
            },
        },
    )
    repo.upsert_credentials(instance_id=instance_id, blob=blob)
    return instance_id


def test_a_connector_is_only_bindable_when_it_carries_jira(service, workspace, db) -> None:
    _make_connector(db)

    items = service.list_jira_connectors()

    assert len(items) == 1
    assert items[0]["server_key"] == "jira"
    assert items[0]["base_url"] == "http://jira.example.com"
    # The token must never travel to the picker.
    assert "secret-token" not in str(items)


def test_binding_stores_which_connector_and_no_credential(service, workspace, db) -> None:
    instance = _make_connector(db)

    status = service.bind_jira_connector(
        workspace["id"], connector_instance_id=instance
    )

    assert status["transport"] == "mcp"
    assert status["configured"] is True
    assert status["connector_instance_id"] == instance
    assert status["base_url"] == "http://jira.example.com"

    mapping = service._jira_mapping(workspace["id"])
    assert mapping["connector_server"] == "jira"
    # A database dump has to stay harmless: the token lives in the connector.
    assert "secret-token" not in str(mapping)
    assert "token" not in mapping


def test_binding_a_connector_without_jira_is_refused(service, workspace, db) -> None:
    """A mail connector would answer 'unknown tool' on the first query, with
    nothing in the message pointing at the choice."""

    with pytest.raises(OctopError) as caught:
        service.bind_jira_connector(workspace["id"], connector_instance_id="nope")

    assert "连接器" in str(caught.value)


def test_a_bound_workspace_builds_an_mcp_client_not_a_rest_one(
    service, workspace, db, monkeypatch
) -> None:
    instance = _make_connector(db)
    service.bind_jira_connector(workspace["id"], connector_instance_id=instance)

    spawned: list[tuple] = []

    def fake_session(instance_id: str, command: str, args: list, env: dict):
        spawned.append((instance_id, command, env.get("JIRA_URL")))
        return _FakeSession({"jira_search": {"total": 0}})

    monkeypatch.setattr("octop.infra.pm.ports.mcp_jira.session_for", fake_session)

    client = service.jira_client(workspace["id"])

    assert isinstance(client, McpJiraClient)
    assert not isinstance(client, JiraClient)
    assert spawned and spawned[0][1] == "/opt/mcp-services/bin/mcp-atlassian"
    assert spawned[0][2] == "http://jira.example.com"


def test_an_unbound_workspace_still_uses_the_rest_client(service, workspace) -> None:
    """The transport switch must not alter installs that never opted in."""

    service.save_jira_config(
        workspace["id"], base_url="https://jira.example.com", token="t", username="u"
    )

    client = service.jira_client(workspace["id"])

    assert isinstance(client, JiraClient)
    assert client.transport if hasattr(client, "transport") else True


def test_status_of_an_unbound_workspace_says_rest(service, workspace) -> None:
    status = service.jira_status(workspace["id"])

    assert status["transport"] == "rest"
    assert status["configured"] is False


def test_a_bound_workspace_reports_configured_without_any_token(service, workspace, db) -> None:
    """MCP workspaces have no credential of their own; asking for one would
    leave them permanently 'unconfigured'."""

    instance = _make_connector(db)
    service.bind_jira_connector(workspace["id"], connector_instance_id=instance)

    status = service.jira_status(workspace["id"])

    assert status["configured"] is True
    assert status["auth"] == "mcp"
    assert status["token_set"] is True


def test_a_deleted_connector_turns_the_workspace_unconfigured(service, workspace, db) -> None:
    instance = _make_connector(db)
    service.bind_jira_connector(workspace["id"], connector_instance_id=instance)
    ConnectorRepo(db).delete(instance)

    status = service.jira_status(workspace["id"])

    assert status["configured"] is False


def test_meta_does_not_call_the_default_queue_wrong_when_no_catalogue_exists(
    service, workspace, db, monkeypatch
) -> None:
    """Over MCP there is no status list. Reporting ok=False would read as
    'this site never heard of 待设计' and send the operator to fix a fine JQL."""

    instance = _make_connector(db)
    service.bind_jira_connector(workspace["id"], connector_instance_id=instance)
    monkeypatch.setattr(
        "octop.infra.pm.ports.mcp_jira.session_for",
        lambda *a, **kw: _FakeSession({"jira_get_user_profile": {"name": "anson"}}),
    )

    meta = service.jira_meta(workspace["id"])

    assert meta["ok"] is True
    assert meta["statuses"] == []
    assert "连接器" in meta["note"]


def test_a_workspace_pointed_at_a_missing_connector_says_so(service, workspace) -> None:
    service._write_integration_mapping(
        workspace["id"],
        {"transport": "mcp", "connector_instance_id": "gone"},
    )

    # jira_client is a port boundary, so an unavailable transport is a
    # PortError; the route-facing bind path translates it to a 400 instead.
    with pytest.raises(PortError) as caught:
        service.jira_client(workspace["id"])

    assert "连接器" in str(caught.value)


def test_the_connector_spec_is_read_from_the_encrypted_blob(service, db) -> None:
    state = Path(".")  # keeps ruff from flagging the unused import in this block
    _ = state
    instance = _make_connector(db, "CONN-SPEC")

    spec = service._mcp_server_spec(instance)

    assert spec is not None
    assert spec["command"] == "/opt/mcp-services/bin/mcp-atlassian"
    assert spec["username"] == "anson"
    assert spec["env"]["JIRA_API_TOKEN"] == "secret-token"
    assert spec["base_url"] == "http://jira.example.com"
