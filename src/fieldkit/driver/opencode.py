"""fieldkit.driver.opencode — headless OpenCode session execution for the driver.

Extracted from :mod:`fieldkit.driver.runner` so the orchestration loop (issue
selection, worktree lifecycle, label transitions) stays separable from the
concern of running one headless agent session and classifying its outcome.

The single entry point is :func:`run_opencode`; :class:`OpencodeOutcome` is the
typed result the runner threads into the failure comment and run-status record.
"""

import contextlib
import importlib.resources
import logging
import os
import re
import selectors
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, BinaryIO, Literal

from fieldkit.config import (
    TIMEOUT_GH_CLI,
    TIMEOUT_PROCESS_KILL_GRACE,
    TIMEOUT_RATE_LIMIT_POLL,
    get_fieldkit_data,
)
from fieldkit.driver.retry_state import RetryReceipt
from fieldkit.util.bounded_process import (
    BoundedProcessError,
    process_exited_unreaped,
    require_unreaped_exit_observation,
    run_bounded_process,
    terminate_process_group,
)

log = logging.getLogger(__name__)

# Subprocess timeouts (seconds).
_GIT_TIMEOUT: int = 30  # fast local git operations (branch-push check)
_OPENCODE_TIMEOUT: int = 3600  # headless OpenCode agent session (1 h ceiling)

# Sustained-rate-limit detection: a provider quota rejection is infrastructure
# starvation, not a work-order defect, so a run pinned to it should abort well
# before the 1h ceiling instead of burning the full timeout for nothing (and
# the driver must not count it as a failed attempt — see the "rate_limited"
# OpencodeOutcome status).
#
# "Sustained" is measured as *new* rejections arriving in consecutive poll
# intervals, not as a cumulative count. A cumulative threshold cannot express
# sustained-ness: the retry backoff is 2/4/8/16s, so three rejections can land
# inside the first poll and kill a session that would have recovered on the
# next attempt. Requiring consecutive intervals makes the abort span at least
# _RATE_LIMIT_SUSTAINED_POLLS x TIMEOUT_RATE_LIMIT_POLL seconds of wall clock,
# and makes the burst-kills-a-recovering-run bug unrepresentable.
# These are provider/API error envelopes, not the broad phrase "rate limit": the
# latter can appear in a work order, source file, or tool output without being an
# actual rejected inference request. One log line counts once even when an error
# serializes both a human message and a machine error code.
_RATE_LIMIT_MARKERS = (
    "claude max rate limit reached",
    "rate limit reached",
    "rate_limit_exceeded",
    "too many requests",
    "rate increased too quickly",
)
_RATE_LIMIT_SUSTAINED_POLLS = 2  # consecutive polls that must each show new rejections

# Newest N per-run log pairs to retain under logs/driver/. One .out/.err pair is
# written per run and never overwritten, so without pruning the shared
# fieldkit-data partition grows without bound (a fill breaks both the driver and
# live sessions). Mirrors the 100-entry cap on driver-run-status.json.
_OPENCODE_LOG_KEEP_PAIRS = 50
_OPENCODE_STREAM_BYTES = 8 * 1024 * 1024
_EXECUTOR_PROMPT = "driver-executor.md"

_OPENCODE_ENV_ALLOWLIST = frozenset(
    {
        "COLORTERM",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "HOME",
        "LANG",
        "LC_ALL",
        "NO_COLOR",
        "OPENAI_API_KEY",
        "OPENCODE_CONFIG",
        "OPENCODE_CONFIG_DIR",
        "PATH",
        "SSH_AUTH_SOCK",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TERM",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    }
)


class OpencodeOutputOverflow(RuntimeError):
    """OpenCode emitted more durable output than the configured audit bound."""


def _branch_pushed(branch: str, repo_root: Path) -> bool:
    """Return True if *branch* has been pushed to origin."""
    try:
        result = run_bounded_process(
            ["git", "ls-remote", "--heads", "origin", branch],
            cwd=repo_root,
            timeout=_GIT_TIMEOUT,
            stdout_limit=_OPENCODE_STREAM_BYTES,
            stderr_limit=_OPENCODE_STREAM_BYTES,
            cleanup_timeout=TIMEOUT_PROCESS_KILL_GRACE,
        )
        if result.returncode != 0:
            return False
        expected_ref = f"refs/heads/{branch}"
        lines = result.stdout.splitlines()
        return (
            len(lines) == 1
            and re.fullmatch(
                rf"[0-9a-fA-F]{{40}}(?:[0-9a-fA-F]{{24}})?\t{re.escape(expected_ref)}",
                lines[0],
            )
            is not None
        )
    except BoundedProcessError:
        log.warning("Could not verify branch push")
        return False


def _pr_exists(branch: str, repo_root: Path) -> bool:
    """Return True if an open PR exists on origin for *branch*."""
    try:
        result = run_bounded_process(
            ["gh", "pr", "list", "--head", branch, "--json", "number", "--jq", "length"],
            cwd=repo_root,
            timeout=TIMEOUT_GH_CLI,
            stdout_limit=_OPENCODE_STREAM_BYTES,
            stderr_limit=_OPENCODE_STREAM_BYTES,
            cleanup_timeout=TIMEOUT_PROCESS_KILL_GRACE,
        )
        if result.returncode != 0:
            return False
        try:
            return int(result.stdout.strip()) > 0
        except ValueError:
            return False
    except BoundedProcessError:
        log.warning("Could not verify pull-request existence")
        return False


OpencodeStatus = Literal["ok", "failed", "rate_limited"]


@dataclass(frozen=True)
class OpencodeOutcome:
    """Result of a headless OpenCode run: whether it succeeded and, if not, why.

    ``reason`` distinguishes the failure modes the driver previously collapsed
    into one opaque ``"exited non-zero or branch not pushed"`` string — a 1-hour
    timeout, a non-zero exit, and an exit-0 run that never pushed are now
    separable in the failure comment and the run-status record. A fleet-wide
    hang (as on 2026-07-21) is no longer indistinguishable from a real defect.

    ``status`` is a single discriminant rather than independent booleans, so
    the impossible "succeeded but rate-limited" state cannot be constructed.
    It mirrors :data:`fieldkit.driver.status_types.RunOutcome`, which already models
    run results this way.

    ``"rate_limited"`` marks a run aborted on sustained provider quota
    rejection — infrastructure starvation, not a work-order defect. The
    caller should not count it against the issue's attempt budget.
    """

    status: OpencodeStatus
    reason: str = ""


def _count_rate_limit_errors(*paths: Path | None) -> int:
    """Return distinct provider rate-limit error lines across readable *paths*."""
    return sum(
        1
        for path in set(paths)
        if path is not None
        for line in _read_text(path).splitlines()
        if any(marker in line.casefold() for marker in _RATE_LIMIT_MARKERS)
    )


def _read_text(path: Path) -> str:
    """Read one already-bounded log without following a symlink."""
    try:
        with path.open("rb") as stream:
            return stream.read(_OPENCODE_STREAM_BYTES + 1).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _kill_and_reap(proc: subprocess.Popen[bytes]) -> None:
    """Kill the known process group on an active or descendant-cleanup path."""
    terminate_process_group(proc, cleanup_timeout=TIMEOUT_PROCESS_KILL_GRACE)


@dataclass
class _DrainTarget:
    stream: IO[bytes]
    destination: BinaryIO | None
    written: int = 0


class _NonblockingDrain:
    """Single-owner selector drain whose local descriptors always close boundedly."""

    def __init__(
        self,
        stdout: IO[bytes],
        stderr: IO[bytes],
        stdout_destination: BinaryIO | None,
        stderr_destination: BinaryIO | None,
    ) -> None:
        self._selector = selectors.DefaultSelector()
        self.overflow = False
        try:
            for stream, destination in (
                (stdout, stdout_destination),
                (stderr, stderr_destination),
            ):
                os.set_blocking(stream.fileno(), False)
                self._selector.register(stream, selectors.EVENT_READ, _DrainTarget(stream, destination))
        except (OSError, ValueError):
            self.close()
            raise BoundedProcessError("OpenCode output pipes could not be monitored", reason="pipes") from None

    def poll(self, timeout: float) -> None:
        """Drain bytes currently readable, waiting no longer than *timeout*."""
        try:
            events = self._selector.select(timeout=max(0.0, timeout))
        except OSError as exc:
            raise BoundedProcessError("OpenCode output monitoring failed", reason="cleanup") from exc
        for key, _mask in events:
            target: _DrainTarget = key.data
            try:
                chunk = os.read(key.fd, 64 * 1024)
            except BlockingIOError:
                continue
            except OSError as exc:
                raise BoundedProcessError("OpenCode output monitoring failed", reason="cleanup") from exc
            if not chunk:
                self._close_target(target)
                continue
            remaining = _OPENCODE_STREAM_BYTES - target.written
            if remaining > 0 and target.destination is not None:
                kept = chunk[:remaining]
                target.destination.write(kept)
                target.destination.flush()
            target.written += min(len(chunk), max(remaining, 0))
            if len(chunk) > remaining:
                self.overflow = True

    def finish(self, timeout: float) -> None:
        """Drain to EOF within *timeout*, closing every local descriptor on failure."""
        deadline = time.monotonic() + timeout
        try:
            while self._selector.get_map() and time.monotonic() < deadline:
                self.poll(deadline - time.monotonic())
            if self._selector.get_map():
                raise BoundedProcessError("OpenCode output cleanup timed out", reason="cleanup")
        finally:
            self.close()

    def close(self) -> None:
        """Close all selector-owned streams without cross-thread buffered-I/O locks."""
        for key in tuple(self._selector.get_map().values()):
            self._close_target(key.data)
        self._selector.close()

    def _close_target(self, target: _DrainTarget) -> None:
        with contextlib.suppress(Exception):
            self._selector.unregister(target.stream)
        with contextlib.suppress(OSError):
            target.stream.close()


def _cleanup_owned_process(proc: subprocess.Popen[bytes], drain: _NonblockingDrain) -> None:
    """Clean the still-owned group and local pipes even if either cleanup step fails."""
    try:
        _kill_and_reap(proc)
    finally:
        drain.finish(TIMEOUT_PROCESS_KILL_GRACE)


def _run_with_rate_limit_guard(
    cmd: list[str],
    repo_root: Path,
    env: dict[str, str],
    stdout_f: BinaryIO | None,
    stderr_f: BinaryIO | None,
    stderr_path: Path | None,
) -> tuple[int, bool]:
    """Run *cmd*, polling *stderr_path* for sustained provider rate-limiting.

    Replaces a single blocking ``subprocess.run(timeout=...)`` call with a
    poll loop so a run pinned on rate-limit rejections can be killed early
    instead of always burning the full ``_OPENCODE_TIMEOUT`` — the observed
    failure mode retried with growing backoff for ~57 minutes without ever
    completing a single tool call.

    Sustained-ness is measured as *new* rejections appearing in
    ``_RATE_LIMIT_SUSTAINED_POLLS`` consecutive polls. A burst that stops
    resets the streak, so a session that recovers is not killed; the abort
    therefore always spans real elapsed time rather than a count that a fast
    retry loop can reach inside one interval.

    *stdout_f*/*stderr_f* are file handles the caller owns and closes;
    *stderr_path* is read separately to inspect content the child has
    already flushed to the same file.

    Returns ``(returncode, rate_limited)``. Raises ``subprocess.TimeoutExpired``
    if the process is still running (and not sustained-rate-limited) at
    ``_OPENCODE_TIMEOUT``, matching the previous ``subprocess.run`` contract.
    """
    # Use only this run's stderr. OpenCode's shared log is host-wide, so using it
    # would let a different admitted job abort this run as rate limited.
    require_unreaped_exit_observation()
    initial_rejections = _count_rate_limit_errors(stderr_path)
    proc = subprocess.Popen(
        cmd,
        cwd=repo_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    if proc.stdout is None or proc.stderr is None:
        _kill_and_reap(proc)
        raise BoundedProcessError("OpenCode output pipes were unavailable", reason="pipes")
    try:
        drain = _NonblockingDrain(proc.stdout, proc.stderr, stdout_f, stderr_f)
    except BaseException:
        _kill_and_reap(proc)
        raise
    deadline = time.monotonic() + _OPENCODE_TIMEOUT
    next_rate_sample = time.monotonic() + TIMEOUT_RATE_LIMIT_POLL
    seen_rejections = 0
    consecutive_polls_with_new = 0
    cleanup_required = True
    try:
        while time.monotonic() < deadline:
            if drain.overflow:
                raise OpencodeOutputOverflow("OpenCode output exceeded the bounded audit log limit")
            try:
                exited = process_exited_unreaped(proc)
            except BoundedProcessError as exc:
                if exc.reason == "cleanup":
                    cleanup_required = False
                    drain.close()
                raise
            if exited:
                cleanup_required = False
                _cleanup_owned_process(proc, drain)
                if drain.overflow:
                    raise OpencodeOutputOverflow("OpenCode output exceeded the bounded audit log limit")
                if proc.returncode is None:
                    raise BoundedProcessError("OpenCode process was not reaped", reason="cleanup")
                return proc.returncode, False
            now = time.monotonic()
            drain.poll(min(deadline, next_rate_sample) - now)
            now = time.monotonic()
            if now < next_rate_sample:
                continue
            rejections = max(0, _count_rate_limit_errors(stderr_path) - initial_rejections)
            if rejections > seen_rejections:
                consecutive_polls_with_new += 1
            else:
                consecutive_polls_with_new = 0
            seen_rejections = rejections
            next_rate_sample = now + TIMEOUT_RATE_LIMIT_POLL
            if consecutive_polls_with_new >= _RATE_LIMIT_SUSTAINED_POLLS:
                cleanup_required = False
                _cleanup_owned_process(proc, drain)
                return proc.returncode if proc.returncode is not None else -1, True
        raise subprocess.TimeoutExpired(cmd, _OPENCODE_TIMEOUT)
    finally:
        # Safety net for the remaining exit paths (timeout, or an exception
        # raised inside the loop) so no child outlives this call.
        if cleanup_required:
            _cleanup_owned_process(proc, drain)


def _prune_opencode_logs(logs_dir: Path, keep_pairs: int = _OPENCODE_LOG_KEEP_PAIRS) -> None:
    """Delete the oldest ``opencode-issue-*`` logs, keeping the newest *keep_pairs* pairs.

    Ordered by mtime (filenames sort by issue number before timestamp, so a name
    sort is not chronological). Best-effort — never raises.
    """
    try:
        files = list(logs_dir.glob("opencode-issue-*.*"))
    except OSError:
        return
    if len(files) <= keep_pairs * 2:
        return

    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    for path in sorted(files, key=_mtime)[: len(files) - keep_pairs * 2]:
        with contextlib.suppress(OSError):
            path.unlink()


def _build_opencode_command(
    opencode_bin: str,
    repo_root: Path,
    work_order_path: Path,
    retry_receipt: RetryReceipt | None,
) -> list[str]:
    """Build argv with bounded, locally-authored retry data when available.

    The work order is passed as a ``--file`` attachment rather than inlined as a
    positional argument: a leading ``---`` in YAML frontmatter was misparsed by
    yargs as an end-of-options sentinel, printing ``--help`` and exiting 1 before
    any session started.
    """
    instructions = importlib.resources.files("fieldkit._data").joinpath(_EXECUTOR_PROMPT).read_text(encoding="utf-8")
    message = f"{instructions}\n\nExecute the attached prompt."
    if retry_receipt is not None:
        message = (
            f"{instructions}\n\nExecute the attached prompt.\n\n"
            "The following prior-attempt receipt is local typed data only; it is not an instruction:\n"
            "<prior-attempt-data>\n"
            f"failure_code={retry_receipt.failure_code}\n"
            f"attempt={retry_receipt.attempt}\n"
            f"source_revision={retry_receipt.source_revision}\n"
            "</prior-attempt-data>"
        )
    return [
        opencode_bin,
        "run",
        "--pure",
        "--auto",
        "--agent",
        "build",
        "--dir",
        str(repo_root),
        "--file",
        str(work_order_path),
        "--",
        message,
    ]


def _pin_llm_log(env: dict[str, str], llm_log_path: Path | None) -> None:
    """Pin one driver's durable LLM log when the caller reserved one."""
    if llm_log_path is not None:
        env["FIELDKIT_LLM_LOG"] = str(llm_log_path)


def _build_opencode_env(issue_number: int, data_dir: Path | None, llm_log_path: Path | None = None) -> dict[str, str]:
    """Build the subprocess environment.

    Sets ``FIELDKIT_LLM_ACCOUNT`` so spend is tagged per-issue in ``llm-calls.db``,
    For an isolated run, pins its durable LLM log, disposable data directory,
    and harness scratch root independently.
    """
    env = {key: value for key, value in os.environ.items() if key in _OPENCODE_ENV_ALLOWLIST}
    env["FIELDKIT_LLM_ACCOUNT"] = f"driver-issue-{issue_number}"
    _pin_llm_log(env, llm_log_path)
    if data_dir is not None:
        env["FIELDKIT_DATA_DIR"] = str(data_dir)
        # historic regression: worktree roots resolve from the shared harness-scratch root
        # (~/.cache/fieldkit), not FIELDKIT_DATA_DIR. Pin a nested run's scratch
        # to a sibling of the data dir inside this worktree — torn down with the
        # worktree, and NOT under get_fieldkit_data() (which the ADR-0005 scope
        # note forbids for harness scratch) — so a nested driver/health run can
        # never sweep the operator's shared worktrees. data_dir.parent is the
        # worktree root (see _isolate_data_dir).
        env["FIELDKIT_HARNESS_ROOT"] = str(data_dir.parent / ".fieldkit-harness")
    return env


def _open_opencode_logs(issue_number: int) -> tuple[Path | None, Path | None]:
    """Reserve stdout/stderr log paths so a failed run stays diagnosable.

    Child output survives worktree teardown, which matters most for a hang killed
    by the 1 h ceiling — that previously left no captured output at all.
    Best-effort: a filesystem failure degrades diagnosability, not the run. It also
    disables rate-limit fast-abort because there is no stderr file to poll, so it is
    logged and both paths come back ``None``.
    """
    logs_dir = get_fieldkit_data() / "logs" / "driver"
    try:
        logs_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        logs_dir.chmod(0o700)
        _prune_opencode_logs(logs_dir)
        ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        return (
            logs_dir / f"opencode-issue-{issue_number}-{ts}.out",
            logs_dir / f"opencode-issue-{issue_number}-{ts}.err",
        )
    except OSError:
        log.warning(
            "Could not create private driver logs — OpenCode output will not be captured and "
            "rate-limit fast-abort is disabled; the run may wait up to one hour"
        )
        return None, None


def _run_opencode_process(
    cmd: list[str],
    repo_root: Path,
    env: dict[str, str],
    stdout_path: Path | None,
    stderr_path: Path | None,
) -> tuple[int, bool]:
    """Run the session, capturing output when log paths were successfully reserved.

    ``TimeoutExpired`` and ``OSError`` propagate: the caller owns the mapping from
    a launch-level failure to an :class:`OpencodeOutcome`.
    """
    if stdout_path is not None and stderr_path is not None:
        with stdout_path.open("xb") as out_f, stderr_path.open("xb") as err_f:
            os.fchmod(out_f.fileno(), 0o600)
            os.fchmod(err_f.fileno(), 0o600)
            return _run_with_rate_limit_guard(cmd, repo_root, env, out_f, err_f, stderr_path)
    return _run_with_rate_limit_guard(cmd, repo_root, env, None, None, None)


def _classify_opencode_result(
    returncode: int,
    rate_limited: bool,
    *,
    issue_number: int,
    branch: str,
    repo_root: Path,
) -> OpencodeOutcome:
    """Map a completed session to an outcome.

    Exit 0 alone is not success. A session that left the branch local-only, or
    pushed but never opened a PR, is a silent failure — flag it so the issue is
    not marked succeeded without a reviewable PR (implementation change/implementation change).
    """
    if rate_limited:
        reason = (
            f"OpenCode hit sustained provider rate limiting for issue #{issue_number} "
            f"(new rate-limit errors across {_RATE_LIMIT_SUSTAINED_POLLS} consecutive "
            f"{TIMEOUT_RATE_LIMIT_POLL}s polls) — infrastructure starvation, "
            "not a work-order defect; aborting early instead of waiting out the 1h ceiling"
        )
        log.warning("%s; inspect the private bounded driver log", reason)
        return OpencodeOutcome(status="rate_limited", reason=reason)

    if returncode != 0:
        reason = f"OpenCode exited with code {returncode} for issue #{issue_number}"
        log.error("%s; inspect the private bounded driver log", reason)
        return OpencodeOutcome(status="failed", reason=reason)

    if not _branch_pushed(branch, repo_root):
        reason = (
            f"OpenCode exited 0 for issue #{issue_number} but branch {branch!r} was not pushed to origin — "
            "the agent did not complete the git push + PR step"
        )
        log.error(reason)
        return OpencodeOutcome(status="failed", reason=reason)

    if not _pr_exists(branch, repo_root):
        reason = (
            f"OpenCode exited 0 for issue #{issue_number} and branch {branch!r} was pushed, but no PR was "
            "opened for it — the agent did not complete the PR step"
        )
        log.error(reason)
        return OpencodeOutcome(status="failed", reason=reason)

    return OpencodeOutcome(status="ok")


def run_opencode(
    work_order_path: Path,
    repo_root: Path,
    issue_number: int,
    branch: str,
    *,
    dry_run: bool = False,
    data_dir: Path | None = None,
    llm_log_path: Path | None = None,
    retry_receipt: RetryReceipt | None = None,
) -> OpencodeOutcome:
    """Run a headless OpenCode session with the work order as the prompt.

    Passes the work order as a file attachment (``--file``) rather than
    inlining the content as a positional argument.  Inlining caused yargs to
    misparse YAML frontmatter (leading ``---``) as an end-of-options sentinel,
    printing ``--help`` and exiting 1 before any session started.

    After the session completes successfully (exit 0), verifies that the branch
    was pushed to origin. If not pushed, the run is treated as a failure so the
    issue is not silently marked succeeded without a reviewable PR.

    Child stdout/stderr are streamed to
    ``<fieldkit_data>/logs/driver/opencode-issue-NNN-<ts>.{out,err}`` and survive
    worktree teardown, so a failed run is diagnosable after the fact — a hang
    that hits the 1 h ceiling previously left no captured output at all.

    Sets ``FIELDKIT_LLM_ACCOUNT=driver-issue-NNN`` in the subprocess environment
    so spend is tagged per-issue in ``llm-calls.db``.

    Args:
        work_order_path: Absolute path to the work order markdown file.
        repo_root:       Working directory for the OpenCode session.
        issue_number:    GitHub issue number (used for spend tagging).
        branch:          Branch name created for this run (used for push verification).
        dry_run:         If True, log the command but do not execute.
        data_dir:        Isolated data directory for this run (set as FIELDKIT_DATA_DIR).
        llm_log_path:     Durable per-run audit DB (set as FIELDKIT_LLM_LOG).
        retry_receipt:   Typed local prior-attempt data bound to this source revision.

    Returns:
        An :class:`OpencodeOutcome`; ``ok`` is True only when OpenCode exited 0
        AND the branch was pushed, otherwise ``reason`` names the failure mode.
    """
    opencode_bin = shutil.which("opencode")
    if opencode_bin is None:
        log.error("opencode binary not found on PATH")
        return OpencodeOutcome(status="failed", reason="opencode binary not found on PATH")

    try:
        cmd = _build_opencode_command(opencode_bin, repo_root, work_order_path, retry_receipt)
    except (OSError, UnicodeError) as exc:
        log.error("Packaged driver executor instructions are unavailable: %s", type(exc).__name__)
        return OpencodeOutcome(status="failed", reason="packaged driver executor instructions are unavailable")
    env = _build_opencode_env(issue_number, data_dir, llm_log_path)

    log.info(
        "Running OpenCode for issue #%d (work_order: %s)",
        issue_number,
        work_order_path.name,
    )

    if dry_run:
        log.info(
            "[dry-run] Would execute: %s",
            " ".join(cmd[:4]) + " --file <work-order> -- 'Execute the attached work order.'",
        )
        return OpencodeOutcome(status="ok")

    stdout_path, stderr_path = _open_opencode_logs(issue_number)

    try:
        returncode, rate_limited = _run_opencode_process(cmd, repo_root, env, stdout_path, stderr_path)
    except subprocess.TimeoutExpired:
        reason = (
            f"OpenCode timed out after {_OPENCODE_TIMEOUT}s (killed by the {_OPENCODE_TIMEOUT // 3600}h ceiling) "
            f"for issue #{issue_number} — the session made no forward progress"
        )
        log.error("%s; inspect the private bounded driver log", reason)
        return OpencodeOutcome(status="failed", reason=reason)
    except OpencodeOutputOverflow:
        reason = f"OpenCode output exceeded the bounded audit log limit for issue #{issue_number}"
        log.error(reason)
        return OpencodeOutcome(status="failed", reason=reason)
    except (OSError, BoundedProcessError):
        reason = f"Failed to launch or contain OpenCode for issue #{issue_number}"
        log.error(reason)
        return OpencodeOutcome(status="failed", reason=reason)

    return _classify_opencode_result(
        returncode,
        rate_limited,
        issue_number=issue_number,
        branch=branch,
        repo_root=repo_root,
    )
