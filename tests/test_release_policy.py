"""Contracts for the public release policy and local release identity."""

import json
import shutil
from pathlib import Path

import _release_policy as checker
import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def release_policy_repo(tmp_path: Path) -> Path:
    for relative in checker.REPOSITORY_POLICY_PATHS:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_REPO_ROOT / relative, destination)
    return tmp_path


def test_live_release_policy_passes() -> None:
    """The checked-in public release contract agrees with package metadata."""
    report = checker.validate_repository(_REPO_ROOT)
    policy = checker.load_policy(_REPO_ROOT / checker.POLICY_PATH)

    assert report.ok
    assert report.findings == ()
    assert policy.first_public_version == "1.0.0"
    assert policy.supported_line == "latest-patch-of-latest-minor"
    assert policy.required_assets == ("wheel", "sdist", "cyclonedx-json", "sha256sums", "release-evidence")


def test_policy_rejects_unknown_schema_key(release_policy_repo: Path) -> None:
    """Unimplemented release claims cannot enter policy as ignored fields."""
    policy_path = release_policy_repo / checker.POLICY_PATH
    payload = json.loads(policy_path.read_text(encoding="utf-8"))
    payload["unreviewed"] = True
    policy_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="keys must be exactly"):
        checker.load_policy(policy_path)


def test_package_version_must_match_first_public_version(release_policy_repo: Path) -> None:
    """The release policy cannot claim 1.0 while package metadata emits another version."""
    pyproject = release_policy_repo / checker.PYPROJECT_PATH
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace('version = "1.0.0"', 'version = "1.0.1"'), encoding="utf-8"
    )

    report = checker.validate_repository(release_policy_repo)

    assert not report.ok
    assert any(finding.criterion_id == "REL101" for finding in report.findings)


def test_public_contract_cannot_drop_exit_codes(release_policy_repo: Path) -> None:
    """A policy edit cannot silently remove an already-ratified compatibility surface."""
    policy_path = release_policy_repo / checker.POLICY_PATH
    payload = json.loads(policy_path.read_text(encoding="utf-8"))
    payload["compatibility"]["public_contract"].remove("exit-codes")
    policy_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="public_contract"):
        checker.load_policy(policy_path)


def test_release_assets_require_provenance_and_consumer_files(release_policy_repo: Path) -> None:
    """Wheel-only publication cannot satisfy provenance or consumer verification."""
    policy_path = release_policy_repo / checker.POLICY_PATH
    payload = json.loads(policy_path.read_text(encoding="utf-8"))
    payload["artifacts"]["required"] = ["wheel", "sdist"]
    policy_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=r"artifacts\.required"):
        checker.load_policy(policy_path)


def test_recovery_policy_forbids_overwrite_and_version_reuse(release_policy_repo: Path) -> None:
    """A defective public release remains immutable and is replaced by a successor."""
    policy_path = release_policy_repo / checker.POLICY_PATH
    payload = json.loads(policy_path.read_text(encoding="utf-8"))
    payload["recovery"]["allow_overwrite"] = True
    policy_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="recovery"):
        checker.load_policy(policy_path)


def test_report_json_is_stable_and_names_drift(release_policy_repo: Path) -> None:
    """Automation receives a versioned, criterion-addressable release-policy result."""
    pyproject = release_policy_repo / checker.PYPROJECT_PATH
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace('name = "fieldkit-cli"', 'name = "other"'), encoding="utf-8"
    )

    payload = checker.validate_repository(release_policy_repo).to_dict()

    assert payload["schema_version"] == 1
    assert payload["status"] == "fail"
    assert payload["findings"] == [
        {
            "criterion_id": "REL100",
            "message": "project distribution must be fieldkit-cli",
            "subject": "pyproject.toml",
        }
    ]
