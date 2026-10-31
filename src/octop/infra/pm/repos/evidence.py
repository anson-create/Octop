"""Evidence rows: one row per retrievable evidence packet.

``locator_json`` is what makes a citation clickable; ``visibility`` is what keeps
internal facts out of a customer export. Both are enforced by the gate engine,
stored here verbatim.
"""

from __future__ import annotations

from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.pm.models import Evidence
from octop.infra.pm.repos._base import cell, dumps, insert_id, now_iso


class EvidenceRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def add(
        self,
        *,
        task_id: int,
        source_type: str,
        port: str,
        locator: Any,
        excerpt: str,
        content_hash: str,
        retrieved_at: str,
        stale_at: str | None = None,
        visibility: str = "internal",
        confidence: float | None = None,
    ) -> Evidence:
        with self._db.transaction() as conn:
            new_id = insert_id(
                conn,
                "INSERT INTO pm_evidence("
                "task_id, source_type, port, locator_json, excerpt, content_hash,"
                " retrieved_at, stale_at, visibility, confidence, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    source_type,
                    port,
                    dumps(locator),
                    excerpt,
                    content_hash,
                    retrieved_at,
                    stale_at,
                    visibility,
                    confidence,
                    now_iso(),
                ),
            )
        created = self.get(new_id)
        assert created is not None
        return created

    def get(self, evidence_id: int) -> Evidence | None:
        with self._db.connect() as conn:
            row = conn.execute("SELECT * FROM pm_evidence WHERE id = ?", (evidence_id,)).fetchone()
        return Evidence.from_row(row) if row is not None else None

    def list_for_task(self, task_id: int, *, source_type: str | None = None) -> list[Evidence]:
        sql = "SELECT * FROM pm_evidence WHERE task_id = ?"
        params: list[object] = [task_id]
        if source_type:
            sql += " AND source_type = ?"
            params.append(source_type)
        sql += " ORDER BY id"
        with self._db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Evidence.from_row(row) for row in rows]

    def find_by_hash(self, task_id: int, content_hash: str) -> Evidence | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM pm_evidence WHERE task_id = ? AND content_hash = ? LIMIT 1",
                (task_id, content_hash),
            ).fetchone()
        return Evidence.from_row(row) if row is not None else None

    def list_stale(self, task_id: int | None = None, *, now: str | None = None) -> list[Evidence]:
        sql = "SELECT * FROM pm_evidence WHERE stale_at IS NOT NULL AND stale_at <= ?"
        params: list[object] = [now or now_iso()]
        if task_id is not None:
            sql += " AND task_id = ?"
            params.append(task_id)
        with self._db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Evidence.from_row(row) for row in rows]

    def source_breakdown(self, task_id: int) -> dict[str, int]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT source_type, COUNT(*) AS n FROM pm_evidence WHERE task_id = ? "
                "GROUP BY source_type",
                (task_id,),
            ).fetchall()
        return {str(cell(row, "source_type")): int(cell(row, "n", 1) or 0) for row in rows}

    def count_for_task(self, task_id: int) -> int:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM pm_evidence WHERE task_id = ?", (task_id,)
            ).fetchone()
        return int(cell(row, "n", 0) or 0)
