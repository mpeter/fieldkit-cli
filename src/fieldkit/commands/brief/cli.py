"""fieldkit brief CLI group.

Defines the Click group and subcommands for the morning brief command.
Pipeline-only generation lives in fieldkit.brief.pipeline_only; the merged
alerts and calendar path lives in generate.py.
"""

import json
from pathlib import Path

import click

from fieldkit.brief.pipeline_only import PipelineOnlyResult, generate_pipeline_only
from fieldkit.cli_exit import EXIT_DATA, EXIT_PARTIAL, cli_main
from fieldkit.cli_registry import declare_write
from fieldkit.commands.brief.generate import _run_generate
from fieldkit.config import ConfigError, get_fieldkit_home
from fieldkit.errors import AuthError
from fieldkit.util.saved_reports import open_saved_report, read_saved_report

# ── Feature introspection metadata (consumed by fieldkit version --features) ──
DESCRIPTION = "Generate today's pipeline-only brief — pursuit alerts, champion signals, decay, and priorities"
USAGE = "fieldkit brief generate --pipeline-only [--no-llm] [--account ACCOUNT] [--verbose]"
FLAGS = {
    "--no-llm": "Skip LLM synthesis; write collector output directly",
    "--account": "Limit brief to a single account slug",
    "--verbose / -v": "Print per-collector timing to stderr",
}
__all__ = ["DESCRIPTION", "FLAGS", "USAGE", "cli"]


def _emit_pipeline_only_result(result: PipelineOnlyResult, *, as_json: bool, verbose: bool) -> None:
    click.echo(f"[morning_brief] Collecting data for {result.path.stem.removeprefix('morning-brief-')} ...", err=True)
    if verbose:
        for collector, seconds in result.timings:
            click.echo(f"[brief] {collector}... done ({seconds:.2f}s)", err=True)
    if as_json:
        click.echo(
            json.dumps(
                {
                    "account": result.account,
                    "degraded": result.degraded,
                    "dry_run": result.dry_run,
                    "path": None if result.dry_run else str(result.path),
                    "written": not result.dry_run,
                },
                sort_keys=True,
            )
        )
    else:
        notice = "" if result.dry_run else f"\n\nBrief saved: {result.path}"
        click.echo(result.text + notice)


def _run_pipeline_only(
    *, no_llm: bool, account: str | None, verbose: bool = False, as_json: bool = False, dry_run: bool = False
) -> None:
    result = generate_pipeline_only(no_llm=no_llm, account=account, dry_run=dry_run)
    _emit_pipeline_only_result(result, as_json=as_json, verbose=verbose)
    if result.provider_failure is not None:
        raise result.provider_failure


@click.group(
    name="brief",
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Morning brief generation and retrieval.

    generate, open.
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        ctx.exit(1)


@declare_write("workspace")
@cli.command("generate")
@click.option(
    "--date",
    "date_str",
    metavar="YYYY-MM-DD",
    default=None,
    help="Date to generate the brief for (default: today). Ignored with --pipeline-only.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Generate the brief and print to stdout; do not write any files.",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    default=False,
    help="Enable verbose/debug output.",
)
@click.option(
    "--account",
    "-a",
    default=None,
    help="Limit brief to a single account slug.",
)
@click.option(
    "--pipeline-only",
    is_flag=True,
    default=False,
    help="Generate only the pipeline-review brief (pursuit alerts, champion/decay signals, "
    "tasks) without watcher-alert aggregation or calendar meetings.",
)
@click.option(
    "--no-llm",
    is_flag=True,
    help="(--pipeline-only only) Skip LLM synthesis; write pre-computed sections only.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the generation result as JSON.")
def cmd_generate(
    date_str: str | None,
    dry_run: bool,
    verbose: bool,
    account: str | None,
    pipeline_only: bool,
    no_llm: bool,
    as_json: bool,
) -> None:
    """Generate a dated morning brief.

    By default, aggregates watcher alerts (backstory, pursuit stalls, Slack,
    contract expiry, draft queue), calendar meetings, and a pipeline review
    into a single brief written to <fieldkit_home>/briefs/. Use --pipeline-only
    for just the pursuit/champion/decay pipeline review, without watcher
    alerts or calendar data.
    """
    from fieldkit.commands._account_guard import validate_account_slug

    validate_account_slug(account)

    if pipeline_only:
        with cli_main():
            _run_pipeline_only(no_llm=no_llm, account=account, verbose=verbose, as_json=as_json, dry_run=dry_run)
        return

    import logging

    from fieldkit.config import ConfigError
    from fieldkit.watch.integration_plan import build_integration_plan
    from fieldkit.watch.preflight import preflight_check

    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        plan = build_integration_plan(
            google_when_configured=False,
            sf_requested=False,
            backstory_when_configured=False,
            draft_queue_when_configured=False,
            calendar_when_configured=True,
            slack_requested=False,
            llm_when_configured=True,
        )
    except ConfigError as exc:
        click.echo(f"Config error: {exc}", err=True)
        raise SystemExit(EXIT_DATA) from None

    failures = [] if dry_run else preflight_check(list(plan.preflight_services), dry_run=False)
    if failures:
        raise AuthError("Selected brief integration requires authentication or user setup")

    with cli_main():
        raise SystemExit(
            _run_generate(
                date_str=date_str,
                dry_run=dry_run,
                verbose=verbose,
                account=account,
                as_json=as_json,
                no_llm=not plan.llm,
                calendar_enabled=plan.calendar,
            ).exit_code
        )


def _emit_open_result(latest: Path, age_seconds: float, *, as_json: bool, opened: bool) -> None:
    if as_json:
        click.echo(
            json.dumps(
                {
                    "path": str(latest),
                    "uri": latest.as_uri(),
                    "opened": opened,
                    "stale": age_seconds > 86400,
                    "age_hours": int(age_seconds // 3600),
                },
                indent=2,
            )
        )
    else:
        click.echo(f"{'Opened' if opened else 'Selected'}: {latest}")


@cli.command("open")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the selected brief as JSON.")
@click.option("--no-open", is_flag=True, help="Select the report without launching a viewer.")
def cmd_open(as_json: bool, no_open: bool) -> None:
    """Open the most recent saved morning brief in the system default viewer.

    Looks for the most recent brief file in the briefs/ directory under the
    configured data root. Use 'fieldkit brief generate' to generate a new one.
    """
    try:
        data_root = get_fieldkit_home()
    except ConfigError as exc:
        click.echo(f"Config error: {exc}", err=True)
        raise SystemExit(EXIT_DATA) from None

    briefs_dir = Path(data_root) / "briefs"
    briefs = sorted(briefs_dir.glob("morning-brief-*.md"), reverse=True)
    if not briefs:
        click.echo(f"No morning brief files found in {briefs_dir}", err=True)
        click.echo("Run 'fieldkit brief generate' to generate one.", err=True)
        raise SystemExit(EXIT_DATA) from None

    import time

    latest = briefs[0]

    # historic regression: warn when the most recent brief is stale (>24h old)
    try:
        snapshot = read_saved_report(latest, Path(data_root))
    except (ValueError, OSError):
        click.echo("Saved brief is missing, unsafe, invalid, or empty; generate it again.", err=True)
        raise SystemExit(EXIT_DATA) from None
    age_seconds = time.time() - snapshot.info.st_mtime
    if age_seconds > 86400:
        age_hours = int(age_seconds // 3600)
        age_label = f"{age_hours // 24} day(s)" if age_hours >= 48 else f"{age_hours} hour(s)"
        click.echo(
            f"WARNING: Most recent brief is {age_label} old. Run 'fieldkit brief generate' to generate a fresh one.",
            err=True,
        )

    opened = open_saved_report(latest, no_open=no_open)
    _emit_open_result(latest, age_seconds, as_json=as_json, opened=opened)
    if not no_open and not opened:
        click.echo("The system viewer did not accept the report; open it manually.", err=True)
        raise SystemExit(EXIT_PARTIAL)
