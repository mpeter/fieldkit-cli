#!/usr/bin/env python3
"""Run ``agentready assess`` against a clean checkout of HEAD.

AgentReady's structure assessors enumerate source files with a naive
``Path.rglob`` rooted at the repository directory — they do not honour
``.gitignore`` and do not skip dependency or scratch directories. Assessing the
live working tree therefore counts every vendored file under ``.venv/``
(installed by ``uv sync --all-extras --dev``) and every nested clone under ``.claude/worktrees/``.
That pollution tanks ``separation_of_concerns`` (dozens of third-party
``utils.py``/``helpers.py`` files → naming score 0) and inflates the file-count
statistics, failing the gate for reasons that have nothing to do with the
source under review.

To measure the source and nothing else, assess a *linked git worktree* checked
out at HEAD. A linked worktree contains only tracked files — no ``.venv``, no
build output, no nested worktrees — while still sharing the real ``.git`` dir,
so git-history attributes (conventional commits, ADR history, …) score exactly
as they do on the real repo. A ``git archive`` export would strip that history
and zero those attributes, so a worktree is used instead.

The assessment reports are written back into the real repo's ``.agentready/``
directory via ``--output-dir`` so ``check_agentready.py`` can read them.

The pinned ``uvx`` package spec (e.g. ``agentready==2.49.0``) is passed as the
sole argument so the version stays managed where every other tool pin lives —
inline in the Makefile and CI workflow — rather than duplicated in source.

Exit code is whatever ``agentready assess`` returns (0 on success).
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

# Generous ceilings; assess walks the whole tree and shells out to uvx.
_GIT_TIMEOUT_SECONDS = 120
_ASSESS_TIMEOUT_SECONDS = 600

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONFIG = _REPO_ROOT / ".agentready-config.yaml"
_OUTPUT_DIR = _REPO_ROOT / ".agentready"


def _run(cmd: list[str], *, timeout: int, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, timeout=timeout, check=False, cwd=cwd)


def _cleanup_failed_creation(worktree: Path) -> str:
    """Attempt cleanup after a failed worktree add and describe any failure."""
    try:
        removed = _run(
            ["git", "-C", str(_REPO_ROOT), "worktree", "remove", "--force", str(worktree)],
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"; cleanup error: {error}"
    if removed.returncode != 0:
        return f"; cleanup exit {removed.returncode}"
    return ""


def main() -> None:
    """Assess a clean HEAD worktree and forward agentready's exit code."""
    if len(sys.argv) != 2 or not sys.argv[1].startswith("agentready=="):
        print(
            "usage: agentready_assess.py agentready==<version>\n"
            "The version is pinned by the caller (Makefile / CI), not this script.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    package_spec = sys.argv[1]

    with tempfile.TemporaryDirectory(prefix="agentready-clean-") as tmp:
        # git worktree add wants a non-existent leaf path.
        worktree = Path(tmp) / "tree"
        try:
            added = _run(
                ["git", "-C", str(_REPO_ROOT), "worktree", "add", "--detach", str(worktree), "HEAD"],
                timeout=_GIT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            cleanup_detail = _cleanup_failed_creation(worktree)
            print(
                f"agentready_assess: failed to create clean worktree {worktree}: {error}{cleanup_detail}",
                file=sys.stderr,
            )
            raise SystemExit(1) from error
        if added.returncode != 0:
            cleanup_detail = _cleanup_failed_creation(worktree)
            print(
                f"agentready_assess: failed to create clean worktree {worktree} "
                f"(exit {added.returncode}){cleanup_detail}",
                file=sys.stderr,
            )
            raise SystemExit(added.returncode)

        assessed: subprocess.CompletedProcess[str] | None = None
        assessment_error: OSError | subprocess.TimeoutExpired | None = None
        removed: subprocess.CompletedProcess[str] | None = None
        removal_error: OSError | subprocess.TimeoutExpired | None = None
        try:
            try:
                assessed = _run(
                    [
                        "uvx",
                        package_spec,
                        "assess",
                        str(worktree),
                        "--config",
                        str(_CONFIG),
                        "--output-dir",
                        str(_OUTPUT_DIR),
                    ],
                    timeout=_ASSESS_TIMEOUT_SECONDS,
                    cwd=worktree,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                assessment_error = error
        finally:
            try:
                removed = _run(
                    ["git", "-C", str(_REPO_ROOT), "worktree", "remove", "--force", str(worktree)],
                    timeout=_GIT_TIMEOUT_SECONDS,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                removal_error = error

    assessment_outcome = (
        f"assessment exit {assessed.returncode}" if assessed is not None else f"assessment error: {assessment_error}"
    )
    if removal_error is not None:
        print(
            f"agentready_assess: failed to remove clean worktree {worktree}: {removal_error}; {assessment_outcome}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if removed is None:
        print(
            f"agentready_assess: no removal result for clean worktree {worktree}; {assessment_outcome}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if removed.returncode != 0:
        print(
            f"agentready_assess: failed to remove clean worktree {worktree} "
            f"(exit {removed.returncode}); {assessment_outcome}",
            file=sys.stderr,
        )
        raise SystemExit(removed.returncode)
    if assessment_error is not None:
        print(f"agentready_assess: assessment failed: {assessment_error}", file=sys.stderr)
        raise SystemExit(1)
    if assessed is None:
        print("agentready_assess: assessment produced no result", file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(assessed.returncode)


if __name__ == "__main__":
    main()
