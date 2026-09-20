"""fieldkit brief CLI group.

Defines the Click group and subcommands for the morning brief command.
Business logic lives in main.py (_run, the pipeline-only path) and
generate.py (_run_generate, the merged alerts+calendar+pipeline path);
data collection in collect.py; rendering in render.py.
"""

import json
from pathlib import Path

import click

from fieldkit.cli_exit import EXIT_DATA, EXIT_PARTIAL, cli_main
from fieldkit.cli_registry import declare_write
from fieldkit.commands.brief.generate import _run_generate
from fieldkit.commands.brief.main import (
    DESCRIPTION,
    FLAGS,
    USAGE,
    _run,
)
from fieldkit.config import ConfigError, get_fieldkit_home

# ── Feature introspection metadata (consumed by fieldkit version --features) ──
__all__ = ["DESCRIPTION", "FLAGS", "USAGE", "cli"]


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
            _run(no_llm=no_llm, account=account, verbose=verbose, as_json=as_json, dry_run=dry_run)
        return

    import logging

    from fieldkit.watch.preflight import preflight_check

    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # implementation note: fast-fail before starting the heavy operation.
    # Dry-run skips MCP and LLM checks (not required for a dry run).
    failures = preflight_check(["sf", "gmail", "mcp", "llm"], dry_run=dry_run)
    if failures:
        for msg in failures:
            click.echo(f"Pre-flight check failed: {msg}", err=True)
        raise SystemExit(EXIT_PARTIAL)

    try:
        raise SystemExit(
            _run_generate(date_str=date_str, dry_run=dry_run, verbose=verbose, account=account, as_json=as_json)
        )
    except RuntimeError as exc:
        # implementation note: 0-byte brief guard raises RuntimeError; map to EXIT_DATA (3) so
        # the watcher reports a data error rather than an unhandled Python traceback.
        click.echo(f"Fatal: {exc}", err=True)
        raise SystemExit(EXIT_DATA) from None


def _emit_open_result(latest: Path, age_seconds: float, *, as_json: bool) -> None:
    if as_json:
        click.echo(
            json.dumps(
                {
                    "path": str(latest),
                    "uri": latest.as_uri(),
                    "opened": True,
                    "stale": age_seconds > 86400,
                    "age_hours": int(age_seconds // 3600),
                },
                indent=2,
            )
        )
    else:
        click.echo(f"Opening: {latest}")


@cli.command("open")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the selected brief as JSON.")
def cmd_open(as_json: bool) -> None:
    """Open the most recent saved morning brief in the system default viewer.

    Looks for the most recent brief file in the briefs/ directory under the
    configured data root. Use 'fieldkit brief generate' to generate a new one.
    """
    import webbrowser

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
    age_seconds = time.time() - latest.stat().st_mtime
    if age_seconds > 86400:
        age_hours = int(age_seconds // 3600)
        age_label = f"{age_hours // 24} day(s)" if age_hours >= 48 else f"{age_hours} hour(s)"
        click.echo(
            f"WARNING: Most recent brief is {age_label} old. Run 'fieldkit brief generate' to generate a fresh one.",
            err=True,
        )

    _emit_open_result(latest, age_seconds, as_json=as_json)
    webbrowser.open(latest.as_uri())
