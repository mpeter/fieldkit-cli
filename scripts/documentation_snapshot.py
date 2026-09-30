"""Materialize history-free fixed inputs for documentation verification."""

import hashlib
import io
import os
import posixpath
import re
import selectors
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, BinaryIO

from fieldkit.util.bounded_process import (
    BoundedProcessError,
    ProcessOwnershipLostError,
    process_exited_unreaped,
    require_unreaped_exit_observation,
    terminate_process_group,
)

if TYPE_CHECKING or __package__:
    from scripts.git_worktree import git_environment
    from scripts.install_git_hooks import _move_no_replace
else:  # pragma: no cover - direct script execution
    from git_worktree import git_environment
    from install_git_hooks import _move_no_replace

_GIT_TIMEOUT_SECONDS = 120
_CLEANUP_TIMEOUT_SECONDS = 3
_MAX_ENTRIES = 10_000
_MAX_BLOB_BYTES = 64 * 1024 * 1024
_MAX_TREE_BYTES = 512 * 1024 * 1024


def _safe_path(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name) and not path.is_absolute() and ".." not in path.parts and ".git" not in path.parts


def _blob_id(content: bytes) -> str:
    """Compute Git's content identity, not a cryptographic assurance claim."""
    return hashlib.sha1(f"blob {len(content)}\0".encode() + content, usedforsecurity=False).hexdigest()


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    """Terminate and reap the isolated Git process group."""
    try:
        terminate_process_group(process, cleanup_timeout=_CLEANUP_TIMEOUT_SECONDS)
    except (BoundedProcessError, OSError, subprocess.SubprocessError):
        raise ValueError("candidate Git cleanup did not complete") from None


def _stream_git(
    repo_root: Path,
    arguments: tuple[str, ...],
    output: BinaryIO,
    *,
    max_bytes: int,
    bound_name: str,
) -> None:
    """Stream Git output with hard byte/time limits and complete child cleanup."""
    try:
        require_unreaped_exit_observation()
        process = subprocess.Popen(
            ("git", "--no-replace-objects", *arguments),
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=git_environment(),
            start_new_session=True,
        )
    except (BoundedProcessError, OSError):
        raise ValueError("candidate Git command could not start") from None
    selector: selectors.BaseSelector | None = None
    owned = True
    try:
        if process.stdout is None:
            raise ValueError("candidate Git command has no output stream")
        try:
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
        except (OSError, ValueError):
            raise ValueError("candidate Git output selector is unavailable") from None
        deadline = time.monotonic() + _GIT_TIMEOUT_SECONDS
        total = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("candidate Git command exceeded its time bound")
            if not selector.select(remaining):
                raise ValueError("candidate Git command exceeded its time bound")
            chunk = os.read(process.stdout.fileno(), 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"{bound_name} exceeds verification bound")
            written = output.write(chunk)
            if written is not None and written != len(chunk):
                raise OSError("candidate snapshot output write was incomplete")
        while not process_exited_unreaped(process):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("candidate Git command exceeded its time bound")
            time.sleep(min(0.01, remaining))
    except ProcessOwnershipLostError:
        owned = False
        raise ValueError("candidate Git process ownership was lost") from None
    finally:
        try:
            if selector is not None:
                selector.close()
        finally:
            try:
                if process.stdout is not None:
                    process.stdout.close()
            finally:
                if owned:
                    _stop_process(process)
    if process.returncode != 0:
        raise ValueError("candidate Git command failed")


def _inventory(repo_root: Path, revision: str) -> dict[str, tuple[str, str, int]]:
    with tempfile.TemporaryFile() as output:
        _stream_git(
            repo_root,
            ("ls-tree", "-rlz", "--full-tree", revision),
            output,
            max_bytes=_MAX_ENTRIES * 4096,
            bound_name="candidate tree inventory",
        )
        output.seek(0)
        records = output.read().split(b"\0")
    entries: dict[str, tuple[str, str, int]] = {}
    for record in filter(None, records):
        metadata, raw_path = record.split(b"\t", 1)
        mode, kind, oid, raw_size = metadata.decode("ascii").split()
        path = raw_path.decode("utf-8")
        if kind != "blob" or mode not in {"100644", "100755", "120000"} or not _safe_path(path):
            raise ValueError("candidate tree contains an unsupported entry")
        size = int(raw_size)
        if path in entries or size > _MAX_BLOB_BYTES:
            raise ValueError("candidate tree contains an ambiguous or oversized entry")
        entries[path] = (mode, oid, size)
    if len(entries) > _MAX_ENTRIES or sum(entry[2] for entry in entries.values()) > _MAX_TREE_BYTES:
        raise ValueError("candidate tree exceeds verification bound")
    return entries


def _member_content(archive: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    if member.issym():
        target = member.linkname
        if not target:
            raise ValueError("candidate tree contains an unsafe symlink")
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(member.name), target))
        if PurePosixPath(target).is_absolute() or not _safe_path(resolved):
            raise ValueError("candidate tree contains an unsafe symlink")
        return target.encode("utf-8")
    stream = archive.extractfile(member)
    if stream is None:
        raise ValueError("candidate archive contains an unreadable entry")
    with stream:
        return stream.read(_MAX_BLOB_BYTES + 1)


def materialize_candidate(repo_root: Path, revision: str, destination: Path) -> None:
    """Materialize and verify exactly one committed tree without Git history."""
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("candidate snapshot requires a full immutable commit identity")
    try:
        source = repo_root.resolve(strict=True)
    except OSError:
        raise ValueError("candidate snapshot source is unavailable") from None
    object_output = io.BytesIO()
    _stream_git(
        source,
        ("cat-file", "-t", revision),
        object_output,
        max_bytes=32,
        bound_name="candidate object type",
    )
    object_type = object_output.getvalue().strip()
    if object_type != b"commit":
        raise ValueError("candidate snapshot requires a commit object")
    if destination.exists() or destination.is_symlink() or destination.resolve().is_relative_to(source):
        raise ValueError("candidate snapshot destination must be new and outside the source tree")
    if not destination.parent.is_dir():
        raise ValueError("candidate snapshot destination parent must exist")
    entries = _inventory(source, revision)
    with tempfile.TemporaryFile() as output:
        _stream_git(
            source,
            ("archive", "--format=tar", revision),
            output,
            max_bytes=_MAX_TREE_BYTES + _MAX_ENTRIES * 16_384,
            bound_name="candidate archive",
        )
        output.seek(0)
        with tarfile.open(fileobj=output, mode="r:") as archive:
            members: dict[str, tarfile.TarInfo] = {}
            for member in archive:
                if not _safe_path(member.name):
                    raise ValueError("candidate archive contains an unsafe path")
                if member.isdir():
                    continue
                if not (member.isfile() or member.issym()) or member.name in members:
                    raise ValueError("candidate archive contains an ambiguous entry")
                expected = entries.get(member.name)
                if expected is None:
                    raise ValueError("candidate archive does not match committed tree")
                mode, oid, size = expected
                content = _member_content(archive, member)
                if (
                    len(content) != size
                    or _blob_id(content) != oid
                    or member.issym() != (mode == "120000")
                    or (member.isfile() and bool(member.mode & 0o111) != (mode == "100755"))
                ):
                    raise ValueError("candidate archive does not match committed tree")
                members[member.name] = member
            if set(members) != set(entries):
                raise ValueError("candidate archive does not match committed tree")
            staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
            try:
                for name, member in sorted(members.items()):
                    target = staging / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if member.issym():
                        target.symlink_to(member.linkname)
                    else:
                        target.write_bytes(_member_content(archive, member))
                        target.chmod(0o755 if entries[name][0] == "100755" else 0o644)
                _move_no_replace(staging, destination)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
