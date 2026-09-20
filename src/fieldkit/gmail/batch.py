"""Typed outcomes and callback accounting for Gmail batch fetches."""

from collections.abc import Callable
from dataclasses import dataclass, field
from logging import Logger
from typing import Any

from googleapiclient.errors import HttpError

from fieldkit.errors import GmailAuthError
from fieldkit.gmail.retry import _normalize_gmail_http_error

MessageBuilder = Callable[[dict[str, Any], str], dict[str, Any]]


@dataclass(frozen=True)
class BatchFetchResult:
    """Accounted outcomes from one Gmail batch request."""

    messages: list[dict[str, Any]]
    not_found: int = 0
    unresolved: int = 0

    @property
    def failed(self) -> int:
        return self.not_found + self.unresolved


def warn_if_batch_incomplete(result: BatchFetchResult, logger: Logger) -> None:
    if result.failed:
        logger.warning("Batch fetch incomplete: %d not found, %d unresolved", result.not_found, result.unresolved)


@dataclass(frozen=True)
class SyncSummary:
    """User-visible Gmail sync outcome counts."""

    added: int = 0
    not_found: int = 0
    unresolved: int = 0

    @property
    def failed(self) -> int:
        return self.not_found + self.unresolved

    def plus(self, other: "SyncSummary") -> "SyncSummary":
        return SyncSummary(
            added=self.added + other.added,
            not_found=self.not_found + other.not_found,
            unresolved=self.unresolved + other.unresolved,
        )


@dataclass
class BatchAccumulator:
    """Mutable callback state for one Gmail batch request."""

    message_builder: MessageBuilder
    messages: list[dict[str, Any]] = field(default_factory=list)
    not_found: int = 0
    unresolved: int = 0
    auth_error: GmailAuthError | None = None

    def record(self, message_id: str, request_id: str, response: Any, exception: Any) -> None:
        del request_id
        if exception is None:
            if response is None:
                self.unresolved += 1
            else:
                self.messages.append(self.message_builder(response, message_id))
            return
        if not isinstance(exception, HttpError):
            self.unresolved += 1
            return
        if normalized := _normalize_gmail_http_error(exception):
            self.auth_error = normalized
        elif exception.resp.status == 404:
            self.not_found += 1
        else:
            self.unresolved += 1

    def result(self) -> BatchFetchResult:
        if self.auth_error is not None:
            raise self.auth_error
        return BatchFetchResult(
            messages=self.messages,
            not_found=self.not_found,
            unresolved=self.unresolved,
        )
