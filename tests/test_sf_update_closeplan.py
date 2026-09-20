"""CLI contracts for the guarded native ClosePlan writer."""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.sf.update_closeplan import cli
from fieldkit.sf.client import SFAuthError
from fieldkit.sf.closeplan_writer import ClosePlanScoreWriteOutcome, ClosePlanScoreWriteResult

pytestmark = pytest.mark.unit

_OPPORTUNITY_ID = "006000000000000AAA"
_DEAL_ID = "a1D000000000000AAA"
_QUESTION_ID = "a1E000000000009AAA"


def test_help_exposes_preview_and_plan_confirmation_contract() -> None:
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "--score" in result.output
    assert "--confirm PLAN_ID" in result.output


def test_preview_requires_exact_target_and_score_before_authentication() -> None:
    with patch("fieldkit.commands.sf.update_closeplan.get_sf_session_id") as session_id:
        result = CliRunner().invoke(cli, [_OPPORTUNITY_ID, "--deal-id", _DEAL_ID])

    assert result.exit_code == 3
    assert "at least one --score" in result.output
    session_id.assert_not_called()


def test_preview_rejects_duplicate_question_assignments_before_authentication() -> None:
    with patch("fieldkit.commands.sf.update_closeplan.get_sf_session_id") as session_id:
        result = CliRunner().invoke(
            cli,
            [
                _OPPORTUNITY_ID,
                "--deal-id",
                _DEAL_ID,
                "--score",
                f"{_QUESTION_ID}=2",
                "--score",
                f"{_QUESTION_ID}=0",
            ],
        )

    assert result.exit_code == 3
    assert "more than once" in result.output
    session_id.assert_not_called()


def test_confirmation_refuses_target_overrides_before_authentication() -> None:
    with patch("fieldkit.commands.sf.update_closeplan.get_sf_session_id") as session_id:
        result = CliRunner().invoke(cli, [_OPPORTUNITY_ID, "--confirm", "0" * 32])

    assert result.exit_code == 3
    assert "only a stored preview" in result.output
    session_id.assert_not_called()


def test_preview_persists_only_the_reviewable_plan() -> None:
    scorecard = MagicMock()
    preview = MagicMock(
        opportunity_id=_OPPORTUNITY_ID,
        deal_id=_DEAL_ID,
        changes=(MagicMock(),),
    )
    client_context = MagicMock()
    client_context.__enter__.return_value = MagicMock()

    with (
        patch("fieldkit.commands.sf.update_closeplan.get_sf_session_id", return_value="sid"),
        patch(
            "fieldkit.commands.sf.update_closeplan.get_sf_rest_base_url",
            return_value="https://example.my.salesforce.com",
        ),
        patch("fieldkit.commands.sf.update_closeplan._sf.SFDirectClient", return_value=client_context),
        patch("fieldkit.commands.sf.update_closeplan._read_scorecard", return_value=scorecard),
        patch("fieldkit.commands.sf.update_closeplan.build_score_preview", return_value=preview) as build_preview,
        patch("fieldkit.commands.sf.update_closeplan.persist_score_preview", return_value="a" * 32),
    ):
        result = CliRunner().invoke(
            cli,
            [_OPPORTUNITY_ID, "--deal-id", _DEAL_ID, "--score", f"{_QUESTION_ID}=2"],
        )

    assert result.exit_code == 0
    assert "status: preview" in result.output
    build_preview.assert_called_once_with(scorecard, requested_scores={_QUESTION_ID: 2.0})


def test_confirmation_can_be_aborted_before_any_write() -> None:
    preview = MagicMock(org_url="https://example.my.salesforce.com", opportunity_id=_OPPORTUNITY_ID, deal_id=_DEAL_ID)
    client_context = MagicMock()
    client_context.__enter__.return_value = MagicMock()

    with (
        patch("fieldkit.commands.sf.update_closeplan.load_score_preview", return_value=preview),
        patch("fieldkit.commands.sf.update_closeplan.get_sf_session_id", return_value="sid"),
        patch("fieldkit.commands.sf.update_closeplan.get_sf_rest_base_url", return_value=preview.org_url),
        patch("fieldkit.commands.sf.update_closeplan._sf.SFDirectClient", return_value=client_context),
        patch("fieldkit.commands.sf.update_closeplan._read_scorecard"),
        patch("fieldkit.commands.sf.update_closeplan.assert_preview_current"),
        patch("fieldkit.commands.sf.update_closeplan.stdin_is_interactive", return_value=True),
        patch("fieldkit.commands.sf.update_closeplan.click.confirm", return_value=False),
        patch("fieldkit.commands.sf.update_closeplan.apply_score_preview") as apply_preview,
    ):
        result = CliRunner().invoke(cli, ["--confirm", "0" * 32])

    assert result.exit_code == 0
    assert "status: aborted" in result.output
    apply_preview.assert_not_called()


def test_confirmation_verifies_native_rollup_after_resolved_writes() -> None:
    preview = MagicMock(org_url="https://example.my.salesforce.com", opportunity_id=_OPPORTUNITY_ID, deal_id=_DEAL_ID)
    write_result = ClosePlanScoreWriteResult(outcomes=(ClosePlanScoreWriteOutcome(_QUESTION_ID, "verified_applied"),))
    receipt = MagicMock(stem="receipt-id")
    rollup = MagicMock(status="verified")
    client_context = MagicMock()
    client_context.__enter__.return_value = MagicMock()

    with (
        patch("fieldkit.commands.sf.update_closeplan.load_score_preview", return_value=preview),
        patch("fieldkit.commands.sf.update_closeplan.get_sf_session_id", return_value="sid"),
        patch("fieldkit.commands.sf.update_closeplan.get_sf_rest_base_url", return_value=preview.org_url),
        patch("fieldkit.commands.sf.update_closeplan._sf.SFDirectClient", return_value=client_context),
        patch("fieldkit.commands.sf.update_closeplan._read_scorecard", return_value=MagicMock()),
        patch("fieldkit.commands.sf.update_closeplan.assert_preview_current"),
        patch("fieldkit.commands.sf.update_closeplan.stdin_is_interactive", return_value=False),
        patch("fieldkit.commands.sf.update_closeplan.apply_score_preview", return_value=write_result),
        patch("fieldkit.commands.sf.update_closeplan.persist_recovery_receipt", return_value=receipt),
        patch("fieldkit.commands.sf.update_closeplan.poll_scorecard_rollup", return_value=rollup) as poll_rollup,
    ):
        result = CliRunner().invoke(cli, ["--confirm", "0" * 32])

    assert result.exit_code == 0
    assert "rollup_verification: verified" in result.output
    poll_rollup.assert_called_once()


def test_confirmation_persists_recovery_receipt_before_propagating_reread_auth_failure() -> None:
    preview = MagicMock(org_url="https://example.my.salesforce.com", opportunity_id=_OPPORTUNITY_ID, deal_id=_DEAL_ID)
    result = ClosePlanScoreWriteResult(
        outcomes=(ClosePlanScoreWriteOutcome(_QUESTION_ID, "outcome_unknown"),),
        reread_error=SFAuthError("session expired during reread"),
    )
    receipt = MagicMock(stem="receipt-id")
    client_context = MagicMock()
    client_context.__enter__.return_value = MagicMock()

    with (
        patch("fieldkit.commands.sf.update_closeplan.load_score_preview", return_value=preview),
        patch("fieldkit.commands.sf.update_closeplan.get_sf_session_id", return_value="sid"),
        patch("fieldkit.commands.sf.update_closeplan.get_sf_rest_base_url", return_value=preview.org_url),
        patch("fieldkit.commands.sf.update_closeplan._sf.SFDirectClient", return_value=client_context),
        patch("fieldkit.commands.sf.update_closeplan._read_scorecard"),
        patch("fieldkit.commands.sf.update_closeplan.assert_preview_current"),
        patch("fieldkit.commands.sf.update_closeplan.stdin_is_interactive", return_value=False),
        patch("fieldkit.commands.sf.update_closeplan.apply_score_preview", return_value=result),
        patch(
            "fieldkit.commands.sf.update_closeplan.persist_recovery_receipt", return_value=receipt
        ) as persist_receipt,
    ):
        cli_result = CliRunner().invoke(cli, ["--confirm", "0" * 32])

    assert cli_result.exit_code == 2
    persist_receipt.assert_called_once_with(preview, result)
