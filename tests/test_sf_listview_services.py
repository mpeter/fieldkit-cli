"""Services-only contracts for the Salesforce listview command."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from fieldkit.commands.sf.listview import cli
from fieldkit.sf.components import ComponentLine
from fieldkit.sf.types import OpportunityRecord, OpportunitySearchResult

pytestmark = pytest.mark.unit


def _fake_config() -> dict[str, Any]:
    return {"accounts": {"global-pay": {"keywords": ["Global Pay", "globalpay"]}}}


def _make_candidate(opp_id: str) -> OpportunityRecord:
    return {"opportunity_id": opp_id, "name": "Test Opp", "stage": "Negotiate", "close_date": "2026-06-01"}


def test_services_only_capped_result_refuses_before_component_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from fieldkit.commands.sf.listview import _sync_account_opps

    monkeypatch.setattr("fieldkit.commands.sf.listview._quiet_mode", True)
    client = MagicMock()
    client.search_opportunity_candidates.return_value = OpportunitySearchResult(
        records=[_make_candidate(str(index)) for index in range(2000)], capped=True
    )
    result = _sync_account_opps(
        "global-pay", _fake_config()["accounts"], tmp_path, client, services_only=True, limit=2000
    )
    assert result[2] == 1
    client.fetch_related_list_records.assert_not_called()
    assert "total is unknown" in capsys.readouterr().err


def test_services_only_over_limit_reports_diagnostic_while_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from fieldkit.commands.sf.listview import _sync_account_opps

    monkeypatch.setattr("fieldkit.commands.sf.listview._quiet_mode", True)
    client = MagicMock()
    client.search_opportunity_candidates.return_value = OpportunitySearchResult(
        records=[_make_candidate("one"), _make_candidate("two")], capped=False
    )
    result = _sync_account_opps("global-pay", _fake_config()["accounts"], tmp_path, client, services_only=True, limit=1)
    assert result[2] == 1
    assert "Rerun with --limit 2" in capsys.readouterr().err


def test_services_only_search_failure_reports_diagnostic_while_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from fieldkit.commands.sf.listview import _sync_account_opps
    from fieldkit.sf.client import SFAPIError

    monkeypatch.setattr("fieldkit.commands.sf.listview._quiet_mode", True)
    client = MagicMock()
    client.search_opportunity_candidates.side_effect = SFAPIError("unavailable")
    result = _sync_account_opps("global-pay", _fake_config()["accounts"], tmp_path, client, services_only=True)
    assert result[2] == 1
    assert "SOSL search failed" in capsys.readouterr().err


def test_services_only_auth_failure_reports_reauth_while_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from fieldkit.commands.sf.listview import _sync_account_opps
    from fieldkit.sf.client import SFAuthError

    monkeypatch.setattr("fieldkit.commands.sf.listview._quiet_mode", True)
    client = MagicMock()
    client.search_opportunity_candidates.side_effect = SFAuthError("expired")
    result = _sync_account_opps("global-pay", _fake_config()["accounts"], tmp_path, client, services_only=True)
    assert result[3] is True
    assert "Auth failure" in capsys.readouterr().err


def test_services_only_missing_keywords_reports_diagnostic_while_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from fieldkit.commands.sf.listview import _sync_account_opps

    monkeypatch.setattr("fieldkit.commands.sf.listview._quiet_mode", True)
    result = _sync_account_opps("global-pay", {"global-pay": {}}, tmp_path, MagicMock(), services_only=True)
    assert result[2] == 1
    assert "No keywords configured" in capsys.readouterr().err


def test_services_only_qualifies_tam_and_excludes_product(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit.commands.sf.listview import _sync_account_opps

    candidates = [_make_candidate("tam"), _make_candidate("product")]
    client = MagicMock()
    client.search_opportunity_candidates.return_value = OpportunitySearchResult(records=candidates, capped=False)
    monkeypatch.setattr(
        "fieldkit.sf.components.fetch_opp_component_lines",
        lambda _client, opp_id: [
            {
                "quote_id": "Q1",
                "line_id": "L1",
                "sku": "S1",
                "product_family": "SUPPORT - TAM" if opp_id == "tam" else "PRODUCT",
                "bucket": "tam" if opp_id == "tam" else "product",
                "comp_measure": "M000114" if opp_id == "tam" else "",
                "quantity": 1.0,
                "unit_price": 1.0,
                "net_price": 1.0,
            }
        ],
    )
    monkeypatch.setattr("fieldkit.commands.sf.listview._process_opp", lambda opp, _path: (0, 1, 0, opp))
    result = _sync_account_opps("global-pay", _fake_config()["accounts"], tmp_path, client, services_only=True)
    assert result[1] == 1
    assert [opp["opportunity_id"] for opp in result[4]] == ["tam"]


def test_services_only_missing_id_is_partial_and_scans_later_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from fieldkit.commands.sf.listview import _sync_account_opps

    candidates: list[OpportunityRecord] = [{"name": "missing"}, _make_candidate("tam")]
    client = MagicMock()
    client.search_opportunity_candidates.return_value = OpportunitySearchResult(records=candidates, capped=False)
    walk = MagicMock(return_value=[])
    monkeypatch.setattr("fieldkit.sf.components.fetch_opp_component_lines", walk)
    result = _sync_account_opps("global-pay", _fake_config()["accounts"], tmp_path, client, services_only=True)
    assert result[2] == 1
    walk.assert_called_once_with(client, "tam")
    assert "no opportunity id" in capsys.readouterr().err


def test_services_only_walk_failure_reports_opportunity_while_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from fieldkit.commands.sf.listview import _sync_account_opps
    from fieldkit.sf.client import SFAPIError

    monkeypatch.setattr("fieldkit.commands.sf.listview._quiet_mode", True)
    client = MagicMock()
    client.search_opportunity_candidates.return_value = OpportunitySearchResult(
        records=[_make_candidate("broken")], capped=False
    )
    monkeypatch.setattr(
        "fieldkit.sf.components.fetch_opp_component_lines", MagicMock(side_effect=SFAPIError("timeout"))
    )
    result = _sync_account_opps("global-pay", _fake_config()["accounts"], tmp_path, client, services_only=True)
    assert result[2] == 1
    diagnostic = capsys.readouterr().err
    assert "broken" in diagnostic
    assert "timeout" in diagnostic


def test_services_only_walk_failure_continues_to_later_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fieldkit.commands.sf.listview import _sync_account_opps
    from fieldkit.sf.client import SFAPIError

    client = MagicMock()
    client.search_opportunity_candidates.return_value = OpportunitySearchResult(
        records=[_make_candidate("broken"), _make_candidate("tam")], capped=False
    )
    tam_line: ComponentLine = {
        "quote_id": "Q1",
        "line_id": "L1",
        "sku": "T1",
        "product_family": "SUPPORT - TAM",
        "bucket": "tam",
        "comp_measure": "M000114",
        "quantity": 1.0,
        "unit_price": 1.0,
        "net_price": 1.0,
    }
    walk = MagicMock(side_effect=[SFAPIError("timeout"), [tam_line]])
    monkeypatch.setattr("fieldkit.sf.components.fetch_opp_component_lines", walk)
    monkeypatch.setattr("fieldkit.commands.sf.listview._process_opp", lambda opp, _path: (0, 1, 0, opp))
    result = _sync_account_opps("global-pay", _fake_config()["accounts"], tmp_path, client, services_only=True)
    assert result[2] == 1
    assert [opp["opportunity_id"] for opp in result[4]] == ["tam"]


def test_services_only_component_auth_failure_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fieldkit.commands.sf.listview import _sync_account_opps
    from fieldkit.sf.client import SFAuthError

    client = MagicMock()
    client.search_opportunity_candidates.return_value = OpportunitySearchResult(
        records=[_make_candidate("tam")], capped=False
    )
    monkeypatch.setattr(
        "fieldkit.sf.components.fetch_opp_component_lines", MagicMock(side_effect=SFAuthError("expired"))
    )
    with pytest.raises(SFAuthError, match="expired"):
        _sync_account_opps("global-pay", _fake_config()["accounts"], tmp_path, client, services_only=True)


@pytest.mark.parametrize("limit", ["0", "-1"])
def test_services_only_rejects_nonpositive_limit_before_run(monkeypatch: pytest.MonkeyPatch, limit: str) -> None:
    run = MagicMock()
    monkeypatch.setattr("fieldkit.commands.sf.listview._run_listview", run)
    result = (
        __import__("click.testing", fromlist=["CliRunner"])
        .CliRunner()
        .invoke(cli, ["global-pay", "--services-only", "--limit", limit])
    )
    assert result.exit_code != 0
    run.assert_not_called()
