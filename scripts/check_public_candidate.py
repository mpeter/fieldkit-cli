#!/usr/bin/env python3
"""Build and scan a retained release candidate from one verified public export."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tomllib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.machinery import ModuleSpec
from importlib.util import module_from_spec
from pathlib import Path

from fieldkit.util.bounded_process import BoundedProcessError, run_bounded_process_bytes

if __package__ in {None, ""}:
    scripts_spec = ModuleSpec("scripts", loader=None, is_package=True)
    scripts_spec.submodule_search_locations = [str(Path(__file__).resolve().parent)]
    sys.modules["scripts"] = module_from_spec(scripts_spec)

from scripts import (
    check_artifacts,
    check_public_identity,
    export_public_tree,
    public_history_source,
    public_tree_scan,
    release_approval_archive,
    release_bundle,
    release_wheelhouse,
    runtime_license_inventory,
)

_BUILD_TIMEOUT_SECONDS = 120
_LICENSE_SCOPE = "runtime-all-extras"
_OBSERVATION_STDERR_LIMIT_BYTES = 16 * 1024
_OBSERVATION_CLEANUP_TIMEOUT_SECONDS = 3


def _observe_runtime_licenses(python: Path, export_dir: Path, environment: dict[str, str]) -> bytes:
    """Capture a fixed runtime collector with bounded streams and owned group cleanup."""
    try:
        observation = run_bounded_process_bytes(
            [
                str(python),
                "-I",
                "-S",
                str(export_dir / "scripts" / "runtime_license_inventory.py"),
                "--environment-root",
                str(python.parent.parent),
            ],
            cwd=export_dir,
            env=environment,
            timeout=_BUILD_TIMEOUT_SECONDS,
            stdout_limit=runtime_license_inventory.MAX_OBSERVATION_BYTES,
            stderr_limit=_OBSERVATION_STDERR_LIMIT_BYTES,
            cleanup_timeout=_OBSERVATION_CLEANUP_TIMEOUT_SECONDS,
        )
    except BoundedProcessError as exc:
        raise ValueError(f"candidate runtime license observation failed: {exc}") from exc
    if observation.returncode != 0:
        raise ValueError(f"candidate runtime license observation failed with exit status {observation.returncode}")
    return observation.stdout


def _validate_observation_privacy(export_dir: Path, contents: bytes) -> None:
    """Reject unclassified private data before runtime facts enter a public receipt."""
    text = contents.decode("utf-8")
    policy = public_tree_scan._load_policy(
        runtime_license_inventory.read_snapshot(export_dir / public_tree_scan.POLICY_PATH),
        datetime.now(tz=UTC).date(),
    )
    identity_policy = check_public_identity.parse_policy(
        runtime_license_inventory.read_snapshot(export_dir / public_tree_scan.IDENTITY_POLICY_PATH)
    )
    if any(rule.pattern.search(text) for rule in policy.text_rules) or any(
        rule.pattern.search(text) for rule in identity_policy.rules
    ):
        raise ValueError("runtime license observations contain unapproved private or sensitive data")


@dataclass(frozen=True)
class CandidateReport:
    """Evidence from an authoritative public release-candidate build."""

    schema_version: int
    expected_repository: str
    package: str
    planned_tag: str
    export_manifest: export_public_tree.ExportManifest
    artifact_validation: check_artifacts.ValidationReport
    license_evidence: dict[str, object]
    runtime_requirements: dict[str, str]
    release_build_requirements: dict[str, str]
    runtime_wheelhouse: dict[str, str]
    scan: public_tree_scan.ScanReport

    @property
    def ok(self) -> bool:
        """Return whether every candidate-bound check passed."""
        return self.artifact_validation.ok and self.license_evidence["status"] == "pass" and self.scan.ok

    def to_dict(self) -> dict[str, object]:
        """Render stable retained evidence without archive payloads."""
        return {**_common_report(self), "export_manifest": asdict(self.export_manifest)}


@dataclass(frozen=True)
class PublicHistoryCandidateReport:
    """Version 8 source checks, pending successor bundle assembly and approval."""

    schema_version: int
    expected_repository: str
    package: str
    planned_tag: str
    source: public_history_source.PublicHistorySource
    artifact_validation: check_artifacts.ValidationReport
    license_evidence: dict[str, object]
    runtime_requirements: dict[str, str]
    release_build_requirements: dict[str, str]
    runtime_wheelhouse: dict[str, str]
    scan: public_tree_scan.PublicHistoryScanReport
    source_sha256: str

    @property
    def ok(self) -> bool:
        """Return source-check success, not release or sealed-bundle readiness."""
        return self.artifact_validation.ok and self.license_evidence["status"] == "pass" and self.scan.ok

    def to_dict(self) -> dict[str, object]:
        """Keep unassembled successor evidence explicitly pending."""
        return {
            **_common_report(self),
            "status": "pending" if self.ok else "fail",
            "checks_status": "pass" if self.ok else "fail",
            "bundle_status": "not-assembled",
            "source_kind": public_history_source.SOURCE_KIND,
            "source": asdict(self.source),
            "source_sha256": self.source_sha256,
        }


def _common_report(report: CandidateReport | PublicHistoryCandidateReport) -> dict[str, object]:
    return {
        "schema_version": report.schema_version,
        "status": "pass" if report.ok else "fail",
        "expected_repository": report.expected_repository,
        "package": report.package,
        "planned_tag": report.planned_tag,
        "artifact_validation": report.artifact_validation.to_dict(),
        "license_evidence": report.license_evidence,
        "runtime_requirements": report.runtime_requirements,
        "release_build_requirements": report.release_build_requirements,
        "runtime_wheelhouse": report.runtime_wheelhouse,
        "scan": report.scan.to_dict(),
    }


def _validate_expected_identity(
    manifest: export_public_tree.ExportManifest,
    expected_repository: str,
    planned_tag: str,
) -> None:
    if manifest.expected_repository != expected_repository:
        raise ValueError("verified export expected repository does not match the requested candidate")
    if manifest.planned_tag != planned_tag:
        raise ValueError("verified export planned tag does not match the requested candidate")


def _package_identity(export_dir: Path) -> str:
    """Read the distribution name whose metadata the artifact checker validates."""
    try:
        project = tomllib.loads((export_dir / "pyproject.toml").read_text(encoding="utf-8")).get("project")
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"candidate pyproject.toml cannot provide package identity: {error}") from error
    if not isinstance(project, dict):
        raise ValueError("candidate pyproject.toml project metadata must be an object")
    package = project.get("name")
    if not isinstance(package, str) or not package.strip():
        raise ValueError("candidate pyproject.toml project.name must be a non-empty string")
    return package.strip()


def _build_export(export_dir: Path, dist_dir: Path) -> tuple[Path, ...]:
    """Build exactly one wheel and sdist from the verified export snapshot."""
    environment = os.environ.copy()
    environment["SOURCE_DATE_EPOCH"] = "315532800"
    result = subprocess.run(
        ["uv", "build", "--out-dir", str(dist_dir)],
        cwd=export_dir,
        capture_output=True,
        text=True,
        check=False,
        timeout=_BUILD_TIMEOUT_SECONDS,
        env=environment,
    )
    if result.returncode != 0:
        diagnostics = (result.stderr or result.stdout).strip()
        raise ValueError(f"candidate package build failed with exit status {result.returncode}: {diagnostics}")
    artifacts = tuple(sorted((*dist_dir.glob("*.whl"), *dist_dir.glob("*.tar.gz"))))
    if len(artifacts) != 2:
        raise ValueError("candidate build must produce exactly one wheel and one source distribution")
    return artifacts


def _require_reproducible_builds(first: tuple[Path, ...], second: tuple[Path, ...]) -> None:
    """Reject a candidate unless two controlled builds have identical artifact bytes."""
    first_digests = {artifact.name: hashlib.sha256(artifact.read_bytes()).hexdigest() for artifact in first}
    second_digests = {artifact.name: hashlib.sha256(artifact.read_bytes()).hexdigest() for artifact in second}
    if first_digests != second_digests:
        raise ValueError("candidate builds are not byte-for-byte reproducible")


def _environment_python(environment_dir: Path) -> Path:
    """Return the Python executable created by uv on the current platform."""
    candidates = (environment_dir / "bin" / "python", environment_dir / "Scripts" / "python.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ValueError("candidate dependency environment has no Python executable")


def _license_evidence(
    export_dir: Path,
    staging: Path,
    manifest: export_public_tree.ExportManifest | public_history_source.PublicHistorySource,
) -> dict[str, object]:
    """Collect export-bound licenses and retain the environment without unsafe cleanup."""
    environment_dir = staging / "license-environment"
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    sbom_path = staging / "locked-graph.cdx.json"
    sbom = subprocess.run(
        [
            "uv",
            "export",
            "--format",
            "cyclonedx1.5",
            "--preview-features",
            "sbom-export",
            "--locked",
            "--no-dev",
            "--all-extras",
            "--output-file",
            str(sbom_path),
        ],
        cwd=export_dir,
        capture_output=True,
        text=True,
        check=False,
        timeout=_BUILD_TIMEOUT_SECONDS,
    )
    if sbom.returncode != 0:
        raise ValueError(f"candidate locked SBOM export failed with exit status {sbom.returncode}")
    all_extras_requirements_path = staging / runtime_license_inventory.PLATFORM_REQUIREMENTS_NAME
    requirements = subprocess.run(
        [
            "uv",
            "export",
            "--format",
            "requirements-txt",
            "--locked",
            "--no-dev",
            "--all-extras",
            "--no-emit-project",
            "--output-file",
            str(all_extras_requirements_path),
        ],
        cwd=export_dir,
        capture_output=True,
        text=True,
        check=False,
        timeout=_BUILD_TIMEOUT_SECONDS,
    )
    if requirements.returncode != 0:
        raise ValueError(f"candidate platform requirements export failed with exit status {requirements.returncode}")
    runtime_requirements_path = staging / "runtime-requirements.txt"
    runtime_requirements = subprocess.run(
        [
            "uv",
            "export",
            "--format",
            "requirements-txt",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--output-file",
            str(runtime_requirements_path),
        ],
        cwd=export_dir,
        capture_output=True,
        text=True,
        check=False,
        timeout=_BUILD_TIMEOUT_SECONDS,
    )
    if runtime_requirements.returncode != 0:
        raise ValueError(
            f"candidate runtime requirements export failed with exit status {runtime_requirements.returncode}"
        )
    build_requirements_path = staging / "release-build-requirements.txt"
    build_requirements = subprocess.run(
        [
            "uv",
            "export",
            "--format",
            "requirements-txt",
            "--locked",
            "--only-group",
            "release-build",
            "--no-emit-project",
            "--output-file",
            str(build_requirements_path),
        ],
        cwd=export_dir,
        capture_output=True,
        text=True,
        check=False,
        timeout=_BUILD_TIMEOUT_SECONDS,
    )
    if build_requirements.returncode != 0:
        raise ValueError(
            f"candidate locked build requirements export failed with exit status {build_requirements.returncode}"
        )
    if environment_dir.exists() or environment_dir.is_symlink():
        raise ValueError("candidate dependency environment must not already exist")
    creation_environment = environment.copy()
    creation_environment.pop("UV_VENV_CLEAR", None)
    creation_environment.pop("UV_VENV_ALLOW_EXISTING", None)
    create = subprocess.run(
        ["uv", "venv", str(environment_dir)],
        cwd=export_dir,
        env=creation_environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=_BUILD_TIMEOUT_SECONDS,
    )
    if create.returncode != 0:
        raise ValueError(f"candidate dependency environment failed with exit status {create.returncode}")
    python = _environment_python(environment_dir)
    environment["VIRTUAL_ENV"] = str(environment_dir)
    environment["PATH"] = f"{python.parent}{os.pathsep}{environment.get('PATH', '')}"
    sync = subprocess.run(
        [
            "uv",
            "sync",
            "--locked",
            "--no-dev",
            "--all-extras",
            "--no-install-project",
            "--no-editable",
            "--active",
        ],
        cwd=export_dir,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=_BUILD_TIMEOUT_SECONDS,
    )
    if sync.returncode != 0:
        raise ValueError(f"candidate locked dependency sync failed with exit status {sync.returncode}")
    observation_bytes = _observe_runtime_licenses(python, export_dir, environment)
    runtime_license_inventory.parse_observations(observation_bytes)
    _validate_observation_privacy(export_dir, observation_bytes)
    observations_path = staging / runtime_license_inventory.OBSERVATIONS_NAME
    with observations_path.open("xb") as stream:
        stream.write(observation_bytes)
    observations_digest = hashlib.sha256(observation_bytes).hexdigest()
    policy_path = staging / "license-policy.json"
    with policy_path.open("xb") as stream:
        stream.write(
            runtime_license_inventory.read_snapshot(
                export_dir / "docs" / "release-readiness" / "dependency-security-policy.json"
            )
        )
    qa_environment_dir = staging / "license-qa-environment"
    if qa_environment_dir.exists() or qa_environment_dir.is_symlink():
        raise ValueError("candidate QA dependency environment must not already exist")
    create_qa = subprocess.run(
        ["uv", "venv", str(qa_environment_dir)],
        cwd=export_dir,
        env=creation_environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=_BUILD_TIMEOUT_SECONDS,
    )
    if create_qa.returncode != 0:
        raise ValueError(f"candidate QA dependency environment failed with exit status {create_qa.returncode}")
    qa_python = _environment_python(qa_environment_dir)
    qa_environment = creation_environment.copy()
    qa_environment["VIRTUAL_ENV"] = str(qa_environment_dir)
    qa_environment["PATH"] = f"{qa_python.parent}{os.pathsep}{qa_environment.get('PATH', '')}"
    qa_sync = subprocess.run(
        ["uv", "sync", "--locked", "--only-group", "dev", "--no-install-project", "--no-editable", "--active"],
        cwd=export_dir,
        env=qa_environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=_BUILD_TIMEOUT_SECONDS,
    )
    if qa_sync.returncode != 0:
        raise ValueError(f"candidate locked QA dependency sync failed with exit status {qa_sync.returncode}")
    output_path = staging / "license-evidence.json"
    evidence = subprocess.run(
        [
            str(qa_python),
            "-I",
            str(export_dir / "scripts" / "check_supply_chain_policy.py"),
            "license-evidence",
            "--policy",
            str(policy_path),
            "--output",
            str(output_path),
            "--scope",
            _LICENSE_SCOPE,
            "--revision",
            manifest.source_commit,
            "--export-policy-sha256",
            manifest.policy_sha256,
            "--sbom",
            str(sbom_path),
            "--platform-requirements",
            str(all_extras_requirements_path),
            "--observations",
            str(observations_path),
            "--observations-sha256",
            observations_digest,
        ],
        cwd=export_dir,
        env=qa_environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=_BUILD_TIMEOUT_SECONDS,
    )
    if evidence.returncode not in {0, 1}:
        detail = evidence.stderr.strip() or evidence.stdout.strip()
        suffix = f": {detail}" if detail else ""
        raise ValueError(f"candidate license evidence failed with exit status {evidence.returncode}{suffix}")
    payload = json.loads(runtime_license_inventory.read_snapshot(output_path))
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "status",
        "scope",
        "revision",
        "export_policy_sha256",
        "sbom_sha256",
        "observations",
        "platform_requirements",
        "marker_environment",
        "observed_packages",
        "packages",
        "findings",
    }:
        raise ValueError("candidate license evidence has an unsupported schema")
    if (
        payload["schema_version"] != 2
        or payload["status"] not in {"pass", "fail"}
        or payload["scope"] != _LICENSE_SCOPE
        or payload["revision"] != manifest.source_commit
        or payload["export_policy_sha256"] != manifest.policy_sha256
        or not isinstance(payload["sbom_sha256"], str)
        or not isinstance(payload["observed_packages"], int)
        or payload["observed_packages"] < 0
        or not isinstance(payload["packages"], list)
        or not isinstance(payload["findings"], list)
        or payload["observed_packages"] != len(payload["packages"])
    ):
        raise ValueError("candidate license evidence does not bind the verified export")
    _, marker_environment = runtime_license_inventory.parse_observations(observation_bytes)
    if (
        payload["observations"] != {"name": runtime_license_inventory.OBSERVATIONS_NAME, "sha256": observations_digest}
        or payload["platform_requirements"]
        != {
            "name": runtime_license_inventory.PLATFORM_REQUIREMENTS_NAME,
            "sha256": hashlib.sha256(runtime_license_inventory.read_snapshot(all_extras_requirements_path)).hexdigest(),
        }
        or payload["marker_environment"] != marker_environment
    ):
        raise ValueError("candidate license evidence does not bind the runtime observation inputs")
    return payload


def _report_bytes(report: CandidateReport | PublicHistoryCandidateReport) -> bytes:
    """Return the canonical bytes used for both retention and verification."""
    return (json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_report(directory_fd: int, report: CandidateReport | PublicHistoryCandidateReport) -> None:
    """Exclusively create evidence under the retained unpublished staging descriptor."""
    release_bundle._write_bytes_at(directory_fd, "report.json", _report_bytes(report))


def _verify_open_candidate(directory_fd: int, report_bytes: bytes) -> None:
    """Verify the report and closed bundle under one retained candidate descriptor."""
    if release_bundle._safe_bytes_at(directory_fd, "report.json") != report_bytes:
        raise ValueError("candidate report bytes changed during publication")
    bundle_fd = release_bundle._open_directory_at(directory_fd, "bundle")
    try:
        identity = os.fstat(bundle_fd)
        release_bundle._verify_open_bundle(bundle_fd, candidate_report=report_bytes)
        if release_bundle._safe_bytes_at(directory_fd, "report.json") != report_bytes:
            raise ValueError("candidate report bytes changed during verification")
        final = os.stat("bundle", dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISDIR(final.st_mode) or (final.st_dev, final.st_ino) != (identity.st_dev, identity.st_ino):
            raise ValueError("candidate bundle identity changed during verification")
    finally:
        os.close(bundle_fd)


def check_candidate(
    repo: Path,
    revision: str,
    *,
    expected_repository: str,
    planned_tag: str,
    output_dir: Path,
    public_history: public_history_source.PublicHistorySource | None = None,
    expected_anchor: public_history_source.ApprovedCutoverAnchor | None = None,
    cutover_record: bytes | None = None,
) -> CandidateReport | PublicHistoryCandidateReport:
    """Prepare one verified source; publishing still requires a supported bundle.

    Successors require independently trusted inputs and retain report version 8
    as pending in unpublished staging until downstream bundle support exists.
    The initial export and its version 7 release controls are unchanged.
    """
    if not expected_repository or not planned_tag:
        raise ValueError("expected repository and planned tag must be non-empty")
    if public_history is None:
        if expected_anchor is not None or cutover_record is not None:
            raise ValueError("initial export does not accept public history trust inputs")
    else:
        if expected_anchor is None or cutover_record is None:
            raise ValueError("public history candidate requires an independently approved anchor and exact record")
        public_history_source.source_bytes(
            public_history, expected_anchor=expected_anchor, cutover_record=cutover_record
        )
        if public_history.repository != expected_repository or public_history.planned_tag != planned_tag:
            raise ValueError("public history identity differs from the requested candidate")
        if export_public_tree._resolve_commit(repo, revision)[0] != public_history.source_commit:
            raise ValueError("requested revision differs from the public history source")
    output_dir = output_dir.absolute()
    policy_path = repo / export_public_tree._POLICY_PATH
    parent_fd = release_approval_archive._open_real_directory(output_dir.parent, create=True)
    staging_fd: int | None = None
    try:
        try:
            os.stat(output_dir.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ValueError(f"candidate output directory already exists: {output_dir}")
        staging_name, staging_fd = release_approval_archive._create_staging_directory(parent_fd, output_dir.name)
        staging = output_dir.parent / staging_name
        export_dir = staging / "export"
        manifest_path = staging / "manifest.json"
        if public_history is None:
            manifest = export_public_tree.export_tree(repo, revision, policy_path, export_dir, manifest_path)
            _validate_expected_identity(manifest, expected_repository, planned_tag)
        else:
            if expected_anchor is None or cutover_record is None:
                raise ValueError("public history candidate requires an independently approved anchor and exact record")
            public_history_source.materialize_source(
                repo, public_history, export_dir, expected_anchor=expected_anchor, cutover_record=cutover_record
            )
            export_public_tree._write_file_at(
                staging_fd,
                manifest_path.name,
                public_history_source.source_bytes(
                    public_history, expected_anchor=expected_anchor, cutover_record=cutover_record
                ),
                0o644,
            )
        source_kind = "initial-export" if public_history is None else public_history_source.SOURCE_KIND
        verified_manifest = public_tree_scan._scan_source(
            repo,
            export_dir,
            manifest_path,
            policy_path,
            source_kind=source_kind,
            expected_anchor=expected_anchor,
            cutover_record=cutover_record,
        )
        artifacts = _build_export(export_dir, staging / "dist")
        rebuilt_artifacts = _build_export(export_dir, staging / "rebuild")
        _require_reproducible_builds(artifacts, rebuilt_artifacts)
        validation = check_artifacts.validate(
            artifacts,
            export_dir,
            source_revision=verified_manifest.source_commit,
            tracked_payload_paths=tuple(
                entry.path
                for entry in (
                    verified_manifest.included
                    if isinstance(verified_manifest, export_public_tree.ExportManifest)
                    else verified_manifest.entries
                )
            ),
        )
        licenses = _license_evidence(export_dir, staging, verified_manifest)
        if (
            public_tree_scan._scan_source(
                repo,
                export_dir,
                manifest_path,
                policy_path,
                source_kind=source_kind,
                expected_anchor=expected_anchor,
                cutover_record=cutover_record,
            )
            != verified_manifest
        ):
            raise ValueError("verified export changed during license collection")
        wheelhouse = staging / "runtime-wheelhouse"
        release_wheelhouse.collect(
            staging / "runtime-requirements.txt",
            staging / "release-build-requirements.txt",
            wheelhouse=wheelhouse,
        )
        wheelhouse_archive = staging / "runtime-wheelhouse.zip"
        wheelhouse_digest = release_wheelhouse.archive(wheelhouse, wheelhouse_archive)
        scan = public_tree_scan.scan_public_tree(
            repo,
            export_dir,
            manifest_path,
            policy_path,
            artifacts=artifacts,
            artifact_validation=validation,
            source_kind=source_kind,
            expected_anchor=expected_anchor,
            cutover_record=cutover_record,
        )
        candidate_identity = (
            expected_repository,
            _package_identity(export_dir),
            planned_tag,
        )
        evidence = (
            validation,
            licenses,
            {
                "name": "runtime-requirements.txt",
                "sha256": hashlib.sha256((staging / "runtime-requirements.txt").read_bytes()).hexdigest(),
            },
            {
                "name": "release-build-requirements.txt",
                "sha256": hashlib.sha256((staging / "release-build-requirements.txt").read_bytes()).hexdigest(),
            },
            {"name": "runtime-wheelhouse.zip", "sha256": wheelhouse_digest},
            scan,
        )
        report: CandidateReport | PublicHistoryCandidateReport
        if isinstance(verified_manifest, export_public_tree.ExportManifest):
            report = CandidateReport(
                7,
                candidate_identity[0],
                candidate_identity[1],
                candidate_identity[2],
                verified_manifest,
                validation,
                licenses,
                evidence[2],
                evidence[3],
                evidence[4],
                scan,
            )
        else:
            if (
                not isinstance(scan, public_tree_scan.PublicHistoryScanReport)
                or expected_anchor is None
                or cutover_record is None
            ):
                raise ValueError("public history candidate lacks a versioned scan and independent trust")
            source_sha256 = hashlib.sha256(
                public_history_source.source_bytes(
                    verified_manifest, expected_anchor=expected_anchor, cutover_record=cutover_record
                )
            ).hexdigest()
            if scan.source_sha256 != source_sha256 or scan.anchor != expected_anchor:
                raise ValueError("public history scan differs from the candidate source identity")
            report = PublicHistoryCandidateReport(
                8,
                candidate_identity[0],
                candidate_identity[1],
                candidate_identity[2],
                verified_manifest,
                validation,
                licenses,
                evidence[2],
                evidence[3],
                evidence[4],
                scan,
                source_sha256,
            )
        if not report.ok:
            raise ValueError(
                "candidate artifact validation, dependency-license evidence, or public-tree scan did not pass "
                f"(artifact_validation={validation.ok}, licenses={licenses['status']}, scan={scan.to_dict()['status']}, "
                f"findings={[(finding.rule_id, finding.path, finding.line) for finding in scan.findings]})"
            )
        _write_report(staging_fd, report)
        release_bundle.materialize(staging)
        report_bytes = _report_bytes(report)
        _verify_open_candidate(staging_fd, report_bytes)
        release_approval_archive._rename_no_replace_at(parent_fd, staging_name, output_dir.name)
        published_fd = release_approval_archive._open_directory_at(parent_fd, output_dir.name)
        try:
            original = os.fstat(staging_fd)
            published = os.fstat(published_fd)
            identity = (original.st_dev, original.st_ino)
            if (published.st_dev, published.st_ino) != identity:
                raise ValueError("published candidate identity does not match retained staging")
            _verify_open_candidate(published_fd, report_bytes)
            final = os.stat(output_dir.name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISDIR(final.st_mode) or (final.st_dev, final.st_ino) != identity:
                raise ValueError("published candidate identity changed during verification")
        finally:
            os.close(published_fd)
        return report
    finally:
        # Retain unpublished random staging: its mutable name may now identify
        # unrelated content, so path-based cleanup cannot safely remove it.
        if staging_fd is not None:
            os.close(staging_fd)
        os.close(parent_fd)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--expected-repository", required=True)
    parser.add_argument("--planned-tag", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the authoritative candidate check and return a stable exit status."""
    args = _parser().parse_args(argv)
    try:
        report = check_candidate(
            args.repo.resolve(),
            args.revision,
            expected_repository=args.expected_repository,
            planned_tag=args.planned_tag,
            output_dir=args.output_dir.absolute(),
        )
    except (OSError, ValueError, subprocess.TimeoutExpired, export_public_tree.ExportError) as error:
        print(f"Public candidate check: ERROR: {error}", file=sys.stderr)
        return 2
    if args.as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(f"Public candidate check: PASS ({report.scan.source_commit})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
