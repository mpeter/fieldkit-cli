"""Construct immutable, guarded native ClosePlan score previews."""

import json
import math
import os
import re
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from fieldkit.config import get_fieldkit_data
from fieldkit.errors import FieldkitError
from fieldkit.sf.client import SFConditionalWriteConflict, SFConditionalWriteOutcomeUnknown, SFDirectClient
from fieldkit.sf.types import MeddpiccDeal, MeddpiccQuestion, MeddpiccReadResult

_SCORE_FIELD = "TSPC__Score__c"
_PLAN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_RECEIPT_RETENTION = timedelta(days=7)


class ClosePlanPlanError(FieldkitError):
    """Raised when live ClosePlan evidence cannot safely form a score plan."""


@dataclass(frozen=True)
class ClosePlanScoreChange:
    """One exact, conditionally writable native question score assignment."""

    question_id: str
    field_api_name: Literal["TSPC__Score__c"]
    current_value: float | None
    proposed_value: float
    allowed_values: tuple[float, ...]
    if_unmodified_since: str
    template_question_id: str
    template_version: float


@dataclass(frozen=True)
class ClosePlanScorePreview:
    """Immutable bindings that must survive until guarded mutation begins."""

    org_url: str
    opportunity_id: str
    deal_id: str
    template_id: str
    template_version: float
    changes: tuple[ClosePlanScoreChange, ...]


WriteOutcome = Literal[
    "verified_applied",
    "already_matching",
    "rejected_conflict",
    "not_attempted",
    "outcome_unknown",
]


@dataclass(frozen=True)
class ClosePlanScoreWriteOutcome:
    """The resolved, non-retried result for one previewed native question."""

    question_id: str
    status: WriteOutcome


@dataclass(frozen=True)
class ClosePlanScoreWriteResult:
    """Ordered question outcomes from one sequential guarded write attempt."""

    outcomes: tuple[ClosePlanScoreWriteOutcome, ...]
    reread_error: Exception | None = None


RollupVerificationStatus = Literal["verified", "pending"]


@dataclass(frozen=True)
class ClosePlanRollupVerification:
    """Salesforce-managed rollup verification without a locally derived total."""

    status: RollupVerificationStatus
    attempts: int


def _preview_dir(data_root: Path | None) -> Path:
    root = get_fieldkit_data() if data_root is None else data_root
    return root / "closeplan" / "previews"


def _recovery_dir(data_root: Path | None) -> Path:
    root = get_fieldkit_data() if data_root is None else data_root
    return root / "closeplan" / "recovery"


def _atomic_private_json(path: Path, payload: dict[str, object], *, prefix: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=prefix, suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _preview_path(plan_id: str, *, data_root: Path | None) -> Path:
    if not _PLAN_ID_RE.fullmatch(plan_id):
        raise ClosePlanPlanError("the ClosePlan preview identifier is invalid")
    return _preview_dir(data_root) / f"{plan_id}.json"


def _preview_payload(preview: ClosePlanScorePreview) -> dict[str, object]:
    return {
        "schema_version": 1,
        "org_url": preview.org_url,
        "opportunity_id": preview.opportunity_id,
        "deal_id": preview.deal_id,
        "template_id": preview.template_id,
        "template_version": preview.template_version,
        "changes": [
            {
                "question_id": change.question_id,
                "field_api_name": change.field_api_name,
                "current_value": change.current_value,
                "proposed_value": change.proposed_value,
                "allowed_values": list(change.allowed_values),
                "if_unmodified_since": change.if_unmodified_since,
                "template_question_id": change.template_question_id,
                "template_version": change.template_version,
            }
            for change in preview.changes
        ],
    }


def persist_score_preview(preview: ClosePlanScorePreview, *, data_root: Path | None = None) -> str:
    """Atomically persist a PII-minimized confirmation binding with mode ``0600``."""
    plan_id = uuid.uuid4().hex
    plan_path = _preview_path(plan_id, data_root=data_root)
    _atomic_private_json(plan_path, _preview_payload(preview), prefix=".closeplan-preview-")
    return plan_id


def persist_recovery_receipt(
    preview: ClosePlanScorePreview,
    result: ClosePlanScoreWriteResult,
    *,
    data_root: Path | None = None,
    now: datetime | None = None,
) -> Path:
    """Persist the minimal evidence needed to recover one guarded write attempt."""
    if len(preview.changes) != len(result.outcomes):
        raise ClosePlanPlanError("ClosePlan receipt cannot bind incomplete write outcomes")
    timestamp = datetime.now(tz=UTC) if now is None else now.astimezone(UTC)
    outcomes = [
        {
            "question_id": change.question_id,
            "field_api_name": change.field_api_name,
            "if_unmodified_since": change.if_unmodified_since,
            "requested_value": change.proposed_value,
            "status": outcome.status,
        }
        for change, outcome in zip(preview.changes, result.outcomes, strict=True)
    ]
    if any(
        change.question_id != outcome.question_id
        for change, outcome in zip(preview.changes, result.outcomes, strict=True)
    ):
        raise ClosePlanPlanError("ClosePlan receipt outcomes do not match the preview")
    receipt_path = _recovery_dir(data_root) / f"{uuid.uuid4().hex}.json"
    _atomic_private_json(
        receipt_path,
        {
            "schema_version": 1,
            "created_at": timestamp.isoformat(),
            "org_url": preview.org_url,
            "opportunity_id": preview.opportunity_id,
            "deal_id": preview.deal_id,
            "template_id": preview.template_id,
            "template_version": preview.template_version,
            "outcomes": outcomes,
        },
        prefix=".closeplan-receipt-",
    )
    return receipt_path


def prune_recovery_receipts(*, data_root: Path | None = None, now: datetime | None = None) -> list[Path]:
    """Remove resolved receipts older than seven days; retain unknown outcomes."""
    cutoff = (datetime.now(tz=UTC) if now is None else now.astimezone(UTC)) - _RECEIPT_RETENTION
    removed: list[Path] = []
    recovery_dir = _recovery_dir(data_root)
    if not recovery_dir.exists():
        return removed
    for receipt_path in sorted(recovery_dir.glob("*.json")):
        if receipt_path.is_symlink():
            continue
        try:
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            created_at = payload.get("created_at") if isinstance(payload, dict) else None
            outcomes = payload.get("outcomes") if isinstance(payload, dict) else None
            if not isinstance(created_at, str) or not isinstance(outcomes, list):
                continue
            created = datetime.fromisoformat(created_at)
            if created.tzinfo is None or created >= cutoff:
                continue
            if any(isinstance(outcome, dict) and outcome.get("status") == "outcome_unknown" for outcome in outcomes):
                continue
            receipt_path.unlink()
            removed.append(receipt_path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return removed


def _required_string(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise ClosePlanPlanError("the ClosePlan preview is invalid")
    return value


def _required_number(payload: dict[str, Any], field: str) -> float:
    return _finite_score(payload.get(field), label=f"preview {field}")


def _nullable_number(payload: dict[str, Any], field: str) -> float | None:
    value = payload.get(field)
    return None if value is None else _finite_score(value, label=f"preview {field}")


def _load_change(value: object) -> ClosePlanScoreChange:
    if not isinstance(value, dict):
        raise ClosePlanPlanError("the ClosePlan preview is invalid")
    field_api_name = _required_string(value, "field_api_name")
    if field_api_name != _SCORE_FIELD:
        raise ClosePlanPlanError("the ClosePlan preview is invalid")
    allowed_raw = value.get("allowed_values")
    if not isinstance(allowed_raw, list) or not allowed_raw:
        raise ClosePlanPlanError("the ClosePlan preview is invalid")
    allowed_values = tuple(_finite_score(item, label="preview allowed value") for item in allowed_raw)
    return ClosePlanScoreChange(
        question_id=_required_string(value, "question_id"),
        field_api_name="TSPC__Score__c",
        current_value=_nullable_number(value, "current_value"),
        proposed_value=_required_number(value, "proposed_value"),
        allowed_values=allowed_values,
        if_unmodified_since=_required_string(value, "if_unmodified_since"),
        template_question_id=_required_string(value, "template_question_id"),
        template_version=_required_number(value, "template_version"),
    )


def load_score_preview(plan_id: str, *, data_root: Path | None = None) -> ClosePlanScorePreview:
    """Load one stored confirmation binding, refusing malformed or unsafe state."""
    plan_path = _preview_path(plan_id, data_root=data_root)
    if not plan_path.exists():
        raise ClosePlanPlanError("the ClosePlan preview does not exist")
    if plan_path.is_symlink():
        raise ClosePlanPlanError("refusing a symlinked ClosePlan preview")
    try:
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ClosePlanPlanError("the ClosePlan preview is invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ClosePlanPlanError("the ClosePlan preview is invalid")
    changes_raw = payload.get("changes")
    if not isinstance(changes_raw, list) or not changes_raw:
        raise ClosePlanPlanError("the ClosePlan preview is invalid")
    changes = tuple(_load_change(change) for change in changes_raw)
    return ClosePlanScorePreview(
        org_url=_required_string(payload, "org_url"),
        opportunity_id=_required_string(payload, "opportunity_id"),
        deal_id=_required_string(payload, "deal_id"),
        template_id=_required_string(payload, "template_id"),
        template_version=_required_number(payload, "template_version"),
        changes=changes,
    )


def _finite_score(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ClosePlanPlanError(f"{label} must be a finite native numeric score")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ClosePlanPlanError(f"{label} must be a finite native numeric score")
    return numeric


def _selected_deal(scorecard: MeddpiccReadResult) -> MeddpiccDeal:
    if scorecard["status"] != "complete" or not scorecard["complete"] or scorecard["selected_deal_id"] is None:
        raise ClosePlanPlanError("a complete selected ClosePlan is required before a score preview")
    for deal in scorecard["deals"]:
        if deal["deal_id"] == scorecard["selected_deal_id"]:
            return deal
    raise ClosePlanPlanError("the selected ClosePlan was not present in the complete read")


def _questions_for_deal(deal: MeddpiccDeal) -> dict[str, MeddpiccQuestion]:
    questions = [question for element in deal["elements"] for question in element["questions"]]
    questions.extend(deal["unmapped"])
    result: dict[str, MeddpiccQuestion] = {}
    for question in questions:
        question_id = question["question_id"]
        if question_id is None:
            raise ClosePlanPlanError("the complete ClosePlan read contains a question without an exact Id")
        if question_id in result:
            raise ClosePlanPlanError(f"question {question_id} appears more than once in the selected ClosePlan")
        result[question_id] = question
    return result


def _native_choices(question: MeddpiccQuestion) -> tuple[float, ...]:
    template_question = question["template_question"]
    if template_question is None or question["template_metadata_status"] != "complete":
        raise ClosePlanPlanError("question lacks complete native template metadata")
    if question["question_type"] != "Answers" or template_question["question_type"] != "Answers":
        raise ClosePlanPlanError("only questions with native mode Answers are writable")
    if template_question["answer_model"] != "choice" or not template_question["answer_choices_complete"]:
        raise ClosePlanPlanError("question lacks complete native answer choices")
    values = tuple(
        sorted(
            {
                _finite_score(choice["max_score"], label="native answer choice")
                for choice in template_question["answer_choices"]
            }
        )
    )
    if not values:
        raise ClosePlanPlanError("question lacks complete native answer choices")
    return values


def _build_change(question: MeddpiccQuestion, *, proposed_value: object) -> ClosePlanScoreChange:
    question_id = question["question_id"]
    if question_id is None:
        raise ClosePlanPlanError("question lacks an exact Salesforce Id")
    field = question["field_metadata"].get(_SCORE_FIELD)
    if field is None or field["updateable"] is not True or field["calculated"] is not False:
        raise ClosePlanPlanError(f"question {question_id} score field is not proven writable")
    raw_current_value = question["raw_score"]
    current_value = (
        None
        if raw_current_value is None
        else _finite_score(raw_current_value, label=f"question {question_id} current score")
    )
    allowed_values = _native_choices(question)
    proposed = _finite_score(proposed_value, label=f"question {question_id} proposed score")
    if proposed not in allowed_values:
        raise ClosePlanPlanError(f"question {question_id} proposed score is not an exact native choice")
    template_question = question["template_question"]
    assert template_question is not None
    template_question_id = question["template_question_id"]
    if template_question_id is None or template_question_id != template_question["template_question_id"]:
        raise ClosePlanPlanError(f"question {question_id} template binding is incomplete")
    if question["template_version"] is None:
        raise ClosePlanPlanError(f"question {question_id} template version is missing")
    if_unmodified_since = question["last_modified_date"]
    if if_unmodified_since is None:
        raise ClosePlanPlanError(f"question {question_id} concurrency version is missing")
    return ClosePlanScoreChange(
        question_id=question_id,
        field_api_name="TSPC__Score__c",
        current_value=current_value,
        proposed_value=proposed,
        allowed_values=allowed_values,
        if_unmodified_since=if_unmodified_since,
        template_question_id=template_question_id,
        template_version=question["template_version"],
    )


def build_score_preview(
    scorecard: MeddpiccReadResult,
    *,
    requested_scores: dict[str, float],
) -> ClosePlanScorePreview:
    """Build a native score preview only from complete, exact live evidence."""
    if not requested_scores:
        raise ClosePlanPlanError("at least one exact question score is required")
    deal = _selected_deal(scorecard)
    if not deal["questions_complete"] or deal["template_id"] is None or deal["template_version"] is None:
        raise ClosePlanPlanError("the selected ClosePlan does not have complete writable metadata")
    questions = _questions_for_deal(deal)
    changes: list[ClosePlanScoreChange] = []
    for question_id, proposed_value in requested_scores.items():
        question = questions.get(question_id)
        if question is None:
            raise ClosePlanPlanError(f"question {question_id} is not owned by the selected ClosePlan")
        changes.append(_build_change(question, proposed_value=proposed_value))
    deal_id = deal["deal_id"]
    if deal_id is None:
        raise ClosePlanPlanError("the selected ClosePlan lacks an exact Salesforce Id")
    return ClosePlanScorePreview(
        org_url=scorecard["org_url"],
        opportunity_id=scorecard["opportunity_id"],
        deal_id=deal_id,
        template_id=deal["template_id"],
        template_version=deal["template_version"],
        changes=tuple(changes),
    )


def assert_preview_current(preview: ClosePlanScorePreview, scorecard: MeddpiccReadResult) -> None:
    """Refuse confirmation unless a fresh read reconstructs the exact preview."""
    requested_scores = {change.question_id: change.proposed_value for change in preview.changes}
    fresh_preview = build_score_preview(scorecard, requested_scores=requested_scores)
    if fresh_preview != preview:
        raise ClosePlanPlanError("the ClosePlan changed since preview; create and review a new preview")


def _observed_score(scorecard: MeddpiccReadResult, change: ClosePlanScoreChange) -> float | None:
    deal = _selected_deal(scorecard)
    question = _questions_for_deal(deal).get(change.question_id)
    if question is None:
        raise ClosePlanPlanError(f"question {change.question_id} was absent from its selected ClosePlan reread")
    raw_value = question["raw_score"]
    return None if raw_value is None else _finite_score(raw_value, label=f"question {change.question_id} reread score")


def _rollup_signature(scorecard: MeddpiccReadResult) -> tuple[object, object, object]:
    deal = _selected_deal(scorecard)
    return deal["total_score"], deal["score_ratio"], deal["last_modified_date"]


def poll_scorecard_rollup(
    before_write: MeddpiccReadResult,
    *,
    reread_scorecard: Callable[[], MeddpiccReadResult],
    max_attempts: int = 3,
    sleep_seconds: float = 1.0,
) -> ClosePlanRollupVerification:
    """Poll Salesforce's calculated deal rollup without retrying any mutation."""
    if max_attempts < 1 or sleep_seconds < 0:
        raise ValueError("rollup polling requires a positive attempt count and non-negative interval")
    before_signature = _rollup_signature(before_write)
    for attempt in range(1, max_attempts + 1):
        if _rollup_signature(reread_scorecard()) != before_signature:
            return ClosePlanRollupVerification(status="verified", attempts=attempt)
        if attempt < max_attempts:
            time.sleep(sleep_seconds)
    return ClosePlanRollupVerification(status="pending", attempts=max_attempts)


def apply_score_preview(
    client: SFDirectClient,
    preview: ClosePlanScorePreview,
    *,
    reread_scorecard: Callable[[], MeddpiccReadResult],
) -> ClosePlanScoreWriteResult:
    """Apply previewed scores sequentially, without retrying ambiguous mutations."""
    outcomes: list[ClosePlanScoreWriteOutcome] = []
    for index, change in enumerate(preview.changes):
        if change.current_value == change.proposed_value:
            outcomes.append(ClosePlanScoreWriteOutcome(change.question_id, "already_matching"))
            continue
        try:
            client.conditional_update_sobject_fields(
                "TSPC__DealQuestion__c",
                change.question_id,
                {change.field_api_name: change.proposed_value},
                if_unmodified_since=change.if_unmodified_since,
            )
        except SFConditionalWriteConflict:
            outcomes.append(ClosePlanScoreWriteOutcome(change.question_id, "rejected_conflict"))
        except SFConditionalWriteOutcomeUnknown:
            try:
                _observed_score(reread_scorecard(), change)
            except Exception as exc:  # noqa: BLE001 - receipt must precede propagation of any post-write reread failure.
                outcomes.append(ClosePlanScoreWriteOutcome(change.question_id, "outcome_unknown"))
                outcomes.extend(
                    ClosePlanScoreWriteOutcome(remaining.question_id, "not_attempted")
                    for remaining in preview.changes[index + 1 :]
                )
                return ClosePlanScoreWriteResult(outcomes=tuple(outcomes), reread_error=exc)
            outcomes.append(ClosePlanScoreWriteOutcome(change.question_id, "outcome_unknown"))
        else:
            try:
                observed_score = _observed_score(reread_scorecard(), change)
            except Exception as exc:  # noqa: BLE001 - receipt must precede propagation of any post-write reread failure.
                outcomes.append(ClosePlanScoreWriteOutcome(change.question_id, "outcome_unknown"))
                outcomes.extend(
                    ClosePlanScoreWriteOutcome(remaining.question_id, "not_attempted")
                    for remaining in preview.changes[index + 1 :]
                )
                return ClosePlanScoreWriteResult(outcomes=tuple(outcomes), reread_error=exc)
            if observed_score == change.proposed_value:
                outcomes.append(ClosePlanScoreWriteOutcome(change.question_id, "verified_applied"))
                continue
            outcomes.append(ClosePlanScoreWriteOutcome(change.question_id, "outcome_unknown"))

        outcomes.extend(
            ClosePlanScoreWriteOutcome(remaining.question_id, "not_attempted")
            for remaining in preview.changes[index + 1 :]
        )
        break
    return ClosePlanScoreWriteResult(outcomes=tuple(outcomes))
