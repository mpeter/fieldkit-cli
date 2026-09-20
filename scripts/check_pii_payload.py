#!/usr/bin/env python3
"""GitHub Actions entrypoint: auto-redact PII from issue/PR bodies and comments.

Reads the GitHub Actions event payload from $GITHUB_EVENT_PATH, extracts the
relevant body text, applies PII redaction via scripts/pii_patterns.py, and
if any PII was found:
  1. PATCHes the body via the GitHub REST API.
  2. Posts a notice comment explaining what was changed.

Supported events
----------------
    issues                        — issue body (opened, edited)
    pull_request_target           — PR body (opened, edited; trusted base code)
    issue_comment                 — issue/PR comment (created, edited)
    pull_request_review_comment   — PR inline review comment (created, edited)

Environment
-----------
    GITHUB_TOKEN      — required (provided automatically by GitHub Actions)
    GITHUB_EVENT_PATH — required (provided automatically by GitHub Actions)
    GITHUB_EVENT_NAME — required (provided automatically by GitHub Actions)
    GITHUB_REPOSITORY — required (provided automatically by GitHub Actions)

Exit codes
----------
    0  — no PII found, or redaction and notice both succeeded
    1  — invalid event/configuration, redaction failure, or notice failure
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Allow import of pii_patterns from the same scripts/ directory.
_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_REPO_ROOT / "src"))

from pii_patterns import redact_text, summarize_changes  # noqa: E402

_API_BASE = "https://api.github.com"
_MAX_EVENT_BYTES = 2 * 1024 * 1024
_MAX_BODY_CHARS = 65_536
_REQUEST_TIMEOUT_SECONDS = 15.0
_OWNER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
_REPOSITORY_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_ALLOWED_ACTIONS = {
    "issues": frozenset({"opened", "edited"}),
    "pull_request_target": frozenset({"opened", "edited"}),
    "issue_comment": frozenset({"created", "edited"}),
    "pull_request_review_comment": frozenset({"created", "edited"}),
}


class PayloadError(ValueError):
    """The GitHub event is missing, malformed, or outside the supported contract."""


class GitHubAPIError(RuntimeError):
    """A required GitHub mutation did not complete."""


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------


def _token() -> str:
    tok = os.environ.get("GITHUB_TOKEN", "")
    if not tok:
        raise PayloadError("GITHUB_TOKEN is not set")
    return tok


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }


def _request(method: str, url: str, payload: dict[str, str]) -> None:
    data = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS):
            pass
    except urllib.error.HTTPError as exc:
        raise GitHubAPIError(f"{method} failed with HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise GitHubAPIError(f"{method} failed with {type(exc).__name__}") from None


def _patch(url: str, payload: dict[str, str]) -> None:
    _request("PATCH", url, payload)


def _post_comment(repo: str, issue_number: int, body: str) -> None:
    url = f"{_API_BASE}/repos/{repo}/issues/{issue_number}/comments"
    _request("POST", url, {"body": body})


# ---------------------------------------------------------------------------
# Notice comment builder
# ---------------------------------------------------------------------------


def _build_notice(descriptions: list[str]) -> str:
    lines = "\n".join(f"> - {d}" for d in summarize_changes(descriptions))
    return (
        "> [!WARNING]\n"
        "> **pii-guard**: This body contained personal data that was automatically redacted.\n"
        ">\n"
        "> **What was changed:**\n"
        f"{lines}\n"
        ">\n"
        "> No action needed — the edit has already been applied."
    )


# ---------------------------------------------------------------------------
# Event routing
# ---------------------------------------------------------------------------


def _object(value: object, subject: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise PayloadError(f"{subject} must be an object")
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _positive_int(value: object, subject: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PayloadError(f"{subject} must be a positive integer")
    return value


def _body(value: object, subject: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PayloadError(f"{subject} must be text")
    if len(value) > _MAX_BODY_CHARS:
        raise PayloadError(f"{subject} exceeds {_MAX_BODY_CHARS} characters")
    return value


def _valid_repository(value: str) -> bool:
    owner, separator, name = value.partition("/")
    return bool(
        separator
        and "/" not in name
        and _OWNER_RE.fullmatch(owner)
        and "--" not in owner
        and _REPOSITORY_NAME_RE.fullmatch(name)
        and name not in {".", ".."}
    )


def _load_event() -> tuple[str, str, dict[str, Any]]:
    """Return (event_name, repo, event_payload)."""
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")

    if not event_name:
        raise PayloadError("GITHUB_EVENT_NAME is not set")
    if not _valid_repository(repo):
        raise PayloadError("GITHUB_REPOSITORY is not a valid owner/name")
    if not event_path:
        raise PayloadError("GITHUB_EVENT_PATH is not set")

    try:
        with Path(event_path).open("rb") as event_file:
            raw_event = event_file.read(_MAX_EVENT_BYTES + 1)
    except OSError as exc:
        raise PayloadError(f"could not read event payload: {type(exc).__name__}") from exc
    if len(raw_event) > _MAX_EVENT_BYTES:
        raise PayloadError(f"event payload exceeds {_MAX_EVENT_BYTES} bytes")
    try:
        payload = _object(json.loads(raw_event), "event payload")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PayloadError("event payload is not valid UTF-8 JSON") from exc

    actions = _ALLOWED_ACTIONS.get(event_name)
    if actions is None:
        raise PayloadError(f"unsupported event: {event_name}")
    if payload.get("action") not in actions:
        raise PayloadError(f"unsupported action for {event_name}")
    payload_repo = _object(payload.get("repository"), "repository").get("full_name")
    if payload_repo != repo:
        raise PayloadError("event repository does not match GITHUB_REPOSITORY")

    return event_name, repo, payload


def handle_issues(repo: str, payload: dict[str, Any]) -> None:
    """issues: opened | edited — redact issue body."""
    issue = _object(payload.get("issue"), "issue")
    number = _positive_int(issue.get("number"), "issue number")
    body = _body(issue.get("body"), "issue body")

    if not body:
        return

    redacted, descs = redact_text(body)
    if redacted == body:
        return

    print(f"issue #{number}: {len(descs)} redaction(s)")
    _patch(f"{_API_BASE}/repos/{repo}/issues/{number}", {"body": redacted})
    _post_comment(repo, number, _build_notice(descs))


def handle_pull_request(repo: str, payload: dict[str, Any]) -> None:
    """pull_request_target: opened | edited — redact PR body."""
    pr = _object(payload.get("pull_request"), "pull_request")
    number = _positive_int(pr.get("number"), "pull request number")
    body = _body(pr.get("body"), "pull request body")

    if not body:
        return

    redacted, descs = redact_text(body)
    if redacted == body:
        return

    print(f"PR #{number}: {len(descs)} redaction(s)")
    _patch(f"{_API_BASE}/repos/{repo}/pulls/{number}", {"body": redacted})
    _post_comment(repo, number, _build_notice(descs))


def handle_issue_comment(repo: str, payload: dict[str, Any]) -> None:
    """issue_comment: created | edited — redact issue or PR comment body."""
    comment = _object(payload.get("comment"), "comment")
    comment_id = _positive_int(comment.get("id"), "comment id")
    body = _body(comment.get("body"), "comment body")

    # .issue.number is present for both issue and PR comments
    issue = _object(payload.get("issue"), "issue")
    issue_number = _positive_int(issue.get("number"), "issue number")

    if not body:
        return

    redacted, descs = redact_text(body)
    if redacted == body:
        return

    print(f"comment {comment_id} on #{issue_number}: {len(descs)} redaction(s)")
    _patch(f"{_API_BASE}/repos/{repo}/issues/comments/{comment_id}", {"body": redacted})
    _post_comment(repo, issue_number, _build_notice(descs))


def handle_pull_request_review_comment(repo: str, payload: dict[str, Any]) -> None:
    """pull_request_review_comment: created | edited — redact inline review comment."""
    comment = _object(payload.get("comment"), "comment")
    comment_id = _positive_int(comment.get("id"), "review comment id")
    body = _body(comment.get("body"), "review comment body")

    pr = _object(payload.get("pull_request"), "pull_request")
    pr_number = _positive_int(pr.get("number"), "pull request number")

    if not body:
        return

    redacted, descs = redact_text(body)
    if redacted == body:
        return

    print(f"review comment {comment_id} on PR #{pr_number}: {len(descs)} redaction(s)")
    _patch(f"{_API_BASE}/repos/{repo}/pulls/comments/{comment_id}", {"body": redacted})
    _post_comment(repo, pr_number, _build_notice(descs))


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

_HANDLERS = {
    "issues": handle_issues,
    "pull_request_target": handle_pull_request,
    "issue_comment": handle_issue_comment,
    "pull_request_review_comment": handle_pull_request_review_comment,
}


def main() -> int:
    try:
        event_name, repo, payload = _load_event()
        action = payload.get("action")
        print(f"pii-guard: {event_name}/{action} on {repo}")
        _HANDLERS[event_name](repo, payload)
    except (PayloadError, GitHubAPIError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
