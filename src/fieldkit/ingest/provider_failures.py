"""Batch-local provider failure tracking, independent of source validation."""

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from fieldkit.errors import LLMError


@dataclass
class BatchProviderFailures:
    """Latch a batch stop after consecutive exhausted retryable provider calls."""

    threshold: int
    _consecutive: int = 0
    _failures: int = 0
    _stopped: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def stopped(self) -> bool:
        with self._lock:
            return self._stopped

    @property
    def had_retryable_failure(self) -> bool:
        with self._lock:
            return self._failures > 0

    def record(self, error: LLMError | None) -> None:
        with self._lock:
            if self._stopped:
                return
            if error is None:
                self._consecutive = 0
            elif error.retryable:
                self._failures += 1
                self._consecutive += 1
                self._stopped = self._consecutive >= self.threshold


_BATCH: ContextVar[BatchProviderFailures | None] = ContextVar("ingest_provider_failures", default=None)


@contextmanager
def track_provider_failures(batch: BatchProviderFailures) -> Iterator[None]:
    """Share the batch counter through copied worker contexts."""
    token = _BATCH.set(batch)
    try:
        yield
    finally:
        _BATCH.reset(token)


def tracked_synthesis(prompt: str, synthesize: Callable[[str], str]) -> str:
    """Record the provider outcome before callers inspect the returned text."""
    with provider_call():
        return synthesize(prompt)


@contextmanager
def provider_call() -> Iterator[None]:
    """Observe provider success before parsing or validating source output."""
    batch = _BATCH.get()
    try:
        yield
    except LLMError as exc:
        if batch is not None:
            batch.record(exc)
        raise
    else:
        if batch is not None:
            batch.record(None)
