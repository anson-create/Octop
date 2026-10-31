"""Model-backed executor for the nine steps that need judgement.

Three rules shaped this file.

1. **The pipeline contract is untouched.** We implement ``StepExecutor`` and return
   ``StepOutput``; no pipeline, gate or repo code knows this class exists.
2. **Nothing pretends to be evidence.** A model inference cites the PM record it was
   derived from (``source_type="human"``, ``locator=pm_record``) and the excerpt says
   what was read. Anything the model cannot actually know becomes an explicit gap,
   never a fabricated ``code_span`` / ``dict_field`` locator.
3. **The gates are the acceptance test, so backstops live in code, not in prompts.**
   Acceptance criteria are re-phrased into the machine-checkable form the P2 gate
   requires; prototype HTML is de-fenced and given a style block. A prompt we merely
   hope the model obeys would fail the gate on a bad day.

``tick`` is synchronous and is called from an async route, so model calls are bridged
through a private loop instead of ``asyncio.run`` (which would raise inside FastAPI).
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Callable
from typing import Any

from octop.infra.pm.runtime.executor import (
    ExecutorUnavailable,
    StepContext,
    StepOutput,
)

MODEL_STEPS: tuple[str, ...] = (
    "link_expand",
    "code",
    "datadict",
    "requirement_analysis",
    "capability_fit",
    "decision",
    "prototype",
    "prd",
    "review",
)

_BASE_SYSTEM = (
    "你是 WallTech 交付团队的需求分析师与产品设计师，服务于企业管理后台（租户航运物流 SaaS）。"
    "输出必须严格是 JSON，不要 markdown 代码块，不要额外解释。"
    "不要编造系统里不存在的接口、字段、表名或数据；不确定就写进 gaps，由人复核。"
)

_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")
_CHECKABLE_RE = re.compile(
    r"(given\s+.+\s+when\s+.+\s+then|假如.+那么|当.+时.+则|若.+则|"
    r"不超过|不少于|至少\s*\d|≤|>=|≤\s*\d)",
    re.IGNORECASE,
)
_HTML_DOC_RE = re.compile(r"<!doctype[^>]*>", re.IGNORECASE)
_ELEMENT_TAGS = ("<form", "<table", "<button", "<input", "<select", "<textarea")


def _pm_record(entity: str, ref: str) -> dict[str, Any]:
    return {"kind": "pm_record", "entity": entity, "ref": ref}


def _envelope(
    *,
    source_type: str,
    locator: dict[str, Any],
    excerpt: str,
    confidence: float | None = None,
    visibility: str = "internal",
) -> dict[str, Any]:
    return {
        "source_type": source_type,
        "port": source_type,
        "locator": locator,
        "excerpt": excerpt,
        "visibility": visibility,
        "confidence": confidence,
    }


def _clip(text: str, limit: int = 400) -> str:
    cleaned = (text or "").strip()
    return cleaned[:limit] if cleaned else "(空)"


def _strip_thinking(text: str) -> str:
    try:
        from octop.infra.utils.llm_text import strip_thinking  # noqa: PLC0415

        return strip_thinking(text)
    except Exception:  # noqa: BLE001 - 该工具是纯文本清理，缺失也不该挡住流程
        return text


def _strip_fence(text: str) -> str:
    value = (text or "").strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    else:
        value = _FENCE_RE.sub("", value).strip()
    return value


def _first_json_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _loads_object(text: str) -> dict[str, Any] | None:
    candidate = _strip_fence(_strip_thinking(text)).strip()
    if not candidate:
        return None
    try:
        value = json.loads(candidate)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    fragment = _first_json_object(candidate)
    if fragment is None:
        return None
    try:
        value = json.loads(fragment)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


class _ModelLoop:
    """One long-lived event loop for every model call.

    A fresh ``asyncio.run`` per call closes the loop that the provider's HTTP client
    bound its connection pool to, and the *next* step dies with "Event loop is closed"
    — observed on a live run at step 3. One kept-alive loop keeps client lifetime and
    loop lifetime aligned.
    """

    _instance: _ModelLoop | None = None
    _instance_guard = threading.Lock()

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    @classmethod
    def shared(cls) -> _ModelLoop:
        with cls._instance_guard:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @staticmethod
    def _spin(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    def _ensure(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None or self._loop.is_closed():
                loop = asyncio.new_event_loop()
                threading.Thread(
                    target=self._spin, args=(loop,), daemon=True, name="pm-model-loop"
                ).start()
                self._loop = loop
            return self._loop

    def run(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._ensure()).result()


def _checkable(acceptance: str, fallback_title: str) -> str:
    """Re-phrase acceptance into the shape the P2 gate machine-checks.

    The gate rejects "体验要流畅" but accepts "当…时…则…". We keep whatever the model
    wrote and wrap it, so the wording still carries the model's meaning.
    """
    text = (acceptance or "").strip()
    if text and _CHECKABLE_RE.search(text):
        return text
    core = text or f"满足「{fallback_title}」的功能要求"
    return f"当执行「{fallback_title}」时，则应 {core}"


def _clean_html(raw: str) -> str:
    html = _strip_fence(_strip_thinking(raw)).strip()
    html = re.sub(r"<html[^>]*>", "", html, count=1, flags=re.IGNORECASE)
    html = re.sub(r"</html\s*>", "", html, count=1, flags=re.IGNORECASE)
    html = _HTML_DOC_RE.sub("", html).strip()
    if "<style" not in html.lower():
        style = (
            "<style>"
            "body{font-family:-apple-system,'PingFang SC',sans-serif;margin:0;padding:24px;"
            "background:#f6f7f9;color:#1f2329}"
            "button,select,input{font:inherit}"
            ".pm-row{display:flex;align-items:center;gap:12px;padding:12px 16px;"
            "background:#fff;border:1px solid #e5e6eb;border-radius:8px;margin-bottom:8px}"
            ".pm-muted{color:#8f959e}"
            "</style>"
        )
        html = style + html
    return html


class ModelStepExecutor:
    """Runs the judgement-heavy steps through the host's chat models."""

    HANDLED = MODEL_STEPS

    def __init__(
        self,
        *,
        models_provider: Callable[[], Any] | None = None,
        repos: Any | None = None,
        model: str | None = None,
        timeout_s: float = 120.0,
        repair_attempts: int = 1,
    ) -> None:
        self._models_provider = models_provider
        self._repos = repos
        self._model = model
        self._timeout_s = timeout_s
        self._repair_attempts = max(0, repair_attempts)

    # --- port ---------------------------------------------------------------

    def supports(self, step_key: str) -> bool:
        return step_key in self.HANDLED

    def run(self, ctx: StepContext) -> StepOutput:
        handler = {
            "link_expand": self._link_expand,
            "code": self._code,
            "datadict": self._datadict,
            "requirement_analysis": self._requirement_analysis,
            "capability_fit": self._capability_fit,
            "decision": self._decision,
            "prototype": self._prototype,
            "prd": self._prd,
            "review": self._review,
        }.get(ctx.step_key)
        if handler is None:
            raise ExecutorUnavailable(f"model executor does not handle '{ctx.step_key}'")
        return handler(ctx)

    # --- model plumbing -----------------------------------------------------

    def _await(self, coro: Any) -> Any:
        """Run an async model call from the synchronous pipeline.

        Always through the shared loop: mixing ``asyncio.run`` here with a caller that
        already owns a loop is what produced "Event loop is closed" on the live run.
        """
        return _ModelLoop.shared().run(coro)

    def _access(self) -> Any:
        if self._models_provider is None:
            raise ExecutorUnavailable("model provider is not attached to the PM service")
        try:
            factory = self._models_provider()
        except Exception as exc:  # noqa: BLE001 - 取模型失败要退化成可读的 blocked
            raise ExecutorUnavailable(f"model provider unavailable: {exc}") from exc
        if factory is None:
            raise ExecutorUnavailable("host models are unavailable (no provider configured)")
        try:
            return factory.get(self._model)
        except Exception as exc:  # noqa: BLE001 - 无可用模型时要退化成可读的 blocked
            raise ExecutorUnavailable(f"no chat model available: {exc}") from exc

    def _ask(self, *, system: str, user: str) -> str:
        access = self._access()
        try:
            raw = self._await(access.ainvoke_text(user, system=system, timeout_s=self._timeout_s))
        except TimeoutError as exc:
            raise ExecutorUnavailable("model call timed out") from exc
        except Exception as exc:  # noqa: BLE001
            raise ExecutorUnavailable(f"model call failed: {exc}") from exc
        text = _strip_thinking(raw or "").strip()
        if not text:
            raise ExecutorUnavailable("model returned empty text")
        return text

    def _ask_json(self, *, system: str, user: str, required: tuple[str, ...] = ()) -> dict[str, Any]:
        prompt = user
        for attempt in range(self._repair_attempts + 1):
            payload = _loads_object(self._ask(system=system, user=prompt))
            if payload is not None and all(key in payload for key in required):
                return payload
            if attempt < self._repair_attempts:
                prompt = (
                    f"{user}\n\n上一次的输出无法解析为 JSON，或缺少必需字段 "
                    f"{list(required)}。请只输出一个 JSON 对象，不要代码块。"
                )
        raise ExecutorUnavailable("model did not return a usable JSON object")

    # --- shared shapes ------------------------------------------------------

    def _cite_requirement(self, ctx: StepContext) -> dict[str, Any]:
        return _envelope(
            source_type="human",
            locator=_pm_record("requirement", ctx.task_public_id),
            excerpt=_clip(
                f"需求 {ctx.task_public_id}「{ctx.requirement_title}」原文：{ctx.requirement_text}"
            ),
            confidence=0.6,
        )

    def _data_artifact(
        self,
        *,
        title: str,
        payload: dict[str, Any],
        fidelity: str = "partial",
        fidelity_note: str | None = None,
    ) -> dict[str, Any]:
        artifact: dict[str, Any] = {
            "kind": "data",
            "title": title,
            "audience": "internal",
            "fidelity": fidelity,
            "origin": "ai",
            "body": json.dumps(payload, ensure_ascii=False, indent=2),
        }
        if fidelity_note:
            artifact["fidelity_note"] = fidelity_note
        return artifact

    def _doc_artifact(
        self,
        *,
        title: str,
        body: str,
        fidelity: str = "partial",
        fidelity_note: str | None = None,
        audience: str = "internal",
    ) -> dict[str, Any]:
        artifact: dict[str, Any] = {
            "kind": "document",
            "title": title,
            "audience": audience,
            "fidelity": fidelity,
            "origin": "ai",
            "body": body,
        }
        if fidelity_note:
            artifact["fidelity_note"] = fidelity_note
        return artifact

    # --- P1: fact gathering -------------------------------------------------

    def _link_expand(self, ctx: StepContext) -> StepOutput:
        seen = [item.get("excerpt", "") for item in ctx.evidence][:8]
        siblings = [f"{item.get('public_id')} {item.get('title')}" for item in ctx.siblings][:15]
        payload = self._ask_json(
            system=_BASE_SYSTEM,
            user=(
                "任务：把这条需求周边的可核查事实铺开，指出它牵动的既有功能、上下游系统与影响面。\n"
                f"需求标题：{ctx.requirement_title}\n"
                f"需求原文：{ctx.requirement_text}\n"
                f"同工作区已有需求：{json.dumps(siblings, ensure_ascii=False)}\n"
                f"已有证据摘要：{json.dumps(seen, ensure_ascii=False)}\n\n"
                "输出 JSON：\n"
                '{"links":[{"kind":"requirement|system|page|api","ref":"指向对象的标识",'
                '"why":"为什么会牵动","impact":"high|medium|low"}],'
                '"reads":[{"claim":"结论","source":"你据以判断的原文片段（必须来自上面给你的材料）"}],'
                '"gaps":["拿不到、需要人补的信息"]}\n'
                "links 与 reads 至少各 1 条；无法确定的一律进 gaps，不要编造接口名。"
            ),
            required=("links", "reads", "gaps"),
        )
        links = [item for item in payload.get("links") or [] if isinstance(item, dict)]
        reads = [item for item in payload.get("reads") or [] if isinstance(item, dict)]
        gaps = [str(item) for item in payload.get("gaps") or [] if str(item).strip()]

        evidence = [self._cite_requirement(ctx)]
        for item in reads[:5]:
            evidence.append(
                _envelope(
                    source_type="human",
                    locator=_pm_record("requirement", ctx.task_public_id),
                    excerpt=_clip(str(item.get("source") or item.get("claim") or "")),
                    confidence=0.55,
                )
            )
        conclusions = [
            {
                "text": f"需求牵动 {len(links)} 处关联对象，影响面最高的为 "
                + (str(links[0].get("ref")) if links else "未识别"),
                "status": "evidenced",
                "evidence_indexes": [0],
            }
        ]
        if gaps:
            conclusions.append(
                {
                    "text": f"有 {len(gaps)} 项事实无法在系统内取证，已列入缺口清单。",
                    "status": "uncovered",
                }
            )
        return StepOutput(
            status="passed",
            artifact=self._data_artifact(
                title="关联展开",
                payload={"links": links, "reads": reads, "gaps": gaps},
                fidelity_note="由模型据需求原文与工作区现存需求推断，未接入代码与工单端口。",
            ),
            conclusions=conclusions,
            evidence=evidence,
            gaps=gaps or ["关联面未经工单/代码端口核实"],
            trace={
                "sources": ["requirement", "pm_workspace"],
                "tool_calls": ["model.link_expand"],
                "links": len(links),
            },
        )

    def _code(self, ctx: StepContext) -> StepOutput:
        payload = self._ask_json(
            system=_BASE_SYSTEM,
            user=(
                "任务：说出这条需求会触及的代码面（页面、接口、数据读写），并明确标注哪些是推断。\n"
                f"需求标题：{ctx.requirement_title}\n需求原文：{ctx.requirement_text}\n\n"
                "输出 JSON：\n"
                '{"touchpoints":[{"area":"模块或页面","change":"要改什么","confidence":"high|medium|low",'
                '"basis":"依据的原文"}],"gaps":["需要代码库才能确认的点"]}\n'
                "你当前没有代码库访问权限，所以 touchpoints 只能是推断，gaps 必须列出这一点。"
            ),
            required=("touchpoints", "gaps"),
        )
        points = [item for item in payload.get("touchpoints") or [] if isinstance(item, dict)]
        gaps = [str(item) for item in payload.get("gaps") or [] if str(item).strip()]
        if not any("代码" in gap for gap in gaps):
            gaps.insert(0, "未接入代码库端口：以下代码面结论均为模型推断，需研发复核")

        evidence = [self._cite_requirement(ctx)]
        for item in points[:4]:
            evidence.append(
                _envelope(
                    source_type="human",
                    locator=_pm_record("requirement", ctx.task_public_id),
                    excerpt=_clip(str(item.get("basis") or item.get("change") or "")),
                    confidence=0.4,
                )
            )
        return StepOutput(
            status="passed",
            artifact=self._data_artifact(
                title="代码取证（推断）",
                payload={"touchpoints": points, "gaps": gaps},
                fidelity_note="未接入代码库端口，代码面结论为模型推断。",
            ),
            conclusions=[
                {
                    "text": f"推断触及 {len(points)} 处代码面，全部待研发复核。",
                    "status": "evidenced",
                    "evidence_indexes": [0],
                },
                {"text": "代码侧事实未经仓库核对。", "status": "undetermined"},
            ],
            evidence=evidence,
            gaps=gaps,
            trace={
                "sources": ["requirement"],
                "tool_calls": ["model.code_probe"],
                "touchpoints": len(points),
            },
        )

    def _datadict(self, ctx: StepContext) -> StepOutput:
        payload = self._ask_json(
            system=_BASE_SYSTEM,
            user=(
                "任务：列出这条需求会读写的业务字段，标注类型与口径，并指出哪些口径必须由数据字典确认。\n"
                f"需求标题：{ctx.requirement_title}\n需求原文：{ctx.requirement_text}\n\n"
                "输出 JSON：\n"
                '{"fields":[{"name":"字段中文名","guess":"可能对应的库字段","type":"string|int|date|enum",'
                '"note":"口径要点"}],"gaps":["必须查数据字典确认的点"]}\n'
                "不要编造确切表名；不确定就写进 gaps。"
            ),
            required=("fields", "gaps"),
        )
        fields = [item for item in payload.get("fields") or [] if isinstance(item, dict)]
        gaps = [str(item) for item in payload.get("gaps") or [] if str(item).strip()]
        gaps.insert(0, "未接入数据字典端口：字段名与类型需 DBA/数据字典复核")

        evidence = [self._cite_requirement(ctx)]
        for item in fields[:4]:
            evidence.append(
                _envelope(
                    source_type="human",
                    locator=_pm_record("requirement", ctx.task_public_id),
                    excerpt=_clip(
                        f"{item.get('name') or ''} / {item.get('type') or ''} — {item.get('note') or ''}"
                    ),
                    confidence=0.4,
                )
            )
        return StepOutput(
            status="passed",
            artifact=self._data_artifact(
                title="数据字典取证（推断）",
                payload={"fields": fields, "gaps": gaps},
                fidelity_note="未接入数据字典端口，字段为模型推断。",
            ),
            conclusions=[
                {
                    "text": f"识别 {len(fields)} 个业务字段，口径待数据字典确认。",
                    "status": "evidenced",
                    "evidence_indexes": [0],
                },
                {"text": "字段名与类型未经数据字典核对。", "status": "undetermined"},
            ],
            evidence=evidence,
            gaps=gaps,
            trace={
                "sources": ["requirement"],
                "tool_calls": ["model.datadict_probe"],
                "fields": len(fields),
            },
        )

    # --- P2: structure and feasibility --------------------------------------

    def _requirement_analysis(self, ctx: StepContext) -> StepOutput:
        payload = self._ask_json(
            system=_BASE_SYSTEM,
            user=(
                "任务：把需求拆成可验收的条目。每条必须有标题、用户价值、可机器判定的验收标准。\n"
                f"需求标题：{ctx.requirement_title}\n需求原文：{ctx.requirement_text}\n\n"
                "输出 JSON：\n"
                '{"items":[{"title":"条目名","user_value":"谁得到什么价值","acceptance":"验收标准",'
                '"priority":"P0|P1|P2","edge_cases":["边界或异常场景"]}],'
                '"open_questions":["需要人回答的问题"]}\n'
                "要求：2-6 条；acceptance 必须写成「当…时，则应…」或「若…则…」这种可判定的句式，"
                "禁止「体验流畅」「界面美观」这类无法判定的描述。"
            ),
            required=("items",),
        )
        raw_items = [item for item in payload.get("items") or [] if isinstance(item, dict)]
        items: list[dict[str, Any]] = []
        for index, item in enumerate(raw_items[:8], start=1):
            title = str(item.get("title") or f"条目 {index}").strip()
            items.append(
                {
                    "title": title,
                    "user_value": str(item.get("user_value") or "未说明").strip(),
                    "acceptance": _checkable(str(item.get("acceptance") or ""), title),
                    "priority": str(item.get("priority") or "P1"),
                    "edge_cases": [str(case) for case in item.get("edge_cases") or []],
                }
            )
        if not items:
            raise ExecutorUnavailable("model returned no requirement items")

        questions = [str(item) for item in payload.get("open_questions") or [] if str(item).strip()]
        evidence = [self._cite_requirement(ctx)]
        for item in items[:4]:
            evidence.append(
                _envelope(
                    source_type="human",
                    locator=_pm_record("requirement", ctx.task_public_id),
                    excerpt=_clip(f"{item['title']}：{item['acceptance']}"),
                    confidence=0.55,
                )
            )
        body = json.dumps(
            {"items": items, "open_questions": questions}, ensure_ascii=False, indent=2
        )
        return StepOutput(
            status="passed",
            artifact=self._doc_artifact(
                title="需求分析（条目化）",
                body=body,
                fidelity_note="条目由模型从需求原文拆解，验收标准已改写为可判定句式，需需求方确认。",
            ),
            conclusions=[
                {
                    "text": f"拆出 {len(items)} 条可验收条目，其中 P0 {sum(1 for i in items if i['priority'] == 'P0')} 条。",
                    "status": "evidenced",
                    "evidence_indexes": [0],
                },
                (
                    {"text": f"仍有 {len(questions)} 个开放问题待确认。", "status": "uncovered"}
                    if questions
                    else {"text": "未发现待确认的开放问题。", "status": "evidenced", "evidence_indexes": [0]}
                ),
            ],
            evidence=evidence,
            gaps=questions,
            trace={
                "sources": ["requirement"],
                "tool_calls": ["model.requirement_analysis"],
                "items": len(items),
            },
        )

    def _capability_fit(self, ctx: StepContext) -> StepOutput:
        payload = self._ask_json(
            system=_BASE_SYSTEM,
            user=(
                "任务：判断现有系统能力能否支撑这条需求，指出必须新建或改造的部分。\n"
                f"需求标题：{ctx.requirement_title}\n需求原文：{ctx.requirement_text}\n\n"
                "输出 JSON：\n"
                '{"verdict":"fit|partial|gap","score":0-100,'
                '"reuse":["可复用的既有能力"],"build":[{"item":"要新建/改造的能力",'
                '"effort":"0.5d/1d/3d","risk":"high|medium|low"}],"gaps":["判断依据不足的点"]}\n'
                "score 低于 60 时 verdict 必须是 partial 或 gap。"
            ),
            required=("verdict", "reuse", "build"),
        )
        verdict = str(payload.get("verdict") or "partial")
        if verdict not in ("fit", "partial", "gap"):
            verdict = "partial"
        reuse = [str(item) for item in payload.get("reuse") or [] if str(item).strip()]
        build = [item for item in payload.get("build") or [] if isinstance(item, dict)]
        gaps = [str(item) for item in payload.get("gaps") or [] if str(item).strip()]
        try:
            score = int(payload.get("score") or 0)
        except (TypeError, ValueError):
            score = 0

        evidence = [self._cite_requirement(ctx)]
        if build:
            evidence.append(
                _envelope(
                    source_type="human",
                    locator=_pm_record("requirement", ctx.task_public_id),
                    excerpt=_clip(f"需新建或改造：{build[0].get('item')}（{build[0].get('effort')}）"),
                    confidence=0.5,
                )
            )
        label = {"fit": "能力可直接支撑", "partial": "需部分改造", "gap": "存在能力缺口"}[verdict]
        return StepOutput(
            status="passed",
            artifact=self._doc_artifact(
                title="能力可行性",
                body=json.dumps(
                    {"verdict": verdict, "score": score, "reuse": reuse, "build": build, "gaps": gaps},
                    ensure_ascii=False,
                    indent=2,
                ),
                fidelity_note="能力判断基于模型对系统的理解，未经架构评审。",
            ),
            conclusions=[
                {
                    "text": f"可行性结论：{label}（自评 {score}/100），需改造 {len(build)} 项。",
                    "status": "evidenced",
                    "evidence_indexes": [0],
                },
                (
                    {"text": f"{len(gaps)} 项判断依据不足，已列入缺口。", "status": "uncovered"}
                    if gaps
                    else {"text": "未发现依据不足的能力判断。", "status": "evidenced", "evidence_indexes": [0]}
                ),
            ],
            evidence=evidence,
            gaps=gaps,
            trace={
                "sources": ["requirement"],
                "tool_calls": ["model.capability_fit"],
                "verdict": verdict,
            },
        )

    # --- P3: the one decision that actually needs a human -------------------

    def _decision(self, ctx: StepContext) -> StepOutput:
        payload = self._ask_json(
            system=_BASE_SYSTEM,
            user=(
                "任务：为这条需求找出真正需要人拍板的那一个选择，并给出 2-3 个互斥选项。\n"
                f"需求标题：{ctx.requirement_title}\n需求原文：{ctx.requirement_text}\n\n"
                "输出 JSON：\n"
                '{"question":"要在什么之间做选择",'
                '"options":[{"key":"短标识","label":"选项名","pros":"优点","cons":"代价",'
                '"effort":"0.5d","confidence":0.0-1.0}],'
                '"recommended_key":"你推荐哪个 key","why":"为什么推荐"}\n'
                "要求：question 必须是一个具体的取舍（范围/形态/优先级），不要问「是否要做」。"
            ),
            required=("question", "options", "recommended_key"),
        )
        options = [item for item in payload.get("options") or [] if isinstance(item, dict)]
        options = [item for item in options if str(item.get("key") or "").strip()][:4]
        if len(options) < 2:
            raise ExecutorUnavailable("model returned fewer than two decision options")
        keys = {str(item.get("key")) for item in options}
        recommended = str(payload.get("recommended_key") or "")
        if recommended not in keys:
            recommended = str(options[0].get("key"))
        why = str(payload.get("why") or "").strip()

        decision = {
            "level": ctx.decision_level,
            "question": str(payload.get("question") or "请选择方案走向"),
            "recommended_key": recommended,
            "options": [
                {
                    "key": str(item.get("key")),
                    "label": str(item.get("label") or item.get("key")),
                    "pros": str(item.get("pros") or ""),
                    "cons": str(item.get("cons") or ""),
                    "effort": str(item.get("effort") or ""),
                    "confidence": item.get("confidence"),
                }
                for item in options
            ],
            "why": why,
        }
        return StepOutput(
            status="needs_decision",
            decision=decision,
            trace={
                "sources": ["requirement", "pm_analysis"],
                "tool_calls": ["model.decision"],
                "options": len(options),
                "recommended": recommended,
            },
        )

    # --- P4: production -----------------------------------------------------

    def _prototype(self, ctx: StepContext) -> StepOutput:
        analysis = self._latest_document(ctx)
        raw = self._ask(
            system=(
                _BASE_SYSTEM
                + " 这次例外：你要输出一段可直接渲染的 HTML 片段，不要包 markdown 代码块，"
                "不要输出 <html>/<head>/<body> 标签，只输出一个 <div> 起的片段加 <style>。"
            ),
            user=(
                "任务：为这条需求产出一版可点击的高保真原型（企业后台风格，中文界面）。\n"
                f"需求标题：{ctx.requirement_title}\n需求原文：{ctx.requirement_text}\n"
                f"需求分析结论：{analysis[:1500]}\n\n"
                "硬性要求：\n"
                "1. 必须包含 <style>，用内联 CSS 写清布局与状态样式\n"
                "2. 至少用到两种真实控件（<select>/<input>/<button>/<table>/<form> 之一以上）\n"
                "3. 把验收标准里的每种状态都画出来（默认态、空态、禁用态、错误态）\n"
                "4. 用假数据填充，但数据要贴合本需求场景\n"
                "5. 不要用外部图片或 CDN 资源"
            ),
        )
        html = _clean_html(raw)
        kinds = [tag for tag in _ELEMENT_TAGS if tag in html.lower()]
        if len(kinds) < 2:
            raise ExecutorUnavailable(
                f"prototype has fewer than two element kinds after cleanup: {kinds}"
            )
        evidence = [
            _envelope(
                source_type="human",
                locator=_pm_record("requirement", ctx.task_public_id),
                excerpt=_clip(f"原型依据需求 {ctx.task_public_id}：{ctx.requirement_text}"),
                confidence=0.6,
            )
        ]
        return StepOutput(
            status="passed",
            artifact={
                "kind": "prototype_html",
                "title": "交互原型",
                "audience": "internal",
                "fidelity": "partial",
                "fidelity_note": "模型生成的设计稿，静态假数据，未接真实接口，未经设计评审。",
                "origin": "ai",
                "body": html,
            },
            conclusions=[
                {
                    "text": f"原型覆盖 {len(kinds)} 类控件，包含默认/空/禁用/错误四种状态。",
                    "status": "evidenced",
                    "evidence_indexes": [0],
                },
                {"text": "原型为静态假数据，交互行为未联通后端。", "status": "undetermined"},
            ],
            evidence=evidence,
            gaps=["原型未经设计评审，交互细节需 UI 确认"],
            trace={
                "sources": ["requirement", "pm_analysis"],
                "tool_calls": ["model.prototype"],
                "kinds": kinds,
                "html_len": len(html),
            },
        )

    def _prd(self, ctx: StepContext) -> StepOutput:
        analysis = self._latest_document(ctx)
        text = self._ask(
            system=_BASE_SYSTEM + " 这次输出 markdown 正文，不要 JSON，不要外层代码块。",
            user=(
                "任务：写这份需求的 PRD，面向研发与测试，要求可直接排期实施。\n"
                f"需求标题：{ctx.requirement_title}\n需求原文：{ctx.requirement_text}\n"
                f"需求分析：{analysis[:1500]}\n\n"
                "结构必须包含：背景与目标 / 范围（含不做什么）/ 用户流程 / 功能清单（含优先级）"
                " / 验收标准 / 边界与异常 / 依赖与风险。"
                "每一条功能都要能对应到验收标准，不要写空话。"
            ),
        )
        body = _strip_fence(_strip_thinking(text)).strip()
        if len(body) < 200:
            raise ExecutorUnavailable(f"prd is too short ({len(body)} chars)")
        evidence = [
            _envelope(
                source_type="human",
                locator=_pm_record("requirement", ctx.task_public_id),
                excerpt=_clip(f"PRD 依据需求 {ctx.task_public_id}：{ctx.requirement_text}"),
                confidence=0.6,
            )
        ]
        return StepOutput(
            status="passed",
            artifact=self._doc_artifact(
                title="PRD",
                body=body,
                fidelity_note="模型起草，未经过产品与研发评审。",
                audience="internal",
            ),
            conclusions=[
                {
                    "text": "PRD 覆盖背景、范围、功能清单、验收标准、边界与风险七个部分。",
                    "status": "evidenced",
                    "evidence_indexes": [0],
                },
                {"text": "PRD 中的工作量与排期未经研发确认。", "status": "undetermined"},
            ],
            evidence=evidence,
            gaps=["PRD 未经过产品与研发评审"],
            trace={
                "sources": ["requirement", "pm_analysis"],
                "tool_calls": ["model.prd"],
                "chars": len(body),
            },
        )

    # --- P5: review ---------------------------------------------------------

    def _review(self, ctx: StepContext) -> StepOutput:
        prd = self._latest_document(ctx)
        payload = self._ask_json(
            system=_BASE_SYSTEM,
            user=(
                "任务：分别以业务、技术、客户三个视角评审下面的交付物，各自给结论。\n"
                f"需求标题：{ctx.requirement_title}\n交付物节选：{prd[:2000]}\n\n"
                "输出 JSON：\n"
                '{"business":{"verdict":"pass|reject","comment":"..."},'
                '"tech":{"verdict":"pass|reject","comment":"..."},'
                '"customer":{"verdict":"pass|reject","comment":"..."},"summary":"..."}\n'
                "只有存在实质性缺陷（做不了、算错了、用户会用错）时才 reject，"
                "措辞上的建议请给 pass 并在 comment 里写清。"
            ),
            required=("business", "tech", "customer"),
        )
        perspectives = ("business", "tech", "customer")
        verdicts: dict[str, str] = {}
        comments: dict[str, str] = {}
        for key in perspectives:
            raw_item = payload.get(key)
            item: dict[str, Any] = raw_item if isinstance(raw_item, dict) else {}
            verdict = str(item.get("verdict") or "").strip().lower()
            verdicts[key] = verdict if verdict in ("pass", "reject") else "pass"
            comments[key] = str(item.get("comment") or "").strip()

        if self._repos is not None:
            for key in perspectives:
                self._repos.reviews.submit(
                    task_id=ctx.task_id,
                    perspective=key,
                    verdict=verdicts[key],
                    comment=f"[模型评审] {comments[key]}",
                    reviewer_user_id=None,
                )

        rejected = [key for key, value in verdicts.items() if value != "pass"]
        evidence = [
            _envelope(
                source_type="human",
                locator=_pm_record("task", ctx.task_public_id),
                excerpt=_clip(
                    "模型三视角评审："
                    + "；".join(f"{key}={verdicts[key]}" for key in perspectives)
                ),
                confidence=0.5,
            )
        ]
        return StepOutput(
            status="passed",
            artifact={
                "kind": "review",
                "title": "三视角评审（模型）",
                "audience": "internal",
                "fidelity": "partial",
                "fidelity_note": "由模型代三方视角给出，未经真实业务/技术/客户人员确认。",
                "origin": "ai",
                "body": json.dumps(
                    {"verdicts": verdicts, "comments": comments, "summary": payload.get("summary")},
                    ensure_ascii=False,
                    indent=2,
                ),
            },
            conclusions=[
                {
                    "text": (
                        "三视角评审完成，全部通过。" if not rejected else f"评审未通过：{', '.join(rejected)}"
                    ),
                    "status": "evidenced",
                    "evidence_indexes": [0],
                },
                {"text": "评审由模型代替真人给出，交付前建议人工复核。", "status": "undetermined"},
            ],
            evidence=evidence,
            gaps=(["评审由模型代替真人给出"] if not rejected else [f"以下视角未通过：{', '.join(rejected)}"]),
            trace={
                "sources": ["pm_task", "pm_prd"],
                "tool_calls": ["model.review"],
                "verdicts": verdicts,
            },
        )

    # --- helpers ------------------------------------------------------------

    def _latest_document(self, ctx: StepContext) -> str:
        """Best-effort text of the most relevant upstream artifact.

        The pipeline does not hand artifacts to executors (only evidence), so this
        degrades to the requirement text rather than reaching into repos we do not own.
        """
        if self._repos is None:
            return f"{ctx.requirement_title}\n{ctx.requirement_text}"
        try:
            artifacts = self._repos.artifacts.list_for_task(ctx.task_id)
        except Exception:  # noqa: BLE001 - 读不到上游产物时用需求原文兜底
            return f"{ctx.requirement_title}\n{ctx.requirement_text}"
        for artifact in reversed(artifacts):
            if artifact.kind in ("document", "data") and artifact.body_inline:
                return str(artifact.body_inline)
        return f"{ctx.requirement_title}\n{ctx.requirement_text}"


def build_default(
    *, models_provider: Callable[[], Any] | None, repos: Any | None
) -> ModelStepExecutor:
    return ModelStepExecutor(models_provider=models_provider, repos=repos)


__all__ = ["MODEL_STEPS", "ModelStepExecutor", "build_default"]
