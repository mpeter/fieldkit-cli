"""implementation note — territory-scoped closed-won for `pipeline quota --source sf`.

Covers the config accessors (sf_territory_id read/write-back), the SF client
territory-resolution and closed-won query methods, the quota-domain
`fetch_sf_closed_won` orchestration, and the two-mode CLI output.
"""

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

import fieldkit.config._accounts as accounts_mod
import fieldkit.config._loader as config_mod
import fieldkit.config._paths as config_paths
import fieldkit.sf.territory as territory
from fieldkit.commands.pipeline.cli import cmd_quota
from fieldkit.config import ConfigError, get_sf_territory_ids_from_accounts, set_sf_territory_id_for_account
from fieldkit.sf.client import SFDirectClient
from fieldkit.sf.errors import SFAPIError, SFAuthError
from fieldkit.sf.quota import SFQuotaResult, _quota_accounts, _resolve_missing_territory_ids, fetch_sf_closed_won
from fieldkit.sf.territory import TerritoryResolutionRequest
from fieldkit.util.atomic import exclusive_path_lock

pytestmark = pytest.mark.unit


def test_documentation_local_quota_output_matches_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The published local example uses its real YAML and cannot claim live attainment."""
    document = Path("docs/guides/pipeline-workflow.md").read_text(encoding="utf-8")
    section = document.split("## Pipeline quota\n", 1)[1].split("## SF listview\n", 1)[0]
    blocks = re.findall(r"```[^\n]*\n(.*?)```", section, flags=re.DOTALL)
    assert len(blocks) == 3
    config_path = tmp_path / "config.yaml"
    config_path.write_text(blocks[1], encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", config_path)
    config_mod.clear_config_caches()
    try:
        with patch(
            "fieldkit.commands.pipeline.cli._fetch_sf_closed_won_or_exit",
            side_effect=AssertionError("local quota must not access Salesforce"),
        ) as fetch:
            result = CliRunner().invoke(cmd_quota, ["--data-root", str(tmp_path)])
            json_result = CliRunner().invoke(cmd_quota, ["--data-root", str(tmp_path), "--json"])

        assert result.exit_code == 0, result.output
        assert result.output.split("\n", 1)[1].rstrip("\n") == blocks[2].rstrip("\n")
        assert json_result.exit_code == 0, json_result.output
        payload = json.loads(json_result.output)
        assert payload["source"] == "pursuits"
        assert payload["gap"] is None
        fetch.assert_not_called()
    finally:
        config_mod.clear_config_caches()


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


def test_set_sf_territory_id_for_account_rejects_unknown_slug(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_accounts(tmp_path, monkeypatch, {"globex-bank": {"sf_territory": "FIXTURE_TERR_01"}})
    with pytest.raises(ConfigError, match=r"absent from accounts\.yaml"):
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


def test_quota_resolution_passes_configured_gsg_identity(tmp_path: Path) -> None:
    accounts = {
        "globex-bank": {
            "keywords": ["Globex Corp"],
            "sf_territory": "FIXTURE_TERR_01",
            "sf_gsg_id": " GSG0012345 ",
        }
    }
    (tmp_path / "config").mkdir()
    (tmp_path / "config/accounts.yaml").write_text(yaml.safe_dump({"accounts": accounts}), encoding="utf-8")
    with (
        patch("fieldkit.sf.territory.resolve_territory_ids", return_value={}) as mock_resolve,
    ):
        result = _resolve_missing_territory_ids(
            "sid-test",
            "https://examplecrm.my.salesforce.com",
            data_root=tmp_path,
            accounts_snapshot=_quota_accounts(tmp_path),
        )

    assert result == accounts
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
        patch("fieldkit.sf.quota.get_accounts_config", return_value={"accounts": {}}),
        pytest.raises(ConfigError, match="sf_territory_id"),
    ):
        fetch_sf_closed_won(tmp_path)


def test_fetch_sf_closed_won_raises_auth_error_on_no_sid(tmp_path: Path) -> None:
    with (
        patch(
            "fieldkit.sf.quota.get_accounts_config",
            return_value={"accounts": {"acme-corp": {"sf_territory": "SELECTED_TERR"}}},
        ),
        patch("fieldkit.config.get_sf_session_id", return_value=""),
        pytest.raises(SFAuthError, match="No Salesforce session"),
    ):
        fetch_sf_closed_won(tmp_path)


@pytest.mark.parametrize("contents", [None, "accounts: []\n", "accounts: {acme-corp: {}}\naccounts: {}\n"])
def test_selected_accounts_fail_before_auth_or_provider_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contents: str | None
) -> None:
    configured = _write_accounts(
        tmp_path / "configured",
        monkeypatch,
        {"acme-corp": {"sf_territory": "DEFAULT_TERR", "sf_territory_id": "0MI000000000001"}},
    )
    before = configured.read_bytes()
    selected = tmp_path / "selected"
    (selected / "config").mkdir(parents=True)
    if contents is not None:
        (selected / "config/accounts.yaml").write_text(contents, encoding="utf-8")
    with (
        patch("fieldkit.config.get_sf_session_id", return_value="synthetic-session") as session,
        patch("fieldkit.config.get_sf_rest_base_url", return_value="https://crm.example.com") as base_url,
        patch("fieldkit.sf.client.SFDirectClient") as client,
        patch("fieldkit.sf.territory.resolve_territory_ids") as resolve,
        patch("fieldkit.sf.territory.fetch_closed_won_by_territory", return_value=99.0) as fetch,
        pytest.raises(ConfigError, match=r"accounts\.yaml|territory"),
    ):
        fetch_sf_closed_won(selected)
    session.assert_not_called()
    base_url.assert_not_called()
    client.assert_not_called()
    resolve.assert_not_called()
    fetch.assert_not_called()
    assert configured.read_bytes() == before


def test_selected_workspace_controls_resolution_write_and_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = _write_accounts(
        tmp_path / "configured",
        monkeypatch,
        {"acme-corp": {"sf_territory": "DEFAULT_TERR", "keywords": ["Default Corporation"]}},
    )
    configured_before = configured.read_bytes()
    selected = tmp_path / "selected"
    (selected / "config").mkdir(parents=True)
    path = selected / "config/accounts.yaml"
    path.write_text(
        "accounts:\n  acme-corp:\n    sf_territory: SELECTED_TERR\n    keywords: [Selected Corporation]\n",
        encoding="utf-8",
    )
    client_instance = MagicMock()
    with (
        patch("fieldkit.config.get_sf_session_id", return_value="synthetic-session"),
        patch("fieldkit.config.get_sf_rest_base_url", return_value="https://crm.example.com"),
        patch("fieldkit.sf.client.SFDirectClient", return_value=client_instance),
        patch("fieldkit.sf.territory.resolve_territory_ids", return_value={"acme-corp": "0MI000000000009"}) as resolve,
        patch("fieldkit.sf.territory.fetch_closed_won_by_territory", return_value=99.0) as fetch,
    ):
        result = fetch_sf_closed_won(selected)
    assert result.amount == 99.0
    assert result.resolved_account_slugs == ("acme-corp",)
    requests = resolve.call_args.args[1]
    assert requests["acme-corp"] == TerritoryResolutionRequest("Selected Corporation", "SELECTED_TERR", None)
    fetch.assert_called_once_with(client_instance.__enter__.return_value, ["Selected Corporation"], ["0MI000000000009"])
    assert (
        yaml.safe_load(path.read_text(encoding="utf-8"))["accounts"]["acme-corp"]["sf_territory_id"]
        == "0MI000000000009"
    )
    assert configured.read_bytes() == configured_before


@pytest.mark.parametrize("operation", ["read", "write"])
def test_selected_territory_accessors_reject_config_directory_redirects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    path = outside / "accounts.yaml"
    path.write_text("accounts: {acme-corp: {sf_territory_id: 0MI000000000001}}\n", encoding="utf-8")
    original = path.read_bytes()
    (workspace / "config").symlink_to(outside, target_is_directory=True)
    read = MagicMock(side_effect=AssertionError("outside file must not be read"))
    monkeypatch.setattr(accounts_mod, "read_accounts_mapping_for_update", read)
    with pytest.raises(ConfigError, match=r"accounts\.yaml"):
        if operation == "read":
            get_sf_territory_ids_from_accounts(workspace_root=workspace, strict=True)
        else:
            set_sf_territory_id_for_account("acme-corp", "0MI000000000009", workspace_root=workspace)
    read.assert_not_called()
    assert path.read_bytes() == original


def test_selected_territory_accessors_allow_workspace_alias(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "config").mkdir(parents=True)
    path = workspace / "config/accounts.yaml"
    path.write_text("accounts: {acme-corp: {sf_territory: SELECTED_TERR}}\n", encoding="utf-8")
    alias = tmp_path / "workspace-alias"
    alias.symlink_to(workspace, target_is_directory=True)
    set_sf_territory_id_for_account("acme-corp", "0MI000000000009", workspace_root=alias)
    result = get_sf_territory_ids_from_accounts(workspace_root=alias, strict=True)
    assert result == ["0MI000000000009"]


def test_absent_selected_territory_read_does_not_create_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "absent"
    result = get_sf_territory_ids_from_accounts(workspace_root=workspace, strict=True)
    assert result == []
    assert not workspace.exists()
    with pytest.raises(ConfigError, match=r"accounts\.yaml"):
        set_sf_territory_id_for_account("acme-corp", "0MI000000000009", workspace_root=workspace)
    assert not workspace.exists()


def test_territory_write_lock_timeout_is_bounded_and_preserves_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_accounts(tmp_path, monkeypatch, {"acme-corp": {"sf_territory": "SELECTED_TERR"}})
    original = path.read_bytes()
    observed: list[tuple[Path, float]] = []

    @contextmanager
    def timeout(target: Path, *, timeout_seconds: float) -> Iterator[None]:
        observed.append((target, timeout_seconds))
        raise TimeoutError("synthetic lock contention")
        yield

    monkeypatch.setattr(accounts_mod, "exclusive_path_lock", timeout, raising=False)
    with pytest.raises(TimeoutError, match="lock contention"):
        set_sf_territory_id_for_account("acme-corp", "0MI000000000009", workspace_root=tmp_path)
    assert observed == [(path, 5)]
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sf_territory", True),
        ("sf_territory", 1),
        ("sf_territory", "   "),
        ("sf_gsg_id", True),
        ("sf_gsg_id", "invalid-gsg"),
        ("sf_territory_id", True),
        ("sf_territory_id", "invalid-territory"),
        ("sf_territory_id", "0MI0000000000000"),
        ("sf_territory_id", "0MI00000000000000"),
    ],
)
def test_invalid_selected_identity_stops_before_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    info: dict[str, object] = {"sf_territory": "SELECTED_TERR"}
    info[field] = value
    path = _write_accounts(tmp_path, monkeypatch, {"acme-corp": info})
    original = path.read_bytes()
    with (
        patch("fieldkit.config.get_sf_session_id") as session,
        patch("fieldkit.sf.client.SFDirectClient") as client,
        pytest.raises(ConfigError, match=r"accounts\.yaml"),
    ):
        fetch_sf_closed_won(tmp_path)
    session.assert_not_called()
    client.assert_not_called()
    assert path.read_bytes() == original


@pytest.mark.parametrize("change", ["sf_territory", "sf_gsg_id", "metadata-type", "metadata-set-type", "deleted"])
def test_stale_resolution_cannot_publish_or_report_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], change: str
) -> None:
    info: dict[str, object] = {"sf_territory": "SELECTED_TERR", "sf_gsg_id": "GSG123", "threshold": 1}
    if change == "metadata-set-type":
        info["threshold"] = {1}
    path = _write_accounts(tmp_path, monkeypatch, {"acme-corp": info})
    changed = ""

    def intervene(*args: object) -> dict[str, str]:
        nonlocal changed
        replacement = dict(info)
        if change == "sf_territory":
            replacement["sf_territory"] = "CHANGED_TERR"
        elif change == "sf_gsg_id":
            replacement["sf_gsg_id"] = "GSG456"
        elif change == "metadata-type":
            replacement["threshold"] = True
        elif change == "metadata-set-type":
            replacement["threshold"] = {True}
        accounts = {} if change == "deleted" else {"acme-corp": replacement}
        changed = yaml.safe_dump({"accounts": accounts})
        path.write_text(changed, encoding="utf-8")
        return {"acme-corp": "0MI000000000009"}

    with (
        patch("fieldkit.config.get_sf_session_id", return_value="synthetic-session"),
        patch("fieldkit.config.get_sf_rest_base_url", return_value="https://crm.example.com"),
        patch("fieldkit.sf.client.SFDirectClient"),
        patch("fieldkit.sf.territory.resolve_territory_ids", side_effect=intervene),
        patch("fieldkit.sf.territory.fetch_closed_won_by_territory") as fetch,
        pytest.raises(ConfigError, match=r"accounts\.yaml"),
    ):
        fetch_sf_closed_won(tmp_path)
    fetch.assert_not_called()
    assert path.read_text(encoding="utf-8") == changed
    assert "✓" not in capsys.readouterr().err


@pytest.mark.parametrize("boundary", ["authentication", "client-entry", "query-result", "client-exit"])
def test_resolved_account_scope_cannot_change_during_quota_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    """Already-resolved accounts obey the same selected-snapshot contract."""
    path = _write_accounts(
        tmp_path,
        monkeypatch,
        {
            "old-corp": {
                "sf_territory": "OLD_TERR",
                "sf_territory_id": "0MI000000000001",
                "keywords": ["Old Corporation"],
            }
        },
    )
    changed = yaml.safe_dump(
        {
            "accounts": {
                "new-corp": {
                    "sf_territory": "NEW_TERR",
                    "sf_territory_id": "0MI000000000002",
                    "keywords": ["New Corporation"],
                }
            }
        }
    )

    def mutate() -> None:
        path.write_text(changed, encoding="utf-8")

    def session() -> str:
        if boundary == "authentication":
            mutate()
        return "synthetic-session"

    def enter() -> object:
        if boundary == "client-entry":
            mutate()
        return object()

    def query(*_args: object) -> float:
        if boundary == "query-result":
            mutate()
        return 17.0

    def exit_client(*_args: object) -> bool:
        if boundary == "client-exit":
            mutate()
        return False

    with (
        patch("fieldkit.config.get_sf_session_id", side_effect=session),
        patch("fieldkit.config.get_sf_rest_base_url", return_value="https://crm.example.com"),
        patch("fieldkit.sf.client.SFDirectClient") as client,
        patch("fieldkit.sf.territory.fetch_closed_won_by_territory", side_effect=query) as fetch,
        pytest.raises(ConfigError, match=r"accounts\.yaml"),
    ):
        client.return_value.__enter__.side_effect = enter
        client.return_value.__exit__.side_effect = exit_client
        fetch_sf_closed_won(tmp_path)
    if boundary in {"query-result", "client-exit"}:
        assert fetch.call_args.args[1:] == (["Old Corporation"], ["0MI000000000001"])
    else:
        fetch.assert_not_called()
    assert path.read_text(encoding="utf-8") == changed


@pytest.mark.parametrize("length", [15, 16, 17, 18])
def test_territory_shape_accepts_only_supported_lengths(length: int) -> None:
    result = territory.is_territory_id("0MI" + "0" * (length - 3))
    assert result is (length in {15, 18})


def test_territory_setter_reads_fresh_under_its_cooperative_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_accounts(tmp_path, monkeypatch, {"acme-corp": {"sf_territory": "SELECTED_TERR"}})
    lock = exclusive_path_lock

    @contextmanager
    def changed_before_lock(target: Path, *, timeout_seconds: float) -> Iterator[None]:
        path.write_text(
            "accounts:\n  acme-corp: {sf_territory: SELECTED_TERR}\n  other-corp: {threshold: 7}\n",
            encoding="utf-8",
        )
        with lock(target, timeout_seconds=timeout_seconds):
            yield

    monkeypatch.setattr(accounts_mod, "exclusive_path_lock", changed_before_lock)
    set_sf_territory_id_for_account(
        "acme-corp",
        "0MI000000000009",
        workspace_root=tmp_path,
        expected_account={"sf_territory": "SELECTED_TERR"},
    )
    result = get_sf_territory_ids_from_accounts(workspace_root=tmp_path, strict=True)
    assert result == ["0MI000000000009"]
    written = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert written["accounts"]["other-corp"] == {"threshold": 7}


def test_territory_write_revalidates_path_before_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_accounts(tmp_path, monkeypatch, {"acme-corp": {"sf_territory": "SELECTED_TERR"}})
    original = path.read_bytes()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_path = outside / "accounts.yaml"
    outside_path.write_bytes(original)
    retired = tmp_path / "retired-config"
    read = accounts_mod.read_accounts_mapping_for_update
    reads = 0

    def redirect_after_capture(target: Path) -> dict[str, object]:
        nonlocal reads
        data = read(target)
        reads += 1
        if reads == 2:
            path.parent.rename(retired)
            path.parent.symlink_to(outside, target_is_directory=True)
        return data

    monkeypatch.setattr(accounts_mod, "read_accounts_mapping_for_update", redirect_after_capture)
    with pytest.raises(ConfigError, match=r"accounts\.yaml"):
        set_sf_territory_id_for_account("acme-corp", "0MI000000000009", workspace_root=tmp_path)
    assert outside_path.read_bytes() == original
    assert (retired / "accounts.yaml").read_bytes() == original


def test_absent_workspace_alias_is_not_a_trusted_read_anchor(tmp_path: Path) -> None:
    alias = tmp_path / "absent-alias"
    alias.symlink_to(tmp_path / "missing-destination", target_is_directory=True)
    with pytest.raises(ConfigError, match=r"accounts\.yaml"):
        get_sf_territory_ids_from_accounts(workspace_root=alias, strict=True)


@pytest.mark.parametrize("identity", ["", "   "])
def test_territory_setter_rejects_empty_identity_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity: str
) -> None:
    path = _write_accounts(tmp_path, monkeypatch, {"acme-corp": {"sf_territory": "SELECTED_TERR"}})
    original = path.read_bytes()
    with pytest.raises(ConfigError, match=r"accounts\.yaml"):
        set_sf_territory_id_for_account("acme-corp", identity, workspace_root=tmp_path)
    assert path.read_bytes() == original
    assert not path.with_suffix(".yaml.lock").exists()


@pytest.mark.parametrize("slug", ["", "   "])
def test_empty_selected_account_name_is_rejected_before_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, slug: str
) -> None:
    _write_accounts(tmp_path, monkeypatch, {slug: {"sf_territory": "SELECTED_TERR"}})
    with (
        patch("fieldkit.config.get_sf_session_id") as session,
        patch("fieldkit.sf.client.SFDirectClient") as client,
        pytest.raises(ConfigError, match=r"accounts\.yaml"),
    ):
        fetch_sf_closed_won(tmp_path)
    session.assert_not_called()
    client.assert_not_called()


# ---------------------------------------------------------------------------
# T06 — CLI two-mode output
# ---------------------------------------------------------------------------

_VALID_QUOTA: dict[str, object] = {"target": 3_000_000, "period": "2026-H1"}
_ONE_CLOSED_WON = [{"stage": "closed-won", "sf_amount": 500_000, "name": "deal-a"}]


def test_quota_cli_source_pursuits_suppresses_gap_line(monkeypatch: pytest.MonkeyPatch) -> None:
    with (
        patch("fieldkit.commands.pipeline.cli.get_pipeline_quota", return_value=_VALID_QUOTA),
        patch("fieldkit.commands.pipeline.cli.get_fieldkit_home", return_value=Path("/tmp/x")),
        patch("fieldkit.pipeline.quota._collect_pursuits_for_quota", return_value=_ONE_CLOSED_WON),
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
        patch("fieldkit.pipeline.quota._collect_pursuits_for_quota", return_value=_ONE_CLOSED_WON),
        patch("fieldkit.sf.quota.fetch_sf_closed_won", return_value=SFQuotaResult(5_606_339.0, ())),
    ):
        result = CliRunner().invoke(cmd_quota, ["--source", "sf"])
    assert result.exit_code == 0
    assert "territory-scoped" in result.output
    assert "Gap" in result.output


def test_quota_cli_reports_resolved_account_count_without_customer_diagnostics() -> None:
    private_slug = "private-customer\nINJECTED"
    with (
        patch("fieldkit.commands.pipeline.cli.get_pipeline_quota", return_value=_VALID_QUOTA),
        patch("fieldkit.commands.pipeline.cli.get_fieldkit_home", return_value=Path("/tmp/x")),
        patch("fieldkit.pipeline.quota._collect_pursuits_for_quota", return_value=_ONE_CLOSED_WON),
        patch("fieldkit.sf.quota.fetch_sf_closed_won", return_value=SFQuotaResult(5_606_339.0, (private_slug,))),
    ):
        result = CliRunner().invoke(cmd_quota, ["--source", "sf", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["sf_closed_won"] == 5_606_339.0
    assert "Resolved sf_territory_id for 1 configured account" in result.stderr
    assert "private-customer" not in result.output
    assert "INJECTED" not in result.output


def test_quota_cli_source_sf_config_error_exits_3(monkeypatch: pytest.MonkeyPatch) -> None:
    with (
        patch("fieldkit.commands.pipeline.cli.get_pipeline_quota", return_value=_VALID_QUOTA),
        patch("fieldkit.commands.pipeline.cli.get_fieldkit_home", return_value=Path("/tmp/x")),
        patch("fieldkit.pipeline.quota._collect_pursuits_for_quota", return_value=_ONE_CLOSED_WON),
        patch(
            "fieldkit.sf.quota.fetch_sf_closed_won",
            side_effect=ConfigError("No sf_territory_id values found in accounts.yaml."),
        ),
    ):
        result = CliRunner().invoke(cmd_quota, ["--source", "sf"])
    assert result.exit_code == 3
    assert "sf_territory_id" in result.output
