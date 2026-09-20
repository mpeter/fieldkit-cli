"""Tests for fieldkit sf meddpicc -- ClosePlan/TSPC scorecard reader.

Covers:
  - fetch_meddpicc_scorecard: complete, missing, ambiguous, and incomplete reads
  - print_scorecard: output formatting
  - CLI: happy path, no-ClosePlan message, invalid opp_id, auth error propagation
"""

import json
import math
from typing import Any
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from fieldkit.commands.sf.meddpicc import (
    cli,
    fetch_meddpicc_scorecard,
    print_scorecard,
)
from fieldkit.sf.client import SFAuthError
from fieldkit.sf.meddpicc import normalize_questions
from fieldkit.sf.types import MeddpiccDeal, MeddpiccQuestion, MeddpiccReadResult, UIAPIRecordCollection

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_OPP_ID = "006000000000000AAA"
_DEAL_ID = "a1D000000000000AAA"

_DEAL_RECORD_UI_API: dict[str, Any] = {
    "fields": {
        "Id": {"value": _DEAL_ID},
        "Name": {"value": "Test Deal"},
        "TSPC__ScorecardScoreRatio__c": {"value": 0.75},
        "TSPC__ScorecardTotalScore__c": {"value": 42.0},
        "TSPC__Template__c": {"value": "a2T000000000001AAA"},
        "TSPC__TemplateDeployDate__c": {"value": "2026-09-01T00:00:00.000Z"},
        "LastModifiedDate": {"value": "2026-09-12T12:00:00.000Z"},
    }
}

_ANSWER_RECORDS_UI_API: list[dict[str, Any]] = [
    {
        "fields": {
            "Id": {"value": "a1E000000000000AAA"},
            "Name": {"value": "ECONOMIC BUYER - Individual within the customer's organization"},
            "TSPC__Score__c": {"value": 3.0},
            "TSPC__TextAnswer__c": {"value": "CFO engaged"},
            "TSPC__RichTextAnswer__c": {"value": None},
            "TSPC__Answer__c": {"value": None},
            "TSPC__MaxScore__c": {"value": 6.0},
            "TSPC__HasTextAnswer__c": {"value": False},
            "TSPC__TemplateQuestion__c": {"value": None},
            "LastModifiedDate": {"value": "2026-09-12T12:01:00.000Z"},
        }
    },
    {
        "fields": {
            "Id": {"value": "a1E000000000001AAA"},
            "Name": {"value": "DECISION CRITERIA - Formal or informal buying process"},
            "TSPC__Score__c": {"value": 2.0},
            "TSPC__TextAnswer__c": {"value": "Security and compliance"},
            "TSPC__RichTextAnswer__c": {"value": None},
            "TSPC__Answer__c": {"value": None},
            "TSPC__MaxScore__c": {"value": 4.0},
            "TSPC__HasTextAnswer__c": {"value": False},
            "TSPC__TemplateQuestion__c": {"value": None},
            "LastModifiedDate": {"value": "2026-09-12T12:02:00.000Z"},
        }
    },
]


def _question(
    category: str | None,
    *,
    name: str | None = None,
    score: float | None = None,
    answer: str | None = None,
) -> MeddpiccQuestion:
    return {
        "question_id": "a1E000000000009AAA",
        "category": category,
        "name": name,
        "score": score,
        "answer": answer,
        "raw_score": score,
        "raw_text_answer": answer,
        "raw_rich_text_answer": None,
        "raw_answer": None,
        "has_text_answer": None,
        "last_modified_date": None,
        "concurrency": {
            "field": "LastModifiedDate",
            "value": None,
            "conditional_header": "If-Unmodified-Since",
            "strength": "weak_timestamp",
            "mutation_enabled": False,
        },
        "field_metadata": {},
        "question_type": None,
        "score_maximum": None,
        "weight": None,
        "template_question_id": None,
        "template_metadata_status": "absent",
        "template_question": None,
        "template_version": None,
        "metadata_gaps": ["question_type", "score_maximum", "template_version"],
        "metadata_issues": [],
    }


def _scorecard(questions: list[MeddpiccQuestion] | None = None) -> MeddpiccReadResult:
    normalized = normalize_questions(questions or [])
    deal: MeddpiccDeal = {
        "deal_id": _DEAL_ID,
        "name": "Test Deal",
        "score_ratio": 0.75,
        "total_score": 42.0,
        "template_id": "a2T000000000001AAA",
        "template_deploy_date": "2026-09-01T00:00:00.000Z",
        "template_metadata_status": "complete",
        "template_version": 3.0,
        "template_total_maximum": None,
        "question_maximum_total": None,
        "native_maximums_consistent": None,
        "last_modified_date": "2026-09-12T12:00:00.000Z",
        "concurrency": {
            "field": "LastModifiedDate",
            "value": "2026-09-12T12:00:00.000Z",
            "conditional_header": "If-Unmodified-Since",
            "strength": "weak_timestamp",
            "mutation_enabled": False,
        },
        "questions_reported_count": len(questions or []),
        "questions_complete": True,
        "issues": [],
        "elements": normalized.elements,
        "gaps": normalized.gaps,
        "unmapped": normalized.unmapped,
    }
    return {
        "org_url": "https://org.my.salesforce.com",
        "opportunity_id": _OPP_ID,
        "status": "complete",
        "complete": True,
        "selected_deal_id": _DEAL_ID,
        "deals_reported_count": 1,
        "deals": [deal],
        "field_metadata": {},
        "metadata_gaps": ["question_type", "score_maximum", "template_version"],
        "issues": [],
    }


def _make_client_mock(
    *,
    deal_record: dict[str, Any] | None = None,
    answers: list[dict[str, Any]] | None = None,
    deal_side_effect: Exception | None = None,
    answers_side_effect: Exception | None = None,
    no_deals: bool = False,
) -> MagicMock:
    """Build a mock SFDirectClient context manager."""
    if deal_record is None and deal_side_effect is None and not no_deals:
        deal_record = _DEAL_RECORD_UI_API
    client = MagicMock()
    question_records = answers if answers is not None else _ANSWER_RECORDS_UI_API
    if deal_side_effect is not None:
        client.fetch_closeplan_deals.side_effect = deal_side_effect
    else:
        deal_records = [] if deal_record is None else [deal_record]
        client.fetch_closeplan_deals.return_value = UIAPIRecordCollection(
            records=deal_records,
            reported_count=len(deal_records),
            complete=True,
            issues=[],
        )
    if answers_side_effect is not None:
        client.fetch_closeplan_questions.side_effect = answers_side_effect
    else:
        client.fetch_closeplan_questions.return_value = UIAPIRecordCollection(
            records=question_records,
            reported_count=len(question_records),
            complete=True,
            issues=[],
        )
    maxima = [record.get("fields", {}).get("TSPC__MaxScore__c", {}).get("value") for record in question_records]
    finite_maxima: list[float] = []
    for maximum in maxima:
        if isinstance(maximum, bool) or not isinstance(maximum, (int, float)):
            continue
        try:
            numeric = float(maximum)
        except OverflowError:
            continue
        if math.isfinite(numeric):
            finite_maxima.append(numeric)
    client.fetch_closeplan_template.return_value = {
        "Id": "a2T000000000001AAA",
        "TSPC__Version__c": 3.0,
        "TSPC__VersionName__c": None,
        "TSPC__Type__c": "OCP",
        "TSPC__Status__c": "Active",
        "TSPC__SC_TotalMaxScore__c": sum(finite_maxima) if len(finite_maxima) == len(maxima) else None,
    }
    client.describe_sobject.return_value = {"fields": []}
    # Support context manager protocol
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=client)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


# ---------------------------------------------------------------------------
# fetch_meddpicc_scorecard -- happy path
# ---------------------------------------------------------------------------


def test_fetch_meddpicc_scorecard_returns_scorecard_dict() -> None:
    """The happy path retains the selected deal and canonical element evidence."""
    cm = _make_client_mock()
    with (
        patch("fieldkit.commands.sf.meddpicc.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.meddpicc.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.sf.client.SFDirectClient", return_value=cm),
    ):
        result = fetch_meddpicc_scorecard(_OPP_ID)

    assert result is not None
    assert result["status"] == "complete"
    assert result["opportunity_id"] == _OPP_ID
    assert result["org_url"] == "https://org.my.salesforce.com"
    assert result["selected_deal_id"] == _DEAL_ID
    deal = result["deals"][0]
    assert deal["deal_id"] == _DEAL_ID
    assert deal["score_ratio"] == 0.75
    assert deal["total_score"] == 42.0
    assert len(deal["elements"]) == 8
    assert len(deal["elements"][1]["questions"]) == 1
    assert len(deal["elements"][2]["questions"]) == 1


def test_fetch_meddpicc_scorecard_reports_ambiguity_and_enumerates_all_deals() -> None:
    second_deal_id = "a1D000000000001AAA"
    first = _DEAL_RECORD_UI_API
    second = {
        "fields": {
            "Id": {"value": second_deal_id},
            "Name": {"value": "Second Deal"},
            "TSPC__ScorecardScoreRatio__c": {"value": None},
            "TSPC__ScorecardTotalScore__c": {"value": None},
            "TSPC__Template__c": {"value": "a2T000000000001AAA"},
            "TSPC__TemplateDeployDate__c": {"value": "2026-09-01T00:00:00.000Z"},
            "LastModifiedDate": {"value": "2026-09-12T13:00:00.000Z"},
        }
    }
    cm = _make_client_mock()
    client = cm.__enter__.return_value
    client.fetch_closeplan_deals.return_value = UIAPIRecordCollection(
        records=[first, second], reported_count=2, complete=True, issues=[]
    )
    client.fetch_closeplan_questions.side_effect = [
        UIAPIRecordCollection(records=[_ANSWER_RECORDS_UI_API[0]], reported_count=1, complete=True, issues=[]),
        UIAPIRecordCollection(records=[_ANSWER_RECORDS_UI_API[1]], reported_count=1, complete=True, issues=[]),
    ]
    client.fetch_closeplan_template.return_value["TSPC__SC_TotalMaxScore__c"] = None
    with (
        patch("fieldkit.commands.sf.meddpicc.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.meddpicc.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.sf.client.SFDirectClient", return_value=cm),
    ):
        result = fetch_meddpicc_scorecard(_OPP_ID)

    assert result["status"] == "ambiguous"
    assert result["selected_deal_id"] is None
    assert [deal["deal_id"] for deal in result["deals"]] == [_DEAL_ID, second_deal_id]
    assert result["deals"][0]["elements"][1]["questions"][0]["question_id"] == "a1E000000000000AAA"
    assert result["deals"][1]["elements"][2]["questions"][0]["question_id"] == "a1E000000000001AAA"


def test_fetch_meddpicc_scorecard_marks_mismatched_deal_identity_incomplete() -> None:
    mismatched = {**_DEAL_RECORD_UI_API, "id": "a1D000000000001AAA"}
    cm = _make_client_mock(deal_record=mismatched)

    with (
        patch("fieldkit.commands.sf.meddpicc.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.meddpicc.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.sf.client.SFDirectClient", return_value=cm),
    ):
        result = fetch_meddpicc_scorecard(_OPP_ID)

    assert result["status"] == "incomplete"
    assert result["complete"] is False
    assert result["deals"][0]["deal_id"] is None
    assert "deal record 1 identity mismatch between top-level id and fields.Id.value" in result["deals"][0]["issues"]
    cm.__enter__.return_value.fetch_closeplan_questions.assert_not_called()


def test_fetch_meddpicc_scorecard_marks_duplicate_deal_identity_incomplete() -> None:
    cm = _make_client_mock()
    client = cm.__enter__.return_value
    client.fetch_closeplan_deals.return_value = UIAPIRecordCollection(
        records=[_DEAL_RECORD_UI_API, {**_DEAL_RECORD_UI_API}],
        reported_count=2,
        complete=True,
        issues=[],
    )

    with (
        patch("fieldkit.commands.sf.meddpicc.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.meddpicc.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.sf.client.SFDirectClient", return_value=cm),
    ):
        result = fetch_meddpicc_scorecard(_OPP_ID)

    assert result["status"] == "incomplete"
    assert result["complete"] is False
    assert [deal["deal_id"] for deal in result["deals"]] == [_DEAL_ID, _DEAL_ID]
    assert f"duplicate deal Id {_DEAL_ID} appears in the collection" in result["issues"]


def test_fetch_meddpicc_scorecard_selects_exact_deal_without_skipping_other_questions() -> None:
    second_deal_id = "a1D000000000001AAA"
    second = {
        "fields": {
            "Id": {"value": second_deal_id},
            "Name": {"value": "Second Deal"},
            "TSPC__ScorecardScoreRatio__c": {"value": None},
            "TSPC__ScorecardTotalScore__c": {"value": None},
            "TSPC__Template__c": {"value": "a2T000000000001AAA"},
            "TSPC__TemplateDeployDate__c": {"value": "2026-09-01T00:00:00.000Z"},
            "LastModifiedDate": {"value": None},
        }
    }
    cm = _make_client_mock()
    client = cm.__enter__.return_value
    client.fetch_closeplan_deals.return_value = UIAPIRecordCollection(
        records=[_DEAL_RECORD_UI_API, second], reported_count=2, complete=True, issues=[]
    )
    client.fetch_closeplan_questions.side_effect = [
        UIAPIRecordCollection(records=[_ANSWER_RECORDS_UI_API[0]], reported_count=1, complete=True, issues=[]),
        UIAPIRecordCollection(records=[_ANSWER_RECORDS_UI_API[1]], reported_count=1, complete=True, issues=[]),
    ]
    client.fetch_closeplan_template.return_value["TSPC__SC_TotalMaxScore__c"] = None
    with (
        patch("fieldkit.commands.sf.meddpicc.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.meddpicc.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.sf.client.SFDirectClient", return_value=cm),
    ):
        result = fetch_meddpicc_scorecard(_OPP_ID, deal_id=second_deal_id)

    assert result["status"] == "complete"
    assert result["selected_deal_id"] == second_deal_id
    assert len(result["deals"]) == 2
    assert client.fetch_closeplan_questions.call_count == 2


def test_fetch_meddpicc_scorecard_selected_deal_cannot_hide_cross_deal_question_identity() -> None:
    second_deal_id = "a1D000000000001AAA"
    second_deal = {
        "fields": {
            **_DEAL_RECORD_UI_API["fields"],
            "Id": {"value": second_deal_id},
            "Name": {"value": "Second Deal"},
        }
    }
    shared_question = _ANSWER_RECORDS_UI_API[0]
    cm = _make_client_mock()
    client = cm.__enter__.return_value
    client.fetch_closeplan_deals.return_value = UIAPIRecordCollection(
        records=[_DEAL_RECORD_UI_API, second_deal], reported_count=2, complete=True, issues=[]
    )
    client.fetch_closeplan_questions.side_effect = [
        UIAPIRecordCollection(records=[shared_question], reported_count=1, complete=True, issues=[]),
        UIAPIRecordCollection(records=[{**shared_question}], reported_count=1, complete=True, issues=[]),
    ]
    client.fetch_closeplan_template.return_value["TSPC__SC_TotalMaxScore__c"] = None

    with (
        patch("fieldkit.commands.sf.meddpicc.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.meddpicc.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.sf.client.SFDirectClient", return_value=cm),
    ):
        result = fetch_meddpicc_scorecard(_OPP_ID, deal_id=_DEAL_ID)

    assert result["status"] == "incomplete"
    assert result["complete"] is False
    assert result["selected_deal_id"] is None
    assert all(deal["questions_complete"] for deal in result["deals"])
    assert result["issues"] == [
        f"question Id a1E000000000000AAA appears under multiple deals: {_DEAL_ID}, {second_deal_id}"
    ]


def test_fetch_meddpicc_scorecard_marks_incomplete_question_collection() -> None:
    cm = _make_client_mock()
    cm.__enter__.return_value.fetch_closeplan_questions.return_value = UIAPIRecordCollection(
        records=[_ANSWER_RECORDS_UI_API[0]],
        reported_count=2,
        complete=False,
        issues=["reported count 2 does not match 1 returned record(s)"],
    )
    with (
        patch("fieldkit.commands.sf.meddpicc.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.meddpicc.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.sf.client.SFDirectClient", return_value=cm),
    ):
        result = fetch_meddpicc_scorecard(_OPP_ID)

    assert result["status"] == "incomplete"
    assert result["complete"] is False
    assert result["deals"][0]["questions_complete"] is False
    assert result["deals"][0]["native_maximums_consistent"] is None
    assert not any("question maximum total" in issue for issue in result["deals"][0]["issues"])


def test_fetch_meddpicc_scorecard_rejects_invalid_question_identity_as_incomplete() -> None:
    cm = _make_client_mock(
        answers=[
            {
                "fields": {
                    "Id": {"value": "not-an-id"},
                    "Name": {"value": "METRICS - Question"},
                    "TSPC__Score__c": {"value": 6.0},
                    "TSPC__TextAnswer__c": {"value": None},
                    "TSPC__RichTextAnswer__c": {"value": None},
                    "TSPC__Answer__c": {"value": None},
                    "TSPC__MaxScore__c": {"value": 6.0},
                    "TSPC__HasTextAnswer__c": {"value": False},
                    "TSPC__TemplateQuestion__c": {"value": None},
                    "LastModifiedDate": {"value": None},
                }
            }
        ]
    )
    with (
        patch("fieldkit.commands.sf.meddpicc.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.meddpicc.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.sf.client.SFDirectClient", return_value=cm),
    ):
        result = fetch_meddpicc_scorecard(_OPP_ID)

    assert result["status"] == "incomplete"
    assert result["complete"] is False
    assert result["deals"][0]["questions_complete"] is False
    assert result["deals"][0]["issues"] == ["question record 1 fields.Id.value is not a valid Salesforce Id"]


def test_fetch_meddpicc_scorecard_marks_mismatched_question_identity_incomplete() -> None:
    mismatched = {**_ANSWER_RECORDS_UI_API[0], "id": "a1E000000000009AAA"}
    cm = _make_client_mock(answers=[mismatched])

    with (
        patch("fieldkit.commands.sf.meddpicc.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.meddpicc.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.sf.client.SFDirectClient", return_value=cm),
    ):
        result = fetch_meddpicc_scorecard(_OPP_ID)

    assert result["status"] == "incomplete"
    assert result["complete"] is False
    assert result["deals"][0]["questions_complete"] is False
    assert result["deals"][0]["elements"][1]["questions"][0]["question_id"] is None
    assert (
        "question record 1 identity mismatch between top-level id and fields.Id.value" in result["deals"][0]["issues"]
    )


def test_fetch_meddpicc_scorecard_marks_duplicate_question_identity_incomplete() -> None:
    duplicate = {**_ANSWER_RECORDS_UI_API[0]}
    cm = _make_client_mock(answers=[_ANSWER_RECORDS_UI_API[0], duplicate])

    with (
        patch("fieldkit.commands.sf.meddpicc.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.meddpicc.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.sf.client.SFDirectClient", return_value=cm),
    ):
        result = fetch_meddpicc_scorecard(_OPP_ID)

    deal = result["deals"][0]
    assert result["status"] == "incomplete"
    assert result["complete"] is False
    assert deal["questions_complete"] is False
    assert [question["question_id"] for question in deal["elements"][1]["questions"]] == [
        "a1E000000000000AAA",
        "a1E000000000000AAA",
    ]
    assert "duplicate question Id a1E000000000000AAA appears in the collection" in deal["issues"]


def test_fetch_meddpicc_scorecard_unescapes_html_entities_in_name() -> None:
    """TSPC__DealQuestion__c.Name may carry literal HTML entities such as
    ``customer&#39;s``, matching the rich-text representation."""
    cm = _make_client_mock(
        answers=[
            {
                "fields": {
                    "Id": {"value": "a1E000000000002AAA"},
                    "Name": {"value": "ECONOMIC BUYER - the customer&#39;s decision maker"},
                    "TSPC__Score__c": {"value": 6.0},
                    "TSPC__TextAnswer__c": {"value": None},
                    "TSPC__RichTextAnswer__c": {"value": None},
                    "TSPC__Answer__c": {"value": None},
                    "TSPC__MaxScore__c": {"value": 6.0},
                    "TSPC__HasTextAnswer__c": {"value": False},
                    "TSPC__TemplateQuestion__c": {"value": None},
                    "LastModifiedDate": {"value": None},
                }
            }
        ]
    )
    with (
        patch("fieldkit.commands.sf.meddpicc.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.meddpicc.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.sf.client.SFDirectClient", return_value=cm),
    ):
        result = fetch_meddpicc_scorecard(_OPP_ID)

    assert result is not None
    first = result["deals"][0]["elements"][1]["questions"][0]
    assert first.get("name") == "ECONOMIC BUYER - the customer's decision maker"
    assert first.get("category") == "ECONOMIC BUYER"


def test_fetch_meddpicc_scorecard_maps_answer_fields() -> None:
    cm = _make_client_mock()
    with (
        patch("fieldkit.commands.sf.meddpicc.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.meddpicc.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.sf.client.SFDirectClient", return_value=cm),
    ):
        result = fetch_meddpicc_scorecard(_OPP_ID)

    assert result is not None
    first = result["deals"][0]["elements"][1]["questions"][0]
    assert first.get("category") == "ECONOMIC BUYER"
    assert first.get("name") == "ECONOMIC BUYER - Individual within the customer's organization"
    assert first.get("score") == 3.0
    assert first.get("answer") == "CFO engaged"


# ---------------------------------------------------------------------------
# fetch_meddpicc_scorecard -- no ClosePlan (graceful not-found)
# ---------------------------------------------------------------------------


def test_fetch_meddpicc_scorecard_reports_not_found_when_no_closeplan() -> None:
    """Opportunities without a ClosePlan return an explicit not-found result."""
    cm = _make_client_mock(no_deals=True)
    with (
        patch("fieldkit.commands.sf.meddpicc.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.meddpicc.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.sf.client.SFDirectClient", return_value=cm),
    ):
        result = fetch_meddpicc_scorecard(_OPP_ID)

    assert result["status"] == "not_found"
    assert result["deals"] == []


def test_fetch_meddpicc_scorecard_rejects_selected_deal_when_no_closeplan() -> None:
    cm = _make_client_mock(no_deals=True)
    with (
        patch("fieldkit.commands.sf.meddpicc.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.meddpicc.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.sf.client.SFDirectClient", return_value=cm),
    ):
        result = fetch_meddpicc_scorecard(_OPP_ID, deal_id=_DEAL_ID)

    assert result["status"] == "invalid_selection"
    assert result["selected_deal_id"] is None
    assert result["deals"] == []
    assert result["issues"] == [f"selected deal {_DEAL_ID} is not linked to opportunity {_OPP_ID}"]


def test_fetch_meddpicc_scorecard_marks_missing_question_value_envelope_incomplete() -> None:
    malformed = {
        "fields": {
            **_ANSWER_RECORDS_UI_API[0]["fields"],
            "TSPC__Score__c": {"displayValue": "3"},
        }
    }
    cm = _make_client_mock(answers=[malformed])
    with (
        patch("fieldkit.commands.sf.meddpicc.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.meddpicc.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.sf.client.SFDirectClient", return_value=cm),
    ):
        result = fetch_meddpicc_scorecard(_OPP_ID)

    assert result["status"] == "incomplete"
    assert result["complete"] is False
    assert result["deals"][0]["questions_complete"] is False
    assert result["deals"][0]["issues"] == ["question a1E000000000000AAA field TSPC__Score__c has no value envelope"]


def test_fetch_meddpicc_scorecard_marks_missing_deal_value_envelope_incomplete() -> None:
    malformed = {
        "fields": {
            **_DEAL_RECORD_UI_API["fields"],
            "TSPC__ScorecardScoreRatio__c": {"displayValue": "75%"},
        }
    }
    cm = _make_client_mock(deal_record=malformed)
    with (
        patch("fieldkit.commands.sf.meddpicc.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.meddpicc.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.sf.client.SFDirectClient", return_value=cm),
    ):
        result = fetch_meddpicc_scorecard(_OPP_ID)

    assert result["status"] == "incomplete"
    assert result["complete"] is False
    assert result["deals"][0]["score_ratio"] is None
    assert result["deals"][0]["issues"] == ["deal field TSPC__ScorecardScoreRatio__c has no value envelope"]


# ---------------------------------------------------------------------------
# fetch_meddpicc_scorecard -- auth and API errors propagate
# ---------------------------------------------------------------------------


def test_fetch_meddpicc_scorecard_propagates_auth_error() -> None:
    """SFAuthError propagates to the caller (cli_main maps it to EXIT_AUTH 2)."""
    import fieldkit.sf.client as _sf

    cm = _make_client_mock(deal_side_effect=_sf.SFAuthError("session expired"))
    with (
        patch("fieldkit.commands.sf.meddpicc.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.meddpicc.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.sf.client.SFDirectClient", return_value=cm),
        pytest.raises(_sf.SFAuthError, match="session expired"),
    ):
        fetch_meddpicc_scorecard(_OPP_ID)


def test_fetch_meddpicc_scorecard_propagates_describe_auth_error() -> None:
    """Metadata degradation never turns an expired session into a partial read."""
    cm = _make_client_mock()
    cm.__enter__.return_value.describe_sobject.side_effect = SFAuthError("session expired during describe")
    with (
        patch("fieldkit.commands.sf.meddpicc.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.meddpicc.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.sf.client.SFDirectClient", return_value=cm),
        pytest.raises(SFAuthError, match="session expired during describe"),
    ):
        fetch_meddpicc_scorecard(_OPP_ID)


def test_fetch_meddpicc_scorecard_propagates_api_error() -> None:
    """SFAPIError propagates to the caller (cli_main maps it to EXIT_DATA 3)."""
    import fieldkit.sf.client as _sf

    cm = _make_client_mock(deal_side_effect=_sf.SFAPIError("connection refused"))
    with (
        patch("fieldkit.commands.sf.meddpicc.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.meddpicc.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.sf.client.SFDirectClient", return_value=cm),
        pytest.raises(_sf.SFAPIError, match="connection refused"),
    ):
        fetch_meddpicc_scorecard(_OPP_ID)


# ---------------------------------------------------------------------------
# print_scorecard
# ---------------------------------------------------------------------------


def test_print_scorecard_includes_score_ratio_and_total() -> None:
    scorecard = _scorecard()
    runner = CliRunner()

    @click.command()
    def _cmd() -> None:
        print_scorecard(scorecard)

    result = runner.invoke(_cmd)
    assert result.exit_code == 0
    assert "75%" in result.output
    assert "42" in result.output


def test_print_scorecard_shows_all_elements_and_gaps_when_empty() -> None:
    scorecard = _scorecard()
    runner = CliRunner()

    @click.command()
    def _cmd() -> None:
        print_scorecard(scorecard)

    result = runner.invoke(_cmd)
    assert result.exit_code == 0
    assert "Metrics: unpopulated" in result.output
    assert "Champion: unpopulated" in result.output
    assert "Gaps: Metrics, Economic Buyer" in result.output


def test_print_scorecard_groups_questions_by_canonical_element() -> None:
    questions: list[MeddpiccQuestion] = [
        _question("Economic Buyer", name="EB", score=3.0, answer="CFO engaged"),
        _question("Decision Criteria", name="DC", score=2.0, answer="Security"),
    ]
    scorecard = _scorecard(questions)
    runner = CliRunner()

    @click.command()
    def _cmd() -> None:
        print_scorecard(scorecard)

    result = runner.invoke(_cmd)
    assert result.exit_code == 0
    assert "Economic Buyer" in result.output
    assert "Decision Criteria" in result.output
    assert "CFO engaged" in result.output
    assert "Security" in result.output


def test_print_scorecard_renders_unmapped_question_defaults() -> None:
    scorecard = _scorecard([_question("Unknown Category")])
    runner = CliRunner()

    @click.command()
    def _cmd() -> None:
        print_scorecard(scorecard)

    result = runner.invoke(_cmd)
    assert result.exit_code == 0
    assert "-- Unmapped ClosePlan Questions --" in result.output
    assert "(unnamed)  score=(not set)" in result.output
    assert "(no answer)" in result.output


# ---------------------------------------------------------------------------
# CLI -- happy path
# ---------------------------------------------------------------------------


def test_cli_happy_path_exits_0_and_prints_scorecard() -> None:
    questions: list[MeddpiccQuestion] = [
        _question("Metrics", name="ROI", score=4.0, answer="20% cost reduction"),
    ]
    scorecard = _scorecard(questions)
    scorecard["deals"][0]["score_ratio"] = 0.8
    scorecard["deals"][0]["total_score"] = 50.0
    runner = CliRunner()
    with patch("fieldkit.commands.sf.meddpicc.fetch_meddpicc_scorecard", return_value=scorecard):
        result = runner.invoke(cli, [_OPP_ID])

    assert result.exit_code == 0
    assert "80%" in result.output
    assert "50" in result.output


def test_cli_passes_exact_deal_id_to_native_reader() -> None:
    scorecard = _scorecard()
    runner = CliRunner()
    with patch("fieldkit.commands.sf.meddpicc.fetch_meddpicc_scorecard", return_value=scorecard) as fetch:
        result = runner.invoke(cli, [_OPP_ID, "--deal-id", _DEAL_ID])

    assert result.exit_code == 0
    fetch.assert_called_once_with(_OPP_ID, deal_id=_DEAL_ID)


def test_cli_rejects_selected_deal_when_no_closeplan() -> None:
    scorecard = _scorecard()
    scorecard.update(
        status="invalid_selection",
        selected_deal_id=None,
        deals_reported_count=0,
        deals=[],
        issues=[f"selected deal {_DEAL_ID} is not linked to opportunity {_OPP_ID}"],
    )
    runner = CliRunner()
    with patch("fieldkit.commands.sf.meddpicc.fetch_meddpicc_scorecard", return_value=scorecard):
        result = runner.invoke(cli, [_OPP_ID, "--deal-id", _DEAL_ID, "--json"])

    assert result.exit_code == 3
    assert json.loads(result.output)["status"] == "invalid_selection"


def test_cli_json_reports_ambiguity_with_all_deal_ids_and_exits_partial() -> None:
    scorecard = _scorecard()
    second = scorecard["deals"][0].copy()
    second["deal_id"] = "a1D000000000001AAA"
    scorecard["deals"] = [scorecard["deals"][0], second]
    scorecard["status"] = "ambiguous"
    scorecard["selected_deal_id"] = None
    scorecard["issues"] = ["multiple ClosePlan deals are linked; select an exact deal Id"]
    runner = CliRunner()
    with patch("fieldkit.commands.sf.meddpicc.fetch_meddpicc_scorecard", return_value=scorecard):
        result = runner.invoke(cli, [_OPP_ID, "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "ambiguous"
    assert [deal["deal_id"] for deal in payload["deals"]] == [_DEAL_ID, "a1D000000000001AAA"]


def test_cli_json_reports_incomplete_result_and_exits_partial() -> None:
    scorecard = _scorecard()
    scorecard["status"] = "incomplete"
    scorecard["complete"] = False
    scorecard["issues"] = ["question collection is incomplete"]
    runner = CliRunner()
    with patch("fieldkit.commands.sf.meddpicc.fetch_meddpicc_scorecard", return_value=scorecard):
        result = runner.invoke(cli, [_OPP_ID, "--json"])

    assert result.exit_code == 1
    assert json.loads(result.output)["status"] == "incomplete"


def test_cli_human_output_preserves_question_id_and_missing_native_metadata() -> None:
    scorecard = _scorecard([_question("METRICS", name="METRICS - Outcome", score=6)])
    runner = CliRunner()
    with patch("fieldkit.commands.sf.meddpicc.fetch_meddpicc_scorecard", return_value=scorecard):
        result = runner.invoke(cli, [_OPP_ID])

    assert result.exit_code == 0
    assert "[a1E000000000009AAA]" in result.output
    assert "Native metadata unknown: question_type, score_maximum, template_version" in result.output


def test_cli_json_preserves_named_party_evidence_and_zero_state() -> None:
    scorecard = _scorecard(
        [
            _question(
                "CHAMPION",
                name="CHAMPION - Person with influence",
                score=0,
                answer="Alex Example is the named champion",
            )
        ]
    )
    runner = CliRunner()
    with patch("fieldkit.commands.sf.meddpicc.fetch_meddpicc_scorecard", return_value=scorecard):
        result = runner.invoke(cli, [_OPP_ID, "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    champion = payload["deals"][0]["elements"][5]
    assert champion["state"] == "scored_zero"
    assert champion["questions"][0]["answer"] == "Alex Example is the named champion"
    assert "champion" not in payload["deals"][0]["gaps"]


# ---------------------------------------------------------------------------
# CLI -- no ClosePlan (TSPC not found)
# ---------------------------------------------------------------------------


def test_cli_no_closeplan_prints_clear_message_and_exits_0() -> None:
    """Opportunities without a ClosePlan show a clear not-found message, not a crash."""
    runner = CliRunner()
    scorecard = _scorecard()
    scorecard.update(status="not_found", selected_deal_id=None, deals_reported_count=0, deals=[])
    with patch("fieldkit.commands.sf.meddpicc.fetch_meddpicc_scorecard", return_value=scorecard):
        result = runner.invoke(cli, [_OPP_ID])

    assert result.exit_code == 0
    # Acceptance criterion: clear "no ClosePlan" / "not found" message
    output_lower = result.output.lower()
    assert "no closeplan" in output_lower or "not found" in output_lower or "tspc" in output_lower, (
        f"Expected a no-ClosePlan message in output, got: {result.output!r}"
    )


def test_cli_json_no_closeplan_returns_empty_normalized_collections() -> None:
    runner = CliRunner()
    scorecard = _scorecard()
    scorecard.update(status="not_found", selected_deal_id=None, deals_reported_count=0, deals=[])
    with patch("fieldkit.commands.sf.meddpicc.fetch_meddpicc_scorecard", return_value=scorecard):
        result = runner.invoke(cli, [_OPP_ID, "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["opportunity_id"] == _OPP_ID
    assert payload["status"] == "not_found"
    assert payload["deals"] == []


# ---------------------------------------------------------------------------
# CLI -- invalid opp_id
# ---------------------------------------------------------------------------


def test_cli_exits_3_for_invalid_opp_id() -> None:
    """Invalid opportunity ID format exits with code 3."""
    runner = CliRunner()
    result = runner.invoke(cli, ["not-a-valid-id"])
    assert result.exit_code == 3


def test_cli_exits_3_for_empty_opp_id() -> None:
    """Empty string is rejected as invalid opportunity ID."""
    runner = CliRunner()
    result = runner.invoke(cli, [""])
    assert result.exit_code == 3


def test_cli_exits_3_for_invalid_deal_id() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, [_OPP_ID, "--deal-id", "not-a-valid-id"])

    assert result.exit_code == 3
    assert "Invalid deal ID" in result.output


# ---------------------------------------------------------------------------
# CLI -- auth error propagates through cli_main
# ---------------------------------------------------------------------------


def test_cli_exits_2_when_auth_error_raised() -> None:
    """SFAuthError raised by fetch_meddpicc_scorecard maps to exit code 2 via cli_main."""
    import fieldkit.sf.client as _sf

    runner = CliRunner()
    with patch(
        "fieldkit.commands.sf.meddpicc.fetch_meddpicc_scorecard",
        side_effect=_sf.SFAuthError("session expired"),
    ):
        result = runner.invoke(cli, [_OPP_ID])

    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# fetch_meddpicc_scorecard -- missing SID raises SFAuthError (RC-TEST-4)
# ---------------------------------------------------------------------------


def test_fetch_meddpicc_scorecard_raises_auth_error_when_no_sid() -> None:
    """SFAuthError is raised when no Salesforce session ID is configured."""

    with (
        patch("fieldkit.commands.sf.meddpicc.get_sf_session_id", return_value=""),
        pytest.raises(SFAuthError, match="No Salesforce session"),
    ):
        fetch_meddpicc_scorecard(_OPP_ID)


# ---------------------------------------------------------------------------
# fetch_meddpicc_scorecard -- malformed deal_id returns None (RC-TEST-5)
# ---------------------------------------------------------------------------


def test_fetch_meddpicc_scorecard_marks_malformed_deal_id_incomplete() -> None:
    """An API-derived malformed deal Id remains visible as an issue and makes the read incomplete."""
    bad_deal_record: dict[str, Any] = {
        "fields": {
            "Id": {"value": "BAD ID WITH SPACES"},
            "Name": {"value": "Malformed Deal"},
            "TSPC__ScorecardScoreRatio__c": {"value": 0.5},
            "TSPC__ScorecardTotalScore__c": {"value": 10.0},
            "TSPC__Template__c": {"value": None},
            "TSPC__TemplateDeployDate__c": {"value": None},
            "LastModifiedDate": {"value": None},
        }
    }
    cm = _make_client_mock(deal_record=bad_deal_record)
    cm.__enter__.return_value.fetch_closeplan_deals.return_value = UIAPIRecordCollection(
        records=[bad_deal_record], reported_count=1, complete=True, issues=[]
    )

    with (
        patch("fieldkit.commands.sf.meddpicc.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.meddpicc.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.sf.client.SFDirectClient", return_value=cm),
    ):
        result = fetch_meddpicc_scorecard(_OPP_ID)

    assert result["status"] == "incomplete"
    assert result["deals"][0]["deal_id"] is None
    assert "deal record 1 fields.Id.value is not a valid Salesforce Id" in result["deals"][0]["issues"]
    assert any("deal has no valid exact Id; questions were not requested" in issue for issue in result["issues"])
