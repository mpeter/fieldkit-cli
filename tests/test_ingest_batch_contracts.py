"""Direct contracts for provider boundaries and ordered ingest accounting."""

import sqlite3
import threading
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fieldkit.commands.ingest import run
from fieldkit.commands.ingest._output import BatchOutcomes, provider_stop_metadata
from fieldkit.commands.ingest.registry import PIPELINES
from fieldkit.config import ConfigError
from fieldkit.errors import AuthError, LLMError, LLMErrorCategory, LLMErrorScope, _normalize_llm_policy
from fieldkit.ingest.db import init_db
from fieldkit.ingest.docs import DocAccessDeniedError, DocNotFoundError, GeminiDocContent
from fieldkit.ingest.pipeline import Stage1Result, TranscriptMeta
from fieldkit.ingest.provider_failures import BatchProviderFailures, track_provider_failures, tracked_synthesis
from fieldkit.ingest.sources import SourceRecord

pytestmark = pytest.mark.unit


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = init_db(tmp_path / "pipeline.db", pipelines=PIPELINES)
    connection.execute(
        "INSERT INTO sources (source_id, pipeline_id, file_path) VALUES ('example', 'transcript-ingest', 'example-doc')"
    )
    connection.commit()
    yield connection
    connection.close()


def source(source_id: str = "example") -> SourceRecord:
    return SourceRecord(
        source_id=source_id,
        pipeline_id="transcript-ingest",
        discovered_at="2026-01-01T00:00:00Z",
        subject="Example meeting",
        meeting_title="Example meeting",
        meeting_date=None,
        doc_url="https://docs.google.com/document/d/example-doc",
        email_message_id="example-message",
    )


@pytest.mark.parametrize(
    "category,retryable,scope,expected",
    [
        ("auth", True, "source", (False, "provider")),
        ("rate-limit", False, "source", (True, "provider")),
        ("general", True, "source", (True, "provider")),
        ("general", False, "source", (False, "source")),
        ("general", False, "provider", (False, "provider")),
    ],
)
def test_normalize_llm_policy(
    category: LLMErrorCategory,
    retryable: bool,
    scope: LLMErrorScope,
    expected: tuple[bool, LLMErrorScope],
) -> None:
    result = _normalize_llm_policy(category, retryable, scope)
    assert result == expected
    original = RuntimeError("fictional response")
    error = LLMError("safe diagnostic", category, original, retryable=retryable, scope=scope)
    assert (error.retryable, error.scope) == result
    assert error.original is original
    assert str(error) == f"[LLMError/{category}] safe diagnostic"


@pytest.mark.parametrize(
    "error,expected",
    [
        (LLMError("outage", retryable=True), True),
        (LLMError("auth", category="auth"), False),
        (LLMError("source", scope="source"), False),
        (RuntimeError("raw provider error"), False),
    ],
)
def test_retryable_provider_error(error: BaseException, expected: bool) -> None:
    from fieldkit.llm.core import _retryable_provider_error

    result = _retryable_provider_error(error)
    assert result is expected


def test_run_ambient_transcript_ingest_rejects_interactive(capsys: pytest.CaptureFixture[str]) -> None:
    result = run._run_ambient_transcript_ingest(
        spec=object(), dry_run=False, limit=None, interactive=True, as_json=False
    )
    assert result == 1
    assert "not supported" in capsys.readouterr().err


def test_provider_stop_metadata() -> None:
    result = provider_stop_metadata("transcript-ingest", 3)
    assert result == {
        "stop_reason": "consecutive_retryable_provider_failures",
        "provider_failure_threshold": 3,
        "resume_command": "fieldkit ingest run --pipeline transcript-ingest",
    }
    assert provider_stop_metadata("transcript-ingest", None) == {}


def test_tracked_synthesis_resets_before_output_validation() -> None:
    batch = BatchProviderFailures(2)
    batch.record(LLMError("outage", category="rate-limit"))
    with track_provider_failures(batch):
        result = tracked_synthesis("fictional prompt", lambda _prompt: "malformed JSON")
    assert result == "malformed JSON"
    batch.record(LLMError("outage", category="rate-limit"))
    assert batch.stopped is False
    batch.record(LLMError("outage", category="rate-limit"))
    assert batch.stopped is True


@pytest.mark.parametrize("scope,retryable,stopped", [("source", False, False), ("provider", True, True)])
def test_tracked_synthesis_records_only_provider_exhaustion(
    scope: LLMErrorScope, retryable: bool, stopped: bool
) -> None:
    batch = BatchProviderFailures(1)
    error = LLMError("safe failure", scope=scope, retryable=retryable)
    with track_provider_failures(batch), pytest.raises(LLMError, match="safe failure") as captured:
        tracked_synthesis("prompt", MagicMock(side_effect=error))
    assert captured.value is error
    assert batch.stopped is stopped


def test_tracked_synthesis_shares_copied_worker_context() -> None:
    from contextvars import copy_context

    batch = BatchProviderFailures(1)
    error = LLMError("outage", category="rate-limit")
    with track_provider_failures(batch), ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(copy_context().run, tracked_synthesis, "prompt", MagicMock(side_effect=error))
        with pytest.raises(LLMError, match="outage") as captured:
            future.result()
    assert captured.value is error
    assert batch.stopped is True


def test_fetch_doc_for_run_success(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    doc = GeminiDocContent("example", "Example meeting", "notes", "transcript")
    monkeypatch.setattr("fieldkit.ingest.docs.fetch_gemini_doc", MagicMock(return_value=doc))
    result = run._fetch_doc_for_run(object(), "example", conn)
    assert result is doc
    assert conn.execute("SELECT status FROM sources WHERE source_id='example'").fetchone()[0] == "pending"


@pytest.mark.parametrize(
    "error", [AuthError("auth required"), ConfigError("configuration required"), LLMError("provider")]
)
def test_fetch_doc_for_run_propagates_domain_errors(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    monkeypatch.setattr("fieldkit.ingest.docs.fetch_gemini_doc", MagicMock(side_effect=error))
    with pytest.raises(type(error), match=str(error).replace("[", r"\[").replace("]", r"\]")) as captured:
        run._fetch_doc_for_run(object(), "example", conn)
    assert captured.value is error
    assert conn.execute("SELECT status FROM sources WHERE source_id='example'").fetchone()[0] == "pending"


@pytest.mark.parametrize(
    "error,status",
    [
        (DocNotFoundError("not found"), "failed"),
        (DocAccessDeniedError("denied"), "failed"),
        (RuntimeError("temporary document failure"), "pending"),
        (TimeoutError("timeout"), "failed"),
    ],
)
def test_fetch_doc_for_run_persists_recoverable_status(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    status: str,
) -> None:
    monkeypatch.setattr("fieldkit.ingest.docs.fetch_gemini_doc", MagicMock(side_effect=error))
    result = run._fetch_doc_for_run(object(), "example", conn)
    assert result is None
    assert conn.execute("SELECT status FROM sources WHERE source_id='example'").fetchone()[0] == status


@pytest.mark.parametrize("failed_stage", [None, "stage1", "stage2"])
def test_clean_and_extract_transcript_complete_result(
    monkeypatch: pytest.MonkeyPatch, failed_stage: str | None
) -> None:
    doc = GeminiDocContent("example", "Meeting", "notes", "raw transcript")
    cleaned = Stage1Result(text="cleaned transcript")
    meta = TranscriptMeta(confidence="high")
    monkeypatch.setattr(
        "fieldkit.ingest.pipeline.stage1_clean",
        MagicMock(
            return_value=cleaned,
            side_effect=RuntimeError("validation") if failed_stage == "stage1" else None,
        ),
    )
    monkeypatch.setattr(
        "fieldkit.ingest.pipeline.stage2_extract",
        MagicMock(
            return_value=meta,
            side_effect=ValueError("validation") if failed_stage == "stage2" else None,
        ),
    )
    result = run._clean_and_extract_transcript("example", doc)
    assert result == run._CleanResult(
        "raw transcript" if failed_stage == "stage1" else "cleaned transcript",
        TranscriptMeta(confidence="low") if failed_stage == "stage2" else meta,
        failed_stage is not None,
    )


@pytest.mark.parametrize("stage", ["stage1_clean", "stage2_extract"])
def test_clean_and_extract_transcript_propagates_domain_errors(monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    error = LLMError("fatal provider", category="auth")
    monkeypatch.setattr("fieldkit.ingest.pipeline.stage1_clean", MagicMock(return_value=Stage1Result(text="cleaned")))
    monkeypatch.setattr(f"fieldkit.ingest.pipeline.{stage}", MagicMock(side_effect=error))
    with pytest.raises(LLMError, match="fatal provider") as captured:
        run._clean_and_extract_transcript("example", GeminiDocContent("example", "Meeting", "", "raw"))
    assert captured.value is error


@pytest.mark.parametrize("status", ["completed", "degraded", "failed", "skipped", "pending"])
def test_completed_source_returns_worker_status(status: run._SourceStatus) -> None:
    future: Future[tuple[str, run._SourceStatus]] = Future()
    future.set_result(("example", status))
    result = run._completed_source(future, "example")
    assert result == ("example", status)


@pytest.mark.parametrize(
    "error", [LLMError("outage", retryable=True), LLMError("source", scope="source"), RuntimeError("ordinary")]
)
def test_completed_source_isolates_recoverable_errors(error: Exception) -> None:
    future: Future[tuple[str, run._SourceStatus]] = Future()
    future.set_exception(error)
    result = run._completed_source(future, "example")
    assert result == ("example", "failed")


@pytest.mark.parametrize("error", [LLMError("auth", category="auth"), ConfigError("config"), AuthError("auth")])
def test_completed_source_propagates_fatal_errors(error: Exception) -> None:
    future: Future[tuple[str, run._SourceStatus]] = Future()
    future.set_exception(error)
    with pytest.raises(type(error), match=r"auth|config") as captured:
        run._completed_source(future, "example")
    assert captured.value is error


def test_parallel_counts_and_ordered_outcomes() -> None:
    statuses: dict[str, run._SourceStatus] = {"a": "degraded", "b": "completed", "c": "failed", "d": "skipped"}
    result = run._parallel_counts(statuses)
    assert result == (2, 1, 1, 1)
    outcomes = BatchOutcomes()
    ordered = [source(name) for name in ["b", "a", "e", "d", "c"]]
    assert run._ordered_parallel_outcomes(ordered, statuses, outcomes) is None
    assert outcomes == BatchOutcomes(completed=["b", "a"], degraded=["a"], failed=["c"], skipped=["d"], pending=["e"])


@pytest.mark.parametrize("auth", [AuthError("auth"), LLMError("auth", category="auth")])
def test_parallel_state_preserves_auth_precedence(auth: AuthError | LLMError) -> None:
    state = run._ParallelState()
    state.fatal = ConfigError("config")
    future: Future[tuple[str, run._SourceStatus]] = Future()
    future.set_exception(auth)
    assert state.collect(future, source()) is None
    assert state.fatal is auth
    second: Future[tuple[str, run._SourceStatus]] = Future()
    second.set_exception(ConfigError("later config"))
    assert state.collect(second, source()) is None
    assert state.fatal is auth


def test_claimed_source_skips_without_processing_output(
    conn: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("fieldkit.ingest.sources.claim_pending_source", MagicMock(return_value=False))
    process = MagicMock()
    monkeypatch.setattr(run, "_process_one_source", process)
    context = run._WorkerContext(tmp_path, tmp_path / "pipeline.db", "1.0", threading.Lock(), BatchProviderFailures(3))
    result = run._claimed_source(source(), conn, object(), context)
    assert result == "skipped"
    process.assert_not_called()
    assert capsys.readouterr().out == ""
    assert conn.execute("SELECT status FROM sources WHERE source_id='example'").fetchone()[0] == "pending"


def test_parallel_state_cancelled_source_remains_unstarted() -> None:
    state = run._ParallelState()
    future: Future[tuple[str, run._SourceStatus]] = Future()
    assert future.cancel() is True
    assert state.collect(future, source()) is None
    assert state.statuses == {}
    assert state.fatal is None
