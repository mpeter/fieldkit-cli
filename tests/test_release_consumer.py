"""Contracts for public-index release consumer verification."""

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scripts import release_bundle, release_consumer, release_wheelhouse

pytestmark = pytest.mark.unit


def _write(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _candidate(tmp_path: Path) -> Path:
    candidate = tmp_path / "candidate"
    wheel = "fieldkit_cli-1.0.0-py3-none-any.whl"
    sdist = "fieldkit_cli-1.0.0.tar.gz"
    wheel_digest = _write(candidate / "dist" / wheel, b"wheel payload")
    sdist_digest = _write(candidate / "dist" / sdist, b"sdist payload")
    sbom_digest = _write(candidate / "locked-graph.cdx.json", b'{"components": []}\n')
    requirements_digest = _write(
        candidate / "runtime-requirements.txt",
        b"click==8.5.0 --hash=sha256:" + b"a" * 64 + b"\n",
    )
    build_requirements_digest = _write(
        candidate / "release-build-requirements.txt",
        b"hatchling==1.32.0 --hash=sha256:" + b"b" * 64 + b"\n",
    )
    wheelhouse_digest = _write(candidate / "runtime-wheelhouse.zip", b"wheelhouse payload")
    revision = "a" * 40
    report = {
        "schema_version": 6,
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
            "schema_version": 1,
            "status": "pass",
            "scope": "runtime-all-extras",
            "revision": revision,
            "export_policy_sha256": "d" * 64,
            "sbom_sha256": sbom_digest,
            "observed_packages": 0,
            "packages": [],
            "findings": [],
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
    assert [(artifact.kind, artifact.name) for artifact in expected.artifacts] == [
        ("sdist", "fieldkit_cli-1.0.0.tar.gz"),
        ("wheel", "fieldkit_cli-1.0.0-py3-none-any.whl"),
    ]


def test_wheelhouse_extraction_requires_a_verified_bundle_and_rejects_unsafe_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
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
    with zipfile.ZipFile(bundle / "runtime-wheelhouse.zip", "w") as archive:
        archive.writestr("runtime-wheelhouse.json", json.dumps(manifest))
        for target in release_wheelhouse.supported_targets():
            archive.writestr(f"{target.name}/click.whl", b"wheel")
    report = tmp_path / "report.json"
    report.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(release_bundle, "verify", lambda *_args, **_kwargs: None)

    extracted = release_consumer.extract_runtime_wheelhouse(bundle, report, tmp_path / "wheelhouse")

    assert (extracted / "linux-x86_64-python311/click.whl").read_bytes() == b"wheel"


def test_offline_install_uses_only_the_verified_target_wheelhouse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    artifact_path = Path("fieldkit_cli-1.0.0-py3-none-any.whl")
    artifact_path.write_bytes(b"artifact")
    digest = hashlib.sha256(b"artifact").hexdigest()
    wheelhouse = tmp_path / "wheelhouse"
    (wheelhouse / "linux-x86_64-python311").mkdir(parents=True)
    monkeypatch.setattr(release_consumer, "extract_runtime_wheelhouse", lambda *_args: wheelhouse)
    commands: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    dependencies, installed = release_consumer.install_offline(
        tmp_path / "bundle",
        tmp_path / "report.json",
        release_consumer.DownloadedArtifact(artifact_path, digest),
        release_consumer.ExpectedArtifact("wheel", artifact_path.name, digest),
        tmp_path / "venv/bin/python",
        tmp_path / "extract",
        system="linux",
        machine="x86_64",
        python_version="3.11",
        runner=runner,
    )

    assert installed is not None
    assert dependencies.returncode == installed.returncode == 0
    assert commands[0][3:9] == [
        "install",
        "--no-index",
        "--find-links",
        str(wheelhouse / "linux-x86_64-python311"),
        "--require-hashes",
        "--requirement",
    ]
    assert commands[1][3:7] == ["install", "--no-index", "--no-deps", str((tmp_path / artifact_path).resolve())]


def test_offline_install_resolves_candidate_inputs_before_changing_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    artifact_path = Path("fieldkit_cli-1.0.0-py3-none-any.whl")
    artifact_path.write_bytes(b"artifact")
    digest = hashlib.sha256(b"artifact").hexdigest()
    monkeypatch.setattr(release_consumer, "extract_runtime_wheelhouse", lambda *_args: tmp_path)
    commands: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    release_consumer.install_offline(
        Path("bundle"),
        Path("report.json"),
        release_consumer.DownloadedArtifact(artifact_path, digest),
        release_consumer.ExpectedArtifact("wheel", artifact_path.name, digest),
        tmp_path / "venv/bin/python",
        tmp_path / "extract",
        system="linux",
        machine="x86_64",
        python_version="3.11",
        cwd=tmp_path,
        runner=runner,
    )

    assert commands[0][-1] == str((tmp_path / "bundle/runtime-requirements.txt").resolve())
    assert commands[1][-1] == str((tmp_path / artifact_path).resolve())


def test_offline_sdist_install_bootstraps_only_the_locked_build_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_path = tmp_path / "fieldkit_cli-1.0.0.tar.gz"
    artifact_path.write_bytes(b"artifact")
    digest = hashlib.sha256(b"artifact").hexdigest()
    wheelhouse = tmp_path / "wheelhouse"
    (wheelhouse / "linux-x86_64-python311").mkdir(parents=True)
    monkeypatch.setattr(release_consumer, "extract_runtime_wheelhouse", lambda *_args: wheelhouse)
    commands: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    _, installed = release_consumer.install_offline(
        tmp_path / "bundle",
        tmp_path / "report.json",
        release_consumer.DownloadedArtifact(artifact_path, digest),
        release_consumer.ExpectedArtifact("sdist", artifact_path.name, digest),
        tmp_path / "venv/bin/python",
        tmp_path / "extract",
        system="linux",
        machine="x86_64",
        python_version="3.11",
        runner=runner,
    )

    assert installed is not None
    assert commands[1][-1] == str((tmp_path / "bundle/release-build-requirements.txt").resolve())
    assert commands[2][-2:] == ["--no-build-isolation", str(artifact_path.resolve())]


def test_offline_install_does_not_install_the_artifact_after_dependency_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_path = tmp_path / "fieldkit_cli-1.0.0-py3-none-any.whl"
    artifact_path.write_bytes(b"artifact")
    digest = hashlib.sha256(b"artifact").hexdigest()
    monkeypatch.setattr(release_consumer, "extract_runtime_wheelhouse", lambda *_args: tmp_path)
    calls = 0

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(command, 1, "", "missing wheel")

    dependencies, installed = release_consumer.install_offline(
        tmp_path / "bundle",
        tmp_path / "report.json",
        release_consumer.DownloadedArtifact(artifact_path, digest),
        release_consumer.ExpectedArtifact("wheel", artifact_path.name, digest),
        tmp_path / "venv/bin/python",
        tmp_path / "extract",
        system="linux",
        machine="x86_64",
        python_version="3.11",
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

    monkeypatch.setattr(release_consumer, "urlopen", lambda _url, timeout: Response())

    with pytest.raises(ValueError, match="artifact URL host is not allowed"):
        release_consumer.fetch_https_bytes(
            "https://files.example.test/artifact",
            timeout_seconds=5,
            allowed_download_hosts=frozenset({"files.example.test"}),
        )


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

    monkeypatch.setattr(release_consumer, "urlopen", fake_urlopen)

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

    monkeypatch.setattr(release_consumer, "urlopen", timeout)

    with pytest.raises(release_consumer.IndexTransportError, match="artifact download failed"):
        release_consumer.fetch_https_bytes(
            "https://files.example.test/artifact",
            timeout_seconds=5,
            allowed_download_hosts=frozenset({"files.example.test"}),
        )
