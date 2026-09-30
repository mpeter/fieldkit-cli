"""Additional branch coverage for fieldkit.ingest.run._run_processing_loop
and _process_one_source — targets the uncovered 28% branches."""

import json
from contextlib import closing, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.commands.ingest.registry import PIPELINES
from fieldkit.commands.ingest.run import _ProcessResult
from fieldkit.errors import AuthError, LLMError
from fieldkit.ingest.db import get_db, init_db
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
    pending: list[SourceRecord],
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
        patch("fieldkit.commands.ingest.run.load_prepared", return_value=None),
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


def test_run_parallel_loop_single_source_error_returns_partial_failure() -> None:
    """Human output must not hide a failed source behind a successful exit."""
    rc = _run_loop([_make_src()], process_result=False)
    assert rc == 1


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
        patch("fieldkit.commands.ingest.run.load_prepared", return_value=None),
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
        patch("fieldkit.commands.ingest.run.load_prepared", return_value=None),
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
        patch("fieldkit.ingest.sources.claim_pending_source", return_value=True),
        patch("fieldkit.config.get_fieldkit_home", return_value=Path("/tmp/fake-data")),
        patch("fieldkit.ingest.db.get_db_path", return_value=Path("/tmp/fake.db")),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=MagicMock()),
        patch("fieldkit.commands.ingest.run._prompt_process_choice", return_value="y"),
        patch("fieldkit.commands.ingest.run.load_prepared", return_value=None),
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

    assert (processed, degraded, skipped, errors) == (1, 0, 0, 1)
    assert outcomes.completed == ["src-0"]
    assert outcomes.pending == ["src-1", "src-2"]


def test_run_parallel_loop_worker_exception_counted_not_raised(capsys: pytest.CaptureFixture[str]) -> None:
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
        patch("fieldkit.commands.ingest.run.load_prepared", return_value=None),
        patch("fieldkit.commands.ingest.run._process_one_source", side_effect=RuntimeError("private-document-payload")),
    ):
        rc = _run_processing_loop([src], conn=mock_conn, pipeline_version="0.2.0", interactive=False)

    assert rc == 1
    diagnostic = capsys.readouterr().err
    assert "Source processing failed" in diagnostic
    assert "private-document-payload" not in diagnostic


def test_run_parallel_loop_many_workers_capped() -> None:
    """40+ sources triggers 8-worker cap — verify loop completes."""
    sources = [_make_src(f"src-{i}") for i in range(40)]
    rc = _run_loop(sources, process_result=True)
    assert rc == 0


# ---------------------------------------------------------------------------
# _run_processing_loop — interactive mode branches
# ---------------------------------------------------------------------------


# ── TestRunProcessingLoopInteractive (flattened) ────────────────────────────


@pytest.mark.parametrize(
    ("status", "choice", "expected"),
    [
        ("pending", "y", (1, 0, 0, 0)),
        ("in_progress", "y", (0, 0, 1, 0)),
        ("processed", "y", (0, 0, 1, 0)),
        ("failed", "y", (0, 0, 1, 0)),
        ("pending", "n", (0, 0, 1, 0)),
        ("pending", "q", (0, 0, 0, 0)),
    ],
)
def test_interactive_processing_requires_a_durable_claim(
    tmp_path: Path, status: str, choice: str, expected: tuple[int, int, int, int]
) -> None:
    from fieldkit.commands.ingest.run import _run_interactive_loop

    db_path = tmp_path / "pipeline.db"
    src = _make_src()

    def process_claimed_source(**kwargs: object) -> _ProcessResult:
        with closing(get_db(db_path)) as reader:
            row = reader.execute("SELECT status FROM sources WHERE source_id = ?", (src.source_id,)).fetchone()
            assert row["status"] == "in_progress"
        return _ProcessResult(completed=True, degraded=False)

    with closing(init_db(db_path, pipelines=PIPELINES)) as conn:
        conn.execute(
            "INSERT INTO sources (source_id, pipeline_id, file_path, status) VALUES (?, ?, '', ?)",
            (src.source_id, src.pipeline_id, status),
        )
        conn.commit()
        with (
            patch("fieldkit.commands.ingest.run._prompt_process_choice", return_value=choice),
            patch("fieldkit.commands.ingest.run._process_one_source", side_effect=process_claimed_source) as process,
        ):
            result = _run_interactive_loop(
                [src], service=object(), conn=conn, data_root=tmp_path, pipeline_version="0.1.0"
            )

        assert result == expected
        assert process.call_count == expected[0]
        final_status = conn.execute("SELECT status FROM sources WHERE source_id = ?", (src.source_id,)).fetchone()[0]
        assert final_status == ("in_progress" if expected[0] else status)


def test_run_processing_loop_interactive_yes_success() -> None:
    from fieldkit.commands.ingest.run import _run_processing_loop

    src = _make_src()
    mock_conn = MagicMock()

    with (
        patch("fieldkit.ingest.sources.claim_pending_source", return_value=True),
        patch("fieldkit.config.get_fieldkit_home", return_value=Path("/tmp/fake-data")),
        patch("fieldkit.ingest.db.get_db_path", return_value=Path("/tmp/fake.db")),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=MagicMock()),
        patch("fieldkit.commands.ingest.run._prompt_process_choice", return_value="y"),
        patch("fieldkit.commands.ingest.run.load_prepared", return_value=None),
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
        patch("fieldkit.ingest.sources.claim_pending_source", return_value=True),
        patch("fieldkit.config.get_fieldkit_home", return_value=Path("/tmp/fake-data")),
        patch("fieldkit.ingest.db.get_db_path", return_value=Path("/tmp/fake.db")),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=MagicMock()),
        patch("fieldkit.commands.ingest.run._prompt_process_choice", return_value="y"),
        patch("fieldkit.commands.ingest.run.load_prepared", return_value=None),
        patch(
            "fieldkit.commands.ingest.run._process_one_source",
            return_value=_ProcessResult(completed=False, degraded=False),
        ),
    ):
        rc = _run_processing_loop([src], conn=mock_conn, pipeline_version="0.1.0", interactive=True)

    assert rc == 1


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

    assert rc == 1


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
        patch("fieldkit.commands.ingest.run.load_prepared", return_value=None),
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


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("outcome", ["success", "fetch_failure", "auth_failure", "prepare_failure", "replay_failure"])
def test_source_service_lifetime(owned: bool, outcome: str, tmp_path: Path) -> None:
    from fieldkit.commands.ingest import run

    service = MagicMock()
    connection = MagicMock()
    events = MagicMock()
    events.attach_mock(service.close, "close")
    document = GeminiDocContent("src-abc", "Meeting", "Notes", None)
    with (
        patch.object(run, "load_prepared", return_value=None),
        patch.object(run, "require_optional_profile"),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=service) as create,
        patch.object(run, "_fetch_doc_for_run", return_value=document) as fetch,
        patch.object(run, "prepare_meeting") as prepare,
        patch.object(run, "save_prepared") as save,
        patch.object(run, "_replay_source", return_value=_ProcessResult(completed=True, degraded=False)) as replay,
    ):
        events.attach_mock(prepare, "prepare")
        events.attach_mock(save, "save")
        events.attach_mock(replay, "replay")
        if outcome == "fetch_failure":
            fetch.return_value = None
        elif outcome == "auth_failure":
            fetch.side_effect = AuthError("synthetic failure")
        elif outcome == "prepare_failure":
            prepare.side_effect = ValueError("synthetic failure")
        elif outcome == "replay_failure":
            replay.side_effect = ValueError("synthetic failure")
        with (
            pytest.raises(AuthError if outcome == "auth_failure" else ValueError, match="synthetic failure")
            if outcome in {"auth_failure", "prepare_failure", "replay_failure"}
            else nullcontext()
        ):
            result = run._process_one_source(
                src=_make_src(),
                service=None if owned else service,
                conn=connection,
                data_root=tmp_path,
                pipeline_version="0.1.0",
            )
            assert result.completed is (outcome == "success")
        fetch.assert_called_once_with(service, "src-abc", connection)
    if owned:
        create.assert_called_once()
        service.close.assert_called_once()
        observed = [call[0] for call in events.mock_calls]
        assert observed[0] == "close"
        if outcome == "success":
            assert observed == ["close", "prepare", "save", "replay"]
    else:
        create.assert_not_called()
        service.close.assert_not_called()


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
    output_mocks: list[MagicMock] | None = None,
) -> _ProcessResult:
    from fieldkit.commands.ingest.run import _process_one_source
    from fieldkit.ingest.preparation import _CleanResult

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
    from fieldkit.tasks.classifier import ClassifiedItem, ItemClass

    classified = tuple(
        ClassifiedItem(text, ItemClass.MY_TASK, "Alice", "Assigned", "Acme") for text in (action_items or [])
    )

    with (
        patch("fieldkit.commands.ingest.run.load_prepared", return_value=None),
        patch("fieldkit.commands.ingest.run._fetch_doc_for_run", return_value=doc_content),
        patch("fieldkit.ingest.preparation._route_source", return_value=route),
        patch(
            "fieldkit.ingest.preparation.clean_and_extract_transcript",
            return_value=_CleanResult(cleaned="cleaned text", meta=meta, used_fallback=used_fallback),
        ),
        patch("fieldkit.ingest.router.match_pursuits_for_account", return_value=pursuits or []),
        patch("fieldkit.ingest.pipeline.render_vault_note", return_value="---\ntype: meeting\n---\n# Note content"),
        patch("fieldkit.commands.ingest.run.mark_source_status"),
        patch("fieldkit.ingest.preparation.classify_meeting_tasks", return_value=classified),
        patch("fieldkit.commands.ingest.run.save_prepared") as save,
        patch("fieldkit.commands.ingest.run.replay_prepared"),
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
        writeback_mocks.append(save)
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
    with (
        patch("fieldkit.commands.ingest.run.load_prepared", return_value=None),
        patch("fieldkit.commands.ingest.run._fetch_doc_for_run", return_value=None),
    ):
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
    request = writeback_mocks[0].call_args.args[1]
    assert request.pursuits == ("deal-1",)


def test_process_one_source_with_action_items_calls_sync(tmp_path: Path) -> None:
    writeback_mocks: list[MagicMock] = []
    result = _process_one_source_run_process(
        tmp_path, action_items=["Follow up with CTO"], writeback_mocks=writeback_mocks
    )
    assert result.completed is True
    writeback_mocks[0].assert_called_once()
    request = writeback_mocks[0].call_args.args[1]
    assert request.action_items == ("Follow up with CTO",)


def test_process_one_source_renders_success_after_replay(tmp_path: Path) -> None:
    output_mocks: list[MagicMock] = []
    result = _process_one_source_run_process(
        tmp_path,
        output_mocks=output_mocks,
    )

    assert result.completed is True
    assert any(str(call.args[0]).startswith("  Wrote:") for call in output_mocks[0].call_args_list)


def test_process_one_source_no_meeting_date_parses_from_title(tmp_path: Path) -> None:
    """src.meeting_date=None triggers _parse_date_from_title."""
    result = _process_one_source_run_process(tmp_path, meeting_date=None)
    assert result.completed is True


@pytest.mark.parametrize("failure_type", [None, OSError, AuthError])
def test_process_one_source_journals_before_replay_and_propagates_authentication(
    tmp_path: Path, failure_type: type[Exception] | None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Replay follows journal persistence and never masks authentication failures."""
    from fieldkit.commands.ingest.run import _process_one_source
    from fieldkit.ingest.preparation import _CleanResult

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
    meta.confidence = "high"

    src = _make_src()
    connection = MagicMock()
    failure = failure_type("private-task-payload", str(tmp_path / "private-tasks.md")) if failure_type else None
    result: _ProcessResult | None = None
    calls = MagicMock()

    with (
        patch("fieldkit.commands.ingest.run.load_prepared", return_value=None),
        patch("fieldkit.commands.ingest.run._fetch_doc_for_run", return_value=doc_content),
        patch("fieldkit.ingest.preparation._route_source", return_value=route),
        patch(
            "fieldkit.ingest.preparation.clean_and_extract_transcript",
            return_value=_CleanResult(cleaned="cleaned", meta=meta, used_fallback=False),
        ),
        patch("fieldkit.ingest.pipeline.render_vault_note", return_value="---\ntype: meeting\n---\ncontent"),
        patch("fieldkit.ingest.router.match_pursuits_for_account", return_value=[]),
        patch("fieldkit.ingest.preparation.classify_meeting_tasks", return_value=()),
        patch("fieldkit.commands.ingest.run.mark_source_status") as mark_status,
        patch("fieldkit.commands.ingest.run.save_prepared") as save,
        patch(
            "fieldkit.commands.ingest.run.replay_prepared",
            side_effect=failure,
            return_value=tmp_path / "note.md",
        ) as replay,
        pytest.raises(AuthError, match="private-task-payload")
        if isinstance(failure, AuthError)
        else nullcontext() as caught,
    ):
        calls.attach_mock(save, "save")
        calls.attach_mock(replay, "replay")
        result = _process_one_source(
            src=src,
            service=MagicMock(),
            conn=connection,
            data_root=tmp_path,
            pipeline_version="0.2.0",
        )

    if isinstance(failure, AuthError):
        assert result is None
        assert caught is not None
        assert caught.value is failure
    elif failure is not None:
        assert result is not None
        assert result.completed is False
        diagnostic = capsys.readouterr().err
        assert "Required meeting writeback failed" in diagnostic
        assert "private-task-payload" not in diagnostic
        assert str(tmp_path) not in diagnostic
    else:
        assert result is not None
        assert result.completed is True
    save.assert_called_once()
    replay.assert_called_once_with(connection, src.source_id, tmp_path)
    assert [call[0] for call in calls.mock_calls] == ["save", "replay"]
    mark_status.assert_not_called()


@pytest.mark.parametrize("interactive", [False, True])
@pytest.mark.parametrize(
    "failure",
    [
        AuthError("Re-authentication required"),
        LLMError("provider failure", "auth"),
        LLMError("provider failure", "rate-limit"),
        LLMError("provider failure", "general"),
    ],
)
def test_processing_loop_preserves_authentication_failure(
    interactive: bool, tmp_path: Path, failure: Exception
) -> None:
    from fieldkit.commands.ingest.run import _run_processing_loop

    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
        patch("fieldkit.ingest.db.get_db", return_value=MagicMock()),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=MagicMock()),
        patch("fieldkit.ingest.sources.claim_pending_source", return_value=True),
        patch("fieldkit.commands.ingest.run._prompt_process_choice", return_value="y"),
        patch("fieldkit.commands.ingest.run.load_prepared", return_value=None),
        patch("fieldkit.commands.ingest.run._process_one_source", side_effect=failure) as process,
        pytest.raises(type(failure), match=r"required|provider failure") as caught,
    ):
        _run_processing_loop([_make_src()], conn=MagicMock(), pipeline_version="0.1.0", interactive=interactive)
    assert caught.value is failure
    process.assert_called_once()


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


def test_fetch_authentication_failure_propagates_without_retry_status(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.ingest.run import _fetch_doc_for_run

    failure = AuthError("synthetic credential failure")
    with (
        patch("fieldkit.ingest.docs.fetch_gemini_doc", side_effect=failure),
        patch("fieldkit.commands.ingest.run.mark_source_status") as mark,
        pytest.raises(AuthError, match="synthetic credential failure") as caught,
    ):
        _fetch_doc_for_run(object(), "src-auth", MagicMock())
    assert caught.value is failure
    mark.assert_not_called()
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("stage", ["stage1_clean", "stage2_extract"])
@pytest.mark.parametrize(
    "failure",
    [
        AuthError("synthetic credential failure"),
        LLMError("provider failure", "auth"),
        LLMError("provider failure", "rate-limit"),
        LLMError("provider failure", "general"),
    ],
)
def test_extraction_authentication_failure_never_degrades(
    stage: str, capsys: pytest.CaptureFixture[str], failure: Exception
) -> None:
    from fieldkit.ingest.pipeline import Stage1Result, TranscriptMeta
    from fieldkit.ingest.preparation import clean_and_extract_transcript

    doc = GeminiDocContent(doc_id="src-auth", doc_title="Test", transcript_text="raw", notes_text="")
    with (
        patch("fieldkit.ingest.pipeline.stage1_clean", return_value=Stage1Result("cleaned")) as clean,
        patch("fieldkit.ingest.pipeline.stage2_extract", return_value=TranscriptMeta()) as extract,
    ):
        failing_stage = clean if stage == "stage1_clean" else extract
        failing_stage.side_effect = failure
        with pytest.raises(type(failure), match=r"credential failure|provider failure") as caught:
            clean_and_extract_transcript(doc, MagicMock())
    assert caught.value is failure
    if stage == "stage1_clean":
        extract.assert_not_called()
    assert capsys.readouterr().err == ""


def test_clean_and_extract_stage1_fallback_is_degraded() -> None:
    from fieldkit.ingest import pipeline
    from fieldkit.ingest.pipeline import Stage1Result
    from fieldkit.ingest.preparation import clean_and_extract_transcript

    doc = GeminiDocContent(doc_id="src-1", doc_title="Test", transcript_text="raw transcript", notes_text="")
    with (
        patch("fieldkit.ingest.pipeline.stage1_clean", side_effect=RuntimeError("stage 1 unavailable")),
        patch("fieldkit.ingest.pipeline.llm_disabled", return_value=False),
        patch("fieldkit.ingest.pipeline.synthesize", return_value='{"confidence": "high"}') as provider,
        patch("fieldkit.ingest.pipeline.stage2_extract", wraps=pipeline.stage2_extract) as extract,
    ):
        result = clean_and_extract_transcript(doc, MagicMock())

    assert result.used_fallback is True
    assert result.cleaned == "raw transcript"
    assert result.meta.confidence == "low"
    extract.assert_called_once_with(Stage1Result("raw transcript", bypassed=True))
    provider.assert_called_once()


def test_clean_and_extract_stage2_fallback_is_degraded() -> None:
    from fieldkit.ingest.pipeline import Stage1Result
    from fieldkit.ingest.preparation import clean_and_extract_transcript

    doc = GeminiDocContent(doc_id="src-1", doc_title="Test", transcript_text="raw transcript", notes_text="")
    with (
        patch("fieldkit.ingest.pipeline.stage1_clean", return_value=Stage1Result("cleaned transcript")),
        patch("fieldkit.ingest.pipeline.stage2_extract", side_effect=RuntimeError("stage 2 unavailable")),
    ):
        result = clean_and_extract_transcript(doc, MagicMock())

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
# Transport failures leave sources retryable without detached fetch workers.
# ---------------------------------------------------------------------------


# ── TestFetchDocForRunTimeout (flattened) ───────────────────────────────────


def test_fetch_doc_for_run_timeout_marks_pending_without_background_worker() -> None:
    """Transport timeouts remain retryable without launching a fetch thread."""
    import sqlite3

    from fieldkit.commands.ingest import run as run_mod

    conn = MagicMock(spec=sqlite3.Connection)
    service = MagicMock()
    source_id = "DOC_TIMEOUT_001"

    with (
        patch("fieldkit.commands.ingest.run.ThreadPoolExecutor") as pool,
        patch("fieldkit.ingest.docs.fetch_gemini_doc", side_effect=TimeoutError("private payload")),
        patch.object(run_mod, "mark_source_status") as mock_mark,
    ):
        result = run_mod._fetch_doc_for_run(service, source_id, conn)

    assert result is None
    mock_mark.assert_called_once_with(conn, source_id, "pending")
    pool.assert_not_called()


def test_fetch_doc_for_run_transient_error_returns_source_to_pending(capsys: pytest.CaptureFixture[str]) -> None:
    """An unexpected fetch error must leave the claimed source eligible for resume."""
    from fieldkit.commands.ingest import run as run_mod

    conn = MagicMock()
    with (
        patch("fieldkit.ingest.docs.fetch_gemini_doc", side_effect=RuntimeError("SENTINEL-PRIVATE-PAYLOAD")),
        patch.object(run_mod, "mark_source_status") as mock_mark,
    ):
        result = run_mod._fetch_doc_for_run(MagicMock(), "DOC_RETRY_001", conn)

    assert result is None
    mock_mark.assert_called_once_with(conn, "DOC_RETRY_001", "pending")
    diagnostic = capsys.readouterr().err
    assert "SENTINEL-PRIVATE-PAYLOAD" not in diagnostic
    assert "retry" in diagnostic.lower()


@pytest.mark.parametrize("stage", ["stage1_clean", "stage2_extract"])
def test_extraction_diagnostics_do_not_reflect_provider_payload(stage: str) -> None:
    from fieldkit.ingest.pipeline import Stage1Result, TranscriptMeta
    from fieldkit.ingest.preparation import clean_and_extract_transcript

    messages: list[str] = []
    doc = GeminiDocContent(doc_id="src-1", doc_title="Test", transcript_text="raw", notes_text="")
    with (
        patch("fieldkit.ingest.pipeline.stage1_clean", return_value=Stage1Result("cleaned")) as clean,
        patch("fieldkit.ingest.pipeline.stage2_extract", return_value=TranscriptMeta()) as extract,
    ):
        failing_stage = clean if stage == "stage1_clean" else extract
        failing_stage.side_effect = RuntimeError("SENTINEL-PRIVATE-PAYLOAD")
        result = clean_and_extract_transcript(doc, messages.append)
    assert result.used_fallback is True
    assert len(messages) == 1
    assert "SENTINEL-PRIVATE-PAYLOAD" not in messages[0]
    assert "review" in messages[0].lower()


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


@pytest.mark.parametrize("retryable", [False, True])
def test_process_routing_failure_restores_claim_before_any_effect(tmp_path: Path, retryable: bool) -> None:
    from fieldkit.commands.ingest import run as run_mod
    from fieldkit.config import ConfigError
    from fieldkit.errors import RoutingReadRetryableError
    from fieldkit.ingest.sources import claim_pending_source

    conn = init_db(tmp_path / "pipeline.db", pipelines=PIPELINES)
    source = _make_src()
    conn.execute(
        "INSERT INTO sources(source_id, pipeline_id, file_path, status) VALUES (?, 'transcript-ingest', 'fictional', 'pending')",
        (source.source_id,),
    )
    conn.commit()
    assert claim_pending_source(conn, source.source_id) is True
    failure = RoutingReadRetryableError("Cannot inspect pursuits") if retryable else ConfigError("Invalid pursuit path")
    with (
        patch.object(run_mod, "load_prepared", return_value=None),
        patch.object(
            run_mod,
            "_fetch_doc_for_run",
            return_value=GeminiDocContent(
                doc_id=source.source_id, doc_title="Fictional", notes_text="", transcript_text=""
            ),
        ),
        patch.object(run_mod, "prepare_meeting", side_effect=failure),
        patch.object(run_mod, "save_prepared") as save,
        patch.object(run_mod, "_replay_source") as writer,
        pytest.raises(type(failure), match="pursuit") as caught,
    ):
        run_mod._process_one_source(
            src=source, service=object(), conn=conn, data_root=tmp_path, pipeline_version="0.1.0"
        )
    assert caught.value is failure
    assert conn.execute("SELECT status FROM sources WHERE source_id = ?", (source.source_id,)).fetchone()[0] == (
        "pending" if retryable else "failed"
    )
    save.assert_not_called()
    writer.assert_not_called()
    conn.close()


def test_parallel_processing_preserves_invalid_routing_category(tmp_path: Path) -> None:
    from fieldkit.commands.ingest import run as run_mod
    from fieldkit.config import ConfigError

    failure = ConfigError("Invalid pursuit path")
    with (
        patch("fieldkit.ingest.db.get_db", return_value=MagicMock()),
        patch("fieldkit.ingest.sources.claim_pending_source", return_value=True),
        patch.object(run_mod, "require_optional_profile"),
        patch.object(run_mod, "load_prepared", return_value=None),
        patch.object(run_mod, "_process_one_source", side_effect=failure),
        pytest.raises(ConfigError, match="Invalid pursuit path") as caught,
    ):
        run_mod._run_parallel_loop(
            [_make_src()], data_root=tmp_path, db_path=tmp_path / "pipeline.db", pipeline_version="0.1.0"
        )
    assert caught.value is failure


def test_parallel_retryable_routing_is_pending_not_completed(tmp_path: Path) -> None:
    from fieldkit.commands.ingest import run as run_mod
    from fieldkit.commands.ingest._output import BatchOutcomes
    from fieldkit.errors import RoutingReadRetryableError

    outcomes = BatchOutcomes()
    with (
        patch("fieldkit.ingest.db.get_db", return_value=MagicMock()),
        patch("fieldkit.ingest.sources.claim_pending_source", return_value=True),
        patch.object(run_mod, "require_optional_profile"),
        patch.object(run_mod, "load_prepared", return_value=None),
        patch.object(run_mod, "_process_one_source", side_effect=RoutingReadRetryableError("Cannot inspect pursuits")),
    ):
        result = run_mod._run_parallel_loop(
            [_make_src()],
            data_root=tmp_path,
            db_path=tmp_path / "pipeline.db",
            pipeline_version="0.1.0",
            outcomes=outcomes,
        )
    assert result == (0, 0, 0, 1)
    assert outcomes.pending == ["src-abc"]
    assert outcomes.failed == []
    assert outcomes.completed == []


@pytest.mark.parametrize("routing_mode", ["domains", "title"])
def test_preparation_routes_and_enriches_using_selected_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, routing_mode: str
) -> None:
    from fieldkit.config import clear_config_caches
    from fieldkit.ingest import preparation
    from fieldkit.ingest.pipeline import TranscriptMeta

    selected = tmp_path / "selected"
    configured = tmp_path / "configured"
    for root, account in ((selected, "acme"), (configured, "wrong")):
        config = root / "config"
        config.mkdir(parents=True)
        (config / "accounts.yaml").write_text(
            f"accounts:\n  {account}:\n    domains: [acme-corp.example.com]\n    keywords: [phoenix]\n"
            f"    pursuit_dir: accounts/{account}/pursuits\n",
            encoding="utf-8",
        )
        pursuits = root / "accounts" / account / "pursuits"
        pursuits.mkdir(parents=True)
        (pursuits / "phoenix-planning.md").write_text("# Phoenix\n", encoding="utf-8")
        (pursuits / "topic-only.md").write_text("# Nebula\n", encoding="utf-8")
    config_path = tmp_path / "fieldkit.yaml"
    config_path.write_text(f"fieldkit_home: {configured}\n", encoding="utf-8")
    monkeypatch.setattr("fieldkit.config._loader.CONFIG_PATH", config_path)
    clear_config_caches()
    doc = GeminiDocContent(
        doc_id="src-abc",
        doc_title="Phoenix discussion",
        notes_text="",
        transcript_text="Fictional discussion",
        invited_emails=["alice@acme-corp.example.com"] if routing_mode == "domains" else [],
    )
    clean = preparation._CleanResult("Fictional discussion", TranscriptMeta(key_topics=["nebula"]), False)
    with (
        patch.object(preparation, "clean_and_extract_transcript", return_value=clean),
        patch.object(preparation, "classify_meeting_tasks", return_value=[]),
    ):
        result = preparation.prepare_meeting(
            src=_make_src(meeting_title="Phoenix discussion"),
            doc_content=doc,
            data_root=selected,
            pipeline_version="0.1.0",
            report_warning=lambda _message: None,
        )
    assert result.account == "acme"
    assert result.pursuits == ("phoenix-planning", "topic-only")
    assert result.vault_relative_path.startswith("accounts/acme/meetings/")
    assert list(selected.glob("accounts/*/meetings/*.md")) == []
    assert list(configured.glob("accounts/*/meetings/*.md")) == []
