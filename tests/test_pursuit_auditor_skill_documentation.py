"""Semantic contract for the public pursuit-auditor skill."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SKILL = Path(__file__).parents[1] / "src/fieldkit/skills/pursuit-auditor/SKILL.md"


def test_pursuit_auditor_distinguishes_read_preview_and_write_modes() -> None:
    content = _SKILL.read_text(encoding="utf-8")
    one_line = " ".join(content.split())

    for required in (
        "`--json` without `--fix` emits findings without a report",
        "`--dry-run` requires `--fix`",
        "cannot be combined with `--json`, `--output`, or `--check-yaml`",
        "`--fix --json` still edits pursuit files",
        "JSON suppresses only the report, not requested fixes",
    ):
        assert required in one_line


def test_pursuit_auditor_keeps_missing_data_and_qualification_non_passing() -> None:
    content = _SKILL.read_text(encoding="utf-8")
    one_line = " ".join(content.split())

    assert "current qualification is reported as `unavailable`" in one_line
    assert "historical local values are never interpreted" in one_line
    assert "An empty workspace is not a passing audit" in one_line
    assert "exits 3, including in JSON mode" in one_line
    assert "Missing JSON is not a successful empty array" in one_line
    assert "Do not use audit output as a qualification pass" in content


def test_pursuit_auditor_bounds_fixes_and_private_report_writes() -> None:
    content = _SKILL.read_text(encoding="utf-8")
    one_line = " ".join(content.split())

    assert "implementation's explicit rename table" in one_line
    assert "underscore form `sf_opportunity_number` is canonical" in one_line
    assert "Stop on refused or failed repairs" in one_line
    assert "Reread each changed file" in one_line
    assert "Repeating the same date and account can overwrite that report" in one_line
    assert "separately approved safe destination" in one_line
    assert "inside the configured workspace's `accounts/.audit/`" in one_line
    assert "Relative output names resolve there" in one_line
    assert "through symlinks are refused before any repair or report write" in one_line
    assert "keep them private, not in public issues or release evidence" in one_line
    assert "Verify the saved result is nonempty" in one_line
    assert "```" not in content
    assert "|---" not in content
