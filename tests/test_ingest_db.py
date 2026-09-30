"""Unit tests for lib/ingest_db.py — DB init, registry seeding, and path resolution.

Pattern: use tmp_path SQLite file (not :memory:) so file-existence checks work.
"""

import sqlite3
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import fieldkit.ingest.db as ingest_db_mod
from fieldkit.commands.ingest.registry import PIPELINES
from fieldkit.errors import SQLiteSnapshotError
from fieldkit.ingest.db import (
    ArtifactRecord,
    get_artifacts_for_reprocess,
    get_db,
    get_db_path,
    get_db_read_only,
    init_db,
)

pytestmark = pytest.mark.unit


# ── get_db_path ────────────────────────────────────────────────────────────


def test_get_db_path_ends_with_pipeline_db() -> None:
    """Canonical path must end with data/pipeline.db."""
    p = get_db_path()
    assert p.name == "pipeline.db"
    assert p.parent.name == "data"


def test_get_db_path_returns_path_object() -> None:
    """get_db_path() must return an absolute pathlib.Path."""
    result = get_db_path()
    assert isinstance(result, Path)
    assert result.is_absolute(), "get_db_path must return an absolute Path"


# ── init_db ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("message", ["database is locked", "disk I/O error", "no such table: artifacts"])
def test_init_db_propagates_migration_failure(tmp_path: Path, message: str) -> None:
    """An operational migration failure must not return a usable-looking connection."""
    connection = MagicMock(spec=sqlite3.Connection)
    journal_cursor = MagicMock()
    journal_cursor.fetchone.return_value = ("delete",)
    connection.execute.side_effect = [journal_cursor, None, None, None, sqlite3.OperationalError(message)]

    with (
        patch("fieldkit.ingest.db.sqlite3.connect", return_value=connection),
        pytest.raises(sqlite3.OperationalError, match=message),
    ):
        init_db(tmp_path / "pipeline.db")

    connection.close.assert_called_once()
    connection.commit.assert_not_called()


def test_init_db_creates_file(tmp_path: Path) -> None:
    """init_db() must create the SQLite file at the given path."""
    db_path = tmp_path / "pipeline.db"
    assert not db_path.exists()
    conn = init_db(db_path)
    conn.close()
    assert db_path.exists()


def test_init_db_creates_all_four_tables(tmp_path: Path) -> None:
    """All four schema tables must be present after init_db()."""
    db_path = tmp_path / "pipeline.db"
    conn = init_db(db_path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.close()
    assert {"pipelines", "sources", "artifacts", "checkpoints"} <= tables


def test_init_db_creates_all_four_indexes(tmp_path: Path) -> None:
    """All four schema indexes must be present after init_db()."""
    db_path = tmp_path / "pipeline.db"
    conn = init_db(db_path)
    indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    conn.close()
    expected = {
        "idx_sources_pipeline",
        "idx_sources_status",
        "idx_artifacts_source",
        "idx_artifacts_pipeline",
    }
    assert expected <= indexes


def test_init_db_seeds_all_pipeline_rows(tmp_path: Path) -> None:
    """init_db() must seed every row from the registry."""
    db_path = tmp_path / "pipeline.db"
    conn = init_db(db_path, pipelines=PIPELINES)
    count = conn.execute("SELECT COUNT(*) FROM pipelines").fetchone()[0]
    conn.close()
    assert count == 4


def test_init_db_seeds_expected_pipeline_ids(tmp_path: Path) -> None:
    """The seeded pipeline_ids must match the registry entries."""
    db_path = tmp_path / "pipeline.db"
    conn = init_db(db_path, pipelines=PIPELINES)
    ids = {row[0] for row in conn.execute("SELECT pipeline_id FROM pipelines").fetchall()}
    conn.close()
    assert ids == {
        "ambient-transcript-ingest",
        "transcript-ingest",
        "customer-notes-ingest",
        "pm-status-ingest",
    }


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    """Calling init_db() twice must not raise and must not duplicate rows."""
    db_path = tmp_path / "pipeline.db"
    conn1 = init_db(db_path, pipelines=PIPELINES)
    conn1.close()
    conn2 = init_db(db_path, pipelines=PIPELINES)
    count = conn2.execute("SELECT COUNT(*) FROM pipelines").fetchone()[0]
    conn2.close()
    assert count == 4


def test_init_db_returns_open_connection(tmp_path: Path) -> None:
    """init_db() must return an open, usable sqlite3.Connection."""
    db_path = tmp_path / "pipeline.db"
    conn = init_db(db_path)
    # A simple query must not raise
    result = conn.execute("SELECT 1").fetchone()
    conn.close()
    assert result is not None


def test_init_db_creates_parent_dirs(tmp_path: Path) -> None:
    """init_db() must create parent directories that don't yet exist."""
    db_path = tmp_path / "nested" / "dirs" / "pipeline.db"
    assert not db_path.parent.exists()
    conn = init_db(db_path)
    conn.close()
    assert db_path.exists()


# ── get_db ─────────────────────────────────────────────────────────────────


def test_get_db_raises_on_nonexistent_path(tmp_path: Path) -> None:
    """get_db() must raise FileNotFoundError when the DB file is absent."""
    db_path = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError) as exc_info:
        get_db(db_path)
    assert exc_info.type is FileNotFoundError


def test_get_db_opens_existing_db(tmp_path: Path) -> None:
    """get_db() must return an open connection to an existing DB."""
    db_path = tmp_path / "pipeline.db"
    conn = init_db(db_path, pipelines=PIPELINES)
    conn.close()

    conn2 = get_db(db_path)
    count = conn2.execute("SELECT COUNT(*) FROM pipelines").fetchone()[0]
    assert isinstance(conn2, sqlite3.Connection), "get_db must return an sqlite3.Connection"
    assert conn2.row_factory is sqlite3.Row, "get_db must set row_factory = sqlite3.Row"
    conn2.close()
    assert count == 4


def test_get_db_migrates_a_closed_wal_database_through_sqlite(tmp_path: Path) -> None:
    """An authorized write open converts legacy WAL state without losing rows."""
    db_path = tmp_path / "pipeline.db"
    legacy = sqlite3.connect(db_path)
    assert legacy.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    legacy.execute("CREATE TABLE retained(value TEXT NOT NULL)")
    legacy.execute("INSERT INTO retained VALUES ('kept')")
    legacy.commit()
    legacy.close()
    assert tuple(db_path.read_bytes()[18:20]) == (2, 2)

    migrated = get_db(db_path)
    try:
        assert migrated.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert migrated.execute("SELECT value FROM retained").fetchone()[0] == "kept"
    finally:
        migrated.close()

    assert tuple(db_path.read_bytes()[18:20]) == (1, 1)


def test_get_db_refuses_wal_migration_while_another_writer_is_active(tmp_path: Path) -> None:
    db_path = tmp_path / "pipeline.db"
    active = sqlite3.connect(db_path)
    assert active.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    active.execute("CREATE TABLE retained(value TEXT NOT NULL)")
    active.commit()
    active.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(SQLiteSnapshotError) as caught:
            get_db(db_path)
        assert caught.value.reason == "active"
    finally:
        active.rollback()
        active.close()


def test_get_db_rejects_malformed_database_with_fixed_typed_error(tmp_path: Path) -> None:
    db_path = tmp_path / "fictional-private-customer.db"
    db_path.write_bytes(b"not a sqlite database")

    with pytest.raises(SQLiteSnapshotError) as caught:
        get_db(db_path)

    assert caught.value.reason == "unverified"
    assert str(db_path) not in str(caught.value)


def test_delete_journal_writers_serialize_and_preserve_both_commits(tmp_path: Path) -> None:
    db_path = tmp_path / "pipeline.db"
    first = init_db(db_path, pipelines=PIPELINES)
    first.execute("BEGIN IMMEDIATE")
    first.execute("UPDATE pipelines SET description = 'first' WHERE pipeline_id = 'transcript-ingest'")
    attempted = threading.Event()
    finished = threading.Event()
    failures: list[BaseException] = []

    def second_writer() -> None:
        attempted.set()
        try:
            second = get_db(db_path)
            try:
                second.execute("UPDATE pipelines SET version = '2.0.0' WHERE pipeline_id = 'transcript-ingest'")
                second.commit()
            finally:
                second.close()
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=second_writer)
    worker.start()
    assert attempted.wait(timeout=1)
    assert not finished.wait(timeout=0.05)
    first.commit()
    first.close()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert failures == []
    reader = get_db(db_path)
    try:
        row = reader.execute(
            "SELECT description, version FROM pipelines WHERE pipeline_id = 'transcript-ingest'"
        ).fetchone()
        assert tuple(row) == ("first", "2.0.0")
    finally:
        reader.close()


def test_get_db_error_message_contains_path(tmp_path: Path) -> None:
    """FileNotFoundError message must contain the missing path."""
    db_path = tmp_path / "pipeline.db"
    with pytest.raises(FileNotFoundError, match=str(db_path)):
        get_db(db_path)


def test_get_db_read_only_does_not_create_sidecars_or_mutate_database(tmp_path: Path) -> None:
    db_path = tmp_path / "pipeline.db"
    conn = init_db(db_path, pipelines=PIPELINES)
    conn.close()
    for sidecar in (tmp_path / "pipeline.db-wal", tmp_path / "pipeline.db-shm"):
        if sidecar.exists():
            sidecar.unlink()
    before = (db_path.read_bytes(), db_path.stat().st_mtime_ns, {p.name for p in tmp_path.iterdir()})

    reader = get_db_read_only(db_path)
    try:
        count = reader.execute("SELECT COUNT(*) FROM pipelines").fetchone()[0]
        assert count == len(PIPELINES)
    finally:
        reader.close()

    after = (db_path.read_bytes(), db_path.stat().st_mtime_ns, {p.name for p in tmp_path.iterdir()})
    assert after == before


def test_get_db_read_only_refuses_an_active_delete_journal_writer(tmp_path: Path) -> None:
    db_path = tmp_path / "pipeline.db"
    writer = init_db(db_path, pipelines=PIPELINES)
    writer.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(SQLiteSnapshotError) as caught:
            get_db_read_only(db_path)
        assert caught.value.reason == "active"
    finally:
        writer.rollback()
        writer.close()


def test_get_db_read_only_missing_database_does_not_create_it(tmp_path: Path) -> None:
    db_path = tmp_path / "missing.db"

    with pytest.raises(FileNotFoundError):
        get_db_read_only(db_path)

    assert not db_path.exists()


# ── ArtifactRecord / get_artifacts_for_reprocess ─


def _seed_artifact(
    conn: sqlite3.Connection,
    artifact_id: str,
    pipeline_id: str = "transcript-ingest",
    pipeline_version: str = "0.1.0",
    content_path: str = "/vault/meeting.md",
    source_id: str = "src-001",
) -> None:
    """Insert a minimal pipeline + source + artifact row for testing."""
    # Ensure the FK-referenced pipeline row exists (idempotent).
    conn.execute(
        """
        INSERT OR IGNORE INTO pipelines
            (pipeline_id, description, version, source_format, status)
        VALUES (?, '', '0.1.0', 'text', 'active')
        """,
        (pipeline_id,),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO sources (source_id, pipeline_id, file_path, status)
        VALUES (?, ?, '/tmp/raw.txt', 'processed')
        """,
        (source_id, pipeline_id),
    )
    conn.execute(
        """
        INSERT INTO artifacts
            (artifact_id, source_id, pipeline_id, artifact_type,
             pipeline_version, content_path, created_at)
        VALUES (?, ?, ?, 'meeting-note', ?, ?, datetime('now'))
        """,
        (artifact_id, source_id, pipeline_id, pipeline_version, content_path),
    )
    conn.commit()


@pytest.mark.parametrize("other", ["acmeXcorp", "ACME_CORP", "acme_corp-extra"])
def test_reprocess_account_selection_is_literal(tmp_path: Path, other: str) -> None:
    conn = init_db(tmp_path / "pipeline.db")
    try:
        _seed_artifact(conn, "selected", content_path="/workspace/accounts/acme_corp/meetings/note.md")
        _seed_artifact(conn, "excluded", content_path=f"/workspace/accounts/{other}/meetings/note.md")
        result = get_artifacts_for_reprocess(conn, "transcript-ingest", account="acme_corp")
        assert len(result) == 1
        assert result[0].artifact_id == "selected"
    finally:
        conn.close()


def test_get_artifacts_for_reprocess_empty_db(tmp_path: Path) -> None:
    """Empty artifacts table must return an empty list."""
    conn = init_db(tmp_path / "pipeline.db")
    results = get_artifacts_for_reprocess(conn, "transcript-ingest")
    conn.close()
    assert results == []


def test_get_artifacts_for_reprocess_returns_artifact_record(tmp_path: Path) -> None:
    """Seeded row must be returned as a correctly populated ArtifactRecord."""
    conn = init_db(tmp_path / "pipeline.db")
    _seed_artifact(conn, "art-001", pipeline_version="0.1.0", content_path="/vault/a.md")

    results = get_artifacts_for_reprocess(conn, "transcript-ingest")
    conn.close()

    assert len(results) == 1
    rec = results[0]
    assert isinstance(rec, ArtifactRecord)
    assert rec.artifact_id == "art-001"
    assert rec.source_id == "src-001"
    assert rec.pipeline_id == "transcript-ingest"
    assert rec.pipeline_version == "0.1.0"
    assert rec.content_path == "/vault/a.md"
    assert rec.created_at  # non-empty timestamp


def test_get_artifacts_for_reprocess_filters_by_pipeline(tmp_path: Path) -> None:
    """Only artifacts matching pipeline_id must be returned."""
    conn = init_db(tmp_path / "pipeline.db")
    _seed_artifact(conn, "art-t", pipeline_id="transcript-ingest", source_id="src-t")
    _seed_artifact(conn, "art-n", pipeline_id="customer-notes-ingest", source_id="src-n")

    results = get_artifacts_for_reprocess(conn, "transcript-ingest")
    conn.close()

    assert len(results) == 1
    assert results[0].artifact_id == "art-t"


def test_get_artifacts_for_reprocess_from_version_filter(tmp_path: Path) -> None:
    """from_version must exclude artifacts at a different version."""
    conn = init_db(tmp_path / "pipeline.db")
    _seed_artifact(conn, "art-old", pipeline_version="0.1.0", source_id="src-old")
    _seed_artifact(conn, "art-new", pipeline_version="0.2.0", source_id="src-new")

    results = get_artifacts_for_reprocess(conn, "transcript-ingest", from_version="0.1.0")
    conn.close()

    assert len(results) == 1
    assert results[0].artifact_id == "art-old"
    assert results[0].pipeline_version == "0.1.0"


def test_get_artifacts_for_reprocess_limit(tmp_path: Path) -> None:
    """limit parameter must cap the number of returned rows."""
    conn = init_db(tmp_path / "pipeline.db")
    for i in range(5):
        _seed_artifact(conn, f"art-{i:03d}", source_id=f"src-{i:03d}")

    results = get_artifacts_for_reprocess(conn, "transcript-ingest", limit=3)
    conn.close()

    assert len(results) == 3


# ── get_db_path path-is-not-__file__-relative regression ──────────────────


def _package_path() -> Path:
    """Return the directory that contains lib/ingest_db.py."""
    return Path(ingest_db_mod.__file__).resolve().parent


def test_get_db_path_under_custom_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """get_db_path() must return a path inside the monkeypatched data root.

    This is the core regression guard: before T02 the path was inferred from
    __file__, so a uv tool install would point at the package directory.
    """
    monkeypatch.setattr(ingest_db_mod, "get_fieldkit_data", lambda: tmp_path)
    monkeypatch.setattr(ingest_db_mod, "CONFIG_PATH", tmp_path / "no-config.yaml")

    path = get_db_path()

    assert isinstance(path, Path)
    assert path.is_relative_to(tmp_path), f"Expected get_db_path() to be under {tmp_path}, got {path}"


def test_get_db_path_not_under_package_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """get_db_path() must NOT return a path under the package installation directory.

    Guards against regression to __file__-relative path resolution.
    """
    monkeypatch.setattr(ingest_db_mod, "get_fieldkit_data", lambda: tmp_path)
    monkeypatch.setattr(ingest_db_mod, "CONFIG_PATH", tmp_path / "no-config.yaml")

    path = get_db_path()
    pkg_dir = _package_path()

    assert not path.is_relative_to(pkg_dir), f"get_db_path() returned a path under the package dir {pkg_dir}: {path}"


def test_get_db_path_ends_with_data_pipeline_db_under_custom_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_db_path() must produce <data_root>/pipeline.db for any injected root."""
    data_root = tmp_path / "custom-data-root"
    monkeypatch.setattr(ingest_db_mod, "get_fieldkit_data", lambda: data_root)
    monkeypatch.setattr(ingest_db_mod, "CONFIG_PATH", tmp_path / "no-config.yaml")

    path = get_db_path()

    assert path == data_root / "pipeline.db"


def test_get_db_path_uses_pipeline_db_override_when_present(tmp_path: Path) -> None:
    """pipeline_db config key overrides the default path."""
    import fieldkit.ingest.db as ingest_db_mod

    override = tmp_path / "custom.db"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"fieldkit_home: {tmp_path}\nfieldkit_data: {tmp_path}/data\npipeline_db: {override}\n",
        encoding="utf-8",
    )
    with patch.object(ingest_db_mod, "CONFIG_PATH", cfg), patch("fieldkit.config._loader.CONFIG_PATH", cfg):
        result = get_db_path()
    assert result == override.resolve()


def test_get_db_path_falls_back_to_fieldkit_data_when_override_absent(tmp_path: Path) -> None:
    """When pipeline_db key absent, falls back to get_fieldkit_data() / 'pipeline.db'."""
    import fieldkit.ingest.db as ingest_db_mod

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"fieldkit_home: {tmp_path}\nfieldkit_data: {tmp_path}/data\n",
        encoding="utf-8",
    )
    with patch.object(ingest_db_mod, "CONFIG_PATH", cfg), patch("fieldkit.config._loader.CONFIG_PATH", cfg):
        result = get_db_path()
    assert result == (tmp_path / "data" / "pipeline.db").resolve()


def test_get_db_path_rejects_override_outside_approved_roots(tmp_path: Path) -> None:
    """pipeline_db override outside approved roots raises ValueError."""
    import fieldkit.ingest.db as ingest_db_mod

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"fieldkit_home: {tmp_path}\nfieldkit_data: {tmp_path}/data\npipeline_db: /tmp/evil.db\n",
        encoding="utf-8",
    )
    with (
        patch.object(ingest_db_mod, "CONFIG_PATH", cfg),
        patch("fieldkit.config._loader.CONFIG_PATH", cfg),
        pytest.raises(ValueError, match="not under an approved root"),
    ):
        get_db_path()


@pytest.mark.parametrize("bad_value", ["", "   "])
def test_get_db_path_raises_when_pipeline_db_override_is_empty_or_whitespace(tmp_path: Path, bad_value: str) -> None:
    """Empty or whitespace pipeline_db override raises ValueError."""
    import fieldkit.ingest.db as ingest_db_mod

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"fieldkit_home: {tmp_path}\nfieldkit_data: {tmp_path}/data\npipeline_db: {bad_value!r}\n",
        encoding="utf-8",
    )
    with (
        patch.object(ingest_db_mod, "CONFIG_PATH", cfg),
        patch("fieldkit.config._loader.CONFIG_PATH", cfg),
        pytest.raises(ValueError, match="must not be empty or whitespace"),
    ):
        get_db_path()
