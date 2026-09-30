"""Tests for fieldkit.enrich._io loader helpers."""

import json

import pytest

import fieldkit.enrich._io as _io_mod
from fieldkit.enrich._io import load_enriched_contacts, load_raw_contacts


@pytest.fixture(autouse=True)
def patch_enrich_dir(monkeypatch, tmp_path):
    """Redirect enrich_dir() to tmp_path for all tests in this module."""
    monkeypatch.setattr(_io_mod, "enrich_dir", lambda: tmp_path)


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
