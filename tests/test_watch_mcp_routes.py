"""Validate configured MCP group names used by watchers."""

import os
import subprocess
import sys
from pathlib import Path
from typing import Literal
from unittest.mock import patch

import pytest

from fieldkit.config import ConfigError, _loader, get_mcp_work_group

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("service", ["calendar", "mail", "sales"])
def test_work_group_is_unconfigured_when_missing(service: Literal["calendar", "mail", "sales"]) -> None:
    with patch("fieldkit.config._integrations._load_mcp_gateway_config", return_value=None):
        result = get_mcp_work_group(service)
    assert result is None


def test_work_group_is_unconfigured_when_null() -> None:
    with patch("fieldkit.config._integrations._load_mcp_gateway_config", return_value={"mcp_mail_group": None}):
        result = get_mcp_work_group("mail")
    assert result is None


@pytest.mark.parametrize("service", ["calendar", "mail", "sales"])
def test_work_group_uses_configured_name(service: Literal["calendar", "mail", "sales"]) -> None:
    with patch(
        "fieldkit.config._integrations._load_mcp_gateway_config", return_value={f"mcp_{service}_group": "work-group"}
    ):
        result = get_mcp_work_group(service)
    assert result == "work-group"


@pytest.mark.parametrize("value", ["", "../other", "work/group", 42])
def test_work_group_rejects_invalid_name(value: object) -> None:
    with (
        patch("fieldkit.config._integrations._load_mcp_gateway_config", return_value={"mcp_mail_group": value}),
        pytest.raises(ConfigError, match="mcp_mail_group"),
    ):
        get_mcp_work_group("mail")


def test_work_group_rejects_wrong_type_from_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("mcp_mail_group: 42\n", encoding="utf-8")
    monkeypatch.setattr(_loader, "CONFIG_PATH", config_path)
    with pytest.raises(ConfigError, match="mcp_mail_group"):
        get_mcp_work_group("mail")


@pytest.mark.parametrize("config_text", ["mcp_calendar_group: bad/group\n", "mcp_calendar_group: [oops\n"])
def test_invalid_calendar_config_does_not_break_watch_import(tmp_path: Path, config_text: str) -> None:
    config_dir = tmp_path / "fieldkit"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(config_text, encoding="utf-8")
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-c", "import fieldkit.commands.watch.cli; import fieldkit.watch.morning_brief"],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_sales_session_uses_configured_group() -> None:
    from fieldkit.watch import backstory_health

    with (
        patch.object(backstory_health, "get_mcp_work_group", return_value="work-sales"),
        patch.object(backstory_health, "_get_mcp_gateway_base", return_value="http://127.0.0.1:8080"),
        patch.object(backstory_health, "MCPSession") as session_class,
    ):
        result = backstory_health._open_mcp_session()
    assert result is session_class.return_value
    session_class.assert_called_once_with("http://127.0.0.1:8080/v0/groups/work-sales/mcp")


def test_sales_session_is_not_opened_without_group() -> None:
    from fieldkit.watch import backstory_health

    with (
        patch.object(backstory_health, "get_mcp_work_group", return_value=None),
        patch.object(backstory_health, "MCPSession") as session_class,
        pytest.raises(ConfigError, match="mcp_sales_group"),
    ):
        backstory_health._open_mcp_session()
    session_class.assert_not_called()


@pytest.mark.parametrize(
    "group_error", [None, ConfigError("Config key 'mcp_sales_group' must be a valid MCP group name")]
)
def test_backstory_health_records_fatal_run_for_unusable_group(
    group_error: ConfigError | None, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from fieldkit.watch import backstory_health

    lookup = {"side_effect": group_error} if group_error else {"return_value": None}
    with (
        patch.object(backstory_health, "get_mcp_work_group", **lookup),
        patch.object(backstory_health, "_load_and_filter_accounts", return_value={"acme": {}}),
        patch.object(backstory_health, "load_state", return_value={}),
        patch.object(backstory_health, "watcher_logging"),
        patch.object(backstory_health, "write_run_status") as write_status,
        pytest.raises(ConfigError, match="mcp_sales_group"),
    ):
        backstory_health._run_backstory_health(threshold=50, account=None, dry_run=False, as_json=True)
    assert write_status.call_args.kwargs["outcome"] == "fatal"
    assert json.loads(capsys.readouterr().out)["outcome"] == "fatal"


@pytest.mark.parametrize(
    "group_error", [None, ConfigError("Config key 'mcp_mail_group' must be a valid MCP group name")]
)
def test_mail_session_is_not_opened_for_unusable_group(
    group_error: ConfigError | None, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from fieldkit.watch import draft_queue

    lookup = {"side_effect": group_error} if group_error else {"return_value": None}
    with (
        patch.object(draft_queue, "get_mcp_work_group", **lookup),
        patch.object(draft_queue, "_resolve_user_email", return_value="user@example.com"),
        patch.object(draft_queue, "watcher_logging"),
        patch("fieldkit.watch.morning_brief_mcp.MCPSession") as session_class,
        patch.object(draft_queue, "write_run_status") as write_status,
        pytest.raises(ConfigError, match="mcp_mail_group"),
    ):
        draft_queue._run_draft_queue(dry_run=False, as_json=True)
    assert write_status.call_args.kwargs["outcome"] == "fatal"
    assert json.loads(capsys.readouterr().out)["outcome"] == "fatal"
    session_class.assert_not_called()


def test_run_all_keeps_watcher_config_errors_as_data_errors() -> None:
    from fieldkit.cli_exit import EXIT_DATA
    from fieldkit.commands.watch import cli as watch_cli

    def unusable_group() -> int:
        raise ConfigError("Config key 'mcp_mail_group' must be a valid MCP group name")

    rc = watch_cli._invoke_watcher("draft-queue", unusable_group)
    assert rc == EXIT_DATA
    assert watch_cli._run_all_exit_code(rc, allow_partial=True) == EXIT_DATA


def test_calendar_session_is_not_opened_without_group() -> None:
    from datetime import date

    from fieldkit.watch import morning_brief

    with (
        patch.object(morning_brief, "get_mcp_work_group", return_value=None),
        patch.object(morning_brief, "MCPSession") as session_class,
    ):
        result = morning_brief._collect_calendar_meetings(date(2026, 10, 1), set(), "user@example.com")
    assert isinstance(result, str)
    assert "mcp_calendar_group" in result
    session_class.assert_not_called()
