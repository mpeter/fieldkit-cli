"""Offline contract checks for the public ShadowBot authentication guide."""

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.auth.shadowbot import auth_shadowbot_cmd
from fieldkit.shadowbot.auth import ShadowbotAuthError

pytestmark = pytest.mark.unit

GUIDE = Path("docs/guides/shadowbot-auth.md")


def test_guide_requires_administrator_configuration_and_manual_token_input() -> None:
    page = GUIDE.read_text(encoding="utf-8")

    assert "fieldkit does not provide working service\nendpoints" in page
    assert "../reference/config-file.md#organization-provided-assistant" in page
    assert "The command does not launch a browser login." in page
    assert "Chrome recovery is\nnot a first-time login path" in page
    assert "uv tool install --force 'fieldkit-cli[chrome-auth]'" in page


def test_documented_headless_auth_failure_never_prompts_or_injects() -> None:
    page = GUIDE.read_text(encoding="utf-8")
    assert "A non-interactive invocation exits `2` with reauthorization\nguidance instead of prompting." in page

    with (
        patch("fieldkit.shadowbot.auth.get_token", side_effect=ShadowbotAuthError("expired")),
        patch("fieldkit.commands.auth.shadowbot.stdin_is_interactive", return_value=False),
        patch("fieldkit.commands.auth.shadowbot._prompt_for_refresh_token") as prompt,
        patch("fieldkit.shadowbot.auth.inject_refresh_token") as inject,
    ):
        result = CliRunner().invoke(auth_shadowbot_cmd, [])

    assert result.exit_code == 2
    assert "--refresh-token-file PATH" in result.output
    prompt.assert_not_called()
    inject.assert_not_called()


def test_documented_secret_file_rules_precede_token_exchange(tmp_path: Path) -> None:
    page = GUIDE.read_text(encoding="utf-8")
    assert "at most 8,192 bytes" in page
    assert "rejects symlinks, pipes, directories, and\ngroup- or world-accessible secret files" in page

    token_file = tmp_path / "refresh-token"
    token_file.write_text("fictional-refresh-token", encoding="utf-8")
    token_file.chmod(0o644)
    with patch("fieldkit.shadowbot.auth.inject_refresh_token") as inject:
        result = CliRunner().invoke(auth_shadowbot_cmd, ["--refresh-token-file", str(token_file)])

    assert result.exit_code == 2
    assert "owner-only" in result.output
    inject.assert_not_called()
