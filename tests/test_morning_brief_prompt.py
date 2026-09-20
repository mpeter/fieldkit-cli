"""Regression test: synthesize() prompt must not contain unsatisfiable directives.

Covers tasks 2.1-2.3 from openspec/changes/brief-hallucinated-sections/tasks.md:
- The prompt must NOT contain "Search Gmail", "Google Calendar",
  "Read CLAUDE.md", "list all meetings", or "list meetings".
- The prompt MUST still contain the section headers "📬 Overnight Email Triage"
  and "📅 Today's Calendar" (static placeholder blocks, not directives).
"""

import contextlib
from pathlib import Path
from unittest.mock import patch

import pytest

# The synthesize() prompt must not instruct the LLM to query tools it cannot access.


@pytest.mark.unit
def test_prompt_contains_no_unsatisfiable_directives(tmp_path: Path) -> None:
    """Capture the prompt passed to synthesize() and assert no tool-call directives."""
    from fieldkit.commands.brief.main import _run
    from fieldkit.config import ConfigError

    captured_prompt: list[str] = []

    def _capture_synthesize(prompt: str, **kwargs: object) -> str:
        captured_prompt.append(prompt)
        return "## ☀️ Morning Brief — 2026-07-11\n\nMock brief content."

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("fieldkit.commands.brief.main.synthesize", side_effect=_capture_synthesize))
        stack.enter_context(
            patch("fieldkit.commands.brief.main.get_fieldkit_home", side_effect=ConfigError("no config"))
        )
        stack.enter_context(patch("fieldkit.commands.brief.main.get_fieldkit_root", return_value=tmp_path))
        stack.enter_context(patch("fieldkit.commands.brief.main.collect_pursuit_alerts", return_value="no alerts"))
        stack.enter_context(patch("fieldkit.commands.brief.main.collect_champion_signals", return_value="no signals"))
        stack.enter_context(patch("fieldkit.commands.brief.main.collect_decay_signals", return_value="no decay"))
        stack.enter_context(patch("fieldkit.commands.brief.main.collect_stale_prose", return_value="no stale"))
        stack.enter_context(
            patch("fieldkit.commands.brief.main.collect_tasks", return_value=("no tasks", "no waiting"))
        )
        stack.enter_context(patch("fieldkit.commands.brief.main._render_degraded_section", return_value=""))
        stack.enter_context(patch("fieldkit.commands.brief.main._collect_degraded_sources", return_value=[]))

        _run(no_llm=False, account=None)

    assert captured_prompt, "synthesize() was never called — prompt not captured"
    prompt = captured_prompt[0]

    # Task 2.2: must NOT contain unsatisfiable directives
    forbidden = [
        "Search Gmail",
        "Google Calendar",
        "Read CLAUDE.md",
        "list all meetings",
        "list meetings",
    ]
    for phrase in forbidden:
        assert phrase not in prompt, f"Prompt still contains unsatisfiable directive: {phrase!r}"

    # Task 2.3: section headers MUST still be present (static placeholders)
    assert "📬 Overnight Email Triage" in prompt, "Missing email triage section header"
    assert "📅 Today's Calendar" in prompt, "Missing calendar section header"
