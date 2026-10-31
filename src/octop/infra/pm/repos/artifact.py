"""Artifacts: versioned, audience-scoped outputs of a step."""

from __future__ import annotations

from octop.infra.db.pool import DatabasePool
from octop.infra.pm.models import Artifact
from octop.infra.pm.repos._base import cell, insert_id, now_iso


class ArtifactRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def create(
        self,
        *,
        task_id: int,
        kind: str,
        origin: str,
        audience: str = "internal",
        title: str | None = None,
        fidelity: str | None = None,
        fidelity_note: str | None = None,
        body_inline: str | None = None,
        body_path: str | None = None,
        step_id: int | None = None,
    ) -> Artifact:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM pm_artifact "
                "WHERE task_id = ? AND kind = ? AND audience = ?",
                (task_id, kind, audience),
            ).fetchone()
        next_version = int(cell(row, "v", 0) or 0) + 1
        with self._db.transaction() as conn:
            new_id = insert_id(
                conn,
                "INSERT INTO pm_artifact("
                "task_id, step_id, kind, title, audience, fidelity, fidelity_note,"
                " version, body_path, body_inline, origin, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    step_id,
                    kind,
                    title,
                    audience,
                    fidelity,
                    fidelity_note,
                    next_version,
                    body_path,
                    body_inline,
                    origin,
                    now_iso(),
                ),
            )
        created = self.get(new_id)
        assert created is not None
        return created

    def get(self, artifact_id: int) -> Artifact | None:
        with self._db.connect() as conn:
            row = conn.execute("SELECT * FROM pm_artifact WHERE id = ?", (artifact_id,)).fetchone()
        return Artifact.from_row(row) if row is not None else None

    def list_for_task(self, task_id: int, *, audience: str | None = None) -> list[Artifact]:
        sql = "SELECT * FROM pm_artifact WHERE task_id = ?"
        params: list[object] = [task_id]
        if audience:
            sql += " AND audience = ?"
            params.append(audience)
        sql += " ORDER BY kind, version DESC"
        with self._db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Artifact.from_row(row) for row in rows]

    def latest(
        self, task_id: int, kind: str, *, audience: str = "internal"
    ) -> Artifact | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM pm_artifact WHERE task_id = ? AND kind = ? AND audience = ? "
                "ORDER BY version DESC LIMIT 1",
                (task_id, kind, audience),
            ).fetchone()
        return Artifact.from_row(row) if row is not None else None

    def update_body(
        self,
        artifact_id: int,
        *,
        body_inline: str | None = None,
        body_path: str | None = None,
        fidelity: str | None = None,
        fidelity_note: str | None = None,
    ) -> bool:
        assignments: list[str] = []
        params: list[object] = []
        if body_inline is not None:
            assignments.append("body_inline = ?")
            params.append(body_inline)
        if body_path is not None:
            assignments.append("body_path = ?")
            params.append(body_path)
        if fidelity is not None:
            assignments.append("fidelity = ?")
            params.append(fidelity)
        if fidelity_note is not None:
            assignments.append("fidelity_note = ?")
            params.append(fidelity_note)
        if not assignments:
            return False
        params.append(artifact_id)
        with self._db.transaction() as conn:
            cursor = conn.execute(
                f"UPDATE pm_artifact SET {', '.join(assignments)} WHERE id = ?", params
            )
            return bool(cursor.rowcount)

    def count_for_task(self, task_id: int) -> int:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM pm_artifact WHERE task_id = ?", (task_id,)
            ).fetchone()
        return int(cell(row, "n", 0) or 0)
