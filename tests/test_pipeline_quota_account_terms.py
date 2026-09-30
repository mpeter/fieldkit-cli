"""Quota account terms and resolution retain selected-workspace authority.

Patch the account accessor at its imported module binding; Salesforce and setter
collaborators remain deferred imports patched at their source modules.
"""

from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from fieldkit.config import ConfigError, set_sf_territory_id_for_account
from fieldkit.sf.quota import (
    _account_search_term,
    _load_account_names,
    _quota_accounts,
    _resolve_missing_territory_ids,
)

pytestmark = pytest.mark.unit

_SET_TERRITORY_ID = "fieldkit.config.set_sf_territory_id_for_account"
_SF_CLIENT = "fieldkit.sf.client.SFDirectClient"
_RESOLVE_IDS = "fieldkit.sf.territory.resolve_territory_ids"


def _write_accounts(data_root: Path, body: str) -> None:
    cfg = data_root / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "accounts.yaml").write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# _account_search_term — first keyword wins, else a humanized slug
# ---------------------------------------------------------------------------


def test_account_search_term_prefers_the_first_keyword() -> None:
    """The first configured keyword is the most specific searchable name."""
    result = _account_search_term("globex", {"keywords": ["Globex Corp", "Globex"]})
    assert result == "Globex Corp"


@pytest.mark.parametrize(
    ("slug", "expected"),
    [
        ("globex", "Globex"),
        ("globex-corp", "Globex Corp"),
        ("globex_corp", "Globex Corp"),
        ("acme-corp-holdings", "Acme Corp Holdings"),
    ],
)
def test_account_search_term_humanizes_the_slug_when_no_keyword(slug: str, expected: str) -> None:
    """Both separators are normalised to spaces and the result is title-cased."""
    result = _account_search_term(slug, {})
    assert result == expected


@pytest.mark.parametrize(
    "info",
    [
        {},
        {"keywords": []},
        {"keywords": None},
        {"keywords": "Globex Corp"},
        {"keywords": [""]},
        {"keywords": ["   "]},
        {"keywords": [123]},
        {"keywords": [None]},
    ],
    ids=[
        "no-key",
        "empty-list",
        "null",
        "string-not-list",
        "blank-first",
        "whitespace-first",
        "int-first",
        "null-first",
    ],
)
def test_account_search_term_falls_back_when_the_keyword_is_unusable(info: dict[str, object]) -> None:
    """An unusable first keyword falls back to the slug rather than searching for a blank.

    A whitespace-only or non-string keyword reaching SOSL would search for
    nothing useful; the slug is always a better term than an empty one.
    """
    result = _account_search_term("globex-corp", info)
    assert result == "Globex Corp"


# ---------------------------------------------------------------------------
# _load_account_names — only accounts in the operator's patch
# ---------------------------------------------------------------------------


def test_load_account_names_includes_only_accounts_with_a_territory(tmp_path: Path) -> None:
    """Accounts without sf_territory are outside the patch and must be skipped."""
    _write_accounts(
        tmp_path,
        """
accounts:
  globex:
    sf_territory: FSI_SOUTH_TERR03
    keywords: [Globex Corp]
  acme-corp:
    sf_territory: MFG_WEST_TERR01
  not-my-patch:
    keywords: [Someone Else]
""",
    )
    result = _load_account_names(tmp_path)
    assert result == ["Globex Corp", "Acme Corp"]


def test_load_account_names_returns_empty_when_the_file_is_missing(tmp_path: Path) -> None:
    """A fresh workspace with no accounts.yaml yields no search terms rather than raising."""
    result = _load_account_names(tmp_path)
    assert result == []


def test_load_account_names_rejects_malformed_yaml(tmp_path: Path) -> None:
    """Invalid configuration must stop territory search rather than erase terms."""
    _write_accounts(tmp_path, "accounts: [unclosed\n")
    with pytest.raises(ConfigError, match=r"accounts\.yaml"):
        _load_account_names(tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        "just a string\n",
        "- a\n- b\n",
        "accounts: null\n",
        "accounts: a-string\n",
        "accounts: []\n",
        "accounts:\n  globex: null\n",
        "accounts:\n  globex: a-string\n",
    ],
    ids=[
        "scalar-doc",
        "list-doc",
        "null-accounts",
        "scalar-accounts",
        "list-accounts",
        "null-account",
        "scalar-account",
    ],
)
def test_load_account_names_rejects_unusable_shapes(tmp_path: Path, body: str) -> None:
    """Malformed account shapes cannot become an empty search scope."""
    _write_accounts(tmp_path, body)
    with pytest.raises(ConfigError, match=r"accounts\.yaml"):
        _load_account_names(tmp_path)


def test_load_account_names_allows_empty_mapping(tmp_path: Path) -> None:
    _write_accounts(tmp_path, "accounts: {}\n")
    result = _load_account_names(tmp_path)
    assert result == []


def test_load_account_names_rejects_duplicate_keys(tmp_path: Path) -> None:
    _write_accounts(tmp_path, "accounts: {acme: {sf_territory: T1}}\naccounts: {}\n")
    with pytest.raises(ConfigError, match=r"accounts\.yaml"):
        _load_account_names(tmp_path)


# ---------------------------------------------------------------------------
# _resolve_missing_territory_ids — lazy, and silent when there is nothing to do
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "accounts",
    [
        {},
        {"globex": {"sf_territory": "T1", "sf_territory_id": "0MI000000000001AAA"}},
        {"globex": {"keywords": ["Globex"]}},
    ],
    ids=["no-accounts", "already-resolved", "no-territory"],
)
def test_resolve_missing_territory_ids_opens_no_connection_when_nothing_needs_resolving(
    accounts: dict[str, dict[str, object]],
    tmp_path: Path,
) -> None:
    """The early return must happen before any Salesforce connection is opened.

    This runs on every `--source sf` invocation, so a client opened here would
    be a live round trip on every run for a fully-resolved workspace.
    """
    _write_accounts(tmp_path, yaml.safe_dump({"accounts": accounts}))
    with (
        patch(_SF_CLIENT) as mock_client,
        patch(_RESOLVE_IDS) as mock_resolve,
        patch(_SET_TERRITORY_ID, wraps=set_sf_territory_id_for_account) as mock_set,
    ):
        _resolve_missing_territory_ids(
            "sid-123",
            "https://example.my.salesforce.com",
            data_root=tmp_path,
            accounts_snapshot=deepcopy(_quota_accounts(tmp_path)),
        )

    mock_client.assert_not_called()
    mock_resolve.assert_not_called()
    mock_set.assert_not_called()


def test_resolve_missing_territory_ids_persists_each_resolved_id(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Resolved IDs are written back without leaking them to CLI output."""
    accounts: dict[str, dict[str, object]] = {
        "globex": {"sf_territory": "FSI_SOUTH_TERR03", "keywords": ["Globex Corp"]},
        "acme-corp": {"sf_territory": "MFG_WEST_TERR01"},
    }
    _write_accounts(tmp_path, yaml.safe_dump({"accounts": accounts}))
    with (
        patch(_SF_CLIENT),
        patch(_RESOLVE_IDS, return_value={"globex": "0MI000000000001AAA", "acme-corp": "0MI000000000002AAA"}),
        patch(_SET_TERRITORY_ID, wraps=set_sf_territory_id_for_account) as mock_set,
    ):
        result = _resolve_missing_territory_ids(
            "sid-123",
            "https://example.my.salesforce.com",
            data_root=tmp_path,
            accounts_snapshot=deepcopy(_quota_accounts(tmp_path)),
        )

    persisted = {call.args for call in mock_set.call_args_list}
    assert persisted == {("globex", "0MI000000000001AAA"), ("acme-corp", "0MI000000000002AAA")}
    assert result["globex"]["sf_territory_id"] == "0MI000000000001AAA"
    assert result["acme-corp"]["sf_territory_id"] == "0MI000000000002AAA"
    for call in mock_set.call_args_list:
        assert call.kwargs["workspace_root"] == tmp_path
        assert call.kwargs["expected_account"] == accounts[call.args[0]]
        assert call.kwargs["expected_account"] is not accounts[call.args[0]]
    globex_snapshot = mock_set.call_args_list[0].kwargs["expected_account"]
    assert globex_snapshot["keywords"] is not accounts["globex"]["keywords"]

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def test_resolve_missing_territory_ids_passes_the_search_term_and_developer_name(tmp_path: Path) -> None:
    """Each account is resolved by (search term, DeveloperName), not by slug."""
    accounts = {"globex-corp": {"sf_territory": "FSI_SOUTH_TERR03", "keywords": ["Globex Corp"]}}
    _write_accounts(tmp_path, yaml.safe_dump({"accounts": accounts}))
    with (
        patch(_SF_CLIENT),
        patch(_RESOLVE_IDS, return_value={}) as mock_resolve,
        patch(_SET_TERRITORY_ID, wraps=set_sf_territory_id_for_account),
    ):
        _resolve_missing_territory_ids(
            "sid-123",
            "https://example.my.salesforce.com",
            data_root=tmp_path,
            accounts_snapshot=deepcopy(_quota_accounts(tmp_path)),
        )

    requested = mock_resolve.call_args.args[1]
    request = requested["globex-corp"]
    assert request.search_term == "Globex Corp"
    assert request.expected_developer_name == "FSI_SOUTH_TERR03"
    assert request.gsg_id is None


def test_resolve_missing_territory_ids_treats_blank_gsg_identity_as_absent(tmp_path: Path) -> None:
    """Whitespace-only account identity must preserve the unscoped compatibility query."""
    accounts = {
        "globex-corp": {
            "sf_territory": "FSI_SOUTH_TERR03",
            "sf_gsg_id": "   ",
        }
    }
    _write_accounts(tmp_path, yaml.safe_dump({"accounts": accounts}))
    with (
        patch(_SF_CLIENT),
        patch(_RESOLVE_IDS, return_value={}) as mock_resolve,
        patch(_SET_TERRITORY_ID, wraps=set_sf_territory_id_for_account),
    ):
        _resolve_missing_territory_ids(
            "sid-123",
            "https://example.my.salesforce.com",
            data_root=tmp_path,
            accounts_snapshot=deepcopy(_quota_accounts(tmp_path)),
        )

    requested = mock_resolve.call_args.args[1]
    assert requested["globex-corp"].gsg_id is None


def test_resolve_missing_territory_ids_warns_about_unresolved_accounts(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Unresolved accounts are counted without exposing account identities."""
    accounts = {
        "globex": {"sf_territory": "FSI_SOUTH_TERR03"},
        "ghost-account": {"sf_territory": "NO_SUCH_TERR"},
    }
    _write_accounts(tmp_path, yaml.safe_dump({"accounts": accounts}))
    with (
        patch(_SF_CLIENT),
        patch(_RESOLVE_IDS, return_value={"globex": "0MI000000000001AAA"}),
        patch(_SET_TERRITORY_ID, wraps=set_sf_territory_id_for_account),
        caplog.at_level("WARNING"),
    ):
        _resolve_missing_territory_ids(
            "sid-123",
            "https://example.my.salesforce.com",
            data_root=tmp_path,
            accounts_snapshot=deepcopy(_quota_accounts(tmp_path)),
        )

    assert "Could not resolve territory IDs for 1 configured account(s)" in caplog.text
    assert "ghost-account" not in caplog.text
    assert "NO_SUCH_TERR" not in caplog.text
    assert "globex" not in caplog.text
