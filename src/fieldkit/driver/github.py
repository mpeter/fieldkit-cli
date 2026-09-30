"""fieldkit.driver.github — GitHub label management for the driver loop.

Provides thin wrappers over ``gh`` CLI for the three label transitions the
driver uses to process work orders:

    agent-ready  →  (executing)  →  agent-failed / (PR open, label removed)

Label constants are defined here so both the runner and the CLI adapter share
them without importing from each other.
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Literal

from fieldkit.config._timeouts import TIMEOUT_GH_CLI, TIMEOUT_PROCESS_KILL_GRACE
from fieldkit.errors import AuthError, GitHubRequestError
from fieldkit.util.bounded_process import BoundedProcessError, run_bounded_process

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Label names — the consent-bit and outcome labels
# ---------------------------------------------------------------------------

LABEL_READY: str = "agent-ready"
LABEL_FAILED: str = "agent-failed"
LABEL_BLOCKED: str = "agent-blocked"

MAX_ATTEMPTS: int = 3

_GH_TIMEOUT: int = 30  # gh CLI API calls
_MAX_QUEUE_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_IDENTITY_RESPONSE_BYTES = 1024 * 1024


class GitHubLookupError(RuntimeError):
    """A required, read-only GitHub identity lookup failed closed."""


@dataclass(frozen=True)
class PullRequestIdentity:
    """The immutable GitHub identity fields used by independent verification."""

    number: int
    repository: str
    repository_id: int
    base_branch: str
    head_branch: str
    head_sha: str


def github_read_failure_kind(stderr: str) -> Literal["authentication", "not-found", "provider"]:
    """Classify a failed GitHub read without retaining provider payloads."""
    lowered = stderr.casefold()
    if any(marker in lowered for marker in ("rate limit", "too many requests", "http 429")):
        return "provider"
    if any(marker in lowered for marker in ("authentication", "authenticate", "not logged", "http 401", "http 403")):
        return "authentication"
    if "http 404" in lowered or "not found" in lowered:
        return "not-found"
    return "provider"


def _raise_lookup_failure(stderr: str) -> None:
    if github_read_failure_kind(stderr) == "authentication":
        raise AuthError("GitHub authentication failed during required identity lookup")
    raise GitHubLookupError("GitHub required identity lookup failed")


def _run_identity_lookup(argv: list[str], timeout: float) -> str:
    try:
        result = run_bounded_process(
            argv,
            timeout=timeout,
            stdout_limit=_MAX_IDENTITY_RESPONSE_BYTES,
            stderr_limit=_MAX_IDENTITY_RESPONSE_BYTES,
            cleanup_timeout=TIMEOUT_PROCESS_KILL_GRACE,
        )
    except BoundedProcessError as exc:
        raise GitHubLookupError("GitHub required identity lookup did not complete") from exc
    if result.returncode != 0:
        _raise_lookup_failure(result.stderr)
    return result.stdout


def _parse_pr_identity(payload: str, repo: str, branch: str) -> tuple[int, str, str, str]:
    try:
        decoded: object = json.loads(payload)
    except (json.JSONDecodeError, TypeError) as exc:
        raise GitHubLookupError("GitHub returned malformed pull-request JSON") from exc
    if not isinstance(decoded, list) or any(not isinstance(row, dict) for row in decoded):
        raise GitHubLookupError("GitHub returned malformed pull-request JSON")
    rows: list[dict[str, object]] = decoded
    if len(rows) != 1:
        raise GitHubLookupError(f"expected exactly one open pull request for {branch!r}, found {len(rows)}")
    row = rows[0]
    number = row.get("number")
    base = row.get("baseRefName")
    head = row.get("headRefName")
    sha = row.get("headRefOid")
    head_repository = row.get("headRepository")
    head_owner = row.get("headRepositoryOwner")
    expected_owner, _, expected_name = repo.partition("/")
    if (
        isinstance(number, bool)
        or not isinstance(number, int)
        or number <= 0
        or not isinstance(base, str)
        or not isinstance(head, str)
        or not isinstance(sha, str)
        or len(sha) != 40
        or any(character not in "0123456789abcdefABCDEF" for character in sha)
        or not isinstance(head_repository, dict)
        or head_repository.get("name") != expected_name
        or not isinstance(head_owner, dict)
        or head_owner.get("login") != expected_owner
    ):
        raise GitHubLookupError("GitHub returned malformed pull-request identity fields")
    return number, base, head, sha.lower()


def _repository_id(repo: str, timeout: float) -> int:
    payload = _run_identity_lookup(["gh", "api", f"repos/{repo}", "--jq", ".id"], timeout)
    try:
        repository_id = int(payload.strip())
    except ValueError as exc:
        raise GitHubLookupError("GitHub returned a malformed repository ID") from exc
    if repository_id <= 0:
        raise GitHubLookupError("GitHub returned a malformed repository ID")
    return repository_id


def get_pr_identity(repo: str, branch: str, *, timeout: float = TIMEOUT_GH_CLI) -> PullRequestIdentity:
    """Return the sole open pull request for *branch*, or fail closed."""
    deadline = time.monotonic() + timeout
    payload = _run_identity_lookup(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "number,headRefName,headRefOid,baseRefName,headRepository,headRepositoryOwner",
        ],
        timeout,
    )
    number, base, head, sha = _parse_pr_identity(payload, repo, branch)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise GitHubLookupError("GitHub identity lookup timed out")
    repository_id = _repository_id(repo, remaining)
    return PullRequestIdentity(number, repo, repository_id, base, head, sha.lower())


@dataclass(frozen=True)
class AgentIssue:
    """A GitHub issue that carries the ``agent-ready`` label."""

    number: int
    title: str
    body: str
    labels: list[str]

    @property
    def attempt_count(self) -> int:
        """Return the current attempt count recorded in the issue labels.

        Labels of the form ``attempt:N`` are set by the driver on each run.
        Returns 0 if no such label is found.
        """
        for lbl in self.labels:
            if lbl.startswith("attempt:"):
                try:
                    return int(lbl.split(":", 1)[1])
                except ValueError:
                    pass
        return 0


def _fetch_issues_by_label(repo: str, label: str) -> list[AgentIssue]:
    """Return open issues carrying *label*, failing closed when the source is unavailable."""
    try:
        result = run_bounded_process(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                repo,
                "--label",
                label,
                "--state",
                "open",
                "--json",
                "number,title,body,labels",
                "--limit",
                "50",
            ],
            timeout=_GH_TIMEOUT,
            stdout_limit=_MAX_QUEUE_RESPONSE_BYTES,
            stderr_limit=_MAX_IDENTITY_RESPONSE_BYTES,
            cleanup_timeout=TIMEOUT_PROCESS_KILL_GRACE,
        )
    except BoundedProcessError as exc:
        raise GitHubRequestError("GitHub issue queue is unavailable") from exc
    if result.returncode != 0:
        stderr = result.stderr
        if github_read_failure_kind(stderr) == "authentication":
            raise AuthError("GitHub issue queue authentication failed")
        raise GitHubRequestError("GitHub issue queue is unavailable")

    try:
        if not isinstance(result.stdout, str):
            raise ValueError("response must be text")
        raw: object = json.loads(result.stdout)
        if not isinstance(raw, list):
            raise ValueError("response must be a list")
        issues: list[AgentIssue] = []
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("issue must be an object")
            number = item.get("number")
            title = item.get("title")
            body = item.get("body")
            labels = item.get("labels", [])
            if (
                isinstance(number, bool)
                or not isinstance(number, int)
                or number < 1
                or not isinstance(title, str)
                or (body is not None and not isinstance(body, str))
                or not isinstance(labels, list)
            ):
                raise ValueError("issue fields are invalid")
            label_names: list[str] = []
            for raw_label in labels:
                if not isinstance(raw_label, dict) or not isinstance(raw_label.get("name"), str):
                    raise ValueError("issue label is invalid")
                label_names.append(raw_label["name"])
            issues.append(AgentIssue(number, title, body or "", label_names))
        return issues
    except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
        raise GitHubRequestError("GitHub issue queue response is invalid") from exc


def list_ready_issues(repo: str) -> list[AgentIssue]:
    """Return open issues the driver may run this tick, oldest first.

    Two sources, unioned and de-duplicated by issue number:

    * **Fresh** — issues labeled ``agent-ready`` that carry neither
      ``agent-failed`` nor ``agent-blocked``. Excluding ``agent-failed`` here
      also covers the partial-transition case (both ``agent-ready`` and
      ``agent-failed`` present because a prior ``transition_to_failed`` was
      interrupted mid-way); such an issue is instead picked up by the retry
      source below while it is under the attempt cap.
    * **Retry** — issues labeled ``agent-failed`` (but not ``agent-blocked``).
      The local retry ledger owns the attempt cap, so this source intentionally
      does not filter on potentially stale GitHub attempt labels.

    Args:
        repo: GitHub repository in ``owner/name`` format.

    Returns:
        List of :class:`AgentIssue` ordered by issue number ascending
        (oldest first, approximating creation order).
    """
    fresh = [
        i
        for i in _fetch_issues_by_label(repo, LABEL_READY)
        if LABEL_FAILED not in i.labels and LABEL_BLOCKED not in i.labels
    ]
    retry = [i for i in _fetch_issues_by_label(repo, LABEL_FAILED) if LABEL_BLOCKED not in i.labels]
    # Union by number; a fresh entry wins over a retry duplicate (identical
    # AgentIssue, but fresh reflects the label set the operator last intended).
    merged: dict[int, AgentIssue] = {i.number: i for i in fresh}
    for issue in retry:
        merged.setdefault(issue.number, issue)
    return sorted(merged.values(), key=lambda i: i.number)


def add_label(repo: str, issue_number: int, label: str) -> bool:
    """Add *label* to an issue. Returns True on success."""
    succeeded = _run_gh_mutation(["gh", "issue", "edit", str(issue_number), "--repo", repo, "--add-label", label])
    if not succeeded:
        log.error("add_label(%d) failed", issue_number)
    return succeeded


def remove_label(repo: str, issue_number: int, label: str) -> bool:
    """Remove *label* from an issue. Returns True on success."""
    succeeded = _run_gh_mutation(["gh", "issue", "edit", str(issue_number), "--repo", repo, "--remove-label", label])
    if not succeeded:
        log.error("remove_label(%d) failed", issue_number)
    return succeeded


def comment_on_issue(repo: str, issue_number: int, body: str) -> bool:
    """Post a comment on a GitHub issue.  Best-effort; returns False on failure."""
    succeeded = _run_gh_mutation(["gh", "issue", "comment", str(issue_number), "--repo", repo, "--body", body])
    if not succeeded:
        log.error("comment_on_issue(%d) failed", issue_number)
    return succeeded


def _run_gh_mutation(argv: list[str]) -> bool:
    """Run one bounded gh mutation without promoting provider payloads."""
    try:
        result = run_bounded_process(
            argv,
            timeout=_GH_TIMEOUT,
            stdout_limit=_MAX_IDENTITY_RESPONSE_BYTES,
            stderr_limit=_MAX_IDENTITY_RESPONSE_BYTES,
            cleanup_timeout=TIMEOUT_PROCESS_KILL_GRACE,
        )
    except BoundedProcessError:
        return False
    return result.returncode == 0


def comment_once(repo: str, issue_number: int, marker: str, body: str) -> bool:
    """Post *body* only if no existing comment starts with *marker*.

    Idempotent commenting for a condition that recurs every tick (e.g. a
    referenced work order that has not landed yet) — without this the driver
    would append an identical comment on every hourly pass. Best-effort: if the
    existing-comment lookup fails, does **not** post, so a transient ``gh``
    error cannot turn into comment spam.

    Returns True only when a new comment was actually posted.
    """
    try:
        result = run_bounded_process(
            [
                "gh",
                "issue",
                "view",
                str(issue_number),
                "--repo",
                repo,
                "--json",
                "comments",
                "--jq",
                f".comments | map(select(.body | startswith({json.dumps(marker)}))) | length",
            ],
            timeout=_GH_TIMEOUT,
            stdout_limit=_MAX_IDENTITY_RESPONSE_BYTES,
            stderr_limit=_MAX_IDENTITY_RESPONSE_BYTES,
            cleanup_timeout=TIMEOUT_PROCESS_KILL_GRACE,
        )
    except BoundedProcessError:
        log.error("comment_once lookup failed for #%d", issue_number)
        return False
    if result.returncode != 0:
        log.error("comment_once lookup failed for #%d", issue_number)
        return False
    if result.stdout.strip() not in ("", "0"):
        return False  # a marker comment already exists — stay quiet
    return comment_on_issue(repo, issue_number, body)


def transition_to_failed(repo: str, issue: AgentIssue, *, attempt: int, error: str = "") -> None:
    """Move an issue from ``agent-ready`` to ``agent-failed``.

    Projects the locally-reserved ``attempt:N`` label.  The local retry ledger
    remains authoritative when these best-effort mutations fail.

    Posts a comment on the issue with the failure reason so the operator
    (and future retry attempts) have context without reading journald.

    Label operations use add-before-remove ordering: if an intermediate
    call fails, the issue retains its old labels and remains visible.

    Args:
        repo:  GitHub repo in ``owner/name`` format.
        issue: The issue that failed.
        error: Human-readable failure reason (posted as a comment).
    """
    add_label(repo, issue.number, f"attempt:{attempt}")

    if attempt >= MAX_ATTEMPTS:
        log.warning(
            "Issue #%d reached max attempts (%d) — projecting %s",
            issue.number,
            MAX_ATTEMPTS,
            LABEL_BLOCKED,
        )
        add_label(repo, issue.number, LABEL_BLOCKED)
        # Now remove old labels
        remove_label(repo, issue.number, LABEL_READY)
        remove_label(repo, issue.number, LABEL_FAILED)
        if error:
            note = "This issue needs operator intervention — the local driver retry ledger will not retry it."
            comment_on_issue(
                repo,
                issue.number,
                f"🚫 **Driver blocked** (attempt {attempt}/{MAX_ATTEMPTS})\n\n```\n{error}\n```\n\n{note}",
            )
    else:
        add_label(repo, issue.number, LABEL_FAILED)
        # Now remove old labels
        remove_label(repo, issue.number, LABEL_READY)
        log.info("Issue #%d marked %s (attempt %d/%d)", issue.number, LABEL_FAILED, attempt, MAX_ATTEMPTS)
        if error:
            comment_on_issue(
                repo,
                issue.number,
                f"⚠️ **Driver failed** (attempt {attempt}/{MAX_ATTEMPTS})\n\n"
                f"```\n{error}\n```\n\n"
                f"Will retry on next hourly tick ({MAX_ATTEMPTS - attempt} attempt(s) remaining).",
            )

    # Remove old attempt label last (safe: if this fails, we have both old and new — next run handles it)
    for lbl in issue.labels:
        if lbl.startswith("attempt:"):
            remove_label(repo, issue.number, lbl)


def transition_to_succeeded(repo: str, issue: AgentIssue, *, attempt: int) -> None:
    """Remove ``agent-ready`` after a successful run (PR opened).

    Leaves any ``attempt:N`` labels for audit trail.

    Args:
        repo:  GitHub repo in ``owner/name`` format.
        issue: The issue that succeeded.
    """
    remove_label(repo, issue.number, LABEL_READY)
    remove_label(repo, issue.number, LABEL_FAILED)
    log.info("Issue #%d: agent-ready removed after local attempt %d — PR opened successfully", issue.number, attempt)
