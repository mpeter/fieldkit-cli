"""Standalone regression tests for hooks (S07 and later).

Covers:
2. outbound_gate: malformed input blocks without exposing the event contents.
3. outbound_gate: calendar invite guard (manage_event action x attendees x send_updates).
4. outbound_gate: Salesforce write guard (--confirm set-next-steps / set-field).

All tests import the hook modules directly and use monkeypatching / io.StringIO
to avoid subprocess calls or filesystem access to vault data.
"""

import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Ensure hooks/ is importable (mirrors conftest approach)
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = ROOT / "hooks"
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

from hooks import outbound_gate  # noqa: E402

# ===========================================================================
# 2. outbound_gate — try/except around json.load
# ===========================================================================


def _run_outbound(stdin_content: str) -> int:
    """Run outbound_gate.main() with synthetic stdin, return exit code."""
    with patch("sys.stdin", io.StringIO(stdin_content)):
        return int(outbound_gate.main())


# ── TestOutboundGateJsonCrashFix (flattened) ────────────────────────────────


@pytest.mark.parametrize(
    "event",
    [
        "invalid json {{{",
        "",
        '{"tool_name": "Read"',
        "[1, 2, 3]",
        "null",
        "{}",
        '{"tool_name": null, "tool_input": {}}',
        '{"tool_name": 7, "tool_input": {}}',
        '{"tool_name": "", "tool_input": {}}',
        '{"tool_name": "Read"}',
        '{"tool_name": "Read", "tool_input": []}',
        '{"tool_name": "Bash", "tool_input": {}}',
        '{"tool_name": "Bash", "tool_input": {"command": null}}',
        '{"tool_name": "Bash", "tool_input": {"command": 7}}',
    ],
)
def test_outbound_gate_malformed_event_blocks(event: str, capsys: pytest.CaptureFixture[str]) -> None:
    """Uninspectable events block with a fixed diagnostic, never their contents."""
    result = _run_outbound(event)

    assert result == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "BLOCKED: Invalid outbound hook event; tool execution cannot be evaluated.\n"


def test_outbound_gate_gmail_send_blocked() -> None:
    """Gmail send tool must be blocked (exit 2)."""
    payload = json.dumps(
        {
            "tool_name": "mcp__fieldkit-mail__google_workspace__send_gmail_message",
            "tool_input": {},
        }
    )
    assert _run_outbound(payload) == 2


def test_outbound_gate_gmail_send_mcpjungle_blocked() -> None:
    """Alternate mcpjungle Gmail send tool must also be blocked (exit 2)."""
    payload = json.dumps(
        {
            "tool_name": "mcp__mcpjungle__google_workspace__send_gmail_message",
            "tool_input": {},
        }
    )
    assert _run_outbound(payload) == 2


def test_outbound_gate_read_tool_allowed() -> None:
    """Read tool is not an outbound action — must return 0."""
    payload = json.dumps(
        {
            "tool_name": "Read",
            "tool_input": {"file_path": "accounts/globalpay/account.md"},
        }
    )
    assert _run_outbound(payload) == 0


def test_outbound_gate_bash_slackcli_send_blocked() -> None:
    """Bash tool running slackcli messages send must be blocked (exit 2)."""
    payload = json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "slackcli messages send #general 'hello'"},
        }
    )
    assert _run_outbound(payload) == 2


def test_outbound_gate_bash_non_send_allowed() -> None:
    """Bash tool running a non-send command must return 0."""
    payload = json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "ls -la"},
        }
    )
    assert _run_outbound(payload) == 0


@pytest.mark.parametrize(
    "command",
    [
        "gh issue create --title 'Safe title' --body 'Safe placeholder text'",
        "fieldkit issue create --type bug --title 'Safe title' --body 'Safe placeholder text'",
    ],
)
def test_outbound_gate_bash_safe_publication_command_allowed(command: str) -> None:
    """Inspectable safe GitHub publication commands must be allowed."""
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})

    result = _run_outbound(payload)

    assert result == 0


@pytest.mark.parametrize(
    "command",
    [
        "gh issue comment 7 --body 'person@example.com'",
        "fieldkit issue note historic regression 'person@example.com'",
    ],
)
def test_outbound_gate_bash_unsafe_publication_command_blocks_category_only(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unsafe publication diagnostics expose only the fixed category and source."""
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})

    result = _run_outbound(payload)
    captured = capsys.readouterr()

    assert result == 2
    assert captured.err == "BLOCKED: GitHub publication blocked: category=personal_email source=body\n"
    assert command not in captured.err
    assert "person@acme-corp.example.com" not in captured.err


@pytest.mark.parametrize(
    "command",
    [
        "git status --short",
        "gh pr edit 7 --title 'Safe title'",
    ],
)
def test_outbound_gate_bash_non_target_or_metadata_command_allowed(command: str) -> None:
    """Non-target and metadata-only GitHub operations remain allowed."""
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})

    result = _run_outbound(payload)

    assert result == 0


def test_outbound_gate_unknown_tool_allowed() -> None:
    """Unknown/unrecognized tool names must pass through (exit 0)."""
    payload = json.dumps(
        {
            "tool_name": "SomeFutureTool",
            "tool_input": {},
        }
    )
    assert _run_outbound(payload) == 0


# ===========================================================================
# 3. outbound_gate — calendar invite guard
# ===========================================================================

_CAL_FIELDKIT = "mcp__fieldkit-calendar__google_workspace__manage_event"
_CAL_JUNGLE = "mcp__mcpjungle__google_workspace__manage_event"
_ATTENDEES = ["person@example.com"]


def _cal_payload(
    tool: str,
    action: str,
    attendees: list[str] | None = None,
    send_updates: str | None = None,
) -> str:
    tool_input: dict[str, object] = {"action": action}
    if attendees is not None:
        tool_input["attendees"] = attendees
    if send_updates is not None:
        tool_input["send_updates"] = send_updates
    return json.dumps({"tool_name": tool, "tool_input": tool_input})


def test_calendar_create_with_attendees_fieldkit_blocked() -> None:
    """fieldkit-calendar manage_event create + attendees → blocked."""
    assert _run_outbound(_cal_payload(_CAL_FIELDKIT, "create", _ATTENDEES)) == 2


def test_calendar_create_with_attendees_mcpjungle_blocked() -> None:
    """mcpjungle manage_event create + attendees → blocked."""
    assert _run_outbound(_cal_payload(_CAL_JUNGLE, "create", _ATTENDEES)) == 2


def test_calendar_create_with_attendees_send_updates_none_allowed() -> None:
    """manage_event create + attendees + send_updates=none → allowed (no notification)."""
    assert _run_outbound(_cal_payload(_CAL_FIELDKIT, "create", _ATTENDEES, "none")) == 0


def test_calendar_create_with_attendees_send_updates_none_uppercase_allowed() -> None:
    """send_updates='None' (capital N) must also be treated as no-notification."""
    assert _run_outbound(_cal_payload(_CAL_FIELDKIT, "create", _ATTENDEES, "None")) == 0


def test_calendar_create_no_attendees_allowed() -> None:
    """manage_event create with no attendees → allowed (nobody to notify)."""
    assert _run_outbound(_cal_payload(_CAL_FIELDKIT, "create", [])) == 0


def test_calendar_delete_action_allowed() -> None:
    """manage_event delete is not in the blocked action set → allowed."""
    assert _run_outbound(_cal_payload(_CAL_FIELDKIT, "delete", _ATTENDEES)) == 0


def test_calendar_update_with_attendees_blocked() -> None:
    """manage_event update + attendees → blocked."""
    assert _run_outbound(_cal_payload(_CAL_FIELDKIT, "update", _ATTENDEES)) == 2


def test_calendar_rsvp_with_attendees_blocked() -> None:
    """manage_event rsvp + attendees → blocked."""
    assert _run_outbound(_cal_payload(_CAL_JUNGLE, "rsvp", _ATTENDEES)) == 2


# ===========================================================================
# 4. outbound_gate — Salesforce write guard (previously untested)
# ===========================================================================


def test_outbound_gate_bash_sf_set_field_confirm_blocked() -> None:
    """Bash --confirm set-field must be blocked (autonomous SF write)."""
    payload = json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "fieldkit sf set-field --confirm opp123 ACV__c 50000"},
        }
    )
    assert _run_outbound(payload) == 2


def test_outbound_gate_bash_sf_set_next_steps_confirm_blocked() -> None:
    """Bash --confirm set-next-steps must be blocked (autonomous SF write)."""
    payload = json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "fieldkit sf set-next-steps --confirm opp123 'follow up'"},
        }
    )
    assert _run_outbound(payload) == 2


def test_outbound_gate_bash_sf_no_confirm_allowed() -> None:
    """Bash sf command without --confirm is not an autonomous write → allowed."""
    payload = json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "fieldkit sf set-next-steps opp123 'follow up'"},
        }
    )
    assert _run_outbound(payload) == 0


# Direct-script import contract for the remaining optional adapters.
# No repository-owned Claude settings bind these scripts automatically.

import subprocess  # noqa: E402


def _run_hook_subprocess(hook_relpath: str, payload: dict) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
    """Invoke an optional adapter directly with a synthetic stdin event."""
    return subprocess.run(
        [sys.executable, hook_relpath],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=10,
        check=False,
    )


@pytest.mark.parametrize(
    "hook_relpath",
    ["hooks/outbound_gate.py", "hooks/tool_scope_guard.py"],
)
def test_hook_runs_as_real_script_entry_point(hook_relpath: str) -> None:
    """Optional adapters must work as scripts, not only imported modules."""
    result = _run_hook_subprocess(hook_relpath, {"tool_name": "Read", "tool_input": {}})
    assert result.returncode in (0, 2), (
        f"{hook_relpath} crashed as a direct script (exit {result.returncode}):\n{result.stderr}"
    )
    assert "ModuleNotFoundError" not in result.stderr


def test_outbound_gate_subprocess_blocks_calendar_invite() -> None:
    """End-to-end: the calendar-invite guard actually blocks via the real entry point."""
    payload = {
        "tool_name": "mcp__fieldkit-calendar__google_workspace__manage_event",
        "tool_input": {"action": "create", "attendees": ["a@b.example.com"]},
    }
    result = _run_hook_subprocess("hooks/outbound_gate.py", payload)
    assert result.returncode == 2, f"expected block (exit 2), got {result.returncode}:\n{result.stderr}"
    assert "BLOCKED" in result.stderr


def test_outbound_gate_send_updates_non_string_does_not_crash() -> None:
    """A non-string send_updates value (e.g. a dict from a malformed/adversarial
    payload) must not crash the guard with an unhandled AttributeError."""
    payload = {
        "tool_name": "mcp__fieldkit-calendar__google_workspace__manage_event",
        "tool_input": {"action": "create", "attendees": ["a@b.example.com"], "send_updates": {"weird": "dict"}},
    }
    assert _run_outbound(json.dumps(payload)) == 2


def test_tool_input_non_dict_value_coerced_to_empty_dict() -> None:
    """hooks._common.tool_input()'s 'guaranteed dict' docstring claim must hold
    even when the payload's tool_input field is a non-dict truthy value."""
    from hooks._common import tool_input

    assert tool_input({"tool_input": "not-a-dict"}) == {}
    assert tool_input({"tool_input": ["a", "list"]}) == {}
    assert tool_input({"tool_input": {"a": 1}}) == {"a": 1}
    assert tool_input({}) == {}
