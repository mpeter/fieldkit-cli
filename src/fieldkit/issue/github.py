"""GitHub Issues integration for the canonical issue domain.

All operations map to ``gh issue`` and ``gh api`` subcommands. Read failures
propagate as typed errors; only a valid empty JSON list is an empty query.
"""

import json
import re
import subprocess
from datetime import UTC, datetime
from typing import Any, Literal

from fieldkit.config import TIMEOUT_GH_CLI, ConfigError
from fieldkit.errors import (
    AuthError,
    GitHubCreationUncertainError,
    GitHubDataError,
    GitHubNotFoundError,
    GitHubRequestError,
)
from fieldkit.issue.model import (
    ISSUE_MODULES,
    ISSUE_SEVERITIES,
    ISSUE_TYPES,
    GHIssue,
    IssueSeverity,
    IssueStatus,
    IssueType,
    display_title,
    format_issue_id,
    parse_issue_id,
    parse_managed_labels,
    status_from_github,
)

# GitHub state_reason values
_REASON_COMPLETED = "completed"
_REASON_NOT_PLANNED = "not planned"

_INVALID_ISSUE_RESPONSE = "GitHub issue response is incomplete or invalid; repair provider data before retrying."
_INVALID_MILESTONE_RESPONSE = (
    "GitHub milestone response is incomplete or invalid; repair provider data before retrying."
)
_UNCERTAIN_CREATION = (
    "GitHub resource may have been created but its identity was not proven; reconcile remote state before retrying."
)


class _DuplicateJsonKey(ValueError):
    """Provider JSON repeated an object key, so its meaning is ambiguous."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _gh(*args: str, irreversible: bool = False) -> str:
    """Run ``gh`` without exposing provider payloads through diagnostics."""
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=TIMEOUT_GH_CLI,
        )
    except FileNotFoundError:
        raise ConfigError("GitHub CLI (gh) is not installed; install it before using issue commands.") from None
    except subprocess.TimeoutExpired:
        if irreversible:
            raise GitHubCreationUncertainError(_UNCERTAIN_CREATION) from None
        raise GitHubRequestError("GitHub request timed out; check connectivity and retry.") from None
    except UnicodeError:
        if irreversible:
            raise GitHubCreationUncertainError(_UNCERTAIN_CREATION) from None
        raise GitHubRequestError("GitHub CLI returned undecodable output; retry the request.") from None
    except OSError:
        raise GitHubRequestError("GitHub CLI could not run; check availability and retry.") from None
    if result.returncode != 0:
        stderr = result.stderr.strip()
        match = re.search(r"\bHTTP\s+(\d{3})\b", stderr)
        status = int(match.group(1)) if match is not None else None
        if "rate limit" in stderr.lower():
            raise GitHubRequestError(
                "GitHub rate limit reached; check `gh api rate_limit` and retry later.", http_status=status
            )
        if result.returncode == 4 or status in {401, 403}:
            raise AuthError(
                "GitHub authentication or repository permission is required; run `gh auth login` and verify access."
            )
        if status == 404:
            raise GitHubNotFoundError("GitHub issue or repository was not found; verify the configured target.")
        if status is not None and 400 <= status < 500:
            raise GitHubDataError(
                "GitHub rejected the issue request; correct the request or repository state before retrying."
            )
        if irreversible:
            raise GitHubCreationUncertainError(_UNCERTAIN_CREATION)
        raise GitHubRequestError(
            "GitHub request failed; check repository access and provider availability before retrying.",
            http_status=status,
        )
    return result.stdout.strip()


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _decode_json(raw: str, *, irreversible: bool) -> Any:
    try:
        return json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
    except (json.JSONDecodeError, _DuplicateJsonKey):
        if irreversible:
            raise GitHubCreationUncertainError(_UNCERTAIN_CREATION) from None
        raise GitHubDataError("GitHub CLI returned invalid JSON; repair provider output before retrying.") from None


def _gh_json(*args: str, irreversible: bool = False) -> Any:
    """Run ``gh`` CLI, parse JSON output.

    Empty or invalid JSON is a provider failure, not an empty query result.
    """
    raw = _gh(*args, irreversible=irreversible)
    return _decode_json(raw, irreversible=irreversible)


def _post_comment(issue_number: str, repo: str, body: str) -> None:
    """Post one comment, treating an unconfirmed result as non-retryable."""
    _gh("issue", "comment", issue_number, "--repo", repo, "--body", body, irreversible=True)


def _labels_from_issue(issue_data: dict[str, Any]) -> list[str]:
    return [lbl["name"] for lbl in issue_data.get("labels", [])]


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
    """Convert one validated response, filtering ordinary community issues."""
    title = data["title"]
    labels = _labels_from_issue(data)
    managed = parse_managed_labels(labels)
    if managed is None:
        return None
    issue_type, severity, module = managed
    gh_number = data["number"]
    state = data["state"]
    state_reason = data.get("stateReason") or data.get("state_reason")
    status = status_from_github(state, state_reason, labels)
    body = data.get("body", "") or ""
    # Strip the migration footer from body display
    clean_body = re.split(r"\n---\n\*Migrated from", body)[0].strip()
    # Also strip the metadata blockquote line at the top
    clean_body = re.sub(r"^> \*\*Source:\*\*.*\n\n", "", clean_body)
    created_raw = data.get("createdAt") or data.get("created_at", "")
    return GHIssue(
        id=format_issue_id(gh_number),
        type=issue_type,
        title=display_title(title),
        status=status,
        severity=severity,
        module=module,
        gh_number=gh_number,
        body=clean_body,
        source=_parse_source(body),
        created=_parse_created(body, created_raw),
    )


# ---------------------------------------------------------------------------
# GHIssueStore
# ---------------------------------------------------------------------------


def _validate_issue_record(item: dict[str, Any]) -> bool:
    """Validate every provider field required by the managed-issue projection."""
    if not {
        "number",
        "title",
        "state",
        "stateReason",
        "labels",
        "body",
        "createdAt",
    }.issubset(item):
        raise GitHubDataError(_INVALID_ISSUE_RESPONSE)
    labels = item.get("labels")
    number = item.get("number")
    state = item.get("state")
    created = item.get("createdAt")
    if (
        not isinstance(item.get("title"), str)
        or not isinstance(number, int)
        or isinstance(number, bool)
        or number < 1
        or not isinstance(state, str)
        or state.lower() not in {"open", "closed"}
        or not isinstance(labels, list)
        or not all(isinstance(label, dict) and isinstance(label.get("name"), str) for label in labels)
        or not isinstance(created, str)
        or (item.get("body") is not None and not isinstance(item.get("body"), str))
        or (item.get("stateReason") is not None and not isinstance(item.get("stateReason"), str))
    ):
        raise GitHubDataError(_INVALID_ISSUE_RESPONSE)
    try:
        datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        raise GitHubDataError(_INVALID_ISSUE_RESPONSE) from None
    label_names = [label["name"] for label in labels]
    if parse_managed_labels(label_names) is None:
        return False
    status_from_github(state, item.get("stateReason"), label_names)
    return True


def _read_issues(*args: str) -> list[GHIssue]:
    """Decode complete issue records, preserving genuine non-fieldkit filtering."""
    payload = _gh_json(*args)
    if not isinstance(payload, list):
        raise GitHubDataError(_INVALID_ISSUE_RESPONSE)
    issues: list[GHIssue] = []
    for item in payload:
        if not isinstance(item, dict):
            raise GitHubDataError(_INVALID_ISSUE_RESPONSE)
        if not _validate_issue_record(item):
            continue
        issue = _issue_from_gh(item)
        if issue is not None:
            issues.append(issue)
    return issues


def _read_milestones(*args: str) -> list[tuple[int, str]]:
    """Decode the complete identity fields required for milestone selection."""
    payload = _gh_json(*args)
    if not isinstance(payload, list):
        raise GitHubDataError(_INVALID_MILESTONE_RESPONSE)
    milestones: list[tuple[int, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise GitHubDataError(_INVALID_MILESTONE_RESPONSE)
        number = item.get("number")
        title = item.get("title")
        if (
            not isinstance(number, int)
            or isinstance(number, bool)
            or number < 1
            or not isinstance(title, str)
            or not title
        ):
            raise GitHubDataError(_INVALID_MILESTONE_RESPONSE)
        milestones.append((number, title))
    return milestones


class GHIssueStore:
    """Read/write fieldkit issues via the GitHub Issues API (gh CLI)."""

    def __init__(self, repo: str) -> None:
        """
        Args:
            repo: GitHub repo slug, e.g. "owner/fieldkit-project".
        """
        self.repo = repo

    # ------------------------------------------------------------------
    # find / list
    # ------------------------------------------------------------------

    def find(self, issue_id: str) -> GHIssue | None:
        """Find a single issue by a lowercase fieldkit ID (e.g. 'fieldkit-042')."""
        gh_number = parse_issue_id(issue_id.lower())
        try:
            payload = _gh_json(
                "issue",
                "view",
                str(gh_number),
                "--repo",
                self.repo,
                "--json",
                "number,title,state,stateReason,labels,body,createdAt",
            )
        except GitHubNotFoundError:
            return None
        if not isinstance(payload, dict):
            raise GitHubDataError(_INVALID_ISSUE_RESPONSE)
        if not _validate_issue_record(payload):
            return None
        return _issue_from_gh(payload)

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

        items = _read_issues(*args)

        issues: list[GHIssue] = []
        for gh_issue in items:
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
        if issue_type not in ISSUE_TYPES or severity not in ISSUE_SEVERITIES or module not in ISSUE_MODULES:
            raise GitHubDataError("Issue type, severity, or module is invalid; correct the request before retrying.")
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
            f"title={title}",
            "-f",
            f"body={gh_body}",
            "--jq",
            "{number}",
        ]
        for label in labels:
            args += ["-f", f"labels[]={label}"]

        raw = _gh(*args, irreversible=True)
        payload = _decode_json(raw, irreversible=True)
        gh_number = payload.get("number") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or set(payload) != {"number"}
            or not isinstance(gh_number, int)
            or isinstance(gh_number, bool)
            or gh_number < 1
        ):
            raise GitHubCreationUncertainError(_UNCERTAIN_CREATION)

        return GHIssue(
            id=format_issue_id(gh_number),
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
            if issue.status == "fixed":
                _gh("issue", "edit", n, "--repo", self.repo, "--remove-label", "status:fixed")
            if issue.status not in ("open", "planned"):
                _gh("issue", "reopen", n, "--repo", self.repo)
            if issue.status == "planned":
                _gh("issue", "edit", n, "--repo", self.repo, "--remove-label", "status:planned")
        elif status == "planned":
            if issue.status == "fixed":
                _gh("issue", "edit", n, "--repo", self.repo, "--remove-label", "status:fixed")
            if issue.status not in ("open", "planned"):
                _gh("issue", "reopen", n, "--repo", self.repo)
            if issue.status != "planned":
                _gh("issue", "edit", n, "--repo", self.repo, "--add-label", "status:planned")
        elif status == "wont-fix":
            if issue.status != "wont-fix":
                if issue.status == "planned":
                    _gh("issue", "edit", n, "--repo", self.repo, "--remove-label", "status:planned")
                elif issue.status == "fixed":
                    _gh("issue", "edit", n, "--repo", self.repo, "--remove-label", "status:fixed")
                _gh("issue", "close", n, "--repo", self.repo, "--reason", _REASON_NOT_PLANNED)
        elif status == "closed":
            if issue.status == "planned":
                _gh("issue", "edit", n, "--repo", self.repo, "--remove-label", "status:planned")
                _gh("issue", "close", n, "--repo", self.repo, "--reason", _REASON_COMPLETED)
            elif issue.status == "fixed":
                _gh("issue", "edit", n, "--repo", self.repo, "--remove-label", "status:fixed")
            elif issue.status != "closed":
                _gh("issue", "close", n, "--repo", self.repo, "--reason", _REASON_COMPLETED)
        elif status == "fixed":
            self._apply_fixed_state(issue)

        if note:
            _post_comment(n, self.repo, note)

        issue.status = status
        return issue

    def _apply_fixed_state(self, issue: GHIssue) -> None:
        """Idempotently add the fixed label before closing the issue."""
        if issue.status == "fixed":
            return
        n = str(issue.gh_number)
        if issue.status == "planned":
            _gh("issue", "edit", n, "--repo", self.repo, "--remove-label", "status:planned")
        _gh("issue", "edit", n, "--repo", self.repo, "--add-label", "status:fixed")
        if issue.status != "closed":
            _gh("issue", "close", n, "--repo", self.repo, "--reason", _REASON_COMPLETED)

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
        self._apply_fixed_state(issue)

        comment_parts: list[str] = []
        if commit:
            comment_parts.append(f"**Fix commit:** {commit}")
        if note:
            comment_parts.append(note)
        if comment_parts:
            _post_comment(n, self.repo, "\n\n".join(comment_parts))

        issue.status = "fixed"
        return issue

    def add_note(self, issue_id: str, note: str) -> GHIssue | None:
        """Add a comment to an issue without changing its status."""
        issue = self.find(issue_id.lower())
        if issue is None:
            return None
        _post_comment(str(issue.gh_number), self.repo, note)
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
            edit_args += ["--title", title]
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
        milestones = _read_milestones(
            "api",
            f"repos/{self.repo}/milestones",
            "--jq",
            "[.[] | {number, title}]",
        )
        milestone_number: int | None = None
        for number, title in milestones:
            if title.upper() == milestone_title.upper():
                milestone_number = number
                break
        if milestone_number is None:
            return []

        return _read_issues(
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
        milestones = _read_milestones(
            "api",
            f"repos/{self.repo}/milestones",
            "--jq",
            "[.[] | {number, title}]",
        )
        milestone_number: int | None = None
        for number, title in milestones:
            if title.upper() == milestone_title.upper():
                milestone_number = number
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
                irreversible=True,
            )
            if not isinstance(result, dict):
                raise GitHubCreationUncertainError(_UNCERTAIN_CREATION)
            created_number = result.get("number")
            created_title = result.get("title")
            if (
                set(result) != {"number", "title"}
                or not isinstance(created_number, int)
                or isinstance(created_number, bool)
                or created_number < 1
                or not isinstance(created_title, str)
                or created_title != milestone_title
            ):
                raise GitHubCreationUncertainError(_UNCERTAIN_CREATION)
            milestone_number = created_number

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
            _post_comment(str(issue.gh_number), self.repo, note)

        return issue

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    def known_modules(self) -> set[str]:
        return set(ISSUE_MODULES)
