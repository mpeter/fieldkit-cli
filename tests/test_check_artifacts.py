"""Contracts for fail-closed release artifact validation."""

import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "check_artifacts.py"


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_artifacts", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


@pytest.fixture
def artifact_repo(tmp_path: Path) -> Path:
    for relative in (checker.POLICY_PATH, checker.PYPROJECT_PATH):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_REPO_ROOT / relative, destination)
    for source in _REPO_ROOT.glob("src/fieldkit/**/*"):
        if source.is_file() and source.name != "AGENTS.md" and "__pycache__" not in source.parts:
            destination = tmp_path / source.relative_to(_REPO_ROOT)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, timeout=10)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, timeout=10)
    subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "commit", "-qm", "fixture"],
        cwd=tmp_path,
        check=True,
        timeout=10,
    )
    return tmp_path


def _metadata(repo_root: Path) -> bytes:
    project = checker.tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return (
        f"Metadata-Version: 2.4\nName: {project['name']}\nVersion: {project['version']}\n"
        f"License-Expression: {project['license']}\nDescription-Content-Type: text/markdown\n\n# fieldkit\n"
    ).encode()


def _wheel(repo_root: Path, destination: Path, *, omit: str | None = None, extra: str | None = None) -> None:
    with zipfile.ZipFile(destination, mode="w") as archive:
        for source in repo_root.glob("src/fieldkit/**/*"):
            if source.is_file() and source.name != "AGENTS.md" and "__pycache__" not in source.parts:
                name = source.relative_to(repo_root / "src").as_posix()
                if name != omit:
                    archive.write(source, name)
        archive.writestr("fieldkit_cli-1.0.0.dist-info/METADATA", _metadata(repo_root))
        archive.writestr("fieldkit_cli-1.0.0.dist-info/WHEEL", "Wheel-Version: 1.0\n")
        if extra is not None:
            archive.writestr(extra, "private payload is not reported")


def _sdist(repo_root: Path, destination: Path, *, extra: str | None = None) -> None:
    root = "fieldkit_cli-1.0.0"
    with tarfile.open(destination, mode="w:gz") as archive:
        for relative in (Path("LICENSE"), Path("README.md"), Path("pyproject.toml")):
            source = _REPO_ROOT / relative
            archive.add(source, f"{root}/{relative.as_posix()}")
        for source in repo_root.glob("src/fieldkit/**/*"):
            if source.is_file() and source.name != "AGENTS.md" and "__pycache__" not in source.parts:
                archive.add(source, f"{root}/{source.relative_to(repo_root).as_posix()}")
        payload = _metadata(repo_root)
        info = tarfile.TarInfo(f"{root}/PKG-INFO")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
        if extra is not None:
            extra_payload = b"not part of the public artifact contract"
            extra_info = tarfile.TarInfo(f"{root}/{extra}")
            extra_info.size = len(extra_payload)
            archive.addfile(extra_info, io.BytesIO(extra_payload))


def test_valid_wheel_and_sdist_report_revision_digest_and_criteria(artifact_repo: Path, tmp_path: Path) -> None:
    """A clean candidate records immutable identity and passes every payload rule."""
    wheel = tmp_path / "fieldkit_cli-1.0.0-py3-none-any.whl"
    sdist = tmp_path / "fieldkit_cli-1.0.0.tar.gz"
    _wheel(artifact_repo, wheel)
    _sdist(artifact_repo, sdist)

    report = checker.validate((wheel, sdist), artifact_repo)

    assert report.ok
    payload = report.to_dict()
    assert len(payload["source_revision"]) == 40
    assert {artifact["kind"] for artifact in payload["artifacts"]} == {"wheel", "sdist"}
    assert all(len(artifact["sha256"]) == 64 for artifact in payload["artifacts"])


def test_core_metadata_23_uses_the_declared_legacy_license_field(artifact_repo: Path, tmp_path: Path) -> None:
    project = checker.tomllib.loads((artifact_repo / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    metadata = (
        f"Metadata-Version: 2.3\nName: {project['name']}\nVersion: {project['version']}\n"
        f"License: {project['license']}\nDescription-Content-Type: text/markdown\n\n# fieldkit\n"
    ).encode()
    wheel = tmp_path / "fieldkit_cli-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, mode="w") as archive:
        archive.writestr("fieldkit_cli-1.0.0.dist-info/METADATA", metadata)
        archive.writestr("fieldkit_cli-1.0.0.dist-info/WHEEL", "Wheel-Version: 1.0\n")
        for source in artifact_repo.glob("src/fieldkit/**/*"):
            if source.is_file() and source.name != "AGENTS.md" and "__pycache__" not in source.parts:
                archive.write(source, source.relative_to(artifact_repo / "src").as_posix())

    report = checker.validate((wheel,), artifact_repo)

    assert report.ok


def test_missing_required_sql_names_archive_path(artifact_repo: Path, tmp_path: Path) -> None:
    """Required runtime SQL must survive packaging under its expected path."""
    wheel = tmp_path / "fieldkit_cli-1.0.0-py3-none-any.whl"
    _wheel(artifact_repo, wheel, omit="fieldkit/ingest/schema.sql")

    report = checker.validate((wheel,), artifact_repo)

    criterion = next(item for item in report.artifacts[0].criteria if item.criterion_id == "ART104")
    assert criterion.status == "fail"
    assert criterion.diagnostics == ("missing fieldkit/ingest/schema.sql",)


def test_forbidden_agent_state_reports_path_not_payload(artifact_repo: Path, tmp_path: Path) -> None:
    """Private agent state fails safely without copying its contents into logs."""
    wheel = tmp_path / "fieldkit_cli-1.0.0-py3-none-any.whl"
    _wheel(artifact_repo, wheel, extra="fieldkit/ingest/AGENTS.md")

    report = checker.validate((wheel,), artifact_repo)
    payload = json.dumps(report.to_dict())

    criterion = next(item for item in report.artifacts[0].criteria if item.criterion_id == "ART211")
    assert criterion.status == "fail"
    assert "fieldkit/ingest/AGENTS.md" in payload
    assert "private payload" not in payload


def test_unexpected_sdist_path_fails_closed(artifact_repo: Path, tmp_path: Path) -> None:
    """An undeclared source-distribution member is a release-blocking failure."""
    sdist = tmp_path / "fieldkit_cli-1.0.0.tar.gz"
    _sdist(artifact_repo, sdist, extra="docs/internal-plan.md")

    report = checker.validate((sdist,), artifact_repo)

    criterion = next(item for item in report.artifacts[0].criteria if item.criterion_id == "ART200")
    assert criterion.status == "fail"
    assert criterion.diagnostics == ("unexpected path docs/internal-plan.md",)


def test_untracked_token_inside_package_fails_closed_without_exposing_payload(
    artifact_repo: Path, tmp_path: Path
) -> None:
    """A plausible token path cannot hide inside the broad package namespace."""
    wheel = tmp_path / "fieldkit_cli-1.0.0-py3-none-any.whl"
    _wheel(artifact_repo, wheel, extra="fieldkit/_data/token.json")

    report = checker.validate((wheel,), artifact_repo)

    assert report.ok is False
    payload = json.dumps(report.to_dict())
    tracked = next(item for item in report.artifacts[0].criteria if item.criterion_id == "ART220")
    credential = next(item for item in report.artifacts[0].criteria if item.criterion_id == "ART221")
    assert tracked.diagnostics == ("untracked package path fieldkit/_data/token.json",)
    assert credential.status == "fail"
    assert "private payload" not in payload


def test_json_cli_is_machine_readable_on_failure(
    artifact_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wheel = tmp_path / "fieldkit_cli-1.0.0-py3-none-any.whl"
    _wheel(artifact_repo, wheel, omit="fieldkit/ingest/schema.sql")

    exit_code = checker.main(["--repo-root", str(artifact_repo), "--json", str(wheel)])

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["status"] == "fail"


def test_diagnostics_are_bounded() -> None:
    """Artifact failures remain useful without overwhelming CI logs."""
    criterion = checker._criterion("ART999", [f"finding {index}" for index in range(30)])

    assert criterion.status == "fail"
    assert len(criterion.diagnostics) == 21
    assert criterion.diagnostics[-1] == "... 10 additional finding(s) omitted"
