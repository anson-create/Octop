"""Conclusions and their evidence links.

This pair of tables is where "编造率 = 0" stops being a slogan. A conclusion may
only be stored as ``evidenced`` when at least one evidence row is linked; the
repo enforces that at write time by downgrading to ``uncovered``. The fabrication
gate (G-P4-04) then has something real to query.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.pm.models import Conclusion
from octop.infra.pm.repos._base import cell, insert_id, now_iso


@dataclass(frozen=True)
class ConclusionInput:
    text: str
    status: str = "uncovered"
    customer_text: str | None = None
    evidence_ids: tuple[int, ...] = ()


class ConclusionRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def replace_for_artifact(
        self, artifact_id: int, items: list[ConclusionInput]
    ) -> list[Conclusion]:
        """Rewrite an artifact's conclusions (idempotent re-generation)."""
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM pm_conclusion WHERE artifact_id = ?", (artifact_id,))
            for index, item in enumerate(items, start=1):
                status = item.status
                evidence_ids = tuple(dict.fromkeys(item.evidence_ids))
                if status == "evidenced" and not evidence_ids:
                    # Write-time proof: an unsupported claim cannot be stored as
                    # evidenced. This is the enforcement half of §10.1.
                    status = "uncovered"
                new_id = insert_id(
                    conn,
                    "INSERT INTO pm_conclusion("
                    "artifact_id, seq, text, customer_text, status, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    (artifact_id, index, item.text, item.customer_text, status, now_iso()),
                )
                for evidence_id in evidence_ids:
                    conn.execute(
                        "INSERT INTO pm_conclusion_evidence(conclusion_id, evidence_id)"
                        " VALUES (?, ?)",
                        (new_id, evidence_id),
                    )
        return self.list_for_artifact(artifact_id)

    def list_for_artifact(self, artifact_id: int) -> list[Conclusion]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pm_conclusion WHERE artifact_id = ? ORDER BY seq",
                (artifact_id,),
            ).fetchall()
        return [Conclusion.from_row(row) for row in rows]

    def get(self, conclusion_id: int) -> Conclusion | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM pm_conclusion WHERE id = ?", (conclusion_id,)
            ).fetchone()
        return Conclusion.from_row(row) if row is not None else None

    def evidence_ids(self, conclusion_id: int) -> list[int]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT evidence_id FROM pm_conclusion_evidence WHERE conclusion_id = ?",
                (conclusion_id,),
            ).fetchall()
        return [int(cell(row, "evidence_id")) for row in rows]

    def set_status(self, conclusion_id: int, status: str) -> bool:
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE pm_conclusion SET status = ? WHERE id = ?", (status, conclusion_id)
            )
            return bool(cursor.rowcount)

    def link(self, conclusion_id: int, evidence_id: int) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO pm_conclusion_evidence(conclusion_id, evidence_id) VALUES (?, ?)"
                " ON CONFLICT(conclusion_id, evidence_id) DO NOTHING",
                (conclusion_id, evidence_id),
            )

    def stats_for_task(self, task_id: int) -> dict[str, Any]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT c.status AS status, COUNT(*) AS n FROM pm_conclusion c "
                "JOIN pm_artifact a ON a.id = c.artifact_id WHERE a.task_id = ? "
                "GROUP BY c.status",
                (task_id,),
            ).fetchall()
        counts = {str(cell(row, "status")): int(cell(row, "n", 1) or 0) for row in rows}
        total = sum(counts.values())
        evidenced = counts.get("evidenced", 0)
        return {
            "total": total,
            "evidenced": evidenced,
            "uncovered": counts.get("uncovered", 0),
            "undetermined": counts.get("undetermined", 0),
            "completeness": round(evidenced / total, 4) if total else 0.0,
        }

    def stale_evidenced(self, task_id: int, now: str) -> list[int]:
        """Conclusions currently marked evidenced whose evidence has gone stale."""
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT c.id AS id FROM pm_conclusion c "
                "JOIN pm_artifact a ON a.id = c.artifact_id "
                "JOIN pm_conclusion_evidence ce ON ce.conclusion_id = c.id "
                "JOIN pm_evidence e ON e.id = ce.evidence_id "
                "WHERE a.task_id = ? AND c.status = 'evidenced' "
                "AND e.stale_at IS NOT NULL AND e.stale_at <= ?",
                (task_id, now),
            ).fetchall()
        return [int(cell(row, "id")) for row in rows]
