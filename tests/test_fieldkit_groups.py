"""Tests for fieldkit CLI group dispatch:
- fieldkit.commands.sf.cli  → sf subcommands
- fieldkit.commands.pipeline.main → pipeline command
- fieldkit.commands.brief.main   → brief command
- fieldkit.commands.gmail.cli      → gmail subcommands

Tests verify option propagation, return code propagation, and SystemExit handling.
"""

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.brief.cli import cli as brief_cli
from fieldkit.commands.gmail.cli import cli as gmail_cli
from fieldkit.commands.pipeline.cli import cli as pipeline_cli
from fieldkit.commands.sf.cli import cli as sf_cli

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# tools/fieldkit/sf.py
# ---------------------------------------------------------------------------


# ── TestSfGroup (flattened) ─────────────────────────────────────────────────


def test_sf_group_no_args_gives_0() -> None:
    """Invoking sf cli with no args shows help and returns 0."""
    runner = CliRunner()
    result = runner.invoke(sf_cli, [])
    assert result.exit_code == 0


def test_sf_group_help_gives_0() -> None:
    """sf --help returns 0."""
    runner = CliRunner()
    result = runner.invoke(sf_cli, ["--help"])
    assert result.exit_code == 0


def test_sf_group_all_subcommands_listed() -> None:
    """All sf subcommands appear in help output."""
    runner = CliRunner()
    result = runner.invoke(sf_cli, ["--help"])
    for subcmd in ("account", "frontmatter", "listview", "opportunity", "reconcile"):
        assert subcmd in result.output, f"'{subcmd}' missing from sf --help"
    assert "ingest" not in result.output, "'ingest' should not appear in sf --help"


def test_sf_group_systemexit_none_gives_0() -> None:
    """Invoking sf cli with no args returns 0 (same as no_args)."""
    runner = CliRunner()
    result = runner.invoke(sf_cli, [])
    assert result.exit_code == 0


def test_sf_group_unknown_subcommand_gives_nonzero() -> None:
    """An unknown subcommand returns a non-zero exit code."""
    runner = CliRunner()
    result = runner.invoke(sf_cli, ["bogus-subcmd"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# tools/fieldkit/pipeline.py
# ---------------------------------------------------------------------------


# ── TestPipelineGroup (flattened) ───────────────────────────────────────────


def test_collect_pipeline_pulse_normal_return_gives_0() -> None:
    runner = CliRunner()
    with patch("fieldkit.commands.pipeline.main._run") as mock_run:
        mock_run.return_value = None
        result = runner.invoke(pipeline_cli, [])
    assert result.exit_code == 0


def test_collect_pipeline_pulse_systemexit_propagated() -> None:
    runner = CliRunner()
    with patch("fieldkit.commands.pipeline.main._run") as mock_run:
        mock_run.side_effect = SystemExit(2)
        result = runner.invoke(pipeline_cli, [])
    assert result.exit_code == 2


def test_collect_pipeline_pulse_argv_set_correctly() -> None:
    runner = CliRunner()
    with patch("fieldkit.commands.pipeline.main._run") as mock_run:
        mock_run.return_value = None
        result = runner.invoke(pipeline_cli, ["--no-llm"])
    assert result.exit_code == 0
    mock_run.assert_called_once()
    _, kwargs = mock_run.call_args
    assert kwargs.get("no_llm") is True


# ---------------------------------------------------------------------------
# tools/fieldkit/brief.py
# ---------------------------------------------------------------------------


# ── TestBriefGroup (flattened) ──────────────────────────────────────────────


def test_brief_group_bare_invocation_shows_help_and_exits_1() -> None:
    """Bare `fieldkit brief` (no subcommand) prints help and exits 1 (R25 — no alias)."""
    runner = CliRunner()
    result = runner.invoke(brief_cli, [])
    assert result.exit_code == 1
    assert "Usage:" in result.output


def test_brief_group_normal_return_gives_0() -> None:
    runner = CliRunner()
    with patch("fieldkit.commands.brief.cli._run") as mock_run:
        mock_run.return_value = None
        result = runner.invoke(brief_cli, ["generate", "--pipeline-only"])
    assert result.exit_code == 0


def test_brief_group_systemexit_propagated() -> None:
    runner = CliRunner()
    with patch("fieldkit.commands.brief.cli._run") as mock_run:
        mock_run.side_effect = SystemExit(1)
        result = runner.invoke(brief_cli, ["generate", "--pipeline-only"])
    assert result.exit_code == 1


def test_brief_group_argv_set_correctly() -> None:
    runner = CliRunner()
    with (
        patch("fieldkit.commands.brief.cli._run") as mock_run,
        patch("fieldkit.config.get_account_names", return_value=["TestCorp"]),
    ):
        mock_run.return_value = None
        result = runner.invoke(brief_cli, ["generate", "--pipeline-only", "--account", "TestCorp"])
    assert result.exit_code == 0
    mock_run.assert_called_once()
    _, kwargs = mock_run.call_args
    assert kwargs.get("account") == "TestCorp"


# ---------------------------------------------------------------------------
# tools/fieldkit/gmail.py
# ---------------------------------------------------------------------------


# ── TestGmailGroup (flattened) ──────────────────────────────────────────────


def test_gmail_db_exists_no_args_returns_0() -> None:
    """gmail cli with no args shows help and exits 0 (Click group default)."""
    runner = CliRunner()
    result = runner.invoke(gmail_cli, [])
    assert result.exit_code == 0


def test_gmail_db_exists_help_flag_returns_0() -> None:
    """gmail --help exits 0 and lists Commands:."""
    runner = CliRunner()
    result = runner.invoke(gmail_cli, ["--help"])
    assert result.exit_code == 0
    assert "Commands:" in result.output


def test_gmail_db_exists_h_flag_returns_0() -> None:
    """gmail -h exits 0 (help_option_names includes -h)."""
    runner = CliRunner()
    result = runner.invoke(gmail_cli, ["-h"])
    assert result.exit_code == 0


def test_gmail_db_exists_unknown_subcommand_returns_nonzero() -> None:
    """An unknown subcommand returns a non-zero exit code."""
    runner = CliRunner()
    result = runner.invoke(gmail_cli, ["nonexistent"])
    assert result.exit_code != 0
    assert "nonexistent" in result.output


def test_gmail_db_exists_help_lists_all_subcommands() -> None:
    """All subcommand names appear in gmail --help output."""
    runner = CliRunner()
    result = runner.invoke(gmail_cli, ["--help"])
    for subcmd in ("sync", "query", "account-tags", "enrich-pursuits", "decay"):
        assert subcmd in result.output, f"'{subcmd}' missing from gmail --help"


def test_gmail_db_exists_dispatch_sync_smoke() -> None:
    """sync subcommand is reachable and its --help exits 0."""
    runner = CliRunner()
    result = runner.invoke(gmail_cli, ["sync", "--help"])
    assert result.exit_code == 0


def test_gmail_db_exists_dispatch_query_smoke() -> None:
    """query subcommand is reachable via CliRunner."""
    runner = CliRunner()
    result = runner.invoke(gmail_cli, ["query", "--help"])
    assert result.exit_code == 0


def test_gmail_db_exists_dispatch_returns_nonzero_on_unknown() -> None:
    """An unknown subcommand returns non-zero — baseline error propagation."""
    runner = CliRunner()
    result = runner.invoke(gmail_cli, ["no-such-cmd"])
    assert result.exit_code != 0


@pytest.mark.parametrize(
    "subcmd",
    [
        "account-tags",
        # apply-intel removed — was a no-op stub; use enrich-pursuits
        "backstory-gap",
        "decay",
        "enrich-pursuits",
        "query",
        "sync",
    ],
)
def test_gmail_db_exists_all_subcommands_registered(subcmd: str) -> None:
    """Every subcommand is registered: invoking <subcmd> --help exits 0."""
    runner = CliRunner()
    result = runner.invoke(gmail_cli, [subcmd, "--help"])
    assert result.exit_code == 0, f"'{subcmd} --help' exited {result.exit_code}: {result.output}"


# ---------------------------------------------------------------------------
# 4E.2 get_command / list_commands for gmail, pursuit, sf CLIs
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_gmail_get_command_known() -> None:
    """gmail cli returns a click BaseCommand for a known subcommand."""
    import click

    ctx = gmail_cli.make_context("gmail", [])
    result = gmail_cli.get_command(ctx, "query")
    ctx.close()
    assert result is not None
    assert isinstance(result, click.Command)


@pytest.mark.unit
def test_gmail_list_commands_sorted() -> None:
    """gmail cli list_commands returns a sorted list."""
    ctx = gmail_cli.make_context("gmail", [])
    result = gmail_cli.list_commands(ctx)
    ctx.close()
    assert isinstance(result, list)
    assert len(result) > 0
    assert result == sorted(result)


@pytest.mark.unit
def test_sf_get_command_known() -> None:
    """sf cli returns a click BaseCommand for a known subcommand."""
    import click

    ctx = sf_cli.make_context("sf", [])
    result = sf_cli.get_command(ctx, "account")
    ctx.close()
    assert result is not None
    assert isinstance(result, click.Command)


@pytest.mark.unit
def test_sf_list_commands_sorted() -> None:
    """sf cli list_commands returns a sorted list."""
    ctx = sf_cli.make_context("sf", [])
    result = sf_cli.list_commands(ctx)
    ctx.close()
    assert isinstance(result, list)
    assert result == sorted(result)
