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
