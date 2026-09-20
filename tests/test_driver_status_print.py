"""Tests for print_status in fieldkit.commands.driver._status.

Covers:
- Missing status file, both as_json branches
- Present status file with empty runs, both as_json branches
- JSON rendering: newest-first ordering and limit truncation
- Human rendering: outcome symbols, issue number/title formatting, elapsed
  seconds, and the conditional spend_note / error extra lines
"""

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.commands.driver._status import print_status

pytestmark = pytest.mark.unit

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _make_run(
    *,
    outcome: str = "ok",
    issue_number: int | None = 42,
    issue_title: str = "Fix the thing",
    ts: str = "2026-08-01T12:00:00Z",
    elapsed_seconds: float = 12.3,
    spend_note: str | None = None,
    error: str | None = None,
) -> dict:
    run: dict = {
        "outcome": outcome,
        "issue_number": issue_number,
        "issue_title": issue_title,
        "ts": ts,
        "elapsed_seconds": elapsed_seconds,
    }
    if spend_note is not None:
        run["spend_note"] = spend_note
    if error is not None:
        run["error"] = error
    return run


def _write_status_file(tmp_path: Path, runs: list[dict]) -> Path:
    status_dir = tmp_path / "logs" / "driver"
    status_dir.mkdir(parents=True, exist_ok=True)
    status_file = status_dir / "driver-run-status.json"
    status_file.write_text(json.dumps({"runs": runs}))
    return status_file


def test_missing_status_file_human_prints_no_runs_message(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    with patch("fieldkit.commands.driver._status.get_fieldkit_data", return_value=tmp_path):
        print_status(limit=10, as_json=False)

    out = capsys.readouterr().out
    assert "No driver runs recorded yet." in out


def test_missing_status_file_json_emits_empty_shape(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    with patch("fieldkit.commands.driver._status.get_fieldkit_data", return_value=tmp_path):
        print_status(limit=7, as_json=True)

    payload = json.loads(capsys.readouterr().out)
    assert payload["items"] == []
    assert payload["count"] == 0
    assert payload["error"] is None
    assert payload["filters"]["limit"] == 7


def test_present_file_empty_runs_human_prints_no_runs_message(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _write_status_file(tmp_path, [])

    with patch("fieldkit.commands.driver._status.get_fieldkit_data", return_value=tmp_path):
        print_status(limit=10, as_json=False)

    out = capsys.readouterr().out
    assert "No driver runs recorded yet." in out


def test_present_file_empty_runs_json_emits_empty_shape(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _write_status_file(tmp_path, [])

    with patch("fieldkit.commands.driver._status.get_fieldkit_data", return_value=tmp_path):
        print_status(limit=5, as_json=True)

    payload = json.loads(capsys.readouterr().out)
    assert payload["items"] == []
    assert payload["count"] == 0
    assert payload["error"] is None
    assert payload["filters"]["limit"] == 5


def test_json_output_is_newest_first_and_respects_limit(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    runs = [
        _make_run(issue_number=1, ts="2026-08-01T00:00:00Z"),
        _make_run(issue_number=2, ts="2026-08-02T00:00:00Z"),
        _make_run(issue_number=3, ts="2026-08-03T00:00:00Z"),
        _make_run(issue_number=4, ts="2026-08-04T00:00:00Z"),
    ]
    _write_status_file(tmp_path, runs)

    with patch("fieldkit.commands.driver._status.get_fieldkit_data", return_value=tmp_path):
        print_status(limit=2, as_json=True)

    payload = json.loads(capsys.readouterr().out)
    assert [item["issue_number"] for item in payload["items"]] == [4, 3]
    assert payload["count"] == 2


def test_human_rendering_outcome_symbols(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    runs = [
        _make_run(outcome="ok", issue_number=1, ts="2026-08-01T00:00:00Z"),
        _make_run(outcome="dry-run", issue_number=2, ts="2026-08-02T00:00:00Z"),
        _make_run(outcome="failed", issue_number=3, ts="2026-08-03T00:00:00Z"),
        _make_run(outcome="skipped", issue_number=4, ts="2026-08-04T00:00:00Z"),
        _make_run(outcome="mystery", issue_number=5, ts="2026-08-05T00:00:00Z"),
    ]
    _write_status_file(tmp_path, runs)

    with patch("fieldkit.commands.driver._status.get_fieldkit_data", return_value=tmp_path):
        print_status(limit=10, as_json=False)

    lines = [_strip_ansi(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 5
    # Rendering is newest-first: issue 5 (mystery) is newest, issue 1 (ok) oldest.
    assert "?" in lines[0]
    assert "-" in lines[1]
    assert "✗" in lines[2]
    assert "~" in lines[3]
    assert "✓" in lines[4]


def test_human_rendering_is_newest_first(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    runs = [
        _make_run(issue_number=1, ts="2026-08-01T00:00:00Z"),
        _make_run(issue_number=2, ts="2026-08-02T00:00:00Z"),
        _make_run(issue_number=3, ts="2026-08-03T00:00:00Z"),
    ]
    _write_status_file(tmp_path, runs)

    with patch("fieldkit.commands.driver._status.get_fieldkit_data", return_value=tmp_path):
        print_status(limit=10, as_json=False)

    lines = [_strip_ansi(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert next(line for line in lines if "#3" in line) == lines[0]
    assert next(line for line in lines if "#1" in line) == lines[-1]


def test_human_rendering_missing_issue_number_shows_none(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _write_status_file(tmp_path, [_make_run(issue_number=None)])

    with patch("fieldkit.commands.driver._status.get_fieldkit_data", return_value=tmp_path):
        print_status(limit=10, as_json=False)

    out = _strip_ansi(capsys.readouterr().out)
    assert "(none)" in out
    assert "#" not in out


def test_human_rendering_with_issue_number_shows_hash_prefix(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _write_status_file(tmp_path, [_make_run(issue_number=99)])

    with patch("fieldkit.commands.driver._status.get_fieldkit_data", return_value=tmp_path):
        print_status(limit=10, as_json=False)

    out = _strip_ansi(capsys.readouterr().out)
    assert "#99" in out
    assert "(none)" not in out


def test_human_rendering_truncates_long_title_to_50_chars(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    long_title = "x" * 80
    _write_status_file(tmp_path, [_make_run(issue_title=long_title)])

    with patch("fieldkit.commands.driver._status.get_fieldkit_data", return_value=tmp_path):
        print_status(limit=10, as_json=False)

    out = _strip_ansi(capsys.readouterr().out)
    assert "x" * 50 in out
    assert "x" * 51 not in out


def test_human_rendering_shows_spend_note_when_present(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _write_status_file(tmp_path, [_make_run(spend_note="cost: $1.23")])

    with patch("fieldkit.commands.driver._status.get_fieldkit_data", return_value=tmp_path):
        print_status(limit=10, as_json=False)

    out = _strip_ansi(capsys.readouterr().out)
    assert "cost: $1.23" in out


def test_human_rendering_omits_spend_note_when_absent(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _write_status_file(tmp_path, [_make_run()])

    with patch("fieldkit.commands.driver._status.get_fieldkit_data", return_value=tmp_path):
        print_status(limit=10, as_json=False)

    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1


def test_human_rendering_shows_error_when_present(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _write_status_file(tmp_path, [_make_run(error="boom: timeout")])

    with patch("fieldkit.commands.driver._status.get_fieldkit_data", return_value=tmp_path):
        print_status(limit=10, as_json=False)

    out = _strip_ansi(capsys.readouterr().out)
    assert "boom: timeout" in out


def test_human_rendering_omits_error_when_absent(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _write_status_file(tmp_path, [_make_run()])

    with patch("fieldkit.commands.driver._status.get_fieldkit_data", return_value=tmp_path):
        print_status(limit=10, as_json=False)

    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1
