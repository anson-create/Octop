-- PM workspace schema v1 (SQLite).
-- Paired with pm_0001_init.pg.sql — edit both or CI fails.
-- All DDL is idempotent so a partially applied migration can be re-run safely.

CREATE TABLE IF NOT EXISTS pm_workspace (
  id              INTEGER PRIMARY KEY,
  name            TEXT NOT NULL,
  description     TEXT,
  owner_user_id   INTEGER NOT NULL,
  client_token    TEXT,
  created_at      TEXT NOT NULL,
  archived_at     TEXT,
  UNIQUE(owner_user_id, name)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_pm_workspace_client_token
  ON pm_workspace(client_token) WHERE client_token IS NOT NULL;

CREATE TABLE IF NOT EXISTS pm_requirement (
  id             INTEGER PRIMARY KEY,
  public_id      TEXT NOT NULL UNIQUE,
  workspace_id   INTEGER NOT NULL REFERENCES pm_workspace(id) ON DELETE CASCADE,
  title          TEXT NOT NULL,
  raw_text       TEXT NOT NULL,
  source         TEXT NOT NULL DEFAULT 'manual',
  source_ref     TEXT,
  proposer       TEXT,
  priority       INTEGER,
  status         TEXT NOT NULL DEFAULT 'open',
  baseline_json  TEXT,
  client_token   TEXT,
  created_at     TEXT NOT NULL,
  CHECK (status IN ('open','in_progress','delivered','closed')),
  CHECK (source IN ('jira','manual','customer','kf','excel'))
);

CREATE INDEX IF NOT EXISTS ix_pm_requirement_workspace
  ON pm_requirement(workspace_id, status, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS ux_pm_requirement_source_ref
  ON pm_requirement(workspace_id, source, source_ref) WHERE source_ref IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_pm_requirement_client_token
  ON pm_requirement(client_token) WHERE client_token IS NOT NULL;

CREATE TABLE IF NOT EXISTS pm_requirement_change (
  id              INTEGER PRIMARY KEY,
  requirement_id  INTEGER NOT NULL REFERENCES pm_requirement(id) ON DELETE CASCADE,
  change_text     TEXT NOT NULL,
  raised_by       INTEGER,
  decision_id     INTEGER,
  accepted        INTEGER NOT NULL DEFAULT 0,
  created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_pm_requirement_change_req
  ON pm_requirement_change(requirement_id, created_at DESC);

CREATE TABLE IF NOT EXISTS pm_task (
  id                  INTEGER PRIMARY KEY,
  public_id           TEXT NOT NULL UNIQUE,
  requirement_id      INTEGER NOT NULL UNIQUE REFERENCES pm_requirement(id) ON DELETE CASCADE,
  workspace_id        INTEGER NOT NULL REFERENCES pm_workspace(id) ON DELETE CASCADE,
  stage               TEXT NOT NULL DEFAULT 'P1',
  status              TEXT NOT NULL DEFAULT 'queued',
  owner_user_id       INTEGER NOT NULL,
  pipeline_thread_id  TEXT,
  pipeline_agent_id   INTEGER,
  generation          INTEGER NOT NULL DEFAULT 1,
  locked_until        TEXT,
  client_token        TEXT,
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL,
  CHECK (stage IN ('P1','P2','P3','P4','P5')),
  CHECK (status IN ('queued','running','waiting_human','blocked','paused','delivered','archived'))
);

CREATE INDEX IF NOT EXISTS ix_pm_task_status ON pm_task(status, updated_at);

CREATE UNIQUE INDEX IF NOT EXISTS ux_pm_task_client_token
  ON pm_task(client_token) WHERE client_token IS NOT NULL;

CREATE TABLE IF NOT EXISTS pm_step_handler (
  workflow_key        TEXT NOT NULL,
  seq                 INTEGER NOT NULL,
  step_key            TEXT NOT NULL,
  handler_id          TEXT NOT NULL,
  threshold           INTEGER NOT NULL DEFAULT 80,
  decision_timeout_s  INTEGER NOT NULL DEFAULT 180,
  decision_level      TEXT NOT NULL DEFAULT 'L2',
  skill_name          TEXT,
  enabled             INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (workflow_key, seq),
  CHECK (decision_level IN ('L1','L2','L3'))
);

CREATE TABLE IF NOT EXISTS pm_task_step (
  id               INTEGER PRIMARY KEY,
  task_id          INTEGER NOT NULL REFERENCES pm_task(id) ON DELETE CASCADE,
  seq              INTEGER NOT NULL,
  step_key         TEXT NOT NULL,
  stage            TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'pending',
  attempt          INTEGER NOT NULL DEFAULT 0,
  gate_result_json TEXT,
  trace_json       TEXT,
  wall_clock_ms    INTEGER,
  error_code       TEXT,
  started_at       TEXT,
  finished_at      TEXT,
  UNIQUE(task_id, seq),
  CHECK (status IN ('pending','running','passed','failed','waiting_card','skipped'))
);

CREATE INDEX IF NOT EXISTS ix_pm_task_step_task ON pm_task_step(task_id, seq);

CREATE TABLE IF NOT EXISTS pm_artifact (
  id             INTEGER PRIMARY KEY,
  task_id        INTEGER NOT NULL REFERENCES pm_task(id) ON DELETE CASCADE,
  step_id        INTEGER REFERENCES pm_task_step(id) ON DELETE SET NULL,
  kind           TEXT NOT NULL,
  title          TEXT,
  audience       TEXT NOT NULL DEFAULT 'internal',
  fidelity       TEXT,
  fidelity_note  TEXT,
  version        INTEGER NOT NULL DEFAULT 1,
  body_path      TEXT,
  body_inline    TEXT,
  origin         TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  CHECK (kind IN ('data','document','prototype_html','review','bundle')),
  CHECK (audience IN ('internal','customer')),
  CHECK (fidelity IS NULL OR fidelity IN ('exact','partial','unlabeled')),
  CHECK (origin IN ('ai','rule','human','deterministic'))
);

CREATE INDEX IF NOT EXISTS ix_pm_artifact_task ON pm_artifact(task_id, kind, version DESC);

CREATE TABLE IF NOT EXISTS pm_conclusion (
  id             INTEGER PRIMARY KEY,
  artifact_id    INTEGER NOT NULL REFERENCES pm_artifact(id) ON DELETE CASCADE,
  seq            INTEGER NOT NULL,
  text           TEXT NOT NULL,
  customer_text  TEXT,
  status         TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  UNIQUE(artifact_id, seq),
  CHECK (status IN ('evidenced','uncovered','undetermined'))
);

CREATE INDEX IF NOT EXISTS ix_pm_conclusion_artifact ON pm_conclusion(artifact_id);

CREATE TABLE IF NOT EXISTS pm_evidence (
  id             INTEGER PRIMARY KEY,
  task_id        INTEGER NOT NULL REFERENCES pm_task(id) ON DELETE CASCADE,
  source_type    TEXT NOT NULL,
  port           TEXT NOT NULL,
  locator_json   TEXT NOT NULL,
  excerpt        TEXT NOT NULL,
  content_hash   TEXT NOT NULL,
  retrieved_at   TEXT NOT NULL,
  stale_at       TEXT,
  visibility     TEXT NOT NULL DEFAULT 'internal',
  confidence     REAL,
  created_at     TEXT NOT NULL,
  CHECK (source_type IN ('jira','code','datadict','kb','human','web')),
  CHECK (port IN ('jira','code','datadict','kb','human','web')),
  CHECK (visibility IN ('internal','customer'))
);

CREATE INDEX IF NOT EXISTS ix_pm_evidence_task ON pm_evidence(task_id, source_type);
CREATE INDEX IF NOT EXISTS ix_pm_evidence_stale ON pm_evidence(stale_at) WHERE stale_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS pm_conclusion_evidence (
  conclusion_id  INTEGER NOT NULL REFERENCES pm_conclusion(id) ON DELETE CASCADE,
  evidence_id    INTEGER NOT NULL REFERENCES pm_evidence(id) ON DELETE CASCADE,
  PRIMARY KEY (conclusion_id, evidence_id)
);

CREATE TABLE IF NOT EXISTS pm_decision (
  id                    INTEGER PRIMARY KEY,
  task_id               INTEGER NOT NULL REFERENCES pm_task(id) ON DELETE CASCADE,
  step_id               INTEGER REFERENCES pm_task_step(id) ON DELETE SET NULL,
  level                 TEXT NOT NULL,
  question              TEXT NOT NULL,
  options_json          TEXT NOT NULL,
  recommended_key       TEXT,
  option_snapshot_json  TEXT,
  assignee_user_id      INTEGER,
  status                TEXT NOT NULL DEFAULT 'pending',
  selected_key          TEXT,
  note                  TEXT,
  decided_by            INTEGER,
  decided_at            TEXT,
  timeout_at            TEXT NOT NULL,
  auto_decided          INTEGER NOT NULL DEFAULT 0,
  appeal_state          TEXT,
  thread_id             TEXT,
  resume_agent_id       INTEGER,
  action_requests_json  TEXT,
  client_token          TEXT,
  created_at            TEXT NOT NULL,
  CHECK (level IN ('L1','L2','L3')),
  CHECK (status IN ('pending','answered','auto_answered','expired','void')),
  CHECK (appeal_state IS NULL OR appeal_state IN ('appealed','upheld','reversed'))
);

CREATE INDEX IF NOT EXISTS ix_pm_decision_pending
  ON pm_decision(status, timeout_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS ix_pm_decision_task ON pm_decision(task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_pm_decision_assignee
  ON pm_decision(assignee_user_id, status);

CREATE TABLE IF NOT EXISTS pm_review (
  id                INTEGER PRIMARY KEY,
  task_id           INTEGER NOT NULL REFERENCES pm_task(id) ON DELETE CASCADE,
  artifact_id       INTEGER REFERENCES pm_artifact(id) ON DELETE SET NULL,
  perspective       TEXT NOT NULL,
  verdict           TEXT NOT NULL,
  comment           TEXT,
  reviewer_user_id  INTEGER,
  created_at        TEXT NOT NULL,
  UNIQUE(task_id, perspective),
  CHECK (perspective IN ('business','tech','customer')),
  CHECK (verdict IN ('pass','reject'))
);

CREATE TABLE IF NOT EXISTS pm_integration (
  id              INTEGER PRIMARY KEY,
  workspace_id    INTEGER NOT NULL REFERENCES pm_workspace(id) ON DELETE CASCADE,
  vendor          TEXT NOT NULL,
  enabled         INTEGER NOT NULL DEFAULT 1,
  mode            TEXT NOT NULL DEFAULT 'real',
  credential_ref  TEXT,
  mapping_json    TEXT,
  last_probe_at   TEXT,
  probe_status    TEXT,
  UNIQUE(workspace_id, vendor),
  CHECK (mode IN ('real','drill'))
);

CREATE TABLE IF NOT EXISTS pm_metric_daily (
  day           TEXT NOT NULL,
  workspace_id  INTEGER NOT NULL,
  metric_key    TEXT NOT NULL,
  value         REAL NOT NULL,
  numerator     INTEGER,
  denominator   INTEGER,
  PRIMARY KEY (day, workspace_id, metric_key)
);

CREATE TABLE IF NOT EXISTS pm_eval_case (
  id           INTEGER PRIMARY KEY,
  name         TEXT NOT NULL UNIQUE,
  kind         TEXT NOT NULL,
  input_json   TEXT NOT NULL,
  expect_json  TEXT NOT NULL,
  active       INTEGER NOT NULL DEFAULT 1,
  CHECK (kind IN ('golden','adversarial'))
);
