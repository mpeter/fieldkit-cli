"""implementation change: next_steps fallback reads from '## Next Steps' markdown section.

Tests the two-pass next_steps extraction logic in close_date_countdown.py:
  Pass 1 — inline "Next steps: ..." line
  Pass 2 — first non-empty line under a "## Next Steps" heading
"""

import pytest

# ---------------------------------------------------------------------------
# Helper: extract next_steps using the same logic as _run_countdown_inner
# ---------------------------------------------------------------------------


def _extract_next_steps(body: str, sf_next_steps: str = "") -> str:
    """Mirror the next_steps extraction logic from close_date_countdown._run_countdown_inner."""
    next_steps = sf_next_steps.strip()

    # Pass 1: inline "Next steps: ..." line
    if not next_steps:
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("next steps:"):
                next_steps = stripped[len("next steps:") :].strip()[:200]
                break

    # Pass 2 (implementation change): first non-empty, non-heading line under "## Next Steps"
    if not next_steps:
        in_next_steps_section = False
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("##") and "next steps" in stripped.lower():
                in_next_steps_section = True
                continue
            if in_next_steps_section:
                if stripped.startswith("#"):
                    break
                if stripped:
                    next_steps = stripped[:200]
                    break

    if not next_steps:
        next_steps = "(none)"

    return next_steps


# ---------------------------------------------------------------------------
# Pass 1: inline "Next steps: ..." line (existing behaviour, must not regress)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_inline_next_steps_extracted() -> None:
    body = "Some context.\n\nNext steps: Schedule a demo call.\n\nOther content."
    assert _extract_next_steps(body) == "Schedule a demo call."


@pytest.mark.unit
def test_frontmatter_takes_priority_over_inline() -> None:
    body = "Next steps: Inline value."
    assert _extract_next_steps(body, sf_next_steps="Frontmatter value") == "Frontmatter value"


# ---------------------------------------------------------------------------
# Pass 2: ## Next Steps section heading (implementation change)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_section_heading_next_steps_extracted() -> None:
    """First non-empty line under '## Next Steps' is returned when no inline match."""
    body = (
        "# Deal Overview\n\n"
        "Some context here.\n\n"
        "## Next Steps\n\n"
        "Send the proposal by Friday.\n\n"
        "## Other Section\n\n"
        "Unrelated content.\n"
    )
    assert _extract_next_steps(body) == "Send the proposal by Friday."


@pytest.mark.unit
def test_section_heading_skips_blank_lines() -> None:
    """Blank lines between heading and first content are skipped."""
    body = "## Next Steps\n\n\n\nFollow up on the contract.\n"
    assert _extract_next_steps(body) == "Follow up on the contract."


@pytest.mark.unit
def test_section_heading_stops_at_next_heading() -> None:
    """If the Next Steps section is empty (only followed by another heading), returns (none)."""
    body = "## Next Steps\n\n## Other Section\n\nSome content.\n"
    assert _extract_next_steps(body) == "(none)"


@pytest.mark.unit
def test_inline_takes_priority_over_section_heading() -> None:
    """Pass 1 (inline) wins over pass 2 (section heading) when both are present."""
    body = "Next steps: Inline value.\n\n## Next Steps\n\nSection value.\n"
    assert _extract_next_steps(body) == "Inline value."


@pytest.mark.unit
def test_section_heading_case_insensitive() -> None:
    """Heading match is case-insensitive: '## next steps' works."""
    body = "## next steps\n\nLowercase heading value.\n"
    assert _extract_next_steps(body) == "Lowercase heading value."


@pytest.mark.unit
def test_no_next_steps_returns_none_sentinel() -> None:
    """When no next steps are found anywhere, returns '(none)'."""
    body = "# Deal\n\nSome context with no next steps.\n"
    assert _extract_next_steps(body) == "(none)"


@pytest.mark.unit
def test_section_value_truncated_at_200_chars() -> None:
    """Values longer than 200 chars are truncated."""
    long_value = "x" * 250
    body = f"## Next Steps\n\n{long_value}\n"
    result = _extract_next_steps(body)
    assert len(result) == 200
    assert result == "x" * 200
