"""Regression coverage for historic regression partial batch mutation accounting."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

pytestmark = pytest.mark.unit


def _write_meeting(path: Path) -> None:
    path.write_text("---\ntitle: Account meeting\n---\n\nNotes.\n", encoding="utf-8")


def _write_pursuit(path: Path) -> None:
    path.write_text("---\nstage: closed-won\ngate-status: pending\n---\n\n# Deal\n", encoding="utf-8")


def _fail_rename_for(monkeypatch: pytest.MonkeyPatch, failing_name: str) -> list[str]:
    original_rename = Path.rename
    attempted: list[str] = []

    def fail_selected(path: Path, target: str | Path) -> Path:
        attempted.append(path.name)
        if path.name == failing_name:
            raise OSError("private filesystem detail must not reach JSON")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_selected)
    return attempted


def _ingest_root(tmp_path: Path) -> tuple[Path, list[Path]]:
    unknown = tmp_path / "accounts" / "unknown" / "meetings"
    unknown.mkdir(parents=True)
    files = [unknown / f"acme-corp-0{index}.md" for index in range(1, 4)]
    for path in files:
        _write_meeting(path)
    return unknown, files


def test_ingest_route_json_reports_completed_and_aborting_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fieldkit.commands.ingest.route import cli

    unknown, files = _ingest_root(tmp_path)
    attempted = _fail_rename_for(monkeypatch, files[1].name)
    accounts = {"accounts": {"acme-corp": {}}}

    with patch("fieldkit.config.get_accounts_config", return_value=accounts):
        result = CliRunner().invoke(cli, ["--data-root", str(tmp_path), "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["outcome"] == "partial"
    assert payload["aborted_at"] == files[1].name
    assert payload["error"] == {"code": "route-mutation-failed", "operation": "move"}
    assert payload["count"] == len(payload["items"]) == 2
    assert payload["items"][0]["outcome"] == "moved"
    assert payload["items"][1] == {
        "item": files[1].name,
        "outcome": "error",
        "ok": False,
        "frontmatter_updated": True,
        "move_completed": False,
    }
    assert "private filesystem detail" not in result.stdout
    assert attempted == [files[0].name, files[1].name]
    assert (tmp_path / "accounts" / "acme-corp" / "meetings" / files[0].name).exists()
    assert files[1].exists()
    assert "account: acme-corp" in files[1].read_text(encoding="utf-8")
    assert files[2].exists()
    assert "account: acme-corp" not in files[2].read_text(encoding="utf-8")
    assert not (unknown / files[0].name).exists()


def test_ingest_route_human_failure_has_summary_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fieldkit.commands.ingest.route import cli

    _unknown, files = _ingest_root(tmp_path)
    _fail_rename_for(monkeypatch, files[1].name)
    accounts = {"accounts": {"acme-corp": {}}}

    with patch("fieldkit.config.get_accounts_config", return_value=accounts):
        result = CliRunner().invoke(cli, ["--data-root", str(tmp_path)])

    assert result.exit_code == 1
    assert f"MOVED {files[0].name}" in result.output
    assert f"move failed for {files[1].name}" in result.output
    assert "PARTIAL Summary: 1 moved" in result.output
    assert "2 of 3 attempted, 1 error" in result.output
    assert "Traceback" not in result.output


def _pursuit_root(tmp_path: Path) -> tuple[Path, list[Path]]:
    pursuits = tmp_path / "accounts" / "acme-corp" / "pursuits"
    pursuits.mkdir(parents=True)
    files = [pursuits / f"deal-0{index}.md" for index in range(1, 4)]
    for path in files:
        _write_pursuit(path)
    return pursuits, files


def test_pursuit_archive_json_reports_completed_and_aborting_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fieldkit.commands.pursuit.archive_cmd import cli

    pursuits, files = _pursuit_root(tmp_path)
    attempted = _fail_rename_for(monkeypatch, files[1].name)

    with patch("fieldkit.commands.pursuit.archive_cmd._data_root", return_value=tmp_path):
        result = CliRunner().invoke(cli, ["--account", "acme-corp", "--all-closed", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["outcome"] == "partial"
    assert payload["aborted_at"] == files[1].name
    assert payload["error"] == {"code": "archive-mutation-failed", "operation": "move"}
    assert payload["count"] == len(payload["items"]) == 2
    assert payload["items"][0]["outcome"] == "archived"
    assert payload["items"][0]["archive_dir_created"] is True
    assert payload["items"][1]["outcome"] == "error"
    assert payload["items"][1]["archive_dir_created"] is False
    assert "private filesystem detail" not in result.stdout
    assert attempted == [files[0].name, files[1].name]
    assert (tmp_path / "accounts" / "acme-corp" / "archive" / files[0].name).exists()
    assert files[1].exists()
    assert files[2].exists()
    assert not (pursuits / files[0].name).exists()


def test_pursuit_archive_first_failure_records_directory_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fieldkit.commands.pursuit.archive_cmd import cli

    _pursuits, files = _pursuit_root(tmp_path)
    attempted = _fail_rename_for(monkeypatch, files[0].name)

    with patch("fieldkit.commands.pursuit.archive_cmd._data_root", return_value=tmp_path):
        result = CliRunner().invoke(cli, ["--account", "acme-corp", "--all-closed", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["items"][0]["archive_dir_created"] is True
    assert attempted == [files[0].name]


def test_pursuit_archive_human_failure_has_summary_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fieldkit.commands.pursuit.archive_cmd import cli

    _pursuits, files = _pursuit_root(tmp_path)
    _fail_rename_for(monkeypatch, files[1].name)

    with patch("fieldkit.commands.pursuit.archive_cmd._data_root", return_value=tmp_path):
        result = CliRunner().invoke(cli, ["--account", "acme-corp", "--all-closed"])

    assert result.exit_code == 1
    assert f"archived: {files[0].name}" in result.output
    assert f"move failed for {files[1].name}" in result.output
    assert "PARTIAL archived 1 pursuit(s)" in result.output
    assert "Traceback" not in result.output
