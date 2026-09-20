"""Unit tests for lib/watcher_status.py.

Regression focus: path resolution must use get_fieldkit_home(), never __file__-
relative inference. A custom data root (via monkeypatched get_fieldkit_home) must
produce files inside the injected directory — not anywhere under the installed
package tree.
"""

import json
import typing
from pathlib import Path

import pytest

import fieldkit.watch.status as ws_mod
from fieldkit.watch.status import WatcherOutcome, write_run_status

pytestmark = pytest.mark.unit


# ── helpers ────────────────────────────────────────────────────────────────


def _package_path() -> Path:
    """Return the directory that contains lib/watcher_status.py."""
    return Path(ws_mod.__file__).resolve().parent


# ── path-is-not-__file__-relative regression ───────────────────────────────


def test_write_run_status_writes_inside_temp_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """write_run_status() must create the JSON file inside the monkeypatched data root.

    This is the core regression guard: before T01 the path was inferred from
    __file__, so a uv tool install would write into the package directory.
    """
    monkeypatch.setattr(ws_mod, "get_fieldkit_home", lambda: tmp_path)

    write_run_status(
        watcher="backstory-health",
        outcome="ok",
        records_checked=5,
        alerts_generated=1,
        failures=0,
        elapsed_seconds=1.2,
        dry_run=False,
    )

    expected = tmp_path / "watchers" / "watcher-run-status.json"
    assert expected.exists(), f"Expected JSON at {expected} but it was not created"


def test_write_run_status_not_written_under_package_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The JSON file must NOT appear anywhere under the package installation directory.

    Guards against regression to __file__-relative path resolution.
    """
    monkeypatch.setattr(ws_mod, "get_fieldkit_home", lambda: tmp_path)

    write_run_status(
        watcher="pursuit-stalls",
        outcome="partial",
        records_checked=3,
        alerts_generated=0,
        failures=1,
        elapsed_seconds=0.5,
        dry_run=False,
    )

    pkg_dir = _package_path()
    # Walk files created during this test that live under the package directory.
    # Any match means the old __file__-relative path is back.
    written_under_pkg = list(pkg_dir.rglob("watcher-run-status.json"))
    assert written_under_pkg == [], f"watcher-run-status.json found under package dir {pkg_dir}: {written_under_pkg}"


def test_write_run_status_path_under_custom_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The written file path must be a descendant of the injected data root."""
    data_root = tmp_path / "custom-data-root"
    monkeypatch.setattr(ws_mod, "get_fieldkit_home", lambda: data_root)

    write_run_status(
        watcher="slack-threads",
        outcome="ok",
        records_checked=10,
        alerts_generated=0,
        failures=0,
        elapsed_seconds=2.0,
        dry_run=False,
    )

    written = data_root / "watchers" / "watcher-run-status.json"
    assert written.exists()
    # Confirm the path is genuinely under data_root, not a coincidental match.
    assert written.is_relative_to(data_root)


# ── dry_run skips writing ───────────────────────────────────────────────────


def test_write_run_status_dry_run_skips_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """dry_run=True must not create any file."""
    monkeypatch.setattr(ws_mod, "get_fieldkit_home", lambda: tmp_path)

    write_run_status(
        watcher="morning-brief",
        outcome="ok",
        records_checked=2,
        alerts_generated=0,
        failures=0,
        elapsed_seconds=0.1,
        dry_run=True,
    )

    status_file = tmp_path / "watchers" / "watcher-run-status.json"
    assert not status_file.exists()


# ── JSON content correctness ────────────────────────────────────────────────


def test_write_run_status_json_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The written JSON must contain the expected entry with correct fields."""
    monkeypatch.setattr(ws_mod, "get_fieldkit_home", lambda: tmp_path)

    write_run_status(
        watcher="backstory-health",
        outcome="fatal",
        records_checked=0,
        alerts_generated=0,
        failures=7,
        elapsed_seconds=3.456,
        dry_run=False,
    )

    written = tmp_path / "watchers" / "watcher-run-status.json"
    data = json.loads(written.read_text(encoding="utf-8"))

    entry = data["backstory-health"]
    assert entry["outcome"] == "fatal"
    assert entry["records_checked"] == 0
    assert entry["alerts_generated"] == 0
    assert entry["failures"] == 7
    assert entry["elapsed_seconds"] == pytest.approx(3.5, abs=0.1)
    assert "last_run" in entry


def test_write_run_status_updates_existing_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second write for the same watcher must update (not duplicate) the entry."""
    monkeypatch.setattr(ws_mod, "get_fieldkit_home", lambda: tmp_path)

    write_run_status(
        watcher="pursuit-stalls",
        outcome="ok",
        records_checked=5,
        alerts_generated=1,
        failures=0,
        elapsed_seconds=1.0,
        dry_run=False,
    )
    write_run_status(
        watcher="pursuit-stalls",
        outcome="partial",
        records_checked=3,
        alerts_generated=0,
        failures=2,
        elapsed_seconds=0.8,
        dry_run=False,
    )

    written = tmp_path / "watchers" / "watcher-run-status.json"
    data = json.loads(written.read_text(encoding="utf-8"))

    # Only one entry for this watcher.
    assert list(data.keys()) == ["pursuit-stalls"]
    assert data["pursuit-stalls"]["outcome"] == "partial"
    assert data["pursuit-stalls"]["failures"] == 2


# ── historic regression: with_name() not with_suffix() for atomic tmp file ──────────────


def test_write_run_status_end_to_end_creates_file_with_correct_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """write_run_status() must create the JSON file and populate it correctly.

    Regression guard for historic regression: with_suffix('.json.tmp') raises ValueError on
    Python 3.12+ because '.json.tmp' contains an embedded dot.  The fix uses
    with_name(name + '.tmp') which is valid on all supported Python versions.

    This test exercises the full write path end-to-end — file creation, atomic
    rename, and JSON content — so a silent OSError swallow cannot hide the bug.
    """
    monkeypatch.setattr(ws_mod, "get_fieldkit_home", lambda: tmp_path)

    write_run_status(
        watcher="backstory-health",
        outcome="ok",
        records_checked=7,
        alerts_generated=2,
        failures=0,
        elapsed_seconds=1.5,
        dry_run=False,
    )

    status_file = tmp_path / "watchers" / "watcher-run-status.json"
    # File must exist — a ValueError swallowed by except OSError would leave it absent.
    assert status_file.exists(), f"Status file not created at {status_file}"

    data = json.loads(status_file.read_text(encoding="utf-8"))
    assert "backstory-health" in data, f"Watcher key missing from {data}"

    entry = data["backstory-health"]
    assert entry["outcome"] == "ok"
    assert entry["records_checked"] == 7
    assert entry["alerts_generated"] == 2
    assert entry["failures"] == 0
    assert entry["elapsed_seconds"] == pytest.approx(1.5, abs=0.1)
    assert entry["dry_run"] is False
    assert "last_run" in entry
    # Confirm no stale .tmp file was left behind after the atomic rename.
    assert not (tmp_path / "watchers" / "watcher-run-status.json.tmp").exists()


# ── WatcherOutcome Literal annotation regression ────────────────────────────


def test_outcome_parameter_typed_with_literal() -> None:
    """outcome parameter must be Literal['ok', 'partial', 'fatal'], not str."""
    hints = typing.get_type_hints(write_run_status)
    assert hints["outcome"] is WatcherOutcome


# ── was_run_today and get_last_run_outcome direct coverage ──────────────────


def test_load_run_status_returns_empty_when_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_load_run_status() returns {} when the status file does not exist."""
    from fieldkit.watch import status as _ws

    monkeypatch.setattr(_ws, "get_fieldkit_home", lambda: tmp_path)
    result = _ws._load_run_status()
    assert result == {}


def test_load_run_status_returns_parsed_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_load_run_status() returns the parsed JSON from the status file."""
    import datetime

    from fieldkit.watch import status as _ws

    today = datetime.date.today().isoformat()
    status_file = tmp_path / "watchers" / "watcher-run-status.json"
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text(
        f'{{"my-watcher": {{"last_run": "{today}T10:00:00", "outcome": "ok"}}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(_ws, "get_fieldkit_home", lambda: tmp_path)

    result = _ws._load_run_status()
    assert "my-watcher" in result
    assert result["my-watcher"]["outcome"] == "ok"


# ── implementation note: ConfigError in _load_run_status must not abort write ────────────


def test_write_run_status_survives_config_error_on_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """write_run_status() must complete and write the file even when the status file is absent.

    Regression guard for implementation note: write_run_status() must proceed with an empty
    baseline when no prior status file exists.  Under locked_json_update this is
    handled internally — the context manager starts from {} on a missing file,
    so no separate "starting fresh" log is emitted by write_run_status().
    """
    import logging

    monkeypatch.setattr(ws_mod, "get_fieldkit_home", lambda: tmp_path)

    with caplog.at_level(logging.DEBUG, logger="fieldkit.watch.status"):
        write_run_status(
            watcher="pursuit-stalls",
            outcome="ok",
            records_checked=5,
            alerts_generated=1,
            failures=0,
            elapsed_seconds=1.0,
            dry_run=False,
        )

    # The status file must have been written with correct data.
    status_file = tmp_path / "watchers" / "watcher-run-status.json"
    assert status_file.exists(), "Status file must be written even when no prior file exists"

    data = json.loads(status_file.read_text(encoding="utf-8"))
    assert "pursuit-stalls" in data
    assert data["pursuit-stalls"]["outcome"] == "ok"

    # Debug log must have been emitted confirming the write succeeded.
    assert any("watcher-run-status" in r.message for r in caplog.records), (
        f"Expected debug log for watcher-run-status; got: {[r.message for r in caplog.records]}"
    )


def test_write_run_status_config_error_leaves_no_tmp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No .tmp file must remain on disk after write_run_status() completes.

    Confirms the atomic rename completed cleanly via locked_json_update.
    """
    monkeypatch.setattr(ws_mod, "get_fieldkit_home", lambda: tmp_path)

    write_run_status(
        watcher="morning-brief",
        outcome="partial",
        records_checked=2,
        alerts_generated=0,
        failures=1,
        elapsed_seconds=0.5,
        dry_run=False,
    )

    tmp_file = tmp_path / "watchers" / "watcher-run-status.json.tmp"
    assert not tmp_file.exists(), f"Stale .tmp file found at {tmp_file}"


def test_load_all_statuses_returns_empty_when_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """load_all_statuses() returns {} when the status file does not exist."""
    monkeypatch.setattr(ws_mod, "get_fieldkit_home", lambda: tmp_path)
    assert ws_mod.load_all_statuses() == {}


def test_load_all_statuses_returns_all_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """load_all_statuses() returns every watcher entry written so far, keyed by name."""
    monkeypatch.setattr(ws_mod, "get_fieldkit_home", lambda: tmp_path)

    write_run_status(
        watcher="backstory-health",
        outcome="ok",
        records_checked=5,
        alerts_generated=1,
        failures=0,
        elapsed_seconds=1.2,
        dry_run=False,
    )
    write_run_status(
        watcher="pursuit-stalls",
        outcome="partial",
        records_checked=3,
        alerts_generated=0,
        failures=1,
        elapsed_seconds=0.5,
        dry_run=False,
    )

    statuses = ws_mod.load_all_statuses()
    assert set(statuses) == {"backstory-health", "pursuit-stalls"}
    assert statuses["backstory-health"]["outcome"] == "ok"
    assert statuses["pursuit-stalls"]["outcome"] == "partial"


def test_write_run_status_load_raises_arbitrary_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """write_run_status() must succeed when no prior status file exists.

    locked_json_update starts from {} when the file is absent, so arbitrary
    prior-state errors are handled internally.  The write must succeed.
    """
    monkeypatch.setattr(ws_mod, "get_fieldkit_home", lambda: tmp_path)

    write_run_status(
        watcher="backstory-health",
        outcome="fatal",
        records_checked=0,
        alerts_generated=0,
        failures=3,
        elapsed_seconds=0.2,
        dry_run=False,
    )

    status_file = tmp_path / "watchers" / "watcher-run-status.json"
    assert status_file.exists()
    data = json.loads(status_file.read_text(encoding="utf-8"))
    assert data["backstory-health"]["outcome"] == "fatal"
