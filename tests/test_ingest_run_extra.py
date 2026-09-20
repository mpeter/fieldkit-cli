"""Additional branch coverage for fieldkit.ingest.run._run_processing_loop
and _process_one_source — targets the uncovered 28% branches."""

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.commands.ingest.run import _ProcessResult
from fieldkit.ingest.docs import GeminiDocContent
from fieldkit.ingest.sources import SourceRecord

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_src(
    source_id: str = "src-abc",
    meeting_title: str = "Acme Sync",
    meeting_date: datetime | None = datetime(2026, 6, 19, tzinfo=UTC),
) -> SourceRecord:
    return SourceRecord(
        source_id=source_id,
        pipeline_id="transcript-ingest",
        subject=meeting_title,
        meeting_title=meeting_title,
        meeting_date=meeting_date,
        doc_url=f"https://docs.google.com/document/d/{source_id}",
        email_message_id=f"msg-{source_id}",
        discovered_at="2026-06-19T00:00:00Z",
    )


def _run_loop(
    pending: list,
    *,
    interactive: bool = False,
    process_result: bool = True,
    docs_service: object | None = None,
) -> int:
    from fieldkit.commands.ingest.run import _run_processing_loop

    mock_conn = MagicMock()
    mock_service = docs_service or MagicMock()

    def _fake_open_db(path: object) -> MagicMock:
        c = MagicMock()
        c.close = MagicMock()
        return c

    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=Path("/tmp/fake-data")),
        patch("fieldkit.ingest.db.get_db_path", return_value=Path("/tmp/fake.db")),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=mock_service),
        patch("fieldkit.ingest.db.get_db", side_effect=_fake_open_db),
        patch("fieldkit.ingest.sources.claim_pending_source", return_value=True),
        patch(
            "fieldkit.commands.ingest.run._process_one_source",
            return_value=_ProcessResult(completed=process_result, degraded=False),
        ),
    ):
        return _run_processing_loop(
            pending,
            conn=mock_conn,
            pipeline_version="0.2.0",
            interactive=interactive,
        )


# ---------------------------------------------------------------------------
# _run_processing_loop — parallel mode branches
# ---------------------------------------------------------------------------


# ── TestRunProcessingLoopParallel (flattened) ───────────────────────────────


def test_run_parallel_loop_empty_pending_runs_without_error() -> None:
    rc = _run_loop([])
    assert rc == 0


def test_run_parallel_loop_single_source_success() -> None:
    rc = _run_loop([_make_src()], process_result=True)
    assert rc == 0


def test_run_parallel_loop_single_source_error_preserves_human_exit() -> None:
    """Human mode preserves its historical zero exit after reporting errors."""
    rc = _run_loop([_make_src()], process_result=False)
    assert rc == 0


def test_run_parallel_loop_source_with_no_meeting_date() -> None:
    """src.meeting_date=None falls back to 'unknown date'."""
    src = _make_src(meeting_date=None)
    rc = _run_loop([src])
    assert rc == 0


def test_run_parallel_loop_multiple_sources_parallel() -> None:
    sources = [_make_src(f"src-{i}") for i in range(4)]
    rc = _run_loop(sources, process_result=True)
    assert rc == 0


def test_run_parallel_json_reports_deterministic_outcomes(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.ingest._output import json_output
    from fieldkit.commands.ingest.run import _run_processing_loop

    sources = [_make_src("src-3"), _make_src("src-1"), _make_src("src-2")]
    with (
        json_output(True),
        patch("fieldkit.config.get_fieldkit_home", return_value=Path("/tmp/fake-data")),
        patch("fieldkit.ingest.db.get_db_path", return_value=Path("/tmp/fake.db")),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=MagicMock()),
        patch("fieldkit.ingest.db.get_db", return_value=MagicMock()),
        patch("fieldkit.ingest.sources.claim_pending_source", return_value=True),
        patch(
            "fieldkit.commands.ingest.run._process_one_source",
            side_effect=lambda **kwargs: _ProcessResult(completed=kwargs["src"].source_id != "src-1", degraded=False),
        ),
    ):
        rc = _run_processing_loop(sources, conn=MagicMock(), pipeline_version="0.2.0", interactive=False, as_json=True)

    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["completed"] == ["src-3", "src-2"]
    assert payload["failed"] == ["src-1"]


def test_run_parallel_json_reports_ordered_degraded_completed_subset(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fieldkit.commands.ingest._output import json_output
    from fieldkit.commands.ingest.run import _ProcessResult, _run_processing_loop

    sources = [_make_src("src-3"), _make_src("src-1"), _make_src("src-2")]
    results = {
        "src-3": _ProcessResult(completed=True, degraded=True),
        "src-1": _ProcessResult(completed=False, degraded=False),
        "src-2": _ProcessResult(completed=True, degraded=False),
    }
    with (
        json_output(True),
        patch("fieldkit.config.get_fieldkit_home", return_value=Path("/tmp/fake-data")),
        patch("fieldkit.ingest.db.get_db_path", return_value=Path("/tmp/fake.db")),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=MagicMock()),
        patch("fieldkit.ingest.db.get_db", return_value=MagicMock()),
        patch("fieldkit.ingest.sources.claim_pending_source", return_value=True),
        patch(
            "fieldkit.commands.ingest.run._process_one_source",
            side_effect=lambda **kwargs: results[kwargs["src"].source_id],
        ),
    ):
        rc = _run_processing_loop(sources, conn=MagicMock(), pipeline_version="0.2.0", interactive=False, as_json=True)

    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["completed"] == ["src-3", "src-2"]
    assert payload["degraded"] == ["src-3"]
    assert payload["failed"] == ["src-1"]


def test_run_interactive_summary_counts_degraded_inside_processed(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.ingest.run import _ProcessResult, _run_processing_loop

    sources = [_make_src("src-degraded"), _make_src("src-good")]
    results = iter(
        [
            _ProcessResult(completed=True, degraded=True),
            _ProcessResult(completed=True, degraded=False),
        ]
    )
    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=Path("/tmp/fake-data")),
        patch("fieldkit.ingest.db.get_db_path", return_value=Path("/tmp/fake.db")),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=MagicMock()),
        patch("fieldkit.commands.ingest.run._prompt_process_choice", return_value="y"),
        patch("fieldkit.commands.ingest.run._process_one_source", side_effect=lambda **kwargs: next(results)),
    ):
        rc = _run_processing_loop(sources, conn=MagicMock(), pipeline_version="0.2.0", interactive=True)

    assert rc == 0
    assert "Summary: 2 processed (1 degraded), 0 skipped, 0 error(s)." in capsys.readouterr().out


def test_run_parallel_interrupt_preserves_retry_boundary() -> None:
    from fieldkit.commands.ingest._output import BatchOutcomes
    from fieldkit.commands.ingest.run import _run_parallel_loop

    sources = [_make_src("src-0"), _make_src("src-1"), _make_src("src-2")]
    first_future = MagicMock()
    first_future.result.return_value = ("src-0", "completed")
    submitted = iter([first_future, MagicMock(), MagicMock()])
    pool = MagicMock()
    pool.__enter__.return_value.submit.side_effect = lambda *args: next(submitted)

    def interrupted(futures: object) -> object:
        yield first_future
        raise KeyboardInterrupt

    outcomes = BatchOutcomes()
    with (
        patch("fieldkit.commands.ingest.run.ThreadPoolExecutor", return_value=pool),
        patch("fieldkit.commands.ingest.run.as_completed", side_effect=interrupted),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=MagicMock()),
    ):
        processed, degraded, skipped, errors = _run_parallel_loop(
            sources,
            data_root=Path("/tmp/fake-data"),
            db_path=Path("/tmp/fake.db"),
            pipeline_version="0.2.0",
            outcomes=outcomes,
        )

    assert (processed, degraded, skipped, errors) == (1, 0, 0, 0)
    assert outcomes.completed == ["src-0"]
    assert outcomes.pending == ["src-1", "src-2"]


def test_run_parallel_loop_worker_exception_counted_not_raised() -> None:
    """Exception raised inside worker is caught and counted as error."""
    from fieldkit.commands.ingest.run import _run_processing_loop

    mock_conn = MagicMock()
    mock_service = MagicMock()
    src = _make_src()

    def _fake_open_db(path: object) -> MagicMock:
        c = MagicMock()
        c.close = MagicMock()
        return c

    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=Path("/tmp/fake-data")),
        patch("fieldkit.ingest.db.get_db_path", return_value=Path("/tmp/fake.db")),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=mock_service),
        patch("fieldkit.ingest.db.get_db", side_effect=_fake_open_db),
        patch("fieldkit.ingest.sources.claim_pending_source", return_value=True),
        patch("fieldkit.commands.ingest.run._process_one_source", side_effect=RuntimeError("unexpected")),
    ):
        rc = _run_processing_loop([src], conn=mock_conn, pipeline_version="0.2.0", interactive=False)

    assert rc == 0


def test_run_parallel_loop_many_workers_capped() -> None:
    """40+ sources triggers 8-worker cap — verify loop completes."""
    sources = [_make_src(f"src-{i}") for i in range(40)]
    rc = _run_loop(sources, process_result=True)
    assert rc == 0


# ---------------------------------------------------------------------------
# _run_processing_loop — interactive mode branches
# ---------------------------------------------------------------------------


# ── TestRunProcessingLoopInteractive (flattened) ────────────────────────────


def test_run_processing_loop_interactive_yes_success() -> None:
    from fieldkit.commands.ingest.run import _run_processing_loop

    src = _make_src()
    mock_conn = MagicMock()

    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=Path("/tmp/fake-data")),
        patch("fieldkit.ingest.db.get_db_path", return_value=Path("/tmp/fake.db")),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=MagicMock()),
        patch("fieldkit.commands.ingest.run._prompt_process_choice", return_value="y"),
        patch(
            "fieldkit.commands.ingest.run._process_one_source",
            return_value=_ProcessResult(completed=True, degraded=False),
        ),
    ):
        rc = _run_processing_loop([src], conn=mock_conn, pipeline_version="0.1.0", interactive=True)

    assert rc == 0


def test_run_processing_loop_interactive_yes_failure_counted() -> None:
    from fieldkit.commands.ingest.run import _run_processing_loop

    src = _make_src()
    mock_conn = MagicMock()

    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=Path("/tmp/fake-data")),
        patch("fieldkit.ingest.db.get_db_path", return_value=Path("/tmp/fake.db")),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=MagicMock()),
        patch("fieldkit.commands.ingest.run._prompt_process_choice", return_value="y"),
        patch(
            "fieldkit.commands.ingest.run._process_one_source",
            return_value=_ProcessResult(completed=False, degraded=False),
        ),
    ):
        rc = _run_processing_loop([src], conn=mock_conn, pipeline_version="0.1.0", interactive=True)

    assert rc == 0


def test_run_processing_loop_interactive_quit_after_one_skip() -> None:
    from fieldkit.commands.ingest.run import _run_processing_loop

    sources = [_make_src("src-1"), _make_src("src-2")]
    mock_conn = MagicMock()
    choices = iter(["n", "q"])

    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=Path("/tmp/fake-data")),
        patch("fieldkit.ingest.db.get_db_path", return_value=Path("/tmp/fake.db")),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=MagicMock()),
        patch("fieldkit.commands.ingest.run._prompt_process_choice", side_effect=choices),
        patch("fieldkit.commands.ingest.run._process_one_source") as mock_proc,
    ):
        rc = _run_processing_loop(sources, conn=mock_conn, pipeline_version="0.1.0", interactive=True)

    mock_proc.assert_not_called()
    assert rc == 0


def test_run_processing_loop_interactive_keyboard_interrupt_handled() -> None:
    from fieldkit.commands.ingest.run import _run_processing_loop

    src = _make_src()
    mock_conn = MagicMock()

    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=Path("/tmp/fake-data")),
        patch("fieldkit.ingest.db.get_db_path", return_value=Path("/tmp/fake.db")),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=MagicMock()),
        patch("fieldkit.commands.ingest.run._prompt_process_choice", side_effect=KeyboardInterrupt),
    ):
        rc = _run_processing_loop([src], conn=mock_conn, pipeline_version="0.1.0", interactive=True)

    assert rc == 0


def test_run_processing_loop_interactive_source_no_meeting_date() -> None:
    """src.meeting_date=None in interactive mode uses 'unknown date'."""
    from fieldkit.commands.ingest.run import _run_processing_loop

    src = _make_src(meeting_date=None)
    mock_conn = MagicMock()

    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=Path("/tmp/fake-data")),
        patch("fieldkit.ingest.db.get_db_path", return_value=Path("/tmp/fake.db")),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=MagicMock()),
        patch("fieldkit.commands.ingest.run._prompt_process_choice", return_value="q"),
        patch(
            "fieldkit.commands.ingest.run._process_one_source",
            return_value=_ProcessResult(completed=True, degraded=False),
        ),
    ):
        rc = _run_processing_loop([src], conn=mock_conn, pipeline_version="0.1.0", interactive=True)

    assert rc == 0


# ---------------------------------------------------------------------------
# _process_one_source — branch coverage via mocked dependencies
# ---------------------------------------------------------------------------


# ── TestProcessOneSource (flattened) ────────────────────────────────────────


def _process_one_source_run_process(
    tmp_path: Path,
    *,
    doc_content: object | None = None,
    account: str = "acme",
    meeting_date: datetime | None = datetime(2026, 6, 19, tzinfo=UTC),
    pursuits: list[str] | None = None,
    action_items: list[str] | None = None,
    key_topics: list[str] | None = None,
    key_decisions: list[str] | None = None,
    confidence: str = "high",
    used_fallback: bool = False,
    writeback_mocks: list[MagicMock] | None = None,
    writeback_notices: tuple[object, ...] = (),
    output_mocks: list[MagicMock] | None = None,
) -> _ProcessResult:
    from fieldkit.commands.ingest.run import _CleanResult, _process_one_source

    # Build fake doc_content
    if doc_content is None:
        doc_content = GeminiDocContent(
            doc_id="src-abc",
            doc_title="Test Meeting",
            transcript_text="Alice: Hi. Bob: Hello.",
            notes_text="",
            invited_emails=["alice@acme.example.com"],
        )

    # Build fake route
    from types import SimpleNamespace as SN

    route_confidence = SN(value="high")
    route = SN(
        accounts=[account],
        confidence=route_confidence,
        is_internal=False,
        pursuits=pursuits or [],
    )

    # Build fake meta
    meta = MagicMock()
    meta.accounts = [account]
    meta.pursuits = pursuits or []
    meta.action_items = action_items or []
    meta.key_topics = key_topics or []
    meta.key_decisions = key_decisions or []
    meta.confidence = confidence

    src = _make_src(meeting_date=meeting_date)
    vault_path = tmp_path / "vault.md"

    with (
        patch("fieldkit.commands.ingest.run._fetch_doc_for_run", return_value=doc_content),
        patch("fieldkit.commands.ingest.run._route_source", return_value=route),
        patch(
            "fieldkit.commands.ingest.run._clean_and_extract_transcript",
            return_value=_CleanResult(cleaned="cleaned text", meta=meta, used_fallback=used_fallback),
        ),
        patch("fieldkit.ingest.router.match_pursuits_for_account", return_value=pursuits or []),
        patch("fieldkit.ingest.pipeline.render_vault_note", return_value="# Note content"),
        patch("fieldkit.ingest.pipeline.compute_vault_path", return_value=vault_path),
        patch("fieldkit.commands.ingest.run.mark_source_status"),
        patch("fieldkit.commands.ingest.run.insert_vault_note_artifact"),
        patch(
            "fieldkit.commands.ingest.run.apply_meeting_writebacks", return_value=writeback_notices
        ) as apply_writebacks,
        patch("fieldkit.commands.ingest.run.human_echo") as human_echo,
    ):
        result = _process_one_source(
            src=src,
            service=MagicMock(),
            conn=MagicMock(),
            data_root=tmp_path,
            pipeline_version="0.2.0",
        )
    if writeback_mocks is not None:
        writeback_mocks.append(apply_writebacks)
    if output_mocks is not None:
        output_mocks.append(human_echo)
    return result


def test_process_one_source_successful_processing_returns_true(tmp_path: Path) -> None:
    result = _process_one_source_run_process(tmp_path)
    assert result.completed is True
    assert result.degraded is False


@pytest.mark.parametrize(
    ("confidence", "used_fallback", "expected_degraded"),
    [
        ("low", False, True),
        ("stub", False, False),
        ("high", True, True),
    ],
)
def test_process_one_source_classifies_successful_degradation(
    tmp_path: Path,
    confidence: str,
    used_fallback: bool,
    expected_degraded: bool,
) -> None:
    result = _process_one_source_run_process(
        tmp_path,
        confidence=confidence,
        used_fallback=used_fallback,
    )

    assert result.completed is True
    assert result.degraded is expected_degraded


def test_process_one_source_fetch_returns_none_returns_false(tmp_path: Path) -> None:
    from fieldkit.commands.ingest.run import _process_one_source

    src = _make_src()
    with patch("fieldkit.commands.ingest.run._fetch_doc_for_run", return_value=None):
        result = _process_one_source(
            src=src,
            service=MagicMock(),
            conn=MagicMock(),
            data_root=tmp_path,
            pipeline_version="0.1.0",
        )
    assert result.completed is False
    assert result.degraded is False


def test_process_one_source_unknown_account_skips_pursuit_enrichment(tmp_path: Path) -> None:
    """account='unknown' branch: skips match_pursuits_for_account call."""
    result = _process_one_source_run_process(tmp_path, account="unknown", pursuits=[])
    assert result.completed is True


def test_process_one_source_with_pursuits_calls_append(tmp_path: Path) -> None:
    writeback_mocks: list[MagicMock] = []
    result = _process_one_source_run_process(
        tmp_path, account="acme", pursuits=["deal-1"], writeback_mocks=writeback_mocks
    )
    assert result.completed is True
    writeback_mocks[0].assert_called_once()
    request = writeback_mocks[0].call_args.args[0]
    assert request.pursuits == ("deal-1",)


def test_process_one_source_with_action_items_calls_sync(tmp_path: Path) -> None:
    writeback_mocks: list[MagicMock] = []
    result = _process_one_source_run_process(
        tmp_path, action_items=["Follow up with CTO"], writeback_mocks=writeback_mocks
    )
    assert result.completed is True
    writeback_mocks[0].assert_called_once()
    request = writeback_mocks[0].call_args.args[0]
    assert request.action_items == ("Follow up with CTO",)


def test_process_one_source_renders_writeback_warning_to_stderr(tmp_path: Path) -> None:
    from fieldkit.ingest.writeback import WritebackNotice

    output_mocks: list[MagicMock] = []
    result = _process_one_source_run_process(
        tmp_path,
        writeback_notices=(WritebackNotice("  Warning: action item sync failed: unavailable", err=True),),
        output_mocks=output_mocks,
    )

    assert result.completed is True
    output_mocks[0].assert_any_call("  Warning: action item sync failed: unavailable", err=True)


def test_process_one_source_no_meeting_date_parses_from_title(tmp_path: Path) -> None:
    """src.meeting_date=None triggers _parse_date_from_title."""
    result = _process_one_source_run_process(tmp_path, meeting_date=None)
    assert result.completed is True


def test_process_one_source_file_lock_used_when_provided(tmp_path: Path) -> None:
    """file_lock kwarg is accepted and used for serialization."""
    from fieldkit.commands.ingest.run import _CleanResult, _process_one_source

    doc_content = GeminiDocContent(
        doc_id="src-abc",
        doc_title="Test",
        transcript_text="text",
        notes_text="",
        invited_emails=[],
    )
    from types import SimpleNamespace as SN

    route = SN(accounts=["acme"], confidence=SN(value="high"), is_internal=False, pursuits=[])
    meta = MagicMock()
    meta.accounts = ["acme"]
    meta.pursuits = []
    meta.action_items = []
    meta.key_topics = []
    meta.key_decisions = []

    src = _make_src()
    vault_path = tmp_path / "vault.md"
    lock = threading.Lock()

    with (
        patch("fieldkit.commands.ingest.run._fetch_doc_for_run", return_value=doc_content),
        patch("fieldkit.commands.ingest.run._route_source", return_value=route),
        patch(
            "fieldkit.commands.ingest.run._clean_and_extract_transcript",
            return_value=_CleanResult(cleaned="cleaned", meta=meta, used_fallback=False),
        ),
        patch("fieldkit.ingest.pipeline.render_vault_note", return_value="content"),
        patch("fieldkit.ingest.pipeline.compute_vault_path", return_value=vault_path),
        patch("fieldkit.commands.ingest.run.mark_source_status"),
        patch("fieldkit.commands.ingest.run.insert_vault_note_artifact"),
        patch("fieldkit.commands.ingest.run.apply_meeting_writebacks", return_value=()),
    ):
        result = _process_one_source(
            src=src,
            service=MagicMock(),
            conn=MagicMock(),
            data_root=tmp_path,
            pipeline_version="0.2.0",
            file_lock=lock,
        )

    assert result.completed is True


def test_process_one_source_doc_content_with_notes_text_not_transcript(tmp_path: Path) -> None:
    """Fallback from transcript_text=None to notes_text."""
    doc = GeminiDocContent(
        doc_id="src-abc",
        doc_title="Notes Meeting",
        transcript_text=None,
        notes_text="Bob: Good notes here.",
        invited_emails=["bob@acme.example.com"],
    )
    result = _process_one_source_run_process(tmp_path, doc_content=doc)
    assert result.completed is True


def test_clean_and_extract_stage1_fallback_is_degraded() -> None:
    from fieldkit.commands.ingest.run import _clean_and_extract_transcript
    from fieldkit.ingest.pipeline import TranscriptMeta

    doc = GeminiDocContent(doc_id="src-1", doc_title="Test", transcript_text="raw transcript", notes_text="")
    with (
        patch("fieldkit.ingest.pipeline.stage1_clean", side_effect=RuntimeError("stage 1 unavailable")),
        patch("fieldkit.ingest.pipeline.stage2_extract", return_value=TranscriptMeta(confidence="high")),
    ):
        result = _clean_and_extract_transcript("src-1", doc)

    assert result.used_fallback is True
    assert result.cleaned == "raw transcript"


def test_clean_and_extract_stage2_fallback_is_degraded() -> None:
    from fieldkit.commands.ingest.run import _clean_and_extract_transcript
    from fieldkit.ingest.pipeline import Stage1Result

    doc = GeminiDocContent(doc_id="src-1", doc_title="Test", transcript_text="raw transcript", notes_text="")
    with (
        patch("fieldkit.ingest.pipeline.stage1_clean", return_value=Stage1Result("cleaned transcript")),
        patch("fieldkit.ingest.pipeline.stage2_extract", side_effect=RuntimeError("stage 2 unavailable")),
    ):
        result = _clean_and_extract_transcript("src-1", doc)

    assert result.used_fallback is True
    assert result.meta.confidence == "low"


def test_batch_outcomes_degraded_field_is_opt_in(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.ingest._output import BatchOutcomes

    outcomes = BatchOutcomes(completed=["src-1"], degraded=["src-1"])
    outcomes.emit(pipeline="transcript-ingest", dry_run=False)
    compatible_payload = json.loads(capsys.readouterr().out)
    assert "degraded" not in compatible_payload

    outcomes.emit(pipeline="transcript-ingest", dry_run=False, include_degraded=True)
    enriched_payload = json.loads(capsys.readouterr().out)
    assert enriched_payload["degraded"] == ["src-1"]


# ---------------------------------------------------------------------------
# historic regression: _fetch_doc_for_run times out after 30s
# ---------------------------------------------------------------------------


# ── TestFetchDocForRunTimeout (flattened) ───────────────────────────────────


def test_fetch_doc_for_run_timeout_marks_failed_and_returns_none(tmp_path: Path) -> None:
    """When future.result() raises TimeoutError, _fetch_doc_for_run returns None
    and calls mark_source_status with 'failed'."""
    import sqlite3
    from concurrent.futures import Future
    from concurrent.futures import TimeoutError as FuturesTimeoutError

    from fieldkit.commands.ingest import run as run_mod

    conn = MagicMock(spec=sqlite3.Connection)
    service = MagicMock()
    source_id = "DOC_TIMEOUT_001"

    # Mock the future so result() immediately raises TimeoutError — no real sleep.
    mock_future: Future[object] = MagicMock(spec=Future)
    mock_future.result.side_effect = FuturesTimeoutError()

    mock_pool = MagicMock()
    mock_pool.__enter__ = MagicMock(return_value=mock_pool)
    mock_pool.__exit__ = MagicMock(return_value=False)
    mock_pool.submit.return_value = mock_future

    with (
        patch("fieldkit.commands.ingest.run.ThreadPoolExecutor", return_value=mock_pool),
        patch.object(run_mod, "mark_source_status") as mock_mark,
    ):
        result = run_mod._fetch_doc_for_run(service, source_id, conn)

    assert result is None
    mock_mark.assert_called_once_with(conn, source_id, "failed")


def test_fetch_doc_for_run_transient_error_returns_source_to_pending() -> None:
    """An unexpected fetch error must leave the claimed source eligible for resume."""
    from fieldkit.commands.ingest import run as run_mod

    conn = MagicMock()
    with (
        patch("fieldkit.ingest.docs.fetch_gemini_doc", side_effect=RuntimeError("temporary network error")),
        patch.object(run_mod, "mark_source_status") as mock_mark,
    ):
        result = run_mod._fetch_doc_for_run(MagicMock(), "DOC_RETRY_001", conn)

    assert result is None
    mock_mark.assert_called_once_with(conn, "DOC_RETRY_001", "pending")


def test_fetch_doc_for_run_transient_error_requeues_claimed_database_source(tmp_path: Path) -> None:
    """A transient fetch error makes an already-claimed source selectable again."""
    from fieldkit.commands.ingest import run as run_mod
    from fieldkit.commands.ingest.registry import PIPELINES
    from fieldkit.ingest.db import init_db
    from fieldkit.ingest.sources import claim_pending_source, get_pending_sources

    source_id = "DOC_RETRY_DATABASE"
    conn = init_db(tmp_path / "pipeline.db", pipelines=PIPELINES)
    conn.execute(
        "INSERT INTO sources (source_id, pipeline_id, file_path, status) "
        "VALUES (?, 'transcript-ingest', '/fake/path', 'pending')",
        (source_id,),
    )
    conn.commit()
    assert claim_pending_source(conn, source_id) is True

    with patch("fieldkit.ingest.docs.fetch_gemini_doc", side_effect=RuntimeError("temporary network error")):
        result = run_mod._fetch_doc_for_run(MagicMock(), source_id, conn)

    assert result is None
    row = conn.execute("SELECT status FROM sources WHERE source_id = ?", (source_id,)).fetchone()
    assert row is not None
    assert row[0] == "pending"
    assert [source.source_id for source in get_pending_sources(conn, "transcript-ingest")] == [source_id]
    conn.close()
