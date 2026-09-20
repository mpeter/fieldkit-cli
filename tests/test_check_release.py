"""CLI contracts for fail-closed release-policy validation."""

import json
import shutil
from pathlib import Path

import _release_policy as policy_check
import check_release
import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _release_repo(tmp_path: Path) -> Path:
    for relative in policy_check.REPOSITORY_POLICY_PATHS:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_REPO_ROOT / relative, destination)
    return tmp_path


def test_json_cli_passes_with_versioned_evidence(capsys: pytest.CaptureFixture[str]) -> None:
    """Automation can consume a stable policy result from the checked-in tree."""
    result = check_release.main(["policy", "--repo-root", str(_REPO_ROOT), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload == {"findings": [], "schema_version": 1, "status": "pass"}


def test_cli_returns_one_for_policy_drift(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A valid but inconsistent release policy is a failing gate."""
    repo = _release_repo(tmp_path)
    pyproject = repo / policy_check.PYPROJECT_PATH
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace('license = "Apache-2.0"', 'license = "MIT"'), encoding="utf-8"
    )

    result = check_release.main(["policy", "--repo-root", str(repo)])

    output = capsys.readouterr()
    assert result == 1
    assert "REL103" in output.out
    assert output.err == ""


def test_cli_returns_two_for_invalid_schema(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Unreadable policy state fails as an execution error, not as a clean report."""
    repo = _release_repo(tmp_path)
    (repo / policy_check.POLICY_PATH).write_text("{}\n", encoding="utf-8")

    result = check_release.main(["policy", "--repo-root", str(repo), "--json"])

    output = capsys.readouterr()
    assert result == 2
    assert output.out == ""
    assert "keys must be exactly" in output.err
