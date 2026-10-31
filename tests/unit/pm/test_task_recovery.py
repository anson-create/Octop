"""Task recovery: a blocked task must have a way back without a manual reflow."""

from __future__ import annotations

import pytest

from octop.infra.pm.service import PMService


def test_blocked_task_can_be_resumed(service: PMService, requirement: dict) -> None:
    task = service.create_task(requirement_id=requirement["id"], owner_user_id=1)

    first = service.tick(task["id"])
    assert first["step"] == "intake" and first["step_status"] == "passed"

    # No model executor attached here, so the next step fails and blocks the task.
    second = service.tick(task["id"])
    assert second["status"] == "blocked", second
    assert second["error_code"] == "executor_unavailable"
    assert service.task_detail(task["id"])["status"] == "blocked"

    service.resume(task["id"])
    assert service.task_detail(task["id"])["status"] == "queued"


def test_resume_rejects_a_task_that_is_not_stuck(service: PMService, requirement: dict) -> None:
    task = service.create_task(requirement_id=requirement["id"], owner_user_id=1)
    with pytest.raises(Exception) as excinfo:
        service.resume(task["id"])
    assert "queued" in str(excinfo.value) or "not paused" in str(excinfo.value)
