"""The ingest domain schedules work independently of CLI adapters."""

import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fieldkit.errors import AuthError, LLMError
from fieldkit.ingest.batch import WorkerContext, WorkerResult, drain_sources
from fieldkit.ingest.provider_failures import BatchProviderFailures, tracked_synthesis
from fieldkit.ingest.sources import SourceRecord

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "responses,expected_statuses,stopped,fatal",
    [
        (
            [LLMError("outage", retryable=True), LLMError("outage", retryable=True)],
            {"source-0": "failed", "source-1": "failed"},
            True,
            False,
        ),
        (
            [LLMError("outage", retryable=True), "ok", LLMError("outage", retryable=True), "ok"],
            {"source-0": "failed", "source-1": "completed", "source-2": "failed", "source-3": "completed"},
            False,
            False,
        ),
        (
            [LLMError("invalid source", scope="source"), "ok", "ok", "ok"],
            {"source-0": "failed", "source-1": "completed", "source-2": "completed", "source-3": "completed"},
            False,
            False,
        ),
        ([AuthError("auth required")], {}, False, True),
    ],
)
def test_domain_scheduler_stop_and_resume_contract(
    tmp_path: Path,
    responses: list[str | Exception],
    expected_statuses: dict[str, str],
    stopped: bool,
    fatal: bool,
) -> None:
    sources = [
        SourceRecord(f"source-{index}", "transcript-ingest", "Meeting", "Meeting", None, "", "", "2026-01-01")
        for index in range(4)
    ]
    provider = MagicMock(side_effect=responses)
    context = WorkerContext(tmp_path, None, "1.0", threading.Lock(), BatchProviderFailures(2))
    reports = MagicMock()
    interrupt = MagicMock()

    def worker(src: SourceRecord, _context: WorkerContext) -> WorkerResult:
        tracked_synthesis("fictional transcript", provider)
        return src.source_id, "completed"

    result = drain_sources(sources, 1, context, worker=worker, report_error=reports, report_interrupt=interrupt)
    assert result.statuses == expected_statuses
    assert context.batch.stopped is stopped
    assert isinstance(result.fatal, AuthError) is fatal
    assert provider.call_count == len(responses)
    assert reports.call_count == sum(status == "failed" for status in expected_statuses.values())
    assert result.interrupted is False
    interrupt.assert_not_called()


def test_domain_scheduler_does_not_admit_work_after_stop(tmp_path: Path) -> None:
    batch = BatchProviderFailures(1)
    batch.record(LLMError("outage", retryable=True))
    context = WorkerContext(tmp_path, None, "1.0", threading.Lock(), batch)
    worker = MagicMock()
    sources = [SourceRecord("example", "transcript-ingest", "Meeting", "Meeting", None, "", "", "2026-01-01")]
    result = drain_sources(sources, 1, context, worker=worker, report_error=MagicMock(), report_interrupt=MagicMock())
    assert result.statuses == {}
    assert result.fatal is None
    worker.assert_not_called()
