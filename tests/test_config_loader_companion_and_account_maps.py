"""Tests for the four configuration readers the per-function CRAP fix exposed.

``get_companion_tier``, ``get_companion_act_allowlist`` and
``get_territory_account_map`` sat at 0% line coverage and
``build_domain_account_map`` at partial, while the file around them read far
higher. gaze-py <=0.8.2 applied that file aggregate to every function in it, so
none of the four was flagged. gaze-py 0.9.0 attributes coverage per function and
surfaced them carrying 113.8 CRAP between them.

All four are pure readers over a parsed config dict, so every test stubs the
loader beneath them (``_load_raw_config`` / ``_load_accounts_yaml``) rather than
writing YAML to disk. Note that monkeypatching ``CONFIG_PATH`` would not reach
xdist workers (L02); patching the loader function does.
"""

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.config import ConfigError, get_account_ids, get_gsg_id
from fieldkit.config._accounts import (
    _load_accounts_yaml,
    build_domain_account_map,
    get_territory_account_map,
)
from fieldkit.config._loader import clear_config_caches
from fieldkit.config._settings import get_companion_act_allowlist, get_companion_tier

pytestmark = pytest.mark.unit

_RAW_CONFIG = "fieldkit.config._loader._load_raw_config"
_ACCOUNTS_YAML = "fieldkit.config._accounts._load_accounts_yaml"
_CONFIG_PATH = "fieldkit.config._accounts.get_config_path"


@pytest.fixture(autouse=True)
def _isolate_config_caches() -> Iterator[None]:
    """Clear the loader caches around every test.

    The four functions under test are uncached, but the loaders beneath them are
    not; clearing on both sides keeps a stubbed value from leaking either into
    this module's tests or out of them.
    """
    clear_config_caches()
    yield
    clear_config_caches()


# ---------------------------------------------------------------------------
# _load_accounts_yaml — file-backed loader behavior
# ---------------------------------------------------------------------------


def test_load_accounts_yaml_reads_mapping_and_ignores_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "accounts.yaml"
    path.write_text("accounts:\n  acme: {}\n", encoding="utf-8")

    with patch(_CONFIG_PATH, return_value=path):
        loaded = _load_accounts_yaml()

    clear_config_caches()
    with patch(_CONFIG_PATH, return_value=tmp_path / "missing.yaml"):
        missing = _load_accounts_yaml()

    assert loaded == {"accounts": {"acme": {}}}
    assert missing == {}


@pytest.mark.parametrize(
    "contents",
    ["accounts: [\n", "- not-a-mapping\n"],
    ids=["invalid-yaml", "non-mapping"],
)
def test_load_accounts_yaml_rejects_malformed_or_non_mapping_input(tmp_path: Path, contents: str) -> None:
    path = tmp_path / "accounts.yaml"
    path.write_text(contents, encoding="utf-8")

    with patch(_CONFIG_PATH, return_value=path):
        result = _load_accounts_yaml()

    assert result == {}


def test_load_accounts_yaml_returns_empty_when_config_path_is_unavailable() -> None:
    with patch(_CONFIG_PATH, side_effect=ConfigError("missing fieldkit home")):
        result = _load_accounts_yaml()

    assert result == {}


# ---------------------------------------------------------------------------
# get_companion_tier — misconfiguration must fail closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("read", "read"),
        ("propose", "propose"),
        ("act", "act"),
        ("ACT", "act"),
        ("  Propose  ", "propose"),
        ("admin", "read"),
        ("", "read"),
        (None, "read"),
        (42, "read"),
    ],
)
def test_get_companion_tier_normalises_and_fails_closed(configured: object, expected: str) -> None:
    """Known tiers pass through case/whitespace-normalised; anything else degrades to read.

    The failure direction is the point: an unrecognised tier must never widen
    permissions, so every unknown value resolves to the most restrictive tier.
    """
    with patch(_RAW_CONFIG, return_value={"companion": {"tier": configured}}):
        result = get_companion_tier()
    assert result == expected


def test_get_companion_tier_defaults_to_read_when_key_absent() -> None:
    """A companion block with no tier key is the same as an unconfigured tier."""
    with patch(_RAW_CONFIG, return_value={"companion": {}}):
        result = get_companion_tier()
    assert result == "read"


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        {"companion": None},
        {"companion": "act"},
        {"companion": ["act"]},
    ],
    ids=["no-config", "empty-config", "null-block", "scalar-block", "list-block"],
)
def test_get_companion_tier_returns_read_for_unusable_config(data: object) -> None:
    """A missing or non-dict companion block cannot grant a tier."""
    with patch(_RAW_CONFIG, return_value=data):
        result = get_companion_tier()
    assert result == "read"


def test_get_companion_tier_returns_read_when_config_load_fails() -> None:
    """An unreadable config must not escalate the tier, and must not raise."""
    with patch(_RAW_CONFIG, side_effect=ConfigError("unreadable")):
        result = get_companion_tier()
    assert result == "read"


# ---------------------------------------------------------------------------
# get_companion_act_allowlist — empty by default
# ---------------------------------------------------------------------------


def test_get_companion_act_allowlist_strips_entries_and_drops_blanks() -> None:
    """Entries are whitespace-trimmed; blank and whitespace-only entries are dropped."""
    raw = ["pursuit advance --dry-run", "  brief  ", "", "   "]
    with patch(_RAW_CONFIG, return_value={"companion": {"act_allowlist": raw}}):
        result = get_companion_act_allowlist()
    assert result == ["pursuit advance --dry-run", "brief"]


def test_get_companion_act_allowlist_coerces_non_string_entries() -> None:
    """Non-string entries are coerced rather than dropped, so they cannot match an argv by accident."""
    with patch(_RAW_CONFIG, return_value={"companion": {"act_allowlist": [42, True]}}):
        result = get_companion_act_allowlist()
    assert result == ["42", "True"]


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        {"companion": None},
        {"companion": "pursuit advance"},
        {"companion": {}},
        {"companion": {"act_allowlist": None}},
        {"companion": {"act_allowlist": "pursuit advance"}},
        {"companion": {"act_allowlist": {"cmd": "pursuit advance"}}},
    ],
    ids=[
        "no-config",
        "empty-config",
        "null-block",
        "scalar-block",
        "no-allowlist-key",
        "null-allowlist",
        "string-allowlist",
        "dict-allowlist",
    ],
)
def test_get_companion_act_allowlist_returns_empty_for_unusable_config(data: object) -> None:
    """Anything that is not a list of entries yields no allowlist at all.

    A string is the dangerous case: iterating one would allowlist single
    characters, so it must be rejected as a whole rather than consumed.
    """
    with patch(_RAW_CONFIG, return_value=data):
        result = get_companion_act_allowlist()
    assert result == []


def test_get_companion_act_allowlist_returns_empty_when_config_load_fails() -> None:
    """An unreadable config grants nothing, and must not raise."""
    with patch(_RAW_CONFIG, side_effect=ConfigError("unreadable")):
        result = get_companion_act_allowlist()
    assert result == []


# ---------------------------------------------------------------------------
# get_territory_account_map — keyed by territory, valued by account slug
# ---------------------------------------------------------------------------


def test_get_territory_account_map_keys_by_territory_not_by_account() -> None:
    """The mapping direction is territory -> account slug, which is easy to build backwards."""
    accounts = {
        "global-pay": {"sf_territory": "FSI_SOUTH_TERR03"},
        "acme": {"sf_territory": "MFG_WEST_TERR01"},
    }
    with patch(_ACCOUNTS_YAML, return_value={"accounts": accounts}):
        result = get_territory_account_map()
    assert result == {"FSI_SOUTH_TERR03": "global-pay", "MFG_WEST_TERR01": "acme"}


def test_get_territory_account_map_preserves_territory_case() -> None:
    """Territory identifiers are Salesforce-side values and must not be normalised."""
    with patch(_ACCOUNTS_YAML, return_value={"accounts": {"acme": {"sf_territory": "Fsi_South"}}}):
        result = get_territory_account_map()
    assert result == {"Fsi_South": "acme"}


@pytest.mark.parametrize(
    "info",
    [None, "FSI_SOUTH_TERR03", {}, {"sf_territory": ""}, {"sf_territory": None}, {"sf_territory": 123}],
    ids=["null-account", "scalar-account", "no-territory", "empty-territory", "null-territory", "int-territory"],
)
def test_get_territory_account_map_skips_accounts_without_a_usable_territory(info: object) -> None:
    """An account missing a usable sf_territory is skipped, not mapped to a falsy key."""
    with patch(_ACCOUNTS_YAML, return_value={"accounts": {"acme": info}}):
        result = get_territory_account_map()
    assert result == {}


@pytest.mark.parametrize(
    "cfg",
    [{}, {"accounts": None}, {"accounts": []}, {"accounts": "global-pay"}, {"accounts": {}}],
    ids=["no-key", "null", "list", "scalar", "empty"],
)
def test_get_territory_account_map_returns_empty_for_unusable_accounts_block(cfg: dict) -> None:
    """A missing or non-dict accounts block yields an empty map rather than raising."""
    with patch(_ACCOUNTS_YAML, return_value=cfg):
        result = get_territory_account_map()
    assert result == {}


def test_account_identity_accessors_return_normalized_configured_values() -> None:
    accounts = {
        "global-pay": {
            "sf_gsg_id": "  GSG0012345  ",
            "sf_account_ids": ["001000000000001", " 001000000000002 ", "001000000000001", "", 42],
        }
    }
    with patch(_ACCOUNTS_YAML, return_value={"accounts": accounts}):
        gsg_id = get_gsg_id("global-pay")
        account_ids = get_account_ids("global-pay")

    assert gsg_id == "GSG0012345"
    assert account_ids == ["001000000000001", "001000000000002"]


@pytest.mark.parametrize(
    ("cfg", "slug"),
    [
        ({}, "global-pay"),
        ({"accounts": []}, "global-pay"),
        ({"accounts": {"global-pay": []}}, "global-pay"),
        ({"accounts": {"global-pay": {"sf_gsg_id": 42, "sf_account_ids": "001"}}}, "global-pay"),
        ({"accounts": {"other": {"sf_gsg_id": "GSG0012345", "sf_account_ids": ["001"]}}}, "global-pay"),
    ],
)
def test_account_identity_accessors_return_empty_for_missing_or_malformed_values(cfg: dict, slug: str) -> None:
    with patch(_ACCOUNTS_YAML, return_value=cfg):
        gsg_id = get_gsg_id(slug)
        account_ids = get_account_ids(slug)

    assert gsg_id is None
    assert account_ids == []


# ---------------------------------------------------------------------------
# build_domain_account_map — keyed by lowercased domain
# ---------------------------------------------------------------------------


def test_build_domain_account_map_keys_by_domain_and_lowercases_it() -> None:
    """Domains are lowercased so an address parsed from a header matches regardless of case.

    Contrast with get_territory_account_map, which preserves case: domains are
    compared against email addresses, territories against Salesforce values.
    """
    accounts = {"global-pay": {"domains": ["GlobalPay.com", "gp.example"]}}
    with patch(_ACCOUNTS_YAML, return_value={"accounts": accounts}):
        result = build_domain_account_map()
    assert result == {"globalpay.com": "global-pay", "gp.example": "global-pay"}


def test_build_domain_account_map_covers_every_account() -> None:
    """Domains from separate accounts land in one flat map."""
    accounts = {
        "global-pay": {"domains": ["globalpay.com"]},
        "acme": {"domains": ["acme-corp.com"]},
    }
    with patch(_ACCOUNTS_YAML, return_value={"accounts": accounts}):
        result = build_domain_account_map()
    assert result == {"globalpay.com": "global-pay", "acme-corp.com": "acme"}


@pytest.mark.parametrize(
    "accounts",
    [
        {
            "acme": {"domains": ["Shared.Example"]},
            "global-pay": {"domains": ["shared.example"]},
        },
        {
            "global-pay": {"domains": ["shared.example"]},
            "acme": {"domains": ["Shared.Example"]},
        },
    ],
)
def test_build_domain_account_map_omits_cross_account_conflict_in_any_order(
    accounts: dict[str, object], caplog: pytest.LogCaptureFixture
) -> None:
    with patch(_ACCOUNTS_YAML, return_value={"accounts": accounts}):
        result = build_domain_account_map()

    assert result == {}
    assert [record.message for record in caplog.records] == [
        "accounts.yaml: domain shared.example is claimed by accounts acme and global-pay; omitting ambiguous mapping"
    ]


def test_build_domain_account_map_same_account_duplicate_is_idempotent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    accounts = {"acme": {"domains": ["Shared.Example", "shared.example"]}}
    with patch(_ACCOUNTS_YAML, return_value={"accounts": accounts}):
        result = build_domain_account_map()

    assert result == {"shared.example": "acme"}
    assert caplog.records == []


def test_build_domain_account_map_conflict_cannot_be_reinserted_or_warn_twice(
    caplog: pytest.LogCaptureFixture,
) -> None:
    accounts = {
        "acme": {"domains": ["shared.example"]},
        "global-pay": {"domains": ["shared.example"]},
        "third-account": {"domains": ["shared.example"]},
    }
    with patch(_ACCOUNTS_YAML, return_value={"accounts": accounts}):
        result = build_domain_account_map()

    assert result == {}
    assert len(caplog.records) == 1


@pytest.mark.parametrize(
    "info",
    [None, "globalpay.com", {}, {"domains": None}, {"domains": "globalpay.com"}, {"domains": []}],
    ids=["null-account", "scalar-account", "no-domains", "null-domains", "string-domains", "empty-domains"],
)
def test_build_domain_account_map_skips_accounts_without_a_domain_list(info: object) -> None:
    """A non-list domains value is rejected whole; iterating a string would map single characters."""
    with patch(_ACCOUNTS_YAML, return_value={"accounts": {"acme": info}}):
        result = build_domain_account_map()
    assert result == {}


def test_build_domain_account_map_skips_unusable_entries_but_keeps_the_rest() -> None:
    """One bad domain entry must not discard its account's good ones."""
    accounts = {"acme": {"domains": ["acme-corp.com", "", None, 123, "ACME.example"]}}
    with patch(_ACCOUNTS_YAML, return_value={"accounts": accounts}):
        result = build_domain_account_map()
    assert result == {"acme-corp.com": "acme", "acme.example": "acme"}


@pytest.mark.parametrize(
    "cfg",
    [{}, {"accounts": None}, {"accounts": []}, {"accounts": "acme"}, {"accounts": {}}],
    ids=["no-key", "null", "list", "scalar", "empty"],
)
def test_build_domain_account_map_returns_empty_for_unusable_accounts_block(cfg: dict) -> None:
    """A missing or non-dict accounts block yields an empty map rather than raising."""
    with patch(_ACCOUNTS_YAML, return_value=cfg):
        result = build_domain_account_map()
    assert result == {}
