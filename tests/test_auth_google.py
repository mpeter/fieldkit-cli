"""Unit tests for fieldkit.commands.auth.google."""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.auth.google import auth_google_cmd
from fieldkit.errors import GmailAuthError

pytestmark = pytest.mark.unit


def test_auth_google_success_exits_0() -> None:
    runner = CliRunner()

    with patch("fieldkit.commands.auth.google.get_gmail_service", return_value=MagicMock()):
        result = runner.invoke(auth_google_cmd, [], catch_exceptions=False)

    assert result.exit_code == 0
    assert "Google auth active" in result.output


def test_auth_google_error_exits_2() -> None:
    runner = CliRunner()

    with patch(
        "fieldkit.commands.auth.google.get_gmail_service",
        side_effect=GmailAuthError("no credentials configured"),
    ):
        result = runner.invoke(auth_google_cmd, [], catch_exceptions=False)

    assert result.exit_code == 2
    assert "no credentials configured" in result.output
