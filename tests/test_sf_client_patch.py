"""Unit tests for SFDirectClient PATCH methods — update_opportunity_fields() and update_sobject_fields().

historic regression: The HTTP layer for PATCH operations was entirely untested.
These are the most dangerous write operations — they patch live SF data.

Uses unittest.mock.patch on httpx.Client (NOT respx — not in deps).
Pattern mirrors test_sf_direct.py: patch 'httpx.Client' constructor so
SFDirectClient.__init__ picks up the mock instance.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from fieldkit.sf.client import (
    SFAPIError,
    SFAuthError,
    SFConditionalWriteConflict,
    SFConditionalWriteOutcomeUnknown,
    SFDirectClient,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Shared constants and helpers
# ---------------------------------------------------------------------------

_BASE_URL = "https://examplecrm.my.salesforce.com"
_SID = "test_session_id_patch_abc123"
_OPP_ID = "006A100000TestOppId"
_SOBJECT_TYPE = "SBQQ__Quote__c"
_RECORD_ID = "a0B1000000TestQuoteId"


def _make_client() -> SFDirectClient:
    """Construct a SFDirectClient with test credentials and enter its context manager.

    G1a: SFDirectClient now requires a context manager. Tests that call methods
    directly must use this helper which enters __enter__ so _require_open() works.
    The httpx.Client constructor must be patched BEFORE calling this function.
    """
    client = SFDirectClient(session_id=_SID, base_url=_BASE_URL)
    client.__enter__()
    return client


def _make_response(
    *,
    status_code: int = 204,
    json_body: Any = None,
    content_type: str = "application/json",
) -> MagicMock:
    """Build a mock httpx.Response with the given status code."""
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = status_code
    mock_resp.headers = {"content-type": content_type}
    if json_body is not None:
        mock_resp.json.return_value = json_body
    else:
        mock_resp.json.return_value = {}
    return mock_resp


def _make_mock_http_client(
    *,
    status_code: int = 204,
    json_body: Any = None,
    content_type: str = "application/json",
    side_effects: list[Any] | None = None,
) -> MagicMock:
    """Build a mock httpx.Client instance for patching 'httpx.Client' constructor.

    SFDirectClient calls self._client.request(method, url, **kwargs) via
    _request_with_retry. Patch with:
        with patch("httpx.Client", return_value=mock_instance): ...
    """
    mock_instance = MagicMock()
    if side_effects is not None:
        mock_instance.request.side_effect = side_effects
    else:
        resp = _make_response(status_code=status_code, json_body=json_body, content_type=content_type)
        mock_instance.request.return_value = resp
    return mock_instance


# ---------------------------------------------------------------------------
# TestUpdateOpportunityFields
# ---------------------------------------------------------------------------


# ── TestUpdateOpportunityFields (flattened) ─────────────────────────────────


def test_update_opportunity_fields_204_response_succeeds() -> None:
    """HTTP 204 (No Content) → success, no exception raised."""
    mock_http = _make_mock_http_client(status_code=204)
    with patch("httpx.Client", return_value=mock_http):
        client = _make_client()
        # Should not raise
        client.update_opportunity_fields(_OPP_ID, {"StageName": "Closed Won"})

    # Verify PATCH was called
    assert mock_http.request.call_count == 1
    call_args = mock_http.request.call_args
    assert call_args[0][0] == "PATCH"


def test_update_opportunity_fields_200_response_also_succeeds() -> None:
    """HTTP 200 → also treated as success (some SF orgs return 200 on PATCH)."""
    mock_http = _make_mock_http_client(status_code=200)
    with patch("httpx.Client", return_value=mock_http):
        client = _make_client()
        # Should not raise
        client.update_opportunity_fields(_OPP_ID, {"Next_Steps__c": "Schedule POC"})


def test_update_opportunity_fields_401_raises_sfautherror() -> None:
    """HTTP 401 → raises SFAuthError with auth-sf guidance."""
    # _request_with_retry raises SFAuthError immediately on 401 (no retry).
    mock_http = _make_mock_http_client(status_code=401)
    with patch("httpx.Client", return_value=mock_http):
        client = _make_client()
        with pytest.raises(SFAuthError) as exc_info:
            client.update_opportunity_fields(_OPP_ID, {"StageName": "Closed Won"})
    assert "auth sf" in str(exc_info.value)


def test_update_opportunity_fields_400_raises_sfapierror() -> None:
    """HTTP 400 (Bad Request) → raises SFAPIError with status code in message."""
    mock_http = _make_mock_http_client(
        status_code=400,
        json_body=[{"message": "Required fields missing", "errorCode": "REQUIRED_FIELD_MISSING"}],
    )
    with patch("httpx.Client", return_value=mock_http):
        client = _make_client()
        with pytest.raises(SFAPIError, match="400"):
            client.update_opportunity_fields(_OPP_ID, {"Bad_Field__c": "value"})


def test_update_opportunity_fields_403_raises_sfapierror() -> None:
    """HTTP 403 (Forbidden) → raises SFAPIError."""
    mock_http = _make_mock_http_client(
        status_code=403,
        json_body=[
            {"message": "insufficient access rights", "errorCode": "INSUFFICIENT_ACCESS_ON_CROSS_REFERENCE_ENTITY"}
        ],
    )
    with patch("httpx.Client", return_value=mock_http):
        client = _make_client()
        with pytest.raises(SFAPIError, match="403"):
            client.update_opportunity_fields(_OPP_ID, {"StageName": "Closed Won"})


def test_update_opportunity_fields_connect_error_raises_sfapierror() -> None:
    """httpx.ConnectError → raises SFAPIError.

    Surprise: update_opportunity_fields() calls _request_with_retry() which
    does NOT wrap ConnectError — it propagates out of the retry loop.
    The ConnectError is then caught by the caller's exception handler in
    update_opportunity_fields(), which does NOT have a try/except around
    _request_with_retry. So ConnectError propagates as-is.

    Actually: _request_with_retry calls self._client.request() directly.
    ConnectError from self._client.request() propagates out of _request_with_retry
    because _request_with_retry has no ConnectError handler (unlike sosl_search).
    update_opportunity_fields() also has no ConnectError handler.
    So ConnectError propagates to the test.
    """
    mock_http = _make_mock_http_client()
    mock_http.request.side_effect = httpx.ConnectError("connection refused")
    with patch("httpx.Client", return_value=mock_http):
        client = _make_client()
        with pytest.raises(httpx.ConnectError, match="connection refused"):
            client.update_opportunity_fields(_OPP_ID, {"StageName": "Closed Won"})


def test_update_opportunity_fields_retries_on_503_then_succeeds() -> None:
    """HTTP 503 (transient) → retried; second attempt returns 204 → success.

    historic regression: update_opportunity_fields() routes through _request_with_retry
    so transient 5xx errors are retried (up to _MAX_RETRIES=3 attempts).
    """
    resp_503 = _make_response(status_code=503)
    resp_204 = _make_response(status_code=204)
    mock_http = _make_mock_http_client()
    mock_http.request.side_effect = [resp_503, resp_204]

    with patch("httpx.Client", return_value=mock_http), patch("time.sleep"):
        client = _make_client()
        # Should succeed on second attempt (no exception)
        client.update_opportunity_fields(_OPP_ID, {"StageName": "Closed Won"})

    # Verify two requests were made (first 503, then 204)
    assert mock_http.request.call_count == 2


def test_update_opportunity_fields_patch_url_contains_opportunity_id() -> None:
    """PATCH request URL contains the opportunity record ID."""
    mock_http = _make_mock_http_client(status_code=204)
    with patch("httpx.Client", return_value=mock_http):
        client = _make_client()
        client.update_opportunity_fields(_OPP_ID, {"StageName": "Closed Won"})

    call_args = mock_http.request.call_args
    url = call_args[0][1]
    assert _OPP_ID in url
    assert "/sobjects/Opportunity/" in url


def test_update_opportunity_fields_patch_sends_json_body() -> None:
    """PATCH request sends the fields dict as JSON body."""
    fields = {"StageName": "Closed Won", "Next_Steps__c": "Sign contract"}
    mock_http = _make_mock_http_client(status_code=204)
    with patch("httpx.Client", return_value=mock_http):
        client = _make_client()
        client.update_opportunity_fields(_OPP_ID, fields)

    call_kwargs = mock_http.request.call_args[1]
    assert call_kwargs["json"] == fields


def test_update_opportunity_fields_patch_sends_auth_header() -> None:
    """PATCH request includes Authorization: Bearer <sid> header."""
    mock_http = _make_mock_http_client(status_code=204)
    with patch("httpx.Client", return_value=mock_http):
        client = _make_client()
        client.update_opportunity_fields(_OPP_ID, {"StageName": "Closed Won"})

    call_kwargs = mock_http.request.call_args[1]
    assert call_kwargs["headers"]["Authorization"] == f"Bearer {_SID}"
    assert call_kwargs["headers"]["Content-Type"] == "application/json"


def test_update_opportunity_fields_all_retries_exhausted_on_503_raises_sfapierror() -> None:
    """HTTP 503 on all 3 attempts → raises SFAPIError after retries exhausted."""
    resp_503 = _make_response(status_code=503)
    mock_http = _make_mock_http_client()
    # Return 503 for all 3 attempts (_MAX_RETRIES = 3)
    mock_http.request.side_effect = [resp_503, resp_503, resp_503]

    with patch("httpx.Client", return_value=mock_http), patch("time.sleep"):
        client = _make_client()
        with pytest.raises(SFAPIError, match=r"after \d+ attempts"):
            client.update_opportunity_fields(_OPP_ID, {"StageName": "Closed Won"})

    assert mock_http.request.call_count == 3


# ---------------------------------------------------------------------------
# TestUpdateSobjectFields
# ---------------------------------------------------------------------------


# ── TestUpdateSobjectFields (flattened) ─────────────────────────────────────


def test_update_sobject_fields_204_response_succeeds() -> None:
    """HTTP 204 → success, no exception raised."""
    mock_http = _make_mock_http_client(status_code=204)
    with patch("httpx.Client", return_value=mock_http):
        client = _make_client()
        client.update_sobject_fields(_SOBJECT_TYPE, _RECORD_ID, {"Status__c": "Active"})


def test_update_sobject_fields_200_response_succeeds() -> None:
    """HTTP 200 → also treated as success."""
    mock_http = _make_mock_http_client(status_code=200)
    with patch("httpx.Client", return_value=mock_http):
        client = _make_client()
        client.update_sobject_fields(_SOBJECT_TYPE, _RECORD_ID, {"Status__c": "Active"})


def test_update_sobject_fields_401_raises_sfautherror() -> None:
    """HTTP 401 → raises SFAuthError."""
    mock_http = _make_mock_http_client(status_code=401)
    with patch("httpx.Client", return_value=mock_http):
        client = _make_client()
        with pytest.raises(SFAuthError, match=r"."):
            client.update_sobject_fields(_SOBJECT_TYPE, _RECORD_ID, {"Status__c": "Active"})


def test_update_sobject_fields_400_raises_sfapierror() -> None:
    """HTTP 400 → raises SFAPIError with sobject type in message."""
    mock_http = _make_mock_http_client(
        status_code=400,
        json_body=[{"message": "invalid field", "errorCode": "INVALID_FIELD"}],
    )
    with patch("httpx.Client", return_value=mock_http):
        client = _make_client()
        with pytest.raises(SFAPIError, match="400"):
            client.update_sobject_fields(_SOBJECT_TYPE, _RECORD_ID, {"Bad__c": "x"})


def test_update_sobject_fields_patch_url_contains_sobject_type_and_record_id() -> None:
    """PATCH URL contains the sObject type and record ID."""
    mock_http = _make_mock_http_client(status_code=204)
    with patch("httpx.Client", return_value=mock_http):
        client = _make_client()
        client.update_sobject_fields(_SOBJECT_TYPE, _RECORD_ID, {"Status__c": "Active"})

    call_args = mock_http.request.call_args
    url = call_args[0][1]
    assert _SOBJECT_TYPE in url
    assert _RECORD_ID in url


def test_update_sobject_fields_routes_through_retry_on_503() -> None:
    """update_sobject_fields() routes through _request_with_retry (historic regression fix).

    Verifies that transient 503 errors are retried rather than immediately failing.
    """
    resp_503 = _make_response(status_code=503)
    resp_204 = _make_response(status_code=204)
    mock_http = _make_mock_http_client()
    mock_http.request.side_effect = [resp_503, resp_204]

    with patch("httpx.Client", return_value=mock_http), patch("time.sleep"):
        client = _make_client()
        # Should succeed on second attempt
        client.update_sobject_fields(_SOBJECT_TYPE, _RECORD_ID, {"Status__c": "Active"})

    # Two requests: first 503 (retried), then 204 (success)
    assert mock_http.request.call_count == 2


# ---------------------------------------------------------------------------
# Conditional ClosePlan writer transport
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "timestamp",
    ["2026-09-15T12:00:00.000+0000", "2026-09-15T12:00:00.000Z"],
)
def test_conditional_update_sobject_fields_uses_http_date_precondition_without_retry(timestamp: str) -> None:
    """A guarded write converts the UI API timestamp to Salesforce's HTTP-date header."""
    mock_http = _make_mock_http_client(status_code=204)
    with patch("httpx.Client", return_value=mock_http):
        client = _make_client()
        client.conditional_update_sobject_fields(
            "TSPC__DealQuestion__c",
            _RECORD_ID,
            {"TSPC__Score__c": 2},
            if_unmodified_since=timestamp,
        )

    assert mock_http.request.call_count == 1
    request_kwargs = mock_http.request.call_args.kwargs
    assert request_kwargs["headers"]["If-Unmodified-Since"] == "Tue, 15 Sep 2026 12:00:00 GMT"
    assert request_kwargs["headers"]["Content-Type"] == "application/json"
    assert request_kwargs["json"] == {"TSPC__Score__c": 2}


def test_conditional_update_sobject_fields_refuses_timestamp_without_timezone() -> None:
    """A malformed concurrency timestamp fails before a guarded write is sent."""
    mock_http = _make_mock_http_client(status_code=204)
    with patch("httpx.Client", return_value=mock_http):
        client = _make_client()
        with pytest.raises(SFAPIError, match="lacks a timezone"):
            client.conditional_update_sobject_fields(
                "TSPC__DealQuestion__c",
                _RECORD_ID,
                {"TSPC__Score__c": 2},
                if_unmodified_since="2026-09-15T12:00:00",
            )

    mock_http.request.assert_not_called()


def test_conditional_update_sobject_fields_rejects_server_conflict_without_retry() -> None:
    """A stale server precondition stays a conflict, never a generic PATCH retry."""
    mock_http = _make_mock_http_client(status_code=412)
    with patch("httpx.Client", return_value=mock_http):
        client = _make_client()
        with pytest.raises(SFConditionalWriteConflict, match="precondition"):
            client.conditional_update_sobject_fields(
                "TSPC__DealQuestion__c",
                _RECORD_ID,
                {"TSPC__Score__c": 2},
                if_unmodified_since="2026-09-15T12:00:00.000+0000",
            )

    assert mock_http.request.call_count == 1


def test_conditional_update_sobject_fields_treats_transient_response_as_unknown_once() -> None:
    """A response that cannot establish application is unknown and is never retried."""
    mock_http = _make_mock_http_client(status_code=503)
    with patch("httpx.Client", return_value=mock_http):
        client = _make_client()
        with pytest.raises(SFConditionalWriteOutcomeUnknown, match="outcome is unknown"):
            client.conditional_update_sobject_fields(
                "TSPC__DealQuestion__c",
                _RECORD_ID,
                {"TSPC__Score__c": 2},
                if_unmodified_since="2026-09-15T12:00:00.000+0000",
            )

    assert mock_http.request.call_count == 1


def test_conditional_update_sobject_fields_treats_transport_error_as_unknown_once() -> None:
    """A lost transport response cannot be retried because Salesforce may have applied it."""
    mock_http = _make_mock_http_client()
    mock_http.request.side_effect = httpx.ConnectError("connection lost")
    with patch("httpx.Client", return_value=mock_http):
        client = _make_client()
        with pytest.raises(SFConditionalWriteOutcomeUnknown, match="outcome is unknown"):
            client.conditional_update_sobject_fields(
                "TSPC__DealQuestion__c",
                _RECORD_ID,
                {"TSPC__Score__c": 2},
                if_unmodified_since="2026-09-15T12:00:00.000+0000",
            )

    assert mock_http.request.call_count == 1
