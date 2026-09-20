"""Contracts for retained release-approval inputs."""

import hashlib
import json
from pathlib import Path

import pytest

from scripts import release_approval_input

pytestmark = pytest.mark.unit


def _write_input(root: Path) -> None:
    members = {
        "private-candidate-report.json": json.dumps(
            {"status": "pass", "export_manifest": {"source_commit": "a" * 40, "exported_tree": "b" * 40}}
        ).encode(),
        "public-candidate/report.json": json.dumps(
            {
                "status": "pass",
                "export_manifest": {"source_commit": "e" * 40, "exported_tree": "b" * 40},
                "artifact_validation": {
                    "artifacts": [
                        {"name": "fieldkit.whl", "kind": "wheel", "sha256": "c" * 64, "status": "pass"},
                        {"name": "fieldkit.tar.gz", "kind": "sdist", "sha256": "d" * 64, "status": "pass"},
                    ]
                },
            }
        ).encode(),
        "public-candidate/bundle/SHA256SUMS": b"",
        "evidence/ledger.json": b'{"criteria": [{"id": "cutover-approval", "record": {"path": "evidence/record.json"}}]}',
        "cutover-record.json": b"{}",
        "evidence/record.json": b"{}",
    }
    for name, data in members.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest = {
        "schema_version": 1,
        "status": "pass",
        "candidate": {
            "repository": "example/fieldkit-cli",
            "private_source_sha": "a" * 40,
            "exported_tree": "b" * 40,
            "planned_tag": "v1.0.0",
            "artifacts": [
                {"name": "fieldkit.whl", "kind": "wheel", "sha256": "c" * 64},
                {"name": "fieldkit.tar.gz", "kind": "sdist", "sha256": "d" * 64},
            ],
        },
        "public": {"initial_commit": "e" * 40, "initial_tree": "b" * 40},
        "files": [{"path": name, "sha256": hashlib.sha256(data).hexdigest()} for name, data in members.items()],
    }
    (root / "approval-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_verify_revalidates_every_retained_release_component(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_input(tmp_path)
    observed: list[str] = []
    monkeypatch.setattr(
        release_approval_input.release_check, "_manual_criteria", lambda *_args: observed.append("ledger")
    )
    monkeypatch.setattr(
        release_approval_input.release_bundle, "verify", lambda *_args, **_kwargs: observed.append("bundle")
    )
    monkeypatch.setattr(
        release_approval_input.cutover_record, "validate", lambda *_args, **_kwargs: observed.append("cutover")
    )
    monkeypatch.setattr(release_approval_input, "_cutover_record_digest", lambda *_args: observed.append("binding"))

    assert release_approval_input.verify(tmp_path, repo_root=Path(__file__).parents[1])["status"] == "pass"
    assert observed == ["ledger", "bundle", "cutover", "binding"]


def test_verify_rejects_substituted_member(tmp_path: Path) -> None:
    _write_input(tmp_path)
    (tmp_path / "evidence/ledger.json").write_text('{"substituted": true}', encoding="utf-8")
    with pytest.raises(ValueError, match="digest does not match"):
        release_approval_input.verify(tmp_path, repo_root=Path(__file__).parents[1])


def test_verify_rejects_public_candidate_from_another_cutover(tmp_path: Path) -> None:
    _write_input(tmp_path)
    report = json.loads((tmp_path / "public-candidate/report.json").read_text(encoding="utf-8"))
    report["export_manifest"]["source_commit"] = "f" * 40
    path = tmp_path / "public-candidate/report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    manifest_path = tmp_path / "approval-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        if entry["path"] == "public-candidate/report.json":
            entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="identities are not bound"):
        release_approval_input.verify(tmp_path, repo_root=Path(__file__).parents[1])


def test_verify_rejects_evidence_record_not_listed_in_manifest(tmp_path: Path) -> None:
    _write_input(tmp_path)
    ledger_path = tmp_path / "evidence/ledger.json"
    ledger_path.write_text(
        '{"criteria": [{"id": "cutover-approval", "record": {"path": "evidence/unretained.json"}}]}',
        encoding="utf-8",
    )
    manifest_path = tmp_path / "approval-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        if entry["path"] == "evidence/ledger.json":
            entry["sha256"] = hashlib.sha256(ledger_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="does not retain every evidence record"):
        release_approval_input.verify(tmp_path, repo_root=Path(__file__).parents[1])
