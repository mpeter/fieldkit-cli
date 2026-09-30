"""fieldkit sync — Ordered full data pipeline runner.

Runs selected fieldkit data workflows in dependency order and reports each
step's bounded outcome. Some selected integrations use credentials or update
configured workspace and service data.

Default pipeline:
  Phase 1 — Gmail (only when configured; skipped with --quick)
    [1] fieldkit gmail sync
    [2] people-index rebuild (in-process — fieldkit.contact.people_index.build_people_index())
    [3] fieldkit gmail account-tags
    [4] fieldkit gmail enrich-pursuits

  Phase 2 — Ingest (always runs)
    [5] fieldkit ingest discover
    [6] fieldkit ingest run

  Phase 3 — Watchers
    fieldkit watch run pursuit-stalls (always)
    fieldkit watch run backstory-health (only when configured)
    fieldkit watch run slack-threads (only with --slack)

  Phase 4 — Salesforce (only with --sf)
    [+1] fieldkit sf listview

The people-index step refreshes the contact cache in-process. The separate
``fieldkit contact list`` command remains a pure cache read.

Ordinary step failures are non-fatal: the pipeline continues and failures are
reported in a summary at the end. The final status preserves fieldkit's
canonical 0/1/2/3 exit contract across all completed steps.
"""

import json
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import click

from fieldkit.cli_exit import EXIT_AUTH, EXIT_DATA, EXIT_PARTIAL, EXIT_SUCCESS
from fieldkit.cli_registry import declare_write
from fieldkit.config import TIMEOUT_DATASYNC, TIMEOUT_PROCESS_KILL_GRACE, ConfigError
from fieldkit.errors import AuthError
from fieldkit.util.bounded_process import BoundedProcessError, run_bounded_process

LOG_PREFIX = "[sync]"

#: Sentinel label for the in-process people-index rebuild step (empty argv in
#: ``_build_steps`` — routed to ``_run_people_index_step`` by ``run_pipeline``).
PEOPLE_INDEX_LABEL = "people-index"

# Maximum retained and rendered output per child stream. The process runner
# enforces the byte limit before decoding; verbose rendering applies the tighter
# line and character limits to the retained diagnostic tail.
MAX_VERBOSE_LINES = 100
MAX_VERBOSE_CHARS = 16 * 1024
DATASYNC_CAPTURE_BYTES = 4 * 1024 * 1024
_PROCESS_START_FAILURE_NOTE = "Unable to start command"
_PROCESS_EXECUTION_FAILURE_NOTE = "Command execution failed"
_PROCESS_OUTPUT_LIMIT_NOTE = "Command output exceeded limit"

_WATCHER_NAMES = frozenset({"backstory-health", "pursuit-stalls", "slack-threads"})


def _truncate_output(
    text: str,
    max_lines: int = MAX_VERBOSE_LINES,
    max_chars: int = MAX_VERBOSE_CHARS,
) -> str:
    """Return *text* truncated to at most *max_lines* lines from the tail.

    When truncation occurs, a summary line is appended so the user knows how
    many lines were dropped.  Tail lines are kept (not head) because the most
    recent output is typically the most diagnostic.

    Args:
        text: Raw subprocess output string.
        max_lines: Maximum number of lines to retain.
        max_chars: Maximum rendered characters, including a truncation notice.

    Returns:
        The (possibly truncated) string.
    """
    if max_lines <= 0 or max_chars <= 0:
        raise ValueError("verbose output bounds must be positive")

    lines = text.splitlines()
    if len(lines) > max_lines:
        dropped = len(lines) - max_lines
        text = (
            "\n".join(lines[-max_lines:])
            + f"\n[... truncated — showing last {max_lines} of {len(lines)} lines ({dropped} dropped)]"
        )
    if len(text) > max_chars:
        notice = f"[... truncated — showing last characters of {len(text)} total ...]\n"
        retained = max_chars - len(notice)
        if retained <= 0:
            return notice[:max_chars]
        return notice + text[-retained:]
    return text


@dataclass
class StepResult:
    index: int
    total: int
    label: str
    cmd: list[str]
    success: bool
    elapsed: float
    note: str = ""
    exit_code: int = EXIT_SUCCESS


@dataclass
class _StepExecution:
    result: StepResult
    stdout: str = ""
    stderr: str = ""
    display: str | None = None


@dataclass
class RunConfig:
    quick: bool = False
    sf: bool = False
    google: bool = False
    backstory: bool = False
    slack: bool = False
    dry_run: bool = False
    account: str | None = None
    verbose: bool = False
    #: Suppress the stdout banner and per-step progress lines so the CLI can
    #: own stdout with a single JSON document.
    as_json: bool = False
    skipped: tuple[str, ...] = ()


def _build_steps(cfg: RunConfig) -> list[tuple[str, list[str]]]:
    """Return ordered list of (label, argv) for the configured run.

    historic regression: All step commands use ``[sys.executable, "-m", "fieldkit", ...]``
    instead of ``["fieldkit", ...]``. This guarantees same-interpreter,
    same-source execution — the PATH-resolved ``fieldkit`` binary may be stale
    if ``make install`` was not run after a source change.

    The ``people-index`` step has an empty argv — it is not a subprocess call.
    ``run_pipeline`` recognises the ``PEOPLE_INDEX_LABEL`` and routes it to
    ``_run_people_index_step``, which calls
    ``fieldkit.contact.people_index.build_people_index()`` in-process.
    """
    # historic regression: Use sys.executable + "-m" + "fieldkit" to avoid PATH version skew.
    _fk = [sys.executable, "-m", "fieldkit"]
    steps: list[tuple[str, list[str]]] = []

    if cfg.google and not cfg.quick:
        gmail_sync = [*_fk, "gmail", "sync"]
        gmail_tags = [*_fk, "gmail", "account-tags"]
        gmail_enrich = [*_fk, "gmail", "enrich-pursuits"]
        if cfg.account:
            # `gmail sync` is deliberately NOT scoped. It fills the local SQLite
            # cache; narrowing it to one account would leave that cache partial,
            # and every later unscoped query would then read the gap as an
            # absence of mail rather than an absence of sync. The cache stays
            # complete (it is incremental, so the cost is small) and only the
            # analysis steps below are scoped.
            #
            # It also does not accept the flag: passing it produced
            # "Error: No such option: --account", failing the first step of every
            # `fieldkit sync --account <slug>` run and taking the whole sync to
            # exit 1.
            gmail_tags += ["--account", cfg.account]
            gmail_enrich += ["--account", cfg.account]
        steps += [
            ("gmail sync", gmail_sync),
            (PEOPLE_INDEX_LABEL, []),
            ("account-tags", gmail_tags),
            ("enrich-pursuits", gmail_enrich),
        ]

    ingest_discover = [*_fk, "ingest", "discover", "--pipeline", "transcript-ingest"]
    ingest_run = [*_fk, "ingest", "run", "--pipeline", "transcript-ingest"]
    steps += [
        ("ingest discover", ingest_discover),
        ("ingest run", ingest_run),
    ]

    stalls_cmd = [*_fk, "watch", "run", "pursuit-stalls"]
    if cfg.account:
        stalls_cmd += ["--account", cfg.account]
    watcher_steps: list[tuple[str, list[str]]] = [("pursuit-stalls", stalls_cmd)]
    if cfg.backstory:
        backstory_cmd = [*_fk, "watch", "run", "backstory-health"]
        if cfg.account:
            backstory_cmd += ["--account", cfg.account]
        watcher_steps.insert(0, ("backstory-health", backstory_cmd))
    if cfg.slack:
        slack_cmd = [*_fk, "watch", "run", "slack-threads"]
        if cfg.account:
            slack_cmd += ["--account", cfg.account]
        watcher_steps.append(("slack-threads", slack_cmd))
    steps += watcher_steps

    if cfg.sf:
        sf_cmd = [*_fk, "sf", "listview"]  # no TARGET = sync all accounts
        steps.append(("sf listview", sf_cmd))

    return steps


def _public_argv(cmd: list[str]) -> list[str]:
    """Return an equivalent diagnostic argv without private executable paths."""
    if len(cmd) >= 3 and cmd[1:3] == ["-m", "fieldkit"]:
        return ["fieldkit", *cmd[3:]]
    if cmd and Path(cmd[0]).is_absolute():
        return [Path(cmd[0]).name, *cmd[1:]]
    return list(cmd)


def _canonical_child_exit(returncode: int) -> int:
    """Normalize one child return code into fieldkit's public exit contract."""
    if returncode in {EXIT_SUCCESS, EXIT_PARTIAL, EXIT_AUTH, EXIT_DATA}:
        return returncode
    return EXIT_PARTIAL


def _step_note(exit_code: int) -> str:
    """Return a fixed payload-free note for one canonical child outcome."""
    return {
        EXIT_SUCCESS: "",
        EXIT_PARTIAL: "Command failed",
        EXIT_AUTH: "Authentication required",
        EXIT_DATA: "Invalid data or usage",
    }[exit_code]


def _execute_step(index: int, total: int, label: str, cmd: list[str]) -> _StepExecution:
    """Execute one real subprocess step without writing terminal output."""
    t0 = time.monotonic()
    public_cmd = _public_argv(cmd)
    try:
        completed = run_bounded_process(
            cmd,
            timeout=TIMEOUT_DATASYNC,
            stdout_limit=DATASYNC_CAPTURE_BYTES,
            stderr_limit=DATASYNC_CAPTURE_BYTES,
            cleanup_timeout=TIMEOUT_PROCESS_KILL_GRACE,
        )
    except BoundedProcessError as exc:
        elapsed = time.monotonic() - t0
        if exc.reason == "timeout":
            note = "timeout"
            display = f"TIMEOUT after {elapsed:.0f}s"
        elif exc.reason == "start":
            note = _PROCESS_START_FAILURE_NOTE
            display = note
        elif exc.reason == "overflow":
            note = _PROCESS_OUTPUT_LIMIT_NOTE
            display = note
        else:
            note = _PROCESS_EXECUTION_FAILURE_NOTE
            display = note
        result = StepResult(index, total, label, public_cmd, False, elapsed, note, EXIT_PARTIAL)
        return _StepExecution(result, display=display)

    elapsed = time.monotonic() - t0
    exit_code = _canonical_child_exit(completed.returncode)
    result = StepResult(
        index=index,
        total=total,
        label=label,
        cmd=public_cmd,
        success=exit_code == EXIT_SUCCESS,
        elapsed=elapsed,
        note=_step_note(exit_code),
        exit_code=exit_code,
    )
    return _StepExecution(result, stdout=completed.stdout or "", stderr=completed.stderr or "")


def _render_step(execution: _StepExecution, *, verbose: bool) -> None:
    """Render one completed subprocess step from the coordinator thread."""
    result = execution.result
    label_str = f"{result.label:<22}"
    if execution.display is not None:
        click.echo(f"  [{result.index}/{result.total}] {label_str}  ✗  {execution.display}", err=True)
        return

    icon = "✓" if result.success else "✗"
    click.echo(
        f"  [{result.index}/{result.total}] {label_str}  {icon}  {result.note[:60]:<60}  ({result.elapsed:.1f}s)",
        err=True,
    )
    if verbose:
        for stream_name, stream_content in (("stderr", execution.stderr), ("stdout", execution.stdout)):
            if stream_content.strip():
                click.echo(f"--- {result.label} {stream_name} ---", err=True)
                click.echo(_truncate_output(stream_content.rstrip()), err=True)


def _run_step(
    index: int,
    total: int,
    label: str,
    cmd: list[str],
    *,
    dry_run: bool,
    verbose: bool = False,
) -> StepResult:
    """Execute one pipeline step and return its result.

    When ``verbose=True``, bounded subprocess stdout and stderr are printed to
    stderr after the step's summary line, enabling in-terminal failure diagnosis
    without re-running individual watchers (implementation change).
    """
    prefix = f"[{index}/{total}]"
    label_str = f"{label:<22}"

    if dry_run:
        public_cmd = _public_argv(cmd)
        click.echo(f"  {prefix} {label_str}  (dry-run) {' '.join(public_cmd)}", err=True)
        return StepResult(
            index=index,
            total=total,
            label=label,
            cmd=public_cmd,
            success=True,
            elapsed=0.0,
            note="dry-run",
            exit_code=EXIT_SUCCESS,
        )

    execution = _execute_step(index, total, label, cmd)
    _render_step(execution, verbose=verbose)
    return execution.result


def _run_people_index_step(
    index: int,
    total: int,
    account: str | None,
    *,
    dry_run: bool,
) -> StepResult:
    """Rebuild the people-index cache in-process (no subprocess)."""
    prefix = f"[{index}/{total}]"
    label_str = f"{PEOPLE_INDEX_LABEL:<22}"

    if dry_run:
        click.echo(f"  {prefix} {label_str}  (dry-run) build_people_index(account={account!r})", err=True)
        return StepResult(
            index=index,
            total=total,
            label=PEOPLE_INDEX_LABEL,
            cmd=[],
            success=True,
            elapsed=0.0,
            note="dry-run",
            exit_code=EXIT_SUCCESS,
        )

    from fieldkit.contact.people_index import build_people_index
    from fieldkit.errors import GmailSyncPartialError, SQLiteSnapshotError
    from fieldkit.gmail.discover import get_gmail_db_path

    t0 = time.monotonic()
    try:
        build_people_index(get_gmail_db_path(), account_filter=account, show_progress=False)
        elapsed = time.monotonic() - t0
        click.echo(f"  {prefix} {label_str}  {'✓':<1}  {'':<60}  ({elapsed:.1f}s)", err=True)
        return StepResult(
            index=index,
            total=total,
            label=PEOPLE_INDEX_LABEL,
            cmd=[],
            success=True,
            elapsed=elapsed,
            note="",
            exit_code=EXIT_SUCCESS,
        )
    except GmailSyncPartialError:
        elapsed = time.monotonic() - t0
        note = "Gmail cache is not ready"
        click.echo(f"  {prefix} {label_str}  {'✗':<1}  {note:<60}  ({elapsed:.1f}s)", err=True)
        return StepResult(
            index=index,
            total=total,
            label=PEOPLE_INDEX_LABEL,
            cmd=[],
            success=False,
            elapsed=elapsed,
            note=note,
            exit_code=EXIT_PARTIAL,
        )
    except SQLiteSnapshotError as exc:
        if exc.reason != "active":
            raise
        elapsed = time.monotonic() - t0
        note = "Gmail cache is active"
        click.echo(f"  {prefix} {label_str}  {'✗':<1}  {note:<60}  ({elapsed:.1f}s)", err=True)
        return StepResult(
            index=index,
            total=total,
            label=PEOPLE_INDEX_LABEL,
            cmd=[],
            success=False,
            elapsed=elapsed,
            note=note,
            exit_code=EXIT_PARTIAL,
        )
    except AuthError:
        raise AuthError("People index authentication is required") from None
    except ConfigError:
        raise ConfigError("People index configuration is invalid") from None
    except ValueError:
        raise ConfigError("People index data is invalid") from None
    except RuntimeError:
        raise RuntimeError("People index rebuild failed") from None


def _show_concurrent_progress(result: StepResult, *, as_json: bool) -> None:
    """Render one ordered watcher completion line."""
    if not as_json:
        status = "ok" if result.success else "FAILED"
        click.echo(f"  [{result.index}/{result.total}] {result.label:<22}  → {status} ({result.elapsed:.1f}s)")


def _collect_watcher(
    future: Future[_StepExecution], index: int, total: int, label: str, cmd: list[str]
) -> _StepExecution:
    """Translate one worker exception into the phase's partial-result contract."""
    try:
        return future.result()
    except AuthError:
        raise
    except Exception as exc:  # noqa: BLE001 -- sibling watcher failures must remain isolated
        note = _PROCESS_START_FAILURE_NOTE if isinstance(exc, OSError) else "Watcher execution failed"
        result = StepResult(index, total, label, _public_argv(cmd), False, 0.0, note, EXIT_PARTIAL)
        return _StepExecution(result, display=note)


def _run_concurrent_watchers(
    steps: list[tuple[str, list[str]]], *, start_index: int, total: int, verbose: bool, as_json: bool
) -> list[StepResult]:
    """Execute the independent watcher phase and render it in source order."""
    indexed = [(start_index + offset, label, cmd) for offset, (label, cmd) in enumerate(steps)]
    if not as_json:
        for index, label, _ in indexed:
            click.echo(f"  [{index}/{total}] {label:<22}  → running...")
    with ThreadPoolExecutor(max_workers=len(indexed), thread_name_prefix="fieldkit-sync-watch") as executor:
        futures = [executor.submit(_execute_step, index, total, label, cmd) for index, label, cmd in indexed]
        executions = [
            _collect_watcher(future, index, total, label, cmd)
            for future, (index, label, cmd) in zip(futures, indexed, strict=True)
        ]
    for execution in executions:
        _render_step(execution, verbose=verbose)
        _show_concurrent_progress(execution.result, as_json=as_json)
    return [execution.result for execution in executions]


def _run_pipeline_steps(cfg: RunConfig, steps: list[tuple[str, list[str]]]) -> list[StepResult]:
    """Run sequential phases around the concurrent watcher phase."""
    total = len(steps)
    results: list[StepResult] = []
    offset = 0
    while offset < total:
        label, cmd = steps[offset]
        idx = offset + 1
        if not cfg.dry_run and label in _WATCHER_NAMES:
            watcher_steps: list[tuple[str, list[str]]] = []
            for candidate in steps[offset:]:
                if candidate[0] not in _WATCHER_NAMES:
                    break
                watcher_steps.append(candidate)
            if len(watcher_steps) > 1:
                concurrent_results = _run_concurrent_watchers(
                    watcher_steps, start_index=idx, total=total, verbose=cfg.verbose, as_json=cfg.as_json
                )
                results.extend(concurrent_results)
                offset += len(concurrent_results)
                continue
        # Print "running..." before blocking so users know which step is active
        if not cfg.dry_run and not cfg.as_json:
            click.echo(f"  [{idx}/{total}] {label:<22}  → running...", nl=False)
        if label == PEOPLE_INDEX_LABEL:
            result = _run_people_index_step(idx, total, cfg.account, dry_run=cfg.dry_run)
        else:
            result = _run_step(idx, total, label, cmd, dry_run=cfg.dry_run, verbose=cfg.verbose)
        if not cfg.dry_run and not cfg.as_json:
            status = "ok" if result.success else "FAILED"
            click.echo(f"\r  [{idx}/{total}] {label:<22}  → {status} ({result.elapsed:.1f}s)")
        results.append(result)
        offset += 1
    return results


def _render_pipeline_start(cfg: RunConfig) -> None:
    """Render the human-readable pipeline banner when stdout is available."""
    if cfg.as_json:
        return
    date_str = datetime.now(tz=UTC).date().isoformat()
    mode_tags = []
    if cfg.quick:
        mode_tags.append("--quick")
    if cfg.sf:
        mode_tags.append("--sf")
    if cfg.slack:
        mode_tags.append("--slack")
    if cfg.dry_run:
        mode_tags.append("--dry-run")
    mode_str = "  " + " ".join(mode_tags) if mode_tags else ""
    # historic regression/implementation change: banner goes to stdout (visible regardless of stderr redirect)
    click.echo(f"\nfieldkit sync — {date_str}{mode_str}\n")
    for reason in cfg.skipped:
        click.echo(f"  not run: {reason}")


def _run_all_steps(cfg: RunConfig) -> list[StepResult]:
    """Execute every configured pipeline step and return its outcomes."""
    return _run_pipeline_steps(cfg, _build_steps(cfg))


def _render_pipeline_summary(cfg: RunConfig, results: list[StepResult]) -> None:
    """Render the human-readable result summary after every pipeline step."""

    failed = [r for r in results if not r.success]
    total_elapsed = sum(r.elapsed for r in results)
    click.echo("", err=True)
    if not cfg.dry_run:
        if failed:
            click.echo(
                f"  {len(failed)} step(s) failed — {len(results) - len(failed)}/{len(results)} succeeded  "
                f"({total_elapsed:.1f}s total)",
                err=True,
            )
            for r in failed:
                click.echo(f"    ✗ {r.label}: {r.note}", err=True)
        else:
            click.echo(
                f"  All {len(results)} steps succeeded  ({total_elapsed:.1f}s total)",
                err=True,
            )
        click.echo("  Run 'fieldkit brief generate' to see the morning brief.", err=True)


def run_pipeline(cfg: RunConfig) -> list[StepResult]:
    """Execute the full pipeline and return per-step results."""
    _render_pipeline_start(cfg)
    results = _run_all_steps(cfg)
    _render_pipeline_summary(cfg, results)
    return results


def _pipeline_exit_code(results: list[StepResult]) -> int:
    """Return the strongest canonical outcome observed across all steps."""
    codes = [result.exit_code for result in results]
    if any(not result.success for result in results) and not any(codes):
        return EXIT_PARTIAL
    return max(codes, default=EXIT_SUCCESS)


@declare_write("workspace")
@click.command(
    name="sync",
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.option(
    "--quick",
    is_flag=True,
    default=False,
    help="Skip gmail sync phase (use cached data); run ingest + watchers only.",
)
@click.option(
    "--sf",
    is_flag=True,
    default=False,
    help="Include Salesforce listview sync (weekly mode).",
)
@click.option(
    "--slack",
    is_flag=True,
    default=False,
    help="Include the optional Slack thread watcher.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would run without executing any steps.",
)
@click.option(
    "--account",
    "-a",
    default=None,
    metavar="NAME",
    help="Scope gmail and watcher steps to one account.",
)
@click.option(
    "--verbose",
    is_flag=True,
    default=False,
    help=(
        f"Print up to the last {MAX_VERBOSE_LINES} lines and {MAX_VERBOSE_CHARS:,} characters of "
        "each subprocess stdout/stderr stream after each step's summary line. "
        "Useful for diagnosing failures without re-running individual watchers."
    ),
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the per-step results as JSON.")
def cli(quick: bool, sf: bool, slack: bool, dry_run: bool, account: str | None, verbose: bool, as_json: bool) -> None:
    """Run the full fieldkit data pipeline in the correct order.

    Executes selected gmail, ingest, and watcher workflows. Ordinary step
    failures are non-fatal — the pipeline continues and prints a summary.

    Use --verbose to show a bounded tail of each subprocess stream after its
    summary line, enabling in-terminal failure diagnosis without re-running
    individual watchers or flooding the terminal.

    Exit codes:
      0 — all steps succeeded (or --dry-run)
      1 — one or more steps failed
      2 — a step requires authentication or user action
      3 — a step rejected invalid data or usage
    """
    from fieldkit.commands._account_guard import validate_account_slug
    from fieldkit.watch.integration_plan import build_integration_plan
    from fieldkit.watch.preflight import preflight_check

    validate_account_slug(account)
    try:
        plan = build_integration_plan(
            google_when_configured=not quick,
            sf_requested=sf,
            backstory_when_configured=True,
            draft_queue_when_configured=False,
            calendar_when_configured=False,
            slack_requested=slack,
            llm_when_configured=False,
        )
    except ConfigError:
        click.echo("Config error: selected integration configuration is invalid", err=True)
        raise SystemExit(EXIT_DATA) from None

    # A preview resolves only local configuration and never reads a credential
    # file or contacts a selected service.
    failures = [] if dry_run else preflight_check(list(plan.preflight_services))
    if failures:
        raise AuthError("Selected sync integration requires authentication or user setup")

    cfg = RunConfig(
        quick=quick,
        sf=sf,
        google="gmail" in plan.preflight_services,
        backstory="backstory-health" in plan.optional_watchers,
        slack="slack-threads" in plan.optional_watchers,
        dry_run=dry_run,
        account=account,
        verbose=verbose,
        as_json=as_json,
        skipped=plan.skipped,
    )
    results = run_pipeline(cfg)
    failed = [r for r in results if not r.success]

    if as_json:
        # The pipeline ran — emit the per-step detail even when steps failed,
        # which is exactly when a caller needs it. Exit code is unchanged.
        click.echo(
            json.dumps(
                {
                    "items": [asdict(r) for r in results],
                    "count": len(results),
                    "succeeded": len(results) - len(failed),
                    "failed": len(failed),
                    "elapsed": sum(r.elapsed for r in results),
                    "filters": {
                        "quick": quick,
                        "sf": sf,
                        "slack": slack,
                        "account": account,
                        "dry_run": dry_run,
                    },
                    "not_run": list(plan.skipped),
                },
                indent=2,
                default=str,
            )
        )

    raise SystemExit(_pipeline_exit_code(results))
