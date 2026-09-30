"""Shared ownership parsing recognizes structure rather than marker substrings."""

import pytest

from fieldkit.util.owned_markdown import OwnedMarker, inspect_owned_markdown, read_owned_markers

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("prefix,section", [("fieldkit-task:", "Active"), ("fieldkit-ingest-pursuit:", "Activity Log")])
def test_owned_marker_contract_is_shared(prefix: str, section: str) -> None:
    identity, fingerprint = "a" * 64, "b" * 64
    line = f"- [x] Edited entry <!-- {prefix}v1:{identity}:{fingerprint} -->"
    content = f"## {section}\n{line}\n"
    result = read_owned_markers(content, prefix=prefix, section_titles=(section,), error_message="Invalid ownership")
    assert result == {identity: OwnedMarker(fingerprint, section)}
    with pytest.raises(ValueError, match="Invalid ownership"):
        read_owned_markers(content + line, prefix=prefix, section_titles=(section,), error_message="Invalid ownership")


def test_activity_section_retains_offsets_and_inline_marker() -> None:
    content = "## Activity Log\n- Meeting <!-- owned -->\n"
    result = inspect_owned_markdown(content, section_titles=("Activity Log",))
    assert result.section_ends == {"Activity Log": [len("## Activity Log\n")]}
    assert result.bullet_lines[1].section == "Activity Log"
    assert result.bullet_lines[1].final_html == "<!-- owned -->"


@pytest.mark.parametrize(
    "content",
    [
        "```md\n## Activity Log\n- Meeting <!-- owned -->\n```\n",
        "> ## Activity Log\n> - Meeting <!-- owned -->\n",
        "## Other\n- Meeting <!-- owned -->\n",
        "## Activity Log\n<!--\n- Meeting <!-- owned -->\n-->\n",
    ],
)
def test_non_owned_examples_are_excluded(content: str) -> None:
    result = inspect_owned_markdown(content, section_titles=("Activity Log",))
    assert result.bullet_lines == {}


def test_duplicate_activity_sections_are_ambiguous() -> None:
    with pytest.raises(ValueError, match="Ambiguous Markdown section"):
        inspect_owned_markdown("## Activity Log\n\n## Activity Log\n", section_titles=("Activity Log",))
