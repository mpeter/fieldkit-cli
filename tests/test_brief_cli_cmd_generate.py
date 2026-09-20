"""Coverage for `cmd_generate`'s full-brief path (`pipeline_only=False`).

`fieldkit brief generate` (`commands/brief/cli.py`) has two paths:

- `--pipeline-only` delegates to `_run(...)` — covered elsewhere (see
  `tests/test_account_filter_plumbing.py`, `tests/test_fieldkit_groups.py`,
  `tests/test_fieldkit_main.py`, `tests/test_morning_brief_paths.py`,
  `tests/test_watch_hardening_016.py`).
- the default (full brief) path — verbose logging setup, the implementation note
  pre-flight fast-fail, `_run_generate(...)`, and the implementation note RuntimeError ->
  EXIT_DATA(3) mapping — is untested. This file covers that path only.

All tests invoke the `generate` subcommand through `click.testing.CliRunner`
and mock `preflight_check` (patched at its definition site,
`fieldkit.watch.preflight.preflight_check`, since `cmd_generate` imports it
locally) and `_run_generate` (patched at the module binding
`fieldkit.commands.brief.cli._run_generate`, imported at module level).
`--account` is never set (account slug validation is covered elsewhere), so
every test passes `account=None` through to `_run_generate`.
"""

import logging
from unittest.mock import MagicMock, _Call, patch

import pytest
from click.testing import CliRunner, Result

from fieldkit.commands.brief.cli import cli

pytestmark = pytest.mark.unit


def _invoke(*args: str) -> tuple[Result, list[_Call], list[_Call]]:
    """Invoke `brief generate` with preflight_check/_run_generate mocked.

    Returns (CliRunner result, preflight_check call_args_list, _run_generate call_args_list).
    """
    with (
        patch("fieldkit.watch.preflight.preflight_check") as mock_preflight,
        patch("fieldkit.commands.brief.cli._run_generate") as mock_run_generate,
    ):
        mock_preflight.return_value = []
        mock_run_generate.return_value = 0
        runner = CliRunner()
        result = runner.invoke(cli, ["generate", *args], catch_exceptions=True)
        # Snapshot call args before the patch context exits.
        preflight_calls = mock_preflight.call_args_list
        run_generate_calls = mock_run_generate.call_args_list
    return result, preflight_calls, run_generate_calls


@pytest.mark.unit
def test_verbose_sets_root_logger_debug() -> None:
    """--verbose sets the root logger to DEBUG via logging.getLogger().setLevel."""
    with (
        patch("fieldkit.watch.preflight.preflight_check", return_value=[]),
        patch("fieldkit.commands.brief.cli._run_generate", return_value=0),
        patch("logging.getLogger") as mock_get_logger,
    ):
        mock_logger = MagicMock()
        mock_get_logger.return_value = mock_logger
        runner = CliRunner()
        runner.invoke(cli, ["generate", "--verbose"], catch_exceptions=True)

    mock_get_logger.assert_called_once_with()
    mock_logger.setLevel.assert_called_once_with(logging.DEBUG)


@pytest.mark.unit
def test_no_verbose_does_not_touch_root_logger_level() -> None:
    """Without --verbose, logging.getLogger().setLevel is never called by cmd_generate."""
    with (
        patch("fieldkit.watch.preflight.preflight_check", return_value=[]),
        patch("fieldkit.commands.brief.cli._run_generate", return_value=0),
        patch("logging.getLogger") as mock_get_logger,
    ):
        mock_logger = MagicMock()
        mock_get_logger.return_value = mock_logger
        runner = CliRunner()
        runner.invoke(cli, ["generate"], catch_exceptions=True)

    mock_logger.setLevel.assert_not_called()


@pytest.mark.unit
def test_preflight_check_called_with_full_tool_list_and_dry_run_true() -> None:
    """preflight_check gets the exact tool list and dry_run=True with --dry-run."""
    result, preflight_calls, _ = _invoke("--dry-run")

    assert result.exit_code == 0, result.output
    assert len(preflight_calls) == 1
    args, kwargs = preflight_calls[0]
    assert args == (["sf", "gmail", "mcp", "llm"],)
    assert kwargs == {"dry_run": True}


@pytest.mark.unit
def test_preflight_check_called_with_dry_run_false_by_default() -> None:
    """preflight_check gets dry_run=False when --dry-run is not passed."""
    result, preflight_calls, _ = _invoke()

    assert result.exit_code == 0, result.output
    assert len(preflight_calls) == 1
    args, kwargs = preflight_calls[0]
    assert args == (["sf", "gmail", "mcp", "llm"],)
    assert kwargs == {"dry_run": False}


@pytest.mark.unit
def test_preflight_failures_echoed_with_prefix_and_exit_1_no_run_generate() -> None:
    """Non-empty preflight failures are echoed with the fixed prefix, in order, exit 1."""
    with (
        patch("fieldkit.watch.preflight.preflight_check") as mock_preflight,
        patch("fieldkit.commands.brief.cli._run_generate") as mock_run_generate,
    ):
        mock_preflight.return_value = ["sf: not authenticated", "gmail: token expired"]
        runner = CliRunner()
        result = runner.invoke(cli, ["generate"], catch_exceptions=True)
        run_generate_call_count = mock_run_generate.call_count

    assert result.exit_code == 1
    lines = [line for line in result.output.splitlines() if line.startswith("Pre-flight check failed:")]
    assert lines == [
        "Pre-flight check failed: sf: not authenticated",
        "Pre-flight check failed: gmail: token expired",
    ]
    assert run_generate_call_count == 0


@pytest.mark.unit
def test_empty_preflight_calls_run_generate_with_threaded_kwargs_exit_0() -> None:
    """Empty preflight failures proceed to _run_generate with all CLI options threaded through.

    --verbose is set but --dry-run is not, deliberately asymmetric, so a
    mutation that swaps the dry_run/verbose kwargs cannot pass by accident.
    """
    result, _, run_generate_calls = _invoke("--date", "2026-08-01", "--verbose")

    assert result.exit_code == 0, result.output
    assert len(run_generate_calls) == 1
    args, kwargs = run_generate_calls[0]
    assert args == ()
    assert kwargs == {
        "date_str": "2026-08-01",
        "dry_run": False,
        "verbose": True,
        "account": None,
        "as_json": False,
    }


@pytest.mark.unit
def test_json_is_threaded_to_full_generator() -> None:
    result, _, run_generate_calls = _invoke("--dry-run", "--json")

    assert result.exit_code == 0, result.output
    assert run_generate_calls[0].kwargs["as_json"] is True


def test_pipeline_only_dry_run_is_threaded_to_the_pipeline_generator() -> None:
    """The pipeline-only route must honor the public no-write dry-run contract."""
    with patch("fieldkit.commands.brief.cli._run") as run:
        runner = CliRunner()
        result = runner.invoke(cli, ["generate", "--pipeline-only", "--no-llm", "--dry-run"])

    assert result.exit_code == 0, result.output
    run.assert_called_once_with(no_llm=True, account=None, verbose=False, as_json=False, dry_run=True)


@pytest.mark.unit
def test_run_generate_return_value_becomes_exit_code_nonzero() -> None:
    """The command's exit code is exactly whatever _run_generate returns (not hardcoded)."""
    with (
        patch("fieldkit.watch.preflight.preflight_check", return_value=[]),
        patch("fieldkit.commands.brief.cli._run_generate", return_value=7),
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["generate"], catch_exceptions=True)

    assert result.exit_code == 7


@pytest.mark.unit
def test_run_generate_return_value_becomes_exit_code_zero() -> None:
    """A 0 return from _run_generate produces exit code 0 (not treated as falsy failure)."""
    with (
        patch("fieldkit.watch.preflight.preflight_check", return_value=[]),
        patch("fieldkit.commands.brief.cli._run_generate", return_value=0),
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["generate"], catch_exceptions=True)

    assert result.exit_code == 0


@pytest.mark.unit
def test_run_generate_runtime_error_maps_to_fatal_message_and_exit_3() -> None:
    """RuntimeError from _run_generate becomes 'Fatal: <message>' on stderr and exit 3.

    The exception must be suppressed (`raise SystemExit(3) from None`), so
    Click's runner sees a clean SystemExit rather than the raw RuntimeError.
    """
    with (
        patch("fieldkit.watch.preflight.preflight_check", return_value=[]),
        patch("fieldkit.commands.brief.cli._run_generate", side_effect=RuntimeError("brief was 0 bytes")),
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["generate"], catch_exceptions=True)

    assert result.exit_code == 3
    assert isinstance(result.exception, SystemExit)
    assert result.exception.__suppress_context__ is True
    assert "Fatal: brief was 0 bytes" in result.output
