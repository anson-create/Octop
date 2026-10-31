"""Request bodies for the PM API. Responses are plain dicts assembled by service."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Audience = Literal["internal", "customer"]
Level = Literal["L1", "L2", "L3"]
Perspective = Literal["business", "tech", "customer"]
Verdict = Literal["pass", "reject"]


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None
    client_token: str | None = None


class RequirementCreate(BaseModel):
    workspace_id: int
    title: str = Field(min_length=1, max_length=300)
    raw_text: str = Field(min_length=1)
    source: str = "manual"
    source_ref: str | None = None
    proposer: str | None = None
    priority: int | None = None
    baseline: dict[str, Any] | None = None
    client_token: str | None = None


class ChangeCreate(BaseModel):
    change_text: str = Field(min_length=1)
    raised_by: int | None = None


class TaskCreate(BaseModel):
    requirement_id: int
    client_token: str | None = None


class TickRequest(BaseModel):
    human_note: str | None = None


class RejectRequest(BaseModel):
    # Length is a domain rule (>=10 chars) enforced in the service so the client
    # gets PM_VALIDATION with a readable message instead of a pydantic 422.
    note: str = Field(max_length=4000)


class AnswerRequest(BaseModel):
    selected_key: str = Field(min_length=1)
    note: str = Field(max_length=4000)


class TransferRequest(BaseModel):
    assignee_user_id: int


class AppealResolveRequest(BaseModel):
    state: Literal["upheld", "reversed"]


class ReviewCreate(BaseModel):
    task_id: int
    perspective: Perspective
    verdict: Verdict
    comment: str | None = None
    artifact_id: int | None = None


class IntegrationUpsert(BaseModel):
    workspace_id: int
    vendor: str = Field(min_length=1)
    enabled: bool = True
    mode: Literal["real", "drill"] = "real"
    credential_ref: str | None = None
    mapping: dict[str, Any] | None = None


class ReflowRequest(BaseModel):
    from_step_key: str | None = None


class ExportRequest(BaseModel):
    audience: Audience = "internal"
    confirm: bool = False
    internal_names: list[str] = Field(default_factory=list)


class ConclusionRecheck(BaseModel):
    note: str | None = None
