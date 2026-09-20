"""Unit tests for fieldkit.config.retry — the shared retry policy factories.

Backoff is neutralised by passing ``wait_min=0, wait_max=0`` to the factory at
decoration, never by patching ``time.sleep`` — patching the definition site couples
the test to tenacity's internal sleep binding (L01). No test in this file may patch
``time.sleep``. ``.with_policy()`` covers the case where an already-decorated
function needs different policy; ``test_with_policy_overrides_attempts`` exercises it.
"""

import logging
import re
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from fieldkit.config.retry import (
    RETRY_MAX_ATTEMPTS,
    RETRY_TRANSIENT_STATUSES,
    connect_retry,
    transient_retry,
)

pytestmark = pytest.mark.unit

_SUBPROCESS_TIMEOUT = 60
"""Ceiling for the import-cost guard subprocess."""

_LOG = logging.getLogger("test_retry_policy")


def _no_backoff_connect() -> object:
    """A connect_retry decorator with backoff removed."""
    return connect_retry(_LOG, wait_min=0, wait_max=0)


def _is_transient_status(exc: BaseException) -> bool:
    """Stand-in domain predicate: transient iff the status is in the shared set."""
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in RETRY_TRANSIENT_STATUSES


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.com/threads")
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


# ---------------------------------------------------------------------------
# connect_retry — the non-idempotent path
# ---------------------------------------------------------------------------


def test_connect_retry_retries_connect_error_then_succeeds() -> None:
    """A connection refused before transmission is safe to replay."""
    calls: list[int] = []

    @connect_retry(_LOG, wait_min=0, wait_max=0)
    def _create() -> str:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("connection refused")
        return "thread-abc"

    result = _create()

    assert result == "thread-abc"
    assert len(calls) == 2, "expected one failure then one success"


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadTimeout("response lost"),
        httpx.WriteTimeout("write timed out"),
        httpx.RemoteProtocolError("server disconnected mid-response"),
    ],
    ids=["read-timeout", "write-timeout", "protocol-error"],
)
def test_connect_retry_does_not_retry_post_transmission_failure(exc: Exception) -> None:
    """The duplicate-write guard.

    Each of these can occur *after* the request reached the server, so the server may
    already have applied the effect. Retrying would duplicate it.
    """
    calls: list[int] = []

    @connect_retry(_LOG, wait_min=0, wait_max=0)
    def _create() -> str:
        calls.append(1)
        raise exc

    with pytest.raises(type(exc)):
        _create()

    assert len(calls) == 1, f"{type(exc).__name__} must not be retried — it may have been applied server-side"


def test_connect_retry_does_not_retry_server_error() -> None:
    """A 5xx proves the request arrived, so it is not retried on a write path."""
    calls: list[int] = []

    @connect_retry(_LOG, wait_min=0, wait_max=0)
    def _create() -> str:
        calls.append(1)
        raise _status_error(503)

    with pytest.raises(httpx.HTTPStatusError, match="boom"):
        _create()

    assert len(calls) == 1


# ---------------------------------------------------------------------------
# transient_retry — the idempotent path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", sorted(RETRY_TRANSIENT_STATUSES))
def test_transient_retry_retries_every_transient_status(code: int) -> None:
    """All five shared statuses are retried — including 502 and 504."""
    calls: list[int] = []

    @transient_retry(_is_transient_status, _LOG, wait_min=0, wait_max=0)
    def _fetch() -> str:
        calls.append(1)
        if len(calls) == 1:
            raise _status_error(code)
        return "ok"

    result = _fetch()

    assert result == "ok"
    assert len(calls) == 2


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_transient_retry_does_not_retry_permanent_status(code: int) -> None:
    calls: list[int] = []

    @transient_retry(_is_transient_status, _LOG, wait_min=0, wait_max=0)
    def _fetch() -> str:
        calls.append(1)
        raise _status_error(code)

    with pytest.raises(httpx.HTTPStatusError, match="boom"):
        _fetch()

    assert len(calls) == 1, f"HTTP {code} is permanent and must be attempted exactly once"


# ---------------------------------------------------------------------------
# Exhaustion behaviour
# ---------------------------------------------------------------------------


def test_exhaustion_reraises_original_exception_type() -> None:
    """Not tenacity's RetryError — existing except clauses depend on the real type."""
    calls: list[int] = []

    @connect_retry(_LOG, wait_min=0, wait_max=0)
    def _create() -> str:
        calls.append(1)
        raise httpx.ConnectError("connection refused")

    with pytest.raises(httpx.ConnectError, match="connection refused") as exc_info:
        _create()

    assert isinstance(exc_info.value, httpx.ConnectError)
    assert len(calls) == RETRY_MAX_ATTEMPTS


def test_exhaustion_still_matches_broad_caller_except_clause() -> None:
    """A caller catching httpx.RequestError keeps working after the migration."""

    @connect_retry(_LOG, wait_min=0, wait_max=0)
    def _create() -> str:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(httpx.RequestError, match="connection refused"):
        _create()


def test_exhaustion_logs_error_with_attempt_count(caplog: pytest.LogCaptureFixture) -> None:
    """The operator-visible report, emitted by the factory itself.

    It must not depend on how a calling module formats its own error message.
    """

    @connect_retry(_LOG, wait_min=0, wait_max=0)
    def _create() -> str:
        raise httpx.ConnectError("connection refused")

    with caplog.at_level(logging.ERROR, logger="test_retry_policy"), pytest.raises(httpx.ConnectError):
        _create()

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]

    assert len(errors) == 1, "exactly one exhaustion report expected"
    assert str(RETRY_MAX_ATTEMPTS) in errors[0].getMessage()
    assert "failed after" in errors[0].getMessage()


def test_successful_retry_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """A retry that recovers is still visible — silent absorption is the defect."""
    calls: list[int] = []

    @connect_retry(_LOG, wait_min=0, wait_max=0)
    def _create() -> str:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("connection refused")
        return "ok"

    with caplog.at_level(logging.WARNING, logger="test_retry_policy"):
        result = _create()

    assert result == "ok"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "a recovered retry must still emit a warning"


def test_exhaustion_attaches_attempt_count_note() -> None:
    """Traceback context, since the re-raised exception's own message cannot carry it."""

    @connect_retry(_LOG, wait_min=0, wait_max=0)
    def _create() -> str:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(httpx.ConnectError) as exc_info:
        _create()

    notes = getattr(exc_info.value, "__notes__", [])

    assert any(f"failed after {RETRY_MAX_ATTEMPTS} attempts" in n for n in notes), (
        f"expected an attempt-count note, got {notes!r}"
    )


def test_non_retryable_exception_is_not_annotated() -> None:
    """A single-attempt failure must stay textually distinguishable from exhaustion."""

    @connect_retry(_LOG, wait_min=0, wait_max=0)
    def _create() -> str:
        raise httpx.ReadTimeout("response lost")

    with pytest.raises(httpx.ReadTimeout) as exc_info:
        _create()

    notes = getattr(exc_info.value, "__notes__", [])

    assert notes == [], f"a non-retried failure must carry no attempt-count note, got {notes!r}"


# ---------------------------------------------------------------------------
# Policy surface
# ---------------------------------------------------------------------------


def test_with_policy_overrides_attempts() -> None:
    calls: list[int] = []

    @connect_retry(_LOG, wait_min=0, wait_max=0)
    def _create() -> str:
        calls.append(1)
        raise httpx.ConnectError("connection refused")

    tuned = _create.with_policy(attempts=5)

    with pytest.raises(httpx.ConnectError, match="connection refused"):
        tuned()

    assert len(calls) == 5


def test_factories_expose_no_way_to_disable_logging() -> None:
    """Spec: retry observability is not optional."""
    import inspect

    for factory in (transient_retry, connect_retry):
        params = set(inspect.signature(factory).parameters)
        assert not params & {"before_sleep", "log", "quiet", "silent", "verbose"}, (
            f"{factory.__name__} exposes a parameter that could disable retry logging"
        )


def test_config_import_does_not_pull_http_stack() -> None:
    """Regression guard for design D3.

    Re-exporting this module from fieldkit.config.__init__ would add httpx (232ms) and
    tenacity (51ms) to every CLI invocation, including ones that never open a socket.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, fieldkit.config; "
            "assert 'httpx' not in sys.modules, 'httpx'; "
            "assert 'tenacity' not in sys.modules, 'tenacity'",
        ],
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT,
        check=False,
    )

    assert result.returncode == 0, f"fieldkit.config eagerly imports the HTTP stack: {result.stderr}"


# ---------------------------------------------------------------------------
# Drift guard
# ---------------------------------------------------------------------------

_POLICY_MODULE = "fieldkit/config/retry.py"
_TENACITY_IMPORT_RE = re.compile(r"^\s*(?:from\s+tenacity(?:\.\S+)?\s+import|import\s+tenacity)\b", re.M)


def test_no_bare_tenacity_retry_outside_the_policy_module() -> None:
    """The consolidation is only durable if it cannot be bypassed.

    Nine sites drifted because each author copied a neighbour's inline @retry. Task
    3.7 checked this once by hand, which is the same "someone remembers" mechanism
    that produced the drift. This is the check that runs every time.

    It asserts the import, not a set of symbol names. Matching on
    stop_after_attempt/wait_exponential would miss stop_after_delay, wait_fixed, and
    the Retrying iterator API, and would false-positive on a docstring mentioning any
    of them. If tenacity is not imported, none of its strategies can be used.

    A new call site must import a factory from fieldkit.config.retry. If a genuinely
    new policy is needed, add it to that module — do not inline tenacity here.
    """
    src = Path(__file__).resolve().parent.parent / "src" / "fieldkit"
    offenders: dict[str, list[str]] = {}

    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(src.parent).as_posix()
        if rel == _POLICY_MODULE:
            continue
        match = _TENACITY_IMPORT_RE.search(path.read_text(encoding="utf-8"))
        if match:
            offenders[rel] = [match.group(0).strip()]

    assert offenders == {}, (
        "tenacity retry configured outside fieldkit/config/retry.py: "
        f"{offenders}. Import transient_retry or connect_retry instead — see that "
        "module for which one, and design D6 on choosing."
    )


def test_exhaustion_preserves_the_underlying_cause_chain() -> None:
    """`raise ... from None` would null __cause__ and discard the transport-level root.

    An httpx.ConnectError is typically raised *from* an OS-level error carrying the
    actual reason. Dropping that chain turns a diagnosable outage into a bare
    "connection failed" with no forensic trail.
    """

    def _connect_error_with_root_cause() -> httpx.ConnectError:
        exc = httpx.ConnectError("connect failed")
        try:
            raise ConnectionRefusedError("ECONNREFUSED")
        except ConnectionRefusedError as root:
            exc.__cause__ = root
        return exc

    @connect_retry(_LOG, wait_min=0, wait_max=0)
    def _create() -> str:
        raise _connect_error_with_root_cause()

    with pytest.raises(httpx.ConnectError, match="connect failed") as exc_info:
        _create()

    cause = exc_info.value.__cause__

    assert isinstance(cause, ConnectionRefusedError), f"root cause was discarded, got {cause!r}"
    assert exc_info.value.__suppress_context__ is True, "tenacity's RetryError should stay hidden"
