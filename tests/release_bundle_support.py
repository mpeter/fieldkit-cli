"""Shared fictional release candidate and standalone consumer fixture builders."""

import hashlib
import json
from pathlib import Path

from scripts import _release_bundle_evidence
from scripts.runtime_license_inventory import MARKER_KEYS


def write(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def candidate(tmp_path: Path) -> Path:
    candidate = tmp_path / "candidate"
    wheel = "fieldkit_cli-1.0.0-py3-none-any.whl"
    sdist = "fieldkit_cli-1.0.0.tar.gz"
    wheel_digest = write(candidate / "dist" / wheel, b"wheel payload")
    sdist_digest = write(candidate / "dist" / sdist, b"sdist payload")
    sbom_digest = write(candidate / "locked-graph.cdx.json", b'{"components": []}\n')
    marker_environment = dict.fromkeys(MARKER_KEYS, "example")
    observations_digest = write(
        candidate / "runtime-license-observations.json",
        json.dumps(
            {
                "schema_version": 1,
                "packages": [{"name": "click", "version": "8.5.0", "license_expression": "BSD-3-Clause"}],
                "marker_environment": marker_environment,
            }
        ).encode(),
    )
    platform_digest = write(candidate / "platform-all-extras-requirements.txt", b"click==8.5.0\n")
    requirements_digest = write(
        candidate / "runtime-requirements.txt",
        b"click==8.5.0 --hash=sha256:" + b"a" * 64 + b"\n",
    )
    build_requirements_digest = write(
        candidate / "release-build-requirements.txt",
        b"hatchling==1.26.3 --hash=sha256:" + b"b" * 64 + b"\n",
    )
    wheelhouse_digest = write(candidate / "runtime-wheelhouse.zip", b"wheelhouse payload")
    revision = "a" * 40
    report = {
        "schema_version": 7,
        "status": "pass",
        "expected_repository": "example/fieldkit-cli",
        "package": "fieldkit-cli",
        "planned_tag": "v1.0.0",
        "export_manifest": {
            "schema_version": 1,
            "source_commit": revision,
            "source_tree": "b" * 40,
            "policy_path": "docs/release-readiness/public-tree-policy.json",
            "policy_oid": "c" * 40,
            "policy_sha256": "d" * 64,
            "expected_repository": "example/fieldkit-cli",
            "planned_tag": "v1.0.0",
            "exported_tree": "e" * 40,
            "included": [],
            "excluded": [],
        },
        "artifact_validation": {
            "schema_version": 1,
            "status": "pass",
            "source_revision": revision,
            "artifacts": [
                {
                    "name": wheel,
                    "kind": "wheel",
                    "sha256": wheel_digest,
                    "criteria": [{"criterion_id": "ART001", "status": "pass", "diagnostics": []}],
                    "status": "pass",
                },
                {
                    "name": sdist,
                    "kind": "sdist",
                    "sha256": sdist_digest,
                    "criteria": [{"criterion_id": "ART001", "status": "pass", "diagnostics": []}],
                    "status": "pass",
                },
            ],
        },
        "license_evidence": {
            "schema_version": 2,
            "status": "pass",
            "scope": "runtime-all-extras",
            "revision": revision,
            "export_policy_sha256": "d" * 64,
            "sbom_sha256": sbom_digest,
            "observed_packages": 1,
            "packages": [{"package_url": "pkg:pypi/click@8.5.0", "license_expression": "BSD-3-Clause"}],
            "findings": [],
            "observations": {"name": "runtime-license-observations.json", "sha256": observations_digest},
            "platform_requirements": {"name": "platform-all-extras-requirements.txt", "sha256": platform_digest},
            "marker_environment": marker_environment,
        },
        "runtime_requirements": {
            "name": "runtime-requirements.txt",
            "sha256": requirements_digest,
        },
        "release_build_requirements": {
            "name": "release-build-requirements.txt",
            "sha256": build_requirements_digest,
        },
        "runtime_wheelhouse": {"name": "runtime-wheelhouse.zip", "sha256": wheelhouse_digest},
        "scan": {
            "schema_version": 1,
            "source_commit": revision,
            "source_tree": "b" * 40,
            "exported_tree": "e" * 40,
            "expected_repository": "example/fieldkit-cli",
            "planned_tag": "v1.0.0",
            "export_policy_oid": "c" * 40,
            "export_policy_sha256": "d" * 64,
            "scan_policy_oid": "f" * 40,
            "scan_policy_sha256": "1" * 64,
            "identity_policy_oid": "2" * 40,
            "identity_policy_sha256": "3" * 64,
            "scanned_entries": 1,
            "scanned_artifact_entries": 2,
            "scanned_text_entries": 1,
            "approved_binary_entries": 0,
            "classified_matches": 0,
            "gitleaks_version": "8.30.1",
            "gitleaks_findings": 0,
            "artifacts": [
                {
                    "name": wheel,
                    "kind": "wheel",
                    "sha256": wheel_digest,
                    "member_count": 1,
                    "total_uncompressed_bytes": 1,
                },
                {
                    "name": sdist,
                    "kind": "sdist",
                    "sha256": sdist_digest,
                    "member_count": 1,
                    "total_uncompressed_bytes": 1,
                },
            ],
            "findings": [],
            "status": "pass",
            "artifact_coverage": "pass",
        },
    }
    (candidate / "report.json").write_text(json.dumps(report), encoding="utf-8")
    return candidate


def expected_asset_provenance(report: dict[str, object]) -> dict[str, object]:
    validation = _release_bundle_evidence._object(report["artifact_validation"], "test validation")
    artifacts = validation["artifacts"]
    assert isinstance(artifacts, list)
    license_evidence = _release_bundle_evidence._object(report["license_evidence"], "test license evidence")
    return {
        "artifacts": sorted(
            ({key: item[key] for key in ("name", "kind", "sha256")} for item in artifacts),
            key=lambda item: item["name"],
        ),
        "runtime_sbom": {
            "name": "locked-graph.cdx.json",
            "sha256": license_evidence["sbom_sha256"],
            "scope": "runtime-all-extras",
        },
        "runtime_requirements": report["runtime_requirements"],
        "release_build_requirements": report["release_build_requirements"],
        "runtime_wheelhouse": report["runtime_wheelhouse"],
        "license_observations": license_evidence["observations"],
        "platform_requirements": license_evidence["platform_requirements"],
        "marker_environment": license_evidence["marker_environment"],
        "checksum_manifest": {"name": "SHA256SUMS", "format_version": 1},
    }


def standalone_scripts(tmp_path: Path) -> Path:
    scripts = tmp_path / "standalone/scripts"
    scripts.mkdir(parents=True)
    for name in (
        "release_bundle.py",
        "release_filesystem.py",
        "_release_bundle_evidence.py",
        "runtime_license_inventory.py",
        "json_policy.py",
    ):
        source = Path(__file__).parents[1] / "scripts" / name
        (scripts / name).write_bytes(source.read_bytes())
    return scripts
