"""Model executor contract tests.

The model is faked: these tests check that what we hand the gates is shaped the way
the gates demand, and that a full task can reach ``delivered`` end to end. Real model
quality is a separate question and is exercised on a live instance, not here.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from octop.infra.pm.runtime.executor import DeterministicExecutor, StepContext
from octop.infra.pm.runtime.model_executor import (
    MODEL_STEPS,
    ModelStepExecutor,
    _checkable,
    _clean_html,
    _loads_object,
)
from octop.infra.pm.service import PMService

PROTOTYPE_HTML = (
    "<style>.row{display:flex;gap:8px}</style>"
    '<div class="row"><select><option>全部</option></select>'
    '<input placeholder="搜索供应商"><button>筛选</button></div>'
    "<table><tr><td>供应商 A</td></tr></table>"
)

PRD_BODY = (
    "# 派单供应商列表 · 就绪状态筛选\n\n"
    "## 背景与目标\n运营在列表页看不出供应商是否就绪，需逐条点开确认。\n\n"
    "## 范围\n仅列表页；不改详情页；复用既有 GET /vendors 返回字段。\n\n"
    "## 用户流程\n进入列表 → 选择筛选 → 查看未就绪原因 → 处理。\n\n"
    "## 功能清单\nP0 筛选器；P0 未就绪禁用与提示；P1 空态与错误态。\n\n"
    "## 验收标准\n当切换筛选时则应即时过滤且 URL 保持；当失败时则应可重试。\n\n"
    "## 边界与异常\n30 家以上供应商、并发修改、权限不足。\n\n"
    "## 依赖与风险\n依赖供应商注册表字段；风险为字段口径不一致。\n"
)

SCRIPT: tuple[tuple[str, Any], ...] = (
    ("可核查事实", {
        "links": [{"kind": "page", "ref": "派单供应商管理", "why": "同页操作", "impact": "high"}],
        "reads": [{"claim": "列表字段已含就绪标记", "source": "需求原文提到 ready 与 missing 字段"}],
        "gaps": ["未接入工单端口，历史反馈无法核对"],
    }),
    ("代码面", {
        "touchpoints": [{"area": "供应商列表页", "change": "新增筛选器", "confidence": "medium",
                         "basis": "需求要求列表页顶部增加筛选"}],
        "gaps": ["需代码库确认组件路径"],
    }),
    ("业务字段", {
        "fields": [{"name": "就绪状态", "guess": "ready", "type": "enum", "note": "布尔或枚举待确认"}],
        "gaps": ["需数据字典确认 ready 字段口径"],
    }),
    ("可验收的条目", {
        "items": [
            {"title": "状态筛选", "user_value": "运营一屏排查", "acceptance": "当切换筛选时则应即时过滤",
             "priority": "P0", "edge_cases": ["空结果"]},
            {"title": "未就绪禁用", "user_value": "避免误启用", "acceptance": "体验要流畅",
             "priority": "P0", "edge_cases": ["多原因并存"]},
        ],
        "open_questions": ["未就绪的判定口径由谁维护？"],
    }),
    ("系统能力", {
        "verdict": "partial", "score": 72,
        "reuse": ["既有列表接口"],
        "build": [{"item": "筛选器与禁用态", "effort": "0.5d", "risk": "low"}],
        "gaps": ["未确认后端是否返回 missing 原因"],
    }),
    ("需要人拍板", {
        "question": "筛选口径按「就绪」还是按「缺失原因」分组？",
        "options": [
            {"key": "by_ready", "label": "按就绪二分", "pros": "简单", "cons": "原因需再点开",
             "effort": "0.5d", "confidence": 0.7},
            {"key": "by_reason", "label": "按缺失原因分组", "pros": "一屏定位", "cons": "选项变多",
             "effort": "1d", "confidence": 0.6},
        ],
        "recommended_key": "by_reason",
        "why": "运营的真实诉求是定位原因，不是知道有没有",
    }),
    ("可点击的高保真原型", PROTOTYPE_HTML),
    ("写这份需求的 PRD", PRD_BODY),
    ("三个视角评审", {
        "business": {"verdict": "pass", "comment": "价值明确，可排期"},
        "tech": {"verdict": "pass", "comment": "复用既有接口，改动可控"},
        "customer": {"verdict": "pass", "comment": "减少排查时间"},
        "summary": "三视角通过",
    }),
)


class FakeAccess:
    def __init__(self, script: tuple[tuple[str, Any], ...]) -> None:
        self._script = script

    async def ainvoke_text(
        self, prompt: str, *, system: str | None = None, timeout_s: float | None = None
    ) -> str:
        for marker, payload in self._script:
            if marker in prompt:
                if isinstance(payload, str):
                    return payload
                return json.dumps(payload, ensure_ascii=False)
        raise AssertionError(f"no scripted reply for prompt: {prompt[:80]}")


class FakeFactory:
    def __init__(self, script: tuple[tuple[str, Any], ...] = SCRIPT) -> None:
        self._access = FakeAccess(script)

    def get(self, model: str | None = None) -> FakeAccess:
        return self._access


def _ctx(step_key: str, **overrides: Any) -> StepContext:
    base: dict[str, Any] = {
        "task_id": 1,
        "task_public_id": "PM-20261031-001",
        "requirement_id": 1,
        "requirement_title": "派单供应商列表 · 就绪状态筛选",
        "requirement_text": (
            "运营在派单供应商列表里看不出哪些供应商没配好，30 家排查一轮 20 分钟。"
            "要求列表页顶部增加就绪筛选，未就绪行禁用启用开关并提示原因。"
        ),
        "workspace_id": 1,
        "step_key": step_key,
        "seq": 1,
        "threshold": 80,
        "decision_level": "L2",
        "decision_timeout_s": 300,
    }
    base.update(overrides)
    return StepContext(**base)


def _executor(**kwargs: Any) -> ModelStepExecutor:
    return ModelStepExecutor(models_provider=lambda: FakeFactory(), **kwargs)


# --- pure helpers -----------------------------------------------------------


def test_checkable_rewrites_unjudgeable_acceptance() -> None:
    assert _checkable("当切换筛选时，则列表即时过滤", "x") == "当切换筛选时，则列表即时过滤"
    assert _checkable("体验要流畅", "筛选器") == "当执行「筛选器」时，则应 体验要流畅"
    assert _checkable("", "空态") == "当执行「空态」时，则应 满足「空态」的功能要求"


def test_loads_object_survives_code_fences_and_prose() -> None:
    assert _loads_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert _loads_object('好的，结果如下：{"a": {"b": 2}} 希望有帮助') == {"a": {"b": 2}}
    assert _loads_object("完全不是 JSON") is None


def test_clean_html_strips_fence_and_injects_style() -> None:
    cleaned = _clean_html("```html\n<div><button>点我</button></div>\n```")
    assert "```" not in cleaned
    assert "<style" in cleaned
    assert "<button" in cleaned


# --- per-step contracts -----------------------------------------------------


def test_link_expand_evidence_is_citable() -> None:
    out = _executor().run(_ctx("link_expand"))
    assert out.status == "passed"
    assert out.evidence, "P1 needs at least one envelope"
    for item in out.evidence:
        assert item["locator"]["kind"] == "pm_record"
        assert str(item["excerpt"]).strip()
    assert out.trace["sources"], "G-P1-03 requires a source breakdown"
    assert out.artifact is not None and out.artifact["kind"] == "data"


def test_requirement_analysis_backstops_acceptance() -> None:
    out = _executor().run(_ctx("requirement_analysis"))
    body = json.loads(out.artifact["body"])
    assert len(body["items"]) == 2
    # The second item's acceptance was "体验要流畅" — the backstop must have wrapped it.
    assert all("当" in item["acceptance"] for item in body["items"])
    assert out.artifact["kind"] == "document"


def test_prototype_passes_shape_gates() -> None:
    out = _executor().run(_ctx("prototype"))
    html = str(out.artifact["body"])
    assert "```" not in html
    assert "<style" in html
    assert sum(tag in html for tag in ("<select", "<table", "<button", "<input")) >= 2
    assert out.artifact["fidelity"] == "partial"
    assert out.artifact["fidelity_note"]


def test_decision_returns_a_card_not_an_artifact() -> None:
    out = _executor().run(_ctx("decision"))
    assert out.status == "needs_decision"
    assert out.decision is not None
    assert len(out.decision["options"]) >= 2
    assert out.decision["recommended_key"] in {o["key"] for o in out.decision["options"]}


def test_missing_provider_degrades_to_unavailable() -> None:
    from octop.infra.pm.runtime.executor import ExecutorUnavailable

    executor = ModelStepExecutor(models_provider=None)
    with pytest.raises(ExecutorUnavailable):
        executor.run(_ctx("prd"))


def test_supports_only_the_nine_model_steps() -> None:
    executor = _executor()
    for key in MODEL_STEPS:
        assert executor.supports(key)
    for key in ("intake", "dedup", "prototype_gate", "deliver"):
        assert not executor.supports(key)


def test_sequential_calls_reuse_one_live_loop() -> None:
    """Regression: a fresh loop per call closed the provider client's loop.

    On the live instance step 2 passed and step 3 died with "Event loop is closed".
    Every model call must therefore run on the same, still-open loop.
    """
    import asyncio

    seen: list[Any] = []

    class LoopRecorder:
        async def ainvoke_text(self, prompt: str, **kwargs: Any) -> str:
            seen.append(asyncio.get_running_loop())
            for marker, payload in SCRIPT:
                if marker in prompt:
                    return payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
            raise AssertionError("unscripted prompt")

    class RecorderFactory:
        def get(self, model: str | None = None) -> LoopRecorder:
            return LoopRecorder()

    executor = ModelStepExecutor(models_provider=RecorderFactory)
    for key in ("link_expand", "requirement_analysis", "code"):
        assert executor.run(_ctx(key)).status == "passed"

    assert len(seen) >= 3
    assert len({id(loop) for loop in seen}) == 1, "model calls must share one loop"
    assert not seen[0].is_closed()


# --- end to end -------------------------------------------------------------


def test_full_task_reaches_delivered(service: PMService, requirement: dict) -> None:
    """The whole 13-step pipeline, model steps faked, must end at ``delivered``."""
    task = service.create_task(requirement_id=requirement["id"], owner_user_id=1)
    factories: list[FakeFactory] = [FakeFactory()]
    service.attach_models(lambda: factories[0])

    for _ in range(40):
        detail = service.task_detail(task["id"])
        if detail["status"] == "delivered":
            break
        if detail["status"] == "waiting_human":
            cards = detail["pending_decisions"]
            assert cards, "waiting_human without a pending card"
            service.answer_decision(
                cards[0]["id"],
                selected_key=cards[0]["recommended_key"],
                note="按推荐项执行，验收标准已确认可判定",
                user_id=1,
            )
            continue
        service.tick(task["id"])

    detail = service.task_detail(task["id"])
    steps = {item["step_key"]: item for item in detail["steps"]}
    assert detail["status"] == "delivered", (
        f"stuck at {detail['status']}; "
        f"failed={[k for k, v in steps.items() if v['status'] == 'failed']}"
    )
    assert all(item["status"] == "passed" for item in detail["steps"])
    assert len(detail["steps"]) == 13
    reviews = service.list_reviews(task["id"])
    assert {item["perspective"] for item in reviews["items"]} == {"business", "tech", "customer"}


def test_deterministic_executor_still_wins_its_own_steps() -> None:
    """Registering the model executor must not shadow intake/dedup/gate/deliver."""
    from octop.infra.pm.runtime.executor import RegistryExecutor

    registry = RegistryExecutor(DeterministicExecutor(), _executor())
    assert registry.supports("intake")
    out = registry.run(_ctx("intake"))
    assert out.artifact is not None and out.artifact["origin"] == "rule"
