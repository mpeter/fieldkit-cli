"""Validate retained release input structure without granting release authority."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import sys
from dataclasses import dataclass, fields
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from jsonschema import Draft202012Validator

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import cutover_record, rehearsal_acquisition, release_bundle, release_filesystem, release_manual_evidence
from scripts.json_policy import load_json_bytes
from scripts.rehearsal_acquisition import (
    AcquisitionBudget,
    manual_record_paths,
    read_root_bytes,
    verified_bundle_artifacts,
)

_SCHEMA = Path("docs/release-readiness/release-approval-input.schema.json")
_PUBLIC_TREE_POLICY = Path("docs/release-readiness/public-tree-policy.json")
_MAX_APPROVAL_MEMBER_BYTES = 5 * 1024 * 1024
_MAX_APPROVAL_TREE_ENTRIES = 4096
_MAX_APPROVAL_TREE_DEPTH = 64
_REQUIRED_MEMBERS = frozenset(
    {
        "private-candidate-report.json",
        "public-candidate/report.json",
        "public-candidate/bundle/SHA256SUMS",
        "evidence/ledger.json",
        "cutover-record.json",
    }
)


@dataclass(frozen=True, kw_only=True)
class ExpectedApprovalIdentity:
    """Caller-selected structural expectations; this object is not authentication."""

    repository: str
    private_source_sha: str
    private_source_tree: str
    exported_tree: str
    public_commit: str
    public_tree: str
    planned_tag: str
    artifacts: tuple[tuple[str, Literal["wheel", "sdist"], str], ...]
    documentation_contract_sha256: str
    public_tree_policy_sha256: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository) is None:
            raise ValueError("approval expected repository is invalid")
        for value in (
            self.private_source_sha,
            self.private_source_tree,
            self.exported_tree,
            self.public_commit,
            self.public_tree,
        ):
            if re.fullmatch(r"[0-9a-f]{40}", value) is None:
                raise ValueError("approval expected commit or tree is invalid")
        if re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", self.planned_tag) is None:
            raise ValueError("approval expected tag is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", self.documentation_contract_sha256) is None:
            raise ValueError("approval expected documentation contract digest is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", self.public_tree_policy_sha256) is None:
            raise ValueError("approval expected public tree policy digest is invalid")
        if (
            not isinstance(self.artifacts, tuple)
            or len(self.artifacts) != 2
            or any(
                not isinstance(item, tuple)
                or len(item) != 3
                or not item[0]
                or "/" in item[0]
                or "\\" in item[0]
                or re.fullmatch(r"[0-9a-f]{64}", item[2]) is None
                for item in self.artifacts
            )
            or {item[1] for item in self.artifacts} != {"wheel", "sdist"}
            or len({item[0] for item in self.artifacts}) != 2
        ):
            raise ValueError("approval expected artifacts are invalid")


def _expected_selection(path: Path) -> ExpectedApprovalIdentity:
    raw = _load_json_bytes(read_root_bytes(path.parent, path.name), "expected selection")
    string_fields = {field.name for field in fields(ExpectedApprovalIdentity)} - {"artifacts"}
    if set(raw) != string_fields | {"artifacts"} or not all(isinstance(raw[name], str) for name in string_fields):
        raise ValueError("approval expected selection fields are invalid")
    artifacts = raw["artifacts"]
    if (
        not isinstance(artifacts, list)
        or len(artifacts) != 2
        or any(
            not isinstance(item, list) or len(item) != 3 or not all(isinstance(value, str) for value in item)
            for item in artifacts
        )
    ):
        raise ValueError("approval expected artifacts are invalid")
    parsed_artifacts = []
    for name, kind_value, digest in artifacts:
        if kind_value not in {"wheel", "sdist"}:
            raise ValueError("approval expected artifacts are invalid")
        kind: Literal["wheel", "sdist"] = "wheel" if kind_value == "wheel" else "sdist"
        parsed_artifacts.append((name, kind, digest))
    return ExpectedApprovalIdentity(
        repository=raw["repository"],
        private_source_sha=raw["private_source_sha"],
        private_source_tree=raw["private_source_tree"],
        exported_tree=raw["exported_tree"],
        public_commit=raw["public_commit"],
        public_tree=raw["public_tree"],
        planned_tag=raw["planned_tag"],
        artifacts=tuple(parsed_artifacts),
        documentation_contract_sha256=raw["documentation_contract_sha256"],
        public_tree_policy_sha256=raw["public_tree_policy_sha256"],
    )


def _load_json_bytes(data: bytes, subject: str) -> dict[str, Any]:
    try:
        value = load_json_bytes(data)
    except ValueError:
        raise ValueError(f"invalid approval input JSON: {subject}") from None
    if not isinstance(value, dict):
        raise ValueError(f"approval input JSON must be an object: {subject}")
    return value


def _member_parts(name: object) -> tuple[str, ...]:
    if not isinstance(name, str) or not name or Path(name).is_absolute() or "\\" in name:
        raise ValueError("approval input member path is invalid")
    parts = Path(name).parts
    if not parts or PurePosixPath(name).as_posix() != name or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("approval input member escapes its root")
    if len(parts) > _MAX_APPROVAL_TREE_DEPTH:
        raise ValueError("approval input member exceeds its depth limit")
    return parts


def _open_real_directory(path: Path) -> int:
    """Open a directory through descriptor-relative, no-follow traversal."""
    try:
        return release_filesystem.open_real_directory(path)
    except (OSError, ValueError):
        raise ValueError("approval input root must be a real directory path") from None


def _open_member_directory(root_fd: int, name: object) -> int:
    """Open one nested directory from a pinned approval-input root."""
    parts = _member_parts(name)
    descriptor = os.dup(root_fd)
    try:
        for part in parts:
            child = release_filesystem.open_directory_at(descriptor, part)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException as error:
        os.close(descriptor)
        if isinstance(error, (OSError, ValueError)):
            raise ValueError("approval input directory is unavailable") from error
        raise


def _safe_bytes(root_fd: int, name: object) -> bytes:
    """Read one bounded member relative to a pinned approval-input root."""
    _member_parts(name)
    assert isinstance(name, str)
    try:
        return rehearsal_acquisition.read_relative_bytes(root_fd, name, maximum_bytes=_MAX_APPROVAL_MEMBER_BYTES)
    except (OSError, ValueError):
        raise ValueError("approval input member is unavailable or exceeds its byte limit") from None


def _retained_tree(root_fd: int, prefix: str = "", *, remaining: list[int] | None = None) -> tuple[set[str], set[str]]:
    """Enumerate regular files and real directories below one pinned root."""
    budget = [_MAX_APPROVAL_TREE_ENTRIES] if remaining is None else remaining
    files: set[str] = set()
    directories: set[str] = set()
    try:
        names = sorted(os.listdir(root_fd))
    except OSError as error:
        raise ValueError("approval input retained directory is unavailable") from error
    for name in names:
        budget[0] -= 1
        if budget[0] < 0:
            raise ValueError("approval input retained tree exceeds its entry limit")
        relative = f"{prefix}/{name}" if prefix else name
        _member_parts(relative)
        try:
            metadata = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except OSError as error:
            raise ValueError("approval input retained member is unavailable") from error
        if stat.S_ISREG(metadata.st_mode):
            files.add(relative)
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("approval input contains an unsafe retained member")
        directories.add(relative)
        try:
            child_fd = release_filesystem.open_directory_at(root_fd, name)
        except (OSError, ValueError) as error:
            raise ValueError("approval input retained directory is unavailable") from error
        try:
            child_files, child_directories = _retained_tree(child_fd, relative, remaining=budget)
        finally:
            os.close(child_fd)
        files.update(child_files)
        directories.update(child_directories)
    return files, directories


def _assert_exact_tree(root_fd: int, expected_files: set[str]) -> None:
    """Reject any file or directory outside the digest-closed approval input."""
    expected_directories = {
        parent.as_posix()
        for path in expected_files
        for parent in PurePosixPath(path).parents
        if parent != PurePosixPath(".")
    }
    actual_files, actual_directories = _retained_tree(root_fd)
    if actual_files != expected_files or actual_directories != expected_directories:
        raise ValueError("approval input contains unlisted files or directories")


def _load_repo_json(path: Path) -> dict[str, Any]:
    try:
        return _load_json_bytes(read_root_bytes(path.parent, path.name), "approval repository policy")
    except OSError as error:
        raise ValueError(f"approval input schema is unavailable: {path.name}") from error


def _artifact_identities(report: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
    validation = report.get("artifact_validation")
    if not isinstance(validation, dict) or not isinstance(validation.get("artifacts"), list):
        raise ValueError("approval input candidate report has no artifact validation")
    identities: set[tuple[str, str, str]] = set()
    for item in validation["artifacts"]:
        if not isinstance(item, dict) or item.get("status") != "pass":
            continue
        name, kind, digest = item.get("name"), item.get("kind"), item.get("sha256")
        if not isinstance(name, str) or not isinstance(kind, str) or not isinstance(digest, str):
            raise ValueError("approval input candidate report has invalid artifact identities")
        identities.add((name, kind, digest))
    if len(identities) != 2:
        raise ValueError("approval input candidate report must retain one wheel and source distribution")
    return tuple(sorted(identities))


def _manifest_artifacts(manifest: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
    candidate = manifest["candidate"]
    if not isinstance(candidate, dict) or not isinstance(candidate.get("artifacts"), list):
        raise ValueError("approval manifest has no candidate artifact identities")
    identities: set[tuple[str, str, str]] = set()
    for item in candidate["artifacts"]:
        if not isinstance(item, dict):
            raise ValueError("approval manifest candidate artifact is invalid")
        name, kind, digest = item.get("name"), item.get("kind"), item.get("sha256")
        if not isinstance(name, str) or not isinstance(kind, str) or not isinstance(digest, str):
            raise ValueError("approval manifest candidate artifact is invalid")
        identities.add((name, kind, digest))
    if len(identities) != 2:
        raise ValueError("approval manifest candidate artifacts are ambiguous")
    return tuple(sorted(identities))


def _ledger_record_paths(ledger: dict[str, Any]) -> frozenset[str]:
    criteria = ledger.get("criteria")
    if not isinstance(criteria, list):
        raise ValueError("approval input evidence ledger is invalid")
    paths = [
        record.get("path")
        for item in criteria
        if isinstance(item, dict) and isinstance(record := item.get("record"), dict)
    ]
    if not paths or not all(isinstance(path, str) for path in paths):
        raise ValueError("approval input evidence ledger has invalid record paths")
    normalized: list[str] = []
    for path in paths:
        assert isinstance(path, str)
        candidate = PurePosixPath(path)
        if (
            not path
            or candidate.is_absolute()
            or "\\" in path
            or not candidate.parts
            or candidate.parts[0] == "evidence"
            or any(part in {"", ".", ".."} for part in candidate.parts)
            or candidate.as_posix() != path
        ):
            raise ValueError("approval input evidence ledger has invalid record paths")
        normalized.append(path)
    if len(normalized) != len(set(normalized)):
        raise ValueError("approval input evidence ledger has ambiguous record paths")
    return frozenset(normalized)


def _record_bytes(ledger: dict[str, Any], members: dict[str, bytes]) -> dict[str, bytes]:
    """Map every retained evidence member to its canonical ledger-relative path."""
    retained = {
        member_path.removeprefix("evidence/"): data
        for member_path, data in members.items()
        if member_path.startswith("evidence/") and member_path != "evidence/ledger.json"
    }
    paths = _ledger_record_paths(ledger)
    if not paths <= set(retained):
        raise ValueError("approval manifest does not retain every evidence record")
    for path in retained:
        candidate = PurePosixPath(path)
        if (
            not path
            or candidate.is_absolute()
            or "\\" in path
            or not candidate.parts
            or candidate.parts[0] == "evidence"
            or any(part in {"", ".", ".."} for part in candidate.parts)
            or candidate.as_posix() != path
        ):
            raise ValueError("approval manifest has an invalid evidence member path")
    return retained


def _cutover_record_digest(ledger: dict[str, Any], record_bytes: dict[str, bytes], expected: str) -> None:
    criteria = ledger.get("criteria")
    assert isinstance(criteria, list)
    matches = [item for item in criteria if isinstance(item, dict) and item.get("id") == "cutover-approval"]
    if len(matches) != 1 or not isinstance(matches[0].get("record"), dict):
        raise ValueError("approval input evidence ledger has no cutover approval record")
    record_path = matches[0]["record"].get("path")
    if not isinstance(record_path, str) or record_path not in record_bytes:
        raise ValueError("approval input evidence ledger has no retained cutover approval record")
    record = _load_json_bytes(record_bytes[record_path], "manual release evidence record")
    proof = record.get("proof")
    payload = proof.get("payload") if isinstance(proof, dict) else None
    if not isinstance(payload, dict) or payload.get("cutover_record_sha256") != expected:
        raise ValueError("approval input cutover record is not bound by its evidence ledger")


def _verify_open_root(root_fd: int, *, controller_root: Path, expected: ExpectedApprovalIdentity) -> dict[str, Any]:
    """Verify every retained member below one pinned approval-input root."""
    manifest = _load_json_bytes(_safe_bytes(root_fd, "approval-manifest.json"), "approval-manifest.json")
    schema = _load_repo_json(controller_root / _SCHEMA)
    try:
        error = next(Draft202012Validator(schema).iter_errors(manifest), None)
    except RecursionError:
        raise ValueError("approval manifest schema validation exceeds nesting limit") from None
    if error is not None:
        location = ".".join(str(part) for part in error.absolute_schema_path) or "<root>"
        raise ValueError(f"approval manifest schema violation at {location}: {error.validator}")
    entries = manifest["files"]
    paths = [entry["path"] for entry in entries]
    if len(paths) != len(set(paths)) or not set(paths) >= _REQUIRED_MEMBERS:
        raise ValueError("approval manifest must list every required member exactly once")
    if any(path not in _REQUIRED_MEMBERS and not path.startswith("evidence/") for path in paths):
        raise ValueError("approval manifest lists an unknown member")
    members = {name: _safe_bytes(root_fd, name) for name in sorted(_REQUIRED_MEMBERS)}
    for entry in entries:
        if entry["path"] in members and hashlib.sha256(members[entry["path"]]).hexdigest() != entry["sha256"]:
            raise ValueError("approval input digest does not match")
    expected_files = {"approval-manifest.json", *paths}
    expected_files.update(
        f"public-candidate/bundle/{name}"
        for name, _digest in release_bundle._parse_checksums(members["public-candidate/bundle/SHA256SUMS"])
    )
    _assert_exact_tree(root_fd, expected_files)
    private = _load_json_bytes(members["private-candidate-report.json"], "private-candidate-report.json")
    public = _load_json_bytes(members["public-candidate/report.json"], "public-candidate/report.json")
    private_export = private.get("export_manifest")
    public_export = public.get("export_manifest")
    if not isinstance(private_export, dict) or not isinstance(public_export, dict):
        raise ValueError("approval input candidate reports have no export manifest")
    candidate = manifest["candidate"]
    manifest_artifacts = _manifest_artifacts(manifest)
    if (
        candidate.get("repository") != expected.repository
        or candidate.get("private_source_sha") != expected.private_source_sha
        or private_export.get("source_tree") != expected.private_source_tree
        or candidate.get("exported_tree") != expected.exported_tree
        or candidate.get("planned_tag") != expected.planned_tag
        or manifest["public"].get("initial_commit") != expected.public_commit
        or manifest["public"].get("initial_tree") != expected.public_tree
        or manifest_artifacts != tuple(sorted(expected.artifacts))
    ):
        raise ValueError("approval input does not match independently selected candidate identities")
    policy_path = controller_root / _PUBLIC_TREE_POLICY
    try:
        policy_bytes = read_root_bytes(policy_path.parent, policy_path.name)
    except OSError as error:
        raise ValueError(f"approval input policy is unavailable: {policy_path.name}") from error
    policy_sha256 = hashlib.sha256(policy_bytes).hexdigest()
    if policy_sha256 != expected.public_tree_policy_sha256:
        raise ValueError("approval controller public tree policy does not match independently selected digest")
    policy = _load_json_bytes(policy_bytes, "approval repository policy")
    if private_export.get("policy_sha256") != policy_sha256 or public_export.get("policy_sha256") != policy_sha256:
        raise ValueError("approval input candidate policy digests are not bound to selected controller policy bytes")
    expected_repository = policy.get("expected_repository")
    planned_tag = policy.get("planned_tag")
    if (
        candidate.get("repository") != expected_repository
        or candidate.get("planned_tag") != planned_tag
        or private_export.get("expected_repository") != expected_repository
        or private_export.get("planned_tag") != planned_tag
        or public_export.get("expected_repository") != expected_repository
        or public_export.get("planned_tag") != planned_tag
    ):
        raise ValueError("approval input candidate repository and planned tag are not policy-bound")
    if (
        private.get("status") != "pass"
        or public.get("status") != "pass"
        or private_export.get("source_commit") != candidate["private_source_sha"]
        or private_export.get("exported_tree") != candidate["exported_tree"]
        or public_export.get("exported_tree") != candidate["exported_tree"]
        or public_export.get("source_commit") != manifest["public"]["initial_commit"]
        or manifest["public"]["initial_tree"] != candidate["exported_tree"]
    ):
        raise ValueError("approval input candidate identities are not bound to one cutover")
    if manifest_artifacts != _artifact_identities(private) or manifest_artifacts != _artifact_identities(public):
        raise ValueError("approval manifest artifacts do not match both candidate reports")
    ledger = _load_json_bytes(members["evidence/ledger.json"], "evidence/ledger.json")
    record_paths = manual_record_paths(
        members["evidence/ledger.json"], expected_criterion_ids=release_manual_evidence.REQUIRED_EVIDENCE_IDS
    )
    if not {f"evidence/{name}" for name in record_paths} <= set(paths):
        raise ValueError("approval manifest does not retain every evidence record")
    budget = AcquisitionBudget()
    for entry in entries:
        name = entry["path"]
        if name not in members:
            members[name] = budget.read(root_fd, name, maximum_bytes=_MAX_APPROVAL_MEMBER_BYTES)
            if hashlib.sha256(members[name]).hexdigest() != entry["sha256"]:
                raise ValueError("approval input digest does not match")
    record_bytes = _record_bytes(ledger, members)
    bundle_fd = _open_member_directory(root_fd, "public-candidate/bundle")
    try:
        artifacts = verified_bundle_artifacts(
            bundle_fd,
            candidate_report_bytes=members["public-candidate/report.json"],
            expected_checksums=members["public-candidate/bundle/SHA256SUMS"],
        )
    finally:
        os.close(bundle_fd)
    criteria, _ledger_digest = release_manual_evidence.validate_bytes(
        controller_root,
        evidence_bytes=members["evidence/ledger.json"],
        candidate_report_bytes=members["public-candidate/report.json"],
        record_bytes=record_bytes,
        artifact_bytes=artifacts,
        private_candidate_report_bytes=members["private-candidate-report.json"],
        documentation_contract_sha256=expected.documentation_contract_sha256,
    )
    if any(criterion.status != "pass" for criterion in criteria):
        raise ValueError("release approval input has pending or nonpassing rehearsal criteria")
    cutover_bytes = members["cutover-record.json"]
    cutover_record.validate(
        _load_json_bytes(cutover_bytes, "cutover-record.json"), private, controller_root=controller_root
    )
    _cutover_record_digest(ledger, record_bytes, hashlib.sha256(cutover_bytes).hexdigest())
    return manifest


def verify(root: Path, *, controller_root: Path, expected: ExpectedApprovalIdentity) -> dict[str, Any]:
    """Validate retained members; the returned manifest is not release authority."""
    root_fd = _open_real_directory(root)
    try:
        return _verify_open_root(root_fd, controller_root=controller_root, expected=expected)
    finally:
        os.close(root_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--controller-root", type=Path, required=True)
    parser.add_argument("--expected-selection", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        verify(args.input, controller_root=args.controller_root, expected=_expected_selection(args.expected_selection))
    except ValueError as error:
        print(f"Release approval input: ERROR: {error}", file=sys.stderr)
        return 2
    print(
        "Release approval input: PENDING: structural validation completed; "
        "independent controller and release authority are not authenticated",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
