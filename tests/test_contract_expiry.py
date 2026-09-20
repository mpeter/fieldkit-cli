"""Unit tests for fieldkit/commands/watch/contract_expiry.py.

Focus: _classify_tier with custom thresholds (implementation note), _build_expiry_state_key
threshold encoding, and FieldkitError validation in _run_contract_expiry_inner.

Default-threshold _classify_tier coverage lives in test_watch_contract_expiry.py
(TestClassifyTier). This file focuses exclusively on the custom-threshold and
validation behaviour introduced by implementation note.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.errors import FieldkitError
from fieldkit.watch.contract_expiry import (
    _build_expiry_state_key,
    _classify_tier,
    _parse_frontmatter,
    _run_contract_expiry_inner,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _classify_tier — custom thresholds (implementation note)
# ---------------------------------------------------------------------------


def test_classify_tier_custom_warning_reclassifies() -> None:
    """Project 20 days out is orange at default warning=30.
    With warning=19 (below 20), 20 days falls into the yellow tier."""
    assert _classify_tier(20, critical=14, warning=30, notice=60) == "orange"
    # warning=19: 20 > 19, so not orange; 20 ≤ 60 (notice), so yellow
    assert _classify_tier(20, critical=14, warning=19, notice=60) == "yellow"


def test_classify_tier_custom_critical_reclassifies() -> None:
    """Project 5 days out is red with custom critical=7."""
    assert _classify_tier(5, critical=7, warning=30, notice=60) == "red"


def test_classify_tier_custom_notice_expands_window() -> None:
    """Project 75 days out is outside default window but yellow with notice=90."""
    assert _classify_tier(75, critical=14, warning=30, notice=60) is None
    assert _classify_tier(75, critical=14, warning=30, notice=90) == "yellow"


def test_classify_tier_boundary_at_custom_thresholds() -> None:
    """Boundary values are inclusive (<= comparison)."""
    # critical=7: exactly 7 -> red, 8 -> orange
    assert _classify_tier(7, critical=7, warning=30, notice=60) == "red"
    assert _classify_tier(8, critical=7, warning=30, notice=60) == "orange"
    # warning=25: exactly 25 -> orange, 26 -> yellow
    assert _classify_tier(25, critical=7, warning=25, notice=60) == "orange"
    assert _classify_tier(26, critical=7, warning=25, notice=60) == "yellow"


# ---------------------------------------------------------------------------
# _build_expiry_state_key — threshold suffix encoding (implementation note)
# ---------------------------------------------------------------------------


def test_build_expiry_state_key_includes_threshold_suffix(tmp_path: Path) -> None:
    # Use tmp_path to avoid hardcoded home-dir paths (pii-guard)
    path = tmp_path / "accounts" / "acme" / "projects" / "project-alpha.md"
    key = _build_expiry_state_key("acme", path, "orange", 20, critical=14, warning=30, notice=60)
    assert "14-30-60" in key
    assert key == "pursuit-sf-close-date/v1/acme/project-alpha/orange/14-30-60"


def test_build_expiry_state_key_custom_thresholds_differ(tmp_path: Path) -> None:
    path = tmp_path / "accounts" / "acme" / "projects" / "proj.md"
    key_default = _build_expiry_state_key("acme", path, "orange", 20)
    key_custom = _build_expiry_state_key("acme", path, "orange", 20, critical=7, warning=25, notice=90)
    assert key_default != key_custom
    assert "7-25-90" in key_custom


def test_build_expiry_state_key_expired_includes_band_and_suffix(tmp_path: Path) -> None:
    import datetime as _dt

    path = tmp_path / "accounts" / "acme" / "projects" / "proj.md"
    today = _dt.datetime.now(tz=_dt.UTC).date()
    key = _build_expiry_state_key("acme", path, "expired", -20, today=today, critical=14, warning=30, notice=60)
    # days_past=20 -> band "0-30d"
    # implementation note: key includes the injected UTC date between band and threshold suffix
    assert "0-30d" in key
    assert "14-30-60" in key
    assert today.isoformat() in key, f"implementation note: injected date {today!r} must appear in expired key: {key!r}"
    assert key == f"pursuit-sf-close-date/v1/acme/proj/expired/0-30d/{today.isoformat()}/14-30-60"


def test_build_expiry_state_key_expired_90d_plus_band(tmp_path: Path) -> None:
    path = tmp_path / "accounts" / "acme" / "projects" / "proj.md"
    key = _build_expiry_state_key("acme", path, "expired", -95)
    assert "90d+" in key


def test_parse_frontmatter_fallback_handles_empty_frontmatter() -> None:
    frontmatter, body = _parse_frontmatter("---\n---\n\nPursuit body")

    assert frontmatter == {}
    assert body == "\n\nPursuit body"


def test_parse_frontmatter_fallback_rejects_unclosed_frontmatter() -> None:
    frontmatter, body = _parse_frontmatter("---\nstage: qualify")

    assert frontmatter is None
    assert body == "---\nstage: qualify"


# ---------------------------------------------------------------------------
# _run_contract_expiry_inner — threshold validation (implementation note)
# ---------------------------------------------------------------------------


def _inner_with_no_accounts(tmp_path: Path, **kwargs: int) -> int:
    """Call _run_contract_expiry_inner with tmp_path as home (no projects dir).

    Returns the exit code or raises FieldkitError for invalid thresholds.
    """
    with patch(
        "fieldkit.watch.contract_expiry.get_fieldkit_home",
        return_value=tmp_path,
    ):
        return _run_contract_expiry_inner(account_filter=None, dry_run=True, **kwargs)


def test_validation_critical_gte_warning_raises(tmp_path: Path) -> None:
    with pytest.raises(FieldkitError, match=r"critical=30.*warning=30") as exc_info:
        _inner_with_no_accounts(tmp_path, critical=30, warning=30, notice=60)
    assert "warning" in str(exc_info.value)


def test_validation_critical_gt_warning_raises(tmp_path: Path) -> None:
    """critical > warning (not just equal) also raises."""
    with pytest.raises(FieldkitError, match=r"critical=50.*warning=30"):
        _inner_with_no_accounts(tmp_path, critical=50, warning=30, notice=60)


def test_validation_warning_gte_notice_raises(tmp_path: Path) -> None:
    with pytest.raises(FieldkitError, match=r"warning=60.*notice=60") as exc_info:
        _inner_with_no_accounts(tmp_path, critical=14, warning=60, notice=60)
    assert "notice" in str(exc_info.value)


def test_validation_critical_zero_raises(tmp_path: Path) -> None:
    with pytest.raises(FieldkitError, match=r"critical=0") as exc_info:
        _inner_with_no_accounts(tmp_path, critical=0, warning=30, notice=60)
    assert "positive" in str(exc_info.value)


def test_validation_critical_negative_raises(tmp_path: Path) -> None:
    with pytest.raises(FieldkitError, match=r"critical=-5") as exc_info:
        _inner_with_no_accounts(tmp_path, critical=-5, warning=30, notice=60)
    assert "positive" in str(exc_info.value)


def test_validation_valid_custom_thresholds_does_not_raise(tmp_path: Path) -> None:
    """Valid thresholds should not raise — even with no accounts directory."""
    # tmp_path has no "accounts" subdir -> returns 1 (not FieldkitError)
    with (
        patch(
            "fieldkit.watch.contract_expiry.get_fieldkit_home",
            return_value=tmp_path,
        ),
        patch("fieldkit.watch.contract_expiry.write_run_status"),
    ):
        result = _run_contract_expiry_inner(account_filter=None, dry_run=True, critical=7, warning=25, notice=90)
    assert result == 1  # accounts dir not found -> exit 1, not FieldkitError
