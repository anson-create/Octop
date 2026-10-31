"""Repo round-trips: constraints, 1:1, idempotency, versioning."""

from __future__ import annotations

import sqlite3

import pytest

from octop.infra.db.pool import SqlitePool
from octop.infra.pm import errors
from octop.infra.pm.repos import ConclusionInput, PmRepos
from octop.infra.pm.repos._base import now_iso


@pytest.fixture
def repos(db: SqlitePool) -> PmRepos:
    return PmRepos(db)


def test_workspace_name_unique_per_owner(repos: PmRepos) -> None:
    repos.workspaces.create(name="A", owner_user_id=1)
    with pytest.raises(sqlite3.IntegrityError):
        repos.workspaces.create(name="A", owner_user_id=1)
    other = repos.workspaces.create(name="A", owner_user_id=2)
    assert other.owner_user_id == 2


def test_client_token_is_idempotent(repos: PmRepos) -> None:
    first = repos.workspaces.create(name="A", owner_user_id=1, client_token="tok-1")
    assert repos.workspaces.get_by_client_token("tok-1").id == first.id


def test_requirement_public_id_and_source_ref(repos: PmRepos) -> None:
    workspace = repos.workspaces.create(name="A", owner_user_id=1)
    first = repos.requirements.create(
        workspace_id=workspace.id, title="t1", raw_text="r1", source="jira", source_ref="CARGO-1"
    )
    assert first.public_id.startswith("REQ-")
    found = repos.requirements.get_by_source_ref(workspace.id, "jira", "CARGO-1")
    assert found is not None and found.id == first.id


def test_task_is_one_to_one_with_requirement(repos: PmRepos) -> None:
    workspace = repos.workspaces.create(name="A", owner_user_id=1)
    requirement = repos.requirements.create(workspace_id=workspace.id, title="t", raw_text="r")
    repos.tasks.create(
        requirement_id=requirement.id, workspace_id=workspace.id, owner_user_id=1
    )
    with pytest.raises(RuntimeError):
        repos.tasks.create(
            requirement_id=requirement.id, workspace_id=workspace.id, owner_user_id=1
        )


def test_claim_is_exclusive(repos: PmRepos) -> None:
    workspace = repos.workspaces.create(name="A", owner_user_id=1)
    requirement = repos.requirements.create(workspace_id=workspace.id, title="t", raw_text="r")
    task = repos.tasks.create(
        requirement_id=requirement.id, workspace_id=workspace.id, owner_user_id=1
    )
    assert repos.tasks.claim(task.id) is True
    assert repos.tasks.claim(task.id) is False
    repos.tasks.release(task.id)
    assert repos.tasks.claim(task.id) is True


def test_steps_materialise_thirteen_rows(repos: PmRepos) -> None:
    workspace = repos.workspaces.create(name="A", owner_user_id=1)
    requirement = repos.requirements.create(workspace_id=workspace.id, title="t", raw_text="r")
    task = repos.tasks.create(
        requirement_id=requirement.id, workspace_id=workspace.id, owner_user_id=1
    )
    steps = repos.steps.ensure_steps(task.id)
    assert [step.step_key for step in steps][0] == "intake"
    assert len(steps) == 13
    assert len(repos.steps.ensure_steps(task.id)) == 13


def test_artifact_versions_increment_per_kind_and_audience(repos: PmRepos) -> None:
    workspace = repos.workspaces.create(name="A", owner_user_id=1)
    requirement = repos.requirements.create(workspace_id=workspace.id, title="t", raw_text="r")
    task = repos.tasks.create(
        requirement_id=requirement.id, workspace_id=workspace.id, owner_user_id=1
    )
    v1 = repos.artifacts.create(task_id=task.id, kind="document", origin="rule")
    v2 = repos.artifacts.create(task_id=task.id, kind="document", origin="rule")
    customer = repos.artifacts.create(task_id=task.id, kind="document", origin="rule", audience="customer")
    assert (v1.version, v2.version, customer.version) == (1, 2, 1)
    assert repos.artifacts.latest(task.id, "document").version == 2


def test_evidenced_conclusion_without_evidence_is_downgraded(repos: PmRepos) -> None:
    workspace = repos.workspaces.create(name="A", owner_user_id=1)
    requirement = repos.requirements.create(workspace_id=workspace.id, title="t", raw_text="r")
    task = repos.tasks.create(
        requirement_id=requirement.id, workspace_id=workspace.id, owner_user_id=1
    )
    artifact = repos.artifacts.create(task_id=task.id, kind="document", origin="rule")
    evidence = repos.evidence.add(
        task_id=task.id,
        source_type="code",
        port="code",
        locator={"kind": "code_span", "repo": "r", "path": "a.py", "line": [1, 2]},
        excerpt="x = 1",
        content_hash="hash-1",
        retrieved_at=now_iso(),
    )
    items = [
        ConclusionInput(text="supported", status="evidenced", evidence_ids=(evidence.id,)),
        ConclusionInput(text="unsupported", status="evidenced"),
    ]
    stored = repos.conclusions.replace_for_artifact(artifact.id, items)
    assert [item.status for item in stored] == ["evidenced", "uncovered"]
    assert repos.conclusions.evidence_ids(stored[0].id) == [evidence.id]


def test_evidence_dedup_by_hash(repos: PmRepos) -> None:
    workspace = repos.workspaces.create(name="A", owner_user_id=1)
    requirement = repos.requirements.create(workspace_id=workspace.id, title="t", raw_text="r")
    task = repos.tasks.create(
        requirement_id=requirement.id, workspace_id=workspace.id, owner_user_id=1
    )
    first = repos.evidence.add(
        task_id=task.id,
        source_type="kb",
        port="kb",
        locator={"kind": "kb_section", "kb_id": "k", "doc_id": "d"},
        excerpt="same",
        content_hash="dup",
        retrieved_at=now_iso(),
    )
    assert repos.evidence.find_by_hash(task.id, "dup").id == first.id
    assert repos.evidence.source_breakdown(task.id) == {"kb": 1}


def test_decision_answer_requires_pending(repos: PmRepos) -> None:
    workspace = repos.workspaces.create(name="A", owner_user_id=1)
    requirement = repos.requirements.create(workspace_id=workspace.id, title="t", raw_text="r")
    task = repos.tasks.create(
        requirement_id=requirement.id, workspace_id=workspace.id, owner_user_id=1
    )
    decision = repos.decisions.create(
        task_id=task.id,
        level="L2",
        question="q",
        options=[{"key": "yes", "label": "Y"}],
        timeout_at=now_iso(),
    )
    assert repos.decisions.answer(decision.id, selected_key="yes", note="足够长的理由说明", decided_by=1)
    assert repos.decisions.answer(decision.id, selected_key="yes", note="再答一次", decided_by=1) is False


def test_review_is_upserted_per_perspective(repos: PmRepos) -> None:
    workspace = repos.workspaces.create(name="A", owner_user_id=1)
    requirement = repos.requirements.create(workspace_id=workspace.id, title="t", raw_text="r")
    task = repos.tasks.create(
        requirement_id=requirement.id, workspace_id=workspace.id, owner_user_id=1
    )
    repos.reviews.submit(task_id=task.id, perspective="tech", verdict="reject", comment="x")
    repos.reviews.submit(task_id=task.id, perspective="tech", verdict="pass", comment="y")
    rows = repos.reviews.list_for_task(task.id)
    assert len(rows) == 1 and rows[0].verdict == "pass"


def test_metric_upsert_overwrites_same_day(repos: PmRepos) -> None:
    repos.metrics.upsert(day="2026-10-31", workspace_id=1, metric_key="completeness", value=0.5)
    repos.metrics.upsert(day="2026-10-31", workspace_id=1, metric_key="completeness", value=0.9)
    rows = repos.metrics.series(workspace_id=1, date_from="2026-10-01", date_to="2026-11-01")
    assert len(rows) == 1 and rows[0].value == 0.9


def test_eval_case_upsert_by_name(repos: PmRepos) -> None:
    repos.eval_cases.upsert(name="adv-1", kind="adversarial", input_payload={}, expect_payload={})
    repos.eval_cases.upsert(name="adv-1", kind="adversarial", input_payload={"a": 1}, expect_payload={})
    assert len(repos.eval_cases.list_active(kind="adversarial")) == 1


def test_errors_are_octop_errors() -> None:
    err = errors.conflict("boom", a=1)
    assert err.status == 409 and err.details == {"a": 1}
    assert errors.invalid("bad").status == 400
    assert errors.not_found("task", 1).status == 404
