"""Locating a page and deriving it: the two halves that make a prototype honest.

The rules this file pins down:

* a missing graph, a missing mirror or an unsynced i18n catalogue each produce a
  *different, readable* reason — never an empty string and never a guess;
* two near-equal candidates are reported as ambiguous, because picking one and
  saying nothing would look like knowledge;
* the prototype step marks a derived artifact ``origin="source"`` and a
  model-drawn one ``origin="ai"``, and the model path has to carry the reason
  derivation did not happen.

The end-to-end prototype tests inject a fake deriver and a fake model, so they
assert the branch that was taken rather than the quality of a rendering.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from octop.infra.pm.domain.locate_evidence import (
    WEIGHT_COMPONENT,
    WEIGHT_DESCRIPTION,
    WEIGHT_SUMMARY,
    EvidenceSource,
    LocateEvidence,
)
from octop.infra.pm.domain.repos import REPOS
from octop.infra.pm.ports.graph import GraphHit
from octop.infra.pm.runtime import prototype_derive
from octop.infra.pm.runtime.executor import StepContext
from octop.infra.pm.runtime.model_executor import ModelStepExecutor
from octop.infra.pm.runtime.prototype_derive import (
    DerivationOutcome,
    PageCandidate,
    PageLocation,
    _climb_to_pages,
    derive_from_source,
    ensure_render_script,
    locate_page,
    missing_packages,
)
from octop.infra.pm.runtime.prototype_source import DerivedPrototype

PAGE = "src/views/vendor/list/index.vue"
OTHER_PAGE = "src/views/vendor/settings/index.vue"

ZH_CATALOGUE = """
export default {
  vendor: '供应商',
  readyState: '就绪状态',
  route: '路由',
}
"""


def _graph_payload(labels_by_file: dict[str, list[str]]) -> dict[str, Any]:
    nodes = []
    edges = []
    for index, (source_file, labels) in enumerate(labels_by_file.items()):
        for j, label in enumerate(labels):
            node_id = f"{label}_{index}_{j}"
            nodes.append({"id": node_id, "label": label, "source_file": source_file})
            if j:
                edges.append({"source": nodes[-2]["id"], "target": node_id, "relation": "uses"})
    return {"nodes": nodes, "edges": edges}


def _repo(tmp_path: Path, *, graph: dict[str, Any] | None = None) -> Path:
    root = tmp_path / "repo"
    (root / "src" / "i18n" / "modules" / "zh-CN").mkdir(parents=True)
    (root / "src" / "i18n" / "modules" / "zh-CN" / "vendor.ts").write_text(
        ZH_CATALOGUE, encoding="utf-8"
    )
    for page in (PAGE, OTHER_PAGE):
        path = root / page
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '<template><div class="page"><a-table /></div></template>'
            '<script setup lang="ts">import { vendorList } from "@/api/vendor"\n'
            "export default { vendorList }</script>",
            encoding="utf-8",
        )
    api = root / "src" / "api" / "vendor.ts"
    api.parent.mkdir(parents=True, exist_ok=True)
    api.write_text("export const vendorList = []\n", encoding="utf-8")
    if graph is not None:
        (root / "graphify-out").mkdir(parents=True)
        (root / "graphify-out" / "graph.json").write_text(
            json.dumps(graph, ensure_ascii=False), encoding="utf-8"
        )
    prototype_derive.clear_cache()
    return root


def _spec(key: str, root: Path) -> Any:
    spec = REPOS[key]
    object.__setattr__(spec, "mirror", str(root))
    return spec


def _ctx(**overrides: Any) -> StepContext:
    base: dict[str, Any] = {
        "task_id": 1,
        "task_public_id": "TSK-1",
        "requirement_id": 1,
        "requirement_title": "派单供应商列表加就绪状态筛选",
        "requirement_text": "在供应商列表页增加就绪状态筛选，支持按路由维度过滤。",
        "workspace_id": 1,
        "step_key": "prototype",
        "seq": 8,
        "threshold": 80,
        "decision_level": "L2",
        "decision_timeout_s": 300,
        "source_repo": {
            "project": "CCTOWER",
            "mapped": True,
            "system": "cargowarex",
            "web_repo": "cwx-web",
            "mirror_ready": True,
        },
    }
    base.update(overrides)
    return StepContext(**base)


class _FakeAccess:
    def __init__(self, text: str) -> None:
        self._text = text

    async def ainvoke_text(self, user: str, *, system: str = "", timeout_s: float = 0) -> str:
        return self._text


class _FakeFactory:
    def __init__(self, text: str) -> None:
        self._text = text

    def get(self, model: str | None = None) -> _FakeAccess:
        return _FakeAccess(self._text)


_DRAWN_HTML = (
    '<div><style>.x{color:red}</style><form><input /></form><table></table></div>'
)


# --- locating ---------------------------------------------------------------


def test_missing_graph_is_reported_as_missing_not_as_no_match(tmp_path: Path) -> None:
    root = _repo(tmp_path)

    location = locate_page(root, "供应商列表加就绪状态筛选")

    assert location.ok is False
    assert "图谱" in location.notes[0]
    assert location.method == "none"


def test_i18n_keys_bridge_the_chinese_requirement_to_the_page(tmp_path: Path) -> None:
    # One page only: the question here is whether Chinese text reaches the code's
    # identifiers at all, not which of two sibling pages wins that argument.
    root = _repo(
        tmp_path,
        graph=_graph_payload(
            {
                PAGE: ["VendorTable", "vendorList"],
                "src/utils/format.ts": ["formatDate"],
            }
        ),
    )

    location = locate_page(root, "派单供应商列表加就绪状态筛选")

    assert location.ok is True
    assert location.method == "i18n+graph"
    assert location.page == PAGE
    # The terms handed to the graph are the catalogue's own keys.
    assert "vendor" in location.terms


def test_a_non_renderable_match_is_never_offered_as_a_page(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        graph=_graph_payload({"src/utils/vendor-helper.ts": ["vendorHelper"]}),
    )

    location = locate_page(root, "供应商列表加就绪状态筛选")

    # .ts is a module, not a screen: it cannot be rendered into a prototype.
    assert location.ok is False
    assert "可渲染" in location.notes[-1]


def test_near_equal_candidates_are_refused_rather_than_guessed(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        graph=_graph_payload({PAGE: ["VendorTable"], OTHER_PAGE: ["VendorTable"]}),
    )

    location = locate_page(root, "供应商列表加就绪状态筛选")

    # Two equally plausible pages: deriving either one would hand the reviewer a
    # confident, wrong screen, so the locator declines and names the shortlist.
    assert location.ok is False
    assert "候选不唯一" in location.notes[0]
    assert PAGE in location.notes[0]


def test_a_clear_winner_is_not_flagged_as_ambiguous(tmp_path: Path) -> None:
    """A page that only *mentions* a common word is not a rival candidate.

    Sibling pages under the same directory are genuinely indistinguishable by
    this method and are refused (see the test above). The distinction that keeps
    the refusals rare is that a weak mention never becomes a candidate at all.
    """

    root = _repo(
        tmp_path,
        graph=_graph_payload(
            {
                PAGE: ["VendorTable"],
                # Shares the substring in a symbol name but neither matches a
                # symbol exactly nor appears in its own path: a weak hit, and a
                # weak hit is discarded before ranking rather than ranked low.
                "src/views/settings/index.vue": ["vendorSettingsHelper"],
            }
        ),
    )

    location = locate_page(root, "派单供应商列表加就绪状态筛选")

    assert location.ok is True
    assert location.ambiguous is False
    assert location.page == PAGE


class _StubGraph:
    """Just enough of GraphIndex for the climb step."""

    def __init__(self, importers: dict[str, list[str]]) -> None:
        self._importers = importers

    def importers(self, source_file: str, *, max_files: int = 8) -> list[str]:
        return self._importers.get(source_file, [])[:max_files]


def _hit(path: str, score: float) -> GraphHit:
    return GraphHit(source_file=path, score=score)


def test_climb_accepts_a_page_whose_path_carries_a_term() -> None:
    page = "src/views/vendor/list/index.vue"
    graph = _StubGraph({"src/api/vendor.ts": [page]})

    candidates, climbed = _climb_to_pages(graph, [_hit("src/api/vendor.ts", 12.0)], ["vendor"], 5)

    assert climbed == page
    assert abs(candidates[0].score - 7.2) < 1e-6


def test_climb_rejects_a_page_whose_path_carries_no_term() -> None:
    """`some page imports this enum` is not evidence that it is *the* page.

    Measured on the real mirror: a supplier requirement's terms landed in
    ``src/enums/app-management.ts``, which the app-access page imports. Accepting
    that page would have rendered a confident, wrong screen.
    """

    graph = _StubGraph({"src/enums/app-management.ts": ["src/views/app-management/app-access/index.vue"]})

    candidates, climbed = _climb_to_pages(
        graph, [_hit("src/enums/app-management.ts", 15.5)], ["vendor", "supplierName"], 5
    )

    assert candidates == ()
    assert climbed == ""


# --- renderer placement -----------------------------------------------------


def test_the_packaged_renderer_is_copied_into_the_mirror(tmp_path: Path) -> None:
    root = tmp_path / "mirror"
    root.mkdir()

    placed = ensure_render_script(root)

    assert placed is not None
    assert placed == root / "render.mjs"
    assert "renderToString" in placed.read_text(encoding="utf-8")


def test_a_stale_copy_is_overwritten_so_the_wheel_stays_authoritative(tmp_path: Path) -> None:
    root = tmp_path / "mirror"
    root.mkdir()
    (root / "render.mjs").write_text("// old version\n", encoding="utf-8")

    placed = ensure_render_script(root)

    assert placed is not None
    assert "old version" not in placed.read_text(encoding="utf-8")


def test_both_renderers_ship_with_the_wheel() -> None:
    """Writing a renderer that never reaches the package is a silent no-op."""

    assets = Path(prototype_derive.__file__).parent / "assets"
    for name in ("render.mjs", "render.vue2.mjs"):
        assert (assets / name).is_file(), name


def test_the_vue2_renderer_goes_into_the_toolchain_not_the_mirror(tmp_path: Path) -> None:
    """A node_modules of our own inside a product's tree would be wiped by sync."""

    tool = tmp_path / "tool"

    placed = ensure_render_script(tool, "render.vue2.mjs")

    assert placed == tool / "render.vue2.mjs"
    assert "vue-server-renderer" in placed.read_text(encoding="utf-8")


# --- what has to be installed before node is worth spawning ------------------

VUE2_PACKAGES = ("vue", "vue-template-compiler", "element-ui", "vue-router", "vuex", "vue-i18n")


def _installed(base: Path, packages: tuple[str, ...]) -> None:
    """Give a directory the node_modules a renderer looks for."""

    for name in packages:
        (base / "node_modules" / name).mkdir(parents=True, exist_ok=True)


def test_a_mirror_without_dependencies_still_yields_a_baseline(tmp_path: Path) -> None:
    """A freshly cloned mirror has no node_modules — that must not stop the step.

    It used to be a hard refusal, which was the wrong trade: the source needed
    to write the prototype is in the checkout whether or not anyone ran `npm
    install`, and dependencies are only needed to *render* the result for a
    fidelity measurement. Refusing told the operator to install 81 packages to
    do work that needs none of them.
    """

    root = _routed_repo(tmp_path)
    evidence = LocateEvidence(
        sources=(EvidenceSource("component", "Jira 组件", "供应商管理", WEIGHT_COMPONENT),)
    )
    spec = _spec("cw-web", root)
    try:
        outcome = derive_from_source(spec, "", evidence=evidence, timeout_s=1)
    finally:
        object.__setattr__(spec, "mirror", "/opt/pm-mirror/cw-web")

    assert outcome.ok is True
    assert outcome.baseline is not None
    assert outcome.baseline.usable
    assert outcome.baseline.page.endswith("vendor/index.vue")
    assert {"page", "page__filter", "page__table"} <= set(outcome.baseline.classes)
    # The copy is resolved through the language pack when one exists; here there
    # is none, so the page's own literal Chinese is what anchors the fidelity.
    assert outcome.baseline.anchors
    # No render happened, so there is nothing to measure — and the outcome says
    # so rather than reporting a fabricated zero.
    assert outcome.prototype is None
    assert outcome.verified is False


def test_a_missing_vue2_toolchain_is_named_when_verification_is_asked_for(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Asking for a render without the toolchain costs the measurement only.

    The reason must name the missing packages: "I could not verify" and "the
    page is broken" send an operator to different fixes.
    """

    root = _routed_repo(tmp_path)
    _installed(root, VUE2_PACKAGES)
    monkeypatch.setenv("OCTOP_PM_SSR_TOOLCHAIN", str(tmp_path / "tool"))
    evidence = LocateEvidence(
        sources=(EvidenceSource("component", "Jira 组件", "供应商管理", WEIGHT_COMPONENT),)
    )

    spec = _spec("cw-web", root)
    try:
        outcome = derive_from_source(
            spec, "", evidence=evidence, timeout_s=1, verify=True
        )
    finally:
        object.__setattr__(spec, "mirror", "/opt/pm-mirror/cw-web")

    assert outcome.ok is True
    assert outcome.baseline is not None
    assert outcome.prototype is None
    assert "未做渲染校验" in outcome.reason
    assert "vite" in outcome.reason


def test_a_hoisted_node_modules_one_level_up_is_accepted(tmp_path: Path) -> None:
    """A shared install above the mirror is a real layout, not a mistake."""

    mirror = tmp_path / "mirror"
    mirror.mkdir()
    _installed(tmp_path, ("vue",))

    assert missing_packages(mirror, ("vue",)) == []
    assert missing_packages(mirror, ("vite",)) == ["vite"]


# --- deriving ---------------------------------------------------------------


def test_an_unsynced_mirror_says_so_instead_of_returning_empty_html(tmp_path: Path) -> None:
    spec = _spec("cwx-web", tmp_path / "absent")
    try:
        outcome = derive_from_source(spec, "供应商列表加筛选", timeout_s=1)
    finally:
        object.__setattr__(spec, "mirror", "/opt/pm-mirror/cwx-web")

    assert outcome.ok is False
    assert "未同步" in outcome.reason
    assert outcome.prototype is None


def test_an_api_repository_is_refused_because_it_cannot_be_rendered(tmp_path: Path) -> None:
    spec = _spec("cwx-api", tmp_path)
    try:
        outcome = derive_from_source(spec, "供应商列表加筛选", timeout_s=1)
    finally:
        object.__setattr__(spec, "mirror", "/opt/pm-mirror/cwx-api")

    assert outcome.ok is False
    assert "不是前端仓库" in outcome.reason


# --- the prototype step -----------------------------------------------------


def test_a_derived_prototype_is_exact_and_cites_the_page(tmp_path: Path) -> None:
    prototype = DerivedPrototype(
        html="<html><style>.a{}</style><form><input></form><table></table></html>",
        source_page=f"/{PAGE}",
        repo_key="cwx-web",
        git_ref="abc1234",
        class_overlap=1.0,
        classes_source=12,
        classes_rendered=12,
        styles_inlined=2,
        fidelity="exact",
        fidelity_note="由 cwx-web 的页面经 Vue SSR 渲染生成",
    )
    location = PageLocation(
        candidates=(PageCandidate(source_file=PAGE, score=42.0),),
        terms=("vendor", "readyState"),
        method="i18n+graph",
    )

    def fake(source: dict[str, Any], query: str, evidence: Any = None) -> DerivationOutcome:
        assert "供应商" in query
        return DerivationOutcome(ok=True, prototype=prototype, location=location)

    executor = ModelStepExecutor(repos=None, prototype_source=fake)
    output = executor.run(_ctx())

    assert output.artifact is not None
    assert output.artifact["origin"] == "source"
    assert output.artifact["fidelity"] == "exact"
    assert output.artifact["source_repo"]["derived"] is True
    assert output.artifact["source_repo"]["page"] == PAGE
    # The code evidence points at the file that was actually rendered.
    code_evidence = [item for item in output.evidence if item["source_type"] == "code"]
    assert code_evidence and code_evidence[0]["locator"]["path"] == PAGE


def test_an_ambiguous_page_lowers_confidence_and_raises_a_gap(tmp_path: Path) -> None:
    prototype = DerivedPrototype(
        html="<html><style>.a{}</style><form><input></form><table></table></html>",
        source_page=f"/{PAGE}",
        repo_key="cwx-web",
        git_ref="",
        class_overlap=1.0,
        classes_source=3,
        classes_rendered=3,
        styles_inlined=1,
        fidelity="exact",
        fidelity_note="派生成功",
    )
    location = PageLocation(
        candidates=(
            PageCandidate(source_file=PAGE, score=42.0),
            PageCandidate(source_file=OTHER_PAGE, score=40.0),
        ),
        terms=("vendor",),
        method="i18n+graph",
    )
    executor = ModelStepExecutor(
        repos=None,
        prototype_source=lambda source, query, evidence=None: DerivationOutcome(
            ok=True, prototype=prototype, location=location
        ),
    )

    output = executor.run(_ctx())

    assert output.artifact is not None
    assert output.artifact["source_repo"]["page_ambiguous"] is True
    # A perfect render of *a* page is not 1:1 with *this requirement* when the
    # locator could not tell which page it was.
    assert output.artifact["fidelity"] == "partial"
    assert any("页面定位不唯一" in gap for gap in output.gaps)
    assert "可能对应的是另一个页面" in (output.artifact["fidelity_note"] or "")
    assert any(item["status"] == "uncovered" for item in output.conclusions)


def test_a_failed_derivation_falls_back_and_names_the_reason() -> None:
    executor = ModelStepExecutor(
        models_provider=lambda: _FakeFactory(_DRAWN_HTML),
        repos=None,
        prototype_source=lambda source, query, evidence=None: DerivationOutcome(
            ok=False, reason="源码镜像未同步：/opt/pm-mirror/cwx-web 不存在"
        ),
    )

    output = executor.run(_ctx())

    assert output.artifact is not None
    assert output.artifact["origin"] == "ai"
    # A model that has never seen the product may not claim exact fidelity.
    assert output.artifact["fidelity"] == "partial"
    assert "未从源码派生" in (output.artifact["fidelity_note"] or "")
    assert "镜像未同步" in (output.artifact["fidelity_note"] or "")
    assert any("未从源码派生" in gap for gap in output.gaps)


def test_the_gate_rejects_a_model_drawn_page_claiming_exact_fidelity() -> None:
    """G-P4-05 is what makes the ``exact`` label mean something."""

    from octop.infra.pm.domain import gates

    drawn = gates.GateContext(
        step_key="prototype",
        stage="P4",
        prototype_html="<html><style></style><form><table></table></form></html>",
        prototype_origin="ai",
        fidelity="exact",
        conclusions=({"status": "uncovered"},),
    )
    report = gates.evaluate(drawn)

    failed = [item for item in report.results if item.rule_id == "G-P4-05"]
    assert failed and failed[0].passed is False


# --- multi-source location --------------------------------------------------
#
# The failure this section exists for, measured on the real mirror: a ticket
# whose *body* mentioned two unrelated screens was located on one of them, while
# the Jira component field — which named the right area outright — was never
# read. Per-source weighting is what turns that around.

MULTI_I18N = """
export default {
  vendor: '供应商',
  orderList: '订单列表',
  templateList: '模板列表',
  zipcode: '邮政编码',
}
"""

VENDOR_PAGE = "src/views/vendor/list/index.vue"
ORDER_PAGE = "src/views/order/list/index.vue"
TEMPLATE_PAGE = "src/views/template/list/index.vue"

NOISY_BODY = "订单列表和模板列表的筛选体验不好，参考邮政编码那边的做法"


def _multi_repo(tmp_path: Path) -> Path:
    root = _repo(
        tmp_path,
        graph=_graph_payload(
            {
                VENDOR_PAGE: ["VendorTable"],
                ORDER_PAGE: ["orderList"],
                TEMPLATE_PAGE: ["templateList"],
            }
        ),
    )
    (root / "src/i18n/modules/zh-CN/vendor.ts").write_text(MULTI_I18N, encoding="utf-8")
    for page in (ORDER_PAGE, TEMPLATE_PAGE):
        path = root / page
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("<template><div/></template>", encoding="utf-8")
    prototype_derive.clear_cache()
    return root


def test_a_component_field_rescues_a_ticket_whose_body_names_other_screens(
    tmp_path: Path,
) -> None:
    root = _multi_repo(tmp_path)
    evidence = LocateEvidence(
        sources=(
            EvidenceSource("summary", "标题", "列表筛选优化", WEIGHT_SUMMARY),
            EvidenceSource("component", "Jira 组件", "供应商管理", WEIGHT_COMPONENT),
            EvidenceSource("description", "描述", NOISY_BODY, WEIGHT_DESCRIPTION),
        )
    )

    without = locate_page(root, f"列表筛选优化\n{NOISY_BODY}")
    with_evidence = locate_page(root, "", evidence=evidence)

    # Without the ticket's structure the body wins, and it points at two screens
    # that are both about something other than the requirement.
    assert without.ok is False
    assert any("候选不唯一" in note for note in without.notes)
    # With it, the component field overrules the body entirely.
    assert with_evidence.page == VENDOR_PAGE
    assert with_evidence.ok is True


def test_the_reason_a_page_was_chosen_names_the_field_it_came_from(
    tmp_path: Path,
) -> None:
    """A reviewer must be able to see *why* this page, not just which page."""

    root = _multi_repo(tmp_path)
    evidence = LocateEvidence(
        sources=(
            EvidenceSource("component", "Jira 组件", "供应商管理", WEIGHT_COMPONENT),
        )
    )

    location = locate_page(root, "", evidence=evidence)

    assert location.page == VENDOR_PAGE
    assert any("Jira 组件" in reason for reason in location.candidates[0].reasons)


def test_an_unreadable_screenshot_is_reported_not_silently_dropped(
    tmp_path: Path,
) -> None:
    root = _multi_repo(tmp_path)
    evidence = LocateEvidence(
        sources=(EvidenceSource("component", "Jira 组件", "供应商管理", WEIGHT_COMPONENT),),
        unread=("附件 image (3).png 的文件名不含页面信息，且未做图内文字识别",),
    )

    location = locate_page(root, "", evidence=evidence)

    assert any("未做图内文字识别" in note for note in location.notes)


def test_the_prototype_step_hands_the_ticket_fields_to_the_deriver() -> None:
    """The deriver is useless if the executor never passes the evidence down."""

    seen: dict[str, Any] = {}
    evidence = LocateEvidence(
        sources=(EvidenceSource("component", "Jira 组件", "供应商管理", WEIGHT_COMPONENT),)
    )

    def fake(source: dict[str, Any], query: str, passed: Any = None) -> DerivationOutcome:
        seen["evidence"] = passed
        return DerivationOutcome(ok=False, reason="只验证入参")

    executor = ModelStepExecutor(
        models_provider=lambda: _FakeFactory(_DRAWN_HTML),
        repos=None,
        prototype_source=fake,
    )
    executor.run(_ctx(locate_evidence=evidence))

    assert seen["evidence"] is evidence


# --- the router as the first anchor ------------------------------------------

ROUTED = """
export const routes = [
  { path: '/order', name: 'Order', component: () => import('@/views/order/index.vue'),
    meta: { title: '订单中心' } },
  { path: '/vendor', name: 'Vendor', component: () => import('@/views/vendor/index.vue'),
    meta: { title: '供应商管理' } },
]
"""


# A page fixture that is shaped like a real one rather than a two-line stub.
# The baseline now refuses anything that is too thin to describe a screen (three
# classes, some copy and a form's worth of markup), because a 35-character page
# transcribes to an empty frame that would still be labelled 1:1 — so the fixture
# has to carry enough structure for the "is it usable" question to be meaningful.
ROUTED_PAGE = """<template>
  <div class="page">
    <a-form :model="query" layout="inline" class="page__filter">
      <a-form-item label="供应商编码"><a-input v-model="query.code" /></a-form-item>
      <a-form-item label="供应商名称"><a-input v-model="query.name" /></a-form-item>
      <a-form-item label="所属区域"><a-select v-model="query.region" /></a-form-item>
      <a-form-item label="合作状态"><a-select v-model="query.status" /></a-form-item>
      <a-form-item label="创建时间"><a-range-picker v-model="query.createdAt" /></a-form-item>
      <a-button type="primary" @click="search">查询</a-button>
      <a-button @click="reset">重置</a-button>
      <a-button type="outline" @click="create">新建供应商</a-button>
    </a-form>
    <a-table :columns="columns" :data="rows" class="page__table" row-key="code">
      <template #status="{ record }"><a-tag>{{ record.status }}</a-tag></template>
      <template #actions="{ record }">
        <a-link @click="edit(record)">编辑</a-link>
        <a-link @click="remove(record)">停用</a-link>
      </template>
    </a-table>
    <a-pagination :total="total" show-total class="page__pager" />
  </div>
</template>

<script>
export default {
  name: 'VendorIndex',
  data() {
    return { query: {}, rows: [], total: 0, columns: [{ title: '供应商编码', dataIndex: 'code' }] }
  },
  methods: { search() {}, reset() {}, create() {}, edit() {}, remove() {} },
}
</script>
"""


def _routed_repo(tmp_path: Path, *, graph: dict[str, Any] | None = None) -> Path:
    """A mirror with a router but, unless asked, no code graph at all."""

    root = tmp_path / "routed"
    router = root / "control-tower" / "src" / "router"
    router.mkdir(parents=True)
    (router / "routes.ts").write_text(ROUTED, encoding="utf-8")
    for name in ("vendor", "order"):
        page = root / "control-tower" / "src" / "views" / name / "index.vue"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(ROUTED_PAGE, encoding="utf-8")
    if graph is not None:
        (root / "graphify-out").mkdir(parents=True)
        (root / "graphify-out" / "graph.json").write_text(
            json.dumps(graph, ensure_ascii=False), encoding="utf-8"
        )
    prototype_derive.clear_cache()
    return root


def test_a_component_resolves_with_no_code_graph_at_all(tmp_path: Path) -> None:
    """A component and a route title are the same word, so the graph is optional."""

    root = _routed_repo(tmp_path)
    evidence = LocateEvidence(
        sources=(EvidenceSource("component", "Jira 组件", "供应商管理", WEIGHT_COMPONENT),)
    )

    location = locate_page(root, "", evidence=evidence)

    assert location.ok is True
    assert location.method == "route"
    # Repo-relative: the deriver reads root/page and then hands it to Vite.
    assert location.page == "control-tower/src/views/vendor/index.vue"
    assert location.ambiguous is False


def test_the_router_outranks_the_graph_when_both_could_answer(tmp_path: Path) -> None:
    """The graph matches keywords; the router matches the product's own menu name."""

    root = _routed_repo(
        tmp_path,
        graph=_graph_payload({"control-tower/src/views/order/index.vue": ["vendorList"]}),
    )
    evidence = LocateEvidence(
        sources=(
            EvidenceSource("component", "Jira 组件", "供应商管理", WEIGHT_COMPONENT),
            EvidenceSource("description", "描述", "供应商列表加就绪状态筛选", WEIGHT_DESCRIPTION),
        )
    )

    location = locate_page(root, "", evidence=evidence)

    assert location.method == "route"
    assert location.page.endswith("views/vendor/index.vue")


def test_a_component_the_router_cannot_place_is_reported_not_fabricated(
    tmp_path: Path,
) -> None:
    """No route by that name is a fact about the router, and it gets said out loud."""

    root = _routed_repo(
        tmp_path,
        graph=_graph_payload({"control-tower/src/views/order/index.vue": ["vendorList"]}),
    )
    evidence = LocateEvidence(
        sources=(EvidenceSource("component", "Jira 组件", "供应商管理", WEIGHT_COMPONENT),)
    )
    # Rename the route so the component has no counterpart.
    (root / "control-tower" / "src" / "router" / "routes.ts").write_text(
        ROUTED.replace("供应商管理", "客商管理"), encoding="utf-8"
    )
    prototype_derive.clear_cache()

    location = locate_page(root, "", evidence=evidence)

    assert location.method != "route"
    assert location.ok is False
    assert any("没有同名页面" in note for note in location.notes)
