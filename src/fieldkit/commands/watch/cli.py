"""fieldkit watch CLI group — account health and pipeline watchers.

Subcommands:
  run [NAME|--all]  Run one watcher by name, or all of them in sequence
  status            Show each watcher's last-run outcome and timestamp
  logs              Show recent watcher log files

`run` dispatches to one of the sensor watchers named in
``fieldkit.watch.constants.KNOWN_WATCHERS``:
  backstory-health, close-date-countdown, contract-expiry, draft-queue,
  pursuit-stalls, slack-threads, waiting-on-tracker
Each retains its own flags (e.g. `--account`, `--threshold`, `--dry-run`).
`run --all` runs all of them in sequence and writes the morning brief (via
`fieldkit brief generate`'s orchestration, called in-process).
"""

import json
import logging
import re
import shutil
import subprocess
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import click

import fieldkit.watch.status as _watch_status
from fieldkit.cli_exit import EXIT_PARTIAL
from fieldkit.config import TIMEOUT_CRON, ConfigError
from fieldkit.errors import AuthError, EmptyOutputError, LLMError
from fieldkit.watch.status import WatcherOutcome, WatcherRunResult, validate_watcher_result


@click.group(
    name="watch",
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
    invoke_without_command=True,
)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Account health watchers.

    run, status, logs.
    """
    # implementation change follow-up: logging.basicConfig() removed from group callback —
    # it ran even on --help and is now dead code since main() in __main__.py
    # calls it first. Leaf commands that need logging (repair, run) use
    # logging.getLogger(__name__) which inherits the root handler.
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        ctx.exit(1)


@click.group(
    name="run",
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
)
@click.option(
    "--all",
    "all_flag",
    is_flag=True,
    default=False,
    help="Run local and configured optional watchers, then write the morning brief.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="(--all only) Pass --dry-run to each watcher; no files are written.",
)
@click.option(
    "--install-cron",
    is_flag=True,
    default=False,
    help="(--all only) Install a crontab entry that runs 'fieldkit watch run --all'.",
)
@click.option(
    "--cron-time",
    default="0 6 * * *",
    show_default=True,
    help="(--all only) Cron schedule expression used with --install-cron.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="(--all only) Run even if watchers already ran today (bypass once-per-day guard).",
)
@click.option(
    "--allow-partial",
    is_flag=True,
    default=False,
    help="(--all only) Exit 0 after a completed partial pass; fatal failures still fail.",
)
@click.option(
    "--slack",
    is_flag=True,
    default=False,
    help="(--all only) Include the optional Slack thread watcher.",
)
@click.pass_context
def run_group(
    ctx: click.Context,
    all_flag: bool,
    dry_run: bool,
    install_cron: bool,
    cron_time: str,
    force: bool,
    allow_partial: bool,
    slack: bool,
) -> None:
    """Run one watcher by NAME, or the configured aggregate set with --all."""
    if ctx.invoked_subcommand is not None:
        if all_flag:
            raise click.UsageError("--all cannot be combined with a watcher NAME.")
        return
    if not all_flag:
        click.echo(ctx.get_help())
        ctx.exit(1)
    run_all(
        dry_run=dry_run,
        install_cron=install_cron,
        cron_time=cron_time,
        force=force,
        allow_partial=allow_partial,
        slack=slack,
    )


cli.add_command(run_group)


def _register_commands() -> None:
    from fieldkit.commands.watch import backstory_health as _backstory_mod
    from fieldkit.commands.watch import close_date_countdown as _countdown_mod
    from fieldkit.commands.watch import contract_expiry as _contract_expiry_mod
    from fieldkit.commands.watch import draft_queue as _draft_queue_mod
    from fieldkit.commands.watch import pursuit_stalls as _pursuit_mod
    from fieldkit.commands.watch import slack_threads as _slack_mod
    from fieldkit.commands.watch import waiting_on_tracker as _waiting_mod

    run_group.add_command(_backstory_mod.cli, name="backstory-health")
    run_group.add_command(_countdown_mod.cli, name="close-date-countdown")
    run_group.add_command(_contract_expiry_mod.cli, name="contract-expiry")
    run_group.add_command(_draft_queue_mod.cli, name="draft-queue")
    run_group.add_command(_pursuit_mod.cli, name="pursuit-stalls")
    run_group.add_command(_slack_mod.cli, name="slack-threads")
    run_group.add_command(_waiting_mod.cli, name="waiting-on-tracker")


_register_commands()


@cli.command("status")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the watcher status table as JSON.")
def status_cmd(as_json: bool) -> None:
    """Show each watcher's last-run outcome and timestamp.

    Reads watcher-run-status.json (written by every watcher on completion).
    """
    statuses = _watch_status.load_all_statuses()

    if as_json:
        items = [
            {
                "watcher": name,
                "outcome": statuses[name].get("outcome"),
                "last_run": statuses[name].get("last_run"),
            }
            for name in sorted(statuses)
        ]
        click.echo(json.dumps({"items": items, "count": len(items), "filters": {}}, indent=2, default=str))
        return

    if not statuses:
        click.echo("No watcher runs recorded yet. Run 'fieldkit watch run --all' to start.")
        return

    click.echo(f"  {'Watcher':<28} {'Outcome':>8}  {'Last Run':<20}")
    click.echo(f"  {'-' * 28} {'-------':>8}  {'-' * 20}")
    for name in sorted(statuses):
        entry = statuses[name]
        outcome = entry.get("outcome", "?")
        last_run = entry.get("last_run", "?")
        click.echo(f"  {name:<28} {outcome:>8}  {last_run:<20}")


def _abbrev_log_path(path: Path) -> str:
    """Abbreviate path using ~ for home directory (historic regression)."""
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def _emit_logs_json(files: list[Path], watcher: str | None, tail: int | None, *, list_only: bool) -> None:
    content: str | None = None
    if files and not list_only:
        content = files[0].read_text(encoding="utf-8")
        if tail is not None:
            content = "\n".join(content.splitlines()[-tail:])
    click.echo(
        json.dumps(
            {
                "watcher": watcher,
                "files": [_abbrev_log_path(path) for path in files],
                "selected": None if list_only or not files else _abbrev_log_path(files[0]),
                "content": content,
                "list_only": list_only,
                "tail": tail,
            }
        )
    )


def _emit_logs_human(files: list[Path], watcher: str | None, tail: int | None, *, list_only: bool) -> None:
    if not files:
        label = f" for {watcher!r}" if watcher else ""
        click.echo(f"No log files found{label}.")
        return

    if list_only or (tail is None and len(files) > 1):
        import time as _time_mod

        newest_mtime = max(f.stat().st_mtime for f in files)
        age_hours = (_time_mod.time() - newest_mtime) / 3600
        if age_hours > 24:
            click.echo(
                f"NOTE: Last watcher run was {int(age_hours)}h ago — consider running 'fieldkit watch run --all'.",
                err=True,
            )

        click.echo(f"Recent log files ({len(files)}):")
        for path in files:
            click.echo(f"  {_abbrev_log_path(path)}")
        if not list_only:
            click.echo(f"\nShowing most recent: {_abbrev_log_path(files[0])}")
            click.echo("-" * 60)
            click.echo(files[0].read_text(encoding="utf-8"))
        return

    target = files[0]
    click.echo(f"# {_abbrev_log_path(target)}", err=True)
    content = target.read_text(encoding="utf-8")
    if tail is not None:
        content = "\n".join(content.splitlines()[-tail:])
    click.echo(content)


@cli.command("logs")
@click.argument("watcher", required=False, default=None)
@click.option(
    "--tail",
    "-n",
    type=int,
    default=None,
    help="Show only the last N lines of the most recent log file.",
)
@click.option(
    "--list",
    "list_only",
    is_flag=True,
    default=False,
    help="List recent log files without printing content.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit log selection and content as JSON.")
def logs_cmd(watcher: str | None, tail: int | None, list_only: bool, as_json: bool) -> None:
    """Show recent watcher log files.

    WATCHER is an optional watcher name (e.g. backstory-health) to filter.
    Without arguments, lists log files for all watchers.
    """
    from fieldkit.watch.logging import list_recent_logs

    files = list_recent_logs(watcher_name=watcher, n=20)
    if as_json:
        _emit_logs_json(files, watcher, tail, list_only=list_only)
    else:
        _emit_logs_human(files, watcher, tail, list_only=list_only)


#: Ordered list of watcher domains to invoke.
_WATCHER_ORDER = [
    "waiting-on-tracker",
    "pursuit-stalls",
    "close-date-countdown",
    "contract-expiry",
    "backstory-health",
    "slack-threads",
    "draft-queue",
]


def _run_preflight_guard(services: list[str], *, dry_run: bool) -> None:
    """Run preflight checks and raise SystemExit(1) if any fail.

    Extracted from run_all to reduce cyclomatic complexity (implementation note / CRAP gate).

    Args:
        services: Service names to check (e.g. ["sf", "gmail", "mcp"]).
        dry_run: When True, mcp and llm checks are skipped inside preflight_check.

    Raises:
        SystemExit: With code 1 if any preflight check fails.
    """
    from fieldkit.watch.preflight import preflight_check

    failures = preflight_check(services, dry_run=dry_run)
    _raise_for_preflight_failures(failures)


def _raise_for_preflight_failures(failures: list[str]) -> None:
    """Report failed pre-flight checks and stop the orchestration pass."""
    if failures:
        raise AuthError("Selected watcher integration requires authentication or user setup")


def _run_brief_step(*, dry_run: bool, no_llm: bool, calendar_enabled: bool = False) -> tuple[WatcherRunResult, float]:
    """Run llm pre-flight then generate the morning brief in-process.

    Extracted from run_all to reduce cyclomatic complexity (CRAP gate). Brief
    generation runs last, after all alert watchers, folded into the same
    summary table / run-status bookkeeping as the watcher loop. Called
    in-process because it lives in the same command tree as the watcher domains.

    Args:
        dry_run: When True, no files are written by the brief generator.

    Returns:
        A tuple of (execution result, elapsed_seconds).
    """
    import time as _time

    _brief_start = _time.time()
    from fieldkit.commands.brief.generate import _run_generate_inner

    try:
        _brief_rc = _run_generate_inner(
            date_str=None,
            dry_run=dry_run,
            verbose=False,
            account=None,
            no_llm=no_llm,
            calendar_enabled=calendar_enabled,
        )
    except (AuthError, ConfigError, LLMError):
        raise
    except EmptyOutputError as error:
        logging.error(
            "watcher=morning-brief %s",
            "empty output cleanup failed" if error.cleanup_failed else "produced empty output",
        )
        _brief_rc = WatcherRunResult("fatal", False, None, 3)
    except Exception:  # noqa: BLE001 -- aggregate records a bounded fatal failure
        logging.error("watcher=morning-brief raised an unexpected exception")
        _brief_rc = WatcherRunResult("fatal", False, None)
    _brief_rc = _admit_execution_result("morning-brief", _brief_rc, dry_run=dry_run)
    _brief_elapsed = _time.time() - _brief_start
    logging.info("watcher=morning-brief exit_code=%d", _brief_rc.exit_code)
    return _brief_rc, _brief_elapsed


def _aggregate_outcome(results: list[WatcherRunResult]) -> WatcherOutcome:
    """Classify invocation facts before applying continuation policy."""
    if any(
        result.outcome == "fatal" or (result.exit_code != 0 and not result.completed) or result.exit_code >= 2
        for result in results
    ):
        return "fatal"
    return "partial" if any(result.outcome == "partial" for result in results) else "ok"


def _run_all_exit_code(results: list[WatcherRunResult], *, allow_partial: bool, dry_run: bool) -> int:
    """Allow only completed partial work with same-invocation persistence."""
    max_code = max(result.exit_code for result in results)
    if (
        allow_partial
        and not dry_run
        and _aggregate_outcome(results) == "partial"
        and all(result.completed_partial for result in results if result.exit_code != 0)
    ):
        return 0
    return max_code


def _admit_execution_result(name: str, result: object, *, dry_run: bool) -> WatcherRunResult:
    admitted = validate_watcher_result(result, dry_run=dry_run)
    if admitted is not result:
        logging.error("watcher=%s returned invalid execution evidence", name)
    return admitted


def _invoke_watcher(name: str, run: Callable[[], object], *, dry_run: bool = False) -> WatcherRunResult:
    """Run one watcher behind the aggregate command's output and error boundary."""
    try:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            result = run()
    except (AuthError, ConfigError):
        raise
    except EmptyOutputError as error:
        logging.error(
            "watcher=%s %s", name, "empty output cleanup failed" if error.cleanup_failed else "produced empty output"
        )
        return WatcherRunResult("fatal", False, None, 3)
    except Exception:  # noqa: BLE001 -- one watcher must not stop the daily chain
        logging.error("watcher=%s raised an unexpected exception", name)
        return WatcherRunResult("fatal", False, None)
    return _admit_execution_result(name, result, dry_run=dry_run)


def run_all(
    *,
    dry_run: bool,
    install_cron: bool,
    cron_time: str,
    force: bool,
    allow_partial: bool = False,
    slack: bool = False,
) -> None:
    """Run watcher domains sequentially, then write and persist the morning brief.

    Invoked by `watch run --all`; continues after watcher exceptions, aggregates
    the maximum exit code, and persists a non-dry-run run-all status record.
    """
    if install_cron:
        _handle_install_cron(cron_time=cron_time, dry_run=dry_run)
        raise SystemExit(0)

    from fieldkit.config import ConfigError
    from fieldkit.watch.integration_plan import build_integration_plan

    try:
        plan = build_integration_plan(
            google_when_configured=False,
            sf_requested=False,
            backstory_when_configured=True,
            draft_queue_when_configured=True,
            calendar_when_configured=True,
            slack_requested=slack,
            llm_when_configured=True,
        )
    except ConfigError:
        click.echo("Config error: optional integration selection is invalid", err=True)
        raise SystemExit(3) from None
    if not dry_run:
        _run_preflight_guard(list(plan.preflight_services), dry_run=False)

    import time as _time

    _run_start = _time.time()

    daily = _watch_status.get_daily_run_snapshot("run-all") if not dry_run and not force else None
    if daily is not None and daily.ran_today:
        if daily.outcome != "ok":
            logging.warning(
                "run --all last run today was nonpassing — "
                "re-run with --force to investigate or check watcher-run-status.json"
            )
            raise SystemExit(EXIT_PARTIAL)
        logging.info("run --all already executed today; use --force to override")
        raise SystemExit(0)

    from fieldkit.watch import backstory_health as _backstory_mod
    from fieldkit.watch import close_date_countdown as _countdown_mod
    from fieldkit.watch import contract_expiry as _contract_expiry_mod
    from fieldkit.watch import draft_queue as _draft_queue_mod
    from fieldkit.watch import pursuit_stalls as _pursuit_mod
    from fieldkit.watch import slack_threads as _slack_mod
    from fieldkit.watch import waiting_on_tracker as _waiting_mod

    watcher_runs = {
        "waiting-on-tracker": lambda: _waiting_mod._run(
            threshold=_waiting_mod._DEFAULT_THRESHOLD_DAYS,
            dry_run=dry_run,
            as_json=False,
        ),
        "pursuit-stalls": lambda: _pursuit_mod._run_pursuit_stalls(
            threshold=_pursuit_mod._DEFAULT_STALL_DAYS,
            account=None,
            dry_run=dry_run,
            force=force,
        ),
        "close-date-countdown": lambda: _countdown_mod._run_countdown(
            threshold_red=_countdown_mod._DEFAULT_RED_DAYS,
            threshold_yellow=_countdown_mod._DEFAULT_YELLOW_DAYS,
            threshold_green=_countdown_mod._DEFAULT_GREEN_DAYS,
            account_filter=None,
            dry_run=dry_run,
            as_json=False,
        ),
        "contract-expiry": lambda: _contract_expiry_mod._run_contract_expiry(
            account_filter=None, dry_run=dry_run, as_json=False
        ),
        "backstory-health": lambda: _backstory_mod._run_backstory_health(
            threshold=_backstory_mod._DEFAULT_THRESHOLD,
            account=None,
            dry_run=dry_run,
            as_json=False,
        ),
        "slack-threads": lambda: (
            _slack_mod._run_slack_threads(
                threshold_hours=_slack_mod._DEFAULT_THRESHOLD_HOURS,
                account=None,
                limit=_slack_mod._DEFAULT_SEARCH_LIMIT,
                limit_per_account=None,
                dry_run=dry_run,
            ).run
        ),
        "draft-queue": lambda: _draft_queue_mod._run_draft_queue(dry_run=dry_run, account=None, as_json=False),
    }

    max_code = 0

    # implementation change: collect per-watcher results for summary table
    _watcher_results: list[tuple[str, WatcherRunResult, float]] = []

    selected_optional = set(plan.optional_watchers)
    active_watchers = [
        name
        for name in _WATCHER_ORDER
        if (name not in {"backstory-health", "slack-threads", "draft-queue"} or name in selected_optional)
        and not (dry_run and name == "slack-threads")
    ]

    for name in active_watchers:
        _watcher_start = _time.time()
        rc = _invoke_watcher(name, watcher_runs[name], dry_run=dry_run)
        _watcher_elapsed = _time.time() - _watcher_start

        logging.info("watcher=%s exit_code=%d", name, rc.exit_code)
        _watcher_results.append((name, rc, _watcher_elapsed))
        max_code = max(max_code, rc.exit_code)

    try:
        _brief_rc, _brief_elapsed = _run_brief_step(
            dry_run=dry_run,
            no_llm=not plan.llm,
            calendar_enabled=plan.calendar,
        )
    except LLMError:
        if not dry_run:
            status_write = _watch_status.write_run_status(
                watcher="run-all",
                outcome="fatal",
                records_checked=len(active_watchers),
                alerts_generated=0,
                failures=sum(result.exit_code != 0 for _, result, _ in _watcher_results) + 1,
                elapsed_seconds=_time.time() - _run_start,
                dry_run=False,
            )
            if status_write != "written":
                click.echo("Error: watcher run status was not persisted.", err=True)
        raise
    _watcher_results.append(("morning-brief", _brief_rc, _brief_elapsed))
    max_code = max(max_code, _brief_rc.exit_code)

    # implementation change: print summary table
    click.echo("\nWatcher Summary:")
    click.echo(f"  {'Watcher':<28} {'Exit':>4}  {'Duration':>8}")
    click.echo(f"  {'-' * 28} {'----':>4}  {'--------':>8}")
    for _name, _rc, _dur in _watcher_results:
        _status = "ok" if _rc.exit_code == 0 else ("WARN" if _rc.exit_code == 1 else "FAIL")
        click.echo(f"  {_name:<28} {_status:>4}  {_dur:>6.1f}s")
    for reason in plan.skipped:
        click.echo(f"  not run: {reason}")
    if dry_run and "slack-threads" in selected_optional:
        click.echo("  not run: Slack selected; provider scan not run in aggregate preview")

    if not dry_run:
        elapsed = _time.time() - _run_start
        _outcome = _aggregate_outcome([result for _, result, _ in _watcher_results])
        status_write = _watch_status.write_run_status(
            watcher="run-all",
            outcome=_outcome,
            records_checked=len(active_watchers),
            alerts_generated=0,
            failures=sum(1 for _name, result, _duration in _watcher_results if result.exit_code != 0),
            elapsed_seconds=elapsed,
            dry_run=False,
        )
        if type(status_write) is not str or status_write != "written":
            click.echo("Error: watcher run status was not persisted.", err=True)
            raise SystemExit(max(max_code, EXIT_PARTIAL))

    raise SystemExit(
        _run_all_exit_code([result for _, result, _ in _watcher_results], allow_partial=allow_partial, dry_run=dry_run)
    )


_CRON_FIELD_RE = re.compile(r"^[0-9*/,-]+$")
_CRON_NUMBER_RE = re.compile(r"[0-9]+")

_CRON_FIELD_RANGES: tuple[tuple[str, int, int], ...] = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day of month", 1, 31),
    ("month", 1, 12),
    ("day of week", 0, 7),
)


def _validate_cron_field_range(field: str, name: str, low: int, high: int) -> None:
    """Raise ClickException if any numeric value in `field` falls outside [low, high].

    Checks every digit run in the field (plain values, and the numeric bounds
    of ranges/lists/steps) rather than parsing cron syntax fully — sufficient
    to catch out-of-range values like hour 99 without rejecting valid
    '*', ',', '-', '/' combinations.
    """
    for match in _CRON_NUMBER_RE.finditer(field):
        value = int(match.group())
        if not low <= value <= high:
            raise click.ClickException(
                f"Invalid cron expression: {name} field {field!r} contains "
                f"out-of-range value {value} (expected {low}-{high})"
            )


def _validate_cron_expression(cron_time: str) -> None:
    """Validate a 5-field cron expression before it is written to crontab.

    Raises ClickException — not click.BadParameter, since this runs outside a
    Click command callback — when the expression does not have exactly 5
    whitespace-separated fields, when a field contains characters outside the
    standard cron field character set (ASCII digits, '*', '/', ',', '-'), or
    when a field's numeric value is outside its valid range (e.g. hour 99).
    """
    fields = cron_time.split()
    if len(fields) != 5:
        raise click.ClickException(
            "Invalid cron expression: expected 5 fields (minute hour day month weekday), "
            f"got {len(fields)}: {cron_time!r}"
        )
    for field in fields:
        if not _CRON_FIELD_RE.match(field):
            raise click.ClickException(
                f"Invalid cron expression: field {field!r} contains invalid characters "
                "(expected digits, '*', '/', ',', '-')"
            )
    for field, (name, low, high) in zip(fields, _CRON_FIELD_RANGES, strict=True):
        _validate_cron_field_range(field, name, low, high)


def _handle_install_cron(*, cron_time: str, dry_run: bool) -> None:
    """Install (or preview) a crontab entry for 'fieldkit watch run --all'.

    historic regression: use the absolute path to the fieldkit binary so the cron entry
    works even when /usr/local/bin (or wherever uv installs tools) is not in
    the cron environment's PATH.

    Raises ClickException when the fieldkit binary cannot be located in PATH,
    because installing a cron entry with a bare name would silently fail at
    runtime in a cron environment that has a minimal PATH. Also raises
    ClickException when --cron-time is not a valid 5-field cron expression.
    """
    _validate_cron_expression(cron_time)
    fieldkit_bin = shutil.which("fieldkit")
    if not fieldkit_bin:
        raise click.ClickException(
            "fieldkit binary not found in PATH — cannot install cron entry. "
            "Ensure 'fieldkit' is on PATH (e.g. run 'uv tool install . --reinstall') "
            "and try again."
        )
    cron_line = f"{cron_time} {fieldkit_bin} watch run --all"

    if dry_run:
        click.echo(f"[dry-run] would add crontab entry: {cron_line}")
        return

    result = subprocess.run(
        ["crontab", "-l"],
        capture_output=True,
        text=True,
        check=False,
        timeout=TIMEOUT_CRON,
    )
    existing = result.stdout if result.returncode == 0 else ""

    if "fieldkit watch run --all" in existing:
        logging.info("cron entry already present — no changes made")
        click.echo("fieldkit watch run --all cron entry already installed.")
        return

    new_crontab = existing.rstrip("\n") + ("\n" if existing else "") + cron_line + "\n"
    install = subprocess.run(
        ["crontab", "-"],
        input=new_crontab,
        text=True,
        capture_output=True,
        check=False,
        timeout=TIMEOUT_CRON,
    )
    if install.returncode != 0:
        logging.error("crontab install failed: %s", install.stderr.strip())
        click.echo(f"Error installing crontab: {install.stderr.strip()}", err=True)
        raise SystemExit(EXIT_PARTIAL)

    logging.info("cron entry installed: %s", cron_line)
    click.echo(f"Installed crontab entry: {cron_line}")
