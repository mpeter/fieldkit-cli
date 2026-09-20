"""Unit tests for fieldkit.commands.shadowbot.cli.

All tests are @pytest.mark.unit and use click.testing.CliRunner for isolation.
No real network calls, auth, or filesystem access are made.
"""

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.shadowbot.cli import cli
from fieldkit.config import TIMEOUT_SHADOWBOT_QUERY
from fieldkit.shadowbot.auth import ShadowbotAuthError
from fieldkit.shadowbot.client import ShadowbotQueryError, ShadowbotResponse

# NOTE: `shadowbot auth` moved to `fieldkit auth shadowbot` — see test_auth_shadowbot.py.


@pytest.mark.unit
def test_cli_bare_invocation_prints_help_and_exits_1() -> None:
    """`fieldkit shadowbot` with no subcommand prints help and exits 1."""
    result = CliRunner().invoke(cli, [], catch_exceptions=False)

    assert result.exit_code == 1
    assert "query" in result.output


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(content: str = "AI response text", thread_id: str = "t1") -> ShadowbotResponse:
    """Build a minimal ShadowbotResponse for use in mocks."""
    return ShadowbotResponse(content=content, thread_id=thread_id)


# ---------------------------------------------------------------------------
# query command tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_query_no_prompt_no_stdin() -> None:
    """query with no args and no stdin (TTY) prints error and exits 1."""
    runner = CliRunner()
    result = runner.invoke(cli, ["query"], catch_exceptions=False)

    assert result.exit_code == 1
    assert "prompt required" in result.output or "prompt required" in result.stderr


@pytest.mark.unit
def test_query_stdin_prompt() -> None:
    """query reads prompt from stdin when stdin is not a TTY."""
    runner = CliRunner()

    mock_response = _make_response("Answer from stdin prompt")

    with (
        patch("fieldkit.shadowbot.auth.get_token", return_value="tok"),
        patch("fieldkit.commands.shadowbot.cli.client_module.query", return_value=mock_response) as mock_query,
    ):
        result = runner.invoke(
            cli,
            ["query"],
            input="What is my pipeline?\n",
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert "Answer from stdin prompt" in result.output
    mock_query.assert_called_once()
    call_args = mock_query.call_args
    assert "What is my pipeline?" in call_args.args[0]


@pytest.mark.unit
def test_query_uses_shared_timeout_by_default() -> None:
    """query forwards the shared default timeout when --timeout is omitted."""
    mock_response = _make_response()

    with (
        patch("fieldkit.shadowbot.auth.get_token", return_value="tok"),
        patch("fieldkit.commands.shadowbot.cli.client_module.query", return_value=mock_response) as mock_query,
    ):
        result = CliRunner().invoke(cli, ["query", "hello"], catch_exceptions=False)

    assert result.exit_code == 0
    assert mock_query.call_args.kwargs["timeout"] == TIMEOUT_SHADOWBOT_QUERY


@pytest.mark.unit
def test_query_args_over_stdin() -> None:
    """query uses positional args as prompt, ignoring stdin."""
    runner = CliRunner()

    mock_response = _make_response("Answer from args")

    with (
        patch("fieldkit.shadowbot.auth.get_token", return_value="tok"),
        patch("fieldkit.commands.shadowbot.cli.client_module.query", return_value=mock_response) as mock_query,
    ):
        result = runner.invoke(
            cli,
            ["query", "hello", "world"],
            input="stdin content that should be ignored\n",
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert "Answer from args" in result.output
    call_args = mock_query.call_args
    assert "hello world" in call_args.args[0]


@pytest.mark.unit
def test_query_auth_error() -> None:
    """query prints auth error to stderr and exits 2 on ShadowbotAuthError."""
    runner = CliRunner()

    with patch(
        "fieldkit.shadowbot.auth.get_token",
        side_effect=ShadowbotAuthError("token expired"),
    ):
        result = runner.invoke(
            cli,
            ["query", "hello"],
            catch_exceptions=False,
        )

    assert result.exit_code == 2
    assert "token expired" in result.stderr


@pytest.mark.unit
def test_query_query_error() -> None:
    """query prints query error to stderr and exits 1 on ShadowbotQueryError."""
    runner = CliRunner()

    with (
        patch("fieldkit.shadowbot.auth.get_token", return_value="tok"),
        patch(
            "fieldkit.commands.shadowbot.cli.client_module.query",
            side_effect=ShadowbotQueryError("stream failed"),
        ),
    ):
        result = runner.invoke(
            cli,
            ["query", "hello"],
            catch_exceptions=False,
        )

    assert result.exit_code == 1
    assert "stream failed" in result.stderr


@pytest.mark.unit
@pytest.mark.parametrize("message", ["Failed to open ShadowBot state directory", "Failed to save ShadowBot state file"])
def test_shadowbot_cli_maps_state_selection_and_write_failures_to_exit_1(message: str) -> None:
    with (
        patch("fieldkit.shadowbot.auth.get_token", return_value="tok"),
        patch(
            "fieldkit.commands.shadowbot.cli.client_module.query",
            side_effect=ShadowbotQueryError(message),
        ),
    ):
        result = CliRunner().invoke(cli, ["query", "hello"], catch_exceptions=False)

    assert result.exit_code == 1
    assert f"Query failed: {message}" in result.stderr


@pytest.mark.unit
def test_query_success_content_on_stdout() -> None:
    """query prints response.content to stdout on success."""
    runner = CliRunner()

    mock_response = _make_response("The answer is 42")

    with (
        patch("fieldkit.shadowbot.auth.get_token", return_value="tok"),
        patch("fieldkit.commands.shadowbot.cli.client_module.query", return_value=mock_response),
    ):
        result = runner.invoke(
            cli,
            ["query", "what is the answer?"],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert "The answer is 42" in result.output


@pytest.mark.unit
def test_query_new_flag_passed_to_client() -> None:
    """query --new passes new_thread=True to client.query."""
    runner = CliRunner()

    mock_response = _make_response("Fresh thread response")

    with (
        patch("fieldkit.shadowbot.auth.get_token", return_value="tok"),
        patch("fieldkit.commands.shadowbot.cli.client_module.query", return_value=mock_response) as mock_query,
    ):
        result = runner.invoke(
            cli,
            ["query", "--new", "hello"],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    call_kwargs = mock_query.call_args.kwargs
    assert call_kwargs.get("new_thread") is True


@pytest.mark.unit
@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf"])
def test_query_rejects_non_finite_or_non_positive_timeout(timeout: str) -> None:
    result = CliRunner().invoke(cli, ["query", "--timeout", timeout, "hello"])

    assert result.exit_code == 2
    assert "must be a finite positive number of seconds" in result.output
