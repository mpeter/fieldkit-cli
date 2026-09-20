"""Tests for the quota module's account-term and territory-resolution helpers.

`_account_search_term` and `_load_account_names` sat at 0% line coverage and
`_resolve_missing_territory_ids` at 42.9%, while `commands/pipeline/quota.py`
around them read higher. gaze-py <=0.8.2 applied that file aggregate to every
function in it, so none was flagged; gaze-py 0.9.0 attributes coverage per
function and surfaced them carrying 130.7 CRAP.

`_resolve_missing_territory_ids` imports its collaborators inside the function
body, so they are patched at their source modules — that is the binding the
deferred import resolves against.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.commands.pipeline.quota import (
    _account_search_term,
    _load_account_names,
    _resolve_missing_territory_ids,
)

pytestmark = pytest.mark.unit

_ACCOUNTS_CONFIG = "fieldkit.config.get_accounts_config"
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
def test_account_search_term_falls_back_when_the_keyword_is_unusable(info: dict) -> None:
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


def test_load_account_names_returns_empty_on_malformed_yaml(tmp_path: Path) -> None:
    """A YAML syntax error degrades to no terms rather than propagating."""
    _write_accounts(tmp_path, "accounts: [unclosed\n")
    result = _load_account_names(tmp_path)
    assert result == []


@pytest.mark.parametrize(
    "body",
    [
        "just a string\n",
        "- a\n- b\n",
        "accounts: null\n",
        "accounts: a-string\n",
        "accounts: []\n",
        "accounts: {}\n",
        "accounts:\n  globex: null\n",
        "accounts:\n  globex: a-string\n",
    ],
    ids=[
        "scalar-doc",
        "list-doc",
        "null-accounts",
        "scalar-accounts",
        "list-accounts",
        "empty-accounts",
        "null-account",
        "scalar-account",
    ],
)
def test_load_account_names_returns_empty_for_unusable_shapes(tmp_path: Path, body: str) -> None:
    """Anything that is not a mapping of account mappings yields no terms."""
    _write_accounts(tmp_path, body)
    result = _load_account_names(tmp_path)
    assert result == []


# ---------------------------------------------------------------------------
# _resolve_missing_territory_ids — lazy, and silent when there is nothing to do
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "accounts",
    [
        {},
        {"globex": {"sf_territory": "T1", "sf_territory_id": "0MI000000000001AAA"}},
        {"globex": {"keywords": ["Globex"]}},
        {"globex": None},
        {"globex": "a-string"},
    ],
    ids=["no-accounts", "already-resolved", "no-territory", "null-account", "scalar-account"],
)
def test_resolve_missing_territory_ids_opens_no_connection_when_nothing_needs_resolving(
    accounts: dict,
) -> None:
    """The early return must happen before any Salesforce connection is opened.

    This runs on every `--source sf` invocation, so a client opened here would
    be a live round trip on every run for a fully-resolved workspace.
    """
    with (
        patch(_ACCOUNTS_CONFIG, return_value={"accounts": accounts}),
        patch(_SF_CLIENT) as mock_client,
        patch(_RESOLVE_IDS) as mock_resolve,
        patch(_SET_TERRITORY_ID) as mock_set,
    ):
        result = _resolve_missing_territory_ids("sid-123", "https://example.my.salesforce.com")

    assert result is None
    mock_client.assert_not_called()
    mock_resolve.assert_not_called()
    mock_set.assert_not_called()


def test_resolve_missing_territory_ids_persists_each_resolved_id(capsys: pytest.CaptureFixture[str]) -> None:
    """Resolved IDs are written back to accounts.yaml and reported on stderr."""
    accounts = {
        "globex": {"sf_territory": "FSI_SOUTH_TERR03", "keywords": ["Globex Corp"]},
        "acme-corp": {"sf_territory": "MFG_WEST_TERR01"},
    }
    with (
        patch(_ACCOUNTS_CONFIG, return_value={"accounts": accounts}),
        patch(_SF_CLIENT),
        patch(_RESOLVE_IDS, return_value={"globex": "0MI000000000001AAA", "acme-corp": "0MI000000000002AAA"}),
        patch(_SET_TERRITORY_ID) as mock_set,
    ):
        _resolve_missing_territory_ids("sid-123", "https://example.my.salesforce.com")

    persisted = {call.args for call in mock_set.call_args_list}
    assert persisted == {("globex", "0MI000000000001AAA"), ("acme-corp", "0MI000000000002AAA")}

    captured = capsys.readouterr()
    assert "0MI000000000001AAA" in captured.err
    assert captured.out == "", "diagnostics must go to stderr so --json stdout stays machine-readable"


def test_resolve_missing_territory_ids_passes_the_search_term_and_developer_name() -> None:
    """Each account is resolved by (search term, DeveloperName), not by slug."""
    accounts = {"globex-corp": {"sf_territory": "FSI_SOUTH_TERR03", "keywords": ["Globex Corp"]}}
    with (
        patch(_ACCOUNTS_CONFIG, return_value={"accounts": accounts}),
        patch(_SF_CLIENT),
        patch(_RESOLVE_IDS, return_value={}) as mock_resolve,
        patch(_SET_TERRITORY_ID),
    ):
        _resolve_missing_territory_ids("sid-123", "https://example.my.salesforce.com")

    requested = mock_resolve.call_args.args[1]
    request = requested["globex-corp"]
    assert request.search_term == "Globex Corp"
    assert request.expected_developer_name == "FSI_SOUTH_TERR03"
    assert request.gsg_id is None


def test_resolve_missing_territory_ids_treats_blank_gsg_identity_as_absent() -> None:
    """Whitespace-only account identity must preserve the unscoped compatibility query."""
    accounts = {
        "globex-corp": {
            "sf_territory": "FSI_SOUTH_TERR03",
            "sf_gsg_id": "   ",
        }
    }
    with (
        patch(_ACCOUNTS_CONFIG, return_value={"accounts": accounts}),
        patch(_SF_CLIENT),
        patch(_RESOLVE_IDS, return_value={}) as mock_resolve,
        patch(_SET_TERRITORY_ID),
    ):
        _resolve_missing_territory_ids("sid-123", "https://example.my.salesforce.com")

    requested = mock_resolve.call_args.args[1]
    assert requested["globex-corp"].gsg_id is None


def test_resolve_missing_territory_ids_warns_about_unresolved_accounts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An account SF could not match is warned about rather than silently skipped."""
    accounts = {
        "globex": {"sf_territory": "FSI_SOUTH_TERR03"},
        "ghost-account": {"sf_territory": "NO_SUCH_TERR"},
    }
    with (
        patch(_ACCOUNTS_CONFIG, return_value={"accounts": accounts}),
        patch(_SF_CLIENT),
        patch(_RESOLVE_IDS, return_value={"globex": "0MI000000000001AAA"}),
        patch(_SET_TERRITORY_ID),
        caplog.at_level("WARNING"),
    ):
        _resolve_missing_territory_ids("sid-123", "https://example.my.salesforce.com")

    assert "ghost-account" in caplog.text
    assert "NO_SUCH_TERR" in caplog.text
    assert "globex" not in caplog.text.replace("ghost-account", ""), "a resolved account must not be warned about"
