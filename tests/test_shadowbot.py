"""Unit tests for fieldkit shadowbot CLI — unverified narration label (implementation note)."""

from unittest.mock import patch

import pytest
from click.testing import CliRunner, Result

from fieldkit.commands.shadowbot.cli import (
    UNVERIFIED_FOOTER,
    UNVERIFIED_HEADER,
    cli,
)
from fieldkit.shadowbot.client import ShadowbotResponse

pytestmark = pytest.mark.unit

_SYNTHETIC_RESPONSE = ShadowbotResponse(
    content="The opportunity stage is Propose.",
    _raw_events=[],
)


def _invoke_query(prompt: str = "What is the stage?") -> Result:
    runner = CliRunner()
    with (
        patch("fieldkit.commands.shadowbot.cli.auth.get_token", return_value="tok"),
        patch(
            "fieldkit.commands.shadowbot.cli.client_module.query",
            return_value=_SYNTHETIC_RESPONSE,
        ),
    ):
        result = runner.invoke(cli, ["query", prompt])
    return result


# ---------------------------------------------------------------------------
# Unverified label tests (implementation note)
# ---------------------------------------------------------------------------


def test_query_output_contains_unverified_header() -> None:
    """stdout must begin with the UNVERIFIED_HEADER disclaimer."""
    result = _invoke_query()
    assert result.exit_code == 0, result.output
    assert UNVERIFIED_HEADER in result.output


def test_query_output_contains_unverified_footer() -> None:
    """stdout must end with the UNVERIFIED_FOOTER disclaimer."""
    result = _invoke_query()
    assert result.exit_code == 0, result.output
    assert UNVERIFIED_FOOTER in result.output


def test_query_output_header_precedes_content() -> None:
    """UNVERIFIED_HEADER must appear before the response content."""
    result = _invoke_query()
    assert result.exit_code == 0, result.output
    header_pos = result.output.index(UNVERIFIED_HEADER)
    content_pos = result.output.index(_SYNTHETIC_RESPONSE.content)
    assert header_pos < content_pos, "Header must precede response content"


def test_query_output_footer_follows_content() -> None:
    """UNVERIFIED_FOOTER must appear after the response content."""
    result = _invoke_query()
    assert result.exit_code == 0, result.output
    content_pos = result.output.index(_SYNTHETIC_RESPONSE.content)
    footer_pos = result.output.index(UNVERIFIED_FOOTER)
    assert content_pos < footer_pos, "Footer must follow response content"


def test_query_output_response_content_present() -> None:
    """The actual response content must still appear in stdout."""
    result = _invoke_query()
    assert result.exit_code == 0, result.output
    assert _SYNTHETIC_RESPONSE.content in result.output
