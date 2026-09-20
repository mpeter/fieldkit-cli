"""Validate ClosePlan record identity and collection completeness evidence."""

import re
from collections import Counter
from typing import Any, Final

from fieldkit.sf.types import MeddpiccDeal

_SF_ID_RE: Final = re.compile(r"^[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?$")


def ui_api_field_issues(record: dict[str, Any], field_names: tuple[str, ...], *, subject: str) -> list[str]:
    """Report requested UI API fields whose raw value envelope is unproven."""
    fields = record.get("fields")
    if not isinstance(fields, dict):
        return [f"{subject} fields collection is missing or invalid"]
    issues: list[str] = []
    for field_name in field_names:
        if field_name not in fields:
            issues.append(f"{subject} field {field_name} is missing")
        elif not isinstance(field := fields[field_name], dict) or "value" not in field:
            issues.append(f"{subject} field {field_name} has no value envelope")
    return issues


def resolve_record_identity(record: dict[str, Any], *, subject: str) -> tuple[str | None, list[str]]:
    """Resolve and validate the independent identity sources on a UI API record."""
    candidates: list[tuple[str, object]] = []
    issues: list[str] = []
    if "id" in record:
        candidates.append(("top-level id", record["id"]))

    fields = record.get("fields")
    if isinstance(fields, dict) and "Id" in fields:
        id_field = fields["Id"]
        if isinstance(id_field, dict) and "value" in id_field:
            candidates.append(("fields.Id.value", id_field["value"]))
        else:
            issues.append(f"{subject} fields.Id has no value envelope")

    valid: list[str] = []
    for source, candidate in candidates:
        if isinstance(candidate, str) and _SF_ID_RE.fullmatch(candidate) is not None:
            valid.append(candidate)
        else:
            issues.append(f"{subject} {source} is not a valid Salesforce Id")

    distinct = set(valid)
    if len(distinct) > 1:
        issues.append(f"{subject} identity mismatch between top-level id and fields.Id.value")
        return None, issues
    if distinct:
        return valid[0], issues
    if not issues:
        issues.append(f"{subject} has no valid exact Id")
    return None, issues


def deal_identity_evidence(
    records: list[dict[str, Any]],
) -> tuple[list[tuple[str | None, list[str]]], list[str], bool]:
    """Return per-record identities, duplicate IDs, and collection completeness."""
    identities = [
        resolve_record_identity(record, subject=f"deal record {index}") for index, record in enumerate(records, start=1)
    ]
    deal_ids = [deal_id for deal_id, _identity_issues in identities if deal_id is not None]
    duplicate_ids = sorted(deal_id for deal_id, count in Counter(deal_ids).items() if count > 1)
    complete = not duplicate_ids and all(
        deal_id is not None and not identity_issues for deal_id, identity_issues in identities
    )
    return identities, duplicate_ids, complete


def _question_ids(deal: MeddpiccDeal) -> set[str]:
    """Collect valid exact question IDs retained under one normalized deal."""
    questions = [question for element in deal["elements"] for question in element["questions"]]
    questions.extend(deal["unmapped"])
    return {
        question_id
        for question in questions
        if isinstance(question_id := question["question_id"], str) and _SF_ID_RE.fullmatch(question_id) is not None
    }


def cross_deal_question_issues(deals: list[MeddpiccDeal]) -> list[str]:
    """Report exact question IDs that appear beneath more than one deal."""
    question_owners: dict[str, set[str]] = {}
    for deal in deals:
        if (deal_id := deal["deal_id"]) is None:
            continue
        for question_id in _question_ids(deal):
            question_owners.setdefault(question_id, set()).add(deal_id)
    return [
        f"question Id {question_id} appears under multiple deals: {', '.join(sorted(owner_ids))}"
        for question_id, owner_ids in sorted(question_owners.items())
        if len(owner_ids) > 1
    ]
