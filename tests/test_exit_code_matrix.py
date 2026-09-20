"""Subprocess exit-code matrix — validates the two-layer exit-boundary contract.

Each test invokes `python -m fieldkit <args>` as a subprocess and asserts the
process exit code. This proves the dispatcher backstop guarantee: every command
group exits with a canonical code (0-3) under controlled failure states, even
groups that do not wrap their entry in cli_main().

Marked @pytest.mark.slow — excluded from the default fast suite but run by CI.

Key assertions from the spec (cli-exit-contract.md):
- `fieldkit pursuit audit` with no config → EXIT_DATA (3), not raw traceback/exit 1
- `fieldkit contact enrich` with no config → EXIT_DATA (3)
- Unknown command → EXIT_DATA (3) via Click UsageError handler

historic regression follow-up: leaf commands that hand-roll wrong sys.exit() values cannot
be corrected by the dispatcher (they become SystemExit before the backstop).
Those commands are documented as xfail here so the gap has a concrete worklist.

To fix a historic regression case:
1. Replace `sys.exit(N)` in the leaf with `raise TypedError(...)`.
2. Verify the subprocess test here passes (no longer xfail).
3. Remove the xfail marker.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PYTHON = sys.executable


def _run(args: list[str], *, tmp_home: Path) -> int:
    """Run `python -m fieldkit <args>` with a clean HOME (no real config.yaml).

    Args:
        args: fieldkit subcommand arguments.
        tmp_home: a tmp_path directory used as HOME, ensuring ~/.config/fieldkit/
            does not exist and get_fieldkit_home() raises ConfigError.

    Returns:
        The process exit code.
    """
    env = {**os.environ, "HOME": str(tmp_home)}
    # #1211: scrub Google OAuth client credentials from the child env so that
    # `gmail sync` cannot resolve creds and slip past the no-config exit-3 guard
    # into a live InstalledAppFlow browser consent screen. Without this, a parent
    # env that has these set (e.g. the autonomous driver service) turns this
    # "no config → exit 3" assertion into a real interactive OAuth launch.
    for _oauth_var in (
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        # Note: GOOGLE_APPLICATION_CREDENTIALS is ADC (service account), not used
        # by InstalledAppFlow — it is intentionally omitted from this scrub list.
    ):
        env.pop(_oauth_var, None)
    result = subprocess.run(
        [_PYTHON, "-m", "fieldkit", *args],
        env=env,
        capture_output=True,
        check=False,
        stdin=subprocess.DEVNULL,  # #1211: non-TTY stdin — never allow an interactive OAuth prompt
        timeout=30,  # prevent CI hang from a subprocess that blocks indefinitely
    )
    return result.returncode


# ---------------------------------------------------------------------------
# No-config matrix — dispatcher backstop routes ConfigError → EXIT_DATA (3)
#
# These are the empirically-confirmed regression tests from the audit:
#   Before this change: both commands exited 1 (raw ConfigError traceback).
#   Current contract: configuration failures exit 3 (EXIT_DATA, structured).
# ---------------------------------------------------------------------------


# ── TestNoConfigExitsData (flattened) ───────────────────────────────────────


@pytest.mark.slow
def test_no_config_exits_data_pursuit_audit_no_config_is_exit_3(tmp_path: Path) -> None:
    """fieldkit pursuit audit with no config → EXIT_DATA (3).

    Regression: previously exited 1 (raw ConfigError traceback before this change).
    The dispatcher backstop routes ConfigError → EXIT_DATA (3).
    """
    rc = _run(["pursuit", "audit"], tmp_home=tmp_path)
    assert rc == 3, (
        f"Expected EXIT_DATA (3) for 'pursuit audit' with no config, got {rc}. "
        "If this is 1, the dispatcher backstop is not catching the ConfigError."
    )


@pytest.mark.slow
def test_no_config_exits_data_contact_enrich_no_config_is_exit_3(tmp_path: Path) -> None:
    """fieldkit contact enrich with no config → EXIT_DATA (3).

    Regression: previously exited 1 (raw ConfigError traceback before this change).
    The dispatcher backstop routes ConfigError → EXIT_DATA (3).
    """
    rc = _run(["contact", "enrich"], tmp_home=tmp_path)
    assert rc == 3, (
        f"Expected EXIT_DATA (3) for 'contact enrich' with no config, got {rc}. "
        "If this is 1, the dispatcher backstop is not catching the ConfigError."
    )


@pytest.mark.slow
def test_no_config_exits_data_gmail_sync_no_config_is_exit_3(tmp_path: Path) -> None:
    """fieldkit gmail sync with no config → EXIT_DATA (3).

    gmail/sync.py does not use cli_main(); ConfigError propagates to the dispatcher.
    """
    rc = _run(["gmail", "sync"], tmp_home=tmp_path)
    assert rc == 3, f"Expected EXIT_DATA (3) for 'gmail sync' with no config, got {rc}."


@pytest.mark.slow
@pytest.mark.parametrize("args", [["pipeline"], ["pursuit", "repair-dates"]])
def test_no_config_exits_data_for_local_config_commands(tmp_path: Path, args: list[str]) -> None:
    """Local pipeline and repair commands route missing configuration to exit 3."""
    rc = _run(args, tmp_home=tmp_path)
    assert rc == 3, f"Expected EXIT_DATA (3) for {args!r} with no config, got {rc}."


# ---------------------------------------------------------------------------
# Usage error → EXIT_DATA (3) via Click UsageError handler (D4 ratified)
# ---------------------------------------------------------------------------


# ── TestUsageErrors (flattened) ─────────────────────────────────────────────


@pytest.mark.slow
def test_usage_errors_unknown_group_is_exit_3(tmp_path: Path) -> None:
    """fieldkit nosuchcommand → EXIT_DATA (3) via Click UsageError handler."""
    rc = _run(["nosuchcommand"], tmp_home=tmp_path)
    assert rc == 3, f"Expected EXIT_DATA (3) for unknown command, got {rc}"


@pytest.mark.slow
def test_usage_errors_no_args_is_exit_0(tmp_path: Path) -> None:
    """fieldkit (no args) → exit 0 (shows help, invoke_without_command=True)."""
    rc = _run([], tmp_home=tmp_path)
    assert rc == 0, f"Expected 0 for no args (help display), got {rc}"


# ---------------------------------------------------------------------------
# historic regression xfail — leaf commands that hand-roll sys.exit() with wrong codes
#
# The dispatcher backstop cannot fix these: the leaf converts the exception to
# SystemExit(N) before it reaches the dispatcher, which then returns N unchanged.
#
# Each xfail documents one leaf that uses the wrong mechanism (sys.exit instead
# of raising a typed exception) and, where the code is wrong, the wrong code.
# When a leaf is migrated to raise a typed exception, its test flips from
# xfail to pass — remove the xfail at that point.
# ---------------------------------------------------------------------------


# ── TestBug508KnownWrongCodes (flattened) ───────────────────────────────────


@pytest.mark.slow
def test_bug508_known_wrong_codes_watch_pursuit_stalls_no_config_should_be_exit_3(tmp_path: Path) -> None:
    """fieldkit watch run pursuit-stalls with no config → EXIT_DATA (3).

    historic regression resolved: pursuit_stalls.py no longer catches ConfigError; re-raise
    lets the backstop route ConfigError → EXIT_DATA (3).
    """
    rc = _run(["watch", "run", "pursuit-stalls"], tmp_home=tmp_path)
    assert rc == 3, f"Expected EXIT_DATA (3) but got {rc}"


# ---------------------------------------------------------------------------
# historic regression migration tests — no-config exits EXIT_DATA (3)
#
# Added per the brief: one test per group, added RED (before migration),
# flipped GREEN after each group's sys.exit() calls are replaced.
# ---------------------------------------------------------------------------


# ── watch group ──────────────────────────────────────────────────────────────


@pytest.mark.slow
def test_no_config_exits_data_brief_generate_no_config_is_exit_3(tmp_path: Path) -> None:
    """fieldkit brief generate with no config → EXIT_DATA (3).

    historic regression: morning_brief.py catches ConfigError and calls sys.exit(1).
    After migration: re-raise lets the backstop route ConfigError → EXIT_DATA (3).
    """
    rc = _run(["brief", "generate"], tmp_home=tmp_path)
    assert rc == 3, f"Expected EXIT_DATA (3) for 'brief generate' with no config, got {rc}."


@pytest.mark.slow
def test_no_config_exits_data_watch_contract_expiry_no_config_is_exit_3(tmp_path: Path) -> None:
    """fieldkit watch run contract-expiry with no config → EXIT_DATA (3).

    historic regression: contract_expiry.py catches FieldkitError and calls sys.exit(1).
    After migration: re-raise lets the backstop route ConfigError → EXIT_DATA (3).
    """
    rc = _run(["watch", "run", "contract-expiry"], tmp_home=tmp_path)
    assert rc == 3, f"Expected EXIT_DATA (3) for 'watch contract-expiry' with no config, got {rc}."


@pytest.mark.slow
def test_no_config_exits_data_watch_slack_threads_no_config_is_exit_3(tmp_path: Path) -> None:
    """fieldkit watch run slack-threads with no config → EXIT_DATA (3).

    historic regression: slack_threads.py catches ConfigError and calls sys.exit(1).
    After migration: re-raise lets the backstop route ConfigError → EXIT_DATA (3).
    """
    rc = _run(["watch", "run", "slack-threads"], tmp_home=tmp_path)
    assert rc == 3, f"Expected EXIT_DATA (3) for 'watch run slack-threads' with no config, got {rc}."


@pytest.mark.slow
def test_no_config_exits_data_watch_backstory_health_no_config_is_exit_3(tmp_path: Path) -> None:
    """fieldkit watch run backstory-health with no config → EXIT_DATA (3).

    historic regression: backstory_health.py catches ConfigError and calls sys.exit(1).
    After migration: re-raise lets the backstop route ConfigError → EXIT_DATA (3).
    """
    rc = _run(["watch", "run", "backstory-health"], tmp_home=tmp_path)
    assert rc == 3, f"Expected EXIT_DATA (3) for 'watch run backstory-health' with no config, got {rc}."


# ── sf group ─────────────────────────────────────────────────────────────────


@pytest.mark.slow
def test_no_config_exits_auth_sf_frontmatter_no_config_is_exit_2(tmp_path: Path) -> None:
    """fieldkit sf listview with no session → EXIT_AUTH (2).

    historic regression: sf/frontmatter.py hand-rolls sys.exit() calls for auth/config errors.
    After migration: raise SystemExit(2) is equivalent; _check_sf_auth raises SystemExit(2)
    which propagates directly (no backstop translation needed).

    sf listview --all is the correct proxy: with no session cookie it reaches
    _check_sf_auth() → raise SystemExit(2) without hitting arg validation first.
    """
    rc = _run(["sf", "listview", "--all"], tmp_home=tmp_path)
    assert rc == 2, f"Expected EXIT_AUTH (2) for 'sf listview --all' with no session, got {rc}."


@pytest.mark.slow
def test_no_config_exits_auth_sf_session_check_no_config_is_exit_2(tmp_path: Path) -> None:
    """fieldkit sf session-check with no session → EXIT_AUTH (2).

    historic regression: sf/session_check.py used sys.exit(EXIT_AUTH) for missing session.
    After migration: raise SystemExit(EXIT_AUTH) is equivalent.
    """
    rc = _run(["sf", "session-check"], tmp_home=tmp_path)
    assert rc == 2, f"Expected EXIT_AUTH (2) for 'sf session-check' with no session, got {rc}."


# ── pursuit group ─────────────────────────────────────────────────────────────


@pytest.mark.slow
def test_no_config_exits_data_pursuit_health_no_config_is_exit_3(tmp_path: Path) -> None:
    """fieldkit pursuit health with no config → EXIT_DATA (3).

    historic regression: pursuit/pipeline_health.py hand-rolls sys.exit() calls.
    After migration: re-raise lets the backstop route ConfigError → EXIT_DATA (3).
    """
    rc = _run(["pursuit", "health"], tmp_home=tmp_path)
    assert rc == 3, f"Expected EXIT_DATA (3) for 'pursuit health' with no config, got {rc}."


@pytest.mark.slow
def test_no_config_exits_data_pursuit_forecast_no_config_is_exit_3(tmp_path: Path) -> None:
    """fieldkit pursuit forecast with no config → EXIT_DATA (3).

    historic regression: pursuit/forecast.py hand-rolls sys.exit() calls.
    After migration: re-raise lets the backstop route ConfigError → EXIT_DATA (3).
    """
    rc = _run(["pursuit", "forecast"], tmp_home=tmp_path)
    assert rc == 3, f"Expected EXIT_DATA (3) for 'pursuit forecast' with no config, got {rc}."


# ── issue group ───────────────────────────────────────────────────────────────


@pytest.mark.slow
def test_no_config_exits_data_issue_list_no_config_is_exit_3(tmp_path: Path) -> None:
    """fieldkit issue list with no config → EXIT_DATA (3).

    historic regression: issue/cli.py hand-rolls sys.exit() calls for config errors.
    After migration: re-raise lets the backstop route ConfigError → EXIT_DATA (3).
    """
    rc = _run(["issue", "list"], tmp_home=tmp_path)
    assert rc == 3, f"Expected EXIT_DATA (3) for 'issue list' with no config, got {rc}."


# ── meeting group ─────────────────────────────────────────────────────────────


@pytest.mark.slow
def test_no_config_exits_data_meeting_list_no_config_is_exit_3(tmp_path: Path) -> None:
    """fieldkit meeting list with no config → EXIT_DATA (3).

    historic regression: docs/cli.py (now fieldkit.commands.meeting) hand-rolled sys.exit()
    calls for config errors. After migration: re-raise lets the backstop route
    ConfigError → EXIT_DATA (3).
    """
    rc = _run(["meeting", "list"], tmp_home=tmp_path)
    assert rc == 3, f"Expected EXIT_DATA (3) for 'meeting list' with no config, got {rc}."


def test_no_config_exits_nonzero_sf_account_no_config(tmp_path: Path) -> None:
    """fieldkit sf account with no config → non-zero exit.

    historic regression: sf/account.py was catching errors and calling sys.exit(1).
    After migration: errors propagate to the backstop (exits 2 or 3 depending
    on the specific error path; both are non-zero and correct).
    """
    rc = _run(["sf", "account", "acme"], tmp_home=tmp_path)
    assert rc != 0, f"Expected non-zero exit for 'sf account' with no config, got {rc}."
