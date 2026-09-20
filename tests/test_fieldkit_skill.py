"""Smoke tests for the fieldkit skill Click group.

Tests use CliRunner against `fieldkit.skill.cli` directly (same pattern as
test_fieldkit_groups.py). The skill _runner uses @functools.cache for
_load_all_skills(); in-process CliRunner invocations share the same cache,
which is fine — no patching needed.
"""

import json
import typing
from pathlib import Path

import pytest
from click.testing import CliRunner

from fieldkit.commands.skill._runner import _cmd_list, _cmd_variables
from fieldkit.commands.skill.cli import cli as skill_cli

pytestmark = pytest.mark.unit


# ── TestSkillGroup (flattened) ──────────────────────────────────────────────


def test_load_skill_skill_help() -> None:
    """skill --help exits 0 and lists both subcommands."""
    result = CliRunner().invoke(skill_cli, ["--help"])
    assert result.exit_code == 0, result.output
    assert "list" in result.output
    assert "show" in result.output


def test_load_skill_skill_h_flag() -> None:
    """skill -h exits 0 (help_option_names includes -h)."""
    result = CliRunner().invoke(skill_cli, ["-h"])
    assert result.exit_code == 0, result.output


def test_load_skill_skill_no_subcommand() -> None:
    """Invoking skill with no args exits non-zero (invoke_without_command guard)."""
    result = CliRunner().invoke(skill_cli, [])
    assert result.exit_code != 0, f"Expected non-zero, got {result.exit_code}"


def test_load_skill_skill_list() -> None:
    """skill list exits 0 and prints at least one skill name."""
    result = CliRunner().invoke(skill_cli, ["list"])
    assert result.exit_code == 0, result.output
    # Output should contain at least one known skill name
    assert "sf-sync" in result.output or "tool-routing" in result.output or "NAME" in result.output


def test_load_skill_skill_list_json() -> None:
    """skill list --json exits 0 and output is valid JSON list."""
    result = CliRunner().invoke(skill_cli, ["list", "--json"])
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert isinstance(parsed, list)
    assert len(parsed) > 0, "Expected at least one skill in JSON output"


def test_load_skill_skill_show_existing() -> None:
    """skill show sf-sync exits 0 for a known skill."""
    result = CliRunner().invoke(skill_cli, ["show", "sf-sync"])
    assert result.exit_code == 0, result.output


def test_load_skill_skill_show_nonexistent() -> None:
    """skill show zzz-nonexistent-skill-zzz exits non-zero."""
    result = CliRunner().invoke(skill_cli, ["show", "zzz-nonexistent-skill-zzz"])
    assert result.exit_code != 0, f"Expected non-zero, got {result.exit_code}"


def test_load_skill_skill_unknown_subcommand() -> None:
    """An unrecognised subcommand exits non-zero."""
    result = CliRunner().invoke(skill_cli, ["bogus-subcmd"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# historic regression: skill show Usage line must include "show" subcommand
# ---------------------------------------------------------------------------


# ── TestBug161SkillShowUsageLine (flattened) ────────────────────────────────


def test_cmd_show_show_no_usage_line_when_argument_hint_set(capsys: pytest.CaptureFixture[str]) -> None:
    """_render_show_human must NOT print a Usage line even when argument_hint is set."""
    from fieldkit.commands.skill._runner import _render_show_human

    skill = {
        "name": "sf-sync",
        "version": "1.0",
        "description": "Sync Salesforce data.",
        "argument_hint": "[--account NAME]",
        "user_invocable": True,
        "groups_needed": "",
        "has_evals": False,
        "path": "/fake/path/SKILL.md",
    }
    _render_show_human(skill)

    output = capsys.readouterr().out
    # historic regression: Usage line must be absent regardless of argument_hint value
    assert "Usage:" not in output, f"Expected no 'Usage:' line in output, got:\n{output}"


def test_cmd_show_show_usage_line_absent_when_no_argument_hint(capsys: pytest.CaptureFixture[str]) -> None:
    """When argument_hint is empty, no Usage line is printed at all."""
    from fieldkit.commands.skill._runner import _render_show_human

    skill = {
        "name": "sf-sync",
        "version": "",
        "description": "Sync Salesforce data.",
        "argument_hint": "",  # no hint → no Usage line
        "user_invocable": True,
        "groups_needed": "",
        "has_evals": False,
        "path": "/fake/path/SKILL.md",
    }
    _render_show_human(skill)

    output = capsys.readouterr().out
    assert "Usage:" not in output


# ---------------------------------------------------------------------------
# implementation change — skill list shows description snippet for skills without evals
# ---------------------------------------------------------------------------


# ── TestEnh172SkillListDescription (flattened) ──────────────────────────────


def _cmd_list_make_skill_dir(tmp_path: Path, name: str, description: str, has_evals: bool) -> Path:
    """Create a minimal skill directory for testing."""
    skill_dir = tmp_path / name
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    if has_evals:
        evals_dir = skill_dir / "evals"
        evals_dir.mkdir()
        (evals_dir / "evals.json").write_text("[]", encoding="utf-8")
    return skill_dir


def test_cmd_list_skill_list_shows_description_for_skill_without_evals(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """implementation change: a skill without evals must show description snippet in list output."""
    from fieldkit.commands.skill._runner import _load_skill, _render_list_human

    skill_dir = _cmd_list_make_skill_dir(
        tmp_path,
        "my-skill",
        "Sync data from Salesforce. More detail here.",
        has_evals=False,
    )
    skill = _load_skill(skill_dir)
    assert skill is not None

    _render_list_human([skill])

    output = capsys.readouterr().out
    # Description first sentence must appear in the output
    assert "Sync data from Salesforce" in output, (
        f"Expected description snippet in list output for skill without evals:\n{output}"
    )


def test_cmd_list_skill_list_shows_evals_marker_for_skill_with_evals(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """implementation change: a skill with evals must show the evals marker (✓) in list output."""
    from fieldkit.commands.skill._runner import _load_skill, _render_list_human

    skill_dir = _cmd_list_make_skill_dir(
        tmp_path,
        "eval-skill",
        "A skill with evals. More detail.",
        has_evals=True,
    )
    skill = _load_skill(skill_dir)
    assert skill is not None

    _render_list_human([skill])

    output = capsys.readouterr().out
    assert "✓" in output, f"Expected evals marker (✓) in list output for skill with evals:\n{output}"


def _verbose_list_skills() -> list[dict[str, typing.Any]]:
    return [
        {
            "name": "long-skill",
            "description": (
                "Find the right account signal quickly across every configured source and connected workspace. "
                "Include the complete second sentence for routing detail."
            ),
            "groups_needed": "sales",
            "has_evals": False,
        },
        {
            "name": "other-skill",
            "description": "Handle an unrelated workflow. Preserve this detail too.",
            "groups_needed": "operations",
            "has_evals": True,
        },
    ]


@pytest.mark.parametrize("flag", ["--verbose", "-v"])
def test_skill_list_verbose_shows_complete_description(monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
    import fieldkit.commands.skill._runner as runner

    skills = _verbose_list_skills()
    monkeypatch.setattr(runner, "_load_all_skills", lambda: skills)

    result = CliRunner().invoke(skill_cli, ["list", flag])

    assert result.exit_code == 0
    assert skills[0]["description"] in result.output


def test_skill_list_default_keeps_compact_description(monkeypatch: pytest.MonkeyPatch) -> None:
    import fieldkit.commands.skill._runner as runner

    skills = _verbose_list_skills()
    monkeypatch.setattr(runner, "_load_all_skills", lambda: skills)

    result = CliRunner().invoke(skill_cli, ["list"])

    assert result.exit_code == 0
    assert "…" in result.output
    assert "Include the complete second sentence" not in result.output


def test_skill_list_verbose_composes_with_group_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    import fieldkit.commands.skill._runner as runner

    skills = _verbose_list_skills()
    monkeypatch.setattr(runner, "_load_all_skills", lambda: skills)

    result = CliRunner().invoke(skill_cli, ["list", "-v", "--group", "sales"])

    assert result.exit_code == 0
    assert skills[0]["description"] in result.output
    assert "other-skill" not in result.output


def test_skill_list_verbose_does_not_change_json(monkeypatch: pytest.MonkeyPatch) -> None:
    import fieldkit.commands.skill._runner as runner

    skills = _verbose_list_skills()
    monkeypatch.setattr(runner, "_load_all_skills", lambda: skills)

    default_json = CliRunner().invoke(skill_cli, ["list", "--json"])
    verbose_json = CliRunner().invoke(skill_cli, ["list", "--json", "--verbose"])

    assert default_json.exit_code == 0
    assert verbose_json.exit_code == 0
    assert json.loads(verbose_json.output) == json.loads(default_json.output)


@pytest.mark.parametrize("verbose", [False, True])
def test_render_skill_list_tolerates_empty_description(capsys: pytest.CaptureFixture[str], verbose: bool) -> None:
    from fieldkit.commands.skill._runner import _render_list_human

    skill = {"name": "empty-skill", "description": None, "has_evals": False}

    result = _render_list_human([skill], verbose=verbose)

    assert result is None
    assert "empty-skill" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# implementation change — skill show displays argument_hint
# ---------------------------------------------------------------------------


# ── TestEnh265ArgumentHint (flattened) ──────────────────────────────────────


def _enh265_argument_hint_make_skill(argument_hint: str, version: str = "") -> dict:
    return {
        "name": "test-skill",
        "version": version,
        "description": "A test skill.",
        "argument_hint": argument_hint,
        "user_invocable": True,
        "groups_needed": "",
        "has_evals": False,
        "path": "/fake/SKILL.md",
    }


def test_enh265_argument_hint_argument_hint_shown_when_set(capsys: pytest.CaptureFixture[str]) -> None:
    """_render_show_human shows Argument hint: line when argument_hint is non-empty."""
    from fieldkit.commands.skill._runner import _render_show_human

    skill = _enh265_argument_hint_make_skill("[account] [pursuit]")
    _render_show_human(skill)

    output = capsys.readouterr().out
    assert "Argument hint:" in output, f"Expected 'Argument hint:' in output:\n{output}"
    assert "[account] [pursuit]" in output


def test_enh265_argument_hint_argument_hint_absent_when_empty(capsys: pytest.CaptureFixture[str]) -> None:
    """_render_show_human omits Argument hint: line when argument_hint is empty."""
    from fieldkit.commands.skill._runner import _render_show_human

    skill = _enh265_argument_hint_make_skill("")
    _render_show_human(skill)

    output = capsys.readouterr().out
    assert "Argument hint:" not in output, f"Expected no 'Argument hint:' in output:\n{output}"


# ---------------------------------------------------------------------------
# implementation change — skill show suppresses empty groups_needed and version
# ---------------------------------------------------------------------------


# ── TestEnh266SuppressEmptyFields (flattened) ───────────────────────────────


def _enh266_suppress_empty_fields_make_skill(groups_needed: str, version: str) -> dict:
    return {
        "name": "test-skill",
        "version": version,
        "description": "A test skill.",
        "argument_hint": "",
        "user_invocable": True,
        "groups_needed": groups_needed,
        "has_evals": False,
        "path": "/fake/SKILL.md",
    }


def test_enh266_suppress_empty_fields_empty_groups_and_version_suppressed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Empty groups_needed and version must not produce output lines."""
    from fieldkit.commands.skill._runner import _render_show_human

    skill = _enh266_suppress_empty_fields_make_skill("", "")
    _render_show_human(skill)

    output = capsys.readouterr().out
    assert "MCP groups:" not in output, f"Expected no 'MCP groups:' line:\n{output}"
    assert "Version:" not in output, f"Expected no 'Version:' line:\n{output}"


def test_enh266_suppress_empty_fields_non_empty_groups_and_version_shown(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Non-empty groups_needed and version must appear in output."""
    from fieldkit.commands.skill._runner import _render_show_human

    skill = _enh266_suppress_empty_fields_make_skill("fieldkit-markdown", "2.0")
    _render_show_human(skill)

    output = capsys.readouterr().out
    assert "MCP groups:" in output, f"Expected 'MCP groups:' in output:\n{output}"
    assert "Version:" in output, f"Expected 'Version:' in output:\n{output}"
    assert "fieldkit-markdown" in output
    assert "2.0" in output


# ---------------------------------------------------------------------------
# implementation change — skill variables --json flag
# ---------------------------------------------------------------------------


# ── TestEnh268VariablesJson (flattened) ─────────────────────────────────────

_CMD_VARIABLES__MOCK_CTX: dict[str, str] = {
    "name": "Alice Example",
    "email": "alice@example.com",  # pii-guard: ignore
    "primary_account": "acme-corp",
    "accounts.0": "acme-corp",
    "accounts.all": "acme-corp",
}


def test_cmd_variables_variables_json_returns_zero(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """_cmd_variables(['--json']) returns 0."""
    from fieldkit.skill import template as skill_template

    monkeypatch.setattr(skill_template, "build_template_ctx", lambda: _CMD_VARIABLES__MOCK_CTX)
    rc = _cmd_variables(["--json"])
    assert rc == 0


def test_cmd_variables_variables_json_output_is_valid_json(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """_cmd_variables(['--json']) outputs valid JSON with at least one key."""
    from fieldkit.skill import template as skill_template

    monkeypatch.setattr(skill_template, "build_template_ctx", lambda: _CMD_VARIABLES__MOCK_CTX)
    _cmd_variables(["--json"])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert isinstance(parsed, dict)
    assert len(parsed) > 0


def test_cmd_variables_variables_no_json_is_human_readable(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """_cmd_variables([]) returns human-readable table (not JSON)."""
    from fieldkit.skill import template as skill_template

    monkeypatch.setattr(skill_template, "build_template_ctx", lambda: _CMD_VARIABLES__MOCK_CTX)
    _cmd_variables([])
    out = capsys.readouterr().out
    assert "Variable" in out
    assert "Current Value" in out
    # Must not be JSON
    try:
        json.loads(out)
        raise AssertionError("Expected human-readable output, got valid JSON")
    except json.JSONDecodeError:
        pass  # expected


# ---------------------------------------------------------------------------
# implementation change — skill list --group empty match notice
# ---------------------------------------------------------------------------


# ── TestEnh270GroupEmptyNotice (flattened) ──────────────────────────────────


def test_enh270_group_empty_notice_empty_group_notice(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When --group matches no skills, stdout contains the notice and rc is 0."""
    import fieldkit.commands.skill._runner as runner

    # Patch _load_all_skills to return an empty list (simulates no matching skills)
    monkeypatch.setattr(runner, "_load_all_skills", lambda: [])
    rc = _cmd_list(["--group", "nonexistent-group-xyz"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no skills found matching group 'nonexistent-group-xyz'" in out


# ── TestCmdEvalCLI (flattened) ──────────────────────────────────────────────


def test_cmd_list_eval_help() -> None:
    """fieldkit skill eval --help exits 0 and documents new flags."""
    result = CliRunner().invoke(skill_cli, ["eval", "--help"])
    assert result.exit_code == 0
    assert "--behavioral" in result.output
    assert "--calibrate" in result.output
    assert "--limit" in result.output


def test_cmd_list_eval_behavioral_flag_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    """--behavioral flag is accepted and forwarded to run_eval_cmd."""
    import fieldkit.commands.skill.eval_runner as eval_runner

    received: list[dict[str, object]] = []

    def fake_run_eval_cmd(**kwargs: object) -> int:
        received.append(kwargs)
        return 0

    monkeypatch.setattr(eval_runner, "run_eval_cmd", fake_run_eval_cmd)
    result = CliRunner().invoke(skill_cli, ["eval", "--behavioral"])
    assert result.exit_code == 0
    assert received, "run_eval_cmd was not called"
    assert received[0]["behavioral"] is True


def test_cmd_list_eval_calibrate_flag_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    """--calibrate flag is accepted and forwarded to run_eval_cmd."""
    import fieldkit.commands.skill.eval_runner as eval_runner

    received: list[dict[str, object]] = []

    def fake_run_eval_cmd(**kwargs: object) -> int:
        received.append(kwargs)
        return 0

    monkeypatch.setattr(eval_runner, "run_eval_cmd", fake_run_eval_cmd)
    result = CliRunner().invoke(skill_cli, ["eval", "--calibrate"])
    assert result.exit_code == 0
    assert received, "run_eval_cmd was not called"
    assert received[0]["calibrate"] is True


def test_cmd_list_eval_limit_flag_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    """--limit N flag is accepted and forwarded to run_eval_cmd."""
    import fieldkit.commands.skill.eval_runner as eval_runner

    received: list[dict[str, object]] = []

    def fake_run_eval_cmd(**kwargs: object) -> int:
        received.append(kwargs)
        return 0

    monkeypatch.setattr(eval_runner, "run_eval_cmd", fake_run_eval_cmd)
    result = CliRunner().invoke(skill_cli, ["eval", "--limit", "5"])
    assert result.exit_code == 0
    assert received, "run_eval_cmd was not called"
    assert received[0]["limit"] == 5


def test_cmd_list_eval_skill_flags_precede_positional_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """--skill values are ordered ahead of bare positional names.

    The order is load-bearing, not incidental: --limit truncates skill_names in
    behavioral mode, so swapping the two groups changes which skills get judged.
    """
    import fieldkit.commands.skill.eval_runner as eval_runner

    received: list[dict[str, object]] = []

    def fake_run_eval_cmd(**kwargs: object) -> int:
        received.append(kwargs)
        return 0

    monkeypatch.setattr(eval_runner, "run_eval_cmd", fake_run_eval_cmd)
    result = CliRunner().invoke(
        skill_cli,
        ["eval", "pos-one", "--skill", "flag-one", "pos-two", "--skill", "flag-two"],
    )
    assert result.exit_code == 0
    assert received, "run_eval_cmd was not called"
    assert received[0]["skill_names"] == ["flag-one", "flag-two", "pos-one", "pos-two"]


# ── TestCmdInstallCLI (flattened) ───────────────────────────────────────────


def test_cmd_install_install_all_flag_documented() -> None:
    """fieldkit skill install --help documents --all flag."""
    result = CliRunner().invoke(skill_cli, ["install", "--help"])
    assert result.exit_code == 0
    assert "--all" in result.output


def test_cmd_install_install_all_flag_with_tool_no_opencode_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--all --tool opencode installs all skills; exits 0 even when .opencode not present in CWD."""
    import fieldkit.commands.skill._install_execution as install_execution

    installed: list[tuple[str, str]] = []

    def fake_install_to_tools(
        targets: list[typing.Any],
        selected_skills: list[str],
        skills_dir: typing.Any,
        ctx: typing.Any,
        decisions: typing.Any,
        *,
        dry_run: bool = False,
    ) -> tuple[int, int, int, int]:
        for target in targets:
            for skill in selected_skills:
                installed.append((target.key, skill))
        return len(selected_skills), 0, 0, 0

    monkeypatch.setattr(install_execution, "_install_to_tools", fake_install_to_tools)
    result = CliRunner().invoke(skill_cli, ["install", "--tool", "opencode", "--all"])
    assert result.exit_code == 0, result.output
    # All SKILL_CATEGORIES skills should be installed
    from fieldkit.skill.targets import SKILL_CATEGORIES

    all_skills = [s for skills in SKILL_CATEGORIES.values() for s in skills]
    installed_skills = [s for _, s in installed]
    assert set(all_skills) == set(installed_skills)
