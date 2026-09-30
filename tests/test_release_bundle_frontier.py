"""Cross-boundary release preparation must retain canonical validation and rejection."""

import ctypes
import errno
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

import pytest

from scripts import (
    _public_history_bundle,
    _release_bundle_evidence,
    _release_governance,
    public_history_source,
    release_bundle,
)
from tests import release_bundle_support
from tests.test_public_history_bundle import _prepared
from tests.test_public_history_source import history as history
from tests.test_public_tree_scan import History
from tests.test_release_governance import _policy

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("mutation", ["no-ctypes", "rejection", "nested-observations"])
def test_copied_stdlib_consumer_preserves_failure_boundaries(tmp_path: Path, mutation: str) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    report_path = candidate / "report.json"
    if mutation == "nested-observations":
        data = b'{"private-sentinel":' + b"[" * 5000 + b"0" + b"]" * 5000 + b"}"
        (candidate / "runtime-license-observations.json").write_bytes(data)
        report = json.loads(report_path.read_bytes())
        report["license_evidence"]["observations"]["sha256"] = _release_bundle_evidence._digest(data)
        report_path.write_text(json.dumps(report), encoding="utf-8")
        with pytest.raises(ValueError, match="runtime license observations contain excessively nested JSON"):
            release_bundle.materialize(candidate)
        retained = list(candidate.glob(".bundle-*"))
        assert len(retained) == 1
        bundle = retained[0]
    else:
        assert release_bundle.materialize(candidate).ok is True
        bundle = candidate / "bundle"
        if mutation == "rejection":
            (bundle / "bundle-rejection.txt").write_text("rejected", encoding="utf-8")
    scripts = release_bundle_support.standalone_scripts(tmp_path)
    bootstrap = "import runpy,sys;sys.modules['_ctypes']=None;sys.argv=sys.argv[1:];runpy.run_path(sys.argv[0],run_name='__main__')"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            bootstrap,
            str(scripts / "release_bundle.py"),
            str(bundle),
            "--candidate-report",
            str(report_path),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == (0 if mutation == "no-ctypes" else 3)
    if mutation == "no-ctypes":
        assert json.loads(result.stdout)["status"] == "pass"
        assert result.stderr == ""
    else:
        message = (
            "bundle preparation was rejected"
            if mutation == "rejection"
            else "runtime license observations contain excessively nested JSON"
        )
        assert result.stdout == ""
        assert result.stderr == f"Release bundle: ERROR: {message}\n"


@pytest.mark.parametrize("operation", ["materialize", "verify", "governance"])
@pytest.mark.parametrize("trust", ["anchor", "record"])
def test_partial_successor_trust_is_rejected_exactly(tmp_path: Path, operation: str, trust: str) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    anchor = public_history_source.ApprovedCutoverAnchor("example/fieldkit-cli", 42, "a" * 40, "b" * 40, "c" * 64)
    expected_anchor = anchor if trust == "anchor" else None
    cutover_record = b"record" if trust == "record" else None
    with pytest.raises(ValueError, match="requires independent anchor and exact cutover record"):
        if operation == "materialize":
            release_bundle.materialize(candidate, expected_anchor=expected_anchor, cutover_record=cutover_record)
        elif operation == "verify":
            assert release_bundle.materialize(candidate).ok is True
            release_bundle.verify(
                candidate / "bundle",
                candidate_report=(candidate / "report.json").read_bytes(),
                expected_anchor=expected_anchor,
                cutover_record=cutover_record,
            )
        else:
            policy = tmp_path / "policy.json"
            policy.write_text(json.dumps(_policy()), encoding="utf-8")
            _release_governance.validate(
                policy, candidate / "report.json", expected_anchor=expected_anchor, cutover_record=cutover_record
            )


@pytest.mark.parametrize("trust", ["repo", "anchor-record"])
def test_producer_requires_complete_current_source_trust(tmp_path: Path, trust: str) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    anchor = public_history_source.ApprovedCutoverAnchor("example/fieldkit-cli", 42, "a" * 40, "b" * 40, "c" * 64)
    with pytest.raises(
        ValueError,
        match="initial export cannot accept successor source trust"
        if trust == "repo"
        else "successor producer requires a trusted current source repository",
    ):
        release_bundle.materialize(
            candidate,
            expected_anchor=anchor if trust == "anchor-record" else None,
            cutover_record=b"record" if trust == "anchor-record" else None,
            source_repo=tmp_path if trust == "repo" else None,
        )


def test_source_digest_rejection_precedes_decoder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    report = json.loads((candidate / "report.json").read_bytes())
    del report["export_manifest"]
    report.update(
        schema_version=8,
        status="pending",
        checks_status="pass",
        bundle_status="not-assembled",
        source_kind="public-history",
        source={},
        source_sha256="0" * 64,
    )
    anchor = public_history_source.ApprovedCutoverAnchor("example/fieldkit-cli", 42, "a" * 40, "b" * 40, "c" * 64)

    def decode(*args: object, **kwargs: object) -> None:
        pytest.fail("mismatched digest reached source decoder")

    monkeypatch.setattr(public_history_source, "source_from_bytes", decode)
    with pytest.raises(ValueError, match="source digest does not match"):
        _public_history_bundle.candidate_inputs(
            json.dumps(report).encode(), b"unbound", expected_anchor=anchor, cutover_record=b"record"
        )


@pytest.mark.parametrize("boundary", ["staged", "published"])
def test_retained_checksum_invalidates_without_directory_writes(
    history: History, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    candidate, _, payload = _prepared(history, tmp_path)
    original_verify = release_bundle._verify_open_bundle
    original_rename = release_bundle._rename_no_replace_at
    original_write = release_bundle._write_bytes_at
    calls = 0

    def verify(descriptor: int, **kwargs: object) -> _public_history_bundle.PreparationBundleReport:
        nonlocal calls
        result = original_verify(
            descriptor, candidate_report=payload, expected_anchor=history[1], cutover_record=history[2]
        )
        assert isinstance(result, _public_history_bundle.PreparationBundleReport)
        calls += 1
        if calls == (1 if boundary == "staged" else 2):
            (history[0] / "README.md").write_text("source changed", encoding="utf-8")
        return result

    def rename(descriptor: int, source: str, destination: str) -> None:
        if source == "SHA256SUMS":
            raise OSError(errno.EACCES, "private rename details")
        original_rename(descriptor, source, destination)

    def write(descriptor: int, name: str, data: bytes) -> None:
        if name == "bundle-rejection.txt":
            raise OSError(errno.EMFILE, "private marker details")
        original_write(descriptor, name, data)

    monkeypatch.setattr(release_bundle, "_verify_open_bundle", verify)
    monkeypatch.setattr(release_bundle, "_rename_no_replace_at", rename)
    monkeypatch.setattr(release_bundle, "_write_bytes_at", write)
    with pytest.raises(ValueError, match=r"^successor rejected bundle retention failed$") as caught:
        release_bundle.materialize(
            candidate, expected_anchor=history[1], cutover_record=history[2], source_repo=history[0]
        )
    retained = list(candidate.glob(".bundle-rejected-*"))
    assert len(retained) == 1
    retained[0].rename(candidate / "renamed-rejection")
    assert (candidate / "renamed-rejection/SHA256SUMS").read_bytes() == b""
    monkeypatch.setattr(release_bundle, "_verify_open_bundle", original_verify)
    with pytest.raises(ValueError, match="checksum manifest is not canonical"):
        release_bundle.verify(
            candidate / "renamed-rejection",
            candidate_report=payload,
            expected_anchor=history[1],
            cutover_record=history[2],
        )
    assert caught.value.__cause__ is None


def test_rename_cancellation_quarantines_retained_published_inode(
    history: History, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate, _, payload = _prepared(history, tmp_path)
    original_rename = release_bundle._rename_no_replace_at

    def rename(descriptor: int, source: str, destination: str) -> None:
        original_rename(descriptor, source, destination)
        if destination == "bundle":
            raise KeyboardInterrupt

    monkeypatch.setattr(release_bundle, "_rename_no_replace_at", rename)
    with pytest.raises(KeyboardInterrupt) as caught:
        release_bundle.materialize(
            candidate, expected_anchor=history[1], cutover_record=history[2], source_repo=history[0]
        )
    assert not (candidate / "bundle").exists()
    retained = list(candidate.glob(".bundle-rejected-*"))
    assert len(retained) == 1
    assert caught.value.__cause__ is None
    with pytest.raises(ValueError, match="preparation was rejected"):
        release_bundle.verify(
            retained[0], candidate_report=payload, expected_anchor=history[1], cutover_record=history[2]
        )


def test_unexpected_publication_error_is_sanitized_after_retention(
    history: History, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate, _, payload = _prepared(history, tmp_path)
    original_rename = release_bundle._rename_no_replace_at

    def rename(descriptor: int, source: str, destination: str) -> None:
        original_rename(descriptor, source, destination)
        if destination == "bundle":
            raise RuntimeError("private-unexpected-publication-sentinel")

    monkeypatch.setattr(release_bundle, "_rename_no_replace_at", rename)
    with pytest.raises(ValueError, match=r"^successor bundle preparation failed$") as caught:
        release_bundle.materialize(
            candidate, expected_anchor=history[1], cutover_record=history[2], source_repo=history[0]
        )
    assert caught.value.__cause__ is None
    retained = list(candidate.glob(".bundle-rejected-*"))
    assert len(retained) == 1
    with pytest.raises(ValueError, match="preparation was rejected"):
        release_bundle.verify(
            retained[0], candidate_report=payload, expected_anchor=history[1], cutover_record=history[2]
        )


@pytest.mark.parametrize(
    "mutation", ["manifest-version", "manifest-oid", "manifest-mode", "package-url", "identity-digest"]
)
@pytest.mark.parametrize("validator", ["bundle", "governance"])
def test_initial_report_has_one_complete_canonical_validator(tmp_path: Path, mutation: str, validator: str) -> None:
    candidate = release_bundle_support.candidate(tmp_path)
    report = json.loads((candidate / "report.json").read_bytes())
    if mutation == "manifest-version":
        report["export_manifest"]["schema_version"] = 1.0
    elif mutation == "manifest-oid":
        report["export_manifest"]["source_tree"] = report["scan"]["source_tree"] = "invalid"
    elif mutation == "manifest-mode":
        report["export_manifest"]["included"] = [
            {"path": "README.md", "mode": "100600", "oid": "a" * 40, "category": "public", "rule_id": "public"}
        ]
    elif mutation == "package-url":
        report["license_evidence"]["packages"][0]["package_url"] = "pkg:npm/example@1"
    else:
        report["scan"]["identity_policy_sha256"] = "invalid"
    payload = json.dumps(report).encode()
    (candidate / "report.json").write_bytes(payload)
    match = {
        "manifest-version": "candidate export manifest has an unsupported schema_version",
        "manifest-oid": "candidate export manifest source_tree",
        "manifest-mode": "candidate export manifest entry mode is unsupported",
        "package-url": "candidate license package URL must use pkg:pypi/",
        "identity-digest": "candidate identity policy digest",
    }[mutation]
    with pytest.raises(ValueError, match=match):
        if validator == "bundle":
            _release_bundle_evidence._candidate_inputs(payload)
        else:
            _release_governance._candidate_report(
                candidate / "report.json",
                _release_governance.PlannedCandidate("example/fieldkit-cli", "fieldkit-cli", "v1.0.0"),
            )


def test_unexpected_retention_error_does_not_skip_later_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[str] = []

    def rename(*args: object) -> None:
        raise ctypes.ArgumentError("private FFI details")

    monkeypatch.setattr(release_bundle, "_rename_no_replace_at", rename)
    monkeypatch.setattr(release_bundle, "_write_bytes_at", lambda *args: attempts.append("marker"))
    monkeypatch.setattr(release_bundle, "_quarantine_rejected_bundle", lambda *args: attempts.append("quarantine"))
    with pytest.raises(ValueError, match=r"^successor rejected bundle retention failed$"):
        release_bundle._reject_successor_bundle(1, 2, "bundle", checksum_fd=None)
    assert attempts == ["marker", "quarantine"]


def test_unwritten_checksum_is_not_a_retention_failure(
    history: History, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate, _, payload = _prepared(history, tmp_path)
    original_write = release_bundle._write_bytes_at

    def write(descriptor: int, name: str, data: bytes) -> None:
        if name == "locked-graph.cdx.json":
            raise OSError(errno.ENOSPC, "private-asset-write-sentinel")
        original_write(descriptor, name, data)

    monkeypatch.setattr(release_bundle, "_write_bytes_at", write)
    with pytest.raises(ValueError, match=r"^successor bundle preparation failed$") as caught:
        release_bundle.materialize(
            candidate, expected_anchor=history[1], cutover_record=history[2], source_repo=history[0]
        )
    assert str(caught.value) == "successor bundle preparation failed"
    retained = list(candidate.glob(".bundle-rejected-*"))
    assert len(retained) == 1
    assert not (retained[0] / "SHA256SUMS").exists()
    assert (retained[0] / "bundle-rejection.txt").read_bytes() == b"preparation rejected\n"
    with pytest.raises(ValueError, match="preparation was rejected"):
        release_bundle.verify(
            retained[0], candidate_report=payload, expected_anchor=history[1], cutover_record=history[2]
        )


def test_missing_previously_saved_checksum_is_a_retention_failure(
    history: History, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate, _, _ = _prepared(history, tmp_path)
    original_rename = release_bundle._rename_no_replace_at

    def cancel(*args: object, **kwargs: object) -> None:
        raise ValueError("preparation rejected")

    def rename(descriptor: int, source: str, destination: str) -> None:
        if source == "SHA256SUMS":
            os.unlink(source, dir_fd=descriptor)
        original_rename(descriptor, source, destination)

    monkeypatch.setattr(release_bundle, "_verify_open_bundle", cancel)
    monkeypatch.setattr(release_bundle, "_rename_no_replace_at", rename)
    with pytest.raises(ValueError, match=r"^successor rejected bundle retention failed$") as caught:
        release_bundle.materialize(
            candidate, expected_anchor=history[1], cutover_record=history[2], source_repo=history[0]
        )
    assert str(caught.value) == "successor rejected bundle retention failed"
    assert len(list(candidate.glob(".bundle-rejected-*"))) == 1


@pytest.mark.parametrize("error_type", [ValueError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("chain", ["implicit", "explicit"])
def test_successful_retention_suppresses_original_failure_chain(
    history: History, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[BaseException], chain: str
) -> None:
    candidate, _, payload = _prepared(history, tmp_path)
    original_verify = release_bundle._verify_open_bundle
    cancellation = (
        error_type(3)
        if error_type is SystemExit
        else error_type("candidate report changed during bundle preparation")
        if error_type is ValueError
        else error_type()
    )
    private_cause = OSError(errno.EACCES, "private-cancellation-sentinel", "/private/example-path")

    def cancel(*args: object, **kwargs: object) -> None:
        if chain == "explicit":
            raise cancellation from private_cause
        try:
            raise private_cause
        except OSError:
            raise cancellation  # noqa: B904 -- exercise an implicitly chained producer failure

    monkeypatch.setattr(release_bundle, "_verify_open_bundle", cancel)
    with pytest.raises(error_type) as caught:
        release_bundle.materialize(
            candidate, expected_anchor=history[1], cutover_record=history[2], source_repo=history[0]
        )
    assert caught.value is cancellation
    if isinstance(cancellation, ValueError):
        assert str(cancellation) == "candidate report changed during bundle preparation"
    rendered = "".join(traceback.format_exception(caught.value))
    assert "private-cancellation-sentinel" not in rendered
    assert "/private/example-path" not in rendered
    if chain == "implicit":
        # Formatting suppression is not erasure of the retained Python exception context.
        assert cancellation.__context__ is private_cause
    if isinstance(cancellation, SystemExit):
        assert cancellation.code == 3
    retained = list(candidate.glob(".bundle-rejected-*"))
    assert len(retained) == 1
    monkeypatch.setattr(release_bundle, "_verify_open_bundle", original_verify)
    with pytest.raises(ValueError, match="preparation was rejected"):
        release_bundle.verify(
            retained[0], candidate_report=payload, expected_anchor=history[1], cutover_record=history[2]
        )


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("chain", ["implicit", "explicit"])
@pytest.mark.parametrize("step", ["checksum", "marker", "quarantine"])
def test_second_cancellation_is_sanitized_without_claiming_invalidation(
    history: History,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[BaseException],
    chain: str,
    step: str,
) -> None:
    candidate, _, payload = _prepared(history, tmp_path)
    original_rename = release_bundle._rename_no_replace_at
    original_write = release_bundle._write_bytes_at
    original_quarantine = release_bundle._quarantine_rejected_bundle
    cancellation = error_type(3) if error_type is SystemExit else error_type()
    private_cause = OSError(errno.EACCES, "private-second-cause", "/private/second-path")
    publication_failure = RuntimeError("private-publication-context")
    attempts: list[str] = []

    def interrupt() -> None:
        if chain == "explicit":
            raise cancellation from private_cause
        raise cancellation

    def rename(descriptor: int, source: str, destination: str) -> None:
        if source == "SHA256SUMS":
            attempts.append("checksum")
            if step == "checksum":
                interrupt()
        original_rename(descriptor, source, destination)
        if destination == "bundle":
            attempts.append("publication")
            raise publication_failure

    def write(descriptor: int, name: str, data: bytes) -> None:
        if name == "bundle-rejection.txt":
            attempts.append("marker")
            if step == "marker":
                interrupt()
        original_write(descriptor, name, data)

    def quarantine(parent_fd: int, retained_fd: int, name: str) -> None:
        attempts.append("quarantine")
        if step == "quarantine":
            interrupt()
        original_quarantine(parent_fd, retained_fd, name)

    monkeypatch.setattr(release_bundle, "_rename_no_replace_at", rename)
    monkeypatch.setattr(release_bundle, "_write_bytes_at", write)
    monkeypatch.setattr(release_bundle, "_quarantine_rejected_bundle", quarantine)
    with pytest.raises(error_type) as caught:
        release_bundle.materialize(
            candidate, expected_anchor=history[1], cutover_record=history[2], source_repo=history[0]
        )
    assert caught.value is cancellation
    rendered = "".join(traceback.format_exception(caught.value))
    assert "private-second-cause" not in rendered
    assert "private-publication-context" not in rendered
    assert "/private/second-path" not in rendered
    assert cancellation.__notes__ == ["successor rejected bundle retention interrupted"]
    assert cancellation.__context__ is publication_failure
    if isinstance(cancellation, SystemExit):
        assert cancellation.code == 3
    expected_attempts = ["publication", "checksum", "marker", "quarantine"]
    assert attempts == expected_attempts[: expected_attempts.index(step) + 1]
    assert not list(candidate.glob(".bundle-rejected-*"))
    assert (candidate / "bundle/bundle-rejection.txt").exists() is (step == "quarantine")
    # A second interruption can leave valid preparation bytes: it is not a successful producer return or approval.
    if step == "checksum":
        result = release_bundle.verify(
            candidate / "bundle", candidate_report=payload, expected_anchor=history[1], cutover_record=history[2]
        )
        assert result.ok is True
        assert result.to_dict()["release_ready"] is False
    else:
        with pytest.raises(ValueError, match="preparation was rejected"):
            release_bundle.verify(
                candidate / "bundle", candidate_report=payload, expected_anchor=history[1], cutover_record=history[2]
            )
