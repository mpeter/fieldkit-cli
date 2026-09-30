"""Private Salesforce request dispatch using the shared retry policy."""

import logging
from typing import Any

import httpx

from fieldkit.config.retry import RETRY_TRANSIENT_STATUSES, connect_retry, transient_retry

logger = logging.getLogger(__name__)

# Retry policy lives in fieldkit.config.retry.
# PATCH is idempotent *for this client*: both PATCH call sites
# (update_sobject_fields, update_opportunity_fields) send an absolute field
# assignment via json=fields, so applying one twice leaves exactly the state
# applying it once does. POST is excluded — a resource-creating POST is not safe to repeat.
# There are no POST call sites today; this is a rail for the next one.
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "PUT", "DELETE", "PATCH"})


def _is_sf_transient(exc: BaseException) -> bool:
    """True for connection failures, timeouts, and canonical transient HTTP statuses."""
    return isinstance(exc, (httpx.ConnectError, httpx.TimeoutException)) or (
        isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in RETRY_TRANSIENT_STATUSES
    )


def _raw_sf_request(client: httpx.Client, method: str, url: str, **kwargs: Any) -> httpx.Response:
    """Make an HTTP request, raising httpx.HTTPStatusError on retryable status codes.

    Module-level function so tenacity can decorate it without binding to self.
    The caller (_request_with_retry) handles auth errors and non-retryable responses.

    Args:
        client: The httpx.Client to use.
        method: HTTP method string.
        url:    Fully-qualified URL.
        **kwargs: Passed through to client.request.

    Returns:
        The httpx.Response.

    Raises:
        httpx.HTTPStatusError: for status codes in RETRY_TRANSIENT_STATUSES (triggers retry).
        httpx.HTTPError: for transport errors; the enclosing retry policy determines replay.
    """
    resp = client.request(method, url, **kwargs)
    # Raise httpx.HTTPStatusError for retryable codes (tenacity retries on these)
    # and for 401 (not retryable — _is_sf_transient returns False, tenacity re-raises).
    # We raise manually rather than calling resp.raise_for_status() so the exception
    # is raised even when resp is a MagicMock in tests (MagicMock.raise_for_status()
    # returns a MagicMock rather than raising).
    if resp.status_code in RETRY_TRANSIENT_STATUSES or resp.status_code == 401:
        logger.warning("Salesforce request returned HTTP %d", resp.status_code)
        raise httpx.HTTPStatusError(
            f"HTTP {resp.status_code}",
            request=httpx.Request(method, url),
            response=resp,
        )
    return resp


# One function serves reads and writes, so the policy cannot be chosen at
# decoration time. Decorate twice; dispatch on the method.
_sf_request_idempotent = transient_retry(_is_sf_transient, logger)(_raw_sf_request)
_sf_request_unsafe = connect_retry(logger)(_raw_sf_request)


def _sf_request(client: httpx.Client, method: str, url: str, **kwargs: Any) -> httpx.Response:
    """Make an HTTP request under the retry policy appropriate to the method.

    Idempotent methods get the full transient policy (connect failures, timeouts,
    429/5xx). Other methods retry only ConnectError and ConnectTimeout. This
    narrows but does not eliminate duplicate-write risk; see
    fieldkit.config.retry.connect_retry for the canonical policy and limitations.
    """
    if method.upper() in _IDEMPOTENT_METHODS:
        return _sf_request_idempotent(client, method, url, **kwargs)
    return _sf_request_unsafe(client, method, url, **kwargs)
