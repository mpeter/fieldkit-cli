"""Tests for src/fieldkit/ingest/constants.py and src/fieldkit/watch/constants.py."""

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.ingest.constants import (
    GEMINI_TRANSCRIPT_PIPELINE,
    SOURCE_STATUS_FAILED,
    SOURCE_STATUS_IN_PROGRESS,
    SOURCE_STATUS_PENDING,
    SOURCE_STATUS_PROCESSED,
)
from fieldkit.ingest.pipeline import ConfidenceLevel, TranscriptMeta, stage2_extract
from fieldkit.watch.constants import KNOWN_WATCHERS
from fieldkit.watch.status import was_run_today


@pytest.mark.unit
def test_pipeline_id_constant_value() -> None:
    """GEMINI_TRANSCRIPT_PIPELINE must equal the canonical pipeline identifier."""
    assert GEMINI_TRANSCRIPT_PIPELINE == "transcript-ingest"


@pytest.mark.unit
def test_source_status_constant_values() -> None:
    """All four source status constants must have their canonical values."""
    assert SOURCE_STATUS_PENDING == "pending"
    assert SOURCE_STATUS_IN_PROGRESS == "in_progress"
    assert SOURCE_STATUS_PROCESSED == "processed"
    assert SOURCE_STATUS_FAILED == "failed"


@pytest.mark.unit
def test_known_watchers_contains_all_active() -> None:
    """KNOWN_WATCHERS must contain all watchers listed in _WATCHER_ORDER."""
    # These are the names from commands/watch/cli.py _WATCHER_ORDER + run-all
    expected = {
        "waiting-on-tracker",
        "pursuit-stalls",
        "close-date-countdown",
        "contract-expiry",
        "backstory-health",
        "slack-threads",
        "draft-queue",
        "morning-brief",
        "run-all",
    }
    assert expected.issubset(KNOWN_WATCHERS)


@pytest.mark.unit
def test_was_run_today_warns_unknown_watcher(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """was_run_today() must log a WARNING and return False for unknown watcher names."""
    # Point to a fresh temp dir so no state file exists.
    # Patch get_fieldkit_home at the call site in status.py (L01 pattern from watch/AGENTS.md).
    with (
        patch("fieldkit.watch.status.get_fieldkit_home", return_value=tmp_path),
        caplog.at_level(logging.WARNING, logger="fieldkit.watch.status"),
    ):
        result = was_run_today("totally-unknown-watcher-xyz")

    assert result is False
    assert any("unknown watcher" in r.message for r in caplog.records), (
        f"Expected 'unknown watcher' warning, got: {[r.message for r in caplog.records]}"
    )


@pytest.mark.unit
def test_confidence_level_valid_values_pass_through() -> None:
    """stage2_extract() must preserve valid ConfidenceLevel values from LLM JSON.

    Regression guard for the cast() narrowing at pipeline.py:181 — verifies that
    known-good values are not accidentally normalised to "low".
    """
    valid_json = '{"confidence": "high", "participants": [], "action_items": [], "key_decisions": [], "key_topics": []}'
    with patch("fieldkit.ingest.pipeline.synthesize", return_value=valid_json):
        meta = stage2_extract("Some transcript text that is long enough to pass the guard " * 3)
    assert meta.confidence == "high"


@pytest.mark.unit
def test_confidence_level_invalid_value_normalised_to_low() -> None:
    """stage2_extract() must normalise unknown confidence values to 'low'.

    Regression guard for the cast() narrowing at pipeline.py:181 — verifies that
    an invalid LLM-returned confidence value (e.g. 'very-high') falls back to 'low'.
    """
    invalid_json = (
        '{"confidence": "very-high", "participants": [], "action_items": [], "key_decisions": [], "key_topics": []}'
    )
    with patch("fieldkit.ingest.pipeline.synthesize", return_value=invalid_json):
        meta = stage2_extract("Some transcript text that is long enough to pass the guard " * 3)
    assert meta.confidence == "low"


@pytest.mark.unit
def test_confidence_level_stub_is_default() -> None:
    """TranscriptMeta.confidence defaults to 'stub' — the NO_LLM sentinel value."""
    meta = TranscriptMeta()
    assert meta.confidence == "stub"
    # Verify 'stub' is a valid ConfidenceLevel (type checker would catch this at import time)
    level: ConfidenceLevel = meta.confidence
    assert level == "stub"
