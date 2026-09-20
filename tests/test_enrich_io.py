"""Tests for fieldkit.enrich._io loader helpers."""

import json

import pytest

import fieldkit.enrich._io as _io_mod
from fieldkit.enrich._io import load_enriched_contacts, load_raw_contacts


@pytest.fixture(autouse=True)
def patch_enrich_dir(monkeypatch, tmp_path):
    """Redirect _enrich_dir() to tmp_path for all tests in this module."""
    real_fn = _io_mod._enrich_dir  # save the @cache-decorated original
    real_fn.cache_clear()
    monkeypatch.setattr(_io_mod, "_enrich_dir", lambda: tmp_path)
    yield
    real_fn.cache_clear()  # clear cache on the real function after restore


def test_load_raw_contacts_file_present(tmp_path):
    """load_raw_contacts returns parsed list when contacts_raw.json exists."""
    data = [{"full_name": "Alice Smith", "account": "acme"}]
    (tmp_path / "contacts-raw.json").write_text(json.dumps(data), encoding="utf-8")

    result = load_raw_contacts()

    assert result == data


def test_load_raw_contacts_file_missing(tmp_path):
    """load_raw_contacts returns [] when contacts_raw.json is absent."""
    result = load_raw_contacts()
    assert result == []


def test_load_enriched_contacts_file_present(tmp_path):
    """load_enriched_contacts returns parsed list when contacts_enriched.json exists."""
    data = [{"full_name": "Bob Jones", "account": "beta", "confidence": "HIGH"}]
    (tmp_path / "contacts-enriched.json").write_text(json.dumps(data), encoding="utf-8")

    result = load_enriched_contacts()

    assert result == data


def test_load_enriched_contacts_file_missing(tmp_path):
    """load_enriched_contacts returns [] when contacts_enriched.json is absent."""
    result = load_enriched_contacts()
    assert result == []


# ---------------------------------------------------------------------------
# 4B.2 contacts_memory_dir
# ---------------------------------------------------------------------------


# ── TestContactsMemoryDir (flattened) ───────────────────────────────────────


@pytest.mark.unit
def test_contacts_memory_dir_creates_and_returns_path(tmp_path, monkeypatch) -> None:
    """contacts_memory_dir creates and returns the memory/personal/contacts path."""
    from unittest.mock import patch

    from fieldkit.enrich._io import contacts_memory_dir

    with patch("fieldkit.enrich._io.get_fieldkit_home", return_value=tmp_path):
        result = contacts_memory_dir()

    assert isinstance(result, __import__("pathlib").Path)
    assert result.exists()


@pytest.mark.unit
def test_contacts_memory_dir_returns_correct_subpath(tmp_path, monkeypatch) -> None:
    """contacts_memory_dir path ends with memory/personal/contacts."""
    from unittest.mock import patch

    from fieldkit.enrich._io import contacts_memory_dir

    with patch("fieldkit.enrich._io.get_fieldkit_home", return_value=tmp_path):
        result = contacts_memory_dir()

    assert result.parts[-3:] == ("memory", "personal", "contacts")


# ---------------------------------------------------------------------------
# 4B.3 migrate_legacy_memory_files
# ---------------------------------------------------------------------------


# ── TestMigrateLegacyMemoryFiles (flattened) ────────────────────────────────


@pytest.mark.unit
def test_migrate_legacy_memory_files_returns_zero_when_no_legacy_dir(tmp_path) -> None:
    """Returns 0 when no legacy directory exists."""
    from unittest.mock import patch

    from fieldkit.enrich._io import migrate_legacy_memory_files

    with patch("fieldkit.enrich._io.get_fieldkit_home", return_value=tmp_path):
        result = migrate_legacy_memory_files()

    assert result == 0


@pytest.mark.unit
def test_migrate_legacy_memory_files_migrates_files(tmp_path) -> None:
    """Moves .md files from legacy dir to contacts_memory_dir; returns count."""
    from unittest.mock import patch

    from fieldkit.enrich._io import migrate_legacy_memory_files

    # Set up legacy dir with 2 .md files
    legacy = tmp_path / "contact-enrich" / "memory"
    legacy.mkdir(parents=True)
    (legacy / "alice.md").write_text("Alice", encoding="utf-8")
    (legacy / "bob.md").write_text("Bob", encoding="utf-8")

    dest = tmp_path / "memory" / "personal" / "contacts"
    dest.mkdir(parents=True, exist_ok=True)

    # contacts_memory_dir() is @cache so patching get_fieldkit_home alone is
    # insufficient once the cache is warm from another test. Patch both.
    with (
        patch("fieldkit.enrich._io.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.enrich._io.contacts_memory_dir", return_value=dest),
    ):
        result = migrate_legacy_memory_files()

    assert result == 2
    assert (dest / "alice.md").exists()
    assert (dest / "bob.md").exists()
