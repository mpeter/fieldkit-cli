"""Unit tests for ClosePlan UI API collection reads.

Uses unittest.mock.patch on httpx.Client (NOT respx — not in deps).
Pattern mirrors test_sf_direct.py: patch 'httpx.Client' constructor so
SFDirectClient.__init__ picks up the mock instance.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from fieldkit.sf.client import SFAPIError, SFAuthError, SFDirectClient

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Shared constants and helpers
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
) -> MagicMock:
    """Build a mock httpx.Client instance for patching ``httpx.Client`` as a constructor.

    SFDirectClient calls ``self._client.request(method, url, **kwargs)`` via
    ``_request_with_retry``.  Patch with:
        ``with patch("httpx.Client", return_value=mock_instance): ...``
    """
    mock_instance = MagicMock()
    resp = _make_response(status_code=status_code, json_body=json_body, text_body=text_body)
    mock_instance.request.return_value = resp
    return mock_instance


# ---------------------------------------------------------------------------
# TestFetchClosePlanDeal
# ---------------------------------------------------------------------------


_CLOSEPLAN_DEAL__OPP_ID = "006CLOSEPLAN01"


def _closeplan_deal_response(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the UI API child-relationships response shape for TSPC__Deals__r."""
    return {"count": len(records), "records": records}


def test_fetch_closeplan_deals_returns_every_record_with_complete_count_evidence() -> None:
    """Every linked deal remains visible; transport order never selects a deal."""
    records = [
        {"id": "a1Z000000000001AAA", "fields": {"Name": {"value": "Deal One"}}},
        {"id": "a1Z000000000002AAA", "fields": {"Name": {"value": "Deal Two"}}},
    ]
    mock_instance = _make_mock_http_client(status_code=200, json_body=_closeplan_deal_response(records))
    with patch("httpx.Client", return_value=mock_instance):
        result = _make_client().fetch_closeplan_deals(_CLOSEPLAN_DEAL__OPP_ID)
    assert result.records == records
    assert result.reported_count == 2
    assert result.complete is True
    assert result.issues == []


def test_fetch_closeplan_deals_correct_url_and_params() -> None:
    """fetch_closeplan_deal: hits the TSPC__Deals__r child-relationships endpoint with deal fields."""
    mock_instance = _make_mock_http_client(status_code=200, json_body=_closeplan_deal_response([]))
    with patch("httpx.Client", return_value=mock_instance):
        _make_client().fetch_closeplan_deals(_CLOSEPLAN_DEAL__OPP_ID)

    call_args = mock_instance.request.call_args
    assert call_args[0][0] == "GET"
    url = call_args[0][1]
    assert f"/ui-api/records/{_CLOSEPLAN_DEAL__OPP_ID}/child-relationships/TSPC__Deals__r" in url
    params = call_args[1]["params"]
    assert "TSPC__Deal__c.Id" in params["fields"]
    assert "TSPC__Deal__c.TSPC__ScorecardScoreRatio__c" in params["fields"]
    assert "TSPC__Deal__c.TSPC__ScorecardTotalScore__c" in params["fields"]
    assert "TSPC__Deal__c.TSPC__Template__c" in params["fields"]
    assert "TSPC__Deal__c.TSPC__TemplateDeployDate__c" in params["fields"]
    assert "TSPC__Deal__c.LastModifiedDate" in params["fields"]
    assert params["pageSize"] == "100"


def test_fetch_closeplan_deals_marks_count_mismatch_incomplete() -> None:
    """A reported count mismatch must remain visible as incomplete evidence."""
    records = [{"id": "a1Z000000000001AAA", "fields": {"Name": {"value": "Deal One"}}}]
    payload = {"count": 2, "records": records}
    mock_instance = _make_mock_http_client(status_code=200, json_body=payload)
    with patch("httpx.Client", return_value=mock_instance):
        result = _make_client().fetch_closeplan_deals(_CLOSEPLAN_DEAL__OPP_ID)

    assert result.records == records
    assert result.complete is False
    assert result.issues == ["reported count 2 does not match 1 returned record(s)"]


def test_fetch_closeplan_deals_marks_missing_count_incomplete() -> None:
    """A response without a count cannot prove that the collection is complete."""
    records = [{"id": "a1Z000000000001AAA", "fields": {"Name": {"value": "Deal One"}}}]
    mock_instance = _make_mock_http_client(status_code=200, json_body={"records": records})
    with patch("httpx.Client", return_value=mock_instance):
        result = _make_client().fetch_closeplan_deals(_CLOSEPLAN_DEAL__OPP_ID)

    assert result.records == records
    assert result.reported_count is None
    assert result.complete is False
    assert result.issues == ["reported count is missing or invalid"]


def test_fetch_closeplan_deals_404_returns_complete_empty_collection() -> None:
    """A missing relationship is represented as a complete empty collection."""
    mock_instance = _make_mock_http_client(status_code=404, text_body="Not Found")
    with patch("httpx.Client", return_value=mock_instance):
        result = _make_client().fetch_closeplan_deals(_CLOSEPLAN_DEAL__OPP_ID)
    assert result.records == []
    assert result.complete is True
    assert mock_instance.request.call_count == 1


def test_fetch_closeplan_deal_400_raises_api_error() -> None:
    """fetch_closeplan_deal: a non-retryable, non-404 HTTP status raises SFAPIError citing the status code."""
    mock_instance = _make_mock_http_client(status_code=400, text_body="Bad Request")
    with patch("httpx.Client", return_value=mock_instance), pytest.raises(SFAPIError, match="400"):
        _make_client().fetch_closeplan_deals(_CLOSEPLAN_DEAL__OPP_ID)


def test_fetch_closeplan_deal_401_raises_auth_error() -> None:
    """fetch_closeplan_deal: HTTP 401 propagates as SFAuthError, not swallowed."""
    mock_instance = _make_mock_http_client(status_code=401)
    with patch("httpx.Client", return_value=mock_instance), pytest.raises(SFAuthError, match=r"."):
        _make_client().fetch_closeplan_deals(_CLOSEPLAN_DEAL__OPP_ID)


def test_fetch_closeplan_deal_connection_error_raises_api_error() -> None:
    """fetch_closeplan_deal: httpx.ConnectError raises SFAPIError with the connection-failure prefix.

    Uses a non-matching underlying exception message so the assertion pins our
    wrapper text, not a coincidental substring of the wrapped exception.
    """
    mock_instance = _make_mock_http_client()
    mock_instance.request.side_effect = httpx.ConnectError("refused")
    with patch("httpx.Client", return_value=mock_instance), pytest.raises(SFAPIError, match="SF connection failed"):
        _make_client().fetch_closeplan_deals(_CLOSEPLAN_DEAL__OPP_ID)


def test_fetch_closeplan_deal_request_error_raises_api_error() -> None:
    """fetch_closeplan_deal: a non-connect httpx.RequestError raises SFAPIError with the request-error prefix.

    Uses a non-matching underlying exception message so the assertion pins our
    wrapper text, not a coincidental substring of the wrapped exception.
    """
    mock_instance = _make_mock_http_client()
    mock_instance.request.side_effect = httpx.RequestError("kaboom")
    with patch("httpx.Client", return_value=mock_instance), pytest.raises(SFAPIError, match="SF request error"):
        _make_client().fetch_closeplan_deals(_CLOSEPLAN_DEAL__OPP_ID)


def test_fetch_closeplan_deals_missing_records_key_is_incomplete() -> None:
    """A response without a records key cannot prove collection completeness."""
    payload: dict[str, Any] = {"count": 0}
    mock_instance = _make_mock_http_client(status_code=200, json_body=payload)
    with patch("httpx.Client", return_value=mock_instance):
        result = _make_client().fetch_closeplan_deals(_CLOSEPLAN_DEAL__OPP_ID)
    assert result.records == []
    assert result.complete is False


def test_fetch_closeplan_deals_records_is_dict_is_incomplete() -> None:
    """A non-list records value cannot prove collection completeness."""
    payload: dict[str, Any] = {"records": {"TSPC__Deals__r": {"records": []}}}
    mock_instance = _make_mock_http_client(status_code=200, json_body=payload)
    with patch("httpx.Client", return_value=mock_instance):
        result = _make_client().fetch_closeplan_deals(_CLOSEPLAN_DEAL__OPP_ID)
    assert result.records == []
    assert result.complete is False


def test_fetch_closeplan_deals_drops_non_object_members_and_marks_incomplete() -> None:
    valid = {"id": "a1Z000000000001AAA", "fields": {"Name": {"value": "Deal One"}}}
    payload: dict[str, Any] = {"count": 2, "records": [valid, None]}
    mock_instance = _make_mock_http_client(status_code=200, json_body=payload)
    with patch("httpx.Client", return_value=mock_instance):
        result = _make_client().fetch_closeplan_deals(_CLOSEPLAN_DEAL__OPP_ID)

    assert result.records == [valid]
    assert result.reported_count == 2
    assert result.complete is False
    assert result.issues == ["records collection contains 1 non-object member(s)"]


# ---------------------------------------------------------------------------
# TestFetchClosePlanAnswers
# ---------------------------------------------------------------------------


_CLOSEPLAN_ANSWERS__DEAL_ID = "a1ZANSWERS0001AA"


def _closeplan_answers_response(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the UI API child-relationships response shape for TSPC__DealQuestions__r."""
    return {"count": len(records), "records": records}


def test_fetch_closeplan_questions_returns_all_records_with_complete_count_evidence() -> None:
    """fetch_closeplan_answers: returns the full answers list, in order, not just one record."""
    records = [
        {"id": "a2Y000000000001AAA", "fields": {"Name": {"value": "METRICS - Quantifiable measurements"}}},
        {"id": "a2Y000000000002AAA", "fields": {"Name": {"value": "ECONOMIC BUYER - Individual within..."}}},
    ]
    mock_instance = _make_mock_http_client(status_code=200, json_body=_closeplan_answers_response(records))
    with patch("httpx.Client", return_value=mock_instance):
        result = _make_client().fetch_closeplan_questions(_CLOSEPLAN_ANSWERS__DEAL_ID)
    assert result.records == records
    assert result.reported_count == 2
    assert result.complete is True


def test_fetch_closeplan_questions_correct_url_and_params() -> None:
    """fetch_closeplan_answers: hits the TSPC__DealQuestions__r child-relationships endpoint (historic regression).

    The supported schema uses TSPC__DealQuestions__r / TSPC__DealQuestion__c,
    as returned by GET /ui-api/object-info/TSPC__Deal__c.
    """
    mock_instance = _make_mock_http_client(status_code=200, json_body=_closeplan_answers_response([]))
    with patch("httpx.Client", return_value=mock_instance):
        _make_client().fetch_closeplan_questions(_CLOSEPLAN_ANSWERS__DEAL_ID)

    call_args = mock_instance.request.call_args
    assert call_args[0][0] == "GET"
    url = call_args[0][1]
    assert f"/ui-api/records/{_CLOSEPLAN_ANSWERS__DEAL_ID}/child-relationships/TSPC__DealQuestions__r" in url
    params = call_args[1]["params"]
    assert "TSPC__DealQuestion__c.TSPC__Score__c" in params["fields"]
    assert "TSPC__DealQuestion__c.TSPC__TextAnswer__c" in params["fields"]
    assert "TSPC__DealQuestion__c.TSPC__RichTextAnswer__c" in params["fields"]
    assert "TSPC__DealQuestion__c.TSPC__Answer__c" in params["fields"]
    assert "TSPC__DealQuestion__c.TSPC__MaxScore__c" in params["fields"]
    assert "TSPC__DealQuestion__c.TSPC__HasTextAnswer__c" in params["fields"]
    assert "TSPC__DealQuestion__c.TSPC__TemplateQuestion__c" in params["fields"]
    assert "TSPC__DealQuestion__c.LastModifiedDate" in params["fields"]
    assert params["pageSize"] == "100"


def test_fetch_closeplan_questions_marks_count_mismatch_incomplete() -> None:
    records = [{"id": "a2Y000000000001AAA", "fields": {"Name": {"value": "METRICS - Question"}}}]
    payload = {"count": 2, "records": records}
    mock_instance = _make_mock_http_client(status_code=200, json_body=payload)
    with patch("httpx.Client", return_value=mock_instance):
        result = _make_client().fetch_closeplan_questions(_CLOSEPLAN_ANSWERS__DEAL_ID)

    assert result.records == records
    assert result.complete is False


def test_fetch_closeplan_questions_404_returns_complete_empty_collection() -> None:
    """A missing question relationship is represented as a complete empty collection."""
    mock_instance = _make_mock_http_client(status_code=404, text_body="Not Found")
    with patch("httpx.Client", return_value=mock_instance):
        result = _make_client().fetch_closeplan_questions(_CLOSEPLAN_ANSWERS__DEAL_ID)
    assert result.records == []
    assert result.complete is True
    assert mock_instance.request.call_count == 1


def test_fetch_closeplan_answers_400_raises_api_error() -> None:
    """fetch_closeplan_answers: a non-retryable, non-404 HTTP status raises SFAPIError citing the status code."""
    mock_instance = _make_mock_http_client(status_code=400, text_body="Bad Request")
    with patch("httpx.Client", return_value=mock_instance), pytest.raises(SFAPIError, match="400"):
        _make_client().fetch_closeplan_questions(_CLOSEPLAN_ANSWERS__DEAL_ID)


def test_fetch_closeplan_answers_401_raises_auth_error() -> None:
    """fetch_closeplan_answers: HTTP 401 propagates as SFAuthError, not swallowed."""
    mock_instance = _make_mock_http_client(status_code=401)
    with patch("httpx.Client", return_value=mock_instance), pytest.raises(SFAuthError, match=r"."):
        _make_client().fetch_closeplan_questions(_CLOSEPLAN_ANSWERS__DEAL_ID)


def test_fetch_closeplan_answers_connection_error_raises_api_error() -> None:
    """fetch_closeplan_answers: httpx.ConnectError raises SFAPIError with the connection-failure prefix.

    Uses a non-matching underlying exception message so the assertion pins our
    wrapper text, not a coincidental substring of the wrapped exception.
    """
    mock_instance = _make_mock_http_client()
    mock_instance.request.side_effect = httpx.ConnectError("refused")
    with patch("httpx.Client", return_value=mock_instance), pytest.raises(SFAPIError, match="SF connection failed"):
        _make_client().fetch_closeplan_questions(_CLOSEPLAN_ANSWERS__DEAL_ID)


def test_fetch_closeplan_answers_request_error_raises_api_error() -> None:
    """fetch_closeplan_answers: a non-connect httpx.RequestError raises SFAPIError with the request-error prefix.

    Uses a non-matching underlying exception message so the assertion pins our
    wrapper text, not a coincidental substring of the wrapped exception.
    """
    mock_instance = _make_mock_http_client()
    mock_instance.request.side_effect = httpx.RequestError("kaboom")
    with patch("httpx.Client", return_value=mock_instance), pytest.raises(SFAPIError, match="SF request error"):
        _make_client().fetch_closeplan_questions(_CLOSEPLAN_ANSWERS__DEAL_ID)


def test_fetch_closeplan_questions_missing_records_key_is_incomplete() -> None:
    """A question response without records cannot prove completeness."""
    payload: dict[str, Any] = {"count": 0}
    mock_instance = _make_mock_http_client(status_code=200, json_body=payload)
    with patch("httpx.Client", return_value=mock_instance):
        result = _make_client().fetch_closeplan_questions(_CLOSEPLAN_ANSWERS__DEAL_ID)
    assert result.records == []
    assert result.complete is False


def test_fetch_closeplan_questions_records_is_dict_is_incomplete() -> None:
    """A non-list question records value cannot prove completeness."""
    payload: dict[str, Any] = {"records": {"TSPC__DealQuestions__r": {"records": []}}}
    mock_instance = _make_mock_http_client(status_code=200, json_body=payload)
    with patch("httpx.Client", return_value=mock_instance):
        result = _make_client().fetch_closeplan_questions(_CLOSEPLAN_ANSWERS__DEAL_ID)
    assert result.records == []
    assert result.complete is False


def test_fetch_closeplan_questions_drops_non_object_members_and_marks_incomplete() -> None:
    valid = {"id": "a2Y000000000001AAA", "fields": {"Name": {"value": "METRICS - Question"}}}
    payload: dict[str, Any] = {"count": 2, "records": [None, valid]}
    mock_instance = _make_mock_http_client(status_code=200, json_body=payload)
    with patch("httpx.Client", return_value=mock_instance):
        result = _make_client().fetch_closeplan_questions(_CLOSEPLAN_ANSWERS__DEAL_ID)

    assert result.records == [valid]
    assert result.reported_count == 2
    assert result.complete is False
    assert result.issues == ["records collection contains 1 non-object member(s)"]


def test_fetch_closeplan_template_question_reads_exact_native_metadata() -> None:
    template_question_id = "a2G000000000001AAA"
    record = {
        "Id": template_question_id,
        "Name": "Template question",
        "TSPC__Template__c": "a2T000000000001AAA",
    }
    mock_instance = _make_mock_http_client(status_code=200, json_body=record)
    with patch("httpx.Client", return_value=mock_instance):
        result = _make_client().fetch_closeplan_template_question(template_question_id)

    assert result == record
    call_args = mock_instance.request.call_args
    assert call_args.args[0] == "GET"
    assert f"/sobjects/TSPC__TemplateQuestion__c/{template_question_id}" in call_args.args[1]
    fields = call_args.kwargs["params"]["fields"]
    assert fields == (
        "Id,Name,TSPC__Template__c,TSPC__Category__c,TSPC__QuestionCategory__c,"
        "TSPC__Mode__c,TSPC__HasTextAnswer__c,TSPC__MaxScore__c,TSPC__Sync_ScoreField__c,"
        "TSPC__Sync_ScoreRatioField__c,TSPC__HasSharedScore__c"
    )


def test_fetch_closeplan_template_reads_exact_version_metadata() -> None:
    template_id = "a2T000000000001AAA"
    record = {
        "Id": template_id,
        "TSPC__Version__c": 3.0,
        "TSPC__VersionName__c": None,
        "TSPC__Type__c": "OCP",
        "TSPC__Status__c": "Active",
        "TSPC__SC_TotalMaxScore__c": 87.0,
    }
    mock_instance = _make_mock_http_client(status_code=200, json_body=record)
    with patch("httpx.Client", return_value=mock_instance):
        result = _make_client().fetch_closeplan_template(template_id)

    assert result == record
    call_args = mock_instance.request.call_args
    assert call_args.args[0] == "GET"
    assert f"/sobjects/TSPC__Template__c/{template_id}" in call_args.args[1]
    assert call_args.kwargs["params"]["fields"] == (
        "Id,TSPC__Version__c,TSPC__VersionName__c,TSPC__Type__c,TSPC__Status__c,TSPC__SC_TotalMaxScore__c"
    )


def test_fetch_closeplan_template_answers_reads_exact_choices_with_complete_count_evidence() -> None:
    template_question_id = "a2G000000000001AAA"
    records = [
        {
            "id": "a2H000000000001AAA",
            "fields": {
                "Id": {"value": "a2H000000000001AAA"},
                "Name": {"value": "Choice one"},
                "TSPC__MaxScore__c": {"value": 2.0},
            },
        }
    ]
    mock_instance = _make_mock_http_client(status_code=200, json_body={"count": 1, "records": records})
    with patch("httpx.Client", return_value=mock_instance):
        result = _make_client().fetch_closeplan_template_answers(template_question_id)

    assert result.records == records
    assert result.reported_count == 1
    assert result.complete is True
    call_args = mock_instance.request.call_args
    assert call_args.args[0] == "GET"
    assert f"/ui-api/records/{template_question_id}/child-relationships/TSPC__Answers__r" in call_args.args[1]
    fields = call_args.kwargs["params"]["fields"]
    assert "TSPC__TemplateQuestionAnswer__c.Id" in fields
    assert "TSPC__TemplateQuestionAnswer__c.Name" in fields
    assert "TSPC__TemplateQuestionAnswer__c.TSPC__Text__c" in fields
    assert "TSPC__TemplateQuestionAnswer__c.TSPC__Attitude__c" in fields
    assert "TSPC__TemplateQuestionAnswer__c.TSPC__HasTextAnswer__c" in fields
    assert "TSPC__TemplateQuestionAnswer__c.TSPC__MaxScore__c" in fields
    assert "TSPC__TemplateQuestionAnswer__c.TSPC__SortOrder__c" in fields
    assert "TSPC__TemplateQuestionAnswer__c.TSPC__Sync_TextAnswerField__c" in fields
    assert "TSPC__TemplateQuestionAnswer__c.TSPC__Sync_ValueFieldPickval__c" in fields
    assert call_args.kwargs["params"]["pageSize"] == "100"


def test_fetch_closeplan_template_answers_marks_count_mismatch_incomplete() -> None:
    template_question_id = "a2G000000000001AAA"
    records = [{"id": "a2H000000000001AAA", "fields": {"Name": {"value": "Choice one"}}}]
    mock_instance = _make_mock_http_client(status_code=200, json_body={"count": 3, "records": records})
    with patch("httpx.Client", return_value=mock_instance):
        result = _make_client().fetch_closeplan_template_answers(template_question_id)

    assert result.reported_count == 3
    assert result.records == records
    assert result.complete is False
    assert result.issues == ["reported count 3 does not match 1 returned record(s)"]


def test_fetch_closeplan_template_answers_404_is_incomplete_not_empty() -> None:
    template_question_id = "a2G000000000001AAA"
    mock_instance = _make_mock_http_client(status_code=404, text_body="Not Found")
    with patch("httpx.Client", return_value=mock_instance):
        result = _make_client().fetch_closeplan_template_answers(template_question_id)

    assert result.records == []
    assert result.reported_count is None
    assert result.complete is False
    assert result.issues == ["template answer relationship was not found"]
