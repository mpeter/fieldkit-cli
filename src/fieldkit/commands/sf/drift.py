"""fieldkit sf drift — report where pursuit files have drifted from Salesforce.

Read-only and portfolio-wide: every pursuit in pipeline-review scope that links
an opportunity is fetched live and compared with its stored ``sf_*`` snapshot.
Nothing is written; ``fieldkit sf opportunity <id> <file>`` syncs one pursuit.

Not to be confused with ``fieldkit sf reconcile``, which rewrites the Key Fields
table of a single pursuit file from its own frontmatter.
"""

import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, TypedDict

import click

from fieldkit.cli_exit import EXIT_AUTH, EXIT_DATA, EXIT_PARTIAL, EXIT_SUCCESS
from fieldkit.commands.sf.sync import PLACEHOLDER_VALUES
from fieldkit.config import get_fieldkit_home, get_sf_rest_base_url, get_sf_session_id
from fieldkit.pursuit.drift import DriftFlag, DriftStatus, LiveOpportunity, assess_drift, drift_status
from fieldkit.pursuit.io import ReportAssessment, ReportFailure, ReportInput, scan_report_inputs
from fieldkit.pursuit.stages import ALL_STAGES, TERMINAL_STAGES, in_review_scope
from fieldkit.sf.client import SFAPIError, SFDirectClient, SFNotFoundError
from fieldkit.sf.opportunities import is_opportunity_id
from fieldkit.sf.types import OpportunitySObject

LOG_PREFIX = "[sf-drift]"

_DRIFT_FIELDS = "Id,Name,StageName,CloseDate,IsClosed,Consulting_Total_USD__c"
_STATUS_ORDER: dict[DriftStatus, int] = {"RED": 0, "YELLOW": 1, "GREEN": 2}


@dataclass
class DriftRow:
    """One linked pursuit and how it compares with its live opportunity."""

    opportunity_id: str
    pursuit: str  # workspace-relative, e.g. accounts/acme-corp/pursuits/renewal.md
    name: str | None
    status: DriftStatus
    flags: list[DriftFlag]
    live: LiveOpportunity | None


class DriftCounts(TypedDict):
    red: int
    yellow: int
    green: int
    total: int


class UnassessedPursuit(TypedDict):
    pursuit: str
    reason: str


class DriftReport(TypedDict):
    """The ``--json`` contract: completeness, counts, unreadable files, rows RED first."""

    complete: bool
    counts: DriftCounts
    unassessed: list[UnassessedPursuit]
    opportunities: list[dict[str, Any]]


def _opportunity_id(frontmatter: dict[str, object]) -> str:
    return str(frontmatter.get("sf_opportunity_id") or "").strip()


def select_linked_pursuits(
    root: Path, account: str | None, *, include_prospect: bool
) -> tuple[list[ReportInput], ReportAssessment]:
    """Return in-scope pursuits that link an opportunity, and the scan assessment.

    A blank or placeholder ``sf_opportunity_id`` (``TBD``) means not yet linked. Any
    other value that is not a Salesforce record id is reported as unassessed and is
    never sent to Salesforce. A linked pursuit whose ``stage`` is missing or not a
    known stage cannot be placed in review scope, so it is unassessed as well.
    """
    assessment = ReportAssessment()
    linked: list[ReportInput] = []
    for report_input in scan_report_inputs(root, account, assessment):
        opp_id = _opportunity_id(report_input.frontmatter)
        if opp_id.lower() in PLACEHOLDER_VALUES:
            continue
        stage = report_input.frontmatter.get("stage")
        if not isinstance(stage, str) or stage.strip().lower() not in ALL_STAGES | TERMINAL_STAGES:
            reason = "stage is missing or not a recognized pursuit stage"
            assessment.failures.append(ReportFailure(report_input.relative_path, reason))
            continue
        if not in_review_scope(stage.strip(), include_prospect=include_prospect):
            continue
        if not is_opportunity_id(opp_id):
            reason = "sf_opportunity_id is not a 15- or 18-character Salesforce id"
            assessment.failures.append(ReportFailure(report_input.relative_path, reason))
            continue
        linked.append(report_input)
    assessment.finish(len(linked))
    return linked, assessment


def live_from_record(record: OpportunitySObject) -> LiveOpportunity:
    """Map a Salesforce Opportunity record to the values drift detection compares."""
    return {
        "stage": record.get("StageName"),
        "close_date": record.get("CloseDate"),
        "consulting_acv": record.get("Consulting_Total_USD__c"),
        "is_closed": bool(record.get("IsClosed")),
    }


def _unfetched_row(opp_id: str, pursuit: str, flag: DriftFlag) -> DriftRow:
    return DriftRow(opp_id, pursuit, None, "RED", [flag], None)


def assess_pursuit(client: SFDirectClient, report_input: ReportInput, today: date) -> DriftRow:
    """Fetch one pursuit's opportunity and compare it.

    An opportunity Salesforce reports as missing is a RED finding (a stale id that a
    retry cannot fix); any other failed request is a RED row that leaves the report
    incomplete. ``SFAuthError`` propagates so a dead session stops the run with exit 2
    instead of reading as partial drift.
    """
    opp_id = _opportunity_id(report_input.frontmatter)
    pursuit = f"accounts/{report_input.relative_path}"
    try:
        record = client.fetch_record(opp_id, fields=_DRIFT_FIELDS)
    except SFNotFoundError:
        detail = "no such opportunity in Salesforce; check sf_opportunity_id"
        return _unfetched_row(opp_id, pursuit, DriftFlag("RED", "opportunity-not-found", detail))
    except SFAPIError:
        return _unfetched_row(opp_id, pursuit, DriftFlag("RED", "sf-fetch-failed", "Salesforce request failed"))
    live = live_from_record(record)
    flags = assess_drift(report_input.frontmatter, live, today)
    return DriftRow(opp_id, pursuit, record.get("Name"), drift_status(flags), flags, live)


def _sorted(rows: list[DriftRow]) -> list[DriftRow]:
    return sorted(rows, key=lambda row: (_STATUS_ORDER[row.status], row.pursuit))


def build_report(rows: list[DriftRow], assessment: ReportAssessment) -> DriftReport:
    """Build the report: completeness, counts, unassessed files, then rows RED first."""
    unassessed: list[UnassessedPursuit] = [
        {"pursuit": f"accounts/{failure.relative_path}", "reason": failure.reason} for failure in assessment.failures
    ]
    fetch_failed = any(flag.code == "sf-fetch-failed" for row in rows for flag in row.flags)

    def count(status: DriftStatus) -> int:
        return sum(1 for row in rows if row.status == status)

    return {
        "complete": not unassessed and not fetch_failed,
        "counts": {"red": count("RED"), "yellow": count("YELLOW"), "green": count("GREEN"), "total": len(rows)},
        "unassessed": unassessed,
        "opportunities": [asdict(row) for row in _sorted(rows)],
    }


def _echo_table(rows: list[DriftRow], report: DriftReport) -> None:
    unassessed = report["unassessed"]
    counts = report["counts"]
    for item in unassessed:
        click.echo(f"NOT ASSESSED  {item['pursuit']}: {item['reason']}")
    for row in _sorted(rows):
        detail = "; ".join(f"{flag.code}: {flag.detail}" for flag in row.flags) or "clean"
        click.echo(f"{row.status:<6}  {row.pursuit}  {detail}")
    click.echo(
        f"\n{counts['red']} RED, {counts['yellow']} YELLOW, {counts['green']} GREEN"
        f" of {counts['total']} linked pursuits; {len(unassessed)} not assessed"
    )


def _open_client() -> SFDirectClient:
    base_url = get_sf_rest_base_url()
    if not base_url:
        click.echo(
            f"{LOG_PREFIX} Salesforce org URL is not configured. Set sf_org_url in the fieldkit config.", err=True
        )
        raise SystemExit(EXIT_DATA)
    session_id = get_sf_session_id()
    if not session_id:
        click.echo(f"{LOG_PREFIX} No Salesforce session. Run: fieldkit auth sf", err=True)
        raise SystemExit(EXIT_AUTH)
    return SFDirectClient(session_id=session_id, base_url=base_url)


@click.command(name="drift")
@click.option("-a", "--account", default=None, help="Limit to a single account directory name.")
@click.option("--include-prospect", is_flag=True, default=False, help="Include prospect-stage pursuits.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the report as JSON.")
def cli(account: str | None, include_prospect: bool, as_json: bool) -> None:
    """Report pursuits whose stored Salesforce snapshot has drifted from live Salesforce.

    Compares stage, close date and consulting ACV, and flags open opportunities
    that are overdue or close within 14 (RED) or 30 (YELLOW) days. Read-only.

    \b
    Exit codes:
      0 — complete report (drift is reported, not an error)
      1 — incomplete: unreadable pursuit files, linked pursuits with an invalid
          opportunity id or an unrecognized stage, or failed Salesforce requests
      2 — a pursuit needs fetching and the Salesforce session is missing or
          expired; run: fieldkit auth sf
      3 — no workspace or accounts directory, unknown --account, or a pursuit
          needs fetching and no sf_org_url is configured
    """
    root = get_fieldkit_home()
    if root is None or not (Path(root) / "accounts").is_dir():
        click.echo(f"{LOG_PREFIX} Accounts directory not found. Run 'fieldkit init' to initialize.", err=True)
        raise SystemExit(EXIT_DATA)
    if account is not None and not (Path(root) / "accounts" / account).is_dir():
        click.echo(f"{LOG_PREFIX} Account directory not found: accounts/{account}", err=True)
        raise SystemExit(EXIT_DATA)

    linked, assessment = select_linked_pursuits(Path(root), account, include_prospect=include_prospect)
    rows: list[DriftRow] = []
    if linked:
        today = datetime.now(tz=UTC).date()
        with _open_client() as client:
            for index, report_input in enumerate(linked, start=1):
                click.echo(f"{LOG_PREFIX} {index}/{len(linked)} {report_input.relative_path}", err=True)
                rows.append(assess_pursuit(client, report_input, today))

    report = build_report(rows, assessment)
    if as_json:
        click.echo(json.dumps(report, indent=2))
    else:
        _echo_table(rows, report)
    raise SystemExit(EXIT_SUCCESS if report["complete"] else EXIT_PARTIAL)
