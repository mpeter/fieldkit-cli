"""The first-run command exposes only current initialization workflows."""

import importlib.util

import pytest
from click.testing import CliRunner

from fieldkit.commands.init.cli import cli

pytestmark = pytest.mark.unit


def test_init_help_has_no_obsolete_migration_command() -> None:
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "migrate" not in result.output


@pytest.mark.parametrize("module", ["fieldkit.commands.init.migrate", "fieldkit.config.migrate"])
def test_obsolete_migration_modules_are_removed(module: str) -> None:
    assert importlib.util.find_spec(module) is None
