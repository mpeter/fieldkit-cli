"""Provider outages stop scheduling without losing durable ingest progress."""

import json
import logging
import sqlite3
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from fieldkit.cli_exit import cli_main
from fieldkit.commands.ingest import run
from fieldkit.commands.ingest._output import BatchOutcomes
from fieldkit.commands.ingest.registry import PIPELINES
from fieldkit.config import ConfigError, get_ingest_provider_failure_threshold
from fieldkit.errors import LLMError
from fieldkit.ingest.db import init_db
from fieldkit.ingest.docs import GeminiDocContent
from fieldkit.ingest.provider_failures import BatchProviderFailures
from fieldkit.ingest.router import Confidence, RouteResult
from fieldkit.ingest.sources import get_pending_sources

pytestmark = pytest.mark.unit


@pytest.fixture
def batch_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "pipeline.db"
    conn = init_db(path, pipelines=PIPELINES)
    for index in range(6):
        conn.execute(
            "INSERT INTO sources (source_id, pipeline_id, file_path, meeting_title) VALUES (?, ?, ?, ?)",
            (f"source-{index}", "transcript-ingest", f"doc-{index}", f"Meeting {index}"),
        )
    conn.commit()
    monkeypatch.setenv("FIELDKIT_INGEST_PROVIDER_FAILURE_THRESHOLD", "2")
    monkeypatch.setattr("fieldkit.config.get_fieldkit_home", lambda: tmp_path)
    monkeypatch.setattr("fieldkit.ingest.db.get_db_path", lambda: path)
    monkeypatch.setattr("fieldkit.ingest.docs.get_docs_service", MagicMock)
    monkeypatch.setattr("fieldkit.gmail.discover.scan_gemini_candidates", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(run, "_dynamic_worker_count", lambda _count: 1)
    monkeypatch.setattr(
        run,
        "_fetch_doc_for_run",
        lambda _service, source_id, _conn: GeminiDocContent(
            source_id, source_id, "", (source_id + " discussion ") * 20
        ),
    )
    monkeypatch.setattr(run, "_route_source", lambda *_args: RouteResult(["unknown"], Confidence.NONE, False))
    yield conn
    conn.close()


def outage() -> LLMError:
    return LLMError("fictional provider response must not be rendered", category="rate-limit")


@pytest.mark.parametrize("as_json", [False, True])
def test_threshold_stops_and_reports_resume(
    batch_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, as_json: bool
) -> None:
    calls = MagicMock(side_effect=outage())
    monkeypatch.setattr("fieldkit.ingest.pipeline.synthesize", calls)
    args = ["--pipeline", "transcript-ingest"] + (["--json"] if as_json else [])
    result = CliRunner().invoke(run.cli, args)
    assert result.exit_code == 1
    assert calls.call_count == 2
    assert len(get_pending_sources(batch_db, "transcript-ingest")) == 6
    assert batch_db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
    assert "fictional provider response" not in result.output
    assert "threshold 2" in result.output
    assert "fieldkit ingest run --pipeline transcript-ingest" in result.output
    if as_json:
        payload = json.loads(result.stdout)
        assert payload["failed"] == ["source-0", "source-1"]
        assert payload["pending"] == [f"source-{i}" for i in range(2, 6)]
        assert payload["completed"] == payload["degraded"] == payload["skipped"] == []
        assert payload["provider_failure_threshold"] == 2
        assert payload["stop_reason"] == "consecutive_retryable_provider_failures"
        assert payload["resume_command"] == "fieldkit ingest run --pipeline transcript-ingest"


@pytest.mark.parametrize("interactive", [False, True])
def test_provider_failure_below_threshold_is_partial(
    batch_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, interactive: bool
) -> None:
    monkeypatch.setattr("fieldkit.ingest.pipeline.synthesize", MagicMock(side_effect=outage()))
    monkeypatch.setattr(run, "_prompt_process_choice", lambda *_args: "y")
    result = run._run_processing_loop(
        get_pending_sources(batch_db, "transcript-ingest", limit=1),
        conn=batch_db,
        pipeline_version="0.1.0",
        interactive=interactive,
    )
    assert result == 1
    assert len(get_pending_sources(batch_db, "transcript-ingest")) == 6


def test_interactive_threshold_stops_prompting(batch_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    prompt = MagicMock(return_value="y")
    monkeypatch.setattr(run, "_prompt_process_choice", prompt)
    monkeypatch.setattr("fieldkit.ingest.pipeline.synthesize", MagicMock(side_effect=outage()))
    result = run._run_processing_loop(
        get_pending_sources(batch_db, "transcript-ingest"),
        conn=batch_db,
        pipeline_version="0.1.0",
        interactive=True,
    )
    assert result == 1
    assert prompt.call_count == 2
    assert len(get_pending_sources(batch_db, "transcript-ingest")) == 6


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize(
    "kind,code,attempts",
    [
        ("RateLimitError", 1, 6),
        ("Timeout", 1, 6),
        ("APIConnectionError", 1, 6),
        ("AuthenticationError", 2, 1),
        ("BadRequestError", 3, 1),
    ],
)
def test_real_provider_failures_do_not_expose_response_bodies(
    batch_db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    as_json: bool,
    kind: str,
    code: int,
    attempts: int,
) -> None:
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.delenv("NO_LLM", raising=False)
    monkeypatch.delenv("FIELDKIT_NO_LLM", raising=False)
    monkeypatch.setenv("FIELDKIT_LLM_MODEL", "vertex_ai/test-model")
    import litellm

    from fieldkit.__main__ import main
    from fieldkit.llm import log as audit_log

    marker = "fictional-sensitive-provider-response-marker"
    error = getattr(litellm.exceptions, kind)(message=marker, llm_provider="vertex_ai", model="vertex_ai/test-model")
    audit_path = tmp_path / "llm-calls.db"
    audit_log.init_db(audit_path)
    monkeypatch.setattr(audit_log, "get_db_path", lambda: audit_path)
    monkeypatch.setattr("fieldkit.__main__.load_dotenv_safe", lambda: None)
    monkeypatch.setattr("time.sleep", MagicMock())
    caplog.set_level(logging.WARNING)

    def fail_provider(**kwargs: object) -> str:
        audit_log._failure_callback({**kwargs, "exception": error}, None, 0.0, 1.0)
        raise error

    calls = MagicMock(side_effect=fail_provider)
    monkeypatch.setattr(litellm, "completion", calls)
    args = ["ingest", "run", "--pipeline", "transcript-ingest"] + (["--json"] if as_json else [])
    result = main(args)
    assert result == code
    captured = capsys.readouterr()
    assert marker not in captured.out
    assert marker not in captured.err
    assert marker not in caplog.text
    assert calls.call_count == attempts
    with sqlite3.connect(audit_path) as audit_conn:
        records = audit_conn.execute("SELECT error FROM llm_calls").fetchall()
    assert len(records) == attempts
    assert all(marker not in row[0] for row in records)
    if code == 1:
        retry_records = [record for record in caplog.records if record.name == "fieldkit.llm.core"]
        assert sum(record.levelno == logging.WARNING for record in retry_records) == 4
        assert sum(record.levelno == logging.ERROR for record in retry_records) == 2
        assert len(get_pending_sources(batch_db, "transcript-ingest")) == 6
        assert "threshold 2" in captured.err
        assert "fieldkit ingest run --pipeline transcript-ingest" in captured.err
        if as_json:
            payload = json.loads(captured.out)
            assert payload["failed"] == ["source-0", "source-1"]
            assert payload["pending"] == [f"source-{i}" for i in range(2, 6)]
    else:
        assert len(get_pending_sources(batch_db, "transcript-ingest")) == 6


def test_successful_call_resets_before_source_validation(
    batch_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = MagicMock(side_effect=[outage(), "clean transcript", "invalid JSON", outage(), outage()])
    monkeypatch.setattr("fieldkit.ingest.pipeline.synthesize", calls)
    outcomes = BatchOutcomes()
    result = run._run_parallel_loop(
        get_pending_sources(batch_db, "transcript-ingest"),
        data_root=tmp_path,
        db_path=tmp_path / "pipeline.db",
        pipeline_version="0.1.0",
        outcomes=outcomes,
    )
    assert result == (1, 1, 0, 3)
    assert calls.call_count == 5
    assert outcomes.failed == ["source-0", "source-2", "source-3"]
    assert outcomes.completed == outcomes.degraded == ["source-1"]
    assert outcomes.pending == ["source-4", "source-5"]
    assert [src.source_id for src in get_pending_sources(batch_db, "transcript-ingest")] == [
        "source-0",
        "source-2",
        "source-3",
        "source-4",
        "source-5",
    ]
    artifact = batch_db.execute("SELECT content_path FROM artifacts").fetchone()[0]
    assert Path(artifact).read_text(encoding="utf-8")


@pytest.mark.parametrize("error", [ValueError("invalid source"), RuntimeError("output validation")])
def test_source_specific_failure_does_not_stop_batch(
    batch_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: Exception
) -> None:
    monkeypatch.setattr("fieldkit.ingest.pipeline.synthesize", MagicMock(side_effect=error))
    outcomes = BatchOutcomes()
    result = run._run_parallel_loop(
        get_pending_sources(batch_db, "transcript-ingest"),
        data_root=tmp_path,
        db_path=tmp_path / "pipeline.db",
        pipeline_version="0.1.0",
        outcomes=outcomes,
    )
    assert result == (6, 6, 0, 0)
    assert outcomes.provider_failure_threshold is None
    assert outcomes.pending == outcomes.failed == []
    assert get_pending_sources(batch_db, "transcript-ingest") == []


def test_oversized_prompt_fails_only_its_source_and_keeps_completed_notes(
    batch_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fieldkit.llm.core import _MAX_PROMPT_CHARS, synthesize

    monkeypatch.delenv("NO_LLM", raising=False)
    monkeypatch.delenv("FIELDKIT_NO_LLM", raising=False)

    def provider(prompt: str) -> str:
        if len(prompt) > _MAX_PROMPT_CHARS:
            return synthesize(prompt)
        if "source-1" in prompt:
            return "source-1 " + "x" * _MAX_PROMPT_CHARS
        return '{"confidence":"high"}'

    monkeypatch.setattr("fieldkit.ingest.pipeline.synthesize", provider)
    outcomes = BatchOutcomes()
    batch = BatchProviderFailures(1)
    result = run._run_parallel_loop(
        get_pending_sources(batch_db, "transcript-ingest"),
        data_root=tmp_path,
        db_path=tmp_path / "pipeline.db",
        pipeline_version="0.1.0",
        outcomes=outcomes,
        batch=batch,
    )
    assert result == (5, 0, 0, 1)
    assert outcomes.completed == ["source-0", "source-2", "source-3", "source-4", "source-5"]
    assert outcomes.failed == ["source-1"]
    assert outcomes.pending == outcomes.degraded == outcomes.skipped == []
    assert outcomes.provider_failure_threshold is None
    assert batch.had_retryable_failure is False
    assert batch.stopped is False
    assert batch_db.execute("SELECT status FROM sources WHERE source_id = 'source-1'").fetchone()[0] == "failed"
    assert get_pending_sources(batch_db, "transcript-ingest") == []
    artifacts = batch_db.execute("SELECT source_id, content_path FROM artifacts ORDER BY source_id").fetchall()
    assert [row["source_id"] for row in artifacts] == outcomes.completed
    assert all(Path(row["content_path"]).read_text(encoding="utf-8") for row in artifacts)


def test_interactive_source_input_rejection_continues_to_healthy_sources(
    batch_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = MagicMock(
        side_effect=[
            '{"confidence":"high"}',
            '{"confidence":"high"}',
            LLMError("fictional invalid source body", scope="source"),
            *['{"confidence":"high"}'] * 8,
        ]
    )
    monkeypatch.setattr("fieldkit.ingest.pipeline.synthesize", calls)
    monkeypatch.setattr(run, "_prompt_process_choice", lambda *_args: "y")
    result = run._run_processing_loop(
        get_pending_sources(batch_db, "transcript-ingest"),
        conn=batch_db,
        pipeline_version="0.1.0",
        interactive=True,
    )
    assert result == 0
    assert calls.call_count == 11
    assert batch_db.execute("SELECT COUNT(*) FROM sources WHERE status = 'processed'").fetchone()[0] == 5
    assert batch_db.execute("SELECT status FROM sources WHERE source_id = 'source-1'").fetchone()[0] == "failed"
    assert batch_db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 5


def test_queued_source_never_becomes_failed(
    batch_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(run, "_dynamic_worker_count", lambda _count: 2)
    monkeypatch.setattr(run, "ThreadPoolExecutor", lambda **_kwargs: ThreadPoolExecutor(max_workers=1))
    calls = MagicMock(side_effect=outage())
    monkeypatch.setattr("fieldkit.ingest.pipeline.synthesize", calls)
    outcomes = BatchOutcomes()
    result = run._run_parallel_loop(
        get_pending_sources(batch_db, "transcript-ingest"),
        data_root=tmp_path,
        db_path=tmp_path / "pipeline.db",
        pipeline_version="0.1.0",
        outcomes=outcomes,
        batch=BatchProviderFailures(1),
    )
    assert result == (0, 0, 0, 1)
    assert calls.call_count == 1
    assert outcomes.failed == ["source-0"]
    assert outcomes.pending == [f"source-{i}" for i in range(1, 6)]
    assert len(get_pending_sources(batch_db, "transcript-ingest")) == 6


def test_running_source_finishes_and_resume_keeps_artifact(
    batch_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(run, "_dynamic_worker_count", lambda _count: 2)
    running = threading.Event()
    stopped = threading.Event()
    batch = BatchProviderFailures(1)
    original_record = batch.record

    def record(error: LLMError | None) -> None:
        original_record(error)
        if batch.stopped:
            stopped.set()

    monkeypatch.setattr(batch, "record", record)

    def provider(prompt: str) -> str:
        if "clean transcript" in prompt:
            return "invalid JSON"
        if "source-0" in prompt:
            running.set()
            assert stopped.wait(timeout=5)
            return "clean transcript"
        assert running.wait(timeout=5)
        raise outage()

    monkeypatch.setattr("fieldkit.ingest.pipeline.synthesize", provider)
    outcomes = BatchOutcomes()
    result = run._run_parallel_loop(
        get_pending_sources(batch_db, "transcript-ingest"),
        data_root=tmp_path,
        db_path=tmp_path / "pipeline.db",
        pipeline_version="0.1.0",
        outcomes=outcomes,
        batch=batch,
    )
    assert result == (1, 1, 0, 1)
    assert outcomes.completed == ["source-0"]
    assert outcomes.failed == ["source-1"]
    assert outcomes.pending == [f"source-{i}" for i in range(2, 6)]
    artifact = batch_db.execute("SELECT content_path FROM artifacts").fetchone()[0]
    content = Path(artifact).read_text(encoding="utf-8")
    assert content
    monkeypatch.setattr("fieldkit.ingest.pipeline.synthesize", lambda _prompt: '{"confidence":"high"}')
    resumed = run._run_parallel_loop(
        get_pending_sources(batch_db, "transcript-ingest"),
        data_root=tmp_path,
        db_path=tmp_path / "pipeline.db",
        pipeline_version="0.1.0",
    )
    assert resumed == (5, 0, 0, 0)
    assert Path(artifact).read_text(encoding="utf-8") == content
    assert batch_db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 6


@pytest.mark.parametrize(
    "error,code",
    [
        (LLMError("authentication required", category="auth"), 2),
        (ConfigError("invalid provider configuration"), 3),
        (LLMError("invalid model"), 3),
    ],
)
def test_fatal_provider_error_keeps_normal_exit_path(
    batch_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, error: Exception, code: int
) -> None:
    monkeypatch.setattr("fieldkit.ingest.pipeline.synthesize", MagicMock(side_effect=error))
    with pytest.raises(SystemExit) as exc_info, cli_main():
        run._run_processing_loop(
            get_pending_sources(batch_db, "transcript-ingest"),
            conn=batch_db,
            pipeline_version="0.1.0",
            interactive=False,
        )
    assert exc_info.value.code == code
    assert len(get_pending_sources(batch_db, "transcript-ingest")) == 6


@pytest.mark.parametrize("value", ["0", "-1", "false", "1.5", ""])
def test_invalid_threshold_rejected(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("FIELDKIT_INGEST_PROVIDER_FAILURE_THRESHOLD", value)
    with pytest.raises(ConfigError, match="positive integer"):
        get_ingest_provider_failure_threshold()


def test_threshold_config_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIELDKIT_INGEST_PROVIDER_FAILURE_THRESHOLD", raising=False)
    monkeypatch.setattr("fieldkit.config._loader._load_raw_config", lambda: {})
    assert get_ingest_provider_failure_threshold() == 3
    monkeypatch.setattr("fieldkit.config._loader._load_raw_config", lambda: {"ingest_provider_failure_threshold": 5})
    assert get_ingest_provider_failure_threshold() == 5
    monkeypatch.setenv("FIELDKIT_INGEST_PROVIDER_FAILURE_THRESHOLD", "2")
    assert get_ingest_provider_failure_threshold() == 2
