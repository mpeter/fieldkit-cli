"""fieldkit.config.retry — the single home for retry policy.

Two factories, split by call-site safety. Choosing between them forces an explicit
idempotency judgement, which is the point:

    transient_retry(predicate, logger)   idempotent calls only — retries connect
                                         failures, timeouts, and 429/5xx.
    connect_retry(logger)                non-idempotent calls — retries ONLY
                                         connect-phase failures, where the request
                                         provably never reached the server.

Retrying is at-least-once. A timeout or lost response *after* the request has been
transmitted does not prove the server did not act on it, so anything that creates or
mutates server-side state must use ``connect_retry``.

Per-attempt logging is baked in and cannot be disabled. Eight of the nine retry sites
that existed before this module omitted it; a convention that depends on authors
remembering an optional argument has already been shown to fail here.

Two constraints on this module (design D3):

1. It MUST NOT import a third-party API client. ``httpx`` is imported for the
   connect-phase exception types only; ``googleapiclient``, ``litellm``, and ``openai``
   stay out. That is what keeps the domain predicates in their own domain modules.
2. It MUST NOT be re-exported from ``fieldkit.config.__init__``. ``fieldkit.config`` is
   a pure foundation imported on every CLI invocation and today pulls neither ``httpx``
   nor ``tenacity`` (232ms and 51ms cumulative). Import this module directly:
   ``from fieldkit.config.retry import connect_retry``.
"""

import functools
import logging
from collections.abc import Callable
from typing import ParamSpec, TypeVar

import httpx
from tenacity import RetryError, before_sleep_log, retry, retry_if_exception, stop_after_attempt, wait_exponential

# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

RETRY_MAX_ATTEMPTS = 3
"""Total attempts, not retries-after-the-first. 3 means one call plus two retries."""

RETRY_WAIT_MIN = 2
"""Exponential backoff floor, seconds. Matches sf/client.py, the only site that was
already fully conformant before this module existed."""

RETRY_WAIT_MAX = 10
"""Exponential backoff ceiling, seconds. Override explicitly where a quota-limited API
needs longer, with a comment stating why."""

RETRY_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})
"""The single definition of a transient HTTP status. Domain predicates source this
rather than declaring their own set."""

_CONNECT_EXCEPTIONS = (httpx.ConnectError, httpx.ConnectTimeout)
"""Failures that occur before the request reaches the server, so replaying is safe
even for a non-idempotent call."""


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _build(
    predicate: Callable[[BaseException], bool],
    logger: logging.Logger,
    attempts: int,
    wait_min: float,
    wait_max: float,
) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Compose the shared retry policy around a callable.

    The decorated function keeps its own signature and return type — these factories
    are transparent to mypy, so a wrapped ``fetch_x() -> Doc`` still reads as returning
    ``Doc`` at every call site.

    ``reraise`` is deliberately left off the tenacity decorator: we want the
    ``RetryError`` so exhaustion can be logged and annotated, then we re-raise the
    original exception ourselves. From the caller's perspective this is identical to
    ``reraise=True`` — the exception type that escapes is the underlying one, so
    existing ``except`` clauses and the fieldkit.errors exit-code mapping still hold.

    The returned function carries a ``with_policy(**overrides)`` attribute that rebuilds
    it with overridden policy values, preserving the exhaustion logging and note
    attachment. Tests use it to neutralise backoff without patching ``time.sleep``,
    which would couple them to tenacity's internal sleep binding (L01). It is attached
    dynamically and so is not visible to mypy; it takes this module's policy keywords
    (``attempts``, ``wait_min``, ``wait_max``), NOT tenacity's ``retry_with`` surface.
    """

    # Bound here so with_policy's keyword params may reuse the public names.
    _logger, _attempts, _wait_min, _wait_max = logger, attempts, wait_min, wait_max

    def decorate(fn: Callable[_P, _R]) -> Callable[_P, _R]:
        retrying = retry(
            stop=stop_after_attempt(attempts),
            wait=wait_exponential(multiplier=1, min=wait_min, max=wait_max),
            retry=retry_if_exception(predicate),
            before_sleep=before_sleep_log(logger, logging.WARNING),
        )(fn)

        @functools.wraps(fn)
        def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            try:
                return retrying(*args, **kwargs)
            except RetryError as retry_error:
                exc = retry_error.last_attempt.exception()
                if exc is None:  # pragma: no cover — tenacity only wraps failed attempts
                    raise
                # The operator-visible report. Emitted here, by the factory, so it does
                # not depend on how a calling module formats its own error message.
                logger.error("%s failed after %d attempts: %s", fn.__qualname__, attempts, exc)
                exc.add_note(f"{fn.__qualname__} failed after {attempts} attempts")
                # Re-raise from the exception's OWN cause, not `from None`. Both hide
                # tenacity's RetryError from the traceback, but `from None` also nulls
                # __cause__ — discarding the transport-level root cause (the
                # ConnectionRefusedError behind an httpx.ConnectError) that a responder
                # needs. This keeps the chain intact and still satisfies B904.
                raise exc from exc.__cause__

        def with_policy(
            *,
            attempts: int | None = None,
            wait_min: float | None = None,
            wait_max: float | None = None,
            logger: logging.Logger | None = None,
        ) -> Callable[_P, _R]:
            return _build(
                predicate,
                _logger if logger is None else logger,
                _attempts if attempts is None else attempts,
                _wait_min if wait_min is None else wait_min,
                _wait_max if wait_max is None else wait_max,
            )(fn)

        wrapper.with_policy = with_policy  # type: ignore[attr-defined]
        return wrapper

    return decorate


def _is_connect_failure(exc: BaseException) -> bool:
    """True for failures that occurred before the request reached the server."""
    return isinstance(exc, _CONNECT_EXCEPTIONS)


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def transient_retry(
    predicate: Callable[[BaseException], bool],
    logger: logging.Logger,
    *,
    attempts: int = RETRY_MAX_ATTEMPTS,
    wait_min: float = RETRY_WAIT_MIN,
    wait_max: float = RETRY_WAIT_MAX,
) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Retry a transient failure. **Idempotent calls only.**

    Do not use this on anything that creates or mutates server-side state: the
    predicate necessarily matches failures that occur after the request was
    transmitted, so a retry can repeat an effect the server already applied. Use
    ``connect_retry`` for those.

    Args:
        predicate: Classifies an exception as transient. Lives in the caller's domain
            module because it is specific to that client library; it should source
            ``RETRY_TRANSIENT_STATUSES`` rather than declaring its own status set.
        logger: The calling module's logger, so retry warnings are attributed to it.
        attempts: Total attempts including the first.
        wait_min: Backoff floor in seconds.
        wait_max: Backoff ceiling in seconds. Override only with a stated reason.
    """
    return _build(predicate, logger, attempts, wait_min, wait_max)


def connect_retry(
    logger: logging.Logger,
    *,
    attempts: int = RETRY_MAX_ATTEMPTS,
    wait_min: float = RETRY_WAIT_MIN,
    wait_max: float = RETRY_WAIT_MAX,
) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Retry only a failure to connect. **Required for non-idempotent calls.**

    Retries ``httpx.ConnectError`` and ``httpx.ConnectTimeout`` only. A read timeout,
    a mid-response protocol error, or any 5xx means the request reached the server and
    may already have taken effect, so none of them is retried here.

    This narrows the duplicate-write window; it does not eliminate it. A connection can
    in principle be established and lost before any response. Closing that fully needs
    server-side idempotency keys.

    Args:
        logger: The calling module's logger, so retry warnings are attributed to it.
        attempts: Total attempts including the first.
        wait_min: Backoff floor in seconds.
        wait_max: Backoff ceiling in seconds.
    """
    return _build(_is_connect_failure, logger, attempts, wait_min, wait_max)
