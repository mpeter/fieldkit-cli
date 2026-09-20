"""fieldkit driver — Autonomous brief-execution driver loop.

Usage:
    fieldkit driver run          Run one iteration (picks oldest agent-ready issue)
    fieldkit driver run --dry-run  Log what would happen without label changes or git
    fieldkit driver status       Show the last 10 driver run results
    fieldkit driver retry status Show local retry reservations
    fieldkit driver list         List all agent-ready issues
"""

import json
import subprocess
from dataclasses import asdict
from pathlib import Path

import click

from fieldkit.cli_exit import EXIT_PARTIAL, cli_main
from fieldkit.cli_registry import declare_write
from fieldkit.commands.driver._status import print_status
from fieldkit.config import get_github_repo
from fieldkit.driver.github import list_ready_issues
from fieldkit.errors import FieldkitError


def _repo_root() -> Path:
    """Return the repository root (the directory containing pyproject.toml).

    Falls back to CWD if the repo root cannot be determined.  The driver
    runs from a checked-out fieldkit clone, so CWD is correct when
    invoked by the systemd unit (WorkingDirectory is set explicitly).
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return Path(result.stdout.strip())
    except subprocess.CalledProcessError:
        return Path.cwd()


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    """Autonomous brief-execution driver loop."""


@declare_write(
    "external",
    confirm_exempt="scheduled systemd unit; writes are already gated behind --dry-run and a prompt would hang the timer",
)
@cli.command("run")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Log actions without executing label changes, git operations, or OpenCode.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable JSON output.")
def run(dry_run: bool, as_json: bool) -> None:
    """Pick the oldest agent-ready issue and execute its brief.

    Exits 0 on success or skip (no issues).  Exits 1 on partial failure
    (OpenCode non-zero or brief resolution error).
    """
    with cli_main():
        from fieldkit.driver.runner import run_driver

        result = run_driver(repo_root=_repo_root(), dry_run=dry_run)

        if as_json:
            # The exit code still encodes the outcome, so the document is emitted
            # for a failed run too — that is exactly when the caller needs the
            # branch and error fields.
            click.echo(
                json.dumps(
                    {
                        "issue_number": result.issue_number,
                        "issue_title": result.issue_title,
                        "branch": result.branch,
                        "outcome": result.outcome,
                        "elapsed_seconds": result.elapsed_seconds,
                        "spend_note": result.spend_note,
                        "error": result.error,
                        "dry_run": dry_run,
                    },
                    indent=2,
                    default=str,
                )
            )
            if result.outcome == "failed":
                raise SystemExit(EXIT_PARTIAL)
            return

        prefix = "[dry-run] " if dry_run else ""

        if result.outcome == "skipped":
            click.echo(f"{prefix}No agent-ready issues — nothing to do.")
            return

        status_sym = {
            "ok": click.style("✓", fg="green"),
            "dry-run": click.style("~", fg="cyan"),
            "failed": click.style("✗", fg="red"),
            "skipped": click.style("-", fg="yellow"),
        }.get(result.outcome, "?")

        click.echo(f"{prefix}{status_sym} Issue #{result.issue_number}: {result.issue_title}")
        click.echo(f"  Branch:  {result.branch}")
        click.echo(f"  Outcome: {result.outcome}")
        click.echo(f"  Elapsed: {result.elapsed_seconds:.1f}s")

        if result.spend_note:
            click.echo(f"  {result.spend_note}")

        if result.error:
            click.echo(click.style(f"  Error: {result.error}", fg="red"), err=True)

        if result.outcome == "failed":
            raise SystemExit(EXIT_PARTIAL)


@cli.command("admit")
@click.option("--job", required=True, help="Scheduled developer job requesting the global admission lease.")
@click.option("--release", "release_lease", is_flag=True, default=False, help="Release this job's admission lease.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable JSON output.")
def admit(job: str, release_lease: bool, as_json: bool) -> None:
    """Acquire or release the global developer-schedule admission lease."""
    with cli_main():
        from fieldkit.driver.admission import admit as request_admission
        from fieldkit.driver.admission import release

        decision = release(job) if release_lease else request_admission(job)
        if as_json:
            click.echo(json.dumps(asdict(decision), indent=2, default=str))
        else:
            state = "admitted" if decision.allowed else "denied"
            click.echo(f"{state}: {decision.reason_code} — {decision.detail}")
        if not decision.allowed:
            raise SystemExit(EXIT_PARTIAL)


@cli.command("status")
@click.option("--limit", default=10, show_default=True, help="Number of recent runs to show.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable JSON output.")
def status(limit: int, as_json: bool) -> None:
    """Show recent driver run results."""
    with cli_main():
        print_status(limit=limit, as_json=as_json)


@cli.group("retry")
def retry() -> None:
    """Inspect or reset local driver retry reservations."""


@retry.command("status")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable JSON output.")
def retry_status(as_json: bool) -> None:
    """Show local retry state without contacting GitHub."""
    with cli_main():
        from fieldkit.driver.retry_state import retry_status as get_retry_status

        entries = get_retry_status()
        if as_json:
            click.echo(json.dumps({"items": [asdict(entry) for entry in entries], "count": len(entries)}, indent=2))
            return
        if not entries:
            click.echo("No local retry reservations.")
            return
        for entry in entries:
            click.echo(f"{entry.issue_key}: {entry.phase} ({entry.started_attempts}/3)")


@declare_write("workspace")
@retry.command("reset")
@click.option("--issue", type=click.IntRange(min=1), required=True, help="GitHub issue number to reset.")
@click.option("--reason", required=True, help="Audit reason for resetting local retry state.")
@click.option("--dry-run", is_flag=True, help="Report the reset without modifying local retry state.")
@click.option("--json", "as_json", is_flag=True, help="Emit the reset outcome as JSON.")
def retry_reset(issue: int, reason: str, dry_run: bool, as_json: bool) -> None:
    """Reset a non-running local retry entry with an audit reason."""
    with cli_main():
        if dry_run:
            clean_reason = reason.strip()
            if not clean_reason:
                raise FieldkitError("Reset reason must not be empty.")
            issue_key = f"{get_github_repo()}#{issue}"
            if as_json:
                click.echo(
                    json.dumps({"issue_key": issue_key, "reset": False, "dry_run": True, "detail": clean_reason})
                )
            else:
                click.echo(f"Would attempt to reset retry state for {issue_key}: {clean_reason}")
            return

        from fieldkit.driver.retry_state import reset_retry

        result = reset_retry(get_github_repo(), issue, reason)
        if result.reset:
            if as_json:
                click.echo(json.dumps({**asdict(result), "dry_run": False}, default=str))
            else:
                click.echo(f"Reset {result.issue_key}: {result.detail}")
            return
        raise FieldkitError(f"Did not reset {result.issue_key}: {result.detail}")


@cli.command("list")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable JSON output.")
def list_issues(as_json: bool) -> None:
    """List all open agent-ready issues."""
    with cli_main():
        repo = get_github_repo()
        issues = list_ready_issues(repo)
        if as_json:
            click.echo(
                json.dumps(
                    {
                        "items": [
                            {"number": i.number, "title": i.title, "attempt_count": i.attempt_count} for i in issues
                        ],
                        "count": len(issues),
                        "filters": {"repo": repo},
                    },
                    indent=2,
                    default=str,
                )
            )
            return
        if not issues:
            click.echo("No agent-ready issues.")
            return
        for issue in issues:
            attempts = issue.attempt_count
            attempt_str = f" (attempt {attempts}/3)" if attempts > 0 else ""
            click.echo(f"  #{issue.number:>4}  {issue.title}{attempt_str}")
