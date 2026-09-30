"""Preparatory Gmail rehearsal for a caller-verified, trusted candidate executable.

This isolates configuration and disposable output, not executable authority.
The caller asserts the credential belongs to the intended test account; this
recorder neither verifies mailbox identity nor establishes artifact identity.
Nothing returned here approves a release. Temporary credentials and caches are
removed on successful teardown; teardown errors remain nonpassing. Raw child
streams never leave memory.

Live credential use requires a separately qualified isolation and teardown
backend. The process-group runner cannot contain detached descendants, so this
preparatory module and its synthetic tests do not qualify that safety boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from fieldkit.util.bounded_process import BoundedProcessError, run_bounded_process_bytes
from scripts import release_filesystem
from scripts.gmail_rehearsal_observation import observe_gmail_cache

SYNC_TIMEOUT_SECONDS = 120.0
CLEANUP_TIMEOUT_SECONDS = 5.0
STREAM_LIMIT_BYTES = 64 * 1024
TOKEN_LIMIT_BYTES = 64 * 1024
INVENTORY_FILE_LIMIT = 64
INVENTORY_BYTE_LIMIT = 64 * 1024 * 1024
Preservation = Literal["unchanged", "changed", "unverifiable"]
Failure = Literal[
    "setup", "teardown", "process", "timeout", "overflow", "cleanup", "nonzero", "json", "partial", "observation"
]


@dataclass(frozen=True)
class SyncCounters:
    """The only allowlisted data retained from child JSON."""

    added: int
    failed: int
    not_found: int
    unresolved: int
    partial: bool


@dataclass(frozen=True)
class CacheObservation:
    """Non-content cache observations only."""

    message_count: int
    since_checkpoint_present: bool
    since_count: int | None
    history_checkpoint_present: bool
    query_ready: bool


@dataclass(frozen=True)
class GmailRehearsal:
    """Pending preparation, including explicit preservation uncertainty."""

    run_complete: bool
    failure: Failure | None
    credential_preservation: Preservation
    cache_preservation: Preservation
    summary: SyncCounters | None
    observation: CacheObservation | None
    status: Literal["pending"] = "pending"
    release_approved: Literal[False] = False


def _read_file(path: Path, *, limit: int, private: bool = False) -> tuple[bytes, tuple[int, ...]]:
    parent = release_filesystem.open_real_directory(path.parent)
    try:
        descriptor = os.open(path.name, release_filesystem.file_flags(), dir_fd=parent)
        try:
            before = os.fstat(descriptor)
            if private and (before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) & 0o077):
                raise ValueError("credential must be owner-only and owned by the caller")
            data = release_filesystem.read_regular_file(descriptor, maximum_bytes=limit)
            after = os.fstat(descriptor)
            identity = (before.st_dev, before.st_ino, before.st_mode, before.st_uid, before.st_size, before.st_mtime_ns)
            if identity != (after.st_dev, after.st_ino, after.st_mode, after.st_uid, after.st_size, after.st_mtime_ns):
                raise ValueError("input changed during observation")
            return data, identity
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


def _credential_snapshot(path: Path) -> tuple[bytes, tuple[int, ...]]:
    try:
        data, identity = _read_file(path, limit=TOKEN_LIMIT_BYTES, private=True)
        if not data:
            raise ValueError("empty credential")
        return data, identity
    except (OSError, ValueError):
        raise ValueError("credential is unavailable, unsafe, or exceeds its bound") from None


def _cache_inventory(root: Path | None) -> dict[str, tuple[tuple[int, ...], bytes]] | None:
    if root is None:
        return None
    inventory: dict[str, tuple[tuple[int, ...], bytes]] = {}
    total = 0

    def visit(descriptor: int, prefix: str) -> None:
        nonlocal total
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if len(inventory) >= INVENTORY_FILE_LIMIT:
                    raise ValueError("inventory exceeds entry limit")
                name = f"{prefix}{entry.name}"
                metadata = entry.stat(follow_symlinks=False)
                identity = (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_size, metadata.st_mtime_ns)
                if stat.S_ISDIR(metadata.st_mode):
                    inventory[name] = (identity, b"")
                    child = release_filesystem.open_directory_at(descriptor, entry.name)
                    try:
                        visit(child, f"{name}/")
                        after = os.fstat(child)
                        if identity != (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns):
                            raise ValueError("inventory changed during read")
                    finally:
                        os.close(child)
                else:
                    file_fd = os.open(entry.name, release_filesystem.file_flags(), dir_fd=descriptor)
                    try:
                        data = release_filesystem.read_regular_file(file_fd, maximum_bytes=INVENTORY_BYTE_LIMIT - total)
                        after = os.fstat(file_fd)
                        if identity != (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns):
                            raise ValueError("inventory changed during read")
                        total += len(data)
                        inventory[name] = (identity, hashlib.sha256(data).digest())
                    finally:
                        os.close(file_fd)

    try:
        descriptor = release_filesystem.open_real_directory(root)
        try:
            before = os.fstat(descriptor)
            inventory[""] = ((before.st_dev, before.st_ino, before.st_mode, before.st_size, before.st_mtime_ns), b"")
            visit(descriptor, "")
            after = os.fstat(descriptor)
            if inventory[""][0] != (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns):
                raise ValueError("inventory changed during read")
        finally:
            os.close(descriptor)
    except (OSError, ValueError):
        return None
    return inventory


def _preservation(before: object, after: object) -> Preservation:
    if before is None or after is None:
        return "unverifiable"
    return "unchanged" if before == after else "changed"


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _summary(raw: bytes, maximum: int) -> SyncCounters:
    payload = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(payload, dict) or payload.get("mode") != "since":
        raise ValueError("unexpected summary")
    counts: list[int] = []
    for key in ("added", "failed", "not_found", "unresolved"):
        value = payload.get(key)
        if type(value) is not int or not 0 <= value <= maximum:
            raise ValueError("invalid or unbounded summary count")
        counts.append(value)
    if type(payload.get("partial")) is not bool:
        raise ValueError("invalid summary")
    if payload["failed"] != payload["not_found"] + payload["unresolved"] or payload["partial"] != (
        payload["failed"] > 0
    ):
        raise ValueError("inconsistent summary")
    return SyncCounters(
        payload["added"], payload["failed"], payload["not_found"], payload["unresolved"], payload["partial"]
    )


def _observe(db: Path) -> CacheObservation:
    observed = observe_gmail_cache(db)
    counts = (observed.message_count,)
    flags = (observed.since_checkpoint_present, observed.history_checkpoint_present, observed.query_ready)
    if any(type(value) is not int or value < 0 for value in counts) or any(type(value) is not bool for value in flags):
        raise ValueError("invalid observation")
    if observed.since_count is not None and (type(observed.since_count) is not int or observed.since_count < 0):
        raise ValueError("invalid observation")
    if observed.since_checkpoint_present != (observed.since_count is not None):
        raise ValueError("inconsistent checkpoint observation")
    return CacheObservation(counts[0], flags[0], observed.since_count, flags[1], flags[2])


def run_gmail_rehearsal(
    *,
    executable: Path,
    credential_path: Path,
    test_account: str,
    since: str,
    max_messages: int,
    original_cache: Path | None = None,
) -> GmailRehearsal:
    """Run one bounded since-sync; original_cache is the whole original cache directory.

    Supply an absolute executable independently verified by the caller. No
    ambient authentication, proxy, Python path, or dotenv settings are forwarded.
    Missing/bounded-out preservation evidence remains explicitly unverifiable.
    Live credentials require separately qualified isolation and teardown; this
    process-group implementation alone cannot contain detached descendants.
    """
    if type(max_messages) is not int or max_messages <= 0:
        raise ValueError("max_messages must be a positive integer")
    try:
        parsed = date.fromisoformat(since)
    except (TypeError, ValueError):
        raise ValueError("since must be a YYYY-MM-DD date") from None
    if parsed.isoformat() != since:
        raise ValueError("since must be a YYYY-MM-DD date")
    if not isinstance(test_account, str) or not test_account.strip():
        raise ValueError("caller must assert the intended test account")
    if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("candidate executable must be an absolute executable file")
    source = _credential_snapshot(credential_path)
    cache_before = _cache_inventory(original_cache)
    summary = None
    observation = None
    failure: Failure | None = None
    filesystem_phase: Failure = "setup"
    try:
        with TemporaryDirectory(prefix="fieldkit-gmail-rehearsal-") as temporary:
            root = Path(temporary)
            dirs = {
                name: root / name
                for name in ("home", "config", "workspace", "data", "cache", "tmp", "state", "runtime")
            }
            for directory in dirs.values():
                directory.mkdir(mode=0o700)
            config_dir = dirs["config"] / "fieldkit"
            config_dir.mkdir(mode=0o700)
            token = dirs["data"] / "token.json"
            with token.open("xb") as stream:
                token.chmod(0o600)
                stream.write(source[0])
            config = config_dir / "config.yaml"
            config.write_text(
                json.dumps(
                    {
                        "fieldkit_home": str(dirs["workspace"]),
                        "fieldkit_data": str(dirs["data"]),
                        "gmail_token": str(token),
                    }
                ),
                encoding="utf-8",
            )
            config.chmod(0o600)
            env = {
                "HOME": str(dirs["home"]),
                "XDG_CONFIG_HOME": str(dirs["config"]),
                "XDG_CACHE_HOME": str(dirs["cache"]),
                "XDG_DATA_HOME": str(dirs["data"]),
                "XDG_STATE_HOME": str(dirs["state"]),
                "XDG_RUNTIME_DIR": str(dirs["runtime"]),
                "TMPDIR": str(dirs["tmp"]),
                "TMP": str(dirs["tmp"]),
                "TEMP": str(dirs["tmp"]),
                "FIELDKIT_DATA_DIR": str(dirs["data"]),
                "PYTHON_DOTENV_DISABLED": "1",
                "PYTHONNOUSERSITE": "1",
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "TZ": "UTC",
            }
            db = dirs["data"] / "gmail.db"
            filesystem_phase = "process"
            try:
                result = run_bounded_process_bytes(
                    [
                        str(executable),
                        "gmail",
                        "sync",
                        "--db",
                        str(db),
                        "--since",
                        since,
                        "--max-messages",
                        str(max_messages),
                        "--json",
                    ],
                    timeout=SYNC_TIMEOUT_SECONDS,
                    cleanup_timeout=CLEANUP_TIMEOUT_SECONDS,
                    stdout_limit=STREAM_LIMIT_BYTES,
                    stderr_limit=STREAM_LIMIT_BYTES,
                    cwd=dirs["workspace"],
                    env=env,
                )
                if result.returncode != 0:
                    failure = "nonzero"
                else:
                    try:
                        summary = _summary(result.stdout, max_messages)
                    except (ValueError, UnicodeError, RecursionError):
                        failure = "json"
                    if summary is not None:
                        if summary.partial or summary.failed or summary.unresolved:
                            failure = "partial"
                        else:
                            try:
                                observation = _observe(db)
                            except (OSError, ValueError):
                                failure = "observation"
                            if observation is not None and (
                                not observation.query_ready
                                or not 0 < observation.message_count <= max_messages
                                or observation.message_count != summary.added
                                or observation.history_checkpoint_present
                                or (
                                    observation.since_count is not None
                                    and observation.since_count < observation.message_count
                                )
                            ):
                                failure = "observation"
            except BoundedProcessError as error:
                if error.reason == "timeout":
                    failure = "timeout"
                elif error.reason == "overflow":
                    failure = "overflow"
                elif error.reason == "cleanup":
                    failure = "cleanup"
                else:
                    failure = "process"
            filesystem_phase = "teardown"
    except OSError:
        failure = filesystem_phase
    try:
        source_after = _credential_snapshot(credential_path)
    except ValueError:
        source_after = None
    credential_preservation = _preservation(source, source_after)
    cache_preservation = _preservation(cache_before, _cache_inventory(original_cache))
    return GmailRehearsal(
        failure is None and credential_preservation == "unchanged" and cache_preservation == "unchanged",
        failure,
        credential_preservation,
        cache_preservation,
        summary,
        observation,
    )
