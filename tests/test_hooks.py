"""Standalone regression tests for hooks (S07 and later).

Covers:
1. validate_pursuit_frontmatter: parents[1] regression (schema path resolves correctly).
2. outbound_gate: try/except around json.load (invalid stdin → exit 0, not crash).
3. outbound_gate: calendar invite guard (manage_event action x attendees x send_updates).
4. outbound_gate: Salesforce write guard (--confirm set-next-steps / set-field).
5. pursuit_frontmatter_guard: extract_frontmatter and main() logic.

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

import hooks.validate_pursuit_frontmatter as vpf  # noqa: E402
from hooks import outbound_gate, pursuit_frontmatter_guard  # noqa: E402

# ===========================================================================
# 1. validate_pursuit_frontmatter — parents[1] regression
# ===========================================================================


# ── TestValidatePursuitFrontmatterParentsFix (flattened) ────────────────────


def test_validate_pursuit_frontmatter_schema_path_exists() -> None:
    """_schema_path() must resolve to a file that actually exists on disk."""
    # This is the canonical regression test: if parents index was wrong,
    # the schema would not be found and this assertion fails.
    schema = vpf._schema_path()
    assert schema.exists(), f"Schema not found at {schema!r}. parents[1] regression may have been reintroduced."


def test_validate_pursuit_frontmatter_schema_path_points_into_config() -> None:
    """_schema_path() must live inside the repo's _data/ directory."""
    schema = vpf._schema_path()
    assert schema.name == "pursuit-frontmatter.schema.json"
    assert schema.parent.name == "_data"


def test_validate_pursuit_frontmatter_repo_root_is_cwd_ancestor() -> None:
    """_repo_root() must be an ancestor of the current working directory."""
    repo_root = vpf._repo_root()
    # Both should share a common ancestor path — repo_root must be a
    # parent of the hooks/ file itself.
    assert repo_root == ROOT, (
        f"_repo_root() returned {repo_root!r}; expected {ROOT!r}. parents[1] fix may have regressed."
    )


def test_validate_pursuit_frontmatter_schema_is_valid_json() -> None:
    """The schema file must be valid JSON (sanity check after path fix)."""
    schema_text = vpf._schema_path().read_text(encoding="utf-8")
    parsed = json.loads(schema_text)
    assert isinstance(parsed, dict)
    assert "$schema" in parsed or "type" in parsed


def test_validate_pursuit_frontmatter_validate_file_on_valid_pursuit(tmp_path: Path) -> None:
    """validate_file() returns no errors for a minimal valid pursuit fixture."""
    schema = json.loads(vpf._schema_path().read_text(encoding="utf-8"))

    valid_md = tmp_path / "test-pursuit.md"
    # Use schema-compliant values: hyphenated meddpicc keys, valid gate-status enum.
    valid_md.write_text(
        "---\n"
        "stage: discover\n"
        "gate-status: pending\n"
        "meddpicc:\n"
        "  metrics: 0\n"
        "  economic-buyer: 0\n"
        "  decision-criteria: 0\n"
        "  decision-process: 0\n"
        "  identify-pain: 0\n"
        "  paper-process: 0\n"
        "  champion: 0\n"
        "  competition: 0\n"
        "---\n\n"
        "# Pursuit Notes\n",
        encoding="utf-8",
    )

    errors = vpf.validate_file(valid_md, schema)
    assert errors == [], f"Unexpected errors on valid pursuit: {errors}"


def test_validate_pursuit_frontmatter_validate_file_missing_required_fields(tmp_path: Path) -> None:
    """validate_file() returns errors when required frontmatter fields are absent."""
    schema = json.loads(vpf._schema_path().read_text(encoding="utf-8"))

    invalid_md = tmp_path / "bad-pursuit.md"
    invalid_md.write_text(
        "---\nsome_field: value\n---\n\n# Notes\n",
        encoding="utf-8",
    )

    errors = vpf.validate_file(invalid_md, schema)
    assert len(errors) > 0, "Expected validation errors for missing required fields"
    # At least one error should mention a required field
    combined = " ".join(errors)
    assert any(field in combined for field in ("stage", "gate-status", "meddpicc")), (
        f"Expected required-field errors, got: {errors}"
    )


# ===========================================================================
# 2. outbound_gate — try/except around json.load
# ===========================================================================


def _run_outbound(stdin_content: str) -> int:
    """Run outbound_gate.main() with synthetic stdin, return exit code."""
    with patch("sys.stdin", io.StringIO(stdin_content)):
        return int(outbound_gate.main())


# ── TestOutboundGateJsonCrashFix (flattened) ────────────────────────────────


def test_outbound_gate_invalid_json_returns_0() -> None:
    """Completely invalid JSON must not crash — returns 0 (allow)."""
    assert _run_outbound("invalid json {{{") == 0


def test_outbound_gate_empty_stdin_returns_0() -> None:
    """Empty stdin is also not valid JSON — must return 0."""
    assert _run_outbound("") == 0


def test_outbound_gate_partial_json_returns_0() -> None:
    """Truncated JSON object must return 0 (allow)."""
    assert _run_outbound('{"tool_name": "Read"') == 0


def test_outbound_gate_json_array_instead_of_object_returns_0() -> None:
    """JSON array (not object) at top level — returns 0."""
    assert _run_outbound("[1, 2, 3]") == 0


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
    assert "person@acme-corp.com" not in captured.err


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
    attendees: list | None = None,
    send_updates: str | None = None,
) -> str:
    tool_input: dict = {"action": action}
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


# ===========================================================================
# 5. pursuit_frontmatter_guard — extract_frontmatter and main()
# ===========================================================================


def _run_pfg(payload: dict) -> int:
    """Run pursuit_frontmatter_guard.main() with synthetic stdin."""
    with patch("sys.stdin", io.StringIO(json.dumps(payload))):
        return int(pursuit_frontmatter_guard.main())


# ── extract_frontmatter (pure function) ─────────────────────────────────────


def test_extract_frontmatter_valid_block() -> None:
    content = "---\nsf_foo: bar\n---\n# body"
    assert pursuit_frontmatter_guard.extract_frontmatter(content) == "sf_foo: bar"


def test_extract_frontmatter_no_delimiters_returns_empty() -> None:
    assert pursuit_frontmatter_guard.extract_frontmatter("# just a heading\nno front") == ""


def test_extract_frontmatter_only_one_delimiter_returns_content_after_it() -> None:
    # With only one --- the function enters frontmatter mode and collects until EOF.
    # The hook's main() then runs the sf- key check on that content — which is the
    # safe/conservative behaviour (better to check than to silently skip).
    result = pursuit_frontmatter_guard.extract_frontmatter("---\nkey: val\n")
    assert result == "key: val"


# ── main() ───────────────────────────────────────────────────────────────────

_PURSUIT_PATH = "accounts/acme-corp/pursuits/q3-expansion.md"
_NON_PURSUIT_PATH = "accounts/acme-corp/notes.md"

_WRITE_HYPHENATED = {
    "tool_name": "Write",
    "tool_input": {
        "file_path": _PURSUIT_PATH,
        "content": "---\nsf-account-name: Acme\n---\n# body",
    },
}


def test_pfg_non_pursuit_path_allowed() -> None:
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": _NON_PURSUIT_PATH,
            "content": "---\nsf-foo: bar\n---\n",
        },
    }
    assert _run_pfg(payload) == 0


def test_pfg_non_write_tool_allowed() -> None:
    payload = {"tool_name": "Read", "tool_input": {"file_path": _PURSUIT_PATH}}
    assert _run_pfg(payload) == 0


def test_pfg_invalid_json_allowed() -> None:
    with patch("sys.stdin", io.StringIO("not json")):
        assert pursuit_frontmatter_guard.main() == 0


def test_pfg_write_hyphenated_sf_key_blocked(capsys: pytest.CaptureFixture[str]) -> None:
    """Write with sf- hyphenated key in frontmatter → blocked, stderr shows correction."""
    assert _run_pfg(_WRITE_HYPHENATED) == 2
    err = capsys.readouterr().err
    assert "BLOCKED" in err
    assert "sf-account-name" in err
    assert "sf_account_name" in err


def test_pfg_edit_hyphenated_sf_key_blocked() -> None:
    payload = {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": _PURSUIT_PATH,
            "new_string": "---\nsf-stage: discover\n---\n",
        },
    }
    assert _run_pfg(payload) == 2


def test_pfg_multiedit_hyphenated_sf_key_blocked() -> None:
    payload = {
        "tool_name": "MultiEdit",
        "tool_input": {
            "file_path": _PURSUIT_PATH,
            "edits": [{"new_string": "---\nsf-owner: Matt\n---\n"}],
        },
    }
    assert _run_pfg(payload) == 2


def test_pfg_write_underscored_sf_key_allowed() -> None:
    """Correctly underscored sf_ keys must not be blocked."""
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": _PURSUIT_PATH,
            "content": "---\nsf_account_name: Acme\nstage: discover\n---\n# body",
        },
    }
    assert _run_pfg(payload) == 0


# ===========================================================================
# 6. Subprocess-level regression: the REAL entry point .claude/settings.json
#    invokes ("python3 hooks/<name>.py" as a direct script run, not a module
#    import). Every test above imports the hook module directly, which puts
#    the repo root on sys.path via pytest's own conftest machinery -- it
#    cannot detect a `hooks._common` import that only breaks under direct
#    script execution (sys.path[0] becomes hooks/ itself, not its parent).
#    docs/hooks-and-skills.md documents `python3 hooks/outbound_gate.py` as
#    the literal command each PreToolUse hook is registered with.
# ===========================================================================

import subprocess  # noqa: E402


def _run_hook_subprocess(hook_relpath: str, payload: dict) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
    """Invoke a hook exactly as .claude/settings.json does: `python3 hooks/<name>.py`
    with the repo root as cwd, feeding the payload as stdin JSON."""
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
    ["hooks/outbound_gate.py", "hooks/pursuit_frontmatter_guard.py", "hooks/tool_scope_guard.py"],
)
def test_hook_runs_as_real_script_entry_point(hook_relpath: str) -> None:
    """Regression guard: each PreToolUse hook must be importable/runnable via the
    exact `python3 hooks/<name>.py` invocation .claude/settings.json uses -- not
    just via `from hooks import <name>` (which pytest's own sys.path setup masks).
    A hooks/_common cross-module import that only resolves under module-import
    semantics would crash here with ModuleNotFoundError."""
    result = _run_hook_subprocess(hook_relpath, {"tool_name": "Read", "tool_input": {}})
    assert result.returncode in (0, 2), (
        f"{hook_relpath} crashed when invoked the way .claude/settings.json actually "
        f"invokes it (exit {result.returncode}):\n{result.stderr}"
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
