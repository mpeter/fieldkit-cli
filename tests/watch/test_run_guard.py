"""tests/watch/test_run_guard.py — Unit tests for the once-per-day run guard.

Tests:
  - was_run_today() returns True when status shows today's date
  - was_run_today() returns False when status shows a past date
  - was_run_today() returns False when entry is absent
  - run_all exits 0 immediately when guard fires (no --force)
  - run_all proceeds when --force is set even if guard would fire
"""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from fieldkit.commands.watch.cli import cli
from fieldkit.watch.integration_plan import IntegrationPlan
from fieldkit.watch.slack_threads import SlackRunOutcome
from fieldkit.watch.status import WatcherDailySnapshot, WatcherRunResult, was_run_today

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# was_run_today unit tests (no I/O — mock _load_run_status)
# ---------------------------------------------------------------------------


def _today_ts() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _yesterday_ts() -> str:
    from datetime import timedelta

    return (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_was_run_today_returns_true_for_today() -> None:
    status = {"run-all": {"last_run": _today_ts()}}
    with patch("fieldkit.watch.status._load_run_status", return_value=status):
        assert was_run_today("run-all") is True


def test_was_run_today_returns_false_for_yesterday() -> None:
    status = {"run-all": {"last_run": _yesterday_ts()}}
    with patch("fieldkit.watch.status._load_run_status", return_value=status):
        assert was_run_today("run-all") is False


def test_was_run_today_returns_false_when_absent() -> None:
    with patch("fieldkit.watch.status._load_run_status", return_value={}):
        assert was_run_today("run-all") is False


def test_was_run_today_returns_false_on_bad_timestamp() -> None:
    status = {"run-all": {"last_run": "not-a-timestamp"}}
    with patch("fieldkit.watch.status._load_run_status", return_value=status):
        assert was_run_today("run-all") is False


# ---------------------------------------------------------------------------
# run_all CLI guard tests
# ---------------------------------------------------------------------------


def _mock_was_run_today_true(watcher: str) -> bool:
    return True


def _mock_was_run_today_false(watcher: str) -> bool:
    return False


def _successful_slack_outcome() -> SlackRunOutcome:
    return SlackRunOutcome(
        run=WatcherRunResult("ok", True, "written"),
        records_checked=0,
        alerts_generated=0,
        failures=0,
        auth_error=False,
        provider_error=False,
        elapsed_seconds=0.0,
        dry_run=False,
    )


def test_run_all_exits_early_when_already_run_today() -> None:
    """run-all with no --force exits 0 immediately when guard fires."""
    runner = CliRunner()
    with (
        patch("fieldkit.watch.status.get_daily_run_snapshot", return_value=WatcherDailySnapshot(True, "ok")),
        patch("fieldkit.watch.status.write_run_status", return_value="written"),
        # implementation note: bypass preflight checks in unit tests (no real credentials).
        patch("fieldkit.watch.preflight.preflight_check", return_value=[]),
    ):
        result = runner.invoke(cli, ["run", "--all"])
    # Should exit 0 (already ran today)
    assert result.exit_code == 0


def test_run_all_skips_watchers_when_guard_fires() -> None:
    """No watcher domain is invoked when guard exits early."""
    runner = CliRunner()

    with (
        patch("fieldkit.watch.status.get_daily_run_snapshot", return_value=WatcherDailySnapshot(True, "ok")),
        patch("fieldkit.watch.status.write_run_status", return_value="written"),
        patch("fieldkit.watch.waiting_on_tracker._run") as mock_waiting,
        patch("fieldkit.watch.pursuit_stalls._run_pursuit_stalls") as mock_pursuit,
        patch("fieldkit.watch.close_date_countdown._run_countdown") as mock_countdown,
        patch("fieldkit.watch.contract_expiry._run_contract_expiry") as mock_contract,
        patch("fieldkit.watch.backstory_health._run_backstory_health") as mock_backstory,
        patch("fieldkit.watch.slack_threads._run_slack_threads") as mock_slack,
        patch("fieldkit.watch.draft_queue._run_draft_queue") as mock_drafts,
        # implementation note: bypass preflight checks in unit tests (no real credentials).
        patch("fieldkit.watch.preflight.preflight_check", return_value=[]),
    ):
        result = runner.invoke(cli, ["run", "--all"])

    assert result.exit_code == 0
    for watcher in (mock_waiting, mock_pursuit, mock_countdown, mock_contract, mock_backstory, mock_slack, mock_drafts):
        watcher.assert_not_called()


def test_run_all_force_bypasses_guard() -> None:
    """--force flag causes run-all to proceed despite guard returning True."""
    runner = CliRunner()
    plan = IntegrationPlan((), ("backstory-health", "draft-queue", "slack-threads"), False, False, ())

    with (
        patch("fieldkit.watch.status.get_daily_run_snapshot", return_value=WatcherDailySnapshot(True, "ok")),
        patch("fieldkit.watch.status.write_run_status", return_value="written") as mock_write,
        patch(
            "fieldkit.watch.waiting_on_tracker._run", return_value=WatcherRunResult("ok", True, "written")
        ) as mock_waiting,
        patch(
            "fieldkit.watch.pursuit_stalls._run_pursuit_stalls", return_value=WatcherRunResult("ok", True, "written")
        ) as mock_pursuit,
        patch(
            "fieldkit.watch.close_date_countdown._run_countdown", return_value=WatcherRunResult("ok", True, "written")
        ) as mock_countdown,
        patch(
            "fieldkit.watch.contract_expiry._run_contract_expiry", return_value=WatcherRunResult("ok", True, "written")
        ) as mock_contract,
        patch(
            "fieldkit.watch.backstory_health._run_backstory_health",
            return_value=WatcherRunResult("ok", True, "written"),
        ) as mock_backstory,
        patch(
            "fieldkit.watch.slack_threads._run_slack_threads", return_value=_successful_slack_outcome()
        ) as mock_slack,
        patch(
            "fieldkit.watch.draft_queue._run_draft_queue", return_value=WatcherRunResult("ok", True, "written")
        ) as mock_drafts,
        patch("fieldkit.watch.integration_plan.build_integration_plan", return_value=plan),
        # implementation note: bypass preflight checks in unit tests (no real credentials).
        patch("fieldkit.watch.preflight.preflight_check", return_value=[]),
        patch(
            "fieldkit.commands.brief.generate._run_generate_inner", return_value=WatcherRunResult("ok", True, "written")
        ),
    ):
        result = runner.invoke(cli, ["run", "--all", "--force"])

    # Should have run and exited 0 (all watchers ok)
    assert result.exit_code == 0
    for watcher in (mock_waiting, mock_pursuit, mock_countdown, mock_contract, mock_backstory, mock_slack, mock_drafts):
        watcher.assert_called_once()
    assert mock_pursuit.call_args.kwargs["force"] is True
    # write_run_status should be called once for "run-all"
    mock_write.assert_called_once()
    call_kwargs = mock_write.call_args.kwargs
    assert call_kwargs["watcher"] == "run-all"


def test_run_all_propagates_a_fatal_countdown_exit() -> None:
    """A fatal close-date-countdown run cannot leave the aggregate successful."""
    runner = CliRunner()
    with (
        patch("fieldkit.watch.status.get_daily_run_snapshot", return_value=WatcherDailySnapshot(False, None)),
        patch("fieldkit.watch.status.write_run_status", return_value="written") as mock_write,
        patch("fieldkit.watch.waiting_on_tracker._run", return_value=WatcherRunResult("ok", True, "written")),
        patch(
            "fieldkit.watch.pursuit_stalls._run_pursuit_stalls", return_value=WatcherRunResult("ok", True, "written")
        ),
        patch(
            "fieldkit.watch.close_date_countdown._run_countdown",
            return_value=WatcherRunResult("fatal", True, "written"),
        ),
        patch(
            "fieldkit.watch.contract_expiry._run_contract_expiry", return_value=WatcherRunResult("ok", True, "written")
        ),
        patch(
            "fieldkit.watch.backstory_health._run_backstory_health",
            return_value=WatcherRunResult("ok", True, "written"),
        ),
        patch("fieldkit.watch.slack_threads._run_slack_threads", return_value=_successful_slack_outcome()),
        patch("fieldkit.watch.draft_queue._run_draft_queue", return_value=WatcherRunResult("ok", True, "written")),
        patch("fieldkit.watch.preflight.preflight_check", return_value=[]),
        patch(
            "fieldkit.commands.brief.generate._run_generate_inner", return_value=WatcherRunResult("ok", True, "written")
        ),
    ):
        result = runner.invoke(cli, ["run", "--all", "--force"])

    assert result.exit_code == 1
    assert mock_write.call_args.kwargs["outcome"] == "fatal"


# ---------------------------------------------------------------------------
# historic regression: fatal prior outcome tests
# ---------------------------------------------------------------------------


def test_was_run_today_fatal_outcome_logs_warning_and_exits_1_for_pursuit_stalls() -> None:
    """historic regression: when pursuit-stalls ran today with outcome=fatal, skip exits 1 with WARNING."""
    from fieldkit.commands.watch import pursuit_stalls as wps

    with (
        patch("fieldkit.watch.pursuit_stalls.get_daily_run_snapshot", return_value=WatcherDailySnapshot(True, "fatal")),
        patch("fieldkit.watch.pursuit_stalls.watcher_logging"),
    ):
        rc = wps._run_pursuit_stalls(threshold=14, account=None, dry_run=False)

    assert rc == WatcherRunResult("fatal", False, None), f"Expected exit 1 when prior outcome=fatal, got {rc}"


def test_was_run_today_ok_outcome_exits_0_silently_for_pursuit_stalls() -> None:
    """historic regression: when pursuit-stalls ran today with outcome=ok, skip still exits 0."""
    from fieldkit.commands.watch import pursuit_stalls as wps

    with (
        patch("fieldkit.watch.pursuit_stalls.get_daily_run_snapshot", return_value=WatcherDailySnapshot(True, "ok")),
        patch("fieldkit.watch.pursuit_stalls.write_run_status", return_value="written"),
        patch("fieldkit.watch.pursuit_stalls.watcher_logging"),
    ):
        rc = wps._run_pursuit_stalls(threshold=14, account=None, dry_run=False)

    assert rc == WatcherRunResult("ok", False, "written"), f"Expected exit 0 when prior outcome=ok, got {rc}"


def test_run_all_fatal_prior_outcome_exits_1() -> None:
    """historic regression: run-all exits 1 (not 0) when last run was fatal and guard fires."""
    runner = CliRunner()
    with (
        patch("fieldkit.watch.status.get_daily_run_snapshot", return_value=WatcherDailySnapshot(True, "fatal")),
        patch("fieldkit.watch.status.write_run_status", return_value="written"),
    ):
        result = runner.invoke(cli, ["run", "--all"])
    assert result.exit_code == 1, f"Expected exit 1 on fatal prior outcome, got {result.exit_code}"
