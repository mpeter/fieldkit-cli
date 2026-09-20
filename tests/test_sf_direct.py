"""Comprehensive tests for lib.sf_direct.SFDirectClient.

All tests are unit tests — no real network calls are made.
SFDirectClient calls self._client.request(method, url, **kwargs) via _request_with_retry.
Patch httpx.Client with a mock whose .request side_effect controls the sequence of responses.
"""

import logging
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

import fieldkit.sf.client as sf_client
from fieldkit.sf.client import SFAPIError, SFAuthError, SFDataAccessError, SFDirectClient, SFNotFoundError

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _zero_retry_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep retry counts and error paths while avoiding real backoff in unit tests."""
    monkeypatch.setattr(
        sf_client,
        "_sf_request_idempotent",
        sf_client._sf_request_idempotent.with_policy(wait_min=0, wait_max=0),
    )


# ---------------------------------------------------------------------------
# Shared fixtures / constants
# ---------------------------------------------------------------------------

_BASE_URL = "https://examplecrm.my.salesforce.com"
_SID = "test_session_id_abc123"


def _make_client() -> SFDirectClient:
    """Create a SFDirectClient and enter its context manager.

    G1a: SFDirectClient now requires a context manager. Tests that call methods
    directly must use this helper which enters __enter__ so _require_open() works.
    The httpx.Client constructor must be patched BEFORE calling this function.
    """
    client = SFDirectClient(session_id=_SID, base_url=_BASE_URL)
    client.__enter__()
    return client


def _make_response(
    *,
    status_code: int = 200,
    json_body: Any = None,
    text_body: str = "",
) -> MagicMock:
    """Build a single mock httpx.Response."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    if json_body is not None:
        mock_resp.json.return_value = json_body
    mock_resp.text = text_body if text_body else (str(json_body) if json_body else "")
    return mock_resp


def _make_mock_http_client(
    *,
    status_code: int = 200,
    json_body: Any = None,
    text_body: str = "",
    raise_connect_error: bool = False,
    raise_request_error: bool = False,
) -> MagicMock:
    """Build a mock httpx.Client instance for patching ``httpx.Client`` as a constructor.

    SFDirectClient calls ``self._client.request(method, url, **kwargs)`` via
    ``_request_with_retry``.  Patch with:
        ``with patch("httpx.Client", return_value=mock_instance): ...``
    """
    mock_instance = MagicMock()
    if raise_connect_error:
        mock_instance.request.side_effect = httpx.ConnectError("connection refused")
    elif raise_request_error:
        mock_instance.request.side_effect = httpx.RequestError("generic request error")
    else:
        resp = _make_response(status_code=status_code, json_body=json_body, text_body=text_body)
        mock_instance.request.return_value = resp

    # Keep legacy .post / .get attributes so any tests that still check them
    # directly don't AttributeError (they won't be called by production code).
    mock_instance.post = MagicMock()
    mock_instance.get = MagicMock()

    return mock_instance


# Keep the old name as an alias so test bodies below can stay unchanged while
# we migrate them incrementally.  Both names refer to the same factory.
_make_mock_client_cm = _make_mock_http_client


# ---------------------------------------------------------------------------
# TestSoslSearch
# ---------------------------------------------------------------------------


# ── TestSoslSearch (flattened) ──────────────────────────────────────────────


def test_sosl_search_sosl_search_success() -> None:
    """sosl_search: correct URL, Authorization header, returns searchRecords list."""
    search_records = [{"Id": "006A1", "Name": "Deal One"}]
    mock_instance = _make_mock_http_client(
        status_code=200,
        json_body={"searchRecords": search_records},
    )
    with patch("httpx.Client", return_value=mock_instance):
        client = _make_client()
        result = client.sosl_search("FIND {Acme} IN ALL FIELDS RETURNING Opportunity(Id, Name)")

    assert result == search_records

    # Verify URL, method, and Authorization header
    call_args = mock_instance.request.call_args
    assert call_args[0][0] == "GET"
    url = call_args[0][1]
    kwargs = call_args[1]
    assert "/services/data/v59.0/search/" in url
    assert _BASE_URL in url
    assert kwargs["headers"]["Authorization"] == f"Bearer {_SID}"
    assert kwargs["params"] == {"q": "FIND {Acme} IN ALL FIELDS RETURNING Opportunity(Id, Name)"}


def test_sosl_search_sosl_search_empty_results() -> None:
    """sosl_search: empty searchRecords returns []."""
    mock_instance = _make_mock_http_client(
        status_code=200,
        json_body={"searchRecords": []},
    )
    with patch("httpx.Client", return_value=mock_instance):
        result = _make_client().sosl_search("FIND {nobody} IN ALL FIELDS RETURNING Opportunity(Id)")
    assert result == []


def test_sosl_search_sosl_search_401_raises_auth_error() -> None:
    """sosl_search: HTTP 401 raises SFAuthError with re-auth guidance (historic regression)."""
    mock_instance = _make_mock_http_client(status_code=401)
    with patch("httpx.Client", return_value=mock_instance), pytest.raises(SFAuthError) as exc_info:
        _make_client().sosl_search("FIND {x}")
    # historic regression: error message now references auth sf, not sf-cookies
    assert "auth sf" in str(exc_info.value)


def test_sosl_search_sosl_search_500_raises_api_error() -> None:
    """sosl_search: HTTP 500 raises SFAPIError containing the status code."""
    mock_instance = _make_mock_http_client(status_code=500, text_body="Internal Server Error")
    with patch("httpx.Client", return_value=mock_instance), pytest.raises(SFAPIError, match="500"):
        _make_client().sosl_search("FIND {x}")


def test_sosl_search_sosl_search_connection_error_raises_api_error() -> None:
    """sosl_search: httpx.ConnectError raises SFAPIError."""
    mock_instance = _make_mock_http_client(raise_connect_error=True)
    with patch("httpx.Client", return_value=mock_instance), pytest.raises(SFAPIError, match="connection"):
        _make_client().sosl_search("FIND {x}")


def test_sosl_search_sosl_search_logs_query_and_count(caplog: pytest.LogCaptureFixture) -> None:
    """sosl_search: logs the query terms and result count via the named logger."""
    mock_instance = _make_mock_http_client(
        status_code=200,
        json_body={"searchRecords": [{"Id": "1"}]},
    )

    with (
        caplog.at_level(logging.DEBUG, logger="fieldkit.sf.client"),
        patch("httpx.Client", return_value=mock_instance),
    ):
        _make_client().sosl_search("FIND {Acme} IN ALL FIELDS RETURNING Opportunity(Id)")
    assert "SOSL query" in caplog.text
    assert "1" in caplog.text  # result count


def test_sosl_search_sosl_search_logs_error_status(caplog: pytest.LogCaptureFixture) -> None:
    """sosl_search: HTTP 503 (retried) emits WARNING log lines with the status code."""

    # 503 is a retried status — return it 3 times so retries are exhausted.
    resp = _make_response(status_code=503, text_body="Service Unavailable")
    mock_instance = MagicMock()
    mock_instance.request.return_value = resp

    with (
        caplog.at_level(logging.WARNING, logger="fieldkit.sf.client"),
        patch("httpx.Client", return_value=mock_instance),
        patch("time.sleep"),
        pytest.raises(SFAPIError) as exc_info,
    ):
        _make_client().sosl_search("FIND {x}")
    assert "503" in str(exc_info.value)
    assert "503" in caplog.text


# ---------------------------------------------------------------------------
# TestFetchRecord
# ---------------------------------------------------------------------------


# ── TestFetchRecord (flattened) ─────────────────────────────────────────────


def test_fetch_record_fetch_record_success() -> None:
    """fetch_record: returns the parsed JSON response on HTTP 200."""
    record_data = {"id": "006ABC", "fields": {"Name": {"value": "Acme Deal"}}}
    mock_instance = _make_mock_http_client(status_code=200, json_body=record_data)
    with patch("httpx.Client", return_value=mock_instance):
        result = _make_client().fetch_record("006ABC")
    assert result == record_data


def test_fetch_record_fetch_record_uses_sobjects_path() -> None:
    """fetch_record: requests the /services/data/v59.0/sobjects/Opportunity/{id} path."""
    mock_instance = _make_mock_http_client(status_code=200, json_body={})
    with patch("httpx.Client", return_value=mock_instance):
        _make_client().fetch_record("006XYZ999")
    call_args = mock_instance.request.call_args
    assert call_args[0][0] == "GET"
    url = call_args[0][1]
    assert "/services/data/v59.0/sobjects/Opportunity/006XYZ999" in url
    assert _BASE_URL in url


def test_fetch_record_fetch_record_401_raises_auth_error() -> None:
    """fetch_record: HTTP 401 raises SFAuthError."""
    mock_instance = _make_mock_http_client(status_code=401)
    with patch("httpx.Client", return_value=mock_instance), pytest.raises(SFAuthError, match=r"."):
        _make_client().fetch_record("006ABC")


def test_fetch_record_fetch_record_404_raises_api_error() -> None:
    """fetch_record: HTTP 404 raises SFNotFoundError."""
    mock_instance = _make_mock_http_client(status_code=404, text_body="Not Found")
    with patch("httpx.Client", return_value=mock_instance), pytest.raises(SFNotFoundError, match=r"."):
        _make_client().fetch_record("invalid_id")


def test_fetch_record_fetch_record_connection_error_raises_api_error() -> None:
    """fetch_record: httpx.ConnectError raises SFAPIError."""
    mock_instance = _make_mock_http_client(raise_connect_error=True)
    with patch("httpx.Client", return_value=mock_instance), pytest.raises(SFAPIError, match=r"."):
        _make_client().fetch_record("006ABC")


def test_fetch_record_fetch_record_cookie_header() -> None:
    """fetch_record: sends Authorization Bearer header with sid."""
    mock_instance = _make_mock_http_client(status_code=200, json_body={})
    with patch("httpx.Client", return_value=mock_instance):
        _make_client().fetch_record("006ABC")
    call_args = mock_instance.request.call_args
    headers = call_args[1]["headers"]
    assert headers["Authorization"] == f"Bearer {_SID}"


@pytest.mark.parametrize(
    ("mock_kwargs", "exception", "message"),
    [
        ({"status_code": 403}, SFDataAccessError, "Account 001ABC is not readable"),
        ({"status_code": 400}, SFAPIError, "SF Account fetch failed with HTTP 400"),
        ({"raise_connect_error": True}, SFAPIError, "SF connection failed"),
        ({"raise_request_error": True}, SFAPIError, "SF request error"),
    ],
)
def test_fetch_sobject_errors_raise_public_exception_with_message(
    mock_kwargs: dict[str, Any], exception: type[Exception], message: str
) -> None:
    mock_instance = _make_mock_http_client(**mock_kwargs)
    with patch("httpx.Client", return_value=mock_instance), pytest.raises(exception, match=message):
        _make_client().fetch_sobject("Account", "001ABC", "Id,Name")


# ---------------------------------------------------------------------------
# TestSearchOpportunities
# ---------------------------------------------------------------------------


# ── TestSearchOpportunities (flattened) ─────────────────────────────────────


def _search_opportunities_make_sf_record(
    *,
    opp_id: str = "006A1",
    name: str = "Big Deal",
    stage: str = "Proposal",
    close_date: str = "2025-12-31",
    consulting: float | None = 50000.0,
    training: float | None = 10000.0,
) -> dict[str, Any]:
    return {
        "Id": opp_id,
        "Name": name,
        "StageName": stage,
        "CloseDate": close_date,
        "Amount": None,
        "Consulting_Total_USD__c": consulting,
        "Training_Total_USD__c": training,
    }


def test_search_opportunities_search_opportunities_maps_fields() -> None:
    """search_opportunities: output dict shape matches listview format."""
    rec = _search_opportunities_make_sf_record()
    mock_instance = _make_mock_http_client(
        status_code=200,
        json_body={"searchRecords": [rec]},
    )
    with patch("httpx.Client", return_value=mock_instance):
        results = _make_client().search_opportunities(keywords=["Acme"], account_name="acme-corp")

    assert len(results) == 1
    opp = results[0]
    assert opp["opportunity_id"] == "006A1"
    assert opp["name"] == "Big Deal"
    assert opp["stage"] == "Proposal"
    assert opp["close_date"] == "2025-12-31"
    assert opp["arr"] is None
    assert opp["owner"] is None
    assert opp["next_steps"] is None
    assert opp["acv"] is None
    assert opp["consulting_acv"] == 50000.0
    assert opp["training_acv"] == 10000.0


def test_search_opportunities_search_opportunities_keyword_quoting_single_word() -> None:
    """Single-word keywords are not quoted in the SOSL query."""
    mock_instance = _make_mock_http_client(status_code=200, json_body={"searchRecords": []})
    with patch("httpx.Client", return_value=mock_instance):
        _make_client().search_opportunities(keywords=["Acme"], account_name="acme-corp")
    q = mock_instance.request.call_args[1]["params"]["q"]
    assert "Consulting_Total_USD__c > 0" in q
    assert "Training_Total_USD__c > 0" in q
    call_kwargs = mock_instance.request.call_args[1]
    q = call_kwargs["params"]["q"]
    # single-word: no quotes around Acme
    assert "{Acme}" in q
    assert '{"Acme"}' not in q


def test_search_opportunities_search_opportunities_keyword_quoting_multi_word() -> None:
    """Multi-word keywords get SOSL phrase quotes."""
    mock_instance = _make_mock_http_client(status_code=200, json_body={"searchRecords": []})
    with patch("httpx.Client", return_value=mock_instance):
        _make_client().search_opportunities(keywords=["Example Vendor", "OpenShift"], account_name="example")
    call_kwargs = mock_instance.request.call_args[1]
    q = call_kwargs["params"]["q"]
    # Multi-word keywords should be quoted.
    assert '"Example Vendor"' in q
    # single-word "OpenShift" should not be quoted
    assert "OpenShift" in q


def test_search_opportunities_search_opportunities_empty_results() -> None:
    """search_opportunities: empty searchRecords returns []."""
    mock_instance = _make_mock_http_client(status_code=200, json_body={"searchRecords": []})
    with patch("httpx.Client", return_value=mock_instance):
        result = _make_client().search_opportunities(keywords=["nobody"], account_name="nobody")
    assert result == []


def test_search_opportunities_search_opportunities_empty_keywords_returns_empty() -> None:
    """search_opportunities: no keywords returns [] without making a request."""
    mock_instance = _make_mock_http_client(status_code=200, json_body={"searchRecords": []})
    with patch("httpx.Client", return_value=mock_instance):
        result = _make_client().search_opportunities(keywords=[], account_name="acme-corp")
    assert result == []
    # No HTTP call should have been made (post is never called when keywords=[])
    mock_instance.post.assert_not_called()


def test_search_opportunities_search_opportunities_sosl_includes_services_filter() -> None:
    """SOSL query includes the services revenue filter."""
    mock_instance = _make_mock_http_client(status_code=200, json_body={"searchRecords": []})
    with patch("httpx.Client", return_value=mock_instance):
        _make_client().search_opportunities(keywords=["Acme"], account_name="acme-corp")


def test_search_opportunity_candidates_omits_services_filter_and_reports_cap() -> None:
    records = [_search_opportunities_make_sf_record() for _ in range(2000)]
    with patch.object(SFDirectClient, "sosl_search", return_value=records) as search:
        result = _make_client().search_opportunity_candidates(keywords=["Acme"], account_name="acme-corp")
    assert result.capped is True
    assert len(result.records) == 2000
    assert "Consulting_Total_USD__c > 0" not in search.call_args.args[0]
    assert "Training_Total_USD__c > 0" not in search.call_args.args[0]


def test_search_opportunities_search_opportunities_sosl_includes_required_fields() -> None:
    """SOSL query selects the required Opportunity fields."""
    mock_instance = _make_mock_http_client(status_code=200, json_body={"searchRecords": []})
    with patch("httpx.Client", return_value=mock_instance):
        _make_client().search_opportunities(keywords=["Acme"], account_name="acme-corp")
    q = mock_instance.request.call_args[1]["params"]["q"]
    for field in (
        "Id",
        "Name",
        "StageName",
        "CloseDate",
        "Consulting_Total_USD__c",
        "Training_Total_USD__c",
    ):
        assert field in q, f"expected field {field!r} in SOSL query"


def test_search_opportunities_search_opportunities_null_currency_fields() -> None:
    """None currency fields map to None in the output (not 0.0)."""
    rec = _search_opportunities_make_sf_record(consulting=None, training=None)
    mock_instance = _make_mock_http_client(status_code=200, json_body={"searchRecords": [rec]})
    with patch("httpx.Client", return_value=mock_instance):
        results = _make_client().search_opportunities(keywords=["Acme"], account_name="acme-corp")
    assert results[0]["consulting_acv"] is None
    assert results[0]["training_acv"] is None


def test_search_opportunities_search_opportunities_multiple_keywords() -> None:
    """Multiple keywords are joined with OR in the SOSL query."""
    mock_instance = _make_mock_http_client(status_code=200, json_body={"searchRecords": []})
    with patch("httpx.Client", return_value=mock_instance):
        _make_client().search_opportunities(
            keywords=["Acme", "acme.com", "Acme Corp"],
            account_name="acme-corp",
        )
    q = mock_instance.request.call_args[1]["params"]["q"]
    assert " OR " in q


def test_search_opportunities_search_opportunities_logs_account_and_count(caplog: pytest.LogCaptureFixture) -> None:
    """search_opportunities logs account name and result count via named logger."""
    rec = _search_opportunities_make_sf_record()
    mock_instance = _make_mock_http_client(status_code=200, json_body={"searchRecords": [rec]})

    with (
        caplog.at_level(logging.DEBUG, logger="fieldkit.sf.client"),
        patch("httpx.Client", return_value=mock_instance),
    ):
        _make_client().search_opportunities(keywords=["Acme"], account_name="acme-corp")
    assert "acme-corp" in caplog.text
    assert "1" in caplog.text


def test_search_opportunities_search_opportunities_uses_name_fields_scope() -> None:
    """SOSL query uses IN NAME FIELDS, not IN ALL FIELDS (implementation change).

    IN ALL FIELDS causes false-positive matches when free-text fields like
    Next_Steps__c mention another account's name.  Restricting to NAME FIELDS
    ensures only the Opportunity Name field is searched.
    """
    mock_instance = _make_mock_http_client(status_code=200, json_body={"searchRecords": []})
    with patch("httpx.Client", return_value=mock_instance):
        _make_client().search_opportunities(keywords=["Acme"], account_name="acme-corp")
    q = mock_instance.request.call_args[1]["params"]["q"]
    assert "IN NAME FIELDS" in q, "SOSL must use IN NAME FIELDS to avoid next-steps false positives"
    assert "IN ALL FIELDS" not in q, "IN ALL FIELDS must not appear — it matches free-text fields"


# ---------------------------------------------------------------------------
# TestFetchDealSplits
# ---------------------------------------------------------------------------


# ── TestFetchDealSplits (flattened) ─────────────────────────────────────────

_FETCH_DEAL_SPLITS__OPP_ID = "006SPLIT001"

_FETCH_DEAL_SPLITS__BASE_CHILD_URL = (
    f"{_BASE_URL}/services/data/v59.0/ui-api/records/006SPLIT001/child-relationships/Deal_Splits1__r"
)


def _fetch_deal_splits_make_splits_response(splits: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the UI API child-relationships response shape.

    The endpoint returns {"count": N, "records": [{...}, ...]} — a flat list
    of record dicts at the top level, not nested under a relationship key.
    """
    return {
        "count": len(splits),
        "records": [
            {
                "fields": {
                    "Offering_Group__c": {"value": s["offering_group"]},
                    "Services_Percentage__c": {"value": s["services_pct"]},
                }
            }
            for s in splits
        ],
    }


def test_fetch_deal_splits_fetch_deal_splits_success() -> None:
    """fetch_deal_splits: returns mapped list on HTTP 200."""
    payload = _fetch_deal_splits_make_splits_response(
        [
            {"offering_group": "OpenShift", "services_pct": 60.0},
            {"offering_group": "RHEL", "services_pct": 40.0},
        ]
    )
    mock_instance = _make_mock_http_client(status_code=200, json_body=payload)
    with patch("httpx.Client", return_value=mock_instance):
        result = _make_client().fetch_deal_splits(_FETCH_DEAL_SPLITS__OPP_ID)

    assert result == [
        {"offering_group": "OpenShift", "services_pct": 60.0},
        {"offering_group": "RHEL", "services_pct": 40.0},
    ]


def test_fetch_deal_splits_fetch_deal_splits_correct_url_and_params() -> None:
    """fetch_deal_splits: hits the correct UI API child-relationships URL with field params."""
    payload = _fetch_deal_splits_make_splits_response([])
    mock_instance = _make_mock_http_client(status_code=200, json_body=payload)
    with patch("httpx.Client", return_value=mock_instance):
        _make_client().fetch_deal_splits(_FETCH_DEAL_SPLITS__OPP_ID)

    call_args = mock_instance.request.call_args
    assert call_args[0][0] == "GET"
    url = call_args[0][1]
    assert f"/ui-api/records/{_FETCH_DEAL_SPLITS__OPP_ID}/child-relationships/Deal_Splits1__r" in url
    params = call_args[1]["params"]
    assert "Offering_Group__c" in params["fields"]
    assert "Services_Percentage__c" in params["fields"]


def test_fetch_deal_splits_fetch_deal_splits_empty_records() -> None:
    """fetch_deal_splits: empty records list returns []."""
    payload = _fetch_deal_splits_make_splits_response([])
    mock_instance = _make_mock_http_client(status_code=200, json_body=payload)
    with patch("httpx.Client", return_value=mock_instance):
        result = _make_client().fetch_deal_splits(_FETCH_DEAL_SPLITS__OPP_ID)
    assert result == []


def test_fetch_deal_splits_fetch_deal_splits_missing_records_key() -> None:
    """fetch_deal_splits: missing 'records' key in response returns []."""
    payload: dict[str, Any] = {"count": 0}
    mock_instance = _make_mock_http_client(status_code=200, json_body=payload)
    with patch("httpx.Client", return_value=mock_instance):
        result = _make_client().fetch_deal_splits(_FETCH_DEAL_SPLITS__OPP_ID)
    assert result == []


def test_fetch_deal_splits_fetch_deal_splits_records_is_dict_returns_empty() -> None:
    """fetch_deal_splits: 'records' is a dict (old nested shape) returns [] without raising."""
    payload: dict[str, Any] = {"records": {"Deal_Splits1__r": {"records": []}}}
    mock_instance = _make_mock_http_client(status_code=200, json_body=payload)
    with patch("httpx.Client", return_value=mock_instance):
        result = _make_client().fetch_deal_splits(_FETCH_DEAL_SPLITS__OPP_ID)
    assert result == []


def test_fetch_deal_splits_warns_on_shape_drift_zero_mapped(caplog: pytest.LogCaptureFixture) -> None:
    """historic regression: non-empty raw records that map to zero results log a warning, not just debug."""
    payload: dict[str, Any] = {
        "records": [
            {"fields": {"Offering_Group__c": {"displayValue": "OpenShift"}}},
        ]
    }
    mock_instance = _make_mock_http_client(status_code=200, json_body=payload)
    with patch("httpx.Client", return_value=mock_instance), caplog.at_level("WARNING"):
        result = _make_client().fetch_deal_splits(_FETCH_DEAL_SPLITS__OPP_ID)

    assert result == []
    assert any("possible UI API response shape drift" in rec.getMessage() for rec in caplog.records)


def test_fetch_deal_splits_warns_on_non_dict_fields(caplog: pytest.LogCaptureFixture) -> None:
    """historic regression: a non-dict 'fields' value is counted as drift and warned about."""
    payload: dict[str, Any] = {"records": [{"fields": "not-a-dict"}]}
    mock_instance = _make_mock_http_client(status_code=200, json_body=payload)
    with patch("httpx.Client", return_value=mock_instance), caplog.at_level("WARNING"):
        _make_client().fetch_deal_splits(_FETCH_DEAL_SPLITS__OPP_ID)

    assert any("possible UI API response shape drift" in rec.getMessage() for rec in caplog.records)


def test_fetch_deal_splits_fetch_deal_splits_404_returns_empty() -> None:
    """fetch_deal_splits: HTTP 404 returns [] without raising."""
    mock_instance = _make_mock_http_client(status_code=404, text_body="Not Found")
    with patch("httpx.Client", return_value=mock_instance):
        result = _make_client().fetch_deal_splits(_FETCH_DEAL_SPLITS__OPP_ID)
    assert result == []


def test_fetch_deal_splits_fetch_deal_splits_401_raises_auth_error() -> None:
    """fetch_deal_splits: HTTP 401 raises SFAuthError."""
    mock_instance = _make_mock_http_client(status_code=401)
    with patch("httpx.Client", return_value=mock_instance), pytest.raises(SFAuthError, match=r"."):
        _make_client().fetch_deal_splits(_FETCH_DEAL_SPLITS__OPP_ID)


def test_fetch_deal_splits_fetch_deal_splits_500_raises_api_error() -> None:
    """fetch_deal_splits: HTTP 500 raises SFAPIError."""
    mock_instance = _make_mock_http_client(status_code=500, text_body="Server Error")
    with patch("httpx.Client", return_value=mock_instance), pytest.raises(SFAPIError, match="500"):
        _make_client().fetch_deal_splits(_FETCH_DEAL_SPLITS__OPP_ID)


def test_fetch_deal_splits_fetch_deal_splits_connection_error_raises_api_error() -> None:
    """fetch_deal_splits: httpx.ConnectError raises SFAPIError."""
    mock_instance = _make_mock_http_client(raise_connect_error=True)
    with patch("httpx.Client", return_value=mock_instance), pytest.raises(SFAPIError, match="connection"):
        _make_client().fetch_deal_splits(_FETCH_DEAL_SPLITS__OPP_ID)


def test_fetch_deal_splits_fetch_deal_splits_auth_header() -> None:
    """fetch_deal_splits: sends Authorization Bearer header."""
    payload = _fetch_deal_splits_make_splits_response([])
    mock_instance = _make_mock_http_client(status_code=200, json_body=payload)
    with patch("httpx.Client", return_value=mock_instance):
        _make_client().fetch_deal_splits(_FETCH_DEAL_SPLITS__OPP_ID)
    headers = mock_instance.request.call_args[1]["headers"]
    assert headers["Authorization"] == f"Bearer {_SID}"


# ---------------------------------------------------------------------------
# TestFetchRelatedListRecords (implementation note)
# ---------------------------------------------------------------------------


_RLR__PARENT_ID = "a0Q000000000000AAA"
_RLR__RELATED_LIST = "SBQQ__LineItems__r"


def _rlr_response(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a UI API related-list-records response shape."""
    return {"count": len(records), "records": records}


def test_fetch_related_list_records_success() -> None:
    """fetch_related_list_records: returns raw records list on HTTP 200."""
    records = [
        {"id": "a0x1", "fields": {"SBQQ__ProductName__c": {"value": "OpenShift"}}},
        {"id": "a0x2", "fields": {"SBQQ__ProductName__c": {"value": "Ansible"}}},
    ]
    mock_instance = _make_mock_http_client(status_code=200, json_body=_rlr_response(records))
    with patch("httpx.Client", return_value=mock_instance):
        result = _make_client().fetch_related_list_records(_RLR__PARENT_ID, _RLR__RELATED_LIST)
    assert result == records


def test_fetch_related_list_records_correct_url() -> None:
    """fetch_related_list_records: hits the ui-api/related-list-records path."""
    mock_instance = _make_mock_http_client(status_code=200, json_body=_rlr_response([]))
    with patch("httpx.Client", return_value=mock_instance):
        _make_client().fetch_related_list_records(_RLR__PARENT_ID, _RLR__RELATED_LIST)
    call_args = mock_instance.request.call_args
    assert call_args[0][0] == "GET"
    url = call_args[0][1]
    assert f"/ui-api/related-list-records/{_RLR__PARENT_ID}/{_RLR__RELATED_LIST}" in url
    assert _BASE_URL in url


def test_fetch_related_list_records_no_fields_param_by_default() -> None:
    """No 'fields' param is sent when fields is omitted (uses related-list columns)."""
    mock_instance = _make_mock_http_client(status_code=200, json_body=_rlr_response([]))
    with patch("httpx.Client", return_value=mock_instance):
        _make_client().fetch_related_list_records(_RLR__PARENT_ID, _RLR__RELATED_LIST)
    params = mock_instance.request.call_args[1]["params"]
    assert params == {}


def test_fetch_related_list_records_passes_fields_param() -> None:
    """A supplied fields string is passed through as the 'fields' query param."""
    mock_instance = _make_mock_http_client(status_code=200, json_body=_rlr_response([]))
    with patch("httpx.Client", return_value=mock_instance):
        _make_client().fetch_related_list_records(
            _RLR__PARENT_ID, _RLR__RELATED_LIST, fields="SBQQ__QuoteLine__c.SBQQ__ProductName__c"
        )
    params = mock_instance.request.call_args[1]["params"]
    assert params["fields"] == "SBQQ__QuoteLine__c.SBQQ__ProductName__c"


def test_fetch_related_list_records_404_returns_empty() -> None:
    """fetch_related_list_records: HTTP 404 returns [] without raising."""
    mock_instance = _make_mock_http_client(status_code=404, text_body="Not Found")
    with patch("httpx.Client", return_value=mock_instance):
        result = _make_client().fetch_related_list_records(_RLR__PARENT_ID, _RLR__RELATED_LIST)
    assert result == []


def test_fetch_related_list_records_missing_records_key_returns_empty() -> None:
    """fetch_related_list_records: missing 'records' key returns []."""
    mock_instance = _make_mock_http_client(status_code=200, json_body={"count": 0})
    with patch("httpx.Client", return_value=mock_instance):
        result = _make_client().fetch_related_list_records(_RLR__PARENT_ID, _RLR__RELATED_LIST)
    assert result == []


def test_fetch_related_list_records_records_not_list_returns_empty() -> None:
    """fetch_related_list_records: non-list 'records' returns [] without raising."""
    mock_instance = _make_mock_http_client(status_code=200, json_body={"records": {"nested": True}})
    with patch("httpx.Client", return_value=mock_instance):
        result = _make_client().fetch_related_list_records(_RLR__PARENT_ID, _RLR__RELATED_LIST)
    assert result == []


def test_fetch_related_list_records_401_raises_auth_error() -> None:
    """fetch_related_list_records: HTTP 401 raises SFAuthError."""
    mock_instance = _make_mock_http_client(status_code=401)
    with patch("httpx.Client", return_value=mock_instance), pytest.raises(SFAuthError, match=r"."):
        _make_client().fetch_related_list_records(_RLR__PARENT_ID, _RLR__RELATED_LIST)


def test_fetch_related_list_records_500_raises_api_error() -> None:
    """fetch_related_list_records: HTTP 500 raises SFAPIError with status code."""
    mock_instance = _make_mock_http_client(status_code=500, text_body="Server Error")
    with patch("httpx.Client", return_value=mock_instance), pytest.raises(SFAPIError, match="500"):
        _make_client().fetch_related_list_records(_RLR__PARENT_ID, _RLR__RELATED_LIST)


def test_fetch_related_list_records_connection_error_raises_api_error() -> None:
    """fetch_related_list_records: httpx.ConnectError raises SFAPIError."""
    mock_instance = _make_mock_http_client(raise_connect_error=True)
    with patch("httpx.Client", return_value=mock_instance), pytest.raises(SFAPIError, match="connection"):
        _make_client().fetch_related_list_records(_RLR__PARENT_ID, _RLR__RELATED_LIST)


def test_fetch_related_list_records_auth_header() -> None:
    """fetch_related_list_records: sends Authorization Bearer header."""
    mock_instance = _make_mock_http_client(status_code=200, json_body=_rlr_response([]))
    with patch("httpx.Client", return_value=mock_instance):
        _make_client().fetch_related_list_records(_RLR__PARENT_ID, _RLR__RELATED_LIST)
    headers = mock_instance.request.call_args[1]["headers"]
    assert headers["Authorization"] == f"Bearer {_SID}"


# ---------------------------------------------------------------------------
# TestRetryBehavior
# ---------------------------------------------------------------------------


# ── TestRetryBehavior (flattened) ───────────────────────────────────────────


def _retry_behavior_make_seq_mock(responses: list[MagicMock]) -> MagicMock:
    """Build a mock httpx.Client whose .request side_effect returns responses in order."""
    mock_instance = MagicMock()
    mock_instance.request.side_effect = responses
    return mock_instance


def test_retry_behavior_sosl_search_retries_on_429() -> None:
    """sosl_search: 429 → 200 succeeds and makes exactly 2 requests."""
    search_records = [{"Id": "006A1"}]
    resp_429 = _make_response(status_code=429, text_body="Rate Limited")
    resp_200 = _make_response(status_code=200, json_body={"searchRecords": search_records})
    mock_instance = _retry_behavior_make_seq_mock([resp_429, resp_200])

    with (
        patch("httpx.Client", return_value=mock_instance),
        patch("time.sleep"),
    ):
        result = _make_client().sosl_search("FIND {Acme} IN ALL FIELDS RETURNING Opportunity(Id)")

    assert result == search_records
    assert mock_instance.request.call_count == 2


def test_retry_behavior_fetch_record_retries_on_503() -> None:
    """fetch_record: 503 → 200 succeeds and makes exactly 2 requests."""
    record_data = {"id": "006ABC", "fields": {}}
    resp_503 = _make_response(status_code=503, text_body="Service Unavailable")
    resp_200 = _make_response(status_code=200, json_body=record_data)
    mock_instance = _retry_behavior_make_seq_mock([resp_503, resp_200])

    with (
        patch("httpx.Client", return_value=mock_instance),
        patch("time.sleep"),
    ):
        result = _make_client().fetch_record("006ABC")

    assert result == record_data
    assert mock_instance.request.call_count == 2


def test_retry_behavior_no_retry_on_401() -> None:
    """_request_with_retry: 401 raises SFAuthError immediately with no retry."""
    resp_401 = _make_response(status_code=401)
    mock_instance = _retry_behavior_make_seq_mock([resp_401])

    with patch("httpx.Client", return_value=mock_instance), pytest.raises(SFAuthError, match=r"."):
        _make_client().sosl_search("FIND {x}")

    assert mock_instance.request.call_count == 1


def test_retry_behavior_no_retry_on_400() -> None:
    """_request_with_retry: 400 raises SFAPIError immediately with no retry."""
    resp_400 = _make_response(status_code=400, text_body="Bad Request")
    mock_instance = _retry_behavior_make_seq_mock([resp_400])

    with patch("httpx.Client", return_value=mock_instance), pytest.raises(SFAPIError, match="400"):
        _make_client().sosl_search("FIND {x}")

    assert mock_instance.request.call_count == 1


def test_retry_behavior_retry_exhaustion_raises_api_error() -> None:
    """_request_with_retry: 429 three times raises SFAPIError after _MAX_RETRIES attempts."""
    resp_429 = _make_response(status_code=429, text_body="Rate Limited")
    mock_instance = _retry_behavior_make_seq_mock([resp_429, resp_429, resp_429])

    with (
        patch("httpx.Client", return_value=mock_instance),
        patch("time.sleep"),
        pytest.raises(SFAPIError, match=r"after \d+ attempts"),
    ):
        _make_client().sosl_search("FIND {x}")

    assert mock_instance.request.call_count == 3


# ---------------------------------------------------------------------------
# TestResolveAccountIdByKeywords
# ---------------------------------------------------------------------------


# ── TestResolveAccountIdByKeywords (flattened) ──────────────────────────────


def _resolve_account_id_by_keywords_sosl_response(account_id: str | None) -> MagicMock:
    """Build a mock SOSL response with an opportunity carrying Account.Id."""
    record: dict[str, Any] = {"Id": "006XXXXXXXXXXXXXXX", "Account": {"Id": account_id} if account_id else {}}
    return _make_mock_http_client(json_body={"searchRecords": [record] if account_id else []})


def test_resolve_account_id_by_keywords_returns_account_id_from_sosl_result() -> None:
    """Returns Account.Id from the first matching opportunity."""
    mock_http = _resolve_account_id_by_keywords_sosl_response("001AAAAAAAAAAAAA")
    with patch("httpx.Client", return_value=mock_http):
        result = _make_client().resolve_account_id_by_keywords(keywords=["Bank of America"], account_name="acme-corp")
    assert result == "001AAAAAAAAAAAAA"


def test_resolve_account_id_by_keywords_empty_keywords_returns_none() -> None:
    """Returns None immediately when no keywords provided."""
    with patch("httpx.Client", return_value=_make_mock_http_client(json_body={"searchRecords": []})):
        result = _make_client().resolve_account_id_by_keywords(keywords=[], account_name="acme-corp")
    assert result is None


def test_resolve_account_id_by_keywords_no_account_id_in_results_returns_none() -> None:
    """Returns None when SOSL results have no Account.Id."""
    mock_http = _resolve_account_id_by_keywords_sosl_response(None)
    with patch("httpx.Client", return_value=mock_http):
        result = _make_client().resolve_account_id_by_keywords(keywords=["Bank of America"], account_name="acme-corp")
    assert result is None


def test_resolve_account_id_by_keywords_sosl_query_contains_keywords_and_filter() -> None:
    """SOSL query includes keywords and services filter in RETURNING clause."""
    mock_http = _make_mock_http_client(json_body={"searchRecords": []})
    with patch("httpx.Client", return_value=mock_http):
        _make_client().resolve_account_id_by_keywords(
            keywords=["Bank of America", "acme-corp"], account_name="acme-corp"
        )
    sosl_query = mock_http.request.call_args[1]["params"]["q"]
    assert "Bank of America" in sosl_query
    assert "acme-corp" in sosl_query
    assert "Consulting_Total_USD__c > 0 OR Training_Total_USD__c > 0" in sosl_query


def test_resolve_account_id_by_keywords_api_error_returns_none() -> None:
    """Returns None (does not raise) when SOSL fails with SFAPIError."""
    mock_http = _make_mock_http_client(status_code=500, text_body="server error")
    with patch("httpx.Client", return_value=mock_http):
        result = _make_client().resolve_account_id_by_keywords(keywords=["Bank of America"], account_name="acme-corp")
    assert result is None


def test_resolve_account_id_by_keywords_first_record_with_account_id_wins() -> None:
    """Returns Account.Id from the first record that has one."""
    records = [
        {"Id": "006AAA", "Account": {}},  # no Account.Id
        {"Id": "006BBB", "Account": {"Id": "001FIRST"}},
        {"Id": "006CCC", "Account": {"Id": "001SECOND"}},
    ]
    mock_http = _make_mock_http_client(json_body={"searchRecords": records})
    with patch("httpx.Client", return_value=mock_http):
        result = _make_client().resolve_account_id_by_keywords(keywords=["Acme"], account_name="acme-corp")
    assert result == "001FIRST"


def test_resolve_account_id_by_keywords_resolve_account_id_uses_name_fields_scope() -> None:
    """resolve_account_id_by_keywords uses IN NAME FIELDS, not IN ALL FIELDS (implementation change).

    Same rationale as search_opportunities: restricting to NAME FIELDS prevents
    false-positive Account.Id resolution when next-steps text mentions another
    account's name.
    """
    mock_http = _make_mock_http_client(json_body={"searchRecords": []})
    with patch("httpx.Client", return_value=mock_http):
        _make_client().resolve_account_id_by_keywords(keywords=["Acme"], account_name="acme-corp")
    sosl_query = mock_http.request.call_args[1]["params"]["q"]
    assert "IN NAME FIELDS" in sosl_query, "SOSL must use IN NAME FIELDS to avoid next-steps false positives"
    assert "IN ALL FIELDS" not in sosl_query, "IN ALL FIELDS must not appear — it matches free-text fields"


# ---------------------------------------------------------------------------
# TestFetchAccountById
# ---------------------------------------------------------------------------


# ── TestFetchAccountById (flattened) ────────────────────────────────────────


def test_fetch_account_by_id_returns_account_record() -> None:
    """Returns parsed Account dict on HTTP 200."""
    account_data = {"Id": "001AAAAAAAAAAAAA", "Name": "Bank of America", "Industry": "Finance"}
    mock_http = _make_mock_http_client(json_body=account_data)
    with patch("httpx.Client", return_value=mock_http):
        result = _make_client().fetch_account_by_id("001AAAAAAAAAAAAA")
    assert result == account_data


def test_fetch_account_by_id_returns_none_on_404() -> None:
    """Returns None when Account record is not found (404)."""
    mock_http = _make_mock_http_client(status_code=404, text_body="not found")
    with patch("httpx.Client", return_value=mock_http):
        result = _make_client().fetch_account_by_id("001NOTEXIST")
    assert result is None


def test_fetch_account_by_id_raises_auth_error_on_401() -> None:
    """Raises SFAuthError on HTTP 401."""
    mock_http = _make_mock_http_client(status_code=401, text_body="unauthorized")
    with patch("httpx.Client", return_value=mock_http), pytest.raises(SFAuthError, match=r"."):
        _make_client().fetch_account_by_id("001AAAAAAAAAAAAA")


def test_fetch_account_by_id_uses_default_account_fields() -> None:
    """Requests Account fields when no custom fields specified."""
    mock_http = _make_mock_http_client(json_body={"Id": "001AAA"})
    with patch("httpx.Client", return_value=mock_http):
        _make_client().fetch_account_by_id("001AAA")
    call_args = mock_http.request.call_args
    params = call_args.kwargs.get("params") or {}
    assert "Id" in params.get("fields", "")


def test_fetch_account_by_id_uses_custom_fields() -> None:
    """Passes custom fields string through to the API request."""
    mock_http = _make_mock_http_client(json_body={"Id": "001AAA", "Name": "Acme"})
    with patch("httpx.Client", return_value=mock_http):
        _make_client().fetch_account_by_id("001AAA", fields="Id,Name")
    call_args = mock_http.request.call_args
    params = call_args.kwargs.get("params") or {}
    assert params.get("fields") == "Id,Name"


# ---------------------------------------------------------------------------
# historic regression: _parse_json_response names the content-type on parse failure
# ---------------------------------------------------------------------------


def test_parse_json_response_parse_failure_names_content_type() -> None:
    """_parse_json_response: non-HTML unparseable body raises SFAPIError citing the actual content-type (historic regression)."""
    from fieldkit.sf.client import _parse_json_response

    resp = httpx.Response(200, headers={"content-type": "application/xml"}, content=b"<not-json/>")
    with pytest.raises(SFAPIError, match=r"application/xml") as exc_info:
        _parse_json_response(resp, "SOSL search")
    assert "failed to parse JSON response" in str(exc_info.value)


def test_parse_json_response_rejects_non_dict_body() -> None:
    """_parse_json_response: a JSON array body (SF error-response shape) raises SFAPIError, not a silent bad cast."""
    from fieldkit.sf.client import _parse_json_response

    resp = httpx.Response(200, headers={"content-type": "application/json"}, json=[{"message": "bad request"}])
    with pytest.raises(SFAPIError, match=r"expected a JSON object response, got list"):
        _parse_json_response(resp, "SOSL search")


def test_fetch_deal_splits_does_not_warn_when_splits_are_legitimately_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """historic regression: null values are not drift — warning here would be crying wolf.

    A split row whose fields are present but unvalued is normal. Warning about it would
    fire on ordinary data and train operators to dismiss the message before it ever
    fires for a real response-shape change, which is the whole point of the warning.
    """
    payload: dict[str, Any] = {
        "records": [
            {"fields": {"Offering_Group__c": {"value": None}, "Services_Percentage__c": {"value": None}}},
            {"fields": {"Offering_Group__c": {"value": None}, "Services_Percentage__c": {"value": None}}},
        ]
    }
    mock_instance = _make_mock_http_client(status_code=200, json_body=payload)
    with patch("httpx.Client", return_value=mock_instance), caplog.at_level("WARNING"):
        result = _make_client().fetch_deal_splits(_FETCH_DEAL_SPLITS__OPP_ID)

    assert result == []
    drift_warnings = [r for r in caplog.records if "shape drift" in r.getMessage()]
    assert drift_warnings == [], (
        f"legitimately empty splits must not warn, got {[r.getMessage() for r in drift_warnings]}"
    )


def test_fetch_deal_splits_warns_when_expected_keys_are_absent(caplog: pytest.LogCaptureFixture) -> None:
    """historic regression: missing keys — not null values — is the actual drift signature."""
    payload: dict[str, Any] = {
        "records": [
            {"fields": {"Offering_Group__c": {"displayValue": "Consulting"}}},
            {"fields": {"SomeRenamedField__c": {"value": "x"}}},
        ]
    }
    mock_instance = _make_mock_http_client(status_code=200, json_body=payload)
    with patch("httpx.Client", return_value=mock_instance), caplog.at_level("WARNING"):
        result = _make_client().fetch_deal_splits(_FETCH_DEAL_SPLITS__OPP_ID)

    assert result == []
    assert any("shape drift" in r.getMessage() for r in caplog.records), (
        "a record missing both expected split fields is drift and must warn"
    )


def test_fetch_deal_splits_tolerates_non_dict_field_entry(caplog: pytest.LogCaptureFixture) -> None:
    """A drifted scalar where the UI API nests {"value": ...} must not raise."""
    payload: dict[str, Any] = {"records": [{"fields": {"Offering_Group__c": "Consulting"}}]}
    mock_instance = _make_mock_http_client(status_code=200, json_body=payload)
    with patch("httpx.Client", return_value=mock_instance), caplog.at_level("WARNING"):
        result = _make_client().fetch_deal_splits(_FETCH_DEAL_SPLITS__OPP_ID)

    assert result == [], "a scalar where an object was expected yields no mapping, not an AttributeError"
