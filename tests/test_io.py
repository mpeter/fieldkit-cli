"""Regression tests for lib/io._split_frontmatter guard, load_pursuit, and public frontmatter helpers."""

import pytest

from fieldkit.pursuit.io import (
    extract_frontmatter_text,
    load_pursuit,
    split_frontmatter_raw,
)

pytestmark = pytest.mark.unit

# Minimal valid frontmatter used across happy-path tests
_VALID_CONTENT = "---\nstage: discover\n---\n\nBody text.\n"


# ---------------------------------------------------------------------------
# Malformed frontmatter — opening --- not at line 0
# ---------------------------------------------------------------------------


def test_load_pursuit_raises_when_heading_precedes_frontmatter(tmp_path):
    """load_pursuit raises ValueError when file starts with a prose heading."""
    bad_file = tmp_path / "opp.md"
    bad_file.write_text("# Heading\n---\nstage: discover\n---\n\nBody.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="line 0"):
        load_pursuit(bad_file)


def test_load_pursuit_raises_when_blank_line_precedes_frontmatter(tmp_path):
    """load_pursuit raises ValueError when a blank line precedes the opening ---."""
    bad_file = tmp_path / "opp.md"
    bad_file.write_text("\n---\nstage: discover\n---\n\nBody.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="line 0"):
        load_pursuit(bad_file)


# ---------------------------------------------------------------------------
# Happy path — valid file still loads successfully
# ---------------------------------------------------------------------------


def test_load_pursuit_valid_file_succeeds(tmp_path):
    """load_pursuit loads a well-formed pursuit file without error."""
    good_file = tmp_path / "opp.md"
    good_file.write_text(_VALID_CONTENT, encoding="utf-8")

    fm, body, _ = load_pursuit(good_file)

    assert fm.stage == "discover"
    assert "Body text." in body


# ---------------------------------------------------------------------------
# extract_frontmatter_text — anchored match
# ---------------------------------------------------------------------------

# The regex captures text between the \n after opening --- and the \n before closing ---
# so the captured group does NOT include a trailing newline.
_FM_TEXT = "stage: discover\ntitle: Test Deal"
_FM_DOC = f"---\n{_FM_TEXT}\n---\n\nBody.\n"


# ── TestExtractFrontmatterText (flattened) ──────────────────────────────────


def test_extract_frontmatter_text_returns_yaml_text_for_valid_doc():
    result = extract_frontmatter_text(_FM_DOC)
    assert result == _FM_TEXT


def test_extract_frontmatter_text_returns_none_when_no_frontmatter():
    assert extract_frontmatter_text("# Just a heading\n\nBody.\n") is None


def test_extract_frontmatter_text_returns_none_when_preamble_precedes_dashes():
    """Regex uses ^ anchor — preamble before --- means no match."""
    assert extract_frontmatter_text("preamble\n---\nstage: x\n---\n") is None


def test_extract_frontmatter_text_returns_none_for_empty_string():
    assert extract_frontmatter_text("") is None


def test_extract_frontmatter_text_multiline_yaml_captured():
    text = "---\nstage: discover\ntitle: My Deal\nclosed: 2025-01-01\n---\n\nBody\n"
    result = extract_frontmatter_text(text)
    assert result is not None
    assert "stage: discover" in result
    assert "title: My Deal" in result


# ---------------------------------------------------------------------------
# split_frontmatter_raw — anchored, returns (fm_text, tail)
# ---------------------------------------------------------------------------


# ── TestSplitFrontmatterRaw (flattened) ─────────────────────────────────────


def test_split_frontmatter_raw_returns_tuple_for_valid_doc():
    result = split_frontmatter_raw(_FM_DOC)
    assert result is not None
    fm_text, tail = result
    assert fm_text == _FM_TEXT
    # tail starts immediately after the closing "---" (no leading \n stripped)
    assert tail == "\n\nBody.\n"


def test_split_frontmatter_raw_returns_none_when_no_frontmatter():
    assert split_frontmatter_raw("# No dashes\n") is None


def test_split_frontmatter_raw_returns_none_when_preamble_precedes():
    assert split_frontmatter_raw("preamble\n---\nk: v\n---\n") is None


def test_split_frontmatter_raw_tail_empty_when_no_body():
    text = "---\nstage: x\n---"
    result = split_frontmatter_raw(text)
    assert result is not None
    fm_text, tail = result
    assert fm_text == "stage: x"
    assert tail == ""


def test_split_frontmatter_raw_tail_reconstruction_roundtrip():
    """Reassembling fm + tail reproduces the original text."""
    text = "---\nstage: discover\n---\n\nBody content.\n"
    result = split_frontmatter_raw(text)
    assert result is not None
    fm_text, tail = result
    reconstructed = f"---\n{fm_text}\n---{tail}"
    assert reconstructed == text
