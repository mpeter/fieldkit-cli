"""fieldkit.pursuit.forecast — Deterministic weighted pipeline forecast.

Reads pursuit frontmatter to compute:
  - Commit scenario: closed-won + negotiate deals
  - Best-case scenario: all non-closed active deals
  - Weighted scenario: probability-weighted sum by stage

Stage probability weights:
  closed-won  → 1.00
  negotiate   → 0.75
  propose     → 0.50
  validate    → 0.25
  discover    → 0.10
  qualify     → 0.05

Public API:
  DealRow: a single deal's forecast data
  compute_forecast(root, account_filter, quota, today) → ForecastResult
"""

import dataclasses
import json
import warnings
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import click
from pydantic import ValidationError

from fieldkit.cli_exit import EXIT_DATA, EXIT_PARTIAL
from fieldkit.commands.pursuit.audit import (
    assessment_failure_warnings,
    assessment_summary_line,
    echo_reserved_skips,
    no_files_message,
)
from fieldkit.config import get_fieldkit_home, get_pipeline_quota
from fieldkit.errors import FieldkitError
from fieldkit.pursuit.enums import Stage
from fieldkit.pursuit.io import ReportAssessment, is_reserved_pursuit_path, read_pursuit_for_report, scan_report_inputs
from fieldkit.pursuit.models import PursuitFrontmatter
from fieldkit.pursuit.stage_weights import STAGE_WEIGHTS
from fieldkit.sf.components import effective_net_consulting_acv

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# historic regression: STAGE_WEIGHT is now imported from the shared stage_weights module.
# Both forecast.py and pipeline/quota.py use the same weights so they report
# consistent weighted pipeline numbers.
STAGE_WEIGHT: dict[str, float] = STAGE_WEIGHTS

COMMIT_STAGES = frozenset({Stage.CLOSED_WON, Stage.NEGOTIATE})
ACTIVE_STAGES = frozenset(STAGE_WEIGHT.keys()) - frozenset({Stage.CLOSED_WON, Stage.CLOSED_LOST})
SKIP_STAGES = frozenset({Stage.CLOSED_LOST, Stage.PRE_PIPELINE, Stage.PROSPECT})

LOG_PREFIX = "[pursuit-forecast]"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class DealRow:
    relative_path: str
    name: str
    stage: str
    acv: float  # ACV in USD
    close_date_str: str
    weight: float


@dataclass
class ForecastResult:
    deals: list[DealRow]
    commit: float  # closed-won + negotiate
    best_case: float  # all active deals
    weighted: float  # probability-weighted
    quota: float | None
    closed_won: float
    skipped: list[str]  # stage strings of deals dropped for unknown stage (historic regression)
    assessment: ReportAssessment = field(default_factory=ReportAssessment)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _normalize_forecast_amounts(stage: str, frontmatter: dict[str, object]) -> PursuitFrontmatter:
    """Normalize forecast inputs without exposing raw values in model warnings."""
    with warnings.catch_warnings(record=True) as validation_warnings:
        warnings.simplefilter("always", UserWarning)
        try:
            amounts = PursuitFrontmatter.model_validate(
                {
                    "stage": stage,
                    "sf_consulting_acv": frontmatter.get("sf_consulting_acv"),
                    "sf_acv": frontmatter.get("sf_acv"),
                    "sf_arr": frontmatter.get("sf_arr"),
                }
            )
        except ValidationError:
            raise FieldkitError(
                "Invalid forecast amount in sf_consulting_acv, sf_acv, or sf_arr. Expected a numeric value or blank."
            ) from None
    if validation_warnings:
        click.echo(
            "WARNING: Malformed forecast amount in sf_consulting_acv, sf_acv, or sf_arr. "
            "Treating malformed amounts as missing.",
            err=True,
        )
    return amounts


def _extract_name_from_path(path: Path, accounts_dir: Path) -> str:
    """Derive a display name from the pursuit file path."""
    try:
        rel = str(path.relative_to(accounts_dir))
        # accounts/acme/pursuits/deal.md → acme/deal
        parts = rel.replace("/pursuits/", "/").replace(".md", "")
        return parts
    except ValueError:
        return path.stem


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _parse_deal_row(
    path: Path,
    accounts_dir: Path,
    skipped: list[str] | None = None,
) -> DealRow | None:
    """Parse a single pursuit file into a DealRow, or return None if it should be skipped.

    Args:
        path:         Path to the pursuit markdown file.
        accounts_dir: Root accounts directory (used for display name extraction).
        skipped:      Optional list to collect unrecognized stage strings (historic regression).
                      When a deal is dropped for an unknown stage, its stage value
                      is appended so callers can surface a summary warning.
    """
    if is_reserved_pursuit_path(path):
        return None
    fm = read_pursuit_for_report(path).frontmatter
    if fm is None:
        return None
    return _deal_row_from_frontmatter(path, fm, accounts_dir, skipped)


def _deal_row_from_frontmatter(
    path: Path,
    fm: dict[str, Any],
    accounts_dir: Path,
    skipped: list[str] | None = None,
) -> DealRow | None:
    """Build a DealRow from parsed frontmatter, or return None for closed or unweighted stages."""
    stage = str(fm.get("stage", "")).lower()
    if stage in SKIP_STAGES:
        return None
    if stage not in STAGE_WEIGHT:
        click.echo(
            f"WARNING: {path.name}: stage '{stage}' not in forecast weights — skipping",
            err=True,
        )
        if skipped is not None:
            skipped.append(stage)
        return None
    amounts = _normalize_forecast_amounts(stage, fm)
    acv = effective_net_consulting_acv(
        str(fm["sf_contract_type"]) if fm.get("sf_contract_type") is not None else None,
        amounts.sf_consulting_acv,
        amounts.sf_acv,
        amounts.sf_arr,
    )
    close_str = str(fm.get("sf_close_date") or fm.get("sf-close-date") or "")
    name = _extract_name_from_path(path, accounts_dir)
    # historic regression: use path relative to accounts root; fallback to filename only
    try:
        _rel = str(path.relative_to(accounts_dir))
    except ValueError:
        _rel = path.name
    return DealRow(
        relative_path=_rel,
        name=name,
        stage=stage,
        acv=acv,
        close_date_str=close_str,
        weight=STAGE_WEIGHT.get(stage, 0.0),
    )


def compute_forecast(
    root: Path,
    account_filter: str | None = None,
    quota: float | None = None,
    today: date | None = None,
) -> ForecastResult:
    """Compute a weighted pipeline forecast from all active pursuit files.

    Args:
        root:           Data root directory (contains `accounts/` subdirectory).
        account_filter: If set, only include files under accounts/<account_filter>/.
        quota:          Optional quota target in USD for gap-to-quota display.
        today:          Reference date (defaults to today).

    Returns:
        ForecastResult with commit/best-case/weighted totals and per-deal breakdown.
    """
    if today is None:
        today = datetime.now(tz=UTC).date()

    accounts_dir = root / "accounts"
    deals: list[DealRow] = []
    skipped: list[str] = []  # unrecognized stage strings (historic regression)

    assessment = ReportAssessment()
    for report_input in scan_report_inputs(root, account_filter, assessment):
        row = _deal_row_from_frontmatter(report_input.path, report_input.frontmatter, accounts_dir, skipped)
        if row is not None:
            deals.append(row)
    assessment.finish(len(deals))

    # Sort: by stage weight descending, then ACV descending
    deals.sort(key=lambda d: (-d.weight, -d.acv))

    commit = sum(d.acv for d in deals if d.stage in COMMIT_STAGES)
    best_case = sum(d.acv for d in deals if d.stage in ACTIVE_STAGES)
    weighted = sum(d.acv * d.weight for d in deals)
    closed_won = sum(d.acv for d in deals if d.stage == Stage.CLOSED_WON)

    return ForecastResult(
        deals=deals,
        commit=commit,
        best_case=best_case,
        weighted=weighted,
        quota=quota,
        closed_won=closed_won,
        skipped=skipped,
        assessment=assessment,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _fmt_usd(v: float) -> str:
    """Format a USD amount with commas and no cents."""
    return f"${v:,.0f}"


def _resolve_quota(quota: float | None) -> float | None:
    """Return the explicit quota, else the configured pipeline target when numeric."""
    if quota is not None:
        return quota
    quota_cfg = get_pipeline_quota()
    if isinstance(quota_cfg, dict):
        target_val = quota_cfg.get("target")
        if isinstance(target_val, (int, float)):
            return float(target_val)
    return None


def _warn_zero_acv(deals: list[DealRow]) -> None:
    """Warn on stderr about deals with $0 ACV (data quality issue)."""
    zero_acv_deals = [d.name for d in deals if d.acv == 0.0]
    if not zero_acv_deals:
        return
    click.echo(
        f"\nWARNING: {len(zero_acv_deals)} deal(s) have $0 ACV — update SF data:",
        err=True,
    )
    for name in zero_acv_deals:
        click.echo(f"  - {name}", err=True)


def _echo_deal_table(deals: list[DealRow], today: date) -> None:
    click.echo(f"\nPipeline Forecast — {today}")
    click.echo(f"{'Deal':<45} {'Stage':<12} {'Weight':>6} {'ACV':>12} {'Close'}")
    click.echo("-" * 90)
    for d in deals:
        won_marker = " (won)" if d.stage == Stage.CLOSED_WON else ""
        click.echo(f"{d.name:<45} {d.stage:<12} {d.weight:>5.0%} {_fmt_usd(d.acv):>12} {d.close_date_str}{won_marker}")
    click.echo("-" * 90)


def _quota_standing(gap: float) -> str:
    return "over quota" if gap <= 0 else "under quota"


def _echo_scenarios(result: ForecastResult, quota: float | None) -> None:
    click.echo("")
    click.echo("Scenarios:")
    click.echo(f"  Closed Won   : {_fmt_usd(result.closed_won):>12}")
    click.echo(f"  Commit       : {_fmt_usd(result.commit):>12}  (closed-won + negotiate)")
    click.echo(f"  Weighted     : {_fmt_usd(result.weighted):>12}  (probability-weighted)")
    click.echo(f"  Best Case    : {_fmt_usd(result.best_case):>12}  (pipeline total, active deals excl. closed-won)")

    if quota is not None:
        gap_commit = quota - result.commit
        gap_weighted = quota - result.weighted
        click.echo("")
        click.echo(f"Quota          : {_fmt_usd(quota):>12}")
        click.echo(f"Gap (commit)   : {_fmt_usd(gap_commit):>12}  ({_quota_standing(gap_commit)})")
        click.echo(f"Gap (weighted) : {_fmt_usd(gap_weighted):>12}  ({_quota_standing(gap_weighted)})")


@click.command(name="forecast")
@click.option("--account", "-a", default=None, help="Limit to a single account directory name.")
@click.option(
    "--quota",
    "-q",
    default=None,
    type=float,
    help="Quota target in USD (e.g. 2000000). Overrides config. Shows gap-to-quota.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Machine-readable JSON output.",
)
def cli(account: str | None, quota: float | None, as_json: bool) -> None:
    """Weighted pipeline forecast with scenario analysis.

    Reads pursuit frontmatter to compute three scenarios:
      Commit    — closed-won + negotiate deals (high confidence)
      Best-case — all active deals summed
      Weighted  — probability-weighted by stage

    Stage weights: closed-won=100%, negotiate=75%, propose=50%, validate=25%, discover=10%, qualify=5%

    When -q/--quota is not provided, reads pipeline.quota.target from config automatically.

    Exit codes:
      0 — complete forecast computed
      1 — partial forecast; unreadable records disclosed
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

    # implementation change: auto-read quota from config when -q not provided.
    # get_pipeline_quota() returns None (not raises) when quota is not configured.
    quota = _resolve_quota(quota)

    today = datetime.now(tz=UTC).date()
    result = compute_forecast(root, account_filter=account, quota=quota, today=today)

    for warning in assessment_failure_warnings(result.assessment):
        click.echo(warning, err=True)
    echo_reserved_skips(result.assessment.reserved)
    exit_code = EXIT_PARTIAL if result.assessment.failures else 0

    if not result.deals and not result.assessment.failures:
        click.echo(no_files_message("pursuit", account), err=True)
        raise SystemExit(EXIT_DATA) from None

    # implementation change: warn on $0 ACV deals (data quality issue).
    _warn_zero_acv(result.deals)

    if as_json:
        # cell-28b9dae2e9395288: machine-readable output.
        click.echo(json.dumps(dataclasses.asdict(result), indent=2, default=str))
        raise SystemExit(exit_code)

    _echo_deal_table(result.deals, today)
    _echo_scenarios(result, quota)

    # historic regression: surface deals silently dropped for unrecognized stages
    if result.skipped:
        click.echo(
            f"\nWARNING: {len(result.skipped)} deal(s) skipped — unrecognized stage(s):"
            f" {sorted(set(result.skipped))}."
            " Update STAGE_WEIGHT in forecast.py to include these stages.",
            err=True,
        )

    if result.assessment.failures:
        click.echo(assessment_summary_line(result.assessment))
        raise SystemExit(exit_code)
