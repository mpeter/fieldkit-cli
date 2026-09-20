"""Regression tests for historic regression — invalid --since/--before date format.

Before the fix, passing a non-ISO-8601 date to any ``fieldkit gmail query``
subcommand raised an unhandled ``ValueError`` traceback and exited with code 1.

After the fix, ``_DateEpoch`` (a custom Click ``ParamType``) validates dates at
parse time. Click owns the error path, producing a clean ``UsageError`` message
and exiting via Click's own machinery — without conflicting with fieldkit's
``cli_main()`` exit-code taxonomy (which reserves 2 for auth failures).

``date_to_epoch()`` is retained as a pure converter for non-CLI callers; it
raises ``ValueError`` on bad input and callers are responsible for handling it.

Each CLI-level test is a regression test per TC-006: it reproduces the original
failure scenario and verifies the fix.
"""

import pytest
from click.testing import CliRunner

from fieldkit.commands.gmail.query import _DateEpoch, cli, date_to_epoch

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# CLI-level regression tests (via CliRunner — no real DB needed)
# ---------------------------------------------------------------------------


# ── TestInvalidSinceDateCLI (flattened) ─────────────────────────────────────


def test_cli_invalid_since_nonzero_exit() -> None:
    """Exit code MUST be 2 (Click UsageError) for invalid --since date."""
    runner = CliRunner()
    result = runner.invoke(cli, ["threads", "test", "--since", "not-a-date"])
    assert result.exit_code == 2, f"Expected exit code 2, got {result.exit_code}. Output: {result.output}"


def test_cli_invalid_since_error_mentions_flag() -> None:
    """Error message MUST reference '--since' so the user knows which flag is wrong."""
    runner = CliRunner()
    result = runner.invoke(cli, ["threads", "test", "--since", "not-a-date"])
    assert "--since" in result.output, f"Expected '--since' in output. Got: {result.output!r}"


def test_cli_invalid_since_error_mentions_expected_format() -> None:
    """Error message MUST include 'YYYY-MM-DD' for actionable guidance."""
    runner = CliRunner()
    result = runner.invoke(cli, ["threads", "test", "--since", "not-a-date"])
    assert "YYYY-MM-DD" in result.output, f"Expected 'YYYY-MM-DD' in output. Got: {result.output!r}"


def test_cli_invalid_since_no_traceback() -> None:
    """No Python traceback should appear — only a clean Click error message."""
    runner = CliRunner()
    result = runner.invoke(cli, ["threads", "test", "--since", "not-a-date"])
    assert "Traceback" not in result.output, f"Unexpected traceback in output: {result.output!r}"
    assert "ValueError" not in result.output, f"Unexpected ValueError in output: {result.output!r}"


# ── TestInvalidBeforeDateCLI (flattened) ────────────────────────────────────


def test_cli_invalid_before_nonzero_exit() -> None:
    """Exit code MUST be 2 (Click UsageError) for invalid --before date."""
    runner = CliRunner()
    result = runner.invoke(cli, ["threads", "test", "--before", "2026/06/09"])
    assert result.exit_code == 2, f"Expected exit code 2, got {result.exit_code}. Output: {result.output}"


def test_cli_invalid_before_error_mentions_flag() -> None:
    """Error message MUST reference '--before' so the user knows which flag is wrong."""
    runner = CliRunner()
    result = runner.invoke(cli, ["threads", "test", "--before", "2026/06/09"])
    assert "--before" in result.output, f"Expected '--before' in output. Got: {result.output!r}"


def test_cli_invalid_before_error_mentions_expected_format() -> None:
    """Error message MUST include 'YYYY-MM-DD'."""
    runner = CliRunner()
    result = runner.invoke(cli, ["threads", "test", "--before", "2026/06/09"])
    assert "YYYY-MM-DD" in result.output, f"Expected 'YYYY-MM-DD' in output. Got: {result.output!r}"


# ---------------------------------------------------------------------------
# Unit tests for _DateEpoch param type
# ---------------------------------------------------------------------------


# ── TestDateEpochParamType (flattened) ──────────────────────────────────────


def test_normalize_date_valid_date_converts_to_epoch() -> None:
    """Valid YYYY-MM-DD must convert to correct Unix epoch."""
    # 2026-01-01 00:00:00 UTC = 1767225600
    result = _DateEpoch().convert("2026-01-01", None, None)
    assert result == 1767225600, f"Unexpected epoch: {result}"


def test_normalize_date_is_before_adds_one_day() -> None:
    """is_before=True must advance epoch by 86400 for exclusive upper bound."""
    base = _DateEpoch().convert("2026-01-01", None, None)
    with_before = _DateEpoch(is_before=True).convert("2026-01-01", None, None)
    assert with_before == base + 86400


def test_normalize_date_invalid_date_fails() -> None:
    """Invalid date string MUST cause convert() to call fail(), raising UsageError."""
    import click

    with pytest.raises(click.exceptions.BadParameter, match=r"YYYY-MM-DD"):
        _DateEpoch().convert("not-a-date", None, None)


def test_normalize_date_invalid_date_message_contains_format() -> None:
    """Failure message MUST contain 'YYYY-MM-DD'."""
    import click

    with pytest.raises(click.exceptions.BadParameter) as exc_info:
        _DateEpoch().convert("bad-date", None, None)
    assert "YYYY-MM-DD" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Unit tests for date_to_epoch() — non-CLI helper
# ---------------------------------------------------------------------------


# ── TestDateToEpochHelper (flattened) ───────────────────────────────────────


def test_normalize_date_valid_date_returns_epoch() -> None:
    """2026-01-01 must parse to the correct Unix epoch."""
    result = date_to_epoch("2026-01-01")
    assert result == 1767225600, f"Unexpected epoch: {result}"


def test_normalize_date_is_before_adds_one_day_2() -> None:
    """is_before=True must advance by 86400 seconds."""
    base = date_to_epoch("2026-01-01")
    with_before = date_to_epoch("2026-01-01", is_before=True)
    assert with_before == base + 86400


def test_normalize_date_invalid_date_raises_value_error() -> None:
    """date_to_epoch raises ValueError on bad input — callers handle it."""
    with pytest.raises(ValueError, match=r"does not match format"):
        date_to_epoch("not-a-date")


# ---------------------------------------------------------------------------
# historic regression: --limit must reject values < 1
# ---------------------------------------------------------------------------


# ── TestNegativeLimitRejected (flattened) ───────────────────────────────────


def test_negative_limit_rejected_negative_limit_exits_nonzero() -> None:
    """--limit -1 MUST exit with a non-zero code (Click UsageError = 2)."""
    runner = CliRunner()
    result = runner.invoke(cli, ["threads", "test", "--limit", "-1"])
    assert result.exit_code != 0, (
        f"Expected non-zero exit for --limit -1, got {result.exit_code}. Output: {result.output}"
    )


def test_negative_limit_rejected_negative_limit_exit_code_is_2() -> None:
    """Click UsageError produces exit code 2."""
    runner = CliRunner()
    result = runner.invoke(cli, ["threads", "test", "--limit", "-1"])
    assert result.exit_code == 2, (
        f"Expected exit code 2 for --limit -1, got {result.exit_code}. Output: {result.output}"
    )


def test_negative_limit_rejected_zero_limit_rejected() -> None:
    """--limit 0 is also invalid (must be ≥ 1)."""
    runner = CliRunner()
    result = runner.invoke(cli, ["threads", "test", "--limit", "0"])
    assert result.exit_code == 2, f"Expected exit code 2 for --limit 0, got {result.exit_code}. Output: {result.output}"


def test_negative_limit_rejected_negative_limit_on_person_subcommand() -> None:
    """--limit validation applies to all subcommands sharing _shared_options."""
    runner = CliRunner()
    result = runner.invoke(cli, ["person", "Alice", "--limit", "-5"])
    assert result.exit_code == 2, (
        f"Expected exit code 2 for person --limit -5, got {result.exit_code}. Output: {result.output}"
    )


def test_negative_limit_rejected_positive_limit_accepted() -> None:
    """--limit 1 is valid; error must come from missing DB, not option parsing."""
    runner = CliRunner()
    result = runner.invoke(cli, ["threads", "test", "--limit", "1", "--db", "/nonexistent/gmail.db"])
    # Exit code 1 (DB not found) or 0 — anything except 2 (UsageError)
    assert result.exit_code != 2, f"--limit 1 should be accepted by Click. Got exit code 2. Output: {result.output}"
