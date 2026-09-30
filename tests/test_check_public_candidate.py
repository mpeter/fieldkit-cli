"""End-to-end contracts for verified-export release candidates."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import venv
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest

from fieldkit.util.bounded_process import BoundedProcessBytesResult
from scripts import (
    check_public_candidate,
    export_public_tree,
    release_approval_archive,
    release_bundle,
    release_wheelhouse,
    runtime_license_inventory,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_runtime_observer_bounds_real_child_output(tmp_path: Path, stream: str) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    limit = 2 * 1024 * 1024 if stream == "stdout" else 16 * 1024
    (scripts / "runtime_license_inventory.py").write_text(
        f"import sys, time\nsys.{stream}.buffer.write(b'x' * {limit + 1})\nsys.{stream}.buffer.flush()\ntime.sleep(30)\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="exceeded"):
        check_public_candidate._observe_runtime_licenses(Path(sys.executable), tmp_path, dict(os.environ))


def test_isolated_candidate_controller_loads_installed_base_without_candidate_root(tmp_path: Path) -> None:
    sentinel = tmp_path / "shadow-imported"
    (tmp_path / "fieldkit.py").write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\n", encoding="utf-8"
    )
    result = subprocess.run(
        [sys.executable, "-I", str(Path(check_public_candidate.__file__).resolve()), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert "--revision" in result.stdout
    assert not sentinel.exists()


_MARKER_ENVIRONMENT = {
    "implementation_name": "cpython",
    "implementation_version": "3.11.8",
    "os_name": "posix",
    "platform_machine": "x86_64",
    "platform_python_implementation": "CPython",
    "platform_release": "6.0.0",
    "platform_system": "Linux",
    "platform_version": "Example kernel",
    "python_full_version": "3.11.8",
    "python_version": "3.11",
    "sys_platform": "linux",
}


def _license_inputs(staging: Path) -> dict[str, object]:
    inventory = runtime_license_inventory
    observations = json.dumps(
        {
            "schema_version": 1,
            "packages": [{"name": "example-base", "version": "2.0.0", "license_expression": "MIT"}],
            "marker_environment": _MARKER_ENVIRONMENT,
        }
    ).encode("utf-8")
    requirements = b"example-base==2.0.0\n"
    (staging / inventory.OBSERVATIONS_NAME).write_bytes(observations)
    (staging / inventory.PLATFORM_REQUIREMENTS_NAME).write_bytes(requirements)
    return {
        "observations": {"name": inventory.OBSERVATIONS_NAME, "sha256": hashlib.sha256(observations).hexdigest()},
        "platform_requirements": {
            "name": inventory.PLATFORM_REQUIREMENTS_NAME,
            "sha256": hashlib.sha256(requirements).hexdigest(),
        },
        "marker_environment": _MARKER_ENVIRONMENT,
    }


@pytest.mark.parametrize("private", [False, True])
def test_runtime_observations_apply_public_identity_rules(tmp_path: Path, private: bool) -> None:
    release_docs = tmp_path / "docs" / "release-readiness"
    release_docs.mkdir(parents=True)
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
    contents = json.dumps(
        {
            "marker_environment": {
                **_MARKER_ENVIRONMENT,
                "platform_version": "private-identity" if private else "Example kernel",
            }
        }
    ).encode("utf-8")

    if private:
        with pytest.raises(ValueError, match="unapproved private or sensitive data"):
            check_public_candidate._validate_observation_privacy(tmp_path, contents)
    else:
        check_public_candidate._validate_observation_privacy(tmp_path, contents)


@pytest.mark.parametrize("destination", ["recognized-environment", "dangling-symlink"])
def test_license_environment_refuses_occupied_destination_without_changing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    destination: str,
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    environment = staging / "license-environment"
    if destination == "recognized-environment":
        venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
        (environment / "sentinel").write_bytes(b"unrelated")
        before = (environment / "pyvenv.cfg").read_bytes()
    else:
        environment.symlink_to(tmp_path / "missing", target_is_directory=True)
        before = b""
    manifest = export_public_tree.ExportManifest(
        1, "a" * 40, "b" * 40, "policy.json", "c" * 40, "d" * 64, "example/fieldkit", "v1.0.0", "e" * 40, (), ()
    )
    real_run = subprocess.run
    creations: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["uv", "export"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ["uv", "venv"]:
            creations.append(command)
            return real_run(
                [*command, "--python", sys.executable, "--no-python-downloads", "--no-config"],
                cwd=tmp_path,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        return subprocess.CompletedProcess(command, 1, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ValueError, match="dependency environment must not already exist"):
        check_public_candidate._license_evidence(tmp_path, staging, manifest)
    assert creations == []
    if destination == "recognized-environment":
        assert (environment / "sentinel").read_bytes() == b"unrelated"
        assert (environment / "pyvenv.cfg").read_bytes() == before
    else:
        assert environment.is_symlink()
        assert not (tmp_path / "missing").exists()


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
    for script_name in ("_supply_chain_policy.py", "check_supply_chain_policy.py", "runtime_license_inventory.py"):
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

    monkeypatch.setattr(subprocess, "run", build)

    artifacts = check_public_candidate._build_export(tmp_path, tmp_path / "dist")

    assert len(artifacts) == 2
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert environment["SOURCE_DATE_EPOCH"] == "315532800"


@pytest.mark.parametrize(
    "attack",
    [
        "none",
        "after-materialize",
        "inside-rename",
        "competing-directory",
        "competing-symlink",
        "report-corruption",
        "report-corruption-during-verify",
        "bundle-corruption",
        "root-replacement",
        "bundle-replacement",
    ],
)
def test_candidate_builds_and_scans_only_the_verified_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], attack: str
) -> None:
    repo = tmp_path / "source"
    repo.mkdir()
    commit = _write_candidate_repository(repo)
    (repo / "src" / "fieldkit" / "__init__.py").write_text('token = "example-secret-1234"\n', encoding="utf-8")

    def collect(*_requirements: Path, wheelhouse: Path) -> dict[str, object]:
        wheelhouse.mkdir()
        for target in release_wheelhouse.supported_targets():
            target_dir = wheelhouse / target.name
            target_dir.mkdir()
            (target_dir / "example-1.0.0-py3-none-any.whl").write_bytes(b"wheel")
        (wheelhouse / "runtime-wheelhouse.json").write_text('{"schema_version": 1}\n', encoding="utf-8")
        return {"schema_version": 1}

    def archive_wheelhouse(_wheelhouse: Path, destination: Path) -> str:
        payload = b"wheelhouse"
        destination.write_bytes(payload)
        return hashlib.sha256(payload).hexdigest()

    monkeypatch.setattr(release_wheelhouse, "collect", collect)
    monkeypatch.setattr(release_wheelhouse, "archive", archive_wheelhouse)

    def license_evidence(
        _export_dir: Path, staging: Path, manifest: export_public_tree.ExportManifest
    ) -> dict[str, object]:
        (staging / "runtime-requirements.txt").write_text("click==8.5.0\n", encoding="utf-8")
        (staging / "release-build-requirements.txt").write_text("hatchling==1.26.3\n", encoding="utf-8")
        sbom = b'{"components": [{"purl": "pkg:pypi/example-base@2.0.0"}]}\n'
        (staging / "locked-graph.cdx.json").write_bytes(sbom)
        return {
            "schema_version": 2,
            "status": "pass",
            "scope": "runtime-all-extras",
            "revision": manifest.source_commit,
            "export_policy_sha256": manifest.policy_sha256,
            "sbom_sha256": hashlib.sha256(sbom).hexdigest(),
            "observed_packages": 1,
            "packages": [{"package_url": "pkg:pypi/example-base@2.0.0", "license_expression": "MIT"}],
            "findings": [],
            **_license_inputs(staging),
        }

    monkeypatch.setattr(check_public_candidate, "_license_evidence", license_evidence)

    output_dir = tmp_path / "candidate"
    retained = tmp_path / "retained-original"
    opened: list[int] = []
    genuine_open = os.open

    def track_open(path: str | bytes | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        descriptor = genuine_open(path, flags, mode, dir_fd=dir_fd)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(os, "open", track_open)
    genuine_materialize = release_bundle.materialize
    genuine_rename = release_approval_archive._rename_no_replace_at
    genuine_verify = release_bundle._verify_open_bundle
    competing_identity: list[int] = []
    final_publications: list[str] = []

    def substitute(staging: Path) -> None:
        staging.rename(retained)
        staging.mkdir(mode=0o700)
        (staging / "replacement.txt").write_text("replacement", encoding="utf-8")

    def materialize(staging: Path) -> release_bundle.BundleReport:
        result = genuine_materialize(staging)
        if attack == "after-materialize":
            substitute(staging)
        return result

    def rename(parent_fd: int, source: str, destination: str) -> None:
        if destination != output_dir.name:
            genuine_rename(parent_fd, source, destination)
            return
        final_publications.append(destination)
        if attack == "inside-rename":
            substitute(tmp_path / source)
        elif attack == "competing-directory":
            output_dir.mkdir()
            competing_identity.append(output_dir.stat().st_ino)
        elif attack == "competing-symlink":
            output_dir.symlink_to(tmp_path / "missing")
            competing_identity.append(output_dir.lstat().st_ino)
        genuine_rename(parent_fd, source, destination)
        if attack == "report-corruption":
            (output_dir / "report.json").write_bytes(b"corrupt")
        elif attack == "bundle-corruption":
            (output_dir / "bundle" / "runtime-requirements.txt").write_bytes(b"corrupt")

    def verify(
        directory_fd: int,
        *,
        candidate_report: bytes,
        expected_checksums: bytes | None = None,
        expected_anchor: None = None,
        cutover_record: None = None,
    ) -> release_bundle.BundleReport:
        assert expected_anchor is None
        assert cutover_record is None
        result = genuine_verify(
            directory_fd,
            candidate_report=candidate_report,
            expected_checksums=expected_checksums,
            expected_anchor=expected_anchor,
            cutover_record=cutover_record,
        )
        if output_dir.exists() and attack == "root-replacement":
            substitute(output_dir)
        elif output_dir.exists() and attack == "report-corruption-during-verify":
            (output_dir / "report.json").write_bytes(b"corrupt")
        elif output_dir.exists() and attack == "bundle-replacement":
            bundle = output_dir / "bundle"
            bundle.rename(tmp_path / "retained-bundle")
            bundle.mkdir()
        return result

    monkeypatch.setattr(release_bundle, "materialize", materialize)
    monkeypatch.setattr(release_approval_archive, "_rename_no_replace_at", rename)
    monkeypatch.setattr(release_bundle, "_verify_open_bundle", verify)

    if attack != "none":
        result = check_public_candidate.main(
            [
                "--repo",
                str(repo),
                "--revision",
                commit,
                "--expected-repository",
                "example/fieldkit-cli",
                "--planned-tag",
                "v1.0.0",
                "--output-dir",
                str(output_dir),
                "--json",
            ]
        )
        assert result == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "ERROR" in captured.err
        if competing_identity:
            assert output_dir.lstat().st_ino == competing_identity[0]
        if attack in {"after-materialize", "inside-rename", "root-replacement"}:
            assert (retained / "report.json").is_file()
            assert (output_dir / "replacement.txt").read_text(encoding="utf-8") == "replacement"
        if attack == "inside-rename":
            assert final_publications == [output_dir.name]
        for descriptor in set(opened):
            with pytest.raises(OSError, match="Bad file descriptor"):
                os.fstat(descriptor)
        return

    report = check_public_candidate.check_candidate(
        repo,
        commit,
        expected_repository="example/fieldkit-cli",
        planned_tag="v1.0.0",
        output_dir=tmp_path / "candidate",
    )

    assert report.ok is True
    for descriptor in set(opened):
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(descriptor)
    assert report.scan.source_commit == commit
    assert report.package == "fieldkit-cli"
    assert report.to_dict()["schema_version"] == 7
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


def test_candidate_failure_retains_substituted_staging_without_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failure must not recursively delete a replacement at the staging name."""
    output_dir = tmp_path / "candidate"
    retained = tmp_path / "retained-original"
    substituted: list[Path] = []

    def fail_export(
        _repo: Path, _revision: str, _policy: Path, export_dir: Path, _manifest: Path
    ) -> export_public_tree.ExportManifest:
        staging = export_dir.parent
        (staging / "original.txt").write_text("original staging", encoding="utf-8")
        staging.rename(retained)
        staging.mkdir(mode=0o700)
        (staging / "replacement.txt").write_text("unrelated replacement", encoding="utf-8")
        substituted.append(staging)
        raise ValueError("injected candidate failure")

    monkeypatch.setattr(export_public_tree, "export_tree", fail_export)

    result = check_public_candidate.main(
        [
            "--repo",
            str(tmp_path),
            "--revision",
            "HEAD",
            "--expected-repository",
            "example/fieldkit-cli",
            "--planned-tag",
            "v1.0.0",
            "--output-dir",
            str(output_dir),
            "--json",
        ]
    )

    assert result == 2
    assert len(substituted) == 1
    assert (substituted[0] / "replacement.txt").read_text(encoding="utf-8") == "unrelated replacement"
    assert (retained / "original.txt").read_text(encoding="utf-8") == "original staging"
    assert not output_dir.exists()
    assert not (retained / "report.json").exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "injected candidate failure" in captured.err


@pytest.mark.parametrize("replacement", ["none", "directory", "symlink"])
def test_candidate_report_writes_through_retained_staging_descriptor(tmp_path: Path, replacement: str) -> None:
    """A moved staging directory cannot redirect report creation into its replacement."""
    staging = tmp_path / "staging"
    staging.mkdir()
    retained = tmp_path / "retained"
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    sentinel = unrelated / "sentinel.txt"
    sentinel.write_text("unrelated content", encoding="utf-8")
    (staging / "report.tmp").symlink_to(sentinel)
    report = Mock(spec=check_public_candidate.CandidateReport)
    report.to_dict.return_value = {"fictional": "candidate"}
    descriptor = release_approval_archive._open_real_directory(staging, create=False)
    try:
        if replacement != "none":
            staging.rename(retained)
            if replacement == "directory":
                staging.mkdir()
            else:
                staging.symlink_to(unrelated, target_is_directory=True)
        check_public_candidate._write_report(descriptor, report)
        original = staging if replacement == "none" else retained
        assert (original / "report.json").read_bytes() == check_public_candidate._report_bytes(report)
        assert (original / "report.json").stat().st_mode & 0o777 == 0o600
        assert sentinel.read_text(encoding="utf-8") == "unrelated content"
        if replacement != "none":
            assert not (staging / "report.json").exists()
        assert not (unrelated / "report.json").exists()
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("existing", ["symlink", "hardlink", "regular"])
def test_candidate_report_refuses_existing_members(tmp_path: Path, existing: str) -> None:
    """Report creation never truncates an existing file or a linked sentinel."""
    staging = tmp_path / "staging"
    staging.mkdir()
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("unrelated content", encoding="utf-8")
    destination = staging / "report.json"
    if existing == "symlink":
        destination.symlink_to(sentinel)
    elif existing == "hardlink":
        destination.hardlink_to(sentinel)
    else:
        destination.write_text("existing report", encoding="utf-8")
    original_identity = destination.lstat().st_ino
    report = Mock(spec=check_public_candidate.CandidateReport)
    report.to_dict.return_value = {"fictional": "candidate"}
    descriptor = release_approval_archive._open_real_directory(staging, create=False)
    try:
        with pytest.raises(FileExistsError, match="File exists") as error:
            check_public_candidate._write_report(descriptor, report)
        assert error.value.filename == "report.json"
        assert destination.lstat().st_ino == original_identity
        assert destination.read_text(encoding="utf-8") == (
            "existing report" if existing == "regular" else "unrelated content"
        )
        assert sentinel.read_text(encoding="utf-8") == "unrelated content"
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("replacement", ["none", "environment", "staging-directory", "staging-symlink"])
def test_candidate_license_failure_retains_environment_and_replacements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    """License failure cannot remove the current occupant of a mutable environment path."""
    staging = tmp_path / "staging"
    environment = staging / "license-environment"
    environment.mkdir(parents=True)
    (environment / "original.txt").write_text("original environment", encoding="utf-8")
    retained = tmp_path / "retained"
    unrelated = tmp_path / "unrelated"
    manifest = export_public_tree.ExportManifest(
        1, "a" * 40, "b" * 40, "policy.json", "c" * 40, "d" * 64, "example/fieldkit", "v1.0.0", "e" * 40, (), ()
    )

    def fail(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if replacement == "environment":
            environment.rename(retained)
            environment.mkdir()
        elif replacement.startswith("staging-"):
            staging.rename(retained)
            if replacement == "staging-directory":
                environment.mkdir(parents=True)
            else:
                (unrelated / "license-environment").mkdir(parents=True)
                staging.symlink_to(unrelated, target_is_directory=True)
        if replacement != "none":
            (environment / "replacement.txt").write_text("unrelated replacement", encoding="utf-8")
        return subprocess.CompletedProcess(command, 1, "", "")

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(ValueError, match="candidate locked SBOM export failed with exit status 1") as error:
        check_public_candidate._license_evidence(tmp_path, staging, manifest)
    assert str(error.value) == "candidate locked SBOM export failed with exit status 1"
    original = environment if replacement == "none" else retained
    if replacement.startswith("staging-"):
        original /= "license-environment"
    assert (original / "original.txt").read_text(encoding="utf-8") == "original environment"
    if replacement != "none":
        assert (environment / "replacement.txt").read_text(encoding="utf-8") == "unrelated replacement"
    assert not (staging / "report.json").exists()


@pytest.mark.parametrize("override", [None, "UV_VENV_CLEAR", "UV_VENV_ALLOW_EXISTING"])
@pytest.mark.parametrize(
    "attack",
    [
        "none",
        "runtime-observation-failure",
        "qa-sync-failure",
        "receipt-marker-substitution",
        "receipt-symlink",
        "receipt-fifo",
        "receipt-oversize",
    ],
)
def test_runtime_license_evidence_covers_every_optional_profile_but_excludes_dev_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override: str | None,
    attack: str,
) -> None:
    """Runtime license proof covers every installable extra without inheriting QA-only dependencies."""
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    manifest = cast(
        export_public_tree.ExportManifest,
        SimpleNamespace(source_commit="a" * 40, policy_sha256="b" * 64),
    )
    commands: list[list[str]] = []
    (export_dir / "docs" / "release-readiness").mkdir(parents=True)
    (export_dir / "docs" / "release-readiness" / "dependency-security-policy.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(check_public_candidate, "_validate_observation_privacy", lambda _export, _contents: None)
    if override is not None:
        monkeypatch.setenv(override, "1")

    def run(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
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
            assert command[-1] in {str(staging / "license-environment"), str(staging / "license-qa-environment")}
            creation_environment = _kwargs.get("env")
            assert isinstance(creation_environment, dict)
            assert "UV_VENV_CLEAR" not in creation_environment
            assert "UV_VENV_ALLOW_EXISTING" not in creation_environment
            environment_dir = Path(command[-1])
            python = environment_dir / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.touch()
        elif any(argument.endswith("runtime_license_inventory.py") for argument in command):
            observations = json.dumps(
                {
                    "schema_version": 1,
                    "packages": [{"name": "example-base", "version": "2.0.0", "license_expression": "MIT"}],
                    "marker_environment": _MARKER_ENVIRONMENT,
                }
            ).encode("utf-8")
            assert command[0] == str(staging / "license-environment" / "bin" / "python")
            assert command[1] == "-I"
            assert command[2] == "-S"
            assert command[-2:] == ["--environment-root", str(staging / "license-environment")]
            if attack == "runtime-observation-failure":
                return subprocess.CompletedProcess(command, 2, b"", b"observer unavailable")
            return subprocess.CompletedProcess(command, 0, observations, b"")
        elif command[:2] == ["uv", "sync"] and "--only-group" in command and attack == "qa-sync-failure":
            return subprocess.CompletedProcess(command, 2, "", "QA resolver unavailable")
        elif command[:2] != ["uv", "sync"]:
            output = Path(command[command.index("--output") + 1])
            if attack == "receipt-symlink":
                output.symlink_to(staging / "runtime-license-observations.json")
                return subprocess.CompletedProcess(command, 0, "", "")
            if attack == "receipt-fifo":
                os.mkfifo(output)
                return subprocess.CompletedProcess(command, 0, "", "")
            if attack == "receipt-oversize":
                output.write_bytes(b" " * (runtime_license_inventory.MAX_OBSERVATION_BYTES + 1))
                return subprocess.CompletedProcess(command, 0, "", "")
            output.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "status": "pass",
                        "scope": "runtime-all-extras",
                        "revision": manifest.source_commit,
                        "export_policy_sha256": manifest.policy_sha256,
                        "sbom_sha256": "c" * 64,
                        "observed_packages": 1,
                        "packages": [{"package_url": "pkg:pypi/example-base@2.0.0", "license_expression": "MIT"}],
                        "findings": [],
                        "observations": {
                            "name": "runtime-license-observations.json",
                            "sha256": command[command.index("--observations-sha256") + 1],
                        },
                        "platform_requirements": {
                            "name": "platform-all-extras-requirements.txt",
                            "sha256": hashlib.sha256(
                                (staging / "platform-all-extras-requirements.txt").read_bytes()
                            ).hexdigest(),
                        },
                        "marker_environment": {**_MARKER_ENVIRONMENT, "python_version": "3.13"}
                        if attack == "receipt-marker-substitution"
                        else _MARKER_ENVIRONMENT,
                    }
                ),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)

    def observe(command: list[str], **kwargs: object) -> BoundedProcessBytesResult:
        assert kwargs["stdout_limit"] == 2 * 1024 * 1024
        assert kwargs["stderr_limit"] == 16 * 1024
        result = run(command, **kwargs)
        assert isinstance(result.stdout, bytes) and isinstance(result.stderr, bytes)
        return BoundedProcessBytesResult(result.returncode, result.stdout, result.stderr)

    monkeypatch.setattr(check_public_candidate, "run_bounded_process_bytes", observe)

    if attack != "none":
        error = {
            "runtime-observation-failure": "runtime license observation failed",
            "qa-sync-failure": "locked QA dependency sync failed",
            "receipt-marker-substitution": "does not bind the runtime observation inputs",
            "receipt-symlink": "symbolic links",
            "receipt-fifo": "bounded regular file",
            "receipt-oversize": "bounded regular file",
        }[attack]
        with pytest.raises((OSError, ValueError), match=error):
            check_public_candidate._license_evidence(export_dir, staging, manifest)
        return
    evidence = check_public_candidate._license_evidence(export_dir, staging, manifest)

    assert evidence["status"] == "pass"
    assert (staging / "license-environment" / "bin" / "python").is_file()
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
    qa_sync = [command for command in commands if command[:2] == ["uv", "sync"]][1]
    assert "--only-group" in qa_sync and "dev" in qa_sync
    assert "--all-extras" not in qa_sync
    qa_controller = next(command for command in commands if "license-evidence" in command)
    assert qa_controller[0] == str(staging / "license-qa-environment" / "bin" / "python")
    assert qa_controller[1] == "-I"
