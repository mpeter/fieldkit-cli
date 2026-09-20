"""Tests for scripts/check_agentready.py.

``scripts/`` is added to sys.path by conftest.py (line 20), so the module
can be imported directly as ``check_agentready``.

JSON_PATH is patched via monkeypatch.setattr so no real filesystem state is
required and tests are fully isolated.
"""

import json
from pathlib import Path

import check_agentready
import pytest


@pytest.mark.unit
def test_pass_score_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Score >= 85 returns normally (exit 0) and prints score, tier, and checkmark."""
    json_file = tmp_path / ".agentready" / "assessment-latest.json"
    json_file.parent.mkdir(parents=True)
    json_file.write_text(json.dumps({"overall_score": 87.3, "tier": "Gold"}), encoding="utf-8")

    monkeypatch.setattr("check_agentready.JSON_PATH", json_file)

    # Should not raise at all
    check_agentready.main()

    captured = capsys.readouterr()
    assert "87.3" in captured.out
    assert "Gold" in captured.out
    assert "✓" in captured.out


@pytest.mark.unit
def test_fail_score_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Score < 85 exits 1 and prints score, tier, FAIL, and threshold."""
    json_file = tmp_path / ".agentready" / "assessment-latest.json"
    json_file.parent.mkdir(parents=True)
    json_file.write_text(json.dumps({"overall_score": 72.1, "tier": "Silver"}), encoding="utf-8")

    monkeypatch.setattr("check_agentready.JSON_PATH", json_file)

    with pytest.raises(SystemExit) as exc_info:
        check_agentready.main()

    assert exc_info.value.code == 1

    captured = capsys.readouterr()
    assert "72.1" in captured.out
    assert "FAIL" in captured.out
    assert "85" in captured.out


@pytest.mark.unit
def test_missing_file_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Missing JSON file exits 1 with absolute path and repo root hint."""
    json_file = tmp_path / ".agentready" / "assessment-latest.json"
    # Do NOT create the file — it must be absent.

    monkeypatch.setattr("check_agentready.JSON_PATH", json_file)

    with pytest.raises(SystemExit) as exc_info:
        check_agentready.main()

    assert exc_info.value.code == 1

    captured = capsys.readouterr()
    # Error goes to stderr
    assert str(json_file.resolve()) in captured.err
    assert "repo root" in captured.err


@pytest.mark.unit
def test_malformed_json_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Malformed JSON exits 1."""
    json_file = tmp_path / ".agentready" / "assessment-latest.json"
    json_file.parent.mkdir(parents=True)
    json_file.write_text("not json{{{", encoding="utf-8")

    monkeypatch.setattr("check_agentready.JSON_PATH", json_file)

    with pytest.raises(SystemExit) as exc_info:
        check_agentready.main()

    assert exc_info.value.code == 1


@pytest.mark.unit
def test_missing_overall_score_field_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """JSON without 'overall_score' field exits 1."""
    json_file = tmp_path / ".agentready" / "assessment-latest.json"
    json_file.parent.mkdir(parents=True)
    json_file.write_text(json.dumps({"tier": "Bronze"}), encoding="utf-8")

    monkeypatch.setattr("check_agentready.JSON_PATH", json_file)

    with pytest.raises(SystemExit) as exc_info:
        check_agentready.main()

    assert exc_info.value.code == 1


@pytest.mark.unit
def test_non_numeric_score_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """'overall_score' with a non-numeric value exits 1 with a type error message."""
    json_file = tmp_path / ".agentready" / "assessment-latest.json"
    json_file.parent.mkdir(parents=True)
    json_file.write_text(json.dumps({"overall_score": "Gold", "tier": "Gold"}), encoding="utf-8")

    monkeypatch.setattr("check_agentready.JSON_PATH", json_file)

    with pytest.raises(SystemExit) as exc_info:
        check_agentready.main()

    assert exc_info.value.code == 1

    captured = capsys.readouterr()
    assert "not numeric" in captured.err
    assert "str" in captured.err
