"""Unit tests for fieldkit.commands.auth.shadowbot.

Moved from test_shadowbot_cli.py's "auth command tests" section when the
`shadowbot auth` command was relocated to `fieldkit auth shadowbot`.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.auth.shadowbot import auth_shadowbot_cmd
from fieldkit.shadowbot.auth import ShadowbotAuthError

pytestmark = pytest.mark.unit


def _token_file(tmp_path: Path, token: str) -> Path:
    """Create one owner-only refresh-token input file for command tests."""
    path = tmp_path / "refresh-token"
    path.write_text(token, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_auth_status_active() -> None:
    """auth (no flags) prints '✓ auth active' to stdout and exits 0."""
    runner = CliRunner()

    with patch("fieldkit.shadowbot.auth.get_token", return_value="valid-token"):
        result = runner.invoke(auth_shadowbot_cmd, [], catch_exceptions=False)

    assert result.exit_code == 0
    assert "✓ auth active" in result.output


def test_auth_json_status_is_credential_safe() -> None:
    runner = CliRunner()
    result_value = MagicMock(service="shadowbot", healthy=True, configured=True, message="token valid")
    with patch("fieldkit.commands.doctor.shadowbot.check_shadowbot", return_value=result_value):
        result = runner.invoke(auth_shadowbot_cmd, ["--json"], catch_exceptions=False)
    assert result.exit_code == 0
    assert json.loads(result.output) == {
        "authenticated": True,
        "configured": True,
        "service": "shadowbot",
        "state": "authenticated",
    }


def test_auth_status_expired() -> None:
    """auth (no flags) prints error to stderr and exits 2 when auth fails."""
    runner = CliRunner()

    with patch(
        "fieldkit.shadowbot.auth.get_token",
        side_effect=ShadowbotAuthError("session expired"),
    ):
        result = runner.invoke(auth_shadowbot_cmd, [], catch_exceptions=False)

    assert result.exit_code == 2
    assert "session expired" in result.stderr


def test_auth_expired_interactively_recovers_with_refresh_token() -> None:
    """An interactive operator receives guidance, supplies a token, and gets validation."""
    runner = CliRunner()

    with (
        patch("fieldkit.shadowbot.auth.get_token", side_effect=ShadowbotAuthError("session expired")),
        patch("fieldkit.commands.auth.shadowbot.stdin_is_interactive", return_value=True),
        patch("fieldkit.shadowbot.auth.inject_refresh_token") as inject,
    ):
        result = runner.invoke(auth_shadowbot_cmd, [], input="refresh-token-value\n", catch_exceptions=False)

    assert result.exit_code == 0
    assert "DevTools" in result.output
    assert "validated" in result.output.lower()
    inject.assert_called_once_with("refresh-token-value")


def test_auth_expired_noninteractive_gives_reauthorization_guidance() -> None:
    """Piped callers never receive a credential prompt or mutation."""
    runner = CliRunner()

    with (
        patch("fieldkit.shadowbot.auth.get_token", side_effect=ShadowbotAuthError("session expired")),
        patch("fieldkit.commands.auth.shadowbot.stdin_is_interactive", return_value=False),
        patch("fieldkit.shadowbot.auth.inject_refresh_token") as inject,
    ):
        result = runner.invoke(auth_shadowbot_cmd, [], catch_exceptions=False)

    assert result.exit_code == 2
    assert "fieldkit auth shadowbot" in result.stderr
    inject.assert_not_called()


def test_auth_refresh_token_success(tmp_path: Path) -> None:
    """auth --refresh-token TOKEN saves token and prints success path."""
    runner = CliRunner()

    with (
        patch("fieldkit.shadowbot.auth.inject_refresh_token") as mock_inject,
        patch(
            "fieldkit.shadowbot.auth.get_token_path",
            return_value=MagicMock(__str__=lambda s: "/fake/path/shadowbot-token.json"),
        ),
    ):
        result = runner.invoke(
            auth_shadowbot_cmd,
            ["--refresh-token-file", str(_token_file(tmp_path, "my-refresh-jwt"))],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert "✓ token saved to" in result.output
    mock_inject.assert_called_once_with("my-refresh-jwt")


def test_auth_refresh_token_invalid(tmp_path: Path) -> None:
    """auth --refresh-token TOKEN prints error to stderr and exits 2 on failure."""
    runner = CliRunner()

    with patch(
        "fieldkit.shadowbot.auth.inject_refresh_token",
        side_effect=ShadowbotAuthError("invalid_grant"),
    ):
        result = runner.invoke(
            auth_shadowbot_cmd,
            ["--refresh-token-file", str(_token_file(tmp_path, "bad-token"))],
            catch_exceptions=False,
        )

    assert result.exit_code == 2
    assert "invalid_grant" in result.stderr


def test_auth_refresh_token_file_requires_owner_only_permissions(tmp_path: Path) -> None:
    """The command reports unsafe credential input as a Click usage error."""
    token_file = tmp_path / "refresh-token"
    token_file.write_text("refresh-token-value", encoding="utf-8")
    token_file.chmod(0o644)

    result = CliRunner().invoke(auth_shadowbot_cmd, ["--refresh-token-file", str(token_file)])

    assert result.exit_code == 2
    assert "owner-only" in result.output
