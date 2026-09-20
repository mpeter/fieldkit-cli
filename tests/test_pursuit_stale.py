"""Test module for fieldkit.pursuit.stale.

Covers:
- ``_parse_frontmatter(text: str) -> dict[str, str]`` -- line-by-line k:v parsing,
  returns ``{}`` on empty/missing frontmatter
- ``check_file(path: str) -> list[str]`` -- returns warning strings (empty = no issues)
- ``main()`` -- reads ``sys.argv[1:]``; sys.exit(1) when called with no arguments

Populated by parallel tasks 2.9-2.14 in the crap-reduction phase.
"""

import sys

import pytest

from fieldkit.errors import PursuitStaleError
from fieldkit.pursuit.stale import _parse_frontmatter, check_file, main

# ---------------------------------------------------------------------------
# Task 2.9 — _parse_frontmatter
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_frontmatter_returns_empty_on_no_text() -> None:
    """Empty string has no frontmatter block → returns {}."""
    # extract_frontmatter_text requires '---\n...\n---' anchored at position 0.
    # An empty string matches nothing, so _parse_frontmatter must return {}.
    result = _parse_frontmatter("")
    assert result == {}


@pytest.mark.unit
def test_parse_frontmatter_returns_empty_on_no_colon_lines() -> None:
    """Frontmatter block with no 'key: value' lines → returns {}.

    The block is syntactically valid (has --- delimiters) but contains only
    prose-style lines with no colon separator, so the inner loop produces no
    entries and the function returns an empty dict.
    """
    text = "---\nno colon here\njust plain text\n---\n"
    result = _parse_frontmatter(text)
    assert result == {}


@pytest.mark.unit
def test_parse_frontmatter_returns_parsed_dict() -> None:
    """Frontmatter block with 'key: value' lines → returns populated dict."""
    text = "---\ntitle: My Pursuit\nstage: Qualify\n---\n"
    result = _parse_frontmatter(text)
    assert result == {"title": "My Pursuit", "stage": "Qualify"}


# ---------------------------------------------------------------------------
# Task 2.10 — check_file
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_file_returns_empty_list_when_no_frontmatter(tmp_path: pytest.TempPathFactory) -> None:
    """File with no '---' delimiters has no frontmatter → check_file returns []."""
    pursuit = tmp_path / "no_fm.md"  # type: ignore[operator]
    pursuit.write_text("Just some prose with no frontmatter block.\n", encoding="utf-8")
    result = check_file(str(pursuit))
    assert result == []


@pytest.mark.unit
def test_check_file_returns_empty_list_when_no_contradiction(tmp_path: pytest.TempPathFactory) -> None:
    """File with valid frontmatter whose prose does not contradict it → returns []."""
    # sf_opportunity_id is set, but the body does NOT contain the stale-prose
    # patterns that would trigger a warning (e.g. 'no SF opportunity').
    content = (
        "---\n"
        "title: Clean Deal\n"
        "sf_opportunity_id: OPP-001\n"
        "sf_stage: Qualify\n"
        "---\n\n"
        "Everything is consistent. The deal is progressing well.\n"
    )
    pursuit = tmp_path / "clean.md"  # type: ignore[operator]
    pursuit.write_text(content, encoding="utf-8")
    result = check_file(str(pursuit))
    assert result == []


@pytest.mark.unit
def test_check_file_returns_warnings_on_contradiction(tmp_path: pytest.TempPathFactory) -> None:
    """File where frontmatter and prose conflict → returns non-empty list of strings.

    Trigger: sf_opportunity_id is set in frontmatter, but the body contains
    'no SF opportunity' — matching the first CHECKS entry.
    """
    content = (
        "---\n"
        "title: Stale Deal\n"
        "sf_opportunity_id: OPP-999\n"
        "---\n\n"
        "The pipeline (no SF opportunity) has not been updated yet.\n"
    )
    pursuit = tmp_path / "stale.md"  # type: ignore[operator]
    pursuit.write_text(content, encoding="utf-8")
    result = check_file(str(pursuit))
    assert len(result) > 0
    assert all(isinstance(w, str) for w in result)


# ---------------------------------------------------------------------------
# Task 2.11 — main
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_main_exits_nonzero_on_no_args(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() must raise PursuitStaleError when invoked with no positional arguments."""
    # Patch argv to simulate bare invocation with no file/dir arguments.
    monkeypatch.setattr(sys, "argv", ["cmd"])

    with pytest.raises(PursuitStaleError, match="No arguments"):
        main()


@pytest.mark.unit
def test_main_exits_zero_when_dir_has_no_stale_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pytest.TempPathFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """main() returns normally (implicit exit 0) when given an empty directory."""
    # tmp_path is an empty directory — no .md files, so no stale warnings.
    monkeypatch.setattr(sys, "argv", ["cmd", str(tmp_path)])

    # Should not raise SystemExit; returns None implicitly.
    main()

    captured = capsys.readouterr()
    assert "No stale prose contradictions found." in captured.out
