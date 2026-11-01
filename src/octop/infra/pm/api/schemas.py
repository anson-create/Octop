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
    """Internal shape. The HTTP contract no longer accepts this — see below.

    Kept because the service layer and tests still build requirements
    programmatically, and because a future non-Jira intake port will need the same
    fields. Handing these two strings in over HTTP is exactly the behaviour we
    removed: nothing verifies that the text corresponds to a real ticket.
    """

    workspace_id: int
    title: str = Field(min_length=1, max_length=300)
    raw_text: str = Field(min_length=1)
    source: str = "manual"
    source_ref: str | None = None
    proposer: str | None = None
    priority: int | None = None
    baseline: dict[str, Any] | None = None
    client_token: str | None = None


class RequirementFromJira(BaseModel):
    """Intake by reference: the browser names an issue, the server reads it."""

    workspace_id: int
    issue_key: str = Field(min_length=1, max_length=64)
    proposer: str | None = None
    priority: int | None = None
    client_token: str | None = None


class JiraConfig(BaseModel):
    base_url: str = Field(min_length=1, max_length=300)
    # Write-only. Never echoed back — jira_status reports token_set instead.
    token: str | None = None
    # Required only for instances that authenticate with an account password
    # (Basic) instead of a personal access token (Bearer). Send it and the
    # client switches to Basic; leave it out and the token is sent as a bearer.
    username: str | None = None
    jql: str | None = None
    verify_tls: bool = True


class JiraConnectorBinding(BaseModel):
    """Point this workspace's Jira at an MCP connector that already has it.

    No credential here on purpose: the site URL and token stay inside the
    connector, so this payload cannot leak them and the row it writes is safe
    to dump.
    """

    connector_instance_id: str = Field(min_length=1, max_length=64)
    # Which server inside a multi-server connector to use. Empty means "the one
    # whose name mentions jira".
    server_key: str = ""
    jql: str | None = None


class JiraProjectMapping(BaseModel):
    """Which source repositories a Jira project's requirements belong to.

    ``target`` accepts a system name (``cargowarex``), a repo key (``cwx-web``)
    or a comma-separated list (``cwx-web,cwx-api``). An empty string clears the
    override, falling back to the built-in seed table.
    """

    project: str = Field(min_length=1, max_length=64)
    target: str = Field(default="", max_length=200)


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
