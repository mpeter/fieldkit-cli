"""fieldkit.health.runner — orchestrates one nightly health run.

Flow: fetch ``origin/main`` → ephemeral detached worktree → run every gate in
:data:`~fieldkit.health.checks.HEALTH_CHECKS` order → file new regressions
through the injected filer → record the run in
``<fieldkit_data>/logs/health/health-run-status.json`` (driver-style append log,
capped at 100 entries).

Outcome vocabulary (design Decision 5, historic regression discipline):

- ``ok``      — every check *executed*; gate failures are sensed regressions,
                not runner failures, so a run that files issues is still ``ok``.
- ``partial`` — some checks executed but some invocations errored, or filing
                failed after sensing. Retry may help.
- ``fatal``   — the runner could not sense at all (fetch/worktree failure, or
                zero checks executed). Never silent: a crash writes a ``fatal``
                entry before propagating.
"""

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Literal

from fieldkit.config import TIMEOUT_HEALTH_GATE, TIMEOUT_HEALTH_GIT, get_fieldkit_data, get_harness_scratch_root
from fieldkit.health.checks import HEALTH_CHECKS, CheckResult, run_check
from fieldkit.health.filing import FilingOutcome, IssueFiler, file_regressions
from fieldkit.util.atomic import locked_json_update

log = logging.getLogger(__name__)

#: Cap on retained run-status entries (mirrors the driver's run log).
_STATUS_LOG_CAP = 100

HealthOutcome = Literal["ok", "partial", "fatal"]


@dataclass(frozen=True)
class HealthRunResult:
    """Typed summary of one nightly health run (no ``dict[str, Any]`` crosses modules)."""

    outcome: HealthOutcome
    checks_run: int
    gate_failures: tuple[str, ...]
    runner_errors: tuple[str, ...]
    issues_filed: tuple[str, ...]
    issues_deduped: tuple[str, ...]
    elapsed_seconds: float
    error: str = ""


def _worktrees_root() -> Path:
    # Harness-scratch root (cache-class), not get_fieldkit_data(): an ephemeral
    # checkout of the code repo is not a fieldkit runtime artifact
    # (historic regression / ADR-0005 scope note). Shared resolver — must not fork.
    return get_harness_scratch_root() / "health" / "worktrees"


def _cleanup_stale_worktrees(repo_root: Path) -> None:
    """Remove leftover worktrees from prior crashed/killed runs. Best-effort.

    A SIGKILL mid-run (unit timeout, OOM) leaves the timestamped worktree —
    including its ``.venv`` — on disk. Without this sweep, nightly crashes
    accumulate gigabytes under ``<fieldkit_data>/health/worktrees/``.
    """
    root = _worktrees_root()
    if not root.is_dir():
        return
    for leftover in sorted(root.iterdir()):
        if not leftover.is_dir():
            continue
        log.info("health: removing stale worktree %s", leftover)
        remove_main_worktree(leftover, repo_root)
        if leftover.exists():
            shutil.rmtree(leftover, ignore_errors=True)


def prepare_main_worktree(repo_root: Path) -> Path | None:
    """Create a detached ephemeral worktree of ``origin/main`` to sense against.

    Fetches first so the base ref is current; the run must evaluate committed
    ``main``, never the shared checkout's dirty tree (spec scenario). Stale
    worktrees from prior killed runs are swept first.

    Returns:
        The worktree path, or ``None`` on failure (→ ``fatal``).
    """
    try:
        _cleanup_stale_worktrees(repo_root)
    except OSError as exc:
        log.warning("health: stale-worktree sweep failed: %s", exc)
    try:
        subprocess.run(
            ["git", "fetch", "origin", "main"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=TIMEOUT_HEALTH_GIT,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        log.error("health: fetch of origin/main failed: %s", stderr.strip() or exc)
        return None

    ts = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
    worktree_path = _worktrees_root() / f"health-{ts}"
    try:
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", "--force", "--detach", str(worktree_path), "origin/main"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=TIMEOUT_HEALTH_GIT,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        log.error("health: worktree add failed at %s: %s", worktree_path, stderr.strip() or exc)
        remove_main_worktree(worktree_path, repo_root)
        return None
    log.info("health: sensing origin/main in %s", worktree_path)
    return worktree_path


def remove_main_worktree(worktree_path: Path, repo_root: Path) -> None:
    """Tear down the ephemeral worktree. Best-effort; never raises."""
    for argv in (
        ["git", "worktree", "remove", "--force", str(worktree_path)],
        ["git", "worktree", "prune"],
    ):
        try:
            subprocess.run(
                argv,
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
                timeout=TIMEOUT_HEALTH_GIT,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            log.warning("health: %s failed: %s", " ".join(argv[:3]), exc)


def write_health_run_status(result: HealthRunResult, *, dry_run: bool) -> None:
    """Append *result* to ``<fieldkit_data>/logs/health/health-run-status.json``.

    Dry-run results are not persisted (watcher-pattern convention). A
    filesystem error is logged, never raised — the status write must not mask
    the run outcome it is recording.
    """
    if dry_run:
        return
    try:
        logs_dir = get_fieldkit_data() / "logs" / "health"
        logs_dir.mkdir(parents=True, exist_ok=True)
        status_file = logs_dir / "health-run-status.json"
        entry = {
            "ts": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "outcome": result.outcome,
            "checks_run": result.checks_run,
            "gate_failures": list(result.gate_failures),
            "runner_errors": list(result.runner_errors),
            "issues_filed": list(result.issues_filed),
            "issues_deduped": list(result.issues_deduped),
            "elapsed_seconds": round(result.elapsed_seconds, 1),
            "error": result.error,
        }
        with locked_json_update(status_file) as existing:
            entries: list[dict[str, Any]] = list(existing.get("runs", []))
            entries.append(entry)
            if len(entries) > _STATUS_LOG_CAP:
                del entries[: len(entries) - _STATUS_LOG_CAP]
            existing.clear()
            existing["runs"] = entries
    except OSError as exc:
        log.warning("health: could not write health-run-status.json: %s", exc)


def _check_env(worktree_path: Path) -> dict[str, str]:
    """Return a worktree-local environment so checks cannot read live fieldkit state."""
    home_dir = worktree_path / ".fieldkit-home"
    temp_dir = worktree_path / ".tmp"
    home_dir.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["HOME"] = str(home_dir)
    env["TMPDIR"] = str(temp_dir)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.pop("FIELDKIT_DATA_DIR", None)
    env.pop("VIRTUAL_ENV", None)
    env.pop("UV_PROJECT_ENVIRONMENT", None)
    return env


def _bootstrap_check_environment(worktree_path: Path, env: dict[str, str]) -> str | None:
    """Create the locked all-extras environment before running individual gates."""
    try:
        proc = subprocess.run(
            ["uv", "sync", "--frozen", "--all-extras", "--dev"],
            cwd=worktree_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=TIMEOUT_HEALTH_GATE,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"environment bootstrap could not run: {exc}"
    if proc.returncode == 0:
        return None
    output = (proc.stderr or proc.stdout).strip()
    return f"environment bootstrap failed (exit {proc.returncode}): {output or 'no output'}"


def _run_checks_in_worktree(worktree_path: Path) -> tuple[list[CheckResult], str | None]:
    """Bootstrap and run health checks in the isolated worktree environment."""
    env = _check_env(worktree_path)
    bootstrap_error = _bootstrap_check_environment(worktree_path, env)
    if bootstrap_error is not None:
        return [], bootstrap_error
    env["UV_NO_SYNC"] = "1"
    return [run_check(check, cwd=worktree_path, env=env) for check in HEALTH_CHECKS], None


def _fatal_health_result(started: float, runner_error: str, *, error: str | None = None) -> HealthRunResult:
    """Build the consistent result used when health cannot execute a check."""
    return HealthRunResult(
        outcome="fatal",
        checks_run=0,
        gate_failures=(),
        runner_errors=(runner_error,),
        issues_filed=(),
        issues_deduped=(),
        elapsed_seconds=monotonic() - started,
        error=runner_error if error is None else error,
    )


def _execute_health_run(repo_root: Path, filer: IssueFiler, *, dry_run: bool, started: float) -> HealthRunResult:
    """Sense checks and file regressions, without persisting a run-status entry."""
    worktree = prepare_main_worktree(repo_root)
    if worktree is None:
        return _fatal_health_result(started, "could not prepare origin/main worktree")

    try:
        results, bootstrap_error = _run_checks_in_worktree(worktree)
    finally:
        remove_main_worktree(worktree, repo_root)

    if bootstrap_error is not None:
        return _fatal_health_result(started, bootstrap_error)

    executed = [r for r in results if r.status != "skipped"]
    gate_failures = tuple(r.check_id for r in results if r.status == "fail")
    runner_errors = [
        f"{r.check_id}: {r.output.strip().splitlines()[-1] if r.output.strip() else 'error'}"
        for r in results
        if r.status == "error"
    ]

    filing = FilingOutcome(filed=(), deduped=())
    filing_error = ""
    try:
        filing = file_regressions(results, filer, dry_run=dry_run)
    except RuntimeError as exc:
        # Sensing succeeded but filing did not — partial, loudly recorded.
        filing_error = f"filing failed: {exc}"
        runner_errors.append(filing_error)
        log.error("health: %s", filing_error)

    if not executed:
        outcome: HealthOutcome = "fatal"
    elif runner_errors:
        outcome = "partial"
    else:
        outcome = "ok"

    return HealthRunResult(
        outcome=outcome,
        checks_run=len(executed),
        gate_failures=gate_failures,
        runner_errors=tuple(runner_errors),
        issues_filed=filing.filed,
        issues_deduped=filing.deduped,
        elapsed_seconds=monotonic() - started,
        error=filing_error,
    )


def run_health(repo_root: Path, filer: IssueFiler, *, dry_run: bool = False) -> HealthRunResult:
    """Execute one full health run: sense → dedup → file → record.

    The returned result is persisted on every non-dry-run path, including a
    runner crash, before that crash is allowed to propagate to the CLI.
    """
    started = monotonic()
    try:
        result = _execute_health_run(repo_root, filer, dry_run=dry_run, started=started)
    except Exception as exc:
        # Runner self-failure must be recorded, then stay loud (spec: fatal,
        # never silently swallowed). cli_main() maps the re-raise to an exit code.
        crash = _fatal_health_result(started, f"runner crash: {exc}", error=str(exc))
        write_health_run_status(crash, dry_run=dry_run)
        raise
    write_health_run_status(result, dry_run=dry_run)
    return result
