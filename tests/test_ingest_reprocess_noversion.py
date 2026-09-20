"""Tests for historic regression: reprocess without --from-version warns and exits 1.

Spec: openspec/changes/ingest-correctness/specs/ingest-ux-correctness/spec.md
  - Scenario: No --from-version on live run emits warning and exits 1
  - Scenario: No --from-version on dry-run also emits warning
  - Scenario: --from-version present — happy path unchanged
"""

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.commands.ingest.registry import PIPELINES
from fieldkit.ingest.db import init_db

pytestmark = pytest.mark.unit


def _seed_artifact(
    conn: sqlite3.Connection,
    *,
    artifact_id: str = "art-001",
    source_id: str = "src-001",
    pipeline_version: str = "0.1.0",
    content_path: str = "/vault/meeting.md",
) -> None:
    """Insert a minimal source + artifact row for testing."""
    conn.execute(
        """
        INSERT OR IGNORE INTO sources (source_id, pipeline_id, file_path, status)
        VALUES (?, 'transcript-ingest', '/tmp/raw.txt', 'processed')
        """,
        (source_id,),
    )
    conn.execute(
        """
        INSERT INTO artifacts
            (artifact_id, source_id, pipeline_id, artifact_type,
             pipeline_version, content_path, created_at)
        VALUES (?, ?, 'transcript-ingest', 'meeting-note', ?, ?, datetime('now'))
        """,
        (artifact_id, source_id, pipeline_version, content_path),
    )
    conn.commit()


# ── TestReprocessNoVersionLiveRun (flattened) ───────────────────────────────


def test_reprocess_no_version_live_run_exit_code_is_1(tmp_path: Path) -> None:
    """Without --from-version the command must exit 1 (not silently succeed)."""
    pipeline_db = tmp_path / "pipeline.db"
    conn = init_db(pipeline_db, pipelines=PIPELINES)
    _seed_artifact(conn, artifact_id="art-001", source_id="src-001")
    conn.close()

    with patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db):
        from fieldkit.commands.ingest.reprocess import main

        rc = main(["--pipeline", "transcript-ingest"])

    assert rc == 1


def test_reprocess_no_version_live_run_stderr_contains_from_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Warning message must mention --from-version so the operator knows what to fix."""
    pipeline_db = tmp_path / "pipeline.db"
    conn = init_db(pipeline_db, pipelines=PIPELINES)
    _seed_artifact(conn, artifact_id="art-002", source_id="src-002")
    conn.close()

    with patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db):
        from fieldkit.commands.ingest.reprocess import main

        main(["--pipeline", "transcript-ingest"])

    captured = capsys.readouterr()
    assert "--from-version" in captured.err


def test_reprocess_no_version_live_run_stderr_contains_pipeline_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Warning must include the current pipeline version so the operator understands the no-op."""
    pipeline_db = tmp_path / "pipeline.db"
    conn = init_db(pipeline_db, pipelines=PIPELINES)
    _seed_artifact(conn, artifact_id="art-003", source_id="src-003")
    conn.close()

    with patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db):
        from fieldkit.commands.ingest.reprocess import main

        main(["--pipeline", "transcript-ingest"])

    captured = capsys.readouterr()
    # The current transcript-ingest pipeline version is 0.1.0
    assert "0.1.0" in captured.err


def test_reprocess_no_version_live_run_no_artifacts_written(tmp_path: Path) -> None:
    """Without --from-version the command must not write or overwrite any files."""
    pipeline_db = tmp_path / "pipeline.db"
    vault_file = tmp_path / "meeting.md"
    vault_file.write_text("original content", encoding="utf-8")

    conn = init_db(pipeline_db, pipelines=PIPELINES)
    _seed_artifact(
        conn,
        artifact_id="art-004",
        source_id="src-004",
        content_path=str(vault_file),
    )
    conn.close()

    with patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db):
        from fieldkit.commands.ingest.reprocess import main

        main(["--pipeline", "transcript-ingest"])

    # File must be untouched
    assert vault_file.read_text(encoding="utf-8") == "original content"


# ── TestReprocessNoVersionDryRun (flattened) ────────────────────────────────


def test_reprocess_no_version_dry_run_dry_run_exit_code_is_1(tmp_path: Path) -> None:
    """--dry-run without --from-version must also exit 1 (the no-op is still misleading)."""
    pipeline_db = tmp_path / "pipeline.db"
    conn = init_db(pipeline_db, pipelines=PIPELINES)
    _seed_artifact(conn, artifact_id="art-005", source_id="src-005")
    conn.close()

    with patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db):
        from fieldkit.commands.ingest.reprocess import main

        rc = main(["--pipeline", "transcript-ingest", "--dry-run"])

    assert rc == 1


def test_reprocess_no_version_dry_run_dry_run_stderr_contains_from_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--dry-run warning must also mention --from-version."""
    pipeline_db = tmp_path / "pipeline.db"
    conn = init_db(pipeline_db, pipelines=PIPELINES)
    _seed_artifact(conn, artifact_id="art-006", source_id="src-006")
    conn.close()

    with patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db):
        from fieldkit.commands.ingest.reprocess import main

        main(["--pipeline", "transcript-ingest", "--dry-run"])

    captured = capsys.readouterr()
    assert "--from-version" in captured.err


def test_reprocess_no_version_dry_run_dry_run_empty_db_still_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Even with an empty DB, --dry-run without --from-version must warn and exit 1.

    The guard fires before the DB query so the warning is consistent regardless
    of whether any artifacts exist.
    """
    pipeline_db = tmp_path / "pipeline.db"
    init_db(pipeline_db, pipelines=PIPELINES)

    with patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db):
        from fieldkit.commands.ingest.reprocess import main

        rc = main(["--pipeline", "transcript-ingest", "--dry-run"])

    assert rc == 1
    captured = capsys.readouterr()
    assert "--from-version" in captured.err


# ── TestReprocessWithFromVersion (flattened) ────────────────────────────────


def test_reprocess_with_from_version_from_version_reaches_artifact_loop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With --from-version the command must proceed past the guard and list artifacts."""
    pipeline_db = tmp_path / "pipeline.db"
    conn = init_db(pipeline_db, pipelines=PIPELINES)
    _seed_artifact(
        conn,
        artifact_id="art-007",
        source_id="src-007",
        pipeline_version="0.0.9",
        content_path="/vault/old.md",
    )
    conn.close()

    with patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db):
        from fieldkit.commands.ingest.reprocess import main

        rc = main(
            [
                "--pipeline",
                "transcript-ingest",
                "--from-version",
                "0.0.9",
                "--dry-run",
            ]
        )

    # Must succeed (0) and list the artifact — not exit 1 with warning
    assert rc == 0
    captured = capsys.readouterr()
    assert "src-007" in captured.out
    # No --from-version warning should appear
    assert "--from-version" not in captured.err


def test_reprocess_with_from_version_from_version_no_warning_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With --from-version, stderr must be clean (no historic regression warning)."""
    pipeline_db = tmp_path / "pipeline.db"
    conn = init_db(pipeline_db, pipelines=PIPELINES)
    _seed_artifact(
        conn,
        artifact_id="art-008",
        source_id="src-008",
        pipeline_version="0.0.1",
    )
    conn.close()

    with patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db):
        from fieldkit.commands.ingest.reprocess import main

        main(
            [
                "--pipeline",
                "transcript-ingest",
                "--from-version",
                "0.0.1",
                "--dry-run",
            ]
        )

    captured = capsys.readouterr()
    assert "Warning: --from-version not set" not in captured.err
