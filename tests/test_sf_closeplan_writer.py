"""Unit tests for guarded native ClosePlan score preview plans."""

import json
import stat
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from test_sf_meddpicc import _DEAL_ID, _scorecard

from fieldkit.sf.client import SFConditionalWriteConflict, SFConditionalWriteOutcomeUnknown
from fieldkit.sf.closeplan_writer import (
    ClosePlanPlanError,
    apply_score_preview,
    assert_preview_current,
    build_score_preview,
    load_score_preview,
    persist_recovery_receipt,
    persist_score_preview,
    poll_scorecard_rollup,
    prune_recovery_receipts,
)
from fieldkit.sf.types import MeddpiccQuestion

pytestmark = pytest.mark.unit

_QUESTION_ID = "a1E000000000009AAA"


def _writable_question(*, mode: str = "Answers", choices_complete: bool = True) -> MeddpiccQuestion:
    return {
        "question_id": _QUESTION_ID,
        "category": "Metrics",
        "name": "Metrics - Native score prompt",
        "score": 0.0,
        "answer": None,
        "raw_score": 0.0,
        "raw_text_answer": None,
        "raw_rich_text_answer": None,
        "raw_answer": None,
        "has_text_answer": False,
        "last_modified_date": "2026-09-15T12:00:00.000+0000",
        "concurrency": {
            "field": "LastModifiedDate",
            "value": "2026-09-15T12:00:00.000+0000",
            "conditional_header": "If-Unmodified-Since",
            "strength": "weak_timestamp",
            "mutation_enabled": False,
        },
        "field_metadata": {
            "TSPC__Score__c": {
                "api_name": "TSPC__Score__c",
                "type": "double",
                "updateable": True,
                "calculated": False,
                "precision": 4,
                "scale": 1,
                "length": None,
                "picklist_values": None,
            }
        },
        "question_type": mode,
        "score_maximum": 6.0,
        "weight": None,
        "template_question_id": "a2G000000000009AAA",
        "template_metadata_status": "complete",
        "template_question": {
            "template_question_id": "a2G000000000009AAA",
            "name": "Native score prompt",
            "template_id": "a2T000000000001AAA",
            "category_id": "a1o000000000009AAA",
            "question_category_id": "a1u000000000009AAA",
            "question_type": mode,
            "has_text_answer": False,
            "score_maximum": 6.0,
            "sync_score_field": None,
            "sync_score_ratio_field": None,
            "has_shared_score": False,
            "answer_model": "choice",
            "answer_choices_reported_count": 2,
            "answer_choices_complete": choices_complete,
            "answer_choices": [
                {
                    "answer_id": "a2H000000000001AAA",
                    "name": "Not established",
                    "text": "Not established",
                    "attitude": None,
                    "has_text_answer": False,
                    "max_score": 0.0,
                    "sort_order": 1.0,
                    "sync_text_answer_field": None,
                    "sync_value_field_pickval": None,
                    "issues": [],
                },
                {
                    "answer_id": "a2H000000000002AAA",
                    "name": "Established",
                    "text": "Established",
                    "attitude": None,
                    "has_text_answer": False,
                    "max_score": 2.0,
                    "sort_order": 2.0,
                    "sync_text_answer_field": None,
                    "sync_value_field_pickval": None,
                    "issues": [],
                },
            ],
            "issues": [],
        },
        "template_version": 3.0,
        "metadata_gaps": [],
        "metadata_issues": [],
    }


def test_build_score_preview_binds_exact_native_score_and_precondition() -> None:
    preview = build_score_preview(
        _scorecard([_writable_question()]),
        requested_scores={_QUESTION_ID: 2.0},
    )

    assert preview.opportunity_id == "006000000000000AAA"
    assert preview.deal_id == _DEAL_ID
    assert preview.template_id == "a2T000000000001AAA"
    assert preview.changes[0].question_id == _QUESTION_ID
    assert preview.changes[0].current_value == 0.0
    assert preview.changes[0].proposed_value == 2.0
    assert preview.changes[0].allowed_values == (0.0, 2.0)
    assert preview.changes[0].if_unmodified_since == "2026-09-15T12:00:00.000+0000"


def test_build_score_preview_preserves_an_unscored_native_null() -> None:
    question = _writable_question()
    question["score"] = None
    question["raw_score"] = None

    preview = build_score_preview(_scorecard([question]), requested_scores={_QUESTION_ID: 2.0})

    assert preview.changes[0].current_value is None


def test_build_score_preview_refuses_unproven_native_score_choice() -> None:
    with pytest.raises(ClosePlanPlanError, match="not an exact native choice"):
        build_score_preview(
            _scorecard([_writable_question()]),
            requested_scores={_QUESTION_ID: 3.0},
        )


def test_build_score_preview_refuses_non_answers_mode() -> None:
    with pytest.raises(ClosePlanPlanError, match="mode Answers"):
        build_score_preview(
            _scorecard([_writable_question(mode="RichText")]),
            requested_scores={_QUESTION_ID: 2.0},
        )


def test_build_score_preview_refuses_incomplete_choice_evidence() -> None:
    with pytest.raises(ClosePlanPlanError, match="complete native answer choices"):
        build_score_preview(
            _scorecard([_writable_question(choices_complete=False)]),
            requested_scores={_QUESTION_ID: 2.0},
        )


def test_build_score_preview_refuses_ambiguous_or_incomplete_scorecard() -> None:
    scorecard = _scorecard([_writable_question()])
    scorecard["status"] = "ambiguous"
    scorecard["selected_deal_id"] = None

    with pytest.raises(ClosePlanPlanError, match="complete selected ClosePlan"):
        build_score_preview(scorecard, requested_scores={_QUESTION_ID: 2.0})


def test_persisted_preview_omits_prompt_and_answer_content_and_is_owner_only(tmp_path) -> None:
    preview = build_score_preview(
        _scorecard([_writable_question()]),
        requested_scores={_QUESTION_ID: 2.0},
    )

    plan_id = persist_score_preview(preview, data_root=tmp_path)
    plan_path = tmp_path / "closeplan" / "previews" / f"{plan_id}.json"

    assert stat.S_IMODE(plan_path.stat().st_mode) == 0o600
    serialized = json.loads(plan_path.read_text(encoding="utf-8"))
    assert "Native score prompt" not in plan_path.read_text(encoding="utf-8")
    assert "Established" not in plan_path.read_text(encoding="utf-8")
    assert serialized["schema_version"] == 1
    assert load_score_preview(plan_id, data_root=tmp_path) == preview


def test_load_score_preview_rejects_malformed_or_missing_plan(tmp_path) -> None:
    with pytest.raises(ClosePlanPlanError, match="does not exist"):
        load_score_preview("0" * 32, data_root=tmp_path)

    plan_dir = tmp_path / "closeplan" / "previews"
    plan_dir.mkdir(parents=True)
    (plan_dir / f"{'1' * 32}.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ClosePlanPlanError, match="invalid"):
        load_score_preview("1" * 32, data_root=tmp_path)


def test_assert_preview_current_accepts_identical_fresh_bindings() -> None:
    scorecard = _scorecard([_writable_question()])
    preview = build_score_preview(scorecard, requested_scores={_QUESTION_ID: 2.0})

    assert_preview_current(preview, scorecard)


def test_assert_preview_current_refuses_any_question_version_drift() -> None:
    scorecard = _scorecard([_writable_question()])
    preview = build_score_preview(scorecard, requested_scores={_QUESTION_ID: 2.0})
    question = scorecard["deals"][0]["elements"][0]["questions"][0]
    question["last_modified_date"] = "2026-09-15T12:01:00.000+0000"

    with pytest.raises(ClosePlanPlanError, match="changed since preview"):
        assert_preview_current(preview, scorecard)


def test_apply_score_preview_verifies_each_success_with_an_exact_reread() -> None:
    initial = _scorecard([_writable_question()])
    preview = build_score_preview(initial, requested_scores={_QUESTION_ID: 2.0})
    reread_question = _writable_question()
    reread_question["score"] = 2.0
    reread_question["raw_score"] = 2.0
    client = MagicMock()

    result = apply_score_preview(client, preview, reread_scorecard=lambda: _scorecard([reread_question]))

    assert result.outcomes[0].status == "verified_applied"
    client.conditional_update_sobject_fields.assert_called_once_with(
        "TSPC__DealQuestion__c",
        _QUESTION_ID,
        {"TSPC__Score__c": 2.0},
        if_unmodified_since="2026-09-15T12:00:00.000+0000",
    )


def test_apply_score_preview_stops_after_a_conflict() -> None:
    initial = _scorecard([_writable_question()])
    second = _writable_question()
    second["question_id"] = "a1E000000000008AAA"
    initial = _scorecard([_writable_question(), second])
    preview = build_score_preview(initial, requested_scores={_QUESTION_ID: 2.0, second["question_id"]: 2.0})
    client = MagicMock()
    client.conditional_update_sobject_fields.side_effect = SFConditionalWriteConflict("stale")

    result = apply_score_preview(client, preview, reread_scorecard=lambda: initial)

    assert [outcome.status for outcome in result.outcomes] == ["rejected_conflict", "not_attempted"]
    client.conditional_update_sobject_fields.assert_called_once()


def test_apply_score_preview_stops_after_an_unknown_transport_outcome() -> None:
    preview = build_score_preview(_scorecard([_writable_question()]), requested_scores={_QUESTION_ID: 2.0})
    client = MagicMock()
    client.conditional_update_sobject_fields.side_effect = SFConditionalWriteOutcomeUnknown("lost response")
    reread_question = _writable_question()
    reread_question["score"] = 2.0
    reread_question["raw_score"] = 2.0

    result = apply_score_preview(client, preview, reread_scorecard=lambda: _scorecard([reread_question]))

    assert [outcome.status for outcome in result.outcomes] == ["outcome_unknown"]


def test_apply_score_preview_returns_unknown_outcome_before_propagating_reread_failure() -> None:
    preview = build_score_preview(_scorecard([_writable_question()]), requested_scores={_QUESTION_ID: 2.0})
    client = MagicMock()

    result = apply_score_preview(
        client,
        preview,
        reread_scorecard=lambda: (_ for _ in ()).throw(RuntimeError("Salesforce reread failed")),
    )

    assert [outcome.status for outcome in result.outcomes] == ["outcome_unknown"]
    assert isinstance(result.reread_error, RuntimeError)


def test_recovery_receipt_is_atomic_minimized_and_owner_only(tmp_path) -> None:
    preview = build_score_preview(_scorecard([_writable_question()]), requested_scores={_QUESTION_ID: 2.0})
    result = apply_score_preview(MagicMock(), preview, reread_scorecard=lambda: _scorecard([_writable_question()]))

    receipt_path = persist_recovery_receipt(preview, result, data_root=tmp_path, now=datetime(2026, 9, 15, tzinfo=UTC))

    content = receipt_path.read_text(encoding="utf-8")
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert "Native score prompt" not in content
    assert "Established" not in content
    assert json.loads(content)["outcomes"][0]["status"] == "outcome_unknown"


def test_prune_recovery_receipts_keeps_unresolved_unknown_outcomes(tmp_path) -> None:
    preview = build_score_preview(_scorecard([_writable_question()]), requested_scores={_QUESTION_ID: 2.0})
    unknown = apply_score_preview(MagicMock(), preview, reread_scorecard=lambda: _scorecard([_writable_question()]))
    verified_question = _writable_question()
    verified_question["score"] = 2.0
    verified_question["raw_score"] = 2.0
    verified = apply_score_preview(MagicMock(), preview, reread_scorecard=lambda: _scorecard([verified_question]))
    old = datetime(2026, 9, 1, tzinfo=UTC)
    unknown_path = persist_recovery_receipt(preview, unknown, data_root=tmp_path, now=old)
    verified_path = persist_recovery_receipt(preview, verified, data_root=tmp_path, now=old)

    removed = prune_recovery_receipts(data_root=tmp_path, now=old + timedelta(days=8))

    assert removed == [verified_path]
    assert unknown_path.exists()
    assert not verified_path.exists()


def test_rollup_poll_reports_verified_only_when_salesforce_changes_its_own_rollup() -> None:
    initial = _scorecard([_writable_question()])
    recalculated = _scorecard([_writable_question()])
    recalculated["deals"][0]["total_score"] = 44.0
    reads = iter([initial, recalculated])

    result = poll_scorecard_rollup(initial, reread_scorecard=lambda: next(reads), max_attempts=2, sleep_seconds=0)

    assert result.status == "verified"
    assert result.attempts == 2


def test_rollup_poll_reports_pending_without_deriving_a_total() -> None:
    initial = _scorecard([_writable_question()])

    result = poll_scorecard_rollup(initial, reread_scorecard=lambda: initial, max_attempts=2, sleep_seconds=0)

    assert result.status == "pending"
    assert result.attempts == 2
