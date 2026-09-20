"""Guarded native ClosePlan score preview and confirmation command."""

import json
import math
import re

import click

import fieldkit.sf.client as _sf
from fieldkit.cli_exit import EXIT_DATA, EXIT_PARTIAL, cli_main
from fieldkit.cli_registry import declare_write
from fieldkit.commands.sf._util import stdin_is_interactive
from fieldkit.config import get_sf_rest_base_url, get_sf_session_id
from fieldkit.sf.closeplan_writer import (
    ClosePlanPlanError,
    ClosePlanScorePreview,
    apply_score_preview,
    assert_preview_current,
    build_score_preview,
    load_score_preview,
    persist_recovery_receipt,
    persist_score_preview,
    poll_scorecard_rollup,
)
from fieldkit.sf.meddpicc import read_meddpicc
from fieldkit.sf.types import MeddpiccReadResult

_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9]{15}([A-Za-z0-9]{3})?$")


def _validate_record_id(record_id: str, *, label: str) -> None:
    if not _RECORD_ID_RE.fullmatch(record_id):
        raise ClosePlanPlanError(f"invalid {label} Salesforce Id")


def _parse_scores(values: tuple[str, ...]) -> dict[str, float]:
    scores: dict[str, float] = {}
    for value in values:
        question_id, separator, raw_score = value.partition("=")
        if not separator or not question_id or not raw_score:
            raise ClosePlanPlanError("each --score must be QUESTION_ID=NATIVE_SCORE")
        _validate_record_id(question_id, label="question")
        try:
            score = float(raw_score)
        except ValueError as exc:
            raise ClosePlanPlanError("each --score must use a finite numeric native score") from exc
        if not math.isfinite(score):
            raise ClosePlanPlanError("each --score must use a finite numeric native score")
        if question_id in scores:
            raise ClosePlanPlanError(f"question {question_id} was supplied more than once")
        scores[question_id] = score
    return scores


def _read_scorecard(
    client: _sf.SFDirectClient, *, base_url: str, opportunity_id: str, deal_id: str
) -> MeddpiccReadResult:
    return read_meddpicc(
        client,
        org_url=base_url,
        opportunity_id=opportunity_id,
        selected_deal_id=deal_id,
    )


def _emit(payload: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(payload, sort_keys=True))
        return
    for key, value in payload.items():
        click.echo(f"{key}: {value}")


def _create_preview_payload(
    client: _sf.SFDirectClient,
    *,
    base_url: str,
    opportunity_id: str,
    deal_id: str,
    requested_scores: dict[str, float],
) -> dict[str, object]:
    scorecard = _read_scorecard(client, base_url=base_url, opportunity_id=opportunity_id, deal_id=deal_id)
    preview = build_score_preview(scorecard, requested_scores=requested_scores)
    plan_id = persist_score_preview(preview)
    return {
        "status": "preview",
        "plan_id": plan_id,
        "opportunity_id": preview.opportunity_id,
        "deal_id": preview.deal_id,
        "changes": len(preview.changes),
        "confirm": f"fieldkit sf update-closeplan --confirm {plan_id}",
    }


def _validated_preview_arguments(
    opportunity_id: str | None, deal_id: str | None, scores: tuple[str, ...]
) -> tuple[str, str, dict[str, float]]:
    if opportunity_id is None or deal_id is None or not scores:
        raise ClosePlanPlanError("preview requires OPPORTUNITY_ID, --deal-id, and at least one --score")
    _validate_record_id(opportunity_id, label="opportunity")
    _validate_record_id(deal_id, label="deal")
    return opportunity_id, deal_id, _parse_scores(scores)


def _validate_confirmation_arguments(opportunity_id: str | None, deal_id: str | None, scores: tuple[str, ...]) -> None:
    if opportunity_id is not None or deal_id is not None or scores:
        raise ClosePlanPlanError("--confirm accepts only a stored preview plan identifier")


def _confirm_preview_payload(
    client: _sf.SFDirectClient,
    *,
    base_url: str,
    preview: ClosePlanScorePreview,
    confirm_plan: str,
) -> tuple[dict[str, object], bool]:
    if preview.org_url.rstrip("/") != base_url.rstrip("/"):
        raise ClosePlanPlanError("the preview belongs to a different Salesforce org")
    scorecard = _read_scorecard(
        client,
        base_url=base_url,
        opportunity_id=preview.opportunity_id,
        deal_id=preview.deal_id,
    )
    assert_preview_current(preview, scorecard)
    if stdin_is_interactive() and not click.confirm("Apply this reviewed ClosePlan preview?", default=False):
        return {"status": "aborted", "plan_id": confirm_plan}, False
    result = apply_score_preview(
        client,
        preview,
        reread_scorecard=lambda: _read_scorecard(
            client,
            base_url=base_url,
            opportunity_id=preview.opportunity_id,
            deal_id=preview.deal_id,
        ),
    )
    receipt = persist_recovery_receipt(preview, result)
    outcome_names = [outcome.status for outcome in result.outcomes]
    if result.reread_error is not None:
        raise result.reread_error
    rollup_status = "not_checked"
    if all(status in {"verified_applied", "already_matching"} for status in outcome_names):
        rollup_status = poll_scorecard_rollup(
            scorecard,
            reread_scorecard=lambda: _read_scorecard(
                client,
                base_url=base_url,
                opportunity_id=preview.opportunity_id,
                deal_id=preview.deal_id,
            ),
        ).status
    is_partial = any(status not in {"verified_applied", "already_matching"} for status in outcome_names)
    return (
        {
            "status": "applied",
            "plan_id": confirm_plan,
            "outcomes": outcome_names,
            "receipt_id": receipt.stem,
            "rollup_verification": rollup_status,
        },
        is_partial or rollup_status == "pending",
    )


@declare_write("external")
@click.command(name="update-closeplan", context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("opportunity_id", required=False)
@click.option("--deal-id", help="Exact ClosePlan deal Id for a preview.")
@click.option("--score", "scores", multiple=True, help="Exact QUESTION_ID=NATIVE_SCORE assignment; repeat as needed.")
@click.option("--confirm", "confirm_plan", metavar="PLAN_ID", help="Confirm one previously reviewed preview plan.")
@click.option("--json", "as_json", is_flag=True, help="Emit structural JSON output.")
def cli(
    opportunity_id: str | None,
    deal_id: str | None,
    scores: tuple[str, ...],
    confirm_plan: str | None,
    as_json: bool,
) -> None:
    """Preview or confirm guarded native ClosePlan question scores.

    Default mode creates a PII-minimized preview plan. Confirmation accepts only
    that plan identifier, rereads every binding, and stops on any conflict or
    uncertain response. It never writes answer fields or derived rollups.
    """
    with cli_main():
        try:
            preview_arguments: tuple[str, str, dict[str, float]] | None = None
            if confirm_plan is None:
                preview_arguments = _validated_preview_arguments(opportunity_id, deal_id, scores)
            else:
                _validate_confirmation_arguments(opportunity_id, deal_id, scores)

            sid = get_sf_session_id()
            if not sid:
                raise _sf.SFAuthError("No Salesforce session. Run: fieldkit auth sf")
            base_url = get_sf_rest_base_url()
            with _sf.SFDirectClient(session_id=sid, base_url=base_url) as client:
                if preview_arguments is not None:
                    validated_opportunity_id, validated_deal_id, requested_scores = preview_arguments
                    _emit(
                        _create_preview_payload(
                            client,
                            base_url=base_url,
                            opportunity_id=validated_opportunity_id,
                            deal_id=validated_deal_id,
                            requested_scores=requested_scores,
                        ),
                        as_json=as_json,
                    )
                    return

                assert confirm_plan is not None
                preview = load_score_preview(confirm_plan)
                payload, is_partial = _confirm_preview_payload(
                    client,
                    base_url=base_url,
                    preview=preview,
                    confirm_plan=confirm_plan,
                )
            _emit(
                payload,
                as_json=as_json,
            )
            if is_partial:
                raise SystemExit(EXIT_PARTIAL)
        except ClosePlanPlanError as exc:
            click.echo(f"ERROR: {exc}", err=True)
            raise SystemExit(EXIT_DATA) from None
