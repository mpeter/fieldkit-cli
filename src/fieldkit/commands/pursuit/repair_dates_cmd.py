"""repair-dates — CLI wiring for the repair domain module.

Business logic lives in ``fieldkit.watch.repair``; this file is Click wiring
only (parse args → call domain → return exit code).
"""

import click

from fieldkit.cli_registry import declare_write
from fieldkit.commands._account_guard import validate_account_slug
from fieldkit.watch.repair import _run_repair


@declare_write("workspace")
@click.command("repair-dates")
@click.option(
    "--dry-run",
    is_flag=True,
    default=True,
    help="Print proposed changes without writing (default; use --apply to write).",
)
@click.option(
    "--apply",
    "do_apply",
    is_flag=True,
    default=False,
    help="Write corrected last-transition dates to pursuit files.",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    default=False,
    help="Show all files scanned, not just those with proposed changes.",
)
@click.option(
    "--account",
    "-a",
    default=None,
    metavar="SLUG",
    help="Limit the repair sweep to a single account slug. Default: all accounts.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit repair outcomes as JSON.")
def cli(dry_run: bool, do_apply: bool, verbose: bool, account: str | None, as_json: bool) -> None:
    """Repair pursuit files whose last-transition date was incorrectly set to today.

    Identifies pursuits where ``last-transition`` equals today after a
    normalization-only stage pass. For each affected file the command recovers
    the real date via git log (falling back to file mtime) and either prints the
    proposal (dry-run) or patches the file in place (--apply).

    The command is idempotent: subsequent runs after --apply produce no changes.

    Examples:

    \b
        fieldkit pursuit repair-dates --dry-run
        fieldkit pursuit repair-dates --apply
        fieldkit pursuit repair-dates --account acme-corp --apply

    Exit codes: 0 success; 3 configuration missing or unknown account slug.
    """
    if dry_run and do_apply:
        raise click.UsageError("--dry-run and --apply are mutually exclusive.")

    validate_account_slug(account)
    effective_dry_run = not do_apply
    raise SystemExit(_run_repair(dry_run=effective_dry_run, verbose=verbose, account=account, as_json=as_json))
