"""Row models for the PM tables.

Plain frozen dataclasses, no ORM: repos own the SQL, models only carry shape.
``from_row`` tolerates missing keys so a read of a partial projection (e.g. a
list endpoint that skips heavy TEXT columns) still constructs.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Self


class RowModel:
    """Build a dataclass from a DB row mapping (sqlite3.Row or compat row)."""

    @classmethod
    def from_row(cls, row: Any) -> Self:
        values: dict[str, Any] = {}
        for field in fields(cls):  # type: ignore[arg-type]
            try:
                values[field.name] = row[field.name]
            except (KeyError, IndexError):
                continue
        return cls(**values)


@dataclass(frozen=True)
class Workspace(RowModel):
    id: int
    name: str
    description: str | None
    owner_user_id: int
    client_token: str | None
    created_at: str
    archived_at: str | None


@dataclass(frozen=True)
class Requirement(RowModel):
    id: int
    public_id: str
    workspace_id: int
    title: str
    raw_text: str
    source: str
    source_ref: str | None
    proposer: str | None
    priority: int | None
    status: str
    baseline_json: str | None
    client_token: str | None
    created_at: str


@dataclass(frozen=True)
class RequirementChange(RowModel):
    id: int
    requirement_id: int
    change_text: str
    raised_by: int | None
    decision_id: int | None
    accepted: int
    created_at: str


@dataclass(frozen=True)
class Task(RowModel):
    id: int
    public_id: str
    requirement_id: int
    workspace_id: int
    stage: str
    status: str
    owner_user_id: int
    pipeline_thread_id: str | None
    pipeline_agent_id: int | None
    generation: int
    locked_until: str | None
    client_token: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class StepHandler(RowModel):
    workflow_key: str
    seq: int
    step_key: str
    handler_id: str
    threshold: int
    decision_timeout_s: int
    decision_level: str
    skill_name: str | None
    enabled: int


@dataclass(frozen=True)
class TaskStep(RowModel):
    id: int
    task_id: int
    seq: int
    step_key: str
    stage: str
    status: str
    attempt: int
    gate_result_json: str | None
    trace_json: str | None
    wall_clock_ms: int | None
    error_code: str | None
    started_at: str | None
    finished_at: str | None


@dataclass(frozen=True)
class Artifact(RowModel):
    id: int
    task_id: int
    step_id: int | None
    kind: str
    title: str | None
    audience: str
    fidelity: str | None
    fidelity_note: str | None
    version: int
    body_path: str | None
    body_inline: str | None
    origin: str
    created_at: str


@dataclass(frozen=True)
class Conclusion(RowModel):
    id: int
    artifact_id: int
    seq: int
    text: str
    customer_text: str | None
    status: str
    created_at: str


@dataclass(frozen=True)
class Evidence(RowModel):
    id: int
    task_id: int
    source_type: str
    port: str
    locator_json: str
    excerpt: str
    content_hash: str
    retrieved_at: str
    stale_at: str | None
    visibility: str
    confidence: float | None
    created_at: str


@dataclass(frozen=True)
class Decision(RowModel):
    id: int
    task_id: int
    step_id: int | None
    level: str
    question: str
    options_json: str
    recommended_key: str | None
    option_snapshot_json: str | None
    assignee_user_id: int | None
    status: str
    selected_key: str | None
    note: str | None
    decided_by: int | None
    decided_at: str | None
    timeout_at: str
    auto_decided: int
    appeal_state: str | None
    thread_id: str | None
    resume_agent_id: int | None
    action_requests_json: str | None
    client_token: str | None
    created_at: str


@dataclass(frozen=True)
class Review(RowModel):
    id: int
    task_id: int
    artifact_id: int | None
    perspective: str
    verdict: str
    comment: str | None
    reviewer_user_id: int | None
    created_at: str


@dataclass(frozen=True)
class Integration(RowModel):
    id: int
    workspace_id: int
    vendor: str
    enabled: int
    mode: str
    credential_ref: str | None
    mapping_json: str | None
    last_probe_at: str | None
    probe_status: str | None


@dataclass(frozen=True)
class MetricDaily(RowModel):
    day: str
    workspace_id: int
    metric_key: str
    value: float
    numerator: int | None
    denominator: int | None


@dataclass(frozen=True)
class EvalCase(RowModel):
    id: int
    name: str
    kind: str
    input_json: str
    expect_json: str
    active: int


ARTIFACT_KINDS: tuple[str, ...] = ("data", "document", "prototype_html", "review", "bundle")
AUDIENCES: tuple[str, ...] = ("internal", "customer")
VISIBILITIES: tuple[str, ...] = ("internal", "customer")
CONCLUSION_STATUSES: tuple[str, ...] = ("evidenced", "uncovered", "undetermined")
