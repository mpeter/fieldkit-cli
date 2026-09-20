"""Read and normalize exact native ClosePlan template-question metadata."""

import math
from collections import Counter
from dataclasses import dataclass
from typing import Any, Final, Literal

from fieldkit.sf.client import SFAPIError, SFDataAccessError, SFDirectClient, SFNotFoundError
from fieldkit.sf.closeplan_evidence import resolve_record_identity, ui_api_field_issues
from fieldkit.sf.types import ClosePlanAnswerChoice, ClosePlanTemplate, ClosePlanTemplateQuestion, UIAPIRecordCollection

_TEMPLATE_FIELDS: Final = (
    "Id",
    "TSPC__Version__c",
    "TSPC__VersionName__c",
    "TSPC__Type__c",
    "TSPC__Status__c",
    "TSPC__SC_TotalMaxScore__c",
)

_TEMPLATE_QUESTION_FIELDS: Final = (
    "Id",
    "Name",
    "TSPC__Template__c",
    "TSPC__Category__c",
    "TSPC__QuestionCategory__c",
    "TSPC__Mode__c",
    "TSPC__HasTextAnswer__c",
    "TSPC__MaxScore__c",
    "TSPC__Sync_ScoreField__c",
    "TSPC__Sync_ScoreRatioField__c",
    "TSPC__HasSharedScore__c",
)
_TEMPLATE_ANSWER_FIELDS: Final = (
    "Name",
    "TSPC__Text__c",
    "TSPC__Attitude__c",
    "TSPC__HasTextAnswer__c",
    "TSPC__MaxScore__c",
    "TSPC__SortOrder__c",
    "TSPC__Sync_TextAnswerField__c",
    "TSPC__Sync_ValueFieldPickval__c",
)


@dataclass(frozen=True)
class TemplateQuestionRead:
    """One exact template-question read or its explicit incomplete evidence."""

    metadata: ClosePlanTemplateQuestion | None
    issues: list[str]


@dataclass(frozen=True)
class TemplateRead:
    """One exact template read or its explicit incomplete evidence."""

    metadata: ClosePlanTemplate | None
    issues: list[str]


def _field_value(record: dict[str, Any], field_name: str) -> Any:
    """Extract a raw value from either a UI API or sObject REST record."""
    fields = record.get("fields")
    if isinstance(fields, dict) and field_name in fields:
        field = fields[field_name]
        if isinstance(field, dict):
            return field.get("value")
        return field
    return record.get(field_name)


def _invalid_scalar_issue(subject: str, field_name: str, expected: str, value: object) -> str:
    return f"{subject} field {field_name} has invalid {type(value).__name__}; expected {expected} or null"


def _optional_string(
    value: object,
    *,
    subject: str,
    field_name: str,
) -> tuple[str | None, list[str]]:
    if value is None:
        return None, []
    if isinstance(value, str):
        return value, []
    return None, [_invalid_scalar_issue(subject, field_name, "string", value)]


def _optional_bool(
    value: object,
    *,
    subject: str,
    field_name: str,
) -> tuple[bool | None, list[str]]:
    if value is None:
        return None, []
    if isinstance(value, bool):
        return value, []
    return None, [_invalid_scalar_issue(subject, field_name, "boolean", value)]


def _optional_float(
    value: object,
    *,
    subject: str,
    field_name: str,
) -> tuple[float | None, list[str]]:
    if value is None:
        return None, []
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, [_invalid_scalar_issue(subject, field_name, "number", value)]
    try:
        numeric = float(value)
    except OverflowError:
        return None, [_invalid_scalar_issue(subject, field_name, "finite number", value)]
    if not math.isfinite(numeric):
        return None, [_invalid_scalar_issue(subject, field_name, "finite number", value)]
    return numeric, []


def _optional_reference(
    value: object,
    *,
    subject: str,
    field_name: str,
) -> tuple[str | None, list[str]]:
    if value is None:
        return None, []
    reference_id, identity_issues = resolve_record_identity({"id": value}, subject=subject)
    if identity_issues:
        return None, [_invalid_scalar_issue(subject, field_name, "Salesforce Id", value)]
    return reference_id, []


def _exact_reference_id(value: object, *, subject: str) -> str | None:
    if value is None:
        return None
    reference_id, _issues = resolve_record_identity({"id": value}, subject=subject)
    return reference_id


def _sobject_field_issues(record: dict[str, Any], field_names: tuple[str, ...], *, subject: str) -> list[str]:
    return [f"{subject} field {field_name} is missing" for field_name in field_names if field_name not in record]


def _normalize_answer_choice(record: dict[str, Any], *, index: int) -> ClosePlanAnswerChoice:
    answer_id, identity_issues = resolve_record_identity(record, subject=f"template answer record {index}")
    subject = f"template answer {answer_id or f'record {index}'}"
    name, name_issues = _optional_string(_field_value(record, "Name"), subject=subject, field_name="Name")
    text, text_issues = _optional_string(
        _field_value(record, "TSPC__Text__c"), subject=subject, field_name="TSPC__Text__c"
    )
    attitude, attitude_issues = _optional_string(
        _field_value(record, "TSPC__Attitude__c"), subject=subject, field_name="TSPC__Attitude__c"
    )
    has_text_answer, has_text_answer_issues = _optional_bool(
        _field_value(record, "TSPC__HasTextAnswer__c"),
        subject=subject,
        field_name="TSPC__HasTextAnswer__c",
    )
    max_score, max_score_issues = _optional_float(
        _field_value(record, "TSPC__MaxScore__c"), subject=subject, field_name="TSPC__MaxScore__c"
    )
    sort_order, sort_order_issues = _optional_float(
        _field_value(record, "TSPC__SortOrder__c"), subject=subject, field_name="TSPC__SortOrder__c"
    )
    sync_text_answer_field, sync_text_answer_field_issues = _optional_string(
        _field_value(record, "TSPC__Sync_TextAnswerField__c"),
        subject=subject,
        field_name="TSPC__Sync_TextAnswerField__c",
    )
    sync_value_field_pickval, sync_value_field_pickval_issues = _optional_string(
        _field_value(record, "TSPC__Sync_ValueFieldPickval__c"),
        subject=subject,
        field_name="TSPC__Sync_ValueFieldPickval__c",
    )
    issues = [
        *identity_issues,
        *ui_api_field_issues(
            record,
            _TEMPLATE_ANSWER_FIELDS,
            subject=subject,
        ),
        *name_issues,
        *text_issues,
        *attitude_issues,
        *has_text_answer_issues,
        *max_score_issues,
        *sort_order_issues,
        *sync_text_answer_field_issues,
        *sync_value_field_pickval_issues,
    ]
    return {
        "answer_id": answer_id,
        "name": name,
        "text": text,
        "attitude": attitude,
        "has_text_answer": has_text_answer,
        "max_score": max_score,
        "sort_order": sort_order,
        "sync_text_answer_field": sync_text_answer_field,
        "sync_value_field_pickval": sync_value_field_pickval,
        "issues": issues,
    }


def _normalize_template_question(
    record: dict[str, Any],
    answers: UIAPIRecordCollection,
    *,
    expected_id: str,
) -> TemplateQuestionRead:
    actual_id, identity_issues = resolve_record_identity({"id": record.get("Id")}, subject="template question")
    subject = f"template question {expected_id}"
    name, name_issues = _optional_string(record.get("Name"), subject=subject, field_name="Name")
    template_id, template_id_issues = _optional_reference(
        record.get("TSPC__Template__c"), subject=subject, field_name="TSPC__Template__c"
    )
    category_id, category_id_issues = _optional_reference(
        record.get("TSPC__Category__c"), subject=subject, field_name="TSPC__Category__c"
    )
    question_category_id, question_category_id_issues = _optional_reference(
        record.get("TSPC__QuestionCategory__c"), subject=subject, field_name="TSPC__QuestionCategory__c"
    )
    question_type, question_type_issues = _optional_string(
        record.get("TSPC__Mode__c"), subject=subject, field_name="TSPC__Mode__c"
    )
    has_text_answer, has_text_answer_issues = _optional_bool(
        record.get("TSPC__HasTextAnswer__c"), subject=subject, field_name="TSPC__HasTextAnswer__c"
    )
    score_maximum, score_maximum_issues = _optional_float(
        record.get("TSPC__MaxScore__c"), subject=subject, field_name="TSPC__MaxScore__c"
    )
    sync_score_field, sync_score_field_issues = _optional_string(
        record.get("TSPC__Sync_ScoreField__c"), subject=subject, field_name="TSPC__Sync_ScoreField__c"
    )
    sync_score_ratio_field, sync_score_ratio_field_issues = _optional_string(
        record.get("TSPC__Sync_ScoreRatioField__c"),
        subject=subject,
        field_name="TSPC__Sync_ScoreRatioField__c",
    )
    has_shared_score, has_shared_score_issues = _optional_bool(
        record.get("TSPC__HasSharedScore__c"), subject=subject, field_name="TSPC__HasSharedScore__c"
    )
    issues = [
        *identity_issues,
        *_sobject_field_issues(record, _TEMPLATE_QUESTION_FIELDS, subject=subject),
        *answers.issues,
        *name_issues,
        *template_id_issues,
        *category_id_issues,
        *question_category_id_issues,
        *question_type_issues,
        *has_text_answer_issues,
        *score_maximum_issues,
        *sync_score_field_issues,
        *sync_score_ratio_field_issues,
        *has_shared_score_issues,
    ]
    if actual_id is not None and actual_id != expected_id:
        issues.append(f"template question identity mismatch: requested {expected_id}, received {actual_id}")

    choices = [_normalize_answer_choice(answer, index=index) for index, answer in enumerate(answers.records, start=1)]
    issues.extend(issue for choice in choices for issue in choice["issues"])
    choice_ids = [choice["answer_id"] for choice in choices if choice["answer_id"] is not None]
    duplicate_ids = sorted(choice_id for choice_id, count in Counter(choice_ids).items() if count > 1)
    issues.extend(f"duplicate template answer Id {choice_id} appears in the collection" for choice_id in duplicate_ids)

    complete = answers.complete and not issues
    if not complete:
        answer_model: Literal["choice", "text", "unsupported", "incomplete"] = "incomplete"
    elif choices:
        answer_model = "choice"
    elif has_text_answer is True:
        answer_model = "text"
    else:
        answer_model = "unsupported"

    return TemplateQuestionRead(
        metadata={
            "template_question_id": expected_id,
            "name": name,
            "template_id": template_id,
            "category_id": category_id,
            "question_category_id": question_category_id,
            "question_type": question_type,
            "has_text_answer": has_text_answer,
            "score_maximum": score_maximum,
            "sync_score_field": sync_score_field,
            "sync_score_ratio_field": sync_score_ratio_field,
            "has_shared_score": has_shared_score,
            "answer_model": answer_model,
            "answer_choices_reported_count": answers.reported_count,
            "answer_choices_complete": complete,
            "answer_choices": choices,
            "issues": issues,
        },
        issues=issues,
    )


def _empty_answers(issue: str) -> UIAPIRecordCollection:
    return UIAPIRecordCollection(records=[], reported_count=None, complete=False, issues=[issue])


def _normalize_template(record: dict[str, Any], *, expected_id: str) -> TemplateRead:
    """Normalize one exact template while preserving nullable native metadata."""
    actual_id, identity_issues = resolve_record_identity({"id": record.get("Id")}, subject="template")
    subject = f"template {expected_id}"
    version, version_issues = _optional_float(
        record.get("TSPC__Version__c"), subject=subject, field_name="TSPC__Version__c"
    )
    version_name, version_name_issues = _optional_string(
        record.get("TSPC__VersionName__c"), subject=subject, field_name="TSPC__VersionName__c"
    )
    template_type, template_type_issues = _optional_string(
        record.get("TSPC__Type__c"), subject=subject, field_name="TSPC__Type__c"
    )
    status, status_issues = _optional_string(
        record.get("TSPC__Status__c"), subject=subject, field_name="TSPC__Status__c"
    )
    total_maximum, total_maximum_issues = _optional_float(
        record.get("TSPC__SC_TotalMaxScore__c"),
        subject=subject,
        field_name="TSPC__SC_TotalMaxScore__c",
    )
    issues = [
        *identity_issues,
        *_sobject_field_issues(record, _TEMPLATE_FIELDS, subject=subject),
        *version_issues,
        *version_name_issues,
        *template_type_issues,
        *status_issues,
        *total_maximum_issues,
    ]
    if actual_id is not None and actual_id != expected_id:
        issues.append(f"template identity mismatch: requested {expected_id}, received {actual_id}")
    return TemplateRead(
        metadata={
            "template_id": expected_id,
            "version": version,
            "version_name": version_name,
            "template_type": template_type,
            "status": status,
            "total_maximum": total_maximum,
            "issues": issues,
        },
        issues=issues,
    )


def read_templates(client: SFDirectClient, template_ids: list[str]) -> dict[str, TemplateRead]:
    """Read each exact ClosePlan template once, preserving failures as evidence."""
    results: dict[str, TemplateRead] = {}
    for template_id in dict.fromkeys(template_ids):
        try:
            record = client.fetch_closeplan_template(template_id)
        except (SFAPIError, SFDataAccessError, SFNotFoundError) as exc:
            results[template_id] = TemplateRead(
                metadata=None,
                issues=[f"template {template_id} metadata unavailable: {exc}"],
            )
            continue
        results[template_id] = _normalize_template(record, expected_id=template_id)
    return results


def _template_question_ids(question_collections: list[UIAPIRecordCollection]) -> list[str]:
    unique_ids: dict[str, None] = {}
    for collection in question_collections:
        for question in collection.records:
            template_question_id = _exact_reference_id(
                _field_value(question, "TSPC__TemplateQuestion__c"), subject="template-question reference"
            )
            if template_question_id is not None:
                unique_ids.setdefault(template_question_id, None)
    return list(unique_ids)


def read_template_questions(
    client: SFDirectClient,
    question_collections: list[UIAPIRecordCollection],
) -> dict[str, TemplateQuestionRead]:
    """Read each unique template question and its exact answer collection once."""
    results: dict[str, TemplateQuestionRead] = {}
    # implementation note: UI API exposes TSPC__Answers__r only beneath one exact template
    # question, and no supported batch seam exists for these child relationships.
    # Deduplication therefore bounds this to one child read per unique exact ID.
    for template_question_id in _template_question_ids(question_collections):
        try:
            record = client.fetch_closeplan_template_question(template_question_id)
        except (SFAPIError, SFDataAccessError, SFNotFoundError) as exc:
            results[template_question_id] = TemplateQuestionRead(
                metadata=None,
                issues=[f"template question {template_question_id} metadata unavailable: {exc}"],
            )
            continue
        try:
            answers = client.fetch_closeplan_template_answers(template_question_id)
        except (SFAPIError, SFDataAccessError, SFNotFoundError) as exc:
            answers = _empty_answers(f"template answer metadata unavailable: {exc}")
        results[template_question_id] = _normalize_template_question(record, answers, expected_id=template_question_id)
    return results
