"""Tests for private independent-verification evidence."""

import json
import os
from pathlib import Path

import pytest

from fieldkit.driver.done_check_evidence import CheckEvidence, EvidenceRecord, OutputEvidence, write_evidence

pytestmark = pytest.mark.unit


def _record() -> EvidenceRecord:
    output = OutputEvidence(byte_count=3, sha256="a" * 64, complete=True)
    return EvidenceRecord(
        attempt_id="attempt-1",
        repository_id=42,
        issue_number=1242,
        pr_number=99,
        attempt=1,
        base_sha="b" * 40,
        initial_head_sha="c" * 40,
        final_head_sha="c" * 40,
        work_order_sha256="d" * 64,
        contract_sha256="e" * 64,
        checker_sha256={"scripts/done_checks/check.py": "f" * 64},
        started_at="2026-09-07T00:00:00Z",
        completed_at="2026-09-07T00:00:01Z",
        checks=(CheckEvidence("tests", "passed", 0, 10, ("pytest", "-q"), "f" * 64, output, output),),
        cleanup_status="passed",
        decision="passed",
        phase="terminal",
    )


def test_write_evidence_is_private_atomic_and_contains_hashes_not_output(tmp_path: Path) -> None:
    destination = write_evidence(tmp_path / "evidence", _record())
    payload = json.loads(destination.read_text(encoding="utf-8"))

    assert destination.stat().st_mode & 0o777 == 0o600
    assert destination.parent.stat().st_mode & 0o777 == 0o700
    assert payload["schema_version"] == 1
    assert payload["checks"][0]["argv"] == ["pytest", "-q"]
    assert payload["checks"][0]["executable_sha256"] == "f" * 64
    assert payload["checks"][0]["stdout"] == {"byte_count": 3, "complete": True, "sha256": "a" * 64}
    serialized = destination.read_text(encoding="utf-8")
    assert "raw output" not in serialized
    assert "example/repo" not in serialized
    assert str(Path.home()) not in serialized
    assert list(destination.parent.glob(".*.tmp")) == []


def test_terminal_record_atomically_replaces_checks_complete(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    initial = _record()
    initial = EvidenceRecord(**{**initial.__dict__, "phase": "checks-complete", "decision": "incomplete"})
    write_evidence(root, initial)

    destination = write_evidence(root, _record())

    assert json.loads(destination.read_text())["phase"] == "terminal"
    assert len(list(root.glob("*.json"))) == 1


def test_write_failure_removes_temporary_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "evidence"

    def fail_replace(self: Path, target: Path) -> Path:
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        write_evidence(root, _record())

    assert list(root.glob(".*.tmp")) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="permission behavior differs as root")
def test_private_directory_mode_is_repaired(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    root.mkdir(mode=0o755)

    write_evidence(root, _record())

    assert root.stat().st_mode & 0o777 == 0o700
