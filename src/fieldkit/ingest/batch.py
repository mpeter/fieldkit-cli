"""Bounded ingest scheduling with provider-stop and resumable result accounting."""

import math
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextvars import copy_context
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from fieldkit.errors import AuthError, FieldkitError, LLMError
from fieldkit.ingest.provider_failures import BatchProviderFailures, track_provider_failures
from fieldkit.ingest.sources import SourceRecord

# Eight workers leave headroom for retries with two LLM calls per transcript.
_MAX_WORKERS = 8
TARGET_MINUTES = 15
_MINUTES_PER_TRANSCRIPT = 3.0

SourceStatus = Literal["pending", "skipped", "failed", "completed", "degraded"]
WorkerResult = tuple[str, SourceStatus]


@dataclass(frozen=True)
class WorkerContext:
    data_root: Path
    db_path: Path | None
    pipeline_version: str
    file_lock: "threading.Lock"
    batch: BatchProviderFailures


@dataclass
class ParallelState:
    statuses: dict[str, SourceStatus] = field(default_factory=dict)
    fatal: FieldkitError | None = None
    interrupted: bool = False

    def collect(
        self,
        future: Future[WorkerResult],
        src: SourceRecord,
        report_error: Callable[[str, Exception], None],
    ) -> None:
        if future.cancelled():
            return
        try:
            source_id, status = completed_source(future, src.source_id, report_error)
            self.statuses[source_id] = status
        except FieldkitError as exc:
            self.fatal = _preferred_fatal(self.fatal, exc)


def _preferred_fatal(current: FieldkitError | None, candidate: FieldkitError) -> FieldkitError:
    if current is None or isinstance(candidate, AuthError):
        return candidate
    if isinstance(candidate, LLMError) and candidate.category == "auth":
        return candidate
    return current


def dynamic_worker_count(queue_size: int) -> int:
    """Target fifteen minutes per queue, capped at eight workers."""
    needed = math.ceil(queue_size * _MINUTES_PER_TRANSCRIPT / TARGET_MINUTES)
    return max(1, min(_MAX_WORKERS, needed))


def completed_source(
    future: Future[WorkerResult],
    source_id: str,
    report_error: Callable[[str, Exception], None],
) -> WorkerResult:
    try:
        return future.result()
    except LLMError as exc:
        if exc.scope != "source" and not exc.retryable:
            raise
        report_error(source_id, exc)
    except FieldkitError:
        raise
    except Exception as exc:  # noqa: BLE001 — retain per-source failure isolation
        report_error(source_id, exc)
    return source_id, "failed"


def _cancel_unstarted(futures: dict[Future[WorkerResult], SourceRecord]) -> None:
    for future in futures:
        future.cancel()


def drain_sources(
    pending: list[SourceRecord],
    workers: int,
    context: WorkerContext,
    *,
    worker: Callable[[SourceRecord, WorkerContext], WorkerResult],
    report_error: Callable[[str, Exception], None],
    report_interrupt: Callable[[], None],
) -> ParallelState:
    """Stop admission on outage, fatal error or interrupt, then drain running work.

    Missing statuses represent unstarted sources and remain pending on resume.
    Worker contexts inherit provider tracking and any adapter output context.
    """
    state = ParallelState()
    remaining = iter(pending)
    with track_provider_failures(context.batch), ThreadPoolExecutor(max_workers=workers) as pool:
        futures: dict[Future[WorkerResult], SourceRecord] = {}
        _fill_workers(pool, futures, remaining, workers, context, worker)
        while futures:
            try:
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
            except KeyboardInterrupt:
                state.interrupted = True
                report_interrupt()
                _cancel_unstarted(futures)
                continue
            for future in done:
                state.collect(future, futures.pop(future), report_error)
            if context.batch.stopped or state.fatal is not None or state.interrupted:
                _cancel_unstarted(futures)
            else:
                _fill_workers(pool, futures, remaining, workers, context, worker)
    return state


def _admitted_worker(
    src: SourceRecord,
    context: WorkerContext,
    worker: Callable[[SourceRecord, WorkerContext], WorkerResult],
) -> WorkerResult:
    if context.batch.stopped:
        return src.source_id, "pending"
    return worker(src, context)


def _fill_workers(
    pool: ThreadPoolExecutor,
    futures: dict[Future[WorkerResult], SourceRecord],
    remaining: Iterator[SourceRecord],
    workers: int,
    context: WorkerContext,
    worker: Callable[[SourceRecord, WorkerContext], WorkerResult],
) -> None:
    for _ in range(workers - len(futures)):
        if context.batch.stopped:
            break
        src = next(remaining, None)
        if src is None:
            break
        futures[pool.submit(copy_context().run, _admitted_worker, src, context, worker)] = src
