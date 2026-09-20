"""Unit tests for lib/ingest_db.py — DB init, registry seeding, and path resolution.

Pattern: use tmp_path SQLite file (not :memory:) so file-existence checks work.
"""

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

import fieldkit.ingest.db as ingest_db_mod
from fieldkit.commands.ingest.registry import PIPELINES
from fieldkit.ingest.db import (
    ArtifactRecord,
    get_artifacts_for_reprocess,
    get_db,
    get_db_path,
    init_db,
    update_artifact_version,
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


def test_get_db_error_message_contains_path(tmp_path: Path) -> None:
    """FileNotFoundError message must contain the missing path."""
    db_path = tmp_path / "pipeline.db"
    with pytest.raises(FileNotFoundError, match=str(db_path)):
        get_db(db_path)


# ── ArtifactRecord / get_artifacts_for_reprocess / update_artifact_version ─


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


def test_update_artifact_version_changes_version_and_path(tmp_path: Path) -> None:
    """update_artifact_version must persist the new version and content_path."""
    conn = init_db(tmp_path / "pipeline.db")
    _seed_artifact(conn, "art-upd", pipeline_version="0.1.0", content_path="/old/path.md")

    update_artifact_version(conn, "art-upd", "0.2.0", "/new/path.md")

    row = conn.execute(
        "SELECT pipeline_version, content_path FROM artifacts WHERE artifact_id = ?",
        ("art-upd",),
    ).fetchone()
    conn.close()

    assert row["pipeline_version"] == "0.2.0"
    assert row["content_path"] == "/new/path.md"


def test_update_artifact_version_is_committed(tmp_path: Path) -> None:
    """Changes from update_artifact_version must be visible on a fresh connection."""
    db_path = tmp_path / "pipeline.db"
    conn = init_db(db_path)
    _seed_artifact(conn, "art-commit", pipeline_version="0.1.0", content_path="/before.md")
    update_artifact_version(conn, "art-commit", "0.3.0", "/after.md")
    conn.close()

    conn2 = get_db(db_path)
    row = conn2.execute(
        "SELECT pipeline_version, content_path FROM artifacts WHERE artifact_id = ?",
        ("art-commit",),
    ).fetchone()
    conn2.close()

    assert row["pipeline_version"] == "0.3.0"
    assert row["content_path"] == "/after.md"


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
