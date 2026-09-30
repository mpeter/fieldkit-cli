"""Deterministically bind a quality run to its current Git worktree inputs."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from fieldkit.util.bounded_process import run_bounded_process_bytes

if TYPE_CHECKING or __package__:
    from scripts.git_worktree import git_environment
else:
    from git_worktree import git_environment

WorktreeState = Literal["clean", "dirty"]

GIT_TIMEOUT_SECONDS = 30
GIT_OUTPUT_LIMIT_BYTES = 16 * 1024 * 1024
CLEANUP_TIMEOUT_SECONDS = 5
MAX_ENTRY_COUNT = 100_000
MAX_PATH_BYTES = 4096
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_WORKTREE_BYTES = 2 * 1024 * 1024 * 1024
MAX_SYMLINK_BYTES = 4096

_REVISION = re.compile(r"[0-9a-f]{40}")


class _HashWriter(Protocol):
    def update(self, value: bytes, /) -> None: ...


@dataclass(frozen=True)
class SourceState:
    """Payload-safe binding to the repository state observed by the controller."""

    head: str
    head_tree: str
    worktree: WorktreeState
    status_sha256: str
    index_sha256: str
    worktree_sha256: str
    dirty_entries: int


def _git(repo: Path, *arguments: str) -> bytes:
    result = run_bounded_process_bytes(
        ["git", *arguments],
        timeout=GIT_TIMEOUT_SECONDS,
        stdout_limit=GIT_OUTPUT_LIMIT_BYTES,
        stderr_limit=64 * 1024,
        cleanup_timeout=CLEANUP_TIMEOUT_SECONDS,
        cwd=repo,
        env=git_environment(),
    )
    if result.returncode != 0:
        raise ValueError("repository identity command failed")
    return result.stdout


def _record(digest: _HashWriter, kind: bytes, *values: bytes) -> None:
    digest.update(len(kind).to_bytes(2, "big"))
    digest.update(kind)
    for value in values:
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)


def _entry_paths(repo: Path) -> tuple[bytes, ...]:
    raw = _git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if raw and not raw.endswith(b"\0"):
        raise ValueError("repository path inventory is not NUL terminated")
    paths = tuple(item for item in raw.split(b"\0") if item)
    if len(paths) > MAX_ENTRY_COUNT:
        raise ValueError("repository path inventory exceeds its entry bound")
    if len(paths) != len(set(paths)):
        raise ValueError("repository path inventory contains duplicate entries")
    for path in paths:
        parts = path.split(b"/")
        if (
            len(path) > MAX_PATH_BYTES
            or path.startswith(b"/")
            or not parts
            or any(part in {b"", b".", b".."} for part in parts)
        ):
            raise ValueError("repository path inventory contains an unsafe path")
    return tuple(sorted(paths))


def _entry_path(repo: Path, raw_path: bytes) -> bytes | None:
    root = os.fsencode(repo)
    current = root
    for part in raw_path.split(b"/")[:-1]:
        current = os.path.join(current, part)  # noqa: PTH118 - preserve raw Git path bytes
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            return None
        except OSError as error:
            raise ValueError("repository entry ancestor could not be inspected") from error
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise ValueError("repository entry has an unsafe ancestor")
    return os.path.join(root, raw_path)  # noqa: PTH118 - preserve raw Git path bytes


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_dev,
        value.st_ino,
    )


def _hash_regular_file(digest: _HashWriter, path: bytes, raw_path: bytes, before: os.stat_result) -> int:
    if before.st_size > MAX_FILE_BYTES:
        raise ValueError("repository entry exceeds its file-size bound")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError("repository entry could not be opened safely") from error
    content_digest = hashlib.sha256()
    consumed = 0
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _stat_identity(opened) != _stat_identity(before):
            raise ValueError("repository entry changed while being opened")
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            consumed += len(chunk)
            if consumed > MAX_FILE_BYTES:
                raise ValueError("repository entry exceeds its file-size bound")
            content_digest.update(chunk)
        after_read = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        after_close = os.lstat(path)
    except OSError as error:
        raise ValueError("repository entry disappeared while being read") from error
    if (
        consumed != before.st_size
        or _stat_identity(after_read) != _stat_identity(before)
        or _stat_identity(after_close) != _stat_identity(before)
    ):
        raise ValueError("repository entry changed while being read")
    _record(
        digest,
        b"regular",
        raw_path,
        before.st_mode.to_bytes(8, "big"),
        before.st_size.to_bytes(8, "big"),
        content_digest.digest(),
    )
    return consumed


def _hash_symlink(digest: _HashWriter, path: bytes, raw_path: bytes, before: os.stat_result) -> None:
    try:
        target = os.readlink(path)  # noqa: PTH115 - preserve raw Git path bytes
        after = os.lstat(path)
    except OSError as error:
        raise ValueError("repository symlink could not be inspected") from error
    target_bytes = target if isinstance(target, bytes) else os.fsencode(target)
    if len(target_bytes) > MAX_SYMLINK_BYTES:
        raise ValueError("repository symlink target exceeds its byte bound")
    if _stat_identity(after) != _stat_identity(before):
        raise ValueError("repository symlink changed while being read")
    _record(digest, b"symlink", raw_path, before.st_mode.to_bytes(8, "big"), target_bytes)


def _worktree_digest(repo: Path, paths: tuple[bytes, ...]) -> str:
    digest = hashlib.sha256()
    total_bytes = 0
    for raw_path in paths:
        path = _entry_path(repo, raw_path)
        if path is None:
            _record(digest, b"missing", raw_path)
            continue
        try:
            before = os.lstat(path)
        except FileNotFoundError:
            _record(digest, b"missing", raw_path)
            continue
        except OSError as error:
            raise ValueError("repository entry could not be inspected") from error
        if stat.S_ISREG(before.st_mode):
            total_bytes += _hash_regular_file(digest, path, raw_path, before)
            if total_bytes > MAX_WORKTREE_BYTES:
                raise ValueError("repository worktree exceeds its content bound")
        elif stat.S_ISLNK(before.st_mode):
            _hash_symlink(digest, path, raw_path, before)
        else:
            raise ValueError("repository inventory contains an unsupported entry type")
    return digest.hexdigest()


def capture_source_state(repo: Path) -> SourceState:
    """Capture a stable revision, status, and current-content worktree binding."""
    resolved_repo = repo.resolve(strict=True)
    if not resolved_repo.is_dir() or resolved_repo.is_symlink():
        raise ValueError("quality source root must be a regular directory")
    top_level = _git(resolved_repo, "rev-parse", "--show-toplevel")
    try:
        canonical_top_level = Path(top_level.decode("utf-8").removesuffix("\n")).resolve(strict=True)
    except (OSError, UnicodeError) as error:
        raise ValueError("repository top level could not be verified") from error
    if canonical_top_level != resolved_repo:
        raise ValueError("quality source root is not the canonical repository top level")
    head = _git(resolved_repo, "rev-parse", "--verify", "HEAD").decode("ascii").strip().lower()
    tree = _git(resolved_repo, "rev-parse", "--verify", "HEAD^{tree}").decode("ascii").strip().lower()
    if _REVISION.fullmatch(head) is None or _REVISION.fullmatch(tree) is None:
        raise ValueError("repository identity is not a full Git object identifier")

    status_before = _git(
        resolved_repo,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--no-renames",
    )
    index_before = _git(resolved_repo, "ls-files", "--stage", "-z")
    paths_before = _entry_paths(resolved_repo)
    worktree_sha256 = _worktree_digest(resolved_repo, paths_before)
    paths_after = _entry_paths(resolved_repo)
    index_after = _git(resolved_repo, "ls-files", "--stage", "-z")
    status_after = _git(
        resolved_repo,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--no-renames",
    )
    head_after = _git(resolved_repo, "rev-parse", "--verify", "HEAD").decode("ascii").strip().lower()
    tree_after = _git(resolved_repo, "rev-parse", "--verify", "HEAD^{tree}").decode("ascii").strip().lower()
    if (
        head != head_after
        or tree != tree_after
        or paths_before != paths_after
        or index_before != index_after
        or status_before != status_after
    ):
        raise ValueError("repository changed while source state was captured")
    entries = sum(1 for item in status_before.split(b"\0") if item)
    return SourceState(
        head=head,
        head_tree=tree,
        worktree="clean" if entries == 0 else "dirty",
        status_sha256=hashlib.sha256(status_before).hexdigest(),
        index_sha256=hashlib.sha256(index_before).hexdigest(),
        worktree_sha256=worktree_sha256,
        dirty_entries=entries,
    )
