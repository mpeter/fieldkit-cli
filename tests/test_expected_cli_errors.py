"""Expected operator errors keep stdout clean and preserve unexpected failures."""

from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.__main__ import main

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("selector", ["shorthand", "path", "flags"])
def test_missing_advance_target(tmp_path: Path, capsys: pytest.CaptureFixture[str], selector: str) -> None:
    accounts = tmp_path / "accounts"
    accounts.mkdir()
    args = {
        "shorthand": ["acme-fictional/missing"],
        "path": [str(accounts / "acme-fictional/pursuits/missing.md")],
        "flags": ["--account", "acme-fictional", "--name", "missing"],
    }[selector]
    before = list(tmp_path.rglob("*"))
    with patch("fieldkit.commands.pursuit.advance_cmd.get_accounts_root", return_value=accounts):
        result = main(["pursuit", "advance", *args, "--dry-run", "--json"])
    assert result == 3
    output = capsys.readouterr()
    assert output.out == ""
    assert "Cannot find pursuit" in output.err
    assert "Pass the full path" in output.err
    assert "Traceback" not in output.err
    assert "Unhandled exception" not in output.err
    assert list(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("args", [["--account", "acme-fictional"], ["--unknown"]])
def test_invalid_advance_usage(args: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    result = main(["pursuit", "advance", *args, "--dry-run", "--json"])
    assert result == 3
    output = capsys.readouterr()
    assert output.out == ""
    assert "Error:" in output.err
    assert "Traceback" not in output.err


@pytest.mark.parametrize("content", ["{invalid", "[]"])
def test_malformed_companion_state(tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str) -> None:
    state = tmp_path / "watchers/watcher-run-status.json"
    state.parent.mkdir()
    state.write_text(content, encoding="utf-8")
    before = state.read_bytes()
    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.config.get_fieldkit_data", return_value=tmp_path),
    ):
        result = main(["companion", "feed", "--all", "--json"])
    assert result == 3
    output = capsys.readouterr()
    assert output.out == ""
    assert "Malformed watcher-run-status.json" in output.err
    assert "Traceback" not in output.err
    assert str(tmp_path) not in output.err
    assert state.read_bytes() == before
    assert not (tmp_path / "companion-cursor.json").exists()


def test_unexpected_failure_keeps_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("fieldkit.__main__.cli.main", side_effect=RuntimeError("fictional programmer failure")):
        result = main(["pursuit", "advance"])
    assert result == 3
    output = capsys.readouterr()
    assert output.out == ""
    assert "Unhandled exception" in output.err
    assert "Traceback" in output.err
    assert "fictional programmer failure" in output.err
