import { request } from "../request";
import type {
  PmArtifact,
  PmConclusion,
  PmDecision,
  PmEvidence,
  PmExportPlan,
  PmHealth,
  PmInbox,
  PmIntegration,
  PmJiraIssue,
  PmJiraIssuePreview,
  PmJiraMeta,
  PmJiraStatus,
  PmMetricRow,
  PmPaged,
  PmRequirement,
  PmRequirementChange,
  PmRequirementDetail,
  PmReviewList,
  PmStep,
  PmStepTrace,
  PmSweepResult,
  PmTask,
  PmTaskDetail,
  PmTickResult,
  PmWorkspace,
} from "../types";

export interface PmRequirementQuery {
  workspace_id?: number;
  status?: string;
  q?: string;
  page?: number;
  page_size?: number;
}

export interface PmTaskQuery {
  owner?: "me";
  status?: string;
  workspace_id?: number;
  page?: number;
  page_size?: number;
}

export interface PmDecisionQuery {
  status?: string;
  level?: string;
  mine?: boolean;
  task_id?: number;
  page?: number;
  page_size?: number;
}

function qs(params: Record<string, unknown>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

function json(body: unknown): RequestInit {
  return { method: "POST", body: JSON.stringify(body) };
}

export const pmApi = {
  // --- health -------------------------------------------------------------
  health: () => request<PmHealth>("/pm/health"),
  inbox: () => request<PmInbox>("/pm/inbox"),
  sweep: () => request<PmSweepResult>("/pm/internal/sweep", { method: "POST" }),

  // --- jira ---------------------------------------------------------------
  jiraStatus: (workspaceId: number) =>
    request<PmJiraStatus>(`/pm/jira/status${qs({ workspace_id: workspaceId })}`),
  saveJiraConfig: (
    workspaceId: number,
    body: { base_url: string; token?: string; jql?: string; verify_tls?: boolean },
  ) =>
    request<PmJiraStatus>(
      `/pm/jira/config${qs({ workspace_id: workspaceId })}`,
      json(body),
    ),
  probeJira: (workspaceId: number) =>
    request<{ base_url: string; user: string; account: string; open_assigned: number | null }>(
      `/pm/jira/probe${qs({ workspace_id: workspaceId })}`,
      { method: "POST" },
    ),
  // The design queue, or whatever the operator types. One call for "which
  // ticket?" at every level of precision: blank = my 待设计 P0/P1, a key, or a
  // fragment of a title. `note` is set when the stored queue filter did not
  // hold on this site and the server fell back to "my open issues".
  listJiraIssues: (workspaceId: number, q?: string, limit = 20) =>
    request<{ jql: string; items: PmJiraIssue[]; total: number; note: string }>(
      `/pm/jira/issues${qs({ workspace_id: workspaceId, q: q || undefined, limit })}`,
    ),
  // Status and priority names as this site spells them, plus which of our
  // defaults hit. Used to explain a mismatched queue filter in words.
  jiraMeta: (workspaceId: number) =>
    request<PmJiraMeta>(`/pm/jira/meta${qs({ workspace_id: workspaceId })}`),
  // One issue, read in full and assembled exactly as intake will store it. The
  // dialog calls this as soon as a key is typed, so the operator sees the
  // comments and custom fields instead of a summary line.
  getJiraIssue: (workspaceId: number, issueKey: string) =>
    request<PmJiraIssuePreview>(
      `/pm/jira/issue/${encodeURIComponent(issueKey)}${qs({ workspace_id: workspaceId })}`,
    ),

  // --- workspaces ---------------------------------------------------------
  listWorkspaces: () => request<{ items: PmWorkspace[] }>("/pm/workspaces"),
  createWorkspace: (body: {
    name: string;
    description?: string;
    client_token?: string;
  }) => request<PmWorkspace>("/pm/workspaces", json(body)),

  // --- requirements -------------------------------------------------------
  listRequirements: (query: PmRequirementQuery = {}) =>
    request<PmPaged<PmRequirement>>(`/pm/requirements${qs({ ...query })}`),
  // Intake is by reference only: the server reads the issue from Jira and builds
  // the requirement itself. There is no title/raw_text variant by design.
  createRequirement: (body: {
    workspace_id: number;
    issue_key: string;
    proposer?: string;
    priority?: number;
    client_token?: string;
  }) => request<PmRequirement>("/pm/requirements", json(body)),
  getRequirement: (requirementId: number) =>
    request<PmRequirementDetail>(`/pm/requirements/${requirementId}`),
  addChange: (requirementId: number, change_text: string) =>
    request<PmRequirementChange>(
      `/pm/requirements/${requirementId}/changes`,
      json({ change_text }),
    ),

  // --- tasks --------------------------------------------------------------
  listTasks: (query: PmTaskQuery = {}) =>
    request<PmPaged<PmTask>>(`/pm/tasks${qs({ ...query })}`),
  createTask: (body: { requirement_id: number; client_token?: string }) =>
    request<PmTask>("/pm/tasks", json(body)),
  getTask: (taskId: number) => request<PmTaskDetail>(`/pm/tasks/${taskId}`),
  taskSteps: (taskId: number) =>
    request<{ items: PmStep[] }>(`/pm/tasks/${taskId}/steps`),
  tick: (taskId: number, human_note?: string) =>
    request<PmTickResult>(`/pm/tasks/${taskId}/tick`, json({ human_note })),
  pause: (taskId: number) =>
    request<PmTask>(`/pm/tasks/${taskId}/pause`, { method: "POST" }),
  resume: (taskId: number) =>
    request<PmTask>(`/pm/tasks/${taskId}/resume`, { method: "POST" }),
  reflow: (taskId: number, from_step_key?: string) =>
    request<{ task_id: number; from_seq: number; reset_steps: number }>(
      `/pm/tasks/${taskId}/reflow`,
      json({ from_step_key }),
    ),
  exportBundle: (taskId: number) =>
    request<{ task_id: number; content: string }>(
      `/pm/tasks/${taskId}/export-bundle`,
    ),

  // --- steps --------------------------------------------------------------
  rejectStep: (stepId: number, note: string) =>
    request<Record<string, unknown>>(`/pm/steps/${stepId}/reject`, json({ note })),
  stepTrace: (stepId: number) =>
    request<PmStepTrace>(`/pm/steps/${stepId}/trace`),

  // --- artifacts / evidence ----------------------------------------------
  getArtifact: (artifactId: number, audience: "internal" | "customer" = "internal") =>
    request<PmArtifact>(`/pm/artifacts/${artifactId}${qs({ audience })}`),
  artifactConclusions: (artifactId: number) =>
    request<{ artifact_id: number; items: PmConclusion[] }>(
      `/pm/artifacts/${artifactId}/conclusions`,
    ),
  exportArtifact: (
    artifactId: number,
    opts: { audience: "internal" | "customer"; confirm?: boolean },
  ) =>
    request<PmExportPlan>(
      `/pm/artifacts/${artifactId}/export${qs({
        audience: opts.audience,
        confirm: opts.confirm ? "true" : "false",
      })}`,
    ),
  getEvidence: (evidenceId: number) =>
    request<PmEvidence>(
      `/pm/evidence/${evidenceId}${qs({ audience: "internal" })}`,
    ),

  // --- decisions ----------------------------------------------------------
  listDecisions: (query: PmDecisionQuery = {}) =>
    request<PmPaged<PmDecision>>(`/pm/decisions${qs({ ...query })}`),
  answerDecision: (decisionId: number, selected_key: string, note: string) =>
    request<{ decision: PmDecision; pipeline: Record<string, unknown> }>(
      `/pm/decisions/${decisionId}/answer`,
      json({ selected_key, note }),
    ),
  appealDecision: (decisionId: number) =>
    request<PmDecision>(`/pm/decisions/${decisionId}/appeal`, {
      method: "POST",
    }),
  resolveAppeal: (decisionId: number, state: "upheld" | "reversed") =>
    request<PmDecision>(
      `/pm/decisions/${decisionId}/appeal/resolve`,
      json({ state }),
    ),
  transferDecision: (decisionId: number, assignee_user_id: number) =>
    request<PmDecision>(
      `/pm/decisions/${decisionId}/transfer`,
      json({ assignee_user_id }),
    ),

  // --- reviews ------------------------------------------------------------
  listReviews: (taskId: number) =>
    request<PmReviewList>(`/pm/reviews${qs({ task_id: taskId })}`),
  submitReview: (body: {
    task_id: number;
    perspective: string;
    verdict: "pass" | "reject";
    comment?: string;
    artifact_id?: number;
  }) => request<Record<string, unknown>>("/pm/reviews", json(body)),

  // --- integrations -------------------------------------------------------
  listIntegrations: (workspaceId: number) =>
    request<{ items: PmIntegration[] }>(
      `/pm/integrations${qs({ workspace_id: workspaceId })}`,
    ),
  upsertIntegration: (body: {
    workspace_id: number;
    vendor: string;
    enabled?: boolean;
    mode?: "real" | "drill";
    credential_ref?: string;
    mapping?: Record<string, unknown>;
  }) => request<PmIntegration>("/pm/integrations", json(body)),
  probeIntegration: (integrationId: number) =>
    request<{ integration_id: number; probe_status: string }>(
      `/pm/integrations/${integrationId}/probe`,
      { method: "POST" },
    ),

  // --- metrics / eval -----------------------------------------------------
  metricsDaily: (workspaceId: number, dateFrom: string, dateTo: string) =>
    request<{ workspace_id: number; items: PmMetricRow[] }>(
      `/pm/metrics/daily${qs({
        workspace_id: workspaceId,
        date_from: dateFrom,
        date_to: dateTo,
      })}`,
    ),
  runEval: () =>
    request<Record<string, unknown>>("/pm/eval/run", { method: "POST" }),
  recheckConclusion: (conclusionId: number) =>
    request<Record<string, unknown>>(`/pm/conclusions/${conclusionId}/recheck`, {
      method: "POST",
    }),
};

export default pmApi;
