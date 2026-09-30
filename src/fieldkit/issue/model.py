"""Canonical issue identity, vocabulary, and provider-independent parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Literal, cast

from fieldkit.errors import GitHubDataError

IssueType = Literal["bug", "enhancement"]
IssueSeverity = Literal["low", "medium", "high", "critical"]
IssueStatus = Literal["open", "planned", "fixed", "closed", "wont-fix"]

ISSUE_TYPES: Final[tuple[IssueType, ...]] = ("bug", "enhancement")
ISSUE_SEVERITIES: Final[tuple[IssueSeverity, ...]] = ("low", "medium", "high", "critical")
ISSUE_STATUSES: Final[tuple[IssueStatus, ...]] = ("open", "planned", "fixed", "closed", "wont-fix")
ISSUE_MODULES: Final[tuple[str, ...]] = (
    "auth",
    "brief",
    "cli",
    "companion",
    "config",
    "contact",
    "docs",
    "doctor",
    "driver",
    "enrich",
    "gmail",
    "health",
    "hooks",
    "ingest",
    "init",
    "issue",
    "lib",
    "meeting",
    "other",
    "pipeline",
    "pursuit",
    "sf",
    "shadowbot",
    "skill",
    "sync",
    "version",
    "watch",
    "web",
)
SEVERITY_RANK: Final[dict[IssueSeverity, int]] = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}

_ISSUE_ID = re.compile(r"fieldkit-0*([1-9][0-9]*)\Z", re.IGNORECASE)
_LEGACY_TITLE_PREFIX = re.compile(r"^fieldkit-[0-9]+:\s*", re.IGNORECASE)


@dataclass
class GHIssue:
    """A managed fieldkit issue backed by one GitHub issue number."""

    id: str
    type: IssueType
    title: str
    status: IssueStatus
    severity: IssueSeverity
    module: str
    gh_number: int
    body: str = ""
    source: str = "unknown"
    created: datetime = field(default_factory=lambda: datetime.now(UTC))


def format_issue_id(gh_number: int) -> str:
    """Return the public ID derived from GitHub's positive atomic issue number."""
    if isinstance(gh_number, bool) or gh_number < 1:
        raise GitHubDataError("GitHub issue number is invalid; retrying unchanged will not help.")
    return f"fieldkit-{gh_number:03d}"


def parse_issue_id(issue_id: str) -> int:
    """Return the GitHub number encoded by a canonical public issue ID."""
    match = _ISSUE_ID.fullmatch(issue_id)
    if match is None:
        raise GitHubDataError("Public issue identifier is invalid; expected fieldkit-NNN with a positive number.")
    return int(match.group(1))


def display_title(title: str) -> str:
    """Strip a legacy presentation prefix without treating it as identity."""
    return _LEGACY_TITLE_PREFIX.sub("", title, count=1).strip()


def parse_managed_labels(labels: list[str]) -> tuple[IssueType, IssueSeverity, str] | None:
    """Classify managed issue labels; return None for ordinary community issues."""
    type_labels: list[IssueType] = [known for known in ISSUE_TYPES for label in labels if label == known]
    if not type_labels:
        return None
    severity_labels = [label.removeprefix("severity:") for label in labels if label.startswith("severity:")]
    module_labels = [label.removeprefix("module:") for label in labels if label.startswith("module:")]
    if (
        len(type_labels) != 1
        or len(severity_labels) > 1
        or any(label not in ISSUE_SEVERITIES for label in severity_labels)
        or len(module_labels) > 1
        or any(label not in ISSUE_MODULES for label in module_labels)
    ):
        raise GitHubDataError("Managed GitHub issue labels are incomplete or invalid; repair them before retrying.")
    issue_type = type_labels[0]
    severity = cast(IssueSeverity, severity_labels[0] if severity_labels else "medium")
    module = module_labels[0] if module_labels else "other"
    return issue_type, severity, module


def status_from_github(state: str, state_reason: str | None, labels: list[str]) -> IssueStatus:
    """Derive the canonical issue status from validated GitHub state."""
    status_labels = [label for label in labels if label.startswith("status:")]
    if len(status_labels) > 1 or any(label not in {"status:planned", "status:fixed"} for label in status_labels):
        raise GitHubDataError("GitHub issue status labels are incomplete or invalid; repair them before retrying.")
    normalized_state = state.lower()
    normalized_reason = state_reason.lower() if state_reason is not None else None
    if normalized_state not in {"open", "closed"} or normalized_reason not in {
        None,
        "completed",
        "not_planned",
        "reopened",
    }:
        raise GitHubDataError("GitHub issue state is incomplete or invalid; repair it before retrying.")
    if normalized_state == "open":
        if normalized_reason not in {None, "reopened"} or "status:fixed" in labels:
            raise GitHubDataError("GitHub issue state is internally inconsistent; repair it before retrying.")
        return "planned" if "status:planned" in labels else "open"
    if normalized_reason not in {"completed", "not_planned"} or "status:planned" in labels:
        raise GitHubDataError("GitHub issue state is internally inconsistent; repair it before retrying.")
    if normalized_reason == "not_planned":
        if "status:fixed" in labels:
            raise GitHubDataError("GitHub issue state is internally inconsistent; repair it before retrying.")
        return "wont-fix"
    return "fixed" if "status:fixed" in labels else "closed"
