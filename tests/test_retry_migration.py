"""Unit tests for tenacity retry migration in sf/client.py and gmail/sync.py (implementation note).

Verifies that the tenacity-decorated retry predicates behave identically to
the previous manual retry loops.
"""

from unittest.mock import MagicMock

import httpx
import pytest
from googleapiclient.errors import HttpError

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _is_sf_transient predicate
# ---------------------------------------------------------------------------


def _make_httpx_status_error(status_code: int) -> httpx.HTTPStatusError:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    return httpx.HTTPStatusError("error", request=MagicMock(), response=resp)


def test_is_sf_transient_true_for_retry_statuses() -> None:
    from fieldkit.sf.client import _is_sf_transient

    for code in (429, 500, 502, 503, 504):
        exc = _make_httpx_status_error(code)
        assert _is_sf_transient(exc), f"Expected transient for HTTP {code}"


def test_is_sf_transient_false_for_401() -> None:
    from fieldkit.sf.client import _is_sf_transient

    exc = _make_httpx_status_error(401)
    assert not _is_sf_transient(exc)


def test_is_sf_transient_false_for_non_http_error() -> None:
    from fieldkit.sf.client import _is_sf_transient

    assert not _is_sf_transient(ValueError("not an http error"))
    assert not _is_sf_transient(RuntimeError("connection refused"))


# ---------------------------------------------------------------------------
# _is_gmail_transient predicate
# ---------------------------------------------------------------------------


def _make_http_error(status: int) -> HttpError:
    resp = MagicMock()
    resp.status = status
    return HttpError(resp=resp, content=b"error")


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_is_gmail_transient_true_for_retry_statuses(code: int) -> None:
    from fieldkit.gmail.retry import _is_gmail_transient

    exc = _make_http_error(code)
    assert _is_gmail_transient(exc), f"Expected transient for HTTP {code}"


def test_is_gmail_transient_false_for_403() -> None:
    from fieldkit.gmail.retry import _is_gmail_transient

    exc = _make_http_error(403)
    assert not _is_gmail_transient(exc)


def test_is_gmail_transient_true_for_403_quota_exceeded() -> None:
    """403 with a quota-exceeded reason in the body is transient (historic regression)."""
    import json

    from fieldkit.gmail.retry import _is_gmail_transient

    resp = MagicMock()
    resp.status = 403
    content = json.dumps({"error": {"errors": [{"reason": "quotaExceeded"}]}}).encode()
    exc = HttpError(resp=resp, content=content)
    assert _is_gmail_transient(exc)


def test_is_gmail_transient_false_for_403_permission_denied() -> None:
    """403 with a non-quota reason (genuine permission failure) is not transient."""
    import json

    from fieldkit.gmail.retry import _is_gmail_transient

    resp = MagicMock()
    resp.status = 403
    content = json.dumps({"error": {"errors": [{"reason": "forbidden"}]}}).encode()
    exc = HttpError(resp=resp, content=content)
    assert not _is_gmail_transient(exc)


def test_is_gmail_transient_false_for_non_http_error() -> None:
    from fieldkit.gmail.retry import _is_gmail_transient

    assert not _is_gmail_transient(ValueError("not an http error"))


# ---------------------------------------------------------------------------
# GmailAuthError raised instead of sys.exit(2) on 403
# ---------------------------------------------------------------------------


def test_gmail_403_quota_raises_gmail_auth_error() -> None:
    """403 with quota-project reason raises GmailAuthError instead of sys.exit(2)."""
    import json

    from googleapiclient.errors import HttpError

    from fieldkit.errors import GmailAuthError
    from fieldkit.gmail.sync_engine import _api_call_with_retry

    resp = MagicMock()
    resp.status = 403
    content = json.dumps({"error": {"status": "PERMISSION_DENIED"}}).encode()
    exc = HttpError(resp=resp, content=content)

    def _raise() -> None:
        raise exc

    with pytest.raises(GmailAuthError, match="access denied"):
        _api_call_with_retry(_raise, context="test")


# ---------------------------------------------------------------------------
# _is_transient_http_error predicate (Google Docs)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_is_transient_http_error_true_for_retry_statuses(code: int) -> None:
    """502 and 504 included: this predicate hardcoded {429, 500, 503} and dropped them.

    It now sources RETRY_TRANSIENT_STATUSES, so it cannot drift from the SF and Gmail
    predicates again.
    """
    from fieldkit.ingest.docs import _is_transient_http_error

    exc = _make_http_error(code)
    assert _is_transient_http_error(exc), f"Expected transient for HTTP {code}"


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_is_transient_http_error_false_for_permanent_statuses(code: int) -> None:
    from fieldkit.ingest.docs import _is_transient_http_error

    exc = _make_http_error(code)
    assert not _is_transient_http_error(exc)


def test_is_transient_http_error_false_for_non_http_error() -> None:
    from fieldkit.ingest.docs import _is_transient_http_error

    assert not _is_transient_http_error(ValueError("not an http error"))


# ---------------------------------------------------------------------------
# Cross-predicate agreement — the drift guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_all_domain_predicates_agree_on_transient(code: int) -> None:
    """The three domain predicates must classify the shared status set identically.

    They live in separate modules by necessity (each is specific to its client
    library), so this is what stops them diverging again.
    """
    from fieldkit.gmail.retry import _is_gmail_transient
    from fieldkit.ingest.docs import _is_transient_http_error
    from fieldkit.sf.client import _is_sf_transient

    verdicts = {
        "sf": _is_sf_transient(_make_httpx_status_error(code)),
        "gmail": _is_gmail_transient(_make_http_error(code)),
        "docs": _is_transient_http_error(_make_http_error(code)),
    }

    assert all(verdicts.values()), f"predicates disagree on HTTP {code}: {verdicts}"


@pytest.mark.parametrize("code", [400, 401, 404])
def test_all_domain_predicates_agree_on_permanent(code: int) -> None:
    from fieldkit.gmail.retry import _is_gmail_transient
    from fieldkit.ingest.docs import _is_transient_http_error
    from fieldkit.sf.client import _is_sf_transient

    verdicts = {
        "sf": _is_sf_transient(_make_httpx_status_error(code)),
        "gmail": _is_gmail_transient(_make_http_error(code)),
        "docs": _is_transient_http_error(_make_http_error(code)),
    }

    assert not any(verdicts.values()), f"predicates disagree on HTTP {code}: {verdicts}"
