"""Acquire diagnostic rehearsal bytes below independently selected, pinned roots.

Catalog names are relative to the selected evidence root, not the receipt's
directory. This module does not select or authenticate a controller. Every
canonical validation performed here uses context=None and remains nonpassing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING or __package__:
    from scripts import release_bundle, release_filesystem
    from scripts._release_bundle_evidence import _candidate_inputs
    from scripts.json_policy import load_json_bytes
    from scripts.rehearsal_evidence import (
        MAX_EVIDENCE_BYTES,
        MAX_RETAINED_MEMBERS,
        InitialExportSubject,
        RehearsalValidation,
        validate_rehearsal,
    )
else:
    import release_bundle
    import release_filesystem
    from _release_bundle_evidence import _candidate_inputs
    from json_policy import load_json_bytes
    from rehearsal_evidence import (
        MAX_EVIDENCE_BYTES,
        MAX_RETAINED_MEMBERS,
        InitialExportSubject,
        RehearsalValidation,
        validate_rehearsal,
    )


# Ten records, seven support slots, and three retained rehearsal catalogs:
# (10 + 7 + 3) * 5 MiB. Ledger, reports, schemas and artifacts are separate.
MAX_ACQUIRED_EVIDENCE_BYTES = 100 * 1024 * 1024


@dataclass(kw_only=True)
class AcquisitionBudget:
    """Bound actual retained bytes; callers deduplicate their selected paths."""

    maximum_bytes: int = field(default_factory=lambda: MAX_ACQUIRED_EVIDENCE_BYTES)
    acquired_bytes: int = field(default=0, init=False)

    def read(self, root_fd: int, name: str, *, maximum_bytes: int = MAX_EVIDENCE_BYTES) -> bytes:
        remaining = self.maximum_bytes - self.acquired_bytes
        if remaining < 0:
            raise ValueError("evidence acquisition exceeds aggregate byte limit")
        limit = min(maximum_bytes, remaining)
        try:
            data = read_relative_bytes(root_fd, name, maximum_bytes=limit)
        except ValueError:
            if limit < maximum_bytes:
                raise ValueError("evidence member is unavailable or exceeds aggregate byte limit") from None
            raise
        if len(data) > limit:
            if remaining < maximum_bytes:
                raise ValueError("evidence acquisition exceeds aggregate byte limit")
            raise ValueError("evidence member exceeds its byte limit")
        self.acquired_bytes += len(data)
        return data


def canonical_relative_path(value: object, subject: str) -> str:
    """Return one canonical POSIX path relative to the selected evidence root."""
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{subject} is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or path.as_posix() != value:
        raise ValueError(f"{subject} is invalid")
    return value


def open_real_directory(path: Path) -> int:
    """Pin a directory after rejecting every symlinked path component."""
    try:
        return release_filesystem.open_real_directory(path)
    except (OSError, ValueError):
        raise ValueError("evidence root must be a real directory path") from None


def read_relative_bytes(root_fd: int, relative_path: str, *, maximum_bytes: int = MAX_EVIDENCE_BYTES) -> bytes:
    """Read one bounded canonical path below a pinned evidence root."""
    parts = PurePosixPath(canonical_relative_path(relative_path, "evidence member path")).parts
    descriptor = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child = release_filesystem.open_directory_at(descriptor, part)
            os.close(descriptor)
            descriptor = child
        file_fd = os.open(parts[-1], release_filesystem.file_flags(), dir_fd=descriptor)
        try:
            return release_filesystem.read_regular_file(file_fd, maximum_bytes=maximum_bytes)
        finally:
            os.close(file_fd)
    except (OSError, ValueError):
        raise ValueError("evidence member is unavailable or exceeds its byte limit") from None
    finally:
        os.close(descriptor)


def read_root_bytes(root: Path, relative_path: str) -> bytes:
    """Acquire a bounded file beneath a caller-selected real root."""
    descriptor = open_real_directory(root)
    try:
        return read_relative_bytes(descriptor, relative_path)
    finally:
        os.close(descriptor)


def rehearsal_member_names(receipt_bytes: bytes) -> tuple[str, ...]:
    """Inspect only the bounded I/O inventory; the canonical decoder checks proof."""
    if len(receipt_bytes) > MAX_EVIDENCE_BYTES:
        raise ValueError("rehearsal receipt exceeds byte limit")
    receipt = load_json_bytes(receipt_bytes)
    if not isinstance(receipt, dict):
        raise ValueError("rehearsal receipt must be an object")
    members = receipt.get("members")
    if not isinstance(members, list) or len(members) > MAX_RETAINED_MEMBERS:
        raise ValueError("rehearsal member inventory is invalid or exceeds limit")
    names: list[str] = []
    seen: set[str] = set()
    declared_bytes = 0
    for member in members:
        if not isinstance(member, dict):
            raise ValueError("rehearsal member inventory is invalid")
        name = canonical_relative_path(member.get("name"), "rehearsal member path")
        size = member.get("size")
        if name in seen or type(size) is not int or size < 0:
            raise ValueError("rehearsal member inventory is invalid")
        declared_bytes += size
        if declared_bytes > MAX_EVIDENCE_BYTES:
            raise ValueError("rehearsal member inventory exceeds aggregate byte limit")
        names.append(name)
        seen.add(name)
    return tuple(names)


def verified_bundle_artifacts(
    bundle_root_fd: int, *, candidate_report_bytes: bytes, expected_checksums: bytes | None = None
) -> dict[str, bytes]:
    """Return the exact artifact bytes captured by initial bundle verification."""
    verified = release_bundle.capture_open_bundle(
        bundle_root_fd, candidate_report=candidate_report_bytes, expected_checksums=expected_checksums
    )
    artifacts: dict[str, bytes] = {}
    for name, _digest in verified.report.files:
        if name.endswith((".whl", ".tar.gz")):
            artifacts[name] = verified.members[name]
    if len(artifacts) != 2:
        raise ValueError("verified bundle does not contain exactly two rehearsal artifacts")
    return artifacts


def manual_record_paths(evidence_bytes: bytes, *, expected_criterion_ids: tuple[str, ...]) -> tuple[str, ...]:
    """Preflight the caller's finite record inventory before any record read."""
    if len(evidence_bytes) > MAX_EVIDENCE_BYTES:
        raise ValueError("manual evidence ledger exceeds byte limit")
    ledger = load_json_bytes(evidence_bytes)
    criteria = ledger.get("criteria") if isinstance(ledger, dict) else None
    if not isinstance(criteria, list) or len(criteria) != len(expected_criterion_ids):
        raise ValueError("manual release evidence criteria inventory is invalid")
    identifiers = [item.get("id") if isinstance(item, dict) else None for item in criteria]
    if (
        not all(isinstance(identifier, str) for identifier in identifiers)
        or len(set(identifiers)) != len(identifiers)
        or set(identifiers) != set(expected_criterion_ids)
    ):
        raise ValueError("manual release evidence criteria inventory is invalid")
    names: list[str] = []
    for item in criteria:
        if not isinstance(item, dict) or not isinstance(item.get("record"), dict):
            raise ValueError("manual release evidence record inventory is invalid")
        names.append(canonical_relative_path(item["record"].get("path"), "manual release evidence record path"))
    if len(names) != len(set(names)):
        raise ValueError("manual release evidence record path is duplicated")
    return tuple(names)


def acquire_manual_evidence(
    evidence_root_fd: int,
    evidence_bytes: bytes,
    *,
    expected_criterion_ids: tuple[str, ...],
    support_path_fields: frozenset[str],
) -> dict[str, bytes]:
    """Acquire a finite ledger inventory without validating its proof semantics."""
    record_paths = manual_record_paths(evidence_bytes, expected_criterion_ids=expected_criterion_ids)
    acquired: dict[str, bytes] = {}
    budget = AcquisitionBudget()

    def acquire(name: str, maximum_bytes: int = MAX_EVIDENCE_BYTES) -> None:
        if name not in acquired:
            if len(acquired) >= MAX_RETAINED_MEMBERS:
                raise ValueError("manual release evidence inventory exceeds member limit")
            acquired[name] = budget.read(evidence_root_fd, name, maximum_bytes=maximum_bytes)

    for name in record_paths:
        acquire(name)
    support_paths: set[str] = set()
    rehearsal_paths: set[str] = set()
    for raw in tuple(acquired.values()):
        record = load_json_bytes(raw)
        proof = record.get("proof") if isinstance(record, dict) else None
        payload = proof.get("payload") if isinstance(proof, dict) else None
        if not isinstance(payload, dict):
            continue
        for path_field in support_path_fields & set(payload):
            support_paths.add(canonical_relative_path(payload[path_field], "manual release evidence support path"))
        if "rehearsal_evidence_path" in payload:
            rehearsal_paths.add(canonical_relative_path(payload["rehearsal_evidence_path"], "rehearsal receipt path"))
    for name in sorted(support_paths):
        acquire(name)
    for receipt_path in sorted(rehearsal_paths):
        retained_size = 0
        for name in rehearsal_member_names(acquired[receipt_path]):
            acquire(name, MAX_EVIDENCE_BYTES - retained_size)
            retained_size += len(acquired[name])
            if retained_size > MAX_EVIDENCE_BYTES:
                raise ValueError("rehearsal retained members exceed aggregate byte limit")
    return acquired


def bind_rehearsal_candidate(
    diagnostic: RehearsalValidation,
    *,
    candidate_report_bytes: bytes,
    private_candidate_report_bytes: bytes | None = None,
) -> None:
    """Bind typed initial observations to selected reports, not to live authority.

    The independent report parse preserves the canonical report validation
    boundary. Public reports alone cannot identify the historical private source.
    """
    current = _candidate_inputs(candidate_report_bytes)
    current_manifest = current.report["export_manifest"]
    assert isinstance(current_manifest, dict)
    if not isinstance(diagnostic.subject, InitialExportSubject):
        raise ValueError("rehearsal acquisition supports only initial-export candidate reports")
    if diagnostic.phase == "public-release":
        if private_candidate_report_bytes is None:
            raise ValueError("public rehearsal diagnostic requires a selected private candidate report")
        private = _candidate_inputs(private_candidate_report_bytes)
        public = diagnostic.public
        if (
            public is None
            or public.repository != current.report["expected_repository"]
            or public.commit_sha != current_manifest["source_commit"]
            or public.tree != current_manifest["exported_tree"]
            or private.report["expected_repository"] != current.report["expected_repository"]
            or private.report["planned_tag"] != current.report["planned_tag"]
            or private.artifacts != current.artifacts
        ):
            raise ValueError("rehearsal diagnostic does not bind the selected public candidate")
    else:
        private = current
    private_manifest = private.report["export_manifest"]
    assert isinstance(private_manifest, dict)
    if (
        diagnostic.subject.private_source_sha != private_manifest["source_commit"]
        or diagnostic.subject.private_source_tree != private_manifest["source_tree"]
        or diagnostic.subject.exported_tree != private_manifest["exported_tree"]
        or diagnostic.subject.exported_tree != current_manifest["exported_tree"]
        or tuple((item.name, item.sha256) for item in diagnostic.artifacts) != current.artifacts
    ):
        raise ValueError("rehearsal diagnostic does not bind the selected private source or artifacts")


def acquire_rehearsal(
    receipt_bytes: bytes,
    *,
    schema_bytes: bytes,
    evidence_root_fd: int,
    bundle_root_fd: int,
    candidate_report_bytes: bytes,
    private_candidate_report_bytes: bytes | None = None,
    expected_checksums: bytes | None = None,
) -> RehearsalValidation:
    """Acquire exact bytes and return a typed-only, explicitly unapproved diagnostic."""
    artifacts = verified_bundle_artifacts(
        bundle_root_fd, candidate_report_bytes=candidate_report_bytes, expected_checksums=expected_checksums
    )
    members: dict[str, bytes] = {}
    budget = AcquisitionBudget()
    retained_bytes = 0
    for name in rehearsal_member_names(receipt_bytes):
        data = budget.read(evidence_root_fd, name, maximum_bytes=MAX_EVIDENCE_BYTES - retained_bytes)
        retained_bytes += len(data)
        members[name] = data
    diagnostic = validate_rehearsal(
        receipt_bytes, schema_bytes=schema_bytes, context=None, retained_members=members, artifact_bytes=artifacts
    )
    bind_rehearsal_candidate(
        diagnostic,
        candidate_report_bytes=candidate_report_bytes,
        private_candidate_report_bytes=private_candidate_report_bytes,
    )
    return diagnostic
