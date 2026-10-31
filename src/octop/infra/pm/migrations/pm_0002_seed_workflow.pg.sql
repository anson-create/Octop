-- PM workflow seed v2 (PostgreSQL): the 13-step pipeline behind wf-csm-solution.
-- Decisions levels follow D3=C (ship defaults, recalibrate after two weeks):
-- only reversible steps may auto-answer at timeout; scope/promise steps stay L2,
-- external publish stays L3.

INSERT INTO pm_step_handler (
  workflow_key, seq, step_key, handler_id, threshold,
  decision_timeout_s, decision_level, skill_name, enabled
) VALUES
  ('wf-csm-solution',  1, 'intake',               'wf_intake.intake',                80, 86400, 'L2', 'pm-intake',       1),
  ('wf-csm-solution',  2, 'link_expand',          'wf_intake.link_expand',           80, 86400, 'L2', 'pm-link-expand',  1),
  ('wf-csm-solution',  3, 'requirement_analysis', 'wf_analyze.requirement_analysis', 80, 86400, 'L2', 'pm-analysis',     1),
  ('wf-csm-solution',  4, 'code',                 'wf_intake.code',                  80, 86400, 'L2', 'pm-code-probe',   1),
  ('wf-csm-solution',  5, 'datadict',             'wf_intake.datadict',              80, 86400, 'L2', 'pm-datadict',     1),
  ('wf-csm-solution',  6, 'dedup',                'wf_intake.dedup',                 80, 86400, 'L2', NULL,              1),
  ('wf-csm-solution',  7, 'capability_fit',       'wf_analyze.capability_fit',       80, 86400, 'L2', 'pm-capability',   1),
  ('wf-csm-solution',  8, 'decision',             'wf_decide.decision',              80, 86400, 'L2', NULL,              1),
  ('wf-csm-solution',  9, 'prototype_gate',       'wf_decide.prototype_gate',        80,   180, 'L1', NULL,              1),
  ('wf-csm-solution', 10, 'prototype',            'wf_produce.prototype',            80,   180, 'L1', 'pm-prototype',    1),
  ('wf-csm-solution', 11, 'prd',                  'wf_produce.prd',                  80,   180, 'L1', 'pm-prd',          1),
  ('wf-csm-solution', 12, 'review',               'wf_review.review',                80, 86400, 'L2', NULL,              1),
  ('wf-csm-solution', 13, 'deliver',              'wf_review.deliver',               80, 86400, 'L3', NULL,              1)
ON CONFLICT (workflow_key, seq) DO NOTHING;
