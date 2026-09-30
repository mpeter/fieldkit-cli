"""Evidence acquisition rejects invalid inventories before retaining payloads."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from scripts import rehearsal_acquisition as acquisition
from scripts import release_approval_input, release_bundle, release_manual_evidence
from scripts.rehearsal_evidence import MAX_EVIDENCE_BYTES
from tests import rehearsal_support, release_approval_support, release_bundle_support

pytestmark = pytest.mark.unit


def _ledger() -> dict[str, object]:
    return {
        "criteria": [
            {"id": identifier, "record": {"path": f"records/{identifier}.json"}}
            for identifier in release_manual_evidence.REQUIRED_EVIDENCE_IDS
        ]
    }


@pytest.mark.parametrize(
    "case",
    ["too_many", "missing", "duplicate_id", "unknown_id", "nonstring_id", "late_record", "late_path", "duplicate_path"],
)
def test_invalid_manual_inventory_precedes_all_record_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    ledger = _ledger()
    criteria = ledger["criteria"]
    assert isinstance(criteria, list)
    if case == "too_many":
        criteria.extend({"id": f"unknown-{i}", "record": {"path": f"records/extra-{i}.json"}} for i in range(90))
    elif case == "missing":
        criteria.pop()
    elif case == "duplicate_id":
        criteria[-1]["id"] = criteria[0]["id"]
    elif case == "unknown_id":
        criteria[-1]["id"] = "unknown"
    elif case == "nonstring_id":
        criteria[-1]["id"] = {"unhashable": True}
    elif case == "late_record":
        criteria[-1]["record"] = None
    elif case == "late_path":
        criteria[-1]["record"]["path"] = "../outside.json"
    else:
        criteria[-1]["record"]["path"] = criteria[0]["record"]["path"]
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    report_path = tmp_path / "report.json"
    report_path.write_text("{}", encoding="utf-8")
    reads: list[str] = []

    def read(_root: int, name: str, *, maximum_bytes: int) -> bytes:
        reads.append(name)
        return b"{}"

    monkeypatch.setattr(acquisition, "read_relative_bytes", read)
    monkeypatch.setattr(release_manual_evidence, "validate_bytes", lambda *_a, **_k: ((), ""))
    with pytest.raises(ValueError):
        release_manual_evidence.validate_paths(tmp_path, ledger_path, report_path)
    assert reads == []


@pytest.mark.parametrize("overreturn", [False, True])
def test_manual_acquisition_enforces_actual_remaining_byte_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overreturn: bool
) -> None:
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text(json.dumps(_ledger()), encoding="utf-8")
    report_path = tmp_path / "report.json"
    report_path.write_text("{}", encoding="utf-8")
    budget = 40 if overreturn else 19
    monkeypatch.setattr(acquisition, "MAX_ACQUIRED_EVIDENCE_BYTES", budget, raising=False)
    limits: list[int] = []

    def read(_root: int, _name: str, *, maximum_bytes: int) -> bytes:
        limits.append(maximum_bytes)
        return b"{}" + (b" " * 48 if overreturn else b"")

    monkeypatch.setattr(acquisition, "read_relative_bytes", read)
    monkeypatch.setattr(release_manual_evidence, "validate_bytes", lambda *_a, **_k: ((), ""))
    with pytest.raises(ValueError, match="aggregate byte limit"):
        release_manual_evidence.validate_paths(tmp_path, ledger_path, report_path)
    assert limits == ([40] if overreturn else list(range(19, 0, -2)))


def test_approval_manifest_uses_shared_evidence_byte_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release_approval_support.write_real_input(tmp_path)
    monkeypatch.setattr(acquisition, "MAX_ACQUIRED_EVIDENCE_BYTES", 1, raising=False)
    with pytest.raises(ValueError, match="aggregate byte limit"):
        release_approval_input.verify(
            tmp_path,
            controller_root=Path(__file__).parents[1],
            expected=release_approval_support.expected_input(tmp_path),
        )


def test_budget_exact_boundary_limits_real_file_reads(tmp_path: Path) -> None:
    (tmp_path / "payload").write_bytes(b"retained")
    (tmp_path / "empty").write_bytes(b"")
    root_fd = acquisition.open_real_directory(tmp_path)
    budget = acquisition.AcquisitionBudget(maximum_bytes=8)
    try:
        assert budget.read(root_fd, "payload") == b"retained"
        assert budget.acquired_bytes == 8
        assert budget.read(root_fd, "empty") == b""
        with pytest.raises(ValueError, match="aggregate byte limit"):
            budget.read(root_fd, "payload")
        assert budget.acquired_bytes == 8
    finally:
        os.close(root_fd)


def test_aggregate_budget_preserves_smaller_per_member_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acquisition, "read_relative_bytes", lambda *_a, **_k: b"abc")
    budget = acquisition.AcquisitionBudget(maximum_bytes=10)
    with pytest.raises(ValueError, match="member exceeds its byte limit"):
        budget.read(-1, "oversized-member", maximum_bytes=2)
    assert budget.acquired_bytes == 0


def test_budget_covers_the_registered_finite_support_slots() -> None:
    fields = release_manual_evidence.RECORD_PAYLOAD_FIELDS.values()
    support_slots = sum(len(item & release_manual_evidence.SUPPORT_PATH_FIELDS) for item in fields)
    rehearsal_slots = sum("rehearsal_evidence_path" in item for item in fields)
    assert len(release_manual_evidence.REQUIRED_EVIDENCE_IDS) == 10
    assert support_slots == 7 and rehearsal_slots == 3
    expected_budget = (10 + support_slots + rehearsal_slots) * MAX_EVIDENCE_BYTES
    assert expected_budget == acquisition.MAX_ACQUIRED_EVIDENCE_BYTES


def test_manual_support_paths_are_counted_once(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = json.dumps({"proof": {"payload": {"prompt_path": "support/shared.txt"}}}).encode()
    reads: list[str] = []

    def read(_root: int, name: str, *, maximum_bytes: int) -> bytes:
        reads.append(name)
        return b"support" if name == "support/shared.txt" else raw

    monkeypatch.setattr(acquisition, "MAX_ACQUIRED_EVIDENCE_BYTES", 10 * len(raw) + 7)
    monkeypatch.setattr(acquisition, "read_relative_bytes", read)
    result = acquisition.acquire_manual_evidence(
        -1,
        json.dumps(_ledger()).encode(),
        expected_criterion_ids=release_manual_evidence.REQUIRED_EVIDENCE_IDS,
        support_path_fields=release_manual_evidence.SUPPORT_PATH_FIELDS,
    )
    assert result["support/shared.txt"] == b"support"
    assert len(result) == 11
    assert reads.count("support/shared.txt") == 1


@pytest.mark.parametrize("catalog", [False, True], ids=["support", "catalog-member"])
def test_support_and_catalog_reads_share_record_budget(monkeypatch: pytest.MonkeyPatch, catalog: bool) -> None:
    ledger = _ledger()
    criteria = ledger["criteria"]
    assert isinstance(criteria, list)
    record_name = criteria[0]["record"]["path"]
    field = "rehearsal_evidence_path" if catalog else "prompt_path"
    raw = json.dumps({"proof": {"payload": {field: "support/receipt.json"}}}).encode()
    support = json.dumps({"members": [{"name": "retained/output", "size": 5}]}).encode() if catalog else b"proof"
    retained = {record_name: raw, "support/receipt.json": support, "retained/output": b"proof"}
    maximum = 9 * 2 + len(raw) + len(support) + (5 if catalog else 0) - 1
    monkeypatch.setattr(acquisition, "MAX_ACQUIRED_EVIDENCE_BYTES", maximum)
    limits: list[int] = []

    def read(_root: int, name: str, *, maximum_bytes: int) -> bytes:
        limits.append(maximum_bytes)
        return retained.get(name, b"{}")

    monkeypatch.setattr(acquisition, "read_relative_bytes", read)
    with pytest.raises(ValueError, match="aggregate byte limit"):
        acquisition.acquire_manual_evidence(
            -1,
            json.dumps(ledger).encode(),
            expected_criterion_ids=release_manual_evidence.REQUIRED_EVIDENCE_IDS,
            support_path_fields=release_manual_evidence.SUPPORT_PATH_FIELDS,
        )
    assert limits[-1] == (4 if catalog else len(support) - 1)


def test_approval_preflights_inventory_before_evidence_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release_approval_support.write_real_input(tmp_path)
    ledger_path = tmp_path / "evidence/ledger.json"
    ledger = json.loads(ledger_path.read_bytes())
    ledger["criteria"][-1]["id"] = "unknown"
    raw = json.dumps(ledger).encode()
    ledger_path.write_bytes(raw)
    manifest_path = tmp_path / "approval-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    for entry in manifest["files"]:
        if entry["path"] == "evidence/ledger.json":
            entry["sha256"] = hashlib.sha256(raw).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    reads: list[str] = []
    original_read = acquisition.read_relative_bytes

    def read(root: int, name: str, *, maximum_bytes: int = MAX_EVIDENCE_BYTES) -> bytes:
        if name.startswith("evidence/") and name != "evidence/ledger.json":
            reads.append(name)
            return b"{}"
        return original_read(root, name, maximum_bytes=maximum_bytes)

    monkeypatch.setattr(acquisition, "read_relative_bytes", read)
    with pytest.raises(ValueError, match="criteria inventory is invalid"):
        release_approval_input.verify(
            tmp_path,
            controller_root=Path(__file__).parents[1],
            expected=release_approval_support.expected_input(tmp_path),
        )
    assert reads == []


def test_approval_schema_is_bounded_before_json_parsing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    schema_path = tmp_path / "schema.json"
    schema_path.write_bytes(b" " * (MAX_EVIDENCE_BYTES + 1))

    def parse(_data: bytes, _subject: str) -> dict[str, object]:
        pytest.fail("oversized schema reached JSON parsing")

    monkeypatch.setattr(release_approval_input, "_load_json_bytes", parse)
    with pytest.raises(ValueError, match="byte limit"):
        release_approval_input._load_repo_json(schema_path)


def test_acquisition_uses_selected_evidence_root_and_keeps_diagnostics_pending(tmp_path: Path) -> None:
    release_approval_support.write_real_input(tmp_path)
    evidence_root = tmp_path / "evidence"
    wrong_root_member = evidence_root / "support/streams/empty.txt"
    wrong_root_member.parent.mkdir(parents=True)
    wrong_root_member.write_bytes(b"wrong receipt-relative member")
    evidence_fd = acquisition.open_real_directory(evidence_root)
    bundle_fd = acquisition.open_real_directory(tmp_path / "public-candidate/bundle")
    try:
        result = acquisition.acquire_rehearsal(
            (evidence_root / "support/rehearsal.json").read_bytes(),
            schema_bytes=(
                Path(__file__).parents[1] / "docs/release-readiness/rehearsal-evidence.schema.json"
            ).read_bytes(),
            evidence_root_fd=evidence_fd,
            bundle_root_fd=bundle_fd,
            candidate_report_bytes=(tmp_path / "public-candidate/report.json").read_bytes(),
            private_candidate_report_bytes=(tmp_path / "private-candidate-report.json").read_bytes(),
        )
    finally:
        os.close(bundle_fd)
        os.close(evidence_fd)

    assert not result.passing
    assert not result.schema_evaluation_performed
    assert result.verified_blocks == () and result.missing_scenarios == ()
    assert result.linked_private is not None and not result.linked_private.passing
    assert "controller-selection-unapproved" in result.issues


@pytest.mark.parametrize("mutation", ["overwrite", "replace"])
def test_artifact_replacement_after_capture_preserves_verified_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    release_approval_support.write_real_input(tmp_path)
    bundle = tmp_path / "public-candidate/bundle"
    original_capture = release_bundle.capture_open_bundle
    expected = {path.name: path.read_bytes() for path in bundle.iterdir() if path.name.endswith((".whl", ".tar.gz"))}

    def replace_artifact(
        directory_fd: int, *, candidate_report: bytes, expected_checksums: bytes | None = None
    ) -> release_bundle.VerifiedBundle[release_bundle.BundleReport]:
        result = original_capture(
            directory_fd, candidate_report=candidate_report, expected_checksums=expected_checksums
        )
        wheel = next(name for name, _digest in result.report.files if name.endswith(".whl"))
        if mutation == "replace":
            (bundle / wheel).unlink()
        (bundle / wheel).write_bytes(b"changed after verification")
        return result

    monkeypatch.setattr(release_bundle, "capture_open_bundle", replace_artifact)

    def unexpected_read(*_args: object, **_kwargs: object) -> bytes:
        pytest.fail("captured artifacts must not be reread")

    monkeypatch.setattr(acquisition, "read_relative_bytes", unexpected_read)
    descriptor_fd = acquisition.open_real_directory(bundle)
    try:
        artifacts = acquisition.verified_bundle_artifacts(
            descriptor_fd, candidate_report_bytes=(tmp_path / "public-candidate/report.json").read_bytes()
        )
        assert artifacts == expected
    finally:
        os.close(descriptor_fd)


@pytest.mark.parametrize("change", ["missing", "private_sha", "private_tree", "exported_tree"])
def test_public_acquisition_requires_matching_independently_selected_private_report(
    tmp_path: Path, change: str
) -> None:
    release_approval_support.write_real_input(tmp_path)
    private = json.loads((tmp_path / "private-candidate-report.json").read_bytes())
    if change == "private_sha":
        private["export_manifest"]["source_commit"] = "1" * 40
        private["artifact_validation"]["source_revision"] = "1" * 40
        private["license_evidence"]["revision"] = "1" * 40
        private["scan"]["source_commit"] = "1" * 40
    elif change == "private_tree":
        private["export_manifest"]["source_tree"] = "1" * 40
        private["scan"]["source_tree"] = "1" * 40
    elif change == "exported_tree":
        private["export_manifest"]["exported_tree"] = "1" * 40
        private["scan"]["exported_tree"] = "1" * 40
    evidence_fd = acquisition.open_real_directory(tmp_path / "evidence")
    bundle_fd = acquisition.open_real_directory(tmp_path / "public-candidate/bundle")
    try:
        with pytest.raises(ValueError, match="selected private"):
            acquisition.acquire_rehearsal(
                (tmp_path / "evidence/support/rehearsal.json").read_bytes(),
                schema_bytes=(
                    Path(__file__).parents[1] / "docs/release-readiness/rehearsal-evidence.schema.json"
                ).read_bytes(),
                evidence_root_fd=evidence_fd,
                bundle_root_fd=bundle_fd,
                candidate_report_bytes=(tmp_path / "public-candidate/report.json").read_bytes(),
                private_candidate_report_bytes=None if change == "missing" else json.dumps(private).encode(),
            )
    finally:
        os.close(bundle_fd)
        os.close(evidence_fd)


@pytest.mark.parametrize("name", ["../escape.txt", "/escape.txt", "streams//empty.txt", "streams\\empty.txt"])
def test_rehearsal_acquisition_inventory_rejects_noncanonical_paths(name: str) -> None:
    receipt = {"members": [{"name": name, "sha256": "a" * 64, "size": 0}]}
    with pytest.raises(ValueError, match="rehearsal member path is invalid"):
        acquisition.rehearsal_member_names(json.dumps(receipt).encode())


@pytest.mark.parametrize("limit", ["count", "declared_bytes"])
def test_rehearsal_acquisition_preflights_inventory_limits(limit: str) -> None:
    members = (
        [{"name": f"stream-{index}", "size": 0} for index in range(4097)]
        if limit == "count"
        else [{"name": "stream", "size": 5 * 1024 * 1024 + 1}]
    )
    with pytest.raises(ValueError, match="limit"):
        acquisition.rehearsal_member_names(json.dumps({"members": members}).encode())


def test_actual_catalog_bytes_are_bounded_even_when_declared_sizes_lie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release_approval_support.write_real_input(tmp_path)
    evidence_root = tmp_path / "evidence"
    for name in ("first.bin", "second.bin"):
        (evidence_root / name).write_bytes(b"x" * (3 * 1024 * 1024))
    receipt = json.loads((evidence_root / "support/rehearsal.json").read_bytes())
    receipt["members"] = [{"name": name, "sha256": "a" * 64, "size": 0} for name in ("first.bin", "second.bin")]
    reads: list[tuple[str, int]] = []
    original_read = acquisition.read_relative_bytes

    def bounded_read(descriptor_fd: int, subject: str, *, maximum_bytes: int = 5 * 1024 * 1024) -> bytes:
        if subject in {"first.bin", "second.bin"}:
            reads.append((subject, maximum_bytes))
        return original_read(descriptor_fd, subject, maximum_bytes=maximum_bytes)

    monkeypatch.setattr(acquisition, "read_relative_bytes", bounded_read)
    evidence_fd = acquisition.open_real_directory(evidence_root)
    bundle_fd = acquisition.open_real_directory(tmp_path / "public-candidate/bundle")
    try:
        with pytest.raises(ValueError, match="byte limit"):
            acquisition.acquire_rehearsal(
                json.dumps(receipt).encode(),
                schema_bytes=(
                    Path(__file__).parents[1] / "docs/release-readiness/rehearsal-evidence.schema.json"
                ).read_bytes(),
                evidence_root_fd=evidence_fd,
                bundle_root_fd=bundle_fd,
                candidate_report_bytes=(tmp_path / "public-candidate/report.json").read_bytes(),
                private_candidate_report_bytes=(tmp_path / "private-candidate-report.json").read_bytes(),
            )
    finally:
        os.close(bundle_fd)
        os.close(evidence_fd)
    assert reads == [("first.bin", 5 * 1024 * 1024), ("second.bin", 2 * 1024 * 1024)]


def test_acquisition_fd_reader_refuses_symlinked_members(tmp_path: Path) -> None:
    (tmp_path / "stream.txt").write_bytes(b"retained")
    (tmp_path / "link.txt").symlink_to(tmp_path / "stream.txt")
    root_fd = acquisition.open_real_directory(tmp_path)
    try:
        assert acquisition.read_relative_bytes(root_fd, "stream.txt") == b"retained"
        with pytest.raises(ValueError, match="unavailable"):
            acquisition.read_relative_bytes(root_fd, "link.txt")
    finally:
        os.close(root_fd)


def test_private_acquisition_uses_its_selected_report_without_a_second_private_report(tmp_path: Path) -> None:
    candidate = release_bundle_support.candidate(tmp_path / "fixture")
    release_bundle.materialize(candidate)
    report_bytes = (candidate / "report.json").read_bytes()
    report = json.loads(report_bytes)
    receipt, members = rehearsal_support.fixture_receipt()
    receipt["subject"] = {
        "source_kind": "initial-export",
        "private_source_sha": report["export_manifest"]["source_commit"],
        "private_source_tree": report["export_manifest"]["source_tree"],
        "exported_tree": report["export_manifest"]["exported_tree"],
        "documentation_contract_sha256": "d" * 64,
    }
    receipt["artifacts"] = [
        {
            "name": item["name"],
            "kind": item["kind"],
            "sha256": item["sha256"],
            "size": len((candidate / "bundle" / item["name"]).read_bytes()),
        }
        for item in report["artifact_validation"]["artifacts"]
    ]
    evidence_root = tmp_path / "observations"
    for name, raw in members.items():
        path = evidence_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    evidence_fd = acquisition.open_real_directory(evidence_root)
    bundle_fd = acquisition.open_real_directory(candidate / "bundle")
    try:
        result = acquisition.acquire_rehearsal(
            json.dumps(receipt).encode(),
            schema_bytes=(
                Path(__file__).parents[1] / "docs/release-readiness/rehearsal-evidence.schema.json"
            ).read_bytes(),
            evidence_root_fd=evidence_fd,
            bundle_root_fd=bundle_fd,
            candidate_report_bytes=report_bytes,
        )
    finally:
        os.close(bundle_fd)
        os.close(evidence_fd)
    assert not result.passing
    assert result.phase == "private-candidate" and result.public is None
    assert not result.schema_evaluation_performed and result.missing_scenarios == ()
