"""Contracts for installed-artifact documentation example scenarios."""

import errno
import functools
import hashlib
import io
import json
import subprocess
import sys
import sysconfig
import zipfile
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from scripts import check_documentation_example_scenarios as scenarios
from scripts import smoke_artifact
from scripts.check_documentation_contract import fenced_blocks
from scripts.release_consumer import OfflineDependencies

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def offline_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Supply real digest-bound preparatory bytes to mocked artifact journeys."""
    root = tmp_path / "dependencies"
    root.mkdir()
    (root / "wheels").mkdir()
    for name in ("pyproject.toml", "uv.lock"):
        (tmp_path / name).write_text("test metadata", encoding="utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("example/__init__.py", "")
    wheel = buffer.getvalue()
    wheel_name = "example-1.0-py3-none-any.whl"
    (root / "wheels" / wheel_name).write_bytes(wheel)
    digest = hashlib.sha256(wheel).hexdigest()
    requirement = f"example==1.0 --hash=sha256:{digest}\n".encode()
    names = ("runtime-requirements.txt", "release-build-requirements.txt")
    for name in names:
        (root / name).write_bytes(requirement)
    manifest = {
        "schema": 1,
        "authority": "preparatory-only; no approval granted",
        "target": {
            "version": list(sys.version_info[:3]),
            "implementation": sys.implementation.name,
            "cache_tag": sys.implementation.cache_tag,
            "platform": sysconfig.get_platform(),
        },
        "source": {
            name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() for name in ("pyproject.toml", "uv.lock")
        },
        "requirements": {name: hashlib.sha256(requirement).hexdigest() for name in names},
        "wheels": [{"name": wheel_name, "size": len(wheel), "sha256": digest}],
    }
    data = json.dumps(manifest).encode()
    (root / "preparation-manifest.json").write_bytes(data)
    monkeypatch.setattr(
        scenarios,
        "check",
        functools.partial(
            scenarios.check, dependency_root=root, dependency_manifest_sha256=hashlib.sha256(data).hexdigest()
        ),
    )
    return root


def test_dirty_diagnostic_exercises_both_artifacts_without_attesting_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel, sdist = tmp_path / "candidate.whl", tmp_path / "candidate.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    monkeypatch.setattr(scenarios, "_safe_block_identifiers", lambda _repo: set(scenarios._BLOCK_CRITERIA))
    monkeypatch.setattr(scenarios, "_build_artifacts", lambda *_args: (wheel, sdist))
    observed: list[Path] = []

    def smoke(
        artifact: Path, *, repo_root: Path, diagnostic_dirty: bool, offline_dependencies: OfflineDependencies
    ) -> smoke_artifact.SmokeReport:
        assert offline_dependencies.artifact.name == artifact.name
        assert offline_dependencies.artifact.sha256 == hashlib.sha256(artifact.read_bytes()).hexdigest()
        assert repo_root == tmp_path
        assert diagnostic_dirty is True
        observed.append(artifact)
        return replace(_report(artifact, set().union(*scenarios._BLOCK_CRITERIA.values())), source_revision=None)

    monkeypatch.setattr(scenarios.smoke_artifact, "smoke", smoke)
    evidence = scenarios.check(tmp_path, diagnostic_dirty=True)

    assert evidence.status == "diagnostic"
    assert set(observed) == {wheel, sdist}
    assert evidence.failures == ()
    assert all(not report.ok for report in evidence.artifacts)
    for report in evidence.artifacts:
        artifact = report.to_dict()
        assert artifact["source_revision"] is None
        assert artifact["source_binding"] == "unattested-dirty"
        assert artifact["criteria"]
    assert evidence.summary()["artifacts"] == [
        {
            "name": report.artifact_name,
            "sha256": report.artifact_sha256,
            "source_revision": None,
            "source_binding": "unattested-dirty",
            "criteria": [
                {"criterion_id": criterion.criterion_id, "status": criterion.status} for criterion in report.criteria
            ],
        }
        for report in evidence.artifacts
    ]


def test_dirty_diagnostic_rejects_claimed_revision_before_build(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot claim a source revision"):
        scenarios.check(tmp_path, source_revision="a" * 40, diagnostic_dirty=True)


def test_artifact_build_uses_the_locked_offline_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "dist"

    def run(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert argv == ("uv", "build", "--no-build-isolation", "--out-dir", str(output))
        assert kwargs["cwd"] == tmp_path
        assert kwargs["timeout"] == scenarios._TIMEOUT_SECONDS
        output.mkdir()
        (output / "fieldkit_cli-1.0.0-py3-none-any.whl").write_bytes(b"wheel")
        (output / "fieldkit_cli-1.0.0.tar.gz").write_bytes(b"sdist")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(scenarios.subprocess, "run", run)

    wheel, sdist = scenarios._build_artifacts(tmp_path, output)

    assert wheel.name == "fieldkit_cli-1.0.0-py3-none-any.whl"
    assert sdist.name == "fieldkit_cli-1.0.0.tar.gz"


def test_config_doctor_criterion_binds_the_current_command_fence() -> None:
    root = Path(__file__).parents[1]
    path = "docs/reference/config-file.md"
    contract = json.loads((root / "docs/documentation-contract.json").read_text(encoding="utf-8"))
    blocks = fenced_blocks(root / path)
    assert blocks
    declared = contract["documents"][path]["fenced_blocks"]
    selected = [
        block.body.strip()
        for record, block in zip(declared, blocks, strict=True)
        if scenarios._BLOCK_CRITERIA.get(record["id"]) == ("SMOKE112",)
    ]
    assert selected == ["fieldkit doctor"]


def test_artifact_journeys_overlap_and_keep_report_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wheel = tmp_path / "candidate.whl"
    sdist = tmp_path / "candidate.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    ready = Barrier(2, timeout=5)
    monkeypatch.setattr(scenarios, "_safe_block_identifiers", lambda _repo: set(scenarios._BLOCK_CRITERIA))
    monkeypatch.setattr(scenarios, "_build_artifacts", lambda *_args: (wheel, sdist))

    def run(
        artifact: Path,
        *,
        repo_root: Path,
        source_revision: str | None = None,
        offline_dependencies: OfflineDependencies,
    ) -> smoke_artifact.SmokeReport:
        assert offline_dependencies.artifact.name == artifact.name
        assert offline_dependencies.artifact.sha256 == hashlib.sha256(artifact.read_bytes()).hexdigest()
        assert repo_root == tmp_path
        ready.wait()
        return _report(artifact, set().union(*scenarios._BLOCK_CRITERIA.values()))

    monkeypatch.setattr(scenarios.smoke_artifact, "smoke", run)

    evidence = scenarios.check(tmp_path)

    assert evidence.status == "pass"
    assert [report.artifact_name for report in evidence.artifacts] == [wheel.name, sdist.name]


@pytest.mark.parametrize("failed_artifact", ["candidate.whl", "candidate.tar.gz"])
def test_concurrent_artifact_error_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_artifact: str
) -> None:
    wheel = tmp_path / "candidate.whl"
    sdist = tmp_path / "candidate.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    ready = Barrier(2, timeout=5)
    monkeypatch.setattr(scenarios, "_safe_block_identifiers", lambda _repo: set(scenarios._BLOCK_CRITERIA))
    monkeypatch.setattr(scenarios, "_build_artifacts", lambda *_args: (wheel, sdist))

    def run(
        artifact: Path,
        *,
        repo_root: Path,
        source_revision: str | None = None,
        offline_dependencies: OfflineDependencies,
    ) -> smoke_artifact.SmokeReport:
        assert offline_dependencies.artifact.name == artifact.name
        assert offline_dependencies.artifact.sha256 == hashlib.sha256(artifact.read_bytes()).hexdigest()
        assert repo_root == tmp_path
        ready.wait()
        if artifact.name == failed_artifact:
            raise OSError("artifact journey failed")
        return _report(artifact, set().union(*scenarios._BLOCK_CRITERIA.values()))

    monkeypatch.setattr(scenarios.smoke_artifact, "smoke", run)

    with pytest.raises(OSError, match="artifact journey failed"):
        scenarios.check(tmp_path)


def _report(artifact: Path, criterion_ids: set[str]) -> smoke_artifact.SmokeReport:
    """Build passing deterministic smoke evidence for one artifact."""
    return smoke_artifact.SmokeReport(
        schema_version=2,
        source_revision="a" * 40,
        artifact_name=artifact.name,
        artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        profile="base",
        python="3.11.0",
        platform="test",
        criteria=tuple(smoke_artifact.SmokeCriterion(criterion_id, "pass") for criterion_id in criterion_ids),
    )


def test_check_passes_snapshot_identity_to_both_artifact_journeys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A history-free snapshot retains the parent's exact immutable identity."""
    wheel = tmp_path / "fieldkit_cli-1.0.0-py3-none-any.whl"
    sdist = tmp_path / "fieldkit_cli-1.0.0.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    monkeypatch.setattr(scenarios, "_safe_block_identifiers", lambda _repo: set(scenarios._BLOCK_CRITERIA))
    monkeypatch.setattr(scenarios, "_build_artifacts", lambda *_args: (wheel, sdist))
    observed: list[str | None] = []
    expected_criteria = set().union(*scenarios._BLOCK_CRITERIA.values())

    def smoke(
        artifact: Path,
        *,
        repo_root: Path,
        source_revision: str | None = None,
        offline_dependencies: OfflineDependencies,
    ) -> smoke_artifact.SmokeReport:
        assert offline_dependencies.artifact.name == artifact.name
        assert offline_dependencies.artifact.sha256 == hashlib.sha256(artifact.read_bytes()).hexdigest()
        assert repo_root == tmp_path
        observed.append(source_revision)
        return _report(artifact, expected_criteria)

    monkeypatch.setattr(scenarios.smoke_artifact, "smoke", smoke)

    evidence = scenarios.check(tmp_path, source_revision="a" * 40)

    assert evidence.status == "pass"
    assert observed == ["a" * 40, "a" * 40]


@pytest.mark.parametrize("revision", ["HEAD", "a" * 39, "A" * 40])
def test_check_rejects_invalid_snapshot_identity_before_build(tmp_path: Path, revision: str) -> None:
    with pytest.raises(ValueError, match="full lowercase commit identity"):
        scenarios.check(tmp_path, source_revision=revision)


def test_check_binds_each_safe_documentation_block_to_fixed_smoke_criteria(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A generic artifact build cannot stand in for the public command examples."""
    wheel = tmp_path / "fieldkit_cli-1.0.0-py3-none-any.whl"
    sdist = tmp_path / "fieldkit_cli-1.0.0.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    monkeypatch.setattr(scenarios, "_safe_block_identifiers", lambda _repo: set(scenarios._BLOCK_CRITERIA))
    monkeypatch.setattr(scenarios, "_build_artifacts", lambda *_args: (wheel, sdist))
    expected_criteria = set().union(*scenarios._BLOCK_CRITERIA.values())
    monkeypatch.setattr(
        scenarios.smoke_artifact,
        "smoke",
        lambda artifact, **_kwargs: _report(artifact, expected_criteria),
    )

    evidence = scenarios.check(tmp_path)

    assert evidence.status == "pass"
    assert set(evidence.scenarios) == set(scenarios._BLOCK_CRITERIA)
    assert evidence.scenarios["readme.md.block-2"] == ("SMOKE119", "SMOKE120", "SMOKE158", "SMOKE153")
    assert evidence.scenarios["docs.getting.started.md.block-3"] == ("SMOKE119", "SMOKE120")
    assert evidence.scenarios["docs.getting.started.md.block-1"] == ("SMOKE125", "SMOKE126")
    assert evidence.scenarios["docs.guides.init.md.block-1"] == ("SMOKE158", "SMOKE153")
    assert evidence.scenarios["docs.user.guide.md.block-3"] == ("SMOKE160", "SMOKE153")
    assert evidence.scenarios["docs.reference.troubleshooting.md.block-2"] == ("SMOKE101", "SMOKE125")
    assert evidence.scenarios["docs.getting.started.md.block-5"] == ("SMOKE113", "SMOKE115")
    assert evidence.scenarios["docs.getting.started.md.block-6"] == (
        "SMOKE155",
        "SMOKE151",
        "SMOKE152",
        "SMOKE153",
    )
    assert evidence.scenarios["docs.getting.started.md.block-7"] == ("SMOKE151", "SMOKE152", "SMOKE153")
    assert evidence.scenarios["docs.getting.started.md.block-8"] == (
        "SMOKE119",
        "SMOKE120",
        "SMOKE154",
        "SMOKE149",
    )
    assert evidence.scenarios["docs.reference.config.file.md.block-2"] == ("SMOKE113", "SMOKE115")
    assert evidence.scenarios["docs.reference.config.file.md.block-8"] == ("SMOKE112",)
    assert evidence.scenarios["docs.reference.troubleshooting.md.block-1"] == (
        "SMOKE101",
        "SMOKE105",
        "SMOKE112",
    )
    assert evidence.scenarios["docs.user.guide.md.block-1"] == ("SMOKE159", "SMOKE153")
    assert evidence.scenarios["docs.guides.watchers.md.block-1"] == ("SMOKE121",)
    assert evidence.scenarios["docs.guides.watchers.md.block-2"] == ("SMOKE122",)
    assert "docs.guides.watchers.md.block-3" not in evidence.scenarios
    assert evidence.scenarios["docs.guides.watchers.md.block-6"] == (
        "SMOKE130",
        "SMOKE133",
        "SMOKE134",
        "SMOKE135",
        "SMOKE136",
        "SMOKE137",
    )
    assert evidence.scenarios["docs.reference.troubleshooting.md.block-5"] == (
        "SMOKE130",
        "SMOKE133",
        "SMOKE134",
        "SMOKE136",
        "SMOKE137",
    )
    assert evidence.scenarios["docs.reference.environment.vars.md.block-1"] == ("SMOKE130", "SMOKE138")
    assert evidence.scenarios["docs.reference.environment.vars.md.block-2"] == ("SMOKE130", "SMOKE139", "SMOKE140")
    assert evidence.scenarios["docs.guides.init.md.block-2"] == ("SMOKE123",)
    assert evidence.scenarios["docs.guides.init.md.block-3"] == ("SMOKE123",)
    assert evidence.scenarios["docs.guides.morning.brief.md.block-1"] == ("SMOKE112",)
    assert "docs.guides.morning.brief.md.block-3" not in evidence.scenarios
    assert evidence.scenarios["docs.guides.pipeline.workflow.md.block-7"] == ("SMOKE115", "SMOKE127", "SMOKE129")
    assert evidence.scenarios["docs.guides.pipeline.workflow.md.block-3"] == ("SMOKE130", "SMOKE131")
    assert evidence.scenarios["docs.guides.pipeline.workflow.md.block-5"] == ("SMOKE130", "SMOKE132")
    assert evidence.scenarios["docs.guides.pipeline.workflow.md.block-9"] == ("SMOKE115", "SMOKE127", "SMOKE128")
    assert evidence.scenarios["src.fieldkit.skills.meeting.skill.md.block-1"] == ("SMOKE156",)
    assert evidence.scenarios["src.fieldkit.skills.post.meeting.skill.md.block-1"] == ("SMOKE157",)
    assert {report.artifact_name for report in evidence.artifacts} == {wheel.name, sdist.name}


def test_check_fails_when_a_documented_command_criterion_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent command evidence fails closed instead of inheriting an aggregate pass."""
    wheel = tmp_path / "fieldkit_cli-1.0.0-py3-none-any.whl"
    sdist = tmp_path / "fieldkit_cli-1.0.0.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    monkeypatch.setattr(scenarios, "_safe_block_identifiers", lambda _repo: set(scenarios._BLOCK_CRITERIA))
    monkeypatch.setattr(scenarios, "_build_artifacts", lambda *_args: (wheel, sdist))
    monkeypatch.setattr(
        scenarios.smoke_artifact,
        "smoke",
        lambda artifact, **_kwargs: _report(artifact, {"SMOKE002"}),
    )

    evidence = scenarios.check(tmp_path)

    assert evidence.status == "fail"
    assert "readme.md.block-2:SMOKE158" in evidence.failures


def test_check_rejects_failed_artifact_even_when_mapped_examples_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel = tmp_path / "candidate.whl"
    sdist = tmp_path / "candidate.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    monkeypatch.setattr(scenarios, "_safe_block_identifiers", lambda _repo: set(scenarios._BLOCK_CRITERIA))
    monkeypatch.setattr(scenarios, "_build_artifacts", lambda *_args: (wheel, sdist))

    def smoke(
        artifact: Path,
        *,
        repo_root: Path,
        source_revision: str | None = None,
        offline_dependencies: OfflineDependencies,
    ) -> smoke_artifact.SmokeReport:
        report = _report(artifact, set().union(*scenarios._BLOCK_CRITERIA.values()))
        return smoke_artifact.SmokeReport(
            schema_version=report.schema_version,
            source_revision=report.source_revision,
            artifact_name=report.artifact_name,
            artifact_sha256=report.artifact_sha256,
            profile=report.profile,
            python=report.python,
            platform=report.platform,
            criteria=(*report.criteria, smoke_artifact.SmokeCriterion("SMOKE109", "fail", "registry missing")),
        )

    monkeypatch.setattr(scenarios.smoke_artifact, "smoke", smoke)

    evidence = scenarios.check(tmp_path)

    assert evidence.status == "fail"
    assert evidence.failures == ("candidate.whl:SMOKE109", "candidate.tar.gz:SMOKE109")


def test_requires_offline_input_before_build(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="externally retained manifest digest are required"):
        scenarios.check(tmp_path, dependency_root=None, dependency_manifest_sha256=None)


@pytest.mark.parametrize("change", ["digest", "source", "target", "wheel-size", "authority", "extra", "wheel-bytes"])
def test_offline_input_rejects_invalid_binding_before_build(
    tmp_path: Path, offline_input: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    manifest_path = offline_input / "preparation-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    retained = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    if change == "digest":
        retained = "0" * 64
    elif change == "source":
        (tmp_path / "uv.lock").write_text("changed metadata", encoding="utf-8")
    elif change == "target":
        manifest["target"]["version"] = [0, 0, 0]
    elif change == "wheel-size":
        manifest["wheels"][0]["size"] += 1
    elif change == "authority":
        manifest["authority"] = "approved"
    elif change == "extra":
        (offline_input / "unexpected.txt").write_text("extra", encoding="utf-8")
    elif change == "wheel-bytes":
        next((offline_input / "wheels").iterdir()).write_bytes(b"changed wheel")
    if change in {"target", "wheel-size", "authority"}:
        data = json.dumps(manifest).encode()
        manifest_path.write_bytes(data)
        retained = hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(scenarios, "_safe_block_identifiers", lambda _repo: set(scenarios._BLOCK_CRITERIA))

    def build(*_args: object) -> tuple[Path, Path]:
        pytest.fail("invalid offline input must fail before artifact build")

    monkeypatch.setattr(scenarios, "_build_artifacts", build)
    with pytest.raises(ValueError, match=r"dependency|original wheel"):
        scenarios.check(tmp_path, dependency_manifest_sha256=retained)


def test_offline_input_captures_private_original_bytes(tmp_path: Path, offline_input: Path) -> None:
    from scripts.documentation_dependency_input import verify_dependencies

    digest = hashlib.sha256((offline_input / "preparation-manifest.json").read_bytes()).hexdigest()
    with verify_dependencies(offline_input, digest, tmp_path) as verified:
        assert len(verified.wheels) == 1
        captured = verified.wheels[0]
        assert not captured.path.is_relative_to(offline_input)
        original = captured.path.read_bytes()
        next((offline_input / "wheels").iterdir()).write_bytes(b"changed after capture")
        assert captured.path.read_bytes() == original
        artifacts = [tmp_path / "candidate.whl", tmp_path / "candidate.tar.gz"]
        for path in artifacts:
            path.write_bytes(path.name.encode())
        wheel = verified.for_artifact(artifacts[0])
        sdist = verified.for_artifact(artifacts[1])
        assert wheel.artifact.kind == "wheel"
        assert sdist.artifact.kind == "sdist"
        assert wheel.artifact.sha256 != sdist.artifact.sha256
        assert wheel.wheels == sdist.wheels
        assert wheel.runtime_requirements == sdist.runtime_requirements
        assert wheel.release_build_requirements == sdist.release_build_requirements
    assert not captured.path.exists()


@pytest.mark.parametrize("unsafe", ["member", "requirements", "symlink"])
def test_offline_input_rejects_digest_bound_unsafe_content(tmp_path: Path, offline_input: Path, unsafe: str) -> None:
    from scripts.documentation_dependency_input import verify_dependencies

    manifest_path = offline_input / "preparation-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    path = next((offline_input / "wheels").iterdir())
    if unsafe == "symlink":
        original = path.read_bytes()
        path.unlink()
        target = tmp_path / "outside.whl"
        target.write_bytes(original)
        path.symlink_to(target)
    else:
        if unsafe == "member":
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("../outside.py", "unsafe")
            path.write_bytes(buffer.getvalue())
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest["wheels"][0].update(size=path.stat().st_size, sha256=digest)
        for name in manifest["requirements"]:
            data = f"example==1.0 --hash=sha256:{digest}\n"
            if unsafe == "requirements":
                data += "--index-url https://example.com\n"
            encoded = data.encode()
            (offline_input / name).write_bytes(encoded)
            manifest["requirements"][name] = hashlib.sha256(encoded).hexdigest()
    encoded_manifest = json.dumps(manifest).encode()
    manifest_path.write_bytes(encoded_manifest)
    with (
        pytest.raises((ValueError, OSError), match=r"unsafe members|hashed exact package pins|symbolic links"),
        verify_dependencies(offline_input, hashlib.sha256(encoded_manifest).hexdigest(), tmp_path),
    ):
        pytest.fail("unsafe input must not yield a verified closure")


def test_direct_script_bootstraps_subject_package_without_pythonpath(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(Path(scenarios.__file__).resolve()), "--help"],
        cwd=tmp_path,
        env={"HOME": str(tmp_path), "PYTHONNOUSERSITE": "1"},
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "--dependency-root" in result.stdout
    assert "--dependency-manifest-sha256" in result.stdout


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_artifact_identity_rejects_oversized_sparse_file(tmp_path: Path, offline_input: Path, kind: str) -> None:
    from scripts.artifact_limits import MAX_ARTIFACT_BYTES
    from scripts.documentation_dependency_input import verify_dependencies

    digest = hashlib.sha256((offline_input / "preparation-manifest.json").read_bytes()).hexdigest()
    artifact = tmp_path / ("candidate.whl" if kind == "wheel" else "candidate.tar.gz")
    with artifact.open("wb") as stream:
        stream.truncate(MAX_ARTIFACT_BYTES + 1)
    with (
        verify_dependencies(offline_input, digest, tmp_path) as verified,
        pytest.raises(ValueError, match="input exceeds size limit"),
    ):
        verified.for_artifact(artifact)


def test_artifact_identity_rejects_symlink(tmp_path: Path, offline_input: Path) -> None:
    from scripts.documentation_dependency_input import verify_dependencies

    digest = hashlib.sha256((offline_input / "preparation-manifest.json").read_bytes()).hexdigest()
    original = tmp_path / "original.whl"
    original.write_bytes(b"original")
    artifact = tmp_path / "candidate.whl"
    artifact.symlink_to(original)
    with verify_dependencies(offline_input, digest, tmp_path) as verified:
        with pytest.raises(OSError) as error:
            verified.for_artifact(artifact)
        assert error.value.errno == errno.ELOOP


def test_artifact_identity_rejects_unsupported_suffix(tmp_path: Path, offline_input: Path) -> None:
    from scripts.documentation_dependency_input import verify_dependencies

    digest = hashlib.sha256((offline_input / "preparation-manifest.json").read_bytes()).hexdigest()
    artifact = tmp_path / "candidate.zip"
    artifact.write_bytes(b"other")
    with (
        verify_dependencies(offline_input, digest, tmp_path) as verified,
        pytest.raises(ValueError, match="artifact must be a wheel or sdist"),
    ):
        verified.for_artifact(artifact)
