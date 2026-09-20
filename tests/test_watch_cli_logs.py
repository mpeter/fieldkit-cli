"""Tests for the `fieldkit watch logs` command (logs_cmd)."""

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.watch.cli import cli

pytestmark = pytest.mark.unit

runner = CliRunner()

_PATCH_TARGET = "fieldkit.watch.logging.list_recent_logs"


def _make_log(tmp_path: Path, name: str, content: str, *, age_hours: float = 0.0) -> Path:
    f = tmp_path / name
    f.write_text(content, encoding="utf-8")
    if age_hours:
        mtime = time.time() - age_hours * 3600
        os.utime(f, (mtime, mtime))
    return f


def test_logs_no_files_without_watcher_name() -> None:
    with patch(_PATCH_TARGET, return_value=[]) as mock_list:
        result = runner.invoke(cli, ["logs"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "No log files found." in result.output
    mock_list.assert_called_once_with(watcher_name=None, n=20)


def test_logs_no_files_with_watcher_name_includes_label() -> None:
    with patch(_PATCH_TARGET, return_value=[]) as mock_list:
        result = runner.invoke(cli, ["logs", "backstory-health"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "No log files found for 'backstory-health'." in result.output
    mock_list.assert_called_once_with(watcher_name="backstory-health", n=20)


def test_logs_multiple_files_no_tail_lists_and_shows_most_recent(tmp_path: Path) -> None:
    newest = _make_log(tmp_path, "a.log", "newest-content")
    older = _make_log(tmp_path, "b.log", "older-content")
    with patch(_PATCH_TARGET, return_value=[newest, older]):
        result = runner.invoke(cli, ["logs"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "Recent log files (2):" in result.output
    assert "Showing most recent:" in result.output
    assert "newest-content" in result.output
    assert "NOTE:" not in result.output


def test_logs_list_only_flag_suppresses_content(tmp_path: Path) -> None:
    a = _make_log(tmp_path, "a.log", "should-not-appear")
    b = _make_log(tmp_path, "b.log", "also-hidden")
    with patch(_PATCH_TARGET, return_value=[a, b]):
        result = runner.invoke(cli, ["logs", "--list"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "Recent log files (2):" in result.output
    assert "Showing most recent:" not in result.output
    assert "should-not-appear" not in result.output


def test_logs_warns_when_last_run_older_than_24h(tmp_path: Path) -> None:
    a = _make_log(tmp_path, "a.log", "content-a", age_hours=48)
    b = _make_log(tmp_path, "b.log", "content-b", age_hours=72)
    with patch(_PATCH_TARGET, return_value=[a, b]):
        result = runner.invoke(cli, ["logs", "--list"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "ago — consider running 'fieldkit watch run --all'" in result.output


def test_logs_single_file_no_tail_shows_full_content(tmp_path: Path) -> None:
    f = _make_log(tmp_path, "only.log", "line1\nline2\nline3")
    with patch(_PATCH_TARGET, return_value=[f]):
        result = runner.invoke(cli, ["logs"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "Recent log files" not in result.output
    assert "line1" in result.output
    assert "line3" in result.output


def test_logs_tail_option_truncates_to_last_n_lines(tmp_path: Path) -> None:
    f = _make_log(tmp_path, "only.log", "l1\nl2\nl3\nl4")
    with patch(_PATCH_TARGET, return_value=[f]):
        result = runner.invoke(cli, ["logs", "--tail", "2"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "l1" not in result.output
    assert "l2" not in result.output
    assert "l3" in result.output
    assert "l4" in result.output


def test_logs_json_emits_one_document_with_selected_content(tmp_path: Path) -> None:
    import json

    newest = _make_log(tmp_path, "a.log", "l1\nl2\nl3")
    older = _make_log(tmp_path, "b.log", "old")
    with patch(_PATCH_TARGET, return_value=[newest, older]):
        result = runner.invoke(cli, ["logs", "--tail", "2", "--json"], catch_exceptions=False)

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["selected"].endswith("a.log")
    assert payload["content"] == "l2\nl3"
    assert len(payload["files"]) == 2
    assert payload["tail"] == 2


def test_logs_json_empty_result_is_structured() -> None:
    import json

    with patch(_PATCH_TARGET, return_value=[]):
        result = runner.invoke(cli, ["logs", "backstory-health", "--json"], catch_exceptions=False)

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload == {
        "watcher": "backstory-health",
        "files": [],
        "selected": None,
        "content": None,
        "list_only": False,
        "tail": None,
    }


def test_logs_json_list_suppresses_content(tmp_path: Path) -> None:
    import json

    log = _make_log(tmp_path, "only.log", "hidden")
    with patch(_PATCH_TARGET, return_value=[log]):
        result = runner.invoke(cli, ["logs", "--list", "--json"], catch_exceptions=False)

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["files"][0].endswith("only.log")
    assert payload["selected"] is None
    assert payload["content"] is None
