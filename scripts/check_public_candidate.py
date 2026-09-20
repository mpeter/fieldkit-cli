#!/usr/bin/env python3
"""Build and scan a retained release candidate from one verified public export."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

if __package__:
    from scripts import check_artifacts, export_public_tree, public_tree_scan, release_bundle, release_wheelhouse
else:
    import check_artifacts
    import export_public_tree
    import public_tree_scan
    import release_bundle
    import release_wheelhouse

_BUILD_TIMEOUT_SECONDS = 120
_LICENSE_SCOPE = "runtime-all-extras"


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
        return {
            "schema_version": self.schema_version,
            "status": "pass" if self.ok else "fail",
            "expected_repository": self.expected_repository,
            "package": self.package,
            "planned_tag": self.planned_tag,
            "export_manifest": asdict(self.export_manifest),
            "artifact_validation": self.artifact_validation.to_dict(),
            "license_evidence": self.license_evidence,
            "runtime_requirements": self.runtime_requirements,
            "release_build_requirements": self.release_build_requirements,
            "runtime_wheelhouse": self.runtime_wheelhouse,
            "scan": self.scan.to_dict(),
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
    manifest: export_public_tree.ExportManifest,
) -> dict[str, object]:
    """Resolve the exported locked graph and collect its own normalized license evidence."""
    environment_dir = staging / "license-environment"
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
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
        all_extras_requirements_path = staging / "all-extras-requirements.txt"
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
            raise ValueError(
                f"candidate platform requirements export failed with exit status {requirements.returncode}"
            )
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
        create = subprocess.run(
            ["uv", "venv", "--clear", str(environment_dir)],
            cwd=export_dir,
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
        output_path = staging / "license-evidence.json"
        evidence = subprocess.run(
            [
                str(python),
                str(export_dir / "scripts" / "check_supply_chain_policy.py"),
                "license-evidence",
                "--policy",
                str(export_dir / "docs" / "release-readiness" / "dependency-security-policy.json"),
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
            ],
            cwd=export_dir,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=_BUILD_TIMEOUT_SECONDS,
        )
        if evidence.returncode not in {0, 1}:
            detail = evidence.stderr.strip() or evidence.stdout.strip()
            suffix = f": {detail}" if detail else ""
            raise ValueError(f"candidate license evidence failed with exit status {evidence.returncode}{suffix}")
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "status",
            "scope",
            "revision",
            "export_policy_sha256",
            "sbom_sha256",
            "observed_packages",
            "packages",
            "findings",
        }:
            raise ValueError("candidate license evidence has an unsupported schema")
        if (
            payload["schema_version"] != 1
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
        return payload
    finally:
        shutil.rmtree(environment_dir, ignore_errors=True)


def _write_report(path: Path, report: CandidateReport) -> None:
    """Atomically retain the candidate evidence beside its exact artifacts."""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def check_candidate(
    repo: Path,
    revision: str,
    *,
    expected_repository: str,
    planned_tag: str,
    output_dir: Path,
) -> CandidateReport:
    """Build, validate, and retain an exact public candidate from one commit."""
    if not expected_repository or not planned_tag:
        raise ValueError("expected repository and planned tag must be non-empty")
    if output_dir.exists():
        raise ValueError(f"candidate output directory already exists: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    policy_path = repo / export_public_tree._POLICY_PATH
    staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}-", dir=output_dir.parent))
    try:
        export_dir = staging / "export"
        manifest_path = staging / "manifest.json"
        manifest = export_public_tree.export_tree(repo, revision, policy_path, export_dir, manifest_path)
        _validate_expected_identity(manifest, expected_repository, planned_tag)
        verified_manifest = export_public_tree.verify_export(repo, export_dir, manifest_path, policy_path)
        artifacts = _build_export(export_dir, staging / "dist")
        rebuilt_artifacts = _build_export(export_dir, staging / "rebuild")
        _require_reproducible_builds(artifacts, rebuilt_artifacts)
        validation = check_artifacts.validate(
            artifacts,
            export_dir,
            source_revision=verified_manifest.source_commit,
            tracked_payload_paths=tuple(entry.path for entry in verified_manifest.included),
        )
        licenses = _license_evidence(export_dir, staging, verified_manifest)
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
        )
        report = CandidateReport(
            6,
            expected_repository,
            _package_identity(export_dir),
            planned_tag,
            verified_manifest,
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
        if not report.ok:
            raise ValueError(
                "candidate artifact validation, dependency-license evidence, or public-tree scan did not pass "
                f"(artifact_validation={validation.ok}, licenses={licenses['status']}, scan={scan.to_dict()['status']}, "
                f"findings={[(finding.rule_id, finding.path, finding.line) for finding in scan.findings]})"
            )
        _write_report(staging / "report.json", report)
        release_bundle.materialize(staging)
        staging.replace(output_dir)
        return report
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


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
            output_dir=args.output_dir.resolve(),
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
