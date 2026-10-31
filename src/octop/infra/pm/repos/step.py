"""Per-task step instances and the workflow handler table.

``pm_step_handler`` is configuration (handler id, threshold, decision level,
timeout) so behaviour changes ship as a row update; ``pm_task_step`` is the
per-task execution record whose status only the gate engine writes.
"""

from __future__ import annotations

from octop.infra.db.pool import DatabasePool
from octop.infra.pm.domain import stages
from octop.infra.pm.models import StepHandler, TaskStep
from octop.infra.pm.repos._base import dumps, now_iso


class HandlerRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def list(self, workflow_key: str = stages.WORKFLOW_KEY) -> list[StepHandler]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pm_step_handler WHERE workflow_key = ? ORDER BY seq",
                (workflow_key,),
            ).fetchall()
        return [StepHandler.from_row(row) for row in rows]

    def get(self, seq: int, workflow_key: str = stages.WORKFLOW_KEY) -> StepHandler | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM pm_step_handler WHERE workflow_key = ? AND seq = ?",
                (workflow_key, seq),
            ).fetchone()
        return StepHandler.from_row(row) if row is not None else None

    def by_step_key(self, step_key: str, workflow_key: str = stages.WORKFLOW_KEY) -> StepHandler | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM pm_step_handler WHERE workflow_key = ? AND step_key = ?",
                (workflow_key, step_key),
            ).fetchone()
        return StepHandler.from_row(row) if row is not None else None

    def set_decision_level(self, seq: int, level: str, workflow_key: str = stages.WORKFLOW_KEY) -> bool:
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE pm_step_handler SET decision_level = ? WHERE workflow_key = ? AND seq = ?",
                (level, workflow_key, seq),
            )
            return bool(cursor.rowcount)

    def set_threshold(self, seq: int, threshold: int, workflow_key: str = stages.WORKFLOW_KEY) -> bool:
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE pm_step_handler SET threshold = ? WHERE workflow_key = ? AND seq = ?",
                (threshold, workflow_key, seq),
            )
            return bool(cursor.rowcount)


class StepRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def ensure_steps(self, task_id: int, workflow_key: str = stages.WORKFLOW_KEY) -> list[TaskStep]:
        """Materialise the 13 step rows for a task. Idempotent."""
        with self._db.connect() as conn:
            existing = {
                int(row["seq"])
                for row in conn.execute(
                    "SELECT seq FROM pm_task_step WHERE task_id = ?", (task_id,)
                ).fetchall()
            }
        missing = [spec for spec in stages.STEPS if spec.seq not in existing]
        if missing:
            with self._db.transaction() as conn:
                for spec in missing:
                    conn.execute(
                        "INSERT INTO pm_task_step(task_id, seq, step_key, stage, status, attempt)"
                        " VALUES (?, ?, ?, ?, 'pending', 0)",
                        (task_id, spec.seq, spec.step_key, spec.stage),
                    )
        return self.list_for_task(task_id)

    def list_for_task(self, task_id: int) -> list[TaskStep]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pm_task_step WHERE task_id = ? ORDER BY seq", (task_id,)
            ).fetchall()
        return [TaskStep.from_row(row) for row in rows]

    def get(self, step_id: int) -> TaskStep | None:
        with self._db.connect() as conn:
            row = conn.execute("SELECT * FROM pm_task_step WHERE id = ?", (step_id,)).fetchone()
        return TaskStep.from_row(row) if row is not None else None

    def get_by_seq(self, task_id: int, seq: int) -> TaskStep | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM pm_task_step WHERE task_id = ? AND seq = ?", (task_id, seq)
            ).fetchone()
        return TaskStep.from_row(row) if row is not None else None

    def start(self, step_id: int) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE pm_task_step SET status = 'running', started_at = ?, finished_at = NULL,"
                " attempt = attempt + 1, error_code = NULL WHERE id = ?",
                (now_iso(), step_id),
            )

    def finish(
        self,
        step_id: int,
        status: str,
        *,
        gate_result: object | None = None,
        error_code: str | None = None,
        wall_clock_ms: int | None = None,
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE pm_task_step SET status = ?, finished_at = ?, gate_result_json = ?,"
                " error_code = ?, wall_clock_ms = ? WHERE id = ?",
                (
                    status,
                    now_iso(),
                    dumps(gate_result) if gate_result is not None else None,
                    error_code,
                    wall_clock_ms,
                    step_id,
                ),
            )

    def set_trace(self, step_id: int, trace: object) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE pm_task_step SET trace_json = ? WHERE id = ?",
                (dumps(trace) if trace is not None else None, step_id),
            )

    def reset_from(self, task_id: int, seq: int) -> int:
        """Put ``seq`` and everything after it back to pending (re-flow / reject)."""
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE pm_task_step SET status = 'pending', attempt = 0, gate_result_json = NULL,"
                " error_code = NULL, started_at = NULL, finished_at = NULL "
                "WHERE task_id = ? AND seq >= ?",
                (task_id, seq),
            )
            return int(cursor.rowcount or 0)

    def reset_one(self, step_id: int) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE pm_task_step SET status = 'pending', gate_result_json = NULL,"
                " error_code = NULL, started_at = NULL, finished_at = NULL WHERE id = ?",
                (step_id,),
            )

    def first_open_step(self, task_id: int) -> TaskStep | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM pm_task_step WHERE task_id = ? AND status IN ('pending','failed')"
                " ORDER BY seq LIMIT 1",
                (task_id,),
            ).fetchone()
        return TaskStep.from_row(row) if row is not None else None

    def add_artifact_step_link(self, step_id: int, artifact_id: int) -> None:
        with self._db.transaction() as conn:
            conn.execute("UPDATE pm_artifact SET step_id = ? WHERE id = ?", (step_id, artifact_id))
