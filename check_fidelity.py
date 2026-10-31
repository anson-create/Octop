"""Verify the ambiguity cap without pytest (the sandbox forbids tempdirs)."""

from octop.infra.pm.runtime.executor import StepContext
from octop.infra.pm.runtime.model_executor import ModelStepExecutor
from octop.infra.pm.runtime.prototype_derive import (
    DerivationOutcome,
    PageCandidate,
    PageLocation,
)
from octop.infra.pm.runtime.prototype_source import DerivedPrototype

PAGE = "src/views/vendor/list/index.vue"
OTHER = "src/views/vendor/settings/index.vue"

HTML = "<html><style>.a{}</style><form><input></form><table></table></html>"


def prototype(overlap: float = 1.0) -> DerivedPrototype:
    return DerivedPrototype(
        html=HTML,
        source_page=f"/{PAGE}",
        repo_key="cwx-web",
        git_ref="abc1234",
        class_overlap=overlap,
        classes_source=10,
        classes_rendered=10,
        styles_inlined=2,
        fidelity="exact" if overlap >= 0.9 else "partial",
        fidelity_note="由 cwx-web 的页面经 Vue SSR 渲染生成",
    )


def ctx() -> StepContext:
    return StepContext(
        task_id=1,
        task_public_id="TSK-1",
        requirement_id=1,
        requirement_title="供应商列表加就绪状态筛选",
        requirement_text="在供应商列表页增加就绪状态筛选。",
        workspace_id=1,
        step_key="prototype",
        seq=8,
        threshold=80,
        decision_level="L2",
        decision_timeout_s=300,
        source_repo={
            "project": "CCTOWER",
            "mapped": True,
            "system": "cargowarex",
            "web_repo": "cwx-web",
            "mirror_ready": True,
        },
    )


def run(location: PageLocation, proto: DerivedPrototype):
    executor = ModelStepExecutor(
        repos=None,
        prototype_source=lambda source, query: DerivationOutcome(
            ok=True, prototype=proto, location=location
        ),
    )
    return executor.run(ctx())


# 1. Unique page + perfect render => exact
unique = PageLocation(
    candidates=(PageCandidate(source_file=PAGE, score=42.0),),
    terms=("vendor",),
    method="i18n+graph",
)
out = run(unique, prototype())
assert out.artifact["fidelity"] == "exact", out.artifact["fidelity"]
assert out.artifact["source_repo"]["page_ambiguous"] is False
assert not any("页面定位不唯一" in g for g in out.gaps)
print("unique page -> fidelity=exact, no ambiguity gap  OK")

# 2. Ambiguous page + perfect render => capped to partial, candidates listed
ambiguous = PageLocation(
    candidates=(
        PageCandidate(source_file=PAGE, score=42.0),
        PageCandidate(source_file=OTHER, score=40.0),
    ),
    terms=("vendor",),
    method="i18n+graph",
)
out2 = run(ambiguous, prototype())
assert out2.artifact["fidelity"] == "partial", out2.artifact["fidelity"]
assert out2.artifact["source_repo"]["page_ambiguous"] is True
gaps = " ".join(out2.gaps)
assert "页面定位不唯一" in gaps
assert OTHER in gaps, gaps
assert any(c["status"] == "uncovered" for c in out2.conclusions)
print("ambiguous page -> fidelity=partial, shortlist in gaps, uncovered conclusion  OK")

# 3. Ambiguous page + imperfect render keeps partial (no promotion)
out3 = run(ambiguous, prototype(0.79))
assert out3.artifact["fidelity"] == "partial"
print("ambiguous + 79% overlap -> partial  OK")

# 4. Gate refuses a model-drawn page claiming exact fidelity
from octop.infra.pm.domain import gates

drawn = gates.GateContext(
    step_key="prototype",
    stage="P4",
    prototype_html=HTML,
    prototype_origin="ai",
    fidelity="exact",
    conclusions=({"status": "uncovered"},),
)
res = [r for r in gates.evaluate(drawn).results if r.rule_id == "G-P4-05"]
assert res and res[0].passed is False
print("G-P4-05 rejects origin=ai + fidelity=exact  OK")

# 5. Derived artifact passes the same gate
derived = gates.GateContext(
    step_key="prototype",
    stage="P4",
    prototype_html=HTML,
    prototype_origin="source",
    fidelity="exact",
    conclusions=({"status": "uncovered"},),
)
res2 = [r for r in gates.evaluate(derived).results if r.rule_id == "G-P4-05"]
assert res2 and res2[0].passed is True
print("G-P4-05 accepts origin=source + fidelity=exact  OK")

print("\nALL CHECKS PASSED")
