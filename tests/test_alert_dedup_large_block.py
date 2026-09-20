"""Tests for _alert_key_exists() with large alert blocks (spec 043).

Verifies that the scan-to-next-date-header logic correctly finds slugs
in alert blocks larger than the old 50-line fixed lookahead window.
"""

from pathlib import Path

import pytest


@pytest.mark.unit
def test_alert_key_exists_detects_slug_beyond_50_lines(tmp_path: Path) -> None:
    """_alert_key_exists() must find a slug in an alert block larger than 50 lines."""
    from fieldkit.watch.logging import _alert_key_exists

    # Build a log file where the slug appears after 60 lines within the same block.
    # The old 50-line lookahead would miss this; the new scan-to-next-header finds it.
    lines = ["## 2026-06-26\n"]
    lines += [f"- line {i}\n" for i in range(60)]  # 60 body lines (beyond old 50-line window)
    lines += ["- slug: test-slug-xyz\n"]  # slug appears at line 62 within the block
    lines += ["\n## 2026-06-25\n"]  # Previous date block (next header — scan stops here)

    log_file = tmp_path / "test.md"
    log_file.write_text("".join(lines), encoding="utf-8")

    assert _alert_key_exists(log_file, "2026-06-26", "test-slug-xyz"), (
        "Alert key should be found even in blocks > 50 lines"
    )


@pytest.mark.unit
def test_alert_key_exists_stops_at_next_date_header(tmp_path: Path) -> None:
    """_alert_key_exists() must not find a slug in a different date block."""
    from fieldkit.watch.logging import _alert_key_exists

    # Slug appears in the NEXT date block, not the target block.
    lines = ["## 2026-06-26\n"]
    lines += [f"- line {i}\n" for i in range(10)]
    lines += ["## 2026-06-25\n"]  # Next date block
    lines += ["- slug: test-slug-xyz\n"]  # Slug is in the WRONG block

    log_file = tmp_path / "test.md"
    log_file.write_text("".join(lines), encoding="utf-8")

    # Should return False — slug is in the 2026-06-25 block, not 2026-06-26
    assert not _alert_key_exists(log_file, "2026-06-26", "test-slug-xyz"), (
        "Alert key in a different date block must not be detected as a duplicate"
    )


@pytest.mark.unit
def test_alert_key_exists_returns_false_for_missing_file(tmp_path: Path) -> None:
    """_alert_key_exists() must return False when the alert file does not exist."""
    from fieldkit.watch.logging import _alert_key_exists

    missing = tmp_path / "nonexistent.md"
    assert not _alert_key_exists(missing, "2026-06-26", "test-slug")


@pytest.mark.unit
def test_alert_key_exists_returns_false_when_slug_absent(tmp_path: Path) -> None:
    """_alert_key_exists() must return False when the slug is not in the block."""
    from fieldkit.watch.logging import _alert_key_exists

    lines = ["## 2026-06-26\n", "- some-other-slug\n"]
    log_file = tmp_path / "test.md"
    log_file.write_text("".join(lines), encoding="utf-8")

    assert not _alert_key_exists(log_file, "2026-06-26", "missing-slug")


@pytest.mark.unit
def test_is_date_header_recognises_date_format() -> None:
    """_is_date_header() must recognise ## YYYY-MM-DD lines."""
    from fieldkit.watch.logging import _is_date_header

    assert _is_date_header("## 2026-06-26\n")
    assert _is_date_header("## 2026-06-26")
    assert _is_date_header("## 2026-01-01")


@pytest.mark.unit
def test_is_date_header_rejects_non_date_lines() -> None:
    """_is_date_header() must reject non-date-header lines."""
    from fieldkit.watch.logging import _is_date_header

    assert not _is_date_header("- some content\n")
    assert not _is_date_header("## ")  # too short
    assert not _is_date_header("### 2026-06-26")  # H3, not H2
    assert not _is_date_header("2026-06-26")  # no ## prefix
