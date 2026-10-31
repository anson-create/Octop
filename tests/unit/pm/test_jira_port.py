"""The Jira client, exercised over a mock transport.

No test here reaches a network. What they pin is the part that would silently
ruin a delivery if it drifted: which failures become "unavailable" (the step
records 未检索 and keeps going) versus hard errors (the step blocks), and whether
the evidence envelope produced for an issue can actually be written to the
evidence table.
"""

from __future__ import annotations

import httpx
import pytest

from octop.infra.pm.domain import evidence as evidence_rules
from octop.infra.pm.ports.jira import (
    JiraClient,
    JiraError,
    JiraPort,
    PortUnavailable,
    issue_to_summary,
)

ISSUE = {
    "key": "CCTOWER-3597",
    "fields": {
        "summary": "派单供应商列表新增就绪状态筛选",
        "description": "供应商列表需要按就绪状态筛选，未就绪的行禁用开关并显示原因。",
        "status": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
        "assignee": {"displayName": "张三"},
        "reporter": {"displayName": "李四"},
        "priority": {"name": "High"},
        "issuetype": {"name": "Story"},
        "project": {"key": "CCTOWER"},
        "labels": ["派单", "供应商"],
        "created": "2026-10-01T09:00:00.000+0800",
        "updated": "2026-10-30T18:00:00.000+0800",
    },
}


def _client(handler) -> JiraClient:
    return JiraClient(
        "https://jira.example.com",
        "pat-token",
        transport=httpx.MockTransport(handler),
    )


def test_search_returns_normalised_issues() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization", "")
        seen["jql"] = request.url.params.get("jql", "")
        return httpx.Response(200, json={"issues": [ISSUE], "total": 1})

    with _client(handler) as client:
        issues = client.search("assignee = currentUser()", limit=5)

    assert seen["path"] == "/rest/api/2/search"
    # A Server/DC PAT is a bearer token; sending Basic here would 401 in real life.
    assert seen["auth"] == "Bearer pat-token"
    assert seen["jql"] == "assignee = currentUser()"
    assert issues[0]["key"] == "CCTOWER-3597"
    assert issues[0]["title"] == "派单供应商列表新增就绪状态筛选"
    assert issues[0]["status"] == "In Progress"
    assert issues[0]["assignee"] == "张三"
    assert issues[0]["labels"] == ["派单", "供应商"]


def test_bad_credentials_are_reported_as_unavailable() -> None:
    """A rotated PAT must degrade to "未检索", not crash the whole tick."""
    unauthorized = lambda request: httpx.Response(401, json={"errorMessages": ["nope"]})  # noqa: E731
    with _client(unauthorized) as client, pytest.raises(PortUnavailable):
        client.search("assignee = currentUser()")


def test_missing_issue_is_a_hard_error() -> None:
    """A 404 means "this ticket does not exist" — intake must refuse, not degrade."""
    missing = lambda request: httpx.Response(404, json={"errorMessages": ["gone"]})  # noqa: E731
    with _client(missing) as client, pytest.raises(JiraError):
        client.issue("CCTOWER-0000")


def test_timeout_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with _client(handler) as client, pytest.raises(PortUnavailable):
        client.search("anything")


def test_html_interception_is_reported_clearly() -> None:
    """A gateway login page returns 200 with HTML; that must not parse as JSON."""
    with _client(
        lambda request: httpx.Response(200, text="<html>login</html>")
    ) as client, pytest.raises(JiraError):
        client.search("anything")


def test_adf_description_is_flattened_to_prose() -> None:
    adf = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "需要有筛选"}],
            },
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "未就绪时禁用"}],
            },
        ],
    }
    summary = issue_to_summary(
        {"key": "X-1", "fields": {"summary": "s", "description": adf}}
    )
    assert "需要有筛选" in summary["description"]
    assert "未就绪时禁用" in summary["description"]
    assert "content" not in summary["description"]


def test_port_envelope_passes_the_evidence_validator() -> None:
    """The only Jira locator kind the schema accepts is jira_field."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"issues": [ISSUE], "total": 1})

    port = JiraPort(lambda: _client(handler))
    result = port.fetch(query="供应商", task_ref="PM-1")

    assert result.ok
    assert result.envelopes
    envelope = result.envelopes[0]
    assert envelope["locator"]["kind"] == "jira_field"
    assert envelope["locator"]["issue"] == "CCTOWER-3597"
    assert envelope["locator"]["field"] == "summary"
    assert evidence_rules.validate({"port": "jira", **envelope}) == ()


def test_port_reports_unavailable_instead_of_inventing() -> None:
    """No connectivity must not fabricate a ticket."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    port = JiraPort(lambda: _client(handler))
    result = port.fetch(query="供应商", task_ref="PM-1")

    assert not result.ok
    assert result.envelopes == []
    assert result.unavailable_reason
    assert result.trace_sources() == ["jira:未检索"]


def test_query_is_quoted_so_it_cannot_rewrite_the_jql() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["jql"] = request.url.params.get("jql", "")
        return httpx.Response(200, json={"issues": [], "total": 0})

    port = JiraPort(lambda: _client(handler))
    port.fetch(query='x" OR project = SECRET OR "', task_ref="PM-1")

    assert '\\"' in captured["jql"]
    assert captured["jql"].count('"') % 2 == 0


def test_components_are_read_from_the_project_they_belong_to() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            json=[
                {"id": "10102", "name": "运价指数", "lead": {"displayName": "王五"}},
                {"id": "10101", "name": "费用管理"},
                {"id": "", "name": "   "},
            ],
        )

    with _client(handler) as client:
        components = client.components("CCTOWER")

    assert seen["path"] == "/rest/api/2/project/CCTOWER/components"
    assert [item["name"] for item in components] == ["费用管理", "运价指数"]
    assert components[1]["lead"] == "王五"
    assert components[0]["lead"] == ""


def test_a_project_without_components_is_an_empty_answer_not_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    with _client(handler) as client:
        assert client.components("CCTOWER") == []


def test_count_reports_the_total_while_fetching_a_single_issue() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["jql"] = request.url.params.get("jql", "")
        seen["max"] = request.url.params.get("maxResults", "")
        return httpx.Response(200, json={"issues": [ISSUE], "total": 42})

    with _client(handler) as client:
        assert client.count('project = "CCTOWER" AND component = "费用管理"') == 42

    assert seen["max"] == "1"
    assert "费用管理" in str(seen["jql"])

