"""Transcript command selection, output, and processing-loop behavior."""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.commands.ingest.run import (
    _dynamic_worker_count,
    _ProcessResult,
)
from fieldkit.errors import AuthError, GmailSyncPartialError
from fieldkit.gmail.exceptions import GmailDbNotFoundError
from fieldkit.ingest.pipeline import infer_meeting_date
from fieldkit.ingest.sources import SourceRecord

pytestmark = pytest.mark.unit


def _make_source(source_id: str, title: str, *, dated: bool = True) -> SourceRecord:
    return SourceRecord(
        source_id=source_id,
        pipeline_id="transcript-ingest",
        subject=title,
        meeting_title=title,
        meeting_date=datetime(2026, 6, 19, tzinfo=UTC) if dated else None,
        doc_url=f"https://docs.google.com/document/d/{source_id}",
        email_message_id=f"msg-{source_id}",
        discovered_at="2026-06-19T00:00:00Z",
    )


def test_run_json_dry_run_reports_ordered_pending(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fieldkit.commands.ingest._output import json_output
    from fieldkit.commands.ingest.run import _run_transcript_ingest

    sources = [
        _make_source("src-002", "Second", dated=False),
        _make_source("src-001", "First", dated=False),
    ]
    conn = MagicMock()
    with (
        json_output(True),
        patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
        patch("fieldkit.ingest.db.get_db_read_only", return_value=conn),
        patch("fieldkit.ingest.db.init_db") as init_db,
        patch("fieldkit.commands.ingest.run.transcript_run_lock") as run_lock,
        patch("fieldkit.gmail.discover.get_gmail_db_path", return_value=tmp_path / "gmail.db"),
        patch(
            "fieldkit.gmail.discover.scan_gemini_candidates",
            return_value=[],
        ),
        patch("fieldkit.ingest.sources.get_pending_sources", return_value=sources),
    ):
        rc = _run_transcript_ingest(
            spec=SimpleNamespace(version="0.1.0"), dry_run=True, limit=None, interactive=False, as_json=True
        )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert rc == 0
    assert payload["pending"] == ["src-001", "src-002"]
    init_db.assert_not_called()
    run_lock.assert_not_called()
    conn.close.assert_called_once_with()


def test_run_dry_run_missing_registry_previews_candidates_without_creating_files(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fieldkit.commands.ingest.run import _run_transcript_ingest
    from fieldkit.gmail.discover import GmailCandidate

    pipeline_db = tmp_path / "pipeline.db"
    candidate = GmailCandidate(
        source_id="candidate-001",
        doc_url="https://docs.google.com/document/d/candidate-001",
        subject="Fictional meeting",
        meeting_title="Fictional meeting",
        meeting_date=None,
        email_message_id="message-001",
    )
    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db),
        patch("fieldkit.gmail.discover.get_gmail_db_path", return_value=tmp_path / "gmail.db"),
        patch("fieldkit.gmail.discover.scan_gemini_candidates", return_value=[candidate]),
        patch("fieldkit.ingest.db.get_db_read_only", side_effect=FileNotFoundError),
        patch("fieldkit.ingest.db.init_db") as init_db,
        patch("fieldkit.commands.ingest.run.transcript_run_lock") as run_lock,
    ):
        rc = _run_transcript_ingest(
            spec=SimpleNamespace(version="0.1.0"),
            dry_run=True,
            limit=1,
            interactive=False,
            as_json=False,
        )

    assert rc == 0
    assert "candidate-001" in capsys.readouterr().out
    assert not pipeline_db.exists()
    init_db.assert_not_called()
    run_lock.assert_not_called()


def test_run_dry_run_reads_existing_registry_without_lock_or_mutation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fieldkit.commands.ingest.registry import PIPELINES
    from fieldkit.commands.ingest.run import _run_transcript_ingest
    from fieldkit.gmail.discover import GmailCandidate
    from fieldkit.ingest.db import init_db
    from fieldkit.ingest.sources import discover_gemini_sources

    pipeline_db = tmp_path / "pipeline.db"
    existing = GmailCandidate(
        source_id="existing-001",
        doc_url="https://docs.google.com/document/d/existing-001",
        subject="Existing meeting",
        meeting_title="Existing meeting",
        meeting_date=None,
        email_message_id="message-existing",
    )
    discovered = GmailCandidate(
        source_id="new-001",
        doc_url="https://docs.google.com/document/d/new-001",
        subject="New meeting",
        meeting_title="New meeting",
        meeting_date=None,
        email_message_id="message-new",
    )
    connection = init_db(pipeline_db, pipelines=PIPELINES)
    discover_gemini_sources(connection, [existing])
    connection.close()
    before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in tmp_path.iterdir() if path.is_file()}

    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db),
        patch("fieldkit.gmail.discover.get_gmail_db_path", return_value=tmp_path / "gmail.db"),
        patch("fieldkit.gmail.discover.scan_gemini_candidates", return_value=[existing, discovered]),
        patch("fieldkit.commands.ingest.run.transcript_run_lock") as run_lock,
    ):
        rc = _run_transcript_ingest(
            spec=SimpleNamespace(version="0.1.0"),
            dry_run=True,
            limit=None,
            interactive=False,
            as_json=False,
        )

    after = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in tmp_path.iterdir() if path.is_file()}
    output = capsys.readouterr().out
    assert rc == 0
    assert "existing-001" in output
    assert "new-001" in output
    assert before == after
    run_lock.assert_not_called()


def test_run_dry_run_duplicate_candidates_matches_live_pending_order(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fieldkit.commands.ingest._output import json_output
    from fieldkit.commands.ingest.registry import PIPELINES
    from fieldkit.commands.ingest.run import _run_transcript_ingest
    from fieldkit.gmail.discover import GmailCandidate
    from fieldkit.ingest.db import get_db_read_only, init_db
    from fieldkit.ingest.sources import discover_gemini_sources, get_pending_sources

    def candidate(source_id: str, title: str) -> GmailCandidate:
        return GmailCandidate(
            source_id=source_id,
            doc_url=f"https://docs.google.com/document/d/{source_id}",
            subject=title,
            meeting_title=title,
            meeting_date=None,
            email_message_id=f"message-{source_id}",
        )

    candidates = [candidate("z", "First Z"), candidate("z", "Duplicate Z"), candidate("a", "A")]
    preview_db = tmp_path / "preview.db"
    with (
        json_output(True),
        patch("fieldkit.ingest.db.get_db_path", return_value=preview_db),
        patch("fieldkit.gmail.discover.get_gmail_db_path", return_value=tmp_path / "gmail.db"),
        patch("fieldkit.gmail.discover.scan_gemini_candidates", return_value=candidates),
    ):
        rc = _run_transcript_ingest(
            spec=SimpleNamespace(version="0.1.0"),
            dry_run=True,
            limit=2,
            interactive=False,
            as_json=True,
        )
    preview_ids = json.loads(capsys.readouterr().out)["pending"]

    live_db = tmp_path / "live.db"
    connection = init_db(live_db, pipelines=PIPELINES)
    discover_gemini_sources(connection, candidates)
    connection.close()
    reader = get_db_read_only(live_db)
    try:
        live_ids = [source.source_id for source in get_pending_sources(reader, "transcript-ingest", limit=2)]
    finally:
        reader.close()

    assert rc == 0
    assert preview_ids == live_ids == ["a", "z"]


def test_run_dry_run_orders_new_candidates_with_existing_registry_before_limit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fieldkit.commands.ingest._output import json_output
    from fieldkit.commands.ingest.registry import PIPELINES
    from fieldkit.commands.ingest.run import _run_transcript_ingest
    from fieldkit.gmail.discover import GmailCandidate
    from fieldkit.ingest.db import get_db_read_only, init_db
    from fieldkit.ingest.sources import discover_gemini_sources, get_pending_sources

    def candidate(source_id: str) -> GmailCandidate:
        return GmailCandidate(
            source_id=source_id,
            doc_url=f"https://docs.google.com/document/d/{source_id}",
            subject=source_id,
            meeting_title=source_id,
            meeting_date=None,
            email_message_id=f"message-{source_id}",
        )

    pipeline_db = tmp_path / "pipeline.db"
    connection = init_db(pipeline_db, pipelines=PIPELINES)
    discover_gemini_sources(connection, [candidate("existing")])
    connection.execute("UPDATE sources SET discovered_at = '9999-12-31T23:59:59Z'")
    connection.commit()
    connection.close()

    with (
        json_output(True),
        patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db),
        patch("fieldkit.gmail.discover.get_gmail_db_path", return_value=tmp_path / "gmail.db"),
        patch("fieldkit.gmail.discover.scan_gemini_candidates", return_value=[candidate("new")]),
    ):
        rc = _run_transcript_ingest(
            spec=SimpleNamespace(version="0.1.0"),
            dry_run=True,
            limit=1,
            interactive=False,
            as_json=True,
        )
    preview_ids = json.loads(capsys.readouterr().out)["pending"]

    writer = init_db(pipeline_db, pipelines=PIPELINES)
    discover_gemini_sources(writer, [candidate("new")])
    writer.close()
    reader = get_db_read_only(pipeline_db)
    try:
        live_ids = [source.source_id for source in get_pending_sources(reader, "transcript-ingest", limit=1)]
    finally:
        reader.close()

    assert rc == 0
    assert preview_ids == live_ids == ["new"]


@pytest.mark.parametrize(
    "discovery_error",
    [
        GmailDbNotFoundError("Gmail cache has not been published"),
        GmailSyncPartialError("Gmail discovery exceeded its work bound"),
    ],
    ids=["missing-publication", "bounded-scan"],
)
def test_run_rejects_unverified_gmail_before_registry_mutation(tmp_path: Path, discovery_error: Exception) -> None:
    from fieldkit.commands.ingest.run import _run_locked_transcript_ingest

    with (
        patch("fieldkit.ingest.db.init_db") as init_db,
        patch("fieldkit.gmail.discover.get_gmail_db_path", return_value=tmp_path / "gmail.db"),
        patch("fieldkit.gmail.discover.scan_gemini_candidates", side_effect=discovery_error),
        patch("fieldkit.commands.ingest.run.recover_interrupted_sources") as recover,
        pytest.raises(type(discovery_error), match=str(discovery_error)),
    ):
        _run_locked_transcript_ingest(
            db_path=tmp_path / "pipeline.db",
            spec=SimpleNamespace(version="0.1.0"),
            limit=None,
            interactive=False,
            as_json=False,
        )

    init_db.assert_not_called()
    recover.assert_not_called()
    assert not (tmp_path / "pipeline.db").exists()


@pytest.mark.parametrize(
    ("argv", "runner_target"),
    [
        (
            ["ingest", "run", "--pipeline", "transcript-ingest", "--interactive", "--json"],
            "fieldkit.commands.ingest.run._run_run",
        ),
        (
            ["ingest", "reprocess", "--pipeline", "transcript-ingest", "--interactive", "--json"],
            "fieldkit.commands.ingest.reprocess._run_reprocess",
        ),
    ],
)
def test_ingest_json_rejects_interactive_before_runner(argv: list[str], runner_target: str) -> None:
    from fieldkit.__main__ import main

    with patch(runner_target) as mock_runner:
        exit_code = main(argv)

    assert exit_code == 3
    mock_runner.assert_not_called()


# ---------------------------------------------------------------------------
# infer_meeting_date  (deduplicated from the CLI-layer parser in historic regression)
# ---------------------------------------------------------------------------


def test_infer_meeting_date_slash_format() -> None:
    assert infer_meeting_date("Meeting - 2026/05/26 14:31 EDT") == "2026-05-26"


def test_infer_meeting_date_dash_format() -> None:
    assert infer_meeting_date("Call - 2026-01-15 09:00") == "2026-01-15"


def test_infer_meeting_date_long_form() -> None:
    """The format the pipeline-side parser knew, before the two were merged."""
    assert infer_meeting_date("Quarterly Review - May 26, 2026") == "2026-05-26"


@pytest.mark.parametrize(
    "title",
    ["No date in this title", "", "Weekly Standup", "Bad date 2026/13/45"],
    ids=["no-date", "empty", "plain-title", "impossible-date"],
)
def test_infer_meeting_date_returns_none_when_absent(title: str) -> None:
    """None, not today.

    Returning today made "this title has no date" indistinguishable from "this meeting
    was today", which is what let ``ingest reprocess`` silently re-stamp historical
    notes (historic regression). The caller now decides what an absent date means.
    """
    assert infer_meeting_date(title) is None


# ---------------------------------------------------------------------------
# _dynamic_worker_count
# ---------------------------------------------------------------------------


# ── TestDynamicWorkerCount (flattened) ──────────────────────────────────────


def test_dynamic_worker_count_small_queue_one_worker() -> None:
    assert _dynamic_worker_count(1) == 1


def test_dynamic_worker_count_five_items_one_worker() -> None:
    assert _dynamic_worker_count(5) == 1


def test_dynamic_worker_count_six_items_two_workers() -> None:
    # 6 * 3 / 15 = 1.2, ceil = 2
    assert _dynamic_worker_count(6) == 2


def test_dynamic_worker_count_capped_at_max() -> None:
    # Very large queue — should be capped at 8
    assert _dynamic_worker_count(1000) == 8


def test_dynamic_worker_count_zero_items() -> None:
    # 0 items — should return 1 (minimum)
    assert _dynamic_worker_count(0) == 1


# No error raised


# Should not error even with empty list


# ---------------------------------------------------------------------------
# _run_processing_loop — error handling
# ---------------------------------------------------------------------------


# ── TestRunProcessingLoop (flattened) ───────────────────────────────────────


def test_run_processing_loop_missing_credentials_propagates_auth(tmp_path: Path) -> None:
    """The real credential loader's authentication failure escapes the worker loop."""
    from fieldkit.commands.ingest.run import _run_processing_loop

    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
        patch("fieldkit.ingest.db.get_db", return_value=MagicMock()),
        patch("fieldkit.ingest.sources.claim_pending_source", return_value=True),
        patch("fieldkit.commands.ingest.run.load_prepared", return_value=None),
        patch("fieldkit.commands.ingest.run.require_optional_profile"),
        patch("fieldkit.config.get_google_token_path", return_value=tmp_path / "missing-token.json"),
        pytest.raises(AuthError, match="Google credentials are missing"),
    ):
        src = _make_source("abc123", "Test", dated=False)
        _run_processing_loop(
            [src],
            conn=MagicMock(),
            pipeline_version="0.1.0",
            interactive=False,
        )


def test_run_processing_loop_interactive_quit_stops_loop(tmp_path: Path) -> None:
    """Interactive mode: 'q' response stops processing immediately."""
    from fieldkit.commands.ingest.run import _run_processing_loop

    mock_service = MagicMock()
    src = _make_source("src001", "Test Meeting")

    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=mock_service),
        patch("fieldkit.commands.ingest.run._prompt_process_choice", return_value="q"),
        patch("fieldkit.commands.ingest.run._process_one_source") as mock_process,
    ):
        rc = _run_processing_loop(
            [src],
            conn=MagicMock(),
            pipeline_version="0.1.0",
            interactive=True,
        )
    mock_process.assert_not_called()
    assert rc == 0


def test_run_processing_loop_interactive_skip_increments_skipped(tmp_path: Path) -> None:
    """Interactive mode: 'n' response skips the source."""
    from fieldkit.commands.ingest.run import _run_processing_loop

    mock_service = MagicMock()
    src = _make_source("src002", "Test Meeting")

    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=mock_service),
        patch("fieldkit.commands.ingest.run._prompt_process_choice", return_value="n"),
        patch("fieldkit.commands.ingest.run._process_one_source") as mock_process,
    ):
        rc = _run_processing_loop(
            [src],
            conn=MagicMock(),
            pipeline_version="0.1.0",
            interactive=True,
        )
    mock_process.assert_not_called()
    assert rc == 0


def test_run_processing_loop_interactive_yes_calls_process(tmp_path: Path) -> None:
    """Interactive mode: 'y' response processes the source."""
    from fieldkit.commands.ingest.run import _run_processing_loop

    mock_service = MagicMock()
    src = _make_source("src003", "Test Meeting")

    with (
        patch("fieldkit.ingest.sources.claim_pending_source", return_value=True),
        patch("fieldkit.config.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=mock_service),
        patch("fieldkit.commands.ingest.run._prompt_process_choice", return_value="y"),
        patch("fieldkit.commands.ingest.run.load_prepared", return_value=None),
        patch(
            "fieldkit.commands.ingest.run._process_one_source",
            return_value=_ProcessResult(completed=True, degraded=False),
        ) as mock_process,
    ):
        rc = _run_processing_loop(
            [src],
            conn=MagicMock(),
            pipeline_version="0.1.0",
            interactive=True,
        )
    mock_process.assert_called_once()
    assert rc == 0


def test_run_processing_loop_parallel_process_error_counted(tmp_path: Path) -> None:
    """Parallel mode: failed _process_one_source increments n_errors."""
    from fieldkit.commands.ingest.run import _run_processing_loop

    mock_service = MagicMock()
    src = _make_source("src004", "Broken Meeting")

    def fake_open_db(path: Any) -> MagicMock:
        conn = MagicMock()
        conn.close = MagicMock()
        return conn

    with (
        patch("fieldkit.ingest.sources.claim_pending_source", return_value=True),
        patch("fieldkit.config.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=mock_service),
        patch("fieldkit.ingest.db.get_db", side_effect=fake_open_db),
        patch("fieldkit.commands.ingest.run.load_prepared", return_value=None),
        patch(
            "fieldkit.commands.ingest.run._process_one_source",
            return_value=_ProcessResult(completed=False, degraded=False),
        ) as process,
    ):
        rc = _run_processing_loop(
            [src],
            conn=MagicMock(),
            pipeline_version="0.1.0",
            interactive=False,
        )
    assert rc == 1
    process.assert_called_once()
