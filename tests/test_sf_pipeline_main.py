"""Tests for fieldkit.commands.sf.__main__ — the sf_pipeline CLI entry point.

python -m fieldkit.commands.sf delegates to fieldkit.commands.sf.cli (the
Click group). Tests use Click's CliRunner so there is no sys.argv
monkeypatching needed.

Covers:
- No-args / help output
- Unknown subcommand error
- Dispatch: each leaf subcommand is registered and reachable
"""

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.sf.__main__ import main
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


def test_sf_subcommand_dispatch_reconcile_dispatches_to_leaf(tmp_path: pytest.TempPathFactory) -> None:
    """Invoke reconcile with a real (minimal) pursuit file — verifies dispatch works."""
    # Create a minimal pursuit file with frontmatter but no sf_ keys
    p = tmp_path / "pursuit.md"
    p.write_text("---\ntitle: Test\n---\n# Body\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(cli, ["reconcile", str(p)])
    # reconcile exits 0 (skip) when there are no sf_* keys
    assert result.exit_code == 0


def test_sf_subcommand_dispatch_frontmatter_subcommand_help_has_description() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["frontmatter", "--help"])
    assert result.exit_code == 0
    # Description from @click.command help string should appear
    assert len(result.output) > 20


# ── TestSfMainModule (flattened) ────────────────────────────────────────────


def test_sf_main_module_main_delegates_to_click_group() -> None:
    """__main__.main() calls cli.main and exits 0 on --help."""
    import fieldkit.commands.sf.__main__ as sf_main

    with patch.object(
        sf_main,
        "main",
        wraps=main,
    ):
        # We call it with --help to get a clean exit
        import sys

        with patch.object(sys, "argv", ["sf_pipeline", "--help"]):
            try:
                main()
            except SystemExit as exc:
                assert exc.code == 0


def test_sf_main_module_main_unknown_exits_nonzero() -> None:
    import sys

    with patch.object(sys, "argv", ["sf_pipeline", "totally_unknown_cmd"]):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code != 0
