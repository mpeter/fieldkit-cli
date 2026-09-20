"""Click adapter for delivery project health checks."""

from datetime import UTC, date, datetime
from pathlib import Path

import click

from fieldkit.cli_exit import EXIT_DATA, EXIT_PARTIAL
from fieldkit.commands.pursuit.audit import no_files_message
from fieldkit.config import get_fieldkit_home
from fieldkit.pursuit.projects import ProjectRow, health_check

LOG_PREFIX = "[pursuit-projects]"

# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

_HEALTH_ICONS: dict[str, str] = {
    "ZOMBIE": "☠",
    "EXPIRING": "✗",
    "SOON": "⚠",
    "ACTIVE": "✓",
    "UNKNOWN": "?",
}


def _format_days(row: ProjectRow) -> str:
    if row.days_until_end is None:
        return "—"
    if row.days_until_end < 0:
        return f"{row.days_until_end}d OVR"
    return str(row.days_until_end)


def _print_health_table(rows: list[ProjectRow], today: date) -> None:
    click.echo(f"\nProject Health — {today}")
    click.echo(f"{'Project':<50} {'Stage':<14} {'End Date':>10} {'Days':>6} Health")
    click.echo("-" * 100)
    for row in rows:
        health_icon = _HEALTH_ICONS.get(row.health, "?")
        days_str = _format_days(row)
        name_short = row.name[:47] + "…" if len(row.name) > 47 else row.name
        click.echo(
            f"{name_short:<50} {row.sf_stage:<14} {row.contract_end_str:>10} {days_str:>6} {health_icon} {row.health}"
        )
    click.echo("-" * 100)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.command(name="projects")
@click.option("--account", "-a", default=None, help="Limit to a single account directory name.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit results as JSON array.")
@click.option(
    "--strict",
    is_flag=True,
    default=False,
    help="Exit 1 when ZOMBIE or UNKNOWN projects are present; valid reports exit 0 by default.",
)
def cli(account: str | None, as_json: bool, strict: bool) -> None:
    """Show delivery project health — zombie, expiring, and active engagements.

    Scans all project files and classifies each by contract end date:
      ZOMBIE   — contract end date past and not completed
      EXPIRING — contract ends within 30 days
      SOON     — contract ends within 90 days
      ACTIVE   — contract end date > 90 days out
      UNKNOWN  — no contract end date in frontmatter

    Exit codes:
      0 — valid report (default), or no ZOMBIE/UNKNOWN projects with --strict
      1 — one or more ZOMBIE/UNKNOWN projects with --strict
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
    rows = health_check(root, account_filter=account, today=today)

    if not rows:
        click.echo(no_files_message("project", account))
        raise SystemExit(EXIT_DATA) from None

    counts = {h: sum(1 for r in rows if r.health == h) for h in ("ZOMBIE", "EXPIRING", "SOON", "ACTIVE", "UNKNOWN")}

    # implementation change: --json output
    if as_json:
        import dataclasses
        import json

        click.echo(json.dumps([dataclasses.asdict(r) for r in rows], indent=2, default=str))
        if strict and (counts["ZOMBIE"] > 0 or counts["UNKNOWN"] > 0):
            raise SystemExit(EXIT_PARTIAL)
        raise SystemExit(0)

    _print_health_table(rows, today)
    click.echo(
        f"\nSummary: {counts['ZOMBIE']} ZOMBIE | {counts['EXPIRING']} EXPIRING"
        f" | {counts['SOON']} SOON | {counts['ACTIVE']} ACTIVE | {counts['UNKNOWN']} UNKNOWN"
    )

    # Exit codes (historic regression):
    #   0 — valid report by default; no ZOMBIE or UNKNOWN projects in strict mode
    #   1 — one or more ZOMBIE or UNKNOWN projects with --strict
    #   3 — fatal error set upstream
    if strict and (counts["ZOMBIE"] > 0 or counts["UNKNOWN"] > 0):
        raise SystemExit(EXIT_PARTIAL)
    raise SystemExit(0)
