"""Candidate identity and cleanliness checks shared by release verifiers."""

import os
from pathlib import Path

from fieldkit.util.bounded_process import BoundedProcessBytesResult, BoundedProcessError, run_bounded_process_bytes

_GIT_TIMEOUT_SECONDS = 10
_GIT_OUTPUT_LIMIT_BYTES = 16 * 1024 * 1024
_GIT_STDERR_LIMIT_BYTES = 64 * 1024
_GIT_CLEANUP_TIMEOUT_SECONDS = 5


def git_environment() -> dict[str, str]:
    """Keep identity, cleanliness and export bound to the explicit repository."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_NO_REPLACE_OBJECTS": "1"})
    return environment


def _git(repo: Path, *arguments: str) -> BoundedProcessBytesResult:
    try:
        return run_bounded_process_bytes(
            ["git", "-C", str(repo), *arguments],
            timeout=_GIT_TIMEOUT_SECONDS,
            stdout_limit=_GIT_OUTPUT_LIMIT_BYTES,
            stderr_limit=_GIT_STDERR_LIMIT_BYTES,
            cleanup_timeout=_GIT_CLEANUP_TIMEOUT_SECONDS,
            env=git_environment(),
        )
    except BoundedProcessError as error:
        raise ValueError("repository identity command exceeded resource bounds") from error


def head_revision(repo: Path) -> str:
    """Return a canonical commit SHA or fail when Git cannot establish it."""
    result = _git(repo, "rev-parse", "HEAD")
    if result.returncode != 0:
        raise ValueError("repository HEAD is unavailable")
    revision = result.stdout.decode("ascii").strip()
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        raise ValueError("repository HEAD is not a full lowercase commit SHA")
    return revision


def require_clean_worktree(repo: Path) -> None:
    """Reject staged, unstaged, and untracked content from candidate evidence."""
    command = [
        "-c",
        "core.fileMode=true",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "core.ignoreStat=false",
    ]
    entries = _git(repo, *command, "ls-files", "-v", "-z")
    if entries.returncode != 0:
        raise ValueError("repository index flags are unavailable")
    if any(entry and (entry[:1].islower() or entry[:1] == b"S") for entry in entries.stdout.split(b"\0")):
        raise ValueError("candidate verification rejects assume-unchanged or skip-worktree index flags")
    result = _git(repo, *command, "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none")
    if result.returncode != 0:
        raise ValueError("repository worktree status is unavailable")
    if result.stdout:
        raise ValueError("candidate verification requires a clean worktree")
