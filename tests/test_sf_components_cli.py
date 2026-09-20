"""CLI contracts for ``fieldkit sf components``."""

import json
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from fieldkit.commands.sf.components import cli
from fieldkit.sf.client import SFAuthError
from fieldkit.sf.components import ComponentLine

pytestmark = pytest.mark.unit

_OPP_ID = "006000000000000AAA"


def _patch_client(monkeypatch: pytest.MonkeyPatch, lines: list[ComponentLine]) -> MagicMock:
    client = MagicMock()
    client.__enter__.return_value = client
    monkeypatch.setattr("fieldkit.commands.sf.components.get_sf_session_id", lambda: "sid")
    monkeypatch.setattr("fieldkit.commands.sf.components.get_sf_rest_base_url", lambda: "https://example.com")
    monkeypatch.setattr("fieldkit.commands.sf.components.SFDirectClient", lambda **kwargs: client)
    monkeypatch.setattr("fieldkit.commands.sf.components.resolve_opportunity_reference", lambda *_: _OPP_ID)
    monkeypatch.setattr("fieldkit.commands.sf.components.fetch_opp_component_lines", lambda *_: lines)
    return client


def test_components_json_is_sorted(monkeypatch: pytest.MonkeyPatch) -> None:
    lines: list[ComponentLine] = [
        {
            "quote_id": "Q2",
            "line_id": None,
            "sku": None,
            "product_family": None,
            "bucket": "product",
            "comp_measure": "",
            "quantity": None,
            "unit_price": None,
            "net_price": None,
        },
        {
            "quote_id": "Q1",
            "line_id": "L1",
            "sku": "T1",
            "product_family": "SUPPORT - TAM",
            "bucket": "tam",
            "comp_measure": "M000114",
            "quantity": 1.0,
            "unit_price": 2.0,
            "net_price": 2.0,
        },
    ]
    _patch_client(monkeypatch, lines)
    result = CliRunner().invoke(cli, [_OPP_ID, "--json"])
    parsed = json.loads(result.output)
    assert result.exit_code == 0
    assert [line["bucket"] for line in parsed] == ["tam", "product"]


def test_components_empty_result(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, [])
    result = CliRunner().invoke(cli, [_OPP_ID])
    assert result.exit_code == 0
    assert result.output == "no component lines\n"


def test_components_human_output_shows_group_and_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    lines: list[ComponentLine] = [
        {
            "quote_id": "Q1",
            "line_id": "L1",
            "sku": "TAM-1",
            "product_family": "SUPPORT - TAM",
            "bucket": "tam",
            "comp_measure": "M000114",
            "quantity": 1.0,
            "unit_price": 2.0,
            "net_price": 2.0,
        }
    ]
    _patch_client(monkeypatch, lines)
    result = CliRunner().invoke(cli, [_OPP_ID])
    assert result.exit_code == 0
    assert "TAM" in result.output
    assert "TAM-1" in result.output
    assert "M000114" in result.output


def test_components_unresolved_reference_is_data_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, [])
    monkeypatch.setattr("fieldkit.commands.sf.components.resolve_opportunity_reference", lambda *_: None)
    result = CliRunner().invoke(cli, ["12345"])
    assert result.exit_code == 3
    assert "was not found" in result.output


def test_components_auth_failure_uses_central_exit_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, [])
    monkeypatch.setattr(
        "fieldkit.commands.sf.components.fetch_opp_component_lines", MagicMock(side_effect=SFAuthError("expired"))
    )
    result = CliRunner().invoke(cli, [_OPP_ID])
    assert result.exit_code == 2
    assert "expired" in result.output
