"""Smoke tests for fieldkit commands CLI (commands/commands/cli.py).

D1 Wave 5 (D4): `fieldkit commands --json` registry entry point.
"""

import json

import pytest
from click.testing import CliRunner

pytestmark = pytest.mark.unit


def _get_cli():
    from fieldkit.commands.commands.cli import cli

    return cli


def test_commands_cli_help_exits_zero() -> None:
    runner = CliRunner()
    result = runner.invoke(_get_cli(), ["--help"])
    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_commands_cli_table_default_exits_zero() -> None:
    """No flags: renders the human-readable table."""
    runner = CliRunner()
    result = runner.invoke(_get_cli(), [])
    assert result.exit_code == 0
    assert "fieldkit sf" in result.output


def test_commands_cli_json_flag_emits_valid_json_array() -> None:
    """--json emits a JSON array covering the required registry fields."""
    runner = CliRunner()
    result = runner.invoke(_get_cli(), ["--json"])
    assert result.exit_code == 0

    data = json.loads(result.output)
    assert isinstance(data, list)
    assert data, "registry JSON must not be empty"
    for entry in data:
        assert "full_name" in entry
        assert "summary" in entry
        assert "write_class" in entry
        assert entry["write_class"] in {"read-only", "workspace", "external"}
