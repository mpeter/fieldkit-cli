"""Unit tests for lib/pursuit.py shared utility functions."""

from datetime import date, timedelta

import pytest

from fieldkit.pursuit import (
    calculate_days_since,
    extract_champion_name,
    iterate_pursuits,
    read_accounts_config,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# iterate_pursuits
# ---------------------------------------------------------------------------


def test_iterate_pursuits_skips_template(tmp_path):
    """Paths containing '.template' are excluded."""
    template_dir = tmp_path / "accounts" / ".template" / "pursuits"
    template_dir.mkdir(parents=True)
    (template_dir / "opp.md").write_text("---\n", encoding="utf-8")

    results = list(iterate_pursuits(tmp_path))
    assert results == []


def test_iterate_pursuits_skips_gmail_intel(tmp_path):
    """Paths containing 'gmail-intel' are excluded."""
    gmail_dir = tmp_path / "accounts" / "global-pay" / "pursuits"
    gmail_dir.mkdir(parents=True)
    (gmail_dir / "gmail-intel.md").write_text("---\n", encoding="utf-8")

    results = list(iterate_pursuits(tmp_path))
    assert results == []


def test_iterate_pursuits_returns_valid_pursuits(tmp_path):
    """Normal pursuit files are yielded."""
    pursuits_dir = tmp_path / "accounts" / "global-pay" / "pursuits"
    pursuits_dir.mkdir(parents=True)
    expected = pursuits_dir / "project-alpha.md"
    expected.write_text("---\nstage: discover\n", encoding="utf-8")

    results = list(iterate_pursuits(tmp_path))
    assert results == [expected]


def test_iterate_pursuits_mixed(tmp_path):
    """Template and gmail-intel paths excluded; normal paths included."""
    good_dir = tmp_path / "accounts" / "acme-bank" / "pursuits"
    good_dir.mkdir(parents=True)
    good_file = good_dir / "deal.md"
    good_file.write_text("---\n", encoding="utf-8")

    bad_dir = tmp_path / "accounts" / ".template" / "pursuits"
    bad_dir.mkdir(parents=True)
    (bad_dir / "opp.md").write_text("---\n", encoding="utf-8")

    results = list(iterate_pursuits(tmp_path))
    assert results == [good_file]


# ---------------------------------------------------------------------------
# calculate_days_since
# ---------------------------------------------------------------------------


def test_calculate_days_since_empty_string():
    assert calculate_days_since("") == -1


def test_calculate_days_since_none_like():
    assert calculate_days_since("not-a-date") == -1


def test_calculate_days_since_known_date():
    ten_days_ago = (date.today() - timedelta(days=10)).isoformat()
    result = calculate_days_since(ten_days_ago)
    assert result == 10


def test_calculate_days_since_today():
    assert calculate_days_since(date.today().isoformat()) == 0


# ---------------------------------------------------------------------------
# extract_champion_name
# ---------------------------------------------------------------------------


def test_extract_champion_name_found(tmp_path):
    (tmp_path / "account.md").write_text("**Champion:** Jane Smith (VP Eng)\n", encoding="utf-8")
    assert extract_champion_name(tmp_path) == "Jane"


def test_extract_champion_name_not_found(tmp_path):
    (tmp_path / "account.md").write_text("# Account\nNo champion here.\n", encoding="utf-8")
    assert extract_champion_name(tmp_path) == ""


def test_extract_champion_name_missing_file(tmp_path):
    assert extract_champion_name(tmp_path) == ""


def test_extract_champion_name_comma_separated(tmp_path):
    (tmp_path / "account.md").write_text("**Champion:** Bob Jones, backup Alice\n", encoding="utf-8")
    assert extract_champion_name(tmp_path) == "Bob"


# ---------------------------------------------------------------------------
# read_accounts_config
# ---------------------------------------------------------------------------


def test_read_accounts_config_returns_dict(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "accounts.yaml").write_text("accounts:\n  - name: global-pay\n", encoding="utf-8")
    result = read_accounts_config(tmp_path)
    assert isinstance(result, dict)
    assert "accounts" in result


def test_read_accounts_config_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError) as exc_info:
        read_accounts_config(tmp_path)
    assert exc_info.type is FileNotFoundError
