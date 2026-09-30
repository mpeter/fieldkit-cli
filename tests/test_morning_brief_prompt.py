"""The account brief prompt scopes missing sources without inventing capabilities.

Email and calendar headings remain static placeholders, not tool-call directives.
"""

import contextlib
from pathlib import Path
from unittest.mock import patch

import pytest

# The synthesize() prompt must not instruct the LLM to query tools it cannot access.


@pytest.mark.unit
def test_prompt_contains_no_unsatisfiable_directives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Capture the prompt passed to synthesize() and assert no tool-call directives."""
    from fieldkit.brief.pipeline_only import generate_pipeline_only

    captured_prompt: list[str] = []
    monkeypatch.delenv("FIELDKIT_NO_LLM", raising=False)

    def _capture_synthesize(prompt: str, **kwargs: object) -> str:
        captured_prompt.append(prompt)
        return "## ☀️ Morning Brief — 2026-07-11\n\nMock brief content."

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("fieldkit.brief.pipeline_only.synthesize", side_effect=_capture_synthesize))
        stack.enter_context(patch("fieldkit.brief.pipeline_only.get_llm_model", return_value="vertex_ai/test-model"))
        stack.enter_context(patch("fieldkit.brief.pipeline_only.get_fieldkit_home", return_value=tmp_path))
        stack.enter_context(patch("fieldkit.brief.pipeline_only.collect_pursuit_alerts", return_value="no alerts"))
        stack.enter_context(patch("fieldkit.brief.pipeline_only.collect_champion_signals", return_value="no signals"))
        stack.enter_context(patch("fieldkit.brief.pipeline_only.collect_decay_signals", return_value="no decay"))
        stack.enter_context(patch("fieldkit.brief.pipeline_only.collect_stale_prose", return_value="no stale"))
        stack.enter_context(
            patch("fieldkit.brief.pipeline_only.collect_tasks", return_value=("no tasks", "no waiting"))
        )
        stack.enter_context(patch("fieldkit.brief.pipeline_only._render_degraded_section", return_value=""))
        stack.enter_context(patch("fieldkit.brief.pipeline_only._collect_degraded_sources", return_value=[]))

        generate_pipeline_only(no_llm=False, account=None)

    assert len(captured_prompt) == 1, "synthesize() must capture exactly one actual provider prompt"
    prompt = captured_prompt[0]

    forbidden = [
        "Search Gmail",
        "Google Calendar",
        "Read CLAUDE.md",
        "list all meetings",
        "list meetings",
        "fieldkit gmail inbox",
        "Calendar data is not yet integrated",
    ]
    for phrase in forbidden:
        assert phrase not in prompt, f"Prompt still contains unsatisfiable directive: {phrase!r}"

    assert "📬 Overnight Email Triage" in prompt, "Missing email triage section header"
    assert "📅 Today's Calendar" in prompt, "Missing calendar section header"
    assert "Overnight email is not collected by this account brief. Review your mail directly." in prompt
    assert "Calendar events are not collected by this account brief. Review your calendar directly." in prompt
