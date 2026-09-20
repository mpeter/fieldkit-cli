"""fieldkit sync — Ordered full data pipeline runner.

Runs the full fieldkit data pipeline in the correct dependency order,
reports per-step status, and is safe to run at any time (all steps are
idempotent).

Default pipeline (9 steps):
  Phase 1 — Gmail (skipped with --quick)
    [1] fieldkit gmail sync
    [2] people-index rebuild (in-process — fieldkit.contact.people_index.build_people_index())
    [3] fieldkit gmail account-tags
    [4] fieldkit gmail enrich-pursuits

  Phase 2 — Ingest (always runs)
    [5] fieldkit ingest discover
    [6] fieldkit ingest run

  Phase 3 — Watchers (always runs)
    [7] fieldkit watch run backstory-health
    [8] fieldkit watch run pursuit-stalls
    [9] fieldkit watch run slack-threads

  Phase 4 — Salesforce (only with --sf)
    [+1] fieldkit sf listview

The people-index rebuild is the sole call site that refreshes the contact
cache — ``fieldkit contact list`` is a pure read and never rebuilds it.

Step failures are non-fatal: the pipeline continues and failures are
reported in a summary at the end. Exit code 0 if all steps succeed,
1 if any step failed.
"""

import json
import subprocess
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass

import click

from fieldkit.cli_exit import EXIT_PARTIAL
from fieldkit.cli_registry import declare_write
from fieldkit.config import TIMEOUT_DATASYNC

LOG_PREFIX = "[sync]"

#: Sentinel label for the in-process people-index rebuild step (empty argv in
#: ``_build_steps`` — routed to ``_run_people_index_step`` by ``run_pipeline``).
PEOPLE_INDEX_LABEL = "people-index"

# Maximum number of lines to print per stream in --verbose mode.  Prevents a
# runaway subprocess from flooding the terminal with megabytes of output.
MAX_VERBOSE_LINES = 100

_CONCURRENT_WATCHERS = frozenset({"backstory-health", "pursuit-stalls", "slack-threads"})


def _truncate_output(text: str, max_lines: int = MAX_VERBOSE_LINES) -> str:
    """Return *text* truncated to at most *max_lines* lines from the tail.

    When truncation occurs, a summary line is appended so the user knows how
    many lines were dropped.  Tail lines are kept (not head) because the most
    recent output is typically the most diagnostic.

    Args:
        text: Raw subprocess output string.
        max_lines: Maximum number of lines to retain.

    Returns:
        The (possibly truncated) string.
    """
    lines = text.splitlines()
    if len(lines) > max_lines:
        dropped = len(lines) - max_lines
        return (
            "\n".join(lines[-max_lines:])
            + f"\n[... truncated — showing last {max_lines} of {len(lines)} lines ({dropped} dropped)]"
        )
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
    dry_run: bool = False
    account: str | None = None
    verbose: bool = False
    #: Suppress the stdout banner and per-step progress lines so the CLI can
    #: own stdout with a single JSON document.
    as_json: bool = False


def _build_steps(cfg: RunConfig) -> list[tuple[str, list[str]]]:
    """Return ordered list of (label, argv) for the configured run.

    historic regression: All step commands use ``[sys.executable, "-m", "fieldkit", ...]``
    instead of ``["fieldkit", ...]``. This guarantees same-interpreter,
    same-source execution — the PATH-resolved ``fieldkit`` binary may be stale
    if ``make install`` was not run after a source change.

    The ``people-index`` step has an empty argv — it is not a subprocess call.
    ``run_pipeline`` recognises the ``PEOPLE_INDEX_LABEL`` and routes it to
    ``_run_people_index_step``, which calls
    ``fieldkit.contact.people_index.build_people_index()`` in-process. This is
    the sole call site that rebuilds the people cache — ``contact list`` never
    triggers a rebuild (decision: D1 Wave 3 PR2).
    """
    # historic regression: Use sys.executable + "-m" + "fieldkit" to avoid PATH version skew.
    _fk = [sys.executable, "-m", "fieldkit"]
    steps: list[tuple[str, list[str]]] = []

    if not cfg.quick:
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

    backstory_cmd = [*_fk, "watch", "run", "backstory-health"]
    stalls_cmd = [*_fk, "watch", "run", "pursuit-stalls"]
    slack_cmd = [*_fk, "watch", "run", "slack-threads"]
    if cfg.account:
        backstory_cmd += ["--account", cfg.account]
        stalls_cmd += ["--account", cfg.account]
        # implementation change: slack-threads supports --account; scope it consistently with
        # backstory-health and pursuit-stalls when --account is provided.
        slack_cmd += ["--account", cfg.account]
    steps += [
        ("backstory-health", backstory_cmd),
        ("pursuit-stalls", stalls_cmd),
        ("slack-threads", slack_cmd),
    ]

    if cfg.sf:
        sf_cmd = [*_fk, "sf", "listview"]  # no TARGET = sync all accounts
        steps.append(("sf listview", sf_cmd))

    return steps


def _step_note(returncode: int, stderr: str, stdout: str) -> str:
    """Return the first useful subprocess line, preserving the exit fallback."""
    note = f"exit {returncode}" if returncode else ""
    for stream in (stderr, stdout):
        for raw_line in stream.splitlines():
            stripped = raw_line.strip()
            if stripped and not stripped.startswith("["):
                return stripped[:80]
    return note


def _execute_step(index: int, total: int, label: str, cmd: list[str]) -> _StepExecution:
    """Execute one real subprocess step without writing terminal output."""
    t0 = time.monotonic()
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_DATASYNC,
            check=False,
        )
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - t0
        result = StepResult(index, total, label, cmd, False, elapsed, "timeout")
        return _StepExecution(result, display=f"TIMEOUT after {elapsed:.0f}s")
    except FileNotFoundError:
        result = StepResult(index, total, label, cmd, False, 0.0, f"command not found: {cmd[0]}")
        return _StepExecution(result, display=result.note)

    elapsed = time.monotonic() - t0
    result = StepResult(
        index=index,
        total=total,
        label=label,
        cmd=cmd,
        success=completed.returncode == 0,
        elapsed=elapsed,
        note=_step_note(completed.returncode, completed.stderr or "", completed.stdout or ""),
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
        click.echo(f"  {prefix} {label_str}  (dry-run) {' '.join(cmd)}", err=True)
        return StepResult(
            index=index,
            total=total,
            label=label,
            cmd=cmd,
            success=True,
            elapsed=0.0,
            note="dry-run",
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
    """Rebuild the people-index cache in-process (no subprocess).

    This is the sole call site that rebuilds ``fieldkit.contact.people_index`` —
    ``fieldkit contact list`` only ever reads the existing cache.
    """
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
        )

    import sqlite3

    from fieldkit.contact.people_index import build_people_index
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
        )
    except sqlite3.OperationalError as exc:
        # No messages table yet (gmail sync hasn't populated the cache) — not a
        # failure, just nothing to index yet.
        if "no such table" in str(exc):
            elapsed = time.monotonic() - t0
            note = "no gmail data yet"
            click.echo(f"  {prefix} {label_str}  {'✓':<1}  {note:<60}  ({elapsed:.1f}s)", err=True)
            return StepResult(
                index=index,
                total=total,
                label=PEOPLE_INDEX_LABEL,
                cmd=[],
                success=True,
                elapsed=elapsed,
                note=note,
            )
        elapsed = time.monotonic() - t0
        note = str(exc)[:80]
        click.echo(f"  {prefix} {label_str}  {'✗':<1}  {note:<60}  ({elapsed:.1f}s)", err=True)
        return StepResult(
            index=index,
            total=total,
            label=PEOPLE_INDEX_LABEL,
            cmd=[],
            success=False,
            elapsed=elapsed,
            note=note,
        )
    except Exception as exc:  # noqa: BLE001  — pipeline step failures are non-fatal, reported in summary
        elapsed = time.monotonic() - t0
        note = str(exc)[:80]
        click.echo(f"  {prefix} {label_str}  {'✗':<1}  {note:<60}  ({elapsed:.1f}s)", err=True)
        return StepResult(
            index=index,
            total=total,
            label=PEOPLE_INDEX_LABEL,
            cmd=[],
            success=False,
            elapsed=elapsed,
            note=note,
        )


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
    except Exception as exc:  # noqa: BLE001 -- sibling watcher failures must remain isolated
        note = f"{type(exc).__name__}: {exc}"[:80]
        result = StepResult(index, total, label, cmd, False, 0.0, note)
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
        if not cfg.dry_run and label == "backstory-health":
            watcher_steps = steps[offset : offset + len(_CONCURRENT_WATCHERS)]
            if {watcher_label for watcher_label, _ in watcher_steps} == _CONCURRENT_WATCHERS:
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
    from datetime import date

    date_str = date.today().isoformat()
    mode_tags = []
    if cfg.quick:
        mode_tags.append("--quick")
    if cfg.sf:
        mode_tags.append("--sf")
    if cfg.dry_run:
        mode_tags.append("--dry-run")
    mode_str = "  " + " ".join(mode_tags) if mode_tags else ""
    # historic regression/implementation change: banner goes to stdout (visible regardless of stderr redirect)
    click.echo(f"\nfieldkit sync — {date_str}{mode_str}\n")


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
        f"Print the last {MAX_VERBOSE_LINES} lines of subprocess stdout/stderr after each step's summary line. "
        "Useful for diagnosing failures without re-running individual watchers."
    ),
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the per-step results as JSON.")
def cli(quick: bool, sf: bool, dry_run: bool, account: str | None, verbose: bool, as_json: bool) -> None:
    """Run the full fieldkit data pipeline in the correct order.

    Executes all data refresh steps idempotently: gmail sync, ingest,
    watchers. Step failures are non-fatal — the pipeline continues and
    a summary is printed at the end.

    Use --verbose to show the last 100 lines of each subprocess stream after
    its summary line, enabling in-terminal failure diagnosis without re-running
    individual watchers or flooding the terminal.

    Exit codes:
      0 — all steps succeeded (or --dry-run)
      1 — one or more steps failed
    """
    from fieldkit.watch.preflight import preflight_check

    # implementation note: fast-fail before starting the heavy pipeline.
    # datasync requires SF and Gmail; dry-run still checks these.
    failures = preflight_check(["sf", "gmail"])
    if failures:
        for msg in failures:
            click.echo(f"Pre-flight check failed: {msg}", err=True)
        raise SystemExit(EXIT_PARTIAL)

    cfg = RunConfig(quick=quick, sf=sf, dry_run=dry_run, account=account, verbose=verbose, as_json=as_json)
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
                    "filters": {"quick": quick, "sf": sf, "account": account, "dry_run": dry_run},
                },
                indent=2,
                default=str,
            )
        )

    raise SystemExit(1 if failed else 0)
