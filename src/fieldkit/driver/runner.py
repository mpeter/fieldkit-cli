"""Core driver-loop scheduling, execution, verification, and status persistence."""

import fcntl
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fieldkit.config import (
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
    comment_on_issue,
    comment_once,
    last_failure_comment,
    list_ready_issues,
    remove_label,
    transition_to_failed,
    transition_to_succeeded,
)
from fieldkit.driver.opencode import OpencodeOutcome, run_opencode
from fieldkit.driver.retry_state import check_eligibility, complete_attempt, reserve_attempt
from fieldkit.driver.scheduler import (
    SchedulerError,
    busy_files,
    open_issue_numbers,
    parse_depends_on,
    select_runnable,
)
from fieldkit.driver.spend import (
    cap_safe_max_concurrent,
    evaluate_daily_spend_cap,
    get_spend_summary,
    reserve_run_db_path,
)
from fieldkit.errors import AuthError
from fieldkit.util.atomic import locked_json_update

log = logging.getLogger(__name__)

# Subprocess timeouts (seconds)
_GIT_TIMEOUT: int = 30  # fast local git operations

# Serializes git fetch + worktree add against the shared repo checkout when
# a batch (max_concurrent > 1) creates worktrees from concurrent threads.
_WORKTREE_CREATE_LOCK = threading.Lock()

# Leading marker for the "work order referenced but not resolvable yet" comment.
# comment_once() keys idempotency off this prefix so the driver posts it at most
# once per issue rather than on every hourly tick while the file is unlanded.
_WO_UNRESOLVED_MARKER = "⏳ **Driver waiting**"
_RATE_LIMITED_MARKER = "⏳ **Driver rate-limited**"


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
        "attention outside the driver (see historic regression).",
    )


# Prompt source resolution
_WORK_ORDER_RE = re.compile(
    r"(?:work\s*order|brief)\s*[:\-]\s*(docs/(?:work-orders|briefs)/[\w\-]+\.md)",
    re.IGNORECASE,
)

_OPENSPEC_RE = re.compile(
    r"(?:openspec\s*[:\-]?\s*)(openspec/changes/[\w\-]+/)",
    re.IGNORECASE,
)

_SPECKIT_RE = re.compile(
    r"(?:speckit\s*[:\-]?\s*)(specs/[\w\-]+/)",
    re.IGNORECASE,
)


def _has_prompt_reference(body: str) -> bool:
    """True if the issue body carries any recognized prompt-source reference.

    Distinguishes "referenced a work order that is not resolvable yet" (leave
    ``agent-ready`` on; it self-heals when the file lands) from "no reference
    at all" (a malformed issue whose label should be removed).
    """
    return bool(_WORK_ORDER_RE.search(body) or _OPENSPEC_RE.search(body) or _SPECKIT_RE.search(body))


def _exists_on_main_or_disk(repo_root: Path, abs_path: Path) -> bool:
    """True if *abs_path* exists on disk or as a blob at ``origin/main``."""
    if abs_path.is_file():
        return True
    try:
        rel = abs_path.relative_to(repo_root)
    except ValueError:
        return False
    try:
        result = subprocess.run(
            ["git", "cat-file", "-t", f"origin/main:{rel.as_posix()}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=_GIT_TIMEOUT,
        )
        return result.stdout.strip() == "blob"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


def _fetch_main(repo_root: Path) -> None:
    """Best-effort ``git fetch origin main`` before work-order resolution."""
    try:
        subprocess.run(
            ["git", "fetch", "origin", "main"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        log.warning("Pre-resolution fetch of origin/main failed (continuing): %s", stderr.strip() or exc)


def _resolve_dir_source(
    *,
    rel_dir: str,
    allowed_root: Path,
    primary_file: str,
    fallback_file: str,
    source_label: str,
    issue_number: int,
    repo_root: Path,
) -> Path | None:
    """Resolve a directory-based prompt source (OpenSpec or Speckit).

    Looks for *primary_file* inside *rel_dir*, falling back to *fallback_file*.
    Applies a ``relative_to`` path-safety guard against *allowed_root*.

    Args:
        rel_dir:       Relative directory path from the issue body match.
        allowed_root:  Absolute path the resolved directory must stay inside.
        primary_file:  Filename to try first (e.g. ``"tasks.md"``).
        fallback_file: Filename to try if primary is absent (e.g. ``"proposal.md"``).
        source_label:  Human-readable label for error messages (e.g. ``"OpenSpec"``).
        issue_number:  GitHub issue number, for error logging.
        repo_root:     Absolute repository root.

    Returns:
        Absolute :class:`Path` to the resolved file, or ``None`` on failure.
    """
    dir_path = (repo_root / rel_dir).resolve()

    # Safety: resolved directory must stay inside its allowed root.
    try:
        dir_path.relative_to(allowed_root)
    except ValueError:
        log.error(
            "%s path %r escapes allowed root %s — refusing",
            source_label,
            rel_dir,
            allowed_root,
        )
        return None

    for candidate in (primary_file, fallback_file):
        candidate_path = dir_path / candidate
        if _exists_on_main_or_disk(repo_root, candidate_path):
            log.info(
                "Issue #%d: resolved %s prompt from %s",
                issue_number,
                source_label,
                candidate_path.relative_to(repo_root),
            )
            return candidate_path

    log.error(
        "Issue #%d: %s directory found but neither %r nor %r exists: %s",
        issue_number,
        source_label,
        primary_file,
        fallback_file,
        dir_path,
    )
    return None


def resolve_prompt_source(issue: AgentIssue, repo_root: Path) -> Path | None:
    """Extract and validate the prompt file path from an issue's body.

    Three reference formats are tried in priority order:

    1. **Work Order** (highest priority)::

           WorkOrder: docs/work-orders/<name>.md
           Brief:     docs/briefs/<name>.md   (backward compat)

    2. **OpenSpec**::

           OpenSpec: openspec/changes/<name>/

       Resolves to ``tasks.md`` inside the directory, falling back to
       ``proposal.md``.

    3. **Speckit**::

           Speckit: specs/<NNN>-<name>/

       Resolves to ``tasks.md`` inside the directory, falling back to
       ``spec.md``.

    All formats are matched case-insensitively. All resolved paths are
    validated with a ``relative_to`` guard to prevent directory traversal.

    Args:
        issue:     The issue whose body is parsed.
        repo_root: Absolute path to the repository root.

    Returns:
        Absolute :class:`Path` to the prompt file if found and safe,
        ``None`` otherwise.
    """
    # --- Work Order ---
    wo_match = _WORK_ORDER_RE.search(issue.body)
    if wo_match:
        rel_path = wo_match.group(1)
        work_order_path = (repo_root / rel_path).resolve()
        work_orders_dir = (repo_root / "docs" / "work-orders").resolve()
        briefs_dir = (repo_root / "docs" / "briefs").resolve()
        # Accept both docs/work-orders/ and docs/briefs/ (backward compat).
        try:
            work_order_path.relative_to(work_orders_dir)
        except ValueError:
            try:
                work_order_path.relative_to(briefs_dir)
            except ValueError:
                log.error("Work order path %r escapes docs/work-orders/ and docs/briefs/ — refusing", rel_path)
                return None
        if not _exists_on_main_or_disk(repo_root, work_order_path):
            log.error("Work order not found on disk or at origin/main: %s", work_order_path)
            return None
        return work_order_path

    # --- OpenSpec ---
    openspec_match = _OPENSPEC_RE.search(issue.body)
    if openspec_match:
        return _resolve_dir_source(
            rel_dir=openspec_match.group(1),
            allowed_root=(repo_root / "openspec" / "changes").resolve(),
            primary_file="tasks.md",
            fallback_file="proposal.md",
            source_label="OpenSpec",
            issue_number=issue.number,
            repo_root=repo_root,
        )

    # --- Speckit ---
    speckit_match = _SPECKIT_RE.search(issue.body)
    if speckit_match:
        return _resolve_dir_source(
            rel_dir=speckit_match.group(1),
            allowed_root=(repo_root / "specs").resolve(),
            primary_file="tasks.md",
            fallback_file="spec.md",
            source_label="Speckit",
            issue_number=issue.number,
            repo_root=repo_root,
        )

    log.error(
        "Issue #%d body contains no prompt source reference "
        "(expected 'WorkOrder: docs/work-orders/<name>.md', "
        "'Brief: docs/briefs/<name>.md', "
        "'OpenSpec: openspec/changes/<name>/', or "
        "'Speckit: specs/<NNN>-<name>/')",
        issue.number,
    )
    return None


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


def create_worktree(branch: str, repo_root: Path, issue_number: int) -> Path | None:
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
    base_ref = "origin/main"
    # git fetch and git worktree add both mutate shared .git state in
    # repo_root; concurrent batch threads (max_concurrent > 1) racing here
    # hit git's ref/worktree lock files and fail spuriously — which would
    # burn an attempt on the issue. Serialize creation; the long-running
    # OpenCode sessions themselves still run concurrently.
    with _WORKTREE_CREATE_LOCK:
        try:
            subprocess.run(
                ["git", "fetch", "origin", "main"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=True,
                timeout=_GIT_TIMEOUT,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            stderr = getattr(exc, "stderr", "") or ""
            log.error("Failed to fetch %s before creating worktree: %s", base_ref, stderr.strip() or exc)
            return None

        ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        worktree_path = _worktrees_root() / f"issue-{issue_number}-{ts}"
        try:
            worktree_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.error("Failed to create worktree root %s: %s", worktree_path.parent, exc)
            return None

        try:
            subprocess.run(
                ["git", "worktree", "add", "--force", "-B", branch, str(worktree_path), base_ref],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=True,
                timeout=_GIT_TIMEOUT,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            stderr = getattr(exc, "stderr", "") or ""
            log.error("Failed to create worktree for %s at %s: %s", branch, worktree_path, stderr.strip() or exc)
            # Best-effort cleanup of a partially-created worktree.
            remove_worktree(worktree_path, repo_root)
            return None

    log.info("Created worktree for %s at %s (base %s)", branch, worktree_path, base_ref)
    return worktree_path


def remove_worktree(worktree_path: Path, repo_root: Path) -> None:
    """Tear down an ephemeral driver worktree.  Best-effort; never raises."""
    try:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("git worktree remove failed for %s: %s", worktree_path, exc)
    try:
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("git worktree prune failed: %s", exc)


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

RunOutcome = Literal["ok", "failed", "skipped", "dry-run"]


@dataclass
class RunResult:
    issue_number: int | None
    issue_title: str
    outcome: RunOutcome
    branch: str
    elapsed_seconds: float
    spend_note: str
    error: str


def _write_run_status(result: RunResult) -> None:
    """Append *result* to ``<fieldkit_data>/logs/driver/driver-run-status.json``."""
    try:
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

    except OSError as exc:
        log.warning("Could not write driver-run-status.json: %s", exc)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _resolve_run_work_order(work_order_path: Path, repo_root: Path, worktree_path: Path | None) -> Path:
    """Point the agent at the worktree's own copy of the work order.

    The work order is committed on main, so it is present in a fresh worktree;
    using that copy keeps the session operating entirely inside its isolation.
    Falls back to the original path on dry-run (no worktree) or if the expected
    copy is missing.
    """
    if worktree_path is None:
        return work_order_path
    candidate = worktree_path / work_order_path.relative_to(repo_root)
    if candidate.exists():
        return candidate
    return work_order_path


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
                return _result("failed", finalized.detail)
            transition_to_succeeded(repo, issue, attempt=attempt)
            comment_on_issue(
                repo,
                issue.number,
                f"✅ **Driver succeeded** — branch `{branch}` pushed, PR opened.",
            )
        return _result("dry-run" if dry_run else "ok", "")

    error_msg = oc_outcome.reason or f"OpenCode failed for issue #{issue.number}"
    if oc_outcome.status == "rate_limited":
        # Provider starvation still charges the reserved attempt; GitHub labels
        # remain unchanged and the idempotent comment exposes the condition.
        log.warning("Issue #%d: %s — skipping after a reserved attempt", issue.number, error_msg)
        if not dry_run:
            finalized = complete_attempt(
                repo, issue.number, succeeded=False, outcome=error_msg, data_root=get_fieldkit_data()
            )
            if not finalized.allowed:
                return _result("failed", finalized.detail)
        _notify_rate_limited(repo, issue.number, error_msg, dry_run)
        return _result("skipped", error_msg)

    if not dry_run:
        finalized = complete_attempt(
            repo, issue.number, succeeded=False, outcome=error_msg, data_root=get_fieldkit_data()
        )
        if not finalized.allowed:
            return _result("failed", finalized.detail)
        transition_to_failed(repo, issue, attempt=attempt, error=error_msg)
    return _result("failed", error_msg)


def _run_attempt_lifecycle(
    repo: str,
    issue: AgentIssue,
    work_order_path: Path,
    repo_root: Path,
    started_at: datetime,
    run_started_monotonic: float,
    attempt: int,
    branch: str,
    worktree_path: Path | None,
    *,
    dry_run: bool,
) -> RunResult:
    snapshot = None
    try:
        work_dir = worktree_path if worktree_path is not None else repo_root
        run_work_order = _resolve_run_work_order(work_order_path, repo_root, worktree_path)
        data_dir = _isolate_data_dir(worktree_path) if worktree_path is not None else None
        llm_log_path = reserve_run_db_path(issue.number) if not dry_run else None
        prior_failure = last_failure_comment(repo, issue.number) if attempt > 1 and not dry_run else ""
        if not dry_run:
            snapshot = create_trusted_snapshot(
                repo_root,
                work_order_path,
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
            prior_failure=prior_failure,
        )
        spend_note = get_spend_summary(issue.number, llm_log_path) if not dry_run else ""

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
        )
    except (VerificationError, OSError, subprocess.SubprocessError) as exc:
        return _finalize_run_outcome(
            repo,
            issue,
            attempt=attempt,
            branch=branch,
            oc_outcome=OpencodeOutcome(status="failed", reason=f"Independent verification setup failed: {exc}"),
            started_at=started_at,
            spend_note="",
            dry_run=dry_run,
        )
    except AuthError as exc:
        if not dry_run:
            finalized = complete_attempt(
                repo,
                issue.number,
                succeeded=False,
                outcome="authentication failed during independent verification",
                data_root=get_fieldkit_data(),
            )
            if not finalized.allowed:
                raise AuthError(f"{exc}; local attempt finalization failed: {finalized.detail}") from exc
        raise
    finally:
        # Always tear down the worktree — the branch is pushed to origin for
        # the PR, so only the local checkout needs removing.
        if worktree_path is not None:
            remove_worktree(worktree_path, repo_root)
        if snapshot is not None and not remove_snapshot(snapshot):
            log.error("Trusted snapshot cleanup failed: %s", snapshot.snapshot_dir)


def _execute_one(
    repo: str,
    issue: AgentIssue,
    work_order_path: Path,
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
        else reserve_attempt(repo, issue.number, issue.labels, data_root=get_fieldkit_data())
    )
    if not reservation.allowed or reservation.attempt is None:
        return RunResult(
            issue_number=issue.number,
            issue_title=issue.title,
            outcome="skipped",
            branch="",
            elapsed_seconds=_elapsed(),
            spend_note="",
            error=reservation.detail,
        )
    attempt = reservation.attempt
    branch = make_branch_name(issue)
    worktree_path: Path | None = None
    if not dry_run:
        worktree_path = create_worktree(branch, repo_root, issue.number)
        if worktree_path is None:
            error_msg = f"Could not create worktree for {branch}"
            finalized = complete_attempt(
                repo, issue.number, succeeded=False, outcome=error_msg, data_root=get_fieldkit_data()
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
        work_order_path,
        repo_root,
        started_at,
        run_started_monotonic,
        attempt,
        branch,
        worktree_path,
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
        The last completed run; every run also gets its own status-file entry.
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
            return _skipped(f"{prefix}: {spend_check.detail}")

    repo = get_github_repo()
    # Refresh origin/main so a work order the foreman merged this hour resolves;
    # otherwise a freshly-authored issue whose agent-ready was flipped near the
    # merge would be seen as "no work order" against a stale checkout.
    if not dry_run:
        _fetch_main(repo_root)
    issues = list_ready_issues(repo)

    if not issues:
        log.info("No agent-ready issues found — nothing to do")
        return _skipped("")

    # Resolve work orders up front — issues without a reference are delabeled
    # and skipped (they don't burn an attempt), the rest become candidates.
    candidates: list[tuple[AgentIssue, Path]] = []
    for issue in issues:
        work_order_path = resolve_prompt_source(issue, repo_root)
        if work_order_path is None:
            if _has_prompt_reference(issue.body):
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
                        "wrong or the file was never merged — check the work-order path in "
                        "the issue body and the foreman run.",
                    )
                _write_run_status(
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
                    "Removing `agent-ready`; re-run the foreman to author a work order.",
                )
                remove_label(repo, issue.number, LABEL_READY)
            _write_run_status(
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
            continue
        if not dry_run:
            retry_decision = check_eligibility(repo, issue.number, issue.labels, data_root=get_fieldkit_data())
            if not retry_decision.allowed:
                log.warning("Issue #%d skipped by local retry state: %s", issue.number, retry_decision.detail)
                continue
        candidates.append((issue, work_order_path))

    if not candidates:
        executed = _skipped("All queued issues lacked a work order reference")
        _write_run_status(executed)
        return executed

    # Covers-as-locks selection: compute the busy set (files changed by open
    # PRs targeting main) and the open dependencies, then pick a batch of
    # pairwise-disjoint candidates. Busy-set failure fails closed.
    try:
        busy = busy_files(repo)
    except SchedulerError as exc:
        log.error("Could not compute busy set — failing closed, running nothing: %s", exc)
        executed = _skipped(f"Busy-set lookup failed (fail closed): {exc}")
        _write_run_status(executed)
        return executed

    declared_deps: set[int] = set()
    for _, work_order_path in candidates:
        declared_deps.update(parse_depends_on(work_order_path))
    open_deps = open_issue_numbers(repo, declared_deps) if declared_deps else frozenset()

    max_concurrent = cap_safe_max_concurrent(
        get_driver_max_concurrent(), dry_run=dry_run, raw_cap=os.environ.get("FIELDKIT_DRIVER_SPEND_CAP")
    )
    selected, skips = select_runnable(candidates, busy, open_deps, max_concurrent)
    for skip in skips:
        log.info("Skipping issue %s", skip)

    if not selected:
        summary = "; ".join(str(skip) for skip in skips)
        executed = _skipped(f"All candidates blocked: {summary}")
        _write_run_status(executed)
        return executed

    # Execute the batch — plain call for one issue, threads for more (the
    # payload is subprocess.run, which releases the GIL).
    if len(selected) == 1:
        issue, work_order_path = selected[0]
        results = [
            _execute_one(
                repo,
                issue,
                work_order_path,
                repo_root,
                start,
                run_started_monotonic,
                dry_run=dry_run,
            )
        ]
    else:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=len(selected)) as pool:
            futures = [
                pool.submit(
                    _execute_one,
                    repo,
                    issue,
                    work_order_path,
                    repo_root,
                    start,
                    run_started_monotonic,
                    dry_run=dry_run,
                )
                for issue, work_order_path in selected
            ]
            results = [future.result() for future in futures]

    for result in results:
        _write_run_status(result)
    return results[-1]
