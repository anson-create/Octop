"""Decision ledger — the durable side of human-in-the-loop.

The host's ``HitlPendingStore`` lives in process memory with a 30-minute TTL, so a
restart forgets every pending card. ``pm_decision`` is the source of truth: the
card survives, and §7.4's degraded recovery replays the step instead of pretending
the host can resume a turn it no longer remembers.
"""

from __future__ import annotations

import builtins
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.pm.models import Decision
from octop.infra.pm.repos._base import cell, dumps, insert_id, now_iso


class DecisionRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def create(
        self,
        *,
        task_id: int,
        level: str,
        question: str,
        options: builtins.list[dict[str, Any]],
        timeout_at: str,
        recommended_key: str | None = None,
        step_id: int | None = None,
        assignee_user_id: int | None = None,
        thread_id: str | None = None,
        resume_agent_id: int | None = None,
        action_requests: Any = None,
        client_token: str | None = None,
    ) -> Decision:
        options_json = dumps(options)
        with self._db.transaction() as conn:
            new_id = insert_id(
                conn,
                "INSERT INTO pm_decision("
                "task_id, step_id, level, question, options_json, recommended_key,"
                " option_snapshot_json, assignee_user_id, status, timeout_at, thread_id,"
                " resume_agent_id, action_requests_json, client_token, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    step_id,
                    level,
                    question,
                    options_json,
                    recommended_key,
                    options_json,
                    assignee_user_id,
                    timeout_at,
                    thread_id,
                    resume_agent_id,
                    dumps(action_requests) if action_requests is not None else None,
                    client_token,
                    now_iso(),
                ),
            )
        created = self.get(new_id)
        assert created is not None
        return created

    def get(self, decision_id: int) -> Decision | None:
        with self._db.connect() as conn:
            row = conn.execute("SELECT * FROM pm_decision WHERE id = ?", (decision_id,)).fetchone()
        return Decision.from_row(row) if row is not None else None

    def get_by_client_token(self, client_token: str) -> Decision | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM pm_decision WHERE client_token = ?", (client_token,)
            ).fetchone()
        return Decision.from_row(row) if row is not None else None

    def list(
        self,
        *,
        task_id: int | None = None,
        status: str | None = None,
        level: str | None = None,
        assignee_user_id: int | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[builtins.list[Decision], int]:
        where: builtins.list[str] = []
        params: builtins.list[object] = []
        if task_id is not None:
            where.append("task_id = ?")
            params.append(task_id)
        if status:
            where.append("status = ?")
            params.append(status)
        if level:
            where.append("level = ?")
            params.append(level)
        if assignee_user_id is not None:
            where.append("(assignee_user_id = ? OR assignee_user_id IS NULL)")
            params.append(assignee_user_id)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        order = " ORDER BY timeout_at ASC, id ASC" if status == "pending" else " ORDER BY id DESC"
        with self._db.connect() as conn:
            total_row = conn.execute(
                f"SELECT COUNT(*) AS n FROM pm_decision{clause}", params
            ).fetchone()
            rows = conn.execute(
                f"SELECT * FROM pm_decision{clause}{order} LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return [Decision.from_row(row) for row in rows], int(cell(total_row, "n", 0) or 0)

    def answer(
        self,
        decision_id: int,
        *,
        selected_key: str,
        note: str,
        decided_by: int | None,
    ) -> bool:
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE pm_decision SET status = 'answered', selected_key = ?, note = ?,"
                " decided_by = ?, decided_at = ?, auto_decided = 0 WHERE id = ? AND status = 'pending'",
                (selected_key, note, decided_by, now_iso(), decision_id),
            )
            return int(cursor.rowcount or 0) == 1

    def auto_answer(self, decision_id: int, *, selected_key: str, note: str) -> bool:
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE pm_decision SET status = 'auto_answered', selected_key = ?, note = ?,"
                " decided_at = ?, auto_decided = 1 WHERE id = ? AND status = 'pending'",
                (selected_key, note, now_iso(), decision_id),
            )
            return int(cursor.rowcount or 0) == 1

    def expire(self, decision_id: int) -> bool:
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE pm_decision SET status = 'expired' WHERE id = ? AND status = 'pending'",
                (decision_id,),
            )
            return int(cursor.rowcount or 0) == 1

    def void_for_task(self, task_id: int) -> int:
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE pm_decision SET status = 'void' WHERE task_id = ? AND status = 'pending'",
                (task_id,),
            )
            return int(cursor.rowcount or 0)

    def appeal(self, decision_id: int) -> bool:
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE pm_decision SET appeal_state = 'appealed' "
                "WHERE id = ? AND auto_decided = 1 AND appeal_state IS NULL",
                (decision_id,),
            )
            return int(cursor.rowcount or 0) == 1

    def resolve_appeal(self, decision_id: int, state: str) -> bool:
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE pm_decision SET appeal_state = ? WHERE id = ? AND appeal_state = 'appealed'",
                (state, decision_id),
            )
            return int(cursor.rowcount or 0) == 1

    def transfer(self, decision_id: int, assignee_user_id: int) -> bool:
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE pm_decision SET assignee_user_id = ? WHERE id = ? AND status = 'pending'",
                (assignee_user_id, decision_id),
            )
            return bool(cursor.rowcount)

    def pending_due(self, now: str | None = None) -> builtins.list[Decision]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pm_decision WHERE status = 'pending' AND timeout_at <= ? "
                "ORDER BY timeout_at",
                (now or now_iso(),),
            ).fetchall()
        return [Decision.from_row(row) for row in rows]

    def pending_for_task(self, task_id: int) -> builtins.list[Decision]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pm_decision WHERE task_id = ? AND status = 'pending' ORDER BY id",
                (task_id,),
            ).fetchall()
        return [Decision.from_row(row) for row in rows]

    def inbox_stats(
        self, *, user_id: int | None = None, now: str | None = None
    ) -> dict[str, object]:
        """Pending-card counts for the sidebar badge.

        Unassigned cards count as everyone's: a card nobody owns is exactly the
        one that expires unseen, so hiding it from the badge would defeat the
        badge's purpose.
        """
        stamp = now or now_iso()
        where = "status = 'pending'"
        params: builtins.list[object] = []
        if user_id is not None:
            where += " AND (assignee_user_id = ? OR assignee_user_id IS NULL)"
            params.append(user_id)
        with self._db.connect() as conn:
            rows = conn.execute(
                f"SELECT level, timeout_at FROM pm_decision WHERE {where}", params
            ).fetchall()
        # Pending cards are bounded by design (the sweeper retires them), so folding
        # in Python keeps one query shape working on both SQLite and PostgreSQL.
        by_level: dict[str, int] = {}
        pending = overdue = 0
        next_at: str | None = None
        for row in rows:
            level = str(cell(row, "level", 0) or "")
            when = cell(row, "timeout_at", 1)
            pending += 1
            by_level[level] = by_level.get(level, 0) + 1
            if isinstance(when, str):
                if when <= stamp:
                    overdue += 1
                if next_at is None or when < next_at:
                    next_at = when
        return {
            "pending": pending,
            "overdue": overdue,
            "by_level": by_level,
            "next_timeout_at": next_at,
        }

    def overturned_rate(self) -> tuple[int, int]:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT SUM(CASE WHEN appeal_state = 'reversed' THEN 1 ELSE 0 END) AS reversed,"
                " COUNT(*) AS total FROM pm_decision WHERE auto_decided = 1"
            ).fetchone()
        return int(cell(row, "reversed", 0) or 0), int(cell(row, "total", 1) or 0)
