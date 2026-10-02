"""End-to-end contracts for verified-export release candidates."""

import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from scripts import check_public_candidate

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True, timeout=10)
    return result.stdout.strip()


def _write_candidate_repository(repo: Path) -> str:
    """Create a minimal committed package with the candidate-check policies."""
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Example Maintainer")
    _git(repo, "config", "user.email", "maintainer@example.com")
    (repo / "src" / "fieldkit").mkdir(parents=True)
    (repo / "src" / "fieldkit" / "__init__.py").write_text('__version__ = "1.0.0"\n', encoding="utf-8")
    (repo / "README.md").write_text("# fieldkit\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        """\
[build-system]
requires = ["hatchling==1.26.3"]
build-backend = "hatchling.build"

[project]
name = "fieldkit-cli"
version = "1.0.0"
description = "Example package"
readme = "README.md"
requires-python = ">=3.11"
license = "Apache-2.0"

[tool.hatch.build.targets.wheel]
packages = ["src/fieldkit"]

""",
        encoding="utf-8",
    )
    release_docs = repo / "docs" / "release-readiness"
    release_docs.mkdir(parents=True)
    (release_docs / "artifact-policy.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "required_families": [
                    {"id": "ART101", "name": "python-package", "source_globs": ["src/fieldkit/**/*.py"]}
                ],
                "allowed_paths": {
                    "wheel": ["fieldkit/**", "fieldkit_cli-*.dist-info/**"],
                    "sdist": [
                        "README.md",
                        "pyproject.toml",
                        "uv.lock",
                        "PKG-INFO",
                        "src/fieldkit/**",
                        "scripts/**",
                        "docs/release-readiness/**",
                    ],
                },
                "tracked_payload": {"id": "ART220", "source_root": "src/fieldkit"},
                "forbidden_paths": [{"id": "ART201", "glob": "**/.git/**", "category": "history"}],
            }
        ),
        encoding="utf-8",
    )
    (release_docs / "public-tree-scan-policy.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "max_text_bytes": 65536,
                "archives": {
                    "max_members": 1000,
                    "max_member_bytes": 65536,
                    "max_total_bytes": 1048576,
                    "max_archive_bytes": 1048576,
                },
                "text_rules": [{"id": "SECRET001", "category": "credential", "pattern": "example-secret-[0-9]{4}"}],
                "text_allowances": [],
                "binary_allowances": [],
            }
        ),
        encoding="utf-8",
    )
    (release_docs / "public-identity-policy.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "scope": ["src/**"],
                "rules": [{"id": "IDENTITY001", "category": "identity", "pattern": "private[-]identity"}],
                "allowances": [],
            }
        ),
        encoding="utf-8",
    )
    (release_docs / "dependency-security-policy.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "vulnerability_threshold": "low",
                "allowed_spdx_licenses": ["MIT"],
                "unknown_license_policy": "fail-unless-excepted",
                "license_exceptions": [],
            }
        ),
        encoding="utf-8",
    )
    scripts = repo / "scripts"
    scripts.mkdir()
    for script_name in ("_supply_chain_policy.py", "check_supply_chain_policy.py"):
        shutil.copy2(Path(__file__).resolve().parent.parent / "scripts" / script_name, scripts / script_name)
    (release_docs / "public-tree-policy.json").write_text(
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
                        "patterns": ["**"],
                        "rationale": "Every minimal candidate file is public.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(["uv", "lock", "--offline"], cwd=repo, check=True, capture_output=True, text=True, timeout=30)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "candidate")
    return _git(repo, "rev-parse", "HEAD")


def test_candidate_build_uses_a_zip_compatible_deterministic_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def build(command: list[str], **kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        dist_dir = Path(command[-1])
        dist_dir.mkdir()
        (dist_dir / "fieldkit_cli-1.0.0-py3-none-any.whl").write_bytes(b"wheel")
        (dist_dir / "fieldkit_cli-1.0.0.tar.gz").write_bytes(b"sdist")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(check_public_candidate.subprocess, "run", build)

    artifacts = check_public_candidate._build_export(tmp_path, tmp_path / "dist")

    assert len(artifacts) == 2
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert environment["SOURCE_DATE_EPOCH"] == "315532800"


def test_candidate_builds_and_scans_only_the_verified_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "source"
    repo.mkdir()
    commit = _write_candidate_repository(repo)
    (repo / "src" / "fieldkit" / "__init__.py").write_text('token = "example-secret-1234"\n', encoding="utf-8")

    def collect(*_requirements: Path, wheelhouse: Path) -> dict[str, object]:
        wheelhouse.mkdir()
        for target in check_public_candidate.release_wheelhouse.supported_targets():
            target_dir = wheelhouse / target.name
            target_dir.mkdir()
            (target_dir / "example-1.0.0-py3-none-any.whl").write_bytes(b"wheel")
        (wheelhouse / "runtime-wheelhouse.json").write_text('{"schema_version": 1}\n', encoding="utf-8")
        return {"schema_version": 1}

    def archive_wheelhouse(_wheelhouse: Path, destination: Path) -> str:
        payload = b"wheelhouse"
        destination.write_bytes(payload)
        return hashlib.sha256(payload).hexdigest()

    monkeypatch.setattr(check_public_candidate.release_wheelhouse, "collect", collect)
    monkeypatch.setattr(check_public_candidate.release_wheelhouse, "archive", archive_wheelhouse)

    def license_evidence(
        _export_dir: Path, staging: Path, manifest: check_public_candidate.export_public_tree.ExportManifest
    ) -> dict[str, object]:
        (staging / "runtime-requirements.txt").write_text("click==8.5.0\n", encoding="utf-8")
        (staging / "release-build-requirements.txt").write_text("hatchling==1.26.3\n", encoding="utf-8")
        sbom = b'{"components": []}\n'
        (staging / "locked-graph.cdx.json").write_bytes(sbom)
        return {
            "schema_version": 1,
            "status": "pass",
            "scope": "runtime-all-extras",
            "revision": manifest.source_commit,
            "export_policy_sha256": manifest.policy_sha256,
            "sbom_sha256": hashlib.sha256(sbom).hexdigest(),
            "observed_packages": 0,
            "packages": [],
            "findings": [],
        }

    monkeypatch.setattr(check_public_candidate, "_license_evidence", license_evidence)

    report = check_public_candidate.check_candidate(
        repo,
        commit,
        expected_repository="example/fieldkit-cli",
        planned_tag="v1.0.0",
        output_dir=tmp_path / "candidate",
    )

    assert report.ok is True
    assert report.scan.source_commit == commit
    assert report.package == "fieldkit-cli"
    assert report.to_dict()["schema_version"] == 6
    assert {artifact.kind for artifact in report.artifact_validation.artifacts} == {"wheel", "sdist"}
    assert (tmp_path / "candidate" / "bundle" / "bundle-provenance.json").is_file()
    wheel = next((tmp_path / "candidate" / "dist").glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        assert archive.read("fieldkit/__init__.py") == b'__version__ = "1.0.0"\n'


def test_candidate_rejects_unexpected_public_repository_before_creating_output(tmp_path: Path) -> None:
    repo = tmp_path / "source"
    repo.mkdir()
    commit = _write_candidate_repository(repo)
    output_dir = tmp_path / "candidate"

    with pytest.raises(ValueError, match="expected repository"):
        check_public_candidate.check_candidate(
            repo,
            commit,
            expected_repository="other/fieldkit-cli",
            planned_tag="v1.0.0",
            output_dir=output_dir,
        )

    assert output_dir.exists() is False


def test_reproducibility_check_rejects_different_artifact_bytes(tmp_path: Path) -> None:
    """A second build is evidence only when both named artifacts have identical bytes."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    first_artifact = first / "fieldkit_cli-1.0.0-py3-none-any.whl"
    second_artifact = second / first_artifact.name
    first_artifact.write_bytes(b"first")
    second_artifact.write_bytes(b"second")

    with pytest.raises(ValueError, match="byte-for-byte"):
        check_public_candidate._require_reproducible_builds((first_artifact,), (second_artifact,))


def test_runtime_license_evidence_covers_every_optional_profile_but_excludes_dev_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runtime license proof covers every installable extra without inheriting QA-only dependencies."""
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    manifest = cast(
        check_public_candidate.export_public_tree.ExportManifest,
        SimpleNamespace(source_commit="a" * 40, policy_sha256="b" * 64),
    )
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[:2] == ["uv", "export"]:
            output = Path(command[command.index("--output-file") + 1])
            if "cyclonedx1.5" in command:
                output.write_text(
                    json.dumps({"components": [{"purl": "pkg:pypi/example-base@2.0.0"}]}), encoding="utf-8"
                )
            else:
                output.write_text("example-base==2.0.0\n", encoding="utf-8")
        elif command[:2] == ["uv", "venv"]:
            environment_dir = Path(command[-1])
            python = environment_dir / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.touch()
        elif command[:2] != ["uv", "sync"]:
            output = Path(command[command.index("--output") + 1])
            output.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "pass",
                        "scope": "runtime-all-extras",
                        "revision": manifest.source_commit,
                        "export_policy_sha256": manifest.policy_sha256,
                        "sbom_sha256": "c" * 64,
                        "observed_packages": 1,
                        "packages": [{"package_url": "pkg:pypi/example-base@2.0.0"}],
                        "findings": [],
                    }
                ),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(check_public_candidate.subprocess, "run", run)

    evidence = check_public_candidate._license_evidence(export_dir, staging, manifest)

    assert evidence["status"] == "pass"
    export_commands = [command for command in commands if command[:2] == ["uv", "export"]]
    assert len(export_commands) == 4
    assert all("--no-dev" in command for command in export_commands[:3])
    assert "--all-extras" in export_commands[0]
    assert "--all-extras" in export_commands[1]
    assert "--all-extras" not in export_commands[2]
    assert "--only-group" in export_commands[3]
    assert "release-build" in export_commands[3]
    sync_command = next(command for command in commands if command[:2] == ["uv", "sync"])
    assert "--no-dev" in sync_command
    assert "--all-extras" in sync_command


@pytest.mark.parametrize("version", ["1.0.0", "1.0.1", "2.0.0"])
def test_package_identity_binds_stable_version_to_tag(tmp_path: Path, version: str) -> None:
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "fieldkit-cli"\nversion = "{version}"\n', encoding="utf-8"
    )
    assert check_public_candidate._package_identity(tmp_path, f"v{version}") == "fieldkit-cli"


@pytest.mark.parametrize("version", ["1.0.0", "1.0.2", "1.0.1rc1", "01.0.1", "1.0.1+local"])
def test_package_identity_rejects_mismatched_version(tmp_path: Path, version: str) -> None:
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "fieldkit-cli"\nversion = "{version}"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match=r"project\.version does not match"):
        check_public_candidate._package_identity(tmp_path, "v1.0.1")


def test_candidate_rejects_stale_project_version_before_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "source"
    repo.mkdir()
    _write_candidate_repository(repo)
    policy = repo / "docs/release-readiness/public-tree-policy.json"
    policy.write_text(policy.read_text().replace("v1.0.0", "v1.0.1"), encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "select successor")
    revision = _git(repo, "rev-parse", "HEAD")

    def forbidden_build(*_args: object) -> tuple[Path, ...]:
        pytest.fail("mismatched package must be rejected before any build")

    monkeypatch.setattr(check_public_candidate, "_build_export", forbidden_build)
    output = tmp_path / "candidate"
    with pytest.raises(ValueError, match=r"project\.version does not match"):
        check_public_candidate.check_candidate(
            repo, revision, expected_repository="example/fieldkit-cli", planned_tag="v1.0.1", output_dir=output
        )
    assert not output.exists()
