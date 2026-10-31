"""HTTP contract: every endpoint answers, permissions bite, errors carry codes."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from octop.api.app import _install_exception_handlers
from octop.api.deps import current_user, get_server
from octop.api.routers.pm import router as pm_router
from octop.infra.pm.service import PMService


class FakeUser:
    def __init__(self, permissions: list[str], *, is_admin: bool = False, uid: int = 1) -> None:
        self.permissions = permissions
        self.is_admin = is_admin
        self.id = uid
        self.username = f"user{uid}"


@pytest.fixture
def actor() -> dict[str, FakeUser]:
    return {"user": FakeUser(["pm"])}


@pytest.fixture
def client(
    service: PMService, actor: dict[str, FakeUser], fake_jira: object
) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(pm_router, prefix="/api/pm")
    _install_exception_handlers(app)
    server = SimpleNamespace(pm_service=service)
    app.dependency_overrides[get_server] = lambda: server
    app.dependency_overrides[current_user] = lambda: actor["user"]
    with TestClient(app) as test_client:
        yield test_client


def _grant_decide(actor: dict[str, FakeUser]) -> None:
    actor["user"] = FakeUser(["pm", "pm_decide"])


def _seed(client: TestClient) -> tuple[int, int, int]:
    workspace = client.post("/api/pm/workspaces", json={"name": "默认工作区"}).json()
    # Intake is by reference: the server reads the issue, the browser only names it.
    requirement = client.post(
        "/api/pm/requirements",
        json={
            "workspace_id": workspace["id"],
            "issue_key": "CARGO-1234",
        },
    ).json()
    task = client.post("/api/pm/tasks", json={"requirement_id": requirement["id"]}).json()
    return workspace["id"], requirement["id"], task["id"]


def test_health_is_open_to_pm_users(client: TestClient) -> None:
    body = client.get("/api/pm/health").json()
    assert body["status"] in ("ok", "degraded")
    assert body["migration"]["version"] >= 1


def test_permission_is_enforced(client: TestClient, actor: dict[str, FakeUser]) -> None:
    actor["user"] = FakeUser([])
    response = client.get("/api/pm/workspaces")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_decide_requires_the_extra_key(client: TestClient) -> None:
    _, _, task_id = _seed(client)
    response = client.post(f"/api/pm/tasks/{task_id}/reflow", json={})
    assert response.status_code == 403
    assert response.json()["error"]["details"]["permission"] == "pm_decide"


def test_workspace_and_requirement_flow(client: TestClient) -> None:
    workspace_id, requirement_id, task_id = _seed(client)

    listed = client.get("/api/pm/workspaces").json()
    assert [item["id"] for item in listed["items"]] == [workspace_id]

    detail = client.get(f"/api/pm/requirements/{requirement_id}").json()
    assert detail["public_id"].startswith("REQ-")
    assert detail["task"]["id"] == task_id

    tasks = client.get("/api/pm/tasks", params={"owner": "me"}).json()
    assert tasks["total"] == 1

    change = client.post(
        f"/api/pm/requirements/{requirement_id}/changes",
        json={"change_text": "补一条：导入后要能撤销"},
    ).json()
    assert change["accepted"] is False


def test_requirement_idempotency(client: TestClient) -> None:
    workspace_id, _, _ = _seed(client)
    payload = {
        "workspace_id": workspace_id,
        "issue_key": "CARGO-5678",
        "client_token": "tok-abc",
    }
    first = client.post("/api/pm/requirements", json=payload).json()
    second = client.post("/api/pm/requirements", json=payload).json()
    assert first["id"] == second["id"]


def test_duplicate_task_is_conflict(client: TestClient) -> None:
    _, requirement_id, _ = _seed(client)
    response = client.post("/api/pm/tasks", json={"requirement_id": requirement_id})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PM_CONFLICT"


def test_unknown_requirement_is_404(client: TestClient) -> None:
    response = client.get("/api/pm/requirements/9999")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_tick_and_parked_conflict(client: TestClient) -> None:
    _, _, task_id = _seed(client)
    first = client.post(f"/api/pm/tasks/{task_id}/tick", json={}).json()
    assert first["step"] == "intake" and first["step_status"] == "passed"
    second = client.post(f"/api/pm/tasks/{task_id}/tick", json={}).json()
    assert second["status"] == "blocked"
    assert client.post(f"/api/pm/tasks/{task_id}/pause").json()["status"] == "paused"
    assert client.post(f"/api/pm/tasks/{task_id}/resume").json()["status"] == "queued"

    steps = client.get(f"/api/pm/tasks/{task_id}/steps").json()["items"]
    assert len(steps) == 13
    trace = client.get(f"/api/pm/steps/{steps[0]['id']}/trace").json()
    assert trace["sources"] == {"human": 1}
    assert trace["evidence"]


def test_reject_note_length_is_enforced(client: TestClient, actor: dict[str, FakeUser]) -> None:
    _grant_decide(actor)
    _, _, task_id = _seed(client)
    client.post(f"/api/pm/tasks/{task_id}/tick", json={})
    step_id = client.get(f"/api/pm/tasks/{task_id}/steps").json()["items"][0]["id"]
    short = client.post(f"/api/pm/steps/{step_id}/reject", json={"note": "不行"})
    assert short.status_code == 400
    assert short.json()["error"]["code"] == "PM_VALIDATION"
    ok = client.post(
        f"/api/pm/steps/{step_id}/reject",
        json={"note": "进件摘要缺少客户名称与影响范围，请补全后重跑"},
    )
    assert ok.status_code == 200
    assert ok.json()["from_seq"] == 1


def test_decision_centre_and_answer(client: TestClient, actor: dict[str, FakeUser]) -> None:
    from octop.infra.pm.repos._base import iso_in

    repo = actor  # keep signature symmetrical for readers
    del repo
    service: PMService = client.app.dependency_overrides[get_server]().pm_service  # type: ignore[attr-defined]
    workspace_id, requirement_id, task_id = _seed(client)
    del workspace_id, requirement_id
    card = service.repos.decisions.create(
        task_id=task_id,
        level="L2",
        question="是否按建议推进？",
        options=[{"key": "yes", "label": "按建议"}, {"key": "no", "label": "调整"}],
        timeout_at=iso_in(3600),
    )
    listed = client.get("/api/pm/decisions", params={"status": "pending", "mine": "true"}).json()
    assert listed["items"][0]["id"] == card.id

    _grant_decide(actor)
    short = client.post(
        f"/api/pm/decisions/{card.id}/answer", json={"selected_key": "yes", "note": "短"}
    )
    assert short.status_code == 400
    invalid_option = client.post(
        f"/api/pm/decisions/{card.id}/answer",
        json={"selected_key": "maybe", "note": "这个选项根本不在卡片里，应当被拒绝"},
    )
    assert invalid_option.status_code == 400

    answered = client.post(
        f"/api/pm/decisions/{card.id}/answer",
        json={"selected_key": "yes", "note": "范围已确认，按建议推进并记录风险"},
    )
    assert answered.status_code == 200
    assert answered.json()["decision"]["status"] == "answered"
    again = client.post(
        f"/api/pm/decisions/{card.id}/answer",
        json={"selected_key": "yes", "note": "重复提交应当返回冲突而不是覆盖"},
    )
    assert again.status_code == 409


def test_reviews_and_bundle(client: TestClient, actor: dict[str, FakeUser]) -> None:
    _grant_decide(actor)
    _, _, task_id = _seed(client)
    for perspective in ("business", "tech", "customer"):
        response = client.post(
            "/api/pm/reviews",
            json={"task_id": task_id, "perspective": perspective, "verdict": "pass"},
        )
        assert response.status_code == 200
    reviews = client.get("/api/pm/reviews", params={"task_id": task_id}).json()
    assert reviews["complete"] is True
    bundle = client.get(f"/api/pm/tasks/{task_id}/export-bundle").json()
    assert task_id and "交付包" in bundle["content"]


def test_integrations_never_return_credentials(client: TestClient, actor: dict[str, FakeUser]) -> None:
    workspace_id, _, _ = _seed(client)
    _grant_decide(actor)
    created = client.post(
        "/api/pm/integrations",
        json={
            "workspace_id": workspace_id,
            "vendor": "jira",
            "credential_ref": "secret/jira-token",
            "mapping": {"issue_key": "customfield_1"},
        },
    ).json()
    assert created["credential_set"] is True
    listed = client.get("/api/pm/integrations", params={"workspace_id": workspace_id}).json()
    assert "credential_ref" not in listed["items"][0]
    probe = client.post(f"/api/pm/integrations/{created['id']}/probe").json()
    assert probe["probe_status"] == "configured"


def test_metrics_eval_and_sweep(client: TestClient, actor: dict[str, FakeUser]) -> None:
    workspace_id, _, _ = _seed(client)
    metrics = client.get(
        "/api/pm/metrics/daily",
        params={"workspace_id": workspace_id, "date_from": "2000-01-01", "date_to": "2999-01-01"},
    ).json()
    assert metrics["items"]

    _grant_decide(actor)
    eval_result = client.post("/api/pm/eval/run").json()
    assert eval_result["ok"] is True
    sweep = client.post("/api/pm/internal/sweep").json()
    assert sweep["due"] >= 0


def test_artifact_and_evidence_endpoints(client: TestClient) -> None:
    _, _, task_id = _seed(client)
    client.post(f"/api/pm/tasks/{task_id}/tick", json={})
    detail = client.get(f"/api/pm/tasks/{task_id}").json()
    artifact_id = detail["artifacts"][0]["id"]

    artifact = client.get(f"/api/pm/artifacts/{artifact_id}").json()
    assert artifact["body_inline"]
    conclusions = client.get(f"/api/pm/artifacts/{artifact_id}/conclusions").json()
    assert conclusions["items"][0]["evidence"]

    evidence_rows = client.get(f"/api/pm/steps/{detail['steps'][0]['id']}/trace").json()["evidence"]
    evidence_id = evidence_rows[0]["id"]
    evidence = client.get(f"/api/pm/evidence/{evidence_id}").json()
    assert evidence["locator"]["kind"] == "pm_record"

    exported = client.get(
        f"/api/pm/artifacts/{artifact_id}/export",
        params={"audience": "internal", "confirm": "true"},
    ).json()
    assert exported["ok"] is True and exported["content"]

    recheck = client.post(f"/api/pm/conclusions/{conclusions['items'][0]['id']}/recheck")
    assert recheck.status_code == 403  # needs pm_decide


def test_validation_error_shape(client: TestClient) -> None:
    response = client.post("/api/pm/workspaces", json={"name": ""})
    assert response.status_code == 422
