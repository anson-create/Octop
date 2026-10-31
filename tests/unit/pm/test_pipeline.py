"""Pipeline behaviour: full run, decision cards, restart survival, rejection."""

from __future__ import annotations

import json

import pytest

from octop.infra.pm.runtime.executor import DeterministicExecutor, StepContext, StepOutput

PROTOTYPE_HTML = (
    "<html><head><style>.btn{color:#c00}</style></head><body>"
    "<form><input name='file'><button class='btn'>导入</button></form>"
    "<table><tr><td>1</td></tr></table></body></html>"
)

PRD_BODY = json.dumps(
    {
        "items": [
            {
                "title": "批量导入",
                "user_value": "单证员不必逐票录入",
                "acceptance": "当上传文件时则单次不超过 200 票，且失败行给出原因",
            }
        ]
    },
    ensure_ascii=False,
)


def _evidence(step: str) -> dict:
    return {
        "source_type": "code",
        "locator": {"kind": "code_span", "repo": "h5", "path": "src/x.vue", "line": [1, 3]},
        "excerpt": f"{step}: 批量导入实现片段",
        "visibility": "internal",
    }


class AllStepsExecutor:
    """Stand-in for the model-backed steps: deterministic content, real envelopes."""

    def __init__(self) -> None:
        self._rule = DeterministicExecutor()

    def supports(self, step_key: str) -> bool:
        return True

    def run(self, ctx: StepContext) -> StepOutput:
        if self._rule.supports(ctx.step_key):
            return self._rule.run(ctx)
        if ctx.step_key in ("decision", "prototype_gate"):
            return StepOutput(
                status="needs_decision",
                decision={
                    "level": ctx.decision_level,
                    "question": f"{ctx.step_key}: 是否按建议推进？",
                    "recommended_key": "yes",
                    "options": [
                        {"key": "yes", "label": "按建议", "pros": "快", "cons": "无"},
                        {"key": "no", "label": "调整", "pros": "稳", "cons": "慢"},
                    ],
                },
                trace={"sources": ["stub"]},
            )
        if ctx.step_key == "prototype":
            return StepOutput(
                status="passed",
                artifact={
                    "kind": "prototype_html",
                    "title": "批量导入原型",
                    "fidelity": "partial",
                    "fidelity_note": "分页与权限态未实现",
                    "origin": "ai",
                    "body": PROTOTYPE_HTML,
                },
                evidence=[_evidence(ctx.step_key)],
                conclusions=[
                    {"text": "原型含导入表单与结果表格", "status": "evidenced", "evidence_indexes": [0]}
                ],
                trace={"sources": ["code"]},
            )
        if ctx.step_key == "requirement_analysis":
            return StepOutput(
                status="passed",
                artifact={
                    "kind": "document",
                    "title": "需求条目",
                    "fidelity": "exact",
                    "origin": "ai",
                    "body": PRD_BODY,
                },
                evidence=[_evidence(ctx.step_key)],
                conclusions=[
                    {"text": "拆出 1 条需求条目", "status": "evidenced", "evidence_indexes": [0]}
                ],
                trace={"sources": ["jira"]},
            )
        return StepOutput(
            status="passed",
            artifact={
                "kind": "document",
                "title": f"{ctx.step_key} 产物",
                "fidelity": "exact",
                "origin": "ai",
                "body": f"{ctx.step_key} 正文",
            },
            evidence=[_evidence(ctx.step_key)],
            conclusions=[
                {"text": f"{ctx.step_key} 结论", "status": "evidenced", "evidence_indexes": [0]}
            ],
            trace={"sources": ["code", "kb"]},
        )


@pytest.fixture
def wired(service):
    service.pipeline.set_executor(AllStepsExecutor())
    return service


def _drive(service, task_id: int, *, max_rounds: int = 30) -> dict:
    """Advance until the task is delivered or stuck, answering cards as they appear."""
    answered = 0
    for _ in range(max_rounds):
        detail = service.task_detail(task_id)
        if detail["status"] == "delivered":
            return detail
        if detail["status"] in ("blocked", "paused"):
            return detail
        if detail["pending_decisions"]:
            card = detail["pending_decisions"][0]
            service.answer_decision(
                card["id"],
                selected_key="yes",
                note="范围已确认，按建议推进，风险已知",
                user_id=1,
            )
            answered += 1
            continue
        service.tick(task_id)
    detail = service.task_detail(task_id)
    detail["answered"] = answered
    return detail


def test_full_run_reaches_delivered(wired, task) -> None:
    service = wired
    for perspective in ("business", "tech", "customer"):
        service.submit_review(task_id=task["id"], perspective=perspective, verdict="pass")
    detail = _drive(service, task["id"])
    assert detail["status"] == "delivered", detail["steps"]
    assert all(step["status"] == "passed" for step in detail["steps"])
    assert detail["artifacts"], "deliverable artifacts expected"
    assert detail["conclusions"]["total"] > 0
    assert detail["evidence_count"] > 0


def test_decision_card_parks_the_task(wired, task) -> None:
    service = wired
    for _ in range(30):
        detail = service.task_detail(task["id"])
        if detail["pending_decisions"]:
            break
        if detail["status"] == "blocked":
            pytest.fail("task blocked before reaching a decision card")
        service.tick(task["id"])
    detail = service.task_detail(task["id"])
    assert detail["status"] == "waiting_human"
    card = detail["pending_decisions"][0]
    assert card["level"] in ("L1", "L2", "L3")
    assert card["timeout_at"]
    # ticking a parked task is a conflict, not a silent no-op
    with pytest.raises(Exception) as excinfo:
        service.tick(task["id"])
    assert getattr(excinfo.value, "status", None) == 409


def test_card_survives_a_restart(wired, task, db) -> None:
    service = wired
    for _ in range(30):
        detail = service.task_detail(task["id"])
        if detail["pending_decisions"]:
            break
        service.tick(task["id"])
    card = service.task_detail(task["id"])["pending_decisions"][0]

    # Simulate a process restart: a brand-new service on the same database.
    from octop.infra.pm.service import PMService

    revived = PMService(db)
    revived.pipeline.set_executor(AllStepsExecutor())
    still_there = revived.list_decisions(status="pending", task_id=task["id"])
    assert still_there["items"][0]["id"] == card["id"]

    result = revived.answer_decision(
        card["id"], selected_key="yes", note="重启后补答，理由写清楚", user_id=1
    )
    assert result["decision"]["status"] == "answered"
    assert result["pipeline"]["advanced"] is True


def test_reject_requires_a_reason_and_resets_from_that_step(wired, task) -> None:
    service = wired
    service.tick(task["id"])
    steps = service.task_steps(task["id"])
    first = steps[0]
    assert first["status"] == "passed"

    with pytest.raises(Exception) as excinfo:
        service.reject_step(first["id"], note="太短", user_id=1)
    assert getattr(excinfo.value, "status", None) == 400

    service.reject_step(first["id"], note="进件摘要遗漏了客户名称与影响范围，请补全", user_id=1)
    steps = service.task_steps(task["id"])
    assert steps[0]["status"] == "pending"
    assert service.task_detail(task["id"])["status"] == "queued"
    trace = service.step_trace(first["id"])["trace"]
    assert "客户名称" in trace["human_note"]


def test_unregistered_step_blocks_instead_of_faking(service, task) -> None:
    """No executor for link_expand → explicit failure, never a fabricated artifact."""
    service.tick(task["id"])
    result = service.tick(task["id"])
    assert result["status"] == "blocked"
    assert result["error_code"] == "executor_unavailable"
    detail = service.task_detail(task["id"])
    assert detail["artifacts"], "only the real intake artifact should exist"
    assert all(item["origin"] == "rule" for item in detail["artifacts"])


def test_pause_and_resume(service, task) -> None:
    service.pause(task["id"])
    assert service.task_detail(task["id"])["status"] == "paused"
    service.resume(task["id"])
    assert service.task_detail(task["id"])["status"] == "queued"


def test_export_blocks_source_paths_in_customer_version(wired, task) -> None:
    service = wired
    task_id = task["id"]

    dirty = service.repos.artifacts.create(
        task_id=task_id,
        kind="document",
        audience="customer",
        origin="ai",
        title="客户版方案",
        body_inline="实现细节见 src/order.vue 的 Importer 组件，字段取自 OMS_BOOKING。",
    )
    blocked = service.export_artifact(dirty.id, audience="customer", confirm=True)
    assert blocked["ok"] is False
    assert blocked["content"] is None
    assert {hit["rule"] for hit in blocked["hits"]} >= {"E-EXPORT-02a", "E-EXPORT-02b"}

    clean = service.repos.artifacts.create(
        task_id=task_id,
        kind="document",
        audience="customer",
        origin="ai",
        title="客户版方案 v2",
        body_inline="单次导入上限 200 票，失败行返回原因。",
    )
    allowed = service.export_artifact(clean.id, audience="customer", confirm=True)
    assert allowed["ok"] is True
    assert allowed["content"] and "200 票" in allowed["content"]

    preview = service.export_artifact(clean.id, audience="customer", confirm=False)
    assert preview["ok"] is True and preview["content"] is None and preview["preview_only"] is True


def test_metrics_and_eval_endpoints_work(wired, workspace, task) -> None:
    service = wired
    service.tick(task["id"])
    metrics = service.metrics_daily(
        workspace_id=workspace["id"], date_from="2000-01-01", date_to="2999-01-01"
    )
    keys = {item["metric_key"] for item in metrics["items"]}
    assert "conclusion_completeness" in keys

    service.repos.eval_cases.upsert(
        name="adv-missing-fields",
        kind="adversarial",
        input_payload={"title": "太短", "raw_text": "加个按钮"},
        expect_payload={"status": "passed", "gaps_contain": ["原文过短"]},
    )
    result = service.run_eval()
    assert result["total"] >= 1
    assert result["ok"] is True, result["results"]


def test_sweep_auto_answers_l1_and_blocks_on_expired_l2(wired, task) -> None:
    """L1 auto-decides at timeout; an unanswered L2 expires into a blocked task."""
    from octop.infra.pm.repos._base import iso_in

    service = wired
    decision_repo = service.repos.decisions
    task_id = task["id"]

    l1 = decision_repo.create(
        task_id=task_id,
        level="L1",
        question="是否出原型？",
        options=[{"key": "yes", "label": "出"}, {"key": "no", "label": "不出"}],
        recommended_key="yes",
        timeout_at=iso_in(-1),
    )
    l2 = decision_repo.create(
        task_id=task_id,
        level="L2",
        question="范围是否包含供应商？",
        options=[{"key": "yes", "label": "含"}, {"key": "no", "label": "不含"}],
        timeout_at=iso_in(-1),
    )
    result = service.sweep()
    assert result["due"] == 2
    assert result["auto_answered"] == 1
    assert result["expired"] == 1
    assert decision_repo.get(l1.id).status == "auto_answered"
    assert decision_repo.get(l1.id).auto_decided == 1
    assert decision_repo.get(l2.id).status == "expired"
