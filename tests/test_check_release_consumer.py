"""CLI contracts for public-index consumer evidence generation."""

import json
import subprocess
from pathlib import Path

import pytest

from scripts import check_release_consumer, release_consumer

pytestmark = pytest.mark.unit


def _expected_release() -> release_consumer.ExpectedRelease:
    return release_consumer.ExpectedRelease(
        repository="example/fieldkit-cli",
        planned_tag="v1.0.0",
        source_commit="a" * 40,
        artifacts=(release_consumer.ExpectedArtifact("wheel", "fieldkit_cli-1.0.0-py3-none-any.whl", "b" * 64),),
    )


def test_live_index_opt_in_writes_pending_evidence_atomically(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    expected = _expected_release()
    monkeypatch.setattr(check_release_consumer.release_consumer, "expected_release", lambda _bundle, _report: expected)
    monkeypatch.setattr(
        check_release_consumer.release_consumer, "fetch_index_observations", lambda *_args, **_kwargs: ()
    )
    output = tmp_path / "evidence.json"

    assert (
        check_release_consumer.main(
            [
                "--bundle",
                str(tmp_path / "bundle"),
                "--candidate-report",
                str(tmp_path / "report.json"),
                "--index-endpoint",
                "https://index.example.test/pypi/fieldkit-cli/1.0.0/json",
                "--download-host",
                "files.example.test",
                "--output",
                str(output),
                "--allow-live-index",
            ]
        )
        == 1
    )

    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert evidence["status"] == "pending"
    assert evidence["repository"] == "example/fieldkit-cli"


def test_cli_requires_explicit_live_index_opt_in(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        check_release_consumer.main(
            [
                "--bundle",
                str(tmp_path / "bundle"),
                "--candidate-report",
                str(tmp_path / "report.json"),
                "--index-endpoint",
                "https://index.example.test/pypi/fieldkit-cli/1.0.0/json",
                "--download-host",
                "files.example.test",
                "--output",
                str(tmp_path / "evidence.json"),
            ]
        )
        == 2
    )

    assert "--allow-live-index is required" in capsys.readouterr().err


def test_cli_download_verification_requires_a_destination(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        check_release_consumer.main(
            [
                "--bundle",
                str(tmp_path / "bundle"),
                "--candidate-report",
                str(tmp_path / "report.json"),
                "--index-endpoint",
                "https://index.example.test/pypi/fieldkit-cli/1.0.0/json",
                "--download-host",
                "files.example.test",
                "--output",
                str(tmp_path / "evidence.json"),
                "--allow-live-index",
                "--verify-downloads",
            ]
        )
        == 2
    )

    assert "--download-dir is required" in capsys.readouterr().err


def test_cli_download_verification_uses_exact_successful_observation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    expected = release_consumer.ExpectedRelease(
        "example/fieldkit-cli",
        "v1.0.0",
        "a" * 40,
        (
            release_consumer.ExpectedArtifact("wheel", "fieldkit_cli-1.0.0-py3-none-any.whl", "b" * 64),
            release_consumer.ExpectedArtifact("sdist", "fieldkit_cli-1.0.0.tar.gz", "c" * 64),
        ),
    )
    observed = tuple(
        release_consumer.ObservedArtifact(artifact.name, artifact.sha256, f"https://files.example.test/{artifact.name}")
        for artifact in expected.artifacts
    )
    seen: dict[str, object] = {}
    monkeypatch.setattr(check_release_consumer.release_consumer, "expected_release", lambda _bundle, _report: expected)
    monkeypatch.setattr(
        check_release_consumer.release_consumer, "fetch_index_observations", lambda *_args, **_kwargs: observed
    )

    def fake_download(
        actual_expected: release_consumer.ExpectedRelease,
        actual_observed: tuple[release_consumer.ObservedArtifact, ...],
        destination: Path,
        **kwargs: object,
    ) -> tuple[release_consumer.DownloadedArtifact, ...]:
        seen.update(expected=actual_expected, observed=actual_observed, destination=destination, **kwargs)
        return tuple(
            release_consumer.DownloadedArtifact(destination / artifact.name, artifact.sha256)
            for artifact in actual_expected.artifacts
        )

    monkeypatch.setattr(check_release_consumer.release_consumer, "download_expected_artifacts", fake_download)

    assert (
        check_release_consumer.main(
            [
                "--bundle",
                str(tmp_path / "bundle"),
                "--candidate-report",
                str(tmp_path / "report.json"),
                "--index-endpoint",
                "https://index.example.test/pypi/fieldkit-cli/1.0.0/json",
                "--download-host",
                "files.example.test",
                "--output",
                str(tmp_path / "evidence.json"),
                "--download-dir",
                str(tmp_path),
                "--allow-live-index",
                "--verify-downloads",
            ]
        )
        == 1
    )

    assert seen["expected"] == expected
    assert seen["observed"] == observed
    assert seen["destination"] == tmp_path
    evidence = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    assert [artifact["downloaded"] for artifact in evidence["artifacts"]] == [True, True]
    assert evidence["status"] == "pending"
    assert evidence["findings"] == ["offline scenario verification not requested"]


def test_cli_creates_an_absent_download_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    expected = _expected_release()
    observed = (release_consumer.ObservedArtifact(expected.artifacts[0].name, expected.artifacts[0].sha256),)
    destination = tmp_path / "consumer-downloads"
    monkeypatch.setattr(check_release_consumer.release_consumer, "expected_release", lambda _bundle, _report: expected)
    monkeypatch.setattr(
        check_release_consumer.release_consumer,
        "poll_index_observations",
        lambda *_args, **_kwargs: release_consumer.PollResult(observed, release_consumer.IndexReport("success", ())),
    )

    def fake_download(
        _expected: release_consumer.ExpectedRelease,
        _observed: tuple[release_consumer.ObservedArtifact, ...],
        actual_destination: Path,
        **_kwargs: object,
    ) -> tuple[release_consumer.DownloadedArtifact, ...]:
        assert actual_destination == destination
        assert actual_destination.is_dir()
        return ()

    monkeypatch.setattr(check_release_consumer.release_consumer, "download_expected_artifacts", fake_download)

    assert (
        check_release_consumer.main(
            [
                "--bundle",
                str(tmp_path / "bundle"),
                "--candidate-report",
                str(tmp_path / "report.json"),
                "--index-endpoint",
                "https://index.example.test/pypi/fieldkit-cli/1.0.0/json",
                "--download-host",
                "files.example.test",
                "--download-dir",
                str(destination),
                "--output",
                str(tmp_path / "evidence.json"),
                "--allow-live-index",
                "--verify-downloads",
            ]
        )
        == 1
    )


def test_cli_writes_failed_evidence_when_offline_scenarios_time_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    expected = _expected_release()
    observed = (release_consumer.ObservedArtifact(expected.artifacts[0].name, expected.artifacts[0].sha256),)
    monkeypatch.setattr(check_release_consumer.release_consumer, "expected_release", lambda _bundle, _report: expected)
    monkeypatch.setattr(
        check_release_consumer.release_consumer,
        "poll_index_observations",
        lambda *_args, **_kwargs: release_consumer.PollResult(observed, release_consumer.IndexReport("success", ())),
    )
    monkeypatch.setattr(
        check_release_consumer.release_consumer,
        "download_expected_artifacts",
        lambda *_args, **_kwargs: (
            release_consumer.DownloadedArtifact(tmp_path / expected.artifacts[0].name, expected.artifacts[0].sha256),
        ),
    )
    monkeypatch.setattr(
        check_release_consumer.release_consumer,
        "run_offline_scenarios",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(["fieldkit"], 60)),
    )
    output = tmp_path / "evidence.json"

    assert (
        check_release_consumer.main(
            [
                "--bundle",
                str(tmp_path / "bundle"),
                "--candidate-report",
                str(tmp_path / "report.json"),
                "--index-endpoint",
                "https://index.example.test/pypi/fieldkit-cli/1.0.0/json",
                "--download-host",
                "files.example.test",
                "--download-dir",
                str(tmp_path),
                "--output",
                str(output),
                "--allow-live-index",
                "--verify-downloads",
                "--verify-install",
            ]
        )
        == 1
    )

    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert evidence["status"] == "failed"
    assert evidence["findings"] == ["offline verification timed out"]


def test_cli_install_verification_requires_download_verification(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        check_release_consumer.main(
            [
                "--bundle",
                str(tmp_path / "bundle"),
                "--candidate-report",
                str(tmp_path / "report.json"),
                "--index-endpoint",
                "https://index.example.test/pypi/fieldkit-cli/1.0.0/json",
                "--download-host",
                "files.example.test",
                "--output",
                str(tmp_path / "evidence.json"),
                "--allow-live-index",
                "--verify-install",
            ]
        )
        == 2
    )

    assert "--verify-install requires --verify-downloads" in capsys.readouterr().err


def test_cli_passes_explicit_polling_bounds_to_the_consumer_library(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    expected = _expected_release()
    seen: dict[str, object] = {}
    monkeypatch.setattr(check_release_consumer.release_consumer, "expected_release", lambda _bundle, _report: expected)

    def fake_poll(actual_expected: release_consumer.ExpectedRelease, **kwargs: object) -> release_consumer.PollResult:
        seen["expected"] = actual_expected
        seen.update(kwargs)
        return release_consumer.PollResult((), release_consumer.IndexReport("pending", ("index unavailable",)))

    monkeypatch.setattr(check_release_consumer.release_consumer, "poll_index_observations", fake_poll)

    assert (
        check_release_consumer.main(
            [
                "--bundle",
                str(tmp_path / "bundle"),
                "--candidate-report",
                str(tmp_path / "report.json"),
                "--index-endpoint",
                "https://index.example.test/pypi/fieldkit-cli/1.0.0/json",
                "--download-host",
                "files.example.test",
                "--output",
                str(tmp_path / "evidence.json"),
                "--allow-live-index",
                "--max-attempts",
                "2",
                "--poll-interval-seconds",
                "0",
            ]
        )
        == 1
    )

    assert seen["expected"] == expected
    assert seen["max_attempts"] == 2
    assert seen["poll_interval_seconds"] == 0


def test_cli_writes_failed_evidence_after_a_verified_candidate_and_invalid_index(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    expected = _expected_release()
    monkeypatch.setattr(check_release_consumer.release_consumer, "expected_release", lambda _bundle, _report: expected)
    monkeypatch.setattr(
        check_release_consumer.release_consumer,
        "poll_index_observations",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("index response is invalid JSON")),
    )
    output = tmp_path / "evidence.json"

    assert (
        check_release_consumer.main(
            [
                "--bundle",
                str(tmp_path / "bundle"),
                "--candidate-report",
                str(tmp_path / "report.json"),
                "--index-endpoint",
                "https://index.example.test/pypi/fieldkit-cli/1.0.0/json",
                "--download-host",
                "files.example.test",
                "--output",
                str(output),
                "--allow-live-index",
            ]
        )
        == 1
    )

    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert evidence["status"] == "failed"
    assert evidence["findings"] == ["index response is invalid JSON"]
