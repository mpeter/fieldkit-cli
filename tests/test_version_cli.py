"""Smoke tests for fieldkit version CLI (commands/version/cli.py).

implementation note: new test file for the version command CLI layer, which had ~50%
coverage with no dedicated test file.
"""

import pytest
from click.testing import CliRunner

pytestmark = pytest.mark.unit


def _get_cli():
    from fieldkit.commands.version.cli import cli

    return cli


def test_version_cli_help_exits_zero() -> None:
    """fieldkit version --help exits 0 and shows help text."""
    runner = CliRunner()
    result = runner.invoke(_get_cli(), ["--help"])
    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_version_cli_runs_without_args() -> None:
    """fieldkit version with no args exits 0 and emits non-empty output."""
    runner = CliRunner()
    result = runner.invoke(_get_cli(), [])
    assert result.exit_code == 0
    assert result.output.strip()


def test_version_cli_features_flag() -> None:
    """fieldkit version --features exits 0 and lists feature entries."""
    runner = CliRunner()
    result = runner.invoke(_get_cli(), ["--features"])
    assert result.exit_code == 0
    assert result.output.strip()


def test_version_cli_json_flag() -> None:
    """fieldkit version --json exits 0 and emits output."""
    runner = CliRunner()
    result = runner.invoke(_get_cli(), ["--json"])
    assert result.exit_code == 0
    assert result.output.strip()
