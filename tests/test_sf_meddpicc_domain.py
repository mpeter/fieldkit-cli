"""Contract tests for normalized ClosePlan MEDDPICC data."""

from unittest.mock import MagicMock

import pytest

from fieldkit.sf.closeplan_template import read_template_questions
from fieldkit.sf.meddpicc import (
    CANONICAL_ELEMENTS,
    normalize_field_metadata,
    normalize_question,
    normalize_questions,
)
from fieldkit.sf.types import MeddpiccQuestion, UIAPIRecordCollection

pytestmark = pytest.mark.unit


def _question(category: str | None, *, score: float | None = None, answer: str | None = None) -> MeddpiccQuestion:
    return {
        "question_id": "a1E000000000001AAA",
        "category": category,
        "name": f"{category or 'OTHER'} - Question",
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


def test_normalize_questions_emits_every_element_in_canonical_order() -> None:
    result = normalize_questions([_question("CHAMPION", score=2)])

    assert [element["key"] for element in result.elements] == [key for key, _label in CANONICAL_ELEMENTS]
    assert result.elements[5]["label"] == "Champion"
    assert result.elements[5]["state"] == "scored"


@pytest.mark.parametrize(
    ("questions", "expected_state", "is_gap"),
    [
        pytest.param([], "unpopulated", True, id="blank"),
        pytest.param([_question("METRICS", answer="Revenue target")], "answered_unscored", False, id="answer-only"),
        pytest.param([_question("METRICS", score=0)], "scored_zero", False, id="zero"),
        pytest.param([_question("METRICS", score=2)], "scored", False, id="positive"),
    ],
)
def test_normalize_questions_distinguishes_population_states(
    questions: list[MeddpiccQuestion], expected_state: str, is_gap: bool
) -> None:
    result = normalize_questions(questions)
    metrics = result.elements[0]

    assert metrics["state"] == expected_state
    assert ("metrics" in result.gaps) is is_gap


def test_normalize_questions_preserves_zero_and_named_party_answers() -> None:
    result = normalize_questions(
        [
            _question("CHAMPION", score=0, answer="Alex Example is the named champion"),
            _question("ECONOMIC BUYER", answer="Jordan Example controls the budget"),
        ]
    )

    champion = result.elements[5]
    economic_buyer = result.elements[1]
    assert champion["questions"][0].get("score") == 0
    assert champion["questions"][0].get("answer") == "Alex Example is the named champion"
    assert economic_buyer["questions"][0].get("answer") == "Jordan Example controls the budget"


def test_normalize_questions_matches_categories_case_insensitively() -> None:
    result = normalize_questions([_question("Identify Pain", score=1)])

    identify_pain = result.elements[4]
    assert identify_pain["key"] == "identify_pain"
    assert identify_pain["state"] == "scored"


def test_normalize_questions_retains_unknown_and_missing_categories() -> None:
    questions = [_question("CUSTOM SIGNAL", answer="Keep me"), _question(None, score=1)]
    result = normalize_questions(questions)

    assert result.unmapped == questions


def test_normalize_question_preserves_exact_identity_raw_fields_and_concurrency() -> None:
    record = {
        "id": "a1E000000000001AAA",
        "fields": {
            "Id": {"value": "a1E000000000001AAA"},
            "Name": {"value": "METRICS - Quantifiable business outcome"},
            "TSPC__Score__c": {"value": 6},
            "TSPC__TextAnswer__c": {"value": None},
            "TSPC__RichTextAnswer__c": {"value": "&lt;p&gt;20% faster&lt;/p&gt;"},
            "LastModifiedDate": {"value": "2026-09-12T12:34:56.000Z"},
        },
    }

    result = normalize_question(record, field_metadata={})

    assert result["question_id"] == "a1E000000000001AAA"
    assert result["raw_score"] == 6
    assert type(result["raw_score"]) is int
    assert result["raw_text_answer"] is None
    assert result["raw_rich_text_answer"] == "&lt;p&gt;20% faster&lt;/p&gt;"
    assert result["answer"] == "20% faster"
    assert result["last_modified_date"] == "2026-09-12T12:34:56.000Z"
    assert result["concurrency"]["field"] == "LastModifiedDate"
    assert result["concurrency"]["conditional_header"] == "If-Unmodified-Since"
    assert result["concurrency"]["value"] == "2026-09-12T12:34:56.000Z"
    assert result["concurrency"]["strength"] == "weak_timestamp"
    assert result["concurrency"]["mutation_enabled"] is False


def test_normalize_field_metadata_keeps_only_proven_generic_describe_properties() -> None:
    describe = {
        "fields": [
            {
                "name": "TSPC__Score__c",
                "type": "double",
                "updateable": True,
                "calculated": False,
                "precision": 18,
                "scale": 2,
                "length": 0,
                "picklistValues": [{"active": True, "value": "6"}, {"active": False, "value": "retired"}],
                "inlineHelpText": "Not part of the normalized contract",
            }
        ]
    }

    result = normalize_field_metadata(describe, ("TSPC__Score__c", "TSPC__TextAnswer__c"))

    assert result["TSPC__Score__c"] == {
        "api_name": "TSPC__Score__c",
        "type": "double",
        "updateable": True,
        "calculated": False,
        "precision": 18,
        "scale": 2,
        "length": 0,
        "picklist_values": ["6"],
    }
    assert result["TSPC__TextAnswer__c"] == {
        "api_name": "TSPC__TextAnswer__c",
        "type": None,
        "updateable": None,
        "calculated": None,
        "precision": None,
        "scale": None,
        "length": None,
        "picklist_values": None,
    }


def test_normalize_question_does_not_guess_package_specific_metadata() -> None:
    result = normalize_question(
        {
            "fields": {
                "Id": {"value": "a1E000000000001AAA"},
                "Name": {"value": "METRICS - Question"},
                "TSPC__Score__c": {"value": 6},
                "TSPC__QuestionType__c": {"value": "Score"},
                "TSPC__Maximum__c": {"value": 10},
                "TSPC__Weight__c": {"value": 2},
                "TSPC__Template__c": {"value": "a1T000000000001AAA"},
            }
        },
        field_metadata={},
    )

    assert result["question_type"] is None
    assert result["score_maximum"] is None
    assert result["weight"] is None
    assert result["template_question_id"] is None
    assert result["template_metadata_status"] == "incomplete"
    assert result["template_question"] is None
    assert result["metadata_issues"] == ["question a1E000000000001AAA field TSPC__TemplateQuestion__c is missing"]
    assert result["template_version"] is None
    assert result["metadata_gaps"] == [
        "question_type",
        "score_maximum",
        "template_version",
    ]


def test_normalize_question_distinguishes_null_from_malformed_template_reference() -> None:
    question_id = "a1E000000000001AAA"
    null_result = normalize_question(
        {
            "fields": {
                "Id": {"value": question_id},
                "TSPC__TemplateQuestion__c": {"value": None},
            }
        },
        field_metadata={},
    )
    malformed_result = normalize_question(
        {
            "fields": {
                "Id": {"value": question_id},
                "TSPC__TemplateQuestion__c": {"displayValue": "Template"},
            }
        },
        field_metadata={},
    )

    assert null_result["template_metadata_status"] == "absent"
    assert null_result["metadata_issues"] == []
    assert malformed_result["template_metadata_status"] == "incomplete"
    assert malformed_result["metadata_issues"] == [
        f"question {question_id} field TSPC__TemplateQuestion__c has no value envelope"
    ]


def _template_record() -> dict[str, object]:
    return {
        "Id": "a2G000000000001AAA",
        "Name": "Template question",
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


def _template_answer_record() -> dict[str, object]:
    return {
        "id": "a2H000000000001AAA",
        "fields": {
            "Id": {"value": "a2H000000000001AAA"},
            "Name": {"value": "Choice"},
            "TSPC__Text__c": {"value": "Choice text"},
            "TSPC__Attitude__c": {"value": None},
            "TSPC__HasTextAnswer__c": {"value": False},
            "TSPC__MaxScore__c": {"value": 2.0},
            "TSPC__SortOrder__c": {"value": 0.0},
            "TSPC__Sync_TextAnswerField__c": {"value": None},
            "TSPC__Sync_ValueFieldPickval__c": {"value": None},
        },
    }


@pytest.mark.parametrize(
    ("scope", "field_name", "invalid_value"),
    [
        pytest.param("template", "Name", 7, id="template-string"),
        pytest.param("template", "TSPC__Template__c", 7, id="template-reference"),
        pytest.param("template", "TSPC__HasTextAnswer__c", "false", id="template-boolean"),
        pytest.param("template", "TSPC__MaxScore__c", "6", id="template-number"),
        pytest.param("template", "TSPC__MaxScore__c", float("nan"), id="template-number-nan"),
        pytest.param("template", "TSPC__MaxScore__c", float("inf"), id="template-number-infinity"),
        pytest.param("template", "TSPC__MaxScore__c", 10**400, id="template-number-overflow"),
        pytest.param("answer", "TSPC__Text__c", 7, id="answer-string"),
        pytest.param("answer", "TSPC__HasTextAnswer__c", "false", id="answer-boolean"),
        pytest.param("answer", "TSPC__MaxScore__c", "2", id="answer-number"),
        pytest.param("answer", "TSPC__MaxScore__c", float("-inf"), id="answer-number-negative-infinity"),
    ],
)
def test_template_metadata_rejects_non_null_invalid_scalar_types(
    scope: str, field_name: str, invalid_value: object
) -> None:
    template = _template_record()
    answer = _template_answer_record()
    if scope == "template":
        template[field_name] = invalid_value
    else:
        fields = answer["fields"]
        assert isinstance(fields, dict)
        fields[field_name] = {"value": invalid_value}
    client = MagicMock()
    client.fetch_closeplan_template_question.return_value = template
    client.fetch_closeplan_template_answers.return_value = UIAPIRecordCollection(
        records=[answer], reported_count=1, complete=True, issues=[]
    )
    questions = UIAPIRecordCollection(
        records=[{"fields": {"TSPC__TemplateQuestion__c": {"value": "a2G000000000001AAA"}}}],
        reported_count=1,
        complete=True,
        issues=[],
    )

    result = read_template_questions(client, [questions])

    metadata = result["a2G000000000001AAA"]
    assert len(metadata.issues) == 1
    assert any(field_name in issue and "invalid" in issue for issue in metadata.issues)
    assert metadata.metadata is not None
    assert metadata.metadata["answer_model"] == "incomplete"


def test_normalize_questions_keeps_duplicate_presentation_text_as_exact_records() -> None:
    first = _question("METRICS", score=6)
    second = _question("METRICS")
    first["question_id"] = "a1E000000000001AAA"
    second["question_id"] = "a1E000000000002AAA"
    first["name"] = second["name"] = "METRICS - Duplicate wording"

    result = normalize_questions([first, second])

    metrics = result.elements[0]
    assert [question["question_id"] for question in metrics["questions"]] == [
        "a1E000000000001AAA",
        "a1E000000000002AAA",
    ]
    assert metrics["state"] == "scored"
    assert metrics["complete"] is False
    assert metrics["unanswered_question_ids"] == ["a1E000000000002AAA"]
