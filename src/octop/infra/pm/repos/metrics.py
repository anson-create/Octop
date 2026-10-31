"""Daily metric rollups and the golden/adversarial evaluation set.

The dashboard reads ``pm_metric_daily`` only — aggregating the business tables on
every page load would put read locks on the same SQLite file the pipeline writes.
"""

from __future__ import annotations

from octop.infra.db.pool import DatabasePool
from octop.infra.pm.models import EvalCase, MetricDaily
from octop.infra.pm.repos._base import dumps, insert_id


class MetricRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def upsert(
        self,
        *,
        day: str,
        workspace_id: int,
        metric_key: str,
        value: float,
        numerator: int | None = None,
        denominator: int | None = None,
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO pm_metric_daily(day, workspace_id, metric_key, value, numerator,"
                " denominator) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(day, workspace_id, metric_key) DO UPDATE SET "
                "value = excluded.value, numerator = excluded.numerator,"
                " denominator = excluded.denominator",
                (day, workspace_id, metric_key, value, numerator, denominator),
            )

    def series(
        self,
        *,
        workspace_id: int,
        date_from: str,
        date_to: str,
        metric_key: str | None = None,
    ) -> list[MetricDaily]:
        sql = (
            "SELECT * FROM pm_metric_daily WHERE workspace_id = ? AND day >= ? AND day <= ?"
        )
        params: list[object] = [workspace_id, date_from, date_to]
        if metric_key:
            sql += " AND metric_key = ?"
            params.append(metric_key)
        sql += " ORDER BY day, metric_key"
        with self._db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [MetricDaily.from_row(row) for row in rows]


class EvalCaseRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def list_active(self, *, kind: str | None = None) -> list[EvalCase]:
        sql = "SELECT * FROM pm_eval_case WHERE active = 1"
        params: list[object] = []
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY kind, id"
        with self._db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [EvalCase.from_row(row) for row in rows]

    def upsert(
        self,
        *,
        name: str,
        kind: str,
        input_payload: dict[str, object],
        expect_payload: dict[str, object],
        active: bool = True,
    ) -> EvalCase:
        with self._db.transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM pm_eval_case WHERE name = ?", (name,)
            ).fetchone()
            if existing is None:
                new_id = insert_id(
                    conn,
                    "INSERT INTO pm_eval_case(name, kind, input_json, expect_json, active)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (name, kind, dumps(input_payload), dumps(expect_payload), 1 if active else 0),
                )
            else:
                new_id = int(existing["id"])
                conn.execute(
                    "UPDATE pm_eval_case SET kind = ?, input_json = ?, expect_json = ?, active = ?"
                    " WHERE id = ?",
                    (kind, dumps(input_payload), dumps(expect_payload), 1 if active else 0, new_id),
                )
            row = conn.execute("SELECT * FROM pm_eval_case WHERE id = ?", (new_id,)).fetchone()
        assert row is not None
        return EvalCase.from_row(row)
