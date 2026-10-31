"""Workspace integrations: which evidence ports are live, and in which mode.

Credentials are never stored here — ``credential_ref`` points at the host secret
store, so a DB dump cannot leak a Jira token.
"""

from __future__ import annotations

from octop.infra.db.pool import DatabasePool
from octop.infra.pm.models import Integration
from octop.infra.pm.repos._base import dumps, loads, now_iso


class IntegrationRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def list_for_workspace(self, workspace_id: int) -> list[Integration]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pm_integration WHERE workspace_id = ? ORDER BY vendor",
                (workspace_id,),
            ).fetchall()
        return [Integration.from_row(row) for row in rows]

    def get(self, integration_id: int) -> Integration | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM pm_integration WHERE id = ?", (integration_id,)
            ).fetchone()
        return Integration.from_row(row) if row is not None else None

    def get_by_vendor(self, workspace_id: int, vendor: str) -> Integration | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM pm_integration WHERE workspace_id = ? AND vendor = ?",
                (workspace_id, vendor),
            ).fetchone()
        return Integration.from_row(row) if row is not None else None

    def upsert(
        self,
        *,
        workspace_id: int,
        vendor: str,
        enabled: bool = True,
        mode: str = "real",
        credential_ref: str | None = None,
        mapping: dict[str, object] | None = None,
    ) -> Integration:
        existing = self.get_by_vendor(workspace_id, vendor)
        mapping_json = dumps(mapping) if mapping is not None else None
        with self._db.transaction() as conn:
            if existing is None:
                conn.execute(
                    "INSERT INTO pm_integration("
                    "workspace_id, vendor, enabled, mode, credential_ref, mapping_json"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        workspace_id,
                        vendor,
                        1 if enabled else 0,
                        mode,
                        credential_ref,
                        mapping_json,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE pm_integration SET enabled = ?, mode = ?, credential_ref = ?,"
                    " mapping_json = COALESCE(?, mapping_json) WHERE id = ?",
                    (
                        1 if enabled else 0,
                        mode,
                        credential_ref,
                        mapping_json,
                        existing.id,
                    ),
                )
        current = self.get_by_vendor(workspace_id, vendor)
        assert current is not None
        return current

    def record_probe(self, integration_id: int, status: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE pm_integration SET last_probe_at = ?, probe_status = ? WHERE id = ?",
                (now_iso(), status, integration_id),
            )

    def mapping(self, integration: Integration) -> dict[str, object]:
        value = loads(integration.mapping_json, {})
        return value if isinstance(value, dict) else {}
