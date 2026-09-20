"""Materialize and verify the closed local release bundle for one candidate."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from argparse import ArgumentParser
from dataclasses import dataclass
from pathlib import Path

_BUNDLE_DIRECTORY = "bundle"
_CHECKSUMS_NAME = "SHA256SUMS"
_PROVENANCE_NAME = "bundle-provenance.json"
_SBOM_NAME = "locked-graph.cdx.json"
_RUNTIME_REQUIREMENTS_NAME = "runtime-requirements.txt"
_RELEASE_BUILD_REQUIREMENTS_NAME = "release-build-requirements.txt"
_RUNTIME_WHEELHOUSE_NAME = "runtime-wheelhouse.zip"
_MAX_FILE_BYTES = 5 * 1024 * 1024
_MAX_WHEELHOUSE_BYTES = 100 * 1024 * 1024
_DIGEST_LENGTH = 64
_BUNDLE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class BundleReport:
    """Successful verification evidence for one closed local release bundle."""

    source_commit: str
    files: tuple[tuple[str, str], ...]

    @property
    def ok(self) -> bool:
        """Return whether this report represents a successful verification."""
        return True

    def to_dict(self) -> dict[str, object]:
        """Render stable machine-readable bundle verification evidence."""
        return {
            "schema_version": _BUNDLE_SCHEMA_VERSION,
            "status": "pass",
            "source_commit": self.source_commit,
            "files": [{"name": name, "sha256": digest} for name, digest in self.files],
        }


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a release-bundle JSON object without ambiguous duplicate keys."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_regular_file(descriptor: int, subject: str, *, maximum_bytes: int = _MAX_FILE_BYTES) -> bytes:
    """Read one already-open bounded regular file descriptor."""
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"bundle input must be a regular file: {subject}")
    if metadata.st_size > maximum_bytes:
        raise ValueError(f"bundle input exceeds size limit: {subject}")
    with os.fdopen(descriptor, "rb", closefd=False) as stream:
        data = stream.read(maximum_bytes + 1)
    if len(data) > maximum_bytes:
        raise ValueError(f"bundle input exceeds size limit: {subject}")
    return data


def _file_flags() -> int:
    """Return fail-closed flags shared by direct and descriptor-relative reads."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    return flags


def _safe_bytes(path: Path) -> bytes:
    """Read one bounded regular file without accepting a symlink."""
    try:
        descriptor = os.open(path, _file_flags())
    except OSError as error:
        raise ValueError(f"bundle input is unavailable: {path.name}") from error
    try:
        return _read_regular_file(descriptor, path.name)
    finally:
        os.close(descriptor)


def _safe_bytes_at(directory_fd: int, name: str, *, maximum_bytes: int = _MAX_FILE_BYTES) -> bytes:
    """Read one bounded regular directory member through an open root descriptor."""
    try:
        descriptor = os.open(name, _file_flags(), dir_fd=directory_fd)
    except OSError as error:
        raise ValueError(f"bundle input is unavailable: {name}") from error
    try:
        return _read_regular_file(descriptor, name, maximum_bytes=maximum_bytes)
    finally:
        os.close(descriptor)


def _json_object(data: bytes, subject: str) -> dict[str, object]:
    """Parse one bounded JSON object with duplicate-key rejection."""
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid {subject}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{subject} must be a JSON object")
    return value


def _string(value: object, subject: str) -> str:
    """Require one non-empty string."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{subject} must be a non-empty string")
    return value


def _digest_string(value: object, subject: str) -> str:
    """Require a lowercase SHA-256 digest."""
    digest = _string(value, subject)
    if len(digest) != _DIGEST_LENGTH or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"{subject} must be a lowercase SHA-256 digest")
    return digest


def _basename(value: object, subject: str) -> str:
    """Require a plain safe filename."""
    name = _string(value, subject)
    if Path(name).name != name or name in {".", ".."} or any(character in name for character in "\0\r\n"):
        raise ValueError(f"{subject} must be a plain filename")
    return name


def _object(value: object, subject: str) -> dict[str, object]:
    """Require a JSON object."""
    if not isinstance(value, dict):
        raise ValueError(f"{subject} must be an object")
    return value


def _validate_scan(
    scan: dict[str, object], manifest: dict[str, object], artifacts: tuple[tuple[str, str], ...]
) -> None:
    """Require scan evidence to describe this exact candidate and its artifacts."""
    required = {
        "schema_version",
        "source_commit",
        "source_tree",
        "exported_tree",
        "expected_repository",
        "planned_tag",
        "export_policy_oid",
        "export_policy_sha256",
        "scan_policy_oid",
        "scan_policy_sha256",
        "identity_policy_oid",
        "identity_policy_sha256",
        "scanned_entries",
        "scanned_artifact_entries",
        "scanned_text_entries",
        "approved_binary_entries",
        "classified_matches",
        "gitleaks_version",
        "gitleaks_findings",
        "artifacts",
        "findings",
        "status",
        "artifact_coverage",
    }
    if set(scan) != required or scan.get("schema_version") != 1 or scan.get("status") != "pass":
        raise ValueError("candidate scan evidence has an unsupported schema or status")
    bindings = {
        "source_commit": "source_commit",
        "source_tree": "source_tree",
        "exported_tree": "exported_tree",
        "expected_repository": "expected_repository",
        "planned_tag": "planned_tag",
        "export_policy_oid": "policy_oid",
        "export_policy_sha256": "policy_sha256",
    }
    if any(scan[scan_key] != manifest[manifest_key] for scan_key, manifest_key in bindings.items()):
        raise ValueError("candidate scan evidence does not bind the verified export")
    _oid(scan["scan_policy_oid"], "candidate scan policy object ID")
    _digest_string(scan["scan_policy_sha256"], "candidate scan policy digest")
    _oid(scan["identity_policy_oid"], "candidate identity policy object ID")
    _digest_string(scan["identity_policy_sha256"], "candidate identity policy digest")
    if scan["gitleaks_version"] != "8.30.1" or scan["gitleaks_findings"] != 0:
        raise ValueError("candidate scan evidence has unverified secret-scanner coverage")
    if scan.get("artifact_coverage") != "pass" or scan.get("findings") != []:
        raise ValueError("candidate scan evidence did not pass")
    numeric = {
        "scanned_entries",
        "scanned_artifact_entries",
        "scanned_text_entries",
        "approved_binary_entries",
        "classified_matches",
        "gitleaks_findings",
    }
    for key in numeric:
        value = scan[key]
        if type(value) is not int or value < 0:
            raise ValueError("candidate scan evidence counts are invalid")
    scanned_artifacts = scan.get("artifacts")
    if not isinstance(scanned_artifacts, list) or len(scanned_artifacts) != 2:
        raise ValueError("candidate scan evidence must contain exactly two artifacts")
    observed: dict[tuple[str, str], str] = {}
    for artifact in scanned_artifacts:
        entry = _object(artifact, "candidate scanned artifact")
        if set(entry) != {"name", "kind", "sha256", "member_count", "total_uncompressed_bytes"}:
            raise ValueError("candidate scanned artifact has an unsupported schema")
        kind = _string(entry["kind"], "candidate scanned artifact kind")
        name = _basename(entry["name"], "candidate scanned artifact name")
        if kind not in {"wheel", "sdist"} or (kind, name) in observed:
            raise ValueError("candidate scanned artifacts are ambiguous")
        if (
            type(entry["member_count"]) is not int
            or entry["member_count"] < 0
            or type(entry["total_uncompressed_bytes"]) is not int
            or entry["total_uncompressed_bytes"] < 0
        ):
            raise ValueError("candidate scanned artifact counts are invalid")
        observed[(kind, name)] = _digest_string(entry["sha256"], "candidate scanned artifact digest")
    expected = {("wheel" if name.endswith(".whl") else "sdist", name): digest for name, digest in artifacts}
    if observed != expected:
        raise ValueError("candidate scan artifacts do not match validation evidence")


def _candidate_inputs(report_bytes: bytes) -> tuple[dict[str, object], tuple[tuple[str, str], ...], str, str, str, str]:
    """Return exact build inputs from a passing retained candidate report."""
    try:
        report = json.loads(report_bytes.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid candidate report") from error
    report = _object(report, "candidate report")
    required = {
        "schema_version",
        "status",
        "expected_repository",
        "package",
        "planned_tag",
        "export_manifest",
        "artifact_validation",
        "license_evidence",
        "runtime_requirements",
        "release_build_requirements",
        "runtime_wheelhouse",
        "scan",
    }
    if set(report) != required or report.get("schema_version") != 6 or report.get("status") != "pass":
        raise ValueError("candidate report has an unsupported schema or status")
    manifest = _object(report["export_manifest"], "candidate export manifest")
    artifact_validation = _object(report["artifact_validation"], "candidate artifact validation")
    license_evidence = _object(report["license_evidence"], "candidate license evidence")
    runtime_requirements = _object(report["runtime_requirements"], "candidate runtime requirements")
    build_requirements = _object(report["release_build_requirements"], "candidate release build requirements")
    runtime_wheelhouse = _object(report["runtime_wheelhouse"], "candidate runtime wheelhouse")
    scan = _object(report["scan"], "candidate scan")
    _string(report["package"], "candidate package")
    source_commit = _string(manifest.get("source_commit"), "candidate source commit")
    if report.get("expected_repository") != manifest.get("expected_repository") or report.get(
        "planned_tag"
    ) != manifest.get("planned_tag"):
        raise ValueError("candidate public identity does not bind the verified export")
    if set(artifact_validation) != {"schema_version", "status", "source_revision", "artifacts"}:
        raise ValueError("candidate artifact validation has an unsupported schema")
    if (
        artifact_validation.get("schema_version") != 1
        or artifact_validation.get("status") != "pass"
        or artifact_validation.get("source_revision") != source_commit
    ):
        raise ValueError("candidate artifact validation does not bind the source commit")
    required_license_evidence = {
        "schema_version",
        "status",
        "scope",
        "revision",
        "export_policy_sha256",
        "sbom_sha256",
        "observed_packages",
        "packages",
        "findings",
    }
    if set(license_evidence) != required_license_evidence:
        raise ValueError("candidate license evidence has an unsupported schema")
    if (
        license_evidence.get("schema_version") != 1
        or license_evidence.get("status") != "pass"
        or license_evidence.get("scope") != "runtime-all-extras"
        or license_evidence.get("revision") != source_commit
        or license_evidence.get("export_policy_sha256") != manifest.get("policy_sha256")
    ):
        raise ValueError("candidate license evidence does not bind the source commit")
    packages = license_evidence.get("packages")
    observed_packages = license_evidence.get("observed_packages")
    if (
        type(observed_packages) is not int
        or observed_packages < 0
        or not isinstance(packages, list)
        or observed_packages != len(packages)
        or license_evidence.get("findings") != []
    ):
        raise ValueError("candidate license evidence does not pass its complete contract")
    package_urls: set[str] = set()
    for package in packages:
        observation = _object(package, "candidate license package")
        if set(observation) != {"package_url", "license_expression"}:
            raise ValueError("candidate license package has an unsupported schema")
        package_url = _string(observation["package_url"], "candidate license package URL")
        if package_url in package_urls:
            raise ValueError("candidate license package inventory contains duplicates")
        _string(observation["license_expression"], "candidate license expression")
        package_urls.add(package_url)
    artifacts = artifact_validation.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("candidate artifacts must be a list")
    entries: list[tuple[str, str]] = []
    kinds: set[str] = set()
    names: set[str] = set()
    for artifact in artifacts:
        item = _object(artifact, "candidate artifact")
        if set(item) != {"name", "kind", "sha256", "criteria", "status"}:
            raise ValueError("candidate artifact has an unsupported schema")
        criteria = item.get("criteria")
        if not isinstance(criteria, list) or not criteria:
            raise ValueError("candidate artifact criteria are invalid")
        for criterion in criteria:
            result = _object(criterion, "candidate artifact criterion")
            if (
                set(result) != {"criterion_id", "status", "diagnostics"}
                or not isinstance(result.get("criterion_id"), str)
                or not result["criterion_id"]
                or result.get("status") != "pass"
                or not isinstance(result.get("diagnostics"), list)
                or result["diagnostics"]
            ):
                raise ValueError("candidate artifact criteria did not pass")
        if item.get("status") != "pass":
            raise ValueError("candidate artifact did not pass validation")
        kind = _string(item.get("kind"), "candidate artifact kind")
        if kind not in {"wheel", "sdist"} or kind in kinds:
            raise ValueError("candidate must contain exactly one wheel and one source distribution")
        kinds.add(kind)
        name = _basename(item.get("name"), "candidate artifact name")
        if name in {_CHECKSUMS_NAME, _PROVENANCE_NAME, _SBOM_NAME} or name in names:
            raise ValueError("candidate artifact name is reserved or duplicated")
        if (kind == "wheel" and not name.endswith(".whl")) or (kind == "sdist" and not name.endswith(".tar.gz")):
            raise ValueError("candidate artifact name does not match its kind")
        names.add(name)
        entries.append(
            (
                name,
                _digest_string(item.get("sha256"), "candidate artifact digest"),
            )
        )
    if kinds != {"wheel", "sdist"}:
        raise ValueError("candidate must contain exactly one wheel and one source distribution")
    _validate_scan(scan, manifest, tuple(sorted(entries)))
    sbom_digest = _digest_string(license_evidence.get("sbom_sha256"), "candidate SBOM digest")
    if (
        set(runtime_requirements) != {"name", "sha256"}
        or runtime_requirements.get("name") != _RUNTIME_REQUIREMENTS_NAME
    ):
        raise ValueError("candidate runtime requirements have an unsupported schema")
    requirements_digest = _digest_string(runtime_requirements.get("sha256"), "candidate runtime requirements digest")
    if (
        set(build_requirements) != {"name", "sha256"}
        or build_requirements.get("name") != _RELEASE_BUILD_REQUIREMENTS_NAME
    ):
        raise ValueError("candidate release build requirements have an unsupported schema")
    build_requirements_digest = _digest_string(
        build_requirements.get("sha256"), "candidate release build requirements digest"
    )
    if set(runtime_wheelhouse) != {"name", "sha256"} or runtime_wheelhouse.get("name") != _RUNTIME_WHEELHOUSE_NAME:
        raise ValueError("candidate runtime wheelhouse has an unsupported schema")
    wheelhouse_digest = _digest_string(runtime_wheelhouse.get("sha256"), "candidate runtime wheelhouse digest")
    return (
        report,
        tuple(sorted(entries)),
        sbom_digest,
        requirements_digest,
        build_requirements_digest,
        wheelhouse_digest,
    )


def _write_bytes(path: Path, data: bytes) -> None:
    """Write one staged bundle file with restricted permissions."""
    path.write_bytes(data)
    path.chmod(0o600)


def _provenance(
    report: dict[str, object],
    report_digest: str,
    files: tuple[tuple[str, str], ...],
    sbom_digest: str,
    requirements_digest: str,
    build_requirements_digest: str,
    wheelhouse_digest: str,
) -> bytes:
    """Render canonical non-hosted provenance for the retained candidate."""
    manifest = _object(report["export_manifest"], "candidate export manifest")
    artifact_entries = [
        {"name": name, "kind": "wheel" if name.endswith(".whl") else "sdist", "sha256": digest}
        for name, digest in files
    ]
    payload = {
        "schema_version": _BUNDLE_SCHEMA_VERSION,
        "candidate_report_sha256": report_digest,
        "source_commit": manifest["source_commit"],
        "source_tree": manifest["source_tree"],
        "exported_tree": manifest["exported_tree"],
        "export_policy_sha256": manifest["policy_sha256"],
        "expected_repository": report["expected_repository"],
        "planned_tag": report["planned_tag"],
        "artifacts": artifact_entries,
        "runtime_sbom": {"name": _SBOM_NAME, "sha256": sbom_digest, "scope": "runtime-all-extras"},
        "runtime_requirements": {"name": _RUNTIME_REQUIREMENTS_NAME, "sha256": requirements_digest},
        "release_build_requirements": {
            "name": _RELEASE_BUILD_REQUIREMENTS_NAME,
            "sha256": build_requirements_digest,
        },
        "runtime_wheelhouse": {"name": _RUNTIME_WHEELHOUSE_NAME, "sha256": wheelhouse_digest},
        "checksum_manifest": {"name": _CHECKSUMS_NAME, "format_version": 1},
    }
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _checksum_manifest(files: tuple[tuple[str, str], ...]) -> bytes:
    """Render the canonical digest list for all non-manifest bundle files."""
    return "".join(f"{digest}  {name}\n" for name, digest in sorted(files)).encode("utf-8")


def _open_directory(path: Path) -> int:
    """Open one real directory without accepting a symlink."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError("bundle directory is unavailable") from error
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError("bundle path must be a directory")
    return descriptor


def _open_directory_at(parent_fd: int, name: str) -> int:
    """Open one real child directory from an already-open parent directory."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as error:
        raise ValueError(f"bundle directory is unavailable: {name}") from error
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError(f"bundle path must be a directory: {name}")
    return descriptor


def materialize(candidate: Path) -> BundleReport:
    """Create a closed verified release bundle from one successful candidate."""
    if candidate.is_symlink() or not candidate.is_dir():
        raise ValueError("candidate must be a directory")
    bundle = candidate / _BUNDLE_DIRECTORY
    if bundle.exists():
        raise ValueError("candidate bundle already exists")
    candidate_fd = _open_directory(candidate)
    try:
        report_bytes = _safe_bytes_at(candidate_fd, "report.json")
        report, artifacts, sbom_digest, requirements_digest, build_requirements_digest, wheelhouse_digest = (
            _candidate_inputs(report_bytes)
        )
        dist_fd = _open_directory_at(candidate_fd, "dist")
        try:
            source_files = (
                *((_safe_bytes_at(dist_fd, name), name, digest) for name, digest in artifacts),
                (_safe_bytes_at(candidate_fd, _SBOM_NAME), _SBOM_NAME, sbom_digest),
                (
                    _safe_bytes_at(candidate_fd, _RUNTIME_REQUIREMENTS_NAME),
                    _RUNTIME_REQUIREMENTS_NAME,
                    requirements_digest,
                ),
                (
                    _safe_bytes_at(candidate_fd, _RELEASE_BUILD_REQUIREMENTS_NAME),
                    _RELEASE_BUILD_REQUIREMENTS_NAME,
                    build_requirements_digest,
                ),
                (
                    _safe_bytes_at(candidate_fd, _RUNTIME_WHEELHOUSE_NAME, maximum_bytes=_MAX_WHEELHOUSE_BYTES),
                    _RUNTIME_WHEELHOUSE_NAME,
                    wheelhouse_digest,
                ),
            )
        finally:
            os.close(dist_fd)
    finally:
        os.close(candidate_fd)
    candidate_report_digest = _digest(report_bytes)
    for data, name, expected_digest in source_files:
        if _digest(data) != expected_digest:
            if name == _SBOM_NAME:
                subject = "SBOM"
            elif name == _RUNTIME_REQUIREMENTS_NAME:
                subject = "runtime requirements"
            elif name == _RELEASE_BUILD_REQUIREMENTS_NAME:
                subject = "release build requirements"
            else:
                subject = "artifact"
            raise ValueError(f"candidate {subject} digest does not match: {name}")
    staging = Path(tempfile.mkdtemp(prefix=f".{_BUNDLE_DIRECTORY}-", dir=candidate))
    try:
        bundle_files = tuple((name, digest) for _, name, digest in source_files)
        provenance = _provenance(
            report,
            candidate_report_digest,
            artifacts,
            sbom_digest,
            requirements_digest,
            build_requirements_digest,
            wheelhouse_digest,
        )
        all_files = tuple(sorted((*bundle_files, (_PROVENANCE_NAME, _digest(provenance)))))
        for data, name, _ in source_files:
            _write_bytes(staging / name, data)
        _write_bytes(staging / _PROVENANCE_NAME, provenance)
        _write_bytes(staging / _CHECKSUMS_NAME, _checksum_manifest(all_files))
        verification = verify(staging, candidate_report=report_bytes)
        staging.replace(bundle)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return verification


def _parse_checksums(data: bytes) -> tuple[tuple[str, str], ...]:
    """Require the canonical closed checksum manifest grammar."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("bundle checksum manifest is not UTF-8") from error
    if not text.endswith("\n") or "\r" in text:
        raise ValueError("bundle checksum manifest is not canonical")
    lines = text.removesuffix("\n").split("\n")
    entries: list[tuple[str, str]] = []
    for line in lines:
        if len(line) < _DIGEST_LENGTH + 3 or line[_DIGEST_LENGTH : _DIGEST_LENGTH + 2] != "  ":
            raise ValueError("bundle checksum manifest has an invalid line")
        entries.append(
            (
                _basename(line[_DIGEST_LENGTH + 2 :], "bundle checksum filename"),
                _digest_string(line[:_DIGEST_LENGTH], "bundle checksum digest"),
            )
        )
    if not entries or entries != sorted(entries) or len({name for name, _ in entries}) != len(entries):
        raise ValueError("bundle checksum manifest is not canonical")
    return tuple(entries)


def _oid(value: object, subject: str) -> str:
    """Require a lowercase Git object ID."""
    oid = _string(value, subject)
    if len(oid) != 40 or any(character not in "0123456789abcdef" for character in oid):
        raise ValueError(f"{subject} must be a lowercase Git object ID")
    return oid


def _verify_provenance(provenance: dict[str, object], checksums: tuple[tuple[str, str], ...]) -> str:
    """Require provenance to bind every checksum-listed release asset."""
    required = {
        "schema_version",
        "candidate_report_sha256",
        "source_commit",
        "source_tree",
        "exported_tree",
        "export_policy_sha256",
        "expected_repository",
        "planned_tag",
        "artifacts",
        "runtime_sbom",
        "runtime_requirements",
        "release_build_requirements",
        "runtime_wheelhouse",
        "checksum_manifest",
    }
    if (
        set(provenance) != required
        or type(provenance.get("schema_version")) is not int
        or provenance.get("schema_version") != _BUNDLE_SCHEMA_VERSION
    ):
        raise ValueError("bundle provenance has an unsupported schema")
    _digest_string(provenance["candidate_report_sha256"], "bundle candidate report digest")
    source_commit = _oid(provenance["source_commit"], "bundle source commit")
    _oid(provenance["source_tree"], "bundle source tree")
    _oid(provenance["exported_tree"], "bundle exported tree")
    _digest_string(provenance["export_policy_sha256"], "bundle export policy digest")
    repository = _string(provenance["expected_repository"], "bundle expected repository")
    if re.fullmatch(r"[a-z0-9-]+/fieldkit-cli", repository) is None:
        raise ValueError("bundle expected repository is invalid")
    if provenance["planned_tag"] != "v1.0.0":
        raise ValueError("bundle planned tag is invalid")
    artifacts = provenance["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) != 2:
        raise ValueError("bundle provenance artifacts must contain one wheel and one source distribution")
    expected: dict[str, str] = {}
    kinds: set[str] = set()
    for artifact in artifacts:
        entry = _object(artifact, "bundle provenance artifact")
        if set(entry) != {"name", "kind", "sha256"}:
            raise ValueError("bundle provenance artifact has an unsupported schema")
        name = _basename(entry["name"], "bundle provenance artifact name")
        kind = _string(entry["kind"], "bundle provenance artifact kind")
        if (
            kind not in {"wheel", "sdist"}
            or kind in kinds
            or name in expected
            or name in {_CHECKSUMS_NAME, _PROVENANCE_NAME, _SBOM_NAME}
            or (kind == "wheel" and not name.endswith(".whl"))
            or (kind == "sdist" and not name.endswith(".tar.gz"))
        ):
            raise ValueError("bundle provenance artifacts are ambiguous")
        kinds.add(kind)
        expected[name] = _digest_string(entry["sha256"], "bundle provenance artifact digest")
    runtime_sbom = _object(provenance["runtime_sbom"], "bundle runtime SBOM")
    if (
        set(runtime_sbom) != {"name", "sha256", "scope"}
        or runtime_sbom.get("name") != _SBOM_NAME
        or runtime_sbom.get("scope") != "runtime-all-extras"
    ):
        raise ValueError("bundle runtime SBOM has an unsupported schema")
    expected[_SBOM_NAME] = _digest_string(runtime_sbom["sha256"], "bundle runtime SBOM digest")
    runtime_requirements = _object(provenance["runtime_requirements"], "bundle runtime requirements")
    if (
        set(runtime_requirements) != {"name", "sha256"}
        or runtime_requirements.get("name") != _RUNTIME_REQUIREMENTS_NAME
    ):
        raise ValueError("bundle runtime requirements have an unsupported schema")
    expected[_RUNTIME_REQUIREMENTS_NAME] = _digest_string(
        runtime_requirements["sha256"], "bundle runtime requirements digest"
    )
    build_requirements = _object(provenance["release_build_requirements"], "bundle release build requirements")
    if (
        set(build_requirements) != {"name", "sha256"}
        or build_requirements.get("name") != _RELEASE_BUILD_REQUIREMENTS_NAME
    ):
        raise ValueError("bundle release build requirements have an unsupported schema")
    expected[_RELEASE_BUILD_REQUIREMENTS_NAME] = _digest_string(
        build_requirements["sha256"], "bundle release build requirements digest"
    )
    runtime_wheelhouse = _object(provenance["runtime_wheelhouse"], "bundle runtime wheelhouse")
    if set(runtime_wheelhouse) != {"name", "sha256"} or runtime_wheelhouse.get("name") != _RUNTIME_WHEELHOUSE_NAME:
        raise ValueError("bundle runtime wheelhouse has an unsupported schema")
    expected[_RUNTIME_WHEELHOUSE_NAME] = _digest_string(
        runtime_wheelhouse["sha256"], "bundle runtime wheelhouse digest"
    )
    checksum_manifest = _object(provenance["checksum_manifest"], "bundle checksum manifest")
    if (
        set(checksum_manifest) != {"name", "format_version"}
        or checksum_manifest.get("name") != _CHECKSUMS_NAME
        or type(checksum_manifest.get("format_version")) is not int
        or checksum_manifest.get("format_version") != 1
    ):
        raise ValueError("bundle checksum manifest identity is invalid")
    observed = dict(checksums)
    if expected.keys() | {_PROVENANCE_NAME} != observed.keys():
        raise ValueError("bundle provenance and checksum manifest disagree")
    if any(observed[name] != digest for name, digest in expected.items()):
        raise ValueError("bundle provenance digests do not match checksums")
    return source_commit


def _verify_candidate_binding(provenance: dict[str, object], candidate_report: bytes) -> None:
    """Require bundle provenance to reproduce one independently supplied candidate report."""
    report, artifacts, sbom_digest, requirements_digest, build_requirements_digest, wheelhouse_digest = (
        _candidate_inputs(candidate_report)
    )
    manifest = _object(report["export_manifest"], "candidate export manifest")
    if provenance["candidate_report_sha256"] != _digest(candidate_report):
        raise ValueError("bundle provenance does not bind the expected candidate report")
    bindings = {
        "source_commit": "source_commit",
        "source_tree": "source_tree",
        "exported_tree": "exported_tree",
        "export_policy_sha256": "policy_sha256",
        "expected_repository": "expected_repository",
        "planned_tag": "planned_tag",
    }
    if any(provenance[key] != manifest[report_key] for key, report_key in bindings.items()):
        raise ValueError("bundle provenance does not match the expected candidate report")
    expected_artifacts = [
        {"name": name, "kind": "wheel" if name.endswith(".whl") else "sdist", "sha256": digest}
        for name, digest in artifacts
    ]
    if (
        provenance["artifacts"] != expected_artifacts
        or provenance["runtime_sbom"]
        != {
            "name": _SBOM_NAME,
            "sha256": sbom_digest,
            "scope": "runtime-all-extras",
        }
        or provenance["runtime_requirements"] != {"name": _RUNTIME_REQUIREMENTS_NAME, "sha256": requirements_digest}
        or provenance["release_build_requirements"]
        != {"name": _RELEASE_BUILD_REQUIREMENTS_NAME, "sha256": build_requirements_digest}
        or provenance["runtime_wheelhouse"] != {"name": _RUNTIME_WHEELHOUSE_NAME, "sha256": wheelhouse_digest}
    ):
        raise ValueError("bundle provenance assets do not match the expected candidate report")


def verify(bundle: Path, *, candidate_report: bytes) -> BundleReport:
    """Fail closed unless a local release bundle is complete and digest-bound."""
    if bundle.is_symlink() or not bundle.is_dir():
        raise ValueError("bundle must be a directory")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        directory_fd = os.open(bundle, flags)
    except OSError as error:
        raise ValueError("bundle is unavailable") from error
    try:
        contents_list: list[str] = []
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                if len(contents_list) == 8:
                    raise ValueError("bundle has unexpected or missing files")
                contents_list.append(entry.name)
        contents = tuple(contents_list)
        data = {
            name: _safe_bytes_at(
                directory_fd,
                name,
                maximum_bytes=_MAX_WHEELHOUSE_BYTES if name == _RUNTIME_WHEELHOUSE_NAME else _MAX_FILE_BYTES,
            )
            for name in contents
        }
    finally:
        os.close(directory_fd)
    if _CHECKSUMS_NAME not in data:
        raise ValueError("bundle has unexpected or missing files")
    checksums = _parse_checksums(data[_CHECKSUMS_NAME])
    expected_contents = {name for name, _ in checksums} | {_CHECKSUMS_NAME}
    if set(contents) != expected_contents:
        raise ValueError("bundle has unexpected or missing files")
    for name, expected_digest in checksums:
        if _digest(data[name]) != expected_digest:
            raise ValueError(f"bundle digest does not match: {name}")
    if _PROVENANCE_NAME not in data:
        raise ValueError("bundle has unexpected or missing files")
    provenance = _json_object(data[_PROVENANCE_NAME], "bundle provenance")
    source_commit = _verify_provenance(provenance, checksums)
    _verify_candidate_binding(provenance, candidate_report)
    return BundleReport(source_commit, checksums)


def _parser() -> ArgumentParser:
    """Build the standalone local bundle-verification command parser."""
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--candidate-report", type=Path, required=True)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Verify one bundle and emit stable local verification evidence."""
    try:
        args = _parser().parse_args(argv)
        report = verify(args.bundle, candidate_report=_safe_bytes(args.candidate_report))
        if args.as_json:
            print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        else:
            print(f"Release bundle: PASS ({report.source_commit})")
    except ValueError as error:
        print(f"Release bundle: ERROR: {error}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
