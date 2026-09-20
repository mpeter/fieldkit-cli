"""Unit tests for fieldkit.contact.enrich — discover() and enrich_records().

Covers the two functions directly (not via the Click adapter in
commands/contact/enrich_cmd.py, which has its own test file:
tests/test_contact_enrich_cmd.py). Distinct from tests/test_contact_enrich.py
and tests/test_contact_enrich_pipeline.py, which cover other parts of this
same domain module.

discover_all_contacts, migrate_legacy_memory_files, and run_enrichment_pipeline
are mocked so no real vault/Gmail scan or enrichment pipeline runs.
enrich_dir() is redirected to tmp_path so the real write/backup/read code
paths in discover() execute against an isolated directory rather than the
real fieldkit_home.
"""

import json
import shutil as real_shutil
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from fieldkit.contact.enrich import DiscoverResult, EnrichRecordsResult, discover, enrich_records

pytestmark = pytest.mark.unit

_MOD = "fieldkit.contact.enrich"


def _make_contact(
    *,
    source: str | None = "gmail",
    account: str | None = "acme-corp",
    email: str | None = "person@example.com",
    linkedin_url: str | None = "https://linkedin.com/in/person",
    omit_source: bool = False,
    omit_account: bool = False,
) -> dict[str, Any]:
    contact: dict[str, Any] = {}
    if not omit_source:
        contact["source"] = source
    if not omit_account:
        contact["account"] = account
    contact["email"] = email
    contact["linkedin_url"] = linkedin_url
    return contact


# ---------------------------------------------------------------------------
# discover()
# ---------------------------------------------------------------------------


def test_discover_first_run_has_none_new_since_last_run(tmp_path: Path) -> None:
    """No `.last-run` file yet -> new_since_last_run is None, not 0.

    Also asserts the "does nothing extra" contract: _count_contacts is never
    called on the prev-count path when this is the first run.
    """
    with (
        patch(f"{_MOD}.enrich_dir", return_value=tmp_path),
        patch(f"{_MOD}.discover_all_contacts", return_value=[_make_contact()]),
        patch(f"{_MOD}._count_contacts") as mock_count,
    ):
        result = discover()

    assert result.new_since_last_run is None
    mock_count.assert_not_called()


def test_discover_not_first_run_positive_delta_counts_new_contacts(tmp_path: Path) -> None:
    """Previous run recorded fewer contacts -> delta is the (floored) difference."""
    (tmp_path / ".last-run").write_text("2026-01-01T00:00:00+00:00", encoding="utf-8")
    (tmp_path / ".contacts-raw-prev.json").write_text(json.dumps([{}] * 2), encoding="utf-8")
    contacts = [_make_contact() for _ in range(5)]

    with (
        patch(f"{_MOD}.enrich_dir", return_value=tmp_path),
        patch(f"{_MOD}.discover_all_contacts", return_value=contacts),
    ):
        result = discover()

    assert result.new_since_last_run == 3


def test_discover_not_first_run_shrinking_count_floors_at_zero(tmp_path: Path) -> None:
    """Contacts removed since last run -> delta floors at 0, never negative."""
    (tmp_path / ".last-run").write_text("2026-01-01T00:00:00+00:00", encoding="utf-8")
    (tmp_path / ".contacts-raw-prev.json").write_text(json.dumps([{}] * 5), encoding="utf-8")
    contacts = [_make_contact() for _ in range(2)]

    with (
        patch(f"{_MOD}.enrich_dir", return_value=tmp_path),
        patch(f"{_MOD}.discover_all_contacts", return_value=contacts),
    ):
        result = discover()

    assert result.new_since_last_run == 0


def test_discover_backs_up_contacts_raw_via_copy2(tmp_path: Path) -> None:
    """A freshly written contacts-raw.json is backed up via shutil.copy2."""
    contacts = [_make_contact()]
    with (
        patch(f"{_MOD}.enrich_dir", return_value=tmp_path),
        patch(f"{_MOD}.discover_all_contacts", return_value=contacts),
        patch(f"{_MOD}.shutil.copy2", side_effect=real_shutil.copy2) as mock_copy2,
    ):
        discover()

    mock_copy2.assert_called_once_with(tmp_path / "contacts-raw.json", tmp_path / ".contacts-raw-prev.json")
    assert (tmp_path / ".contacts-raw-prev.json").exists()


def test_discover_aggregates_by_source_account_email_linkedin(tmp_path: Path) -> None:
    """The per-contact loop tallies four independent counters, with unknown fallback."""
    contacts = [
        _make_contact(source="gmail", account="acme-corp", email="a@example.com", linkedin_url="https://x"),
        _make_contact(source="gmail", account="acme-corp", email=None, linkedin_url=None),
        _make_contact(omit_source=True, omit_account=True, email="b@example.com", linkedin_url=None),
        _make_contact(source="vault", account="beta-inc", email=None, linkedin_url="https://y"),
    ]

    with (
        patch(f"{_MOD}.enrich_dir", return_value=tmp_path),
        patch(f"{_MOD}.discover_all_contacts", return_value=contacts),
    ):
        result = discover()

    assert result == DiscoverResult(
        total=4,
        with_email=2,
        with_linkedin=2,
        new_since_last_run=None,
        by_source={"gmail": 2, "unknown": 1, "vault": 1},
        by_account={"acme-corp": 2, "unknown": 1, "beta-inc": 1},
    )


# ---------------------------------------------------------------------------
# enrich_records()
# ---------------------------------------------------------------------------


def test_enrich_records_account_none_passes_all_raw_contacts() -> None:
    """No account filter -> every raw contact reaches the pipeline unchanged."""
    raw_contacts = [_make_contact(account="acme-corp"), _make_contact(account="beta-inc")]
    with (
        patch(f"{_MOD}.migrate_legacy_memory_files", return_value=0),
        patch(f"{_MOD}.load_raw_contacts", return_value=raw_contacts),
        patch(f"{_MOD}.run_enrichment_pipeline", return_value=(2, 0)) as mock_pipeline,
    ):
        result = enrich_records()

    mock_pipeline.assert_called_once_with(raw_contacts)
    assert result.total_raw_contacts == 2


def test_enrich_records_account_filters_to_matching_slug() -> None:
    """account=<slug> filters raw_contacts to matching accounts before the pipeline call."""
    acme = _make_contact(account="acme-corp")
    beta = _make_contact(account="beta-inc")
    with (
        patch(f"{_MOD}.migrate_legacy_memory_files", return_value=0),
        patch(f"{_MOD}.load_raw_contacts", return_value=[acme, beta]),
        patch(f"{_MOD}.run_enrichment_pipeline", return_value=(1, 0)) as mock_pipeline,
    ):
        result = enrich_records(account="acme-corp")

    mock_pipeline.assert_called_once_with([acme])
    assert result.total_raw_contacts == 1


def test_enrich_records_empty_after_filter_skips_pipeline_but_still_migrates() -> None:
    """No contacts matching the account filter -> pipeline is skipped entirely.

    Migration still ran first, so migrated_legacy_files reflects whatever
    migrate_legacy_memory_files() returned rather than always 0.
    """
    with (
        patch(f"{_MOD}.migrate_legacy_memory_files", return_value=4),
        patch(f"{_MOD}.load_raw_contacts", return_value=[_make_contact(account="beta-inc")]),
        patch(f"{_MOD}.run_enrichment_pipeline") as mock_pipeline,
    ):
        result = enrich_records(account="acme-corp")

    mock_pipeline.assert_not_called()
    assert result == EnrichRecordsResult(
        total_enriched=0, total_failed=0, migrated_legacy_files=4, total_raw_contacts=0
    )


def test_enrich_records_empty_raw_contacts_from_loader_skips_pipeline() -> None:
    """load_raw_contacts() returning [] hits the same "nothing to do" early return."""
    with (
        patch(f"{_MOD}.migrate_legacy_memory_files", return_value=0),
        patch(f"{_MOD}.load_raw_contacts", return_value=[]),
        patch(f"{_MOD}.run_enrichment_pipeline") as mock_pipeline,
    ):
        result = enrich_records()

    mock_pipeline.assert_not_called()
    assert result.total_raw_contacts == 0


def test_enrich_records_happy_path_plumbs_pipeline_and_migration_counts() -> None:
    """Non-empty contacts -> pipeline runs once; its return tuple and the migration
    count are plumbed through verbatim, not hardcoded or swapped."""
    raw_contacts = [_make_contact(account="acme-corp"), _make_contact(account="acme-corp")]
    with (
        patch(f"{_MOD}.migrate_legacy_memory_files", return_value=3),
        patch(f"{_MOD}.load_raw_contacts", return_value=raw_contacts),
        patch(f"{_MOD}.run_enrichment_pipeline", return_value=(5, 2)) as mock_pipeline,
    ):
        result = enrich_records()

    mock_pipeline.assert_called_once_with(raw_contacts)
    assert result == EnrichRecordsResult(
        total_enriched=5, total_failed=2, migrated_legacy_files=3, total_raw_contacts=2
    )
