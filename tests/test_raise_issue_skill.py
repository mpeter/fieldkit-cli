"""Contracts for the project-local raise-issue skill."""

import re
from pathlib import Path

import pytest

from fieldkit.commands.issue.gh_store import GHIssueStore

pytestmark = pytest.mark.unit


def test_documented_modules_match_runtime_authority() -> None:
    skill_path = Path(__file__).parents[1] / ".opencode" / "skills" / "raise-issue" / "SKILL.md"
    skill_text = skill_path.read_text(encoding="utf-8")
    module_row = next(line for line in skill_text.splitlines() if line.startswith("| `--module` |"))
    documented_modules = set(re.findall(r"`([a-z-]+)`", module_row)) - {"--module"}

    runtime_modules = GHIssueStore("owner/repo").known_modules()
    assert runtime_modules == documented_modules
