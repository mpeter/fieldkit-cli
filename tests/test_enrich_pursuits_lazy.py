"""Enrichment help must not read account configuration."""

from unittest.mock import patch

import pytest
from click.testing import CliRunner

pytestmark = pytest.mark.unit


def test_help_does_not_read_accounts_configuration() -> None:
    from fieldkit.commands.gmail import enrich_pursuits

    with patch.object(enrich_pursuits, "get_accounts_config", side_effect=AssertionError("unexpected read")) as read:
        result = CliRunner().invoke(enrich_pursuits.cli, ["--help"])
    assert result.exit_code == 0
    assert "Write gmail-intel.md files per configured account." in result.output
    read.assert_not_called()
