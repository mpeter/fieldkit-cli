"""Unit tests for fieldkit.commands.watch.backstory_health.main().

Covers the legacy entry point shim that wraps the `cli` Click command and
translates its exceptions into a POSIX exit code:

  1. No exception → 0
  2. click.exceptions.Exit(<int>) → that int
  3. click.exceptions.Exit(None) → 0 (pinned actual behavior; differs from
     fieldkit.__main__.main(), which coerces None to a non-zero failure)
  4. click.exceptions.UsageError(<message>) → echoes "Error: <message>" to
     stderr and returns 2
  5. SystemExit(<int>) → that int
  6. SystemExit(None) (bare sys.exit()/SystemExit()) → 0
  7. SystemExit("<string>") (non-int code) → 0
  8. argv is forwarded to cli.main(args=argv, standalone_mode=False) unchanged

Patches `fieldkit.commands.watch.backstory_health.cli` (the module-level Click
command object), following the idiom in
tests/test_cli_exit.py::test_cli_main_click_exit_none_is_treated_as_error.
"""

from unittest.mock import patch

import click
import pytest

from fieldkit.commands.watch.backstory_health import main

pytestmark = pytest.mark.unit


_CLI_TARGET = "fieldkit.commands.watch.backstory_health.cli"


def test_main_returns_zero_on_normal_completion() -> None:
    """cli.main() completes without raising → main() returns 0."""
    with patch(_CLI_TARGET) as mock_cli:
        mock_cli.main.return_value = None
        result = main()

    assert result == 0


def test_main_returns_click_exit_code_when_int() -> None:
    """click.exceptions.Exit(5) → main() returns 5 exactly."""
    with patch(_CLI_TARGET) as mock_cli:
        mock_cli.main.side_effect = click.exceptions.Exit(5)
        result = main()

    assert result == 5


def test_main_returns_zero_for_click_exit_none() -> None:
    """click.exceptions.Exit(None) → main() returns 0.

    This pins the ACTUAL behavior of this shim, which differs from
    fieldkit.__main__.main() (which coerces a None click exit code to a
    non-zero failure). Not asserting this is a bug fix — just documenting
    the observed difference; out of scope for this tests-only change.
    """
    with patch(_CLI_TARGET) as mock_cli:
        mock_cli.main.side_effect = click.exceptions.Exit(None)  # pyright: ignore[reportArgumentType]
        result = main()

    assert result == 0


def test_main_echoes_usage_error_and_returns_two(capsys: pytest.CaptureFixture[str]) -> None:
    """click.exceptions.UsageError(msg) → echoes 'Error: <msg>' to stderr, returns 2."""
    with patch(_CLI_TARGET) as mock_cli:
        mock_cli.main.side_effect = click.exceptions.UsageError("bad option")
        result = main()

    assert result == 2
    captured = capsys.readouterr()
    assert "Error: bad option" in captured.err


def test_main_returns_systemexit_int_code() -> None:
    """SystemExit(3) → main() returns 3 exactly."""
    with patch(_CLI_TARGET) as mock_cli:
        mock_cli.main.side_effect = SystemExit(3)
        result = main()

    assert result == 3


def test_main_returns_zero_for_systemexit_none() -> None:
    """SystemExit(None) (bare sys.exit()/SystemExit()) → main() returns 0."""
    with patch(_CLI_TARGET) as mock_cli:
        mock_cli.main.side_effect = SystemExit()
        result = main()

    assert result == 0


def test_main_returns_zero_for_systemexit_string_code() -> None:
    """SystemExit('some error message') (non-int code) → main() returns 0.

    Distinct from the None case: the isinstance(exc.code, int) guard also
    fails for a string code, not just None.
    """
    with patch(_CLI_TARGET) as mock_cli:
        mock_cli.main.side_effect = SystemExit("some error message")
        result = main()

    assert result == 0


def test_main_forwards_argv_unchanged_to_cli_main() -> None:
    """argv is passed through to cli.main(args=argv, standalone_mode=False)."""
    argv = ["--threshold", "5"]
    with patch(_CLI_TARGET) as mock_cli:
        mock_cli.main.return_value = None
        main(argv)

    mock_cli.main.assert_called_once_with(args=argv, standalone_mode=False)
