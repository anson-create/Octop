"""Fixtures for the PM test suite."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.pm.migrate import run_pm_migrations
from octop.infra.pm.service import PMService


@pytest.fixture
def db(tmp_path: Path) -> Iterator[SqlitePool]:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    run_pm_migrations(pool)
    yield pool
    pool.close()


@pytest.fixture
def service(db: SqlitePool) -> PMService:
    return PMService(db)


@pytest.fixture
def workspace(service: PMService) -> dict:
    return service.create_workspace(name="默认工作区", owner_user_id=1)


@pytest.fixture
def requirement(service: PMService, workspace: dict) -> dict:
    return service.create_requirement(
        workspace_id=workspace["id"],
        title="报关单批量导入",
        raw_text=(
            "客户希望报关单支持批量导入，单次不超过 200 票，"
            "导入失败要能看到失败原因，涉及操作员与单证员两类角色。"
        ),
        source="jira",
        source_ref="CARGO-1234",
    )


@pytest.fixture
def task(service: PMService, requirement: dict) -> dict:
    return service.create_task(requirement_id=requirement["id"], owner_user_id=1)


_FAKE_ISSUES = {
    "CARGO-1234": {
        "key": "CARGO-1234",
        "title": "报关单批量导入",
        "description": "客户希望报关单批量导入，单次不超过 200 票，失败要能看到原因，涉及操作员与单证员。",
        "status": "In Progress",
        "status_category": "indeterminate",
        "assignee": "测试员",
        "reporter": "业务方",
        "priority": "High",
        "issue_type": "Story",
        "project": "CARGO",
        "resolution": "",
        "labels": ["报关"],
        # The functional area, as picked in Jira. Intake must carry this into the
        # evidence snapshot: it is the strongest hint about which page to derive.
        "components": ["报关单管理"],
        "custom_fields": [
            {"id": "customfield_10101", "name": "业务痛点", "text": "关务每天手工录入 200 票"},
        ],
        "comments": [
            {"author": "产品经理", "text": "这个在报关单列表页加个导入按钮", "created": ""},
        ],
        "attachments": [
            {"filename": "报关单列表-导入入口.png", "mime": "image/png", "size": "2048"},
        ],
        "environment": "",
        "created": "2026-10-01T09:00:00.000+0800",
        "updated": "2026-10-30T18:00:00.000+0800",
    },
    "CARGO-5678": {
        "key": "CARGO-5678",
        "title": "运价指数看板",
        "description": "按周展示运价指数，支持港口筛选。",
        "status": "To Do",
        "status_category": "new",
        "assignee": "测试员",
        "reporter": "业务方",
        "priority": "Medium",
        "issue_type": "Story",
        "project": "CARGO",
        "resolution": "",
        "labels": [],
        "created": "2026-10-02T09:00:00.000+0800",
        "updated": "2026-10-29T18:00:00.000+0800",
    },
}


class FakeJiraClient:
    """Stands in for the REST client so contract tests never touch a network.

    Mirrors the real client's surface (context manager, issue, search, probe) so
    the service code under test is unchanged.
    """

    def __init__(self, base_url: str = "https://jira.example.com") -> None:
        self.base_url = base_url
        self.closed = False
        self.issues = {key: dict(value) for key, value in _FAKE_ISSUES.items()}
        self.calls: list[tuple[str, str]] = []

    def __enter__(self) -> FakeJiraClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.closed = True

    def close(self) -> None:
        self.closed = True

    def myself(self) -> dict:
        return {"displayName": "测试员", "name": "tester"}

    def issue(self, key: str, *, with_context: bool = False) -> dict:
        from octop.infra.pm.ports.jira import JiraError

        self.calls.append(("issue", key))
        item = self.issues.get((key or "").strip().upper())
        if item is None:
            raise JiraError("Jira 返回 404：issue 不存在或地址不对")
        payload = dict(item)
        if with_context:
            # The real client asks Jira for these only at intake; the fake has to
            # answer with the same keys or intake silently loses its page hints.
            for name in ("components", "custom_fields", "comments", "attachments"):
                payload.setdefault(name, [])
            payload.setdefault("environment", "")
        return payload

    def search(self, jql: str, *, limit: int = 20) -> list[dict]:
        self.calls.append(("search", jql))
        return [dict(item) for item in list(self.issues.values())[:limit]]

    def probe(self) -> dict:
        return {
            "base_url": self.base_url,
            "user": "测试员",
            "account": "tester",
            "default_jql": "assignee = currentUser() AND resolution = Unresolved",
            "open_assigned": len(self.issues),
        }

    def projects(self) -> list[dict]:
        """Mirrors ``GET /rest/api/2/project``: key + name + type, nothing more."""

        self.calls.append(("projects", ""))
        return [
            {"key": "CARGO", "name": "CargoWare", "type": "software", "archived": False},
            {"key": "CCTOWER", "name": "调度塔", "type": "software", "archived": False},
            {"key": "OPS", "name": "运维支持", "type": "service_desk", "archived": False},
        ]


@pytest.fixture
def fake_jira(monkeypatch: pytest.MonkeyPatch) -> FakeJiraClient:
    """Replace the HTTP client on the service, keeping every other code path real."""
    client = FakeJiraClient()
    monkeypatch.setattr(
        PMService, "jira_client", lambda self, workspace_id: client, raising=True
    )
    return client
