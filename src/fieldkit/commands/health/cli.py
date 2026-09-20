"""fieldkit health — nightly repo-health sensor (implementation change, autonomy Stage 3).

Usage:
    fieldkit health run             Sense origin/main, file new regressions
    fieldkit health run --dry-run   Sense and classify; file nothing, persist nothing
    fieldkit health status          Show recent health-run results

Thin adapter: parse args → call fieldkit.health domain → exit code. The
GHIssueStore-backed filer is injected here so the domain stays free of the
commands layer (design Decision 2). Filed issues carry no ``agent-ready``
label — the operator applies the consent bit during triage (Decision 1).
"""

import dataclasses
import json
import signal
import subprocess
import types
from pathlib import Path
from typing import Any

import click

from fieldkit.cli_exit import EXIT_PARTIAL, cli_main
from fieldkit.cli_registry import declare_write
from fieldkit.commands.issue.gh_store import GHIssueStore
from fieldkit.config import get_fieldkit_data, get_github_repo
from fieldkit.health.runner import HealthRunResult, run_health


class _GHIssueFiler:
    """IssueFiler backed by the same store ``fieldkit issue create`` uses."""

    def __init__(self, repo: str) -> None:
        self._store = GHIssueStore(repo)

    def open_titles(self) -> list[str]:
        # "Still open" for dedup includes planned: an operator who has planned
        # the fix must not receive a duplicate (design Decision 4).
        issues = self._store.list_issues(status="all", issue_type="bug")
        return [issue.title for issue in issues if issue.status in ("open", "planned")]

    def file_regression(self, *, title: str, body: str) -> str:
        issue = self._store.create(
            issue_type="bug",
            title=title,
            body=body,
            severity="high",
            module="other",
            source="nightly-health-sensor",
        )
        return f"{issue.id} (#{issue.gh_number})"


def _repo_root() -> Path:
    """Return the repository root; falls back to CWD (systemd sets WorkingDirectory)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return Path(result.stdout.strip())
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return Path.cwd()


def _echo_result(result: HealthRunResult, *, dry_run: bool) -> None:
    prefix = "[dry-run] " if dry_run else ""
    symbol = {
        "ok": click.style("OK", fg="green"),
        "partial": click.style("PARTIAL", fg="yellow"),
        "fatal": click.style("FATAL", fg="red"),
    }[result.outcome]
    click.echo(f"{prefix}{symbol} — {result.checks_run} check(s) executed in {result.elapsed_seconds:.1f}s")
    if result.gate_failures:
        click.echo(f"  Gate failures: {', '.join(result.gate_failures)}")
        click.echo(f"  Filed: {', '.join(result.issues_filed) or '(none)'}")
        click.echo(f"  Deduped (still-open issue exists): {', '.join(result.issues_deduped) or '(none)'}")
    else:
        click.echo("  All gates green — nothing filed (green is silent).")
    for err in result.runner_errors:
        click.echo(click.style(f"  Runner error: {err}", fg="red"), err=True)
    if any("filing failed" in err for err in result.runner_errors):
        click.echo(
            "  Regressions were sensed but could not be filed — check `gh auth status` "
            "before the next nightly run retries with the same credentials.",
            err=True,
        )


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    """Nightly repo-health sensor — senses regressions on main, files issues."""


@declare_write(
    "external",
    confirm_exempt="scheduled systemd unit; writes are already gated behind --dry-run and a prompt would hang the timer",
)
@cli.command("run")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Sense and classify without filing issues or persisting run status.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable JSON output.")
def run(dry_run: bool, as_json: bool) -> None:
    """Run the gate bundle against origin/main and file new regressions.

    Exits 0 when every check executed (gate failures are sensed regressions,
    not runner failures). Exits 1 on partial/fatal runner outcomes.
    """
    with cli_main():
        # A systemd timeout kill sends SIGTERM; convert it to an exception so
        # the runner's crash path records a `fatal` run-status entry instead
        # of dying silently (spec: runner failure is never silent).
        def _terminated(signum: int, frame: types.FrameType | None) -> None:
            raise RuntimeError(f"health run terminated by signal {signum}")

        signal.signal(signal.SIGTERM, _terminated)
        result = run_health(_repo_root(), _GHIssueFiler(get_github_repo()), dry_run=dry_run)
        if as_json:
            # Emitted for partial/fatal outcomes too — the exit code says the run
            # degraded, the document says which gates and which runner errors.
            click.echo(json.dumps({**dataclasses.asdict(result), "dry_run": dry_run}, indent=2, default=str))
        else:
            _echo_result(result, dry_run=dry_run)
        if result.outcome != "ok":
            raise SystemExit(EXIT_PARTIAL)


def _emit_status_json(runs: list[dict[str, Any]], limit: int) -> None:
    """Emit the health-run list as a machine-readable document on stdout."""
    click.echo(json.dumps({"items": runs, "count": len(runs), "filters": {"limit": limit}}, indent=2, default=str))


@cli.command("status")
@click.option("--limit", default=10, show_default=True, help="Number of recent runs to show.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable JSON output.")
def status(limit: int, as_json: bool) -> None:
    """Show recent health-run results from health-run-status.json."""
    with cli_main():
        status_file = get_fieldkit_data() / "logs" / "health" / "health-run-status.json"
        if not status_file.exists():
            if as_json:
                _emit_status_json([], limit)
            else:
                click.echo("No health runs recorded yet.")
            return
        data = json.loads(status_file.read_text(encoding="utf-8"))
        runs = data.get("runs", [])[-limit:]
        if not runs:
            if as_json:
                _emit_status_json([], limit)
            else:
                click.echo("No health runs recorded yet.")
            return
        if as_json:
            _emit_status_json(runs, limit)
            return
        for entry in runs:
            outcome = entry.get("outcome", "?")
            color = {"ok": "green", "partial": "yellow", "fatal": "red"}.get(outcome, "white")
            failures = ", ".join(entry.get("gate_failures", [])) or "-"
            click.echo(
                f"{entry.get('ts', '?')}  {click.style(f'{outcome:<7}', fg=color)} "
                f"checks={entry.get('checks_run', 0):<3} filed={len(entry.get('issues_filed', []))} "
                f"deduped={len(entry.get('issues_deduped', []))} failures: {failures}"
            )
