"""Tests for `cli` in fieldkit.commands.pursuit.projects_health — remaining branches.

Existing coverage (test_projects_health.py, test_projects_health_classify.py,
test_pursuit_projects_health.py) exercises classify_project/health_check directly
and the CLI happy path. This file fills the remaining gaps in `cli` itself:
  - the `data_root is None` guard (no config)
  - the accounts-directory-missing guard
  - the `no_files_message` early-return when `health_check` returns []
  - the entire `--json` output branch (dataclasses.asdict serialization and its
    own ZOMBIE/UNKNOWN-driven exit code)

Local helpers only — the shared test files above are not touched.
"""

import dataclasses
import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.pursuit.projects_health import cli
from fieldkit.pursuit.projects import ProjectRow

pytestmark = pytest.mark.unit

MODULE = "fieldkit.commands.pursuit.projects_health"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_row(
    health: str = "ACTIVE",
    name: str = "proj",
    relative_path: str = "acme/projects/proj.md",
    sf_stage: str = "Delivery",
    contract_end_str: str = "2026-12-31",
    days_until_end: int | None = 90,
    opportunity: str = "OPP-001",
) -> ProjectRow:
    return ProjectRow(
        relative_path=relative_path,
        name=name,
        sf_stage=sf_stage,
        contract_end_str=contract_end_str,
        days_until_end=days_until_end,
        health=health,
        opportunity=opportunity,
    )


# ---------------------------------------------------------------------------
# 1. data_root is None
# ---------------------------------------------------------------------------


def test_cli_no_data_root_exits_3_without_calling_health_check() -> None:
    runner = CliRunner()
    with (
        patch(f"{MODULE}.get_fieldkit_home", return_value=None),
        patch(f"{MODULE}.health_check") as mock_health_check,
    ):
        result = runner.invoke(cli, ["--strict"])

    assert result.exit_code == 3
    assert "No data root configured. Run: fieldkit init" in result.output
    mock_health_check.assert_not_called()


# ---------------------------------------------------------------------------
# 2. accounts_dir not a directory
# ---------------------------------------------------------------------------


def test_cli_missing_accounts_dir_exits_3_without_calling_health_check(tmp_path: Path) -> None:
    # tmp_path exists but its "accounts" subdirectory is never created.
    runner = CliRunner()
    with (
        patch(f"{MODULE}.get_fieldkit_home", return_value=str(tmp_path)),
        patch(f"{MODULE}.health_check") as mock_health_check,
    ):
        result = runner.invoke(cli, [])

    assert result.exit_code == 3
    assert "Accounts directory not found. Run 'fieldkit init' to initialize." in result.output
    mock_health_check.assert_not_called()


# ---------------------------------------------------------------------------
# 3. empty rows -> no_files_message early return
# ---------------------------------------------------------------------------


def test_cli_empty_rows_uses_no_files_message_and_exits_3(tmp_path: Path) -> None:
    (tmp_path / "accounts").mkdir()
    runner = CliRunner()
    with (
        patch(f"{MODULE}.get_fieldkit_home", return_value=str(tmp_path)),
        patch(f"{MODULE}.health_check", return_value=[]) as mock_health_check,
        patch(f"{MODULE}.no_files_message", return_value="DISTINCTIVE_NO_FILES_MSG") as mock_msg,
    ):
        result = runner.invoke(cli, [])

    assert result.exit_code == 3
    assert "DISTINCTIVE_NO_FILES_MSG" in result.output
    mock_msg.assert_called_once_with("project", None)
    mock_health_check.assert_called_once()


# ---------------------------------------------------------------------------
# 4. --json, no ZOMBIE/UNKNOWN rows -> exit 0, JSON matches asdict output
# ---------------------------------------------------------------------------


def test_cli_json_no_zombie_or_unknown_exits_0_with_matching_json(tmp_path: Path) -> None:
    (tmp_path / "accounts").mkdir()
    rows = [
        _make_row(health="ACTIVE", name="proj-active"),
        _make_row(health="SOON", name="proj-soon", relative_path="acme/projects/proj-soon.md"),
    ]
    runner = CliRunner()
    with (
        patch(f"{MODULE}.get_fieldkit_home", return_value=str(tmp_path)),
        patch(f"{MODULE}.health_check", return_value=rows),
    ):
        result = runner.invoke(cli, ["--json", "--strict"])

    assert result.exit_code == 0
    parsed = json.loads(result.output)
    expected = [dataclasses.asdict(r) for r in rows]
    assert parsed == expected


# ---------------------------------------------------------------------------
# 5. --json with a ZOMBIE row -> exit 1
# ---------------------------------------------------------------------------


def test_cli_json_with_zombie_row_exits_0_by_default(tmp_path: Path) -> None:
    (tmp_path / "accounts").mkdir()
    rows = [_make_row(health="ZOMBIE", name="proj-zombie", days_until_end=-10)]
    runner = CliRunner()
    with (
        patch(f"{MODULE}.get_fieldkit_home", return_value=str(tmp_path)),
        patch(f"{MODULE}.health_check", return_value=rows),
    ):
        result = runner.invoke(cli, ["--json"])

    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed == [dataclasses.asdict(r) for r in rows]


def test_cli_human_with_zombie_row_exits_0_by_default(tmp_path: Path) -> None:
    (tmp_path / "accounts").mkdir()
    rows = [_make_row(health="ZOMBIE", name="proj-zombie", days_until_end=-10)]
    with (
        patch(f"{MODULE}.get_fieldkit_home", return_value=str(tmp_path)),
        patch(f"{MODULE}.health_check", return_value=rows),
    ):
        result = CliRunner().invoke(cli, [])

    assert result.exit_code == 0
    assert "proj-zombie" in result.output


# ---------------------------------------------------------------------------
# 6. --json with an UNKNOWN row (no ZOMBIE) -> exit 1
# ---------------------------------------------------------------------------


def test_cli_json_with_unknown_row_and_no_zombie_exits_0_by_default(tmp_path: Path) -> None:
    (tmp_path / "accounts").mkdir()
    rows = [_make_row(health="UNKNOWN", name="proj-unknown", days_until_end=None, contract_end_str="")]
    runner = CliRunner()
    with (
        patch(f"{MODULE}.get_fieldkit_home", return_value=str(tmp_path)),
        patch(f"{MODULE}.health_check", return_value=rows),
    ):
        result = runner.invoke(cli, ["--json"])

    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed == [dataclasses.asdict(r) for r in rows]


@pytest.mark.parametrize("health", ["ZOMBIE", "UNKNOWN"])
@pytest.mark.parametrize("as_json", [False, True])
def test_cli_strict_attention_rows_exit_1_with_complete_report(tmp_path: Path, health: str, as_json: bool) -> None:
    (tmp_path / "accounts").mkdir()
    rows = [_make_row(health=health, name="attention-project")]
    args = ["--strict", *(["--json"] if as_json else [])]
    with (
        patch(f"{MODULE}.get_fieldkit_home", return_value=str(tmp_path)),
        patch(f"{MODULE}.health_check", return_value=rows),
    ):
        result = CliRunner().invoke(cli, args)

    assert result.exit_code == 1
    assert "attention-project" in result.output


# ---------------------------------------------------------------------------
# 7. --account filter threading
# ---------------------------------------------------------------------------


def test_cli_account_option_threaded_to_health_check(tmp_path: Path) -> None:
    (tmp_path / "accounts").mkdir()
    rows = [_make_row()]
    runner = CliRunner()
    with (
        patch(f"{MODULE}.get_fieldkit_home", return_value=str(tmp_path)),
        patch(f"{MODULE}.health_check", return_value=rows) as mock_health_check,
    ):
        result = runner.invoke(cli, ["--account", "acme", "--json"])

    assert result.exit_code == 0
    mock_health_check.assert_called_once()
    call = mock_health_check.call_args
    assert call.args[0] == tmp_path
    assert call.kwargs["account_filter"] == "acme"
    assert isinstance(call.kwargs["today"], date)


def test_cli_account_option_defaults_to_none(tmp_path: Path) -> None:
    (tmp_path / "accounts").mkdir()
    rows = [_make_row()]
    runner = CliRunner()
    with (
        patch(f"{MODULE}.get_fieldkit_home", return_value=str(tmp_path)),
        patch(f"{MODULE}.health_check", return_value=rows) as mock_health_check,
    ):
        result = runner.invoke(cli, ["--json"])

    assert result.exit_code == 0
    mock_health_check.assert_called_once()
    assert mock_health_check.call_args.kwargs["account_filter"] is None
