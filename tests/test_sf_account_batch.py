"""Unit tests for the batch SOSL strategy in _resolve_account_id_by_pursuit_scan.

Spec 032 — implementation note: the function was refactored from N sequential fetch_record()
calls (one per pursuit file) to a single batch sosl_search() call per chunk of
_SOSL_CHUNK_SIZE (200) IDs.

All tests use tmp_path for filesystem isolation and monkeypatch
get_fieldkit_home to redirect the pursuit directory lookup.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.commands.sf.account import (
    _SOSL_CHUNK_SIZE,
    _resolve_account_id_by_pursuit_scan,
)
from fieldkit.sf.client import SFAPIError, SFAuthError

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Valid 18-char alphanumeric Salesforce opportunity ID (matches _SF_ID_RE).
# Format: '006' (3) + 12 digits + 'AAA' (3) = 18 chars exactly.
_VALID_OPP_ID = "006000000000000AAA"
_EXPECTED_ACCOUNT_ID = "001000000000000AAA"

# Minimal frontmatter with a valid sf_opportunity_id
_FM_WITH_OPP_ID = f"---\nstage: negotiate\nsf_opportunity_id: {_VALID_OPP_ID}\n---\nBody.\n"

# Frontmatter with an invalid / placeholder opp ID (rejected by _SF_ID_RE.fullmatch)
_FM_INVALID_OPP_ID = "---\nstage: negotiate\nsf_opportunity_id: NEEDS-LOOKUP\n---\nBody.\n"

# Frontmatter with no sf_opportunity_id key at all
_FM_NO_OPP_ID = "---\nstage: negotiate\n---\nBody.\n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pursuits_dir(tmp_path: Path, account_name: str = "acme") -> Path:
    """Create accounts/<name>/pursuits/ under tmp_path and return the path."""
    pursuits_dir = tmp_path / "accounts" / account_name / "pursuits"
    pursuits_dir.mkdir(parents=True)
    return pursuits_dir


def _write_pursuit(pursuits_dir: Path, name: str, content: str) -> Path:
    """Write a pursuit .md file and return its path."""
    path = pursuits_dir / f"{name}.md"
    path.write_text(content, encoding="utf-8")
    return path


def _make_valid_opp_id(index: int) -> str:
    """Generate a unique valid 18-char alphanumeric Salesforce ID.

    Format: '006' (3) + 12-digit zero-padded index + 'AAA' (3) = 18 chars.
    All characters are alphanumeric — matches _SF_ID_RE.fullmatch().
    """
    return f"006{index:012d}AAA"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_empty_pursuit_dir_returns_none(tmp_path: Path) -> None:
    """Returns None without calling sosl_search when the pursuits dir has no .md files."""
    pursuits_dir = _make_pursuits_dir(tmp_path)
    # Directory exists but is empty — no .md files.
    assert pursuits_dir.is_dir()

    mock_client = MagicMock()
    with patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path):
        result = _resolve_account_id_by_pursuit_scan("acme", {}, mock_client)

    assert result is None
    mock_client.sosl_search.assert_not_called()


def test_all_invalid_opp_ids_returns_none(tmp_path: Path) -> None:
    """Returns None without calling sosl_search when all opp IDs fail _SF_ID_RE.fullmatch."""
    pursuits_dir = _make_pursuits_dir(tmp_path)
    _write_pursuit(pursuits_dir, "deal-a", _FM_INVALID_OPP_ID)
    _write_pursuit(pursuits_dir, "deal-b", _FM_NO_OPP_ID)

    mock_client = MagicMock()
    with patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path):
        result = _resolve_account_id_by_pursuit_scan("acme", {}, mock_client)

    assert result is None
    mock_client.sosl_search.assert_not_called()


def test_single_valid_opp_id_calls_sosl_once(tmp_path: Path) -> None:
    """sosl_search is called exactly once when there is one valid opp ID."""
    pursuits_dir = _make_pursuits_dir(tmp_path)
    _write_pursuit(pursuits_dir, "deal-a", _FM_WITH_OPP_ID)

    mock_client = MagicMock()
    mock_client.sosl_search.return_value = [{"AccountId": _EXPECTED_ACCOUNT_ID}]

    with patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path):
        result = _resolve_account_id_by_pursuit_scan("acme", {}, mock_client)

    assert result == _EXPECTED_ACCOUNT_ID
    mock_client.sosl_search.assert_called_once()


def test_sosl_search_returns_account_id(tmp_path: Path) -> None:
    """Returns the AccountId from the first matching record in sosl_search results."""
    pursuits_dir = _make_pursuits_dir(tmp_path)
    _write_pursuit(pursuits_dir, "deal-a", _FM_WITH_OPP_ID)

    mock_client = MagicMock()
    mock_client.sosl_search.return_value = [{"AccountId": "001abc123456789ABC"}]

    with patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path):
        result = _resolve_account_id_by_pursuit_scan("acme", {}, mock_client)

    assert result == "001abc123456789ABC"


def test_sfapi_error_caught_returns_none(tmp_path: Path) -> None:
    """SFAPIError from sosl_search is caught (continue to next chunk); returns None."""
    pursuits_dir = _make_pursuits_dir(tmp_path)
    _write_pursuit(pursuits_dir, "deal-a", _FM_WITH_OPP_ID)

    mock_client = MagicMock()
    mock_client.sosl_search.side_effect = SFAPIError("SOSL query failed")

    with patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path):
        result = _resolve_account_id_by_pursuit_scan("acme", {}, mock_client)

    assert result is None
    mock_client.sosl_search.assert_called_once()


def test_sfauth_error_propagates(tmp_path: Path) -> None:
    """SFAuthError from sosl_search propagates — it is NOT caught by the batch loop."""
    pursuits_dir = _make_pursuits_dir(tmp_path)
    _write_pursuit(pursuits_dir, "deal-a", _FM_WITH_OPP_ID)

    mock_client = MagicMock()
    mock_client.sosl_search.side_effect = SFAuthError("session expired")

    with (
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
        pytest.raises(SFAuthError, match="session expired"),
    ):
        _resolve_account_id_by_pursuit_scan("acme", {}, mock_client)


def test_chunking_with_201_ids(tmp_path: Path) -> None:
    """201 valid opp IDs → sosl_search called TWICE (chunk 1: 200 IDs, chunk 2: 1 ID)."""
    pursuits_dir = _make_pursuits_dir(tmp_path)

    # Write 201 pursuit files, each with a unique valid 18-char opp ID.
    for i in range(201):
        opp_id = _make_valid_opp_id(i)
        content = f"---\nstage: negotiate\nsf_opportunity_id: {opp_id}\n---\nBody.\n"
        _write_pursuit(pursuits_dir, f"deal-{i:04d}", content)

    mock_client = MagicMock()
    # Return no AccountId so the loop exhausts all chunks.
    mock_client.sosl_search.return_value = []

    with patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path):
        result = _resolve_account_id_by_pursuit_scan("acme", {}, mock_client)

    assert result is None
    assert mock_client.sosl_search.call_count == 2, (
        f"Expected 2 sosl_search calls for 201 IDs (chunk size {_SOSL_CHUNK_SIZE}), "
        f"got {mock_client.sosl_search.call_count}"
    )

    # Verify the SOSL strings passed to each call contain the right number of IDs.
    first_call_sosl: str = mock_client.sosl_search.call_args_list[0][0][0]
    second_call_sosl: str = mock_client.sosl_search.call_args_list[1][0][0]

    # The SOSL string is: FIND {id1 OR id2 OR ...} IN ALL FIELDS RETURNING Opportunity(AccountId)
    # Count " OR " separators: N IDs → N-1 separators.
    first_chunk_ids = first_call_sosl.count(" OR ") + 1
    second_chunk_ids = second_call_sosl.count(" OR ") + 1

    assert first_chunk_ids == _SOSL_CHUNK_SIZE, f"First chunk should have {_SOSL_CHUNK_SIZE} IDs, got {first_chunk_ids}"
    assert second_chunk_ids == 1, f"Second chunk should have 1 ID, got {second_chunk_ids}"
