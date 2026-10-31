"""The pipeline definition — the single source of truth.

Five stages over thirteen steps. Everything else (DB seed, gate routing,
frontend progress bar, metrics) derives from here. Scattered step lists are what
produced the "canvas gap" defect in the reference build; there is exactly one
copy, and ``tests/unit/pm/test_stages_seed.py`` fails if the DB seed drifts from
it.
"""

from __future__ import annotations

from dataclasses import dataclass

WORKFLOW_KEY = "wf-csm-solution"


@dataclass(frozen=True)
class StageSpec:
    key: str
    title_zh: str
    title_en: str
    purpose_zh: str


@dataclass(frozen=True)
class StepSpec:
    seq: int
    step_key: str
    stage: str
    title_zh: str
    title_en: str
    handler_id: str
    artifact_kind: str | None
    default_level: str
    decision_timeout_s: int
    skill_name: str | None = None


STAGES: tuple[StageSpec, ...] = (
    StageSpec("P1", "事实取证", "Evidence", "把需求周边的可核查事实收齐，缺口显式列出"),
    StageSpec("P2", "结构化与可行性", "Structure", "需求条目化、可验收，并给出能力可行性结论"),
    StageSpec("P3", "拍板与形态", "Decide", "该人定的在这里定，选项与理由留痕"),
    StageSpec("P4", "方案生成", "Produce", "原型与 PRD，双受众，保真度如实标注"),
    StageSpec("P5", "终审与交付", "Deliver", "三视角评审后交付，外部写入强制人签"),
)

STEPS: tuple[StepSpec, ...] = (
    StepSpec(1, "intake", "P1", "需求进件", "Intake", "wf_intake.intake", "data", "L2", 86400, "pm-intake"),
    StepSpec(
        2, "link_expand", "P1", "关联展开", "Link expand", "wf_intake.link_expand",
        "data", "L2", 86400, "pm-link-expand",
    ),
    StepSpec(
        3, "requirement_analysis", "P2", "需求分析", "Requirement analysis",
        "wf_analyze.requirement_analysis", "document", "L2", 86400, "pm-analysis",
    ),
    StepSpec(4, "code", "P1", "代码取证", "Code probe", "wf_intake.code", "data", "L2", 86400, "pm-code-probe"),
    StepSpec(
        5, "datadict", "P1", "数据字典取证", "Data dictionary", "wf_intake.datadict",
        "data", "L2", 86400, "pm-datadict",
    ),
    StepSpec(6, "dedup", "P1", "去重比对", "Dedup", "wf_intake.dedup", "data", "L2", 86400, None),
    StepSpec(
        7, "capability_fit", "P2", "能力可行性", "Capability fit", "wf_analyze.capability_fit",
        "document", "L2", 86400, "pm-capability",
    ),
    StepSpec(8, "decision", "P3", "关键拍板", "Decision", "wf_decide.decision", "data", "L2", 86400, None),
    StepSpec(
        9, "prototype_gate", "P3", "原型门禁", "Prototype gate", "wf_decide.prototype_gate",
        None, "L1", 180, None,
    ),
    StepSpec(
        10, "prototype", "P4", "原型产出", "Prototype", "wf_produce.prototype",
        "prototype_html", "L1", 180, "pm-prototype",
    ),
    StepSpec(11, "prd", "P4", "PRD 产出", "PRD", "wf_produce.prd", "document", "L1", 180, "pm-prd"),
    StepSpec(12, "review", "P5", "三视角评审", "Review", "wf_review.review", "review", "L2", 86400, None),
    StepSpec(13, "deliver", "P5", "交付", "Deliver", "wf_review.deliver", "bundle", "L3", 86400, None),
)

_BY_SEQ = {step.seq: step for step in STEPS}
_BY_KEY = {step.step_key: step for step in STEPS}
_STAGE_BY_KEY = {stage.key: stage for stage in STAGES}


def step_by_seq(seq: int) -> StepSpec | None:
    return _BY_SEQ.get(seq)


def step_by_key(step_key: str) -> StepSpec | None:
    return _BY_KEY.get(step_key)


def stage_of(step_key: str) -> str | None:
    spec = _BY_KEY.get(step_key)
    return spec.stage if spec else None


def stage_spec(stage_key: str) -> StageSpec | None:
    return _STAGE_BY_KEY.get(stage_key)


def steps_in_stage(stage_key: str) -> tuple[StepSpec, ...]:
    return tuple(step for step in STEPS if step.stage == stage_key)


def next_step(seq: int) -> StepSpec | None:
    return _BY_SEQ.get(seq + 1)


def first_step() -> StepSpec:
    return STEPS[0]


def last_step() -> StepSpec:
    return STEPS[-1]


def stage_order() -> tuple[str, ...]:
    return tuple(stage.key for stage in STAGES)


def resolve_step_key(name: str) -> str | None:
    """Accept ``review``/``deliver`` or a stage-scoped alias; None when unknown."""
    cleaned = name.strip().lower()
    if cleaned in _BY_KEY:
        return cleaned
    for alias, target in (("review_stage", "review"), ("delivery", "deliver"), ("signoff", "review")):
        if cleaned == alias:
            return target
    return None
