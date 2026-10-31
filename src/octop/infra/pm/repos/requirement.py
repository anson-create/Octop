"""Requirements: what the business actually asked for, plus later change requests.

Requirements are 1:1 with tasks by constraint, so a change of scope never forks
into a second requirement — it is recorded in ``pm_requirement_change`` and the
affected steps are re-flowed.
"""

from __future__ import annotations

import builtins
import sqlite3
import time

from octop.infra.db.pool import DatabasePool
from octop.infra.pm.models import Requirement, RequirementChange
from octop.infra.pm.repos._base import cell, dumps, insert_id, now_iso

_PUBLIC_PREFIX = "REQ"


def _public_id(date_key: str, ordinal: int) -> str:
    return f"{_PUBLIC_PREFIX}-{date_key}-{ordinal:03d}"


class RequirementRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def _next_public_id(self) -> str:
        date_key = time.strftime("%Y%m%d", time.gmtime())
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM pm_requirement WHERE public_id LIKE ?",
                (f"{_PUBLIC_PREFIX}-{date_key}-%",),
            ).fetchone()
        return _public_id(date_key, int(cell(row, "n", 0) or 0) + 1)

    def create(
        self,
        *,
        workspace_id: int,
        title: str,
        raw_text: str,
        source: str = "manual",
        source_ref: str | None = None,
        proposer: str | None = None,
        priority: int | None = None,
        baseline: dict[str, object] | None = None,
        client_token: str | None = None,
    ) -> Requirement:
        last_error: Exception | None = None
        for _ in range(5):
            public_id = self._next_public_id()
            try:
                with self._db.transaction() as conn:
                    new_id = insert_id(
                        conn,
                        "INSERT INTO pm_requirement("
                        "public_id, workspace_id, title, raw_text, source, source_ref,"
                        " proposer, priority, status, baseline_json, client_token, created_at"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)",
                        (
                            public_id,
                            workspace_id,
                            title,
                            raw_text,
                            source,
                            source_ref,
                            proposer,
                            priority,
                            dumps(baseline) if baseline else None,
                            client_token,
                            now_iso(),
                        ),
                    )
            except sqlite3.IntegrityError as exc:  # public_id collision under concurrency
                last_error = exc
                continue
            created = self.get(new_id)
            assert created is not None
            return created
        raise RuntimeError(f"could not allocate a requirement public id: {last_error}")

    def get(self, requirement_id: int) -> Requirement | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM pm_requirement WHERE id = ?", (requirement_id,)
            ).fetchone()
        return Requirement.from_row(row) if row is not None else None

    def get_by_client_token(self, client_token: str) -> Requirement | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM pm_requirement WHERE client_token = ?", (client_token,)
            ).fetchone()
        return Requirement.from_row(row) if row is not None else None

    def get_by_source_ref(self, workspace_id: int, source: str, source_ref: str) -> Requirement | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM pm_requirement WHERE workspace_id = ? AND source = ? "
                "AND source_ref = ?",
                (workspace_id, source, source_ref),
            ).fetchone()
        return Requirement.from_row(row) if row is not None else None

    def list(
        self,
        *,
        workspace_id: int | None = None,
        status: str | None = None,
        query: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[builtins.list[Requirement], int]:
        where: builtins.list[str] = []
        params: builtins.list[object] = []
        if workspace_id is not None:
            where.append("workspace_id = ?")
            params.append(workspace_id)
        if status:
            where.append("status = ?")
            params.append(status)
        if query:
            where.append("(title LIKE ? OR raw_text LIKE ? OR public_id LIKE ?)")
            like = f"%{query}%"
            params.extend([like, like, like])
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        with self._db.connect() as conn:
            total_row = conn.execute(
                f"SELECT COUNT(*) AS n FROM pm_requirement{clause}", params
            ).fetchone()
            rows = conn.execute(
                f"SELECT * FROM pm_requirement{clause} ORDER BY created_at DESC, id DESC "
                "LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return [Requirement.from_row(row) for row in rows], int(cell(total_row, "n", 0) or 0)

    def set_status(self, requirement_id: int, status: str) -> bool:
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE pm_requirement SET status = ? WHERE id = ?", (status, requirement_id)
            )
            return bool(cursor.rowcount)

    def set_baseline(self, requirement_id: int, baseline: dict[str, object]) -> bool:
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE pm_requirement SET baseline_json = ? WHERE id = ?",
                (dumps(baseline), requirement_id),
            )
            return bool(cursor.rowcount)

    def add_change(
        self,
        *,
        requirement_id: int,
        change_text: str,
        raised_by: int | None = None,
        decision_id: int | None = None,
        accepted: bool = False,
    ) -> RequirementChange:
        with self._db.transaction() as conn:
            new_id = insert_id(
                conn,
                "INSERT INTO pm_requirement_change("
                "requirement_id, change_text, raised_by, decision_id, accepted, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (requirement_id, change_text, raised_by, decision_id, 1 if accepted else 0, now_iso()),
            )
            row = conn.execute(
                "SELECT * FROM pm_requirement_change WHERE id = ?", (new_id,)
            ).fetchone()
        assert row is not None
        return RequirementChange.from_row(row)

    def list_changes(self, requirement_id: int) -> builtins.list[RequirementChange]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pm_requirement_change WHERE requirement_id = ? "
                "ORDER BY created_at, id",
                (requirement_id,),
            ).fetchall()
        return [RequirementChange.from_row(row) for row in rows]

    def accept_change(self, change_id: int, decision_id: int | None = None) -> bool:
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE pm_requirement_change SET accepted = 1, decision_id = ? WHERE id = ?",
                (decision_id, change_id),
            )
            return bool(cursor.rowcount)
