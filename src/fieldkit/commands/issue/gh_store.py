"""GitHub Issues backend for fieldkit issue commands.

Replaces the local markdown file store (store.py) with a GitHub Issues
API backend via the ``gh`` CLI. All operations map to ``gh issue`` and
``gh api`` subcommands — no direct HTTP calls, no extra dependencies.

Public surface mirrors the old IssueStore interface so cli.py needs
minimal changes.
"""

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from fieldkit.config import TIMEOUT_GH_CLI

logger = logging.getLogger(__name__)

IssueType = Literal["bug", "enhancement"]
IssueSeverity = Literal["low", "medium", "high", "critical"]
IssueStatus = Literal["open", "planned", "fixed", "closed", "wont-fix"]

_KNOWN_MODULES = {
    "auth",
    "companion",
    "config",
    "contact",
    "doctor",
    "driver",
    "sf",
    "gmail",
    "health",
    "ingest",
    "enrich",
    "meeting",
    "watch",
    "web",
    "pursuit",
    "skill",
    "init",
    "shadowbot",
    "docs",
    "brief",
    "pipeline",
    "lib",
    "hooks",
    "cli",
    "issue",
    "other",
    "version",
    "sync",
}

# GitHub state_reason values
_REASON_COMPLETED = "completed"
_REASON_NOT_PLANNED = "not planned"

# Repo label used purely as a small durable key-value store: its
# ``description`` field holds the highest public issue number ever allocated.
# See GHIssueStore._get_watermark / _set_watermark.
_WATERMARK_LABEL = "fieldkit-id-watermark"

_SEVERITY_RANK: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# ---------------------------------------------------------------------------
# GHIssue dataclass
# ---------------------------------------------------------------------------


@dataclass
class GHIssue:
    """A fieldkit issue backed by a GitHub Issue."""

    id: str  # "fieldkit-NNN"
    type: IssueType
    title: str
    status: IssueStatus
    severity: IssueSeverity
    module: str
    gh_number: int  # GitHub issue number
    body: str = ""
    source: str = "unknown"
    created: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def severity_rank(self) -> int:
        return _SEVERITY_RANK.get(self.severity, 99)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _gh(*args: str) -> str:
    """Run ``gh`` CLI and return stdout. Raises RuntimeError on failure."""
    result = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=TIMEOUT_GH_CLI,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        # Detect rate limits for actionable error messages.
        if "rate limit" in stderr.lower() or "403" in stderr or result.returncode == 403:
            raise RuntimeError(
                f"GitHub API rate limit exceeded. Check remaining quota with `gh api rate_limit`. "
                f"Command: gh {' '.join(args[:4])}"
            )
        raise RuntimeError(f"gh {' '.join(args[:4])} failed: {stderr}")
    return result.stdout.strip()


def _gh_json(*args: str) -> Any:
    """Run ``gh`` CLI, parse JSON output.

    Returns:
        Parsed JSON value (list, dict, etc.). Returns [] when output is empty.

    Raises:
        RuntimeError: if ``gh`` exits non-zero (via ``_gh``) or returns invalid JSON.
            The message includes the raw payload length for diagnostics (implementation note).
    """
    raw = _gh(*args)
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gh CLI returned invalid JSON (len={len(raw)}): {exc}") from exc


def _labels_from_issue(issue_data: dict[str, Any]) -> list[str]:
    return [lbl["name"] for lbl in issue_data.get("labels", [])]


def _status_from_github(state: str, state_reason: str | None, labels: list[str]) -> IssueStatus:
    """Derive fieldkit IssueStatus from GitHub state + state_reason + labels.

    GitHub returns state as uppercase ("OPEN", "CLOSED") in JSON responses.
    Compare case-insensitively to guard against any future casing changes.
    """
    if state.upper() == "OPEN":
        if "status:planned" in labels:
            return "planned"
        return "open"
    # state is "CLOSED" (or any non-open value)
    if state_reason == "not_planned":
        return "wont-fix"
    if "status:fixed" in labels:
        return "fixed"
    return "closed"


def _parse_fieldkit_id(title: str) -> str | None:
    """Extract a lowercase public fieldkit ID from a GitHub issue title."""
    m = re.match(r"^(fieldkit-\d+):", title)
    return m.group(0)[:-1] if m else None  # strip trailing ":"


def _parse_module(labels: list[str]) -> str:
    for lbl in labels:
        if lbl.startswith("module:"):
            return lbl[len("module:") :]
    return "other"


def _parse_severity(labels: list[str]) -> IssueSeverity:
    for lbl in labels:
        if lbl.startswith("severity:"):
            val = lbl[len("severity:") :]
            if val in ("low", "medium", "high", "critical"):
                return val  # type: ignore[return-value]
    return "medium"


def _parse_type(labels: list[str]) -> IssueType | None:
    if "bug" in labels:
        return "bug"
    if "enhancement" in labels:
        return "enhancement"
    return None


def _parse_issue_identity(title: str, labels: list[str]) -> tuple[str, IssueType] | None:
    """Return the public identifier and label-defined type for a tracked issue."""
    fieldkit_id = _parse_fieldkit_id(title)
    issue_type = _parse_type(labels)
    if fieldkit_id is None or issue_type is None:
        return None
    return fieldkit_id, issue_type


def _parse_source(body: str) -> str:
    """Extract source from the metadata header in the issue body."""
    m = re.search(r"\*\*Source:\*\*\s*([^\s|]+)", body or "")
    return m.group(1).strip() if m else "unknown"


def _parse_created(body: str, gh_created: str) -> datetime:
    """Try to parse created date from the body metadata line; fall back to GH date."""
    m = re.search(r"\*\*Created:\*\*\s*(\d{4}-\d{2}-\d{2})", body or "")
    if m:
        try:
            return datetime.fromisoformat(m.group(1)).replace(tzinfo=UTC)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(gh_created.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.now(UTC)


def _issue_from_gh(data: dict[str, Any]) -> GHIssue | None:
    """Convert a GitHub Issues API response dict to a GHIssue. Returns None if not a fieldkit issue."""
    title = data.get("title", "")
    labels = _labels_from_issue(data)
    identity = _parse_issue_identity(title, labels)
    if identity is None:
        return None
    fk_id, issue_type = identity
    state = data.get("state", "open")
    state_reason = data.get("stateReason") or data.get("state_reason")
    status = _status_from_github(state, state_reason, labels)
    body = data.get("body", "") or ""
    # Strip the migration footer from body display
    clean_body = re.split(r"\n---\n\*Migrated from", body)[0].strip()
    # Also strip the metadata blockquote line at the top
    clean_body = re.sub(r"^> \*\*Source:\*\*.*\n\n", "", clean_body)
    gh_number = data.get("number", 0)
    created_raw = data.get("createdAt") or data.get("created_at", "")
    return GHIssue(
        id=fk_id,
        type=issue_type,
        title=title[len(fk_id) + 2 :].strip(),  # strip the "fieldkit-NNN: " prefix
        status=status,
        severity=_parse_severity(labels),
        module=_parse_module(labels),
        gh_number=gh_number,
        body=clean_body,
        source=_parse_source(body),
        created=_parse_created(body, created_raw),
    )


# ---------------------------------------------------------------------------
# GHIssueStore
# ---------------------------------------------------------------------------


class GHIssueStore:
    """Read/write fieldkit issues via the GitHub Issues API (gh CLI)."""

    def __init__(self, repo: str) -> None:
        """
        Args:
            repo: GitHub repo slug, e.g. "owner/fieldkit-project".
        """
        self.repo = repo

    # ------------------------------------------------------------------
    # ID allocation
    # ------------------------------------------------------------------

    def _scan_max_used_id(self) -> int:
        """Scan all issue titles (open + closed) for the highest public issue number in use.

        Only used to bootstrap the watermark the first time ``next_id()`` runs
        against a repo (no watermark label yet). Not called on every
        allocation: a scan only reflects *currently* live titles, so
        retitling the issue holding the highest number silently un-reserves
        it, which is why allocation itself uses
        ``_get_watermark``/``_set_watermark`` instead.
        """
        used: set[int] = set()
        page = 1
        while True:
            items = _gh_json(
                "api",
                f"repos/{self.repo}/issues",
                "--method",
                "GET",
                "-f",
                "state=all",
                "-f",
                "per_page=100",
                "-f",
                f"page={page}",
                "--jq",
                "[.[] | .title]",
            )
            if not items:
                break
            for title in items:
                m = re.match(r"^fieldkit-(\d+):", title)
                if m:
                    used.add(int(m.group(1)))
            if len(items) < 100:
                break
            page += 1
        return max(used, default=0)

    def _get_watermark(self) -> int | None:
        """Return the persisted high-water mark, or None if never initialized."""
        try:
            raw = _gh(
                "api",
                f"repos/{self.repo}/labels/{_WATERMARK_LABEL}",
                "--jq",
                ".description",
            )
        except RuntimeError as exc:
            if "404" in str(exc) or "Not Found" in str(exc):
                return None
            raise
        try:
            return int(raw.strip())
        except ValueError:
            return None

    def _set_watermark(self, value: int) -> None:
        """Persist the high-water mark, creating the backing label on first use."""
        try:
            _gh(
                "api",
                f"repos/{self.repo}/labels/{_WATERMARK_LABEL}",
                "--method",
                "PATCH",
                "-f",
                f"description={value}",
            )
        except RuntimeError as exc:
            if "404" not in str(exc) and "Not Found" not in str(exc):
                raise
            _gh(
                "api",
                f"repos/{self.repo}/labels",
                "--method",
                "POST",
                "-f",
                f"name={_WATERMARK_LABEL}",
                "-f",
                "color=ededed",
                "-f",
                f"description={value}",
            )

    def next_id(self, issue_type: IssueType) -> str:
        """Return the next available fieldkit-NNN.

        Allocation uses a durable, repo-shared high-water mark rather than a
        scan of current issue titles. A scan only reflects
        *currently* live titles, so retitling the issue holding the highest
        number — e.g. during triage, to consolidate a duplicate — silently
        frees that number for reissue to an unrelated issue. The watermark
        only ever moves forward: once a number is allocated it stays
        allocated no matter what any issue is later retitled to.

        On first-ever use for a repo (no watermark recorded yet), the
        watermark is bootstrapped from the historical title scan so
        previously issued IDs are still honored.
        """
        current = self._get_watermark()
        if current is None:
            current = self._scan_max_used_id()
        next_num = current + 1
        self._set_watermark(next_num)
        return f"fieldkit-{next_num:03d}"

    # ------------------------------------------------------------------
    # find / list
    # ------------------------------------------------------------------

    def find(self, issue_id: str) -> GHIssue | None:
        """Find a single issue by a lowercase fieldkit ID (e.g. 'fieldkit-042')."""
        issue_id = issue_id.lower()
        try:
            items = _gh_json(
                "issue",
                "list",
                "--repo",
                self.repo,
                "--state",
                "all",
                "--search",
                f"{issue_id} in:title",
                "--json",
                "number,title,state,stateReason,labels,body,createdAt",
            )
        except RuntimeError as exc:
            logger.debug("gh_store.find: %s", exc)
            return None
        for item in items:
            candidate = _issue_from_gh(item)
            if candidate is not None and candidate.id == issue_id:
                return candidate
        return None

    def list_issues(
        self,
        *,
        status: IssueStatus | Literal["all"] = "open",
        issue_type: IssueType | Literal["all"] = "all",
        module: str | None = None,
    ) -> list[GHIssue]:
        """List issues, optionally filtered by status, type, and module."""
        # Determine GitHub state filter
        if status in ("open", "planned"):
            gh_state = "open"
        elif status == "all":
            gh_state = "all"
        else:  # fixed, closed, wont-fix
            gh_state = "closed"

        args = [
            "issue",
            "list",
            "--repo",
            self.repo,
            "--state",
            gh_state,
            "--limit",
            "500",
            "--json",
            "number,title,state,stateReason,labels,body,createdAt",
        ]
        # Apply type label filter
        if issue_type != "all":
            args += ["--label", issue_type]
        # Apply module label filter
        if module is not None:
            args += ["--label", f"module:{module}"]

        try:
            items = _gh_json(*args)
        except RuntimeError as exc:
            logger.debug("gh_store.list_issues: %s", exc)
            return []

        issues: list[GHIssue] = []
        for item in items:
            gh_issue = _issue_from_gh(item)
            if gh_issue is None:
                continue
            # Post-filter by status (GH state is coarser than fieldkit status)
            if status != "all" and gh_issue.status != status:
                continue
            issues.append(gh_issue)
        return issues

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        issue_type: IssueType,
        title: str,
        body: str = "",
        severity: IssueSeverity = "medium",
        module: str = "other",
        source: str = "unknown",
    ) -> GHIssue:
        """Create a new GitHub issue and return the resulting GHIssue."""
        issue_id = self.next_id(issue_type)
        gh_title = f"{issue_id}: {title}"
        labels = [
            issue_type,
            f"severity:{severity}",
            f"module:{module}",
        ]
        now_str = datetime.now(UTC).strftime("%Y-%m-%d")
        gh_body = f"> **Source:** {source} | **Created:** {now_str}\n\n" + (
            body.strip() or "_No description provided._"
        )
        # Use REST API directly — gh issue create doesn't emit machine-readable output
        args = [
            "api",
            f"repos/{self.repo}/issues",
            "--method",
            "POST",
            "-f",
            f"title={gh_title}",
            "-f",
            f"body={gh_body}",
            "--jq",
            ".number",
        ]
        for label in labels:
            args += ["-f", f"labels[]={label}"]

        raw = _gh(*args)
        try:
            gh_number = int(raw.strip())
        except ValueError as exc:
            raise RuntimeError(f"Could not parse issue number from gh output: {raw!r}") from exc

        return GHIssue(
            id=issue_id,
            type=issue_type,
            title=title,
            status="open",
            severity=severity,
            module=module,
            gh_number=gh_number,
            body=body,
            source=source,
            created=datetime.now(UTC),
        )

    # ------------------------------------------------------------------
    # status transitions
    # ------------------------------------------------------------------

    def _get_gh_number(self, issue_id: str) -> int | None:
        issue = self.find(issue_id)
        return issue.gh_number if issue else None

    def update_status(
        self,
        issue_id: str,
        status: IssueStatus,
        *,
        note: str | None = None,
    ) -> GHIssue | None:
        """Update an issue's status. Handles open/planned/closed/wont-fix transitions."""
        issue = self.find(issue_id.lower())
        if issue is None:
            return None
        n = str(issue.gh_number)

        if status == "open":
            _gh("issue", "reopen", n, "--repo", self.repo)
            _gh(
                "issue",
                "edit",
                n,
                "--repo",
                self.repo,
                "--remove-label",
                "status:planned",
            )
        elif status == "planned":
            # Ensure open
            if issue.status not in ("open", "planned"):
                _gh("issue", "reopen", n, "--repo", self.repo)
            _gh(
                "issue",
                "edit",
                n,
                "--repo",
                self.repo,
                "--add-label",
                "status:planned",
            )
        elif status == "wont-fix":
            _gh("issue", "close", n, "--repo", self.repo, "--reason", "not planned")
        elif status in ("closed", "fixed"):
            _gh("issue", "close", n, "--repo", self.repo, "--reason", _REASON_COMPLETED)
            if status == "fixed":
                _gh("issue", "edit", n, "--repo", self.repo, "--add-label", "status:fixed")

        if note:
            _gh("issue", "comment", n, "--repo", self.repo, "--body", note)

        issue.status = status
        return issue

    def mark_fixed(
        self,
        issue_id: str,
        *,
        commit: str | None = None,
        note: str | None = None,
    ) -> GHIssue | None:
        """Close issue as completed with status:fixed label. Records commit SHA in a comment."""
        issue = self.find(issue_id.lower())
        if issue is None:
            return None
        n = str(issue.gh_number)

        _gh("issue", "close", n, "--repo", self.repo, "--reason", _REASON_COMPLETED)
        _gh("issue", "edit", n, "--repo", self.repo, "--add-label", "status:fixed")

        comment_parts: list[str] = []
        if commit:
            comment_parts.append(f"**Fix commit:** {commit}")
        if note:
            comment_parts.append(note)
        if comment_parts:
            _gh("issue", "comment", n, "--repo", self.repo, "--body", "\n\n".join(comment_parts))

        issue.status = "fixed"
        return issue

    def add_note(self, issue_id: str, note: str) -> GHIssue | None:
        """Add a comment to an issue without changing its status."""
        issue = self.find(issue_id.lower())
        if issue is None:
            return None
        _gh("issue", "comment", str(issue.gh_number), "--repo", self.repo, "--body", note)
        return issue

    def edit(
        self,
        issue_id: str,
        *,
        title: str | None = None,
        body: str | None = None,
        severity: IssueSeverity | None = None,
        module: str | None = None,
    ) -> GHIssue | None:
        """Edit issue title, body, severity label, or module label."""
        issue = self.find(issue_id.lower())
        if issue is None:
            return None
        n = str(issue.gh_number)

        edit_args = ["issue", "edit", n, "--repo", self.repo]

        if title is not None:
            new_gh_title = f"{issue.id}: {title}"
            edit_args += ["--title", new_gh_title]
            issue.title = title

        if body is not None:
            # Rebuild full GH body preserving metadata line
            now_str = issue.created.strftime("%Y-%m-%d")
            new_gh_body = f"> **Source:** {issue.source} | **Created:** {now_str}\n\n" + (
                body.strip() or "_No description provided._"
            )
            edit_args += ["--body", new_gh_body]
            issue.body = body

        if severity is not None and severity != issue.severity:
            edit_args += [
                "--remove-label",
                f"severity:{issue.severity}",
                "--add-label",
                f"severity:{severity}",
            ]
            issue.severity = severity

        if module is not None and module != issue.module:
            edit_args += [
                "--remove-label",
                f"module:{issue.module}",
                "--add-label",
                f"module:{module}",
            ]
            issue.module = module

        _gh(*edit_args)
        return issue

    # ------------------------------------------------------------------
    # Milestone support (GitHub milestones)
    # ------------------------------------------------------------------

    def list_by_milestone(self, milestone_title: str) -> list[GHIssue]:
        """Return issues linked to a GitHub milestone by title."""
        # Find milestone number
        milestones = _gh_json(
            "api",
            f"repos/{self.repo}/milestones",
            "--jq",
            "[.[] | {number, title}]",
        )
        milestone_number: int | None = None
        for ms in milestones:
            if ms["title"].upper() == milestone_title.upper():
                milestone_number = ms["number"]
                break
        if milestone_number is None:
            return []

        items = _gh_json(
            "issue",
            "list",
            "--repo",
            self.repo,
            "--state",
            "all",
            "--milestone",
            str(milestone_number),
            "--limit",
            "500",
            "--json",
            "number,title,state,stateReason,labels,body,createdAt",
        )
        return [i for item in items if (i := _issue_from_gh(item)) is not None]

    def link_milestone(
        self,
        issue_id: str,
        milestone_title: str,
        *,
        note: str | None = None,
    ) -> GHIssue | None:
        """Link an issue to a GitHub milestone, creating the milestone if needed."""
        issue = self.find(issue_id.lower())
        if issue is None:
            return None

        # Find or create milestone
        milestones = _gh_json(
            "api",
            f"repos/{self.repo}/milestones",
            "--jq",
            "[.[] | {number, title}]",
        )
        milestone_number: int | None = None
        for ms in milestones:
            if ms["title"].upper() == milestone_title.upper():
                milestone_number = ms["number"]
                break
        if milestone_number is None:
            result = _gh_json(
                "api",
                f"repos/{self.repo}/milestones",
                "--method",
                "POST",
                "-f",
                f"title={milestone_title}",
                "--jq",
                "{number, title}",
            )
            milestone_number = int(result["number"])

        _gh(
            "issue",
            "edit",
            str(issue.gh_number),
            "--repo",
            self.repo,
            "--milestone",
            str(milestone_number),
        )

        if note:
            _gh("issue", "comment", str(issue.gh_number), "--repo", self.repo, "--body", note)

        return issue

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    def known_modules(self) -> set[str]:
        return _KNOWN_MODULES

    def count_issues(self) -> dict[str, int]:
        """Return {open_bugs, open_enhancements, closed, wontfix} counts."""
        open_bugs = len(self.list_issues(status="open", issue_type="bug"))
        open_enhs = len(self.list_issues(status="open", issue_type="enhancement"))
        closed = len(self.list_issues(status="closed"))
        wontfix = len(self.list_issues(status="wont-fix"))
        return {
            "open_bugs": open_bugs,
            "open_enhancements": open_enhs,
            "closed": closed,
            "wontfix": wontfix,
        }
