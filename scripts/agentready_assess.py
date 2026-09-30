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

The caller passes the package spec as an explicit assertion. It must match
the canonical AgentReady policy used to verify report versions and scores;
an unsupported version is rejected before execution.

Exit 0 requires successful assessment, worktree cleanup, and fresh report
publication. Operational or evidence errors return 1; invalid arguments return
2. Nonzero Git creation/cleanup or assessor exit codes are forwarded, with
cleanup failures taking precedence over the assessment result.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

if __package__:
    from scripts import agentready_output, agentready_policy, git_worktree
else:
    import agentready_output
    import agentready_policy
    import git_worktree

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
    """Assess clean HEAD and publish fresh evidence only after successful cleanup."""
    if len(sys.argv) != 2 or sys.argv[1] != agentready_policy.PACKAGE_SPEC:
        print(
            "usage: agentready_assess.py agentready==<version>\n"
            "The caller must use the version required by the canonical AgentReady policy.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    package_spec = agentready_policy.PACKAGE_SPEC

    try:
        git_worktree.require_clean_worktree(_REPO_ROOT)
        run_output = agentready_output.begin_run(_OUTPUT_DIR)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"agentready_assess: {error}", file=sys.stderr)
        raise SystemExit(1) from error

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
                        str(run_output),
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
    if assessed.returncode == 0:
        try:
            agentready_output.publish_run(run_output, _OUTPUT_DIR)
        except (OSError, ValueError) as error:
            print(f"agentready_assess: no fresh report: {error}", file=sys.stderr)
            raise SystemExit(1) from error
    raise SystemExit(assessed.returncode)


if __name__ == "__main__":
    main()
