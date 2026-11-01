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


def _matching_defaults(wanted: tuple[str, ...], actual: list[str]) -> list[str]:
    """Which of our default names this site has — exactly or as a substring.

    Workflow names carry suffixes ("待设计(评审中)"), so equality alone would
    report a perfectly good site as broken and send the operator hunting for a
    field that is already correct.
    """

    lowered = [name.lower() for name in actual]
    hits: list[str] = []
    for want in wanted:
        needle = want.lower()
        if any(needle == name or needle in name or name in needle for name in lowered):
            hits.append(want)
    return hits


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

    def _jira_credential_key(self, workspace_id: int) -> str:
        return f"pm.jira.credential.{workspace_id}"

    def _jira_credential(self, workspace_id: int) -> tuple[str, str]:
        """``(username, token)`` as stored, both in the secret store.

        Two kinds of instance exist in the wild: ones that issue a PAT (sent as
        a bearer token, no username needed) and ones that only hand out an
        account password (sent as Basic, username required). Which one applies
        is a property of the instance, so the account name is kept next to the
        password instead of in the integration mapping — one secret, one shape.

        Falls back to the legacy token-only key, so an instance configured
        before this change keeps working without re-entering anything.
        """

        raw = self._jira_secrets().get(self._jira_credential_key(workspace_id))
        if raw:
            try:
                data = loads(raw.decode("utf-8"), None)
            except UnicodeDecodeError:
                data = None
            if isinstance(data, dict):
                return (
                    str(data.get("username") or "").strip(),
                    str(data.get("token") or "").strip(),
                )
        legacy = self._jira_secrets().get(self._jira_token_key(workspace_id))
        return "", legacy.decode("utf-8").strip() if legacy else ""

    def _jira_token(self, workspace_id: int) -> str:
        return self._jira_credential(workspace_id)[1]

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

    def _write_integration_mapping(self, workspace_id: int, mapping: dict[str, Any]) -> None:
        """Persist the jira integration's mapping, leaving the other columns alone.

        ``credential_ref`` and ``mode`` belong to the REST transport and are
        carried over unchanged; a workspace bound to a connector simply has no
        credential of its own.
        """

        item = self._repos.integrations.get_by_vendor(workspace_id, "jira")
        self._repos.integrations.upsert(
            workspace_id=workspace_id,
            vendor="jira",
            enabled=bool(item.enabled) if item is not None else True,
            mode=str(getattr(item, "mode", "real") or "real") if item is not None else "real",
            credential_ref=getattr(item, "credential_ref", None) if item is not None else None,
            mapping=mapping,
        )

    def jira_status(self, workspace_id: int) -> dict[str, Any]:
        """What the picker needs to know before it tries to list anything.

        ``configured`` means different things per transport, and conflating them
        is how a workspace ends up showing a picker that cannot list anything:
        REST needs a site URL plus token, MCP needs a live connector with a Jira
        server in it. No credential is ever read for the MCP branch — only the
        connector's identity is checked.
        """

        mapping = self._jira_mapping(workspace_id)
        item = self._repos.integrations.get_by_vendor(workspace_id, "jira")
        transport = str(mapping.get("transport") or "rest").strip().lower()
        username, token = self._jira_credential(workspace_id)

        if transport == "mcp":
            instance_id = str(mapping.get("connector_instance_id") or "")
            server_key = str(mapping.get("connector_server") or "")
            spec = self._mcp_server_spec(instance_id, server_key)
            return {
                "configured": spec is not None,
                "transport": "mcp",
                "base_url": str((spec or {}).get("base_url") or mapping.get("base_url") or ""),
                "jql": str(mapping.get("jql") or ""),
                "verify_tls": True,
                "token_set": spec is not None,
                "auth": "mcp",
                "connector_instance_id": instance_id,
                "connector_server": str((spec or {}).get("server_key") or server_key),
                "connector_display": str((spec or {}).get("display_name") or ""),
                "enabled": bool(item.enabled) if item is not None else False,
                "last_probe_at": getattr(item, "last_probe_at", None) if item is not None else None,
                "probe_status": getattr(item, "probe_status", None) if item is not None else None,
            }

        return {
            "configured": bool(mapping.get("base_url") and token),
            "transport": "rest",
            "base_url": str(mapping.get("base_url") or ""),
            "jql": str(mapping.get("jql") or ""),
            "verify_tls": bool(mapping.get("verify_tls", True)),
            "token_set": bool(token),
            "auth": "basic" if username else "bearer",
            "enabled": bool(item.enabled) if item is not None else False,
            "last_probe_at": getattr(item, "last_probe_at", None) if item is not None else None,
            "probe_status": getattr(item, "probe_status", None) if item is not None else None,
        }

    def jira_meta(self, workspace_id: int) -> dict[str, Any]:
        """What the intake filter can legally say on this site.

        The queue opens onto "待设计 / P0,P1" — two strings this product guessed
        about a Jira site it does not own. When the guess is wrong the operator
        gets a 400 and no way forward, so this reads back the real status and
        priority names and reports which defaults actually exist here.
        """
        from octop.infra.pm.ports.base import PortError  # noqa: PLC0415
        from octop.infra.pm.ports.jira import (  # noqa: PLC0415
            INTAKE_PRIORITIES,
            INTAKE_STATUSES,
            intake_jql,
        )

        mapping = self._jira_mapping(workspace_id)
        result: dict[str, Any] = {
            "user": "",
            "statuses": [],
            "priorities": [],
            "jql": str(mapping.get("jql") or "").strip() or intake_jql(),
            "status_hits": [],
            "priority_hits": [],
            "ok": False,
            "error": "",
            "note": "",
        }
        try:
            with self.jira_client(workspace_id) as client:
                me = client.myself()
                result["user"] = str(me.get("displayName") or me.get("name") or "")
                # A client may return a tuple; the response schema is a list.
                result["statuses"] = list(client.statuses())
                result["priorities"] = list(client.priorities())
        except PortError as exc:
            result["error"] = str(exc)
            return result

        result["status_hits"] = _matching_defaults(INTAKE_STATUSES, result["statuses"])
        result["priority_hits"] = _matching_defaults(INTAKE_PRIORITIES, result["priorities"])
        if not result["statuses"] and not result["priorities"]:
            # The connector transport exposes no global status/priority catalogue.
            # Reporting "ok: false" there would be a false alarm — it reads as
            # "this site never heard of 待设计" and sends the operator to edit a
            # JQL that was fine. An unmatched JQL simply returns no rows.
            result["ok"] = True
            result["note"] = (
                "该工作区通过连接器（MCP）接入 Jira，服务端不提供全局状态/优先级清单；"
                "默认队列直接按 JQL 查询，状态名对不上时返回空列表而不是报错。"
            )
            return result
        result["ok"] = bool(result["status_hits"]) and bool(result["priority_hits"])
        return result

    def save_jira_config(
        self,
        workspace_id: int,
        *,
        base_url: str,
        token: str | None = None,
        username: str | None = None,
        jql: str | None = None,
        verify_tls: bool = True,
    ) -> dict[str, Any]:
        """Persist the endpoint in the integration row and the credential in the secret store.

        Splitting them is the whole point: a database dump stays harmless. The
        account name travels with the password rather than in the mapping,
        because the two are one credential — an instance either wants both or
        neither, and a username without its password is not config, it is noise.
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

        stored_user, stored_token = self._jira_credential(workspace_id)
        new_user = stored_user if username is None else username.strip()
        new_token = stored_token if token is None or not token.strip() else token.strip()

        key = self._jira_credential_key(workspace_id)
        secrets = self._jira_secrets()
        if new_user or new_token:
            value = dumps({"username": new_user, "token": new_token}).encode("utf-8")

            def _seed(payload: bytes = value) -> bytes:
                return payload

            if secrets.get(key) is None:
                secrets.get_or_create(key, _seed)
                # get_or_create races are fine; make sure the value we were handed
                # actually landed rather than a stale one.
                if secrets.get(key) != value:
                    secrets.rotate(key, value)
            elif secrets.get(key) != value:
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

    def _project_overrides(self, workspace_id: int) -> dict[str, str]:
        """Saved ``project key -> repo`` map, as edited in the UI."""

        raw = self._jira_mapping(workspace_id).get("projects")
        if not isinstance(raw, dict):
            return {}
        return {str(k).upper(): str(v) for k, v in raw.items() if str(v).strip()}

    def jira_projects(self, workspace_id: int) -> dict[str, Any]:
        """Jira's real project list, annotated with what each one routes to.

        This is the screen where the operator decides which source repository a
        project's requirements belong to. It exists because guessing the mapping
        is not acceptable: reading the wrong product's source produces a
        prototype that is structurally faithful and entirely wrong.
        """

        from octop.infra.pm.domain.repos import (  # noqa: PLC0415
            REPOS,
            SYSTEMS,
            resolve_project,
        )
        from octop.infra.pm.ports.base import PortError  # noqa: PLC0415

        overrides = self._project_overrides(workspace_id)
        remote: list[dict[str, Any]] = []
        error = ""
        try:
            with self.jira_client(workspace_id) as client:
                remote = client.projects()
        except PortError as exc:
            error = str(exc)

        items: list[dict[str, Any]] = []
        for project in remote:
            key = project["key"]
            binding = resolve_project(key, overrides)
            web = binding.web if binding is not None else None
            items.append(
                {
                    **project,
                    "mapped": binding is not None,
                    "system": binding.system if binding is not None else "",
                    "repos": list(binding.repos) if binding is not None else [],
                    "origin": binding.origin if binding is not None else "",
                    "web_repo": web.key if web is not None else "",
                    "mirror_ready": bool(web is not None and web.root().is_dir()),
                }
            )

        # A saved mapping for a project this token cannot see is still worth
        # showing: it usually means a permission problem, not a wrong mapping.
        seen = {item["key"] for item in items}
        for key in sorted(overrides):
            if key in seen:
                continue
            binding = resolve_project(key, overrides)
            items.append(
                {
                    "key": key,
                    "name": "",
                    "type": "",
                    "archived": False,
                    "mapped": binding is not None,
                    "system": binding.system if binding is not None else "",
                    "repos": list(binding.repos) if binding is not None else [],
                    "origin": binding.origin if binding is not None else "",
                    "web_repo": binding.web.key if binding is not None and binding.web else "",
                    "mirror_ready": bool(
                        binding is not None and binding.web is not None and binding.web.root().is_dir()
                    ),
                    "invisible_to_token": True,
                }
            )

        return {
            "items": items,
            "error": error,
            "systems": list(SYSTEMS),
            "repo_keys": sorted(REPOS),
            "overrides": overrides,
            "unmapped": [item["key"] for item in items if not item["mapped"]],
        }

    def save_project_mapping(
        self,
        workspace_id: int,
        *,
        project: str,
        target: str,
    ) -> dict[str, Any]:
        """Persist one ``project -> repo`` mapping.

        ``target`` accepts a system name (``cargowarex``), a repo key
        (``cwx-web``), or several repo keys (``cwx-web,cwx-api``). An empty
        ``target`` removes the override, falling back to the seed table.
        """

        from octop.infra.pm.domain.repos import resolve_project  # noqa: PLC0415

        if self._repos.workspaces.get(workspace_id) is None:
            raise errors.not_found("workspace", workspace_id)
        key = (project or "").strip().upper()
        if not key:
            raise errors.invalid("project is required")

        mapping = self._jira_mapping(workspace_id)
        projects = mapping.get("projects")
        if not isinstance(projects, dict):
            projects = {}
        cleaned = (target or "").strip()
        if not cleaned:
            projects.pop(key, None)
        else:
            if resolve_project(key, {key: cleaned}) is None:
                raise errors.invalid(
                    f"无法识别的目标仓库 {cleaned}：填系统名（cargowarex/cargoware）或仓库键（cwx-web 等）"
                )
            projects[key] = cleaned
        mapping["projects"] = projects

        item = self._repos.integrations.get_by_vendor(workspace_id, "jira")
        self._repos.integrations.upsert(
            workspace_id=workspace_id,
            vendor="jira",
            enabled=bool(item.enabled) if item is not None else True,
            mode=str(getattr(item, "mode", "real") or "real") if item is not None else "real",
            credential_ref=getattr(item, "credential_ref", None) if item is not None else None,
            mapping=mapping,
        )
        binding = resolve_project(key, self._project_overrides(workspace_id))
        return {
            "project": key,
            "target": cleaned,
            "mapped": binding is not None,
            "system": binding.system if binding is not None else "",
            "repos": list(binding.repos) if binding is not None else [],
            "origin": binding.origin if binding is not None else "",
        }

    def source_repo_for_requirement(self, workspace_id: int, requirement: Any) -> dict[str, Any]:
        """Which repository a stored requirement's source must be read from.

        Resolution order: the project key recorded at intake, then the issue key
        in ``source_ref``. Both are Jira facts. There is no keyword fallback —
        when nothing matches, the answer is a reason, not a guess.
        """

        from octop.infra.pm.domain.repos import (  # noqa: PLC0415
            project_of_requirement,
            resolve_project,
        )

        baseline = getattr(requirement, "baseline_json", None)
        if isinstance(baseline, str):
            baseline = loads(baseline, {}) or {}
        baseline = baseline if isinstance(baseline, dict) else {}

        project = project_of_requirement(
            baseline,
            str(getattr(requirement, "source_ref", "") or ""),
            source=str(getattr(requirement, "source", "") or ""),
        )
        if not project:
            return {
                "project": "",
                "mapped": False,
                "reason": "需求未关联 Jira 项目：source_ref 为空，无法定位源码仓库",
            }

        binding = resolve_project(project, self._project_overrides(workspace_id))
        if binding is None:
            known = "、".join(sorted(self._project_overrides(workspace_id))) or "（尚无已保存映射）"
            return {
                "project": project,
                "mapped": False,
                "reason": f"Jira 项目 {project} 未映射到任何源码仓库；请先在项目映射里指定。已映射：{known}",
            }

        web = binding.web
        api = binding.api
        return {
            "project": project,
            "mapped": True,
            "system": binding.system,
            "repos": list(binding.repos),
            "origin": binding.origin,
            "web_repo": web.key if web is not None else "",
            "mirror": web.mirror if web is not None else "",
            "mirror_ready": bool(web is not None and web.root().is_dir()),
            "framework": web.framework if web is not None else "",
            # The api mirror is what the PRD's 现状 section and the code probe
            # read. Reported here too, so "why did the PRD say nothing about
            # existing endpoints" has an answer that is not a guess.
            "api_repo": api.key if api is not None else "",
            "api_mirror": api.mirror if api is not None else "",
            "api_mirror_ready": bool(api is not None and api.root().is_dir()),
            "reason": binding.describe(),
        }

    def requirement_source_repo(self, requirement_id: int) -> dict[str, Any]:
        """Expose the resolution for one requirement, so it is auditable."""

        requirement = self._repos.requirements.get(requirement_id)
        if requirement is None:
            raise errors.not_found("requirement", requirement_id)
        result = self.source_repo_for_requirement(requirement.workspace_id, requirement)
        result["requirement_id"] = requirement_id
        return result

    # --- jira transport: MCP connector vs stored REST credential ----------

    def _mcp_server_spec(self, instance_id: str, server_key: str = "") -> dict[str, Any] | None:
        """Read one connector's MCP server spec (command/args/env).

        The credential stays where the operator put it — the connector's
        encrypted blob — and is decrypted into memory only to be handed to the
        child process. Nothing is copied into PM's own secret store, so rotating
        it in the connector rotates it here too, with no second edit.

        Returns ``None`` when the connector is gone, disabled, or has no Jira
        server, so the caller can say which of those it was.
        """

        from octop.infra.connectors.crypto import decrypt_credentials  # noqa: PLC0415
        from octop.infra.db.repos.connectors import ConnectorRepo  # noqa: PLC0415

        if not instance_id:
            return None
        row = ConnectorRepo(self._db).get(instance_id)
        if row is None or not row.credential_blob:
            return None
        if str(getattr(row, "status", "") or "") != "active":
            return None

        creds = decrypt_credentials(self._jira_secrets(), row.credential_blob)
        servers = creds.get("servers") if isinstance(creds, dict) else None
        if not isinstance(servers, dict) or not servers:
            return None

        chosen: tuple[str, dict[str, Any]] | None = None
        if server_key and isinstance(servers.get(server_key), dict):
            chosen = (server_key, servers[server_key])
        else:
            # No explicit choice: take the first server that calls itself Jira.
            # Anything else (mail, other MCPs sharing the connector) has a
            # different tool namespace and would answer "unknown tool".
            for name, item in servers.items():
                if not isinstance(item, dict):
                    continue
                hint = f"{name} {item.get('display_name') or ''}".lower()
                if "jira" in hint:
                    chosen = (name, item)
                    break
        if chosen is None:
            return None

        name, item = chosen
        command = str(item.get("command") or "").strip()
        if not command:
            return None
        env = item.get("env") if isinstance(item.get("env"), dict) else {}
        args = item.get("args") if isinstance(item.get("args"), list) else []
        return {
            "connector_instance_id": instance_id,
            "server_key": name,
            "command": command,
            "args": [str(a) for a in args],
            "env": {str(k): str(v) for k, v in env.items()},
            "display_name": str(item.get("display_name") or name),
            "username": str(env.get("JIRA_USERNAME") or ""),
            "base_url": str(env.get("JIRA_URL") or "").rstrip("/"),
        }

    def _mcp_jira_client(self, mapping: dict[str, Any]) -> Any:
        from octop.infra.pm.ports.base import PortUnavailable  # noqa: PLC0415
        from octop.infra.pm.ports.mcp_jira import McpJiraClient, session_for  # noqa: PLC0415

        instance_id = str(mapping.get("connector_instance_id") or "")
        server_key = str(mapping.get("connector_server") or "")
        spec = self._mcp_server_spec(instance_id, server_key)
        if spec is None:
            raise PortUnavailable(
                "Jira 走连接器接入，但找不到可用的连接器："
                f"实例 {instance_id or '（未指定）'} 不存在、已停用，或其配置里没有 Jira 服务。"
            )
        session = session_for(
            f"{instance_id}:{spec['server_key']}",
            spec["command"],
            spec["args"],
            spec["env"],
        )
        return McpJiraClient(
            session,
            base_url=spec["base_url"],
            username=spec["username"],
        )

    def list_jira_connectors(self) -> list[dict[str, Any]]:
        """Connectors that actually carry a Jira server.

        Only those are bindable. Offering every connector would let an operator
        pick their mail connector and get "unknown tool" on the first query,
        with nothing in the message pointing back at the choice.
        """

        from octop.infra.db.repos.connectors import ConnectorRepo  # noqa: PLC0415

        out: list[dict[str, Any]] = []
        for row in ConnectorRepo(self._db).list_by_kind("custom-mcp"):
            spec = self._mcp_server_spec(row.instance_id)
            if spec is None:
                continue
            out.append(
                {
                    "instance_id": row.instance_id,
                    "display_name": str(row.display_name or ""),
                    "status": str(getattr(row, "status", "") or ""),
                    "server_key": spec["server_key"],
                    "server_display": spec["display_name"],
                    "base_url": spec["base_url"],
                    "username": spec["username"],
                }
            )
        return out

    def jira_client(self, workspace_id: int) -> Any:
        """Build a client from stored config. Raises PortUnavailable when unset.

        Two transports. ``transport=mcp`` drives the Jira server the operator
        already configured as a connector, so the credential is entered once and
        rotations propagate. An empty or ``rest`` transport keeps the old
        site+token path untouched, so an existing install is unaffected.
        """

        from octop.infra.pm.ports.base import PortUnavailable  # noqa: PLC0415
        from octop.infra.pm.ports.jira import JiraClient  # noqa: PLC0415

        mapping = self._jira_mapping(workspace_id)
        if str(mapping.get("transport") or "").strip().lower() == "mcp":
            return self._mcp_jira_client(mapping)

        base_url = str(mapping.get("base_url") or "")
        username, token = self._jira_credential(workspace_id)
        if not base_url:
            raise PortUnavailable("Jira 未配置：请先在设置里填写站点地址与 token")
        if not token:
            raise PortUnavailable("Jira token 未设置")
        return JiraClient(
            base_url,
            token,
            username=username,
            verify_tls=bool(mapping.get("verify_tls", True)),
        )

    def bind_jira_connector(
        self,
        workspace_id: int,
        *,
        connector_instance_id: str,
        server_key: str = "",
        jql: str | None = None,
    ) -> dict[str, Any]:
        """Point this workspace's Jira at an existing MCP connector.

        Stores only *which* connector and *which* server inside it. The site URL
        and token keep living in the connector, so this row is not a credential
        and a database dump stays harmless.
        """

        if self._repos.workspaces.get(workspace_id) is None:
            raise errors.not_found("workspace", workspace_id)
        spec = self._mcp_server_spec(connector_instance_id, server_key)
        if spec is None:
            # PortUnavailable is a RuntimeError, not an OctopError, so raising it
            # from a route would surface as a 500 with no explanation. This is a
            # rejected precondition, which is a 400 with a message.
            raise errors.invalid(
                f"连接器 {connector_instance_id} 不可用：不存在、已停用，或其配置里没有 Jira 服务"
            )

        mapping = dict(self._jira_mapping(workspace_id))
        # A stored rest credential would shadow the connector if the transport
        # flag were ever lost, so the site URL is kept in step with the
        # connector rather than left stale.
        mapping.update(
            {
                "transport": "mcp",
                "connector_instance_id": connector_instance_id,
                "connector_server": spec["server_key"],
                "base_url": spec["base_url"],
            }
        )
        if jql is not None:
            mapping["jql"] = jql
        self._write_integration_mapping(workspace_id, mapping)
        return self.jira_status(workspace_id)

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
        """Candidates for the intake picker: one fuzzy box, not two.

        Empty ``q`` means "the design queue" — mine, waiting to be designed,
        P0/P1. That is what the operator came here to work through, so it loads
        on open instead of hiding behind a "browse" toggle.

        A key, a fragment of a title and a blank box all go through this one
        method, because they are one question ("which ticket?") asked at
        different levels of precision.
        """
        from octop.infra.pm.ports.base import PortError  # noqa: PLC0415
        from octop.infra.pm.ports.jira import DEFAULT_JQL, intake_jql  # noqa: PLC0415

        mapping = self._jira_mapping(workspace_id)
        configured = str(mapping.get("jql") or "").strip()
        pool = configured or intake_jql()
        fallback = configured or DEFAULT_JQL
        needle = (q or "").strip()

        if not needle:
            jql = pool
        elif _looks_like_issue_key(needle):
            # A mistyped key has to read as "nothing matched", not as a 404 in
            # red. This is a search field; an exact `key =` lookup is the one
            # shape a search field can never report gracefully, so the key is
            # OR-ed with the text search.
            escaped = needle.replace('"', '\\"')
            jql = f'key = "{needle.upper()}" OR text ~ "{escaped}" ORDER BY updated DESC'
        else:
            # Quoted so a title containing JQL operators cannot rewrite the query.
            escaped = needle.replace('"', '\\"')
            jql = f'text ~ "{escaped}" ORDER BY updated DESC'

        note = ""
        try:
            with self.jira_client(workspace_id) as client:
                try:
                    issues = client.search(jql, limit=limit)
                except PortError as exc:
                    # The pool filter is a stored guess about what this site
                    # calls its statuses and priorities. When the guess is
                    # wrong, Jira answers 400 and the dialog would open onto an
                    # error nobody can fix from there — so fall back to "my open
                    # issues" and say what happened. A search the operator typed
                    # is never rewritten: masking their query with a fallback
                    # would report success for something that did not run.
                    if needle or jql == fallback:
                        raise
                    issues = client.search(fallback, limit=limit)
                    jql = fallback
                    note = (
                        "默认筛选（状态/优先级）在本站不成立，已退回“我的未完成工单”。"
                        f"原样报错：{exc}"
                    )
        except PortError as exc:
            raise errors.invalid(str(exc)) from exc
        return {"jql": jql, "items": issues, "total": len(issues), "note": note}

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
        from octop.infra.pm.domain.repos import project_key_of  # noqa: PLC0415
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
                # Intake is the only moment the ticket is in hand, and locating
                # the screen later needs fields the picker never shows —
                # components, attachments, comments, and whichever custom field
                # this instance happens to call "痛点". Fetch them once here.
                issue = client.issue(key, with_context=True)
        except PortError as exc:
            raise errors.invalid(str(exc)) from exc

        title = (issue.get("title") or key).strip()

        # Everything the ticket states becomes the requirement text — description,
        # the custom fields the team fills in, environment, components, and the
        # comments. Keeping only the description was the earlier behaviour and it
        # was wrong for this product: the refinement usually lives in the
        # comments, and the model writing the analysis never saw it.
        #
        # The issue key still stays out: every export runs a blacklist that
        # blocks ticket ids and the intake artifact embeds this text verbatim, so
        # a key in here makes the requirement permanently un-exportable. Comments
        # may mention *other* tickets, which is reported rather than rewritten —
        # silently editing what a customer wrote is worse than a blocked export.
        from octop.infra.pm.domain.jira_intake import compose  # noqa: PLC0415
        from octop.infra.pm.domain.locate_evidence import LocateEvidence  # noqa: PLC0415

        composed = compose(issue)
        raw_text = composed.text or f"（Jira 工单未填写任何内容；以下事实仅来自标题）{title}"

        evidence = LocateEvidence.from_issue(issue)

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
                # The project key is what routes every downstream step to a
                # source repository, so it is recorded as a first-class fact
                # rather than re-derived from the issue key later.
                "jira_project": str(issue.get("project") or project_key_of(key)),
                "jira_status": issue.get("status"),
                "jira_issue_type": issue.get("issue_type"),
                "jira_priority": issue.get("priority"),
                "jira_assignee": issue.get("assignee"),
                "jira_updated": issue.get("updated"),
                "jira_components": list(issue.get("components") or []),
                "jira_labels": list(issue.get("labels") or []),
                "jira_custom_fields": [
                    {"name": item.get("name"), "text": str(item.get("text") or "")[:400]}
                    for item in (issue.get("custom_fields") or [])
                ],
                # The full ticket now lives in raw_text, so these record how it
                # was assembled rather than repeating the content: which sections
                # made it, what had to be cut, and what a deliverable may not
                # carry. Without them a truncated text looks like a short ticket.
                "jira_comment_count": len(issue.get("comments") or []),
                "jira_attachments": [
                    {
                        "filename": item.get("filename"),
                        "mime": item.get("mime"),
                        "size": item.get("size"),
                    }
                    for item in (issue.get("attachments") or [])
                ],
                "jira_text_sections": list(composed.sections),
                "jira_text_truncated": list(composed.truncated),
                "jira_export_blockers": list(composed.blockers),
                "locate_evidence": evidence.as_dict(),
            },
            client_token=client_token,
            actor=actor,
        )

    def preview_jira_issue(self, workspace_id: int, *, issue_key: str) -> dict[str, Any]:
        """Read one issue in full, without creating anything.

        The intake dialog needs to show what will be stored before it is stored —
        including the parts a summary would hide: the comments, the custom fields,
        and whether this ticket was already imported. It runs the same
        ``compose`` the write path runs, so the preview cannot drift from the
        result.
        """

        from octop.infra.pm.domain.jira_intake import compose  # noqa: PLC0415
        from octop.infra.pm.ports.base import PortError  # noqa: PLC0415

        key = (issue_key or "").strip().upper()
        if not key:
            raise errors.invalid("issue_key is required")
        if not _looks_like_issue_key(key):
            raise errors.invalid(f"「{issue_key}」不像是 Jira 工单号，应形如 CCTOWER-3597")

        try:
            with self.jira_client(workspace_id) as client:
                issue = client.issue(key, with_context=True)
        except PortError as exc:
            raise errors.invalid(str(exc)) from exc

        composed = compose(issue)
        existing = self._repos.requirements.get_by_source_ref(workspace_id, "jira", key)
        return {
            "key": key,
            "issue": issue,
            "requirement_text": composed.text,
            "sections": list(composed.sections),
            "truncated": list(composed.truncated),
            "blockers": list(composed.blockers),
            # Present means the OK button should open the existing requirement
            # instead of creating a second one, which the API would return anyway.
            "existing": (
                None
                if existing is None
                else {
                    "id": existing.id,
                    "public_id": existing.public_id,
                    "title": existing.title,
                    "status": existing.status,
                }
            ),
        }

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
