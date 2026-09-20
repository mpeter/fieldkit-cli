"""Structured, atomic companion proposal outbox."""

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from fieldkit.companion.decide import ProposedAction
from fieldkit.companion.feed import AttentionItem

_OUTBOX_DIRNAME = "companion-outbox"
_ENRICHMENT_CAP = 4000
_SLUG_SAFE = re.compile(r"[^A-Za-z0-9_-]+")
_ITEM_ID = re.compile(r"[0-9a-f]{16}")
_FILENAME_ITEM_ID = re.compile(r"-([0-9a-f]{16})-")
_COMMAND_GROUP = re.compile(r"[a-z][a-z0-9-]*")
_CURRENT_SUFFIX = ".proposal.json"
_LEGACY_SUFFIX = ".md"
_ENVELOPE_V1_KEYS = frozenset({"version", "item_id", "created_at", "command_argv", "markdown"})
_ENVELOPE_V2_KEYS = _ENVELOPE_V1_KEYS | frozenset({"recommendation", "decision_provenance", "fallback_category"})
_ENVELOPE_KEYS = {1: _ENVELOPE_V1_KEYS, 2: _ENVELOPE_V2_KEYS}
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@dataclass(frozen=True)
class OutboxProposal:
    """One current or legacy proposal exposed to outbox consumers."""

    name: str
    mtime: float
    version: int | None
    item_id: str | None
    created_at: str | None
    command_argv: tuple[str, ...] | None
    markdown: str
    approvable: bool
    validation_error: str | None
    recommendation: str | None = None
    decision_provenance: str | None = None
    fallback_category: str | None = None
    identity: tuple[int, int] | None = None


@dataclass(frozen=True)
class _ValidatedEnvelope:
    version: int
    item_id: str
    created_at: str
    command_argv: tuple[str, ...] | None
    markdown: str
    recommendation: str | None = None
    decision_provenance: str | None = None
    fallback_category: str | None = None


ApprovalStatus = Literal["missing", "not-approvable", "claimed", "conflict"]
RetentionStatus = Literal["missing", "not-retirable", "locked", "conflict"]
ReconciliationStatus = Literal["reconciled", "missing", "not-reconcilable", "conflict"]


@dataclass
class ProposalClaim:
    """Exclusive, at-most-once claim on one proposal generation."""

    status: ApprovalStatus
    proposal: OutboxProposal | None
    _directory_fd: int | None = None
    _lock_fd: int | None = None
    _lock_manager: AbstractContextManager[int] | None = None
    _marker_identity: tuple[int, int] | None = None
    _original_payload: dict[str, object] | None = None
    _finished: bool = False

    def finish(self, *, success: bool) -> bool:
        """Delete a successful marker or restore a failed proposal while locked."""
        if self._finished or self.status != "claimed" or self.proposal is None:
            return False
        self._finished = True
        directory_fd = self._directory_fd
        if directory_fd is None or _name_identity(directory_fd, self.proposal.name) != self._marker_identity:
            return False
        if success:
            try:
                os.unlink(self.proposal.name, dir_fd=directory_fd)
            except OSError:
                return False
            return True
        if self._original_payload is None:
            return False
        _write_atomic(directory_fd, self.proposal.name, self._original_payload)
        return True

    def close(self) -> None:
        """Release the per-name lock and outbox descriptor."""
        errors: list[OSError] = []
        if self._lock_fd is not None:
            if self._lock_manager is not None:
                try:
                    self._lock_manager.__exit__(None, None, None)
                except OSError as error:
                    errors.append(error)
                self._lock_manager = None
                self._lock_fd = None
            else:
                try:
                    _release_proposal_lock(self._lock_fd)
                except OSError as error:
                    errors.append(error)
                self._lock_fd = None
        if self._directory_fd is not None:
            try:
                os.close(self._directory_fd)
            except OSError as error:
                errors.append(error)
            self._directory_fd = None
        if errors:
            raise errors[0]


@dataclass
class ProposalRetention:
    """Locked proposal generation removable without an execution marker."""

    status: RetentionStatus
    proposal: OutboxProposal | None
    _directory_fd: int | None = None
    _lock_fd: int | None = None
    _lock_manager: AbstractContextManager[int] | None = None
    _identity: tuple[int, int] | None = None
    _finished: bool = False

    def finish(self) -> bool:
        """Delete the unchanged proposal generation while its name lock is held."""
        if self._finished or self.status != "locked" or self.proposal is None:
            return False
        self._finished = True
        directory_fd = self._directory_fd
        if directory_fd is None or _name_identity(directory_fd, self.proposal.name) != self._identity:
            return False
        try:
            os.unlink(self.proposal.name, dir_fd=directory_fd)
        except OSError:
            return False
        return True

    def close(self) -> None:
        """Release the per-name lock and outbox descriptor."""
        errors: list[OSError] = []
        if self._lock_fd is not None and self._lock_manager is not None:
            try:
                self._lock_manager.__exit__(None, None, None)
            except OSError as error:
                errors.append(error)
            self._lock_manager = None
            self._lock_fd = None
        if self._directory_fd is not None:
            try:
                os.close(self._directory_fd)
            except OSError as error:
                errors.append(error)
            self._directory_fd = None
        if errors:
            raise errors[0]


def outbox_dir(data_path: Path) -> Path:
    """Return the outbox directory under *data_path* (not created)."""
    return data_path / _OUTBOX_DIRNAME


def _open_directory(path: Path, *, create: bool) -> int | None:
    if create:
        path.mkdir(parents=True, exist_ok=True)
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        if create:
            raise
        return None


def _open_outbox_child(data_fd: int, *, create: bool) -> int | None:
    if create:
        with suppress(FileExistsError):
            os.mkdir(_OUTBOX_DIRNAME, mode=0o700, dir_fd=data_fd)
    try:
        return os.open(_OUTBOX_DIRNAME, _DIRECTORY_FLAGS, dir_fd=data_fd)
    except OSError:
        if create:
            raise
        return None


def _verify_outbox(directory_fd: int, *, create: bool) -> int | None:
    try:
        info = os.fstat(directory_fd)
    except OSError:
        os.close(directory_fd)
        if create:
            raise
        return None
    except BaseException:
        os.close(directory_fd)
        raise
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        os.close(directory_fd)
        if create:
            raise OSError("companion outbox must be an operator-owned directory not writable by group or others")
        return None
    if create and stat.S_IMODE(info.st_mode) != 0o700:
        try:
            os.fchmod(directory_fd, 0o700)
        except OSError:
            os.close(directory_fd)
            raise
    return directory_fd


def _open_outbox(data_path: Path, *, create: bool) -> int | None:
    data_fd = _open_directory(data_path, create=create)
    if data_fd is None:
        return None
    try:
        directory_fd = _open_outbox_child(data_fd, create=create)
    finally:
        os.close(data_fd)
    if directory_fd is None:
        return None
    return _verify_outbox(directory_fd, create=create)


def _name_identity(directory_fd: int, name: str) -> tuple[int, int] | None:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return None
    return info.st_dev, info.st_ino


def _lock_name(name: str) -> str:
    return f".lock-{hashlib.sha256(name.encode()).hexdigest()[:16]}"


def _acquire_proposal_lock(directory_fd: int, name: str) -> int:
    lock_name = _lock_name(name)
    lock_fd = os.open(
        lock_name,
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        info = os.fstat(lock_fd)
        identity = (info.st_dev, info.st_ino)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or _name_identity(directory_fd, lock_name) != identity
        ):
            raise OSError("proposal lock must be a stable operator-owned private regular file")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if _name_identity(directory_fd, lock_name) != identity:
            raise OSError("proposal lock changed while acquiring it")
    except BaseException:
        os.close(lock_fd)
        raise
    return lock_fd


def _release_proposal_lock(lock_fd: int) -> None:
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


@contextmanager
def _proposal_lock(directory_fd: int, name: str) -> Iterator[int]:
    """Hold the hardened persistent lock for one proposal basename."""
    lock_fd = _acquire_proposal_lock(directory_fd, name)
    try:
        yield lock_fd
    finally:
        _release_proposal_lock(lock_fd)


def _validate_created_at(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("created_at must be a timezone-aware ISO-8601 timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("created_at must be a timezone-aware ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("created_at must be a timezone-aware ISO-8601 timestamp")
    return value


def _validate_command_argv(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not value or not all(isinstance(token, str) and token for token in value):
        raise ValueError("command_argv must be null or a non-empty string array")
    command_argv = tuple(cast(list[str], value))
    first = command_argv[0]
    if _COMMAND_GROUP.fullmatch(first) is None or first in {"fieldkit", "gws"} or first.startswith(("/", "-")):
        raise ValueError("command_argv must contain fieldkit command tokens without a program name")
    return command_argv


def _validate_optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 1200:
        raise ValueError(f"{name} must be null or a bounded non-empty string")
    return value


def _validate_v2_fields(payload: dict[object, object]) -> tuple[str, str, str | None]:
    recommendation = _validate_optional_text(payload["recommendation"], "recommendation")
    if recommendation is None:
        raise ValueError("recommendation must be a bounded non-empty string")
    provenance = payload["decision_provenance"]
    if not isinstance(provenance, str) or provenance not in {"deterministic", "llm"}:
        raise ValueError("decision_provenance must be deterministic or llm")
    fallback = payload["fallback_category"]
    if fallback is not None and (
        not isinstance(fallback, str)
        or fallback not in {"disabled", "auth", "rate-limit", "provider", "empty", "parse", "invalid-command"}
    ):
        raise ValueError("fallback_category is invalid")
    return recommendation, provenance, fallback


def _validated_envelope(
    version: int,
    item_id: str,
    created_at: str,
    command_argv: tuple[str, ...] | None,
    markdown: str,
    payload: dict[object, object],
) -> _ValidatedEnvelope:
    if version == 1:
        return _ValidatedEnvelope(version, item_id, created_at, command_argv, markdown)
    recommendation, provenance, fallback = _validate_v2_fields(payload)
    if provenance == "llm" and fallback not in {None, "invalid-command"}:
        raise ValueError("llm provenance permits only an invalid-command fallback")
    if provenance == "deterministic" and fallback is None:
        raise ValueError("deterministic provenance requires a provider fallback category")
    if provenance == "deterministic" and fallback == "invalid-command" and command_argv is None:
        raise ValueError("deterministic invalid-command fallback requires command_argv")
    return _ValidatedEnvelope(
        version, item_id, created_at, command_argv, markdown, recommendation, provenance, fallback
    )


def _validate_envelope(payload: object) -> _ValidatedEnvelope:
    if not isinstance(payload, dict):
        raise ValueError("proposal envelope must be an object")
    version = payload.get("version")
    if type(version) is not int or version not in {1, 2}:
        raise ValueError("version must be 1 or 2")
    expected = _ENVELOPE_KEYS[version]
    if frozenset(payload) != expected:
        raise ValueError(f"expected exactly the version-{version} envelope keys")

    item_id = payload["item_id"]
    if not isinstance(item_id, str) or _ITEM_ID.fullmatch(item_id) is None:
        raise ValueError("item_id must be 16 lowercase hexadecimal characters")

    created_at = _validate_created_at(payload["created_at"])
    command_argv = _validate_command_argv(payload["command_argv"])

    markdown = payload["markdown"]
    if not isinstance(markdown, str) or not markdown.strip():
        raise ValueError("markdown must be non-empty")
    return _validated_envelope(version, item_id, created_at, command_argv, markdown, payload)


def _invalid(name: str, mtime: float, exc: Exception) -> OutboxProposal:
    message = f"Invalid proposal envelope: {exc}"
    return OutboxProposal(name, mtime, None, None, None, None, message, False, message)


def _read_regular_identity(directory_fd: int, name: str) -> tuple[str, float, tuple[int, int]] | None:
    try:
        file_fd = os.open(name, _READ_FLAGS, dir_fd=directory_fd)
    except OSError:
        return None
    try:
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        with os.fdopen(file_fd, encoding="utf-8") as handle:
            file_fd = -1
            return handle.read(), info.st_mtime, (info.st_dev, info.st_ino)
    except (OSError, UnicodeError):
        return None
    finally:
        if file_fd >= 0:
            os.close(file_fd)


def list_proposals(data_path: Path) -> list[OutboxProposal]:
    """Return current and legacy proposals newest first, failing closed on unsafe files."""
    directory_fd = _open_outbox(data_path, create=False)
    if directory_fd is None:
        return []
    proposals: list[OutboxProposal] = []
    try:
        try:
            names = os.listdir(directory_fd)  # noqa: PTH208  # dir-fd containment; Path.iterdir follows paths
        except OSError:
            return []
        for name in names:
            if not name.endswith((_CURRENT_SUFFIX, _LEGACY_SUFFIX)):
                continue
            loaded = _read_regular_identity(directory_fd, name)
            if loaded is None:
                continue
            content, mtime, identity = loaded
            if name.endswith(_LEGACY_SUFFIX):
                match = _FILENAME_ITEM_ID.search(name)
                proposals.append(
                    OutboxProposal(
                        name,
                        mtime,
                        None,
                        match.group(1) if match else None,
                        None,
                        None,
                        content,
                        False,
                        None,
                        identity=identity,
                    )
                )
                continue
            try:
                payload: Any = json.loads(content)
                envelope = _validate_envelope(payload)
            except (json.JSONDecodeError, ValueError) as exc:
                proposals.append(_invalid(name, mtime, exc))
            else:
                proposals.append(
                    OutboxProposal(
                        name,
                        mtime,
                        envelope.version,
                        envelope.item_id,
                        envelope.created_at,
                        envelope.command_argv,
                        envelope.markdown,
                        envelope.command_argv is not None,
                        None,
                        envelope.recommendation,
                        envelope.decision_provenance,
                        envelope.fallback_category,
                        identity,
                    )
                )
    finally:
        os.close(directory_fd)
    return sorted(proposals, key=lambda proposal: (-proposal.mtime, proposal.name))


def load_proposal(data_path: Path, name: str) -> OutboxProposal | None:
    """Load one safe proposal basename through the common listing contract."""
    if Path(name).name != name or not name.endswith((_CURRENT_SUFFIX, _LEGACY_SUFFIX)):
        return None
    return next((proposal for proposal in list_proposals(data_path) if proposal.name == name), None)


def proposal_item_ids(data_path: Path) -> set[str]:
    """Return item ids represented by valid current or readable legacy proposals."""
    return {proposal.item_id for proposal in list_proposals(data_path) if proposal.item_id is not None}


def _slug(item: AttentionItem, action: ProposedAction) -> str:
    raw = action.skill or item.source or "item"
    slug = _SLUG_SAFE.sub("-", raw).strip("-").lower()
    return slug or "item"


def _fmt_command(argv: tuple[str, ...] | None) -> str:
    if not argv:
        return "—"
    return "fieldkit " + " ".join(argv)


def _decision_display(action: ProposedAction, provenance: str | None, fallback: str | None) -> tuple[str, str, str]:
    return action.recommendation or action.rationale, provenance or "deterministic", fallback or "—"


def render_proposal(
    item: AttentionItem,
    action: ProposedAction,
    enrichment_output: str | None,
    *,
    decision_provenance: str | None = None,
    fallback_category: str | None = None,
) -> str:
    """Render the Markdown review surface for one proposal."""
    account = item.account or "—"
    skill = action.skill or "—"
    context = (
        f"```\n{enrichment_output[:_ENRICHMENT_CAP].rstrip()}\n```" if enrichment_output else "_enrichment unavailable_"
    )
    recommendation, source, fallback = _decision_display(action, decision_provenance, fallback_category)
    return "\n".join(
        [
            f"# Companion proposal — {item.severity.upper()} ({account})",
            "",
            f"- **Item:** `{item.item_id}`",
            f"- **Source:** `{item.source}`",
            f"- **Account:** {account}",
            f"- **Severity:** {item.severity}",
            f"- **Suggested skill:** {skill}",
            f"- **Suggested command:** `{_fmt_command(action.command_argv)}`",
            f"- **Decision source:** {source}",
            f"- **Decision fallback:** {fallback}",
            f"- **Evidence:** `{item.evidence_path}`",
            "",
            "## Recommendation",
            "",
            recommendation,
            "",
            "## Rationale",
            "",
            action.rationale,
            "",
            "## Context",
            "",
            context,
            "",
        ]
    )


def _filename_date(observed_at: str) -> str:
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:T.*)?", observed_at)
    if match is None:
        return "undated"
    candidate = match.group(1)
    try:
        dt.date.fromisoformat(candidate)
    except ValueError:
        return "undated"
    return candidate


def _verify_published(directory_fd: int, filename: str, expected_identity: tuple[int, int]) -> tuple[int, int]:
    published_fd = os.open(filename, _READ_FLAGS, dir_fd=directory_fd)
    try:
        info = os.fstat(published_fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size == 0
            or (info.st_dev, info.st_ino) != expected_identity
            or _name_identity(directory_fd, filename) != expected_identity
        ):
            raise OSError("proposal envelope was not published as a non-empty regular file")
        return expected_identity
    finally:
        os.close(published_fd)


def _close_for_cleanup(file_fd: int) -> OSError | None:
    if file_fd < 0:
        return None
    try:
        os.close(file_fd)
    except OSError as error:
        return error
    return None


def _unlink_for_cleanup(directory_fd: int, temp_name: str | None) -> OSError | None:
    if temp_name is None:
        return None
    try:
        os.unlink(temp_name, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    except OSError as error:
        return error
    return None


def _report_cleanup_errors(cleanup_errors: list[OSError], active_error: BaseException | None) -> None:
    if not cleanup_errors:
        return
    if active_error is None:
        raise cleanup_errors[0]
    for cleanup_error in cleanup_errors:
        active_error.add_note(f"proposal cleanup also failed: {cleanup_error}")


def _cleanup_publish(
    directory_fd: int, temp_fd: int, temp_name: str | None, active_error: BaseException | None
) -> None:
    attempts = (
        _close_for_cleanup(temp_fd),
        _unlink_for_cleanup(directory_fd, temp_name),
    )
    _report_cleanup_errors([error for error in attempts if error is not None], active_error)


def _write_atomic(directory_fd: int, filename: str, payload: dict[str, object]) -> tuple[int, int]:
    temp_name: str | None = None
    temp_fd = -1
    temp_identity: tuple[int, int] | None = None
    try:
        temp_name = f".proposal-{secrets.token_hex(16)}.tmp"
        temp_fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        temp_info = os.fstat(temp_fd)
        temp_identity = (temp_info.st_dev, temp_info.st_ino)
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
            temp_fd = -1
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        return _verify_published(directory_fd, filename, temp_identity)
    finally:
        _cleanup_publish(directory_fd, temp_fd, temp_name, sys.exception())


def _publish_envelope(directory_fd: int, filename: str, payload: dict[str, object]) -> None:
    try:
        with _proposal_lock(directory_fd, filename):
            _write_atomic(directory_fd, filename, payload)
    finally:
        os.close(directory_fd)


def _closed_claim(status: ApprovalStatus) -> ProposalClaim:
    return ProposalClaim(status, None)


def _claim_payload(directory_fd: int, name: str) -> tuple[OutboxProposal, dict[str, object], tuple[int, int]] | None:
    loaded = _read_regular_identity(directory_fd, name)
    if loaded is None:
        return None
    content, mtime, identity = loaded
    try:
        raw: Any = json.loads(content)
        envelope = _validate_envelope(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if envelope.command_argv is None:
        return None
    proposal = OutboxProposal(
        name,
        mtime,
        envelope.version,
        envelope.item_id,
        envelope.created_at,
        envelope.command_argv,
        envelope.markdown,
        True,
        None,
        envelope.recommendation,
        envelope.decision_provenance,
        envelope.fallback_category,
        identity,
    )
    payload = dict(cast(dict[str, object], raw))
    return proposal, payload, identity


def claim_proposal(data_path: Path, name: str) -> ProposalClaim:
    """Replace one approvable envelope with an exclusive execution marker."""
    if Path(name).name != name or not name.endswith(_CURRENT_SUFFIX):
        return _closed_claim("not-approvable")
    directory_fd = _open_outbox(data_path, create=False)
    if directory_fd is None:
        return _closed_claim("missing")
    lock_fd: int | None = None
    lock_manager: AbstractContextManager[int] | None = None
    try:
        lock_manager = _proposal_lock(directory_fd, name)
        lock_fd = lock_manager.__enter__()
        claimed = _claim_payload(directory_fd, name)
        if claimed is None:
            status: ApprovalStatus = "missing" if _name_identity(directory_fd, name) is None else "not-approvable"
            lock_manager.__exit__(None, None, None)
            lock_fd = None
            lock_manager = None
            os.close(directory_fd)
            return _closed_claim(status)
        proposal, original_payload, proposal_identity = claimed
        if _name_identity(directory_fd, name) != proposal_identity:
            lock_manager.__exit__(None, None, None)
            lock_fd = None
            lock_manager = None
            os.close(directory_fd)
            return _closed_claim("conflict")
        marker_payload = dict(original_payload)
        marker_payload["command_argv"] = None
        marker_payload["markdown"] = (
            "Execution in progress or outcome unknown; do not retry automatically.\n\n" + proposal.markdown
        )
        marker_identity = _write_atomic(directory_fd, name, marker_payload)
        return ProposalClaim(
            "claimed", proposal, directory_fd, lock_fd, lock_manager, marker_identity, original_payload
        )
    except BaseException:
        if lock_fd is not None and lock_manager is not None:
            lock_manager.__exit__(*sys.exc_info())
        os.close(directory_fd)
        raise


def lock_proposal_for_retention(data_path: Path, name: str) -> ProposalRetention:
    """Lock one approvable generation for retention without changing its payload."""
    if Path(name).name != name or not name.endswith(_CURRENT_SUFFIX):
        return ProposalRetention("not-retirable", None)
    directory_fd = _open_outbox(data_path, create=False)
    if directory_fd is None:
        return ProposalRetention("missing", None)
    lock_fd: int | None = None
    lock_manager: AbstractContextManager[int] | None = None
    try:
        lock_manager = _proposal_lock(directory_fd, name)
        lock_fd = lock_manager.__enter__()
        loaded = _claim_payload(directory_fd, name)
        if loaded is None:
            status: RetentionStatus = "missing" if _name_identity(directory_fd, name) is None else "not-retirable"
            lock_manager.__exit__(None, None, None)
            lock_fd = None
            lock_manager = None
            os.close(directory_fd)
            return ProposalRetention(status, None)
        proposal, _payload, identity = loaded
        if _name_identity(directory_fd, name) != identity:
            lock_manager.__exit__(None, None, None)
            lock_fd = None
            lock_manager = None
            os.close(directory_fd)
            return ProposalRetention("conflict", None)
        return ProposalRetention("locked", proposal, directory_fd, lock_fd, lock_manager, identity)
    except BaseException:
        if lock_fd is not None and lock_manager is not None:
            lock_manager.__exit__(*sys.exc_info())
        os.close(directory_fd)
        raise


def replace_invalid_command_proposal(
    data_path: Path, candidate: OutboxProposal, item: AttentionItem, action: ProposedAction
) -> ReconciliationStatus:
    """Atomically replace one unchanged legacy invalid-command envelope."""
    if not _is_reconciliation_candidate(candidate):
        return "not-reconcilable"
    directory_fd = _open_outbox(data_path, create=False)
    if directory_fd is None:
        return "missing"
    try:
        with _proposal_lock(directory_fd, candidate.name):
            loaded = _read_regular_identity(directory_fd, candidate.name)
            if loaded is None:
                return "missing"
            content, mtime, identity = loaded
            if mtime != candidate.mtime or identity != candidate.identity:
                return "conflict"
            try:
                raw = json.loads(content)
                envelope = _validate_envelope(raw)
            except (json.JSONDecodeError, ValueError):
                return "not-reconcilable"
            status = _reconciliation_status(directory_fd, candidate.name, identity, envelope, item, action)
            if status != "reconciled":
                return status
            payload = _proposal_payload(item, action, None, "deterministic", "invalid-command")
            _validate_envelope(payload)
            _write_atomic(directory_fd, candidate.name, payload)
            return "reconciled"
    finally:
        os.close(directory_fd)


def _is_reconciliation_candidate(candidate: OutboxProposal) -> bool:
    """Return whether a proposal has the safe identity required for replacement."""
    return (
        candidate.name == Path(candidate.name).name
        and candidate.name.endswith(_CURRENT_SUFFIX)
        and candidate.identity is not None
    )


def _reconciliation_status(
    directory_fd: int,
    name: str,
    identity: tuple[int, int],
    envelope: _ValidatedEnvelope,
    item: AttentionItem,
    action: ProposedAction,
) -> ReconciliationStatus:
    """Check an opened envelope still matches the legacy record being repaired."""
    if _name_identity(directory_fd, name) != identity:
        return "conflict"
    expected = (2, item.item_id, "llm", "invalid-command", None)
    actual = (
        envelope.version,
        envelope.item_id,
        envelope.decision_provenance,
        envelope.fallback_category,
        envelope.command_argv,
    )
    if actual != expected or action.command_argv is None:
        return "not-reconcilable"
    return "reconciled"


def _proposal_payload(
    item: AttentionItem,
    action: ProposedAction,
    enrichment_output: str | None,
    decision_provenance: str | None,
    fallback_category: str | None,
) -> dict[str, object]:
    markdown = render_proposal(
        item,
        action,
        enrichment_output,
        decision_provenance=decision_provenance,
        fallback_category=fallback_category,
    )
    version = 2 if decision_provenance is not None else 1
    payload: dict[str, object] = {
        "version": version,
        "item_id": item.item_id,
        "created_at": dt.datetime.now(tz=dt.UTC).isoformat(),
        "command_argv": list(action.command_argv) if action.command_argv is not None else None,
        "markdown": markdown,
    }
    if version == 2:
        payload.update(
            recommendation=action.recommendation or action.rationale,
            decision_provenance=decision_provenance,
            fallback_category=fallback_category,
        )
    return payload


def _proposal_filename(item: AttentionItem, action: ProposedAction) -> str:
    filename = f"{_filename_date(item.observed_at)}-{item.item_id}-{_slug(item, action)}{_CURRENT_SUFFIX}"
    if Path(filename).name != filename or filename in {".", ".."} or "/" in filename:
        raise ValueError("proposal target must be a basename")
    return filename


def write_proposal(
    data_path: Path,
    item: AttentionItem,
    action: ProposedAction,
    *,
    enrichment_output: str | None = None,
    decision_provenance: str | None = None,
    fallback_category: str | None = None,
) -> Path:
    """Atomically publish one validated proposal envelope."""
    payload = _proposal_payload(item, action, enrichment_output, decision_provenance, fallback_category)
    _validate_envelope(payload)
    filename = _proposal_filename(item, action)

    directory_fd = _open_outbox(data_path, create=True)
    if directory_fd is None:
        raise OSError("could not create companion outbox")
    _publish_envelope(directory_fd, filename, payload)
    return outbox_dir(data_path) / filename
