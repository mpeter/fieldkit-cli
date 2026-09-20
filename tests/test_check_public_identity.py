"""Contracts for the versioned public identity validator."""

import json
import subprocess
import tarfile
import zipfile
from io import BytesIO
from pathlib import Path

import check_public_identity
import pytest

pytestmark = pytest.mark.unit


def _write_policy(root: Path, *, allowances: list[dict[str, object]] | None = None) -> None:
    policy_path = root / check_public_identity.POLICY_PATH
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "scope": ["src/**"],
                "rules": [{"id": "TEST001", "category": "organization_identity", "pattern": "private-org-token"}],
                "allowances": allowances or [],
            }
        ),
        encoding="utf-8",
    )


def _tracked_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, timeout=10)
    subprocess.run(["git", "add", "."], cwd=root, check=True, timeout=10)


def test_validate_reports_rule_and_location_without_payload(tmp_path: Path) -> None:
    _write_policy(tmp_path)
    source = tmp_path / "src" / "example.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("safe\nprivate-org-token\n", encoding="utf-8")
    _tracked_repo(tmp_path)

    report = check_public_identity.validate(tmp_path)

    assert report.ok is False
    assert report.findings == (check_public_identity.Finding("TEST001", "organization_identity", "src/example.py", 2),)
    assert "private-org-token" not in json.dumps(report.to_dict())


def test_live_policy_rejects_noncanonical_project_name() -> None:
    """Public text must spell the project name with its canonical lowercase token."""
    policy = check_public_identity.load_policy(Path(__file__).parents[1])
    canonical_case_rule = next(rule for rule in policy.rules if rule.rule_id == "PUBID008")

    report = check_public_identity.scan_documents(
        check_public_identity.Policy(
            schema_version=policy.schema_version,
            scope=policy.scope,
            rules=(canonical_case_rule,),
            allowances=(),
        ),
        (check_public_identity.Document("README.md", "README.md", "Install " + "Field" + "kit.\n"),),
        frozenset({"README.md"}),
    )

    assert report.findings == (check_public_identity.Finding("PUBID008", "noncanonical_project_name", "README.md", 1),)


def test_scan_documents_uses_explicit_subjects_outside_legacy_scope(tmp_path: Path) -> None:
    """A manifest-selected file is scanned even when legacy discovery scope would omit it."""
    _write_policy(tmp_path)
    policy = check_public_identity.load_policy(tmp_path)

    report = check_public_identity.scan_documents(
        policy,
        (check_public_identity.Document("README.md", "README.md", "private-org-token\n"),),
        frozenset({"README.md"}),
    )

    assert report.findings == (check_public_identity.Finding("TEST001", "organization_identity", "README.md", 1),)


def test_exact_path_allowance_classifies_match(tmp_path: Path) -> None:
    _write_policy(
        tmp_path,
        allowances=[
            {
                "rule_id": "TEST001",
                "paths": ["src/integration.py"],
                "pattern": "required=private-org-token",
                "classification": "integration_specific",
                "rationale": "Required by the example integration protocol.",
            }
        ],
    )
    source = tmp_path / "src" / "integration.py"
    source.parent.mkdir(parents=True)
    source.write_text("required=private-org-token and required=private-org-token\n", encoding="utf-8")
    _tracked_repo(tmp_path)

    report = check_public_identity.validate(tmp_path)

    assert report.ok is True
    assert report.classified_matches == 2


def test_allowance_does_not_apply_to_another_path(tmp_path: Path) -> None:
    _write_policy(
        tmp_path,
        allowances=[
            {
                "rule_id": "TEST001",
                "path": "src/integration.py",
                "pattern": "private-org-token",
                "classification": "integration_specific",
                "rationale": "Required by the example integration protocol.",
            }
        ],
    )
    allowed_source = tmp_path / "src" / "integration.py"
    allowed_source.parent.mkdir(parents=True)
    allowed_source.write_text("safe\n", encoding="utf-8")
    source = tmp_path / "src" / "other.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("private-org-token\n", encoding="utf-8")
    _tracked_repo(tmp_path)

    report = check_public_identity.validate(tmp_path)

    assert report.ok is False
    assert report.findings[0].path == "src/other.py"


def test_load_policy_rejects_wildcard_allowance_path(tmp_path: Path) -> None:
    _write_policy(
        tmp_path,
        allowances=[
            {
                "rule_id": "TEST001",
                "path": "src/**",
                "pattern": "private-org-token",
                "classification": "integration_specific",
                "rationale": "Too broad by design.",
            }
        ],
    )

    with pytest.raises(ValueError, match="exact repository-relative path"):
        check_public_identity.load_policy(tmp_path)


def test_validate_rejects_allowance_outside_tracked_scope(tmp_path: Path) -> None:
    _write_policy(
        tmp_path,
        allowances=[
            {
                "rule_id": "TEST001",
                "path": "src/missing.py",
                "pattern": "private-org-token",
                "classification": "integration_specific",
                "rationale": "A stale path must not weaken the policy.",
            }
        ],
    )
    source = tmp_path / "src" / "fieldkit" / "integration.py"
    source.parent.mkdir(parents=True)
    source.write_text("private-org-token\n", encoding="utf-8")
    _tracked_repo(tmp_path)

    with pytest.raises(ValueError, match="not a tracked in-scope path"):
        check_public_identity.validate(tmp_path)


def test_validate_rejects_unused_allowance(tmp_path: Path) -> None:
    _write_policy(
        tmp_path,
        allowances=[
            {
                "rule_id": "TEST001",
                "path": "src/integration.py",
                "pattern": "private-org-token",
                "classification": "integration_specific",
                "rationale": "A stale pattern must not weaken the policy.",
            }
        ],
    )
    source = tmp_path / "src" / "integration.py"
    source.parent.mkdir(parents=True)
    source.write_text("safe\n", encoding="utf-8")
    _tracked_repo(tmp_path)

    with pytest.raises(ValueError, match="unused allowance"):
        check_public_identity.validate(tmp_path)


def test_wheel_and_sdist_use_repository_paths_for_allowances(tmp_path: Path) -> None:
    _write_policy(
        tmp_path,
        allowances=[
            {
                "rule_id": "TEST001",
                "path": "src/fieldkit/integration.py",
                "pattern": "private-org-token",
                "classification": "integration_specific",
                "rationale": "Required by the example integration protocol.",
            }
        ],
    )
    source = tmp_path / "src" / "fieldkit" / "integration.py"
    source.parent.mkdir(parents=True)
    source.write_text("private-org-token\n", encoding="utf-8")
    _tracked_repo(tmp_path)
    wheel = tmp_path / "fieldkit_cli-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, mode="w") as archive:
        archive.writestr("fieldkit/integration.py", "private-org-token\n")
    sdist = tmp_path / "fieldkit_cli-1.0.0.tar.gz"
    with tarfile.open(sdist, mode="w:gz") as archive:
        payload = b"private-org-token\n"
        info = tarfile.TarInfo("fieldkit_cli-1.0.0/src/fieldkit/integration.py")
        info.size = len(payload)
        archive.addfile(info, BytesIO(payload))

    report = check_public_identity.validate(tmp_path, (wheel, sdist))

    assert report.ok is True
    assert report.classified_matches == 3


def test_wheel_and_sdist_scan_generated_metadata(tmp_path: Path) -> None:
    _write_policy(tmp_path)
    _tracked_repo(tmp_path)
    wheel = tmp_path / "fieldkit_cli-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, mode="w") as archive:
        archive.writestr("fieldkit_cli-1.0.0.dist-info/METADATA", "private-org-token\n")
    sdist = tmp_path / "fieldkit_cli-1.0.0.tar.gz"
    with tarfile.open(sdist, mode="w:gz") as archive:
        payload = b"private-org-token\n"
        info = tarfile.TarInfo("fieldkit_cli-1.0.0/PKG-INFO")
        info.size = len(payload)
        archive.addfile(info, BytesIO(payload))

    report = check_public_identity.validate(tmp_path, (wheel, sdist))

    assert report.ok is False
    assert {finding.path for finding in report.findings} == {
        f"artifact:{wheel.name}:fieldkit_cli-1.0.0.dist-info/METADATA",
        f"artifact:{sdist.name}:fieldkit_cli-1.0.0/PKG-INFO",
    }


def test_validate_rejects_oversized_in_scope_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_policy(tmp_path)
    source = tmp_path / "src" / "example.py"
    source.parent.mkdir(parents=True)
    source.write_text("private-org-token\n", encoding="utf-8")
    _tracked_repo(tmp_path)
    monkeypatch.setattr(check_public_identity, "_MAX_TEXT_BYTES", 4)

    with pytest.raises(ValueError, match="oversized in-scope text"):
        check_public_identity.validate(tmp_path)


def test_validate_rejects_non_utf8_in_scope_text(tmp_path: Path) -> None:
    _write_policy(tmp_path)
    source = tmp_path / "src" / "example.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"private-org-token\xff\n")
    _tracked_repo(tmp_path)

    with pytest.raises(ValueError, match="non-UTF-8 in-scope text"):
        check_public_identity.validate(tmp_path)


def test_validate_skips_tracked_files_deleted_from_candidate_tree(tmp_path: Path) -> None:
    """A clean-history deletion cannot crash the working-tree privacy scan."""
    _write_policy(tmp_path)
    source = tmp_path / "src" / "private_history.md"
    source.parent.mkdir(parents=True)
    source.write_text("private-org-token\n", encoding="utf-8")
    _tracked_repo(tmp_path)
    source.unlink()

    report = check_public_identity.validate(tmp_path)

    assert report.ok is True
    assert report.scanned_files == 0


def test_live_policy_does_not_allow_organization_identity_exceptions() -> None:
    """Organization markers may appear only in their dedicated detector implementations."""
    policy = check_public_identity.load_policy(check_public_identity.REPO_ROOT)

    assert all(
        allowance.classification == "release_cutover_policy"
        for allowance in policy.allowances
        if allowance.rule_id == "PUBID003"
    )


@pytest.mark.parametrize(
    ("rule_id", "private_marker"),
    [
        ("PUBID006", "SI" + "PH"),
        ("PUBID007", "this " + "org's deployment"),
        ("PUBID009", "rh" + "-colleague"),
    ],
)
def test_policy_rejects_deployment_specific_markers(rule_id: str, private_marker: str) -> None:
    policy = check_public_identity.load_policy(check_public_identity.REPO_ROOT)
    rule = next(rule for rule in policy.rules if rule.rule_id == rule_id)

    assert rule.pattern.search(private_marker) is not None


def test_main_json_output_is_versioned_and_payload_safe(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_policy(tmp_path)
    source = tmp_path / "src" / "example.py"
    source.parent.mkdir(parents=True)
    source.write_text("private-org-token\n", encoding="utf-8")
    _tracked_repo(tmp_path)

    result = check_public_identity.main(["--repo-root", str(tmp_path), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert result == 1
    assert payload["schema_version"] == 1
    assert payload["status"] == "fail"
    assert "private-org-token" not in json.dumps(payload)
