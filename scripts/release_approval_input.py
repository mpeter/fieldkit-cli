"""Verify a retained, digest-bound release approval input before promotion."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

if __package__:
    from scripts import cutover_record, release_bundle, release_check
    from scripts.json_policy import reject_duplicate_json_keys
else:
    import cutover_record
    import release_bundle
    import release_check
    from json_policy import reject_duplicate_json_keys


_SCHEMA = Path("docs/release-readiness/release-approval-input.schema.json")
_REQUIRED_MEMBERS = frozenset(
    {
        "private-candidate-report.json",
        "public-candidate/report.json",
        "public-candidate/bundle/SHA256SUMS",
        "evidence/ledger.json",
        "cutover-record.json",
    }
)


def _load_json_bytes(data: bytes, subject: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid approval input JSON: {subject}") from error
    if not isinstance(value, dict):
        raise ValueError(f"approval input JSON must be an object: {subject}")
    return value


def _safe_member(root: Path, name: object) -> Path:
    if not isinstance(name, str) or not name or Path(name).is_absolute() or "\\" in name:
        raise ValueError("approval input member path is invalid")
    parts = Path(name).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("approval input member escapes its root")
    path = root
    metadata: os.stat_result | None = None
    for part in parts:
        path /= part
        try:
            metadata = path.stat(follow_symlinks=False)
        except OSError as error:
            raise ValueError(f"approval input member is unavailable: {name}") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"approval input member is not a regular file: {name}")
    if metadata is None or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"approval input member is not a regular file: {name}")
    return path


def _safe_bytes(root: Path, name: object) -> bytes:
    """Read one manifest member through a descriptor without following a leaf symlink."""
    path = _safe_member(root, name)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError(f"approval input member is unavailable: {name}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"approval input member is not a regular file: {name}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


def _safe_directory(root: Path, name: object) -> Path:
    """Return one real input subdirectory after rejecting every symlink component."""
    if not isinstance(name, str) or not name or Path(name).is_absolute() or "\\" in name:
        raise ValueError("approval input member path is invalid")
    parts = Path(name).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("approval input member escapes its root")
    path = root
    metadata: os.stat_result | None = None
    for part in parts:
        path /= part
        try:
            metadata = path.stat(follow_symlinks=False)
        except OSError as error:
            raise ValueError(f"approval input directory is unavailable: {name}") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"approval input directory is not a real directory: {name}")
    if metadata is None or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"approval input directory is not a real directory: {name}")
    return path


def _load_repo_json(path: Path) -> dict[str, Any]:
    try:
        return _load_json_bytes(path.read_bytes(), path.name)
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
    paths = {
        record.get("path")
        for item in criteria
        if isinstance(item, dict) and isinstance(record := item.get("record"), dict)
    }
    if not paths or not all(isinstance(path, str) for path in paths):
        raise ValueError("approval input evidence ledger has invalid record paths")
    return frozenset(path for path in paths if isinstance(path, str))


def _cutover_record_digest(ledger: dict[str, Any], root: Path, expected: str) -> None:
    criteria = ledger.get("criteria")
    assert isinstance(criteria, list)
    matches = [item for item in criteria if isinstance(item, dict) and item.get("id") == "cutover-approval"]
    if len(matches) != 1 or not isinstance(matches[0].get("record"), dict):
        raise ValueError("approval input evidence ledger has no cutover approval record")
    record_path = matches[0]["record"].get("path")
    record = _load_json_bytes(_safe_bytes(root, record_path), str(record_path))
    proof = record.get("proof")
    payload = proof.get("payload") if isinstance(proof, dict) else None
    if not isinstance(payload, dict) or payload.get("cutover_record_sha256") != expected:
        raise ValueError("approval input cutover record is not bound by its evidence ledger")


def verify(root: Path, *, repo_root: Path = Path()) -> dict[str, Any]:
    """Verify every retained member and return the validated approval manifest."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError("approval input root must be a real directory")
    root = root.resolve()
    manifest = _load_json_bytes(_safe_bytes(root, "approval-manifest.json"), "approval-manifest.json")
    schema = _load_repo_json(repo_root / _SCHEMA)
    errors = sorted(Draft202012Validator(schema).iter_errors(manifest), key=lambda error: list(error.absolute_path))
    if errors:
        location = ".".join(str(part) for part in errors[0].absolute_path) or "<root>"
        raise ValueError(f"approval manifest schema violation at {location}: {errors[0].message}")
    entries = manifest["files"]
    paths = [entry["path"] for entry in entries]
    if len(paths) != len(set(paths)) or set(paths) < _REQUIRED_MEMBERS:
        raise ValueError("approval manifest must list every required member exactly once")
    members = {entry["path"]: _safe_bytes(root, entry["path"]) for entry in entries}
    for entry in entries:
        if hashlib.sha256(members[entry["path"]]).hexdigest() != entry["sha256"]:
            raise ValueError(f"approval input digest does not match: {entry['path']}")
    private = _load_json_bytes(members["private-candidate-report.json"], "private-candidate-report.json")
    public = _load_json_bytes(members["public-candidate/report.json"], "public-candidate/report.json")
    private_export = private.get("export_manifest")
    public_export = public.get("export_manifest")
    if not isinstance(private_export, dict) or not isinstance(public_export, dict):
        raise ValueError("approval input candidate reports have no export manifest")
    candidate = manifest["candidate"]
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
    if _manifest_artifacts(manifest) != _artifact_identities(public):
        raise ValueError("approval manifest artifacts do not match the public candidate")
    ledger = _load_json_bytes(members["evidence/ledger.json"], "evidence/ledger.json")
    ledger_paths = _ledger_record_paths(ledger)
    if not ledger_paths <= set(members):
        raise ValueError("approval manifest does not retain every evidence record")
    release_check._manual_criteria(
        repo_root, _safe_member(root, "evidence/ledger.json"), _safe_member(root, "public-candidate/report.json")
    )
    release_bundle.verify(
        _safe_directory(root, "public-candidate/bundle"), candidate_report=members["public-candidate/report.json"]
    )
    cutover_bytes = members["cutover-record.json"]
    cutover_record.validate(_load_json_bytes(cutover_bytes, "cutover-record.json"), private, repo_root=repo_root)
    _cutover_record_digest(ledger, root, hashlib.sha256(cutover_bytes).hexdigest())
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path())
    args = parser.parse_args(argv)
    try:
        verify(args.input, repo_root=args.repo_root)
    except ValueError as error:
        print(f"Release approval input: ERROR: {error}", file=sys.stderr)
        return 2
    print("Release approval input: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
