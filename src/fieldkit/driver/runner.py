"""Core driver-loop scheduling, execution, verification, and status persistence."""

import fcntl
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fieldkit.config import (
    TIMEOUT_PROCESS_KILL_GRACE,
    get_driver_max_concurrent,
    get_fieldkit_data,
    get_github_repo,
    get_harness_scratch_root,
)
from fieldkit.driver.done_check_executor import (
    VerificationError,
    create_trusted_snapshot,
    remove_snapshot,
    verify_submitted_head,
)
from fieldkit.driver.github import (
    LABEL_READY,
    AgentIssue,
    GitHubLookupError,
    comment_on_issue,
    comment_once,
    list_ready_issues,
    remove_label,
    transition_to_failed,
    transition_to_succeeded,
)
from fieldkit.driver.opencode import OpencodeOutcome, run_opencode
from fieldkit.driver.prompt_source import (
    FrozenPrompt,
    PromptSourceError,
    freeze_origin_main,
    freeze_prompt,
    has_prompt_reference,
    resolve_prompt_source,
)
from fieldkit.driver.retry_state import FailureCode, RetryReceipt, check_eligibility, complete_attempt, reserve_attempt
from fieldkit.driver.scheduler import SchedulerError, busy_files, open_issue_numbers, select_runnable
from fieldkit.driver.spend import (
    cap_safe_max_concurrent,
    evaluate_daily_spend_cap,
    get_spend_summary,
    reserve_run_db_path,
)
from fieldkit.driver.status_types import RunOutcome
from fieldkit.errors import AuthError, GitHubRequestError
from fieldkit.util.atomic import locked_json_update
from fieldkit.util.bounded_process import BoundedProcessError, run_bounded_process, run_bounded_process_bytes

log = logging.getLogger(__name__)

# Subprocess timeouts (seconds)
_GIT_TIMEOUT: int = 30  # fast local git operations
_GIT_OUTPUT_BYTES: int = 8 * 1024 * 1024

# Serializes git fetch + worktree add against the shared repo checkout when
# a batch (max_concurrent > 1) creates worktrees from concurrent threads.
_WORKTREE_CREATE_LOCK = threading.Lock()

# Leading marker for the "work order referenced but not resolvable yet" comment.
# comment_once() keys idempotency off this prefix so the driver posts it at most
# once per issue rather than on every hourly tick while the file is unlanded.
_WO_UNRESOLVED_MARKER = "⏳ **Driver waiting**"
_RATE_LIMITED_MARKER = "⏳ **Driver rate-limited**"
_FAILURE_MESSAGES: dict[FailureCode, str] = {
    "agent-failed": "OpenCode execution failed; inspect the bounded local driver logs.",
    "authentication-failed": "Driver authentication failed; operator action is required.",
    "rate-limited": "The model provider applied a rate limit to this attempt; retry later.",
    "setup-failed": "Independent verification setup failed.",
    "verification-failed": "The submitted head failed independent verification.",
    "worktree-failed": "The driver could not create its isolated worktree.",
}
_RETRY_FINALIZATION_FAILED = "Driver could not finalize local retry state."
_RETRY_RESERVATION_DENIED = "Driver could not persist a local retry reservation; execution was not admitted."
_UNKNOWN_EXECUTION_FAILURE = "Driver execution failed; inspect bounded local driver logs."


def _notify_rate_limited(repo: str, issue_number: int, error_msg: str, dry_run: bool) -> None:
    if dry_run:
        return
    comment_once(
        repo,
        issue_number,
        _RATE_LIMITED_MARKER,
        f"{_RATE_LIMITED_MARKER}\n\n"
        f"{error_msg}\n\n"
        "This is infrastructure starvation, not a work-order defect, so "
        "**the local reserved attempt is charged and GitHub labels are unchanged**. "
        "The issue retries automatically only while its local retry budget remains; "
        "use `fieldkit driver retry status` and `fieldkit driver retry reset` after "
        "the budget is exhausted. If this keeps recurring, the rate-limiting needs "
        "attention outside the driver.",
    )


# Branch creation


def make_branch_name(issue: AgentIssue) -> str:
    """Derive a safe git branch name from the issue title.

    Format: ``driver/issue-NNN-slug`` where slug is the lowercase title with
    non-alphanumeric runs replaced by hyphens, truncated to 50 chars.

    Args:
        issue: The issue to create a branch for.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", issue.title.lower()).strip("-")[:50]
    return f"driver/issue-{issue.number}-{slug}"


def _worktrees_root() -> Path:
    """Return the directory that holds ephemeral driver worktrees.

    Rooted at the harness-scratch root (cache-class), not ``get_fieldkit_data()``:
    a driver worktree is a throwaway checkout of the code repo, not a fieldkit
    runtime artifact (historic regression / ADR-0005 scope note).
    """
    return get_harness_scratch_root() / "driver" / "worktrees"


def create_worktree(
    branch: str, repo_root: Path, issue_number: int, *, base_revision: str | None = None
) -> Path | None:
    """Create an isolated git worktree for *branch* off a fresh ``origin/main``.

    Each driver run executes in its own worktree rather than the shared
    ``repo_root`` checkout.  This avoids branch-name collisions between runs,
    keeps the driver from mutating the working tree that live interactive
    sessions share, and guarantees every run starts from the latest pushed
    ``main`` rather than whatever HEAD the shared checkout happens to be on.

    The branch is (re)created with ``-B`` so a leftover *local* ref from a prior
    failed run never blocks the run; the pushed branch on ``origin`` (if any) is
    untouched until the agent pushes.  Fetches ``origin`` first so the base ref
    is current — a run cannot succeed without ``origin`` anyway (it ends at
    ``git push`` + PR).

    Args:
        branch:       Branch name to create in the worktree.
        repo_root:    Repository root directory (the shared checkout).
        issue_number: Issue number, used only to name the worktree directory.

    Returns:
        The absolute path to the new worktree, or ``None`` on failure.
    """
    base_ref = base_revision or "origin/main"
    # git fetch and git worktree add both mutate shared .git state in
    # repo_root; concurrent batch threads (max_concurrent > 1) racing here
    # hit git's ref/worktree lock files and fail spuriously — which would
    # burn an attempt on the issue. Serialize creation; the long-running
    # OpenCode sessions themselves still run concurrently.
    with _WORKTREE_CREATE_LOCK:
        ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        worktree_path = _worktrees_root() / f"issue-{issue_number}-{ts}"
        try:
            worktree_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            log.error("Failed to create driver worktree root")
            return None

        try:
            result = run_bounded_process(
                ["git", "worktree", "add", "--force", "-B", branch, str(worktree_path), base_ref],
                cwd=repo_root,
                timeout=_GIT_TIMEOUT,
                stdout_limit=_GIT_OUTPUT_BYTES,
                stderr_limit=_GIT_OUTPUT_BYTES,
                cleanup_timeout=TIMEOUT_PROCESS_KILL_GRACE,
            )
            if result.returncode != 0:
                raise BoundedProcessError("git worktree add failed", reason="start")
        except BoundedProcessError:
            log.error("Failed to create driver worktree")
            # Best-effort cleanup of a partially-created worktree.
            remove_worktree(worktree_path, repo_root)
            return None

    log.info("Created driver worktree for issue #%d", issue_number)
    return worktree_path


def remove_worktree(worktree_path: Path, repo_root: Path) -> None:
    """Tear down an ephemeral driver worktree.  Best-effort; never raises."""
    try:
        run_bounded_process(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=repo_root,
            timeout=_GIT_TIMEOUT,
            stdout_limit=_GIT_OUTPUT_BYTES,
            stderr_limit=_GIT_OUTPUT_BYTES,
            cleanup_timeout=TIMEOUT_PROCESS_KILL_GRACE,
        )
    except BoundedProcessError:
        log.warning("git worktree remove did not complete")
    try:
        run_bounded_process(
            ["git", "worktree", "prune"],
            cwd=repo_root,
            timeout=_GIT_TIMEOUT,
            stdout_limit=_GIT_OUTPUT_BYTES,
            stderr_limit=_GIT_OUTPUT_BYTES,
            cleanup_timeout=TIMEOUT_PROCESS_KILL_GRACE,
        )
    except BoundedProcessError:
        log.warning("git worktree prune did not complete")


# ---------------------------------------------------------------------------
# Data isolation
# ---------------------------------------------------------------------------


def _isolate_data_dir(worktree_path: Path) -> Path:
    """Create an isolated fieldkit-data directory inside the worktree.

    Prevents the agent's ``make quality`` (pytest) from corrupting shared
    watcher state files or LLM call logs in the operator's live data dir.

    Returns:
        Absolute path to the isolated data directory.
    """
    data_dir = worktree_path / ".fieldkit-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


# ---------------------------------------------------------------------------
# Prior failure context
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Status logging
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateResult:
    """One candidate's outcome and whether its status record was persisted."""

    issue_number: int
    outcome: RunOutcome
    error: str
    status_persisted: bool


@dataclass
class RunResult:
    issue_number: int | None
    issue_title: str
    outcome: RunOutcome
    branch: str
    elapsed_seconds: float
    spend_note: str
    error: str
    candidates: tuple[CandidateResult, ...] = ()


def _write_run_status(result: RunResult) -> None:
    """Append *result* to ``<fieldkit_data>/logs/driver/driver-run-status.json``."""
    logs_dir = get_fieldkit_data() / "logs" / "driver"
    logs_dir.mkdir(parents=True, exist_ok=True)
    status_file = logs_dir / "driver-run-status.json"
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    entry = {
        "ts": ts,
        "issue_number": result.issue_number,
        "issue_title": result.issue_title,
        "outcome": result.outcome,
        "branch": result.branch,
        "elapsed_seconds": round(result.elapsed_seconds, 1),
        "spend_note": result.spend_note,
        "error": result.error,
    }

    with locked_json_update(status_file) as existing:
        entries: list[dict[str, Any]] = list(existing.get("runs", []))
        entries.append(entry)
        if len(entries) > 100:
            del entries[: len(entries) - 100]
        existing.clear()
        existing["runs"] = entries


def _aggregate_run_result(
    result: RunResult, candidates: tuple[CandidateResult, ...], *, status_failed: bool
) -> RunResult:
    """Project all observed candidates into the canonical tick result once."""
    blocked = [candidate for candidate in candidates if candidate.outcome in {"failed", "skipped"}]
    error = result.error
    outcome = result.outcome
    if blocked and len(candidates) > 1:
        error = "; ".join(f"Issue #{candidate.issue_number}: {candidate.error}" for candidate in blocked)
        outcome = "skipped" if all(candidate.outcome == "skipped" for candidate in candidates) else "failed"
    if status_failed:
        error = "; ".join(filter(None, (error, "Driver status persistence failed; durable records are incomplete.")))
        outcome = "skipped" if outcome == "skipped" else "failed"
    return replace(
        result,
        issue_number=None if len(candidates) > 1 else result.issue_number,
        issue_title="(batch)" if len(candidates) > 1 else result.issue_title,
        branch="" if len(candidates) > 1 else result.branch,
        spend_note="" if len(candidates) > 1 else result.spend_note,
        outcome=outcome,
        error=error,
        candidates=candidates,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _sanitize_execution_result(result: RunResult) -> RunResult:
    """Keep canonical failure messages, never publish arbitrary child diagnostics."""
    if (
        result.outcome == "failed"
        and result.error not in _FAILURE_MESSAGES.values()
        and result.error != _RETRY_FINALIZATION_FAILED
    ):
        return replace(result, error=_UNKNOWN_EXECUTION_FAILURE)
    return result


def _resolve_run_work_order(
    work_order_path: Path,
    repo_root: Path,
    worktree_path: Path | None,
    *,
    revision_bound: bool,
) -> Path:
    """Point the agent at the worktree's own copy of the work order.

    The work order is committed on main, so it is present in a fresh worktree;
    using that copy keeps the session operating entirely inside its isolation.
    Dry-runs and isolated non-Git fixtures use the original path because they
    have no frozen revision. A revision-bound run fails closed if its worktree
    does not contain the prompt.
    """
    if worktree_path is None:
        return work_order_path
    candidate = worktree_path / work_order_path.relative_to(repo_root)
    if candidate.is_file() and not candidate.is_symlink():
        return candidate
    if revision_bound:
        raise PromptSourceError("revision-bound worktree does not contain the driver prompt")
    return work_order_path


def _execution_worktree_is_clean(worktree_path: Path) -> bool:
    """Return whether every local change was committed before submitted-head verification."""
    try:
        result = run_bounded_process_bytes(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=worktree_path,
            timeout=_GIT_TIMEOUT,
            stdout_limit=_GIT_OUTPUT_BYTES,
            stderr_limit=_GIT_OUTPUT_BYTES,
            cleanup_timeout=TIMEOUT_PROCESS_KILL_GRACE,
        )
    except BoundedProcessError:
        return False
    return result.returncode == 0 and result.stdout == b""


def _finalize_run_outcome(
    repo: str,
    issue: AgentIssue,
    *,
    attempt: int,
    branch: str,
    oc_outcome: OpencodeOutcome,
    started_at: datetime,
    spend_note: str,
    dry_run: bool,
    failure_code: FailureCode = "agent-failed",
) -> RunResult:
    """Persist the terminal outcome before projecting it onto GitHub."""

    def _result(outcome: RunOutcome, error: str) -> RunResult:
        return RunResult(
            issue_number=issue.number,
            issue_title=issue.title,
            outcome=outcome,
            branch=branch,
            elapsed_seconds=(datetime.now(UTC) - started_at).total_seconds(),
            spend_note=spend_note,
            error=error,
        )

    if oc_outcome.status == "ok":
        if not dry_run:
            finalized = complete_attempt(
                repo, issue.number, succeeded=True, outcome="succeeded", data_root=get_fieldkit_data()
            )
            if not finalized.allowed:
                return _result("failed", _RETRY_FINALIZATION_FAILED)
            transition_to_succeeded(repo, issue, attempt=attempt)
            comment_on_issue(
                repo,
                issue.number,
                f"✅ **Driver succeeded** — branch `{branch}` pushed, PR opened.",
            )
        return _result("dry-run" if dry_run else "ok", "")

    if oc_outcome.status == "rate_limited":
        error_msg = _FAILURE_MESSAGES["rate-limited"]
        # Provider starvation still charges the reserved attempt; GitHub labels
        # remain unchanged and the idempotent comment exposes the condition.
        log.warning("Issue #%d: %s — skipping after a reserved attempt", issue.number, error_msg)
        if not dry_run:
            finalized = complete_attempt(
                repo,
                issue.number,
                succeeded=False,
                outcome=error_msg,
                failure_code="rate-limited",
                data_root=get_fieldkit_data(),
            )
            if not finalized.allowed:
                return _result("failed", _RETRY_FINALIZATION_FAILED)
        _notify_rate_limited(repo, issue.number, error_msg, dry_run)
        return _result("skipped", error_msg)

    error_msg = _FAILURE_MESSAGES[failure_code]
    if not dry_run:
        finalized = complete_attempt(
            repo,
            issue.number,
            succeeded=False,
            outcome=error_msg,
            failure_code=failure_code,
            data_root=get_fieldkit_data(),
        )
        if not finalized.allowed:
            return _result("failed", _RETRY_FINALIZATION_FAILED)
        transition_to_failed(repo, issue, attempt=attempt, error=error_msg)
    return _result("failed", error_msg)


def _run_attempt_lifecycle(
    repo: str,
    issue: AgentIssue,
    work_order_path: Path,
    contract_path: Path,
    repo_root: Path,
    started_at: datetime,
    run_started_monotonic: float,
    attempt: int,
    branch: str,
    worktree_path: Path | None,
    retry_receipt: RetryReceipt | None,
    *,
    source_revision: str | None,
    dry_run: bool,
) -> RunResult:
    snapshot = None
    try:
        work_dir = worktree_path if worktree_path is not None else repo_root
        run_work_order = _resolve_run_work_order(
            work_order_path,
            repo_root,
            worktree_path,
            revision_bound=source_revision is not None,
        )
        data_dir = _isolate_data_dir(worktree_path) if worktree_path is not None else None
        llm_log_path = reserve_run_db_path(issue.number) if not dry_run else None
        if not dry_run:
            snapshot = create_trusted_snapshot(
                repo_root,
                work_order_path,
                contract_path=contract_path,
                base_revision=source_revision,
                repository=repo,
                issue_number=issue.number,
                attempt=attempt,
                branch=branch,
                snapshot_root=get_harness_scratch_root() / "driver" / "done-check-snapshots",
            )
        oc_outcome = run_opencode(
            run_work_order,
            work_dir,
            issue.number,
            branch,
            dry_run=dry_run,
            data_dir=data_dir,
            llm_log_path=llm_log_path,
            retry_receipt=retry_receipt,
        )
        spend_note = get_spend_summary(issue.number, llm_log_path) if not dry_run else ""
        failure_code: FailureCode = "agent-failed"

        if (
            oc_outcome.status == "ok"
            and not dry_run
            and source_revision is not None
            and worktree_path is not None
            and not _execution_worktree_is_clean(worktree_path)
        ):
            failure_code = "verification-failed"
            oc_outcome = OpencodeOutcome(status="failed", reason="execution worktree has unsubmitted changes")

        if oc_outcome.status == "ok" and not dry_run:
            assert snapshot is not None
            verification = verify_submitted_head(
                snapshot,
                repo_root,
                evidence_root=get_fieldkit_data() / "driver" / "done-check-evidence",
                verifier_root=get_harness_scratch_root() / "driver" / "done-check-verifier",
                run_started_monotonic=run_started_monotonic,
            )
            if not verification.passed:
                failure_code = "verification-failed"
                oc_outcome = OpencodeOutcome(
                    status="failed", reason=f"Independent verification failed: {verification.reason}"
                )

        return _finalize_run_outcome(
            repo,
            issue,
            attempt=attempt,
            branch=branch,
            oc_outcome=oc_outcome,
            started_at=started_at,
            spend_note=spend_note,
            dry_run=dry_run,
            failure_code=failure_code,
        )
    except (VerificationError, GitHubLookupError, BoundedProcessError, OSError, subprocess.SubprocessError):
        return _finalize_run_outcome(
            repo,
            issue,
            attempt=attempt,
            branch=branch,
            oc_outcome=OpencodeOutcome(status="failed", reason="Independent verification setup failed"),
            started_at=started_at,
            spend_note="",
            dry_run=dry_run,
            failure_code="setup-failed",
        )
    except AuthError:
        if not dry_run:
            finalized = complete_attempt(
                repo,
                issue.number,
                succeeded=False,
                outcome="authentication failed during independent verification",
                failure_code="authentication-failed",
                data_root=get_fieldkit_data(),
            )
            if not finalized.allowed:
                raise AuthError("Authentication required; local attempt finalization failed") from None
        raise
    finally:
        # Always tear down the worktree — the branch is pushed to origin for
        # the PR, so only the local checkout needs removing.
        if worktree_path is not None:
            remove_worktree(worktree_path, repo_root)
        if snapshot is not None and not remove_snapshot(snapshot):
            log.error("Trusted driver snapshot cleanup failed")


def _execute_one(
    repo: str,
    issue: AgentIssue,
    prompt: FrozenPrompt,
    repo_root: Path,
    started_at: datetime,
    run_started_monotonic: float,
    *,
    dry_run: bool,
) -> RunResult:
    """Execute a single selected issue: worktree → OpenCode → transitions → teardown.

    Thread-safe: each call operates in its own worktree with its own isolated
    data directory; status writes go through ``locked_json_update``.
    """

    def _elapsed() -> float:
        return (datetime.now(UTC) - started_at).total_seconds()

    log.info(
        "Picked issue #%d: %s (attempt %d/%d)",
        issue.number,
        issue.title,
        issue.attempt_count + 1,
        3,
    )

    reservation = (
        check_eligibility(repo, issue.number, issue.labels, data_root=get_fieldkit_data())
        if dry_run
        else reserve_attempt(
            repo,
            issue.number,
            issue.labels,
            source_revision=prompt.revision,
            data_root=get_fieldkit_data(),
        )
    )
    if not reservation.allowed or reservation.attempt is None:
        return RunResult(
            issue_number=issue.number,
            issue_title=issue.title,
            outcome="skipped",
            branch="",
            elapsed_seconds=_elapsed(),
            spend_note="",
            error=_RETRY_RESERVATION_DENIED,
        )
    attempt = reservation.attempt
    branch = make_branch_name(issue)
    worktree_path: Path | None = None
    if not dry_run:
        if prompt.revision is None:
            worktree_path = create_worktree(branch, repo_root, issue.number)
        else:
            worktree_path = create_worktree(branch, repo_root, issue.number, base_revision=prompt.revision)
        if worktree_path is None:
            error_msg = _FAILURE_MESSAGES["worktree-failed"]
            finalized = complete_attempt(
                repo,
                issue.number,
                succeeded=False,
                outcome=error_msg,
                failure_code="worktree-failed",
                data_root=get_fieldkit_data(),
            )
            if finalized.allowed:
                transition_to_failed(repo, issue, attempt=attempt, error=error_msg)
            return RunResult(
                issue_number=issue.number,
                issue_title=issue.title,
                outcome="failed",
                branch=branch,
                elapsed_seconds=_elapsed(),
                spend_note="",
                error=error_msg,
            )
    return _run_attempt_lifecycle(
        repo,
        issue,
        prompt.source.path,
        prompt.contract_path,
        repo_root,
        started_at,
        run_started_monotonic,
        attempt,
        branch,
        worktree_path,
        reservation.receipt,
        source_revision=prompt.revision,
        dry_run=dry_run,
    )


def run_driver(
    *,
    repo_root: Path,
    dry_run: bool = False,
) -> RunResult:
    """Execute one driver-loop iteration.

    Selects a pairwise-disjoint batch of runnable ``agent-ready`` issues after
    excluding work orders blocked by open PRs or unresolved dependencies.

    Args:
        repo_root: Absolute path to the repository root.
        dry_run:   Log actions but make no external changes (no labels, no git).

    Returns:
        The aggregate tick result, including each observed candidate's outcome.
    """
    lock_path = get_fieldkit_data() / "driver" / "driver.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        log.info("Driver lock held by another run — skipping (PM-10/implementation change)")
        return RunResult(
            issue_number=None,
            issue_title="(none)",
            outcome="skipped",
            branch="",
            elapsed_seconds=0.0,
            spend_note="",
            error="Driver lock held by another run — skipping",
        )
    # Keep the file open until return so its advisory lock remains held. File
    # descriptors are non-inheritable, so subprocesses do not receive the lock.

    start = datetime.now(UTC)
    run_started_monotonic = time.monotonic()

    def _elapsed() -> float:
        return (datetime.now(UTC) - start).total_seconds()

    def _skipped(error: str) -> RunResult:
        return RunResult(
            issue_number=None,
            issue_title="(none)",
            outcome="skipped",
            branch="",
            elapsed_seconds=_elapsed(),
            spend_note="",
            error=error,
        )

    observations: list[CandidateResult] = []
    status_failed = False
    record_lock = threading.Lock()

    def _record(result: RunResult) -> None:
        nonlocal status_failed
        with record_lock:
            persisted = True
            try:
                _write_run_status(result)
            except (OSError, ValueError):
                persisted = False
                status_failed = True
                log.warning("Could not persist driver run status")
            if result.issue_number is not None:
                observations.append(CandidateResult(result.issue_number, result.outcome, result.error, persisted))

    def _finish(result: RunResult) -> RunResult:
        candidates = tuple(sorted(observations, key=lambda candidate: candidate.issue_number))
        return _aggregate_run_result(
            replace(result, elapsed_seconds=_elapsed()), candidates, status_failed=status_failed
        )

    # implementation note: daily spend guard. Fails CLOSED. A cap the operator set is a statement
    # that unbounded spend is unacceptable, so "cannot verify spend" and "cap
    # misconfigured" both stop the run rather than proceeding uncapped. This mirrors the
    # busy-set check below, which also runs nothing when it cannot see the state it
    # needs. A false stop costs one skipped hourly tick; a false proceed is unbounded.
    if not dry_run:
        spend_check = evaluate_daily_spend_cap(os.environ.get("FIELDKIT_DRIVER_SPEND_CAP"), required=False)
        if not spend_check.allowed:
            log.error("Daily driver spend guard denied this run: %s", spend_check.detail)
            prefix = (
                "Spend cap misconfigured (fail closed)"
                if spend_check.reason_code == "spend-cap-invalid"
                else "Spend cap check (fail closed)"
            )
            executed = _skipped(prefix)
            _record(executed)
            return _finish(executed)

    repo = get_github_repo()
    try:
        source_revision = freeze_origin_main(repo_root, refresh=not dry_run)
    except PromptSourceError as exc:
        executed = _skipped(f"Prompt-source revision lookup failed (fail closed): {exc}")
        _record(executed)
        return _finish(executed)
    try:
        issues = list_ready_issues(repo)
    except GitHubRequestError:
        executed = _skipped("GitHub issue queue lookup failed")
        _record(executed)
        return _finish(executed)

    if not issues:
        log.info("No agent-ready issues found — nothing to do")
        return _skipped("")

    # Resolve work orders up front — issues without a reference are delabeled
    # and skipped (they don't burn an attempt), the rest become candidates.
    candidates: list[tuple[AgentIssue, FrozenPrompt]] = []
    candidate_failures: list[str] = []
    for issue in issues:
        prompt_source = resolve_prompt_source(issue, repo_root)
        if prompt_source is None:
            if has_prompt_reference(issue.body):
                # Policy A: a reference is present but the file is not resolvable
                # this tick — almost always the work order has not propagated to
                # origin/main yet (foreman flipped agent-ready near the merge).
                # Leave agent-ready ON so the next tick self-heals once the file
                # lands; never silently strip (that stranded the issue from both
                # the driver and the foreman, which skips body-referenced issues).
                log.warning(
                    "Issue #%d references a work order not resolvable yet — leaving agent-ready, skipping this tick",
                    issue.number,
                )
                if not dry_run:
                    comment_once(
                        repo,
                        issue.number,
                        _WO_UNRESOLVED_MARKER,
                        f"{_WO_UNRESOLVED_MARKER}\n\n"
                        "The referenced work order could not be resolved on `origin/main` "
                        "or in the driver checkout, so this run is skipping the issue and "
                        "**leaving `agent-ready` in place**. If the work order was merged "
                        "moments ago it will run automatically on the next tick once the "
                        "file propagates. If it keeps recurring, the reference is likely "
                        "wrong or the file was never merged — correct the `WorkOrder:` path "
                        "in the issue body before the next run.",
                    )
                _record(
                    RunResult(
                        issue_number=issue.number,
                        issue_title=issue.title,
                        outcome="skipped",
                        branch="",
                        elapsed_seconds=_elapsed(),
                        spend_note="",
                        error="Work order referenced but not resolvable this tick — agent-ready retained",
                    )
                )
                candidate_failures.append(f"#{issue.number}: prompt reference is not resolvable")
                continue
            # No prompt reference at all: a malformed agent-ready issue that can
            # never run until someone adds a reference. Remove the label (the
            # foreman re-picks reference-less issues and authors one) and comment
            # so the removal is not silent.
            log.warning(
                "Issue #%d has no work order reference — removing agent-ready label and skipping",
                issue.number,
            )
            if not dry_run:
                comment_on_issue(
                    repo,
                    issue.number,
                    "⚠️ **Driver skipped** — this issue is `agent-ready` but its body has no "
                    "work-order reference (`WorkOrder: docs/work-orders/<name>.md`). "
                    "Removing `agent-ready`; add a valid `WorkOrder:` reference and then "
                    "re-add the label.",
                )
                remove_label(repo, issue.number, LABEL_READY)
            _record(
                RunResult(
                    issue_number=issue.number,
                    issue_title=issue.title,
                    outcome="skipped",
                    branch="",
                    elapsed_seconds=_elapsed(),
                    spend_note="",
                    error="No work order reference — agent-ready label removed",
                )
            )
            candidate_failures.append(f"#{issue.number}: no prompt reference")
            continue
        try:
            prompt = freeze_prompt(repo_root, prompt_source, source_revision)
        except PromptSourceError:
            reason = f"Prompt contract invalid or stale (fail closed): {prompt_source.path.relative_to(repo_root).as_posix()}"
            log.warning("Issue #%d skipped: %s", issue.number, reason)
            _record(RunResult(issue.number, issue.title, "skipped", "", _elapsed(), "", reason))
            candidate_failures.append(f"#{issue.number}: {reason}")
            continue
        if not dry_run:
            retry_decision = check_eligibility(repo, issue.number, issue.labels, data_root=get_fieldkit_data())
            if not retry_decision.allowed:
                reason = "Local retry eligibility denied"
                log.warning("Issue #%d skipped by local retry state", issue.number)
                _record(RunResult(issue.number, issue.title, "skipped", "", _elapsed(), "", reason))
                candidate_failures.append(f"#{issue.number}: {reason}")
                continue
        candidates.append((issue, prompt))

    if not candidates:
        detail = "; ".join(candidate_failures) or "No eligible prompt source was found"
        executed = _skipped(f"No queued issue was executable: {detail}")
        _record(executed)
        return _finish(executed)

    # Covers-as-locks selection: compute the busy set (files changed by open
    # PRs targeting main) and the open dependencies, then pick a batch of
    # pairwise-disjoint candidates. Busy-set failure fails closed.
    try:
        busy = busy_files(repo)
    except SchedulerError:
        log.error("Could not compute busy set — failing closed, running nothing")
        executed = _skipped("Busy-set lookup failed (fail closed)")
        _record(executed)
        return _finish(executed)

    declared_deps: set[int] = set()
    for _, prompt in candidates:
        declared_deps.update(prompt.contract.depends_on)
    open_deps = open_issue_numbers(repo, declared_deps) if declared_deps else frozenset()

    max_concurrent = cap_safe_max_concurrent(
        get_driver_max_concurrent(), dry_run=dry_run, raw_cap=os.environ.get("FIELDKIT_DRIVER_SPEND_CAP")
    )
    selected, skips = select_runnable(candidates, busy, open_deps, max_concurrent)
    for skip in skips:
        log.info("Skipping issue %s", skip)
        issue = next(issue for issue, _prompt in candidates if issue.number == skip.issue_number)
        _record(RunResult(issue.number, issue.title, "skipped", "", _elapsed(), "", str(skip)))

    if not selected:
        summary = "; ".join(str(skip) for skip in skips)
        executed = _skipped(f"All candidates blocked: {summary}")
        _record(executed)
        return _finish(executed)

    def _execute_and_record(issue: AgentIssue, prompt: FrozenPrompt) -> RunResult:
        try:
            result = _sanitize_execution_result(
                _execute_one(repo, issue, prompt, repo_root, start, run_started_monotonic, dry_run=dry_run)
            )
        except AuthError:
            _record(RunResult(issue.number, issue.title, "failed", "", _elapsed(), "", "Authentication required"))
            raise
        _record(result)
        return result

    # Execute the batch — plain call for one issue, threads for more (the
    # payload is subprocess.run, which releases the GIL).
    if len(selected) == 1:
        issue, prompt = selected[0]
        results = [_execute_and_record(issue, prompt)]
    else:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=len(selected)) as pool:
            futures = [pool.submit(_execute_and_record, issue, prompt) for issue, prompt in selected]
            results = [future.result() for future in futures]

    return _finish(results[-1])
