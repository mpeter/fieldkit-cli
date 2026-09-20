"""Tests for tool_scope_guard.py PreToolUse hook.

Tests import main() directly, mocking sys.stdin with io.StringIO.
Verifies that banned MCP tools exit 2 and allowed tools exit 0.
"""

import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

# Ensure the scripts/ directory is importable
_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from hooks import tool_scope_guard  # noqa: E402


def _stdin(tool_name: str, tool_input: dict | None = None) -> io.StringIO:
    payload = {"tool_name": tool_name, "tool_input": tool_input or {}}
    return io.StringIO(json.dumps(payload))


# ── TestBannedTools (flattened) ─────────────────────────────────────────────


def test_banned_tools_blocks_get_opportunity_status(capsys: pytest.CaptureFixture[str]) -> None:
    tool = "mcp__fieldkit-sales__backstory__get_opportunity_status"
    with patch("sys.stdin", _stdin(tool)):
        result = tool_scope_guard.main()
    assert result == 2
    captured = capsys.readouterr()
    assert "BLOCKED" in captured.err
    assert tool in captured.err


def test_banned_tools_blocks_get_recent_opportunity_activity(capsys: pytest.CaptureFixture[str]) -> None:
    tool = "mcp__fieldkit-sales__backstory__get_recent_opportunity_activity"
    with patch("sys.stdin", _stdin(tool)):
        result = tool_scope_guard.main()
    assert result == 2
    captured = capsys.readouterr()
    assert "BLOCKED" in captured.err
    assert tool in captured.err


def test_banned_tools_block_message_includes_reason(capsys: pytest.CaptureFixture[str]) -> None:
    tool = "mcp__fieldkit-sales__backstory__get_opportunity_status"
    with patch("sys.stdin", _stdin(tool)):
        tool_scope_guard.main()
    captured = capsys.readouterr()
    assert "unreliable attribution" in captured.err
    assert "CLAUDE.md" in captured.err


# ── TestAllowedTools (flattened) ────────────────────────────────────────────


def test_allowed_tools_allows_find_account() -> None:
    with patch("sys.stdin", _stdin("mcp__fieldkit-sales__backstory__find_account")):
        result = tool_scope_guard.main()
    assert result == 0


def test_allowed_tools_allows_get_account_status() -> None:
    with patch("sys.stdin", _stdin("mcp__fieldkit-sales__backstory__get_account_status")):
        result = tool_scope_guard.main()
    assert result == 0


def test_allowed_tools_allows_get_recent_account_activity() -> None:
    with patch("sys.stdin", _stdin("mcp__fieldkit-sales__backstory__get_recent_account_activity")):
        result = tool_scope_guard.main()
    assert result == 0


def test_allowed_tools_allows_account_company_news() -> None:
    with patch("sys.stdin", _stdin("mcp__fieldkit-sales__backstory__account_company_news")):
        result = tool_scope_guard.main()
    assert result == 0


def test_allowed_tools_allows_gmail_draft() -> None:
    with patch("sys.stdin", _stdin("mcp__fieldkit-mail__google_workspace__draft_gmail_message")):
        result = tool_scope_guard.main()
    assert result == 0


def test_allowed_tools_allows_write_tool() -> None:
    with patch("sys.stdin", _stdin("Write", {"file_path": "some/file.md"})):
        result = tool_scope_guard.main()
    assert result == 0


def test_allowed_tools_allows_bash_tool() -> None:
    with patch("sys.stdin", _stdin("Bash", {"command": "ls"})):
        result = tool_scope_guard.main()
    assert result == 0


# ── TestEdgeCases (flattened) ───────────────────────────────────────────────


def test_edge_cases_invalid_json_exits_0() -> None:
    with patch("sys.stdin", io.StringIO("not json")):
        result = tool_scope_guard.main()
    assert result == 0


def test_edge_cases_missing_tool_name_exits_0() -> None:
    with patch("sys.stdin", io.StringIO(json.dumps({"tool_input": {}}))):
        result = tool_scope_guard.main()
    assert result == 0


def test_edge_cases_empty_tool_name_exits_0() -> None:
    with patch("sys.stdin", _stdin("")):
        result = tool_scope_guard.main()
    assert result == 0
