"""Unit tests for fieldkit.contact.report.build_report.

Covers:
  - account=None: no filtering, all contacts reach generate_report.
  - account="<slug>": both enriched and raw are independently filtered by
    the same predicate before reaching generate_report.
  - empty enriched after filtering: returns None without calling
    generate_report or writing report.md (gated on enriched, not raw).
  - happy path: report.md is written with exactly the string generate_report
    returns, and that same string is the function's return value.
"""

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from fieldkit.contact.report import build_report

pytestmark = pytest.mark.unit


def _make_contact(full_name: str, account: str, email: str | None = None) -> dict[str, Any]:
    """Build a minimal contact dict for test fixtures."""
    contact: dict[str, Any] = {"full_name": full_name, "account": account}
    if email is not None:
        contact["email"] = email
    return contact


def test_build_report_no_filter_includes_all_accounts(tmp_path: Path) -> None:
    """account=None → no filtering; both accounts' contacts reach generate_report."""
    enriched = [
        _make_contact("Alice Smith", "acme-corp", "alice@acme-corp.com"),
        _make_contact("Bob Jones", "example-co", "bob@example.com"),
    ]
    raw = [
        _make_contact("Alice Smith", "acme-corp", "alice@acme-corp.com"),
        _make_contact("Bob Jones", "example-co", "bob@example.com"),
    ]

    with (
        patch("fieldkit.contact.report.load_enriched_contacts", return_value=enriched),
        patch("fieldkit.contact.report.load_raw_contacts", return_value=raw),
        patch("fieldkit.contact.report.enrich_dir", return_value=tmp_path),
    ):
        report = build_report()

    assert report is not None
    assert "acme-corp" in report
    assert "example-co" in report


def test_build_report_filters_enriched_and_raw_independently(tmp_path: Path) -> None:
    """account="<slug>" filters BOTH lists by the same predicate, independently.

    `eve_acme` exists only in raw (no matching enriched record), proving raw
    is filtered from its own account field rather than derived from the
    filtered enriched list.
    """
    alice_acme = _make_contact("Alice Smith", "acme-corp", "alice@acme-corp.com")
    carol_example = _make_contact("Carol White", "example-co", "carol@example.com")
    dave_example = _make_contact("Dave Brown", "example-co", "dave@example.com")
    eve_acme = _make_contact("Eve Green", "acme-corp", "eve@acme-corp.com")

    enriched = [alice_acme, carol_example]
    raw = [alice_acme, dave_example, eve_acme]

    fake_report = "REPORT"
    with (
        patch("fieldkit.contact.report.load_enriched_contacts", return_value=enriched),
        patch("fieldkit.contact.report.load_raw_contacts", return_value=raw),
        patch("fieldkit.contact.report.enrich_dir", return_value=tmp_path),
        patch("fieldkit.contact.report.generate_report", return_value=fake_report) as mock_generate,
    ):
        result = build_report(account="acme-corp")

    assert result == fake_report
    mock_generate.assert_called_once_with([alice_acme], [alice_acme, eve_acme])


def test_build_report_empty_enriched_after_filter_returns_none(tmp_path: Path) -> None:
    """Empty `enriched` after filtering → None, no generate_report call, no write.

    `raw` still has a matching entry after filtering (alice_acme), proving the
    early return is gated on `enriched`, not `raw`.
    """
    bob_example = _make_contact("Bob Jones", "example-co", "bob@example.com")
    alice_acme = _make_contact("Alice Smith", "acme-corp", "alice@acme-corp.com")

    enriched = [bob_example]
    raw = [alice_acme, bob_example]

    with (
        patch("fieldkit.contact.report.load_enriched_contacts", return_value=enriched),
        patch("fieldkit.contact.report.load_raw_contacts", return_value=raw),
        patch("fieldkit.contact.report.enrich_dir", return_value=tmp_path),
        patch("fieldkit.contact.report.generate_report") as mock_generate,
    ):
        result = build_report(account="acme-corp")

    assert result is None
    mock_generate.assert_not_called()
    assert not (tmp_path / "report.md").exists()


def test_build_report_happy_path_writes_and_returns_same_string(tmp_path: Path) -> None:
    """Non-empty enriched → report.md is written with the exact returned string."""
    enriched = [_make_contact("Alice Smith", "acme-corp", "alice@acme-corp.com")]
    raw = [_make_contact("Alice Smith", "acme-corp", "alice@acme-corp.com")]

    with (
        patch("fieldkit.contact.report.load_enriched_contacts", return_value=enriched),
        patch("fieldkit.contact.report.load_raw_contacts", return_value=raw),
        patch("fieldkit.contact.report.enrich_dir", return_value=tmp_path),
    ):
        result = build_report()

    assert result is not None
    report_file = tmp_path / "report.md"
    assert report_file.exists()
    assert report_file.read_text(encoding="utf-8") == result
