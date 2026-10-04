"""Preserve runtime MCP routing and watcher startup outcomes."""

import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

import fieldkit.commands.watch.cli as watch_cli
import fieldkit.watch.backstory_health as backstory_health
import fieldkit.watch.draft_queue as draft_queue
import fieldkit.watch.morning_brief as morning_brief
from fieldkit.config import ConfigError
from fieldkit.errors import AuthError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("value, expected", [(0, 0), (3, 3), (True, 1), (None, 1), ("0", 1)])
def test_invoke_watcher_accepts_only_exact_integers(value: object, expected: int) -> None:
    result = watch_cli._invoke_watcher("draft-queue", lambda: value)
    assert result == expected


@pytest.mark.parametrize("unavailable", [False, True])
def test_initialize_sales_session_preserves_gateway_outcome(unavailable: bool) -> None:
    session = MagicMock()
    if unavailable:
        session.initialize.side_effect = RuntimeError("gateway unavailable")
    result = backstory_health._initialize_sales_session(session)
    assert result is (None if unavailable else session)
    session.initialize.assert_called_once_with()


@pytest.mark.parametrize("as_json", [False, True])
def test_run_backstory_health_reports_and_propagates_config_error(
    as_json: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    error = ConfigError("mcp_sales_group is invalid")
    with (
        patch.object(backstory_health, "get_mcp_work_group", side_effect=error),
        patch.object(backstory_health, "_load_and_filter_accounts", return_value={"acme": {}}),
        patch.object(backstory_health, "load_state", return_value={}),
        patch.object(backstory_health, "watcher_logging"),
        patch.object(backstory_health, "MCPSession") as session_class,
        patch.object(backstory_health, "write_run_status") as write_status,
        patch("time.monotonic", return_value=10.0),
        pytest.raises(ConfigError, match="mcp_sales_group") as caught,
    ):
        backstory_health._run_backstory_health(threshold=50, account=None, dry_run=False, as_json=as_json)
    assert caught.value is error
    session_class.assert_not_called()
    write_status.assert_called_once_with(
        watcher="backstory-health",
        outcome="fatal",
        records_checked=0,
        alerts_generated=0,
        failures=1,
        elapsed_seconds=0.0,
        dry_run=False,
    )
    output = capsys.readouterr().out
    if as_json:
        assert json.loads(output) == {
            "watcher": "backstory-health",
            "outcome": "fatal",
            "records_checked": 0,
            "alerts_generated": 0,
            "failures": 1,
            "elapsed_seconds": 0.0,
            "dry_run": False,
        }
    else:
        assert output == ""

    session = MagicMock()
    with (
        patch.object(backstory_health, "_open_mcp_session", return_value=session),
        patch.object(backstory_health, "_load_and_filter_accounts", return_value={"acme": {}}),
        patch.object(backstory_health, "load_state", return_value={}),
        patch.object(backstory_health, "_check_all_accounts", return_value=(1, 0, 0, {})),
        patch.object(backstory_health, "watcher_logging"),
        patch.object(backstory_health, "write_run_status") as recovered_status,
    ):
        result = backstory_health._run_backstory_health(threshold=50, account=None, dry_run=False, as_json=as_json)
    assert result == 0
    assert recovered_status.call_args.kwargs["outcome"] == "ok"
    session.close.assert_called_once_with()
    recovered_output = capsys.readouterr().out
    if as_json:
        assert json.loads(recovered_output)["outcome"] == "ok"
    else:
        assert recovered_output == ""


@pytest.mark.parametrize("as_json", [False, True])
def test_run_draft_queue_reports_and_propagates_config_error(as_json: bool, capsys: pytest.CaptureFixture[str]) -> None:
    error = ConfigError("mcp_mail_group is invalid")
    with (
        patch.object(draft_queue, "get_mcp_work_group", side_effect=error),
        patch.object(draft_queue, "_resolve_user_email", return_value="user@example.com"),
        patch.object(draft_queue, "watcher_logging"),
        patch.object(draft_queue, "write_run_status") as write_status,
        patch.object(draft_queue, "write_alerts") as write_alerts,
        patch("fieldkit.watch.morning_brief_mcp.MCPSession") as session_class,
        patch("time.monotonic", return_value=10.0),
        pytest.raises(ConfigError, match="mcp_mail_group") as caught,
    ):
        draft_queue._run_draft_queue(dry_run=False, as_json=as_json)
    assert caught.value is error
    session_class.assert_not_called()
    write_alerts.assert_not_called()
    write_status.assert_called_once_with(
        watcher="draft-queue",
        outcome="fatal",
        records_checked=0,
        alerts_generated=0,
        failures=1,
        elapsed_seconds=0.0,
        dry_run=False,
    )
    output = capsys.readouterr().out
    if as_json:
        assert json.loads(output) == {
            "watcher": "draft-queue",
            "outcome": "fatal",
            "records_checked": 0,
            "alerts_generated": 0,
            "failures": 1,
            "elapsed_seconds": 0.0,
            "dry_run": False,
        }
    else:
        assert output == ""


@pytest.mark.parametrize("as_json", [False, True])
def test_run_draft_queue_recovers_after_configuration_is_fixed(
    as_json: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    error = ConfigError("mcp_mail_group is invalid")
    with (
        patch.object(draft_queue, "get_mcp_work_group", side_effect=error),
        patch.object(draft_queue, "_resolve_user_email", return_value="user@example.com"),
        patch.object(draft_queue, "watcher_logging"),
        patch.object(draft_queue, "write_run_status"),
        pytest.raises(ConfigError, match="mcp_mail_group") as caught,
    ):
        draft_queue._run_draft_queue(dry_run=False)
    assert caught.value is error

    session = MagicMock()
    session.call_tool.return_value = []
    with (
        patch.object(draft_queue, "get_mcp_work_group", return_value="team-mail") as group_lookup,
        patch.object(draft_queue, "_get_mcp_gateway_base", return_value="https://mcp.example.com"),
        patch.object(draft_queue, "_resolve_user_email", return_value="user@example.com"),
        patch.object(draft_queue, "watcher_logging"),
        patch.object(draft_queue, "write_run_status") as write_status,
        patch.object(draft_queue, "write_alerts") as write_alerts,
        patch("fieldkit.watch.morning_brief_mcp.MCPSession", return_value=session) as session_class,
    ):
        result = draft_queue._run_draft_queue(dry_run=False, as_json=as_json)
    assert result == 0
    group_lookup.assert_called_once_with("mail")
    session_class.assert_called_once_with("https://mcp.example.com/v0/groups/team-mail/mcp")
    session.call_tool.assert_called_once_with(
        "google_workspace__search_gmail_messages",
        {"user_google_email": "user@example.com", "query": "in:drafts older_than:1d", "page_size": 50},
    )
    session.close.assert_called_once_with()
    write_alerts.assert_called_once_with([], dry_run=False)
    assert write_status.call_args.kwargs["outcome"] == "ok"
    output = capsys.readouterr().out
    if as_json:
        assert json.loads(output)["outcome"] == "ok"
    else:
        assert output == ""


def test_collect_calendar_meetings_uses_runtime_group_and_closes_session() -> None:
    meetings = [{"summary": "Acme planning"}]
    target_date = date(2026, 10, 1)
    with (
        patch.object(morning_brief, "get_mcp_work_group", return_value="team-calendar") as group_lookup,
        patch.object(morning_brief, "get_mcp_gateway_base", return_value="https://mcp.example.com"),
        patch.object(morning_brief, "MCPSession") as session_class,
        patch.object(morning_brief, "fetch_external_meetings", return_value=meetings) as fetch,
    ):
        result = morning_brief._collect_calendar_meetings(target_date, {"example.com"}, "user@example.com")
    assert result == meetings
    group_lookup.assert_called_once_with("calendar")
    session_class.assert_called_once_with("https://mcp.example.com/v0/groups/team-calendar/mcp")
    session_class.return_value.initialize.assert_called_once_with()
    fetch.assert_called_once_with(session_class.return_value, target_date, {"example.com"}, "user@example.com")
    session_class.return_value.close.assert_called_once_with()


def test_collect_calendar_meetings_handles_invalid_configuration_without_session() -> None:
    with (
        patch.object(morning_brief, "get_mcp_work_group", side_effect=ConfigError("invalid group")),
        patch.object(morning_brief, "MCPSession") as session_class,
    ):
        result = morning_brief._collect_calendar_meetings(date(2026, 10, 1), set(), "user@example.com")
    assert result == "_Calendar unavailable (invalid MCP configuration) — check logs for details_"
    session_class.assert_not_called()


def test_initialize_sales_session_propagates_authentication_failure() -> None:
    session = MagicMock()
    error = AuthError("authentication required")
    session.initialize.side_effect = error
    with pytest.raises(AuthError, match="authentication required") as caught:
        backstory_health._initialize_sales_session(session)
    assert caught.value is error
