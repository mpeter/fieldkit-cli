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
from fieldkit.config import get_fieldkit_home, get_sf_rest_base_url, get_sf_session_id
from fieldkit.pursuit.drift import DriftFlag, DriftStatus, LiveOpportunity, assess_drift, drift_status
from fieldkit.pursuit.io import ReportAssessment, ReportInput, scan_report_inputs
from fieldkit.pursuit.stages import in_review_scope
from fieldkit.sf.client import SFAPIError, SFDirectClient, SFNotFoundError
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
    return str(frontmatter.get("sf_opportunity_id") or frontmatter.get("sf-opportunity-id") or "").strip()


def select_linked_pursuits(
    root: Path, account: str | None, *, include_prospect: bool
) -> tuple[list[ReportInput], ReportAssessment]:
    """Return in-scope pursuits that link an opportunity, and the scan assessment."""
    assessment = ReportAssessment()
    linked = [
        report_input
        for report_input in scan_report_inputs(root, account, assessment)
        if in_review_scope(str(report_input.frontmatter.get("stage", "")), include_prospect=include_prospect)
        and _opportunity_id(report_input.frontmatter)
    ]
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


def _failed_row(opp_id: str, pursuit: str, detail: str) -> DriftRow:
    flags = [DriftFlag("RED", "sf-fetch-failed", detail)]
    return DriftRow(opp_id, pursuit, None, "RED", flags, None)


def assess_pursuit(client: SFDirectClient, report_input: ReportInput, today: date) -> DriftRow:
    """Fetch one pursuit's opportunity and compare it.

    A missing or failed record becomes a RED row; ``SFAuthError`` propagates so a
    dead session stops the run with exit 2 instead of reading as partial drift.
    """
    opp_id = _opportunity_id(report_input.frontmatter)
    pursuit = f"accounts/{report_input.relative_path}"
    try:
        record = client.fetch_record(opp_id, fields=_DRIFT_FIELDS)
    except SFNotFoundError:
        return _failed_row(opp_id, pursuit, "opportunity not found in Salesforce")
    except SFAPIError:
        return _failed_row(opp_id, pursuit, "Salesforce request failed; retry later")
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
    session_id = get_sf_session_id()
    base_url = get_sf_rest_base_url()
    if not session_id or not base_url:
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
      1 — incomplete: unreadable pursuit files or failed opportunity fetches
      2 — Salesforce session missing or expired; run: fieldkit auth sf
      3 — no workspace or accounts directory
    """
    root = get_fieldkit_home()
    if root is None or not (Path(root) / "accounts").is_dir():
        click.echo(f"{LOG_PREFIX} Accounts directory not found. Run 'fieldkit init' to initialize.", err=True)
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
