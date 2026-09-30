"""Tests for fieldkit.commands.sf.account._resolve_sf_account_id — branch coverage."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.commands.sf.account import _resolve_sf_account_id
from fieldkit.errors import FieldkitError

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(
    keyword_result: str | None = None,
    fetch_result: dict[str, Any] | None = None,
) -> MagicMock:
    client = MagicMock()
    client.resolve_account_id_by_keywords.return_value = keyword_result
    if fetch_result is not None:
        client.fetch_record.return_value = fetch_result
    return client


def _write_pursuit(path: Path, sf_opportunity_id: str | None = None) -> None:
    fm = "---\nstage: discover\n"
    if sf_opportunity_id is not None:
        fm += f"sf_opportunity_id: {sf_opportunity_id}\n"
    fm += "---\n\n# Deal\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fm, encoding="utf-8")


# ---------------------------------------------------------------------------
# Strategy 1: SOSL keyword search
# ---------------------------------------------------------------------------


# ── TestResolveViaSosl (flattened) ──────────────────────────────────────────


def test_resolve_via_sosl_keyword_match_returns_id(tmp_path: Path) -> None:
    client = _make_client(keyword_result="001XXXXXXXXXXXXXXXXX")

    with patch(
        "fieldkit.commands.sf.account.get_accounts_config",
        return_value={"accounts": {"acme": {"keywords": ["acme", "corporation"]}}},
    ):
        result = _resolve_sf_account_id("acme", client, "https://sf.com")

    assert result == "001XXXXXXXXXXXXXXXXX"
    client.resolve_account_id_by_keywords.assert_called_once()


def test_resolve_via_sosl_no_keywords_skips_sosl(tmp_path: Path) -> None:
    client = _make_client(keyword_result=None)

    with (
        patch(
            "fieldkit.commands.sf.account.get_accounts_config",
            return_value={"accounts": {"acme": {"keywords": []}}},
        ),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
    ):
        _resolve_sf_account_id("acme", client, "https://sf.com")

    client.resolve_account_id_by_keywords.assert_not_called()


def test_resolve_via_sosl_sosl_returns_none_falls_through(tmp_path: Path) -> None:
    """When SOSL returns None, strategy 2 (file scan) is attempted."""
    client = _make_client(keyword_result=None)

    with (
        patch(
            "fieldkit.commands.sf.account.get_accounts_config",
            return_value={"accounts": {"acme": {"keywords": ["acme"]}}},
        ),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
    ):
        result = _resolve_sf_account_id("acme", client, "https://sf.com")

    # Pursuit dir doesn't exist in tmp_path → None
    assert result is None


# ---------------------------------------------------------------------------
# Strategy 2: local pursuit file scan
# ---------------------------------------------------------------------------


# ── TestResolveViaPursuitFiles (flattened) ──────────────────────────────────


def test_resolve_via_pursuit_files_valid_opp_id_fetches_account(tmp_path: Path) -> None:
    """implementation note: single sosl_search call replaces per-file fetch_record."""
    opp_id = "006Pe000012n2GkIAI"  # 18-char alphanumeric
    pursuit = tmp_path / "accounts" / "acme" / "pursuits" / "deal.md"
    _write_pursuit(pursuit, sf_opportunity_id=opp_id)

    client = _make_client(keyword_result=None)
    # implementation note: sosl_search returns list[dict] directly
    client.sosl_search.return_value = [{"AccountId": "001ABC123456789ABC"}]

    with (
        patch(
            "fieldkit.commands.sf.account.get_accounts_config",
            return_value={"accounts": {"acme": {}}},
        ),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
    ):
        result = _resolve_sf_account_id("acme", client, "https://sf.com")

    assert result == "001ABC123456789ABC"
    client.sosl_search.assert_called_once()
    client.fetch_record.assert_not_called()


def test_resolve_via_pursuit_files_invalid_opp_id_placeholder_rejected(tmp_path: Path) -> None:
    """An invalid identity cannot become a completed no-match scan."""
    pursuit = tmp_path / "accounts" / "acme" / "pursuits" / "deal.md"
    _write_pursuit(pursuit, sf_opportunity_id="NEEDS-LOOKUP")

    client = _make_client(keyword_result=None)

    with (
        patch(
            "fieldkit.commands.sf.account.get_accounts_config",
            return_value={"accounts": {"acme": {}}},
        ),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
        pytest.raises(FieldkitError, match="Salesforce pursuit opportunity identity is invalid") as caught,
    ):
        _resolve_sf_account_id("acme", client, "https://sf.com")

    assert "NEEDS-LOOKUP" not in str(caught.value)
    client.sosl_search.assert_not_called()
    client.fetch_record.assert_not_called()


def test_resolve_via_pursuit_files_no_opp_id_in_frontmatter_skipped(tmp_path: Path) -> None:
    pursuit = tmp_path / "accounts" / "acme" / "pursuits" / "deal.md"
    _write_pursuit(pursuit, sf_opportunity_id=None)

    client = _make_client(keyword_result=None)

    with (
        patch(
            "fieldkit.commands.sf.account.get_accounts_config",
            return_value={"accounts": {"acme": {}}},
        ),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
    ):
        result = _resolve_sf_account_id("acme", client, "https://sf.com")

    assert result is None


def test_resolve_via_pursuit_files_api_error_on_sosl_search_propagates(tmp_path: Path) -> None:
    """Provider failure must remain distinguishable from no match."""
    from fieldkit.sf.errors import SFAPIError

    opp_id1 = "006Pe000012n2GkIAI"
    opp_id2 = "006Pe000012n2GkIAA"

    pursuit1 = tmp_path / "accounts" / "acme" / "pursuits" / "a-deal.md"
    pursuit2 = tmp_path / "accounts" / "acme" / "pursuits" / "b-deal.md"
    _write_pursuit(pursuit1, sf_opportunity_id=opp_id1)
    _write_pursuit(pursuit2, sf_opportunity_id=opp_id2)

    client = _make_client(keyword_result=None)
    # Both IDs are collected into one request; its provider failure must propagate.
    client.sosl_search.side_effect = SFAPIError("500 error")

    with (
        patch(
            "fieldkit.commands.sf.account.get_accounts_config",
            return_value={"accounts": {"acme": {}}},
        ),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
        pytest.raises(SFAPIError, match="500 error"),
    ):
        _resolve_sf_account_id("acme", client, "https://sf.com")


def test_resolve_via_pursuit_files_no_account_id_in_response_continues(tmp_path: Path) -> None:
    """If Account.Id is missing from response, skips to next file."""
    opp_id = "006Pe000012n2GkIAI"
    pursuit = tmp_path / "accounts" / "acme" / "pursuits" / "deal.md"
    _write_pursuit(pursuit, sf_opportunity_id=opp_id)

    client = _make_client(
        keyword_result=None,
        fetch_result={"Account": {"Id": None}},
    )

    with (
        patch(
            "fieldkit.commands.sf.account.get_accounts_config",
            return_value={"accounts": {"acme": {}}},
        ),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
    ):
        result = _resolve_sf_account_id("acme", client, "https://sf.com")

    assert result is None


def test_resolve_via_pursuit_files_account_info_not_dict_returns_none() -> None:
    """When account config is not a dict, returns None immediately."""
    client = _make_client()

    with patch(
        "fieldkit.commands.sf.account.get_accounts_config",
        return_value={"accounts": {"acme": "not-a-dict"}},
    ):
        result = _resolve_sf_account_id("acme", client, "https://sf.com")

    assert result is None
    client.resolve_account_id_by_keywords.assert_not_called()


def test_resolve_via_pursuit_files_get_fieldkit_home_error_propagates() -> None:
    """A failed local scan remains distinguishable from a completed no-match."""
    client = _make_client(keyword_result=None)

    with (
        patch(
            "fieldkit.commands.sf.account.get_accounts_config",
            return_value={"accounts": {"acme": {"keywords": ["acme"]}}},
        ),
        patch(
            "fieldkit.commands.sf.account.get_fieldkit_home",
            side_effect=OSError("no config"),
        ),
        pytest.raises(OSError, match="no config"),
    ):
        _resolve_sf_account_id("acme", client, "https://sf.com")


def test_resolve_via_pursuit_files_pursuit_dir_not_dir_returns_none(tmp_path: Path) -> None:
    """When pursuit_dir does not exist, returns None."""
    client = _make_client(keyword_result=None)

    with (
        patch(
            "fieldkit.commands.sf.account.get_accounts_config",
            return_value={"accounts": {"acme": {}}},
        ),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
    ):
        # Default pursuit_dir would be accounts/acme/pursuits — not created
        result = _resolve_sf_account_id("acme", client, "https://sf.com")

    assert result is None


def test_resolve_via_pursuit_files_legacy_directory_cannot_redirect_scan(tmp_path: Path) -> None:
    """Only the canonical account namespace supplies pursuit identities."""
    custom_dir = tmp_path / "custom" / "pursuits"
    custom_dir.mkdir(parents=True)
    opp_id = "006Pe000012n2GkIAI"
    (custom_dir / "deal.md").write_text("---\nprivate-invalid: [unclosed\n---\n", encoding="utf-8")
    _write_pursuit(tmp_path / "accounts" / "acme" / "pursuits" / "deal.md", sf_opportunity_id=opp_id)

    client = _make_client(keyword_result=None)
    # implementation note: sosl_search returns list[dict] directly
    client.sosl_search.return_value = [{"AccountId": "001ABC123456789ABC"}]

    with (
        patch(
            "fieldkit.commands.sf.account.get_accounts_config",
            return_value={"accounts": {"acme": {"pursuit_dir": "custom/pursuits"}}},
        ),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
    ):
        result = _resolve_sf_account_id("acme", client, "https://sf.com")

    assert result == "001ABC123456789ABC"
    client.sosl_search.assert_called_once_with(f"FIND {{{opp_id}}} IN ALL FIELDS RETURNING Opportunity(AccountId)")


# ---------------------------------------------------------------------------
# Additional branch coverage: yaml parse error, OSError on read
# ---------------------------------------------------------------------------


# ── TestResolveEdgeCases (flattened) ────────────────────────────────────────


def test_resolve_edge_cases_yaml_parse_error_propagates(tmp_path: Path) -> None:
    """An incomplete local scan cannot become a completed no-match."""
    pursuit_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    bad_file = pursuit_dir / "bad.md"
    # Invalid YAML that triggers parse error
    bad_file.write_text("---\nkey: [unclosed\n---\n\n# Deal\n", encoding="utf-8")

    client = _make_client(keyword_result=None)

    with (
        patch(
            "fieldkit.commands.sf.account.get_accounts_config",
            return_value={"accounts": {"acme": {}}},
        ),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
        pytest.raises(FieldkitError, match=r"^Invalid pursuit frontmatter$"),
    ):
        _resolve_sf_account_id("acme", client, "https://sf.com")


def test_resolve_edge_cases_no_frontmatter_rejected(tmp_path: Path) -> None:
    """Missing frontmatter is invalid metadata, not a complete no-match."""
    pursuit_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    (pursuit_dir / "nofront.md").write_text("# No frontmatter\n", encoding="utf-8")

    client = _make_client(keyword_result=None)

    with (
        patch(
            "fieldkit.commands.sf.account.get_accounts_config",
            return_value={"accounts": {"acme": {}}},
        ),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
        pytest.raises(FieldkitError, match="Salesforce pursuit has no frontmatter block"),
    ):
        _resolve_sf_account_id("acme", client, "https://sf.com")

    client.sosl_search.assert_not_called()
    client.fetch_record.assert_not_called()


def test_resolve_edge_cases_opp_id_none_silently_skipped(tmp_path: Path) -> None:
    """A pursuit file with sf_opportunity_id: null is silently skipped (no warning)."""
    pursuit_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    (pursuit_dir / "deal.md").write_text(
        "---\nstage: discover\nsf_opportunity_id: null\n---\n\n# Deal\n",
        encoding="utf-8",
    )
    client = _make_client(keyword_result=None)

    with (
        patch(
            "fieldkit.commands.sf.account.get_accounts_config",
            return_value={"accounts": {"acme": {}}},
        ),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
    ):
        result = _resolve_sf_account_id("acme", client, "https://sf.com")

    assert result is None
    client.fetch_record.assert_not_called()


def test_resolve_edge_cases_auth_error_on_sosl_search_propagates(tmp_path: Path) -> None:
    """implementation note: SFAuthError from sosl_search propagates (not caught in batch loop)."""
    from fieldkit.sf.errors import SFAuthError

    opp_id = "006Pe000012n2GkIAI"
    pursuit = tmp_path / "accounts" / "acme" / "pursuits" / "deal.md"
    _write_pursuit(pursuit, sf_opportunity_id=opp_id)

    client = _make_client(keyword_result=None)
    client.sosl_search.side_effect = SFAuthError("session expired")

    with (
        patch(
            "fieldkit.commands.sf.account.get_accounts_config",
            return_value={"accounts": {"acme": {}}},
        ),
        patch("fieldkit.commands.sf.account.get_fieldkit_home", return_value=tmp_path),
        pytest.raises(SFAuthError, match="session expired"),
    ):
        _resolve_sf_account_id("acme", client, "https://sf.com")
