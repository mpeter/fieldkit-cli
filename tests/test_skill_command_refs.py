"""Tests for executable fieldkit command references in skills."""

from pathlib import Path

import pytest

from scripts.skill_integrity import command_refs, model
from scripts.skill_integrity.command_refs import IGNORE_NEXT, validate_skill_command_refs

pytestmark = pytest.mark.unit

_COMMANDS = {
    ("init",): True,
    ("issue",): False,
    ("issue", "create"): True,
    ("issue", "show"): True,
    ("sf",): False,
    ("sf", "account"): True,
    ("watch",): False,
    ("watch", "run"): False,
    ("watch", "run", "pursuit-stalls"): True,
}


def _skill(tmp_path: Path, text: str, name: str = "SKILL.md") -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "invocation",
    [
        "`fieldkit init`",
        "`fieldkit issue create --type bug`",
        "`fieldkit sf account <name>`",
        "`fieldkit issue create ...`",
        "`fieldkit issue create/show`",
        "```bash\nfieldkit init; echo ready\n```",
        "```bash\nfieldkit issue create&&echo ready\n```",
        "```bash\nfieldkit init&\n```",
        "```bash\nfieldkit issue create&\n```",
        "```bash\n$ fieldkit watch run pursuit-stalls --dry-run\n```",
        "```console\nuv run fieldkit issue create --title x\n```",
    ],
)
def test_valid_command_paths_accept_arguments_and_prefixes(tmp_path: Path, invocation: str) -> None:
    path = _skill(tmp_path, invocation)

    result = validate_skill_command_refs([path], _COMMANDS)

    assert result == []


@pytest.mark.parametrize(
    "text",
    [
        "Prose says fieldkit issue raise is obsolete.",
        "```python\ncommand = 'fieldkit issue raise'\n```",
        "```bash\nrg 'fieldkit issue raise' src/\n```",
    ],
)
def test_non_executable_mentions_are_ignored(tmp_path: Path, text: str) -> None:
    path = _skill(tmp_path, text)

    result = validate_skill_command_refs([path], _COMMANDS)

    assert result == []


@pytest.mark.parametrize(
    ("command", "expected"),
    [("fieldkit issue raise --title x", "issue raise"), ("fieldkit vanished", "vanished")],
)
def test_unknown_command_reports_file_line_and_path(tmp_path: Path, command: str, expected: str) -> None:
    path = _skill(tmp_path, f"# Skill\n\n```bash\n{command}\n```\n")

    result = validate_skill_command_refs([path], _COMMANDS)

    assert len(result) == 1
    assert result[0].file == path
    assert result[0].line == 4
    assert result[0].message == f"unknown fieldkit command path: {expected}"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("fieldkit Issue create", "Issue"),
        ("fieldkit 404", "404"),
        ("fieldkit issue Create", "issue Create"),
        ("fieldkit issue create_bad", "issue create_bad"),
    ],
)
def test_malformed_command_words_fail_closed(tmp_path: Path, command: str, expected: str) -> None:
    path = _skill(tmp_path, f"`{command}`\n")

    result = validate_skill_command_refs([path], _COMMANDS)

    assert len(result) == 1
    assert result[0].message == f"unknown fieldkit command path: {expected}"


def test_slash_shorthand_cannot_hide_an_invalid_group_token(tmp_path: Path) -> None:
    path = _skill(tmp_path, "`fieldkit issue/create`\n")

    result = validate_skill_command_refs([path], _COMMANDS)

    assert len(result) == 1
    assert result[0].message == "unknown fieldkit command path: issue/create"


def test_ignore_next_suppresses_only_one_invocation(tmp_path: Path) -> None:
    path = _skill(tmp_path, f"{IGNORE_NEXT}\n`fieldkit issue raise`\n`fieldkit issue missing`\n")

    result = validate_skill_command_refs([path], _COMMANDS)

    assert len(result) == 1
    assert result[0].line == 3


def test_unused_ignore_marker_is_an_error(tmp_path: Path) -> None:
    path = _skill(tmp_path, f"{IGNORE_NEXT}\nNo command follows.\n")

    result = validate_skill_command_refs([path], _COMMANDS)

    assert len(result) == 1
    assert result[0].line == 1
    assert "unused exception marker" in result[0].message


def test_marker_documented_in_non_shell_fence_cannot_suppress_command(tmp_path: Path) -> None:
    path = _skill(tmp_path, f"```markdown\n{IGNORE_NEXT}\n```\n`fieldkit issue raise`\n")

    result = validate_skill_command_refs([path], _COMMANDS)

    assert len(result) == 1
    assert result[0].message == "unknown fieldkit command path: issue raise"


def test_marker_documented_as_inline_code_cannot_suppress_command(tmp_path: Path) -> None:
    path = _skill(tmp_path, f"`{IGNORE_NEXT}`\n`fieldkit issue raise`\n")

    result = validate_skill_command_refs([path], _COMMANDS)

    assert len(result) == 1
    assert result[0].message == "unknown fieldkit command path: issue raise"


def test_duplicate_canonical_path_is_scanned_once(tmp_path: Path) -> None:
    path = _skill(tmp_path, "`fieldkit issue raise`\n")

    result = validate_skill_command_refs([path, path], _COMMANDS)

    assert len(result) == 1


def test_integrity_adapter_scans_both_canonical_skill_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _skill(tmp_path, "`fieldkit issue raise`\n", ".opencode/skills/dev/SKILL.md")
    shipped = _skill(tmp_path, "`fieldkit vanished`\n", "src/fieldkit/skills/user/SKILL.md")
    nested = _skill(tmp_path, "`fieldkit issue missing`\n", "src/fieldkit/skills/user/ops/check.md")
    registry = model.Registry(project_skills={"dev": project}, fieldkit_skills={"user": shipped})
    context = model.IntegrityContext.from_root(tmp_path, skill_roots=frozenset())
    monkeypatch.setattr(command_refs, "command_tree", lambda: _COMMANDS)

    result = command_refs.check_command_refs(context, registry)

    assert [(item.code, item.file) for item in result] == [
        ("R007", ".opencode/skills/dev/SKILL.md"),
        ("R007", "src/fieldkit/skills/user/SKILL.md"),
        ("R007", str(nested.relative_to(tmp_path))),
    ]
