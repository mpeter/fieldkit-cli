"""Ingest database helper for pipeline.db.

Provides:
  get_db_path()                  — canonical path for pipeline.db
  init_db()                      — create schema + seed pipeline registry rows; returns connection
  get_db()                       — open an existing pipeline.db; raises FileNotFoundError if absent
  get_db_read_only()             — open existing pipeline.db without creating files or changing pragmas
  ArtifactRecord                 — typed dataclass for artifact rows
  get_artifacts_for_reprocess()  — query artifacts by pipeline, optionally filtered by version
"""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from fieldkit.config import CONFIG_PATH, ConfigError, get_fieldkit_data, get_fieldkit_home
from fieldkit.config._loader import _read_config_dict
from fieldkit.errors import SQLiteSnapshotError, SQLiteSnapshotReason
from fieldkit.sqlite_read import open_sqlite_read_only


class ArtifactRow(Protocol):
    """Structural protocol for artifact row objects from the ingest database.

    Any object satisfying this protocol can be passed to functions that
    operate on artifact rows (e.g., ``_reprocess_one_artifact``).
    ``ArtifactRecord`` satisfies this protocol structurally.
    """

    artifact_id: str  # str, not int — matches schema column type
    source_id: str
    pipeline_version: str
    content_path: str


@dataclass
class ArtifactRecord:
    """Typed representation of a row from the artifacts table.

    Fields mirror the schema columns; ``pipeline_version`` is populated from
    the migration column added by ``init_db()``.
    """

    artifact_id: str
    source_id: str
    pipeline_id: str
    pipeline_version: str
    content_path: str
    created_at: str


def get_artifacts_for_reprocess(
    conn: sqlite3.Connection,
    pipeline_id: str,
    from_version: str | None = None,
    limit: int | None = None,
    account: str | None = None,
) -> list[ArtifactRecord]:
    """Return artifacts eligible for reprocessing for a given pipeline.

    Args:
        conn: Open database connection.
        pipeline_id: Pipeline to filter by (exact match on ``pipeline_id``).
        from_version: When set, only return artifacts whose ``pipeline_version``
            equals this value.  Useful for targeting a specific legacy version.
        limit: Maximum number of rows to return.  ``None`` means no limit.
        account: When set, only return artifacts written under
            ``accounts/<account>/``.  The artifacts table has no account column,
            but every artifact is written into its account's directory once
            routing has decided, so the account is recoverable from
            ``content_path`` after the fact.  Artifacts with no
            ``content_path`` are excluded by the account selector: an
            artifact that was never written to a vault path has no account.

    Returns:
        List of :class:`ArtifactRecord` ordered by ``created_at ASC``.
    """
    query = (
        "SELECT artifact_id, source_id, pipeline_id, "
        "COALESCE(pipeline_version, '0.1.0') AS pipeline_version, "
        "COALESCE(content_path, '') AS content_path, created_at "
        "FROM artifacts "
        "WHERE pipeline_id = ?"
    )
    params: list[object] = [pipeline_id]

    if from_version is not None:
        query += " AND COALESCE(pipeline_version, '0.1.0') = ?"
        params.append(from_version)

    if account is not None:
        # Match a literal, case-sensitive segment; SQL LIKE treats '_' as a wildcard.
        query += " AND instr(content_path, ?) > 0"
        params.append(f"/accounts/{account}/")

    query += " ORDER BY created_at ASC"

    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()
    return [
        ArtifactRecord(
            artifact_id=row["artifact_id"],
            source_id=row["source_id"],
            pipeline_id=row["pipeline_id"],
            pipeline_version=row["pipeline_version"],
            content_path=row["content_path"],
            created_at=row["created_at"],
        )
        for row in rows
    ]


def get_db_path() -> Path:
    """Return the canonical path for pipeline.db.

    Resolution order:
    1. ``pipeline_db`` key in config.yaml (explicit override).
    2. ``<fieldkit_data>/pipeline.db`` (default).

    """
    data = _read_config_dict(CONFIG_PATH)
    if data is not None and "pipeline_db" in data:
        raw = str(data["pipeline_db"]).strip()
        if not raw:
            raise ValueError("Config key 'pipeline_db' must not be empty or whitespace")
        resolved = Path(raw).expanduser().resolve()
        # Containment check: override path must be under an approved root.
        # ConfigError is caught here so the check degrades gracefully when
        # fieldkit_home/fieldkit_data are not yet configured (e.g. first run).
        try:
            approved_roots = [
                get_fieldkit_home(),
                get_fieldkit_data(),
                Path("~/.config/fieldkit").expanduser().resolve(),
            ]
            if not any(resolved == root or root in resolved.parents for root in approved_roots):
                raise ValueError(
                    f"Config key 'pipeline_db' path {resolved!r} is not under an approved root"
                    f" ({[str(r) for r in approved_roots]})"
                )
        except ConfigError:
            # fieldkit_home/fieldkit_data not yet configured (e.g. first run or test
            # environment without a full config). Skip the containment check rather
            # than enforcing a fallback root that would reject valid user-configured
            # paths. The containment check is a defence-in-depth measure, not a hard
            # security boundary — the user explicitly set this path in config.yaml.
            pass
        return resolved
    return get_fieldkit_data() / "pipeline.db"


def _schema_path() -> Path:
    """Return the path to schema.sql relative to this file."""
    return Path(__file__).resolve().parent / "schema.sql"


def _configure_write_connection(conn: sqlite3.Connection) -> None:
    """Require the rollback-journal mode supported by zero-mutation readers."""
    try:
        row = conn.execute("PRAGMA journal_mode=DELETE").fetchone()
    except sqlite3.OperationalError as exc:
        conn.close()
        reason: SQLiteSnapshotReason = (
            "active" if "locked" in str(exc).casefold() or "busy" in str(exc).casefold() else "unverified"
        )
        raise SQLiteSnapshotError("pipeline database journal mode could not be migrated safely", reason=reason) from exc
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise SQLiteSnapshotError(
            "pipeline database journal mode could not be migrated safely", reason="unverified"
        ) from exc
    if row is None or str(row[0]).casefold() != "delete":
        conn.close()
        raise SQLiteSnapshotError("pipeline database journal mode is active", reason="active")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout = 10000")


def init_db(
    db_path: Path | None = None,
    pipelines: Sequence[Any] = (),
) -> sqlite3.Connection:
    """Create (or reset) pipeline.db and seed the pipeline registry.

    Args:
        db_path:   Override path for the database file. Defaults to get_db_path().
        pipelines: Sequence of pipeline specifications to seed. The caller
                   supplies the registry so this database adapter does not
                   depend on command modules. An empty sequence seeds no rows.

    Returns:
        An open sqlite3 connection with rollback journaling and foreign keys enabled.
    """
    resolved = db_path if db_path is not None else get_db_path()
    resolved.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(resolved))
    conn.row_factory = sqlite3.Row

    _configure_write_connection(conn)

    schema_sql = _schema_path().read_text(encoding="utf-8")
    conn.executescript(schema_sql)

    # Schema migration: add pipeline_version column to artifacts (idempotent).
    # Needed by S05 reprocess --from-version to filter artifacts by pipeline version.
    try:
        conn.execute("ALTER TABLE artifacts ADD COLUMN pipeline_version TEXT NOT NULL DEFAULT '0.1.0'")
        conn.commit()
    except sqlite3.OperationalError as exc:
        if str(exc) != "duplicate column name: pipeline_version":
            conn.close()
            raise

    for spec in pipelines:
        conn.execute(
            """
            INSERT OR IGNORE INTO pipelines
                (pipeline_id, description, version, source_format, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (spec.pipeline_id, spec.description, spec.version, spec.source_format, spec.status),
        )
    conn.commit()

    return conn


def get_db(db_path: Path | None = None) -> sqlite3.Connection:
    """Open an existing pipeline.db for reading/writing.

    Args:
        db_path: Override path for the database file. Defaults to get_db_path().

    Returns:
        An open sqlite3 connection.

    Raises:
        FileNotFoundError: If the database file does not exist.
    """
    resolved = db_path if db_path is not None else get_db_path()
    if not resolved.exists():
        raise FileNotFoundError(f"pipeline.db not found at {resolved}. Run init_db() first.")

    conn = sqlite3.connect(str(resolved))
    conn.row_factory = sqlite3.Row

    _configure_write_connection(conn)

    return conn


def get_db_read_only(db_path: Path | None = None) -> sqlite3.Connection:
    """Open an existing pipeline database without creating files or changing pragmas."""
    resolved = db_path if db_path is not None else get_db_path()
    return open_sqlite_read_only(resolved, timeout_seconds=10.0)
