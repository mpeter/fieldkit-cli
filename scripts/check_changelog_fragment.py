#!/usr/bin/env python3
"""Require a changelog.d/ fragment on every PR that changes non-doc files.

Replaces the unenforced "CHANGELOG.md updated" checkbox in the PR template.
Editing CHANGELOG.md directly is what made every concurrent PR collide on the
same lines of the single ``## [Unreleased]`` block; a fragment per change makes
the collision structurally impossible. See changelog.d/README.md.

Usage:
    uv run python scripts/check_changelog_fragment.py
    BASE_REF=origin/main uv run python scripts/check_changelog_fragment.py

Environment:
    BASE_REF         Branch to diff against. Default ``origin/main``.
    SKIP_CHANGELOG   When ``true``, waive the requirement. CI sets this from the
                     ``skip-changelog`` PR label.

Exit 0: fragment present, or the change is exempt (doc-only / waived / no diff).
Exit 1: non-doc files changed with no fragment added.
"""

import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _changelog_common import FRAGMENTS_DIRNAME, META_FRAGMENTS

REPO_ROOT = Path(__file__).resolve().parent.parent
FRAGMENTS_DIR = FRAGMENTS_DIRNAME

# Ceiling on any single git invocation. Without it a stale .git/index.lock (an
# IDE or a background `git gc`) hangs `make quality` with no output.
_GIT_TIMEOUT_SECONDS = 30

_PUBLIC_SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\.md")
_PRIVATE_TRACKER_RE = re.compile(r"(?i)(?<![a-z0-9])(?:bug|enh|bi)-[0-9]+(?![a-z0-9])")

# Files that do not on their own require a fragment. This glob list mirrors the
# `case` arm in the `changes` job of .github/workflows/ci.yml — one definition of
# "doc-only" for the whole repo. Parity is enforced by
# tests/test_changelog_fragments.py::test_doc_patterns_match_ci_workflow, because
# a silently-broadened list here makes the gate exit 0 while reporting OK.
#
# Only the glob list is shared; the two compute their diffs differently (the
# `changes` job uses a two-dot BASE_SHA..HEAD_SHA diff, this uses merge-base).
_DOC_PATTERNS: tuple[str, ...] = (
    "docs/*",
    "openspec/*",
    ".opencode/*",
    ".specify/*",
    "*.md",
)


def _git(*args: str) -> str:
    """Run a git command from the repo root and return stripped stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    return result.stdout.strip()


def _is_doc_only(path: str) -> bool:
    """Return True when *path* is exempt from the fragment requirement.

    Uses fnmatch, whose ``*`` spans ``/`` — matching the shell ``case`` globs in
    the CI `changes` job, where ``*.md`` matches a .md file at any depth.
    """
    return any(fnmatch.fnmatch(path, pattern) for pattern in _DOC_PATTERNS)


def _is_fragment(path: str) -> bool:
    """Return True when *path* is a real changelog fragment."""
    parts = Path(path).parts
    if len(parts) != 2 or parts[0] != FRAGMENTS_DIR:
        return False
    return parts[1] not in META_FRAGMENTS and parts[1].endswith(".md")


def _fragment_problem(path: str, content: str) -> str | None:
    """Return why a fragment is unsuitable for public release notes, if any."""
    name = Path(path).name
    if _PUBLIC_SLUG_RE.fullmatch(name) is None:
        return f"{path}: filename must be a lowercase descriptive slug"
    if _PRIVATE_TRACKER_RE.search(f"{name}\n{content}") is not None:
        return f"{path}: private tracker identifier is not allowed; cite a public issue as #<number>"

    lines = content.lstrip().splitlines()
    if not lines or not lines[0].startswith("### "):
        return f"{path}: content must start with a level-three heading"
    if not lines[0].removeprefix("### ").strip():
        return f"{path}: content must start with a descriptive heading"
    if not any(line.strip() for line in lines[1:]):
        return f"{path}: content must describe an observable result below the heading"
    return None


def _fragment_problems(paths: list[str]) -> list[str]:
    """Return public-release format problems for added fragment paths."""
    problems: list[str] = []
    for path in paths:
        try:
            content = (REPO_ROOT / path).read_text(encoding="utf-8")
        except OSError as exc:
            problems.append(f"{path}: cannot read fragment: {exc}")
            continue
        if problem := _fragment_problem(path, content):
            problems.append(problem)
    return problems


def _changed_files(merge_base: str) -> list[str]:
    """Return paths changed from *merge_base* to the working tree.

    Includes untracked-but-not-ignored files: ``git diff`` alone omits them, so a
    branch whose only code change is a brand-new file would pass locally and then
    fail in CI once committed. Diffing against the working tree (not HEAD) and
    folding in untracked files makes the verdict identical before and after
    ``git commit``.
    """
    tracked = _git("diff", "--name-only", merge_base).splitlines()
    untracked = _git("ls-files", "--others", "--exclude-standard").splitlines()
    return [line for line in [*tracked, *untracked] if line]


def _added_fragments(merge_base: str) -> list[str]:
    """Return fragments added since *merge_base*, including untracked ones."""
    added = _git("diff", "--name-only", "--diff-filter=A", merge_base).splitlines()
    untracked = _git("ls-files", "--others", "--exclude-standard", "--", FRAGMENTS_DIR).splitlines()
    return [path for path in [*added, *untracked] if path and _is_fragment(path)]


def _fail(changed_code: list[str]) -> int:
    """Print the remediation message for a missing fragment and return exit 1."""
    print("ERROR: this change touches non-doc files but adds no changelog fragment.", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"Non-doc files changed ({len(changed_code)}):", file=sys.stderr)
    for path in sorted(changed_code)[:10]:
        print(f"  {path}", file=sys.stderr)
    if len(changed_code) > 10:
        print(f"  … and {len(changed_code) - 10} more", file=sys.stderr)
    print("", file=sys.stderr)
    print("Do NOT edit CHANGELOG.md directly — that is what causes the merge conflicts.", file=sys.stderr)
    print("Fix one of:", file=sys.stderr)
    print(
        f"  1. Add {FRAGMENTS_DIR}/<issue-slug>.md  (e.g. {FRAGMENTS_DIR}/fix-bare-pytest-raises.md)",
        file=sys.stderr,
    )
    print(f"     Format and examples: {FRAGMENTS_DIR}/README.md", file=sys.stderr)
    print("  2. Apply the 'skip-changelog' label to the PR (refactor / CI-only work).", file=sys.stderr)
    return 1


def main() -> int:
    """Check that a fragment accompanies any non-doc change.

    Returns:
        0 when a fragment is present or the change is exempt.
        1 when non-doc files changed with no fragment added.
    """
    if os.environ.get("SKIP_CHANGELOG", "").lower() == "true":
        print("OK: SKIP_CHANGELOG set — fragment requirement waived.")
        return 0

    base_ref = os.environ.get("BASE_REF", "origin/main")
    try:
        merge_base = _git("merge-base", base_ref, "HEAD")
    except subprocess.CalledProcessError:
        print(
            f"ERROR: base ref {base_ref!r} not resolvable — run `git fetch origin` first.",
            file=sys.stderr,
        )
        return 1

    changed = _changed_files(merge_base)
    if not changed:
        print(f"OK: no files changed vs {base_ref}.")
        return 0

    changed_code = [path for path in changed if not _is_doc_only(path)]
    if not changed_code:
        print(f"OK: {len(changed)} file(s) changed, all doc-only — no fragment required.")
        return 0

    added = _added_fragments(merge_base)
    if added:
        problems = _fragment_problems(added)
        if problems:
            print("ERROR: invalid changelog fragment(s):", file=sys.stderr)
            for problem in problems:
                print(f"  {problem}", file=sys.stderr)
            return 1
        print(f"OK: fragment(s) added: {', '.join(sorted(added))}")
        return 0

    return _fail(changed_code)


if __name__ == "__main__":
    sys.exit(main())
