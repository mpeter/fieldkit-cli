"""Focused R003 agent-reference tests for the skill integrity checker."""

from pathlib import Path

import pytest
from skill_integrity import references
from test_check_skill_integrity import (
    _context,
    _minimal_corpus,
    _run,
    make_agent,
    make_opencode_json,
    make_project_skill,
)

pytestmark = pytest.mark.unit


def test_r003_frontmatter_agent_missing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """R003 is emitted when a skill's frontmatter 'agent:' names a non-existent agent."""
    _minimal_corpus(tmp_path)
    make_project_skill(
        tmp_path,
        "my-skill",
        frontmatter="description: A skill.\nagent: ghost-agent",
    )
    context = _context(tmp_path)
    exit_code, out = _run(context, capsys)
    assert exit_code == 1
    assert "R003" in out
    assert "ghost-agent" in out


def test_r003_frontmatter_agent_exists(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """R003 is NOT emitted when a skill's frontmatter 'agent:' names an existing agent file."""
    _minimal_corpus(tmp_path)
    make_agent(tmp_path, "real-agent")
    make_project_skill(
        tmp_path,
        "my-skill",
        frontmatter="description: A skill.\nagent: real-agent",
    )
    context = _context(tmp_path)
    _, out = _run(context, capsys)
    assert "R003" not in out


def test_r003_frontmatter_agent_exists_in_opencode_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """R003 accepts an agent registered in opencode.json without a local agent file."""
    _minimal_corpus(tmp_path)
    make_opencode_json(tmp_path, agents={"build": {"mode": "subagent"}})
    make_project_skill(tmp_path, "my-skill", frontmatter="description: A skill.\nagent: build")
    context = _context(tmp_path)
    _, out = _run(context, capsys)
    assert "R003" not in out


def test_r003_frontmatter_agent_exists_in_project_opencode_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """R003 accepts an agent registered in the project-local OpenCode config."""
    _minimal_corpus(tmp_path)
    project_config = tmp_path / ".opencode" / "opencode.jsonc"
    project_config.parent.mkdir(parents=True, exist_ok=True)
    project_config.write_text(
        '{\n  // Review agents\n  "agent": {\n    "proctor-sonnet": {"mode": "subagent"},\n  },\n}\n',
        encoding="utf-8",
    )
    make_project_skill(tmp_path, "my-skill", frontmatter="description: A skill.\nagent: proctor-sonnet")
    context = _context(tmp_path)
    _, out = _run(context, capsys)
    assert "R003" not in out


def test_r003_frontmatter_agent_exists_as_harness_type(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """R003 accepts an explicitly registered harness-only agent type."""
    _minimal_corpus(tmp_path)
    make_project_skill(tmp_path, "my-skill", frontmatter="description: A skill.\nagent: harness-agent")
    context = _context(tmp_path)
    monkeypatch.setattr(references, "KNOWN_HARNESS_AGENT_TYPES", frozenset({"harness-agent"}))
    _, out = _run(context, capsys)
    assert "R003" not in out


def test_r003_frontmatter_ignores_nested_agent_and_reports_top_level_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """R003 validates the top-level dispatch agent and reports its exact line."""
    _minimal_corpus(tmp_path)
    make_agent(tmp_path, "real-agent")
    make_project_skill(
        tmp_path,
        "my-skill",
        frontmatter="description: A skill.\nagent: ghost-agent\nmetadata:\n  agent: real-agent",
    )
    context = _context(tmp_path)
    exit_code, out = _run(context, capsys)
    assert exit_code == 1
    assert "ghost-agent" in out
    assert "SKILL.md:3" in out


def test_r003_frontmatter_agent_allows_inline_comment(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """R003 ignores a YAML inline comment following a valid agent name."""
    _minimal_corpus(tmp_path)
    make_agent(tmp_path, "real-agent")
    make_project_skill(
        tmp_path,
        "my-skill",
        frontmatter="description: A skill.\nagent: real-agent # dispatch owner",
    )
    context = _context(tmp_path)
    _, out = _run(context, capsys)
    assert "R003" not in out


@pytest.mark.parametrize(
    "agent_field",
    [
        '"agent": real-agent',
        "agent: >-\n  real-agent",
    ],
)
def test_r003_frontmatter_agent_supports_valid_yaml_forms(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    agent_field: str,
) -> None:
    """R003 recognizes quoted keys and folded scalar agent values."""
    _minimal_corpus(tmp_path)
    make_agent(tmp_path, "real-agent")
    make_project_skill(tmp_path, "my-skill", frontmatter=f"description: A skill.\n{agent_field}")
    context = _context(tmp_path)
    _, out = _run(context, capsys)
    assert "R003" not in out


def test_r003_frontmatter_literal_scalar_preserves_quotes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """R003 validates the literal scalar value without stripping its content."""
    _minimal_corpus(tmp_path)
    make_agent(tmp_path, "real-agent")
    make_project_skill(
        tmp_path,
        "my-skill",
        frontmatter='description: A skill.\nagent: |-\n  "real-agent"',
    )
    context = _context(tmp_path)
    exit_code, out = _run(context, capsys)
    assert exit_code == 1
    assert "R003" in out
    assert '"real-agent"' in out


@pytest.mark.parametrize(
    "agent_field",
    [
        "agent: >\n  real-agent",
        "agent: |-\n  real-agent\n  ---\n  ghost-agent",
    ],
)
def test_r003_frontmatter_preserves_complete_block_scalar(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    agent_field: str,
) -> None:
    """R003 validates block scalar newlines and indented delimiter text faithfully."""
    _minimal_corpus(tmp_path)
    make_agent(tmp_path, "real-agent")
    make_project_skill(tmp_path, "my-skill", frontmatter=f"description: A skill.\n{agent_field}")
    context = _context(tmp_path)
    exit_code, out = _run(context, capsys)
    assert exit_code == 1
    assert "R003" in out


def test_r003_frontmatter_uses_last_duplicate_agent_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """R003 matches PyYAML's last-value behavior for duplicate mapping keys."""
    _minimal_corpus(tmp_path)
    make_agent(tmp_path, "real-agent")
    make_project_skill(
        tmp_path,
        "my-skill",
        frontmatter="description: A skill.\nagent: real-agent\nagent: ghost-agent",
    )
    context = _context(tmp_path)
    exit_code, out = _run(context, capsys)
    assert exit_code == 1
    assert "ghost-agent" in out
    assert "SKILL.md:4" in out
