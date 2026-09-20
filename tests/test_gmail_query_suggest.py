"""Tests for _SuggestingGroup — historic regression actionable error on unknown query subcommand."""

import pytest
from click.testing import CliRunner

from fieldkit.commands.gmail.query import cli

pytestmark = pytest.mark.unit


def test_unknown_subcommand_prints_suggestion() -> None:
    """Running a multi-word query should print a suggestion and exit non-zero."""
    runner = CliRunner()
    result = runner.invoke(cli, ["Example Vendor"], catch_exceptions=False)
    assert result.exit_code != 0
    assert "fieldkit gmail query threads" in result.output


def test_unknown_subcommand_exit_nonzero() -> None:
    """Unknown subcommand must exit with a non-zero exit code."""
    runner = CliRunner()
    result = runner.invoke(cli, ["SomeUnknownTerm"], catch_exceptions=False)
    assert result.exit_code != 0


def test_threads_help_still_works() -> None:
    """The 'threads' subcommand --help must still exit 0."""
    runner = CliRunner()
    result = runner.invoke(cli, ["threads", "--help"])
    assert result.exit_code == 0
    assert "keyword" in result.output.lower() or "KEYWORD" in result.output
