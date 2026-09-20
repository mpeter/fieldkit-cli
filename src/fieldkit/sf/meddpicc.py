"""Normalize read-only ClosePlan records into the native MEDDPICC contract."""

import html
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Final, Literal

from fieldkit.sf.client import SFAPIError, SFDataAccessError, SFDirectClient, SFNotFoundError
from fieldkit.sf.closeplan_evidence import (
    cross_deal_question_issues,
    deal_identity_evidence,
    resolve_record_identity,
    ui_api_field_issues,
)
from fieldkit.sf.closeplan_template import TemplateQuestionRead, TemplateRead, read_template_questions, read_templates
from fieldkit.sf.types import (
    ClosePlanTemplate,
    ClosePlanTemplateQuestion,
    ConcurrencyEvidence,
    MeddpiccDeal,
    MeddpiccElement,
    MeddpiccQuestion,
    MeddpiccReadResult,
    SalesforceFieldMetadata,
    UIAPIRecordCollection,
)

MeddpiccState = Literal["unpopulated", "answered_unscored", "scored_zero", "scored"]
MeddpiccReadStatus = Literal["complete", "not_found", "ambiguous", "invalid_selection", "incomplete"]

CANONICAL_ELEMENTS: Final[tuple[tuple[str, str], ...]] = (
    ("metrics", "Metrics"),
    ("economic_buyer", "Economic Buyer"),
    ("decision_criteria", "Decision Criteria"),
    ("decision_process", "Decision Process"),
    ("identify_pain", "Identify Pain"),
    ("champion", "Champion"),
    ("competition", "Competition"),
    ("paper_process", "Paper Process"),
)
_CATEGORY_KEYS: Final = {label.casefold(): key for key, label in CANONICAL_ELEMENTS}
_CATEGORY_SEPARATOR_RE: Final = re.compile(r"\s+[-\u2013\u2014]\s+")
_HTML_TAG_RE: Final = re.compile(r"<[^>]+>")
_METADATA_GAP_ORDER: Final = (
    "question_type",
    "score_maximum",
    "template_version",
)
_TEMPLATE_QUESTION_REFERENCE_FIELD: Final = "TSPC__TemplateQuestion__c"
QUESTION_FIELD_API_NAMES: Final = (
    "TSPC__Score__c",
    "TSPC__TextAnswer__c",
    "TSPC__RichTextAnswer__c",
    "TSPC__Answer__c",
    "TSPC__MaxScore__c",
    "TSPC__HasTextAnswer__c",
    _TEMPLATE_QUESTION_REFERENCE_FIELD,
    "LastModifiedDate",
)
DEAL_FIELD_API_NAMES: Final = (
    "Name",
    "TSPC__ScorecardScoreRatio__c",
    "TSPC__ScorecardTotalScore__c",
    "TSPC__Template__c",
    "TSPC__TemplateDeployDate__c",
    "LastModifiedDate",
)


@dataclass(frozen=True)
class NormalizedMeddpicc:
    """Canonical elements, absent-data gaps, and unrecognized questions."""

    elements: list[MeddpiccElement]
    gaps: list[str]
    unmapped: list[MeddpiccQuestion]


def field_value(record: dict[str, Any], field_name: str) -> Any:
    """Extract a raw value from either a UI API or sObject REST record."""
    fields = record.get("fields")
    if isinstance(fields, dict) and field_name in fields:
        field = fields[field_name]
        if isinstance(field, dict):
            return field.get("value")
        return field
    return record.get(field_name)


def extract_category(name: object) -> str | None:
    """Return a presentation category only when the question prefix proves one."""
    if not isinstance(name, str) or not name:
        return None
    parts = _CATEGORY_SEPARATOR_RE.split(name, maxsplit=1)
    if len(parts) != 2:
        return None
    category = parts[0].strip()
    return category or None


def _string_or_none(value: object) -> str | None:
    """Return string values without coercing other Salesforce field types."""
    return value if isinstance(value, str) else None


def _bool_or_none(value: object) -> bool | None:
    """Return native booleans without treating numeric truthiness as evidence."""
    return value if isinstance(value, bool) else None


def _float_or_none(value: object) -> float | None:
    """Return finite Salesforce numeric values while rejecting booleans."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        numeric = float(value)
    except OverflowError:
        return None
    return numeric if math.isfinite(numeric) else None


def _invalid_optional_scalar_issues(
    value: object,
    normalized: object | None,
    *,
    subject: str,
    field_name: str,
    expected: str,
) -> list[str]:
    """Report a non-null native scalar that cannot be normalized exactly."""
    if value is None or normalized is not None:
        return []
    return [f"{subject} field {field_name} has invalid {type(value).__name__}; expected {expected} or null"]


def _exact_reference_id(value: object, *, subject: str) -> tuple[str | None, list[str]]:
    """Validate one Salesforce reference using the shared record-ID rules."""
    if value is None:
        return None, []
    return resolve_record_identity({"id": value}, subject=subject)


def extract_answer(record: dict[str, Any]) -> str | None:
    """Render an answer while leaving both native raw answer fields untouched."""
    text = field_value(record, "TSPC__TextAnswer__c")
    if isinstance(text, str) and text.strip():
        return text.strip()
    rich = field_value(record, "TSPC__RichTextAnswer__c")
    if isinstance(rich, str) and rich.strip():
        return _HTML_TAG_RE.sub("", html.unescape(rich)).strip()
    return None


def _optional_property(field: dict[str, Any], name: str, expected_type: type[Any]) -> Any:
    """Return a describe property only when its runtime type is proven."""
    value = field.get(name)
    if expected_type is int and isinstance(value, bool):
        return None
    return value if isinstance(value, expected_type) else None


def _picklist_values(field: dict[str, Any]) -> list[str] | None:
    """Return active describe picklist values, preserving source order."""
    raw_values = field.get("picklistValues")
    if not isinstance(raw_values, list):
        return None
    values: list[str] = []
    for entry in raw_values:
        if not isinstance(entry, dict) or entry.get("active") is not True:
            continue
        value = entry.get("value")
        if isinstance(value, str):
            values.append(value)
    return values


def normalize_field_metadata(
    describe: dict[str, Any], field_api_names: tuple[str, ...]
) -> dict[str, SalesforceFieldMetadata]:
    """Select only generic field properties established by Salesforce describe."""
    raw_fields = describe.get("fields")
    fields_by_name: dict[str, dict[str, Any]] = {}
    if isinstance(raw_fields, list):
        for field in raw_fields:
            if isinstance(field, dict) and isinstance(name := field.get("name"), str):
                fields_by_name[name] = field

    normalized: dict[str, SalesforceFieldMetadata] = {}
    for api_name in field_api_names:
        field = fields_by_name.get(api_name, {})
        normalized[api_name] = {
            "api_name": api_name,
            "type": _optional_property(field, "type", str),
            "updateable": _optional_property(field, "updateable", bool),
            "calculated": _optional_property(field, "calculated", bool),
            "precision": _optional_property(field, "precision", int),
            "scale": _optional_property(field, "scale", int),
            "length": _optional_property(field, "length", int),
            "picklist_values": _picklist_values(field),
        }
    return normalized


def normalize_question(
    record: dict[str, Any], *, field_metadata: dict[str, SalesforceFieldMetadata]
) -> MeddpiccQuestion:
    """Preserve exact native question identity, raw values, and read version."""
    question_id, _identity_issues = resolve_record_identity(record, subject="question")
    return _normalize_question(record, field_metadata=field_metadata, question_id=question_id)


def _normalize_question(
    record: dict[str, Any],
    *,
    field_metadata: dict[str, SalesforceFieldMetadata],
    question_id: str | None,
    template_questions: dict[str, TemplateQuestionRead] | None = None,
) -> MeddpiccQuestion:
    """Normalize one question using an independently resolved exact identity."""
    raw_name = field_value(record, "Name")
    name = html.unescape(raw_name) if isinstance(raw_name, str) else None
    raw_score = field_value(record, "TSPC__Score__c")
    score = _float_or_none(raw_score)
    raw_answer_value = field_value(record, "TSPC__Answer__c")
    raw_answer = _string_or_none(raw_answer_value)
    has_text_answer_value = field_value(record, "TSPC__HasTextAnswer__c")
    has_text_answer = _bool_or_none(has_text_answer_value)
    score_maximum_value = field_value(record, "TSPC__MaxScore__c")
    score_maximum = _float_or_none(score_maximum_value)
    question_subject = f"question {question_id or '(missing Id)'}"
    native_scalar_issues = [
        *_invalid_optional_scalar_issues(
            raw_answer_value,
            raw_answer,
            subject=question_subject,
            field_name="TSPC__Answer__c",
            expected="string",
        ),
        *_invalid_optional_scalar_issues(
            has_text_answer_value,
            has_text_answer,
            subject=question_subject,
            field_name="TSPC__HasTextAnswer__c",
            expected="boolean",
        ),
        *_invalid_optional_scalar_issues(
            score_maximum_value,
            score_maximum,
            subject=question_subject,
            field_name="TSPC__MaxScore__c",
            expected="finite number",
        ),
    ]
    raw_template_question_id = field_value(record, "TSPC__TemplateQuestion__c")
    template_envelope_issues = ui_api_field_issues(
        record,
        (_TEMPLATE_QUESTION_REFERENCE_FIELD,),
        subject=f"question {question_id or '(missing Id)'}",
    )
    template_question_id, template_reference_issues = _exact_reference_id(
        raw_template_question_id,
        subject=f"question {question_id or '(missing Id)'} template-question reference",
    )
    template_question: ClosePlanTemplateQuestion | None = None
    metadata_issues = [*template_envelope_issues, *template_reference_issues, *native_scalar_issues]
    template_metadata_status: Literal["complete", "absent", "incomplete"]
    if template_envelope_issues:
        template_metadata_status = "incomplete"
    elif raw_template_question_id is None:
        template_metadata_status = "absent"
    elif template_question_id is None:
        template_metadata_status = "incomplete"
    elif template_questions is None or template_question_id not in template_questions:
        template_metadata_status = "incomplete"
        metadata_issues.append(f"template question {template_question_id} was not read")
    else:
        template_read = template_questions[template_question_id]
        template_question = template_read.metadata
        metadata_issues.extend(template_read.issues)
        template_metadata_status = (
            "complete"
            if template_question is not None
            and template_question["answer_choices_complete"]
            and not template_read.issues
            else "incomplete"
        )
    last_modified_date = _string_or_none(field_value(record, "LastModifiedDate"))
    return {
        "question_id": question_id,
        "category": extract_category(name),
        "name": name,
        "score": score,
        "answer": extract_answer(record),
        "raw_score": raw_score,
        "raw_text_answer": _string_or_none(field_value(record, "TSPC__TextAnswer__c")),
        "raw_rich_text_answer": _string_or_none(field_value(record, "TSPC__RichTextAnswer__c")),
        "raw_answer": raw_answer,
        "has_text_answer": has_text_answer,
        "last_modified_date": last_modified_date,
        "concurrency": {
            "field": "LastModifiedDate",
            "value": last_modified_date,
            "conditional_header": "If-Unmodified-Since",
            "strength": "weak_timestamp",
            "mutation_enabled": False,
        },
        "field_metadata": field_metadata,
        "question_type": None,
        "score_maximum": score_maximum,
        "weight": None,
        "template_question_id": template_question_id,
        "template_metadata_status": template_metadata_status,
        "template_question": template_question,
        "template_version": None,
        "metadata_gaps": [
            gap
            for gap, missing in (
                ("question_type", True),
                ("score_maximum", score_maximum is None),
                ("template_version", True),
            )
            if missing
        ],
        "metadata_issues": metadata_issues,
    }


def _concurrency(last_modified_date: str | None) -> ConcurrencyEvidence:
    """Describe the weak timestamp precondition without enabling mutation."""
    return {
        "field": "LastModifiedDate",
        "value": last_modified_date,
        "conditional_header": "If-Unmodified-Since",
        "strength": "weak_timestamp",
        "mutation_enabled": False,
    }


def _metadata_for_read(client: SFDirectClient) -> tuple[dict[str, SalesforceFieldMetadata], list[str]]:
    """Read generic question-field metadata or retain a non-auth data issue."""
    try:
        describe = client.describe_sobject("TSPC__DealQuestion__c")
    except (SFAPIError, SFDataAccessError, SFNotFoundError) as exc:
        return normalize_field_metadata({}, QUESTION_FIELD_API_NAMES), [f"question field metadata unavailable: {exc}"]
    return normalize_field_metadata(describe, QUESTION_FIELD_API_NAMES), []


def _deal_question_metadata_issue(question: MeddpiccQuestion, issue: str) -> str:
    """Scope one question diagnostic to its owning deal without duplicating its field prefix."""
    subject = f"question {question['question_id'] or '(missing Id)'}"
    return issue if issue.startswith(f"{subject} field ") else f"{subject}: {issue}"


def _summarize_template_metadata(questions: list[MeddpiccQuestion]) -> tuple[bool, list[str]]:
    """Return question-template completeness and deal-scoped diagnostics."""
    issues = [
        _deal_question_metadata_issue(question, issue)
        for question in questions
        for issue in question["metadata_issues"]
    ]
    complete = all(question["template_metadata_status"] != "incomplete" for question in questions) and not issues
    return complete, issues


def _question_metadata_gaps(question: MeddpiccQuestion) -> list[str]:
    """Return only metadata that is required but unavailable for this question."""
    missing = {
        "question_type": question["question_type"] is None,
        "score_maximum": question["score_maximum"] is None,
        "template_version": question["template_version"] is None,
    }
    return [name for name in _METADATA_GAP_ORDER if missing[name]]


def _template_for_deal(
    template_id: str | None,
    templates: dict[str, TemplateRead],
) -> tuple[Literal["complete", "absent", "incomplete"], ClosePlanTemplate | None, list[str]]:
    """Resolve one deal template without treating an absent reference as corruption."""
    if template_id is None:
        return "absent", None, []
    if template_id not in templates:
        return "incomplete", None, [f"template {template_id} was not read"]
    template_read = templates[template_id]
    if template_read.metadata is None or template_read.issues:
        return "incomplete", template_read.metadata, list(template_read.issues)
    return "complete", template_read.metadata, []


def _bind_question_template_metadata(
    questions: list[MeddpiccQuestion],
    *,
    deal_template_id: str | None,
    template: ClosePlanTemplate | None,
) -> None:
    """Bind package mode/version and record exact template-ownership drift."""
    template_version = None if template is None else template["version"]
    for question in questions:
        template_question = question["template_question"]
        question["question_type"] = None if template_question is None else template_question["question_type"]
        question["template_version"] = template_version
        if template_question is not None and template_question["template_id"] != deal_template_id:
            question["template_metadata_status"] = "incomplete"
            question["metadata_issues"].append(
                f"template question belongs to template {template_question['template_id'] or '(missing Id)'}, "
                f"not deal template {deal_template_id or '(missing Id)'}"
            )
        question["metadata_gaps"] = _question_metadata_gaps(question)


def _maximum_consistency(
    questions: list[MeddpiccQuestion], template: ClosePlanTemplate | None
) -> tuple[float | None, bool | None, list[str]]:
    """Compare complete deployed maxima with the package-calculated template total."""
    maxima = [question["score_maximum"] for question in questions]
    if any(maximum is None for maximum in maxima):
        return None, None, []
    try:
        question_total = math.fsum(maximum for maximum in maxima if maximum is not None)
    except OverflowError:
        return None, None, ["question maximum total is not a finite number"]
    if not math.isfinite(question_total):
        return None, None, ["question maximum total is not a finite number"]
    template_total = None if template is None else template["total_maximum"]
    if template_total is None:
        return question_total, None, []
    return question_total, math.isclose(question_total, template_total, rel_tol=0.0, abs_tol=1e-9), []


def _template_score_metadata(template: ClosePlanTemplate | None) -> tuple[float | None, float | None]:
    """Project optional template scoring metadata into the deal contract."""
    if template is None:
        return None, None
    return template["version"], template["total_maximum"]


def _duplicate_question_issues(question_ids: list[str]) -> list[str]:
    """Report repeated exact identities without hiding either question."""
    duplicates = sorted(question_id for question_id, count in Counter(question_ids).items() if count > 1)
    return [f"duplicate question Id {question_id} appears in the collection" for question_id in duplicates]


def _question_field_issues(records: list[dict[str, Any]], identities: list[tuple[str | None, list[str]]]) -> list[str]:
    """Collect missing-field evidence for every exact question record."""
    issues: list[str] = []
    for index, (raw_question, (question_id, _record_issues)) in enumerate(
        zip(records, identities, strict=True), start=1
    ):
        issues.extend(
            ui_api_field_issues(
                raw_question,
                ("Name", *(field for field in QUESTION_FIELD_API_NAMES if field != _TEMPLATE_QUESTION_REFERENCE_FIELD)),
                subject=f"question {question_id or f'record {index}'}",
            )
        )
    return issues


def _normalize_deal(
    record: dict[str, Any],
    questions: UIAPIRecordCollection,
    field_metadata: dict[str, SalesforceFieldMetadata],
    template_questions: dict[str, TemplateQuestionRead],
    templates: dict[str, TemplateRead],
    *,
    deal_id: str | None,
    identity_issues: list[str],
) -> MeddpiccDeal:
    """Normalize one deal and retain all identity and completeness issues."""
    question_identities = [
        resolve_record_identity(question, subject=f"question record {index}")
        for index, question in enumerate(questions.records, start=1)
    ]
    normalized_questions = [
        _normalize_question(
            question,
            field_metadata=field_metadata,
            question_id=question_id,
            template_questions=template_questions,
        )
        for question, (question_id, _issues) in zip(questions.records, question_identities, strict=True)
    ]
    template_id, template_identity_issues = _exact_reference_id(
        field_value(record, "TSPC__Template__c"), subject=f"deal {deal_id or '(missing Id)'} template reference"
    )
    template_metadata_status, template, template_issues = _template_for_deal(template_id, templates)
    template_version, template_total_maximum = _template_score_metadata(template)
    _bind_question_template_metadata(
        normalized_questions,
        deal_template_id=template_id,
        template=template,
    )
    question_maximum_total, native_maximums_consistent, maximum_issues = (
        _maximum_consistency(normalized_questions, template) if questions.complete else (None, None, [])
    )
    normalized = normalize_questions(normalized_questions)
    raw_template_deploy_date = field_value(record, "TSPC__TemplateDeployDate__c")
    template_deploy_date = _string_or_none(raw_template_deploy_date)
    template_deploy_date_issues = _invalid_optional_scalar_issues(
        raw_template_deploy_date,
        template_deploy_date,
        subject=f"deal {deal_id or '(missing Id)'}",
        field_name="TSPC__TemplateDeployDate__c",
        expected="string",
    )
    issues = [
        *identity_issues,
        *ui_api_field_issues(record, DEAL_FIELD_API_NAMES, subject="deal"),
        *questions.issues,
        *template_deploy_date_issues,
        *template_identity_issues,
        *template_issues,
        *maximum_issues,
    ]
    questions_complete = questions.complete
    if template_identity_issues or template_metadata_status == "incomplete" or maximum_issues:
        questions_complete = False
    question_identity_issues = [issue for _question_id, record_issues in question_identities for issue in record_issues]
    if question_identity_issues:
        questions_complete = False
        issues.extend(question_identity_issues)

    template_metadata_complete, template_metadata_issues = _summarize_template_metadata(normalized_questions)
    questions_complete = questions_complete and template_metadata_complete
    issues.extend(template_metadata_issues)
    if native_maximums_consistent is False:
        questions_complete = False
        issues.append(
            f"question maximum total {question_maximum_total} does not match "
            f"template calculated total maximum {template_total_maximum}"
        )

    question_ids = [question_id for question_id, _record_issues in question_identities if question_id is not None]
    duplicate_question_issues = _duplicate_question_issues(question_ids)
    if duplicate_question_issues:
        questions_complete = False
        issues.extend(duplicate_question_issues)

    question_field_issues = _question_field_issues(questions.records, question_identities)
    if question_field_issues:
        questions_complete = False
        issues.extend(question_field_issues)
    last_modified_date = _string_or_none(field_value(record, "LastModifiedDate"))
    return {
        "deal_id": deal_id,
        "name": _string_or_none(field_value(record, "Name")),
        "score_ratio": field_value(record, "TSPC__ScorecardScoreRatio__c"),
        "total_score": field_value(record, "TSPC__ScorecardTotalScore__c"),
        "template_id": template_id,
        "template_deploy_date": template_deploy_date,
        "template_metadata_status": template_metadata_status,
        "template_version": template_version,
        "template_total_maximum": template_total_maximum,
        "question_maximum_total": question_maximum_total,
        "native_maximums_consistent": native_maximums_consistent,
        "last_modified_date": last_modified_date,
        "concurrency": _concurrency(last_modified_date),
        "questions_reported_count": questions.reported_count,
        "questions_complete": questions_complete,
        "issues": issues,
        "elements": normalized.elements,
        "gaps": normalized.gaps,
        "unmapped": normalized.unmapped,
    }


def _empty_questions(issue: str) -> UIAPIRecordCollection:
    """Represent a skipped question read as explicit incomplete evidence."""
    return UIAPIRecordCollection(records=[], reported_count=None, complete=False, issues=[issue])


def _read_deals(
    client: SFDirectClient,
    records: list[dict[str, Any]],
    identities: list[tuple[str | None, list[str]]],
    field_metadata: dict[str, SalesforceFieldMetadata],
) -> tuple[list[MeddpiccDeal], list[str]]:
    """Read every question collection beneath its exact owning deal."""
    deals: list[MeddpiccDeal] = []
    issues: list[str] = []
    # UI API exposes questions beneath each exact deal. There is no proven batch
    # relationship route for this package, so complete enumeration requires one
    # read-only child request per linked deal.
    question_collections = [
        (
            _empty_questions("deal has no valid exact Id; questions were not requested")
            if deal_id is None
            else client.fetch_closeplan_questions(deal_id)
        )
        for deal_id, _identity_issues in identities
    ]
    template_questions = read_template_questions(client, question_collections)
    template_ids = [
        template_id
        for record, (deal_id, _identity_issues) in zip(records, identities, strict=True)
        if (
            template_id := _exact_reference_id(
                field_value(record, "TSPC__Template__c"),
                subject=f"deal {deal_id or '(missing Id)'} template reference",
            )[0]
        )
        is not None
    ]
    templates = read_templates(client, template_ids)
    for record, (deal_id, identity_issues), questions in zip(records, identities, question_collections, strict=True):
        deal = _normalize_deal(
            record,
            questions,
            field_metadata,
            template_questions,
            templates,
            deal_id=deal_id,
            identity_issues=identity_issues,
        )
        deals.append(deal)
        issues.extend(f"deal {deal_id or '(missing Id)'}: {issue}" for issue in deal["issues"])
    return deals, issues


def _resolve_read_status(
    *,
    complete: bool,
    deals: list[MeddpiccDeal],
    selected_deal_id: str | None,
    opportunity_id: str,
) -> tuple[MeddpiccReadStatus, str | None, list[str]]:
    """Resolve deterministic read status and selection from complete evidence."""
    ids = [deal["deal_id"] for deal in deals if deal["deal_id"] is not None]
    if not complete:
        return "incomplete", None, []
    if selected_deal_id is not None and selected_deal_id not in ids:
        return (
            "invalid_selection",
            None,
            [f"selected deal {selected_deal_id} is not linked to opportunity {opportunity_id}"],
        )
    if not deals:
        return "not_found", None, []
    if selected_deal_id is not None:
        return "complete", selected_deal_id, []
    if len(deals) > 1:
        return "ambiguous", None, ["multiple ClosePlan deals are linked; select an exact deal Id"]
    return "complete", deals[0]["deal_id"], []


def read_meddpicc(
    client: SFDirectClient,
    *,
    org_url: str,
    opportunity_id: str,
    selected_deal_id: str | None = None,
) -> MeddpiccReadResult:
    """Read every native deal/question and resolve selection without mutation."""
    field_metadata, metadata_issues = _metadata_for_read(client)
    deal_collection = client.fetch_closeplan_deals(opportunity_id)
    issues = [f"deal collection: {issue}" for issue in deal_collection.issues]
    deal_identities, duplicate_deal_ids, deal_identities_complete = deal_identity_evidence(deal_collection.records)
    issues.extend(f"duplicate deal Id {deal_id} appears in the collection" for deal_id in duplicate_deal_ids)
    deals, deal_issues = _read_deals(
        client,
        deal_collection.records,
        deal_identities,
        field_metadata,
    )
    issues.extend(deal_issues)
    cross_deal_issues = cross_deal_question_issues(deals)
    issues.extend(cross_deal_issues)
    complete = (
        deal_collection.complete
        and deal_identities_complete
        and not cross_deal_issues
        and all(deal["questions_complete"] and not deal["issues"] for deal in deals)
    )
    status, resolved_deal_id, selection_issues = _resolve_read_status(
        complete=complete,
        deals=deals,
        selected_deal_id=selected_deal_id,
        opportunity_id=opportunity_id,
    )
    issues.extend(selection_issues)

    return {
        "org_url": org_url,
        "opportunity_id": opportunity_id,
        "status": status,
        "complete": complete,
        "selected_deal_id": resolved_deal_id,
        "deals_reported_count": deal_collection.reported_count,
        "deals": deals,
        "field_metadata": field_metadata,
        "metadata_gaps": [
            gap
            for gap in _METADATA_GAP_ORDER
            if any(
                gap in question["metadata_gaps"]
                for deal in deals
                for element in deal["elements"]
                for question in element["questions"]
            )
            or any(gap in question["metadata_gaps"] for deal in deals for question in deal["unmapped"])
        ],
        "issues": [*issues, *metadata_issues],
    }


def _state_for(questions: list[MeddpiccQuestion]) -> MeddpiccState:
    """Classify one canonical element from preserved score and answer evidence."""
    scores = [question.get("score") for question in questions if question.get("score") is not None]
    has_answer = any(
        isinstance(answer := question.get("answer"), str) and bool(answer.strip()) for question in questions
    )
    if scores:
        return "scored" if any(score != 0 for score in scores) else "scored_zero"
    return "answered_unscored" if has_answer else "unpopulated"


def _category_key(category: str | None) -> str | None:
    """Map a presentation category onto a canonical MEDDPICC key."""
    if category is None:
        return None
    normalized = " ".join(category.replace("_", " ").replace("-", " ").split()).casefold()
    return _CATEGORY_KEYS.get(normalized)


def _has_native_value(question: MeddpiccQuestion) -> bool:
    """Return whether a question retains either a score or nonblank answer."""
    answer = question["answer"]
    return question["score"] is not None or (isinstance(answer, str) and bool(answer.strip()))


def normalize_questions(questions: list[MeddpiccQuestion]) -> NormalizedMeddpicc:
    """Return all canonical elements in order while retaining unmapped questions."""
    grouped: dict[str, list[MeddpiccQuestion]] = {key: [] for key, _label in CANONICAL_ELEMENTS}
    unmapped: list[MeddpiccQuestion] = []
    for question in questions:
        key = _category_key(question.get("category"))
        if key is None:
            unmapped.append(question)
        else:
            grouped[key].append(question)

    elements: list[MeddpiccElement] = []
    gaps: list[str] = []
    for key, label in CANONICAL_ELEMENTS:
        element_questions = grouped[key]
        state = _state_for(element_questions)
        unanswered_ids = [question["question_id"] for question in element_questions if not _has_native_value(question)]
        elements.append(
            {
                "key": key,
                "label": label,
                "state": state,
                "complete": bool(element_questions) and not unanswered_ids,
                "unanswered_question_ids": unanswered_ids,
                "questions": element_questions,
            }
        )
        if state == "unpopulated":
            gaps.append(key)

    return NormalizedMeddpicc(elements=elements, gaps=gaps, unmapped=unmapped)
