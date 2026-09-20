"""Private, atomic evidence for independent driver completion checks."""

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

EvidenceDecision = Literal["passed", "failed", "incomplete"]


@dataclass(frozen=True)
class OutputEvidence:
    """Privacy-bounded metadata for one captured stream."""

    byte_count: int
    sha256: str
    complete: bool


@dataclass(frozen=True)
class CheckEvidence:
    """Durable result metadata for one check, excluding raw output."""

    check_id: str
    status: str
    exit_code: int | None
    duration_ms: int
    argv: tuple[str, ...]
    executable_sha256: str
    stdout: OutputEvidence
    stderr: OutputEvidence


@dataclass(frozen=True)
class EvidenceRecord:
    """Schema-version-1 verification evidence."""

    attempt_id: str
    repository_id: int
    issue_number: int
    pr_number: int | None
    attempt: int
    base_sha: str
    initial_head_sha: str | None
    final_head_sha: str | None
    work_order_sha256: str
    contract_sha256: str
    checker_sha256: dict[str, str]
    started_at: str
    completed_at: str
    checks: tuple[CheckEvidence, ...]
    cleanup_status: str
    decision: EvidenceDecision
    phase: Literal["checks-complete", "terminal", "failure"]


def _payload(record: EvidenceRecord) -> bytes:
    return (json.dumps({"schema_version": 1, **asdict(record)}, sort_keys=True, separators=(",", ":")) + "\n").encode()


def write_evidence(root: Path, record: EvidenceRecord) -> Path:
    """Atomically persist *record* with private modes and directory fsync."""
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    destination = root / f"{record.attempt_id}.json"
    temporary = root / f".{record.attempt_id}.{os.getpid()}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_payload(record))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
        destination.chmod(0o600)
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination
