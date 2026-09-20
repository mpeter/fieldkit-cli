"""fieldkit sf meddpicc — Read the ClosePlan/TSPC scorecard for an opportunity.

Fetches every TSPC__Deal__c scorecard and exact question from the ClosePlan
managed package (TSPC__ namespace) via Salesforce UI API child relationships.

Not all opportunities have a ClosePlan. This case is handled gracefully with a
clear "no ClosePlan configured" message.

Usage:
    fieldkit sf meddpicc <opp_id> [--deal-id <deal_id>] [--json]
"""

import json
import math
import re

import click

import fieldkit.sf.client as _sf
from fieldkit.cli_exit import EXIT_DATA, cli_main
from fieldkit.config import get_sf_rest_base_url, get_sf_session_id
from fieldkit.sf.meddpicc import CANONICAL_ELEMENTS, read_meddpicc
from fieldkit.sf.types import MeddpiccDeal, MeddpiccElement, MeddpiccQuestion, MeddpiccReadResult

# Salesforce record IDs are 15 or 18 alphanumeric characters.
# NOTE: a copy of this pattern also exists in account.py and quote.py.
# Tracked for extraction in implementation change (fieldkit.sf shared validator).
_OPP_ID_RE = re.compile(r"^[A-Za-z0-9]{15}([A-Za-z0-9]{3})?$")


def _float_value(value: object) -> float | None:
    """Return a float only for the scalar types accepted by score formatting."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return numeric if math.isfinite(numeric) else None


def _fmt_score_ratio(ratio: object) -> str:
    """Format a scorecard ratio as a percentage string.

    TSPC__ScorecardScoreRatio__c can be stored as an already-0-100 percentage
    (for example, raw value 63.0 for a 63% scorecard), where unconditionally
    multiplying by 100 would produce "6300%". Both 0.0-1.0-fraction and
    already-a-percentage conventions exist across Salesforce orgs with no
    schema-level way to tell them apart, so treat anything <= 1.0 as a
    fraction (multiply by 100) and anything above as already a percentage.
    """
    if ratio is None:
        return "(not set)"
    value = _float_value(ratio)
    if value is None:
        return str(ratio)
    pct = value * 100 if value <= 1.0 else value
    return f"{pct:.0f}%"


def _fmt_score(score: object) -> str:
    """Format a native numeric value without discarding its fractional part."""
    if score is None:
        return "(not set)"
    value = _float_value(score)
    if value is None:
        return str(score)
    return str(int(value)) if value.is_integer() else str(value)


def fetch_meddpicc_scorecard(opp_id: str, *, deal_id: str | None = None) -> MeddpiccReadResult:
    """Fetch the mutation-safe native ClosePlan read contract.

    Raises:
        ConfigError: when the SF base URL is not configured (→ EXIT_DATA 3).
        SFAuthError: on HTTP 401 (propagates to ``cli_main()`` → EXIT_AUTH 2).
        SFAPIError:  on other HTTP errors (propagates to ``cli_main()`` → EXIT_DATA 3).
    """
    sid = get_sf_session_id()
    if not sid:
        raise _sf.SFAuthError("No Salesforce session. Run: fieldkit auth sf")
    base_url = get_sf_rest_base_url()

    with _sf.SFDirectClient(session_id=sid, base_url=base_url) as client:
        return read_meddpicc(
            client,
            org_url=base_url,
            opportunity_id=opp_id,
            selected_deal_id=deal_id,
        )


def _print_questions(questions: list[MeddpiccQuestion]) -> None:
    """Print question evidence using the shared human-readable layout."""
    for item in questions:
        name = item.get("name") or "(unnamed)"
        score = item.get("score")
        answer = item.get("answer") or "(no answer)"
        score_str = f"score={_fmt_score(score)}" if score is not None else "score=(not set)"
        click.echo(f"    [{item.get('question_id') or 'missing Id'}] {name}  {score_str}")
        click.echo(f"      {answer}")
        click.echo(f"      Native maximum: {_fmt_score(item.get('score_maximum'))}")
        click.echo(f"      Question type: {item.get('question_type') or 'unknown'}")
        click.echo(f"      Template version: {_fmt_score(item.get('template_version'))}")
        click.echo("      Separate weight: not defined by package")
        click.echo(f"      Template metadata: {item['template_metadata_status']}")
        template = item["template_question"]
        if template is not None:
            click.echo(
                "      Template question: "
                f"{template['template_question_id']} "
                f"(template={template['template_id'] or 'null'}, "
                f"category={template['category_id'] or 'null'}, "
                f"question_category={template['question_category_id'] or 'null'})"
            )
            click.echo(f"      Template maximum: {_fmt_score(template['score_maximum'])}")
            reported_count = template["answer_choices_reported_count"]
            click.echo(
                f"      Answer model: {template['answer_model']} "
                f"(complete={'yes' if template['answer_choices_complete'] else 'no'}, "
                f"reported={reported_count if reported_count is not None else 'unknown'})"
            )
            for choice in template["answer_choices"]:
                choice_label = choice["text"] or choice["name"] or "(unnamed)"
                click.echo(
                    f"        [{choice['answer_id'] or 'missing Id'}] {choice_label} "
                    f"max={_fmt_score(choice['max_score'])} sort={_fmt_score(choice['sort_order'])}"
                )
        click.echo(f"      LastModifiedDate: {item.get('last_modified_date') or 'unknown'}")
        click.echo("      Concurrency: weak timestamp evidence; mutation disabled")


def _print_element(element: MeddpiccElement) -> None:
    """Print one canonical MEDDPICC element and its question evidence."""
    click.echo(f"\n  {element['label']}: {element['state']}")
    if element["questions"]:
        _print_questions(element["questions"])
    else:
        click.echo("    (no data)")


def _print_deal(deal: MeddpiccDeal, *, selected_deal_id: str | None) -> None:
    marker = " (selected)" if deal["deal_id"] == selected_deal_id else ""
    click.echo(f"\n  Deal: {deal['deal_id'] or '(missing Id)'}{marker}")
    click.echo(f"  Name: {deal['name'] or '(unnamed)'}")
    click.echo(f"  Score Ratio:   {_fmt_score_ratio(deal['score_ratio'])}")
    click.echo(f"  Total Score:   {_fmt_score(deal['total_score'])}")
    click.echo(f"  Template:      {deal['template_id'] or '(not set)'}")
    click.echo(f"  Template deployed: {deal['template_deploy_date'] or '(not set)'}")
    click.echo(f"  Template metadata: {deal['template_metadata_status']}")
    click.echo(f"  Template version: {_fmt_score(deal['template_version'])}")
    click.echo(f"  Template maximum: {_fmt_score(deal['template_total_maximum'])}")
    click.echo(f"  Question maximum total: {_fmt_score(deal['question_maximum_total'])}")
    consistency = deal["native_maximums_consistent"]
    click.echo(
        "  Native maximums consistent: " + ("unknown" if consistency is None else "yes" if consistency else "no")
    )
    click.echo(f"  LastModifiedDate: {deal['last_modified_date'] or 'unknown'}")
    count = deal["questions_reported_count"]
    click.echo(
        f"  Questions complete: {'yes' if deal['questions_complete'] else 'no'} "
        f"(reported={count if count is not None else 'unknown'})"
    )

    click.echo()
    click.echo("  -- MEDDPICC Elements --")
    for element in deal["elements"]:
        _print_element(element)

    labels = dict(CANONICAL_ELEMENTS)
    click.echo()
    click.echo("  Gaps: " + (", ".join(labels[key] for key in deal["gaps"]) if deal["gaps"] else "none"))

    if deal["unmapped"]:
        click.echo()
        click.echo("  -- Unmapped ClosePlan Questions --")
        _print_questions(deal["unmapped"])


def print_scorecard(scorecard: MeddpiccReadResult) -> None:
    """Print the complete, identity-preserving native read contract."""
    click.echo(f"\n{'=' * 60}")
    click.echo(f"  ClosePlan MEDDPICC -- {scorecard['opportunity_id']}")
    click.echo(f"{'=' * 60}")
    click.echo(f"  Salesforce Org: {scorecard['org_url']}")
    click.echo(f"  Read status: {scorecard['status']}")
    if scorecard["status"] == "not_found":
        click.echo("  No ClosePlan is linked to this opportunity.")
    elif scorecard["status"] == "ambiguous":
        click.echo("  Multiple ClosePlan deals are linked. Re-run with --deal-id and one exact Id.")

    for deal in scorecard["deals"]:
        _print_deal(deal, selected_deal_id=scorecard["selected_deal_id"])

    click.echo()
    if scorecard["metadata_gaps"]:
        click.echo("  Native metadata unknown: " + ", ".join(scorecard["metadata_gaps"]))
    else:
        click.echo("  Native metadata gaps: none")
    if scorecard["issues"]:
        click.echo("  Issues:")
        for issue in scorecard["issues"]:
            click.echo(f"    - {issue}")

    click.echo()


def _validate_record_id(record_id: str, *, label: str) -> None:
    """Exit with the data-error contract when a Salesforce record ID is malformed."""
    if _OPP_ID_RE.match(record_id):
        return
    click.echo(
        f"ERROR: Invalid {label} ID {record_id!r}. Expected 15 or 18 alphanumeric characters.",
        err=True,
    )
    raise SystemExit(EXIT_DATA)


def _exit_for_result(scorecard: MeddpiccReadResult) -> None:
    """Apply the CLI exit-code contract after rendering a native read result."""
    if scorecard["status"] == "invalid_selection":
        raise SystemExit(EXIT_DATA)
    if scorecard["status"] in {"ambiguous", "incomplete"}:
        raise SystemExit(1)


@click.command(
    name="meddpicc",
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
)
@click.argument("opp_id")
@click.option("--deal-id", help="Select one exact linked ClosePlan deal ID; all linked deals are still read.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the scorecard as JSON.")
def cli(opp_id: str, deal_id: str | None, as_json: bool) -> None:
    """Read the ClosePlan/TSPC MEDDPICC scorecard for an opportunity.

    OPP_ID is the 15- or 18-character Salesforce Opportunity ID.

    Fetches every linked TSPC__Deal__c scorecard and question. When several
    deals are linked, use --deal-id to select one exact deal; none is selected
    from response order.

    Opportunities without a ClosePlan display a clear "no ClosePlan configured"
    message rather than an error.
    """
    with cli_main():
        _validate_record_id(opp_id, label="opportunity")
        if deal_id is not None:
            _validate_record_id(deal_id, label="deal")

        scorecard = fetch_meddpicc_scorecard(opp_id, deal_id=deal_id)

        if as_json:
            click.echo(json.dumps(scorecard, indent=2, default=str))
        else:
            print_scorecard(scorecard)

        _exit_for_result(scorecard)
