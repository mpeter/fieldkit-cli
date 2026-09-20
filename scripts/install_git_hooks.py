#!/usr/bin/env python3
"""Install fieldkit Git hooks transactionally in the shared Git directory."""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import hashlib
import math
import os
import shutil
import stat
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import NoReturn

from scripts.hook_install_runtime import (
    InstallError,
    InstallInterrupted,
    TerminationSignals,
    _failure_description,
    _hooks_directory,
    _run,
    _termination_signals_as_interrupts,
)


@dataclass(frozen=True)
class HookSpec:
    """One installed hook and the source used to build it."""

    name: str
    source: PurePosixPath | None
    pre_commit_type: str | None


HOOKS = (
    HookSpec("pre-commit", None, "pre-commit"),
    HookSpec("pre-push", PurePosixPath("hooks/pre-push-git-hook"), None),
    HookSpec("commit-msg", None, "commit-msg"),
    HookSpec("post-commit", PurePosixPath("hooks/post_commit.py"), None),
)
HOOK_NAMES = tuple(hook.name for hook in HOOKS)
MAXIMUM_TIMEOUT_SECONDS = 3600
MAX_CUSTOM_HOOK_BYTES = 1024 * 1024
MAX_DISPLACED_UPDATE_EXCHANGES = 8
PathIdentity = tuple[int, int, int, int, int, int, bytes]


def _reverse_after_inspection_failure(
    staged: Path,
    destination: Path,
    preserved: set[Path],
    inspection_error: BaseException,
    removable_destination: PathIdentity | None = None,
) -> NoReturn:
    """Reverse an exchange whose displaced path could not be inspected."""
    try:
        _exchange_paths(staged, destination)
    except BaseException as exchange_error:  # noqa: BLE001 - preserve recovery through interruption
        preserved.add(staged)
        cleanup_error: BaseException | None = None
        if removable_destination is not None:
            try:
                _remove_matching_destination(destination, removable_destination, preserved)
            except BaseException as exc:  # noqa: BLE001 - retain every cleanup failure
                cleanup_error = exc
        failure = InstallError(
            f"cannot reverse rollback exchange for {destination}; recovery preserved at: {Path.cwd() / staged}"
        )
        failure.add_note(f"rollback inspection failed: {_failure_description(inspection_error)}")
        failure.add_note(f"reverse exchange failed: {_failure_description(exchange_error)}")
        if cleanup_error is not None:
            failure.add_note(f"placeholder cleanup failed: {_failure_description(cleanup_error)}")
        raise failure from (cleanup_error or exchange_error)
    raise inspection_error.with_traceback(inspection_error.__traceback__)


@dataclass
class Snapshot:
    """Restorable state for one hook destination."""

    destination: Path
    backup: Path | None
    original_identity: PathIdentity | None

    def restore(self, published_identity: PathIdentity, preserved: set[Path]) -> None:
        """Restore the captured path without following destination symlinks."""
        current_identity = _path_identity(self.destination)
        if current_identity == self.original_identity:
            return
        if current_identity != published_identity:
            raise InstallError(f"destination changed outside this installation: {self.destination}")
        if self.backup is None:
            descriptor, raw_recovery = tempfile.mkstemp(
                prefix=f".{self.destination.name}.rollback.fieldkit.", dir=self.destination.parent
            )
            recovery = Path(raw_recovery)
            try:
                close_error: BaseException | None = None
                try:
                    os.close(descriptor)
                except BaseException as exc:  # noqa: BLE001 - rollback must continue after close failure
                    close_error = exc
                placeholder_identity = _path_identity(recovery)
                assert placeholder_identity is not None
                _exchange_paths(recovery, self.destination)
                try:
                    displaced_identity = _path_identity(recovery)
                except BaseException as inspection_error:  # noqa: BLE001 - reverse before propagating interruption
                    _reverse_after_inspection_failure(
                        recovery,
                        self.destination,
                        preserved,
                        inspection_error,
                        placeholder_identity,
                    )
                if displaced_identity != published_identity:
                    _restore_displaced_update(recovery, self.destination, placeholder_identity, preserved)
                    raise InstallError(f"destination changed outside this installation: {self.destination}")
                _remove_matching_destination(self.destination, placeholder_identity, preserved)
                if close_error is not None:
                    raise close_error.with_traceback(close_error.__traceback__)
            finally:
                if recovery not in preserved:
                    recovery.unlink(missing_ok=True)
        else:
            backup_identity = _path_identity(self.backup)
            if backup_identity is None:
                raise InstallError(f"rollback backup disappeared: {self.destination}")
            _exchange_paths(self.backup, self.destination)
            try:
                displaced_identity = _path_identity(self.backup)
            except BaseException as inspection_error:  # noqa: BLE001 - reverse before propagating interruption
                _reverse_after_inspection_failure(self.backup, self.destination, preserved, inspection_error)
            if displaced_identity != published_identity:
                _restore_displaced_update(self.backup, self.destination, backup_identity, preserved)
                raise InstallError(f"destination changed outside this installation: {self.destination}")

    def discard(self) -> None:
        """Remove an unused backup."""
        if self.backup is not None:
            self.backup.unlink(missing_ok=True)


def _path_identity(path: Path) -> PathIdentity | None:
    """Return filesystem identity, mutable metadata, and content without following symlinks."""
    if not os.path.lexists(path):
        return None
    item = path.lstat()
    if stat.S_ISDIR(item.st_mode):
        return item.st_dev, item.st_ino, 0, 0, stat.S_IFDIR, 0, b""
    metadata = (
        item.st_dev,
        item.st_ino,
        item.st_size,
        item.st_mtime_ns,
        stat.S_IFMT(item.st_mode),
        stat.S_IMODE(item.st_mode),
    )
    if stat.S_ISLNK(item.st_mode):
        digest = hashlib.sha256(os.fsencode(path.readlink())).digest()
        after = path.lstat()
    elif stat.S_ISREG(item.st_mode):
        if item.st_size > MAX_CUSTOM_HOOK_BYTES:
            raise InstallError(f"existing hook exceeds {MAX_CUSTOM_HOOK_BYTES} bytes: {path.name}")
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as content:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or (before.st_dev, before.st_ino) != (item.st_dev, item.st_ino)
                or before.st_size > MAX_CUSTOM_HOOK_BYTES
            ):
                raise InstallError(f"path changed while being inspected: {path}")
            payload = content.read(MAX_CUSTOM_HOOK_BYTES + 1)
            if len(payload) > MAX_CUSTOM_HOOK_BYTES:
                raise InstallError(f"existing hook exceeds {MAX_CUSTOM_HOOK_BYTES} bytes: {path.name}")
            digest = hashlib.sha256(payload).digest()
            after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (item.st_dev, item.st_ino):
            raise InstallError(f"path changed while being inspected: {path}")
    else:
        digest = b""
        after = path.lstat()
    after_metadata = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        stat.S_IFMT(after.st_mode),
        stat.S_IMODE(after.st_mode),
    )
    if after_metadata != metadata:
        raise InstallError(f"path changed while being inspected: {path}")
    return *metadata, digest


def _content_signature(identity: PathIdentity) -> tuple[int, int, int, bytes]:
    """Return copied content state independent of inode and timestamp."""
    return identity[2], identity[4], identity[5], identity[6]


def _exchange_paths(first: Path, second: Path) -> None:
    """Atomically exchange two paths on supported Linux and macOS hosts."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        try:
            rename = libc.renamex_np
        except AttributeError as exc:
            raise InstallError(f"atomic hook exchange is unavailable on {sys.platform}") from exc
        result = rename(os.fsencode(first), os.fsencode(second), 0x00000002)
    elif sys.platform.startswith("linux"):
        try:
            rename = libc.renameat2
        except AttributeError as exc:
            raise InstallError(f"atomic hook exchange is unavailable on {sys.platform}") from exc
        result = rename(-100, os.fsencode(first), -100, os.fsencode(second), 0x00000002)
    else:
        raise InstallError(f"atomic hook exchange is unsupported on {sys.platform}")
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), f"{first} <-> {second}")


def _move_no_replace(source: Path, destination: Path) -> None:
    """Atomically move a path without replacing an existing destination."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        try:
            rename = libc.renamex_np
        except AttributeError as exc:
            raise InstallError(f"atomic hook move is unavailable on {sys.platform}") from exc
        result = rename(os.fsencode(source), os.fsencode(destination), 0x00000004)
    elif sys.platform.startswith("linux"):
        try:
            rename = libc.renameat2
        except AttributeError as exc:
            raise InstallError(f"atomic hook move is unavailable on {sys.platform}") from exc
        result = rename(-100, os.fsencode(source), -100, os.fsencode(destination), 0x00000001)
    else:
        raise InstallError(f"atomic hook move is unsupported on {sys.platform}")
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), f"{source} -> {destination}")


def _remove_matching_destination(destination: Path, expected: PathIdentity, preserved: set[Path]) -> None:
    """Remove only the expected object, restoring or preserving anything else displaced."""
    descriptor, raw_recovery = tempfile.mkstemp(prefix=f".{destination.name}.remove.fieldkit.", dir=destination.parent)
    os.close(descriptor)
    recovery = Path(raw_recovery)
    recovery.unlink()
    _move_no_replace(destination, recovery)
    try:
        displaced_identity = _path_identity(recovery)
    except BaseException:
        preserved.add(recovery)
        with suppress(FileExistsError):
            os.link(recovery, destination, follow_symlinks=False)
        raise
    if displaced_identity == expected:
        recovery.unlink()
        return
    try:
        os.link(recovery, destination, follow_symlinks=False)
    except FileExistsError:
        preserved.add(recovery)
        raise InstallError(
            f"destination changed during rollback cleanup: {destination}; recovery preserved at: {Path.cwd() / recovery}"
        ) from None
    recovery.unlink()
    raise InstallError(f"destination changed during rollback cleanup: {destination}")


def _recover_failed_exchange(
    staged: Path,
    destination: Path,
    snapshot: Snapshot,
    staged_identity: PathIdentity,
    preserved: set[Path],
    exchange_error: BaseException,
    trigger_error: BaseException | None = None,
) -> None:
    """Restore from the snapshot when reversing an exchange fails."""
    preserved.add(staged)
    try:
        snapshot.restore(staged_identity, preserved)
    except BaseException as restore_error:
        if snapshot.backup is not None:
            preserved.add(snapshot.backup)
        failure = InstallError(
            f"cannot restore destination changed during publication: {destination}; "
            f"displaced hook preserved at: {Path.cwd() / staged}"
        )
        if trigger_error is not None:
            failure.add_note(f"publication inspection failed: {_failure_description(trigger_error)}")
        failure.add_note(f"atomic exchange rollback failed: {_failure_description(exchange_error)}")
        raise failure from restore_error
    failure = InstallError(
        f"destination changed during publication: {destination}; displaced hook preserved at: {Path.cwd() / staged}"
    )
    if trigger_error is not None:
        failure.add_note(f"publication inspection failed: {_failure_description(trigger_error)}")
    failure.add_note(f"atomic exchange rollback failed: {_failure_description(exchange_error)}")
    raise failure from exchange_error


def _restore_displaced_update(
    staged: Path,
    destination: Path,
    expected_destination: PathIdentity,
    preserved: set[Path],
) -> None:
    """Keep following concurrent replacements until the displaced update is live."""
    for _attempt in range(MAX_DISPLACED_UPDATE_EXCHANGES):
        replacement_identity = _path_identity(staged)
        if replacement_identity is None:
            raise InstallError(f"displaced hook disappeared during publication: {destination}")
        try:
            _exchange_paths(staged, destination)
        except BaseException as exc:
            preserved.add(staged)
            raise InstallError(
                f"cannot restore displaced hook at {destination}; recovery preserved at: {Path.cwd() / staged}"
            ) from exc
        displaced_identity = _path_identity(staged)
        if displaced_identity == expected_destination:
            return
        expected_destination = replacement_identity
    preserved.add(staged)
    raise InstallError(
        f"destination kept changing during publication: {destination}; "
        f"newest displaced hook preserved at: {Path.cwd() / staged}"
    )


def _publish_hook(
    staged: Path,
    destination: Path,
    snapshot: Snapshot,
    preserved: set[Path],
) -> PathIdentity:
    """Publish without overwriting a destination that changed after snapshot."""
    staged_identity = _path_identity(staged)
    if staged_identity is None:
        raise InstallError(f"staged hook disappeared before publication: {destination.name}")
    if snapshot.original_identity is None:
        try:
            os.link(staged, destination, follow_symlinks=False)
        except FileExistsError as exc:
            raise InstallError(f"destination changed before publication: {destination}") from exc
        return staged_identity

    _exchange_paths(staged, destination)
    try:
        displaced_identity = _path_identity(staged)
    except BaseException as inspection_error:
        try:
            _exchange_paths(staged, destination)
        except BaseException as exchange_error:  # noqa: BLE001 - recovery must survive interruption
            _recover_failed_exchange(
                staged,
                destination,
                snapshot,
                staged_identity,
                preserved,
                exchange_error,
                inspection_error,
            )
        raise
    if displaced_identity == snapshot.original_identity:
        return staged_identity
    _restore_displaced_update(staged, destination, staged_identity, preserved)
    raise InstallError(f"destination changed during publication: {destination}")


@contextmanager
def _anchored_directory(path: Path, termination_signals: TerminationSignals) -> Iterator[tuple[Path, PathIdentity]]:
    """Keep filesystem operations attached to the validated directory inode."""
    previous_directory = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        os.close(previous_directory)
        raise InstallError(f"cannot open shared hooks directory: {path}: {exc}") from exc
    switched = False
    try:
        item = os.fstat(descriptor)
        if item.st_uid != os.geteuid():
            raise InstallError(f"shared hooks directory is not owned by the current user: {path}")
        if stat.S_IMODE(item.st_mode) & 0o022:
            raise InstallError(f"shared hooks directory is group/world writable: {path}")
        with termination_signals.defer():
            os.fchdir(descriptor)
            switched = True
        yield Path(), (item.st_dev, item.st_ino, 0, 0, stat.S_IFDIR, 0, b"")
    finally:
        try:
            if switched:
                with termination_signals.defer():
                    os.fchdir(previous_directory)
                    switched = False
        finally:
            os.close(descriptor)
            os.close(previous_directory)


@contextmanager
def _installation_lock(
    path: Path, *, timeout_seconds: float, termination_signals: TerminationSignals | None = None
) -> Iterator[None]:
    """Acquire the shared hook lock within a bounded interval."""
    deadline = time.monotonic() + timeout_seconds
    with path.open("a+b") as lock_file:
        while True:
            if termination_signals is not None:
                termination_signals.checkpoint()
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise InstallError("timed out waiting for another hook installation") from None
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _unlink_unless_preserved(path: Path, preserved: set[Path], errors: list[BaseException]) -> None:
    """Remove an allocated path unless rollback retained it for recovery."""
    try:
        if path not in preserved:
            path.unlink(missing_ok=True)
    except BaseException as exc:  # noqa: BLE001 - cleanup records interruptions instead of abandoning artifacts
        errors.append(exc)


def _temporary_path(
    hooks_dir: Path,
    prefix: str,
    cleanup: ExitStack,
    preserved: set[Path],
    cleanup_errors: list[BaseException],
    termination_signals: TerminationSignals,
) -> Path:
    """Allocate a private path and register cleanup before returning it."""
    with termination_signals.defer():
        descriptor, raw_path = tempfile.mkstemp(prefix=f".{prefix}.fieldkit.", dir=hooks_dir)
        path = Path(raw_path)
        cleanup_registered = False
        try:
            cleanup.callback(_unlink_unless_preserved, path, preserved, cleanup_errors)
            cleanup_registered = True
            return path
        finally:
            try:
                os.close(descriptor)
            except BaseException:
                if not cleanup_registered:
                    path.unlink(missing_ok=True)
                with suppress(OSError):
                    os.close(descriptor)
                raise
            else:
                if not cleanup_registered:
                    path.unlink(missing_ok=True)


def _remove_temporary_directory(path: Path, errors: list[BaseException]) -> None:
    """Remove a private staging directory while preserving cleanup diagnostics."""
    try:
        shutil.rmtree(path)
    except BaseException as exc:  # noqa: BLE001 - cleanup errors must not hide installation failure
        errors.append(exc)


def _temporary_directory(
    hooks_dir: Path,
    cleanup: ExitStack,
    cleanup_errors: list[BaseException],
    termination_signals: TerminationSignals,
) -> Path:
    """Allocate a private directory in the shared hook filesystem."""
    with termination_signals.defer():
        path = Path(tempfile.mkdtemp(prefix=".install-git.fieldkit.", dir=hooks_dir))
        cleanup_registered = False
        try:
            cleanup.callback(_remove_temporary_directory, path, cleanup_errors)
            cleanup_registered = True
            return path
        finally:
            if not cleanup_registered:
                shutil.rmtree(path, ignore_errors=True)


def _snapshot(
    destination: Path,
    hooks_dir: Path,
    cleanup: ExitStack,
    preserved: set[Path],
    cleanup_errors: list[BaseException],
    termination_signals: TerminationSignals,
) -> Snapshot:
    """Capture a regular file or symlink for later atomic restoration."""
    original_identity = _path_identity(destination)
    if original_identity is None:
        return Snapshot(destination, None, None)
    backup = _temporary_path(
        hooks_dir,
        f"{destination.name}.backup",
        cleanup,
        preserved,
        cleanup_errors,
        termination_signals,
    )
    try:
        backup.unlink()
        if destination.is_symlink():
            backup.symlink_to(destination.readlink())
        else:
            descriptor = os.open(destination, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as source_file, backup.open("wb") as backup_file:
                before = os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode) or (before.st_dev, before.st_ino) != original_identity[:2]:
                    raise InstallError(f"destination changed while being captured: {destination}")
                copied = 0
                while copied <= before.st_size:
                    chunk = source_file.read(min(64 * 1024, before.st_size + 1 - copied))
                    if not chunk:
                        break
                    backup_file.write(chunk)
                    copied += len(chunk)
                after = os.fstat(descriptor)
            if _source_identity(before) != _source_identity(after) or copied != before.st_size:
                raise InstallError(f"destination changed while being captured: {destination}")
            backup.chmod(stat.S_IMODE(before.st_mode))
            _sync_file(backup)
        _sync_directory(hooks_dir)
    except (InstallError, OSError):
        backup.unlink(missing_ok=True)
        raise
    backup_identity = _path_identity(backup)
    if backup_identity is None or _content_signature(backup_identity) != _content_signature(original_identity):
        raise InstallError(f"destination changed while being captured: {destination}")
    if _path_identity(destination) != original_identity:
        raise InstallError(f"destination changed while being captured: {destination}")
    return Snapshot(destination, backup, original_identity)


def _source_identity(item: os.stat_result) -> tuple[int, int, int, int, int]:
    return item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns


def _stage(
    source: Path,
    hooks_dir: Path,
    name: str,
    cleanup: ExitStack,
    preserved: set[Path],
    cleanup_errors: list[BaseException],
    termination_signals: TerminationSignals,
) -> Path:
    """Copy one custom hook into the destination filesystem with mode 0755."""
    try:
        source_descriptor = os.open(source, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError as exc:
        raise InstallError(f"cannot read custom hook source {source.name}: {exc}") from exc
    with os.fdopen(source_descriptor, "rb") as source_file:
        before = os.fstat(source_descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size == 0:
            raise InstallError(f"custom hook source must be a non-empty regular file: {source.name}")
        if before.st_size > MAX_CUSTOM_HOOK_BYTES:
            raise InstallError(f"custom hook source exceeds {MAX_CUSTOM_HOOK_BYTES} bytes: {source.name}")
        content = source_file.read(MAX_CUSTOM_HOOK_BYTES + 1)
        after = os.fstat(source_descriptor)
        if _source_identity(before) != _source_identity(after) or len(content) != before.st_size:
            raise InstallError(f"custom hook source changed while being copied: {source.name}")
        try:
            path_after = source.lstat()
        except OSError as exc:
            raise InstallError(f"custom hook source changed while being copied: {source.name}") from exc
        if _source_identity(after) != _source_identity(path_after) or not stat.S_ISREG(path_after.st_mode):
            raise InstallError(f"custom hook source changed while being copied: {source.name}")
        temporary = _temporary_path(hooks_dir, name, cleanup, preserved, cleanup_errors, termination_signals)
        try:
            with temporary.open("wb") as temporary_file:
                temporary_file.write(content)
            temporary.chmod(0o755)
            _sync_file(temporary)
            _sync_directory(hooks_dir)
        except (InstallError, OSError):
            temporary.unlink(missing_ok=True)
            raise
        return temporary


def _validate_installed_hooks(destinations: dict[str, Path], published: dict[str, PathIdentity]) -> None:
    """Require a complete executable regular hook set before reporting success."""
    for name, destination in destinations.items():
        if _path_identity(destination) != published.get(name):
            raise InstallError(f"installed hook changed after publication: {name}")
        if destination.is_symlink() or not destination.is_file():
            raise InstallError(f"installed hook is not a regular file: {name}")
        destination_stat = destination.stat()
        if destination_stat.st_size == 0 or stat.S_IMODE(destination_stat.st_mode) != 0o755:
            raise InstallError(f"installed hook has invalid content or mode: {name}")


def _sync_file(path: Path) -> None:
    """Persist a regular file's content and metadata before publication."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_directory(path: Path) -> None:
    """Persist directory-entry changes surrounding an atomic mutation."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_hooks_directory(hooks_dir: Path) -> None:
    """Require an owned real directory after any creation attempt."""
    try:
        hooks_dir_stat = hooks_dir.lstat()
    except FileNotFoundError as exc:
        raise InstallError(f"shared hooks directory disappeared: {hooks_dir}") from exc
    if stat.S_ISLNK(hooks_dir_stat.st_mode) or not stat.S_ISDIR(hooks_dir_stat.st_mode):
        raise InstallError(f"shared hooks path is not a directory: {hooks_dir}")
    if hooks_dir_stat.st_uid != os.geteuid():
        raise InstallError(f"shared hooks directory is not owned by the current user: {hooks_dir}")
    if stat.S_IMODE(hooks_dir_stat.st_mode) & 0o022:
        raise InstallError(f"shared hooks directory is group/world writable: {hooks_dir}")


def install(
    *,
    repo_root: Path,
    lock_timeout: float,
    command_timeout: float,
    kill_after: float,
    termination_signals: TerminationSignals | None = None,
) -> None:
    """Install all hooks atomically enough to restore the complete prior set on failure."""
    if not math.isfinite(lock_timeout) or not 0 <= lock_timeout <= MAXIMUM_TIMEOUT_SECONDS:
        raise InstallError(f"lock timeout must be between zero and {MAXIMUM_TIMEOUT_SECONDS} seconds")
    if not math.isfinite(command_timeout) or not 0 < command_timeout <= MAXIMUM_TIMEOUT_SECONDS:
        raise InstallError(f"command timeout must be greater than zero and at most {MAXIMUM_TIMEOUT_SECONDS} seconds")
    if not math.isfinite(kill_after) or not 0 < kill_after <= MAXIMUM_TIMEOUT_SECONDS:
        raise InstallError(
            f"kill-after timeout must be greater than zero and at most {MAXIMUM_TIMEOUT_SECONDS} seconds"
        )
    if not HOOKS:
        raise InstallError("hook manifest is empty")
    for hook in HOOKS:
        if (hook.source is None) == (hook.pre_commit_type is None):
            raise InstallError(f"hook manifest entry must define exactly one source type: {hook.name}")
    signal_state = termination_signals or TerminationSignals()
    hooks_dir = _hooks_directory(
        repo_root,
        timeout_seconds=command_timeout,
        kill_after_seconds=kill_after,
        termination_signals=signal_state,
    )
    hooks_directory_existed = os.path.lexists(hooks_dir)
    if hooks_directory_existed:
        _validate_hooks_directory(hooks_dir)
    hooks_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not hooks_directory_existed:
        _sync_directory(hooks_dir.parent)
    _validate_hooks_directory(hooks_dir)
    with (
        _anchored_directory(hooks_dir, signal_state) as (anchored_hooks_dir, hooks_dir_identity),
        _installation_lock(
            anchored_hooks_dir / ".fieldkit-install.lock",
            timeout_seconds=lock_timeout,
            termination_signals=signal_state,
        ),
    ):
        preserved_temporaries: set[Path] = set()
        cleanup_errors: list[BaseException] = []
        cleanup = ExitStack()
        destinations = {name: anchored_hooks_dir / name for name in HOOK_NAMES}
        for destination in destinations.values():
            if os.path.lexists(destination):
                destination_stat = destination.lstat()
                if destination_stat.st_uid != os.geteuid():
                    raise InstallError(f"destination is not owned by the current user: {destination}")
                if not destination.is_symlink() and not stat.S_ISREG(destination_stat.st_mode):
                    raise InstallError(f"destination is not a regular file or symlink: {destination}")

        staged: dict[str, Path] = {}
        snapshots: dict[str, Snapshot] = {}
        published: dict[str, PathIdentity] = {}
        mutated = False
        failure: BaseException | None = None
        try:
            for hook in HOOKS:
                if hook.source is None:
                    continue
                staged[hook.name] = _stage(
                    repo_root / hook.source,
                    anchored_hooks_dir,
                    hook.name,
                    cleanup,
                    preserved_temporaries,
                    cleanup_errors,
                    signal_state,
                )
            _run(
                ["uv", "run", "python", "scripts/check_precommit_version.py"],
                cwd=repo_root,
                timeout_seconds=command_timeout,
                kill_after_seconds=kill_after,
                termination_signals=signal_state,
            )
            staging_git_dir = _temporary_directory(anchored_hooks_dir, cleanup, cleanup_errors, signal_state)
            staging_git_dir_for_tools = staging_git_dir.resolve(strict=True)
            _run(
                ["git", "init", "--bare", "--quiet", str(staging_git_dir_for_tools)],
                cwd=repo_root,
                timeout_seconds=command_timeout,
                kill_after_seconds=kill_after,
                termination_signals=signal_state,
            )
            staging_environment = {"GIT_DIR": str(staging_git_dir_for_tools)}
            for hook in HOOKS:
                if hook.source is not None or hook.pre_commit_type is None:
                    continue
                _run(
                    ["uvx", "pre-commit", "install", "-f", "--hook-type", hook.pre_commit_type],
                    cwd=repo_root,
                    timeout_seconds=command_timeout,
                    kill_after_seconds=kill_after,
                    environment=staging_environment,
                    termination_signals=signal_state,
                )
            for hook in HOOKS:
                if hook.source is not None:
                    continue
                staged[hook.name] = _stage(
                    staging_git_dir / "hooks" / hook.name,
                    anchored_hooks_dir,
                    hook.name,
                    cleanup,
                    preserved_temporaries,
                    cleanup_errors,
                    signal_state,
                )
            for name in HOOK_NAMES:
                snapshots[name] = _snapshot(
                    destinations[name],
                    anchored_hooks_dir,
                    cleanup,
                    preserved_temporaries,
                    cleanup_errors,
                    signal_state,
                )
            with signal_state.defer():
                mutated = True
                for name in HOOK_NAMES:
                    published[name] = _publish_hook(
                        staged[name], destinations[name], snapshots[name], preserved_temporaries
                    )
                    _sync_directory(anchored_hooks_dir)
                _validate_installed_hooks(destinations, published)
                if _path_identity(hooks_dir) != hooks_dir_identity:
                    raise InstallError(f"shared hooks directory changed during installation: {hooks_dir}")
            mutated = False
        except BaseException as exc:  # noqa: BLE001 - rollback must run after interruption
            if failure is not None and exc is not failure:
                exc.add_note(f"prior installation failure: {_failure_description(failure)}")
            failure = exc
            if mutated:
                try:
                    with signal_state.defer():
                        for name, published_identity in published.items():
                            snapshot = snapshots[name]
                            try:
                                snapshot.restore(published_identity, preserved_temporaries)
                                _sync_directory(anchored_hooks_dir)
                            except BaseException as rollback_error:  # noqa: BLE001 - restore every eligible hook
                                if snapshot.backup is not None:
                                    preserved_temporaries.add(snapshot.backup)
                                cleanup_errors.append(rollback_error)
                        mutated = False
                except InstallInterrupted as interruption:
                    failure.add_note(f"termination requested by {interruption.received_signal.name}")
        with signal_state.defer():
            for snapshot in snapshots.values():
                if snapshot.backup in preserved_temporaries:
                    continue
                try:
                    snapshot.discard()
                except BaseException as exc:  # noqa: BLE001 - continue cleaning remaining backups
                    cleanup_errors.append(exc)
            for temporary in staged.values():
                if temporary in preserved_temporaries:
                    continue
                try:
                    temporary.unlink(missing_ok=True)
                except BaseException as exc:  # noqa: BLE001 - continue cleaning remaining staged files
                    cleanup_errors.append(exc)
            cleanup.close()

            if cleanup_errors:
                failure_note = f"installation failure: {_failure_description(failure)}; " if failure is not None else ""
                cleanup_note = "; ".join(_failure_description(exc) for exc in cleanup_errors)
                recovery_paths = ", ".join(
                    str(path if path.is_absolute() else Path.cwd() / path) for path in sorted(preserved_temporaries)
                )
                recovery_note = f"; backups preserved at: {recovery_paths}" if recovery_paths else ""
                raise InstallError(f"{failure_note}cleanup failures: {cleanup_note}{recovery_note}") from failure
            if failure is not None:
                raise failure.with_traceback(failure.__traceback__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--lock-timeout", type=float, default=os.environ.get("HOOK_INSTALL_LOCK_TIMEOUT_SECONDS", "30"))
    parser.add_argument(
        "--command-timeout", type=float, default=os.environ.get("HOOK_INSTALL_COMMAND_TIMEOUT_SECONDS", "120")
    )
    parser.add_argument("--kill-after", type=float, default=os.environ.get("HOOK_INSTALL_KILL_AFTER_SECONDS", "5"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Install hooks and report a concise failure."""
    args = _parser().parse_args(argv)
    try:
        with _termination_signals_as_interrupts() as termination_signals:
            install(
                repo_root=args.repo_root.resolve(),
                lock_timeout=args.lock_timeout,
                command_timeout=args.command_timeout,
                kill_after=args.kill_after,
                termination_signals=termination_signals,
            )
    except InstallInterrupted as exc:
        print(f"hooks: interrupted by {exc.received_signal.name}", file=sys.stderr)
        return 1
    except (InstallError, OSError) as exc:
        notes = "; ".join(getattr(exc, "__notes__", ()))
        note_suffix = f"; {notes}" if notes else ""
        print(f"hooks: {exc}{note_suffix}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
