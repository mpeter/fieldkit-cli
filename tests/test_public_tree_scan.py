"""Contracts for manifest-bound public-tree content scanning."""

import hashlib
import io
import json
import subprocess
import tarfile
import zipfile
from datetime import date
from pathlib import Path

import pytest

from scripts import check_artifacts, check_public_identity, export_public_tree, public_tree_artifacts, public_tree_scan

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True, timeout=10)
    return result.stdout.strip()


def _write_policies(
    repo: Path,
    *,
    public_paths: list[str],
    binary_allowances: list[dict[str, object]] | None = None,
) -> tuple[Path, Path]:
    export_policy_path = repo / export_public_tree._POLICY_PATH
    scan_policy_path = repo / public_tree_scan.POLICY_PATH
    identity_policy_path = repo / check_public_identity.POLICY_PATH
    export_policy_path.parent.mkdir(parents=True, exist_ok=True)
    scan_policy_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "max_text_bytes": 1024,
                "archives": {
                    "max_members": 20,
                    "max_member_bytes": 1024,
                    "max_total_bytes": 4096,
                    "max_archive_bytes": 4096,
                },
                "text_rules": [
                    {
                        "id": "SECRET001",
                        "category": "credential",
                        "pattern": "example-secret-[0-9]{4}",
                    }
                ],
                "text_allowances": [],
                "binary_allowances": binary_allowances or [],
            }
        ),
        encoding="utf-8",
    )
    identity_policy_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "scope": ["src/**"],
                "rules": [{"id": "IDENTITY001", "category": "identity", "pattern": "private-identity"}],
                "allowances": [],
            }
        ),
        encoding="utf-8",
    )
    export_policy_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "expected_repository": "example/fieldkit-cli",
                "planned_tag": "v1.0.0",
                "rules": [
                    {
                        "id": "public",
                        "action": "include",
                        "category": "product",
                        "patterns": [
                            *public_paths,
                            export_public_tree._POLICY_PATH.as_posix(),
                            public_tree_scan.POLICY_PATH.as_posix(),
                            check_public_identity.POLICY_PATH.as_posix(),
                        ],
                        "rationale": "Public test files and their release policies.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return export_policy_path, scan_policy_path


def _export(repo: Path, tmp_path: Path) -> tuple[Path, Path, Path]:
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "candidate")
    destination = tmp_path / "public"
    manifest_path = tmp_path / "manifest.json"
    export_policy_path = repo / export_public_tree._POLICY_PATH
    export_public_tree.export_tree(repo, "HEAD", export_policy_path, destination, manifest_path)
    return destination, manifest_path, export_policy_path


def _repository(tmp_path: Path, content: bytes = b"safe\n") -> tuple[Path, Path, Path, Path]:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Example Maintainer")
    _git(repo, "config", "user.email", "maintainer@example.com")
    (repo / "README.md").write_bytes(content)
    _write_policies(repo, public_paths=["README.md"])
    destination, manifest_path, export_policy_path = _export(repo, tmp_path)
    return repo, destination, manifest_path, export_policy_path


def _artifact_validation(
    source_revision: str,
    *artifacts: public_tree_artifacts.ArtifactEvidence,
) -> check_artifacts.ValidationReport:
    criterion = check_artifacts.CriterionResult("TEST", "pass")
    results = tuple(
        check_artifacts.ArtifactResult(artifact.name, artifact.kind, artifact.sha256, (criterion,))
        for artifact in artifacts
    )
    return check_artifacts.ValidationReport(1, source_revision, results)


def test_scan_is_bound_to_verified_manifest_and_ignores_checkout_drift(tmp_path: Path) -> None:
    repo, destination, manifest_path, export_policy_path = _repository(tmp_path)
    (repo / "README.md").write_text("example-secret-9999\n", encoding="utf-8")

    report = public_tree_scan.scan_public_tree(repo, destination, manifest_path, export_policy_path)

    manifest = export_public_tree.verify_export(repo, destination, manifest_path, export_policy_path)
    assert report.content_ok is True
    assert report.ok is False
    assert report.to_dict()["status"] == "pending"
    assert report.source_commit == manifest.source_commit
    assert report.exported_tree == manifest.exported_tree
    assert report.expected_repository == "example/fieldkit-cli"
    assert report.planned_tag == "v1.0.0"
    assert report.scanned_entries == len(manifest.included)


def test_scan_reports_secret_location_without_secret_payload(tmp_path: Path) -> None:
    repo, destination, manifest_path, export_policy_path = _repository(tmp_path, b"safe\nexample-secret-1234\n")

    report = public_tree_scan.scan_public_tree(repo, destination, manifest_path, export_policy_path)

    assert report.ok is False
    assert report.findings == (public_tree_scan.Finding("SECRET001", "credential", "README.md", 2),)
    assert "example-secret-1234" not in json.dumps(report.to_dict())


def test_scan_reports_gitleaks_secret_without_retaining_payload(tmp_path: Path) -> None:
    token = "AKIA" + "QWERTYUIOPASDFGH"
    repo, destination, manifest_path, export_policy_path = _repository(tmp_path, f"token={token}\n".encode())

    report = public_tree_scan.scan_public_tree(repo, destination, manifest_path, export_policy_path)

    assert public_tree_scan.Finding("GITLEAKS001", "secret", "README.md", 1) in report.findings
    assert token not in json.dumps(report.to_dict())


def test_scan_allows_only_the_declared_public_system_sender_path(tmp_path: Path) -> None:
    """A text allowance never authorizes the same match at another public path."""
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Example Maintainer")
    _git(repo, "config", "user.email", "maintainer@example.com")
    sender = "sender@" + "public.invalid"
    (repo / "README.md").write_text(f"{sender}\n", encoding="utf-8")
    (repo / "other.md").write_text(f"{sender}\n", encoding="utf-8")
    _write_policies(repo, public_paths=["README.md", "other.md"])
    scan_policy_path = repo / public_tree_scan.POLICY_PATH
    scan_policy = json.loads(scan_policy_path.read_text(encoding="utf-8"))
    scan_policy["text_rules"] = [{"id": "PII001", "category": "personal_email", "pattern": "sender@public[.]invalid"}]
    scan_policy["text_allowances"] = [
        {
            "rule_id": "PII001",
            "path": "README.md",
            "pattern": "sender@public[.]invalid",
            "classification": "public_system_sender",
            "rationale": "Public automated sender used by the product.",
        }
    ]
    scan_policy_path.write_text(json.dumps(scan_policy), encoding="utf-8")
    destination, manifest_path, export_policy_path = _export(repo, tmp_path)

    report = public_tree_scan.scan_public_tree(repo, destination, manifest_path, export_policy_path)

    assert report.classified_matches == 1
    assert report.findings == (public_tree_scan.Finding("PII001", "personal_email", "other.md", 1),)


def test_public_policy_exempts_reserved_example_subdomains() -> None:
    """Fixture names may use an RFC-reserved example.com subdomain without becoming PII."""
    policy = public_tree_scan._load_policy(
        (Path("docs/release-readiness/public-tree-scan-policy.json")).read_bytes(), date.today()
    )
    rule = next(item for item in policy.text_rules if item.rule_id == "PII001")

    assert rule.pattern.search("user@example.com") is None
    assert rule.pattern.search("user@fixture.example.com") is None
    assert rule.pattern.search("user@example.net") is None
    assert rule.pattern.search("user@fixture.example.org") is None
    assert rule.pattern.search("user@" + "customer.invalid") is not None


def test_public_policy_classifies_git_service_account_only_in_rehearsal_verifier() -> None:
    """The exact Git SSH service account is a transport identifier, not user PII."""
    policy = public_tree_scan._load_policy(
        Path("docs/release-readiness/public-tree-scan-policy.json").read_bytes(), date.today()
    )
    allowance = next(
        item
        for item in policy.text_allowances
        if item.rule_id == "PII001" and item.path == "scripts/check_documentation_examples.py"
    )

    assert allowance.pattern.fullmatch("git" + "@github.com")
    assert allowance.pattern.search("person" + "@github.com") is None


def test_public_policy_classifies_git_service_account_in_cutover_verifier() -> None:
    """Cutover accepts the same canonical Git SSH transport and no user address."""
    policy = public_tree_scan._load_policy(
        Path("docs/release-readiness/public-tree-scan-policy.json").read_bytes(), date.today()
    )
    allowance = next(
        item for item in policy.text_allowances if item.rule_id == "PII001" and item.path == "scripts/cutover_record.py"
    )

    assert allowance.pattern.fullmatch("git" + "@github.com")
    assert allowance.pattern.search("person" + "@github.com") is None


def test_public_policy_rejects_private_tracker_identifiers_case_insensitively() -> None:
    policy = public_tree_scan._load_policy(
        (Path("docs/release-readiness/public-tree-scan-policy.json")).read_bytes(), date.today()
    )
    rule = next(item for item in policy.text_rules if item.rule_id == "PRIVATE002")

    assert rule.pattern.search("bug" + "-123") is not None
    assert rule.pattern.search("Enh" + "-456") is not None
    assert rule.pattern.search("bi" + "-789" + "a") is not None
    assert rule.pattern.search("bug" + "390") is not None


def test_scan_applies_identity_policy_to_every_manifest_text_entry(tmp_path: Path) -> None:
    repo, destination, manifest_path, export_policy_path = _repository(tmp_path, b"private-identity\n")

    report = public_tree_scan.scan_public_tree(repo, destination, manifest_path, export_policy_path)

    assert report.findings == (public_tree_scan.Finding("IDENTITY001", "identity", "README.md", 1),)
    assert len(report.identity_policy_sha256) == 64


def test_scan_structurally_validates_policy_sources_without_self_matching(tmp_path: Path) -> None:
    """Bound and parsed detector definitions are policy inputs, not self-referential payload."""
    repo, destination, manifest_path, export_policy_path = _repository(tmp_path)

    report = public_tree_scan.scan_public_tree(repo, destination, manifest_path, export_policy_path)

    assert report.content_ok is True
    assert report.ok is False
    assert report.scanned_entries == report.scanned_text_entries


def test_scan_detects_secret_embedded_in_identity_policy(tmp_path: Path) -> None:
    """The identity self-match boundary cannot exempt credentials in policy text."""
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Example Maintainer")
    _git(repo, "config", "user.email", "maintainer@example.com")
    (repo / "README.md").write_text("safe\n", encoding="utf-8")
    _write_policies(repo, public_paths=["README.md"])
    identity_policy_path = repo / check_public_identity.POLICY_PATH
    identity_policy = json.loads(identity_policy_path.read_text(encoding="utf-8"))
    identity_policy["rules"][0]["pattern"] = "private-identity|example-secret-1234"
    identity_policy_path.write_text(json.dumps(identity_policy), encoding="utf-8")
    destination, manifest_path, export_policy_path = _export(repo, tmp_path)

    report = public_tree_scan.scan_public_tree(repo, destination, manifest_path, export_policy_path)

    assert report.findings == (
        public_tree_scan.Finding(
            "SECRET001",
            "credential",
            check_public_identity.POLICY_PATH.as_posix(),
            1,
        ),
    )


def test_scan_applies_same_content_rules_to_bounded_artifact_members(tmp_path: Path) -> None:
    repo, destination, manifest_path, export_policy_path = _repository(tmp_path)
    wheel = tmp_path / "fieldkit_cli-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("fieldkit/safe.json", '{"value":"example-secret-1234"}\n')
    artifact, _ = public_tree_artifacts.read_artifact(
        wheel,
        public_tree_artifacts.ArchiveLimits(20, 1024, 4096, 4096),
    )
    manifest = export_public_tree.verify_export(repo, destination, manifest_path, export_policy_path)

    report = public_tree_scan.scan_public_tree(
        repo,
        destination,
        manifest_path,
        export_policy_path,
        artifacts=(wheel,),
        artifact_validation=_artifact_validation(manifest.source_commit, artifact),
    )

    assert report.findings == (
        public_tree_scan.Finding(
            "SECRET001",
            "credential",
            f"artifact:{wheel.name}:fieldkit/safe.json",
            1,
        ),
    )
    assert report.scanned_artifact_entries == 1
    assert report.artifacts[0].kind == "wheel"
    assert len(report.artifacts[0].sha256) == 64


def test_scan_rejects_unapproved_non_text_entry(tmp_path: Path) -> None:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Example Maintainer")
    _git(repo, "config", "user.email", "maintainer@example.com")
    (repo / "asset.bin").write_bytes(b"\x00\x01\x02")
    _write_policies(repo, public_paths=["asset.bin"])
    destination, manifest_path, export_policy_path = _export(repo, tmp_path)

    report = public_tree_scan.scan_public_tree(repo, destination, manifest_path, export_policy_path)

    assert report.ok is False
    assert report.findings == (public_tree_scan.Finding("DATA001", "unapproved_binary_or_data", "asset.bin", None),)


def test_scan_accepts_only_exact_binary_identity(tmp_path: Path) -> None:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Example Maintainer")
    _git(repo, "config", "user.email", "maintainer@example.com")
    payload = b"\x89PNG\r\n\x1a\nexample"
    asset = repo / "asset.png"
    asset.write_bytes(payload)
    binary_allowance = {
        "path": "asset.png",
        "blob_oid": _git(repo, "hash-object", "asset.png"),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "media_type": "image/png",
        "max_bytes": len(payload),
        "classification": "public_product_asset",
        "rationale": "Required public test image.",
    }
    _write_policies(repo, public_paths=["asset.png"], binary_allowances=[binary_allowance])
    destination, manifest_path, export_policy_path = _export(repo, tmp_path)

    report = public_tree_scan.scan_public_tree(repo, destination, manifest_path, export_policy_path)

    assert report.content_ok is True
    assert report.ok is False
    assert report.approved_binary_entries == 1


def test_scan_requires_candidate_bound_validation_for_artifacts(tmp_path: Path) -> None:
    repo, destination, manifest_path, export_policy_path = _repository(tmp_path)
    wheel = tmp_path / "fieldkit_cli-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("fieldkit/safe.py", "safe = True\n")

    with pytest.raises(ValueError, match="artifact validation evidence is required"):
        public_tree_scan.scan_public_tree(
            repo,
            destination,
            manifest_path,
            export_policy_path,
            artifacts=(wheel,),
        )


def test_scan_rejects_artifact_validation_from_another_revision(tmp_path: Path) -> None:
    repo, destination, manifest_path, export_policy_path = _repository(tmp_path)
    wheel = tmp_path / "fieldkit_cli-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("fieldkit/safe.py", "safe = True\n")
    artifact, _ = public_tree_artifacts.read_artifact(
        wheel,
        public_tree_artifacts.ArchiveLimits(20, 1024, 4096, 4096),
    )

    with pytest.raises(ValueError, match="source revision does not match"):
        public_tree_scan.scan_public_tree(
            repo,
            destination,
            manifest_path,
            export_policy_path,
            artifacts=(wheel,),
            artifact_validation=_artifact_validation("0" * 40, artifact),
        )


def test_scan_rejects_validation_for_different_artifact_bytes(tmp_path: Path) -> None:
    repo, destination, manifest_path, export_policy_path = _repository(tmp_path)
    wheel = tmp_path / "fieldkit_cli-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("fieldkit/safe.py", "safe = True\n")
    artifact, _ = public_tree_artifacts.read_artifact(
        wheel,
        public_tree_artifacts.ArchiveLimits(20, 1024, 4096, 4096),
    )
    manifest = export_public_tree.verify_export(repo, destination, manifest_path, export_policy_path)
    different = public_tree_artifacts.ArtifactEvidence(
        artifact.name,
        artifact.kind,
        "0" * 64,
        artifact.member_count,
        artifact.total_uncompressed_bytes,
    )

    with pytest.raises(ValueError, match="identities do not match"):
        public_tree_scan.scan_public_tree(
            repo,
            destination,
            manifest_path,
            export_policy_path,
            artifacts=(wheel,),
            artifact_validation=_artifact_validation(manifest.source_commit, different),
        )


def test_scan_pass_requires_matching_wheel_and_sdist_evidence(tmp_path: Path) -> None:
    repo, destination, manifest_path, export_policy_path = _repository(tmp_path)
    wheel = tmp_path / "fieldkit_cli-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("fieldkit/safe.py", "safe = True\n")
    sdist = tmp_path / "fieldkit_cli-1.0.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        payload = b"safe = True\n"
        member = tarfile.TarInfo("fieldkit_cli-1.0.0/src/fieldkit/safe.py")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    limits = public_tree_artifacts.ArchiveLimits(20, 1024, 4096, 4096)
    wheel_evidence, _ = public_tree_artifacts.read_artifact(wheel, limits)
    sdist_evidence, _ = public_tree_artifacts.read_artifact(sdist, limits)
    manifest = export_public_tree.verify_export(repo, destination, manifest_path, export_policy_path)

    report = public_tree_scan.scan_public_tree(
        repo,
        destination,
        manifest_path,
        export_policy_path,
        artifacts=(wheel, sdist),
        artifact_validation=_artifact_validation(manifest.source_commit, wheel_evidence, sdist_evidence),
    )

    assert report.content_ok is True
    assert report.artifact_coverage == "pass"
    assert report.ok is True
    assert report.to_dict()["status"] == "pass"


def test_scan_rejects_tampered_export_before_emitting_scan_result(tmp_path: Path) -> None:
    repo, destination, manifest_path, export_policy_path = _repository(tmp_path)
    (destination / "README.md").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(export_public_tree.ExportError, match="object mismatch"):
        public_tree_scan.scan_public_tree(repo, destination, manifest_path, export_policy_path)


def test_scan_rechecks_blob_identity_after_export_verification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A post-verification replacement cannot be reported against the old export tree."""
    repo, destination, manifest_path, export_policy_path = _repository(tmp_path)
    original_verify = export_public_tree.verify_export

    def verify_then_replace(
        repo_arg: Path,
        destination_arg: Path,
        manifest_path_arg: Path,
        policy_path_arg: Path,
    ) -> export_public_tree.ExportManifest:
        manifest = original_verify(repo_arg, destination_arg, manifest_path_arg, policy_path_arg)
        (destination / "README.md").write_text("replacement\n", encoding="utf-8")
        return manifest

    monkeypatch.setattr(export_public_tree, "verify_export", verify_then_replace)

    with pytest.raises(ValueError, match=r"object mismatch.*README\.md"):
        public_tree_scan.scan_public_tree(repo, destination, manifest_path, export_policy_path)


def test_scan_rejects_expired_binary_allowance(tmp_path: Path) -> None:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Example Maintainer")
    _git(repo, "config", "user.email", "maintainer@example.com")
    payload = b"\x89PNG\r\n\x1a\nexample"
    (repo / "asset.png").write_bytes(payload)
    _write_policies(
        repo,
        public_paths=["asset.png"],
        binary_allowances=[
            {
                "path": "asset.png",
                "blob_oid": _git(repo, "hash-object", "asset.png"),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "media_type": "image/png",
                "max_bytes": len(payload),
                "classification": "public_product_asset",
                "rationale": "Temporary review decision.",
                "expires_on": "2025-01-01",
            }
        ],
    )
    destination, manifest_path, export_policy_path = _export(repo, tmp_path)

    with pytest.raises(ValueError, match="expired binary allowance"):
        public_tree_scan.scan_public_tree(
            repo,
            destination,
            manifest_path,
            export_policy_path,
            today=date(2026, 1, 1),
        )
