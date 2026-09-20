"""historic regression / implementation change: cron entry uses absolute path and validates expression.

Tests that _handle_install_cron() uses shutil.which("fieldkit") when
available, raises ClickException when which() returns None (historic regression fix),
and validates the cron expression before writing crontab (implementation change).
"""

from unittest.mock import patch

import click
import pytest


@pytest.mark.unit
def test_cron_line_uses_absolute_path_when_which_resolves(tmp_path: pytest.fixture) -> None:
    """When shutil.which finds fieldkit, the cron line uses the absolute path."""
    from fieldkit.commands.watch.cli import _handle_install_cron

    fake_bin = "/usr/local/bin/fieldkit"

    with (
        patch("fieldkit.commands.watch.cli.shutil.which", return_value=fake_bin) as mock_which,
        patch("fieldkit.commands.watch.cli.click.echo") as mock_echo,
    ):
        _handle_install_cron(cron_time="0 7 * * 1-5", dry_run=True)

    mock_which.assert_called_once_with("fieldkit")
    # The echoed dry-run message must contain the absolute path.
    assert mock_echo.call_count == 1
    echoed = mock_echo.call_args[0][0]
    assert fake_bin in echoed, f"Expected absolute path in cron line, got: {echoed!r}"
    assert "watch run --all" in echoed


@pytest.mark.unit
def test_cron_line_raises_when_which_returns_none(tmp_path: pytest.fixture) -> None:
    """When shutil.which returns None, _handle_install_cron raises ClickException.

    historic regression: the old bare-name fallback silently installed a cron entry that
    would fail at runtime. Now the command aborts with a clear error.
    """
    import click

    from fieldkit.commands.watch.cli import _handle_install_cron

    with (
        patch("fieldkit.commands.watch.cli.shutil.which", return_value=None),
        pytest.raises(click.ClickException, match="fieldkit binary not found"),
    ):
        _handle_install_cron(cron_time="0 7 * * 1-5", dry_run=True)


@pytest.mark.unit
def test_cron_line_does_not_use_bare_fieldkit_when_which_resolves() -> None:
    """The cron line must not start with bare 'fieldkit' when an absolute path is available."""
    from fieldkit.commands.watch.cli import _handle_install_cron

    fake_bin = "/usr/local/bin/fieldkit"

    with (
        patch("fieldkit.commands.watch.cli.shutil.which", return_value=fake_bin),
        patch("fieldkit.commands.watch.cli.click.echo") as mock_echo,
    ):
        _handle_install_cron(cron_time="30 6 * * *", dry_run=True)

    echoed = mock_echo.call_args[0][0]
    # The cron line portion must use the absolute path, not start with bare "fieldkit"
    # Extract the cron line from the dry-run message.
    # Format: "[dry-run] would add crontab entry: <cron_line>"
    cron_line = echoed.split("entry: ", 1)[-1]
    parts = cron_line.split()
    # parts[0..4] are cron time fields; parts[5] is the binary
    assert len(parts) >= 6
    assert parts[5] == fake_bin, f"Expected absolute path at position 5, got: {parts[5]!r}"


# --- implementation change: cron expression validation ---


@pytest.mark.unit
def test_validate_cron_expression_rejects_wrong_field_count() -> None:
    """_validate_cron_expression raises ClickException for non-5-field input."""
    from fieldkit.commands.watch.cli import _validate_cron_expression

    with pytest.raises(click.ClickException, match="expected 5 fields"):
        _validate_cron_expression("invalid expression")


@pytest.mark.unit
def test_validate_cron_expression_rejects_bad_characters() -> None:
    """_validate_cron_expression raises ClickException for fields with non-cron characters."""
    from fieldkit.commands.watch.cli import _validate_cron_expression

    # Named weekday "MON" is outside the digit/*/,/-/ set.
    with pytest.raises(click.ClickException, match="invalid characters"):
        _validate_cron_expression("0 6 * * MON")


@pytest.mark.unit
def test_validate_cron_expression_accepts_valid_expression() -> None:
    """_validate_cron_expression does not raise for valid 5-field cron expressions."""
    from fieldkit.commands.watch.cli import _validate_cron_expression

    # Should not raise.
    _validate_cron_expression("0 6 * * *")
    _validate_cron_expression("*/15 8-17 1,15 * 1-5")


@pytest.mark.unit
def test_validate_cron_expression_rejects_four_fields() -> None:
    """_validate_cron_expression raises ClickException for a 4-field expression."""
    from fieldkit.commands.watch.cli import _validate_cron_expression

    with pytest.raises(click.ClickException, match="expected 5 fields"):
        _validate_cron_expression("0 6 * *")


@pytest.mark.unit
def test_validate_cron_expression_rejects_six_fields() -> None:
    """_validate_cron_expression raises ClickException for a 6-field expression."""
    from fieldkit.commands.watch.cli import _validate_cron_expression

    with pytest.raises(click.ClickException, match="expected 5 fields"):
        _validate_cron_expression("0 6 * * * *")


@pytest.mark.unit
def test_validate_cron_expression_rejects_non_ascii_digits() -> None:
    """_validate_cron_expression rejects Unicode digits (e.g. Arabic-Indic) that \\d would accept."""
    from fieldkit.commands.watch.cli import _validate_cron_expression

    with pytest.raises(click.ClickException, match="invalid characters"):
        _validate_cron_expression("\u0665 6 * * *")


@pytest.mark.unit
def test_validate_cron_expression_rejects_out_of_range_values() -> None:
    """_validate_cron_expression raises ClickException when a field's numeric value is out of range."""
    from fieldkit.commands.watch.cli import _validate_cron_expression

    with pytest.raises(click.ClickException, match="out-of-range value"):
        _validate_cron_expression("99 99 99 99 99")


@pytest.mark.unit
def test_validate_cron_expression_accepts_boundary_values() -> None:
    """_validate_cron_expression accepts the min/max boundary of each field's valid range."""
    from fieldkit.commands.watch.cli import _validate_cron_expression

    # minute 0-59, hour 0-23, day-of-month 1-31, month 1-12, day-of-week 0-7
    _validate_cron_expression("0 0 1 1 0")
    _validate_cron_expression("59 23 31 12 7")


@pytest.mark.unit
def test_handle_install_cron_rejects_invalid_cron_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI invocation with --install-cron and an invalid --cron-time exits non-zero.

    Validation must short-circuit before any subprocess call — assert
    subprocess.run is never called.
    """
    from click.testing import CliRunner

    from fieldkit.commands.watch.cli import cli

    called = False

    def _subprocess_run_spy(*args: object, **kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("fieldkit.commands.watch.cli.subprocess.run", _subprocess_run_spy)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["run", "--all", "--install-cron", "--cron-time", "invalid expression"],
        catch_exceptions=False,
    )

    assert result.exit_code != 0, f"Expected non-zero exit, got {result.exit_code}"
    assert "Invalid cron expression" in result.output
    assert not called, "subprocess.run was called — validation did not short-circuit"
