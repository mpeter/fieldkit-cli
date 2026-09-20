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
    audit_directory,
    no_files_message,
)
from fieldkit.config import get_fieldkit_home
from fieldkit.pursuit.enums import Stage

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


def _load_health_frontmatter(path: Path) -> dict[str, Any] | None:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None

    from fieldkit.pursuit.io import parse_frontmatter_fallback

    fm, _ = parse_frontmatter_fallback(content)
    return fm


def classify_pursuit(result: AuditResult, today: date) -> RiskItem | None:
    """Classify a parsed AuditResult into a RiskItem.

    Returns None if the pursuit should be excluded from the health report
    (closed stage or parse error with no meaningful data).
    """
    fm = _load_health_frontmatter(result.path)
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


def health_check(
    root: Path,
    account_filter: str | None = None,
    today: date | None = None,
    include_prospect: bool = False,
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

    results = audit_directory(root, account_filter=account_filter, today=today)
    items: list[RiskItem] = []

    for result in results:
        item = classify_pursuit(result, today)
        if item is None:
            continue
        if not include_prospect and item.stage in _EARLY_STAGES:
            continue
        items.append(item)

    # Sort: HIGH → MEDIUM → LOW, then by days_until_close ascending (None = far future)
    tier_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}

    def sort_key(x: RiskItem) -> tuple[int, int]:
        d = x.days_until_close if x.days_until_close is not None else 9999
        return (tier_order.get(x.risk_tier, 3), d)

    return sorted(items, key=sort_key)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


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

        click.echo(json.dumps([dataclasses.asdict(i) for i in items], indent=2, default=str))
        if strict and high:
            raise SystemExit(EXIT_PARTIAL)
        raise SystemExit(0)

    # Print table
    click.echo(f"\nPipeline Health — {today}")
    click.echo(
        f"{'Deal':<45} {'Stage':<12} {'Days in Stage':>13} {'Qualification':>13} {'Close':>10} {'Risk':<8} Reasons"
    )
    click.echo("-" * 124)

    for item in items:
        close_str = item.close_date_str or "—"
        if item.days_until_close is not None:
            if item.days_until_close < 0:
                close_str = f"{close_str} ({abs(item.days_until_close)}d OVR)"
            elif item.days_until_close <= 30:
                close_str = f"{close_str} ({item.days_until_close}d)"
        days_str = f"{item.days_in_stage}d" if item.days_in_stage is not None else "—"
        reasons_str = "; ".join(item.risk_reasons) if item.risk_reasons else "—"
        tier_icon = {"HIGH": "✗", "MEDIUM": "⚠", "LOW": "✓"}.get(item.risk_tier, "?")
        name = item.relative_path.replace("/pursuits/", "/").replace(".md", "")
        line = (
            f"{name:<45} {item.stage:<12} {days_str:>5} {item.qualification_status:>13} "
            f"{close_str:>10} {tier_icon} {item.risk_tier:<6} {reasons_str}"
        )
        # implementation change: --compact truncates to 80 chars
        click.echo(line[:80] if compact else line)

    click.echo("-" * (80 if compact else 124))
    click.echo(f"\nSummary: {len(high)} HIGH | {len(medium)} MEDIUM | {len(low)} LOW")

    # Exit codes:
    #   0 — valid report by default; no HIGH-risk items in strict mode
    #   1 — one or more HIGH-risk items with --strict
    if strict and high:
        raise SystemExit(EXIT_PARTIAL)
    raise SystemExit(0)


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
      1 — one or more high-risk items with --strict
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
    items = health_check(root, account_filter=account, today=today, include_prospect=include_prospect)

    if not items:
        click.echo(no_files_message("pursuit", account))
        raise SystemExit(EXIT_DATA) from None

    high = [i for i in items if i.risk_tier == "HIGH"]
    medium = [i for i in items if i.risk_tier == "MEDIUM"]
    low = [i for i in items if i.risk_tier == "LOW"]

    _emit_health_results(
        items,
        high=high,
        medium=medium,
        low=low,
        today=today,
        as_json=as_json,
        compact=compact,
        strict=strict,
    )
