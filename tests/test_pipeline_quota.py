"""implementation note — territory-scoped closed-won for `pipeline quota --source sf`.

Covers the config accessors (sf_territory_id read/write-back), the SF client
territory-resolution and closed-won query methods, the quota-domain
`fetch_sf_closed_won` orchestration, and the two-mode CLI output.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

import fieldkit.config._loader as config_mod
import fieldkit.config._paths as config_paths
import fieldkit.sf.territory as territory
from fieldkit.commands.pipeline.cli import cmd_quota
from fieldkit.commands.pipeline.quota import _resolve_missing_territory_ids, fetch_sf_closed_won
from fieldkit.config import ConfigError, get_sf_territory_ids_from_accounts, set_sf_territory_id_for_account
from fieldkit.sf.client import SFAPIError, SFAuthError, SFDirectClient
from fieldkit.sf.territory import TerritoryResolutionRequest

pytestmark = pytest.mark.unit


def _write_accounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, accounts: dict[str, object]) -> Path:
    """Point get_fieldkit_home at tmp_path and write config/accounts.yaml."""
    config_mod.clear_config_caches()
    monkeypatch.setattr(config_paths, "get_fieldkit_home", lambda: tmp_path)
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    path = cfg_dir / "accounts.yaml"
    path.write_text(yaml.safe_dump({"accounts": accounts}), encoding="utf-8")
    config_mod.clear_config_caches()
    return path


def _client() -> SFDirectClient:
    return SFDirectClient(session_id="sid-test", base_url="https://examplecrm.my.salesforce.com")


def _resolution_request(gsg_id: str | None = None) -> TerritoryResolutionRequest:
    return TerritoryResolutionRequest("Globex Corp", "FIXTURE_TERR_01", gsg_id)


# ---------------------------------------------------------------------------
# T01 — config accessors
# ---------------------------------------------------------------------------


def test_get_sf_territory_ids_from_accounts_returns_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_accounts(
        tmp_path,
        monkeypatch,
        {
            "globex-bank": {"sf_territory": "FIXTURE_TERR_01", "sf_territory_id": "0MI000000000001"},
            "initech-ins": {"sf_territory": "FIXTURE_TERR_02", "sf_territory_id": "0MI000000000002"},
            "dupe": {"sf_territory": "FIXTURE_TERR_01", "sf_territory_id": "0MI000000000001"},
        },
    )
    ids = get_sf_territory_ids_from_accounts()
    assert sorted(ids) == ["0MI000000000001", "0MI000000000002"]


def test_get_sf_territory_ids_from_accounts_returns_empty_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_accounts(tmp_path, monkeypatch, {"globex-bank": {"sf_territory": "FIXTURE_TERR_01"}})
    ids = get_sf_territory_ids_from_accounts()
    assert ids == []


def test_set_sf_territory_id_for_account_writes_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_accounts(tmp_path, monkeypatch, {"globex-bank": {"sf_territory": "FIXTURE_TERR_01"}})
    set_sf_territory_id_for_account("globex-bank", "0MI000000000009")
    written = yaml.safe_load(path.read_text(encoding="utf-8"))["accounts"]["globex-bank"]
    assert written["sf_territory_id"] == "0MI000000000009"
    assert written["sf_territory"] == "FIXTURE_TERR_01"  # existing field preserved


def test_set_sf_territory_id_for_account_noops_for_unknown_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_accounts(tmp_path, monkeypatch, {"globex-bank": {"sf_territory": "FIXTURE_TERR_01"}})
    set_sf_territory_id_for_account("does-not-exist", "0MI000000000009")
    accounts = yaml.safe_load(path.read_text(encoding="utf-8"))["accounts"]
    assert "does-not-exist" not in accounts
    assert "sf_territory_id" not in accounts["globex-bank"]


# ---------------------------------------------------------------------------
# T02 — resolve_territory_ids
# ---------------------------------------------------------------------------


def test_resolve_territory_ids_matches_developer_name() -> None:
    client = _client()
    with (
        patch.object(client, "sosl_search", return_value=[{"Id": "006xxx", "Territory2Id": "0MI000000000001"}]),
        patch.object(territory, "_fetch_territory_developer_name", return_value="FIXTURE_TERR_01"),
    ):
        resolved = territory.resolve_territory_ids(client, {"globex-bank": _resolution_request()})
    assert resolved == {"globex-bank": "0MI000000000001"}


def test_resolve_territory_ids_skips_unmatched_developer_name() -> None:
    client = _client()
    with (
        patch.object(client, "sosl_search", return_value=[{"Id": "006xxx", "Territory2Id": "0MI000000000001"}]),
        patch.object(territory, "_fetch_territory_developer_name", return_value="WRONG_TERRITORY"),
    ):
        resolved = territory.resolve_territory_ids(client, {"globex-bank": _resolution_request()})
    assert resolved == {}


def test_resolve_territory_ids_multi_candidate_second_match() -> None:
    """First Territory2Id misses the expected DeveloperName; second matches."""
    client = _client()
    with (
        patch.object(
            client,
            "sosl_search",
            return_value=[
                {"Id": "006aaa", "Territory2Id": "0MI000000000001"},
                {"Id": "006bbb", "Territory2Id": "0MI000000000002"},
            ],
        ),
        patch.object(
            territory,
            "_fetch_territory_developer_name",
            side_effect=["WRONG_TERRITORY", "FIXTURE_TERR_01"],
        ),
    ):
        resolved = territory.resolve_territory_ids(client, {"globex-bank": _resolution_request()})
    assert resolved == {"globex-bank": "0MI000000000002"}


def test_resolve_territory_ids_propagates_sf_auth_error() -> None:
    """SFAuthError from sosl_search must not be swallowed (CLAUDE.md: auth exceptions propagate)."""
    client = _client()
    with (
        patch.object(client, "sosl_search", side_effect=SFAuthError("session expired")),
        pytest.raises(SFAuthError),
    ):
        territory.resolve_territory_ids(client, {"globex-bank": _resolution_request()})


def test_resolve_territory_ids_continues_after_one_account_api_failure() -> None:
    client = _client()
    requests = {
        "unavailable": _resolution_request(),
        "globex-bank": _resolution_request(),
    }
    with (
        patch.object(
            client,
            "sosl_search",
            side_effect=[SFAPIError("temporary failure"), [{"Territory2Id": "0MI000000000001"}]],
        ),
        patch.object(territory, "_fetch_territory_developer_name", return_value="FIXTURE_TERR_01"),
    ):
        resolved = territory.resolve_territory_ids(client, requests)

    assert resolved == {"globex-bank": "0MI000000000001"}


def test_resolve_territory_ids_scopes_candidates_by_valid_gsg() -> None:
    client = _client()
    with (
        patch.object(client, "sosl_search", return_value=[]) as mock_search,
        patch.object(territory, "_fetch_territory_developer_name"),
    ):
        resolved = territory.resolve_territory_ids(client, {"globex-bank": _resolution_request("GSG0012345")})

    assert resolved == {}
    sosl = mock_search.call_args.args[0]
    assert 'FIND {"Globex Corp"} IN NAME FIELDS' in sosl
    assert "WHERE Account.GU_Proxy_ID__c = 'GSG0012345'" in sosl


def test_resolve_territory_ids_scoped_query_returns_matching_territory() -> None:
    client = _client()
    with (
        patch.object(client, "sosl_search", return_value=[{"Territory2Id": "0MI000000000001"}]) as mock_search,
        patch.object(territory, "_fetch_territory_developer_name", return_value="FIXTURE_TERR_01"),
    ):
        resolved = territory.resolve_territory_ids(
            client,
            {"globex-bank": _resolution_request("GSG0012345")},
        )

    assert resolved == {"globex-bank": "0MI000000000001"}
    assert "WHERE Account.GU_Proxy_ID__c = 'GSG0012345'" in mock_search.call_args.args[0]


def test_resolve_territory_ids_missing_gsg_uses_compatibility_query() -> None:
    client = _client()
    with patch.object(client, "sosl_search", return_value=[]) as mock_search:
        resolved = territory.resolve_territory_ids(client, {"globex-bank": _resolution_request()})

    assert resolved == {}
    assert "GU_Proxy_ID__c" not in mock_search.call_args.args[0]


def test_resolve_territory_ids_malformed_gsg_is_not_interpolated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _client()
    malformed = "GSG001' OR Name != '"
    with patch.object(client, "sosl_search", return_value=[]) as mock_search:
        resolved = territory.resolve_territory_ids(client, {"globex-bank": _resolution_request(malformed)})

    assert resolved == {}
    assert malformed not in mock_search.call_args.args[0]
    assert "GU_Proxy_ID__c" not in mock_search.call_args.args[0]
    assert any("ignoring malformed sf_gsg_id" in record.message for record in caplog.records)


def test_quota_resolution_passes_configured_gsg_identity() -> None:
    accounts = {
        "globex-bank": {
            "keywords": ["Globex Corp"],
            "sf_territory": "FIXTURE_TERR_01",
            "sf_gsg_id": " GSG0012345 ",
        }
    }
    with (
        patch("fieldkit.config.get_accounts_config", return_value={"accounts": accounts}),
        patch("fieldkit.sf.territory.resolve_territory_ids", return_value={}) as mock_resolve,
    ):
        _resolve_missing_territory_ids("sid-test", "https://examplecrm.my.salesforce.com")

    requests = mock_resolve.call_args.args[1]
    assert requests == {"globex-bank": TerritoryResolutionRequest("Globex Corp", "FIXTURE_TERR_01", "GSG0012345")}


# ---------------------------------------------------------------------------
# T03 — fetch_closed_won_by_territory
# ---------------------------------------------------------------------------


def test_fetch_closed_won_by_territory_sums_and_dedupes() -> None:
    client = _client()
    # Duplicate Opportunity Ids in a single response → counted once.
    same_opp = [
        {"Id": "006dup", "Consulting_Total_USD__c": 100_000.0},
        {"Id": "006dup", "Consulting_Total_USD__c": 100_000.0},
    ]
    with patch.object(client, "sosl_search", return_value=same_opp):
        total = territory.fetch_closed_won_by_territory(client, ["Acme", "Beta"], ["0MI000000000001"])
    assert total == pytest.approx(100_000.0)


def test_fetch_closed_won_by_territory_issues_single_sosl_call() -> None:
    client = _client()
    with patch.object(client, "sosl_search", return_value=[]) as mock_search:
        total = territory.fetch_closed_won_by_territory(
            client,
            ["Acme", "Beta Corp", "AT&T"],
            ["0MI000000000001"],
        )
    assert total == pytest.approx(0.0)
    assert mock_search.call_count == 1
    call_sosl: str = mock_search.call_args.args[0]
    assert 'FIND {Acme OR "Beta Corp" OR "AT&T"} IN NAME FIELDS' in call_sosl


def test_reject_capped_closed_won_records() -> None:
    capped_results = [{"Id": f"006{i:012d}", "Consulting_Total_USD__c": 100.0} for i in range(2000)]
    with pytest.raises(SFAPIError, match="2000-record limit"):
        territory._reject_capped_closed_won(capped_results)


def test_fetch_closed_won_by_territory_rejects_capped_results() -> None:
    """The public function keeps the capped-response guard connected."""
    client = _client()
    capped_results = [{"Id": f"006{i:012d}", "Consulting_Total_USD__c": 100.0} for i in range(2000)]
    with (
        patch.object(client, "sosl_search", return_value=capped_results),
        pytest.raises(SFAPIError, match="2000-record limit"),
    ):
        territory.fetch_closed_won_by_territory(client, ["Acme"], ["0MI000000000001"])

    with patch.object(client, "sosl_search", return_value=[]):
        total = territory.fetch_closed_won_by_territory(client, ["Acme"], ["0MI000000000001"])
    assert total == pytest.approx(0.0)


def test_fetch_closed_won_by_territory_returns_zero_on_empty() -> None:
    client = _client()
    with patch.object(client, "sosl_search", return_value=[]):
        total = territory.fetch_closed_won_by_territory(client, ["Acme"], ["0MI000000000001"])
    assert total == pytest.approx(0.0)


def test_fetch_closed_won_by_territory_returns_zero_without_territory_ids() -> None:
    client = _client()
    # No territory filter → must not query SF at all (would be unscoped/contaminated).
    with patch.object(client, "sosl_search", side_effect=AssertionError("must not query")) as m:
        total = territory.fetch_closed_won_by_territory(client, ["Acme"], [])
    assert total == pytest.approx(0.0)
    assert m.call_count == 0


def test_fetch_closed_won_by_territory_skips_malformed_territory_ids() -> None:
    client = _client()
    # A hand-edited accounts.yaml typo must not reach the SOSL WHERE clause.
    with patch.object(client, "sosl_search", side_effect=AssertionError("must not query")) as m:
        total = territory.fetch_closed_won_by_territory(client, ["Acme"], ["not-an-id", "x' OR '1'='1"])
    assert total == pytest.approx(0.0)
    assert m.call_count == 0


# ---------------------------------------------------------------------------
# T05 — fetch_sf_closed_won orchestration
# ---------------------------------------------------------------------------


def test_fetch_sf_closed_won_raises_config_error_when_no_territory_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with (
        patch("fieldkit.config.get_sf_session_id", return_value="sid"),
        patch("fieldkit.config.get_sf_rest_base_url", return_value="https://x.my.salesforce.com"),
        patch("fieldkit.config.get_accounts_config", return_value={"accounts": {}}),
        patch("fieldkit.config.get_sf_territory_ids_from_accounts", return_value=[]),
        pytest.raises(ConfigError, match="sf_territory_id"),
    ):
        fetch_sf_closed_won(tmp_path)


def test_fetch_sf_closed_won_raises_auth_error_on_no_sid(tmp_path: Path) -> None:
    with (
        patch("fieldkit.config.get_sf_session_id", return_value=""),
        pytest.raises(SFAuthError, match="No Salesforce session"),
    ):
        fetch_sf_closed_won(tmp_path)


# ---------------------------------------------------------------------------
# T06 — CLI two-mode output
# ---------------------------------------------------------------------------

_VALID_QUOTA: dict[str, object] = {"target": 3_000_000, "period": "2026-H1"}
_ONE_CLOSED_WON = [{"stage": "closed-won", "sf_amount": 500_000, "name": "deal-a"}]


def test_quota_cli_source_pursuits_suppresses_gap_line(monkeypatch: pytest.MonkeyPatch) -> None:
    with (
        patch("fieldkit.commands.pipeline.cli.get_pipeline_quota", return_value=_VALID_QUOTA),
        patch("fieldkit.commands.pipeline.cli.get_fieldkit_home", return_value=Path("/tmp/x")),
        patch("fieldkit.commands.pipeline.quota._collect_pursuits_for_quota", return_value=_ONE_CLOSED_WON),
    ):
        result = CliRunner().invoke(cmd_quota, ["--source", "pursuits"])
    assert result.exit_code == 0
    assert "n/a" in result.output
    assert "not comparable" in result.output
    # sf-mode's closed-won line must not appear in pursuits mode
    assert "Closed-won (Salesforce" not in result.output


def test_quota_cli_source_sf_shows_territory_scoped_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    with (
        patch("fieldkit.commands.pipeline.cli.get_pipeline_quota", return_value=_VALID_QUOTA),
        patch("fieldkit.commands.pipeline.cli.get_fieldkit_home", return_value=Path("/tmp/x")),
        patch("fieldkit.commands.pipeline.quota._collect_pursuits_for_quota", return_value=_ONE_CLOSED_WON),
        patch("fieldkit.commands.pipeline.quota.fetch_sf_closed_won", return_value=5_606_339.0),
    ):
        result = CliRunner().invoke(cmd_quota, ["--source", "sf"])
    assert result.exit_code == 0
    assert "territory-scoped" in result.output
    assert "Gap" in result.output


def test_quota_cli_source_sf_config_error_exits_3(monkeypatch: pytest.MonkeyPatch) -> None:
    with (
        patch("fieldkit.commands.pipeline.cli.get_pipeline_quota", return_value=_VALID_QUOTA),
        patch("fieldkit.commands.pipeline.cli.get_fieldkit_home", return_value=Path("/tmp/x")),
        patch("fieldkit.commands.pipeline.quota._collect_pursuits_for_quota", return_value=_ONE_CLOSED_WON),
        patch(
            "fieldkit.commands.pipeline.quota.fetch_sf_closed_won",
            side_effect=ConfigError("No sf_territory_id values found in accounts.yaml."),
        ),
    ):
        result = CliRunner().invoke(cmd_quota, ["--source", "sf"])
    assert result.exit_code == 3
    assert "sf_territory_id" in result.output
