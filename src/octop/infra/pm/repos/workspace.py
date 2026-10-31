"""Workspaces: the top-level container a requirement lives in."""

from __future__ import annotations

from octop.infra.db.pool import DatabasePool
from octop.infra.pm.models import Workspace
from octop.infra.pm.repos._base import cell, insert_id, now_iso


class WorkspaceRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def create(
        self,
        *,
        name: str,
        owner_user_id: int,
        description: str | None = None,
        client_token: str | None = None,
    ) -> Workspace:
        with self._db.transaction() as conn:
            new_id = insert_id(
                conn,
                "INSERT INTO pm_workspace(name, description, owner_user_id, client_token, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (name, description, owner_user_id, client_token, now_iso()),
            )
        created = self.get(new_id)
        assert created is not None
        return created

    def get(self, workspace_id: int) -> Workspace | None:
        with self._db.connect() as conn:
            row = conn.execute("SELECT * FROM pm_workspace WHERE id = ?", (workspace_id,)).fetchone()
        return Workspace.from_row(row) if row is not None else None

    def get_by_client_token(self, client_token: str) -> Workspace | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM pm_workspace WHERE client_token = ?", (client_token,)
            ).fetchone()
        return Workspace.from_row(row) if row is not None else None

    def get_by_name(self, owner_user_id: int, name: str) -> Workspace | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM pm_workspace WHERE owner_user_id = ? AND name = ?",
                (owner_user_id, name),
            ).fetchone()
        return Workspace.from_row(row) if row is not None else None

    def list_all(self, *, include_archived: bool = False) -> list[Workspace]:
        sql = "SELECT * FROM pm_workspace"
        if not include_archived:
            sql += " WHERE archived_at IS NULL"
        sql += " ORDER BY id"
        with self._db.connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [Workspace.from_row(row) for row in rows]

    def archive(self, workspace_id: int) -> bool:
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE pm_workspace SET archived_at = ? WHERE id = ? AND archived_at IS NULL",
                (now_iso(), workspace_id),
            )
            return bool(cursor.rowcount)

    def count(self) -> int:
        with self._db.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM pm_workspace").fetchone()
        return int(cell(row, "n", 0) or 0)
