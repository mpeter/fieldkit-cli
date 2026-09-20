"""Template-metadata integration and rendering tests for ``fieldkit sf meddpicc``."""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from test_sf_meddpicc import (
    _ANSWER_RECORDS_UI_API,
    _DEAL_ID,
    _DEAL_RECORD_UI_API,
    _OPP_ID,
    _make_client_mock,
    _question,
    _scorecard,
)

from fieldkit.commands.sf.meddpicc import cli, fetch_meddpicc_scorecard
from fieldkit.sf.client import SFAPIError, SFAuthError
from fieldkit.sf.types import MeddpiccReadResult, UIAPIRecordCollection

pytestmark = pytest.mark.unit


def _run_scorecard(cm: MagicMock) -> MeddpiccReadResult:
    with (
        patch("fieldkit.commands.sf.meddpicc.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.meddpicc.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.sf.client.SFDirectClient", return_value=cm),
    ):
        return fetch_meddpicc_scorecard(_OPP_ID)


def _bind_template_question(cm: MagicMock, *, mode: str = "Answers", template_id: str = "a2T000000000001AAA") -> None:
    template_question_id = "a2G000000000001AAA"
    client = cm.__enter__.return_value
    client.fetch_closeplan_template_question.return_value = {
        "Id": template_question_id,
        "Name": "Native template question",
        "TSPC__Template__c": template_id,
        "TSPC__Category__c": "a1o000000000001AAA",
        "TSPC__QuestionCategory__c": "a1u000000000001AAA",
        "TSPC__Mode__c": mode,
        "TSPC__HasTextAnswer__c": mode == "RichText",
        "TSPC__MaxScore__c": 6.0,
        "TSPC__Sync_ScoreField__c": None,
        "TSPC__Sync_ScoreRatioField__c": None,
        "TSPC__HasSharedScore__c": False,
    }
    client.fetch_closeplan_template_answers.return_value = UIAPIRecordCollection(
        records=[], reported_count=0, complete=True, issues=[]
    )


def test_fetch_meddpicc_scorecard_preserves_mode_version_and_native_maximum_consistency() -> None:
    template_question_id = "a2G000000000001AAA"
    template_id = "a2T000000000001AAA"
    question = {"fields": {**_ANSWER_RECORDS_UI_API[0]["fields"]}}
    question["fields"]["TSPC__TemplateQuestion__c"] = {"value": template_question_id}
    question["fields"]["TSPC__MaxScore__c"] = {"value": 6.0}
    cm = _make_client_mock(answers=[question])
    client = cm.__enter__.return_value
    client.fetch_closeplan_template.return_value = {
        "Id": template_id,
        "TSPC__Version__c": 3.0,
        "TSPC__VersionName__c": None,
        "TSPC__Type__c": "OCP",
        "TSPC__Status__c": "Active",
        "TSPC__SC_TotalMaxScore__c": 6.0,
    }
    client.fetch_closeplan_template_question.return_value = {
        "Id": template_question_id,
        "Name": "Native template question",
        "TSPC__Template__c": template_id,
        "TSPC__Category__c": "a1o000000000001AAA",
        "TSPC__QuestionCategory__c": "a1u000000000001AAA",
        "TSPC__Mode__c": "Answers",
        "TSPC__HasTextAnswer__c": False,
        "TSPC__MaxScore__c": 6.0,
        "TSPC__Sync_ScoreField__c": None,
        "TSPC__Sync_ScoreRatioField__c": None,
        "TSPC__HasSharedScore__c": False,
    }
    client.fetch_closeplan_template_answers.return_value = UIAPIRecordCollection(
        records=[], reported_count=0, complete=True, issues=[]
    )

    with (
        patch("fieldkit.commands.sf.meddpicc.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.meddpicc.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.sf.client.SFDirectClient", return_value=cm),
    ):
        result = fetch_meddpicc_scorecard(_OPP_ID)

    assert result["complete"] is True
    deal = result["deals"][0]
    assert deal["template_metadata_status"] == "complete"
    assert deal["template_version"] == 3.0
    assert deal["template_total_maximum"] == 6.0
    assert deal["question_maximum_total"] == 6.0
    assert deal["native_maximums_consistent"] is True
    normalized = deal["elements"][1]["questions"][0]
    assert normalized["question_type"] == "Answers"
    assert normalized["template_version"] == 3.0
    assert normalized["weight"] is None
    assert normalized["metadata_gaps"] == []
    assert result["metadata_gaps"] == []
    client.fetch_closeplan_template.assert_called_once_with(template_id)


def test_fetch_meddpicc_scorecard_reads_one_shared_template_once_across_deals() -> None:
    second_deal_id = "a1D000000000001AAA"
    second_deal = {
        "fields": {
            **_DEAL_RECORD_UI_API["fields"],
            "Id": {"value": second_deal_id},
            "Name": {"value": "Second Deal"},
        }
    }
    cm = _make_client_mock()
    client = cm.__enter__.return_value
    client.fetch_closeplan_deals.return_value = UIAPIRecordCollection(
        records=[_DEAL_RECORD_UI_API, second_deal], reported_count=2, complete=True, issues=[]
    )
    client.fetch_closeplan_questions.side_effect = [
        UIAPIRecordCollection(records=[], reported_count=0, complete=True, issues=[]),
        UIAPIRecordCollection(records=[], reported_count=0, complete=True, issues=[]),
    ]
    client.fetch_closeplan_template.return_value["TSPC__SC_TotalMaxScore__c"] = 0.0

    result = _run_scorecard(cm)

    assert result["status"] == "ambiguous"
    assert all(deal["template_metadata_status"] == "complete" for deal in result["deals"])
    client.fetch_closeplan_template.assert_called_once_with("a2T000000000001AAA")


def test_fetch_meddpicc_scorecard_rejects_template_question_ownership_drift() -> None:
    question = {"fields": {**_ANSWER_RECORDS_UI_API[0]["fields"]}}
    question["fields"]["TSPC__TemplateQuestion__c"] = {"value": "a2G000000000001AAA"}
    cm = _make_client_mock(answers=[question])
    _bind_template_question(cm, template_id="a2T000000000002AAA")

    result = _run_scorecard(cm)

    normalized = result["deals"][0]["elements"][1]["questions"][0]
    assert result["status"] == "incomplete"
    assert normalized["template_metadata_status"] == "incomplete"
    assert normalized["metadata_issues"] == [
        "template question belongs to template a2T000000000002AAA, not deal template a2T000000000001AAA"
    ]


def test_fetch_meddpicc_scorecard_rejects_question_template_when_deal_template_is_absent() -> None:
    deal = {"fields": {**_DEAL_RECORD_UI_API["fields"]}}
    deal["fields"]["TSPC__Template__c"] = {"value": None}
    question = {"fields": {**_ANSWER_RECORDS_UI_API[0]["fields"]}}
    question["fields"]["TSPC__TemplateQuestion__c"] = {"value": "a2G000000000001AAA"}
    cm = _make_client_mock(deal_record=deal, answers=[question])
    _bind_template_question(cm)

    result = _run_scorecard(cm)

    normalized = result["deals"][0]["elements"][1]["questions"][0]
    assert result["status"] == "incomplete"
    assert normalized["template_metadata_status"] == "incomplete"
    assert normalized["metadata_issues"] == [
        "template question belongs to template a2T000000000001AAA, not deal template (missing Id)"
    ]


def test_fetch_meddpicc_scorecard_rejects_native_maximum_total_drift() -> None:
    cm = _make_client_mock(answers=[_ANSWER_RECORDS_UI_API[0]])
    client = cm.__enter__.return_value
    client.fetch_closeplan_template.return_value["TSPC__SC_TotalMaxScore__c"] = 7.0

    result = _run_scorecard(cm)

    deal = result["deals"][0]
    assert result["status"] == "incomplete"
    assert deal["question_maximum_total"] == 6.0
    assert deal["native_maximums_consistent"] is False
    assert deal["issues"] == ["question maximum total 6.0 does not match template calculated total maximum 7.0"]


def test_fetch_meddpicc_scorecard_rejects_overflowing_question_maximum_total() -> None:
    first = {"fields": {**_ANSWER_RECORDS_UI_API[0]["fields"]}}
    first["fields"]["TSPC__MaxScore__c"] = {"value": 1.7e308}
    second = {"fields": {**_ANSWER_RECORDS_UI_API[1]["fields"]}}
    second["fields"]["TSPC__MaxScore__c"] = {"value": 1.7e308}
    cm = _make_client_mock(answers=[first, second])
    cm.__enter__.return_value.fetch_closeplan_template.return_value["TSPC__SC_TotalMaxScore__c"] = 1.0

    result = _run_scorecard(cm)

    deal = result["deals"][0]
    assert result["status"] == "incomplete"
    assert deal["question_maximum_total"] is None
    assert deal["native_maximums_consistent"] is None
    assert "question maximum total is not a finite number" in deal["issues"]


@pytest.mark.parametrize(
    ("version", "expected_status", "expected_issue_fragment"),
    [
        pytest.param(None, "complete", None, id="explicit-null"),
        pytest.param("3.0", "incomplete", "TSPC__Version__c has invalid str", id="malformed-string"),
        pytest.param(float("inf"), "incomplete", "TSPC__Version__c has invalid float", id="nonfinite"),
    ],
)
def test_fetch_meddpicc_scorecard_preserves_nullable_version_and_rejects_malformed_values(
    version: object,
    expected_status: str,
    expected_issue_fragment: str | None,
) -> None:
    cm = _make_client_mock(answers=[_ANSWER_RECORDS_UI_API[0]])
    client = cm.__enter__.return_value
    client.fetch_closeplan_template.return_value["TSPC__Version__c"] = version

    result = _run_scorecard(cm)

    deal = result["deals"][0]
    assert result["status"] == expected_status
    assert deal["template_version"] is None
    assert "template_version" in result["metadata_gaps"]
    if expected_issue_fragment is None:
        assert deal["issues"] == []
    else:
        assert any(expected_issue_fragment in issue for issue in deal["issues"])


def test_fetch_meddpicc_scorecard_degrades_when_template_metadata_is_unavailable() -> None:
    cm = _make_client_mock(answers=[_ANSWER_RECORDS_UI_API[0]])
    cm.__enter__.return_value.fetch_closeplan_template.side_effect = SFAPIError("service unavailable")

    result = _run_scorecard(cm)

    deal = result["deals"][0]
    assert result["status"] == "incomplete"
    assert deal["template_metadata_status"] == "incomplete"
    assert deal["issues"] == ["template a2T000000000001AAA metadata unavailable: service unavailable"]


def test_fetch_meddpicc_scorecard_propagates_template_auth_failure() -> None:
    cm = _make_client_mock(answers=[_ANSWER_RECORDS_UI_API[0]])
    cm.__enter__.return_value.fetch_closeplan_template.side_effect = SFAuthError("session expired")

    with pytest.raises(SFAuthError, match="session expired"):
        _run_scorecard(cm)


def test_fetch_meddpicc_scorecard_keeps_non_answers_mode_readable_but_mutation_disabled() -> None:
    question = {"fields": {**_ANSWER_RECORDS_UI_API[0]["fields"]}}
    question["fields"]["TSPC__TemplateQuestion__c"] = {"value": "a2G000000000001AAA"}
    cm = _make_client_mock(answers=[question])
    _bind_template_question(cm, mode="RichText")

    result = _run_scorecard(cm)

    normalized = result["deals"][0]["elements"][1]["questions"][0]
    assert result["status"] == "complete"
    assert normalized["question_type"] == "RichText"
    assert normalized["template_question"] is not None
    assert normalized["template_question"]["answer_model"] == "text"
    assert normalized["concurrency"]["mutation_enabled"] is False


def test_fetch_meddpicc_scorecard_deduplicates_template_reads_and_preserves_exact_choices() -> None:
    """Read one shared template once while preserving every exact answer choice."""
    template_question_id = "a2G000000000001AAA"
    template_id = "a2T000000000001AAA"
    category_id = "a1o000000000001AAA"
    question_category_id = "a1u000000000001AAA"
    choice_ids = ["a2H000000000001AAA", "a2H000000000002AAA", "a2H000000000003AAA"]
    answers = []
    for record in _ANSWER_RECORDS_UI_API:
        copied = {"fields": {**record["fields"]}}
        copied["fields"]["TSPC__TemplateQuestion__c"] = {"value": template_question_id}
        answers.append(copied)
    cm = _make_client_mock(answers=answers)
    client = cm.__enter__.return_value
    client.fetch_closeplan_template_question.return_value = {
        "Id": template_question_id,
        "Name": "Native template question",
        "TSPC__Template__c": template_id,
        "TSPC__Category__c": category_id,
        "TSPC__QuestionCategory__c": question_category_id,
        "TSPC__Mode__c": "Answers",
        "TSPC__HasTextAnswer__c": False,
        "TSPC__MaxScore__c": 6.0,
        "TSPC__Sync_ScoreField__c": None,
        "TSPC__Sync_ScoreRatioField__c": None,
        "TSPC__HasSharedScore__c": False,
    }
    client.fetch_closeplan_template_answers.return_value = UIAPIRecordCollection(
        records=[
            {
                "id": choice_id,
                "fields": {
                    "Id": {"value": choice_id},
                    "Name": {"value": f"Choice {index}"},
                    "TSPC__Text__c": {"value": f"Evidence level {index}"},
                    "TSPC__Attitude__c": {"value": None},
                    "TSPC__HasTextAnswer__c": {"value": False},
                    "TSPC__MaxScore__c": {"value": maximum},
                    "TSPC__SortOrder__c": {"value": float(index)},
                    "TSPC__Sync_TextAnswerField__c": {"value": None},
                    "TSPC__Sync_ValueFieldPickval__c": {"value": None},
                },
            }
            for index, (choice_id, maximum) in enumerate(zip(choice_ids, (2.0, 4.0, 6.0), strict=True))
        ],
        reported_count=3,
        complete=True,
        issues=[],
    )

    with (
        patch("fieldkit.commands.sf.meddpicc.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.meddpicc.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.sf.client.SFDirectClient", return_value=cm),
    ):
        result = fetch_meddpicc_scorecard(_OPP_ID)

    assert result["complete"] is True
    questions = [question for element in result["deals"][0]["elements"] for question in element["questions"]]
    assert len(questions) == 2
    assert all(question["template_metadata_status"] == "complete" for question in questions)
    template = questions[0]["template_question"]
    assert template is not None
    assert template["template_question_id"] == template_question_id
    assert template["template_id"] == template_id
    assert template["category_id"] == category_id
    assert template["question_category_id"] == question_category_id
    assert template["score_maximum"] == 6.0
    assert template["answer_choices_reported_count"] == 3
    assert template["answer_choices_complete"] is True
    assert [choice["answer_id"] for choice in template["answer_choices"]] == choice_ids
    assert [choice["max_score"] for choice in template["answer_choices"]] == [2.0, 4.0, 6.0]
    assert client.fetch_closeplan_template_question.call_count == 1
    client.fetch_closeplan_template_question.assert_called_once_with(template_question_id)
    assert client.fetch_closeplan_template_answers.call_count == 1
    client.fetch_closeplan_template_answers.assert_called_once_with(template_question_id)


def test_fetch_meddpicc_scorecard_distinguishes_absent_template_reference() -> None:
    cm = _make_client_mock()
    with (
        patch("fieldkit.commands.sf.meddpicc.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.meddpicc.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.sf.client.SFDirectClient", return_value=cm),
    ):
        result = fetch_meddpicc_scorecard(_OPP_ID)

    question = result["deals"][0]["elements"][1]["questions"][0]
    assert question["template_question_id"] is None
    assert question["template_metadata_status"] == "absent"
    assert question["template_question"] is None
    cm.__enter__.return_value.fetch_closeplan_template_question.assert_not_called()
    cm.__enter__.return_value.fetch_closeplan_template_answers.assert_not_called()


def test_fetch_meddpicc_scorecard_propagates_malformed_template_reference_once() -> None:
    """Surface one deal-scoped diagnostic for a malformed template reference."""
    question_id = "a1E000000000000AAA"
    malformed = {"fields": {**_ANSWER_RECORDS_UI_API[0]["fields"]}}
    malformed["fields"]["TSPC__TemplateQuestion__c"] = {"displayValue": "Template"}
    cm = _make_client_mock(answers=[malformed])

    with (
        patch("fieldkit.commands.sf.meddpicc.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.meddpicc.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.sf.client.SFDirectClient", return_value=cm),
    ):
        result = fetch_meddpicc_scorecard(_OPP_ID)

    issue = f"question {question_id} field TSPC__TemplateQuestion__c has no value envelope"
    deal = result["deals"][0]
    question = deal["elements"][1]["questions"][0]
    assert result["status"] == "incomplete"
    assert result["complete"] is False
    assert question["template_metadata_status"] == "incomplete"
    assert question["metadata_issues"] == [issue]
    assert deal["issues"] == [issue]
    assert result["issues"] == [f"deal {_DEAL_ID}: {issue}"]


@pytest.mark.parametrize(
    ("scope", "field_name", "normalized_key", "invalid_value"),
    [
        pytest.param("question", "TSPC__Answer__c", "raw_answer", 7, id="question-answer-string"),
        pytest.param("question", "TSPC__HasTextAnswer__c", "has_text_answer", "false", id="question-has-text-boolean"),
        pytest.param("question", "TSPC__MaxScore__c", "score_maximum", "6", id="question-maximum-number"),
        pytest.param("question", "TSPC__MaxScore__c", "score_maximum", float("nan"), id="question-maximum-nan"),
        pytest.param("question", "TSPC__MaxScore__c", "score_maximum", float("inf"), id="question-maximum-infinity"),
        pytest.param("question", "TSPC__MaxScore__c", "score_maximum", 10**400, id="question-maximum-overflow"),
        pytest.param(
            "deal", "TSPC__TemplateDeployDate__c", "template_deploy_date", False, id="deal-deploy-date-string"
        ),
    ],
)
def test_fetch_meddpicc_scorecard_rejects_invalid_new_native_scalars(
    scope: str,
    field_name: str,
    normalized_key: str,
    invalid_value: object,
) -> None:
    """Reject non-null malformed native scalars instead of coercing them."""
    question_record = {"fields": {**_ANSWER_RECORDS_UI_API[0]["fields"]}}
    deal_record = {"fields": {**_DEAL_RECORD_UI_API["fields"]}}
    target_record = question_record if scope == "question" else deal_record
    target_record["fields"][field_name] = {"value": invalid_value}
    cm = _make_client_mock(deal_record=deal_record, answers=[question_record])

    with (
        patch("fieldkit.commands.sf.meddpicc.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.meddpicc.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.sf.client.SFDirectClient", return_value=cm),
    ):
        result = fetch_meddpicc_scorecard(_OPP_ID)

    deal = result["deals"][0]
    question = deal["elements"][1]["questions"][0]
    issue_fragment = f"field {field_name} has invalid"
    assert result["status"] == "incomplete"
    assert result["complete"] is False
    assert any(issue_fragment in issue for issue in deal["issues"])
    if scope == "question":
        assert deal["questions_complete"] is False
        assert question[normalized_key] is None
        assert any(issue_fragment in issue for issue in question["metadata_issues"])
    else:
        assert deal["questions_complete"] is True
        assert deal[normalized_key] is None


def test_fetch_meddpicc_scorecard_accepts_explicit_null_new_native_scalars() -> None:
    question_record = {"fields": {**_ANSWER_RECORDS_UI_API[0]["fields"]}}
    for field_name in ("TSPC__Answer__c", "TSPC__HasTextAnswer__c", "TSPC__MaxScore__c"):
        question_record["fields"][field_name] = {"value": None}
    deal_record = {"fields": {**_DEAL_RECORD_UI_API["fields"]}}
    deal_record["fields"]["TSPC__TemplateDeployDate__c"] = {"value": None}
    cm = _make_client_mock(deal_record=deal_record, answers=[question_record])

    with (
        patch("fieldkit.commands.sf.meddpicc.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.meddpicc.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.sf.client.SFDirectClient", return_value=cm),
    ):
        result = fetch_meddpicc_scorecard(_OPP_ID)

    deal = result["deals"][0]
    question = deal["elements"][1]["questions"][0]
    assert result["status"] == "complete"
    assert result["complete"] is True
    assert question["raw_answer"] is None
    assert question["has_text_answer"] is None
    assert question["score_maximum"] is None
    assert question["metadata_issues"] == []
    assert deal["template_deploy_date"] is None
    assert deal["issues"] == []


def test_fetch_meddpicc_scorecard_propagates_incomplete_template_answer_count() -> None:
    """Propagate child-count mismatch evidence through each completeness layer."""
    template_question_id = "a2G000000000001AAA"
    answer = {"fields": {**_ANSWER_RECORDS_UI_API[0]["fields"]}}
    answer["fields"]["TSPC__TemplateQuestion__c"] = {"value": template_question_id}
    cm = _make_client_mock(answers=[answer])
    client = cm.__enter__.return_value
    client.fetch_closeplan_template_question.return_value = {
        "Id": template_question_id,
        "Name": "Native template question",
        "TSPC__Template__c": "a2T000000000001AAA",
        "TSPC__Category__c": "a1o000000000001AAA",
        "TSPC__QuestionCategory__c": "a1u000000000001AAA",
        "TSPC__Mode__c": "Answers",
        "TSPC__HasTextAnswer__c": False,
        "TSPC__MaxScore__c": 6.0,
        "TSPC__Sync_ScoreField__c": None,
        "TSPC__Sync_ScoreRatioField__c": None,
        "TSPC__HasSharedScore__c": False,
    }
    client.fetch_closeplan_template_answers.return_value = UIAPIRecordCollection(
        records=[
            {
                "id": "a2H000000000001AAA",
                "fields": {
                    "Id": {"value": "a2H000000000001AAA"},
                    "Name": {"value": "Choice one"},
                    "TSPC__Text__c": {"value": "Evidence level"},
                    "TSPC__Attitude__c": {"value": None},
                    "TSPC__HasTextAnswer__c": {"value": False},
                    "TSPC__MaxScore__c": {"value": 2.0},
                    "TSPC__SortOrder__c": {"value": 0.0},
                    "TSPC__Sync_TextAnswerField__c": {"value": None},
                    "TSPC__Sync_ValueFieldPickval__c": {"value": None},
                },
            }
        ],
        reported_count=3,
        complete=False,
        issues=["reported count 3 does not match 1 returned record(s)"],
    )

    with (
        patch("fieldkit.commands.sf.meddpicc.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.meddpicc.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.sf.client.SFDirectClient", return_value=cm),
    ):
        result = fetch_meddpicc_scorecard(_OPP_ID)

    deal = result["deals"][0]
    question = deal["elements"][1]["questions"][0]
    assert result["status"] == "incomplete"
    assert result["complete"] is False
    assert deal["questions_complete"] is False
    assert question["template_metadata_status"] == "incomplete"
    template = question["template_question"]
    assert template is not None
    assert template["answer_model"] == "incomplete"
    assert template["answer_choices_reported_count"] == 3
    assert template["answer_choices_complete"] is False
    assert len(template["answer_choices"]) == 1
    assert "reported count 3 does not match 1 returned record(s)" in question["metadata_issues"]


def test_fetch_meddpicc_scorecard_propagates_incomplete_template_answers_without_diagnostics() -> None:
    template_question_id = "a2G000000000001AAA"
    question = {"fields": {**_ANSWER_RECORDS_UI_API[0]["fields"]}}
    question["fields"]["TSPC__TemplateQuestion__c"] = {"value": template_question_id}
    cm = _make_client_mock(answers=[question])
    client = cm.__enter__.return_value
    client.fetch_closeplan_template_question.return_value = {
        "Id": template_question_id,
        "Name": "Native template question",
        "TSPC__Template__c": "a2T000000000001AAA",
        "TSPC__Category__c": "a1o000000000001AAA",
        "TSPC__QuestionCategory__c": "a1u000000000001AAA",
        "TSPC__Mode__c": "Answers",
        "TSPC__HasTextAnswer__c": False,
        "TSPC__MaxScore__c": 6.0,
        "TSPC__Sync_ScoreField__c": None,
        "TSPC__Sync_ScoreRatioField__c": None,
        "TSPC__HasSharedScore__c": False,
    }
    client.fetch_closeplan_template_answers.return_value = UIAPIRecordCollection(
        records=[], reported_count=None, complete=False, issues=[]
    )

    with (
        patch("fieldkit.commands.sf.meddpicc.get_sf_session_id", return_value="fake-sid"),
        patch("fieldkit.commands.sf.meddpicc.get_sf_rest_base_url", return_value="https://org.my.salesforce.com"),
        patch("fieldkit.sf.client.SFDirectClient", return_value=cm),
    ):
        result = fetch_meddpicc_scorecard(_OPP_ID)

    assert result["status"] == "incomplete"
    assert result["complete"] is False
    deal = result["deals"][0]
    assert deal["questions_complete"] is False
    normalized_question = deal["elements"][1]["questions"][0]
    assert normalized_question["template_metadata_status"] == "incomplete"
    template = normalized_question["template_question"]
    assert template is not None
    assert template["answer_choices_complete"] is False


def test_cli_human_output_preserves_fractional_zero_and_null_native_numbers() -> None:
    question = _question("METRICS", name="METRICS - Outcome", score=2.5)
    question["score_maximum"] = None
    question["template_metadata_status"] = "complete"
    question["template_question"] = {
        "template_question_id": "a2G000000000001AAA",
        "name": "Template question",
        "template_id": "a2T000000000001AAA",
        "category_id": "a1o000000000001AAA",
        "question_category_id": "a1u000000000001AAA",
        "question_type": "Answers",
        "has_text_answer": False,
        "score_maximum": 4.25,
        "sync_score_field": None,
        "sync_score_ratio_field": None,
        "has_shared_score": False,
        "answer_model": "choice",
        "answer_choices_reported_count": 3,
        "answer_choices_complete": True,
        "answer_choices": [
            {
                "answer_id": "a2H000000000001AAA",
                "name": "Zero",
                "text": None,
                "attitude": None,
                "has_text_answer": False,
                "max_score": 0.0,
                "sort_order": 0.0,
                "sync_text_answer_field": None,
                "sync_value_field_pickval": None,
                "issues": [],
            },
            {
                "answer_id": "a2H000000000002AAA",
                "name": "Fraction",
                "text": None,
                "attitude": None,
                "has_text_answer": False,
                "max_score": 4.25,
                "sort_order": 1.5,
                "sync_text_answer_field": None,
                "sync_value_field_pickval": None,
                "issues": [],
            },
            {
                "answer_id": "a2H000000000003AAA",
                "name": "Null",
                "text": None,
                "attitude": None,
                "has_text_answer": False,
                "max_score": None,
                "sort_order": None,
                "sync_text_answer_field": None,
                "sync_value_field_pickval": None,
                "issues": [],
            },
        ],
        "issues": [],
    }
    runner = CliRunner()
    with patch("fieldkit.commands.sf.meddpicc.fetch_meddpicc_scorecard", return_value=_scorecard([question])):
        result = runner.invoke(cli, [_OPP_ID])

    assert result.exit_code == 0
    assert "score=2.5" in result.output
    assert "Native maximum: (not set)" in result.output
    assert "Template maximum: 4.25" in result.output
    assert "[a2H000000000001AAA] Zero max=0 sort=0" in result.output
    assert "[a2H000000000002AAA] Fraction max=4.25 sort=1.5" in result.output
    assert "[a2H000000000003AAA] Null max=(not set) sort=(not set)" in result.output


@pytest.mark.parametrize(
    "native_maximum",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="infinity"),
        pytest.param(10**400, id="overflow"),
    ],
)
def test_cli_human_output_renders_nonfinite_or_overflow_native_maximum_explicitly(native_maximum: float | int) -> None:
    question = _question("METRICS", name="METRICS - Outcome", score=2.0)
    question["score_maximum"] = native_maximum
    runner = CliRunner()
    with patch("fieldkit.commands.sf.meddpicc.fetch_meddpicc_scorecard", return_value=_scorecard([question])):
        result = runner.invoke(cli, [_OPP_ID])

    assert result.exit_code == 0
    assert f"Native maximum: {native_maximum}" in result.output
