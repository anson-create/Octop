/** Types mirroring ``/api/pm`` payloads (see ``octop.infra.pm.service``). */

export type PmLevel = "L1" | "L2" | "L3";

export type PmTaskStatus =
  | "queued"
  | "running"
  | "waiting_human"
  | "blocked"
  | "paused"
  | "delivered"
  | "archived";

export type PmStepStatus =
  | "pending"
  | "running"
  | "passed"
  | "failed"
  | "waiting_card"
  | "skipped";

export type PmDecisionStatus =
  | "pending"
  | "answered"
  | "auto_answered"
  | "expired"
  | "void";

export type PmConclusionStatus = "evidenced" | "uncovered" | "undetermined";

export type PmRequirementStatus =
  | "open"
  | "in_progress"
  | "delivered"
  | "closed";

export type PmAudience = "internal" | "customer";

export type PmPerspective = "business" | "tech" | "customer";

export interface PmWorkspace {
  id: number;
  name: string;
  description: string | null;
  owner_user_id: number;
  created_at: string | null;
  archived_at: string | null;
}

export interface PmRequirement {
  id: number;
  public_id: string;
  workspace_id: number;
  title: string;
  raw_text: string;
  source: string;
  source_ref: string | null;
  proposer: string | null;
  priority: number | null;
  status: PmRequirementStatus | string;
  baseline: Record<string, unknown> | null;
  created_at: string | null;
}

export interface PmRequirementChange {
  id: number;
  change_text: string;
  accepted: boolean;
  raised_by: number | null;
  created_at: string | null;
}

export interface PmTask {
  id: number;
  public_id: string;
  requirement_id: number;
  workspace_id: number;
  stage: string;
  status: PmTaskStatus | string;
  owner_user_id: number;
  generation: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface PmStep {
  id: number;
  seq: number;
  step_key: string;
  stage: string;
  status: PmStepStatus | string;
  attempt: number;
  gate: PmGateResult | null;
  error_code: string | null;
}

export interface PmGateIssue {
  rule_id?: string;
  message?: string;
  severity?: string;
}

export interface PmGateResult {
  ok?: boolean;
  passed?: boolean;
  rule_id?: string;
  rules?: string[];
  issues?: PmGateIssue[];
  failures?: PmGateIssue[];
  checked_at?: string;
  [key: string]: unknown;
}

export interface PmStageProgress {
  key: string;
  title: string;
  purpose: string;
  state: string;
  steps: number;
  passed: number;
}

export interface PmArtifact {
  id: number;
  task_id: number;
  kind: string;
  title: string | null;
  audience: PmAudience | string;
  fidelity: string | null;
  fidelity_note: string | null;
  version: number;
  origin: string | null;
  created_at: string | null;
  body_inline?: string | null;
  body_path?: string | null;
}

export interface PmEvidence {
  id: number;
  task_id: number;
  source_type: string;
  port: string | null;
  excerpt: string | null;
  retrieved_at: string | null;
  stale_at: string | null;
  visibility: PmAudience | string;
  confidence: number | null;
  locator?: Record<string, unknown> | null;
  content_hash?: string | null;
}

export interface PmConclusion {
  id: number;
  artifact_id: number;
  seq: number;
  text: string;
  customer_text: string | null;
  status: PmConclusionStatus | string;
  evidence?: PmEvidence[];
}

export interface PmDecisionOption {
  key: string;
  label?: string;
  pros?: string;
  cons?: string;
  effort?: string;
  confidence?: number;
  [key: string]: unknown;
}

export interface PmDecision {
  id: number;
  task_id: number;
  step_id: number | null;
  level: PmLevel | string;
  question: string;
  options: PmDecisionOption[];
  recommended_key: string | null;
  status: PmDecisionStatus | string;
  selected_key: string | null;
  note: string | null;
  decided_by: number | null;
  decided_at: string | null;
  timeout_at: string | null;
  auto_decided: boolean;
  appeal_state: string | null;
  assignee_user_id: number | null;
  created_at: string | null;
}

export interface PmTaskDetail extends PmTask {
  requirement: PmRequirement | null;
  steps: PmStep[];
  stages: PmStageProgress[];
  artifacts: PmArtifact[];
  pending_decisions: PmDecision[];
  evidence_count: number;
  evidence_sources: Record<string, number>;
  conclusions: {
    total: number;
    evidenced: number;
    uncovered: number;
    undetermined: number;
  };
}

export interface PmRequirementDetail extends PmRequirement {
  changes: PmRequirementChange[];
  task: PmTask | null;
}

export interface PmPaged<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

export interface PmStepTrace {
  step_id: number;
  task_id: number;
  step_key: string;
  status: string;
  trace: {
    evidence_ids?: number[];
    sources?: unknown[];
    tool_calls?: unknown[];
    gaps?: string[];
    [key: string]: unknown;
  };
  gate: PmGateResult | null;
  evidence: PmEvidence[];
  sources: Record<string, number>;
  workspace_id: number | null;
}

export interface PmExportHit {
  rule: string;
  label: string;
  match: string;
  start: number;
}

export interface PmExportConclusion {
  seq: number;
  text: string;
  customer_text: string | null;
  status: string;
  evidence_ids: number[];
  downgraded: boolean;
}

export interface PmExportPlan {
  audience: PmAudience | string;
  title: string;
  ok: boolean;
  hits: PmExportHit[];
  downgraded: number;
  dropped_evidence: number;
  conclusions: PmExportConclusion[];
  notes: string[];
  content: string | null;
  preview_only: boolean;
}

export interface PmReview {
  id: number;
  perspective: PmPerspective | string;
  verdict: "pass" | "reject" | string;
  comment: string | null;
  reviewer_user_id: number | null;
  created_at: string | null;
}

export interface PmReviewList {
  task_id: number;
  items: PmReview[];
  complete: boolean;
}

export interface PmIntegration {
  id: number;
  workspace_id: number;
  vendor: string;
  enabled: boolean;
  mode: string;
  credential_set: boolean;
  last_probe_at: string | null;
  probe_status: string | null;
}

export interface PmHealth {
  enabled: boolean;
  migration: {
    ok?: boolean;
    version?: number;
    pending?: string[];
    error?: string;
  };
  status: string;
  tasks: Record<string, number>;
  decisions_pending: number;
  decisions_due: number;
}

/** One Jira issue, normalised by the server for the intake picker. */
export interface PmJiraIssue {
  key: string;
  title: string;
  description: string;
  status: string;
  status_category: string;
  assignee: string;
  reporter: string;
  priority: string;
  issue_type: string;
  project: string;
  resolution: string;
  labels: string[];
  created: string;
  updated: string;
  /** Present only on the single-issue read, which asks Jira for the full set. */
  components?: string[];
  environment?: string;
  custom_fields?: { id: string; name: string; text: string }[];
  comments?: { author: string; text: string; created: string }[];
  attachments?: { filename: string; mime: string; size: string }[];
}

/**
 * What the intake dialog shows after a key is typed.
 *
 * `requirement_text` is assembled by the same server-side function that fills
 * the requirement row, so the preview cannot promise something the write path
 * will not store.
 */
export interface PmJiraIssuePreview {
  key: string;
  issue: PmJiraIssue;
  requirement_text: string;
  sections: string[];
  truncated: string[];
  /** Content a deliverable may not carry verbatim (internal ticket ids, paths). */
  blockers: string[];
  existing: { id: number; public_id: string; title: string; status: string } | null;
}

export interface PmJiraStatus {
  configured: boolean;
  /**
   * How this workspace reaches Jira.
   *
   * `mcp` means the site URL and token live in a connector the operator already
   * installed, so nothing is stored here and `token_set` only says "the
   * connector is alive". `rest` keeps the older site+token row.
   */
  transport: "mcp" | "rest";
  base_url: string;
  jql: string;
  verify_tls: boolean;
  token_set: boolean;
  auth?: string;
  /** Present on the `mcp` transport: which connector, never its credential. */
  connector_instance_id?: string;
  connector_server?: string;
  connector_display?: string;
  enabled: boolean;
  last_probe_at: string | null;
  probe_status: string | null;
}

/**
 * A connector that already carries a Jira server, i.e. one worth binding.
 *
 * The list is filtered server-side: picking a mail connector would fail on the
 * first query with a message that points at the tool rather than the choice.
 */
export interface PmJiraConnector {
  instance_id: string;
  display_name: string;
  status: string;
  server_key: string;
  server_display: string;
  base_url: string;
  username: string;
}

/**
 * What the intake queue filter may legally say on this Jira site.
 *
 * The queue defaults to "待设计" plus "P0"/"P1" — this product's guess about a
 * vocabulary it does not own. `status_hits` and `priority_hits` report which of
 * those guesses exist here, so a mismatch becomes a sentence the operator can
 * act on instead of a 400 they cannot.
 */
export interface PmJiraMeta {
  user: string;
  statuses: string[];
  priorities: string[];
  jql: string;
  status_hits: string[];
  priority_hits: string[];
  ok: boolean;
  error: string;
}

/** What is waiting on a human right now — drives the sidebar badge. */
export interface PmInbox {
  enabled: boolean;
  pending: number;
  overdue: number;
  by_level: Record<string, number>;
  next_timeout_at: string | null;
}

export interface PmMetricRow {
  day: string;
  metric_key: string;
  value: number;
  numerator: number | null;
  denominator: number | null;
}

export interface PmTickResult {
  task_id?: number;
  step?: string | null;
  status?: string | null;
  outcome?: string | null;
  [key: string]: unknown;
}

export interface PmSweepResult {
  auto_answered?: number;
  expired?: number;
  [key: string]: unknown;
}
