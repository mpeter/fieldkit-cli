#!/usr/bin/env python3
"""Scrub PII from GitHub issue and PR bodies and comments.

Applies the same detection/redaction patterns as hooks/pii_guard.py
via scripts/pii_patterns.py. Dry-run by default; use --apply to write.

Usage
-----
    uv run python scripts/scrub_github_issues.py [--apply] [--repo OWNER/REPO] [--verbose]

Environment
-----------
    GITHUB_TOKEN  — required; must have issues:write and pull-requests:write scopes.

Exit codes
----------
    0  — clean (no PII) or all redactions applied successfully
    1  — API error or GITHUB_TOKEN not set
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, NamedTuple

# Delay between mutating API calls (PATCH + POST) to stay under GitHub's
# secondary rate limit (~80 content-creation operations/minute).
_WRITE_DELAY_SECONDS = 1.0

# Allow running from any working directory by adding scripts/ parent to path.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "src"))

from pii_patterns import redact_text, summarize_changes  # noqa: E402

_API_BASE = "https://api.github.com"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class Change(NamedTuple):
    kind: str  # "issue body", "PR body", "issue comment", "PR review comment"
    number: int  # issue or PR number (for display and posting notice comment)
    item_id: int  # issue/PR number or comment id depending on kind
    original: str
    redacted: str
    descriptions: list[str]  # human-readable list of what changed


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------


def _token() -> str:
    tok = os.environ.get("GITHUB_TOKEN", "")
    if not tok:
        print("error: GITHUB_TOKEN environment variable is not set", file=sys.stderr)
        raise SystemExit(1)
    return tok


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }


def _get(url: str) -> Any:
    req = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        print(f"error: GET {url} → {exc.code}: {body}", file=sys.stderr)
        raise SystemExit(1) from exc


def _get_paginated(url: str) -> list[Any]:
    """Fetch all pages of a GitHub list endpoint."""
    results: list[Any] = []
    page = 1
    while True:
        sep = "&" if "?" in url else "?"
        page_url = f"{url}{sep}per_page=100&page={page}"
        data = _get(page_url)
        if not data:
            break
        results.extend(data)
        if len(data) < 100:
            break
        page += 1
    return results


def _patch(url: str, payload: dict[str, str], dry_run: bool) -> None:
    if dry_run:
        return
    time.sleep(_WRITE_DELAY_SECONDS)
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=_headers(), method="PATCH")
    try:
        with urllib.request.urlopen(req):
            pass
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        print(f"error: PATCH {url} → {exc.code}: {body}", file=sys.stderr)
        raise SystemExit(1) from exc


def _post_comment(repo: str, number: int, body: str, dry_run: bool) -> None:
    if dry_run:
        return
    time.sleep(_WRITE_DELAY_SECONDS)
    url = f"{_API_BASE}/repos/{repo}/issues/{number}/comments"
    data = json.dumps({"body": body}).encode()
    req = urllib.request.Request(url, data=data, headers=_headers(), method="POST")
    try:
        with urllib.request.urlopen(req):
            pass
    except urllib.error.HTTPError as exc:
        err = exc.read().decode()
        # Notice comments are informational — don't abort remaining patches on failure.
        print(
            f"warning: POST {url} → {exc.code} (notice comment failed, body already patched): {err[:120]}",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def _scan_item(
    repo: str,
    item: dict[str, Any],
) -> list[Change]:
    """Return all pending Changes for one issue or PR (body + comments)."""
    changes: list[Change] = []
    number: int = item["number"]
    is_pr: bool = "pull_request" in item

    # --- body ---
    body: str = item.get("body") or ""
    if body:
        redacted, descs = redact_text(body)
        if redacted != body:
            changes.append(
                Change(
                    kind="PR body" if is_pr else "issue body",
                    number=number,
                    item_id=number,
                    original=body,
                    redacted=redacted,
                    descriptions=descs,
                )
            )

    # --- issue/PR comments (issue_comment type) ---
    comments: list[dict[str, Any]] = _get_paginated(f"{_API_BASE}/repos/{repo}/issues/{number}/comments")
    for c in comments:
        cbody: str = c.get("body") or ""
        if cbody:
            redacted_c, descs_c = redact_text(cbody)
            if redacted_c != cbody:
                changes.append(
                    Change(
                        kind="issue comment" if not is_pr else "PR comment",
                        number=number,
                        item_id=c["id"],
                        original=cbody,
                        redacted=redacted_c,
                        descriptions=descs_c,
                    )
                )

    # --- PR review comments (inline diff comments) ---
    if is_pr:
        review_comments: list[dict[str, Any]] = _get_paginated(f"{_API_BASE}/repos/{repo}/pulls/{number}/comments")
        for rc in review_comments:
            rcbody: str = rc.get("body") or ""
            if rcbody:
                redacted_rc, descs_rc = redact_text(rcbody)
                if redacted_rc != rcbody:
                    changes.append(
                        Change(
                            kind="PR review comment",
                            number=number,
                            item_id=rc["id"],
                            original=rcbody,
                            redacted=redacted_rc,
                            descriptions=descs_rc,
                        )
                    )

    return changes


def _patch_url(repo: str, change: Change) -> str:
    """Return the PATCH URL for a given Change."""
    if change.kind == "issue body":
        return f"{_API_BASE}/repos/{repo}/issues/{change.item_id}"
    if change.kind == "PR body":
        return f"{_API_BASE}/repos/{repo}/pulls/{change.item_id}"
    if change.kind in ("issue comment", "PR comment"):
        return f"{_API_BASE}/repos/{repo}/issues/comments/{change.item_id}"
    if change.kind == "PR review comment":
        return f"{_API_BASE}/repos/{repo}/pulls/comments/{change.item_id}"
    raise ValueError(f"unknown change kind: {change.kind!r}")


def _patch_key(change: Change) -> str:
    """Return the JSON body key for the PATCH payload."""
    if change.kind in ("issue body", "PR body"):
        return "body"
    return "body"  # all comment types use "body"


def _notice_comment(change: Change) -> str:
    lines = "\n".join(f"  - {d}" for d in summarize_changes(change.descriptions))
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
# Display helpers
# ---------------------------------------------------------------------------


def _show_diff(original: str, redacted: str) -> None:
    orig_lines = original.splitlines()
    red_lines = redacted.splitlines()
    for o, r in zip(orig_lines, red_lines, strict=False):
        if o != r:
            print(f"  - {o[:120]}")
            print(f"  + {r[:120]}")
    # Handle length differences (rare — line count shouldn't change)
    for line in orig_lines[len(red_lines) :]:
        print(f"  - {line[:120]}")
    for line in red_lines[len(orig_lines) :]:
        print(f"  + {line[:120]}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write redactions to GitHub (default: dry-run only)",
    )
    parser.add_argument(
        "--repo",
        required=True,
        metavar="OWNER/REPO",
        help="GitHub repository to scan",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show before/after diff for each redaction",
    )
    parser.add_argument(
        "--no-comments",
        action="store_true",
        help="Skip posting notice comments after patching (avoids secondary rate limits on bulk runs)",
    )
    args = parser.parse_args()

    dry_run: bool = not args.apply
    repo: str = args.repo
    post_comments: bool = not args.no_comments

    if dry_run:
        print(f"[dry-run] Scanning {repo} — run with --apply to write changes\n")
    else:
        print(f"[apply] Scanning {repo} and writing redactions\n")

    # Fetch all issues and PRs (GitHub returns PRs via the issues endpoint)
    print("Fetching issues and PRs...", end=" ", flush=True)
    items: list[dict[str, Any]] = _get_paginated(f"{_API_BASE}/repos/{repo}/issues?state=all")
    pr_count = sum(1 for i in items if "pull_request" in i)
    issue_count = len(items) - pr_count
    print(f"{issue_count} issues, {pr_count} PRs")

    # Scan everything
    all_changes: list[Change] = []
    for item in items:
        item_changes = _scan_item(repo, item)
        all_changes.extend(item_changes)

    if not all_changes:
        print("\nNo PII found — nothing to do.")
        return

    # Summary
    print(f"\nFound {len(all_changes)} item(s) with PII:\n")
    for ch in all_changes:
        label = f"#{ch.number} {ch.kind}"
        n = len(ch.descriptions)
        noun = "redaction" if n == 1 else "redactions"
        types = ", ".join(sorted({d.split("→")[0].strip().strip("`").split("/")[0].strip() for d in ch.descriptions}))
        print(f"  {label:<35} {n} {noun}  ({types})")
        if args.verbose:
            _show_diff(ch.original, ch.redacted)
            print()

    if dry_run:
        print(f"\nRun with --apply to write {len(all_changes)} change(s).")
        return

    # Apply
    print()
    for ch in all_changes:
        url = _patch_url(repo, ch)
        key = _patch_key(ch)
        _patch(url, {key: ch.redacted}, dry_run=False)
        if post_comments:
            notice = _notice_comment(ch)
            _post_comment(repo, ch.number, notice, dry_run=False)
        print(f"  patched #{ch.number} {ch.kind} ({len(ch.descriptions)} redaction(s))")

    print(f"\nDone — {len(all_changes)} item(s) redacted.")


if __name__ == "__main__":
    main()
