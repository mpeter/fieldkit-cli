"""Additional branch coverage for fieldkit.commands.sf.sync._extract_opp_id.

Tests the load_pursuit fallback path, raw YAML extraction with hyphenated key,
corrupt value detection, OSError handling, and all placeholder variants.
Also covers _detect_comment_artifact helper and _load_known_accounts fallback paths.
"""

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.commands.sf.sync import (
    PLACEHOLDER_VALUES,
    _detect_comment_artifact,
    _extract_opp_id,
    _load_known_accounts,
    _validate_opp_id,
)
from fieldkit.sf.opportunities import OPPORTUNITY_ID_RE

pytestmark = pytest.mark.unit

# Fixture IDs
OPP_15 = "006ABC123456789"
OPP_18 = "006GPAY00000000AAA"


# ---------------------------------------------------------------------------
# load_pursuit fallback — ValueError triggers raw YAML extraction
# ---------------------------------------------------------------------------


# ── TestExtractOppIdFallback (flattened) ────────────────────────────────────


def test_extract_opp_id_load_pursuit_value_error_falls_back_to_raw_yaml(tmp_path: Path) -> None:
    """When load_pursuit raises ValueError (missing stage), extract from raw YAML."""
    f = tmp_path / "p.md"
    # No 'stage' field — load_pursuit raises ValueError; raw YAML should still work
    f.write_text(f"---\nsf_opportunity_id: {OPP_18}\n---\n\n# Body\n", encoding="utf-8")

    with patch("fieldkit.commands.sf.sync.load_pursuit", side_effect=ValueError("missing stage")):
        result = _extract_opp_id(f)

    assert result == OPP_18


def test_extract_opp_id_load_pursuit_os_error_falls_back(tmp_path: Path) -> None:
    """OSError from load_pursuit triggers raw YAML fallback."""
    f = tmp_path / "p.md"
    f.write_text(f"---\nsf_opportunity_id: {OPP_18}\n---\n\n# Body\n", encoding="utf-8")

    with patch("fieldkit.commands.sf.sync.load_pursuit", side_effect=OSError("permission denied")):
        result = _extract_opp_id(f)

    assert result == OPP_18


def test_extract_opp_id_fallback_with_hyphenated_key(tmp_path: Path) -> None:
    """Raw YAML fallback reads sf-opportunity-id (hyphenated) when load_pursuit fails."""
    f = tmp_path / "p.md"
    f.write_text(f"---\nsf-opportunity-id: {OPP_18}\n---\n\n# Body\n", encoding="utf-8")

    with patch("fieldkit.commands.sf.sync.load_pursuit", side_effect=ValueError("missing stage")):
        result = _extract_opp_id(f)

    assert result == OPP_18


def test_extract_opp_id_fallback_placeholder_returns_none(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """load_pursuit fails + raw YAML has placeholder → None + warning."""
    f = tmp_path / "p.md"
    f.write_text("---\nsf_opportunity_id: TBD\n---\n", encoding="utf-8")

    with (
        patch("fieldkit.commands.sf.sync.load_pursuit", side_effect=ValueError("missing stage")),
        caplog.at_level(logging.WARNING),
    ):
        result = _extract_opp_id(f)

    assert result is None
    assert any("placeholder" in r.message.lower() for r in caplog.records)


def test_extract_opp_id_fallback_corrupt_value_returns_none(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """load_pursuit fails + raw YAML value is invalid format → None + warning."""
    f = tmp_path / "p.md"
    f.write_text("---\nsf_opportunity_id: not-an-opp-id\n---\n", encoding="utf-8")

    with (
        patch("fieldkit.commands.sf.sync.load_pursuit", side_effect=ValueError("missing stage")),
        caplog.at_level(logging.WARNING),
    ):
        result = _extract_opp_id(f)

    assert result is None
    assert any("corrupt" in r.message.lower() or "invalid" in r.message.lower() for r in caplog.records)


def test_extract_opp_id_fallback_no_opp_id_key_returns_none(tmp_path: Path) -> None:
    """load_pursuit fails + raw YAML has no opp ID key → None."""
    f = tmp_path / "p.md"
    f.write_text("---\nstage: discover\n---\n", encoding="utf-8")

    with patch("fieldkit.commands.sf.sync.load_pursuit", side_effect=ValueError("missing stage")):
        result = _extract_opp_id(f)

    assert result is None


def test_extract_opp_id_fallback_comment_artifact_returns_none_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """load_pursuit fails + raw YAML has comment artifact → None + warning."""
    f = tmp_path / "p.md"
    f.write_text("---\nsf_opportunity_id: # was 006ABC123456789ABC\n---\n", encoding="utf-8")

    with (
        patch("fieldkit.commands.sf.sync.load_pursuit", side_effect=ValueError("missing stage")),
        caplog.at_level(logging.WARNING),
    ):
        result = _extract_opp_id(f)

    assert result is None
    assert any("comment" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# OSError reading file — final except branch
# ---------------------------------------------------------------------------


# ── TestExtractOppIdFileError (flattened) ───────────────────────────────────


def test_extract_opp_id_both_read_attempts_fail_returns_none(tmp_path: Path) -> None:
    """When both load_pursuit and file.read_text fail, return None."""
    f = tmp_path / "p.md"
    f.write_text(f"---\nsf_opportunity_id: {OPP_18}\n---\n", encoding="utf-8")

    with (
        patch("fieldkit.commands.sf.sync.load_pursuit", side_effect=ValueError("missing stage")),
        patch.object(Path, "read_text", side_effect=OSError("permission denied")),
    ):
        result = _extract_opp_id(f)

    assert result is None


# ---------------------------------------------------------------------------
# load_pursuit succeeds — normal path validation
# ---------------------------------------------------------------------------


# ── TestExtractOppIdNormalPath (flattened) ──────────────────────────────────


def test_extract_opp_id_valid_15_char_id_returned(tmp_path: Path) -> None:
    f = tmp_path / "p.md"
    f.write_text(f"---\nsf_opportunity_id: {OPP_15}\nstage: discover\n---\n", encoding="utf-8")
    result = _extract_opp_id(f)
    assert result == OPP_15


def test_extract_opp_id_valid_18_char_id_returned(tmp_path: Path) -> None:
    f = tmp_path / "p.md"
    f.write_text(f"---\nsf_opportunity_id: {OPP_18}\nstage: discover\n---\n", encoding="utf-8")
    result = _extract_opp_id(f)
    assert result == OPP_18


def test_extract_opp_id_none_value_returns_none(tmp_path: Path) -> None:
    """sf_opportunity_id: null → None (val is falsy)."""
    f = tmp_path / "p.md"
    f.write_text("---\nsf_opportunity_id:\nstage: discover\n---\n", encoding="utf-8")
    result = _extract_opp_id(f)
    assert result is None


@pytest.mark.parametrize("placeholder", sorted(PLACEHOLDER_VALUES - {""}))
def test_extract_opp_id_placeholder_values_return_none(tmp_path: Path, placeholder: str) -> None:
    f = tmp_path / "p.md"
    f.write_text(f"---\nsf_opportunity_id: {placeholder}\nstage: discover\n---\n", encoding="utf-8")
    result = _extract_opp_id(f)
    assert result is None


def test_extract_opp_id_invalid_format_warns_and_returns_none(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    f = tmp_path / "p.md"
    f.write_text("---\nsf_opportunity_id: toolong_19_chars000\nstage: discover\n---\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        result = _extract_opp_id(f)
    assert result is None
    assert any("invalid" in r.message.lower() or "looks invalid" in r.message.lower() for r in caplog.records)


def test_extract_opp_id_strips_double_quotes(tmp_path: Path) -> None:
    f = tmp_path / "p.md"
    f.write_text(f'---\nsf_opportunity_id: "{OPP_18}"\nstage: discover\n---\n', encoding="utf-8")
    result = _extract_opp_id(f)
    assert result == OPP_18


def test_extract_opp_id_strips_single_quotes(tmp_path: Path) -> None:
    f = tmp_path / "p.md"
    f.write_text(f"---\nsf_opportunity_id: '{OPP_18}'\nstage: discover\n---\n", encoding="utf-8")
    result = _extract_opp_id(f)
    assert result == OPP_18


def test_extract_opp_id_no_frontmatter_returns_none(tmp_path: Path) -> None:
    f = tmp_path / "p.md"
    f.write_text("# No frontmatter at all\n", encoding="utf-8")
    result = _extract_opp_id(f)
    assert result is None


# ---------------------------------------------------------------------------
# OPP_ID_RE / PLACEHOLDER_VALUES — constants sanity
# ---------------------------------------------------------------------------


# ── TestOppIdConstants (flattened) ──────────────────────────────────────────


@pytest.mark.parametrize("valid", [OPP_15, OPP_18, "AbCdEfGhIjKlMnO", "006XYZ123456789ABC"])
def test_extract_opp_id_re_matches_valid(valid: str) -> None:
    assert OPPORTUNITY_ID_RE.match(valid)


# ---------------------------------------------------------------------------
# _detect_comment_artifact — extracted helper
# ---------------------------------------------------------------------------


# ── TestDetectCommentArtifact (flattened) ───────────────────────────────────


def test_detect_comment_artifact_comment_artifact_emits_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Comment artifact pattern triggers a warning log."""
    filepath = tmp_path / "p.md"
    fm_text = "sf_opportunity_id: # was 006ABC123456789ABC\nstage: discover\n"

    with caplog.at_level(logging.WARNING):
        _detect_comment_artifact(filepath, fm_text)

    assert any("comment" in r.message.lower() for r in caplog.records)


def test_detect_comment_artifact_hyphenated_key_comment_artifact_emits_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Hyphenated key comment artifact also triggers warning."""
    filepath = tmp_path / "p.md"
    fm_text = "sf-opportunity-id: # was 006ABC123456789ABC\nstage: discover\n"

    with caplog.at_level(logging.WARNING):
        _detect_comment_artifact(filepath, fm_text)

    assert any("comment" in r.message.lower() for r in caplog.records)


def test_detect_comment_artifact_no_comment_artifact_emits_no_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Normal frontmatter with a valid value emits no warning."""
    filepath = tmp_path / "p.md"
    fm_text = "sf_opportunity_id: 006ABC123456789ABC\nstage: discover\n"

    with caplog.at_level(logging.WARNING):
        _detect_comment_artifact(filepath, fm_text)

    assert not any("comment" in r.message.lower() for r in caplog.records)


def test_detect_comment_artifact_empty_frontmatter_emits_no_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Empty frontmatter text emits no warning."""
    filepath = tmp_path / "p.md"

    with caplog.at_level(logging.WARNING):
        _detect_comment_artifact(filepath, "")

    assert not caplog.records


# ---------------------------------------------------------------------------
# _validate_opp_id — simple helper
# ---------------------------------------------------------------------------


# ── TestValidateOppId (flattened) ───────────────────────────────────────────


def test_validate_opp_id_valid_15_char_returns_true() -> None:
    assert _validate_opp_id(OPP_15) is True


def test_validate_opp_id_valid_18_char_returns_true() -> None:
    assert _validate_opp_id(OPP_18) is True


def test_validate_opp_id_invalid_returns_false() -> None:
    assert _validate_opp_id("short") is False


def test_validate_opp_id_empty_returns_false() -> None:
    assert _validate_opp_id("") is False


# ---------------------------------------------------------------------------
# _load_known_accounts — fallback paths
# ---------------------------------------------------------------------------


# ── TestLoadKnownAccounts (flattened) ───────────────────────────────────────


def test_known_accounts_returns_tuple_of_strings(tmp_path: Path) -> None:
    """_load_known_accounts returns a non-empty tuple of strings."""
    # Use a temp dir without accounts/ so it falls through to config/hardcoded
    with patch("fieldkit.commands.sf.sync._project_root", return_value=tmp_path):
        result = _load_known_accounts()

    assert isinstance(result, tuple)
    assert len(result) > 0
    assert all(isinstance(a, str) for a in result)


def test_known_accounts_scans_accounts_dir_when_present(tmp_path: Path) -> None:
    """When accounts/ dir exists with subdirs, returns those names."""
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    (accounts_dir / "acme-corp").mkdir()
    (accounts_dir / "globalpay").mkdir()
    (accounts_dir / ".hidden").mkdir()  # should be excluded

    with patch("fieldkit.commands.sf.sync._project_root", return_value=tmp_path):
        result = _load_known_accounts()

    assert "acme-corp" in result
    assert "globalpay" in result
    assert ".hidden" not in result
