"""Tests for generate_report metric reconciliation section."""

import json
from pathlib import Path

import pytest
import yaml

from fieldkit.contact.report import _section_metric_reconciliation, generate_report

pytestmark = pytest.mark.unit


def _make_enriched(n: int) -> list[dict]:
    return [{"full_name": f"Contact {i}", "account": "acme"} for i in range(n)]


def test_reconciliation_counts_match(tmp_path: Path) -> None:
    """When cache and checkpoint counts match, no differ explanation is emitted."""
    enriched = _make_enriched(5)
    checkpoint = {"total_enriched": 5}
    (tmp_path / "checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")

    lines = _section_metric_reconciliation(enriched, tmp_path)
    text = "\n".join(lines)

    assert "5 contacts" in text
    assert "mismatch" not in text.lower()


def test_reconciliation_counts_differ(tmp_path: Path) -> None:
    """When cache count < checkpoint count, mismatch explanation is present."""
    enriched = _make_enriched(5)
    checkpoint = {"total_enriched": 10}
    (tmp_path / "checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")

    lines = _section_metric_reconciliation(enriched, tmp_path)
    text = "\n".join(lines)

    assert "5 contacts" in text
    assert "10 contacts" in text
    assert "mismatch" in text.lower()


def test_reconciliation_no_checkpoint(tmp_path: Path) -> None:
    """When checkpoint.json does not exist, a fallback message is shown."""
    enriched = _make_enriched(3)

    lines = _section_metric_reconciliation(enriched, tmp_path)
    text = "\n".join(lines)

    assert "3 contacts" in text
    assert "no checkpoint found" in text.lower()


def test_report_starts_with_summary_caste_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Report content starts with the caste: summary provenance marker (implementation change).

    Spec ref: doc-provenance/spec.md — Scenario: Enrichment coverage report is
    stamped as a summary.
    """
    # L01: patch the import-site binding used inside generate_report().
    monkeypatch.setattr("fieldkit.contact.report.enrich_dir", lambda: tmp_path)

    report = generate_report(_make_enriched(2), _make_enriched(3))

    # CR-014: assert directly on the return value first.
    assert report.startswith("---\n")
    fm = next(iter(yaml.safe_load_all(report)))
    assert fm["caste"] == "summary"
    assert fm["derived_from"] == ["contacts-raw.json", "contacts-enriched.json"]
    assert fm["generated_by"] == "fieldkit contact report"
    assert "not a system of record" in report
