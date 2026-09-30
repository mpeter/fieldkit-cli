"""Successor preparation stays unpublished until its bundle contract is integrated."""

import gzip
import hashlib
import io
import json
import tarfile
import tomllib
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from scripts import check_artifacts, check_public_candidate, public_history_source, release_wheelhouse
from tests.test_public_history_source import history as history
from tests.test_public_tree_scan import History, _successor_snapshot

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "attack", ["dirty", "tag", "repository", "revision", "missing-trust", "record", "contract", "initial-trust"]
)
def test_successor_candidate_rejects_identity_before_build(
    history: History, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attack: str
) -> None:
    repo, anchor, record = history
    _snapshot, _retained, _policy, source = _successor_snapshot(history, tmp_path)

    def unexpected_build(*args: object, **kwargs: object) -> tuple[Path, ...]:
        pytest.fail("untrusted source reached build")

    monkeypatch.setattr(check_public_candidate, "_build_export", unexpected_build)
    if attack == "dirty":
        (repo / "README.md").write_text("dirty", encoding="utf-8")
    elif attack == "record":
        record += b"\n"
    elif attack == "contract":
        source = replace(source, source_tree="f" * 40)
    output = tmp_path / "candidate"
    with pytest.raises(ValueError):
        check_public_candidate.check_candidate(
            repo,
            anchor.initial_commit if attack == "revision" else "HEAD",
            expected_repository="other/fieldkit-cli" if attack == "repository" else anchor.repository,
            planned_tag="v1.0.0" if attack == "tag" else "v1.1.0",
            output_dir=output,
            public_history=None if attack == "initial-trust" else source,
            expected_anchor=None if attack == "missing-trust" else anchor,
            cutover_record=record,
        )
    assert not output.exists()


def _build(_snapshot: Path, destination: Path) -> tuple[Path, ...]:
    destination.mkdir()
    version = tomllib.loads((_snapshot / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    wheel = destination / f"example_cli-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(zipfile.ZipInfo("example/safe.py", date_time=(1980, 1, 1, 0, 0, 0)), "safe = True\n")
    sdist = destination / f"example_cli-{version}.tar.gz"
    payload = b"safe = True\n"
    member = tarfile.TarInfo(f"example_cli-{version}/src/example/safe.py")
    member.size = len(payload)
    tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode="w") as archive:
        archive.addfile(member, io.BytesIO(payload))
    sdist.write_bytes(gzip.compress(tar_bytes.getvalue(), mtime=0))
    return wheel, sdist


@pytest.mark.parametrize(
    ("attack", "version"),
    [("none", "1.1.0"), ("none", "1.0.1"), ("snapshot-after-license", "1.1.0"), ("source-after-license", "1.1.0")],
)
def test_successor_prepares_versioned_evidence_but_bundle_stays_fail_closed(
    history: History, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attack: str, version: str
) -> None:
    repo, anchor, record = history
    _snapshot, _retained, _policy, source = _successor_snapshot(history, tmp_path, version=version)
    monkeypatch.setattr(check_public_candidate, "_build_export", _build)

    def validate(
        artifacts: tuple[Path, ...], _snapshot: Path, *, source_revision: str, tracked_payload_paths: tuple[str, ...]
    ) -> check_artifacts.ValidationReport:
        assert source_revision == source.source_commit
        assert tracked_payload_paths == tuple(entry.path for entry in source.entries)
        return check_artifacts.ValidationReport(
            1,
            source_revision,
            tuple(
                check_artifacts.ArtifactResult(
                    path.name,
                    "wheel" if path.suffix == ".whl" else "sdist",
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    (check_artifacts.CriterionResult("TEST", "pass"),),
                )
                for path in artifacts
            ),
        )

    monkeypatch.setattr(check_artifacts, "validate", validate)

    def licenses(
        snapshot: Path, staging: Path, captured: public_history_source.PublicHistorySource
    ) -> dict[str, object]:
        assert captured == source
        (staging / "runtime-requirements.txt").write_text("example==1.1.0\n", encoding="utf-8")
        (staging / "release-build-requirements.txt").write_text("hatchling==1.26.3\n", encoding="utf-8")
        if attack == "snapshot-after-license":
            (snapshot / "README.md").write_text("tampered", encoding="utf-8")
        elif attack == "source-after-license":
            (repo / "README.md").write_text("dirty", encoding="utf-8")
        return {"status": "pass", "revision": captured.source_commit, "export_policy_sha256": captured.policy_sha256}

    monkeypatch.setattr(check_public_candidate, "_license_evidence", licenses)

    def collect(*_requirements: Path, wheelhouse: Path) -> dict[str, object]:
        wheelhouse.mkdir()
        return {}

    def archive(_wheelhouse: Path, destination: Path) -> str:
        destination.write_bytes(b"wheelhouse")
        return hashlib.sha256(b"wheelhouse").hexdigest()

    monkeypatch.setattr(release_wheelhouse, "collect", collect)
    monkeypatch.setattr(release_wheelhouse, "archive", archive)
    output = tmp_path / "candidate"
    with pytest.raises(
        ValueError, match="unsupported schema or status" if attack == "none" else "bytes or mode changed|clean worktree"
    ):
        check_public_candidate.check_candidate(
            repo,
            "HEAD",
            expected_repository=anchor.repository,
            planned_tag=source.planned_tag,
            output_dir=output,
            public_history=source,
            expected_anchor=anchor,
            cutover_record=record,
        )
    assert not output.exists()
    reports = tuple(tmp_path.glob(".candidate-*/report.json"))
    if attack != "none":
        assert not reports
        return
    assert len(reports) == 1
    retained = json.loads(reports[0].read_text(encoding="utf-8"))
    assert retained["schema_version"] == 8
    assert retained["status"] == "pending"
    assert retained["checks_status"] == "pass"
    assert retained["bundle_status"] == "not-assembled"
    assert "export_manifest" not in retained
    assert retained["source_kind"] == "public-history"
    assert retained["source"]["source_tree"] == source.source_tree != anchor.initial_tree
    assert retained["source"]["anchor"]["initial_tree"] == anchor.initial_tree
    assert (
        retained["license_evidence"]["revision"]
        == retained["artifact_validation"]["source_revision"]
        == source.source_commit
    )
    assert retained["scan"]["schema_version"] == 2
    assert retained["scan"]["source_sha256"] == retained["source_sha256"]
    assert retained["scan"]["anchor"] == retained["source"]["anchor"]
    assert not (reports[0].parent / "bundle").exists()
