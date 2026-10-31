"""PM migration runner: independent version table, idempotency, tamper detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from octop.infra.db.pool import SqlitePool
from octop.infra.pm import migrate
from octop.infra.pm.domain import stages

EXPECTED_TABLES = {
    "pm_workspace",
    "pm_requirement",
    "pm_requirement_change",
    "pm_task",
    "pm_step_handler",
    "pm_task_step",
    "pm_artifact",
    "pm_conclusion",
    "pm_evidence",
    "pm_conclusion_evidence",
    "pm_decision",
    "pm_review",
    "pm_integration",
    "pm_metric_daily",
    "pm_eval_case",
}


def _tables(db: SqlitePool) -> set[str]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'pm_%'"
        ).fetchall()
    return {str(row["name"]) for row in rows}


def test_applies_all_migrations(tmp_path: Path) -> None:
    db = SqlitePool(tmp_path / "pm.db")
    assert migrate.run_pm_migrations(db) == 2
    assert EXPECTED_TABLES.issubset(_tables(db))
    assert "pm_schema_version" in _tables(db)
    db.close()


def test_second_run_is_a_noop(tmp_path: Path) -> None:
    db = SqlitePool(tmp_path / "pm.db")
    migrate.run_pm_migrations(db)
    first = migrate.applied_migrations(db)
    assert migrate.run_pm_migrations(db) == 2
    assert [row.checksum for row in migrate.applied_migrations(db)] == [
        row.checksum for row in first
    ]
    db.close()


def test_does_not_touch_host_schema_version(tmp_path: Path) -> None:
    db = SqlitePool(tmp_path / "pm.db")
    migrate.run_pm_migrations(db)
    assert _tables(db).isdisjoint({"_schema_version"})
    db.close()


def test_tampered_checksum_fails_fast(tmp_path: Path) -> None:
    db = SqlitePool(tmp_path / "pm.db")
    migrate.run_pm_migrations(db)
    with db.transaction() as conn:
        conn.execute("UPDATE pm_schema_version SET checksum = 'deadbeef' WHERE version = 2")
    with pytest.raises(migrate.PmMigrationError):
        migrate.run_pm_migrations(db)
    db.close()


def test_health_reports_applied_and_pending(tmp_path: Path) -> None:
    db = SqlitePool(tmp_path / "pm.db")
    migrate.run_pm_migrations(db)
    health = migrate.migration_health(db)
    assert health["ok"] is True
    assert health["version"] == 2
    assert health["pending"] == []
    db.close()


def test_every_migration_has_a_postgres_pair() -> None:
    directory = migrate.MIGRATIONS_DIR
    files = sorted(path.name for path in directory.iterdir() if path.suffix == ".sql")
    sqlite_files = [name for name in files if not name.endswith(".pg.sql")]
    pg_files = [name for name in files if name.endswith(".pg.sql")]
    for name in sqlite_files:
        assert f"{name[:-4]}.pg.sql" in pg_files, f"missing PostgreSQL pair for {name}"


def test_seed_matches_the_stage_source(db: SqlitePool) -> None:
    """The DB seed must not drift from ``domain.stages`` — the canvas-gap lesson."""
    migrate.run_pm_migrations(db)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT seq, step_key, handler_id, decision_level, decision_timeout_s"
            " FROM pm_step_handler ORDER BY seq"
        ).fetchall()
    assert len(rows) == len(stages.STEPS)
    for row, spec in zip(rows, stages.STEPS, strict=True):
        assert row["seq"] == spec.seq
        assert row["step_key"] == spec.step_key
        assert row["handler_id"] == spec.handler_id
        assert row["decision_level"] == spec.default_level
        assert row["decision_timeout_s"] == spec.decision_timeout_s


def test_step_stage_mapping_is_complete() -> None:
    assert len(stages.STEPS) == 13
    for step in stages.STEPS:
        assert stages.stage_of(step.step_key) == step.stage
        assert stages.step_by_key(step.step_key) is step
    assert stages.stage_of("nope") is None
