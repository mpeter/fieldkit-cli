"""Covers-as-locks scheduling for the driver loop.

Selects which ``agent-ready`` issues may execute in a tick:

1. a candidate's work-order ``covers:`` must not intersect the *busy set*
   (files changed by any open PR targeting ``main``),
2. every issue listed in its ``depends_on:`` frontmatter must be closed,
3. candidates batched into the same tick must have pairwise-disjoint covers.

``select_runnable`` is pure (no gh I/O) — all GitHub reads live in
``busy_files`` / ``open_issue_numbers`` so selection is unit-testable
without mocks. Scheduling fields come from the canonical strict prompt-contract
parser used by the revision-bound execution path.

Spec: openspec/specs/driver-scheduling/spec.md
"""

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from fieldkit.config._timeouts import TIMEOUT_PROCESS_KILL_GRACE
from fieldkit.driver.github import AgentIssue, github_read_failure_kind
from fieldkit.driver.prompt_source import FrozenPrompt
from fieldkit.errors import AuthError, FieldkitError, GitHubRequestError
from fieldkit.util.bounded_process import BoundedProcessError, run_bounded_process

log = logging.getLogger(__name__)

_GH_TIMEOUT = 30  # seconds; matches fieldkit.driver.github
_PR_LIST_LIMIT = 200
_GH_RESPONSE_BYTES = 4 * 1024 * 1024

# GraphQL caps the files connection gh pr list reads at this many entries;
# a PR reporting exactly this count may be truncated and needs REST pagination.
_PR_LIST_FILES_CAP = 100
# GitHub's PR-files REST endpoint caps the complete response, even with pagination.
# https://docs.github.com/en/rest/pulls/pulls#list-pull-requests-files
_PR_FILES_API_CAP = 3000


class SchedulerError(FieldkitError):
    """Busy-set or dependency lookup failed; the caller must fail closed."""


@dataclass(frozen=True)
class SkipReason:
    """Why a candidate issue was not selected this tick."""

    issue_number: int
    kind: Literal["busy-file", "depends-open", "batch-overlap"]
    detail: str

    def __str__(self) -> str:
        return f"#{self.issue_number} [{self.kind}] {self.detail}"


def _gh_json(args: list[str]) -> object:
    try:
        result = run_bounded_process(
            ["gh", *args],
            timeout=_GH_TIMEOUT,
            stdout_limit=_GH_RESPONSE_BYTES,
            stderr_limit=_GH_RESPONSE_BYTES,
            cleanup_timeout=TIMEOUT_PROCESS_KILL_GRACE,
        )
    except BoundedProcessError as exc:
        raise GitHubRequestError("GitHub scheduling lookup did not complete") from exc
    if result.returncode != 0:
        kind = github_read_failure_kind(result.stderr)
        if kind == "authentication":
            raise AuthError("GitHub scheduling authentication failed")
        if kind == "not-found":
            raise SchedulerError("GitHub scheduling target was not found")
        raise GitHubRequestError("GitHub scheduling lookup failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SchedulerError("GitHub scheduling lookup returned invalid JSON") from exc


def busy_files(repo: str) -> dict[str, int]:
    """Map each file changed by an open PR targeting main to a blocking PR number.

    Authentication and retryable provider failures retain their shared typed
    exceptions. Invalid or incomplete scheduling data raises SchedulerError.
    Callers must fail closed rather than proceed with a partial busy set.
    """
    listing = _gh_json(
        [
            "pr",
            "list",
            "--repo",
            repo,
            "--base",
            "main",
            "--state",
            "open",
            "--limit",
            str(_PR_LIST_LIMIT),
            "--json",
            "number,files",
        ]
    )
    if not isinstance(listing, list):
        raise SchedulerError("gh pr list returned a non-list payload")
    if len(listing) >= _PR_LIST_LIMIT:
        raise SchedulerError("PR listing may be truncated; cannot establish the complete busy set")
    busy: dict[str, int] = {}
    for pr in listing:
        if not isinstance(pr, dict):
            raise SchedulerError("gh pr list returned a non-object PR entry")
        number = pr.get("number")
        files = pr.get("files")
        if not isinstance(number, int) or not isinstance(files, list):
            raise SchedulerError("gh pr list PR entry missing number/files")
        paths = _file_paths(files, key="path")
        if len(paths) >= _PR_LIST_FILES_CAP:
            # The files connection may be truncated; refetch the complete
            # set via the paginated REST endpoint. Per-PR call justified
            # (implementation note): only PRs at the cap are refetched, and correctness
            # requires the complete file set (fail-closed spec).
            complete_paths = _pr_files_paginated(repo, number)
            if not set(paths).issubset(complete_paths):
                raise SchedulerError("PR file listings disagree; cannot establish the complete busy set")
            paths = complete_paths
        for path in paths:
            busy.setdefault(path, number)
    return busy


def _file_paths(entries: list[object], *, key: str) -> list[str]:
    paths: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise SchedulerError("GitHub returned an invalid file entry")
        path = entry.get(key)
        if not isinstance(path, str) or not path:
            raise SchedulerError("GitHub returned an invalid file entry")
        paths.append(path)
    return paths


def _pr_files_paginated(repo: str, pr_number: int) -> list[str]:
    payload = _gh_json(
        [
            "api",
            "--paginate",
            "--slurp",
            f"repos/{repo}/pulls/{pr_number}/files",
        ]
    )
    if not isinstance(payload, list):
        raise SchedulerError(f"gh api pulls/{pr_number}/files returned a non-list payload")
    filenames: list[str] = []
    for page in payload:
        if not isinstance(page, list):
            raise SchedulerError(f"gh api pulls/{pr_number}/files returned a non-list page")
        filenames.extend(_file_paths(page, key="filename"))
    if len(filenames) >= _PR_FILES_API_CAP:
        raise SchedulerError("PR file listing may be truncated; cannot establish the complete busy set")
    return filenames


def open_issue_numbers(repo: str, numbers: Iterable[int]) -> frozenset[int]:
    """Return the subset of ``numbers`` that are open or unverifiable.

    A nonexistent or invalid issue is treated as open, so a typo in
    ``depends_on:`` blocks execution instead of silently unlocking it.
    Authentication and retryable provider failures propagate so the whole tick
    stops with the correct exit category. Per-number gh calls are justified:
    depends_on lists are single-digit length.
    """
    open_set: set[int] = set()
    for number in sorted(set(numbers)):
        try:
            payload = _gh_json(["api", f"repos/{repo}/issues/{number}"])
        except SchedulerError as exc:
            log.warning("Dependency issue #%d could not be verified and remains blocking: %s", number, exc)
            open_set.add(number)
            continue
        state = payload.get("state") if isinstance(payload, dict) else None
        if state != "closed":
            open_set.add(number)
    return frozenset(open_set)


def _paths_overlap(a: str, b: str) -> bool:
    """True when two covers/busy entries denote overlapping paths.

    Covers entries may be directories (trailing ``/``); a directory entry
    overlaps any path beneath it.
    """
    if a == b:
        return True
    if a.endswith("/") and b.startswith(a):
        return True
    return b.endswith("/") and a.startswith(b)


def _busy_hit(covers: frozenset[str], busy: Mapping[str, int]) -> tuple[str, int] | None:
    for cover in sorted(covers):
        for busy_path, pr_number in busy.items():
            if _paths_overlap(cover, busy_path):
                return busy_path, pr_number
    return None


def _covers_overlap(a: frozenset[str], b: frozenset[str]) -> str | None:
    for entry_a in sorted(a):
        for entry_b in sorted(b):
            if _paths_overlap(entry_a, entry_b):
                return entry_a
    return None


def select_runnable(
    candidates: Sequence[tuple[AgentIssue, FrozenPrompt]],
    busy: Mapping[str, int],
    open_deps: frozenset[int],
    max_concurrent: int,
) -> tuple[list[tuple[AgentIssue, FrozenPrompt]], list[SkipReason]]:
    """Select up to ``max_concurrent`` runnable candidates, preserving order.

    ``candidates`` arrive oldest-first (from ``list_ready_issues``); the
    scheduler filters but never reorders. Every candidate has already passed
    revision-bound prompt admission, including nonempty covers authority.
    """
    selected: list[tuple[AgentIssue, FrozenPrompt]] = []
    selected_covers: list[tuple[int, frozenset[str]]] = []
    skips: list[SkipReason] = []

    for issue, work_order_path in candidates:
        if len(selected) >= max_concurrent:
            break  # capacity reached; the rest simply wait for the next tick

        covers = work_order_path.contract.covers
        dependencies = work_order_path.contract.depends_on
        blocked_deps = sorted(set(dependencies) & open_deps)
        if blocked_deps:
            deps_text = ", ".join(f"#{n}" for n in blocked_deps)
            skips.append(SkipReason(issue.number, "depends-open", f"waiting on open issue(s) {deps_text}"))
            continue

        hit = _busy_hit(covers, busy)
        if hit is not None:
            busy_path, pr_number = hit
            skips.append(SkipReason(issue.number, "busy-file", f"{busy_path} changed by open PR #{pr_number}"))
            continue

        overlap_detail: SkipReason | None = None
        for other_number, other_covers in selected_covers:
            overlapping = _covers_overlap(covers, other_covers)
            if overlapping is not None:
                overlap_detail = SkipReason(
                    issue.number, "batch-overlap", f"{overlapping} overlaps batch issue #{other_number}"
                )
                break
        if overlap_detail is not None:
            skips.append(overlap_detail)
            continue

        selected.append((issue, work_order_path))
        selected_covers.append((issue.number, covers))

    return selected, skips
