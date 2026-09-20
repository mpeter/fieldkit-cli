"""fieldkit.driver.github — GitHub label management for the driver loop.

Provides thin wrappers over ``gh`` CLI for the three label transitions the
driver uses to process work orders:

    agent-ready  →  (executing)  →  agent-failed / (PR open, label removed)

Label constants are defined here so both the runner and the CLI adapter share
them without importing from each other.
"""

import json
import logging
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from fieldkit.config._timeouts import TIMEOUT_GH_CLI
from fieldkit.errors import AuthError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Label names — the consent-bit and outcome labels
# ---------------------------------------------------------------------------

LABEL_READY: str = "agent-ready"
LABEL_FAILED: str = "agent-failed"
LABEL_BLOCKED: str = "agent-blocked"

MAX_ATTEMPTS: int = 3

_GH_TIMEOUT: int = 30  # gh CLI API calls


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


def _raise_lookup_failure(stderr: str) -> None:
    detail = stderr.strip() or "GitHub lookup failed"
    lowered = detail.lower()
    if any(marker in lowered for marker in ("authentication", "authenticate", "not logged", "http 401", "http 403")):
        raise AuthError(detail)
    raise GitHubLookupError(detail)


def _run_identity_lookup(argv: list[str], timeout: float) -> str:
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitHubLookupError(str(exc)) from exc
    if result.returncode != 0:
        _raise_lookup_failure(result.stderr)
    return result.stdout


def _parse_pr_identity(payload: str, repo: str, branch: str) -> tuple[int, str, str, str]:
    try:
        rows: list[dict[str, object]] = json.loads(payload)
    except (json.JSONDecodeError, TypeError) as exc:
        raise GitHubLookupError("GitHub returned malformed pull-request JSON") from exc
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
        return int(payload.strip())
    except ValueError as exc:
        raise GitHubLookupError("GitHub returned a malformed repository ID") from exc


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


def last_failure_comment(repo: str, issue_number: int) -> str:
    """Return the most recent driver-failure comment, or an empty string."""
    try:
        result = subprocess.run(
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
                '.comments | map(select(.body | startswith("⚠️ **Driver failed") or startswith("🚫 **Driver blocked"))) | last | .body // ""',
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=_GH_TIMEOUT,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""


def _fetch_issues_by_label(repo: str, label: str) -> list[AgentIssue]:
    """Return open issues carrying *label*, or ``[]`` on any gh/parse failure."""
    try:
        result = subprocess.run(
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
            capture_output=True,
            text=True,
            check=True,
            timeout=_GH_TIMEOUT,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        log.error("gh issue list --label %s failed: %s", label, stderr.strip() or exc)
        return []

    try:
        raw: list[dict[str, Any]] = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        log.error("Failed to parse gh output: %s", exc)
        return []

    return [
        AgentIssue(
            number=item["number"],
            title=item["title"],
            body=item.get("body") or "",
            labels=[lbl["name"] for lbl in item.get("labels", [])],
        )
        for item in raw
    ]


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
    try:
        subprocess.run(
            ["gh", "issue", "edit", str(issue_number), "--repo", repo, "--add-label", label],
            capture_output=True,
            text=True,
            check=True,
            timeout=_GH_TIMEOUT,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        log.error("add_label(%d, %r) failed: %s", issue_number, label, stderr.strip() or exc)
        return False


def remove_label(repo: str, issue_number: int, label: str) -> bool:
    """Remove *label* from an issue. Returns True on success."""
    try:
        subprocess.run(
            ["gh", "issue", "edit", str(issue_number), "--repo", repo, "--remove-label", label],
            capture_output=True,
            text=True,
            check=True,
            timeout=_GH_TIMEOUT,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        log.error("remove_label(%d, %r) failed: %s", issue_number, label, stderr.strip() or exc)
        return False


def comment_on_issue(repo: str, issue_number: int, body: str) -> bool:
    """Post a comment on a GitHub issue.  Best-effort; returns False on failure."""
    try:
        subprocess.run(
            ["gh", "issue", "comment", str(issue_number), "--repo", repo, "--body", body],
            capture_output=True,
            text=True,
            check=True,
            timeout=_GH_TIMEOUT,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        log.error("comment_on_issue(%d) failed: %s", issue_number, stderr.strip() or exc)
        return False


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
        result = subprocess.run(
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
            capture_output=True,
            text=True,
            check=True,
            timeout=_GH_TIMEOUT,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        log.error("comment_once lookup failed for #%d: %s", issue_number, stderr.strip() or exc)
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
