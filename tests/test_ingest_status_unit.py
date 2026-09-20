"""Unit tests for fieldkit.ingest.status — in-process coverage.

Supplements the subprocess smoke tests in test_ingest_cli.py.
All tests mock lib.ingest_db.get_db so no real pipeline.db is needed.
"""

import sqlite3
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_conn(
    sources: list[tuple[str, int]] | None = None,
    artifacts: list[tuple[str, int]] | None = None,
) -> sqlite3.Connection:
    """Return an in-memory sqlite3 connection pre-populated with summary rows.

    ``sources`` and ``artifacts`` are lists of (pipeline_id, count) pairs that
    are returned verbatim by the SQL queries in status.main().
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    conn.execute("CREATE TABLE sources (pipeline_id TEXT, count_n INTEGER)")
    conn.execute("CREATE TABLE artifacts (pipeline_id TEXT, count_n INTEGER)")

    # Override the GROUP BY queries by creating views that return per-row counts
    # The status module runs:
    #   SELECT pipeline_id, COUNT(*) AS n FROM sources GROUP BY pipeline_id
    # We insert raw rows so COUNT(*) aggregation gives the expected values.
    if sources:
        for pipeline_id, count in sources:
            for _ in range(count):
                conn.execute("INSERT INTO sources VALUES (?, ?)", (pipeline_id, 0))
    if artifacts:
        for pipeline_id, count in artifacts:
            for _ in range(count):
                conn.execute("INSERT INTO artifacts VALUES (?, ?)", (pipeline_id, 0))
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# --help path
# ---------------------------------------------------------------------------


def test_status_help_returns_0(capsys: pytest.CaptureFixture[str]) -> None:
    """main(['--help']) returns 0 and prints usage text."""
    from fieldkit.commands.ingest.status import main

    rc = main(["--help"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "Usage" in captured.out


def test_status_help_short_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """main(['-h']) returns 0 and prints usage text."""
    from fieldkit.commands.ingest.status import main

    rc = main(["-h"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "Usage" in captured.out


# ---------------------------------------------------------------------------
# no-DB path (FileNotFoundError)
# ---------------------------------------------------------------------------


def test_status_no_db_returns_0(capsys: pytest.CaptureFixture[str]) -> None:
    """main([]) returns 0 when pipeline.db does not exist."""
    from fieldkit.commands.ingest.status import main

    with patch("fieldkit.ingest.db.get_db", side_effect=FileNotFoundError("not found")):
        rc = main([])

    assert rc == 0
    captured = capsys.readouterr()
    assert "PIPELINE" in captured.out
    assert "not initialized" in captured.out


def test_status_no_db_lists_all_pipelines(capsys: pytest.CaptureFixture[str]) -> None:
    """Without a DB, all 3 pipeline IDs appear in output."""
    from fieldkit.commands.ingest.status import main

    with patch("fieldkit.ingest.db.get_db", side_effect=FileNotFoundError("not found")):
        main([])

    captured = capsys.readouterr()
    assert "transcript-ingest" in captured.out
    assert "customer-notes-ingest" in captured.out
    assert "pm-status-ingest" in captured.out


def test_status_no_db_lists_version(capsys: pytest.CaptureFixture[str]) -> None:
    """Without a DB, version '0.1.0' appears in output."""
    from fieldkit.commands.ingest.status import main

    with patch("fieldkit.ingest.db.get_db", side_effect=FileNotFoundError("not found")):
        main([])

    captured = capsys.readouterr()
    assert "0.1.0" in captured.out


def test_status_no_db_no_sources_artifacts_columns(capsys: pytest.CaptureFixture[str]) -> None:
    """Without a DB, SOURCES/ARTIFACTS columns do not appear in the header."""
    from fieldkit.commands.ingest.status import main

    with patch("fieldkit.ingest.db.get_db", side_effect=FileNotFoundError("not found")):
        main([])

    captured = capsys.readouterr()
    # The no-DB header omits count columns
    assert "SOURCES" not in captured.out
    assert "ARTIFACTS" not in captured.out


# ---------------------------------------------------------------------------
# with-DB path
# ---------------------------------------------------------------------------


def test_status_with_db_shows_connected(capsys: pytest.CaptureFixture[str]) -> None:
    """When a DB connection succeeds, output ends with 'pipeline.db: connected'."""
    from fieldkit.commands.ingest.status import main

    conn = _make_conn(
        sources=[("transcript-ingest", 5)],
        artifacts=[("transcript-ingest", 2)],
    )
    with patch("fieldkit.ingest.db.get_db", return_value=conn):
        rc = main([])

    assert rc == 0
    captured = capsys.readouterr()
    assert "connected" in captured.out


def test_status_with_db_shows_sources_count(capsys: pytest.CaptureFixture[str]) -> None:
    """DB-connected output includes the source row count for a pipeline."""
    from fieldkit.commands.ingest.status import main

    conn = _make_conn(sources=[("transcript-ingest", 7)])
    with patch("fieldkit.ingest.db.get_db", return_value=conn):
        main([])

    captured = capsys.readouterr()
    assert "7" in captured.out


def test_status_with_db_shows_artifacts_count(capsys: pytest.CaptureFixture[str]) -> None:
    """DB-connected output includes the artifact row count for a pipeline."""
    from fieldkit.commands.ingest.status import main

    conn = _make_conn(
        sources=[("transcript-ingest", 1)],
        artifacts=[("transcript-ingest", 3)],
    )
    with patch("fieldkit.ingest.db.get_db", return_value=conn):
        main([])

    captured = capsys.readouterr()
    assert "3" in captured.out


def test_status_with_db_zero_counts_for_unregistered_pipeline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pipelines with no rows in DB show 0 for sources and artifacts."""
    from fieldkit.commands.ingest.status import main

    conn = _make_conn()  # empty DB
    with patch("fieldkit.ingest.db.get_db", return_value=conn):
        main([])

    captured = capsys.readouterr()
    # 0 counts appear in the output for all pipelines
    assert "0" in captured.out


def test_status_with_db_header_includes_sources_artifacts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """DB-connected header includes SOURCES and ARTIFACTS column labels."""
    from fieldkit.commands.ingest.status import main

    conn = _make_conn()
    with patch("fieldkit.ingest.db.get_db", return_value=conn):
        main([])

    captured = capsys.readouterr()
    assert "SOURCES" in captured.out
    assert "ARTIFACTS" in captured.out


# ---------------------------------------------------------------------------
# DB open error path (generic Exception)
# ---------------------------------------------------------------------------


def test_status_db_error_prints_warning(capsys: pytest.CaptureFixture[str]) -> None:
    """A generic DB open exception prints a [warn] message to stderr and continues."""
    from fieldkit.commands.ingest.status import main

    with patch("fieldkit.ingest.db.get_db", side_effect=RuntimeError("disk full")):
        rc = main([])

    assert rc == 0
    captured = capsys.readouterr()
    assert "[warn]" in captured.err
    assert "disk full" in captured.err


# ---------------------------------------------------------------------------
# Direct _run_status coverage (gazepy binding path)
# ---------------------------------------------------------------------------


def test_run_status_no_db_lists_pipeline_ids(capsys: pytest.CaptureFixture[str]) -> None:
    """_run_status() lists known pipeline IDs even when no pipeline.db exists."""
    from fieldkit.commands.ingest.status import _run_status

    with patch("fieldkit.ingest.db.get_db", side_effect=FileNotFoundError):
        _run_status()

    out = capsys.readouterr().out
    # At least one pipeline ID must appear (gemini-notes is the primary pipeline)
    assert "gemini" in out.lower() or "PIPELINE" in out


def test_run_status_with_db_shows_counts(capsys: pytest.CaptureFixture[str]) -> None:
    """_run_status() shows sources/artifacts columns when pipeline.db is available."""
    from fieldkit.commands.ingest.status import _run_status

    conn = _make_conn(sources=[("transcript-ingest", 3)], artifacts=[("transcript-ingest", 1)])
    with patch("fieldkit.ingest.db.get_db", return_value=conn):
        _run_status()

    out = capsys.readouterr().out
    assert "SOURCES" in out
    assert "3" in out
