"""Covers-as-locks scheduling for the driver loop.

Selects which ``agent-ready`` issues may execute in a tick:

1. a candidate's work-order ``covers:`` must not intersect the *busy set*
   (files changed by any open PR targeting ``main``),
2. every issue listed in its ``depends_on:`` frontmatter must be closed,
3. candidates batched into the same tick must have pairwise-disjoint covers.

``select_runnable`` is pure (no gh I/O) — all GitHub reads live in
``busy_files`` / ``open_issue_numbers`` so selection is unit-testable
without mocks. Frontmatter parsing is regex-based (no YAML dependency),
mirroring ``scripts/check_doc_freshness.py``.

Spec: openspec/specs/driver-scheduling/spec.md
"""

import json
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fieldkit.driver.github import AgentIssue
from fieldkit.errors import FieldkitError

_GH_TIMEOUT = 30  # seconds; matches fieldkit.driver.github

# GraphQL caps the files connection gh pr list reads at this many entries;
# a PR reporting exactly this count may be truncated and needs REST pagination.
_PR_LIST_FILES_CAP = 100

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---", re.DOTALL)
# Same conventions as scripts/check_doc_freshness.py: a covers block is the
# `covers:` key followed by indented `- item` lines.
_COVERS_BLOCK_RE = re.compile(r"^covers:\s*\n((?:\s+-\s+.+\n?)*)", re.MULTILINE)
_COVERS_ITEM_RE = re.compile(r"^\s+-\s+(.+)$", re.MULTILINE)
_DEPENDS_INLINE_RE = re.compile(r"^depends_on:\s*\[(.*)\]\s*$", re.MULTILINE)
_DEPENDS_BLOCK_RE = re.compile(r"^depends_on:\s*\n((?:\s+-\s+.+\n?)*)", re.MULTILINE)
_ISSUE_NUMBER_RE = re.compile(r"(\d+)")


class SchedulerError(FieldkitError):
    """Busy-set or dependency lookup failed; the caller must fail closed."""


@dataclass(frozen=True)
class SkipReason:
    """Why a candidate issue was not selected this tick."""

    issue_number: int
    kind: Literal["busy-file", "depends-open", "batch-overlap", "no-covers"]
    detail: str

    def __str__(self) -> str:
        return f"#{self.issue_number} [{self.kind}] {self.detail}"


def _frontmatter_text(work_order_path: Path) -> str | None:
    try:
        text = work_order_path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _FRONTMATTER_RE.match(text)
    return match.group(1) if match else None


def parse_covers(work_order_path: Path) -> frozenset[str] | None:
    """Return the covers set, or None when absent/empty (conflicts with everything)."""
    fm_text = _frontmatter_text(work_order_path)
    if fm_text is None:
        return None
    block = _COVERS_BLOCK_RE.search(fm_text + "\n")
    if block is None:
        return None
    items = frozenset(item.strip() for item in _COVERS_ITEM_RE.findall(block.group(1)))
    return items or None


def parse_depends_on(work_order_path: Path) -> tuple[int, ...]:
    """Return the issue numbers listed under ``depends_on:`` (empty if absent)."""
    fm_text = _frontmatter_text(work_order_path)
    if fm_text is None:
        return ()
    inline = _DEPENDS_INLINE_RE.search(fm_text)
    if inline is not None:
        raw_items: list[str] = inline.group(1).split(",")
    else:
        block = _DEPENDS_BLOCK_RE.search(fm_text + "\n")
        if block is None:
            return ()
        raw_items = _COVERS_ITEM_RE.findall(block.group(1))
    numbers: list[int] = []
    for item in raw_items:
        found = _ISSUE_NUMBER_RE.search(item)
        if found is not None:
            numbers.append(int(found.group(1)))
    return tuple(dict.fromkeys(numbers))


def _gh_json(args: list[str]) -> object:
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise SchedulerError(f"gh {args[0]} failed: {exc}") from exc
    if result.returncode != 0:
        raise SchedulerError(f"gh {args[0]} failed: {result.stderr.strip()[:300]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SchedulerError(f"gh {args[0]} returned unparseable JSON: {exc}") from exc


def busy_files(repo: str) -> dict[str, int]:
    """Map each file changed by an open PR targeting main to a blocking PR number.

    Raises SchedulerError on any gh failure — callers must fail closed
    (run nothing this tick) rather than proceed with a partial busy set.
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
            "200",
            "--json",
            "number,files",
        ]
    )
    if not isinstance(listing, list):
        raise SchedulerError("gh pr list returned a non-list payload")
    busy: dict[str, int] = {}
    for pr in listing:
        if not isinstance(pr, dict):
            raise SchedulerError("gh pr list returned a non-object PR entry")
        number = pr.get("number")
        files = pr.get("files")
        if not isinstance(number, int) or not isinstance(files, list):
            raise SchedulerError("gh pr list PR entry missing number/files")
        paths = [path for f in files if isinstance(f, dict) if isinstance(path := f.get("path"), str)]
        if len(paths) >= _PR_LIST_FILES_CAP:
            # The files connection may be truncated; refetch the complete
            # set via the paginated REST endpoint. Per-PR call justified
            # (implementation note): only PRs at the cap are refetched, and correctness
            # requires the complete file set (fail-closed spec).
            paths = _pr_files_paginated(repo, number)
        for path in paths:
            busy.setdefault(path, number)
    return busy


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
        for entry in page:
            if isinstance(entry, dict) and isinstance(entry.get("filename"), str):
                filenames.append(entry["filename"])
    return filenames


def open_issue_numbers(repo: str, numbers: Iterable[int]) -> frozenset[int]:
    """Return the subset of ``numbers`` that are open or unverifiable.

    A nonexistent issue (or any gh failure) is treated as open — fail
    closed — so a typo in ``depends_on:`` blocks execution instead of
    silently unlocking it. Per-number gh calls justified (implementation note):
    depends_on lists are single-digit length.
    """
    open_set: set[int] = set()
    for number in sorted(set(numbers)):
        try:
            payload = _gh_json(["api", f"repos/{repo}/issues/{number}"])
        except SchedulerError:
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
    candidates: Sequence[tuple[AgentIssue, Path]],
    busy: Mapping[str, int],
    open_deps: frozenset[int],
    max_concurrent: int,
) -> tuple[list[tuple[AgentIssue, Path]], list[SkipReason]]:
    """Select up to ``max_concurrent`` runnable candidates, preserving order.

    ``candidates`` arrive oldest-first (from ``list_ready_issues``); the
    scheduler filters but never reorders. A work order with missing or
    empty ``covers:`` conflicts with everything: it runs only when the
    busy set is empty and it is alone in the batch.
    """
    selected: list[tuple[AgentIssue, Path]] = []
    selected_covers: list[tuple[int, frozenset[str]]] = []
    selected_universal = False
    skips: list[SkipReason] = []

    for issue, work_order_path in candidates:
        if len(selected) >= max_concurrent:
            break  # capacity reached; the rest simply wait for the next tick

        blocked_deps = sorted(set(parse_depends_on(work_order_path)) & open_deps)
        if blocked_deps:
            deps_text = ", ".join(f"#{n}" for n in blocked_deps)
            skips.append(SkipReason(issue.number, "depends-open", f"waiting on open issue(s) {deps_text}"))
            continue

        covers = parse_covers(work_order_path)
        if covers is None:
            if busy:
                sample_path, sample_pr = next(iter(sorted(busy.items())))
                detail = (
                    f"no covers frontmatter — runs only in isolation; "
                    f"{len(busy)} file(s) busy (e.g. {sample_path} in PR #{sample_pr})"
                )
                skips.append(SkipReason(issue.number, "no-covers", detail))
                continue
            if selected:
                detail = "no covers frontmatter — runs only in isolation; batch already has selected issue(s)"
                skips.append(SkipReason(issue.number, "no-covers", detail))
                continue
            selected.append((issue, work_order_path))
            selected_universal = True
            continue

        hit = _busy_hit(covers, busy)
        if hit is not None:
            busy_path, pr_number = hit
            skips.append(SkipReason(issue.number, "busy-file", f"{busy_path} changed by open PR #{pr_number}"))
            continue

        if selected_universal:
            other_number = selected[0][0].number
            skips.append(
                SkipReason(issue.number, "batch-overlap", f"batch issue #{other_number} has unrestricted covers")
            )
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
