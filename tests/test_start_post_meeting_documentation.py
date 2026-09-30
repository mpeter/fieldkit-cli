"""Semantic contracts for the start, post-meeting, and always-on skills."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SKILLS = Path(__file__).parents[1] / "src/fieldkit/skills"


def _one_line(content: str) -> str:
    return " ".join(content.split())


def test_start_skill_uses_the_installed_offline_first_journey() -> None:
    content = (_SKILLS / "start/SKILL.md").read_text(encoding="utf-8")
    one_line = _one_line(content)

    for command in (
        "fieldkit --version",
        "fieldkit init --help",
        "fieldkit init --minimal PATH",
        "fieldkit skill list",
    ):
        assert command in content
    assert "does **not** create accounts, `TASKS.md`, lesson memory, or credentials" in one_line
    assert "Never run `fieldkit gmail sync` simply because the workspace is new" in one_line


def test_post_meeting_skill_keeps_every_write_separately_approved() -> None:
    content = (_SKILLS / "post-meeting/SKILL.md").read_text(encoding="utf-8")
    one_line = _one_line(content)

    assert "no customer email, Salesforce field, Google Task, workbook, or workspace file changes" in one_line
    assert "fieldkit ingest run` has no account filter" in content
    assert "Do not send" in one_line
    assert "Ask before creating or changing a file" in content
    assert "written-and-verified, skipped, pending, or unavailable" in one_line


def test_always_on_guidance_cannot_create_external_work_by_itself() -> None:
    content = (_SKILLS / "always-on-guidance/SKILL.md").read_text(encoding="utf-8")
    one_line = _one_line(content)

    assert (
        "does not create product behavior, GitHub issues, pull requests, or configuration changes by itself" in one_line
    )
    assert "wait for the operator to approve execution" in content
    assert "do not create or submit it without approval" in content
