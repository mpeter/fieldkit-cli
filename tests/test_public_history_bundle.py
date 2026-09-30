"""Successor bundles are complete local preparation, never release approval."""

import errno
import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from scripts import (
    _release_governance,
    check_artifacts,
    public_history_source,
    public_tree_scan,
    release_bundle,
)
from scripts._public_history_bundle import PreparationBundleReport
from tests import release_bundle_support
from tests.test_public_history_candidate import _build
from tests.test_public_history_source import history as history
from tests.test_public_tree_scan import History, _successor_snapshot

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("boundary", ["staged", "published"])
@pytest.mark.parametrize("failure", [errno.ENOSPC, errno.EMFILE])
def test_marker_allocation_failure_still_invalidates_and_quarantines(
    history: History, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str, failure: int
) -> None:
    candidate, _source, payload = _prepared(history, tmp_path)
    original_verify = release_bundle._verify_open_bundle
    original_write = release_bundle._write_bytes_at
    calls = 0

    def verify(descriptor: int, **kwargs: object) -> PreparationBundleReport:
        nonlocal calls
        result = original_verify(
            descriptor, candidate_report=payload, expected_anchor=history[1], cutover_record=history[2]
        )
        assert isinstance(result, PreparationBundleReport)
        calls += 1
        if calls == (1 if boundary == "staged" else 2):
            (history[0] / "README.md").write_text("changed at publication boundary", encoding="utf-8")
        return result

    def write(descriptor: int, name: str, data: bytes) -> None:
        if name == "bundle-rejection.txt":
            raise OSError(failure, "sensitive allocation details")
        original_write(descriptor, name, data)

    monkeypatch.setattr(release_bundle, "_verify_open_bundle", verify)
    monkeypatch.setattr(release_bundle, "_write_bytes_at", write)
    with pytest.raises(ValueError, match="successor rejected bundle retention failed") as caught:
        release_bundle.materialize(
            candidate, expected_anchor=history[1], cutover_record=history[2], source_repo=history[0]
        )
    assert str(caught.value) == "successor rejected bundle retention failed"
    retained = [path for path in candidate.iterdir() if path.name.startswith(".bundle") or path.name == "bundle"]
    assert len(retained) == 1
    retained_name = retained[0].name
    retained[0].rename(candidate / "renamed-rejection")
    monkeypatch.setattr(release_bundle, "_verify_open_bundle", original_verify)
    with pytest.raises(ValueError, match="rejected"):
        release_bundle.verify(
            candidate / "renamed-rejection",
            candidate_report=payload,
            expected_anchor=history[1],
            cutover_record=history[2],
        )
    assert retained_name.startswith(".bundle-rejected-")
    assert not (candidate / "bundle").exists()
    assert not (candidate / "renamed-rejection/SHA256SUMS").exists()
    assert (candidate / "renamed-rejection/rejected-SHA256SUMS").is_file()


def test_published_quarantine_failure_does_not_skip_other_invalidation(
    history: History, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate, _source, payload = _prepared(history, tmp_path)
    original_verify = release_bundle._verify_open_bundle
    original_write = release_bundle._write_bytes_at
    original_rename = release_bundle._rename_no_replace_at
    calls = 0
    quarantine_attempts = 0

    def verify(descriptor: int, **kwargs: object) -> PreparationBundleReport:
        nonlocal calls
        result = original_verify(
            descriptor, candidate_report=payload, expected_anchor=history[1], cutover_record=history[2]
        )
        assert isinstance(result, PreparationBundleReport)
        calls += 1
        if calls == 2:
            (history[0] / "README.md").write_text("changed after publication", encoding="utf-8")
        return result

    def write(descriptor: int, name: str, data: bytes) -> None:
        if name == "bundle-rejection.txt":
            raise OSError(errno.EMFILE, "sensitive allocation details")
        original_write(descriptor, name, data)

    def rename(descriptor: int, source: str, destination: str) -> None:
        nonlocal quarantine_attempts
        if destination.startswith(".bundle-rejected-"):
            quarantine_attempts += 1
            raise OSError(errno.EACCES, "sensitive quarantine details")
        original_rename(descriptor, source, destination)

    monkeypatch.setattr(release_bundle, "_verify_open_bundle", verify)
    monkeypatch.setattr(release_bundle, "_write_bytes_at", write)
    monkeypatch.setattr(release_bundle, "_rename_no_replace_at", rename)
    with pytest.raises(ValueError, match="successor rejected bundle retention failed"):
        release_bundle.materialize(
            candidate, expected_anchor=history[1], cutover_record=history[2], source_repo=history[0]
        )
    assert quarantine_attempts == 1
    assert not list(candidate.glob(".bundle-rejected-*"))
    bundle = candidate / "bundle"
    assert not (bundle / "SHA256SUMS").exists()
    bundle.rename(candidate / "renamed-rejection")
    monkeypatch.setattr(release_bundle, "_verify_open_bundle", original_verify)
    with pytest.raises(ValueError, match="rejected"):
        release_bundle.verify(
            candidate / "renamed-rejection",
            candidate_report=payload,
            expected_anchor=history[1],
            cutover_record=history[2],
        )


@pytest.mark.parametrize(
    "field,value", [("artifact_validation", True), ("artifact_validation", 1.0), ("license_evidence", 2.0)]
)
def test_successor_nested_receipts_require_integer_schema(
    history: History, tmp_path: Path, field: str, value: bool | float
) -> None:
    candidate, _source, payload = _prepared(history, tmp_path)
    report = json.loads(payload)
    report[field]["schema_version"] = value
    (candidate / "report.json").write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match=r"unsupported|schema"):
        release_bundle.materialize(
            candidate, expected_anchor=history[1], cutover_record=history[2], source_repo=history[0]
        )
    assert not (candidate / "bundle").exists()


@pytest.mark.parametrize(
    "mutation", ["compact", "reordered", "nested", "alternate-source", "extra", "missing", "missing-rehashed"]
)
def test_offline_successor_requires_exact_closed_canonical_bytes(
    history: History, tmp_path: Path, mutation: str
) -> None:
    candidate, source, payload = _prepared(history, tmp_path)
    result = release_bundle.materialize(
        candidate, expected_anchor=history[1], cutover_record=history[2], source_repo=history[0]
    )
    assert result.ok is True
    bundle = candidate / "bundle"
    path = bundle / "bundle-provenance.json"
    expected_error = "provenance"
    if mutation == "alternate-source":
        path = bundle / "public-history-source.json"
        alternate = replace(source, version="1.2.0", planned_tag="v1.2.0")
        changed = public_history_source.source_bytes(alternate, expected_anchor=history[1], cutover_record=history[2])
        parsed = public_history_source.source_from_bytes(changed, expected_anchor=history[1], cutover_record=history[2])
        assert parsed.version == "1.2.0"
        expected_error = "candidate source digest does not match the source contract"
    elif mutation == "nested":
        changed = b"[" * 5000 + b"0" + b"]" * 5000
        expected_error = "exact preparation candidate"
    elif mutation == "extra":
        (bundle / "extra.txt").write_bytes(b"extra")
        changed = path.read_bytes()
        expected_error = "unexpected or missing files"
    elif mutation in {"missing", "missing-rehashed"}:
        (bundle / "public-history-source.json").unlink()
        changed = path.read_bytes()
        expected_error = "complete source contract" if mutation == "missing-rehashed" else "unexpected or missing files"
    else:
        value = json.loads(path.read_bytes())
        changed = (
            json.dumps(dict(reversed(value.items())), indent=2)
            if mutation == "reordered"
            else json.dumps(value, sort_keys=True)
        ).encode() + b"\n"
    path.write_bytes(changed)
    checksums = tuple(
        (name, hashlib.sha256(changed).hexdigest() if name == path.name else digest)
        for name, digest in result.files
        if mutation != "missing-rehashed" or name != "public-history-source.json"
    )
    (bundle / "SHA256SUMS").write_bytes(release_bundle._checksum_manifest(checksums))
    with pytest.raises(ValueError, match=expected_error):
        release_bundle.verify(bundle, candidate_report=payload, expected_anchor=history[1], cutover_record=history[2])


def test_successor_nested_candidate_json_is_sanitized(history: History, tmp_path: Path) -> None:
    candidate, _source, _payload = _prepared(history, tmp_path)
    (candidate / "report.json").write_bytes(b"[" * 5000 + b"0" + b"]" * 5000)
    with pytest.raises(ValueError, match="JSON input exceeds nesting limit"):
        release_bundle.materialize(
            candidate, expected_anchor=history[1], cutover_record=history[2], source_repo=history[0]
        )


@pytest.mark.parametrize("retention_failure", ["none", "checksum", "quarantine"])
@pytest.mark.parametrize("error_type", [KeyboardInterrupt, ValueError])
def test_successor_cancellation_retains_rejection_and_original_category(
    history: History,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retention_failure: str,
    error_type: type[BaseException],
) -> None:
    candidate, _source, payload = _prepared(history, tmp_path)
    original_verify = release_bundle._verify_open_bundle
    original_rename = release_bundle._rename_no_replace_at

    def cancel(*_args: object, **_kwargs: object) -> None:
        raise error_type("cancelled preparation")

    def rename(descriptor: int, source: str, destination: str) -> None:
        if (retention_failure == "checksum" and source == "SHA256SUMS") or (
            retention_failure == "quarantine" and destination.startswith(".bundle-rejected-")
        ):
            raise OSError("sensitive syscall details")
        original_rename(descriptor, source, destination)

    monkeypatch.setattr(release_bundle, "_verify_open_bundle", cancel)
    monkeypatch.setattr(release_bundle, "_rename_no_replace_at", rename)
    message = (
        "retention failed" if error_type is ValueError and retention_failure != "none" else "cancelled preparation"
    )
    with pytest.raises(error_type, match=message) as caught:
        release_bundle.materialize(
            candidate, expected_anchor=history[1], cutover_record=history[2], source_repo=history[0]
        )
    assert "sensitive" not in str(caught.value)
    if retention_failure != "none" and error_type is KeyboardInterrupt:
        assert caught.value.__notes__ == ["successor rejected bundle retention failed"]
    monkeypatch.setattr(release_bundle, "_verify_open_bundle", original_verify)
    retained = next(path for path in candidate.iterdir() if path.name.startswith(".bundle"))
    retained.rename(candidate / "renamed-rejection")
    with pytest.raises(ValueError, match="rejected"):
        release_bundle.verify(
            candidate / "renamed-rejection",
            candidate_report=payload,
            expected_anchor=history[1],
            cutover_record=history[2],
        )


def _prepared(
    history: History, tmp_path: Path, *, version: str = "1.1.0"
) -> tuple[Path, public_history_source.PublicHistorySource, bytes]:
    repo, anchor, record = history
    snapshot, retained, policy, source = _successor_snapshot(history, tmp_path, version=version)
    candidate = release_bundle_support.candidate(tmp_path)
    report = json.loads((candidate / "report.json").read_bytes())
    for artifact in (candidate / "dist").iterdir():
        artifact.unlink()
    (candidate / "dist").rmdir()
    artifacts = _build(snapshot, candidate / "dist")
    report["artifact_validation"]["source_revision"] = source.source_commit
    for item, artifact in zip(report["artifact_validation"]["artifacts"], artifacts, strict=True):
        item.update(name=artifact.name, sha256=hashlib.sha256(artifact.read_bytes()).hexdigest())
    validation = check_artifacts.ValidationReport(
        1,
        source.source_commit,
        tuple(
            check_artifacts.ArtifactResult(
                artifact.name,
                "wheel" if artifact.suffix == ".whl" else "sdist",
                hashlib.sha256(artifact.read_bytes()).hexdigest(),
                (check_artifacts.CriterionResult("TEST", "pass"),),
            )
            for artifact in artifacts
        ),
    )
    report["artifact_validation"] = validation.to_dict()
    scan = public_tree_scan.scan_public_tree(
        repo,
        snapshot,
        retained,
        policy,
        artifacts=artifacts,
        source_kind="public-history",
        expected_anchor=anchor,
        cutover_record=record,
        artifact_validation=validation,
    )
    assert scan.ok is True
    report.pop("export_manifest")
    report.update(
        schema_version=8,
        status="pending",
        checks_status="pass",
        bundle_status="not-assembled",
        source_kind="public-history",
        package="example-cli",
        planned_tag=source.planned_tag,
        source=asdict(source),
        source_sha256=hashlib.sha256(retained.read_bytes()).hexdigest(),
        scan=scan.to_dict(),
    )
    report["license_evidence"].update(revision=source.source_commit, export_policy_sha256=source.policy_sha256)
    snapshot.rename(candidate / "export")
    (candidate / "manifest.json").write_bytes(retained.read_bytes())
    payload = (json.dumps(report, sort_keys=True) + "\n").encode()
    (candidate / "report.json").write_bytes(payload)
    return candidate, source, payload


@pytest.mark.parametrize("version", ["1.1.0", "1.0.1"])
def test_successor_bundle_is_complete_but_only_preparation(history: History, tmp_path: Path, version: str) -> None:
    candidate, source, payload = _prepared(history, tmp_path, version=version)
    _, anchor, record = history
    result = release_bundle.materialize(
        candidate, expected_anchor=anchor, cutover_record=record, source_repo=history[0]
    )
    assert result.ok is True
    assert result.to_dict()["release_ready"] is False
    assert result.to_dict()["status"] == "prepared"
    assert (candidate / "report.json").read_bytes() == payload
    bundle = candidate / "bundle"
    assert (bundle / "public-history-source.json").read_bytes() == (candidate / "manifest.json").read_bytes()
    provenance = json.loads((bundle / "bundle-provenance.json").read_bytes())
    assert provenance["schema_version"] == 3
    assert provenance["source_commit"] == source.source_commit != anchor.initial_commit
    assert provenance["source_tree"] == source.source_tree != anchor.initial_tree
    assert provenance["anchor"] == asdict(anchor)
    assert provenance["repository_id"] == source.repository_id
    assert provenance["version"] == version
    assert provenance["release_ready"] is False
    expected = {
        **release_bundle_support.expected_asset_provenance(json.loads(payload)),
        "schema_version": 3,
        "source_kind": "public-history",
        "status": "prepared",
        "release_ready": False,
        "pending_controls": ["successor-protected-workflow", "successor-release-approval"],
        "candidate_report_sha256": hashlib.sha256(payload).hexdigest(),
        "source_commit": source.source_commit,
        "source_tree": source.source_tree,
        "expected_repository": source.repository,
        "repository_id": source.repository_id,
        "version": version,
        "planned_tag": source.planned_tag,
        "anchor": asdict(anchor),
        "source_contract": {
            "name": "public-history-source.json",
            "sha256": hashlib.sha256((candidate / "manifest.json").read_bytes()).hexdigest(),
        },
        "export_policy_sha256": source.policy_sha256,
    }
    assert (bundle / "bundle-provenance.json").read_bytes() == (
        json.dumps(expected, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    schema = json.loads(Path("docs/release-readiness/release-bundle-provenance.schema.json").read_text())
    Draft202012Validator(schema).validate(provenance)
    verified = release_bundle.verify(bundle, candidate_report=payload, expected_anchor=anchor, cutover_record=record)
    assert verified == result
    with pytest.raises(ValueError, match=r"trust|anchor|unsupported|unexpected"):
        release_bundle.verify(bundle, candidate_report=payload)


def test_successor_verification_does_not_reparse_provenance(
    history: History, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate, _source, payload = _prepared(history, tmp_path)
    prepared = release_bundle.materialize(
        candidate, expected_anchor=history[1], cutover_record=history[2], source_repo=history[0]
    )
    assert prepared.ok is True
    original = json.loads
    provenance = (candidate / "bundle/bundle-provenance.json").read_bytes()
    report_parses = 0

    def parse(data: str | bytes | bytearray, **kwargs: Any) -> Any:
        nonlocal report_parses
        raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        assert raw != provenance, "exact successor provenance bytes reached a JSON parser"
        if raw == payload:
            report_parses += 1
        return original(data, **kwargs)

    monkeypatch.setattr(json, "loads", parse)
    result = release_bundle.verify(
        candidate / "bundle", candidate_report=payload, expected_anchor=history[1], cutover_record=history[2]
    )
    assert result == prepared
    assert report_parses == 1


@pytest.mark.parametrize(
    "mutation", ["anchor", "record", "source", "scan", "scan-policy", "artifact", "license", "ready"]
)
def test_successor_bundle_rejects_tampered_preparation(history: History, tmp_path: Path, mutation: str) -> None:
    candidate, _source, _payload = _prepared(history, tmp_path)
    _, anchor, record = history
    report = json.loads((candidate / "report.json").read_bytes())
    if mutation == "anchor":
        anchor = replace(anchor, initial_tree="f" * 40)
    elif mutation == "record":
        record += b" "
    elif mutation == "source":
        (candidate / "manifest.json").write_bytes(b"{}\n")
    elif mutation == "scan":
        report["scan"]["source_tree"] = "f" * 40
    elif mutation == "scan-policy":
        report["scan"]["scan_policy_sha256"] = "f" * 64
    elif mutation == "artifact":
        report["artifact_validation"]["source_revision"] = anchor.initial_commit
    elif mutation == "license":
        report["license_evidence"]["revision"] = anchor.initial_commit
    else:
        report["status"] = "pass"
    (candidate / "report.json").write_text(json.dumps(report), encoding="utf-8")
    message = {
        "anchor": "cutover record identity differs from approved anchor",
        "record": "cutover record digest differs from independently approved anchor",
        "source": "candidate source digest does not match the source contract",
        "scan": "candidate scan evidence does not bind the verified export",
        "scan-policy": "successor scan policy digest differs from verified source bytes",
        "artifact": "candidate artifact validation has an unsupported schema or source commit",
        "license": "candidate license evidence has an unsupported schema or source commit",
        "ready": "candidate report has an unsupported preparation schema or status",
    }[mutation]
    with pytest.raises(ValueError, match=message):
        release_bundle.materialize(candidate, expected_anchor=anchor, cutover_record=record, source_repo=history[0])
    assert not (candidate / "bundle").exists()


def test_successor_governance_cannot_promote_historical_controls_to_release_ready(
    history: History, tmp_path: Path
) -> None:
    candidate, source, _payload = _prepared(history, tmp_path)
    _, anchor, record = history
    policy = json.loads(Path("docs/release-readiness/release-governance-policy.json").read_text(encoding="utf-8"))
    planned = policy["candidate"]
    controls = policy["external_controls"]
    assert isinstance(planned, dict) and isinstance(controls, list)
    planned.update(repository=source.repository, package="example-cli", planned_tag=source.planned_tag)
    for control in controls:
        assert isinstance(control, dict)
        control.update(
            status="evidenced",
            evidence={
                "candidate": {**planned, "revision": source.source_commit},
                "record": "https://example.com/evidence",
            },
        )
    policy_path = tmp_path / "governance.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    result = _release_governance.validate(
        policy_path, candidate / "report.json", expected_anchor=anchor, cutover_record=record
    )
    assert result.publication_authorized is False
    assert result.schema_version == 2
    assert result.status == "pending"
    assert "successor-protected-workflow" in result.pending_controls
    assert "successor-release-approval" in result.pending_controls


@pytest.mark.parametrize("mutation", ["classification", "commit", "dirty", "snapshot", "missing-repo"])
def test_successor_producer_requires_real_current_verified_source(
    history: History, tmp_path: Path, mutation: str
) -> None:
    candidate, source, _payload = _prepared(history, tmp_path)
    repo, anchor, record = history
    if mutation in {"classification", "commit"}:
        if mutation == "classification":
            source = replace(
                source, entries=(replace(source.entries[0], category="documentation"), *source.entries[1:])
            )
        else:
            source = replace(source, source_commit="f" * 40)
        payload = public_history_source.source_bytes(source, expected_anchor=anchor, cutover_record=record)
        assert public_history_source.source_from_bytes(payload, expected_anchor=anchor, cutover_record=record) == source
        (candidate / "manifest.json").write_bytes(payload)
        report = json.loads((candidate / "report.json").read_bytes())
        digest = hashlib.sha256(payload).hexdigest()
        report.update(source=asdict(source), source_sha256=digest)
        report["scan"].update(source_sha256=digest, source_commit=source.source_commit)
        report["artifact_validation"]["source_revision"] = source.source_commit
        report["license_evidence"]["revision"] = source.source_commit
        (candidate / "report.json").write_text(json.dumps(report), encoding="utf-8")
    elif mutation == "dirty":
        (repo / "README.md").write_text("changed locally", encoding="utf-8")
    elif mutation == "snapshot":
        (candidate / "export" / "README.md").write_text("changed snapshot", encoding="utf-8")
    with pytest.raises(
        ValueError, match=r"source|revision|HEAD|contract|dirty|commit|repository|Git|worktree|snapshot|export member"
    ):
        release_bundle.materialize(
            candidate,
            expected_anchor=anchor,
            cutover_record=record,
            source_repo=None if mutation == "missing-repo" else repo,
        )
    assert not (candidate / "bundle").exists()


@pytest.mark.parametrize("boundary", ["staged", "published"])
@pytest.mark.parametrize("mutation", ["source", "report"])
def test_successor_producer_rechecks_same_candidate_at_publication_boundaries(
    history: History,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    mutation: str,
) -> None:
    candidate, _source, payload = _prepared(history, tmp_path)
    repo, anchor, record = history
    original = release_bundle._verify_open_bundle
    calls = 0

    def verify(descriptor: int, *, candidate_report: bytes, **kwargs: object) -> PreparationBundleReport:
        nonlocal calls
        result = original(descriptor, candidate_report=candidate_report, expected_anchor=anchor, cutover_record=record)
        assert isinstance(result, PreparationBundleReport)
        calls += 1
        if calls == (1 if boundary == "staged" else 2):
            if mutation == "source":
                (repo / "README.md").write_text("changed during verification", encoding="utf-8")
            else:
                (candidate / "report.json").write_bytes(candidate_report + b" ")
        return result

    monkeypatch.setattr(release_bundle, "_verify_open_bundle", verify)
    with pytest.raises(ValueError, match=r"changed|dirty|worktree"):
        release_bundle.materialize(candidate, expected_anchor=anchor, cutover_record=record, source_repo=repo)
    monkeypatch.setattr(release_bundle, "_verify_open_bundle", original)
    retained_directories = [
        path for path in candidate.iterdir() if path.name.startswith(".bundle") or path.name == "bundle"
    ]
    assert len(retained_directories) == 1
    retained = retained_directories[0]
    assert retained.name.startswith(".bundle-rejected-")
    assert not (candidate / "bundle").exists()
    renamed = candidate / "retained-evidence"
    retained.rename(renamed)
    with pytest.raises(ValueError, match="rejected"):
        release_bundle.verify(renamed, candidate_report=payload, expected_anchor=anchor, cutover_record=record)
    assert not (candidate / "bundle").exists()
    (renamed / "SHA256SUMS").write_bytes((renamed / "rejected-SHA256SUMS").read_bytes())
    with pytest.raises(ValueError, match="rejected"):
        release_bundle.verify(renamed, candidate_report=payload, expected_anchor=anchor, cutover_record=record)


@pytest.mark.parametrize("field", ["release_ready", "schema_version", "repository_id", "anchor.repository_id"])
def test_offline_successor_provenance_rejects_numeric_type_substitution(
    history: History, tmp_path: Path, field: str
) -> None:
    candidate, _source, payload = _prepared(history, tmp_path)
    repo, anchor, record = history
    result = release_bundle.materialize(candidate, expected_anchor=anchor, cutover_record=record, source_repo=repo)
    assert result.ok is True
    bundle = candidate / "bundle"
    path = bundle / "bundle-provenance.json"
    provenance = json.loads(path.read_bytes())
    if field == "anchor.repository_id":
        provenance["anchor"]["repository_id"] = float(anchor.repository_id)
    else:
        provenance[field] = 0 if field == "release_ready" else float(provenance[field])
    changed = (json.dumps(provenance, indent=2, sort_keys=True) + "\n").encode()
    path.write_bytes(changed)
    checksums = tuple(
        (name, hashlib.sha256(changed).hexdigest() if name == path.name else digest) for name, digest in result.files
    )
    (bundle / "SHA256SUMS").write_bytes(release_bundle._checksum_manifest(checksums))
    with pytest.raises(ValueError, match="provenance"):
        release_bundle.verify(bundle, candidate_report=payload, expected_anchor=anchor, cutover_record=record)


@pytest.mark.parametrize("location", ["producer", "offline"])
@pytest.mark.parametrize("mutation", ["oversize", "symlink"])
def test_successor_source_input_is_bounded_nofollow_before_parsing(
    history: History,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    location: str,
    mutation: str,
) -> None:
    candidate, _source, payload = _prepared(history, tmp_path)
    repo, anchor, record = history
    path = candidate / "manifest.json"
    if location == "offline":
        assert release_bundle.materialize(candidate, expected_anchor=anchor, cutover_record=record, source_repo=repo).ok
        path = candidate / "bundle" / "public-history-source.json"
    if mutation == "oversize":
        with path.open("r+b") as stream:
            stream.truncate(public_history_source.MAX_SOURCE_BYTES + 1)
    else:
        path.unlink()
        path.symlink_to(candidate / "report.json")

    def unexpected_parser(*args: object, **kwargs: object) -> None:
        raise AssertionError("unsafe source reached the JSON parser")

    monkeypatch.setattr(public_history_source, "source_from_bytes", unexpected_parser)
    with pytest.raises(ValueError, match=r"size limit|unavailable"):
        if location == "producer":
            release_bundle.materialize(candidate, expected_anchor=anchor, cutover_record=record, source_repo=repo)
        else:
            release_bundle.verify(
                candidate / "bundle", candidate_report=payload, expected_anchor=anchor, cutover_record=record
            )


@pytest.mark.parametrize("field", ["source-version", "source-id", "source-anchor-id", "scan-anchor-id"])
def test_successor_receipt_requires_exact_inline_source_and_anchor_types(
    history: History, tmp_path: Path, field: str
) -> None:
    candidate, _source, _payload = _prepared(history, tmp_path)
    repo, anchor, record = history
    report = json.loads((candidate / "report.json").read_bytes())
    if field == "source-version":
        report["source"]["schema_version"] = 1.0
    elif field == "source-id":
        report["source"]["repository_id"] = float(anchor.repository_id)
    elif field == "source-anchor-id":
        report["source"]["anchor"]["repository_id"] = float(anchor.repository_id)
    else:
        report["scan"]["anchor"]["repository_id"] = float(anchor.repository_id)
    (candidate / "report.json").write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match=r"source|anchor|identity"):
        release_bundle.materialize(candidate, expected_anchor=anchor, cutover_record=record, source_repo=repo)
    assert not (candidate / "bundle").exists()
