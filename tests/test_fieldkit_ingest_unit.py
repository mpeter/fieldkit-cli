"""Unit tests for fieldkit.ingest — in-process coverage.

Supplements subprocess smoke tests in test_ingest_cli.py.
All subcommand dispatch tests use CliRunner so no real I/O or DB access occurs.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from fieldkit.commands.ingest.cli import cli
from fieldkit.commands.ingest.run import _process_one_source, _ProcessResult, _run_processing_loop
from fieldkit.ingest.sources import SourceRecord

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# --help / no-args paths
# ---------------------------------------------------------------------------


def test_ingest_help_flag_returns_0() -> None:
    """cli(['--help']) exits 0 and prints usage."""
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "Usage" in result.output


def test_ingest_no_args_returns_nonzero() -> None:
    """cli([]) exits non-zero (no subcommand given)."""
    runner = CliRunner()
    result = runner.invoke(cli, [])
    assert result.exit_code != 0


def test_ingest_no_args_prints_usage() -> None:
    """cli([]) still prints usage text."""
    runner = CliRunner()
    result = runner.invoke(cli, [])
    assert "Usage" in result.output


def test_ingest_help_lists_subcommands() -> None:
    """--help output lists all known subcommands."""
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    for subcmd in ("status", "run", "discover", "reprocess", "backfill"):
        assert subcmd in result.output, f"Missing subcommand {subcmd!r} in --help output"


# ---------------------------------------------------------------------------
# unknown subcommand
# ---------------------------------------------------------------------------


def test_ingest_bogus_subcommand_returns_nonzero() -> None:
    """cli(['bogus']) exits non-zero."""
    runner = CliRunner()
    result = runner.invoke(cli, ["bogus"])
    assert result.exit_code != 0


def test_ingest_bogus_subcommand_prints_error() -> None:
    """cli(['bogus']) prints an error mentioning the unknown subcommand."""
    runner = CliRunner()
    result = runner.invoke(cli, ["bogus"])
    assert "bogus" in result.output


def test_ingest_bogus_subcommand_lists_available() -> None:
    """cli(['bogus']) output directs user to get help (Click's standard error format)."""
    runner = CliRunner()
    result = runner.invoke(cli, ["bogus"])
    # Click prints "Try 'ingest -h' for help." (group uses -h as help flag)
    assert "-h" in result.output


# ---------------------------------------------------------------------------
# known subcommands — dispatch smoke tests via CliRunner
# ---------------------------------------------------------------------------


def test_ingest_status_dispatches_to_module() -> None:
    """cli(['status', '--help']) dispatches to status subcommand and exits 0."""
    runner = CliRunner()
    result = runner.invoke(cli, ["status", "--help"])
    assert result.exit_code == 0


def test_ingest_status_forwards_extra_args() -> None:
    """cli(['status', '--help']) returns help text for the status subcommand."""
    runner = CliRunner()
    result = runner.invoke(cli, ["status", "--help"])
    assert result.exit_code == 0
    assert "Usage" in result.output


def test_ingest_run_dispatches_to_module() -> None:
    """cli(['run', '--help']) dispatches to run subcommand and exits 0."""
    runner = CliRunner()
    result = runner.invoke(cli, ["run", "--help"])
    assert result.exit_code == 0


def test_ingest_discover_dispatches_to_module() -> None:
    """cli(['discover', '--help']) dispatches to discover subcommand and exits 0."""
    runner = CliRunner()
    result = runner.invoke(cli, ["discover", "--help"])
    assert result.exit_code == 0


def test_ingest_reprocess_dispatches_to_module() -> None:
    """cli(['reprocess', '--help']) dispatches to reprocess subcommand and exits 0."""
    runner = CliRunner()
    result = runner.invoke(cli, ["reprocess", "--help"])
    assert result.exit_code == 0


def test_ingest_backfill_dispatches_to_module() -> None:
    """cli(['backfill', '--help']) dispatches to backfill subcommand and exits 0."""
    runner = CliRunner()
    result = runner.invoke(cli, ["backfill", "--help"])
    assert result.exit_code == 0


def test_ingest_subcommand_nonzero_propagated() -> None:
    """Non-zero exit from a leaf command propagates through cli."""
    runner = CliRunner()
    # Invoke a subcommand with invalid args to trigger a non-zero exit
    result = runner.invoke(cli, ["status", "--no-such-option"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Task 6.7 — _process_one_source returns False on pipeline failure
# ---------------------------------------------------------------------------


def test_process_one_source_returns_error_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """_process_one_source returns False when the doc fetch step raises an exception.

    We mock _fetch_doc_for_run to return None (the error path), which causes
    _process_one_source to return False immediately.
    """
    import fieldkit.commands.ingest.run as run_mod

    # _fetch_doc_for_run returning None signals a fetch failure
    monkeypatch.setattr(run_mod, "_fetch_doc_for_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_mod, "load_prepared", lambda *args: None)

    source = SourceRecord(
        source_id="doc-abc123",
        pipeline_id="transcript-ingest",
        subject="Test Meeting",
        meeting_title="Test Meeting",
        meeting_date=None,
        doc_url="https://docs.google.com/document/d/doc-abc123",
        email_message_id="msg-abc123",
        discovered_at="2026-06-19T00:00:00Z",
    )
    conn = sqlite3.connect(":memory:")

    result = _process_one_source(
        src=source,
        service=object(),
        conn=conn,
        data_root=Path("/tmp/fake-data"),
        pipeline_version="0.1.0",
    )
    conn.close()

    assert result.completed is False
    assert result.degraded is False


# ---------------------------------------------------------------------------
# Task 6.8 — _run_processing_loop always returns 0
# ---------------------------------------------------------------------------


def _make_fake_service() -> object:
    """Return a sentinel object standing in for a Google Docs service."""
    return object()


def test_run_processing_loop_returns_zero_on_all_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """_run_processing_loop returns 0 when all sources process successfully."""
    import pathlib
    import sys
    import types

    import fieldkit.commands.ingest.run as run_mod

    # Patch the lazy imports inside _run_processing_loop
    fake_docs = types.ModuleType("fieldkit.ingest.docs")
    fake_docs.get_docs_service = _make_fake_service  # type: ignore[attr-defined]

    fake_db = types.ModuleType("fieldkit.ingest.db")
    fake_db.get_db_path = lambda: ":memory:"  # type: ignore[attr-defined]
    fake_db.get_db = lambda path: sqlite3.connect(":memory:")  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "fieldkit.ingest.docs", fake_docs)
    monkeypatch.setitem(sys.modules, "fieldkit.ingest.db", fake_db)
    monkeypatch.setattr("fieldkit.ingest.sources.claim_pending_source", lambda conn, source_id: True)

    # Patch get_fieldkit_home inside the run module
    monkeypatch.setattr("fieldkit.config.get_fieldkit_home", lambda: pathlib.Path("/tmp/fake-data"))

    monkeypatch.setattr(run_mod, "load_prepared", lambda conn, source_id: None)
    # _process_one_source always succeeds
    monkeypatch.setattr(
        run_mod,
        "_process_one_source",
        lambda **kwargs: _ProcessResult(completed=True, degraded=False),
    )

    source = SourceRecord(
        source_id="doc-success-1",
        pipeline_id="transcript-ingest",
        subject="Meeting A",
        meeting_title="Meeting A",
        meeting_date=datetime(2026, 6, 19, tzinfo=UTC),
        doc_url="https://docs.google.com/document/d/doc-success-1",
        email_message_id="msg-success-1",
        discovered_at="2026-06-19T00:00:00Z",
    )
    conn = sqlite3.connect(":memory:")

    rc = _run_processing_loop(
        [source],
        conn=conn,
        pipeline_version="0.1.0",
        interactive=False,
    )
    conn.close()

    assert rc == 0, f"_run_processing_loop must return 0 on success, got {rc}"


def test_run_processing_loop_reports_partial_failure_on_item_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Human mode reports an unsuccessful source through its exit status."""
    import sys
    import types

    import fieldkit.commands.ingest.run as run_mod

    fake_docs = types.ModuleType("fieldkit.ingest.docs")
    fake_docs.get_docs_service = _make_fake_service  # type: ignore[attr-defined]

    fake_db = types.ModuleType("fieldkit.ingest.db")
    fake_db.get_db_path = lambda: ":memory:"  # type: ignore[attr-defined]
    fake_db.get_db = lambda path: sqlite3.connect(":memory:")  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "fieldkit.ingest.docs", fake_docs)
    monkeypatch.setitem(sys.modules, "fieldkit.ingest.db", fake_db)
    monkeypatch.setattr("fieldkit.ingest.sources.claim_pending_source", lambda conn, source_id: True)
    monkeypatch.setattr("fieldkit.config.get_fieldkit_home", lambda: __import__("pathlib").Path("/tmp/fake-data"))

    monkeypatch.setattr(run_mod, "load_prepared", lambda conn, source_id: None)
    # _process_one_source always fails (returns False)
    monkeypatch.setattr(
        run_mod,
        "_process_one_source",
        lambda **kwargs: _ProcessResult(completed=False, degraded=False),
    )

    source = SourceRecord(
        source_id="doc-fail-1",
        pipeline_id="transcript-ingest",
        subject="Meeting B",
        meeting_title="Meeting B",
        meeting_date=None,
        doc_url="https://docs.google.com/document/d/doc-fail-1",
        email_message_id="msg-fail-1",
        discovered_at="2026-06-19T00:00:00Z",
    )
    conn = sqlite3.connect(":memory:")

    rc = _run_processing_loop(
        [source],
        conn=conn,
        pipeline_version="0.1.0",
        interactive=False,
    )
    conn.close()

    assert rc == 1
