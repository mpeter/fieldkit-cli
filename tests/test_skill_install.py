"""Tests for fieldkit skill install subcommand (legacy _cmd_variables and eval_runner tests).

Note: The old global-install tests (test_creates_rendered_files, test_dry_run_*, etc.)
have been removed because the install command was redesigned in task 4.1 to use
project-local paths and an interactive flow.  Those behaviors are now covered by
tests/test_skill_install_runner.py.
"""

from pathlib import Path

import pytest

import fieldkit.commands.skill._runner as runner
from fieldkit.commands.skill._runner import _cmd_variables


@pytest.fixture()
def skills_dir(tmp_path: Path) -> Path:
    """Create a minimal fake skills directory with two skill subdirs.

    alpha/SKILL.md contains a {{name}} template variable.
    beta/SKILL.md is a plain file with no template variables.
    """
    sd = tmp_path / "skills"
    for name in ("alpha", "beta"):
        (sd / name).mkdir(parents=True)
        if name == "alpha":
            # Template variable for personalization testing
            (sd / name / "SKILL.md").write_text(f"# {name}\nHello {{{{name}}}}!\n", encoding="utf-8")
        else:
            (sd / name / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    # Dirs that should be skipped
    (sd / "__pycache__").mkdir()
    (sd / ".hidden").mkdir()
    return sd


@pytest.fixture(autouse=True)
def _patch_skills_dir(skills_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Override _skills_dir() cache so tests don't touch the real skills dir."""
    runner._skills_dir.cache_clear()
    monkeypatch.setattr(runner, "_skills_dir", lambda: skills_dir)


# ---------------------------------------------------------------------------
# T025: _cmd_variables tests
# ---------------------------------------------------------------------------

_EXPECTED_KEYS = [
    "name",
    "email",
    "company",
    "role",
    "primary_account",
    "accounts.0",
    "accounts.all",
]

_MOCK_CTX = {
    "name": "Alice Example",
    "email": "alice@example.com",  # pii-guard: ignore
    "company": "Acme Corp",
    "role": "Account Executive",
    "primary_account": "acme-corp",
    "accounts.0": "acme-corp",
    "accounts.1": "acme-corp",
    "accounts.2": "acme-corp",
    "accounts.3": "acme-corp",
    "accounts.all": "acme-corp",
    "internal_domain": "example.com",
    "primary_pursuit": "",
    "data_repo": "<user-home-path>/fieldkit-data",  # pii-guard: ignore
    "example_sf_id": "006Pe000000ExampleId",
}


@pytest.mark.unit
def test_cmd_variables_returns_zero(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """_cmd_variables([]) always returns 0."""
    from fieldkit.skill import template as skill_template

    monkeypatch.setattr(skill_template, "build_template_ctx", lambda: _MOCK_CTX)
    rc = _cmd_variables([])
    assert rc == 0


@pytest.mark.unit
def test_cmd_variables_header_row(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Output contains the 'Variable' and 'Current Value' header."""
    from fieldkit.skill import template as skill_template

    monkeypatch.setattr(skill_template, "build_template_ctx", lambda: _MOCK_CTX)
    _cmd_variables([])
    out = capsys.readouterr().out
    assert "Variable" in out
    assert "Current Value" in out


@pytest.mark.unit
def test_cmd_variables_separator_line(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Output contains a separator line of dashes."""
    from fieldkit.skill import template as skill_template

    monkeypatch.setattr(skill_template, "build_template_ctx", lambda: _MOCK_CTX)
    _cmd_variables([])
    out = capsys.readouterr().out
    # Separator must contain a run of dashes (at least 20 consecutive)
    assert "--------------------" in out


@pytest.mark.unit
def test_cmd_variables_expected_keys_present(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Output contains all expected variable key names."""
    from fieldkit.skill import template as skill_template

    monkeypatch.setattr(skill_template, "build_template_ctx", lambda: _MOCK_CTX)
    _cmd_variables([])
    out = capsys.readouterr().out
    for key in _EXPECTED_KEYS:
        assert key in out, f"Expected key {key!r} not found in output"


@pytest.mark.unit
def test_cmd_variables_sorted_alphabetically(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Variable names appear in alphabetical order in the output."""
    from fieldkit.skill import template as skill_template

    monkeypatch.setattr(skill_template, "build_template_ctx", lambda: _MOCK_CTX)
    _cmd_variables([])
    out = capsys.readouterr().out
    # Extract variable names from data rows (skip header and separator)
    lines = out.splitlines()
    data_lines = [ln for ln in lines if ln and not ln.startswith("-") and "Variable" not in ln]
    keys_in_output = [ln.split()[0] for ln in data_lines if ln.split()]
    assert keys_in_output == sorted(keys_in_output)


@pytest.mark.unit
def test_cmd_variables_left_padded_to_20(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Variable column is left-padded to 20 characters."""
    from fieldkit.skill import template as skill_template

    monkeypatch.setattr(skill_template, "build_template_ctx", lambda: _MOCK_CTX)
    _cmd_variables([])
    out = capsys.readouterr().out
    lines = out.splitlines()
    # Data rows (not header, not separator) must have key padded to 20 chars
    data_lines = [ln for ln in lines if ln and not ln.startswith("-") and "Variable" not in ln]
    for ln in data_lines:
        # The two-space separator starts at position 20
        assert len(ln) >= 22, f"Row too short: {ln!r}"
        assert ln[20:22] == "  ", f"Expected two-space gap at col 20-22 in: {ln!r}"


@pytest.mark.unit
def test_cmd_variables_empty_ctx(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """With an empty context (no config), output still shows header and separator."""
    from fieldkit.skill import template as skill_template

    monkeypatch.setattr(skill_template, "build_template_ctx", lambda: {})
    rc = _cmd_variables([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Variable" in out
    assert "--------------------" in out


# ---------------------------------------------------------------------------
# Task 11.1 — _render_eval_result and _resolve_skill_dirs
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_render_eval_result_stdout_contains_score(capsys: pytest.CaptureFixture[str]) -> None:
    """_render_eval_result prints static check pass/fail counts to stdout."""
    from fieldkit.commands.skill.eval_runner import _render_eval_result

    result = {
        "skill": "my-skill",
        "has_evals": True,
        "static_pass": 3,
        "static_fail": 1,
        "static_results": [
            {"id": "c1", "passed": True, "message": "Has trigger", "detail": ""},
            {"id": "c2", "passed": False, "message": "Has examples", "detail": "Pattern not found: 'Example'"},
        ],
        "behavioral_cases": [],
    }
    _render_eval_result(result, show_behavioral=False)
    out = capsys.readouterr().out
    # Score line: "3/4 passing"
    assert "3" in out
    assert "4" in out
    assert "my-skill" in out


@pytest.mark.unit
def test_render_eval_result_stdout_contains_expected_and_actual(capsys: pytest.CaptureFixture[str]) -> None:
    """_render_eval_result prints failing check detail (expected/actual) to stdout."""
    from fieldkit.commands.skill.eval_runner import _render_eval_result

    result = {
        "skill": "demo-skill",
        "has_evals": True,
        "static_pass": 0,
        "static_fail": 1,
        "static_results": [
            {
                "id": "c1",
                "passed": False,
                "message": "Must contain trigger phrase",
                "detail": "Pattern not found: 'trigger phrase'",
            }
        ],
        "behavioral_cases": [],
    }
    _render_eval_result(result, show_behavioral=False)
    out = capsys.readouterr().out
    # Failing detail line must appear
    assert "Pattern not found" in out
    assert "trigger phrase" in out


@pytest.mark.unit
def test_resolve_skill_dirs_run_all_returns_sorted_dirs(tmp_path: Path) -> None:
    """_resolve_skill_dirs with run_all=True returns all non-underscore dirs sorted."""
    from fieldkit.commands.skill.eval_runner import _resolve_skill_dirs

    # Use a separate directory not touched by the autouse fixture
    skills_dir = tmp_path / "eval_skills"
    (skills_dir / "beta").mkdir(parents=True)
    (skills_dir / "alpha").mkdir(parents=True)
    (skills_dir / "_private").mkdir(parents=True)

    result = _resolve_skill_dirs(skills_dir, run_all=True, skill_names=[])
    assert result is not None
    names = [d.name for d in result]
    assert names == sorted(names)
    assert "_private" not in names
    assert "alpha" in names
    assert "beta" in names


@pytest.mark.unit
def test_resolve_skill_dirs_unknown_name_returns_none(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """_resolve_skill_dirs with an unknown skill name prints error and returns None."""
    from fieldkit.commands.skill.eval_runner import _resolve_skill_dirs

    # Use a separate directory not touched by the autouse fixture
    skills_dir = tmp_path / "eval_skills2"
    (skills_dir / "real-skill").mkdir(parents=True)

    result = _resolve_skill_dirs(skills_dir, run_all=False, skill_names=["nonexistent-skill"])
    assert result is None
