"""Tasks: the unit of pipeline execution, 1:1 with a requirement.

Status and stage are written only by the state machine and the scheduler — never
by model output. ``locked_until`` is the optimistic lock that keeps two ticks
from running the same task.
"""

from __future__ import annotations

import builtins
import sqlite3
import time

from octop.infra.db.pool import DatabasePool
from octop.infra.pm.models import Task
from octop.infra.pm.repos._base import cell, insert_id, iso_in, now_iso

_PUBLIC_PREFIX = "PM"
_TICK_LOCK_SECONDS = 300


class TaskRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def _next_public_id(self) -> str:
        date_key = time.strftime("%Y%m%d", time.gmtime())
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM pm_task WHERE public_id LIKE ?",
                (f"{_PUBLIC_PREFIX}-{date_key}-%",),
            ).fetchone()
        return f"{_PUBLIC_PREFIX}-{date_key}-{int(cell(row, 'n', 0) or 0) + 1:03d}"

    def create(
        self,
        *,
        requirement_id: int,
        workspace_id: int,
        owner_user_id: int,
        client_token: str | None = None,
    ) -> Task:
        last_error: Exception | None = None
        for _ in range(5):
            public_id = self._next_public_id()
            stamp = now_iso()
            try:
                with self._db.transaction() as conn:
                    new_id = insert_id(
                        conn,
                        "INSERT INTO pm_task("
                        "public_id, requirement_id, workspace_id, stage, status, owner_user_id,"
                        " generation, client_token, created_at, updated_at"
                        ") VALUES (?, ?, ?, 'P1', 'queued', ?, 1, ?, ?, ?)",
                        (
                            public_id,
                            requirement_id,
                            workspace_id,
                            owner_user_id,
                            client_token,
                            stamp,
                            stamp,
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                # Either public_id collided or the requirement already has a task.
                last_error = exc
                continue
            created = self.get(new_id)
            assert created is not None
            return created
        raise RuntimeError(f"could not create task: {last_error}")

    def get(self, task_id: int) -> Task | None:
        with self._db.connect() as conn:
            row = conn.execute("SELECT * FROM pm_task WHERE id = ?", (task_id,)).fetchone()
        return Task.from_row(row) if row is not None else None

    def get_by_requirement(self, requirement_id: int) -> Task | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM pm_task WHERE requirement_id = ?", (requirement_id,)
            ).fetchone()
        return Task.from_row(row) if row is not None else None

    def get_by_client_token(self, client_token: str) -> Task | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM pm_task WHERE client_token = ?", (client_token,)
            ).fetchone()
        return Task.from_row(row) if row is not None else None

    def list(
        self,
        *,
        owner_user_id: int | None = None,
        status: str | None = None,
        workspace_id: int | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[builtins.list[Task], int]:
        where: builtins.list[str] = []
        params: builtins.list[object] = []
        if owner_user_id is not None:
            where.append("owner_user_id = ?")
            params.append(owner_user_id)
        if status:
            where.append("status = ?")
            params.append(status)
        if workspace_id is not None:
            where.append("workspace_id = ?")
            params.append(workspace_id)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        with self._db.connect() as conn:
            total_row = conn.execute(
                f"SELECT COUNT(*) AS n FROM pm_task{clause}", params
            ).fetchone()
            rows = conn.execute(
                f"SELECT * FROM pm_task{clause} ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return [Task.from_row(row) for row in rows], int(cell(total_row, "n", 0) or 0)

    def claim(self, task_id: int, *, lock_seconds: int = _TICK_LOCK_SECONDS) -> bool:
        """Take the execution lock. ``True`` only for the winner."""
        stamp = now_iso()
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE pm_task SET status = 'running', locked_until = ?, updated_at = ? "
                "WHERE id = ? AND status IN ('queued','running') "
                "AND (locked_until IS NULL OR locked_until <= ?)",
                (iso_in(lock_seconds), stamp, task_id, stamp),
            )
            return int(cursor.rowcount or 0) == 1

    def release(self, task_id: int) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE pm_task SET locked_until = NULL, updated_at = ? WHERE id = ?",
                (now_iso(), task_id),
            )

    def set_status(self, task_id: int, status: str, *, stage: str | None = None) -> bool:
        assignments = ["status = ?", "updated_at = ?"]
        params: builtins.list[object] = [status, now_iso()]
        if stage is not None:
            assignments.append("stage = ?")
            params.append(stage)
        params.append(task_id)
        with self._db.transaction() as conn:
            cursor = conn.execute(
                f"UPDATE pm_task SET {', '.join(assignments)} WHERE id = ?", params
            )
            return bool(cursor.rowcount)

    def set_pipeline(
        self,
        task_id: int,
        *,
        thread_id: str | None = None,
        agent_id: int | None = None,
        generation: int | None = None,
    ) -> None:
        assignments = ["updated_at = ?"]
        params: builtins.list[object] = [now_iso()]
        if thread_id is not None:
            assignments.append("pipeline_thread_id = ?")
            params.append(thread_id)
        if agent_id is not None:
            assignments.append("pipeline_agent_id = ?")
            params.append(agent_id)
        if generation is not None:
            assignments.append("generation = ?")
            params.append(generation)
        params.append(task_id)
        with self._db.transaction() as conn:
            conn.execute(f"UPDATE pm_task SET {', '.join(assignments)} WHERE id = ?", params)

    def counts_by_status(self) -> dict[str, int]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM pm_task GROUP BY status"
            ).fetchall()
        return {str(cell(row, "status")): int(cell(row, "n", 1) or 0) for row in rows}
