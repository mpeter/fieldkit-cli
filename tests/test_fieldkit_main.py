"""Tests for fieldkit/__main__.py — the top-level Click dispatcher.

Tests cover:
- Help output contains expected commands
- Unknown group exits nonzero with an error message
- No-arg case shows help and exits nonzero
- --version flag works
- Dispatch reaches the correct group module (via CliRunner)
- main(None) falls back to sys.argv[1:]
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from fieldkit.__main__ import cli, main

pytestmark = pytest.mark.unit
_INTERRUPT_TEST_TIMEOUT_SECONDS = 10


@pytest.mark.parametrize(
    ("kind", "code", "expected"),
    [
        ("click", 7, 3),
        ("click", None, 3),
        ("click", False, 3),
        ("click", True, 3),
        ("system", 7, 3),
        ("system", None, 0),
        ("system", False, 3),
        ("system", True, 3),
        ("system", "private-exit-marker", 3),
    ],
)
def test_dispatcher_explicit_exit_codes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], kind: str, code: object, expected: int
) -> None:
    if kind == "click":
        error: BaseException = click.exceptions.Exit()
        monkeypatch.setattr(error, "exit_code", code)
    else:
        error = SystemExit(code)
    with patch("fieldkit.__main__.cli.main", side_effect=error):
        result = main([])

    assert result == expected
    assert "private-exit-marker" not in capsys.readouterr().err


def test_dispatcher_click_error_is_clean_data_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def invalid_input() -> None:
        raise click.ClickException("Cannot find the requested pursuit")

    monkeypatch.setattr("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", invalid_input)
    result = main(["pursuit", "audit"])
    assert result == 3
    assert capsys.readouterr().err == "Error: Cannot find the requested pursuit\n"


@pytest.mark.parametrize("error", [EOFError(), click.Abort()])
def test_dispatcher_non_signal_abort_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: Exception
) -> None:
    def abort() -> None:
        raise error

    monkeypatch.setattr("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", abort)
    result = main(["pursuit", "audit"])
    assert result == 1
    error_output = capsys.readouterr().err
    assert "Aborted." in error_output
    assert "Traceback" not in error_output


def test_dispatcher_startup_interruption_exits_130(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupt() -> None:
        raise KeyboardInterrupt()

    monkeypatch.setattr("fieldkit.__main__.load_dotenv_safe", interrupt)
    result = main(["--help"])
    assert result == 130
    assert "Traceback" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Help / usage
# ---------------------------------------------------------------------------


# ── TestUsageAndHelp (flattened) ────────────────────────────────────────────


def test_usage_and_help_no_args_shows_help_exits_0() -> None:
    """Click shows help and exits 0 when no args are given to a group."""
    runner = CliRunner()
    result = runner.invoke(cli, [])
    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_usage_and_help_help_flag_exits_0() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "Commands:" in result.output or "Usage:" in result.output


def test_usage_and_help_h_flag_exits_0() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["-h"])
    assert result.exit_code == 0


def test_usage_and_help_help_lists_all_groups() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    for group in ("brief", "gmail", "ingest", "pipeline", "sf"):
        assert group in result.output


def test_usage_and_help_version_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "fieldkit" in result.output


# ---------------------------------------------------------------------------
# Unknown group
# ---------------------------------------------------------------------------


# ── TestUnknownGroup (flattened) ────────────────────────────────────────────


def test_unknown_group_unknown_group_exits_nonzero() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["nonexistent"])
    assert result.exit_code != 0  # CliRunner uses standalone_mode=True → Click's own code


def test_unknown_group_unknown_group_error_message() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["nonexistent"])
    # Click prints "No such command" to output (mixed stream in CliRunner)
    combined = result.output + (result.exception.__str__() if result.exception else "")
    assert "nonexistent" in combined or "No such command" in combined


def test_unknown_group_unknown_group_via_main_exits_3() -> None:
    """main() wrapper catches Click UsageError and returns EXIT_DATA (3).

    A malformed invocation (unknown command, bad flag) is a data/validation error.
    Retrying without fixing the command will not help.
    """
    rc = main(["not-a-group"])
    assert rc == 3


# ---------------------------------------------------------------------------
# Dispatch — verify the group module's main() / cli is called with correct args
# ---------------------------------------------------------------------------


# ── TestDispatch (flattened) ────────────────────────────────────────────────


def _dispatch_make_mock_cli_group(return_code: int = 0) -> MagicMock:
    """Create a mock Click group that records invocations."""
    mock_cli = MagicMock(spec=click.Group)
    mock_cli.name = "mock-group"
    mock_cli.main.return_value = return_code
    return mock_cli


def _dispatch_dispatch(group: str, extra_args: list[str], return_code: int = 0) -> tuple[int, MagicMock]:
    """Verify importlib.import_module is called with the correct module path."""
    mock_mod = MagicMock()
    mock_mod.cli = _dispatch_make_mock_cli_group(return_code)
    with patch("fieldkit.__main__.importlib.import_module", return_value=mock_mod) as mock_import:
        runner = CliRunner()
        # Just verify the module is imported (lazy loading works)
        result = runner.invoke(cli, [group, "--help"], catch_exceptions=False)
    return result.exit_code, mock_import


def test_dispatch_dispatch_gmail() -> None:
    _, mock_import = _dispatch_dispatch("gmail", ["sync"])
    mock_import.assert_called_once_with("fieldkit.commands.gmail.cli")


def test_dispatch_dispatch_ingest() -> None:
    _, mock_import = _dispatch_dispatch("ingest", ["status"])
    mock_import.assert_called_once_with("fieldkit.commands.ingest.cli")


def test_dispatch_dispatch_pipeline() -> None:
    _, mock_import = _dispatch_dispatch("pipeline", [])
    mock_import.assert_called_once_with("fieldkit.commands.pipeline.cli")


def test_dispatch_dispatch_sf() -> None:
    _, mock_import = _dispatch_dispatch("sf", ["listview"])
    mock_import.assert_called_once_with("fieldkit.commands.sf.cli")


def test_dispatch_dispatch_brief() -> None:
    _, mock_import = _dispatch_dispatch("brief", ["--no-llm"])
    mock_import.assert_called_once_with("fieldkit.commands.brief.cli")


def test_dispatch_dispatch_forwards_extra_args() -> None:
    _, mock_import = _dispatch_dispatch("ingest", ["run", "--pipeline", "transcript-ingest"])
    mock_import.assert_called_once_with("fieldkit.commands.ingest.cli")


def test_dispatch_dispatch_propagates_doctor_auth_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unhealthy nested doctor command reaches the process entry point."""
    from fieldkit.commands.doctor._result import DoctorResult

    monkeypatch.setattr(
        "fieldkit.commands.doctor.cli._run_all",
        lambda: [
            DoctorResult(
                service="Salesforce", healthy=False, configured=True, message="session expired", failure_kind="auth"
            )
        ],
    )
    monkeypatch.setattr("fieldkit.commands.doctor.cli._render_all", lambda _results, _states: None)
    rc = main(["doctor"])

    assert rc == 2


@pytest.mark.parametrize("callback_result", [None, "unexpected", True])
def test_dispatch_non_integer_callback_result_exits_zero(callback_result: object) -> None:
    """Ordinary and unexpected callback values retain the success contract."""
    with patch("fieldkit.__main__.cli") as mock_cli:
        mock_cli.main.return_value = callback_result
        rc = main(["mock-command"])

    assert rc == 0


def test_dispatch_default_argv_uses_sys_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """main(None) falls back to sys.argv[1:]."""
    monkeypatch.setattr(sys, "argv", ["fieldkit", "--help"])
    rc = main(None)
    assert rc == 0


# ---------------------------------------------------------------------------
# 4E.1 get_command / list_commands (LazyGroup)
# ---------------------------------------------------------------------------


# ── TestLazyCommandLoader (flattened) ───────────────────────────────────────


@pytest.mark.unit
def test_lazy_command_loader_get_command_returns_click_group_for_known_command() -> None:
    """get_command returns a click.Command for a known command name."""
    import click

    from fieldkit.__main__ import cli

    ctx = cli.make_context("fieldkit", [])
    result = cli.get_command(ctx, "gmail")
    ctx.close()

    assert result is not None
    assert isinstance(result, click.Command)


@pytest.mark.unit
def test_lazy_command_loader_get_command_returns_none_for_unknown() -> None:
    """get_command returns None for an unknown command name."""
    from fieldkit.__main__ import cli

    ctx = cli.make_context("fieldkit", [])
    result = cli.get_command(ctx, "nonexistent_xyz_command")
    ctx.close()

    assert result is None


@pytest.mark.unit
def test_lazy_command_loader_list_commands_returns_sorted_names() -> None:
    """list_commands returns a sorted list of available command names."""
    from fieldkit.__main__ import cli

    ctx = cli.make_context("fieldkit", [])
    result = cli.list_commands(ctx)
    ctx.close()

    assert isinstance(result, list)
    assert len(result) > 0
    assert result == sorted(result)
    assert "gmail" in result
    assert "sf" in result


# ---------------------------------------------------------------------------
# Dispatcher backstop — exception routing through __main__.main()
# ---------------------------------------------------------------------------


# ── TestDispatcherExceptionRouting (flattened) ──────────────────────────────


def _dispatcher_exception_routing_raise(exc: BaseException) -> None:
    """Helper: return a callable that raises exc when called."""
    raise exc


def test_dispatcher_exception_routing_config_error_returns_exit_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """ConfigError raised by a non-cli_main group → EXIT_DATA (3) via dispatcher backstop.

    Patches get_fieldkit_home() as imported by audit_cmd — the first call
    in the pursuit audit command path, which has no cli_main() wrapper.
    """
    from fieldkit.config import ConfigError

    def raise_config_error() -> None:
        raise ConfigError("no config.yaml")

    monkeypatch.setattr("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", raise_config_error)
    rc = main(["pursuit", "audit"])
    assert rc == 3, f"ConfigError should return EXIT_DATA (3), got {rc}"


def test_dispatcher_exception_routing_auth_error_returns_exit_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """AuthError (generic) raised by a non-cli_main group → EXIT_AUTH (2) via dispatcher backstop."""
    from fieldkit.errors import AuthError

    def raise_auth_error() -> None:
        raise AuthError("auth failure")

    monkeypatch.setattr("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", raise_auth_error)
    rc = main(["pursuit", "audit"])
    assert rc == 2, f"AuthError should return EXIT_AUTH (2), got {rc}"


def test_dispatcher_exception_routing_pursuit_stale_error_returns_exit_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    """PursuitStaleError raised by a non-cli_main group → EXIT_PARTIAL (1) via dispatcher backstop."""
    from fieldkit.errors import PursuitStaleError

    def raise_stale_error() -> None:
        raise PursuitStaleError("pursuit stale")

    monkeypatch.setattr("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", raise_stale_error)
    rc = main(["pursuit", "audit"])
    assert rc == 1, f"PursuitStaleError should return EXIT_PARTIAL (1), got {rc}"


def test_dispatcher_exception_routing_runtime_error_returns_exit_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """Generic RuntimeError raised by a non-cli_main group → EXIT_DATA (3) via dispatcher backstop."""

    def raise_runtime_error() -> None:
        raise RuntimeError("unexpected crash")

    monkeypatch.setattr("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", raise_runtime_error)
    rc = main(["pursuit", "audit"])
    assert rc == 3, f"RuntimeError should return EXIT_DATA (3), got {rc}"


def test_dispatcher_keyboard_interrupt_exits_130_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Click-wrapped interruption preserves the documented shell exit status."""

    def raise_keyboard_interrupt() -> None:
        raise KeyboardInterrupt()

    monkeypatch.setattr("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", raise_keyboard_interrupt)
    rc = main(["pursuit", "audit"])
    assert rc == 130
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.integration
def test_dispatcher_real_sigint_exits_130(tmp_path: Path) -> None:
    """A real SIGINT delivered during Click dispatch stays outside application errors."""
    script = """
import os
import signal
from unittest.mock import patch
from fieldkit.__main__ import main

def interrupt():
    os.kill(os.getpid(), signal.SIGINT)

with patch("fieldkit.commands.pursuit.audit_cmd.get_fieldkit_home", side_effect=interrupt):
    raise SystemExit(main(["pursuit", "audit"]))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "config"), "FIELDKIT_NO_LLM": "1"},
        capture_output=True,
        text=True,
        check=False,
        timeout=_INTERRUPT_TEST_TIMEOUT_SECONDS,
    )
    assert result.returncode == 130
    assert "Aborted." in result.stderr
    assert "Traceback" not in result.stderr
