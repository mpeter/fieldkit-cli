"""Saved report selection must not imply that a viewer actually launched."""

import importlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from fieldkit.companion.gate import NO_ACT_POLICY, is_allowed
from fieldkit.util.saved_reports import MAX_SAVED_REPORT_BYTES, read_saved_report

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("group", ["brief", "pipeline"])
@pytest.mark.parametrize(
    "scenario", ["no-open", "declined", "os-error", "symlink", "empty", "directory-symlink", "encoding", "oversize"]
)
def test_saved_report_viewer_outcomes(tmp_path: Path, group: str, scenario: str) -> None:
    module = importlib.import_module(f"fieldkit.commands.{group}.cli")
    reports = tmp_path / "briefs"
    reports.mkdir()
    prefix = "morning-brief" if group == "brief" else "pipeline-review"
    report = reports / f"{prefix}-2026-09-27.md"
    if scenario == "symlink":
        outside = tmp_path / "private.md"
        outside.write_text("private", encoding="utf-8")
        report.symlink_to(outside)
    else:
        report.write_text("" if scenario == "empty" else "# Report\n", encoding="utf-8")
    if scenario == "directory-symlink":
        outside_reports = tmp_path / "outside"
        reports.rename(outside_reports)
        reports.symlink_to(outside_reports, target_is_directory=True)
    elif scenario == "encoding":
        report.write_bytes(b"\xff")
    elif scenario == "oversize":
        report.write_bytes(b"x" * (MAX_SAVED_REPORT_BYTES + 1))
    argv = ["--json"] + (["--no-open"] if scenario == "no-open" else [])
    with (
        patch.object(module, "get_fieldkit_home", return_value=tmp_path),
        patch("webbrowser.open", return_value=False) as browser,
    ):
        if scenario == "os-error":
            browser.side_effect = OSError("private diagnostic must not appear")
        result = CliRunner().invoke(module.cmd_open, argv)
    invalid = scenario in {"symlink", "empty", "directory-symlink", "encoding", "oversize"}
    expected = 0 if scenario == "no-open" else (3 if invalid else 1)
    assert result.exit_code == expected, result.output
    if invalid:
        browser.assert_not_called()
    else:
        assert json.loads(result.stdout)["opened"] is False
        if scenario == "no-open":
            browser.assert_not_called()
        else:
            browser.assert_called_once_with(report.as_uri())
    assert "private diagnostic" not in result.output


def test_saved_report_snapshot_contract(tmp_path: Path) -> None:
    reports = tmp_path / "briefs"
    reports.mkdir()
    report = reports / "morning-brief-2026-09-27.md"
    report.write_text("# Brief\n", encoding="utf-8")
    snapshot = read_saved_report(report, tmp_path)
    assert snapshot.content == "# Brief\n"
    assert snapshot.info.st_size == len(snapshot.content.encode("utf-8"))


@pytest.mark.parametrize("group", ["brief", "pipeline"])
@pytest.mark.parametrize("tokens", [[], ["--json"], ["--no-open", "--future-option"], ["--no-open=false"]])
def test_companion_read_policy_denies_viewer_launch_or_unknown_options(group: str, tokens: list[str]) -> None:
    result = is_allowed([group, "open", *tokens], "read", NO_ACT_POLICY)
    assert result is False


@pytest.mark.parametrize("group", ["brief", "pipeline"])
def test_companion_read_policy_accepts_metadata_only(group: str) -> None:
    result = is_allowed([group, "open", "--no-open", "--json"], "read", NO_ACT_POLICY)
    assert result is True


def test_account_value_does_not_grant_no_open_permission() -> None:
    result = is_allowed(["pipeline", "open", "--account", "--no-open"], "read", NO_ACT_POLICY)
    assert result is False
