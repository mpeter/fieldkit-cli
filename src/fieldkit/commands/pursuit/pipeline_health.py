"""fieldkit.pursuit.pipeline_health — Deterministic pipeline health check.

Scans all active pursuit files and classifies each deal by risk tier based on:
  - Close date overdue or approaching
  - Missing Salesforce fields
  - Deal stage vs timeline alignment

Current qualification is explicitly unavailable until a Salesforce-native
policy is ratified. Historical local scores do not affect risk classification.

Public API:
  classify_pursuit(result, today) → RiskItem
  health_check(root, account_filter, today) → list[RiskItem]
"""

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

import click

from fieldkit.cli_exit import EXIT_DATA, EXIT_PARTIAL
from fieldkit.commands.pursuit.audit import (
    AuditResult,
    _parse_sf_date,
    no_files_message,
)
from fieldkit.config import get_fieldkit_home
from fieldkit.pursuit.enums import Stage
from fieldkit.pursuit.io import ReportAssessment, ReportInput, read_pursuit_for_report, scan_report_inputs

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Stages to skip in health check
SKIP_STAGES = frozenset({Stage.CLOSED_WON, Stage.CLOSED_LOST, Stage.PRE_PIPELINE})
# Stages omitted from health by default — too early for meaningful risk scoring.
# Unlike SKIP_STAGES, these can be surfaced with --include-prospect.
_EARLY_STAGES = frozenset({Stage.PROSPECT})

LATE_STAGES = frozenset({Stage.PROPOSE, Stage.NEGOTIATE, Stage.CLOSED_WON})

LOG_PREFIX = "[pursuit-health]"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class RiskItem:
    relative_path: str
    stage: str
    qualification_status: Literal["unavailable"]
    close_date_str: str
    days_until_close: int | None  # None = no date
    days_in_stage: int | None  # None = no last-transition date
    risk_tier: str  # "HIGH", "MEDIUM", "LOW"
    risk_reasons: list[str]
    sf_opportunity_id: str


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _classify_high_risk(
    days_until: int | None,
    raw_close: str,
    reasons: list[str],
) -> str:
    """Return 'HIGH' and populate reasons for overdue close dates."""
    tier = "LOW"
    if days_until is not None and days_until < 0:
        reasons.append(f"Overdue close date ({raw_close})")
        tier = "HIGH"
    return tier


def _classify_medium_risk(
    days_until: int | None,
    stage: str,
    sf_opp_id: str,
    reasons: list[str],
) -> str:
    """Populate medium-risk reasons and return 'MEDIUM' if any apply, else 'LOW'."""
    tier = "LOW"
    if days_until is not None and 0 <= days_until <= 30 and stage not in LATE_STAGES:
        reasons.append(f"Close in {days_until}d but stage='{stage}'")
        tier = "MEDIUM"
    if not sf_opp_id:
        reasons.append("Missing sf_opportunity_id")
        tier = "MEDIUM"
    return tier


def classify_pursuit(result: AuditResult, today: date, frontmatter: dict[str, Any] | None = None) -> RiskItem | None:
    """Classify a parsed AuditResult into a RiskItem.

    Returns None if the pursuit should be excluded from the health report
    (closed stage or parse error with no meaningful data).
    """
    fm = frontmatter if frontmatter is not None else read_pursuit_for_report(result.path).frontmatter
    if fm is None:
        return None

    stage = str(fm.get("stage", "")).lower()
    if stage in SKIP_STAGES:
        return None
    # _EARLY_STAGES are filtered at the health_check level based on include_prospect.
    # We store stage on the item so the caller can filter after classification.

    raw_close = fm.get("sf_close_date") or fm.get("sf-close-date") or ""
    close_date = _parse_sf_date(raw_close)
    days_until: int | None = None
    if close_date:
        days_until = (close_date - today).days

    sf_opp_id = str(fm.get("sf_opportunity_id") or fm.get("sf-opportunity-id") or "")

    # Days in current stage — derived from last-transition date in frontmatter.
    raw_transition = str(fm.get("last-transition") or "")
    transition_date = _parse_sf_date(raw_transition)
    days_in_stage: int | None = (today - transition_date).days if transition_date else None

    reasons: list[str] = []
    tier = _classify_high_risk(days_until, raw_close, reasons)
    if tier == "LOW":
        tier = _classify_medium_risk(days_until, stage, sf_opp_id, reasons)

    return RiskItem(
        relative_path=result.relative_path,
        stage=stage,
        qualification_status="unavailable",
        close_date_str=str(raw_close),
        days_until_close=days_until,
        days_in_stage=days_in_stage,
        risk_tier=tier,
        risk_reasons=reasons,
        sf_opportunity_id=sf_opp_id,
    )


def _reportable_item(report_input: ReportInput, today: date, include_prospect: bool) -> RiskItem | None:
    """Classify one scanned input, or return None when the report excludes it."""
    result = AuditResult(path=report_input.path, relative_path=report_input.relative_path)
    item = classify_pursuit(result, today, report_input.frontmatter)
    if item is None:
        return None
    if not include_prospect and item.stage in _EARLY_STAGES:
        return None
    return item


def health_check(
    root: Path,
    account_filter: str | None = None,
    today: date | None = None,
    include_prospect: bool = False,
    assessment: ReportAssessment | None = None,
) -> list[RiskItem]:
    """Run health check across all pursuit files.

    Args:
        root:           Data root directory (contains `accounts/` subdirectory).
        account_filter:   If set, only check files under accounts/<account_filter>/.
        today:            Reference date (defaults to today).
        include_prospect: If True, include prospect-stage pursuits (pre-pipeline
                          pursuits are always excluded, regardless of this flag).
                          Defaults to False — these are too early for risk scoring.

    Returns:
        List of RiskItems sorted by risk tier (HIGH first) then by days_until_close.
    """
    if today is None:
        today = datetime.now(tz=UTC).date()

    if assessment is None:
        assessment = ReportAssessment()
    items: list[RiskItem] = []

    for report_input in scan_report_inputs(root, account_filter, assessment):
        item = _reportable_item(report_input, today, include_prospect)
        if item is not None:
            items.append(item)
    assessment.finish(len(items))

    # Sort: HIGH → MEDIUM → LOW, then by days_until_close ascending (None = far future)
    tier_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}

    def sort_key(x: RiskItem) -> tuple[int, int]:
        d = x.days_until_close if x.days_until_close is not None else 9999
        return (tier_order.get(x.risk_tier, 3), d)

    return sorted(items, key=sort_key)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _close_label(item: RiskItem) -> str:
    """Close date with an overdue or due-soon day count appended."""
    close_str = item.close_date_str or "—"
    days = item.days_until_close
    if days is None:
        return close_str
    if days < 0:
        return f"{close_str} ({abs(days)}d OVR)"
    if days <= 30:
        return f"{close_str} ({days}d)"
    return close_str


def _health_row(item: RiskItem) -> str:
    """Format one pursuit as a health-table row."""
    days_str = f"{item.days_in_stage}d" if item.days_in_stage is not None else "—"
    reasons_str = "; ".join(item.risk_reasons) if item.risk_reasons else "—"
    tier_icon = {"HIGH": "✗", "MEDIUM": "⚠", "LOW": "✓"}.get(item.risk_tier, "?")
    name = item.relative_path.replace("/pursuits/", "/").replace(".md", "")
    return (
        f"{name:<45} {item.stage:<12} {days_str:>5} {item.qualification_status:>13} "
        f"{_close_label(item):>10} {tier_icon} {item.risk_tier:<6} {reasons_str}"
    )


def _health_exit_code(high: list[RiskItem], strict: bool, assessment: ReportAssessment | None) -> int:
    """EXIT_PARTIAL for an incomplete assessment or HIGH-risk items under --strict, else 0."""
    if assessment is not None and assessment.failures:
        return EXIT_PARTIAL
    if strict and high:
        return EXIT_PARTIAL
    return 0


def _emit_health_results(
    items: list[RiskItem],
    *,
    high: list[RiskItem],
    medium: list[RiskItem],
    low: list[RiskItem],
    today: date,
    as_json: bool,
    compact: bool,
    strict: bool,
    assessment: ReportAssessment | None = None,
) -> None:
    """Emit health results to stdout and raise SystemExit with the appropriate code.

    Extracted from ``cli()`` to reduce its cyclomatic complexity (CRAP gate).
    Handles both JSON and human-readable table output modes.

    Raises:
        SystemExit(0): valid report, or no HIGH-risk items in strict mode.
        SystemExit(1): one or more HIGH-risk items in strict mode.
    """
    # implementation change: --json output
    if as_json:
        import dataclasses

        # The JSON contract stays a list; consumers (web dashboard, sf-reconcile) parse it as
        # one. An incomplete assessment is disclosed on stderr and by EXIT_PARTIAL instead.
        click.echo(json.dumps([dataclasses.asdict(i) for i in items], indent=2, default=str))
        raise SystemExit(_health_exit_code(high, strict, assessment))

    # Print table
    click.echo(f"\nPipeline Health — {today}")
    click.echo(
        f"{'Deal':<45} {'Stage':<12} {'Days in Stage':>13} {'Qualification':>13} {'Close':>10} {'Risk':<8} Reasons"
    )
    click.echo("-" * 124)

    for item in items:
        line = _health_row(item)
        # implementation change: --compact truncates to 80 chars
        click.echo(line[:80] if compact else line)

    click.echo("-" * (80 if compact else 124))
    click.echo(f"\nSummary: {len(high)} HIGH | {len(medium)} MEDIUM | {len(low)} LOW")

    # Exit codes:
    #   0 — valid report by default; no HIGH-risk items in strict mode
    #   1 — one or more HIGH-risk items with --strict
    if assessment is not None and assessment.failures:
        click.echo(assessment.summary_line())
    raise SystemExit(_health_exit_code(high, strict, assessment))


def _items_in_tier(items: list[RiskItem], tier: str) -> list[RiskItem]:
    return [i for i in items if i.risk_tier == tier]


@click.command(name="health")
@click.option("--account", "-a", default=None, help="Limit to a single account directory name.")
@click.option(
    "--include-prospect",
    is_flag=True,
    default=False,
    help="Include prospect-stage pursuits (excluded by default; pre-pipeline pursuits are always excluded, regardless of this flag).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit results as JSON array.")
@click.option(
    "--strict",
    is_flag=True,
    default=False,
    help="Exit 1 when HIGH-risk items are present; valid reports exit 0 by default.",
)
@click.option(
    "--compact",
    is_flag=True,
    default=False,
    help="Truncate output lines to 80 characters for narrow terminals.",
)
def cli(account: str | None, include_prospect: bool, as_json: bool, strict: bool, compact: bool) -> None:
    """Show pipeline health — risk-ranked table of active pursuits.

    Scans all active (non-closed) pursuit files and ranks them by risk tier:
      HIGH   — overdue close date
      MEDIUM — approaching close date at an early stage, or missing SF ID
      LOW    — no immediate concerns

    Current qualification is reported as unavailable; historical local scores
    are not used as current risk evidence.

    Exit codes:
      0 — valid report (default), or no high-risk items with --strict
      1 — incomplete assessment, or one or more high-risk items with --strict
      3 — data error (config missing, accounts directory not found)
    """
    data_root = get_fieldkit_home()
    if data_root is None:
        click.echo(f"{LOG_PREFIX} No data root configured. Run: fieldkit init", err=True)
        raise SystemExit(EXIT_DATA) from None

    root = Path(data_root)
    accounts_dir = root / "accounts"
    if not accounts_dir.is_dir():
        click.echo(f"{LOG_PREFIX} Accounts directory not found. Run 'fieldkit init' to initialize.", err=True)
        raise SystemExit(EXIT_DATA) from None

    today = datetime.now(tz=UTC).date()
    assessment = ReportAssessment()
    items = health_check(
        root, account_filter=account, today=today, include_prospect=include_prospect, assessment=assessment
    )
    for warning in assessment.failure_warnings():
        click.echo(warning, err=True)

    if not items and not assessment.failures:
        click.echo(no_files_message("pursuit", account), err=True)
        raise SystemExit(EXIT_DATA) from None

    high, medium, low = (_items_in_tier(items, tier) for tier in ("HIGH", "MEDIUM", "LOW"))

    _emit_health_results(
        items,
        high=high,
        medium=medium,
        low=low,
        today=today,
        as_json=as_json,
        compact=compact,
        strict=strict,
        assessment=assessment,
    )
