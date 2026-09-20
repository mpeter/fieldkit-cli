"""Tests for implementation change: morning-brief dry-run suppresses optional file warnings.

Covers:
- No WARNING log for missing draft-queue-alerts.md when dry_run=True
  (the file is optional — not_found_msg is provided).
- WARNING is still emitted for missing backstory-alerts.md even in dry-run
  mode (no not_found_msg → required source, always warns).
"""

import logging
from datetime import date
from pathlib import Path

import pytest

from fieldkit.watch._morning_brief_types import SourceNotReady
from fieldkit.watch.morning_brief import _collect_alert_source

pytestmark = pytest.mark.unit

_TARGET_DATE = date(2026, 5, 26)


# ── TestCollectAlertSourceDryRun (flattened) ────────────────────────────────


def test_collect_alert_source_dry_run_no_warning_for_missing_optional_source_in_dry_run(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """implementation change: missing optional source (not_found_msg set) must not warn in dry-run."""
    missing_file = tmp_path / "draft-queue-alerts.md"
    # File does not exist

    with caplog.at_level(logging.DEBUG, logger="fieldkit.watch"):
        result = _collect_alert_source(
            missing_file,
            _TARGET_DATE,
            "Draft Queue",
            not_found_msg="_No draft queue data yet._",
            dry_run=True,
        )

    # Returns SourceNotReady (not a plain str) — optional source not yet run
    assert isinstance(result, SourceNotReady)
    assert result.message == "_No draft queue data yet._"

    # No WARNING-level records for this source
    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING and "Draft Queue" in r.message]
    assert warning_records == [], (
        f"implementation change regression: unexpected WARNING for optional source in dry-run: {warning_records}"
    )


def test_collect_alert_source_dry_run_debug_log_emitted_for_missing_optional_source_in_dry_run(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """implementation change: a DEBUG message is still emitted so troubleshooting is possible."""
    missing_file = tmp_path / "draft-queue-alerts.md"

    with caplog.at_level(logging.DEBUG, logger="fieldkit.watch"):
        _collect_alert_source(
            missing_file,
            _TARGET_DATE,
            "Draft Queue",
            not_found_msg="_No draft queue data yet._",
            dry_run=True,
        )

    debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG and "Draft Queue" in r.message]
    assert debug_records, "Expected at least one DEBUG record for missing optional source in dry-run"


def test_collect_alert_source_dry_run_warning_emitted_for_missing_optional_source_in_non_dry_run(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Non-dry-run mode still emits WARNING for missing optional sources."""
    missing_file = tmp_path / "draft-queue-alerts.md"

    with caplog.at_level(logging.WARNING, logger="fieldkit.watch"):
        result = _collect_alert_source(
            missing_file,
            _TARGET_DATE,
            "Draft Queue",
            not_found_msg="_No draft queue data yet._",
            dry_run=False,
        )

    assert isinstance(result, SourceNotReady)
    assert result.message == "_No draft queue data yet._"

    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING and "Draft Queue" in r.message]
    assert warning_records, "Expected WARNING for missing optional source in non-dry-run mode"


def test_collect_alert_source_dry_run_warning_still_emitted_for_required_source_in_dry_run(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """implementation change: required sources (no not_found_msg) still warn even in dry-run mode."""
    missing_file = tmp_path / "backstory-alerts.md"
    # File does not exist; no not_found_msg → required source

    with caplog.at_level(logging.WARNING, logger="fieldkit.watch"):
        result = _collect_alert_source(
            missing_file,
            _TARGET_DATE,
            "Backstory alerts",
            # not_found_msg intentionally omitted → required source
            dry_run=True,
        )

    # Returns an error string (not a fallback message)
    assert isinstance(result, str)
    assert "unavailable" in result

    # WARNING must still be emitted for required sources even in dry-run
    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING and "Backstory alerts" in r.message]
    assert warning_records, "implementation change: required source (no not_found_msg) must still warn in dry-run mode"


def test_collect_alert_source_dry_run_no_warning_for_missing_slack_optional_source_in_dry_run(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Other optional sources (slack, contract-expiry) also suppress warnings in dry-run."""
    missing_file = tmp_path / "slack-thread-alerts.md"

    with caplog.at_level(logging.DEBUG, logger="fieldkit.watch"):
        result = _collect_alert_source(
            missing_file,
            _TARGET_DATE,
            "Slack",
            not_found_msg="_No Slack thread data yet._",
            dry_run=True,
        )

    assert isinstance(result, SourceNotReady)
    assert result.message == "_No Slack thread data yet._"

    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING and "Slack" in r.message]
    assert warning_records == [], f"Unexpected WARNING for optional Slack source in dry-run: {warning_records}"
