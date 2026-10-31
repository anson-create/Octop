"""Application service: HTTP router → service → repos/domain.

Every endpoint in the contract lands here. The service owns cross-cutting rules
that a repo cannot: permission-scoped lookups, the export gate sequence
(trim → scan → confirm → audit), metric rollups, and pipeline orchestration.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from contextlib import suppress
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.pm import errors
from octop.infra.pm.domain import decisions as decision_rules
from octop.infra.pm.domain import evidence as evidence_rules
from octop.infra.pm.domain import export as export_rules
from octop.infra.pm.domain import stages
from octop.infra.pm.migrate import migration_health
from octop.infra.pm.models import Conclusion, Decision
from octop.infra.pm.repos import PERSPECTIVES, PmRepos
from octop.infra.pm.repos._base import day_key, dumps, loads, now_iso
from octop.infra.pm.runtime import pipeline as pipeline_mod
from octop.infra.pm.runtime.executor import DeterministicExecutor, StepContext
from octop.infra.pm.runtime.pipeline import Pipeline

PM_ENABLED_ENV = "OCTOP_PM_ENABLED"


def is_enabled() -> bool:
    return os.environ.get(PM_ENABLED_ENV, "1").strip().lower() not in ("0", "false", "off", "no")


# A bare issue key is a direct lookup, not a text search: "CCTOWER-3597" as a
# `text ~` query matches nothing on most DC instances.
_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,9}-\d+$")


def _looks_like_issue_key(value: str) -> bool:
    return bool(_ISSUE_KEY_RE.match(value.strip().upper()))


class PMService:
    def __init__(self, db: DatabasePool, *, audit: Any | None = None) -> None:
        self._db = db
        self._repos = PmRepos(db)
        self._pipeline = Pipeline(self._repos)
        self._audit = audit
        # Built on first use: SecretRepo is cheap but tests never touch Jira.
        self._secrets: Any | None = None
        # Idempotent and cheap; keeps CLI/tests consistent with the server start
        # path without requiring callers to remember a second bootstrap step.
        self._migration_error: str | None = None
        try:
            from octop.infra.pm.migrate import run_pm_migrations  # noqa: PLC0415

            run_pm_migrations(db)
        except Exception as exc:
            self._migration_error = str(exc)

    @property
    def repos(self) -> PmRepos:
        return self._repos

    @property
    def pipeline(self) -> Pipeline:
        return self._pipeline

    def attach_models(self, provider: Any | None) -> None:
        """Wire model-backed steps once the host runtime is up.

        ``_start_pm`` runs before ``_boot_runtime``, so the chat models do not exist
        yet at construction time. The server calls this after the runtime boots; a
        ``None`` provider leaves the nine model steps reporting ``executor_unavailable``
        rather than silently degrading to rule-based guesses.
        """
        from octop.infra.pm.runtime.executor import (  # noqa: PLC0415
            DeterministicExecutor,
            RegistryExecutor,
        )
        from octop.infra.pm.runtime.model_executor import ModelStepExecutor  # noqa: PLC0415

        if provider is None:
            model_executor = ModelStepExecutor(models_provider=None, repos=self._repos)
        else:
            model_executor = ModelStepExecutor(models_provider=provider, repos=self._repos)
        self._pipeline.set_executor(
            RegistryExecutor(DeterministicExecutor(), model_executor)
        )

    # --- audit -------------------------------------------------------------

    def _record(self, actor: str | None, action: str, target: str, payload: Any = None) -> None:
        if self._audit is None:
            return
        # Auditing must never break the call it is recording.
        with suppress(Exception):
            self._audit.write(
                actor=actor,
                action=action,
                target=target,
                payload=dumps(payload) if payload is not None else None,
            )

    # --- health ------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        migration = migration_health(self._db)
        if self._migration_error:
            migration = {"ok": False, "error": self._migration_error, "version": 0, "pending": []}
        counts = self._repos.tasks.counts_by_status()
        pending = self._repos.decisions.list(status="pending", limit=1)
        due = len(self._repos.decisions.pending_due())
        return {
            "enabled": is_enabled(),
            "migration": migration,
            "status": "ok" if migration.get("ok") else "degraded",
            "tasks": counts,
            "decisions_pending": pending[1],
            "decisions_due": due,
        }

    # --- workspaces --------------------------------------------------------

    def list_workspaces(self) -> list[dict[str, Any]]:
        return [_workspace_payload(item) for item in self._repos.workspaces.list_all()]

    def create_workspace(
        self,
        *,
        name: str,
        owner_user_id: int,
        description: str | None = None,
        client_token: str | None = None,
    ) -> dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise errors.invalid("workspace name is required")
        if client_token:
            existing = self._repos.workspaces.get_by_client_token(client_token)
            if existing is not None:
                return _workspace_payload(existing)
        if self._repos.workspaces.get_by_name(owner_user_id, name) is not None:
            raise errors.conflict("workspace name already exists", name=name)
        created = self._repos.workspaces.create(
            name=name,
            owner_user_id=owner_user_id,
            description=description,
            client_token=client_token,
        )
        return _workspace_payload(created)

    def ensure_default_workspace(self, owner_user_id: int) -> dict[str, Any]:
        for item in self._repos.workspaces.list_all():
            if item.owner_user_id == owner_user_id:
                return _workspace_payload(item)
        return self.create_workspace(name="默认工作区", owner_user_id=owner_user_id)

    # --- requirements ------------------------------------------------------

    def list_requirements(
        self,
        *,
        workspace_id: int | None = None,
        status: str | None = None,
        query: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        page = max(1, page)
        rows, total = self._repos.requirements.list(
            workspace_id=workspace_id,
            status=status,
            query=query,
            offset=(page - 1) * page_size,
            limit=page_size,
        )
        return {
            "items": [_requirement_payload(item) for item in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def create_requirement(
        self,
        *,
        workspace_id: int,
        title: str,
        raw_text: str,
        source: str = "manual",
        source_ref: str | None = None,
        proposer: str | None = None,
        priority: int | None = None,
        baseline: dict[str, Any] | None = None,
        client_token: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        if self._repos.workspaces.get(workspace_id) is None:
            raise errors.not_found("workspace", workspace_id)
        title = (title or "").strip()
        raw_text = (raw_text or "").strip()
        if not title or not raw_text:
            raise errors.invalid("title and raw_text are required")
        if client_token:
            existing = self._repos.requirements.get_by_client_token(client_token)
            if existing is not None:
                return _requirement_payload(existing)
        if source_ref:
            duplicate = self._repos.requirements.get_by_source_ref(workspace_id, source, source_ref)
            if duplicate is not None:
                return _requirement_payload(duplicate)
        created = self._repos.requirements.create(
            workspace_id=workspace_id,
            title=title,
            raw_text=raw_text,
            source=source,
            source_ref=source_ref,
            proposer=proposer,
            priority=priority,
            baseline=baseline,
            client_token=client_token,
        )
        self._record(actor, "pm.requirement.create", created.public_id, {"source": source})
        return _requirement_payload(created)

    def requirement_detail(self, requirement_id: int) -> dict[str, Any]:
        requirement = self._repos.requirements.get(requirement_id)
        if requirement is None:
            raise errors.not_found("requirement", requirement_id)
        task = self._repos.tasks.get_by_requirement(requirement_id)
        return {
            **_requirement_payload(requirement),
            "changes": [
                {
                    "id": change.id,
                    "change_text": change.change_text,
                    "accepted": bool(change.accepted),
                    "raised_by": change.raised_by,
                    "created_at": change.created_at,
                }
                for change in self._repos.requirements.list_changes(requirement_id)
            ],
            "task": _task_payload(task) if task else None,
        }

    def add_change(
        self,
        *,
        requirement_id: int,
        change_text: str,
        raised_by: int | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        if self._repos.requirements.get(requirement_id) is None:
            raise errors.not_found("requirement", requirement_id)
        text = (change_text or "").strip()
        if not text:
            raise errors.invalid("change_text is required")
        change = self._repos.requirements.add_change(
            requirement_id=requirement_id, change_text=text, raised_by=raised_by
        )
        self._record(actor, "pm.requirement.change", str(requirement_id), {"change_id": change.id})
        return {
            "id": change.id,
            "requirement_id": change.requirement_id,
            "change_text": change.change_text,
            "accepted": bool(change.accepted),
            "created_at": change.created_at,
        }

    def accept_change(self, change_id: int, *, decision_id: int | None = None) -> bool:
        return self._repos.requirements.accept_change(change_id, decision_id)

    # --- tasks -------------------------------------------------------------

    def create_task(
        self,
        *,
        requirement_id: int,
        owner_user_id: int,
        client_token: str | None = None,
    ) -> dict[str, Any]:
        requirement = self._repos.requirements.get(requirement_id)
        if requirement is None:
            raise errors.not_found("requirement", requirement_id)
        if client_token:
            existing = self._repos.tasks.get_by_client_token(client_token)
            if existing is not None:
                return _task_payload(existing)
        current = self._repos.tasks.get_by_requirement(requirement_id)
        if current is not None:
            raise errors.conflict(
                "requirement already has a task", requirement_id=requirement_id, task_id=current.id
            )
        task = self._repos.tasks.create(
            requirement_id=requirement_id,
            workspace_id=requirement.workspace_id,
            owner_user_id=owner_user_id,
            client_token=client_token,
        )
        self._repos.steps.ensure_steps(task.id)
        self._repos.requirements.set_status(requirement_id, "in_progress")
        return _task_payload(task)

    def list_tasks(
        self,
        *,
        owner_user_id: int | None = None,
        status: str | None = None,
        workspace_id: int | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        page = max(1, page)
        rows, total = self._repos.tasks.list(
            owner_user_id=owner_user_id,
            status=status,
            workspace_id=workspace_id,
            offset=(page - 1) * page_size,
            limit=page_size,
        )
        return {
            "items": [_task_payload(item) for item in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def task_detail(self, task_id: int) -> dict[str, Any]:
        task = self._repos.tasks.get(task_id)
        if task is None:
            raise errors.not_found("task", task_id)
        steps = self._repos.steps.list_for_task(task_id)
        requirement = self._repos.requirements.get(task.requirement_id)
        artifacts = self._repos.artifacts.list_for_task(task_id)
        pending = self._repos.decisions.pending_for_task(task_id)
        return {
            **_task_payload(task),
            "requirement": _requirement_payload(requirement) if requirement else None,
            "steps": pipeline_mod.summarize_steps(steps),
            "stages": pipeline_mod.stage_progress(steps),
            "artifacts": [_artifact_payload(item) for item in artifacts],
            "pending_decisions": [_decision_payload(item) for item in pending],
            "evidence_count": self._repos.evidence.count_for_task(task_id),
            "evidence_sources": self._repos.evidence.source_breakdown(task_id),
            "conclusions": self._repos.conclusions.stats_for_task(task_id),
        }

    def tick(self, task_id: int, *, human_note: str | None = None) -> dict[str, Any]:
        return self._pipeline.tick(task_id, human_note=human_note)

    def pause(self, task_id: int) -> dict[str, Any]:
        task = self._repos.tasks.get(task_id)
        if task is None:
            raise errors.not_found("task", task_id)
        if task.status in ("delivered", "archived"):
            raise errors.conflict(f"task is {task.status}")
        self._repos.tasks.set_status(task_id, "paused")
        return _task_payload(self._repos.tasks.get(task_id))

    def resume(self, task_id: int) -> dict[str, Any]:
        task = self._repos.tasks.get(task_id)
        if task is None:
            raise errors.not_found("task", task_id)
        if task.status not in ("paused", "blocked"):
            raise errors.conflict(f"task is {task.status}, not paused or blocked")
        # A blocked task has no other way back: ``claim`` only takes queued/running, so
        # without clearing the lock and re-queueing here, a single failed step would
        # strand the task until someone reflowed it by hand.
        self._repos.tasks.release(task_id)
        self._repos.tasks.set_status(task_id, "queued")
        return _task_payload(self._repos.tasks.get(task_id))

    def task_steps(self, task_id: int) -> list[dict[str, Any]]:
        if self._repos.tasks.get(task_id) is None:
            raise errors.not_found("task", task_id)
        return pipeline_mod.summarize_steps(self._repos.steps.list_for_task(task_id))

    def reject_step(
        self, step_id: int, *, note: str, user_id: int | None = None, actor: str | None = None
    ) -> dict[str, Any]:
        result = self._pipeline.reject_step(step_id, note=note, user_id=user_id)
        self._record(actor, "pm.step.reject", str(step_id), {"note_len": len(note or "")})
        return result

    def step_trace(self, step_id: int) -> dict[str, Any]:
        step = self._repos.steps.get(step_id)
        if step is None:
            raise errors.not_found("step", step_id)
        trace = pipeline_mod.trace_view(step.trace_json)
        task = self._repos.tasks.get(step.task_id)
        evidence = self._repos.evidence.list_for_task(step.task_id)
        cited = [int(item) for item in trace.get("evidence_ids", [])]
        return {
            "step_id": step.id,
            "task_id": step.task_id,
            "step_key": step.step_key,
            "status": step.status,
            "trace": trace,
            "gate": pipeline_mod.gate_payload(step.gate_result_json),
            "evidence": [
                _evidence_payload(item)
                for item in evidence
                if not cited or item.id in cited
            ],
            "sources": self._repos.evidence.source_breakdown(step.task_id),
            "workspace_id": task.workspace_id if task else None,
        }

    def reflow(
        self, task_id: int, *, from_step_key: str | None = None, actor: str | None = None
    ) -> dict[str, Any]:
        task = self._repos.tasks.get(task_id)
        if task is None:
            raise errors.not_found("task", task_id)
        step_key = stages.resolve_step_key(from_step_key or "requirement_analysis")
        if step_key is None:
            raise errors.invalid("unknown step_key", step_key=from_step_key)
        spec = stages.step_by_key(step_key)
        assert spec is not None
        voided = self._repos.decisions.void_for_task(task_id)
        reset = self._repos.steps.reset_from(task_id, spec.seq)
        self._repos.tasks.set_pipeline(task_id, generation=task.generation + 1)
        self._repos.tasks.set_status(task_id, "queued", stage=spec.stage)
        payload = {
            "task_id": task_id,
            "from_seq": spec.seq,
            "reset_steps": reset,
            "voided_decisions": voided,
            "generation": task.generation + 1,
        }
        self._record(actor, "pm.task.reflow", str(task_id), payload)
        return payload

    # --- artifacts / evidence ---------------------------------------------

    def get_artifact(self, artifact_id: int, *, audience: str = "internal") -> dict[str, Any]:
        artifact = self._repos.artifacts.get(artifact_id)
        if artifact is None:
            raise errors.not_found("artifact", artifact_id)
        if audience == "customer" and artifact.audience != "customer":
            raise errors.forbidden("artifact is internal-only", artifact_id=artifact_id)
        return _artifact_payload(artifact, include_body=True)

    def artifact_conclusions(self, artifact_id: int) -> dict[str, Any]:
        artifact = self._repos.artifacts.get(artifact_id)
        if artifact is None:
            raise errors.not_found("artifact", artifact_id)
        items = []
        for conclusion in self._repos.conclusions.list_for_artifact(artifact_id):
            evidence_ids = self._repos.conclusions.evidence_ids(conclusion.id)
            items.append(
                {
                    **_conclusion_payload(conclusion),
                    "evidence": [
                        _evidence_payload(evidence)
                        for evidence in (
                            self._repos.evidence.get(eid) for eid in evidence_ids
                        )
                        if evidence is not None
                    ],
                }
            )
        return {"artifact_id": artifact_id, "items": items}

    def export_artifact(
        self,
        artifact_id: int,
        *,
        audience: str = "internal",
        confirm: bool = False,
        internal_names: Sequence[str] = (),
        actor: str | None = None,
    ) -> dict[str, Any]:
        if audience not in ("internal", "customer"):
            raise errors.invalid("audience must be internal or customer")
        artifact = self._repos.artifacts.get(artifact_id)
        if artifact is None:
            raise errors.not_found("artifact", artifact_id)
        task = self._repos.tasks.get(artifact.task_id)
        conclusions = self._repos.conclusions.list_for_artifact(artifact_id)
        evidence_map = {
            item.id: {"visibility": item.visibility, "source_type": item.source_type}
            for item in self._repos.evidence.list_for_task(artifact.task_id)
        }
        plan = export_rules.build_plan(
            audience=audience,
            title=artifact.title or (task.public_id if task else f"artifact-{artifact_id}"),
            body=artifact.body_inline or "",
            conclusions=[
                {
                    "seq": item.seq,
                    "text": item.text,
                    "customer_text": item.customer_text,
                    "status": item.status,
                    "evidence_ids": self._repos.conclusions.evidence_ids(item.id),
                }
                for item in conclusions
            ],
            evidence=evidence_map,
            internal_names=internal_names,
        )
        content: str | None = None
        if plan.ok and confirm:
            content = export_rules.render_markdown(plan)
        self._record(
            actor,
            "pm.export",
            f"artifact:{artifact_id}:{audience}",
            {
                "audience": audience,
                "hits": len(plan.hits),
                "downgraded": plan.downgraded,
                "confirmed": confirm,
            },
        )
        payload = plan.to_dict()
        payload["content"] = content
        payload["preview_only"] = not confirm
        return payload

    def export_bundle(self, task_id: int, *, actor: str | None = None) -> dict[str, Any]:
        task = self._repos.tasks.get(task_id)
        if task is None:
            raise errors.not_found("task", task_id)
        artifacts = self._repos.artifacts.list_for_task(task_id)
        lines = [f"# 交付包 {task.public_id}", ""]
        for artifact in artifacts:
            lines.append(f"## {artifact.title or artifact.kind} (v{artifact.version})")
            lines.append(f"- 受众：{artifact.audience} · 保真度：{artifact.fidelity or '未标注'}")
            if artifact.body_inline:
                lines.append(artifact.body_inline.strip())
            for conclusion in self._repos.conclusions.list_for_artifact(artifact.id):
                lines.append(f"  - [{conclusion.status}] {conclusion.text}")
            lines.append("")
        decisions = self._repos.decisions.list(task_id=task_id, limit=100)[0]
        if decisions:
            lines.append("## 决策记录")
            for item in decisions:
                lines.append(
                    f"- {item.question} → {item.selected_key or '(未定)'} "
                    f"[{item.status}/{item.level}] {item.note or ''}"
                )
        self._record(actor, "pm.export.bundle", task.public_id, {"artifacts": len(artifacts)})
        return {"task_id": task_id, "content": "\n".join(lines).strip() + "\n"}

    def get_evidence(self, evidence_id: int, *, audience: str = "internal") -> dict[str, Any]:
        evidence = self._repos.evidence.get(evidence_id)
        if evidence is None:
            raise errors.not_found("evidence", evidence_id)
        if audience == "customer" and evidence.visibility != "customer":
            raise errors.forbidden("evidence is internal-only", evidence_id=evidence_id)
        return _evidence_payload(evidence, include_locator=True)

    # --- decisions ---------------------------------------------------------

    def list_decisions(
        self,
        *,
        status: str | None = "pending",
        level: str | None = None,
        mine: bool = False,
        user_id: int | None = None,
        task_id: int | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        rows, total = self._repos.decisions.list(
            task_id=task_id,
            status=status,
            level=level,
            assignee_user_id=user_id if mine else None,
            offset=(max(1, page) - 1) * page_size,
            limit=page_size,
        )
        return {
            "items": [_decision_payload(item) for item in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def answer_decision(
        self,
        decision_id: int,
        *,
        selected_key: str,
        note: str,
        user_id: int | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        decision = self._repos.decisions.get(decision_id)
        if decision is None:
            raise errors.not_found("decision", decision_id)
        if decision.status != "pending":
            raise errors.conflict(f"decision is {decision.status}", status=decision.status)
        options = loads(decision.options_json, [])
        selected, cleaned = decision_rules.validate_answer(
            options, selected_key=selected_key, note=note
        )
        if not self._repos.decisions.answer(
            decision_id, selected_key=selected, note=cleaned, decided_by=user_id
        ):
            raise errors.conflict("decision was already handled")
        self._record(actor, "pm.decision.answer", str(decision_id), {"selected": selected})
        advanced = self._pipeline.after_decision(decision_id)
        return {"decision": _decision_payload(self._repos.decisions.get(decision_id)), "pipeline": advanced}

    def appeal_decision(
        self, decision_id: int, *, user_id: int | None = None, actor: str | None = None
    ) -> dict[str, Any]:
        decision = self._repos.decisions.get(decision_id)
        if decision is None:
            raise errors.not_found("decision", decision_id)
        if not decision.auto_decided:
            raise errors.invalid("only auto-decided cards can be appealed")
        if not self._repos.decisions.appeal(decision_id):
            raise errors.conflict("decision is not appealable", state=decision.appeal_state)
        self._record(actor, "pm.decision.appeal", str(decision_id), {"by": user_id})
        return _decision_payload(self._repos.decisions.get(decision_id))

    def resolve_appeal(self, decision_id: int, *, state: str) -> dict[str, Any]:
        if state not in ("upheld", "reversed"):
            raise errors.invalid("state must be upheld or reversed")
        if not self._repos.decisions.resolve_appeal(decision_id, state):
            raise errors.conflict("decision has no open appeal")
        return _decision_payload(self._repos.decisions.get(decision_id))

    def transfer_decision(
        self, decision_id: int, *, assignee_user_id: int, actor: str | None = None
    ) -> dict[str, Any]:
        if not self._repos.decisions.transfer(decision_id, assignee_user_id):
            raise errors.conflict("decision is not pending")
        self._record(actor, "pm.decision.transfer", str(decision_id), {"to": assignee_user_id})
        return _decision_payload(self._repos.decisions.get(decision_id))

    # --- reviews -----------------------------------------------------------

    def submit_review(
        self,
        *,
        task_id: int,
        perspective: str,
        verdict: str,
        comment: str | None = None,
        reviewer_user_id: int | None = None,
        artifact_id: int | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        if self._repos.tasks.get(task_id) is None:
            raise errors.not_found("task", task_id)
        if perspective not in PERSPECTIVES:
            raise errors.invalid(f"perspective must be one of {PERSPECTIVES}")
        if verdict not in ("pass", "reject"):
            raise errors.invalid("verdict must be pass or reject")
        if verdict == "reject" and len((comment or "").strip()) < decision_rules.NOTE_MIN_LEN:
            raise errors.invalid("rejecting a deliverable requires a reason of 10+ characters")
        review = self._repos.reviews.submit(
            task_id=task_id,
            perspective=perspective,
            verdict=verdict,
            comment=comment,
            reviewer_user_id=reviewer_user_id,
            artifact_id=artifact_id,
        )
        self._record(actor, "pm.review", f"task:{task_id}", {"perspective": perspective})
        return {
            "id": review.id,
            "task_id": review.task_id,
            "perspective": review.perspective,
            "verdict": review.verdict,
            "comment": review.comment,
            "created_at": review.created_at,
        }

    def list_reviews(self, task_id: int) -> dict[str, Any]:
        if self._repos.tasks.get(task_id) is None:
            raise errors.not_found("task", task_id)
        return {
            "task_id": task_id,
            "items": [
                {
                    "id": item.id,
                    "perspective": item.perspective,
                    "verdict": item.verdict,
                    "comment": item.comment,
                    "reviewer_user_id": item.reviewer_user_id,
                    "created_at": item.created_at,
                }
                for item in self._repos.reviews.list_for_task(task_id)
            ],
            "complete": set(self._repos.reviews.verdict_map(task_id)) >= set(PERSPECTIVES),
        }

    # --- jira --------------------------------------------------------------

    def _jira_token_key(self, workspace_id: int) -> str:
        return f"pm.jira.token.{workspace_id}"

    def _jira_secrets(self) -> Any:
        if self._secrets is None:
            from octop.infra.db.repos.secrets import SecretRepo  # noqa: PLC0415

            self._secrets = SecretRepo(self._db)
        return self._secrets

    def _jira_mapping(self, workspace_id: int) -> dict[str, Any]:
        item = self._repos.integrations.get_by_vendor(workspace_id, "jira")
        if item is None:
            return {}
        return loads(item.mapping_json, {}) or {}

    def _jira_token(self, workspace_id: int) -> str:
        raw = self._jira_secrets().get(self._jira_token_key(workspace_id))
        return raw.decode("utf-8").strip() if raw else ""

    def jira_status(self, workspace_id: int) -> dict[str, Any]:
        """What the picker needs to know before it tries to list anything."""
        mapping = self._jira_mapping(workspace_id)
        item = self._repos.integrations.get_by_vendor(workspace_id, "jira")
        return {
            "configured": bool(mapping.get("base_url") and self._jira_token(workspace_id)),
            "base_url": str(mapping.get("base_url") or ""),
            "jql": str(mapping.get("jql") or ""),
            "verify_tls": bool(mapping.get("verify_tls", True)),
            "token_set": bool(self._jira_token(workspace_id)),
            "enabled": bool(item.enabled) if item is not None else False,
            "last_probe_at": getattr(item, "last_probe_at", None) if item is not None else None,
            "probe_status": getattr(item, "probe_status", None) if item is not None else None,
        }

    def save_jira_config(
        self,
        workspace_id: int,
        *,
        base_url: str,
        token: str | None = None,
        jql: str | None = None,
        verify_tls: bool = True,
    ) -> dict[str, Any]:
        """Persist the endpoint in the integration row and the PAT in the secret store.

        Splitting them is the whole point: a database dump stays harmless.
        """
        if self._repos.workspaces.get(workspace_id) is None:
            raise errors.not_found("workspace", workspace_id)
        cleaned = (base_url or "").strip().rstrip("/")
        if not cleaned:
            raise errors.invalid("base_url is required")
        if not cleaned.startswith(("http://", "https://")):
            raise errors.invalid("base_url must start with http:// or https://")

        mapping = self._jira_mapping(workspace_id)
        mapping["base_url"] = cleaned
        mapping["verify_tls"] = bool(verify_tls)
        if jql is not None:
            mapping["jql"] = jql.strip()

        key = self._jira_token_key(workspace_id)
        secrets = self._jira_secrets()
        if token is not None and token.strip():
            value = token.strip().encode("utf-8")
            if secrets.get(key) is None:
                secrets.get_or_create(key, lambda: value)
                # get_or_create races are fine; make sure the value we were handed
                # actually landed rather than a stale one.
                if secrets.get(key) != value:
                    secrets.rotate(key, value)
            else:
                secrets.rotate(key, value)

        self._repos.integrations.upsert(
            workspace_id=workspace_id,
            vendor="jira",
            enabled=True,
            mode="real",
            credential_ref=key,
            mapping=mapping,
        )
        return self.jira_status(workspace_id)

    def jira_client(self, workspace_id: int) -> Any:
        """Build a client from stored config. Raises PortUnavailable when unset."""
        from octop.infra.pm.ports.base import PortUnavailable  # noqa: PLC0415
        from octop.infra.pm.ports.jira import JiraClient  # noqa: PLC0415

        mapping = self._jira_mapping(workspace_id)
        base_url = str(mapping.get("base_url") or "")
        token = self._jira_token(workspace_id)
        if not base_url:
            raise PortUnavailable("Jira 未配置：请先在设置里填写站点地址与 token")
        if not token:
            raise PortUnavailable("Jira token 未设置")
        return JiraClient(
            base_url,
            token,
            verify_tls=bool(mapping.get("verify_tls", True)),
        )

    def probe_jira(self, workspace_id: int) -> dict[str, Any]:
        from octop.infra.pm.ports.base import PortError  # noqa: PLC0415

        item = self._repos.integrations.get_by_vendor(workspace_id, "jira")
        info: dict[str, Any] = {}
        try:
            with self.jira_client(workspace_id) as client:
                raw = client.probe()
                if isinstance(raw, dict):
                    info = raw
        except PortError as exc:
            if item is not None:
                self._repos.integrations.record_probe(item.id, "error")
            raise errors.invalid(str(exc)) from exc
        if item is not None:
            self._repos.integrations.record_probe(item.id, "configured")
        return info

    def list_jira_issues(
        self, workspace_id: int, *, q: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        """Candidates for the intake picker. ``q`` is a keyword or a bare issue key."""
        from octop.infra.pm.ports.base import PortError  # noqa: PLC0415
        from octop.infra.pm.ports.jira import DEFAULT_JQL  # noqa: PLC0415

        mapping = self._jira_mapping(workspace_id)
        default_jql = str(mapping.get("jql") or DEFAULT_JQL)
        needle = (q or "").strip()

        if not needle:
            jql = f"({default_jql})" if default_jql else "ORDER BY updated DESC"
        elif _looks_like_issue_key(needle):
            jql = f'key = "{needle.upper()}"'
        else:
            # Quote so a title containing JQL operators cannot rewrite the query.
            escaped = needle.replace('"', '\\"')
            jql = f'text ~ "{escaped}" ORDER BY updated DESC'

        try:
            with self.jira_client(workspace_id) as client:
                issues = client.search(jql, limit=limit)
        except PortError as exc:
            raise errors.invalid(str(exc)) from exc
        return {"jql": jql, "items": issues, "total": len(issues)}

    def create_requirement_from_jira(
        self,
        workspace_id: int,
        *,
        issue_key: str,
        proposer: str | None = None,
        priority: int | None = None,
        client_token: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """The only way a requirement is born: an issue that exists in Jira.

        Title and raw text are pulled server-side. Whatever the browser sends for
        those fields is ignored, so an edited payload cannot invent a requirement
        that no ticket backs.
        """
        from octop.infra.pm.ports.base import PortError  # noqa: PLC0415

        key = (issue_key or "").strip().upper()
        if not key:
            raise errors.invalid("issue_key is required")

        # A repeat submit should return the existing requirement, not a 400 from
        # Jira being briefly unreachable.
        existing = self._repos.requirements.get_by_source_ref(workspace_id, "jira", key)
        if existing is not None:
            return _requirement_payload(existing)

        try:
            with self.jira_client(workspace_id) as client:
                issue = client.issue(key)
        except PortError as exc:
            raise errors.invalid(str(exc)) from exc

        title = (issue.get("title") or key).strip()
        description = (issue.get("description") or "").strip()
        if not description:
            # G-P1-03 accepts an explicit statement of absence, but an empty raw
            # text makes every downstream step hallucinate around nothing. Say so
            # in the text instead of leaving it blank.
            description = f"（Jira 工单未填写描述；以下事实仅来自标题）{title}"

        # The issue key deliberately stays out of raw_text. Every export runs a
        # blacklist scan that blocks internal ticket ids, and the intake artifact
        # embeds this text verbatim — so a key here would make the requirement
        # permanently un-exportable. The key lives in source_ref and baseline,
        # which are metadata and never rendered into the deliverable.
        raw_text = description

        return self.create_requirement(
            workspace_id=workspace_id,
            title=title,
            raw_text=raw_text,
            source="jira",
            source_ref=key,
            proposer=proposer or issue.get("reporter") or None,
            priority=priority,
            baseline={
                "jira_key": key,
                "jira_status": issue.get("status"),
                "jira_issue_type": issue.get("issue_type"),
                "jira_priority": issue.get("priority"),
                "jira_assignee": issue.get("assignee"),
                "jira_updated": issue.get("updated"),
            },
            client_token=client_token,
            actor=actor,
        )

    # --- integrations ------------------------------------------------------

    def list_integrations(self, workspace_id: int) -> list[dict[str, Any]]:
        return [
            {
                "id": item.id,
                "workspace_id": item.workspace_id,
                "vendor": item.vendor,
                "enabled": bool(item.enabled),
                "mode": item.mode,
                "credential_set": bool(item.credential_ref),
                "last_probe_at": item.last_probe_at,
                "probe_status": item.probe_status,
            }
            for item in self._repos.integrations.list_for_workspace(workspace_id)
        ]

    def upsert_integration(
        self,
        *,
        workspace_id: int,
        vendor: str,
        enabled: bool = True,
        mode: str = "real",
        credential_ref: str | None = None,
        mapping: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if mode not in ("real", "drill"):
            raise errors.invalid("mode must be real or drill")
        item = self._repos.integrations.upsert(
            workspace_id=workspace_id,
            vendor=vendor,
            enabled=enabled,
            mode=mode,
            credential_ref=credential_ref,
            mapping=mapping,
        )
        return {
            "id": item.id,
            "vendor": item.vendor,
            "enabled": bool(item.enabled),
            "mode": item.mode,
            "credential_set": bool(item.credential_ref),
        }

    def probe_integration(self, integration_id: int) -> dict[str, Any]:
        item = self._repos.integrations.get(integration_id)
        if item is None:
            raise errors.not_found("integration", integration_id)
        # A probe never blocks: it records reachability, and an unreachable vendor
        # downgrades the matching port to "未检索" in traces instead of failing steps.
        status = "drill" if item.mode == "drill" else ("configured" if item.credential_ref else "missing_credential")
        self._repos.integrations.record_probe(integration_id, status)
        return {"integration_id": integration_id, "probe_status": status}

    # --- metrics -----------------------------------------------------------

    def metrics_daily(
        self, *, workspace_id: int, date_from: str, date_to: str
    ) -> dict[str, Any]:
        self.refresh_metrics(workspace_id)
        rows = self._repos.metrics.series(
            workspace_id=workspace_id, date_from=date_from, date_to=date_to
        )
        return {
            "workspace_id": workspace_id,
            "items": [
                {
                    "day": item.day,
                    "metric_key": item.metric_key,
                    "value": item.value,
                    "numerator": item.numerator,
                    "denominator": item.denominator,
                }
                for item in rows
            ],
        }

    def refresh_metrics(self, workspace_id: int) -> dict[str, float]:
        tasks, _ = self._repos.tasks.list(workspace_id=workspace_id, limit=1000)
        task_ids = {task.id for task in tasks}
        total = evidenced = uncovered = undetermined = 0
        for task_id in task_ids:
            stats = self._repos.conclusions.stats_for_task(task_id)
            total += int(stats["total"])
            evidenced += int(stats["evidenced"])
            uncovered += int(stats["uncovered"])
            undetermined += int(stats["undetermined"])
        decisions_total = decisions_auto = decisions_expired = 0
        for task_id in task_ids:
            rows, _ = self._repos.decisions.list(task_id=task_id, status=None, limit=500)
            decisions_total += len(rows)
            decisions_auto += sum(1 for item in rows if item.auto_decided)
            decisions_expired += sum(1 for item in rows if item.status == "expired")
        reversed_count, auto_total = self._repos.decisions.overturned_rate()

        values = {
            "conclusion_completeness": round(evidenced / total, 4) if total else 0.0,
            "conclusion_total": float(total),
            "conclusion_uncovered": float(uncovered),
            "conclusion_undetermined": float(undetermined),
            "decision_deadlock_rate": (
                round((decisions_auto + decisions_expired) / decisions_total, 4)
                if decisions_total
                else 0.0
            ),
            "auto_decision_overturned_rate": (
                round(reversed_count / auto_total, 4) if auto_total else 0.0
            ),
        }
        today = day_key()
        for key, value in values.items():
            self._repos.metrics.upsert(
                day=today, workspace_id=workspace_id, metric_key=key, value=value
            )
        return values

    # --- evaluation --------------------------------------------------------

    def run_eval(self) -> dict[str, Any]:
        cases = self._repos.eval_cases.list_active()
        results: list[dict[str, Any]] = []
        executor = DeterministicExecutor()
        for case in cases:
            payload = loads(case.input_json, {}) or {}
            expect = loads(case.expect_json, {}) or {}
            ctx = StepContext(
                task_id=0,
                task_public_id="EVAL",
                requirement_id=0,
                requirement_title=str(payload.get("title") or ""),
                requirement_text=str(payload.get("raw_text") or ""),
                workspace_id=0,
                step_key=str(payload.get("step_key") or "intake"),
                seq=1,
                threshold=80,
                decision_level="L2",
                decision_timeout_s=300,
                siblings=[
                    {"id": index + 1, "public_id": f"S{index}", "title": str(title)}
                    for index, title in enumerate(payload.get("siblings") or [])
                ],
            )
            output = executor.run(ctx) if executor.supports(ctx.step_key) else None
            passed, reason = _check_expectation(output, expect)
            results.append(
                {
                    "name": case.name,
                    "kind": case.kind,
                    "passed": passed,
                    "reason": reason,
                }
            )
        failures = [item for item in results if not item["passed"]]
        return {
            "total": len(results),
            "passed": len(results) - len(failures),
            "failed": len(failures),
            "results": results,
            "ok": not failures,
        }

    # --- conclusions -------------------------------------------------------

    def recheck_conclusion(self, conclusion_id: int, *, actor: str | None = None) -> dict[str, Any]:
        conclusion = self._repos.conclusions.get(conclusion_id)
        if conclusion is None:
            raise errors.not_found("conclusion", conclusion_id)
        evidence_ids = self._repos.conclusions.evidence_ids(conclusion_id)
        stale = []
        now = now_iso()
        for evidence_id in evidence_ids:
            evidence = self._repos.evidence.get(evidence_id)
            if evidence is not None and evidence_rules.is_stale(evidence.stale_at, now):
                stale.append(evidence_id)
        if not evidence_ids:
            new_status = "uncovered"
        elif stale:
            new_status = "undetermined"
        else:
            new_status = "evidenced"
        if new_status != conclusion.status:
            self._repos.conclusions.set_status(conclusion_id, new_status)
        self._record(
            actor,
            "pm.conclusion.recheck",
            str(conclusion_id),
            {"status": new_status, "stale_evidence": stale},
        )
        return {
            "conclusion_id": conclusion_id,
            "status": new_status,
            "stale_evidence_ids": stale,
        }

    # --- scheduler hook ----------------------------------------------------

    def inbox(self, *, user_id: int | None = None) -> dict[str, Any]:
        """What is waiting on a human right now. Drives the sidebar badge."""
        stats = self._repos.decisions.inbox_stats(user_id=user_id)
        return {"enabled": is_enabled(), **stats}

    def sweep(self) -> dict[str, Any]:
        return self._pipeline.sweep_decisions()


# --- payload helpers ------------------------------------------------------


def _workspace_payload(item: Any) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "description": item.description,
        "owner_user_id": item.owner_user_id,
        "created_at": item.created_at,
        "archived_at": item.archived_at,
    }


def _requirement_payload(item: Any) -> dict[str, Any]:
    return {
        "id": item.id,
        "public_id": item.public_id,
        "workspace_id": item.workspace_id,
        "title": item.title,
        "raw_text": item.raw_text,
        "source": item.source,
        "source_ref": item.source_ref,
        "proposer": item.proposer,
        "priority": item.priority,
        "status": item.status,
        "baseline": loads(item.baseline_json, None),
        "created_at": item.created_at,
    }


def _task_payload(item: Any) -> dict[str, Any]:
    return {
        "id": item.id,
        "public_id": item.public_id,
        "requirement_id": item.requirement_id,
        "workspace_id": item.workspace_id,
        "stage": item.stage,
        "status": item.status,
        "owner_user_id": item.owner_user_id,
        "generation": item.generation,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _artifact_payload(item: Any, *, include_body: bool = False) -> dict[str, Any]:
    payload = {
        "id": item.id,
        "task_id": item.task_id,
        "kind": item.kind,
        "title": item.title,
        "audience": item.audience,
        "fidelity": item.fidelity,
        "fidelity_note": item.fidelity_note,
        "version": item.version,
        "origin": item.origin,
        "created_at": item.created_at,
    }
    if include_body:
        payload["body_inline"] = item.body_inline
        payload["body_path"] = item.body_path
    return payload


def _conclusion_payload(item: Conclusion) -> dict[str, Any]:
    return {
        "id": item.id,
        "artifact_id": item.artifact_id,
        "seq": item.seq,
        "text": item.text,
        "customer_text": item.customer_text,
        "status": item.status,
    }


def _evidence_payload(item: Any, *, include_locator: bool = False) -> dict[str, Any]:
    payload = {
        "id": item.id,
        "task_id": item.task_id,
        "source_type": item.source_type,
        "port": item.port,
        "excerpt": item.excerpt,
        "retrieved_at": item.retrieved_at,
        "stale_at": item.stale_at,
        "visibility": item.visibility,
        "confidence": item.confidence,
    }
    if include_locator:
        payload["locator"] = loads(item.locator_json, {})
        payload["content_hash"] = item.content_hash
    return payload


def _decision_payload(item: Decision | None) -> dict[str, Any]:
    if item is None:
        raise errors.not_found("decision", "unknown")
    return {
        "id": item.id,
        "task_id": item.task_id,
        "step_id": item.step_id,
        "level": item.level,
        "question": item.question,
        "options": loads(item.options_json, []),
        "recommended_key": item.recommended_key,
        "status": item.status,
        "selected_key": item.selected_key,
        "note": item.note,
        "decided_by": item.decided_by,
        "decided_at": item.decided_at,
        "timeout_at": item.timeout_at,
        "auto_decided": bool(item.auto_decided),
        "appeal_state": item.appeal_state,
        "assignee_user_id": item.assignee_user_id,
        "created_at": item.created_at,
    }


def _check_expectation(output: Any, expect: dict[str, Any]) -> tuple[bool, str]:
    if output is None:
        return False, "executor does not support this step"
    if expect.get("status") and output.status != expect["status"]:
        return False, f"status {output.status} != {expect['status']}"
    for needle in expect.get("gaps_contain") or []:
        if not any(needle in gap for gap in output.gaps):
            return False, f"missing expected gap: {needle}"
    for needle in expect.get("body_contain") or []:
        body = str((output.artifact or {}).get("body") or "")
        if needle not in body:
            return False, f"artifact body missing: {needle}"
    min_evidence = expect.get("min_evidence")
    if isinstance(min_evidence, int) and len(output.evidence) < min_evidence:
        return False, f"evidence {len(output.evidence)} < {min_evidence}"
    return True, "ok"
