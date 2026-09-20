"""Tests for fieldkit.commands.sf.set_next_steps.cli — branch coverage."""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.sf.set_next_steps import cli

pytestmark = pytest.mark.unit

_OPP_ID = "006Pe000012n2GkIAI"


def _make_client(name: str = "ACME Deal", current_value: str = "Schedule demo") -> MagicMock:
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.fetch_record.return_value = {
        "Id": _OPP_ID,
        "Name": name,
        "Next_Steps__c": current_value,
    }
    return client


# ── TestSetNextStepsCli (flattened) ─────────────────────────────────────────


def _set_next_steps_cli_invoke(args: list[str], client: MagicMock | None = None, **patch_kwargs: object) -> object:
    runner = CliRunner()
    sid = patch_kwargs.pop("sid", "test-sid")
    base_url = patch_kwargs.pop("base_url", "https://examplecrm.my.salesforce.com")
    _client = client or _make_client()

    with (
        patch("fieldkit.commands.sf.set_next_steps.get_sf_session_id", return_value=sid),
        patch("fieldkit.commands.sf.set_next_steps.get_sf_rest_base_url", return_value=base_url),
        patch("fieldkit.commands.sf.set_next_steps.SFDirectClient", return_value=_client),
    ):
        return runner.invoke(cli, args, catch_exceptions=False, **patch_kwargs)


def test_set_next_steps_cli_empty_text_raises_usage_error() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, [_OPP_ID, "   "], catch_exceptions=False)
    assert result.exit_code != 0


def test_set_next_steps_cli_no_text_arg_raises() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, [_OPP_ID], catch_exceptions=False)
    assert result.exit_code != 0


def test_set_next_steps_cli_no_sid_exits_2() -> None:
    runner = CliRunner()
    with patch("fieldkit.commands.sf.set_next_steps.get_sf_session_id", return_value=None):
        result = runner.invoke(cli, [_OPP_ID, "new text"], catch_exceptions=False)
    assert result.exit_code == 2


def test_set_next_steps_cli_preview_shows_current_and_new() -> None:
    result = _set_next_steps_cli_invoke([_OPP_ID, "New next steps text"])
    assert result.exit_code == 0
    assert "Schedule demo" in result.output or "preview" in result.output.lower()
    assert "New next steps text" in result.output


def test_set_next_steps_cli_preview_does_not_call_update() -> None:
    client = _make_client()
    _set_next_steps_cli_invoke([_OPP_ID, "Preview text"], client=client)
    client.update_opportunity_fields.assert_not_called()


def test_set_next_steps_cli_preview_prints_confirm_hint() -> None:
    result = _set_next_steps_cli_invoke([_OPP_ID, "Some text"])
    assert "--confirm" in result.output or "preview" in result.output.lower()


def test_set_next_steps_cli_empty_current_shows_empty_label() -> None:
    client = _make_client(current_value="")
    result = _set_next_steps_cli_invoke([_OPP_ID, "New text"], client=client)
    assert result.exit_code == 0
    assert "(empty)" in result.output


def test_set_next_steps_cli_not_found_exits_1() -> None:
    from fieldkit.sf.client import SFNotFoundError

    client = _make_client()
    client.fetch_record.side_effect = SFNotFoundError("not found")
    result = _set_next_steps_cli_invoke([_OPP_ID, "Text"], client=client)
    assert result.exit_code == 1
    assert "not found" in result.output.lower() or _OPP_ID in result.output


def test_set_next_steps_cli_auth_error_on_fetch_exits_2() -> None:
    """SFAuthError on fetch propagates; backstop in __main__.py maps → 2.

    After historic regression migration, SFAuthError re-raises instead of sys.exit(2).
    CliRunner catch_exceptions=False (via _set_next_steps_cli_invoke) lets it propagate.
    """
    from fieldkit.sf.client import SFAuthError

    client = _make_client()
    client.fetch_record.side_effect = SFAuthError("session expired")
    with pytest.raises(SFAuthError, match="session expired"):
        _set_next_steps_cli_invoke([_OPP_ID, "Text"], client=client)


def test_set_next_steps_cli_api_error_on_fetch_exits_1() -> None:
    from fieldkit.sf.client import SFAPIError

    client = _make_client()
    client.fetch_record.side_effect = SFAPIError("500 server error")
    result = _set_next_steps_cli_invoke([_OPP_ID, "Text"], client=client)
    assert result.exit_code == 1


def test_set_next_steps_cli_confirm_yes_calls_update() -> None:
    """In TTY mode, typing 'yes' at the prompt triggers the write."""
    client = _make_client()
    runner = CliRunner()
    sid = "test-sid"
    base_url = "https://examplecrm.my.salesforce.com"

    with (
        patch("fieldkit.commands.sf.set_next_steps.get_sf_session_id", return_value=sid),
        patch("fieldkit.commands.sf.set_next_steps.get_sf_rest_base_url", return_value=base_url),
        patch("fieldkit.commands.sf.set_next_steps.SFDirectClient", return_value=client),
        patch("fieldkit.commands.sf.set_next_steps.stdin_is_interactive", return_value=True),
    ):
        result = runner.invoke(
            cli,
            [_OPP_ID, "--confirm", "New text"],
            input="yes\n",
            catch_exceptions=False,
        )
    assert result.exit_code == 0
    client.update_opportunity_fields.assert_called_once()
    call_args = client.update_opportunity_fields.call_args
    assert call_args[0][0] == _OPP_ID
    assert "Next_Steps__c" in call_args[0][1]


def test_set_next_steps_cli_confirm_no_aborts() -> None:
    """In TTY mode, typing 'no' at the prompt aborts the write."""
    client = _make_client()
    runner = CliRunner()

    with (
        patch("fieldkit.commands.sf.set_next_steps.get_sf_session_id", return_value="sid"),
        patch("fieldkit.commands.sf.set_next_steps.get_sf_rest_base_url", return_value="https://sf.com"),
        patch("fieldkit.commands.sf.set_next_steps.SFDirectClient", return_value=client),
        patch("fieldkit.commands.sf.set_next_steps.stdin_is_interactive", return_value=True),
    ):
        result = runner.invoke(
            cli,
            [_OPP_ID, "--confirm", "New text"],
            input="no\n",
            catch_exceptions=False,
        )
    assert result.exit_code == 0
    client.update_opportunity_fields.assert_not_called()
    assert "Aborted" in result.output


def test_set_next_steps_cli_confirm_non_tty_writes_without_prompt() -> None:
    """In non-TTY mode, --confirm writes without any interactive prompt."""
    client = _make_client()
    runner = CliRunner()

    with (
        patch("fieldkit.commands.sf.set_next_steps.get_sf_session_id", return_value="sid"),
        patch("fieldkit.commands.sf.set_next_steps.get_sf_rest_base_url", return_value="https://sf.com"),
        patch("fieldkit.commands.sf.set_next_steps.SFDirectClient", return_value=client),
        patch("fieldkit.commands.sf.set_next_steps.stdin_is_interactive", return_value=False),
    ):
        result = runner.invoke(
            cli,
            [_OPP_ID, "--confirm", "New text"],
            catch_exceptions=False,
        )
    assert result.exit_code == 0
    client.update_opportunity_fields.assert_called_once()
    assert "Type" not in result.output


def test_set_next_steps_cli_auth_error_on_update_exits_2() -> None:
    """SFAuthError on update propagates; backstop in __main__.py maps → 2.

    After historic regression migration, SFAuthError re-raises instead of sys.exit(2).
    CliRunner catch_exceptions=False lets it propagate.
    """
    from fieldkit.sf.client import SFAuthError

    client = _make_client()
    client.update_opportunity_fields.side_effect = SFAuthError("token expired")
    runner = CliRunner()

    with (
        patch("fieldkit.commands.sf.set_next_steps.get_sf_session_id", return_value="sid"),
        patch("fieldkit.commands.sf.set_next_steps.get_sf_rest_base_url", return_value="https://sf.com"),
        patch("fieldkit.commands.sf.set_next_steps.SFDirectClient", return_value=client),
        patch("fieldkit.commands.sf.set_next_steps.stdin_is_interactive", return_value=False),
        pytest.raises(SFAuthError, match="token expired"),
    ):
        runner.invoke(
            cli,
            [_OPP_ID, "--confirm", "New text"],
            catch_exceptions=False,
        )


def test_set_next_steps_cli_api_error_on_update_exits_1() -> None:
    from fieldkit.sf.client import SFAPIError

    client = _make_client()
    client.update_opportunity_fields.side_effect = SFAPIError("422 unprocessable")
    runner = CliRunner()

    with (
        patch("fieldkit.commands.sf.set_next_steps.get_sf_session_id", return_value="sid"),
        patch("fieldkit.commands.sf.set_next_steps.get_sf_rest_base_url", return_value="https://sf.com"),
        patch("fieldkit.commands.sf.set_next_steps.SFDirectClient", return_value=client),
        patch("fieldkit.commands.sf.set_next_steps.stdin_is_interactive", return_value=False),
    ):
        result = runner.invoke(
            cli,
            [_OPP_ID, "--confirm", "New text"],
            catch_exceptions=False,
        )
    assert result.exit_code == 1


def test_set_next_steps_cli_multi_word_text_joined() -> None:
    client = _make_client()
    result = _set_next_steps_cli_invoke([_OPP_ID, "Follow", "up", "with", "CTO"], client=client)
    assert result.exit_code == 0
    assert "Follow up with CTO" in result.output
