"""PM schema migrations — an independent version namespace.

The host owns ``_schema_version`` with three-digit numbers (``024``). PM must
never share that sequence: an upstream release that adds ``025`` would collide
with a PM migration and force a renumber on every deployed instance (this is
exactly the failure recorded in the reference build guide).

So PM keeps ``pm_schema_version`` and four-digit ``pm_NNNN_*.sql`` files, applied
by :func:`run_pm_migrations` from ``OctopServer.start`` right after the host's
``run_migrations``. Failures are logged, never raised into host boot: a PM bug
must not keep Octop from starting.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from octop.infra.db.pool import DatabasePool

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_VERSION_TABLE = "pm_schema_version"
_FILE_RE = re.compile(r"^pm_(\d{4})_.*\.sql$")
_STMT_RE = re.compile(r";\s*\n")


class PmMigrationError(RuntimeError):
    """Raised when a PM migration is missing, tampered with, or fails to apply."""


@dataclass(frozen=True)
class AppliedMigration:
    version: int
    name: str
    checksum: str
    applied_at: str


def _discover(dialect: str) -> list[tuple[int, Path]]:
    if not MIGRATIONS_DIR.exists():
        return []
    found: list[tuple[int, Path]] = []
    for entry in sorted(MIGRATIONS_DIR.iterdir()):
        name = entry.name
        if not name.endswith(".sql"):
            continue
        is_pg = name.endswith(".pg.sql")
        if dialect == "postgresql":
            if not is_pg:
                continue
        elif is_pg:
            continue
        match = _FILE_RE.match(name)
        if match:
            found.append((int(match.group(1)), entry))
    seen: dict[int, Path] = {}
    for version, path in found:
        prior = seen.get(version)
        if prior is not None:
            raise PmMigrationError(
                f"duplicate PM migration version {version:04d}: {prior.name} and {path.name}"
            )
        seen[version] = path
    return sorted(found, key=lambda item: item[0])


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _cell(row: Any, key: str, index: int = 0) -> Any:
    if isinstance(row, Mapping):
        return row[key]
    return row[index]


def _ensure_version_table(db: DatabasePool) -> None:
    # Identical DDL on both dialects: INTEGER PK, TEXT timestamps.
    ddl = (
        f"CREATE TABLE IF NOT EXISTS {_VERSION_TABLE} ("
        "version INTEGER PRIMARY KEY,"
        "name TEXT NOT NULL,"
        "checksum TEXT NOT NULL,"
        "applied_at TEXT NOT NULL)"
    )
    with db.connect() as conn:
        conn.execute(ddl)


def applied_migrations(db: DatabasePool) -> list[AppliedMigration]:
    _ensure_version_table(db)
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT version, name, checksum, applied_at FROM {_VERSION_TABLE} ORDER BY version"
        ).fetchall()
    return [
        AppliedMigration(
            version=int(_cell(row, "version")),
            name=str(_cell(row, "name", 1)),
            checksum=str(_cell(row, "checksum", 2)),
            applied_at=str(_cell(row, "applied_at", 3)),
        )
        for row in rows
    ]


def current_version(db: DatabasePool) -> int:
    rows = applied_migrations(db)
    return rows[-1].version if rows else 0


def _split_pg_sql(sql: str) -> list[str]:
    parts = [chunk.strip() for chunk in _STMT_RE.split(sql)]
    statements: list[str] = []
    for part in parts:
        if not part:
            continue
        lines = part.splitlines()
        while lines and (not lines[0].strip() or lines[0].lstrip().startswith("--")):
            lines.pop(0)
        cleaned = "\n".join(lines).strip()
        if cleaned:
            statements.append(cleaned)
    return statements


def _apply(db: DatabasePool, path: Path) -> None:
    sql = path.read_text(encoding="utf-8")
    if db.dialect == "postgresql":
        with db.connect() as conn, conn.transaction():
            for statement in _split_pg_sql(sql):
                conn.execute(statement)
        return
    with db.connect() as conn:
        conn.executescript(sql)


def _record(db: DatabasePool, version: int, path: Path, checksum: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with db.transaction() as conn:
        conn.execute(
            f"INSERT INTO {_VERSION_TABLE}(version, name, checksum, applied_at) "
            "VALUES (?, ?, ?, ?)",
            (version, path.name, checksum, stamp),
        )


def run_pm_migrations(db: DatabasePool) -> int:
    """Apply every pending PM migration in order. Returns the resulting version.

    Idempotent: applied versions are skipped, but a changed checksum on an
    already-applied file is a hard error — silently re-running edited SQL on a
    live instance is how schemas drift between hosts.
    """
    _ensure_version_table(db)
    known = {row.version: row for row in applied_migrations(db)}
    for version, path in _discover(db.dialect):
        digest = _checksum(path)
        prior = known.get(version)
        if prior is not None:
            if prior.checksum != digest:
                raise PmMigrationError(
                    f"pm migration {path.name} changed after it was applied "
                    f"(recorded {prior.checksum[:12]}, file {digest[:12]})"
                )
            continue
        logger.info("applying PM migration %s", path.name)
        _apply(db, path)
        _record(db, version, path, digest)
    return current_version(db)


def migration_health(db: DatabasePool) -> dict[str, Any]:
    """Cheap status snapshot for ``GET /api/pm/health``."""
    try:
        rows = applied_migrations(db)
        expected = [version for version, _ in _discover(db.dialect)]
        applied = [row.version for row in rows]
        return {
            "ok": applied == expected[: len(applied)] and set(applied) <= set(expected),
            "version": applied[-1] if applied else 0,
            "applied": applied,
            "expected": expected,
            "pending": [version for version in expected if version not in applied],
        }
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "error": str(exc), "version": 0, "pending": []}
