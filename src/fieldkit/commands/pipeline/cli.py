"""fieldkit pipeline CLI group.

Contains the Click group and subcommands for the pipeline review command.
Calls into main._run and watch.morning_brief_render.calculate_quota_gap for business logic.
"""

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

import click

from fieldkit.cli_exit import EXIT_AUTH, EXIT_DATA, EXIT_PARTIAL
from fieldkit.config import ConfigError, get_fieldkit_home, get_pipeline_quota, write_pipeline_quota

# Accepted period formats: YYYY-H1, YYYY-H2, YYYY-Q1 … YYYY-Q4
_PERIOD_RE = re.compile(r"\d{4}-[HQ][1-4]")
_SAFE_ACCOUNT_SLUG_RE = re.compile(r"[A-Za-z0-9_-]+")

# ── Feature introspection metadata (consumed by fieldkit version --features) ──

DESCRIPTION = "Generate the weekly pipeline review document"
USAGE = "fieldkit pipeline [--no-llm] [--data-root PATH] [--account SLUG]"
FLAGS = {
    "--no-llm": "Skip LLM synthesis; write deterministic table only",
    "--data-root": "Override data root path",
    "--account": "Generate a review for one configured account",
}


def _validate_pipeline_account(account: str | None) -> None:
    from fieldkit.commands._account_guard import validate_account_slug

    validate_account_slug(account)
    if account is not None and _SAFE_ACCOUNT_SLUG_RE.fullmatch(account) is None:
        click.echo(f"Error: unsafe account slug '{account}'.", err=True)
        raise SystemExit(EXIT_DATA)


@click.group(
    name="pipeline",
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.option(
    "--no-llm",
    is_flag=True,
    help="Skip LLM synthesis; write deterministic table only.",
)
@click.option(
    "--data-root",
    "data_root_override",
    type=click.Path(path_type=Path),
    default=None,
    help="Override data root path (default: from fieldkit.config).",
)
@click.option("--account", "-a", default=None, metavar="SLUG", help="Generate a review for one configured account.")
@click.pass_context
def cli(ctx: click.Context, no_llm: bool, data_root_override: Path | None, account: str | None) -> None:
    """Generate the weekly pipeline review document.

    Running 'fieldkit pipeline' generates and prints the review.
    Use 'fieldkit pipeline open' to re-open the most recent saved review.
    Use 'fieldkit pipeline quota' to show the quota gap summary.

    \b
    The --no-llm flag skips LLM synthesis and works when placed anywhere:
      fieldkit pipeline --no-llm
      fieldkit pipeline open  (--no-llm not applicable to subcommands)
    """
    # historic regression: detect --no-llm placed after a subcommand name in sys.argv and
    # surface a helpful error rather than silently ignoring it.
    import sys

    argv = sys.argv[1:]
    subcmds = {"open", "quota"}
    for i, arg in enumerate(argv):
        if arg in subcmds and "--no-llm" in argv[i + 1 :]:
            click.echo(
                f"Error: --no-llm must come before the subcommand name.\n"
                f"  Got : fieldkit pipeline {arg} --no-llm\n"
                f"  Fix : fieldkit pipeline --no-llm {arg}",
                err=True,
            )
            raise SystemExit(EXIT_DATA) from None

    if ctx.invoked_subcommand is None:
        # Lazy import to avoid circular dependency at module level
        from fieldkit.commands.pipeline.main import _run

        _validate_pipeline_account(account)
        _run(no_llm=no_llm, data_root_override=data_root_override, account=account)
    elif account is not None:
        click.echo(
            f"Error: --account for '{ctx.invoked_subcommand}' must come after the subcommand name.",
            err=True,
        )
        raise SystemExit(EXIT_DATA)


@cli.command("quota")
@click.option(
    "--set",
    "set_target",
    type=int,
    default=None,
    help="Set the quota target in USD (e.g. 5000000). Requires --period.",
)
@click.option(
    "--period",
    default=None,
    help="Quota period label (e.g. 2026-H2). Required with --set.",
)
@click.option(
    "--data-root",
    "data_root_override",
    type=click.Path(path_type=Path),
    default=None,
    help="Override data root path (default: from fieldkit.config).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Machine-readable JSON output.",
)
@click.option("--account", "-a", default=None, help="Filter quota to a single account slug.")
@click.option(
    "--source",
    type=click.Choice(["pursuits", "sf"]),
    default="pursuits",
    show_default=True,
    help=(
        "Source for closed-won figures. 'pursuits' (default) uses local pursuit "
        "files and does not compute an attainment gap (pursuit scope is not "
        "comparable to a full-book quota). 'sf' pulls live territory-scoped "
        "closed-won from Salesforce and computes a real gap."
    ),
)
def cmd_quota(
    set_target: int | None,
    period: str | None,
    data_root_override: Path | None,
    as_json: bool,
    account: str | None,
    source: str,
) -> None:
    """Show quota gap: Weighted pipeline / Closed-won / Quota target / Gap.

    Reads quota target from ~/.config/fieldkit/config.yaml (pipeline.quota.target).
    Configure with: fieldkit pipeline quota --set <target> --period <period>

    Default (--source pursuits) labels closed-won as configured-pursuit-scoped and
    suppresses the gap. Pass --source sf for a live, territory-scoped attainment gap.
    """
    from datetime import UTC, datetime

    from fieldkit.commands._account_guard import validate_account_slug
    from fieldkit.commands.pipeline.quota import _collect_pursuits_for_quota, get_period_end_date
    from fieldkit.watch.morning_brief_render import calculate_quota_gap

    if set_target is not None:
        if period is None:
            raise click.BadParameter("--period is required when using --set", param_hint="'--period'")
        if not _PERIOD_RE.fullmatch(period):
            raise click.BadParameter(
                f"Expected format like '2026-H2' or '2025-Q3', got {period!r}",
                param_hint="'--period'",
            )
        write_pipeline_quota(target=set_target, period=period)
        click.echo(f"Quota set: target={set_target}, period={period}", err=True)
        return

    validate_account_slug(account)
    quota_config = get_pipeline_quota()
    if not quota_config:
        click.echo(
            "No quota configured. Run: fieldkit pipeline quota --set <target> --period <period>",
            err=True,
        )
        raise SystemExit(EXIT_DATA) from None

    try:
        data_root = data_root_override or get_fieldkit_home()
    except ConfigError as exc:
        click.echo(f"Config error: {exc}", err=True)
        raise SystemExit(EXIT_DATA) from None

    pursuits = _collect_pursuits_for_quota(data_root, account_filter=account)
    result = calculate_quota_gap(pursuits, quota_config)

    # implementation note: --source sf pulls a real, territory-scoped closed-won from Salesforce.
    # Fetch it up front so both the JSON and human paths use the same figure.
    sf_closed_won: float | None = _fetch_sf_closed_won_or_exit(data_root) if source == "sf" else None

    target = result["target"]
    closed_won = result["closed_won"]
    weighted_val = result["weighted"]

    if as_json:
        # cell-28b9dae2e9395288: machine-readable output.
        payload: dict[str, object] = dict(result)
        payload["source"] = source
        if sf_closed_won is not None:
            payload["sf_closed_won"] = sf_closed_won
            payload["gap"] = target - sf_closed_won - weighted_val
        click.echo(json.dumps(payload, indent=2, default=str))
        return

    def _fmt(v: float) -> str:
        return f"${v:,.0f}"

    quota_period = str(quota_config.get("period", "") or "")
    period_label = f" ({quota_period})" if quota_period else ""
    if quota_period:
        try:
            end_date = get_period_end_date(quota_period)
        except ValueError:
            pass
        else:
            days_remaining = (end_date - datetime.now(tz=UTC).date()).days
            period_label = f" ({quota_period}, ends {end_date.isoformat()}, {days_remaining} days remaining)"

    # historic regression: warn about pursuits excluded for missing ACV
    excluded_count = result["excluded_count"]
    excluded_names = result["excluded_names"]
    if excluded_count:
        for name in excluded_names:
            click.echo(f"NOTE: '{name}' excluded from quota — no ACV set", err=True)
        click.echo(
            f"NOTE: {excluded_count} pursuit(s) excluded from quota totals (no ACV set)",
            err=True,
        )

    if account is not None:
        click.echo(f"Target is global; showing {account} contribution.")
    click.echo(f"Quota Gap{period_label}")
    click.echo(f"  Weighted pipeline                     : {_fmt(weighted_val)}")

    if sf_closed_won is not None:
        gap = target - sf_closed_won - weighted_val
        gap_str = _fmt(gap) if gap >= 0 else f"-{_fmt(abs(gap))}"
        click.echo(f"  Closed-won (Salesforce, FY, territory-scoped) : {_fmt(sf_closed_won)}")
        click.echo(f"  Quota target                          : {_fmt(target)}")
        click.echo(f"  Gap                                   : {gap_str}")
    else:
        click.echo(f"  Closed-won (configured pursuits only) : {_fmt(closed_won)}")
        click.echo(f"  Quota target                          : {_fmt(target)}")
        click.echo()
        click.echo("  Attainment gap: n/a — pursuit-scope closed-won is not comparable")
        click.echo("  to a full-book quota. Pass --source sf to pull live")
        click.echo("  territory-scoped attainment from Salesforce.")


def _fetch_sf_closed_won_or_exit(data_root: Path) -> float:
    """Pull live territory-scoped closed-won, mapping failures to CLI exit codes.

    implementation note: Wraps :func:`fetch_sf_closed_won` so ``cmd_quota`` stays a thin
    adapter. Exit codes follow the project contract: configuration problems
    require investigation (3), auth requires user action (2), and a transient
    Salesforce API error may clear on retry (1).
    """
    from fieldkit.commands.pipeline.quota import fetch_sf_closed_won
    from fieldkit.sf.client import SFAPIError, SFAuthError

    try:
        return fetch_sf_closed_won(data_root)
    except ConfigError as exc:
        click.echo(f"Config error: {exc}", err=True)
        raise SystemExit(EXIT_DATA) from None
    except SFAuthError as exc:
        click.echo(f"Salesforce auth error: {exc}", err=True)
        raise SystemExit(EXIT_AUTH) from None
    except SFAPIError as exc:
        click.echo(f"Salesforce error: {exc}", err=True)
        raise SystemExit(EXIT_PARTIAL) from None


def _emit_open_result(
    latest: Path,
    file_date: date,
    age_days: int,
    *,
    account: str | None,
    as_json: bool,
) -> None:
    if as_json:
        click.echo(
            json.dumps(
                {
                    "path": str(latest),
                    "uri": latest.as_uri(),
                    "opened": True,
                    "review_date": file_date.isoformat(),
                    "age_days": age_days,
                    "stale": age_days > 0,
                    "account": account,
                },
                indent=2,
            )
        )
    else:
        click.echo(f"Opening: {latest}")


@cli.command("open")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the selected pipeline review as JSON.")
@click.option("--account", "-a", default=None, metavar="SLUG", help="Open the newest review for one account.")
def cmd_open(as_json: bool, account: str | None) -> None:
    """Open the most recent saved pipeline review in the system default viewer.

    Looks for the most recent pipeline-review-*.md file in the briefs/ directory.
    Use 'fieldkit pipeline' to generate a new one.
    """
    import webbrowser

    _validate_pipeline_account(account)

    try:
        data_root = get_fieldkit_home()
    except ConfigError as exc:
        click.echo(f"Config error: {exc}", err=True)
        raise SystemExit(EXIT_DATA) from None

    briefs_dir = Path(data_root) / "briefs"
    if account is None:
        filename_pattern = re.compile(r"pipeline-review-(\d{4}-\d{2}-\d{2})\.md")
        regenerate_command = "fieldkit pipeline"
    else:
        filename_pattern = re.compile(rf"pipeline-review-{re.escape(account)}-(\d{{4}}-\d{{2}}-\d{{2}})\.md")
        regenerate_command = f"fieldkit pipeline --account {account}"

    reviews: list[tuple[date, Path]] = []
    for review in briefs_dir.glob("pipeline-review-*.md"):
        match = filename_pattern.fullmatch(review.name)
        if match is None:
            continue
        try:
            review_date = date.fromisoformat(match.group(1))
        except ValueError:
            continue
        reviews.append((review_date, review))

    if not reviews:
        scope = f" for account '{account}'" if account is not None else ""
        click.echo(f"No pipeline review files found{scope} in {briefs_dir}", err=True)
        click.echo(f"Run '{regenerate_command}' to generate one.", err=True)
        raise SystemExit(EXIT_DATA) from None

    file_date, latest = max(reviews, key=lambda item: item[0])

    # Warn if the most recent review is not from today.
    age_days = (datetime.now(tz=UTC).date() - file_date).days
    if age_days > 0:
        age_label = f"{age_days} day(s)"
        click.echo(
            f"WARNING: Most recent pipeline review is {age_label} old "
            f"(from {file_date}). Run '{regenerate_command}' to regenerate.",
            err=True,
        )

    _emit_open_result(latest, file_date, age_days, account=account, as_json=as_json)
    webbrowser.open(latest.as_uri())
