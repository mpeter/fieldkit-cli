"""Tests for fieldkit.enrich.web_enrich."""

import json

import pytest

import fieldkit.enrich._io as _io_mod
from fieldkit.enrich.web_enrich import extract_contacts_needing_enrichment, main


@pytest.fixture(autouse=True)
def patch_enrich_dir(monkeypatch, tmp_path):
    """Redirect enrich_dir() to tmp_path for all tests in this module."""
    real_fn = _io_mod._enrich_dir
    real_fn.cache_clear()
    monkeypatch.setattr(_io_mod, "_enrich_dir", lambda: tmp_path)
    # web_enrich imports enrich_dir directly from _io; patch that reference too.
    import fieldkit.enrich.web_enrich as web_mod

    monkeypatch.setattr(web_mod, "enrich_dir", lambda: tmp_path)
    yield
    real_fn.cache_clear()


@pytest.mark.unit
def test_extract_contacts_needing_enrichment_missing_file(tmp_path):
    """Returns empty list when contacts-raw.json does not exist."""
    result = extract_contacts_needing_enrichment()
    assert result == []


@pytest.mark.unit
def test_extract_contacts_needing_enrichment_all_complete(tmp_path):
    """Returns empty list when all contacts already have email, linkedin, or phone."""
    contacts = [
        {
            "full_name": "Alice Smith",
            "company": "Acme",
            "account": "acme",
            "title": "VP",
            "email": "alice@acme.example.com",
        },
        {
            "full_name": "Bob Jones",
            "company": "Beta",
            "account": "beta",
            "title": "CTO",
            "linkedin_url": "https://linkedin.com/in/bob",
        },
        {"full_name": "Carol Lee", "company": "Gamma", "account": "gamma", "title": "Dir", "phone": "+15550001234"},
    ]
    (tmp_path / "contacts-raw.json").write_text(json.dumps(contacts), encoding="utf-8")

    result = extract_contacts_needing_enrichment()

    assert result == []


@pytest.mark.unit
def test_extract_contacts_needing_enrichment_partial(tmp_path):
    """Returns only contacts missing all of email, linkedin_url, and phone."""
    contacts = [
        # Has email — should be excluded
        {
            "full_name": "Alice Smith",
            "company": "Acme",
            "account": "acme",
            "title": "VP",
            "email": "alice@acme.example.com",
        },
        # Missing all three — should be included
        {"full_name": "Bob Jones", "company": "Beta Corp", "account": "beta", "title": "CTO"},
        # Has phone — should be excluded
        {"full_name": "Carol Lee", "company": "Gamma", "account": "gamma", "title": "Dir", "phone": "+15550001234"},
        # Missing all three — should be included
        {"full_name": "Dave Wu", "company": "Delta Inc", "account": "delta", "title": "AE"},
    ]
    (tmp_path / "contacts-raw.json").write_text(json.dumps(contacts), encoding="utf-8")

    result = extract_contacts_needing_enrichment()

    assert len(result) == 2
    names = {r["full_name"] for r in result}
    assert names == {"Bob Jones", "Dave Wu"}


@pytest.mark.unit
def test_extract_contacts_needing_enrichment_search_query_format(tmp_path):
    """Each returned contact has a search_query combining name, company, and keywords."""
    contacts = [
        {"full_name": "Eve Nakamura", "company": "Omega Ltd", "account": "omega", "title": "PM"},
    ]
    (tmp_path / "contacts-raw.json").write_text(json.dumps(contacts), encoding="utf-8")

    result = extract_contacts_needing_enrichment()

    assert len(result) == 1
    entry = result[0]
    assert entry["full_name"] == "Eve Nakamura"
    assert entry["company"] == "Omega Ltd"
    assert "Eve Nakamura" in entry["search_query"]
    assert "Omega Ltd" in entry["search_query"]
    assert "LinkedIn" in entry["search_query"]


@pytest.mark.unit
def test_extract_contacts_needing_enrichment_empty_file(tmp_path):
    """Returns empty list when contacts-raw.json contains an empty array."""
    (tmp_path / "contacts-raw.json").write_text("[]", encoding="utf-8")

    result = extract_contacts_needing_enrichment()

    assert result == []


@pytest.mark.unit
def test_main_routes_generated_searches_through_tvly(tmp_path, capsys):
    """Generated operator guidance uses the supported Tavily CLI route."""
    contacts = [
        {"full_name": "Eve Nakamura", "company": "Omega Ltd", "account": "omega", "title": "PM"},
    ]
    (tmp_path / "contacts-raw.json").write_text(json.dumps(contacts), encoding="utf-8")

    main()

    output = capsys.readouterr().out
    assert "tvly search" in output
    assert "MCP" not in output
