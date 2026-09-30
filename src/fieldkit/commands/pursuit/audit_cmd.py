"""fieldkit pursuit audit — Validate pursuit frontmatter compliance and timeline risks.

Usage:
    fieldkit pursuit audit
    fieldkit pursuit audit <account-slug>
    fieldkit pursuit audit --account acme-bank
    fieldkit pursuit audit --fix
    fieldkit pursuit audit --output custom-report.md
"""

import dataclasses
import json
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import click

from fieldkit.cli_exit import EXIT_DATA, EXIT_PARTIAL
from fieldkit.cli_registry import declare_write
from fieldkit.commands.pursuit.audit import (
    AUDIT_SKIPPED_FILENAMES,
    AuditResult,
    Finding,
    apply_fixes,
    audit_directory,
    check_yaml_duplicates_directory,
    no_files_message,
    pursuit_paths,
)
from fieldkit.config import get_fieldkit_home
from fieldkit.errors import FrontmatterStalenessError
from fieldkit.ingest.paths import validate_meeting_account
from fieldkit.pursuit.enums import Stage
from fieldkit.pursuit.stages import CLOSED_STAGES
from fieldkit.util.atomic import PathLockTimeoutError, atomic_text_write
from fieldkit.util.workspace_paths import resolve_workspace_output

LOG_PREFIX = "[pursuit-audit]"
log = logging.getLogger(__name__)


def _data_root() -> Path:
    root = get_fieldkit_home()
    if root is None:
        click.echo(f"{LOG_PREFIX} No data root configured. Run: fieldkit init", err=True)
        raise SystemExit(EXIT_DATA) from None
    return Path(root)


def _report_section(header: str, results: list[AuditResult], item_fn: Callable[[AuditResult], list[str]]) -> list[str]:
    """Render a headed section of an audit report, calling item_fn(r) for each result."""
    lines: list[str] = ["---", "", f"## {header}", ""]
    for r in results:
        lines += item_fn(r)
    return lines


def _error_items(r: AuditResult) -> list[str]:
    lines = [f"### {r.relative_path}", ""]
    for f in r.errors:
        lines.append(f"- **ERROR:** {f.message}")
    if r.parse_error:
        lines.append(f"- **ERROR:** {r.parse_error}")
    lines.append("")
    return lines


def _critical_items(r: AuditResult) -> list[str]:
    return [f"### {r.relative_path}", ""] + [f"- **CRITICAL:** {f.message}" for f in r.criticals] + [""]


def _warning_items(r: AuditResult) -> list[str]:
    return [f"### {r.relative_path}", ""] + [f"- **WARNING:** {f.message}" for f in r.warnings] + [""]


def _split_compliant_active_closed_won(results: list[AuditResult]) -> tuple[list[AuditResult], list[AuditResult]]:
    """Split COMPLIANT results into (active, closed-won) buckets.

    Uses the canonical CLOSED_STAGES set (not a bare != CLOSED_WON check) so
    closed-lost pursuits aren't miscounted as active (implementation change).
    """
    compliant = [r for r in results if r.category == "COMPLIANT"]
    active = [r for r in compliant if r.stage not in CLOSED_STAGES]
    closed_won = [r for r in compliant if r.stage == Stage.CLOSED_WON]
    return active, closed_won


def _render_report(results: list[AuditResult], today: date) -> str:
    errors = [r for r in results if r.category == "ERROR"]
    warnings = [r for r in results if r.category == "WARNING"]
    compliant = [r for r in results if r.category == "COMPLIANT"]
    compliant_active, compliant_closed_won = _split_compliant_active_closed_won(results)
    criticals = [r for r in results if r.criticals]

    lines: list[str] = [
        f"# Pursuit Compliance Report — {today}",
        "",
        f"Generated: {today}",
        "",
        "## Summary",
        "",
        "| Metric | Count |",
        "|--------|-------|",
        f"| Files scanned | {len(results)} |",
        f"| Compliant (active) | {len(compliant_active)} |",
        f"| Compliant (closed-won) | {len(compliant_closed_won)} |",
        f"| Errors | {len(errors)} |",
        f"| Warnings | {len(warnings)} |",
        f"| Critical flags | {sum(len(r.criticals) for r in results)} |",
        "",
    ]
    if errors:
        lines += _report_section("Errors", errors, _error_items)
    if criticals:
        lines += _report_section("Critical Flags", criticals, _critical_items)
    if warnings:
        lines += _report_section("Warnings", warnings, _warning_items)
    if compliant:
        lines += ["---", "", "## Compliant Files", ""]
        lines += [f"- `{r.relative_path}` — current qualification: {r.qualification_status}" for r in compliant]
        lines.append("")
    lines += [
        "---",
        "",
        "## Recommendations",
        "",
        "1. **Fix hyphenated sf- fields first.** Fields like `sf-opportunity-id` are silently ignored by automation.",
        "2. **Treat current qualification as unavailable.** Review native ClosePlan evidence; historical local scores are not current policy.",
        "3. **Keep Salesforce next steps current.** Near-term close dates need an explicit next action.",
        "4. **Overdue close dates need immediate Salesforce action.** Push out, change stage, or qualify out.",
    ]
    return "\n".join(lines)


def _first_noncritical_finding(result: AuditResult) -> tuple[Finding | None, int]:
    """Return the first non-critical finding and the number left undisplayed."""
    noncritical = [finding for finding in result.findings if finding.level != "CRITICAL"]
    return (noncritical[0], len(noncritical) - 1) if noncritical else (None, 0)


def _print_noncritical_detail(result: AuditResult) -> None:
    """Print one actionable non-critical detail and a bounded continuation."""
    first_noncritical, remaining = _first_noncritical_finding(result)
    if first_noncritical is not None:
        click.echo(f"    {first_noncritical.level}: {first_noncritical.message}")
    if remaining:
        click.echo(f"    … and {remaining} more; see report")


def _print_summary_table(results: list[AuditResult]) -> None:
    """Print a compact summary table to stdout."""
    errors = sum(1 for r in results if r.category == "ERROR")
    warnings_count = sum(1 for r in results if r.category == "WARNING")
    compliant_active, compliant_closed_won = _split_compliant_active_closed_won(results)
    criticals = sum(len(r.criticals) for r in results)

    click.echo(f"\nFiles scanned : {len(results)}")
    click.echo(f"Compliant     : {len(compliant_active)}")
    click.echo(f"  closed-won  : {len(compliant_closed_won)}")
    click.echo(f"Errors        : {errors}")
    click.echo(f"Warnings      : {warnings_count}")
    click.echo(f"Critical flags: {criticals}")

    if results:
        click.echo("\nDetails:")
        for r in results:
            icon = "✓" if r.category == "COMPLIANT" else ("✗" if r.category == "ERROR" else "⚠")
            click.echo(
                f"  {icon} {r.relative_path} — qualification {r.qualification_status} ({len(r.findings)} finding(s))"
            )
            for f in r.criticals:
                click.echo(f"    ⚡ {f.message}")
            _print_noncritical_detail(r)


def _run_check_yaml(root: Path, account: str | None) -> None:
    """Run --check-yaml mode: scan for duplicate YAML keys and exit."""
    dup_results = check_yaml_duplicates_directory(root, account_filter=account)
    if not dup_results:
        click.echo(f"{LOG_PREFIX} No duplicate YAML keys found.")
        raise SystemExit(0)
    click.echo(f"{LOG_PREFIX} Duplicate YAML keys detected in {len(dup_results)} file(s):\n")
    for r in dup_results:
        click.echo(f"  {r.relative_path}")
        if r.parse_error:
            click.echo(f"    ERROR: {r.parse_error}")
        for f in r.findings:
            click.echo(f"    {f}")
    raise SystemExit(EXIT_PARTIAL)


def _run_fix(paths: list[Path], accounts_dir: Path, *, dry_run: bool = False, as_json: bool = False) -> bool:
    """Apply or preview corrections; return whether any repair failed."""
    fix_total = 0
    repair_failed = False
    for path in paths:
        changes, failed = _fix_one_pursuit(path, accounts_dir, dry_run=dry_run, as_json=as_json)
        fix_total += changes
        repair_failed = repair_failed or failed
    if fix_total:
        verb = "Would apply" if dry_run else "Applied"
        suffix = "" if dry_run else " Re-auditing..."
        click.echo(f"\n{LOG_PREFIX} {verb} {fix_total} auto-correction(s).{suffix}\n", err=as_json)
    elif not repair_failed:
        click.echo(f"{LOG_PREFIX} No auto-corrections needed.\n", err=as_json)
    return repair_failed


def _fix_one_pursuit(path: Path, accounts_dir: Path, *, dry_run: bool, as_json: bool) -> tuple[int, bool]:
    """Return the completed change count and whether the repair failed."""
    if path.name in AUDIT_SKIPPED_FILENAMES:
        return 0, False
    try:
        fix_result = apply_fixes(path, dry_run=dry_run)
    except FrontmatterStalenessError:
        click.echo(f"  repair refused: {path.relative_to(accounts_dir)} (changed during repair)", err=as_json)
        return 0, True
    except PathLockTimeoutError:
        click.echo(f"  repair failed: {path.relative_to(accounts_dir)} (repair lock timed out)", err=as_json)
        return 0, True
    except ValueError:
        click.echo(f"  repair refused: {path.relative_to(accounts_dir)} (unsafe or invalid pursuit data)", err=as_json)
        return 0, True
    except OSError:
        click.echo(f"  repair failed: {path.relative_to(accounts_dir)} (filesystem write failed)", err=as_json)
        return 0, True
    if not fix_result.total_changes:
        return 0, False
    action = "would fix" if dry_run else "fixed"
    click.echo(
        f"  {action}: {path.relative_to(accounts_dir)} "
        f"({fix_result.renames} renames, {fix_result.legacy_removed} removed)",
        err=as_json,
    )
    return fix_result.total_changes, False


def _resolve_report_path(accounts_dir: Path, account: str | None, output: str | None, today: date) -> Path:
    """Resolve one report destination beneath the workspace audit directory."""
    workspace = accounts_dir.parent.resolve(strict=True)
    audit_dir = resolve_workspace_output(workspace, "accounts/.audit")
    if output is None:
        audit_dir.mkdir(parents=True, exist_ok=True)
        audit_dir = resolve_workspace_output(workspace, "accounts/.audit")
        acct_suffix = f"-{account}" if account else ""
        return resolve_workspace_output(
            workspace,
            f"accounts/.audit/pursuit-compliance-{today}{acct_suffix}.md",
        )

    requested = Path(output)
    candidate = requested.absolute() if requested.is_absolute() else (audit_dir / requested).absolute()
    try:
        relative = candidate.relative_to(workspace).as_posix()
    except ValueError:
        raise ValueError("Audit report output must remain under accounts/.audit") from None
    report_path = resolve_workspace_output(workspace, relative)
    if not report_path.is_relative_to(audit_dir) or report_path == audit_dir:
        raise ValueError("Audit report output must remain under accounts/.audit")
    if not report_path.parent.is_dir():
        raise ValueError("Audit report output parent must already exist")
    return report_path


def _write_audit_report(
    results: list[AuditResult],
    accounts_dir: Path,
    account: str | None,
    output: str | None,
    today: date,
) -> None:
    """Render and write the audit report to disk."""
    try:
        report_path = _resolve_report_path(accounts_dir, account, output, today)
        report = _render_report(results, today)
        if report_path.exists():
            log.info("Overwriting existing audit report")
        atomic_text_write(report_path, report)
    except (OSError, ValueError):
        click.echo(f"{LOG_PREFIX} Cannot safely write the audit report.", err=True)
        raise SystemExit(EXIT_DATA) from None
    workspace = accounts_dir.parent.resolve(strict=True)
    click.echo(f"\nReport written to: {report_path.relative_to(workspace)}")


def _json_result(result: AuditResult) -> dict[str, object]:
    """Return the public result envelope without its private absolute path."""
    payload = dataclasses.asdict(result)
    payload.pop("path", None)
    return payload


def _emit_results(
    results: list[AuditResult],
    *,
    accounts_dir: Path,
    account: str | None,
    output: str | None,
    today: date,
    as_json: bool,
    write_report: bool = True,
    repair_failed: bool = False,
) -> None:
    """Emit audit results to stdout and raise SystemExit with the appropriate code.

    Extracted from ``cli()`` to reduce its cyclomatic complexity (CRAP gate).
    Handles both JSON and human-readable output modes.

    Raises:
        SystemExit(0): all files compliant.
        SystemExit(1): one or more findings (criticals or warnings).
    """
    if as_json:
        # cell-28b9dae2e9395288: machine-readable output suppresses table and report write.
        click.echo(json.dumps([_json_result(r) for r in results], indent=2, default=str))
        errors_j = sum(1 for r in results if r.category == "ERROR")
        warnings_j = sum(1 for r in results if r.category == "WARNING")
        criticals_j = sum(len(r.criticals) for r in results)
        raise SystemExit(1 if (repair_failed or criticals_j > 0 or warnings_j > 0 or errors_j > 0) else 0)

    _print_summary_table(results)
    _maybe_write_report(results, accounts_dir, account, output, today, write_report=write_report)

    # Exit codes:
    #   0 — all files compliant (no findings)
    #   1 — one or more findings (criticals or warnings) — lets scripts detect issues
    #   3 — fatal error (unreadable files, DB error, crash) — set upstream via sys.exit(3)
    errors = sum(1 for r in results if r.category == "ERROR")
    warnings_count = sum(1 for r in results if r.category == "WARNING")
    criticals = sum(len(r.criticals) for r in results)
    if repair_failed or criticals > 0 or warnings_count > 0 or errors > 0:
        raise SystemExit(EXIT_PARTIAL)
    raise SystemExit(0)


def _maybe_write_report(
    results: list[AuditResult],
    accounts_dir: Path,
    account: str | None,
    output: str | None,
    today: date,
    *,
    write_report: bool,
) -> None:
    """Write the audit report when the selected mode permits filesystem output."""
    if write_report:
        _write_audit_report(results, accounts_dir, account, output, today)


def _validate_dry_run(fix: bool, dry_run: bool, check_yaml: bool, as_json: bool, output: str | None) -> None:
    """Reject unsupported preview flag combinations before workspace access."""
    if dry_run and not fix:
        raise click.UsageError("--dry-run requires --fix")
    if dry_run and check_yaml:
        raise click.UsageError("--dry-run cannot be combined with --check-yaml")
    if dry_run and as_json:
        raise click.UsageError("--dry-run cannot be combined with --json")
    if dry_run and output is not None:
        raise click.UsageError("--dry-run cannot be combined with --output")
    if fix and check_yaml:
        raise click.UsageError("--fix cannot be combined with --check-yaml")


@declare_write("workspace")
@click.command(name="audit")
@click.argument("account_pos", default=None, required=False, metavar="ACCOUNT")
@click.option("--account", "-a", default=None, help="Limit audit to a single account directory name.")
@click.option(
    "--fix",
    is_flag=True,
    default=False,
    help="Auto-correct common frontmatter issues (hyphenated SF fields, legacy fields).",
)
@click.option("--dry-run", is_flag=True, help="Preview --fix corrections without writing pursuit files or reports.")
@click.option(
    "--output",
    "-o",
    default=None,
    help="Write beneath <data-root>/accounts/.audit/ (default: pursuit-compliance-YYYY-MM-DD.md).",
)
@click.option(
    "--check-yaml",
    "check_yaml",
    is_flag=True,
    default=False,
    help="Scan pursuit files for duplicate YAML frontmatter keys. Exits 1 if any found.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Machine-readable JSON output (suppresses table and report file write).",
)
def cli(
    account_pos: str | None,
    account: str | None,
    fix: bool,
    dry_run: bool,
    output: str | None,
    check_yaml: bool,
    as_json: bool,
) -> None:
    """Validate pursuit frontmatter compliance and timeline risks.

    Optionally pass --account ACCOUNT (or -a ACCOUNT) to limit the audit to a
    single account directory. The bare positional ACCOUNT form is deprecated.

    Scans all pursuit files under the configured data root, reports errors
    and warnings, and optionally applies safe auto-corrections.

    Exit codes:
      0 — all files compliant
      1 — one or more files have errors or warnings
      3 — data error (config missing, accounts directory not found)
    """
    _validate_dry_run(fix, dry_run, check_yaml, as_json, output)

    # implementation change: resolve account from positional arg or -a flag.
    # Positional arg wins when provided; raise UsageError if both given.
    if account_pos is not None and account is not None:
        raise click.UsageError("Provide ACCOUNT as a positional argument OR via -a/--account, not both.")
    if account_pos is not None:
        click.echo(
            "Warning: positional ACCOUNT is deprecated. Use --account ACCOUNT instead.",
            err=True,
        )
        account = account_pos

    root = _data_root()
    today = datetime.now(tz=UTC).date()

    try:
        if account is not None:
            validate_meeting_account(account)
        accounts_dir = resolve_workspace_output(root, "accounts")
    except ValueError:
        click.echo(f"{LOG_PREFIX} Invalid or unsafe account scope.", err=True)
        raise SystemExit(EXIT_DATA) from None

    if not accounts_dir.is_dir():
        click.echo(f"{LOG_PREFIX} Accounts directory not found. Run 'fieldkit init' to initialize.", err=True)
        raise SystemExit(EXIT_DATA) from None

    try:
        scoped_paths = pursuit_paths(root, account)
    except ValueError:
        click.echo(f"{LOG_PREFIX} Invalid or unsafe account scope.", err=True)
        raise SystemExit(EXIT_DATA) from None

    if check_yaml:
        _run_check_yaml(root, account)
        return  # sys.exit called inside

    repair_failed = False
    if fix:
        repair_failed = _run_fix(scoped_paths, accounts_dir, dry_run=dry_run, as_json=as_json)

    if account is not None and not (accounts_dir / account).is_dir():
        click.echo(f"{LOG_PREFIX} Account directory not found: {account}", err=True)
        raise SystemExit(EXIT_DATA) from None

    results = audit_directory(root, account_filter=account, today=today)

    if not results:
        click.echo(no_files_message("pursuit", account))
        raise SystemExit(EXIT_DATA) from None

    _emit_results(
        results,
        accounts_dir=accounts_dir,
        account=account,
        output=output,
        today=today,
        as_json=as_json,
        write_report=not dry_run,
        repair_failed=repair_failed,
    )
