"""Unit tests for _resolve_account_id_by_pursuit_scan (012-gazecrap-reduction, implementation note).

This function is the "Strategy 2" account ID resolver in commands/sf/account.py:
when SF keyword search returns no results, it scans local pursuit files for
sf_opportunity_ids and makes a single batch sosl_search() call (implementation note).

All tests patch get_fieldkit_home to redirect filesystem access to tmp_path.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.commands.sf.account import _resolve_account_id_by_pursuit_scan
from fieldkit.sf.client import SFAPIError, SFAuthError

pytestmark = pytest.mark.unit

# Valid Salesforce ID: exactly 15 alphanumeric chars (matches _SF_ID_RE anchored alternation)
_VALID_OPP_ID = "006AAAAAAAAAAAA"
_EXPECTED_ACCOUNT_ID = "001BBBBBBBBBBBBB"

# Minimal valid frontmatter for a pursuit file with sf_opportunity_id
_FRONTMATTER_WITH_OPP_ID = f"---\nstage: negotiate\nsf_opportunity_id: {_VALID_OPP_ID}\n---\nBody text.\n"

# Frontmatter without sf_opportunity_id key
_FRONTMATTER_WITHOUT_OPP_ID = "---\nstage: negotiate\n---\nBody text.\n"


def _make_pursuits_dir(tmp_path: Path, account_name: str) -> Path:
    """Create the standard accounts/<name>/pursuits/ directory tree."""
    pursuits_dir = tmp_path / "accounts" / account_name / "pursuits"
    pursuits_dir.mkdir(parents=True)
    return pursuits_dir


# ── TestResolveAccountIdByPursuitScan (flattened) ───────────────────────────


def test_resolve_account_id_by_pursuit_scan_no_pursuits_dir_returns_none(tmp_path: Path) -> None:
    """Returns None when the accounts/<name>/pursuits/ directory does not exist."""
    with patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path):
        result = _resolve_account_id_by_pursuit_scan("acme-corp", {}, MagicMock())
    assert result is None


def test_resolve_account_id_by_pursuit_scan_no_opp_id_in_frontmatter_returns_none(tmp_path: Path) -> None:
    """Returns None when pursuit files exist but none have sf_opportunity_id."""
    pursuits_dir = _make_pursuits_dir(tmp_path, "acme-corp")
    (pursuits_dir / "renewal-2026.md").write_text(_FRONTMATTER_WITHOUT_OPP_ID, encoding="utf-8")
    mock_client = MagicMock()

    with patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path):
        result = _resolve_account_id_by_pursuit_scan("acme-corp", {}, mock_client)

    assert result is None
    # implementation note: no API call when no valid opp IDs collected
    mock_client.sosl_search.assert_not_called()


def test_resolve_account_id_by_pursuit_scan_valid_opp_id_returns_account_id(tmp_path: Path) -> None:
    """Returns SF Account ID when pursuit has valid opp ID and sosl_search succeeds."""
    pursuits_dir = _make_pursuits_dir(tmp_path, "acme-corp")
    (pursuits_dir / "renewal-2026.md").write_text(_FRONTMATTER_WITH_OPP_ID, encoding="utf-8")
    mock_client = MagicMock()
    # implementation note: sosl_search returns list[dict] directly
    mock_client.sosl_search.return_value = [{"AccountId": _EXPECTED_ACCOUNT_ID}]

    with patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path):
        result = _resolve_account_id_by_pursuit_scan("acme-corp", {}, mock_client)

    assert result == _EXPECTED_ACCOUNT_ID
    mock_client.sosl_search.assert_called_once()


def test_resolve_account_id_by_pursuit_scan_sf_auth_error_on_sosl_search_propagates(tmp_path: Path) -> None:
    """SFAuthError from sosl_search propagates (must not be caught)."""
    pursuits_dir = _make_pursuits_dir(tmp_path, "acme-corp")
    (pursuits_dir / "renewal-2026.md").write_text(_FRONTMATTER_WITH_OPP_ID, encoding="utf-8")
    mock_client = MagicMock()
    mock_client.sosl_search.side_effect = SFAuthError("session expired")

    with (
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
        pytest.raises(SFAuthError, match="session expired"),
    ):
        _resolve_account_id_by_pursuit_scan("acme-corp", {}, mock_client)


def test_resolve_account_id_by_pursuit_scan_sf_api_error_on_sosl_search_returns_none(tmp_path: Path) -> None:
    """SFAPIError from sosl_search is caught; returns None."""
    pursuits_dir = _make_pursuits_dir(tmp_path, "acme-corp")
    (pursuits_dir / "renewal-2026.md").write_text(_FRONTMATTER_WITH_OPP_ID, encoding="utf-8")
    mock_client = MagicMock()
    mock_client.sosl_search.side_effect = SFAPIError("API error")

    with patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path):
        result = _resolve_account_id_by_pursuit_scan("acme-corp", {}, mock_client)

    assert result is None


def test_oserror_reading_pursuit_file_is_skipped(tmp_path: Path) -> None:
    """_resolve_account_id_by_pursuit_scan skips files that raise OSError on read."""
    pursuits_dir = _make_pursuits_dir(tmp_path, "acme-corp")
    broken_file = pursuits_dir / "broken.md"
    broken_file.write_text("---\nstage: negotiate\n---\nBody.\n", encoding="utf-8")

    mock_client = MagicMock()

    with (
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
        patch("pathlib.Path.read_text", side_effect=OSError("permission denied")),
    ):
        result = _resolve_account_id_by_pursuit_scan("acme-corp", {}, mock_client)

    assert result is None
    mock_client.sosl_search.assert_not_called()


def test_first_pursuit_no_opp_id_second_has_opp_id_returns_account_id(tmp_path: Path) -> None:
    """Scanning continues past pursuits without opp ID; returns result from first matching file."""
    pursuits_dir = _make_pursuits_dir(tmp_path, "acme-corp")
    # File a.md has no opp ID; b.md has a valid one. sorted() → alphabetical → a first.
    (pursuits_dir / "a-no-id.md").write_text(_FRONTMATTER_WITHOUT_OPP_ID, encoding="utf-8")
    (pursuits_dir / "b-with-id.md").write_text(_FRONTMATTER_WITH_OPP_ID, encoding="utf-8")

    mock_client = MagicMock()
    mock_client.sosl_search.return_value = [{"AccountId": _EXPECTED_ACCOUNT_ID}]

    with patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path):
        result = _resolve_account_id_by_pursuit_scan("acme-corp", {}, mock_client)

    assert result == _EXPECTED_ACCOUNT_ID
    # implementation note: single sosl_search call with the collected opp ID
    mock_client.sosl_search.assert_called_once()


def test_all_pursuits_scanned_none_have_opp_id_returns_none(tmp_path: Path) -> None:
    """Returns None when every pursuit file is scanned but none have sf_opportunity_id."""
    pursuits_dir = _make_pursuits_dir(tmp_path, "acme-corp")
    (pursuits_dir / "deal-a.md").write_text(_FRONTMATTER_WITHOUT_OPP_ID, encoding="utf-8")
    (pursuits_dir / "deal-b.md").write_text(_FRONTMATTER_WITHOUT_OPP_ID, encoding="utf-8")
    (pursuits_dir / "deal-c.md").write_text(_FRONTMATTER_WITHOUT_OPP_ID, encoding="utf-8")

    mock_client = MagicMock()

    with patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path):
        result = _resolve_account_id_by_pursuit_scan("acme-corp", {}, mock_client)

    assert result is None
    mock_client.sosl_search.assert_not_called()
