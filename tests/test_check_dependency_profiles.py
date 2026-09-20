"""Failure-capable contracts for the dependency-profile ownership gate."""

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "check_dependency_profiles.py"


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_dependency_profiles", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


@pytest.fixture
def profile_repo(tmp_path: Path) -> Path:
    for relative_path in (
        Path("docs/release-readiness/dependency-ownership.json"),
        Path("pyproject.toml"),
        Path("src/fieldkit/__main__.py"),
    ):
        destination = tmp_path / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_REPO_ROOT / relative_path, destination)
    return tmp_path


def _manifest(repo_root: Path) -> dict[str, object]:
    return json.loads((repo_root / checker.MANIFEST_PATH).read_text(encoding="utf-8"))


def _write_manifest(repo_root: Path, manifest: dict[str, object]) -> None:
    (repo_root / checker.MANIFEST_PATH).write_text(json.dumps(manifest), encoding="utf-8")


def test_live_dependency_profile_contract_passes() -> None:
    report = checker.validate(_REPO_ROOT)

    assert report.ok
    assert report.findings == ()


def test_unowned_third_party_import_fails_with_module_name(profile_repo: Path) -> None:
    unowned_module = profile_repo / "src/fieldkit/unowned.py"
    unowned_module.write_text("import pandas\n", encoding="utf-8")

    report = checker.validate(profile_repo)

    assert report.findings
    assert any(finding.rule_id == "DEP012" and finding.subject == "pandas" for finding in report.findings)


def test_requirement_missing_from_metadata_fails(profile_repo: Path) -> None:
    pyproject = profile_repo / checker.PYPROJECT_PATH
    pyproject.write_text(pyproject.read_text(encoding="utf-8").replace('    "httpx>=0.25",\n', ""), encoding="utf-8")

    report = checker.validate(profile_repo)

    assert report.findings
    assert any(
        finding.rule_id == "DEP005" and finding.subject == "base" and "httpx" in finding.message
        for finding in report.findings
    )


def test_duplicate_import_root_owner_fails(profile_repo: Path) -> None:
    manifest = _manifest(profile_repo)
    profiles = manifest["profiles"]
    assert isinstance(profiles, dict)
    web = profiles["web"]
    assert isinstance(web, dict)
    roots = web["import_roots"]
    assert isinstance(roots, list)
    roots.append("httpx")
    _write_manifest(profile_repo, manifest)

    report = checker.validate(profile_repo)

    assert report.findings
    assert any(finding.rule_id == "DEP003" and finding.subject == "httpx" for finding in report.findings)


def test_all_extra_must_equal_optional_requirement_union(profile_repo: Path) -> None:
    pyproject = profile_repo / checker.PYPROJECT_PATH
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace(
            '    "uvicorn>=0.30",\n]\n\n[project.scripts]', "]\n\n[project.scripts]"
        ),
        encoding="utf-8",
    )

    report = checker.validate(profile_repo)

    assert report.findings
    assert any(finding.rule_id == "DEP008" and finding.subject == "metadata all" for finding in report.findings)


def test_command_manifest_drift_fails(profile_repo: Path) -> None:
    manifest = _manifest(profile_repo)
    command_groups = manifest["command_groups"]
    assert isinstance(command_groups, dict)
    del command_groups["web"]
    _write_manifest(profile_repo, manifest)

    report = checker.validate(profile_repo)

    assert report.findings
    assert any(finding.rule_id == "DEP009" and "web" in finding.message for finding in report.findings)


def test_dispatcher_profile_roots_must_match_manifest(profile_repo: Path) -> None:
    dispatcher = profile_repo / checker.DISPATCHER_PATH
    dispatcher.write_text(
        dispatcher.read_text(encoding="utf-8").replace(
            '"meeting": (\n        "google",',
            '"meeting": (\n        "web",',
        ),
        encoding="utf-8",
    )

    report = checker.validate(profile_repo)

    assert report.findings
    assert any(finding.rule_id == "DEP013" and finding.subject == "meeting" for finding in report.findings)


def test_json_cli_failure_is_machine_readable(profile_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (profile_repo / "src/fieldkit/unowned.py").write_text("import pandas\n", encoding="utf-8")

    exit_code = checker.main(["--repo-root", str(profile_repo), "--json"])

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "fail"
    assert payload["findings"][0]["rule_id"] == "DEP012"
