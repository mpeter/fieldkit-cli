"""Contracts for public-index release consumer verification."""

import hashlib
import io
import json
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable, Iterator
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from urllib.request import BaseHandler, HTTPRedirectHandler, ProxyHandler, Request
from urllib.response import addinfourl

import pytest
from jsonschema import Draft202012Validator

from scripts import check_release_consumer, release_bundle, release_consumer, release_wheelhouse
from scripts.runtime_license_inventory import MARKER_KEYS

pytestmark = pytest.mark.unit


@pytest.fixture
def direct_dependency_url() -> Iterator[tuple[str, list[str]]]:
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            self.send_error(404)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/missing-1.0-py3-none-any.whl", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _offline_dependencies(tmp_path: Path, *, kind: str = "wheel") -> tuple[Path, release_consumer.OfflineDependencies]:
    artifact = tmp_path / ("candidate.whl" if kind == "wheel" else "candidate.tar.gz")
    artifact.write_bytes(b"artifact")
    wheel = tmp_path / "dependency-1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(f"dependency==1.0 --hash=sha256:{hashlib.sha256(b'wheel').hexdigest()}\n", encoding="utf-8")
    return artifact, release_consumer.OfflineDependencies(
        release_consumer.ExpectedArtifact(
            "wheel" if kind == "wheel" else "sdist", artifact.name, hashlib.sha256(b"artifact").hexdigest()
        ),
        (release_consumer.DownloadedArtifact(wheel, hashlib.sha256(b"wheel").hexdigest()),),
        release_consumer.DownloadedArtifact(requirements, hashlib.sha256(requirements.read_bytes()).hexdigest()),
        release_consumer.DownloadedArtifact(requirements, hashlib.sha256(requirements.read_bytes()).hexdigest()),
    )


def test_offline_closure_snapshots_verified_bytes(tmp_path: Path) -> None:
    artifact, dependencies = _offline_dependencies(tmp_path)
    closure = release_consumer.snapshot_offline_closure(artifact, dependencies, tmp_path / "private")
    assert closure.kind == "wheel"
    artifact.write_bytes(b"changed")
    dependencies.wheels[0].path.write_bytes(b"changed")
    assert closure.artifact.read_bytes() == b"artifact"
    assert (closure.wheelhouse / dependencies.wheels[0].path.name).read_bytes() == b"wheel"


@pytest.mark.parametrize(
    "broken", ["artifact", "wheel", "requirements", "missing", "symlink", "directory", "duplicate"]
)
def test_offline_closure_rejects_unverified_inputs(tmp_path: Path, broken: str) -> None:
    artifact, dependencies = _offline_dependencies(tmp_path)
    if broken == "artifact":
        artifact.write_bytes(b"changed")
    elif broken == "requirements":
        dependencies.runtime_requirements.path.write_bytes(b"changed")
    elif broken == "duplicate":
        dependencies = release_consumer.OfflineDependencies(
            dependencies.artifact,
            dependencies.wheels * 2,
            dependencies.runtime_requirements,
            dependencies.release_build_requirements,
        )
    else:
        wheel = dependencies.wheels[0].path
        wheel.unlink()
        if broken == "wheel":
            wheel.write_bytes(b"changed")
        elif broken == "symlink":
            wheel.symlink_to(artifact)
        elif broken == "directory":
            wheel.mkdir()
    with pytest.raises((ValueError, OSError), match=r"offline|symbolic|No such|directory"):
        release_consumer.snapshot_offline_closure(artifact, dependencies, tmp_path / "private")


@pytest.mark.parametrize(
    "requirement",
    [
        "dependency==1.0",
        "-r other.txt",
        "dependency @ https://example.com/x.whl",
        "--find-links /tmp/other",
        "dependency>=1.0",
    ],
)
def test_offline_closure_rejects_requirements_that_escape_pinned_closure(tmp_path: Path, requirement: str) -> None:
    artifact, dependencies = _offline_dependencies(tmp_path)
    path = dependencies.runtime_requirements.path
    path.write_text(requirement + "\n", encoding="utf-8")
    verified = release_consumer.DownloadedArtifact(path, hashlib.sha256(path.read_bytes()).hexdigest())
    dependencies = release_consumer.OfflineDependencies(dependencies.artifact, dependencies.wheels, verified, verified)
    with pytest.raises(ValueError, match="hashed exact"):
        release_consumer.snapshot_offline_closure(artifact, dependencies, tmp_path / "private")


@pytest.mark.integration
@pytest.mark.parametrize("installer", ["pip", "uv"])
@pytest.mark.parametrize("closure_status", ["complete", "missing", "incompatible", "direct-url"])
def test_local_closure_installs_in_fresh_environment_without_index_or_cache(
    tmp_path: Path, installer: str, closure_status: str, direct_dependency_url: tuple[str, list[str]]
) -> None:
    uv = shutil.which("uv") if installer == "uv" else None
    if installer == "uv" and uv is None:
        pytest.skip("uv is unavailable")
    wheel = tmp_path / "dependency-1.0-py3-none-any.whl"
    dependency_metadata = "Metadata-Version: 2.1\nName: dependency\nVersion: 1.0\n"
    if closure_status == "direct-url":
        dependency_metadata += f"Requires-Dist: missing @ {direct_dependency_url[0]}\n"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("dependency.py", "VALUE = 1\n")
        archive.writestr("dependency-1.0.dist-info/METADATA", dependency_metadata)
        archive.writestr(
            "dependency-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr("dependency-1.0.dist-info/RECORD", "")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    application = tmp_path / "application-1.0-py3-none-any.whl"
    application_requirement = (
        "missing==1.0"
        if closure_status == "missing"
        else "dependency==2.0"
        if closure_status == "incompatible"
        else "dependency==1.0"
    )
    with zipfile.ZipFile(application, "w") as archive:
        archive.writestr("application.py", "import dependency\nVALUE = dependency.VALUE + 1\n")
        archive.writestr(
            "application-1.0.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: application\nVersion: 1.0\nRequires-Dist: {application_requirement}\n",
        )
        archive.writestr(
            "application-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr("application-1.0.dist-info/RECORD", "")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(f"dependency==1.0 --hash=sha256:{digest}\n", encoding="utf-8")
    source = release_consumer.DownloadedArtifact(requirements, hashlib.sha256(requirements.read_bytes()).hexdigest())
    dependencies = release_consumer.OfflineDependencies(
        release_consumer.ExpectedArtifact(
            "wheel", application.name, hashlib.sha256(application.read_bytes()).hexdigest()
        ),
        (release_consumer.DownloadedArtifact(wheel, digest),),
        source,
        source,
    )
    closure = release_consumer.snapshot_offline_closure(application, dependencies, tmp_path / "closure")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home"), "PIP_INDEX_URL": "http://127.0.0.1:1"}
    create = subprocess.run(
        [sys.executable, "-m", "venv", str(tmp_path / "venv")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert create.returncode == 0, create.stderr
    python = tmp_path / "venv/bin/python"
    runtime, installed = release_consumer.install_local_closure(closure, python, uv=uv, cwd=tmp_path, env=env)
    assert runtime.returncode == 0, runtime.stderr
    assert installed is not None
    assert direct_dependency_url[1] == []
    if closure_status != "complete":
        assert installed.returncode != 0
        assert "check" in installed.args
        assert "missing" in installed.stdout + installed.stderr or "2.0" in installed.stdout + installed.stderr
        return
    assert installed.returncode == 0
    imported = subprocess.run(
        [str(python), "-c", "import application; assert application.VALUE == 2"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert imported.returncode == 0, imported.stderr


@pytest.mark.parametrize("installer", ["pip", "uv"])
@pytest.mark.parametrize("failed_stage", ["runtime", "build", "artifact", "check", None])
def test_local_closure_stage_failures_skip_later_commands(
    tmp_path: Path, installer: str, failed_stage: str | None
) -> None:
    artifact, offline = _offline_dependencies(tmp_path, kind="sdist")
    closure = release_consumer.snapshot_offline_closure(artifact, offline, tmp_path / "closure")
    python = tmp_path / "venv/bin/python"
    uv = "/usr/bin/uv" if installer == "uv" else None
    calls: list[tuple[str, list[str], dict[str, object]]] = []

    def controlled_runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        stage = (
            "check"
            if "check" in command
            else "runtime"
            if Path(command[-1]).name == "runtime-requirements.txt"
            else "build"
            if Path(command[-1]).name == "release-build-requirements.txt"
            else "artifact"
        )
        calls.append((stage, command, kwargs))
        return subprocess.CompletedProcess(command, 1 if stage == failed_stage else 0, "", stage)

    dependencies, installed = release_consumer.install_local_closure(
        closure, python, uv=uv, cwd=tmp_path, env={"HOME": str(tmp_path)}, runner=controlled_runner
    )
    assert dependencies.returncode == (1 if failed_stage == "runtime" else 0)
    assert (installed is None) is (failed_stage == "runtime")
    stages = [stage for stage, _command, _kwargs in calls]
    all_stages = ["runtime", "build", "artifact", "check"]
    expected_stages = all_stages if failed_stage is None else all_stages[: all_stages.index(failed_stage) + 1]
    assert stages == expected_stages
    if installed is not None:
        assert installed.returncode == (0 if failed_stage is None else 1)
    for stage, command, kwargs in calls:
        assert kwargs["env"] == calls[0][2]["env"]
        assert kwargs["cwd"] == tmp_path
        assert kwargs["timeout"] == (
            release_consumer._CHECK_TIMEOUT_SECONDS if stage == "check" else release_consumer._INSTALL_TIMEOUT_SECONDS
        )
        if stage != "check":
            assert "--no-deps" in command and "--no-index" in command
        if stage in {"runtime", "build"}:
            assert "--require-hashes" in command
        if stage == "check":
            assert command == (
                [uv, "pip", "check", "--python", str(python)] if uv is not None else [str(python), "-m", "pip", "check"]
            )


def _write(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _wheelhouse_archive_bytes(wheel_bytes: bytes = b"wheel") -> bytes:
    wheel_digest = hashlib.sha256(wheel_bytes).hexdigest()
    manifest = {
        "schema_version": 1,
        "targets": [
            {
                "name": target.name,
                "python_version": target.python_version,
                "platform": target.platform,
                "wheels": [{"name": "click.whl", "sha256": wheel_digest}],
            }
            for target in release_wheelhouse.supported_targets()
        ],
    }
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("runtime-wheelhouse.json", json.dumps(manifest))
        for target in release_wheelhouse.supported_targets():
            archive.writestr(f"{target.name}/click.whl", wheel_bytes)
    return stream.getvalue()


def _candidate(tmp_path: Path, *, wheelhouse_bytes: bytes | None = None, source_revision: str = "a" * 40) -> Path:
    candidate = tmp_path / "candidate"
    wheel = "fieldkit_cli-1.0.0-py3-none-any.whl"
    sdist = "fieldkit_cli-1.0.0.tar.gz"
    wheel_digest = _write(candidate / "dist" / wheel, b"wheel payload")
    sdist_digest = _write(candidate / "dist" / sdist, b"sdist payload")
    sbom_digest = _write(candidate / "locked-graph.cdx.json", b'{"components": []}\n')
    marker_environment = dict.fromkeys(MARKER_KEYS, "example")
    observations_digest = _write(
        candidate / "runtime-license-observations.json",
        json.dumps(
            {
                "schema_version": 1,
                "packages": [{"name": "click", "version": "8.5.0", "license_expression": "BSD-3-Clause"}],
                "marker_environment": marker_environment,
            }
        ).encode(),
    )
    platform_digest = _write(candidate / "platform-all-extras-requirements.txt", b"click==8.5.0\n")
    requirements_digest = _write(
        candidate / "runtime-requirements.txt",
        b"click==8.5.0 --hash=sha256:" + b"a" * 64 + b"\n",
    )
    build_requirements_digest = _write(
        candidate / "release-build-requirements.txt",
        b"hatchling==1.26.3 --hash=sha256:" + b"b" * 64 + b"\n",
    )
    wheelhouse_digest = _write(
        candidate / "runtime-wheelhouse.zip",
        _wheelhouse_archive_bytes() if wheelhouse_bytes is None else wheelhouse_bytes,
    )
    revision = source_revision
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
    release_bundle.materialize(candidate)
    return candidate


def test_expected_release_requires_the_verified_closed_bundle(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)

    expected = release_consumer.expected_release(candidate / "bundle", candidate / "report.json")

    assert expected.repository == "example/fieldkit-cli"
    assert expected.planned_tag == "v1.0.0"
    assert expected.bundle_report == release_bundle.verify(
        candidate / "bundle", candidate_report=(candidate / "report.json").read_bytes()
    )
    assert [(artifact.kind, artifact.name) for artifact in expected.artifacts] == [
        ("sdist", "fieldkit_cli-1.0.0.tar.gz"),
        ("wheel", "fieldkit_cli-1.0.0-py3-none-any.whl"),
    ]


def _selected_bundle_report(candidate: Path) -> release_bundle.BundleReport:
    expected = release_consumer.expected_release(candidate / "bundle", candidate / "report.json")
    assert expected.bundle_report is not None
    return expected.bundle_report


@pytest.mark.parametrize("entry_point", ["install_offline", "run_offline_scenarios"])
@pytest.mark.parametrize("binding", ["omitted", "none"])
def test_offline_entry_points_fail_closed_without_initial_binding(
    tmp_path: Path, entry_point: str, binding: str
) -> None:
    operation: Callable[..., object] = getattr(release_consumer, entry_point)
    args: list[object] = [
        tmp_path / "bundle",
        tmp_path / "report.json",
        release_consumer.DownloadedArtifact(tmp_path / "artifact.whl", "a" * 64),
        release_consumer.ExpectedArtifact("wheel", "artifact.whl", "a" * 64),
    ]
    if entry_point == "install_offline":
        args.extend([tmp_path / "python", tmp_path / "wheelhouse"])
    kwargs: dict[str, object] = {"system": "linux", "machine": "x86_64", "python_version": "3.11"}
    if binding == "none":
        kwargs["expected_bundle_report"] = None
    with pytest.raises(
        TypeError if binding == "omitted" else ValueError,
        match="expected_bundle_report" if binding == "omitted" else "initial bundle verification binding",
    ):
        operation(*args, **kwargs)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("replacement", ["closure", "source"])
def test_consumer_cli_rejects_a_valid_replacement_candidate_with_the_same_application_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    candidate = _candidate(tmp_path / "initial")
    alternative = _candidate(
        tmp_path / "replacement",
        wheelhouse_bytes=_wheelhouse_archive_bytes(b"different closure") if replacement == "closure" else None,
        source_revision="f" * 40 if replacement == "source" else "a" * 40,
    )
    initial = release_consumer.expected_release(candidate / "bundle", candidate / "report.json")
    replaced = release_consumer.expected_release(alternative / "bundle", alternative / "report.json")
    assert initial.artifacts == replaced.artifacts
    assert initial.bundle_report != replaced.bundle_report
    payloads = {artifact.name: (candidate / "dist" / artifact.name).read_bytes() for artifact in initial.artifacts}

    def replace_between_verifications(
        *_args: object, **_kwargs: object
    ) -> tuple[release_consumer.ObservedArtifact, ...]:
        (candidate / "bundle").rename(candidate / "initial-bundle")
        (alternative / "bundle").rename(candidate / "bundle")
        (candidate / "report.json").write_bytes((alternative / "report.json").read_bytes())
        return tuple(
            release_consumer.ObservedArtifact(
                artifact.name, artifact.sha256, f"https://files.example.test/{artifact.name}"
            )
            for artifact in initial.artifacts
        )

    monkeypatch.setattr(release_consumer, "fetch_index_observations", replace_between_verifications)
    monkeypatch.setattr(release_consumer, "fetch_https_bytes", lambda url, **_kwargs: payloads[Path(url).name])
    monkeypatch.setattr(
        release_consumer, "_extract_wheelhouse_bytes", lambda *_args: pytest.fail("replacement reached extraction")
    )
    output = tmp_path / "evidence.json"
    result = check_release_consumer.main(
        [
            "--bundle",
            str(candidate / "bundle"),
            "--candidate-report",
            str(candidate / "report.json"),
            "--index-endpoint",
            "https://index.example.test/pypi/fieldkit-cli/1.0.0/json",
            "--download-host",
            "files.example.test",
            "--download-dir",
            str(tmp_path / "downloads"),
            "--output",
            str(output),
            "--allow-live-index",
            "--verify-downloads",
            "--verify-install",
        ]
    )
    assert result == 1
    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert evidence["status"] == "failed"
    assert evidence["source_commit"] == initial.source_commit
    assert evidence["findings"] == ["offline bundle does not match the initially verified candidate"]
    assert evidence["scenarios"] == []


def test_offline_install_uses_captured_bytes_after_original_inputs_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = _candidate(tmp_path)
    binding = _selected_bundle_report(candidate)
    artifact = candidate / "dist/fieldkit_cli-1.0.0-py3-none-any.whl"
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    bundle = candidate / "bundle"
    runtime_bytes = (bundle / "runtime-requirements.txt").read_bytes()
    build_bytes = (bundle / "release-build-requirements.txt").read_bytes()
    capture = release_bundle.capture_open_bundle
    captures: list[int] = []

    def change_originals(
        descriptor: int, *, candidate_report: bytes
    ) -> release_bundle.VerifiedBundle[release_bundle.BundleReport]:
        captured = capture(descriptor, candidate_report=candidate_report)
        captures.append(descriptor)
        for path in (
            bundle / "runtime-wheelhouse.zip",
            bundle / "runtime-requirements.txt",
            bundle / "release-build-requirements.txt",
            candidate / "report.json",
            artifact,
        ):
            path.write_bytes(b"replacement")
        return captured

    commands: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if "check" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        snapshot = Path(command[-1])
        assert not snapshot.is_relative_to(candidate)
        if snapshot.name == "runtime-requirements.txt":
            assert snapshot.read_bytes() == runtime_bytes
            wheelhouse = Path(command[command.index("--find-links") + 1])
            assert (wheelhouse / "click.whl").read_bytes() == b"wheel"
            assert (snapshot.parent / "release-build-requirements.txt").read_bytes() == build_bytes
        else:
            assert snapshot.read_bytes() == b"wheel payload"
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(release_bundle, "capture_open_bundle", change_originals)
    dependencies, installed = release_consumer.install_offline(
        bundle,
        candidate / "report.json",
        release_consumer.DownloadedArtifact(artifact, digest),
        release_consumer.ExpectedArtifact("wheel", artifact.name, digest),
        tmp_path / "venv/bin/python",
        tmp_path / "extract",
        system="linux",
        machine="x86_64",
        python_version="3.11",
        expected_bundle_report=binding,
        runner=runner,
    )
    assert dependencies.returncode == 0
    assert installed is not None and installed.returncode == 0
    assert len(captures) == 1
    assert len(commands) == 3
    assert not Path(commands[0][-1]).parent.exists()


@pytest.mark.parametrize(
    "invalid",
    [
        "bundle-link",
        "report-link",
        "artifact-link",
        "bundle-ancestor",
        "report-ancestor",
        "artifact-ancestor",
        "artifact-oversized",
        "artifact-mismatch",
        "requirements-changed",
    ],
)
def test_offline_install_rejects_untrusted_inputs_and_cleans_private_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    candidate = _candidate(tmp_path)
    binding = _selected_bundle_report(candidate)
    bundle = candidate / "bundle"
    report = candidate / "report.json"
    artifact = candidate / "dist/fieldkit_cli-1.0.0-py3-none-any.whl"
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    if invalid.endswith("-link"):
        source = bundle if invalid == "bundle-link" else report if invalid == "report-link" else artifact
        link = tmp_path / source.name
        link.symlink_to(source, target_is_directory=source.is_dir())
        if invalid == "bundle-link":
            bundle = link
        elif invalid == "report-link":
            report = link
        else:
            artifact = link
    elif invalid.endswith("-ancestor"):
        link = tmp_path / "ancestor"
        link.symlink_to(candidate, target_is_directory=True)
        if invalid == "bundle-ancestor":
            bundle = link / "bundle"
        elif invalid == "report-ancestor":
            report = link / "report.json"
        else:
            artifact = link / "dist" / artifact.name
    elif invalid == "artifact-oversized":
        monkeypatch.setattr(release_consumer, "MAX_ARTIFACT_BYTES", 1)
    elif invalid == "artifact-mismatch":
        artifact.write_bytes(b"other candidate")
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    else:
        (bundle / "runtime-requirements.txt").write_bytes(b"changed")
    with pytest.raises((ValueError, OSError), match=r"bundle|regular|directory|symbolic|offline|report"):
        release_consumer.install_offline(
            bundle,
            report,
            release_consumer.DownloadedArtifact(artifact, digest),
            release_consumer.ExpectedArtifact("wheel", artifact.name, digest),
            tmp_path / "venv/bin/python",
            tmp_path / "extract",
            system="linux",
            machine="x86_64",
            python_version="3.11",
            expected_bundle_report=binding,
            runner=lambda *_args, **_kwargs: pytest.fail("invalid input reached the installer"),
        )
    assert not list(tmp_path.glob("extract-*"))


def test_offline_install_cleans_snapshot_after_installer_exception(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    artifact = candidate / "dist/fieldkit_cli-1.0.0-py3-none-any.whl"
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

    def fail_runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert Path(command[-1]).is_file()
        raise OSError("installer unavailable")

    with pytest.raises(OSError, match="installer unavailable"):
        release_consumer.install_offline(
            candidate / "bundle",
            candidate / "report.json",
            release_consumer.DownloadedArtifact(artifact, digest),
            release_consumer.ExpectedArtifact("wheel", artifact.name, digest),
            tmp_path / "venv/bin/python",
            tmp_path / "extract",
            system="linux",
            machine="x86_64",
            python_version="3.11",
            expected_bundle_report=_selected_bundle_report(candidate),
            runner=fail_runner,
        )
    assert not list(tmp_path.glob("extract-*"))


@pytest.mark.parametrize("invalid", ["zip", "unsafe", "oversized"])
def test_wheelhouse_extraction_rejects_invalid_captured_archive_and_cleans_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    content = b"invalid zip"
    if invalid == "unsafe":
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("runtime-wheelhouse.json", "{}")
            archive.writestr("../escape.whl", b"wheel")
        content = stream.getvalue()
    elif invalid == "oversized":
        content = _wheelhouse_archive_bytes()
    candidate = _candidate(tmp_path, wheelhouse_bytes=content)
    if invalid == "oversized":
        monkeypatch.setattr(release_consumer._release_bundle_evidence, "MAX_WHEELHOUSE_BYTES", 1)
    destination = tmp_path / "wheelhouse"
    with pytest.raises((ValueError, zipfile.BadZipFile), match=r"zip|unsafe|size"):
        release_consumer.extract_runtime_wheelhouse(candidate / "bundle", candidate / "report.json", destination)
    assert not destination.exists()
    assert not (tmp_path / "escape.whl").exists()


def test_wheelhouse_extraction_requires_a_verified_bundle_and_rejects_unsafe_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = _candidate(tmp_path)
    bundle = candidate / "bundle"
    wheel_digest = hashlib.sha256(b"wheel").hexdigest()
    manifest = {
        "schema_version": 1,
        "targets": [
            {
                "name": target.name,
                "python_version": target.python_version,
                "platform": target.platform,
                "wheels": [{"name": "click.whl", "sha256": wheel_digest}],
            }
            for target in release_wheelhouse.supported_targets()
        ],
    }
    report = candidate / "report.json"
    validator = release_wheelhouse.validate_manifest
    validated: list[dict[str, object]] = []

    def checked_validator(data: bytes, actual: dict[str, str]) -> dict[str, object]:
        result = validator(data, actual)
        assert result == manifest
        assert actual == {f"{target.name}/click.whl": wheel_digest for target in release_wheelhouse.supported_targets()}
        validated.append(result)
        return result

    monkeypatch.setattr(release_wheelhouse, "validate_manifest", checked_validator)

    extracted = release_consumer.extract_runtime_wheelhouse(bundle, report, tmp_path / "wheelhouse")

    assert extracted == tmp_path / "wheelhouse"
    assert validated == [manifest]
    assert (extracted / "linux-x86_64-python311/click.whl").read_bytes() == b"wheel"


def test_offline_install_uses_only_the_verified_target_wheelhouse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    candidate = _candidate(tmp_path)
    artifact_path = Path("candidate/dist/fieldkit_cli-1.0.0-py3-none-any.whl")
    digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    commands: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    dependencies, installed = release_consumer.install_offline(
        candidate / "bundle",
        candidate / "report.json",
        release_consumer.DownloadedArtifact(artifact_path, digest),
        release_consumer.ExpectedArtifact("wheel", artifact_path.name, digest),
        tmp_path / "venv/bin/python",
        tmp_path / "extract",
        system="linux",
        machine="x86_64",
        python_version="3.11",
        expected_bundle_report=_selected_bundle_report(candidate),
        runner=runner,
    )

    assert installed is not None
    assert dependencies.returncode == installed.returncode == 0
    assert commands[0][3:6] == ["install", "--no-index", "--find-links"]
    assert Path(commands[0][6]).name == "linux-x86_64-python311"
    assert commands[0][7:10] == ["--require-hashes", "--no-deps", "--requirement"]
    assert commands[1][3:6] == ["install", "--no-index", "--no-deps"]
    assert Path(commands[1][-1]).name == artifact_path.name
    assert Path(commands[1][-1]) != (tmp_path / artifact_path)
    assert not Path(commands[1][-1]).parent.exists()


def test_offline_install_resolves_candidate_inputs_before_changing_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _candidate(tmp_path)
    artifact_path = Path("candidate/dist/fieldkit_cli-1.0.0-py3-none-any.whl")
    digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    commands: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    release_consumer.install_offline(
        Path("candidate/bundle"),
        Path("candidate/report.json"),
        release_consumer.DownloadedArtifact(artifact_path, digest),
        release_consumer.ExpectedArtifact("wheel", artifact_path.name, digest),
        tmp_path / "venv/bin/python",
        tmp_path / "extract",
        system="linux",
        machine="x86_64",
        python_version="3.11",
        expected_bundle_report=_selected_bundle_report(tmp_path / "candidate"),
        cwd=tmp_path,
        runner=runner,
    )

    assert Path(commands[0][-1]).is_absolute()
    assert Path(commands[0][-1]).name == "runtime-requirements.txt"
    assert Path(commands[1][-1]).is_absolute()
    assert Path(commands[1][-1]).name == artifact_path.name


def test_offline_sdist_install_bootstraps_only_the_locked_build_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = _candidate(tmp_path)
    artifact_path = candidate / "dist/fieldkit_cli-1.0.0.tar.gz"
    digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    commands: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    _, installed = release_consumer.install_offline(
        candidate / "bundle",
        candidate / "report.json",
        release_consumer.DownloadedArtifact(artifact_path, digest),
        release_consumer.ExpectedArtifact("sdist", artifact_path.name, digest),
        tmp_path / "venv/bin/python",
        tmp_path / "extract",
        system="linux",
        machine="x86_64",
        python_version="3.11",
        expected_bundle_report=_selected_bundle_report(candidate),
        runner=runner,
    )

    assert installed is not None
    assert Path(commands[1][-1]).name == "release-build-requirements.txt"
    assert commands[2][-2] == "--no-build-isolation"
    assert Path(commands[2][-1]).name == artifact_path.name
    assert Path(commands[2][-1]) != artifact_path


def test_offline_install_does_not_install_the_artifact_after_dependency_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = _candidate(tmp_path)
    artifact_path = candidate / "dist/fieldkit_cli-1.0.0-py3-none-any.whl"
    digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    calls = 0

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(command, 1, "", "missing wheel")

    dependencies, installed = release_consumer.install_offline(
        candidate / "bundle",
        candidate / "report.json",
        release_consumer.DownloadedArtifact(artifact_path, digest),
        release_consumer.ExpectedArtifact("wheel", artifact_path.name, digest),
        tmp_path / "venv/bin/python",
        tmp_path / "extract",
        system="linux",
        machine="x86_64",
        python_version="3.11",
        expected_bundle_report=_selected_bundle_report(candidate),
        runner=runner,
    )

    assert dependencies.returncode == 1
    assert installed is None
    assert calls == 1


def test_offline_scenarios_use_fixed_argv_in_an_isolated_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = release_consumer.DownloadedArtifact(tmp_path / "artifact.whl", "a" * 64)
    expected = release_consumer.ExpectedArtifact("wheel", "artifact.whl", "a" * 64)
    monkeypatch.setattr(
        release_consumer,
        "install_offline",
        lambda *_args, **_kwargs: (
            subprocess.CompletedProcess(["dependencies"], 0, "", ""),
            subprocess.CompletedProcess(["artifact"], 0, "", ""),
        ),
    )
    commands: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    results = release_consumer.run_offline_scenarios(
        tmp_path / "bundle",
        tmp_path / "report.json",
        artifact,
        expected,
        system="linux",
        machine="x86_64",
        python_version="3.11",
        expected_bundle_report=release_bundle.BundleReport("a" * 40, ()),
        runner=runner,
    )

    assert [result.status for result in results] == ["pass"] * 8
    assert [[Path(command[0]).name, *command[1:]] for command in commands[-5:]] == [
        ["fieldkit", "--version"],
        ["fieldkit", "--help"],
        ["fieldkit", "init", "--minimal", str(commands[-3][-1])],
        ["fieldkit", "doctor", "--json"],
        ["fieldkit", "skill", "list"],
    ]
    assert release_consumer.offline_scenario_evidence(results, artifact_name=expected.name)[0] == {
        "artifact_name": "artifact.whl",
        "id": "CONSUMER001",
        "argv": ["python", "-m", "venv", "<venv>"],
        "exit_status": 0,
        "status": "pass",
    }


def test_failed_dependency_check_marks_offline_scenarios_and_consumer_evidence_failed(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    expected = release_consumer.expected_release(candidate / "bundle", candidate / "report.json")
    assert expected.bundle_report is not None
    downloaded = tuple(
        release_consumer.DownloadedArtifact(candidate / "dist" / artifact.name, artifact.sha256)
        for artifact in expected.artifacts
    )
    observed = tuple(
        release_consumer.ObservedArtifact(artifact.name, artifact.sha256) for artifact in expected.artifacts
    )

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "check" in command:
            return subprocess.CompletedProcess(command, 1, "", "missing dependency")
        assert Path(command[0]).name != "fieldkit", "failed check must skip installed-package user scenarios"
        return subprocess.CompletedProcess(command, 0, "", "")

    scenarios = tuple(
        scenario
        for artifact, retained in zip(expected.artifacts, downloaded, strict=True)
        for scenario in release_consumer.offline_scenario_evidence(
            release_consumer.run_offline_scenarios(
                candidate / "bundle",
                candidate / "report.json",
                retained,
                artifact,
                system="linux",
                machine="x86_64",
                python_version="3.11",
                expected_bundle_report=expected.bundle_report,
                runner=runner,
            ),
            artifact_name=artifact.name,
        )
    )
    assert all(scenario["status"] == "fail" for scenario in scenarios if scenario["id"] == "CONSUMER003")
    assert all(scenario["status"] == "not_run" for scenario in scenarios if str(scenario["id"]).startswith("CONSUMER1"))
    evidence = release_consumer.consumer_evidence(
        expected,
        endpoint="https://index.example.test/pypi/fieldkit-cli/1.0.0/json",
        observed=observed,
        report=release_consumer.IndexReport("success", ()),
        system="linux",
        machine="x86_64",
        python_version="3.11",
        downloaded=downloaded,
        scenarios=scenarios,
    )
    assert evidence["status"] == "failed"
    assert evidence["scenarios"] == list(scenarios)


def test_offline_scenarios_mark_unrun_steps_after_venv_failure(
    tmp_path: Path,
) -> None:
    artifact = release_consumer.DownloadedArtifact(tmp_path / "artifact.whl", "a" * 64)
    expected = release_consumer.ExpectedArtifact("wheel", "artifact.whl", "a" * 64)

    results = release_consumer.run_offline_scenarios(
        tmp_path / "bundle",
        tmp_path / "report.json",
        artifact,
        expected,
        system="linux",
        machine="x86_64",
        python_version="3.11",
        expected_bundle_report=release_bundle.BundleReport("a" * 40, ()),
        runner=lambda command, **_kwargs: subprocess.CompletedProcess(command, 1, "", "venv failed"),
    )

    assert [(result.id, result.status, result.exit_status) for result in results] == [
        ("CONSUMER001", "fail", 1),
        *((scenario_id, "not_run", None) for scenario_id, _ in release_consumer._OFFLINE_SCENARIOS[1:]),
    ]


def test_empty_index_observation_is_pending_not_success(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    expected = release_consumer.expected_release(candidate / "bundle", candidate / "report.json")

    report = release_consumer.evaluate_index(expected, ())

    assert report.status == "pending"
    assert report.findings == ("expected version is not visible",)


def test_polling_retries_an_incomplete_index_until_the_exact_release_is_visible(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    expected = release_consumer.expected_release(candidate / "bundle", candidate / "report.json")
    complete = tuple(
        release_consumer.ObservedArtifact(artifact.name, artifact.sha256, f"https://files.example.test/{artifact.name}")
        for artifact in expected.artifacts
    )
    observations = iter(((), complete))
    delays: list[float] = []

    result = release_consumer.poll_index_observations(
        expected,
        fetch=lambda: next(observations),
        max_attempts=2,
        poll_interval_seconds=1.5,
        sleep=delays.append,
    )

    assert result.observed == complete
    assert result.report.ok
    assert delays == [1.5]


def test_polling_returns_the_final_partial_observation_after_its_bound(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    expected = release_consumer.expected_release(candidate / "bundle", candidate / "report.json")
    partial = (release_consumer.ObservedArtifact(expected.artifacts[0].name, expected.artifacts[0].sha256),)
    calls = 0

    def fetch() -> tuple[release_consumer.ObservedArtifact, ...]:
        nonlocal calls
        calls += 1
        return partial

    result = release_consumer.poll_index_observations(
        expected,
        fetch=fetch,
        max_attempts=3,
        poll_interval_seconds=0,
    )

    assert result.observed == partial
    assert result.report == release_consumer.IndexReport("failed", ("missing expected wheel artifact",))
    assert calls == 3


def test_polling_records_pending_after_retryable_transport_failures(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    expected = release_consumer.expected_release(candidate / "bundle", candidate / "report.json")
    calls = 0
    delays: list[float] = []

    def fetch() -> tuple[release_consumer.ObservedArtifact, ...]:
        nonlocal calls
        calls += 1
        raise release_consumer.IndexTransportError("index request timed out")

    result = release_consumer.poll_index_observations(
        expected,
        fetch=fetch,
        max_attempts=2,
        poll_interval_seconds=1,
        sleep=delays.append,
    )

    assert result.observed == ()
    assert result.report == release_consumer.IndexReport("pending", ("index unavailable after 2 attempts",))
    assert calls == 2
    assert delays == [1]


@pytest.mark.parametrize(("max_attempts", "poll_interval_seconds"), [(0, 1), (1, -1)])
def test_polling_rejects_an_unbounded_or_negative_configuration(
    max_attempts: int, poll_interval_seconds: float
) -> None:
    with pytest.raises(ValueError, match="poll"):
        release_consumer.poll_index_observations(
            release_consumer.ExpectedRelease("example/fieldkit-cli", "v1.0.0", "a" * 40, ()),
            fetch=lambda: (),
            max_attempts=max_attempts,
            poll_interval_seconds=poll_interval_seconds,
        )


def test_partial_index_observation_fails_without_installation(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    expected = release_consumer.expected_release(candidate / "bundle", candidate / "report.json")
    wheel = next(artifact for artifact in expected.artifacts if artifact.kind == "wheel")

    report = release_consumer.evaluate_index(expected, (release_consumer.ObservedArtifact(wheel.name, wheel.sha256),))

    assert report.status == "failed"
    assert report.findings == ("missing expected sdist artifact",)


def test_digest_mismatch_fails_closed(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    expected = release_consumer.expected_release(candidate / "bundle", candidate / "report.json")

    report = release_consumer.evaluate_index(
        expected,
        tuple(release_consumer.ObservedArtifact(artifact.name, "0" * 64) for artifact in expected.artifacts),
    )

    assert report.status == "failed"
    assert report.findings == (
        "digest mismatch for fieldkit_cli-1.0.0-py3-none-any.whl",
        "digest mismatch for fieldkit_cli-1.0.0.tar.gz",
    )


def test_duplicate_or_unexpected_index_name_fails_closed(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    expected = release_consumer.expected_release(candidate / "bundle", candidate / "report.json")
    observed = [release_consumer.ObservedArtifact(artifact.name, artifact.sha256) for artifact in expected.artifacts]
    observed.append(release_consumer.ObservedArtifact(expected.artifacts[0].name, expected.artifacts[0].sha256))

    report = release_consumer.evaluate_index(expected, tuple(observed))

    assert report.status == "failed"
    assert report.findings == (f"duplicate index artifact: {expected.artifacts[0].name}",)


def test_consumer_evidence_binds_observation_to_the_verified_candidate(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    expected = release_consumer.expected_release(candidate / "bundle", candidate / "report.json")
    observed = (release_consumer.ObservedArtifact(expected.artifacts[0].name, expected.artifacts[0].sha256),)
    report = release_consumer.evaluate_index(expected, observed)

    evidence = release_consumer.consumer_evidence(
        expected,
        endpoint="https://test.pypi.org/pypi/fieldkit-cli/1.0.0/json",
        observed=observed,
        report=report,
        system="linux",
        machine="x86_64",
        python_version="3.11",
    )

    assert evidence == {
        "schema_version": 5,
        "status": "failed",
        "repository": "example/fieldkit-cli",
        "source_commit": "a" * 40,
        "planned_tag": "v1.0.0",
        "index_endpoint": "https://test.pypi.org/pypi/fieldkit-cli/1.0.0/json",
        "environment": {"system": "linux", "machine": "x86_64", "python_version": "3.11"},
        "artifacts": [
            {
                "kind": "sdist",
                "name": "fieldkit_cli-1.0.0.tar.gz",
                "sha256": expected.artifacts[0].sha256,
                "observed": True,
                "downloaded": False,
            },
            {
                "kind": "wheel",
                "name": "fieldkit_cli-1.0.0-py3-none-any.whl",
                "sha256": expected.artifacts[1].sha256,
                "observed": False,
                "downloaded": False,
            },
        ],
        "scenarios": [],
        "findings": ["missing expected wheel artifact"],
    }
    schema = json.loads(
        (Path(__file__).parents[1] / "docs/release-readiness/release-consumer-evidence.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert list(Draft202012Validator(schema).iter_errors(json.loads(json.dumps(evidence)))) == []


def test_consumer_evidence_records_only_a_complete_exact_download_receipt(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    expected = release_consumer.expected_release(candidate / "bundle", candidate / "report.json")
    observed = tuple(
        release_consumer.ObservedArtifact(artifact.name, artifact.sha256) for artifact in expected.artifacts
    )
    downloaded = tuple(
        release_consumer.DownloadedArtifact(tmp_path / artifact.name, artifact.sha256)
        for artifact in expected.artifacts
    )

    evidence = release_consumer.consumer_evidence(
        expected,
        endpoint="https://index.example.test/pypi/fieldkit-cli/1.0.0/json",
        observed=observed,
        report=release_consumer.evaluate_index(expected, observed),
        system="linux",
        machine="x86_64",
        python_version="3.11",
        downloaded=downloaded,
    )

    artifacts = evidence["artifacts"]
    assert isinstance(artifacts, list)
    assert [artifact["downloaded"] for artifact in artifacts if isinstance(artifact, dict)] == [True, True]
    assert evidence["status"] == "pending"
    assert evidence["findings"] == ["offline scenario verification not requested"]


def test_consumer_evidence_requires_every_scenario_for_each_downloaded_artifact(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    expected = release_consumer.expected_release(candidate / "bundle", candidate / "report.json")
    observed = tuple(
        release_consumer.ObservedArtifact(artifact.name, artifact.sha256) for artifact in expected.artifacts
    )
    downloaded = tuple(
        release_consumer.DownloadedArtifact(tmp_path / artifact.name, artifact.sha256)
        for artifact in expected.artifacts
    )
    scenarios = tuple(
        {
            "artifact_name": artifact.name,
            "id": scenario_id,
            "argv": list(argv),
            "exit_status": 0,
            "status": "pass",
        }
        for artifact in expected.artifacts
        for scenario_id, argv in release_consumer._OFFLINE_SCENARIOS
    )

    evidence = release_consumer.consumer_evidence(
        expected,
        endpoint="https://index.example.test/pypi/fieldkit-cli/1.0.0/json",
        observed=observed,
        report=release_consumer.evaluate_index(expected, observed),
        system="linux",
        machine="x86_64",
        python_version="3.11",
        downloaded=downloaded,
        scenarios=scenarios,
    )

    assert evidence["status"] == "success"
    assert evidence["findings"] == []


def test_consumer_evidence_rejects_a_partial_or_mismatched_download_receipt(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    expected = release_consumer.expected_release(candidate / "bundle", candidate / "report.json")
    observed = tuple(
        release_consumer.ObservedArtifact(artifact.name, artifact.sha256) for artifact in expected.artifacts
    )

    with pytest.raises(ValueError, match="download receipts"):
        release_consumer.consumer_evidence(
            expected,
            endpoint="https://index.example.test/pypi/fieldkit-cli/1.0.0/json",
            observed=observed,
            report=release_consumer.evaluate_index(expected, observed),
            system="linux",
            machine="x86_64",
            python_version="3.11",
            downloaded=(release_consumer.DownloadedArtifact(tmp_path / "other.whl", "0" * 64),),
        )


def test_index_metadata_requires_https_and_explicit_download_hosts() -> None:
    payload = {
        "urls": [
            {
                "filename": "fieldkit_cli-1.0.0-py3-none-any.whl",
                "digests": {"sha256": "a" * 64},
                "url": "https://files.example.test/packages/fieldkit_cli-1.0.0-py3-none-any.whl",
            }
        ]
    }

    observed = release_consumer.index_observations(
        payload,
        endpoint="https://index.example.test/pypi/fieldkit-cli/1.0.0/json",
        allowed_download_hosts=frozenset({"files.example.test"}),
    )

    assert observed == (
        release_consumer.ObservedArtifact(
            "fieldkit_cli-1.0.0-py3-none-any.whl",
            "a" * 64,
            "https://files.example.test/packages/fieldkit_cli-1.0.0-py3-none-any.whl",
        ),
    )


def test_index_metadata_accepts_documented_pypi_response_fields() -> None:
    payload = {
        "info": {"name": "fieldkit-cli", "version": "1.0.0"},
        "last_serial": 1,
        "urls": [
            {
                "comment_text": "",
                "digests": {"blake2b_256": "b" * 64, "md5": "c" * 32, "sha256": "a" * 64},
                "filename": "fieldkit_cli-1.0.0-py3-none-any.whl",
                "packagetype": "bdist_wheel",
                "size": 123,
                "url": "https://files.example.test/packages/fieldkit_cli-1.0.0-py3-none-any.whl",
                "yanked": False,
            }
        ],
        "vulnerabilities": [],
    }

    observed = release_consumer.index_observations(
        payload,
        endpoint="https://index.example.test/pypi/fieldkit-cli/1.0.0/json",
        allowed_download_hosts=frozenset({"files.example.test"}),
    )

    assert observed == (
        release_consumer.ObservedArtifact(
            "fieldkit_cli-1.0.0-py3-none-any.whl",
            "a" * 64,
            "https://files.example.test/packages/fieldkit_cli-1.0.0-py3-none-any.whl",
        ),
    )


def test_fetch_index_observations_uses_a_separate_endpoint_trust_root(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps(
        {
            "urls": [
                {
                    "filename": "fieldkit_cli-1.0.0-py3-none-any.whl",
                    "digests": {"sha256": "a" * 64},
                    "url": "https://files.example.test/packages/fieldkit_cli-1.0.0-py3-none-any.whl",
                }
            ]
        }
    ).encode()
    seen: dict[str, object] = {}

    def fake_fetch(url: str, *, timeout_seconds: float, allowed_download_hosts: frozenset[str]) -> bytes:
        seen.update(url=url, timeout_seconds=timeout_seconds, allowed_download_hosts=allowed_download_hosts)
        return payload

    monkeypatch.setattr(release_consumer, "fetch_https_bytes", fake_fetch)

    observed = release_consumer.fetch_index_observations(
        "https://index.example.test/pypi/fieldkit-cli/1.0.0/json",
        timeout_seconds=5,
        allowed_download_hosts=frozenset({"files.example.test"}),
    )

    assert observed[0].sha256 == "a" * 64
    assert seen == {
        "url": "https://index.example.test/pypi/fieldkit-cli/1.0.0/json",
        "timeout_seconds": 5,
        "allowed_download_hosts": frozenset({"index.example.test"}),
    }


@pytest.mark.parametrize(
    ("endpoint", "download_url", "message"),
    [
        (
            "http://index.example.test/pypi/pkg/json",
            "https://files.example.test/pkg.whl",
            "index endpoint must use HTTPS",
        ),
        (
            "https://index.example.test/pypi/pkg/json",
            "http://files.example.test/pkg.whl",
            "artifact URL must use HTTPS",
        ),
        (
            "https://index.example.test/pypi/pkg/json",
            "https://other.example.test/pkg.whl",
            "artifact URL host is not allowed",
        ),
        (
            "https://index.example.test:444/pypi/pkg/json",
            "https://files.example.test/pkg.whl",
            "index endpoint must use the default HTTPS port",
        ),
    ],
)
def test_index_metadata_rejects_unsafe_endpoint_or_download_host(
    endpoint: str, download_url: str, message: str
) -> None:
    payload = {"urls": [{"filename": "pkg.whl", "digests": {"sha256": "a" * 64}, "url": download_url}]}

    with pytest.raises(ValueError, match=message):
        release_consumer.index_observations(
            payload, endpoint=endpoint, allowed_download_hosts=frozenset({"files.example.test"})
        )


def test_index_metadata_rejects_malformed_response() -> None:
    with pytest.raises(ValueError, match="unsupported schema"):
        release_consumer.index_observations(
            {"urls": [{"filename": "fieldkit_cli-1.0.0-py3-none-any.whl"}]},
            endpoint="https://index.example.test/pypi/fieldkit-cli/1.0.0/json",
            allowed_download_hosts=frozenset({"files.example.test"}),
        )


def test_verified_download_writes_only_digest_matched_bytes(tmp_path: Path) -> None:
    payload = b"verified wheel"
    artifact = release_consumer.ExpectedArtifact(
        "wheel", "fieldkit_cli-1.0.0-py3-none-any.whl", hashlib.sha256(payload).hexdigest()
    )
    observed = release_consumer.ObservedArtifact(
        artifact.name, artifact.sha256, "https://files.example.test/packages/wheel"
    )

    result = release_consumer.download_verified(observed, artifact, tmp_path, fetch=lambda _url, _timeout: payload)

    assert result.path == tmp_path / artifact.name
    assert result.path.read_bytes() == payload
    assert result.sha256 == artifact.sha256


def test_digest_mismatched_download_never_materializes_a_file(tmp_path: Path) -> None:
    artifact = release_consumer.ExpectedArtifact("wheel", "fieldkit_cli-1.0.0-py3-none-any.whl", "a" * 64)
    observed = release_consumer.ObservedArtifact(
        artifact.name, artifact.sha256, "https://files.example.test/packages/wheel"
    )

    with pytest.raises(ValueError, match="download digest does not match"):
        release_consumer.download_verified(observed, artifact, tmp_path, fetch=lambda _url, _timeout: b"unexpected")

    assert not (tmp_path / artifact.name).exists()


def test_download_expected_artifacts_requires_exact_index_success(tmp_path: Path) -> None:
    expected = release_consumer.ExpectedRelease(
        "example/fieldkit-cli",
        "v1.0.0",
        "a" * 40,
        (
            release_consumer.ExpectedArtifact(
                "wheel", "fieldkit_cli-1.0.0-py3-none-any.whl", hashlib.sha256(b"wheel").hexdigest()
            ),
            release_consumer.ExpectedArtifact(
                "sdist", "fieldkit_cli-1.0.0.tar.gz", hashlib.sha256(b"sdist").hexdigest()
            ),
        ),
    )
    observed = tuple(
        release_consumer.ObservedArtifact(artifact.name, artifact.sha256, f"https://files.example.test/{artifact.name}")
        for artifact in expected.artifacts
    )
    payloads = {artifact.name: artifact.kind.encode() for artifact in expected.artifacts}

    downloaded = release_consumer.download_expected_artifacts(
        expected,
        observed,
        tmp_path,
        fetch=lambda url, _timeout: payloads[url.rsplit("/", 1)[1]],
    )

    assert [(artifact.path.name, artifact.sha256) for artifact in downloaded] == [
        (artifact.name, artifact.sha256) for artifact in expected.artifacts
    ]


def test_download_expected_artifacts_rejects_partial_index(tmp_path: Path) -> None:
    expected = release_consumer.ExpectedRelease(
        "example/fieldkit-cli",
        "v1.0.0",
        "a" * 40,
        (release_consumer.ExpectedArtifact("wheel", "fieldkit_cli-1.0.0-py3-none-any.whl", "a" * 64),),
    )

    with pytest.raises(ValueError, match="index observation is not an exact success"):
        release_consumer.download_expected_artifacts(expected, (), tmp_path, fetch=lambda _url, _timeout: b"")


def test_download_expected_artifacts_removes_verified_siblings_after_later_failure(tmp_path: Path) -> None:
    """A failed second download must not poison a retry with the first artifact."""
    wheel = release_consumer.ExpectedArtifact(
        "wheel", "fieldkit_cli-1.0.0-py3-none-any.whl", hashlib.sha256(b"wheel").hexdigest()
    )
    sdist = release_consumer.ExpectedArtifact(
        "sdist", "fieldkit_cli-1.0.0.tar.gz", hashlib.sha256(b"sdist").hexdigest()
    )
    expected = release_consumer.ExpectedRelease("example/fieldkit-cli", "v1.0.0", "a" * 40, (wheel, sdist))
    observed = tuple(
        release_consumer.ObservedArtifact(artifact.name, artifact.sha256, f"https://files.example.test/{artifact.name}")
        for artifact in expected.artifacts
    )

    with pytest.raises(ValueError, match="download digest does not match"):
        release_consumer.download_expected_artifacts(
            expected,
            observed,
            tmp_path,
            fetch=lambda url, _timeout: b"wheel" if url.endswith(wheel.name) else b"corrupt",
        )

    assert not (tmp_path / wheel.name).exists()
    assert not (tmp_path / sdist.name).exists()


def test_https_fetch_revalidates_the_final_redirect_host(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def geturl(self) -> str:
            return "https://other.example.test/artifact"

        def read(self, _size: int) -> bytes:
            return b"payload"

    monkeypatch.setattr(
        release_consumer, "build_opener", lambda _handler: SimpleNamespace(open=lambda _url, timeout: Response())
    )

    with pytest.raises(ValueError, match="artifact URL host is not allowed"):
        release_consumer.fetch_https_bytes(
            "https://files.example.test/artifact",
            timeout_seconds=5,
            allowed_download_hosts=frozenset({"files.example.test"}),
        )


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
@pytest.mark.parametrize(
    "target",
    [
        "http://forbidden.example.test/artifact",
        "ftp://forbidden.example.test/artifact",
        "https://forbidden.example.test/intermediate",
        "/relative/artifact",
        "https://files.example.test/final",
    ],
)
def test_https_redirects_are_rejected_before_transport_or_body_drain(
    monkeypatch: pytest.MonkeyPatch, code: int, target: str
) -> None:
    opened: list[str] = []
    read_sizes: list[int | None] = []
    closed_responses: list[str] = []
    bodies: list[io.BytesIO] = []
    original_opener = release_consumer.build_opener

    class RedirectBody(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            read_sizes.append(size)
            pytest.fail("redirect body must never be drained")

    class RedirectResponse(addinfourl):
        msg = "Redirect"

        def close(self) -> None:
            closed_responses.append(self.geturl())
            super().close()

    class Transport(BaseHandler):
        handler_order = 100

        def https_open(self, request: Request) -> object:
            opened.append(request.full_url)
            headers = Message()
            headers["Location"] = target if len(opened) == 1 else "https://files.example.test/final"
            body = RedirectBody(b"redirect body")
            bodies.append(body)
            return RedirectResponse(body, headers, request.full_url, code)

        http_open = https_open
        ftp_open = https_open

    def controlled_opener(handler: HTTPRedirectHandler) -> object:
        return original_opener(ProxyHandler({}), handler, Transport())

    monkeypatch.setattr(release_consumer, "build_opener", controlled_opener)
    with pytest.raises(ValueError, match="artifact redirects are not allowed"):
        release_consumer.fetch_https_bytes(
            "https://files.example.test/start",
            timeout_seconds=5,
            allowed_download_hosts=frozenset({"files.example.test"}),
        )
    assert opened == ["https://files.example.test/start"]
    assert read_sizes == []
    assert closed_responses == ["https://files.example.test/start"]
    assert len(bodies) == 1 and bodies[0].closed


def test_https_fetch_passes_an_explicit_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def geturl(self) -> str:
            return "https://files.example.test/artifact"

        def read(self, _size: int) -> bytes:
            return b"payload"

    def fake_urlopen(url: str, *, timeout: float) -> Response:
        seen["url"] = url
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setattr(release_consumer, "build_opener", lambda _handler: SimpleNamespace(open=fake_urlopen))

    assert (
        release_consumer.fetch_https_bytes(
            "https://files.example.test/artifact",
            timeout_seconds=5,
            allowed_download_hosts=frozenset({"files.example.test"}),
        )
        == b"payload"
    )
    assert seen == {"url": "https://files.example.test/artifact", "timeout": 5}


def test_https_fetch_classifies_a_timeout_as_retryable_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(_url: str, *, timeout: float) -> object:
        del timeout
        raise TimeoutError("timed out")

    monkeypatch.setattr(release_consumer, "build_opener", lambda _handler: SimpleNamespace(open=timeout))

    with pytest.raises(release_consumer.IndexTransportError, match="artifact download failed"):
        release_consumer.fetch_https_bytes(
            "https://files.example.test/artifact",
            timeout_seconds=5,
            allowed_download_hosts=frozenset({"files.example.test"}),
        )
