"""Unit tests for _sf_request's method-based retry classification.

`_sf_request` dispatches on the HTTP method: idempotent methods get the full transient
policy, everything else gets connect-phase-only retry. These tests pin that dispatch.

The historic regression guard is the important one. Both SF PATCH call sites send an absolute
field assignment, so they are safe to repeat, and historic regression deliberately routed them
through retry. A future change that reclassifies PATCH as non-idempotent on the
strength of HTTP semantics alone would silently revert that fix — this file fails loudly
if that happens.

`time.sleep` is patched here (matching the pattern in the sibling SF test modules)
because these tests are about which policy applies, not about backoff timing.
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from fieldkit.config.retry import RETRY_MAX_ATTEMPTS
from fieldkit.sf.client import _sf_request

pytestmark = pytest.mark.unit

_URL = "https://examplecrm.my.salesforce.com/services/data/v59.0/sobjects/Opportunity/006A1"


def _client_returning(status_code: int) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    client = MagicMock(spec=httpx.Client)
    client.request.return_value = resp
    return client


def _client_raising(exc: Exception) -> MagicMock:
    client = MagicMock(spec=httpx.Client)
    client.request.side_effect = exc
    return client


# ---------------------------------------------------------------------------
# Idempotent methods keep the full transient policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "HEAD", "PUT", "DELETE", "PATCH"])
def test_idempotent_method_retries_transient_status(method: str) -> None:
    client = _client_returning(503)

    with patch("time.sleep"), pytest.raises(httpx.HTTPStatusError, match="HTTP 503"):
        _sf_request(client, method, _URL)

    assert client.request.call_count == RETRY_MAX_ATTEMPTS


def test_patch_retries_503_bug_495_regression_guard() -> None:
    """historic regression (specs/020-sre-reliability Story 3) routed SF PATCH through retry.

    Both PATCH call sites send `json=fields` — an absolute assignment, so a duplicate
    leaves the same state. If this test fails, PATCH has been reclassified as
    non-idempotent and historic regression has been reverted.
    """
    client = _client_returning(503)

    with patch("time.sleep"), pytest.raises(httpx.HTTPStatusError, match="HTTP 503"):
        _sf_request(client, "PATCH", _URL)

    assert client.request.call_count == RETRY_MAX_ATTEMPTS, (
        "PATCH must keep full transient retry — see historic regression and design D6"
    )


@pytest.mark.parametrize("method", ["get", "Patch", "gEt"])
def test_method_classification_is_case_insensitive(method: str) -> None:
    client = _client_returning(503)

    with patch("time.sleep"), pytest.raises(httpx.HTTPStatusError, match="HTTP 503"):
        _sf_request(client, method, _URL)

    assert client.request.call_count == RETRY_MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# POST gets connect-phase retry only
# ---------------------------------------------------------------------------


def test_post_does_not_retry_transient_status() -> None:
    """A 5xx proves the request arrived; replaying a POST could duplicate a resource."""
    client = _client_returning(503)

    with patch("time.sleep"), pytest.raises(httpx.HTTPStatusError, match="HTTP 503"):
        _sf_request(client, "POST", _URL)

    assert client.request.call_count == 1


@pytest.mark.parametrize(
    "exc",
    [httpx.ReadTimeout("response lost"), httpx.RemoteProtocolError("disconnected mid-response")],
    ids=["read-timeout", "protocol-error"],
)
def test_post_does_not_retry_post_transmission_failure(exc: Exception) -> None:
    client = _client_raising(exc)

    with patch("time.sleep"), pytest.raises(type(exc)):
        _sf_request(client, "POST", _URL)

    assert client.request.call_count == 1


def test_post_does_retry_connect_failure() -> None:
    """The server provably never received it, so replaying is safe."""
    client = _client_raising(httpx.ConnectError("connection refused"))

    with patch("time.sleep"), pytest.raises(httpx.ConnectError, match="connection refused"):
        _sf_request(client, "POST", _URL)

    assert client.request.call_count == RETRY_MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# Non-retryable statuses are untouched by either policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "PATCH", "POST"])
def test_401_is_never_retried(method: str) -> None:
    client = _client_returning(401)

    with patch("time.sleep"), pytest.raises(httpx.HTTPStatusError, match="HTTP 401"):
        _sf_request(client, method, _URL)

    assert client.request.call_count == 1


@pytest.mark.parametrize("method", ["GET", "PATCH", "POST"])
def test_success_is_returned_without_retry(method: str) -> None:
    client = _client_returning(200)

    result = _sf_request(client, method, _URL)

    assert result.status_code == 200
    assert client.request.call_count == 1
