"""Smoke tests for fieldkit completion subcommand (implementation change).

Tests use Click's CliRunner to verify:
- No-arg invocation exits non-zero and prints help
- bash/zsh/fish produce non-empty output and exit 0

The subprocess call inside cli() is mocked so tests don't invoke Python
recursively. The mock returns the shell-specific fallback script content.
"""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.completion.cli import cli

pytestmark = pytest.mark.unit


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


# ── TestCompletionNoArg (flattened) ─────────────────────────────────────────


def test_completion_no_arg_no_arg_exits_nonzero(runner: CliRunner) -> None:
    """fieldkit completion with no shell arg exits non-zero."""
    result = runner.invoke(cli, [], catch_exceptions=False)
    assert result.exit_code != 0, f"Expected non-zero exit for no-arg invocation, got {result.exit_code}"


def test_completion_no_arg_no_arg_shows_help(runner: CliRunner) -> None:
    """fieldkit completion with no shell arg shows help text."""
    result = runner.invoke(cli, [])
    assert "bash" in result.output or "Usage" in result.output


# ── TestCompletionShells (flattened) ────────────────────────────────────────


def _completion_shells_mock_subprocess(stdout: str) -> MagicMock:
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = stdout
    return mock_result


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_completion_shells_shell_exits_zero(runner: CliRunner, shell: str) -> None:
    mock_proc = _completion_shells_mock_subprocess(f"# {shell} completion script\n")
    with patch("fieldkit.commands.completion.cli.subprocess.run", return_value=mock_proc):
        result = runner.invoke(cli, [shell], catch_exceptions=False)
    assert result.exit_code == 0, f"Expected exit 0 for 'completion {shell}', got {result.exit_code}"


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_completion_shells_shell_produces_nonempty_output(runner: CliRunner, shell: str) -> None:
    mock_proc = _completion_shells_mock_subprocess(f"# {shell} completion script\n")
    with patch("fieldkit.commands.completion.cli.subprocess.run", return_value=mock_proc):
        result = runner.invoke(cli, [shell], catch_exceptions=False)
    assert result.output.strip(), f"Expected non-empty output for 'completion {shell}'"


def test_completion_shells_bash_fallback_contains_fieldkit(runner: CliRunner) -> None:
    """When subprocess returns empty stdout, fallback script is used."""
    mock_proc = _completion_shells_mock_subprocess("")  # empty stdout → triggers fallback
    with patch("fieldkit.commands.completion.cli.subprocess.run", return_value=mock_proc):
        result = runner.invoke(cli, ["bash"], catch_exceptions=False)
    assert "fieldkit" in result.output, "Bash fallback script must reference 'fieldkit'"


def test_completion_shells_invalid_shell_rejected(runner: CliRunner) -> None:
    """An invalid shell choice is rejected by Click."""
    result = runner.invoke(cli, ["powershell"])
    assert result.exit_code != 0
