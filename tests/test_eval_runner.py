"""Tests for template rendering in eval_runner (via lib.skill_template)."""

import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.commands.skill.eval_runner import _run_evals
from fieldkit.skill.template import render_skill_text

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# historic regression: --json mode must not emit header lines to stderr
# ---------------------------------------------------------------------------


def _make_skill_dir(tmp_path: Path, *, with_evals: bool = True) -> Path:
    """Create a minimal skill directory for eval tests."""
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Test Skill\n\nA test skill.", encoding="utf-8")
    if with_evals:
        evals_dir = skill_dir / "evals"
        evals_dir.mkdir()
        evals_data = {
            "static_checks": [
                {"id": "has-content", "type": "contains", "pattern": "Test Skill", "message": "Has content"}
            ],
            "evals": [],
        }
        (evals_dir / "evals.json").write_text(json.dumps(evals_data), encoding="utf-8")
    return skill_dir


# ── TestBug133JsonModeNoHeaderLines (flattened) ─────────────────────────────


def test_print_json_summary_json_mode_stderr_is_empty(tmp_path: Path) -> None:
    """When json_output=True, _run_evals must not write anything to stderr."""
    skill_dir = _make_skill_dir(tmp_path)
    stderr_capture = io.StringIO()
    with patch("sys.stderr", stderr_capture):
        _run_evals([skill_dir], failed_only=False, json_output=True, no_behavioral=True)
    assert stderr_capture.getvalue() == "", f"Expected empty stderr in --json mode, got: {stderr_capture.getvalue()!r}"


def test_print_json_summary_json_mode_stdout_is_valid_json(tmp_path: Path) -> None:
    """_run_evals in json_output mode returns results that can be serialised as JSON."""
    skill_dir = _make_skill_dir(tmp_path)
    results, _ = _run_evals([skill_dir], failed_only=False, json_output=True, no_behavioral=True)
    # Must be serialisable without error
    serialised = json.dumps(results)
    parsed = json.loads(serialised)
    assert isinstance(parsed, list)
    assert parsed[0]["skill"] == "test-skill"


def test_print_json_summary_non_json_mode_writes_to_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """In human mode, header lines appear on stdout (not stderr)."""
    skill_dir = _make_skill_dir(tmp_path)
    _run_evals([skill_dir], failed_only=False, json_output=False, no_behavioral=True)
    captured = capsys.readouterr()
    assert "fieldkit Skill Eval" in captured.out
    assert captured.err == ""


# ---------------------------------------------------------------------------
# historic regression: --failed filter must skip skills with no failures (static_fail == 0)
# ---------------------------------------------------------------------------


# ── TestBug135FailedFilter (flattened) ──────────────────────────────────────


def test_bug135_failed_filter_passing_skill_excluded_when_failed_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A skill with all checks passing must not appear in --failed output."""
    skill_dir = _make_skill_dir(tmp_path, with_evals=True)
    _run_evals([skill_dir], failed_only=True, json_output=False, no_behavioral=True)
    captured = capsys.readouterr()
    # The skill passes all checks → must not be rendered
    assert "test-skill" not in captured.out


def test_bug135_failed_filter_failing_skill_included_when_failed_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A skill with a failing check must appear in --failed output."""
    skill_dir = tmp_path / "bad-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Bad Skill\n\nMissing required pattern.", encoding="utf-8")
    evals_dir = skill_dir / "evals"
    evals_dir.mkdir()
    evals_data = {
        "static_checks": [
            {
                "id": "must-have-xyz",
                "type": "contains",
                "pattern": "PATTERN_THAT_DOES_NOT_EXIST",
                "message": "Must contain XYZ",
            }
        ],
        "evals": [],
    }
    (evals_dir / "evals.json").write_text(json.dumps(evals_data), encoding="utf-8")
    _run_evals([skill_dir], failed_only=True, json_output=False, no_behavioral=True)
    captured = capsys.readouterr()
    assert "bad-skill" in captured.out


def test_bug135_failed_filter_no_evals_skill_excluded_when_failed_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A skill with no evals.json has static_fail==0 and must be excluded by --failed."""
    skill_dir = _make_skill_dir(tmp_path, with_evals=False)
    _run_evals([skill_dir], failed_only=True, json_output=False, no_behavioral=True)
    captured = capsys.readouterr()
    assert "test-skill" not in captured.out


# ---------------------------------------------------------------------------
# Adapter: render_skill_text returns (rendered, unresolved_keys).
# These tests exercise the same contract as the old _resolve_templates(),
# verifying that the refactored eval_runner delegates to lib.skill_template.
# ---------------------------------------------------------------------------


def _resolve(text: str, ctx: dict[str, str]) -> str:
    """Thin adapter: call render_skill_text and return only the rendered text."""
    rendered, _ = render_skill_text(text, ctx)
    return rendered


# ---------------------------------------------------------------------------
# Normal substitution
# ---------------------------------------------------------------------------


def test_single_substitution() -> None:
    ctx = {"accounts.0": "acme-bank"}
    assert _resolve("prep QBR for {{accounts.0}}", ctx) == "prep QBR for acme-bank"


def test_multiple_vars() -> None:
    ctx = {"accounts.0": "acme-bank", "accounts.1": "globalpay", "primary_account": "acme-bank"}
    result = _resolve("{{primary_account}} and {{accounts.1}}", ctx)
    assert result == "acme-bank and globalpay"


def test_accounts_all_join() -> None:
    ctx = {"accounts.all": "acme-bank, globalpay, midwest-ins"}
    result = _resolve("All accounts: {{accounts.all}}", ctx)
    assert result == "All accounts: acme-bank, globalpay, midwest-ins"


def test_internal_domain() -> None:
    ctx = {"internal_domain": "internal.example.com"}
    result = _resolve("domain is {{internal_domain}}", ctx)
    assert result == "domain is internal.example.com"


def test_no_placeholders() -> None:
    ctx = {"accounts.0": "acme-bank"}
    result = _resolve("no placeholders here", ctx)
    assert result == "no placeholders here"


def test_empty_string() -> None:
    assert _resolve("", {}) == ""


# ---------------------------------------------------------------------------
# Missing key — leaves {{key}} intact, reported in unresolved list
# ---------------------------------------------------------------------------


def test_missing_key_leaves_intact() -> None:
    rendered, unresolved = render_skill_text("hello {{unknown}}", {})
    assert rendered == "hello {{unknown}}"
    assert "unknown" in unresolved


def test_missing_key_in_unresolved_list() -> None:
    ctx = {"accounts.0": "acme-bank"}
    rendered, unresolved = render_skill_text("{{accounts.0}} {{missing}}", ctx)
    assert rendered == "acme-bank {{missing}}"
    assert "missing" in unresolved


def test_multiple_missing_keys_all_reported() -> None:
    _, unresolved = render_skill_text("{{a}} {{b}} {{c}}", {})
    assert set(unresolved) == {"a", "b", "c"}


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_whitespace_inside_braces() -> None:
    ctx = {"accounts.0": "acme-bank"}
    # {{ accounts.0 }} with spaces should still resolve
    result = _resolve("{{ accounts.0 }}", ctx)
    assert result == "acme-bank"


def test_no_partial_replacement() -> None:
    """Single braces are left untouched."""
    ctx = {"key": "val"}
    result = _resolve("{key}", ctx)
    assert result == "{key}"


# ---------------------------------------------------------------------------
# historic regression — scaffold prints path when evals.json already exists
# ---------------------------------------------------------------------------


# ── TestBug267ScaffoldExistingEvalsJson (flattened) ─────────────────────────


def test_scaffold_evals_scaffold_prints_path_when_exists(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """When evals.json already exists, _scaffold_evals prints its path to stdout."""
    from fieldkit.commands.skill.eval_runner import _scaffold_evals

    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    evals_dir = skill_dir / "evals"
    evals_dir.mkdir()
    evals_file = evals_dir / "evals.json"
    evals_file.write_text("{}", encoding="utf-8")

    # implementation change: exits 1 when evals.json already exists (prevents accidental overwrite)
    rc = _scaffold_evals(skill_dir)
    assert rc == 1
    err = capsys.readouterr().err
    assert str(evals_file) in err, f"Expected evals.json path in stderr, got: {err!r}"


def test_scaffold_evals_scaffold_creates_file_when_absent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """When evals.json does not exist, _scaffold_evals creates it and prints 'Created:'."""
    from fieldkit.commands.skill.eval_runner import _scaffold_evals

    skill_dir = tmp_path / "new-skill"
    skill_dir.mkdir()

    rc = _scaffold_evals(skill_dir)
    assert rc == 0
    evals_file = skill_dir / "evals" / "evals.json"
    assert evals_file.exists(), "Expected evals.json to be created"
    out = capsys.readouterr().out
    assert "Created:" in out


# ---------------------------------------------------------------------------
# historic regression: _render_eval_result must not crash when expected_behavior is absent
# ---------------------------------------------------------------------------


# ── Missing expected behavior regression (flattened) ────────────────────────


def _missing_expected_behavior_make_skill_with_behavioral(tmp_path: Path, case: dict) -> Path:
    skill_dir = tmp_path / "legacy-eval-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# historic regression Test Skill\n\nTest.", encoding="utf-8")
    evals_dir = skill_dir / "evals"
    evals_dir.mkdir()
    evals_data = {"evals": [case]}
    (evals_dir / "evals.json").write_text(json.dumps(evals_data), encoding="utf-8")
    return skill_dir


def test_missing_expected_behavior_does_not_crash(tmp_path: Path) -> None:
    """_render_eval_result must not raise KeyError when expected_behavior absent."""
    from fieldkit.commands.skill.eval_runner import _render_eval_result

    # Case has prompt but no expected_behavior (pre-schema evals format)
    result: dict = {
        "skill": "legacy-eval",
        "has_evals": True,
        "static_results": [],
        "behavioral_cases": [{"id": "case-1", "prompt": "Does it work?", "assertions": []}],
        "static_pass": 0,
        "static_fail": 0,
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
    # Must not raise KeyError
    try:
        _render_eval_result(result, show_behavioral=True)
    except KeyError as exc:
        pytest.fail(f"_render_eval_result raised KeyError: {exc}")


def test_missing_expected_behavior_falls_back_to_prompt(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """When expected_behavior absent, fallback to prompt text in output."""
    from fieldkit.commands.skill.eval_runner import _render_eval_result

    result: dict = {
        "skill": "legacy-eval",
        "has_evals": True,
        "static_results": [],
        "behavioral_cases": [{"id": "case-1", "prompt": "Does it work?", "assertions": []}],
        "static_pass": 0,
        "static_fail": 0,
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
    _render_eval_result(result, show_behavioral=True)
    out = capsys.readouterr().out
    # Expected line should show the prompt as fallback
    assert "Does it work?" in out, f"Fallback prompt text not in output: {out!r}"


def test_expected_behavior_takes_precedence_when_present(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """When expected_behavior is present, it is used (not prompt)."""
    from fieldkit.commands.skill.eval_runner import _render_eval_result

    result: dict = {
        "skill": "legacy-eval",
        "has_evals": True,
        "static_results": [],
        "behavioral_cases": [
            {
                "id": "case-1",
                "prompt": "Does it work?",
                "expected_behavior": "Should summarise correctly",
                "assertions": [],
            }
        ],
        "static_pass": 0,
        "static_fail": 0,
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
    _render_eval_result(result, show_behavioral=True)
    out = capsys.readouterr().out
    assert "Should summarise correctly" in out


@pytest.mark.parametrize(
    ("assertion", "expected_text"),
    [
        ("Persists only numeric scores", "Persists only numeric scores"),
        ({"text": "Legacy dict assertion"}, "Legacy dict assertion"),
    ],
)
def test_render_eval_result_accepts_string_and_dict_assertions(
    capsys: pytest.CaptureFixture[str], assertion: str | dict, expected_text: str
) -> None:
    """Corpus evals.json stores assertions as plain strings; legacy scaffolds used dicts."""
    from fieldkit.commands.skill.eval_runner import _render_eval_result

    result: dict = {
        "skill": "assertion-shape",
        "has_evals": True,
        "static_results": [],
        "behavioral_cases": [
            {"id": "case-1", "prompt": "Does it work?", "assertions": [assertion]},
        ],
        "static_pass": 0,
        "static_fail": 0,
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
    _render_eval_result(result, show_behavioral=True)
    out = capsys.readouterr().out
    assert expected_text in out


# ---------------------------------------------------------------------------
# 4F.2 run_eval_cmd
# ---------------------------------------------------------------------------


# ── TestRunEvalCmd (flattened) ──────────────────────────────────────────────


@pytest.mark.unit
def test_run_eval_cmd_run_eval_cmd_no_args_exits_nonzero() -> None:
    """run_eval_cmd exits 1 with no skill arguments (usage error)."""
    from fieldkit.commands.skill.eval_runner import run_eval_cmd

    rc = run_eval_cmd(skill_names=[])
    assert rc != 0


@pytest.mark.unit
def test_run_eval_cmd_run_eval_cmd_returns_int() -> None:
    """run_eval_cmd always returns an integer exit code."""
    from fieldkit.commands.skill.eval_runner import run_eval_cmd

    rc = run_eval_cmd(skill_names=[])
    assert isinstance(rc, int)


@pytest.mark.unit
def test_run_eval_cmd_empty_scaffold_name_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    """`--scaffold ""` is a usage error, not a request to scaffold a skill named "".

    Reachable from the CLI: Click rejects a *missing* --scaffold value, but passes
    an explicitly empty one through verbatim. Previously covered only indirectly,
    via a test of the deleted argv re-parser.
    """
    from fieldkit.commands.skill.eval_runner import run_eval_cmd

    rc = run_eval_cmd(skill_names=[], scaffold_name="")
    assert rc == 1
    assert "Usage: fieldkit skill eval --scaffold <skill-name>" in capsys.readouterr().err
