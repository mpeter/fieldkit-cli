"""fieldkit.web.prs — PR queue data over the gh CLI.

The operator's daily valve is merge-or-bounce on the driver-generated PR
queue. This module lists open PRs with their CI rollup and performs the
two write actions (merge, comment) via ``gh`` — the same binary the
driver domain already depends on, invoked with an explicit ``-R`` repo
slug so the server's CWD is irrelevant.
"""

import json
import shutil
import subprocess
from typing import Any

from fieldkit.errors import WebDataError

_GH_TIMEOUT_SECONDS = 60

_LIST_FIELDS = "number,title,url,author,createdAt,isDraft,headRefName,statusCheckRollup,reviewDecision"


def run_gh(args: list[str]) -> str:
    """Run ``gh <args>`` and return stdout.

    Raises:
        WebDataError: When gh is missing, times out, or exits non-zero.
    """
    binary = shutil.which("gh")
    if binary is None:
        raise WebDataError("gh CLI not found on PATH — install GitHub CLI to use the PR queue")
    try:
        result = subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise WebDataError(f"gh {' '.join(args[:2])} timed out after {_GH_TIMEOUT_SECONDS}s") from exc
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "no stderr"
        raise WebDataError(f"gh {' '.join(args[:2])} exited {result.returncode}: {detail}")
    return result.stdout


def _ci_summary(rollup: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Collapse a statusCheckRollup list into pass/fail/pending counts."""
    passed = failed = pending = 0
    for check in rollup or []:
        # CheckRun uses conclusion; StatusContext uses state.
        verdict = (check.get("conclusion") or check.get("state") or "").upper()
        if verdict in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            passed += 1
        elif verdict in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"):
            failed += 1
        else:
            pending += 1
    if failed:
        state = "failing"
    elif pending:
        state = "pending"
    elif passed:
        state = "passing"
    else:
        state = "none"
    return {"state": state, "passed": passed, "failed": failed, "pending": pending}


def list_prs(repo: str, *, gh_runner: Any = run_gh) -> list[dict[str, Any]]:
    """Return open PRs for *repo* with a collapsed CI summary, newest first.

    Args:
        repo: GitHub slug, e.g. ``owner/name``.
        gh_runner: Injectable gh invocation (tests).
    """
    stdout = gh_runner(["pr", "list", "-R", repo, "--state", "open", "--json", _LIST_FIELDS])
    try:
        raw = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise WebDataError("gh pr list produced non-JSON output") from exc
    prs = [
        {
            "number": pr.get("number"),
            "title": pr.get("title"),
            "url": pr.get("url"),
            "author": (pr.get("author") or {}).get("login", ""),
            "created_at": pr.get("createdAt"),
            "is_draft": pr.get("isDraft", False),
            "branch": pr.get("headRefName", ""),
            "review_decision": pr.get("reviewDecision") or "",
            "ci": _ci_summary(pr.get("statusCheckRollup")),
        }
        for pr in raw
    ]
    return sorted(prs, key=lambda p: p.get("number") or 0, reverse=True)


def merge_pr(repo: str, number: int, *, gh_runner: Any = run_gh) -> str:
    """Squash-merge PR *number* and delete its branch.

    GitHub's own branch protection is the real gate — a red CI or missing
    required review makes gh exit non-zero, surfaced as WebDataError.
    """
    gh_runner(["pr", "merge", str(number), "-R", repo, "--squash", "--delete-branch"])
    return f"PR #{number} merged (squash) and branch deleted"


def comment_pr(repo: str, number: int, body: str, *, gh_runner: Any = run_gh) -> str:
    """Post *body* as a comment on PR *number* (the 'bounce' action)."""
    body = body.strip()
    if not body:
        raise WebDataError("empty comment body")
    gh_runner(["pr", "comment", str(number), "-R", repo, "--body", body])
    return f"comment posted on PR #{number}"
