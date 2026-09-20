"""Tests for scripts/check_skill_integrity.py — skill corpus integrity validator.

All tests are @pytest.mark.unit, use tmp_path for filesystem isolation, and invoke
the validator directly with an immutable context for xdist compatibility.
"""

import json
from dataclasses import replace
from pathlib import Path

import check_skill_integrity
import pytest
from skill_integrity import model, references

from tests.conftest import skip_if_root

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Real repository root used by corpus consistency tests
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TEST_SKILL_NAMES = frozenset(
    {
        "my-skill",
        "real-skill",
        "lonely-skill",
        "skill-a",
        "skill-b",
        "caller-skill",
        "fk-skill",
    }
)


def _context(tmp_path: Path) -> model.IntegrityContext:
    """Build an isolated validator context for one fixture repository."""
    context = model.IntegrityContext.from_root(tmp_path, skill_roots=model.SKILL_ROOTS | _TEST_SKILL_NAMES)
    return replace(context, fieldkit_skills_dir=tmp_path / "fieldkit" / "skills")


def _run(
    context: model.IntegrityContext,
    capsys: pytest.CaptureFixture[str],
    args: list[str] | None = None,
) -> tuple[int, str]:
    """Invoke check_skill_integrity.main() and capture stdout + exit code.

    Args:
        context: Repository inputs for this validator invocation.
        capsys: The pytest capsys fixture for capturing output.
        args: argv list (excluding script name). Defaults to [].

    Returns:
        A (exit_code, stdout_text) tuple.
    """
    argv = ["check_skill_integrity"] + (args or [])
    with pytest.raises(SystemExit) as exc_info:
        check_skill_integrity.main(argv, context=context)
    captured = capsys.readouterr()
    assert isinstance(exc_info.value.code, int)
    return exc_info.value.code, captured.out


# ---------------------------------------------------------------------------
# Corpus builder helpers
# ---------------------------------------------------------------------------


def make_opencode_json(
    tmp_path: Path,
    extra_mcp_groups: dict | None = None,
    agents: dict | None = None,
) -> None:
    """Write a minimal opencode.json with the given extra MCP groups.

    Args:
        tmp_path: Repo root directory.
        extra_mcp_groups: Additional keys to add under ``mcp``. Defaults to {}.
        agents: Agent definitions to add under ``agent``. Defaults to {}.
    """
    mcp: dict = {}
    if extra_mcp_groups:
        mcp.update(extra_mcp_groups)
    (tmp_path / "opencode.json").write_text(json.dumps({"mcp": mcp, "agent": agents or {}}), encoding="utf-8")


def make_pyproject_toml(tmp_path: Path, fail_under: int = 80) -> None:
    """Write a minimal pyproject.toml with the given coverage fail_under value.

    Args:
        tmp_path: Repo root directory.
        fail_under: The coverage floor value to embed.
    """
    content = f"[tool.coverage.report]\nfail_under = {fail_under}\n"
    (tmp_path / "pyproject.toml").write_text(content, encoding="utf-8")


def make_makefile(tmp_path: Path, max_crapload: int = 1) -> None:
    """Write a minimal Makefile with the given --max-crapload value.

    Args:
        tmp_path: Repo root directory.
        max_crapload: The CRAP threshold value to embed.
    """
    content = f"gazepy:\n\tuv run gazepy crap . --max-crapload {max_crapload}\n"
    (tmp_path / "Makefile").write_text(content, encoding="utf-8")


def make_project_skill(
    tmp_path: Path,
    name: str,
    frontmatter: str = "",
    body: str = "",
) -> Path:
    """Create a project skill under .opencode/skills/<name>/SKILL.md.

    Args:
        tmp_path: Repo root directory.
        name: Skill directory name.
        frontmatter: Raw frontmatter content (between --- delimiters). If empty,
            a valid default frontmatter is used.
        body: Markdown body content below the frontmatter.

    Returns:
        Path to the created SKILL.md file.
    """
    skill_dir = tmp_path / ".opencode" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    if frontmatter:
        content = f"---\n{frontmatter}\n---\n{body}"
    else:
        content = f"---\nname: {name}\ndescription: A project skill.\n---\n{body}"
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(content, encoding="utf-8")
    return skill_file


def make_fieldkit_skill(
    tmp_path: Path,
    name: str,
    frontmatter: str = "",
    body: str = "",
) -> Path:
    """Create a fieldkit skill under fieldkit/skills/<name>/SKILL.md.

    Args:
        tmp_path: Repo root directory.
        name: Skill directory name.
        frontmatter: Raw frontmatter content (between --- delimiters). If empty,
            a valid default frontmatter is used.
        body: Markdown body content below the frontmatter.

    Returns:
        Path to the created SKILL.md file.
    """
    skill_dir = tmp_path / "fieldkit" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    if frontmatter:
        content = f"---\n{frontmatter}\n---\n{body}"
    else:
        content = f"---\nname: {name}\ndescription: A fieldkit skill.\nslash: true\n---\n{body}"
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(content, encoding="utf-8")
    return skill_file


def make_agent(tmp_path: Path, name: str, body: str = "") -> Path:
    """Create an agent file under .opencode/agents/<name>.md.

    Args:
        tmp_path: Repo root directory.
        name: Agent stem name (without .md).
        body: File content.

    Returns:
        Path to the created agent file.
    """
    agents_dir = tmp_path / ".opencode" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    agent_file = agents_dir / f"{name}.md"
    agent_file.write_text(body or f"# Agent: {name}\n", encoding="utf-8")
    return agent_file


def make_command(tmp_path: Path, name: str, body: str = "") -> Path:
    """Create a command file under .opencode/commands/<name>.md.

    Args:
        tmp_path: Repo root directory.
        name: Command stem name (without .md).
        body: File content.

    Returns:
        Path to the created command file.
    """
    commands_dir = tmp_path / ".opencode" / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)
    cmd_file = commands_dir / f"{name}.md"
    cmd_file.write_text(body or f"# Command: {name}\n", encoding="utf-8")
    return cmd_file


def _minimal_corpus(tmp_path: Path) -> None:
    """Set up the minimal required support files (opencode.json, pyproject.toml, Makefile).

    Args:
        tmp_path: Repo root directory.
    """
    make_opencode_json(tmp_path)
    make_pyproject_toml(tmp_path)
    make_makefile(tmp_path)
    # Ensure skill dirs exist so the validator doesn't crash on missing dirs
    (tmp_path / ".opencode" / "skills").mkdir(parents=True, exist_ok=True)
    (tmp_path / "fieldkit" / "skills").mkdir(parents=True, exist_ok=True)


# ===========================================================================
# Schema violation tests (S001-S004)
# ===========================================================================


def test_s001_missing_name(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """S001 is emitted when a fieldkit skill has description: but no name: field.
    Project skills (.opencode/skills/) do not require name: — UF scaffold omits it."""
    _minimal_corpus(tmp_path)
    # fieldkit skill: name: is required
    make_fieldkit_skill(tmp_path, "my-skill", frontmatter="description: A skill without a name.")
    context = _context(tmp_path)
    exit_code, out = _run(context, capsys)
    assert exit_code == 1
    assert "S001" in out
    assert "name" in out


def test_s002_name_dirname_mismatch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """S002 is emitted when a fieldkit skill's name: doesn't match its directory name."""
    _minimal_corpus(tmp_path)
    # Directory is "right" but name: is "wrong"
    make_fieldkit_skill(
        tmp_path,
        "right",
        frontmatter="name: wrong\ndescription: Mismatch skill.\nslash: true",
    )
    context = _context(tmp_path)
    exit_code, out = _run(context, capsys)
    assert exit_code == 1
    assert "S002" in out
    assert "wrong" in out
    assert "right" in out


def test_s003_legacy_user_invocable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """S003 is emitted when a skill still has the legacy user-invocable: field."""
    _minimal_corpus(tmp_path)
    make_fieldkit_skill(
        tmp_path,
        "my-skill",
        frontmatter="name: my-skill\ndescription: Has legacy field.\nuser-invocable: true",
    )
    context = _context(tmp_path)
    exit_code, out = _run(context, capsys)
    assert exit_code == 1
    assert "S003" in out
    assert "user-invocable" in out


def test_s004_wrong_type(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """S004 is emitted when slash: is a string instead of a boolean."""
    _minimal_corpus(tmp_path)
    make_fieldkit_skill(
        tmp_path,
        "my-skill",
        frontmatter='name: my-skill\ndescription: Wrong type.\nslash: "true"',
    )
    context = _context(tmp_path)
    exit_code, out = _run(context, capsys)
    assert exit_code == 1
    assert "S004" in out
    assert "slash" in out


# ===========================================================================
# Dead reference tests (R001-R006)
# ===========================================================================


def test_r001_bold_format(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """R001 is emitted for a bold-format Related Skills entry referencing a non-existent skill."""
    _minimal_corpus(tmp_path)
    body = "\n## Related Skills\n\n- **ghost-skill** — A skill that does not exist.\n"
    make_fieldkit_skill(tmp_path, "real-skill", body=body)
    context = _context(tmp_path)
    exit_code, out = _run(context, capsys)
    assert exit_code == 1
    assert "R001" in out
    assert "ghost-skill" in out


def test_r001_backtick_slash_format(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """R001 is emitted for a backtick-slash Related Skills entry referencing a non-existent skill."""
    _minimal_corpus(tmp_path)
    body = "\n## Related Skills\n\n- `/ghost-skill` — A skill that does not exist.\n"
    make_fieldkit_skill(tmp_path, "real-skill", body=body)
    context = _context(tmp_path)
    exit_code, out = _run(context, capsys)
    assert exit_code == 1
    assert "R001" in out
    assert "ghost-skill" in out


def test_r001_live_link_no_violation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """No violation when a backtick-slash Related Skills entry references an existing skill."""
    _minimal_corpus(tmp_path)
    # meeting exists as a fieldkit skill
    make_fieldkit_skill(tmp_path, "meeting")
    body = "\n## Related Skills\n\n- `/meeting` — Prepare for meetings.\n"
    make_fieldkit_skill(tmp_path, "post-meeting", body=body)
    # Both skills are orphans (G002 warning), but no R001 error
    context = _context(tmp_path)
    exit_code, out = _run(context, capsys)
    # R001 must NOT appear; G002 warnings are acceptable (exit 0)
    assert "R001" not in out
    assert exit_code == 0, out


def test_r002_unregistered_mcp(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """R002 is emitted when a skill references an MCP group not in opencode.json."""
    _minimal_corpus(tmp_path)
    # backstory2 is not in opencode.json and not in KNOWN_MCPJUNGLE_GROUPS
    body = "Use `backstory2__find` to look up account signals.\n"
    make_fieldkit_skill(tmp_path, "my-skill", body=body)
    context = _context(tmp_path)
    exit_code, out = _run(context, capsys)
    assert exit_code == 1
    assert "R002" in out
    assert "backstory2" in out


def test_r003_missing_agent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """R003 is emitted when a command file references an agent that doesn't exist.

    The R003 check fires when a line contains both '.opencode/agents/' AND a
    backtick-quoted hyphenated identifier that is not a known agent. The agent name
    must be in backticks separately from the path (the validator uses _R003_BACKTICK_ID
    which matches standalone backtick-quoted identifiers, not paths).
    """
    _minimal_corpus(tmp_path)
    # Line contains .opencode/agents/ path context AND the agent name in backticks
    body = "See .opencode/agents/ for agent files. Delegates to `gazepy-reporter` for quality analysis.\n"
    make_command(tmp_path, "check-quality", body=body)
    context = _context(tmp_path)
    exit_code, out = _run(context, capsys)
    assert exit_code == 1
    assert "R003" in out
    assert "gazepy-reporter" in out


def test_r004_dead_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """R004 is emitted when a skill body references a command that doesn't exist."""
    _minimal_corpus(tmp_path)
    # opsx-continue is not a command or skill
    body = "Run `/opsx-continue` to resume the workflow.\n"
    make_fieldkit_skill(tmp_path, "my-skill", body=body)
    context = _context(tmp_path)
    exit_code, out = _run(context, capsys)
    assert exit_code == 1
    assert "R004" in out
    assert "opsx-continue" in out


def test_r005_missing_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """R005 is emitted when a skill references a relative path that doesn't exist on disk."""
    _minimal_corpus(tmp_path)
    # nested/path/file.md has a slash so it triggers the path pattern
    body = "See `nested/path/file.md` for details.\n"
    make_fieldkit_skill(tmp_path, "my-skill", body=body)
    context = _context(tmp_path)
    exit_code, out = _run(context, capsys)
    assert exit_code == 1
    assert "R005" in out
    assert "nested/path/file.md" in out


def test_r006_dead_skills_use(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """R006 is emitted when skills_use() references a skill that doesn't exist."""
    _minimal_corpus(tmp_path)
    body = 'Use skills_use({name: "ghost-skill"}) to invoke the helper.\n'
    make_fieldkit_skill(tmp_path, "my-skill", body=body)
    context = _context(tmp_path)
    exit_code, out = _run(context, capsys)
    assert exit_code == 1
    assert "R006" in out
    assert "ghost-skill" in out


# ===========================================================================
# DAG tests (G001, G002)
# ===========================================================================


def test_g001_cycle(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """G001 is emitted when two skills reference each other in Related Skills (cycle)."""
    _minimal_corpus(tmp_path)
    # skill-a references skill-b and vice versa — both have valid complete frontmatter
    body_a = "\n## Related Skills\n\n- **skill-b** — The other skill.\n"
    body_b = "\n## Related Skills\n\n- **skill-a** — The other skill.\n"
    make_fieldkit_skill(tmp_path, "skill-a", body=body_a)
    make_fieldkit_skill(tmp_path, "skill-b", body=body_b)
    context = _context(tmp_path)
    exit_code, out = _run(context, capsys)
    assert exit_code == 1
    assert "G001" in out
    # Both skill names must appear in the cycle message
    assert "skill-a" in out
    assert "skill-b" in out
    assert "S003" not in out


def test_g002_orphan(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """G002 warning is emitted for a skill not referenced by any agent, command, or other skill
    and not marked slash: true."""
    _minimal_corpus(tmp_path)
    # No slash: true and no cross-references — should be G002
    make_fieldkit_skill(
        tmp_path,
        "lonely-skill",
        frontmatter="name: lonely-skill\ndescription: An unreferenced internal helper.",
    )
    context = _context(tmp_path)
    exit_code, out = _run(context, capsys)
    # G002 is a warning — exit code must be 0
    assert exit_code == 0, out
    assert "G002" in out
    assert "lonely-skill" in out
    # The note about global skills must appear
    assert "global skills" in out


# ===========================================================================
# Stale constant tests (C001, C002)
# ===========================================================================


def test_c001_stale_coverage(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """C001 is emitted when a skill declares --cov-fail-under=85 but pyproject.toml has 80."""
    _minimal_corpus(tmp_path)
    # pyproject.toml says 80, skill says 85
    make_pyproject_toml(tmp_path, fail_under=80)
    body = "Run with `--cov-fail-under=85` to enforce coverage.\n"
    make_fieldkit_skill(tmp_path, "my-skill", body=body)
    context = _context(tmp_path)
    exit_code, out = _run(context, capsys)
    assert exit_code == 1
    assert "C001" in out


def test_c002_stale_crap(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """C002 is emitted when a skill declares --max-crapload 5 but Makefile has 1."""
    _minimal_corpus(tmp_path)
    make_makefile(tmp_path, max_crapload=1)
    body = "Run with `--max-crapload 5` to allow more complexity.\n"
    make_fieldkit_skill(tmp_path, "my-skill", body=body)
    context = _context(tmp_path)
    exit_code, out = _run(context, capsys)
    assert exit_code == 1
    assert "C002" in out


# ===========================================================================
# Extra required tests
# ===========================================================================


def test_clean_corpus_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A corpus with no violations exits 0 and prints '0 errors'."""
    _minimal_corpus(tmp_path)
    # One valid fieldkit skill referenced by a command (avoids G002)
    make_fieldkit_skill(tmp_path, "meeting")
    make_command(tmp_path, "prep", body='Use skills_use({name: "meeting"}) to prepare.\n')
    context = _context(tmp_path)
    exit_code, out = _run(context, capsys)
    assert exit_code == 0
    assert "0 error" in out


def test_collect_all_violations_in_one_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Both S001 and S003 are emitted for a fieldkit skill missing name: and having user-invocable:."""
    _minimal_corpus(tmp_path)
    # No name: (S001) and legacy user-invocable: (S003)
    make_fieldkit_skill(
        tmp_path,
        "my-skill",
        frontmatter="description: Missing name and has legacy field.\nuser-invocable: true",
    )
    context = _context(tmp_path)
    exit_code, out = _run(context, capsys)
    assert exit_code == 1
    assert "S001" in out
    assert "S003" in out


def test_s003_not_emitted_for_project_skill_without_legacy_field(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """S003 is NOT emitted for a project skill that does not have user-invocable: at all."""
    _minimal_corpus(tmp_path)
    # Project skill with only description (no user-invocable) — valid, no S003
    make_project_skill(
        tmp_path,
        "my-skill",
        frontmatter="description: A project skill.",
    )
    context = _context(tmp_path)
    _, out = _run(context, capsys)
    assert "S003" not in out


def test_exit_code_1_when_error_and_warning_coexist(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Exit code is 1 when both a G002 warning and an S002 error are present."""
    _minimal_corpus(tmp_path)
    # S002 error: name mismatch in fieldkit skill
    make_fieldkit_skill(
        tmp_path,
        "right-name",
        frontmatter="name: wrong-name\ndescription: Mismatch.\nslash: true",
    )
    # G002 warning: another skill is orphaned (not referenced anywhere, no slash: true)
    make_fieldkit_skill(
        tmp_path,
        "orphan-skill",
        frontmatter="name: orphan-skill\ndescription: An unreferenced internal helper.",
    )
    context = _context(tmp_path)
    exit_code, out = _run(context, capsys)
    assert exit_code == 1
    assert "S002" in out
    assert "G002" in out


def test_report_json_schema(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--report writes valid JSON with all required fields; line is int or null per violation."""
    _minimal_corpus(tmp_path)
    # Introduce one S001 violation so the report has at least one violation object
    make_project_skill(tmp_path, "bad-skill", frontmatter="description: No name field here.")
    report_path = tmp_path / "report.json"
    context = _context(tmp_path)
    _run(context, capsys, args=["--report", str(report_path)])

    assert report_path.exists(), "Report file was not written"
    data = json.loads(report_path.read_text(encoding="utf-8"))

    # Top-level required fields
    assert "generated" in data
    assert "scanned" in data
    assert "summary" in data
    assert "violations" in data

    # scanned sub-fields
    scanned = data["scanned"]
    for key in ("project_skills", "fieldkit_skills", "agents", "commands"):
        assert key in scanned, f"Missing scanned.{key}"

    # summary sub-fields
    assert "errors" in data["summary"]
    assert "warnings" in data["summary"]

    # Each violation must have all required fields with correct types
    required_violation_fields = {"code", "severity", "fixable", "file", "line", "message", "suggestion"}
    for v in data["violations"]:
        missing = required_violation_fields - set(v.keys())
        assert not missing, f"Violation missing fields: {missing}"
        # line must be int or null
        assert v["line"] is None or isinstance(v["line"], int), (
            f"line field must be int or null, got {type(v['line'])!r}"
        )


@skip_if_root
def test_report_unwritable_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--report with an unwritable path exits 1 with an error message."""
    _minimal_corpus(tmp_path)
    bad_report = str(tmp_path / "nonexistent_subdir" / "report.json")
    # Make the subdir exist but read-only so the write fails
    bad_dir = tmp_path / "nonexistent_subdir"
    bad_dir.mkdir(parents=True, exist_ok=True)
    bad_dir.chmod(0o444)
    context = _context(tmp_path)
    argv = ["check_skill_integrity", "--report", bad_report]
    try:
        with pytest.raises(SystemExit) as exc_info:
            check_skill_integrity.main(argv, context=context)
        captured = capsys.readouterr()
        assert exc_info.value.code == 1
        assert "nonexistent_subdir" in captured.err or "report.json" in captured.err
    finally:
        bad_dir.chmod(0o755)


def test_empty_corpus_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An empty corpus (no skills, agents, or commands) exits 0 and prints '0 errors'."""
    _minimal_corpus(tmp_path)
    # Skill dirs exist but are empty — no SKILL.md files
    context = _context(tmp_path)
    exit_code, out = _run(context, capsys)
    assert exit_code == 0
    assert "0 error" in out


def test_r004_not_emitted_for_related_skills_entry(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """R004 is NOT emitted for a /skill-name reference inside the ## Related Skills section."""
    _minimal_corpus(tmp_path)
    # meeting exists as a skill
    make_fieldkit_skill(tmp_path, "meeting")
    # post-meeting references /meeting in Related Skills — must NOT trigger R004
    body = "\n## Related Skills\n\n- `/meeting` — Prepare for meetings.\n"
    make_fieldkit_skill(tmp_path, "post-meeting", body=body)
    context = _context(tmp_path)
    _, out = _run(context, capsys)
    assert "R004" not in out


def test_c001_prose_not_matched(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """C001 is NOT emitted for prose mentions of coverage percentages."""
    _minimal_corpus(tmp_path)
    make_pyproject_toml(tmp_path, fail_under=80)
    # Prose mention — no declaration pattern (fail_under = N or --cov-fail-under N)
    body = "This skill achieves 95% coverage of test cases in the suite.\n"
    make_fieldkit_skill(tmp_path, "my-skill", body=body)
    context = _context(tmp_path)
    _, out = _run(context, capsys)
    assert "C001" not in out


# ===========================================================================
# Additional edge-case and robustness tests
# ===========================================================================


def test_r001_r004_no_cross_fire(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """R004 is not emitted for backtick-slash entries in Related Skills (R001 handles them)."""
    _minimal_corpus(tmp_path)
    # ghost-skill doesn't exist — should trigger R001 but NOT R004
    body = "\n## Related Skills\n\n- `/ghost-skill` — A dead skill reference.\n"
    make_fieldkit_skill(tmp_path, "my-skill", body=body)
    context = _context(tmp_path)
    _, out = _run(context, capsys)
    assert "R001" in out
    assert "R004" not in out


def test_s001_emitted_for_missing_description(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """S001 is emitted when a fieldkit skill has name: but no description: field."""
    _minimal_corpus(tmp_path)
    make_fieldkit_skill(
        tmp_path,
        "my-skill",
        frontmatter="name: my-skill\nslash: true",
    )
    context = _context(tmp_path)
    exit_code, out = _run(context, capsys)
    assert exit_code == 1
    assert "S001" in out
    assert "description" in out


def test_r002_known_mcpjungle_group_no_violation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """R002 is NOT emitted for known mcpjungle groups (backstory, tavily, etc.)."""
    _minimal_corpus(tmp_path)
    # backstory is in KNOWN_MCPJUNGLE_GROUPS — must not trigger R002
    body = "Use `backstory__find_account` to look up account signals.\n"
    make_fieldkit_skill(tmp_path, "my-skill", body=body)
    context = _context(tmp_path)
    _, out = _run(context, capsys)
    assert "R002" not in out


def test_r005_data_repo_path_excluded(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """R005 is NOT emitted for paths starting with data-repo prefixes (accounts/, pursuits/, etc.)."""
    _minimal_corpus(tmp_path)
    # accounts/ is a data-repo prefix — must be excluded from R005
    body = "See `accounts/acme-corp/account.md` for account details.\n"
    make_fieldkit_skill(tmp_path, "my-skill", body=body)
    context = _context(tmp_path)
    _, out = _run(context, capsys)
    assert "R005" not in out


def test_r005_existing_path_no_violation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """R005 is NOT emitted when the referenced path actually exists on disk."""
    _minimal_corpus(tmp_path)
    # Create the referenced file so it exists
    ref_dir = tmp_path / "docs"
    ref_dir.mkdir(parents=True, exist_ok=True)
    (ref_dir / "guide.md").write_text("# Guide\n", encoding="utf-8")
    body = "See `docs/guide.md` for the full guide.\n"
    make_fieldkit_skill(tmp_path, "my-skill", body=body)
    context = _context(tmp_path)
    _, out = _run(context, capsys)
    assert "R005" not in out


def test_r006_existing_skill_no_violation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """R006 is NOT emitted when skills_use() references a skill that actually exists."""
    _minimal_corpus(tmp_path)
    make_fieldkit_skill(tmp_path, "real-skill")
    body = 'Use skills_use({name: "real-skill"}) to invoke the helper.\n'
    make_fieldkit_skill(tmp_path, "caller-skill", body=body)
    context = _context(tmp_path)
    _, out = _run(context, capsys)
    assert "R006" not in out


def test_g002_skill_referenced_by_command_not_orphan(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """G002 is NOT emitted for a skill that is referenced by a command file."""
    _minimal_corpus(tmp_path)
    make_fieldkit_skill(tmp_path, "meeting")
    # Command references meeting via skills_use
    make_command(tmp_path, "prep", body='skills_use({name: "meeting"})\n')
    context = _context(tmp_path)
    _, out = _run(context, capsys)
    # meeting is reachable from the command — no G002 for it
    assert not ("G002" in out and "meeting" in out), "meeting should not be orphan"


def test_multiple_skills_multiple_violations(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """All violations from multiple skills are collected before reporting (collect-all behavior)."""
    _minimal_corpus(tmp_path)
    # Skill A: S001 (missing name — fieldkit namespace requires name:)
    make_fieldkit_skill(tmp_path, "skill-a", frontmatter="description: No name field.")
    # Skill B: S002 (name mismatch)
    make_fieldkit_skill(
        tmp_path,
        "skill-b",
        frontmatter="name: wrong\ndescription: Mismatch.\nslash: true",
    )
    context = _context(tmp_path)
    exit_code, out = _run(context, capsys)
    assert exit_code == 1
    assert "S001" in out
    assert "S002" in out


def test_report_written_on_clean_corpus(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--report writes a valid JSON file even when there are 0 violations."""
    _minimal_corpus(tmp_path)
    report_path = tmp_path / "report.json"
    context = _context(tmp_path)
    exit_code, _out = _run(context, capsys, args=["--report", str(report_path)])
    assert exit_code == 0
    assert report_path.exists()
    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert data["summary"]["errors"] == 0
    assert isinstance(data["violations"], list)


def test_s004_boolean_true_not_flagged(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """S004 is NOT emitted when slash: is a proper boolean true."""
    _minimal_corpus(tmp_path)
    make_fieldkit_skill(
        tmp_path,
        "my-skill",
        frontmatter="name: my-skill\ndescription: Correct type.\nslash: true",
    )
    context = _context(tmp_path)
    _, out = _run(context, capsys)
    assert "S004" not in out


def test_c002_prose_not_matched(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """C002 is NOT emitted for prose mentions of CRAP scores (no --max-crapload pattern)."""
    _minimal_corpus(tmp_path)
    make_makefile(tmp_path, max_crapload=1)
    # Prose mention — no --max-crapload pattern
    body = "This keeps CRAP scores manageable and below acceptable thresholds.\n"
    make_fieldkit_skill(tmp_path, "my-skill", body=body)
    context = _context(tmp_path)
    _, out = _run(context, capsys)
    assert "C002" not in out


def test_r003_existing_agent_no_violation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """R003 is NOT emitted when the referenced agent actually exists."""
    _minimal_corpus(tmp_path)
    make_agent(tmp_path, "quality-reporter")
    # Same pattern as test_r003_missing_agent: .opencode/agents/ context + backtick name
    body = "See .opencode/agents/ for agent files. Delegates to `quality-reporter` for analysis.\n"
    make_command(tmp_path, "check-quality", body=body)
    context = _context(tmp_path)
    _, out = _run(context, capsys)
    assert "R003" not in out


def test_scanned_counts_in_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The validator reports scanned counts for project skills, fieldkit skills, agents, commands."""
    _minimal_corpus(tmp_path)
    make_project_skill(tmp_path, "proj-skill")
    make_fieldkit_skill(tmp_path, "fk-skill")
    make_agent(tmp_path, "some-agent")
    make_command(tmp_path, "some-command")
    context = _context(tmp_path)
    _, out = _run(context, capsys)
    # The output must mention scanned counts
    assert "Scanned" in out
    assert "project skill" in out
    assert "fieldkit skill" in out


def test_r004_existing_command_no_violation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """R004 is NOT emitted when the referenced command actually exists."""
    _minimal_corpus(tmp_path)
    make_command(tmp_path, "opsx-continue")
    body = "Run `/opsx-continue` to resume the workflow.\n"
    make_fieldkit_skill(tmp_path, "my-skill", body=body)
    context = _context(tmp_path)
    _, out = _run(context, capsys)
    assert "R004" not in out


def test_r005_not_emitted_inside_fenced_code_block(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """R005 is NOT emitted for path references inside fenced code blocks."""
    _minimal_corpus(tmp_path)
    body = "```\nSee `nested/path/file.md` for details.\n```\n"
    make_fieldkit_skill(tmp_path, "my-skill", body=body)
    context = _context(tmp_path)
    _, out = _run(context, capsys)
    assert "R005" not in out


def test_known_mcpjungle_groups_not_in_opencode_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """KNOWN_MCPJUNGLE_GROUPS must not overlap with registered MCP groups in opencode.json."""
    ocd = (_REPO_ROOT / "opencode.json").read_text(encoding="utf-8")
    registered = set(json.loads(ocd).get("mcp", {}).keys())
    assert references.KNOWN_MCPJUNGLE_GROUPS.isdisjoint(registered), (
        f"Groups in both allowlist and opencode.json: {references.KNOWN_MCPJUNGLE_GROUPS & registered}"
    )


def test_fixture_context_ignores_conflicting_real_skill(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fixture skill that shadows a real skill is validated only from the fixture root."""
    _minimal_corpus(tmp_path)
    make_fieldkit_skill(tmp_path, "meeting", body="Run `/fixture-only-missing` now.\n")
    context = _context(tmp_path)
    real_skill = _REPO_ROOT / "src" / "fieldkit" / "skills" / "meeting" / "SKILL.md"
    read_paths: list[Path] = []
    original_read_text = Path.read_text

    def recording_read_text(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        read_paths.append(path)
        return original_read_text(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", recording_read_text)

    exit_code, out = _run(context, capsys)

    assert exit_code == 1, out
    assert "fixture-only-missing" in out
    assert str(tmp_path) not in out
    assert real_skill not in read_paths


def test_n001_fires_for_orphan_fieldkit_skill(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """N001 fires when a fieldkit skill dir is not in SKILL_ROOTS."""
    make_opencode_json(tmp_path)
    make_makefile(tmp_path)
    make_pyproject_toml(tmp_path)
    (tmp_path / ".opencode" / "skills").mkdir(parents=True, exist_ok=True)
    make_fieldkit_skill(tmp_path, "orphan-mystery-skill")
    report_path = tmp_path / "report.json"
    context = _context(tmp_path)
    exit_code, out = _run(context, capsys, args=["--report", str(report_path)])
    assert exit_code == 1
    assert "N001" in out
    assert "orphan-mystery-skill" in out
    report = json.loads(report_path.read_text(encoding="utf-8"))
    n001 = next(violation for violation in report["violations"] if violation["code"] == "N001")
    assert "scripts/skill_integrity/model.py" in n001["suggestion"]


def test_n001_does_not_fire_for_known_skill(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """N001 must NOT fire for skills listed in SKILL_ROOTS."""
    make_opencode_json(tmp_path)
    make_makefile(tmp_path)
    make_pyproject_toml(tmp_path)
    (tmp_path / ".opencode" / "skills").mkdir(parents=True, exist_ok=True)
    make_fieldkit_skill(tmp_path, "grill")
    context = _context(tmp_path)
    _, out = _run(context, capsys)
    assert "N001" not in out


def test_n001_fires_per_orphan(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Each orphan skill gets its own N001 violation."""
    make_opencode_json(tmp_path)
    make_makefile(tmp_path)
    make_pyproject_toml(tmp_path)
    (tmp_path / ".opencode" / "skills").mkdir(parents=True, exist_ok=True)
    make_fieldkit_skill(tmp_path, "phantom-alpha")
    make_fieldkit_skill(tmp_path, "phantom-beta")
    make_fieldkit_skill(tmp_path, "grill")
    context = _context(tmp_path)
    exit_code, out = _run(context, capsys)
    assert exit_code == 1
    assert out.count("N001") == 2
    assert "phantom-alpha" in out
    assert "phantom-beta" in out
    n001_lines = [line for line in out.splitlines() if "N001" in line]
    assert all("grill" not in line for line in n001_lines)


def test_c001_uses_default_floor_when_pyproject_missing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """When pyproject.toml is absent, coverage floor defaults to 80."""
    make_opencode_json(tmp_path)
    make_makefile(tmp_path)
    (tmp_path / ".opencode" / "skills").mkdir(parents=True, exist_ok=True)
    (tmp_path / "fieldkit" / "skills").mkdir(parents=True, exist_ok=True)
    # No pyproject.toml — default floor is 80
    body = "Run with `--cov-fail-under=85` to enforce coverage.\n"
    make_fieldkit_skill(tmp_path, "my-skill", body=body)
    context = _context(tmp_path)
    exit_code, out = _run(context, capsys)
    assert exit_code == 1
    assert "C001" in out  # 85 != 80 (default)
