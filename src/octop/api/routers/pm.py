"""``/api/pm`` — the PM workspace router.

Lives here (not inside ``infra/pm``) to follow the host convention for FastAPI
routers. Mounted by ``octop.api.app`` with the ``pm`` openapi tag. Reads need ``pm``;
anything that decides, rejects, reviews, reflows, rechecks or runs the eval set
needs ``pm_decide``. Every handler is thin: it resolves the service, calls one
service method, and lets ``OctopError`` carry the status code.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from octop.api.deps import get_server, require_permission
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.pm.api.schemas import (
    AnswerRequest,
    AppealResolveRequest,
    ChangeCreate,
    IntegrationUpsert,
    JiraConfig,
    JiraProjectMapping,
    ReflowRequest,
    RejectRequest,
    RequirementFromJira,
    ReviewCreate,
    TaskCreate,
    TickRequest,
    TransferRequest,
    WorkspaceCreate,
)
from octop.infra.pm.service import PMService

router = APIRouter()


def get_pm(server: Any = Depends(get_server)) -> PMService:
    service = getattr(server, "pm_service", None)
    if service is None:
        # Set only when PM migrations failed at boot (fail-soft by design);
        # 503 keeps it distinguishable from a business-level state conflict.
        raise OctopError(
            ErrorCode.INTERNAL_ERROR,
            "PM workspace is unavailable on this instance (see /api/pm/health)",
            status=503,
        )
    return service


# --- health ----------------------------------------------------------------


@router.get("/health", summary="PM subsystem health")
async def pm_health(pm: PMService = Depends(get_pm)) -> dict[str, Any]:
    return pm.health()


# --- jira ------------------------------------------------------------------


@router.get("/jira/status", summary="Is Jira wired up for this workspace")
async def jira_status(
    workspace_id: int,
    pm: PMService = Depends(get_pm),
    _: Any = Depends(require_permission("pm")),
) -> dict[str, Any]:
    return pm.jira_status(workspace_id)


@router.post("/jira/config", summary="Save Jira endpoint + token")
async def jira_config(
    body: JiraConfig,
    workspace_id: int,
    pm: PMService = Depends(get_pm),
    _: Any = Depends(require_permission("pm_decide")),
) -> dict[str, Any]:
    """The token lands in the host secret store; only a reference hits the DB.

    Write-only by design: the response reports ``token_set`` and never the value.
    """
    return pm.save_jira_config(
        workspace_id,
        base_url=body.base_url,
        token=body.token,
        username=body.username,
        jql=body.jql,
        verify_tls=body.verify_tls,
    )


@router.post("/jira/probe", summary="Check the stored credentials against Jira")
async def jira_probe(
    workspace_id: int,
    pm: PMService = Depends(get_pm),
    _: Any = Depends(require_permission("pm_decide")),
) -> dict[str, Any]:
    return pm.probe_jira(workspace_id)


@router.get("/jira/issues", summary="Candidates for intake")
async def jira_issues(
    workspace_id: int,
    q: str | None = None,
    limit: int = Query(20, ge=1, le=50),
    pm: PMService = Depends(get_pm),
    _: Any = Depends(require_permission("pm")),
) -> dict[str, Any]:
    return pm.list_jira_issues(workspace_id, q=q, limit=limit)


@router.get("/jira/issue/{issue_key}", summary="Read one issue in full, before importing")
async def jira_issue(
    issue_key: str,
    workspace_id: int,
    pm: PMService = Depends(get_pm),
    _: Any = Depends(require_permission("pm")),
) -> dict[str, Any]:
    """Everything the ticket says, assembled the way intake will store it.

    The dialog accepts a typed key and calls this before creating anything, so
    the operator sees the comments and custom fields that used to be dropped
    silently. ``existing`` is non-null when the ticket is already a requirement.
    """
    return pm.preview_jira_issue(workspace_id, issue_key=issue_key)


@router.get("/jira/projects", summary="Jira projects and which repo each routes to")
async def jira_projects(
    workspace_id: int,
    pm: PMService = Depends(get_pm),
    _: Any = Depends(require_permission("pm")),
) -> dict[str, Any]:
    """The project-code → source-repository map, against Jira's real key list.

    Kept separate from ``/jira/issues`` because it answers a different question:
    not "what should we build" but "whose source do we read".
    """
    return pm.jira_projects(workspace_id)


@router.post("/jira/projects/mapping", summary="Map a Jira project to repos")
async def jira_project_mapping(
    body: JiraProjectMapping,
    workspace_id: int,
    pm: PMService = Depends(get_pm),
    _: Any = Depends(require_permission("pm_decide")),
) -> dict[str, Any]:
    return pm.save_project_mapping(workspace_id, project=body.project, target=body.target)


# --- workspaces ------------------------------------------------------------


@router.get("/workspaces", summary="List workspaces")
async def list_workspaces(
    pm: PMService = Depends(get_pm),
    _: Any = Depends(require_permission("pm")),
) -> dict[str, Any]:
    return {"items": pm.list_workspaces()}


@router.post("/workspaces", summary="Create a workspace")
async def create_workspace(
    body: WorkspaceCreate,
    pm: PMService = Depends(get_pm),
    user: Any = Depends(require_permission("pm")),
) -> dict[str, Any]:
    return pm.create_workspace(
        name=body.name,
        owner_user_id=int(user.id),
        description=body.description,
        client_token=body.client_token,
    )


# --- requirements ----------------------------------------------------------


@router.get("/requirements", summary="List requirements")
async def list_requirements(
    workspace_id: int | None = None,
    status: str | None = None,
    q: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    pm: PMService = Depends(get_pm),
    _: Any = Depends(require_permission("pm")),
) -> dict[str, Any]:
    return pm.list_requirements(
        workspace_id=workspace_id, status=status, query=q, page=page, page_size=page_size
    )


@router.post("/requirements", summary="Intake a requirement from a Jira issue")
async def create_requirement(
    body: RequirementFromJira,
    pm: PMService = Depends(get_pm),
    user: Any = Depends(require_permission("pm")),
) -> dict[str, Any]:
    """Intake is by reference only.

    There is deliberately no "title + text" variant: a requirement that no ticket
    backs is indistinguishable from a model invention, and every downstream step
    (evidence → analysis → PRD) would inherit that. If a future intake port is
    needed (KB, form), it gets its own endpoint with its own verification.
    """
    return pm.create_requirement_from_jira(
        workspace_id=body.workspace_id,
        issue_key=body.issue_key,
        proposer=body.proposer,
        priority=body.priority,
        client_token=body.client_token,
        actor=getattr(user, "username", None),
    )


@router.get("/requirements/{requirement_id}", summary="Requirement detail")
async def requirement_detail(
    requirement_id: int,
    pm: PMService = Depends(get_pm),
    _: Any = Depends(require_permission("pm")),
) -> dict[str, Any]:
    return pm.requirement_detail(requirement_id)


@router.get("/requirements/{requirement_id}/source-repo", summary="Which repo this requirement reads from")
async def requirement_source_repo(
    requirement_id: int,
    pm: PMService = Depends(get_pm),
    _: Any = Depends(require_permission("pm")),
) -> dict[str, Any]:
    """Project code → repository, as resolved for this one requirement.

    Exposed so the mapping is inspectable before a prototype exists: when it is
    wrong, the failure is a wrong repo name here, not a plausible-looking
    prototype derived from another product's source.
    """
    return pm.requirement_source_repo(requirement_id)


@router.post("/requirements/{requirement_id}/changes", summary="Raise a change request")
async def add_change(
    requirement_id: int,
    body: ChangeCreate,
    pm: PMService = Depends(get_pm),
    user: Any = Depends(require_permission("pm")),
) -> dict[str, Any]:
    return pm.add_change(
        requirement_id=requirement_id,
        change_text=body.change_text,
        raised_by=body.raised_by or int(getattr(user, "id", 0) or 0) or None,
        actor=getattr(user, "username", None),
    )


# --- tasks -----------------------------------------------------------------


@router.post("/tasks", summary="Create the task for a requirement (1:1)")
async def create_task(
    body: TaskCreate,
    pm: PMService = Depends(get_pm),
    user: Any = Depends(require_permission("pm")),
) -> dict[str, Any]:
    return pm.create_task(
        requirement_id=body.requirement_id,
        owner_user_id=int(user.id),
        client_token=body.client_token,
    )


@router.get("/tasks", summary="List tasks")
async def list_tasks(
    owner: str | None = None,
    status: str | None = None,
    workspace_id: int | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    pm: PMService = Depends(get_pm),
    user: Any = Depends(require_permission("pm")),
) -> dict[str, Any]:
    owner_id = int(user.id) if owner == "me" else None
    return pm.list_tasks(
        owner_user_id=owner_id,
        status=status,
        workspace_id=workspace_id,
        page=page,
        page_size=page_size,
    )


@router.get("/tasks/{task_id}", summary="Task detail (stages, steps, artifacts, cards)")
async def task_detail(
    task_id: int,
    pm: PMService = Depends(get_pm),
    _: Any = Depends(require_permission("pm")),
) -> dict[str, Any]:
    return pm.task_detail(task_id)


@router.post("/tasks/{task_id}/tick", summary="Advance one step")
async def tick(
    task_id: int,
    body: TickRequest | None = None,
    pm: PMService = Depends(get_pm),
    _: Any = Depends(require_permission("pm")),
) -> dict[str, Any]:
    return pm.tick(task_id, human_note=body.human_note if body else None)


@router.post("/tasks/{task_id}/pause", summary="Pause a task")
async def pause(
    task_id: int,
    pm: PMService = Depends(get_pm),
    _: Any = Depends(require_permission("pm")),
) -> dict[str, Any]:
    return pm.pause(task_id)


@router.post("/tasks/{task_id}/resume", summary="Resume a task")
async def resume(
    task_id: int,
    pm: PMService = Depends(get_pm),
    _: Any = Depends(require_permission("pm")),
) -> dict[str, Any]:
    return pm.resume(task_id)


@router.get("/tasks/{task_id}/steps", summary="Steps with gate results")
async def task_steps(
    task_id: int,
    pm: PMService = Depends(get_pm),
    _: Any = Depends(require_permission("pm")),
) -> dict[str, Any]:
    return {"items": pm.task_steps(task_id)}


@router.post("/tasks/{task_id}/reflow", summary="Re-flow after a requirement change")
async def reflow(
    task_id: int,
    body: ReflowRequest | None = None,
    pm: PMService = Depends(get_pm),
    user: Any = Depends(require_permission("pm_decide")),
) -> dict[str, Any]:
    result = pm.reflow(
        task_id,
        from_step_key=body.from_step_key if body else None,
        actor=getattr(user, "username", None),
    )
    return result


@router.get("/tasks/{task_id}/export-bundle", summary="Whole-delivery bundle")
async def export_bundle(
    task_id: int,
    pm: PMService = Depends(get_pm),
    user: Any = Depends(require_permission("pm")),
) -> dict[str, Any]:
    return pm.export_bundle(task_id, actor=getattr(user, "username", None))


# --- steps -----------------------------------------------------------------


@router.post("/steps/{step_id}/reject", summary="Reject a step and redo it")
async def reject_step(
    step_id: int,
    body: RejectRequest,
    pm: PMService = Depends(get_pm),
    user: Any = Depends(require_permission("pm_decide")),
) -> dict[str, Any]:
    return pm.reject_step(
        step_id,
        note=body.note,
        user_id=int(getattr(user, "id", 0) or 0) or None,
        actor=getattr(user, "username", None),
    )


@router.get("/steps/{step_id}/trace", summary="Retrieval sources and tool calls")
async def step_trace(
    step_id: int,
    pm: PMService = Depends(get_pm),
    _: Any = Depends(require_permission("pm")),
) -> dict[str, Any]:
    return pm.step_trace(step_id)


# --- artifacts / evidence --------------------------------------------------


@router.get("/artifacts/{artifact_id}", summary="Artifact body")
async def get_artifact(
    artifact_id: int,
    audience: str = "internal",
    pm: PMService = Depends(get_pm),
    _: Any = Depends(require_permission("pm")),
) -> dict[str, Any]:
    return pm.get_artifact(artifact_id, audience=audience)


@router.get("/artifacts/{artifact_id}/conclusions", summary="Conclusions with evidence")
async def artifact_conclusions(
    artifact_id: int,
    pm: PMService = Depends(get_pm),
    _: Any = Depends(require_permission("pm")),
) -> dict[str, Any]:
    return pm.artifact_conclusions(artifact_id)


@router.get("/artifacts/{artifact_id}/export", summary="Export through the gate")
async def export_artifact(
    artifact_id: int,
    audience: str = "internal",
    confirm: bool = False,
    pm: PMService = Depends(get_pm),
    user: Any = Depends(require_permission("pm")),
) -> dict[str, Any]:
    return pm.export_artifact(
        artifact_id,
        audience=audience,
        confirm=confirm,
        actor=getattr(user, "username", None),
    )


@router.get("/evidence/{evidence_id}", summary="Evidence detail with locator")
async def get_evidence(
    evidence_id: int,
    audience: str = "internal",
    pm: PMService = Depends(get_pm),
    _: Any = Depends(require_permission("pm")),
) -> dict[str, Any]:
    return pm.get_evidence(evidence_id, audience=audience)


# --- decisions -------------------------------------------------------------


@router.get("/decisions", summary="Decision centre")
async def list_decisions(
    status: str | None = "pending",
    level: str | None = None,
    mine: bool = False,
    task_id: int | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    pm: PMService = Depends(get_pm),
    user: Any = Depends(require_permission("pm")),
) -> dict[str, Any]:
    return pm.list_decisions(
        status=status,
        level=level,
        mine=mine,
        user_id=int(getattr(user, "id", 0) or 0) or None,
        task_id=task_id,
        page=page,
        page_size=page_size,
    )


@router.post("/decisions/{decision_id}/answer", summary="Answer a card")
async def answer_decision(
    decision_id: int,
    body: AnswerRequest,
    pm: PMService = Depends(get_pm),
    user: Any = Depends(require_permission("pm_decide")),
) -> dict[str, Any]:
    return pm.answer_decision(
        decision_id,
        selected_key=body.selected_key,
        note=body.note,
        user_id=int(getattr(user, "id", 0) or 0) or None,
        actor=getattr(user, "username", None),
    )


@router.post("/decisions/{decision_id}/appeal", summary="Appeal an auto-decision")
async def appeal_decision(
    decision_id: int,
    pm: PMService = Depends(get_pm),
    user: Any = Depends(require_permission("pm_decide")),
) -> dict[str, Any]:
    return pm.appeal_decision(
        decision_id,
        user_id=int(getattr(user, "id", 0) or 0) or None,
        actor=getattr(user, "username", None),
    )


@router.post("/decisions/{decision_id}/appeal/resolve", summary="Resolve an appeal")
async def resolve_appeal(
    decision_id: int,
    body: AppealResolveRequest,
    pm: PMService = Depends(get_pm),
    _: Any = Depends(require_permission("pm_decide")),
) -> dict[str, Any]:
    return pm.resolve_appeal(decision_id, state=body.state)


@router.post("/decisions/{decision_id}/transfer", summary="Transfer a card")
async def transfer_decision(
    decision_id: int,
    body: TransferRequest,
    pm: PMService = Depends(get_pm),
    user: Any = Depends(require_permission("pm_decide")),
) -> dict[str, Any]:
    return pm.transfer_decision(
        decision_id,
        assignee_user_id=body.assignee_user_id,
        actor=getattr(user, "username", None),
    )


# --- reviews ---------------------------------------------------------------


@router.post("/reviews", summary="Submit one perspective review")
async def submit_review(
    body: ReviewCreate,
    pm: PMService = Depends(get_pm),
    user: Any = Depends(require_permission("pm_decide")),
) -> dict[str, Any]:
    return pm.submit_review(
        task_id=body.task_id,
        perspective=body.perspective,
        verdict=body.verdict,
        comment=body.comment,
        reviewer_user_id=int(getattr(user, "id", 0) or 0) or None,
        artifact_id=body.artifact_id,
        actor=getattr(user, "username", None),
    )


@router.get("/reviews", summary="Reviews for a task")
async def list_reviews(
    task_id: int,
    pm: PMService = Depends(get_pm),
    _: Any = Depends(require_permission("pm")),
) -> dict[str, Any]:
    return pm.list_reviews(task_id)


# --- integrations ----------------------------------------------------------


@router.get("/integrations", summary="Integration config (no credentials)")
async def list_integrations(
    workspace_id: int,
    pm: PMService = Depends(get_pm),
    _: Any = Depends(require_permission("pm")),
) -> dict[str, Any]:
    return {"items": pm.list_integrations(workspace_id)}


@router.post("/integrations", summary="Configure an integration")
async def upsert_integration(
    body: IntegrationUpsert,
    pm: PMService = Depends(get_pm),
    _: Any = Depends(require_permission("pm_decide")),
) -> dict[str, Any]:
    return pm.upsert_integration(
        workspace_id=body.workspace_id,
        vendor=body.vendor,
        enabled=body.enabled,
        mode=body.mode,
        credential_ref=body.credential_ref,
        mapping=body.mapping,
    )


@router.post("/integrations/{integration_id}/probe", summary="Probe an integration")
async def probe_integration(
    integration_id: int,
    pm: PMService = Depends(get_pm),
    _: Any = Depends(require_permission("pm")),
) -> dict[str, Any]:
    return pm.probe_integration(integration_id)


# --- metrics / eval --------------------------------------------------------


@router.get("/metrics/daily", summary="Daily metric rollups")
async def metrics_daily(
    workspace_id: int,
    date_from: str,
    date_to: str,
    pm: PMService = Depends(get_pm),
    _: Any = Depends(require_permission("pm")),
) -> dict[str, Any]:
    return pm.metrics_daily(workspace_id=workspace_id, date_from=date_from, date_to=date_to)


@router.post("/eval/run", summary="Run the golden + adversarial set")
async def run_eval(
    pm: PMService = Depends(get_pm),
    _: Any = Depends(require_permission("pm_decide")),
) -> dict[str, Any]:
    return pm.run_eval()


@router.post("/conclusions/{conclusion_id}/recheck", summary="Re-check after evidence went stale")
async def recheck_conclusion(
    conclusion_id: int,
    pm: PMService = Depends(get_pm),
    user: Any = Depends(require_permission("pm_decide")),
) -> dict[str, Any]:
    return pm.recheck_conclusion(conclusion_id, actor=getattr(user, "username", None))


# --- internal --------------------------------------------------------------


@router.get("/inbox", summary="Cards waiting on me (sidebar badge)")
async def inbox(
    pm: PMService = Depends(get_pm),
    user: Any = Depends(require_permission("pm")),
) -> dict[str, Any]:
    return pm.inbox(user_id=int(getattr(user, "id", 0) or 0) or None)


@router.post("/internal/sweep", summary="Sweep due cards (scheduler)")
async def sweep(
    pm: PMService = Depends(get_pm),
    _: Any = Depends(require_permission("pm")),
) -> dict[str, Any]:
    return pm.sweep()
