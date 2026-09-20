"""Unit tests for the redesigned fieldkit/skill/_runner.py:_cmd_install().

All tests use @pytest.mark.unit and tmp_path for filesystem isolation.
No real questionary calls are made — the library is mocked where needed.
No LLM or network calls.

Coverage targets (per design.md):
  - Non-interactive path (--tool + --skill flags provided): tests 1, 2, 3, 4, 5, 6, 7, 8
  - Numbered-prompt fallback (HAS_QUESTIONARY=False, non-TTY): test 9
  - questionary path (HAS_QUESTIONARY=True, mocked): test 10
  - No-tool-detected message: test 11
  - Multi-tool install: test 12
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import fieldkit.commands.skill._runner as _runner
from fieldkit.commands.skill._runner import _cmd_install

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_skills_dir(tmp_path: Path) -> Path:
    """Create a minimal fake skills directory with two installable skills.

    account-pulse/SKILL.md  — contains a {{name}} template variable.
    meddpicc-coach/SKILL.md — plain content, no template variables.
    """
    sd = tmp_path / "fake-skills"
    for skill_name in ("account-pulse", "meddpicc-coach"):
        skill_dir = sd / skill_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"# {skill_name}\nHello {{{{name}}}}!\n",
            encoding="utf-8",
        )
    return sd


@pytest.fixture(autouse=True)
def _patch_skills_dir(fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect _skills_dir() to the fake skills dir for every test.

    Also clears the functools.cache so the monkeypatch takes effect.
    """
    _runner._skills_dir.cache_clear()
    monkeypatch.setattr(_runner, "_skills_dir", lambda: fake_skills_dir)


@pytest.fixture(autouse=True)
def _patch_build_template_ctx(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return an empty template context so no real config.yaml is needed."""
    from fieldkit.skill import template as _tmpl

    monkeypatch.setattr(_tmpl, "build_template_ctx", lambda: {})


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_project(tmp_path: Path, *tool_dirs: str) -> Path:
    """Create a project directory with the given tool config dirs and chdir into it.

    Returns the project path.  Caller is responsible for monkeypatch.chdir().
    """
    project = tmp_path / "project"
    project.mkdir()
    for d in tool_dirs:
        (project / d).mkdir()
    return project


# ---------------------------------------------------------------------------
# Test 1: no tools selected → exit 0 with "No tools selected"
# ---------------------------------------------------------------------------


def test_install_no_tools_selected_exits_0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When the numbered fallback yields no tool selection, exit 0 with message.

    Strategy: patch HAS_QUESTIONARY=False and sys.stdin.isatty()=False so the
    numbered fallback is used, then patch builtins.input to return "" (empty
    selection = no tools chosen).
    """
    project = _make_project(tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.setattr(_runner, "HAS_QUESTIONARY", False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    rc = _cmd_install([], [])

    assert rc == 0
    out = capsys.readouterr().out
    assert "No tools selected" in out


# ---------------------------------------------------------------------------
# Test 2: no skills selected → exit 0 with "No skills selected"
# ---------------------------------------------------------------------------


def test_install_no_skills_selected_exits_0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When a tool is selected but no skills, exit 0 with "No skills selected".

    Strategy: patch HAS_QUESTIONARY=False, isatty=False, and builtins.input to
    return "1" for the tool prompt (selects first tool) then "" for skill prompt.
    """
    project = _make_project(tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.setattr(_runner, "HAS_QUESTIONARY", False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    call_count = 0

    def _fake_input(prompt: str = "") -> str:
        nonlocal call_count
        call_count += 1
        # First call = tool selection → pick first tool
        # Second call = skill selection → pick nothing
        return "1" if call_count == 1 else ""

    monkeypatch.setattr("builtins.input", _fake_input)

    rc = _cmd_install([], [])

    assert rc == 0
    out = capsys.readouterr().out
    assert "No skills selected" in out


# ---------------------------------------------------------------------------
# Test 3: --tool opencode --skill account-pulse writes correct path
# ---------------------------------------------------------------------------


def test_install_opencode_writes_correct_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--tool opencode --skill account-pulse writes .opencode/skills/account-pulse/SKILL.md."""
    project = _make_project(tmp_path, ".opencode")
    monkeypatch.chdir(project)

    rc = _cmd_install(["opencode"], ["account-pulse"])

    assert rc == 0
    expected = project / ".opencode" / "skills" / "account-pulse" / "SKILL.md"
    assert expected.exists(), f"Expected {expected} to exist"


# ---------------------------------------------------------------------------
# Test 4: --tool cursor --skill account-pulse writes flat file
# ---------------------------------------------------------------------------


def test_install_cursor_writes_flat_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--tool cursor --skill account-pulse writes .cursor/rules/account-pulse.md (flat)."""
    project = _make_project(tmp_path, ".cursor")
    monkeypatch.chdir(project)

    rc = _cmd_install(["cursor"], ["account-pulse"])

    assert rc == 0
    expected = project / ".cursor" / "rules" / "account-pulse.md"
    assert expected.exists(), f"Expected flat file {expected} to exist"
    # Flat file must NOT be a directory
    assert expected.is_file()


# ---------------------------------------------------------------------------
# Test 5: --tool flag skips prompt (questionary must not be called)
# ---------------------------------------------------------------------------


def test_install_tool_flag_skips_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When --tool and --skill are provided, no interactive prompt is invoked.

    Verifies that questionary.checkbox is never called when both flags are given.
    """
    project = _make_project(tmp_path, ".opencode")
    monkeypatch.chdir(project)

    # Patch HAS_QUESTIONARY=True but make questionary.checkbox raise if called
    monkeypatch.setattr(_runner, "HAS_QUESTIONARY", True)

    mock_questionary = MagicMock()
    mock_questionary.checkbox.side_effect = AssertionError("questionary.checkbox must not be called")
    monkeypatch.setattr(_runner, "questionary", mock_questionary, raising=False)

    rc = _cmd_install(["opencode"], ["account-pulse"])

    assert rc == 0
    expected = project / ".opencode" / "skills" / "account-pulse" / "SKILL.md"
    assert expected.exists()
    # questionary.checkbox must not have been called
    mock_questionary.checkbox.assert_not_called()


# ---------------------------------------------------------------------------
# Test 6: path traversal in --skill flag is rejected
# ---------------------------------------------------------------------------


def test_install_skill_flag_path_traversal_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--skill ../../etc/passwd must be rejected with exit 1 and no file written."""
    project = _make_project(tmp_path, ".opencode")
    monkeypatch.chdir(project)

    rc = _cmd_install(["opencode"], ["../../etc/passwd"])

    assert rc == 1
    err = capsys.readouterr().err
    # Error message must mention the invalid name
    assert "Invalid skill name" in err or "invalid skill" in err.lower()
    # Nothing must be written outside the project
    assert not (project / ".opencode" / "skills").exists()


# ---------------------------------------------------------------------------
# Test 7: unknown --skill flag exits 1
# ---------------------------------------------------------------------------


def test_install_unknown_skill_flag_exits_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--skill ghost-skill-does-not-exist must exit 1 with 'Unknown skill' message."""
    project = _make_project(tmp_path, ".opencode")
    monkeypatch.chdir(project)

    rc = _cmd_install(["opencode"], ["ghost-skill-does-not-exist"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "Unknown skill" in err
    assert "ghost-skill-does-not-exist" in err


# ---------------------------------------------------------------------------
# Test 8: unknown --tool flag exits 1
# ---------------------------------------------------------------------------


def test_install_unknown_tool_flag_exits_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--tool vim must exit 1 with 'Unknown tool' message."""
    project = _make_project(tmp_path)
    monkeypatch.chdir(project)

    rc = _cmd_install(["vim"], [])

    assert rc == 1
    err = capsys.readouterr().err
    assert "Unknown tool" in err
    assert "vim" in err


# ---------------------------------------------------------------------------
# Test 9: non-TTY uses numbered fallback and completes without hanging
# ---------------------------------------------------------------------------


def test_install_non_tty_uses_numbered_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With HAS_QUESTIONARY=False and non-TTY stdin, numbered fallback is used.

    Patches SKILL_CATEGORIES to only contain the fake skills so index 1 maps
    to account-pulse reliably.  Patches builtins.input to select tool 1 (opencode)
    and skill 1 (account-pulse).  Verifies the command completes and writes the file.
    """
    project = _make_project(tmp_path, ".opencode")
    monkeypatch.chdir(project)
    monkeypatch.setattr(_runner, "HAS_QUESTIONARY", False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    # Patch SKILL_CATEGORIES so only the fake skills appear in the numbered list
    from fieldkit.skill import targets as _targets

    monkeypatch.setattr(
        _targets,
        "SKILL_CATEGORIES",
        {"FAKE": ["account-pulse", "meddpicc-coach"]},
    )

    call_count = 0

    def _fake_input(prompt: str = "") -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return "1"  # Select first tool (opencode)
        if call_count == 2:
            return "1"  # Select first skill (account-pulse, index 1 in FAKE category)
        # Related-skill suggestion prompt (if any) — skip
        return ""

    monkeypatch.setattr("builtins.input", _fake_input)

    rc = _cmd_install([], [])

    assert rc == 0
    # account-pulse must be written under .opencode/skills/
    expected = project / ".opencode" / "skills" / "account-pulse" / "SKILL.md"
    assert expected.exists(), f"Expected {expected} to exist after numbered-fallback install"


# ---------------------------------------------------------------------------
# Test 10: questionary path exercised via mock
# ---------------------------------------------------------------------------


def test_install_questionary_path_mocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With HAS_QUESTIONARY=True and questionary.checkbox mocked, the mock selection is used.

    Verifies the questionary code path has test coverage and that the mocked
    selection results in the expected file being written.
    """
    project = _make_project(tmp_path, ".opencode")
    monkeypatch.chdir(project)
    monkeypatch.setattr(_runner, "HAS_QUESTIONARY", True)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    from fieldkit.skill.targets import TOOL_TARGETS

    # Build the mock questionary module
    mock_questionary = MagicMock()

    # First call: tool selection → return OpenCode label
    opencode_label = TOOL_TARGETS["opencode"].label
    tool_checkbox_mock = MagicMock()
    tool_checkbox_mock.ask.return_value = [opencode_label]

    # Second call: skill selection → return ["account-pulse"]
    skill_checkbox_mock = MagicMock()
    skill_checkbox_mock.ask.return_value = ["account-pulse"]

    # Third call (related suggestions): return [] to skip
    related_checkbox_mock = MagicMock()
    related_checkbox_mock.ask.return_value = []

    mock_questionary.checkbox.side_effect = [
        tool_checkbox_mock,
        skill_checkbox_mock,
        related_checkbox_mock,
    ]
    mock_questionary.Separator = MagicMock(side_effect=lambda label: label)

    monkeypatch.setattr(_runner, "questionary", mock_questionary, raising=False)

    rc = _cmd_install([], [])

    assert rc == 0
    expected = project / ".opencode" / "skills" / "account-pulse" / "SKILL.md"
    assert expected.exists(), f"Expected {expected} to be written via questionary path"


# ---------------------------------------------------------------------------
# Test 11: no tool config detected → message shown, all tools unchecked
# ---------------------------------------------------------------------------


def test_install_no_tools_detected_shows_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When no tool config dirs exist in CWD, a 'no tool config was detected' message is shown.

    All four tools are presented unchecked.  The test uses the numbered fallback
    and selects nothing (empty input) to verify the message without writing files.
    """
    # Project directory with NO tool config dirs
    project = _make_project(tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.setattr(_runner, "HAS_QUESTIONARY", False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    # Empty input → no tools selected → exits 0 with "No tools selected"
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    rc = _cmd_install([], [])

    assert rc == 0
    out = capsys.readouterr().out
    # Must mention that no tool config was detected
    assert "no tool config was detected" in out.lower()


# ---------------------------------------------------------------------------
# Test 12: multi-tool install writes both paths
# ---------------------------------------------------------------------------


def test_install_multi_tool_writes_both_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--tool opencode --tool cursor --skill meddpicc-coach writes both target paths."""
    project = _make_project(tmp_path, ".opencode", ".cursor")
    monkeypatch.chdir(project)

    rc = _cmd_install(["opencode", "cursor"], ["meddpicc-coach"])

    assert rc == 0
    # OpenCode: directory format
    opencode_path = project / ".opencode" / "skills" / "meddpicc-coach" / "SKILL.md"
    assert opencode_path.exists(), f"Expected {opencode_path}"
    # Cursor: flat format
    cursor_path = project / ".cursor" / "rules" / "meddpicc-coach.md"
    assert cursor_path.exists(), f"Expected {cursor_path}"


# ---------------------------------------------------------------------------
# Test 13: related-skill suggestion numbered fallback
# ---------------------------------------------------------------------------


def test_install_related_skill_suggestion_numbered_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_skills_dir: Path,
) -> None:
    """Related-skill suggestions appear and can be selected via numbered fallback.

    Setup:
    - account-pulse/SKILL.md has a ## Related Skills section referencing meddpicc-coach.
    - HAS_QUESTIONARY=False, non-TTY stdin.
    - builtins.input: tool=1 (opencode), skill=1 (account-pulse), suggestion=1 (meddpicc-coach).

    Verifies that meddpicc-coach is installed even though it was not explicitly selected.
    """
    # Add a Related Skills section to account-pulse so it references meddpicc-coach
    pulse_md = fake_skills_dir / "account-pulse" / "SKILL.md"
    pulse_md.write_text(
        "# account-pulse\n\n## Related Skills\n\n- **meddpicc-coach** -- MEDDPICC scoring\n",
        encoding="utf-8",
    )

    project = _make_project(tmp_path, ".opencode")
    monkeypatch.chdir(project)
    monkeypatch.setattr(_runner, "HAS_QUESTIONARY", False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    # Patch SKILL_CATEGORIES so only the fake skills appear in the numbered list
    from fieldkit.skill import targets as _targets

    monkeypatch.setattr(
        _targets,
        "SKILL_CATEGORIES",
        {"FAKE": ["account-pulse", "meddpicc-coach"]},
    )

    call_count = 0

    def _fake_input(prompt: str = "") -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return "1"  # Select first tool (opencode)
        if call_count == 2:
            return "1"  # Select first skill (account-pulse)
        if call_count == 3:
            return "1"  # Accept first suggestion (meddpicc-coach)
        return ""

    monkeypatch.setattr("builtins.input", _fake_input)

    rc = _cmd_install([], [])

    assert rc == 0
    # account-pulse must be installed (explicitly selected)
    pulse_path = project / ".opencode" / "skills" / "account-pulse" / "SKILL.md"
    assert pulse_path.exists(), f"Expected {pulse_path}"
    # meddpicc-coach must be installed (via suggestion)
    coach_path = project / ".opencode" / "skills" / "meddpicc-coach" / "SKILL.md"
    assert coach_path.exists(), f"Expected {coach_path} (suggested skill should be installed)"


# ---------------------------------------------------------------------------
# Test 14: second install shows "→ unchanged" in output
# ---------------------------------------------------------------------------


def test_install_is_update_shows_updated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Installing account-pulse twice shows '→ unchanged' on the second run."""
    project = _make_project(tmp_path, ".opencode")
    monkeypatch.chdir(project)

    # First install — should show "→ new"
    rc1 = _cmd_install(["opencode"], ["account-pulse"])
    assert rc1 == 0
    out1 = capsys.readouterr().out
    assert "→ new" in out1

    # Second install — should show "→ unchanged"
    rc2 = _cmd_install(["opencode"], ["account-pulse"])
    assert rc2 == 0
    out2 = capsys.readouterr().out
    assert "→ unchanged" in out2


# ---------------------------------------------------------------------------
# Test 15: error count appears in output when errors occur
# ---------------------------------------------------------------------------


def test_install_error_summary_shown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_skills_dir: Path,
) -> None:
    """When _install_to_tools encounters errors, the error count appears in stderr output.

    Strategy: remove the SKILL.md from account-pulse so install_skill_dir returns errors=1.
    """
    # Remove SKILL.md so the skill directory exists but has no SKILL.md
    (fake_skills_dir / "account-pulse" / "SKILL.md").unlink()

    project = _make_project(tmp_path, ".opencode")
    monkeypatch.chdir(project)

    rc = _cmd_install(["opencode"], ["account-pulse"])

    assert rc == 1
    err = capsys.readouterr().err
    # Error count must appear in stderr
    assert "error" in err.lower()


# ---------------------------------------------------------------------------
# Test 16: Click CLI wrapper smoke test — --dry-run passes through
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_install_cli_dry_run_via_click_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the Click CLI wrapper passes --dry-run through to _cmd_install.

    Uses Click's CliRunner to invoke ``fieldkit skill install`` end-to-end
    (through the lazy-loading dispatcher in __main__.py) with --tool, --skill,
    and --dry-run flags.  Asserts:
    - exit code 0
    - no file is written (dry-run semantics)
    - output mentions dry-run / preview / would
    """
    from click.testing import CliRunner

    from fieldkit.__main__ import cli as fieldkit_cli

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".opencode").mkdir()
    # Use --tool + --skill + --dry-run (non-interactive mode)
    runner = CliRunner()
    result = runner.invoke(
        fieldkit_cli,
        ["skill", "install", "--tool", "opencode", "--skill", "account-pulse", "--dry-run"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    # dry-run: no file written
    assert not (tmp_path / ".opencode" / "skills" / "account-pulse" / "SKILL.md").exists()
    # output should mention dry-run
    assert "dry-run" in result.output.lower() or "preview" in result.output.lower() or "would" in result.output.lower()


@pytest.mark.unit
def test_install_cli_json_dry_run_is_one_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from fieldkit.__main__ import cli as fieldkit_cli

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".opencode").mkdir()
    result = CliRunner().invoke(
        fieldkit_cli,
        ["skill", "install", "--tool", "opencode", "--skill", "account-pulse", "--dry-run", "--json"],
        catch_exceptions=False,
    )

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["tool_count"] == 1
    assert payload["new"] == 1
    assert payload["dry_run"] is True
    assert payload["outcomes"] == {
        "completed": [],
        "failed": [],
        "pending": [{"skill": "account-pulse", "tool": "opencode"}],
        "skipped": [],
    }
    assert not (tmp_path / ".opencode" / "skills" / "account-pulse" / "SKILL.md").exists()


@pytest.mark.unit
def test_install_cli_json_rejects_prompting_before_runner() -> None:
    from fieldkit.__main__ import main

    with patch("fieldkit.commands.skill._runner._cmd_install") as mock_install:
        exit_code = main(["skill", "install", "--json"])

    assert exit_code == 3
    mock_install.assert_not_called()


# ---------------------------------------------------------------------------
# Test 17: locally-modified overwrite warning
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_install_locally_modified_requires_force(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-installing a locally-modified skill refuses without force.

    Strategy:
    1. Install account-pulse normally (first install → ``→ new``).
    2. Overwrite the installed SKILL.md with custom content so it differs
       from the rendered source.
    3. Re-install and verify that it refuses to overwrite the local content.
    """

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".opencode").mkdir()
    # Note: _patch_skills_dir autouse fixture already cleared the cache and
    # replaced _skills_dir with a plain lambda pointing at fake_skills_dir.
    # No cache_clear() needed here.

    # First install
    ret = _cmd_install(["opencode"], ["account-pulse"])
    assert ret == 0

    # Locally modify the installed file so it differs from the rendered source
    installed = tmp_path / ".opencode" / "skills" / "account-pulse" / "SKILL.md"
    assert installed.exists()
    installed.write_text("# locally modified content\n", encoding="utf-8")

    # Re-install — should detect the modification and refuse the overwrite
    ret2 = _cmd_install(["opencode"], ["account-pulse"])

    assert ret2 == 1
    assert installed.read_text(encoding="utf-8") == "# locally modified content\n"
