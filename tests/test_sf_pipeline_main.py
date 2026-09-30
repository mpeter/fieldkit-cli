"""Tests for Salesforce dispatch through the canonical fieldkit entry point.

Covers:
- No-args / help output
- Unknown subcommand error
- Dispatch: each leaf subcommand is registered and reachable
"""

from pathlib import Path

import pytest
from click.testing import CliRunner

from fieldkit.__main__ import main
from fieldkit.commands.sf.cli import cli

pytestmark = pytest.mark.unit


# ── TestSfGroupHelp (flattened) ─────────────────────────────────────────────


def test_sf_group_help_no_args_shows_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, [])
    # Click groups with no standalone_mode=False exit 0 and show help
    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_sf_group_help_help_flag_exits_0() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_sf_group_help_help_lists_all_subcommands() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    for cmd in ("frontmatter", "listview", "reconcile", "sync"):
        assert cmd in result.output


def test_sf_group_help_help_flag_alias() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["-h"])
    assert result.exit_code == 0
    assert "Usage:" in result.output


# ── TestSfGroupUnknownSubcommand (flattened) ────────────────────────────────


def test_sf_group_unknown_subcommand_unknown_subcommand_exits_nonzero() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["nonexistent"])
    assert result.exit_code != 0


def test_sf_group_unknown_subcommand_unknown_subcommand_error_message() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["bogus"])
    # Click reports "No such command" for unknown subcommands
    assert "bogus" in result.output or "No such command" in result.output


# ── TestSfSubcommandDispatch (flattened) ────────────────────────────────────


def test_sf_subcommand_dispatch_reconcile_subcommand_registered() -> None:
    """reconcile is a registered Click command — --help works."""
    runner = CliRunner()
    result = runner.invoke(cli, ["reconcile", "--help"])
    assert result.exit_code == 0
    assert "reconcile" in result.output.lower() or "pursuit" in result.output.lower()


def test_sf_subcommand_dispatch_frontmatter_subcommand_registered() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["frontmatter", "--help"])
    assert result.exit_code == 0


def test_sf_subcommand_dispatch_listview_subcommand_registered() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["listview", "--help"])
    assert result.exit_code == 0


def test_sf_subcommand_dispatch_reconcile_rejects_invalid_pursuit_without_writing(tmp_path: Path) -> None:
    p = tmp_path / "pursuit.md"
    original = "---\ntitle: Test\n---\n# Body\n"
    p.write_text(original, encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(cli, ["reconcile", str(p)])
    assert result.exit_code == 3
    assert p.read_text(encoding="utf-8") == original


def test_sf_subcommand_dispatch_frontmatter_subcommand_help_has_description() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["frontmatter", "--help"])
    assert result.exit_code == 0
    # Description from @click.command help string should appear
    assert len(result.output) > 20


# ── TestSfMainModule (flattened) ────────────────────────────────────────────


def test_sf_main_module_main_delegates_to_click_group(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fieldkit.__main__.load_dotenv_safe", lambda: None)

    result = main(["sf", "--help"])

    assert result == 0


def test_sf_main_module_main_unknown_exits_data_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fieldkit.__main__.load_dotenv_safe", lambda: None)

    result = main(["sf", "totally_unknown_cmd"])

    assert result == 3
