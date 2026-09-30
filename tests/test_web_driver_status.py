"""Developer dashboard status reads local evidence without an unsupported CLI call."""

import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from fieldkit.autonomy.status import MAX_STATUS_RECORD_BYTES, observe_admission, observe_driver
from fieldkit.web.data import DataSource


@pytest.mark.unit
def test_unconfigured_developer_status_does_not_invoke_cli(tmp_path: Path) -> None:
    cli = Mock(side_effect=AssertionError("Developer status must not invoke the CLI"))
    source = DataSource(briefs_dir=tmp_path / "briefs", watchers_dir=tmp_path / "watchers", cli_json=cli)

    result = source._developer_operation()

    assert result.status == "missing"
    assert result.updated_at is None
    assert result.detail == "No developer run has been recorded."
    cli.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("allowed", [False, True])
def test_developer_status_prioritizes_admission_denial_then_driver_failure(tmp_path: Path, allowed: bool) -> None:
    admission = tmp_path / "driver" / "developer-admission.json"
    admission.parent.mkdir()
    admission.write_text(
        json.dumps(
            {
                "decisions": [
                    {
                        "ts": "2026-09-26T12:00:00Z",
                        "allowed": allowed,
                        "reason_code": "policy-check",
                        "detail": "review policy",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    driver = tmp_path / "logs" / "driver" / "driver-run-status.json"
    driver.parent.mkdir(parents=True)
    driver.write_text(
        '{"runs": [{"ts": "2026-09-26T11:00:00Z", "outcome": "failed", "error": "run failed"}]}',
        encoding="utf-8",
    )
    source = DataSource(briefs_dir=tmp_path / "briefs", watchers_dir=tmp_path / "watchers", data_dir=tmp_path)

    result = source._developer_operation()

    assert result.status == "error"
    assert result.detail == ("run failed" if allowed else "Developer admission denied: policy-check — review policy")
    assert result.updated_at == ("2026-09-26T11:00:00Z" if allowed else "2026-09-26T12:00:00Z")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (None, "missing"),
        ("not json", "error"),
        (b"\xff", "error"),
        ('{"runs": [{"ts": "2026-09-26T12:00:00Z", "outcome": "banana"}]}', "error"),
        ('{"runs": [{"ts": "2026-09-26T12:00:00Z", "outcome": "ok", "error": {}}]}', "error"),
        ('{"runs": [{"ts": "2026-09-26T12:00:00Z", "outcome": "ok"}, 42]}', "error"),
        ('{"runs": [{"ts": "2026-09-26T12:00:00Z", "outcome": "ok"}]}', "ok"),
        ('{"runs": [{"ts": "2026-09-26T12:00:00Z", "outcome": "skipped"}]}', "ok"),
        ('{"runs": [{"ts": "2026-09-26T12:00:00Z", "outcome": "dry-run"}]}', "ok"),
        ('{"runs": [{"ts": "2026-09-26T12:00:00Z", "outcome": "failed", "error": "run failed"}]}', "error"),
    ],
)
def test_developer_status_uses_local_record(tmp_path: Path, record: str | bytes | None, expected: str) -> None:
    if record is not None:
        path = tmp_path / "logs" / "driver" / "driver-run-status.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(record if isinstance(record, bytes) else record.encode("utf-8"))
    cli = Mock(side_effect=AssertionError("Developer status must not invoke the CLI"))
    source = DataSource(
        briefs_dir=tmp_path / "briefs", watchers_dir=tmp_path / "watchers", data_dir=tmp_path, cli_json=cli
    )

    result = source._developer_operation()

    assert result.status == expected
    assert "fieldkit driver" not in result.action
    cli.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("record", [b"not json", b"\xff", b'{"decisions": [42]}'])
def test_malformed_admission_is_not_healthy(tmp_path: Path, record: bytes) -> None:
    driver = tmp_path / "logs" / "driver" / "driver-run-status.json"
    driver.parent.mkdir(parents=True)
    driver.write_text('{"runs": [{"ts": "2026-09-26T12:00:00Z", "outcome": "ok"}]}', encoding="utf-8")
    admission = tmp_path / "driver" / "developer-admission.json"
    admission.parent.mkdir()
    admission.write_bytes(record)
    source = DataSource(briefs_dir=tmp_path / "briefs", watchers_dir=tmp_path / "watchers", data_dir=tmp_path)

    result = source._developer_operation()

    assert result.status == "error"
    assert "admission" in result.detail


@pytest.mark.unit
@pytest.mark.parametrize("extra", [0, 1])
@pytest.mark.parametrize("admission", [False, True])
def test_observation_record_size_boundary(tmp_path: Path, extra: int, admission: bool) -> None:
    if admission:
        path = tmp_path / "driver" / "developer-admission.json"
        record = (
            b'{"decisions": [{"ts": "2026-09-26T12:00:00Z", "allowed": true, "reason_code": "admitted", "detail": ""}]}'
        )
        observe = observe_admission
    else:
        path = tmp_path / "logs" / "driver" / "driver-run-status.json"
        record = b'{"runs": [{"ts": "2026-09-26T12:00:00Z", "outcome": "ok"}]}'
        observe = observe_driver
    path.parent.mkdir(parents=True)
    path.write_bytes(record + b" " * (MAX_STATUS_RECORD_BYTES - len(record) + extra))

    result = observe(tmp_path)

    assert result.state == ("malformed" if extra else "available")
    if extra:
        assert "size limit" in result.detail
        assert result.observed_at is None
    else:
        assert result.observed_at == "2026-09-26T12:00:00Z"


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["fifo", "directory", "symlink"])
def test_driver_observation_rejects_nonregular_records(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "logs" / "driver" / "driver-run-status.json"
    path.parent.mkdir(parents=True)
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    else:
        target = tmp_path / "outside.json"
        target.write_text('{"runs": [{"ts": "2026-09-26T12:00:00Z", "outcome": "ok"}]}', encoding="utf-8")
        path.symlink_to(target)

    result = observe_driver(tmp_path)

    assert result.state == "malformed"
    assert result.values is None


@pytest.mark.unit
@pytest.mark.parametrize("detail", [None, 42, {"private": "payload"}, ["payload"]])
def test_admission_rejects_missing_or_nonstring_detail(tmp_path: Path, detail: object) -> None:
    decision: dict[str, object] = {"ts": "2026-09-26T12:00:00Z", "allowed": False, "reason_code": "denied"}
    if detail is not None:
        decision["detail"] = detail
    path = tmp_path / "driver" / "developer-admission.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"decisions": [decision]}), encoding="utf-8")

    result = observe_admission(tmp_path)

    assert result.state == "malformed"
    assert "payload" not in result.detail
    assert result.values is None
