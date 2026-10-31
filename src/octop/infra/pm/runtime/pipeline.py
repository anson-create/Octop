"""Pipeline: one tick advances at most one step.

Design rules encoded here:

* an optimistic lock (``pm_task.locked_until``) makes a tick idempotent under
  retry;
* gate failures only touch the step; the task status is derived afterwards;
* a step that needs a human produces a durable ``pm_decision`` before the task is
  parked, so a restart cannot lose the card;
* evidence that went stale downgrades the affected conclusions before the next
  step reads them.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

from octop.infra.pm import errors
from octop.infra.pm.domain import decisions as decision_rules
from octop.infra.pm.domain import evidence as evidence_rules
from octop.infra.pm.domain import gates, stages
from octop.infra.pm.domain.state_machine import stage_from_steps, task_status_from_steps
from octop.infra.pm.models import Task, TaskStep
from octop.infra.pm.repos import PmRepos
from octop.infra.pm.repos._base import dumps, loads, now_iso
from octop.infra.pm.runtime.executor import (
    DeterministicExecutor,
    ExecutorUnavailable,
    RegistryExecutor,
    StepContext,
    StepExecutor,
    StepOutput,
)

MAX_ATTEMPTS = 2


def default_executor() -> RegistryExecutor:
    return RegistryExecutor(DeterministicExecutor())


class Pipeline:
    def __init__(
        self,
        repos: PmRepos,
        executor: StepExecutor | None = None,
        *,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self._repos = repos
        self._executor: StepExecutor = executor or default_executor()
        self._max_attempts = max_attempts

    # --- public entry points -------------------------------------------------

    def set_executor(self, executor: StepExecutor) -> None:
        self._executor = executor

    def tick(self, task_id: int, *, human_note: str | None = None) -> dict[str, Any]:
        task = self._repos.tasks.get(task_id)
        if task is None:
            raise errors.not_found("task", task_id)
        if task.status == "waiting_human":
            raise errors.conflict(
                "task is waiting for a human decision", task_id=task_id, status=task.status
            )
        if task.status in ("delivered", "archived"):
            raise errors.conflict(f"task is {task.status}", task_id=task_id, status=task.status)
        if not self._repos.tasks.claim(task_id):
            raise errors.conflict("task is locked by another tick", task_id=task_id)

        started = time.monotonic()
        try:
            self._downgrade_stale(task_id)
            return self._run_next_step(task, human_note=human_note, started=started)
        finally:
            self._repos.tasks.release(task_id)

    def sweep_decisions(self) -> dict[str, Any]:
        """Answer or expire due cards. Called by the scheduler or on demand."""
        due = self._repos.decisions.pending_due()
        auto = expired = 0
        for decision in due:
            if not decision_rules.expires(decision.level):
                continue
            options = loads(decision.options_json, [])
            if decision_rules.should_auto_answer(decision.level):
                key = decision_rules.pick_auto_choice(options, decision.recommended_key)
                if self._repos.decisions.auto_answer(
                    decision.id, selected_key=key, note=decision_rules.auto_note(decision.level, key)
                ):
                    auto += 1
                    self.after_decision(decision.id)
            elif self._repos.decisions.expire(decision.id):
                expired += 1
                # An expired L2 card must surface as a blocked task, not as a task
                # that silently waits forever on a card nobody can answer.
                if decision.step_id is not None:
                    step = self._repos.steps.get(decision.step_id)
                    self._repos.steps.finish(
                        decision.step_id, "failed", error_code="decision_expired"
                    )
                    task = self._repos.tasks.get(decision.task_id)
                    if task is not None:
                        self._repos.tasks.set_status(
                            task.id, "blocked", stage=step.stage if step else None
                        )
        return {"due": len(due), "auto_answered": auto, "expired": expired}

    def after_decision(self, decision_id: int) -> dict[str, Any]:
        """Continue the step whose card was just answered (or auto-answered)."""
        decision = self._repos.decisions.get(decision_id)
        if decision is None:
            raise errors.not_found("decision", decision_id)
        task = self._repos.tasks.get(decision.task_id)
        if task is None:
            raise errors.not_found("task", decision.task_id)
        if decision.status not in ("answered", "auto_answered"):
            return {"advanced": False, "reason": f"decision is {decision.status}"}
        if decision.step_id is None:
            self._sync_task(task)
            return {"advanced": True, "step": None}

        step = self._repos.steps.get(decision.step_id)
        if step is None:
            return {"advanced": False, "reason": "step missing"}

        report = gates.evaluate_step(
            step.step_key,
            self._gate_context(
                task=task, step=step, decision=self._decision_view(decision)
            ),
        )
        self._repos.steps.finish(
            step.id, "passed" if report.passed else "failed", gate_result=report.to_dict()
        )
        self._sync_task(task)
        current = self._repos.tasks.get(task.id)
        return {
            "advanced": report.passed,
            "step": step.step_key,
            "gate": report.to_dict(),
            "task_status": current.status if current else None,
        }

    def reject_step(self, step_id: int, *, note: str, user_id: int | None = None) -> dict[str, Any]:
        step = self._repos.steps.get(step_id)
        if step is None:
            raise errors.not_found("step", step_id)
        cleaned = (note or "").strip()
        if len(cleaned) < decision_rules.NOTE_MIN_LEN:
            raise errors.invalid(
                f"rejection reason must be at least {decision_rules.NOTE_MIN_LEN} characters",
                length=len(cleaned),
            )
        task = self._repos.tasks.get(step.task_id)
        if task is None:
            raise errors.not_found("task", step.task_id)
        self._repos.tasks.set_pipeline(task.id, generation=task.generation + 1)
        self._repos.decisions.void_for_task(task.id)
        self._repos.steps.reset_from(task.id, step.seq)
        self._repos.tasks.set_status(task.id, "queued")
        # The human's own words are replayed into the next run of this step.
        self._repos.steps.set_trace(step.id, {"human_note": cleaned, "rejected_by": user_id})
        return {"task_id": task.id, "from_seq": step.seq, "generation": task.generation + 1}

    # --- internals -----------------------------------------------------------

    def _run_next_step(
        self, task: Task, *, human_note: str | None, started: float
    ) -> dict[str, Any]:
        self._repos.steps.ensure_steps(task.id)
        open_step = self._repos.steps.first_open_step(task.id)
        if open_step is None:
            self._repos.tasks.set_status(task.id, "delivered", stage="P5")
            self._repos.tasks.release(task.id)
            return {"task_id": task.id, "status": "delivered", "step": None}

        if open_step.status == "waiting_card":
            return {
                "task_id": task.id,
                "status": "waiting_human",
                "step": open_step.step_key,
                "reason": "step is waiting for a decision",
            }

        if open_step.attempt >= self._max_attempts:
            card = self._escalate(task, open_step)
            return {
                "task_id": task.id,
                "status": "blocked",
                "step": open_step.step_key,
                "reason": "attempts exhausted",
                "decision_id": card.id if card else None,
            }

        handler = self._repos.handlers.get(open_step.seq)
        self._repos.steps.start(open_step.id)
        previous_trace = loads(open_step.trace_json, {}) or {}
        ctx = StepContext(
            task_id=task.id,
            task_public_id=task.public_id,
            requirement_id=task.requirement_id,
            requirement_title="",
            requirement_text="",
            workspace_id=task.workspace_id,
            step_key=open_step.step_key,
            seq=open_step.seq,
            threshold=handler.threshold if handler else 80,
            decision_level=handler.decision_level if handler else "L2",
            decision_timeout_s=handler.decision_timeout_s if handler else 300,
            human_note=human_note,
            evidence=[
                {
                    "source_type": item.source_type,
                    "port": item.port,
                    "locator": loads(item.locator_json, {}),
                    "excerpt": item.excerpt,
                    "visibility": item.visibility,
                }
                for item in self._repos.evidence.list_for_task(task.id)
            ],
            gaps=list(previous_trace.get("gaps") or []),
            siblings=self._sibling_requirements(task),
        )
        requirement = self._repos.requirements.get(task.requirement_id)
        if requirement is not None:
            ctx.requirement_title = requirement.title
            ctx.requirement_text = requirement.raw_text
            ctx.source_repo = self._source_repo(requirement)
            ctx.locate_evidence = self._locate_evidence(requirement)

        try:
            output = self._executor.run(ctx)
        except ExecutorUnavailable as exc:
            self._repos.steps.finish(
                open_step.id, "failed", error_code="executor_unavailable", wall_clock_ms=_elapsed(started)
            )
            self._repos.tasks.set_status(task.id, "blocked", stage=open_step.stage)
            return {
                "task_id": task.id,
                "status": "blocked",
                "step": open_step.step_key,
                "reason": str(exc),
                "error_code": "executor_unavailable",
            }

        if output.status == "needs_decision" and output.decision:
            card = self._open_card(task, open_step, output)
            self._repos.steps.finish(open_step.id, "waiting_card", wall_clock_ms=_elapsed(started))
            self._repos.tasks.set_status(task.id, "waiting_human", stage=open_step.stage)
            return {
                "task_id": task.id,
                "status": "waiting_human",
                "step": open_step.step_key,
                "decision_id": card.id,
            }

        evidence_ids = self._persist_evidence(task.id, output)
        artifact = self._persist_artifact(task, open_step, output, evidence_ids)
        self._repos.steps.set_trace(
            open_step.id,
            {**output.trace, "gaps": output.gaps, "evidence_ids": evidence_ids},
        )
        report = gates.evaluate_step(
            open_step.step_key,
            self._gate_context(task=task, step=open_step, output=output, artifact=artifact),
        )
        self._repos.steps.finish(
            open_step.id,
            "passed" if report.passed else "failed",
            gate_result=report.to_dict(),
            error_code=output.error_code,
            wall_clock_ms=_elapsed(started),
        )
        self._sync_task(task)
        current = self._repos.tasks.get(task.id)
        return {
            "task_id": task.id,
            "status": current.status if current else None,
            "step": open_step.step_key,
            "step_status": "passed" if report.passed else "failed",
            "gate": report.to_dict(),
            "artifact_id": artifact.id if artifact else None,
        }

    def _downgrade_stale(self, task_id: int) -> None:
        now = now_iso()
        for conclusion_id in self._repos.conclusions.stale_evidenced(task_id, now):
            self._repos.conclusions.set_status(conclusion_id, "undetermined")

    def _sibling_requirements(self, task: Task) -> list[dict[str, Any]]:
        rows, _ = self._repos.requirements.list(workspace_id=task.workspace_id, limit=200)
        return [
            {"id": row.id, "public_id": row.public_id, "title": row.title} for row in rows
        ]

    def _source_repo(self, requirement: Any) -> dict[str, Any]:
        """Resolve the requirement's project code to a source repository.

        Done here rather than inside a step so every step sees the same answer,
        and so the resolution is visible in the step trace even when the step
        itself never reads source.
        """

        from octop.infra.pm.domain.repos import (  # noqa: PLC0415
            project_of_requirement,
            resolve_project,
        )

        overrides: dict[str, str] = {}
        item = self._repos.integrations.get_by_vendor(requirement.workspace_id, "jira")
        if item is not None:
            mapping = loads(item.mapping_json, {}) or {}
            saved = mapping.get("projects")
            if isinstance(saved, dict):
                overrides = {str(key).upper(): str(value) for key, value in saved.items()}

        baseline = loads(getattr(requirement, "baseline_json", None) or "{}", {}) or {}
        if not isinstance(baseline, dict):
            baseline = {}
        project = project_of_requirement(
            baseline,
            str(getattr(requirement, "source_ref", "") or ""),
            source=str(getattr(requirement, "source", "") or ""),
        )
        if not project:
            return {"project": "", "mapped": False, "reason": "需求没有关联 Jira 项目"}

        binding = resolve_project(project, overrides)
        if binding is None:
            return {
                "project": project,
                "mapped": False,
                "reason": f"Jira 项目 {project} 未映射到源码仓库",
            }
        web = binding.web
        return {
            "project": project,
            "mapped": True,
            "system": binding.system,
            "repos": list(binding.repos),
            "origin": binding.origin,
            "web_repo": web.key if web is not None else "",
            "mirror": web.mirror if web is not None else "",
            "mirror_ready": bool(web is not None and web.root().is_dir()),
        }

    def _locate_evidence(self, requirement: Any) -> Any:
        """Rebuild the ticket's page hints from the intake snapshot.

        Read from ``baseline`` rather than from Jira so a ticket edited after
        intake cannot move the prototype to a different screen mid-pipeline.
        """

        from octop.infra.pm.domain.locate_evidence import (  # noqa: PLC0415
            evidence_from_baseline,
        )

        baseline = loads(getattr(requirement, "baseline_json", None) or "{}", {}) or {}
        if not isinstance(baseline, dict):
            return None
        evidence = evidence_from_baseline(baseline)
        return evidence if evidence.has_content else None

    def _persist_evidence(self, task_id: int, output: StepOutput) -> list[int]:
        ids: list[int] = []
        stamp = now_iso()
        for payload in output.evidence:
            envelope = evidence_rules.normalise(
                payload, retrieved_at=stamp, stale_at=_stale_at(payload)
            )
            if not envelope.ok:
                raise errors.invalid(
                    "evidence envelope rejected", problems=list(envelope.problems)
                )
            existing = self._repos.evidence.find_by_hash(task_id, envelope.content_hash)
            if existing is not None:
                ids.append(existing.id)
                continue
            created = self._repos.evidence.add(
                task_id=task_id,
                source_type=envelope.source_type,
                port=envelope.port,
                locator=envelope.locator,
                excerpt=envelope.excerpt,
                content_hash=envelope.content_hash,
                retrieved_at=envelope.retrieved_at,
                stale_at=envelope.stale_at,
                visibility=envelope.visibility,
                confidence=envelope.confidence,
            )
            ids.append(created.id)
        return ids

    def _persist_artifact(
        self, task: Task, step: TaskStep, output: StepOutput, evidence_ids: list[int]
    ) -> Any:
        if not output.artifact:
            return None
        payload = output.artifact
        artifact = self._repos.artifacts.create(
            task_id=task.id,
            step_id=step.id,
            kind=str(payload.get("kind") or "document"),
            origin=str(payload.get("origin") or "rule"),
            audience=str(payload.get("audience") or "internal"),
            title=payload.get("title"),
            fidelity=payload.get("fidelity"),
            fidelity_note=payload.get("fidelity_note"),
            body_inline=payload.get("body"),
        )
        if output.conclusions:
            from octop.infra.pm.repos.conclusion import ConclusionInput  # noqa: PLC0415

            items = [
                ConclusionInput(
                    text=str(item.get("text") or ""),
                    status=str(item.get("status") or "uncovered"),
                    customer_text=item.get("customer_text"),
                    evidence_ids=tuple(
                        evidence_ids[index]
                        for index in item.get("evidence_indexes", ())
                        if 0 <= int(index) < len(evidence_ids)
                    ),
                )
                for item in output.conclusions
            ]
            self._repos.conclusions.replace_for_artifact(artifact.id, items)
        return artifact

    def _open_card(self, task: Task, step: TaskStep, output: StepOutput) -> Any:
        payload = output.decision or {}
        level = str(payload.get("level") or "L2")
        decision_rules.ensure_level(level)
        handler = self._repos.handlers.get(step.seq)
        base = int(
            payload.get("timeout_seconds")
            or (handler.decision_timeout_s if handler else 300)
        )
        return self._repos.decisions.create(
            task_id=task.id,
            step_id=step.id,
            level=level,
            question=str(payload.get("question") or "需要拍板"),
            options=list(payload.get("options") or []),
            recommended_key=payload.get("recommended_key"),
            timeout_at=decision_rules.timeout_at(level, base),
        )

    def _escalate(self, task: Task, step: TaskStep) -> Any:
        options = [
            {"key": "retry", "label": "重试该环节", "pros": "保留上下文", "cons": "可能再次失败"},
            {"key": "skip", "label": "跳过并记录缺口", "pros": "不阻塞后续", "cons": "产物将标为缺口"},
            {"key": "stop", "label": "终止任务", "pros": "止损", "cons": "需求退回"},
        ]
        card = self._repos.decisions.create(
            task_id=task.id,
            step_id=step.id,
            level="L2",
            question=f"环节「{step.step_key}」连续 {step.attempt} 次未通过门禁，如何处置？",
            options=options,
            recommended_key="retry",
            timeout_at=decision_rules.timeout_at("L2", 86400),
        )
        self._repos.steps.finish(step.id, "failed", error_code="attempts_exhausted")
        self._repos.tasks.set_status(task.id, "blocked", stage=step.stage)
        return card

    def _decision_view(self, decision: Any) -> dict[str, Any]:
        return {
            "selected_key": decision.selected_key,
            "note": decision.note,
            "decided_by": decision.decided_by,
            "decided_at": decision.decided_at,
            "auto_decided": decision.auto_decided,
            "options": loads(decision.options_json, []),
            "option_snapshot": loads(decision.option_snapshot_json, []),
        }

    def _gate_context(
        self,
        *,
        task: Task,
        step: TaskStep,
        output: StepOutput | None = None,
        artifact: Any = None,
        decision: dict[str, Any] | None = None,
    ) -> gates.GateContext:
        evidence_rows = self._repos.evidence.list_for_task(task.id)
        conclusions: list[dict[str, Any]] = []
        artifacts = (
            [artifact] if artifact is not None else self._repos.artifacts.list_for_task(task.id)
        )
        for item in artifacts:
            for conclusion in self._repos.conclusions.list_for_artifact(item.id):
                conclusions.append(
                    {
                        "seq": conclusion.seq,
                        "text": conclusion.text,
                        "status": conclusion.status,
                        "evidence_ids": self._repos.conclusions.evidence_ids(conclusion.id),
                    }
                )

        trace = loads(step.trace_json, {}) or {}
        if output is not None:
            trace = {**trace, **output.trace}

        prototype_html = None
        prototype_origin = None
        fidelity = None
        fidelity_note = None
        if artifact is not None:
            if artifact.kind == "prototype_html":
                prototype_html = artifact.body_inline
            prototype_origin = getattr(artifact, "origin", None)
            fidelity = artifact.fidelity
            fidelity_note = artifact.fidelity_note

        if decision is None:
            answered, _ = self._repos.decisions.list(task_id=task.id, limit=20)
            answered = [
                item
                for item in answered
                if item.step_id == step.id and item.status in ("answered", "auto_answered")
            ]
            if answered:
                decision = self._decision_view(answered[0])

        return gates.GateContext(
            step_key=step.step_key,
            stage=step.stage,
            evidence=[
                {
                    "source_type": row.source_type,
                    "locator": loads(row.locator_json, {}),
                    "excerpt": row.excerpt,
                    "visibility": row.visibility,
                }
                for row in evidence_rows
            ],
            gaps=tuple(trace.get("gaps") or ()),
            trace_sources=tuple(trace.get("sources") or ()),
            requirement_items=tuple(_requirement_items(artifacts)),
            conclusions=tuple(conclusions),
            prototype_html=prototype_html,
            prototype_origin=prototype_origin,
            fidelity=fidelity,
            fidelity_note=fidelity_note,
            decision=decision,
            reviews=self._repos.reviews.verdict_map(task.id),
            external_write=bool(trace.get("external_write", False)),
            external_write_signed=bool(trace.get("external_write_signed", False)),
        )

    def _sync_task(self, task: Task) -> None:
        steps = self._repos.steps.list_for_task(task.id)
        status = task_status_from_steps([step.status for step in steps])
        stage = stage_from_steps(steps)
        self._repos.tasks.set_status(task.id, status, stage=stage)
        if status == "delivered":
            self._repos.requirements.set_status(task.requirement_id, "delivered")


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _stale_at(payload: dict[str, Any]) -> str | None:
    explicit = payload.get("stale_at")
    if explicit:
        return str(explicit)
    source_type = str(payload.get("source_type") or "")
    window = evidence_rules.STALE_WINDOWS_S.get(source_type)
    if window is None:
        return None
    from octop.infra.pm.repos._base import iso_in  # noqa: PLC0415

    return iso_in(window)


def _requirement_items(artifacts: Iterable[Any]) -> list[dict[str, Any]]:
    """Pull structured requirement items out of P2 artifacts, if present."""
    items: list[dict[str, Any]] = []
    for artifact in artifacts:
        if artifact.kind != "document" or not artifact.body_inline:
            continue
        payload = loads(artifact.body_inline, {})
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            items.extend(item for item in payload["items"] if isinstance(item, dict))
    return items


def summarize_steps(steps: Iterable[TaskStep]) -> list[dict[str, Any]]:
    return [
        {
            "id": step.id,
            "seq": step.seq,
            "step_key": step.step_key,
            "stage": step.stage,
            "status": step.status,
            "attempt": step.attempt,
            "gate": loads(step.gate_result_json, None),
            "error_code": step.error_code,
        }
        for step in steps
    ]


def stage_progress(steps: Iterable[TaskStep]) -> list[dict[str, Any]]:
    by_stage: dict[str, list[str]] = {}
    for step in steps:
        by_stage.setdefault(step.stage, []).append(step.status)
    out: list[dict[str, Any]] = []
    for spec in stages.STAGES:
        statuses = by_stage.get(spec.key, [])
        if not statuses:
            state = "pending"
        elif all(item in ("passed", "skipped") for item in statuses):
            state = "passed"
        elif "running" in statuses:
            state = "running"
        elif "failed" in statuses:
            state = "failed"
        elif "waiting_card" in statuses:
            state = "waiting_human"
        else:
            state = "pending"
        out.append(
            {
                "key": spec.key,
                "title": spec.title_zh,
                "purpose": spec.purpose_zh,
                "state": state,
                "steps": len(statuses),
                "passed": sum(1 for item in statuses if item == "passed"),
            }
        )
    return out


def trace_view(trace_json: str | None) -> dict[str, Any]:
    value = loads(trace_json, {})
    return value if isinstance(value, dict) else {}


def gate_payload(gate_result_json: str | None) -> dict[str, Any] | None:
    value = loads(gate_result_json, None)
    return value if isinstance(value, dict) else None


__all__ = [
    "MAX_ATTEMPTS",
    "Pipeline",
    "default_executor",
    "dumps",
    "gate_payload",
    "stage_progress",
    "summarize_steps",
    "trace_view",
]
