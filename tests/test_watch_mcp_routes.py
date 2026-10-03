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
    ):
        result = backstory_health._open_mcp_session()
    assert result is None
    session_class.assert_not_called()


def test_mail_session_is_not_opened_without_group() -> None:
    from fieldkit.watch import draft_queue

    with (
        patch.object(draft_queue, "get_mcp_work_group", return_value=None),
        patch.object(draft_queue, "_resolve_user_email", return_value="user@example.com"),
        patch.object(draft_queue, "watcher_logging"),
        patch.object(draft_queue, "write_run_status"),
        patch("fieldkit.watch.morning_brief_mcp.MCPSession") as session_class,
    ):
        result = draft_queue._run_draft_queue(dry_run=False)
    assert result == 1
    session_class.assert_not_called()


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
