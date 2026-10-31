"""Three-perspective reviews. P5 cannot pass until all three are in with a verdict."""

from __future__ import annotations

from octop.infra.db.pool import DatabasePool
from octop.infra.pm.models import Review
from octop.infra.pm.repos._base import insert_id, now_iso

PERSPECTIVES: tuple[str, ...] = ("business", "tech", "customer")


class ReviewRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def submit(
        self,
        *,
        task_id: int,
        perspective: str,
        verdict: str,
        comment: str | None = None,
        reviewer_user_id: int | None = None,
        artifact_id: int | None = None,
    ) -> Review:
        """One row per (task, perspective) — a later verdict replaces the earlier one."""
        with self._db.transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM pm_review WHERE task_id = ? AND perspective = ?",
                (task_id, perspective),
            ).fetchone()
            if existing is None:
                new_id = insert_id(
                    conn,
                    "INSERT INTO pm_review("
                    "task_id, artifact_id, perspective, verdict, comment, reviewer_user_id, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        task_id,
                        artifact_id,
                        perspective,
                        verdict,
                        comment,
                        reviewer_user_id,
                        now_iso(),
                    ),
                )
            else:
                new_id = int(existing["id"])
                conn.execute(
                    "UPDATE pm_review SET verdict = ?, comment = ?, artifact_id = ?,"
                    " reviewer_user_id = ?, created_at = ? WHERE id = ?",
                    (verdict, comment, artifact_id, reviewer_user_id, now_iso(), new_id),
                )
            row = conn.execute("SELECT * FROM pm_review WHERE id = ?", (new_id,)).fetchone()
        assert row is not None
        return Review.from_row(row)

    def list_for_task(self, task_id: int) -> list[Review]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pm_review WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
        return [Review.from_row(row) for row in rows]

    def verdict_map(self, task_id: int) -> dict[str, str]:
        return {review.perspective: review.verdict for review in self.list_for_task(task_id)}
