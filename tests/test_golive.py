"""Contract tests for deterministic go-live revenue sourcing (implementation change)."""

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fieldkit.__main__ import main
from fieldkit.commands.golive.cli import cli
from fieldkit.errors import FieldkitError
from fieldkit.sf.client import SFAuthError, SFNotFoundError
from fieldkit.sf.components import Bucket, ComponentLine
from fieldkit.sf.golive import assemble_revenue_block

pytestmark = pytest.mark.unit

_OPP_ID = "006000000000000AAA"
_QUOTE_A = "a0Q000000000000AAA"
_QUOTE_B = "a0Q000000000001AAA"


def _component(
    *,
    quote_id: str = _QUOTE_A,
    line_id: str | None = "a0R000000000000AAA",
    sku: str | None = "SKU-1",
    family: str | None = "OpenShift Platform Plus",
    bucket: Bucket = "product",
    quantity: float | None = 2.0,
    unit_price: float | None = 50.0,
    annual_value: float | None = 100.0,
) -> ComponentLine:
    return ComponentLine(
        quote_id=quote_id,
        line_id=line_id,
        sku=sku,
        product_family=family,
        bucket=bucket,
        comp_measure="",
        quantity=quantity,
        unit_price=unit_price,
        net_price=annual_value,
    )


@pytest.mark.parametrize(
    ("family", "bucket"),
    [
        ("SUPPORT - TAM", "tam"),
        ("TRAINING - PREPAID CREDITS", "learning"),
        ("CONSULTING - FIXED PRICE", "consulting"),
        ("OpenShift Platform Plus", "product"),
    ],
)
def test_assemble_revenue_block_preserves_contract_fields(family: str, bucket: Bucket) -> None:
    component = _component(family=family, bucket=bucket)

    result = assemble_revenue_block([component])

    assert result == [
        {
            "quote_id": _QUOTE_A,
            "sku": "SKU-1",
            "product_family": family,
            "bucket": bucket,
            "units": 2.0,
            "unit_price": 50.0,
            "annual_value": 100.0,
            "source": "sf:quote-line:a0R000000000000AAA",
        }
    ]


def test_assemble_revenue_block_sorts_stably_and_preserves_missing_values() -> None:
    lines = [
        _component(quote_id=_QUOTE_B, sku="Z", bucket="product", quantity=None, unit_price=None, annual_value=None),
        _component(quote_id=_QUOTE_A, sku="B", family="TRAINING", bucket="learning"),
        _component(quote_id=_QUOTE_A, sku="A", family="SUPPORT - TAM", bucket="tam"),
    ]

    first = assemble_revenue_block(lines)
    second = assemble_revenue_block(list(reversed(lines)))

    assert first == second
    assert [(row["bucket"], row["sku"]) for row in first] == [("tam", "A"), ("learning", "B"), ("product", "Z")]
    assert first[-1]["units"] is None
    assert first[-1]["unit_price"] is None
    assert first[-1]["annual_value"] is None


def test_assemble_revenue_block_rejects_line_without_source_id() -> None:
    with pytest.raises(FieldkitError, match="quote line id is required for provenance"):
        assemble_revenue_block([_component(line_id=None)])


def _client() -> MagicMock:
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    return client


def test_cli_json_is_byte_identical_for_fixed_input() -> None:
    lines = [
        _component(quote_id=_QUOTE_B, sku="B", line_id="a0R000000000001AAA"),
        _component(quote_id=_QUOTE_A, sku="A"),
    ]
    runner = CliRunner()

    with (
        patch("fieldkit.commands.golive.cli.get_sf_session_id", return_value="sid"),
        patch("fieldkit.commands.golive.cli.get_sf_rest_base_url", return_value="https://example.my.salesforce.com"),
        patch("fieldkit.commands.golive.cli.SFDirectClient") as client_cls,
        patch("fieldkit.commands.golive.cli.resolve_opportunity_reference", return_value=_OPP_ID),
        patch("fieldkit.commands.golive.cli.fetch_opp_component_lines", return_value=lines),
    ):
        client_cls.return_value = _client()
        first = runner.invoke(cli, [_OPP_ID, "--json"])
        second = runner.invoke(cli, [_OPP_ID, "--json"])

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert first.output == second.output
    assert json.loads(first.output) == assemble_revenue_block(lines)


def test_cli_human_output_groups_quotes_and_prints_totals_with_source_last() -> None:
    lines = [
        _component(quote_id=_QUOTE_A, sku="A", annual_value=100.0),
        _component(quote_id=_QUOTE_B, sku="B", line_id="a0R000000000001AAA", annual_value=250.0),
    ]
    runner = CliRunner()

    with (
        patch("fieldkit.commands.golive.cli.get_sf_session_id", return_value="sid"),
        patch("fieldkit.commands.golive.cli.get_sf_rest_base_url", return_value="https://example.my.salesforce.com"),
        patch("fieldkit.commands.golive.cli.SFDirectClient") as client_cls,
        patch("fieldkit.commands.golive.cli.resolve_opportunity_reference", return_value=_OPP_ID),
        patch("fieldkit.commands.golive.cli.fetch_opp_component_lines", return_value=lines),
    ):
        client_cls.return_value = _client()
        result = runner.invoke(cli, [_OPP_ID])

    assert result.exit_code == 0
    assert _QUOTE_A in result.output
    assert _QUOTE_B in result.output
    assert "Grand total" in result.output
    assert "$350.00" in result.output
    assert "Source" in result.output


def test_cli_human_output_does_not_impute_missing_annual_value_as_zero() -> None:
    runner = CliRunner()
    lines = [_component(annual_value=None)]
    with (
        patch("fieldkit.commands.golive.cli.get_sf_session_id", return_value="sid"),
        patch("fieldkit.commands.golive.cli.get_sf_rest_base_url", return_value="https://example.my.salesforce.com"),
        patch("fieldkit.commands.golive.cli.SFDirectClient") as client_cls,
        patch("fieldkit.commands.golive.cli.resolve_opportunity_reference", return_value=_OPP_ID),
        patch("fieldkit.commands.golive.cli.fetch_opp_component_lines", return_value=lines),
    ):
        client_cls.return_value = _client()
        result = runner.invoke(cli, [_OPP_ID])

    assert result.exit_code == 0
    assert "Product total" in result.output
    assert "Grand total: —" in result.output
    assert "$0.00" not in result.output


def test_cli_human_output_sums_numeric_values_in_mixed_null_bucket() -> None:
    runner = CliRunner()
    lines = [
        _component(sku="A", annual_value=None),
        _component(sku="B", line_id="a0R000000000001AAA", annual_value=125.0),
    ]
    with (
        patch("fieldkit.commands.golive.cli.get_sf_session_id", return_value="sid"),
        patch("fieldkit.commands.golive.cli.get_sf_rest_base_url", return_value="https://example.my.salesforce.com"),
        patch("fieldkit.commands.golive.cli.SFDirectClient") as client_cls,
        patch("fieldkit.commands.golive.cli.resolve_opportunity_reference", return_value=_OPP_ID),
        patch("fieldkit.commands.golive.cli.fetch_opp_component_lines", return_value=lines),
    ):
        client_cls.return_value = _client()
        result = runner.invoke(cli, [_OPP_ID])

    assert result.exit_code == 0
    subtotal_line = next(line for line in result.output.splitlines() if "Product total" in line)
    assert "$125.00" in subtotal_line


def test_cli_no_quote_lines_is_explicit_in_both_modes() -> None:
    runner = CliRunner()
    with (
        patch("fieldkit.commands.golive.cli.get_sf_session_id", return_value="sid"),
        patch("fieldkit.commands.golive.cli.get_sf_rest_base_url", return_value="https://example.my.salesforce.com"),
        patch("fieldkit.commands.golive.cli.SFDirectClient") as client_cls,
        patch("fieldkit.commands.golive.cli.resolve_opportunity_reference", return_value=_OPP_ID),
        patch("fieldkit.commands.golive.cli.fetch_opp_component_lines", return_value=[]),
    ):
        client_cls.return_value = _client()
        human = runner.invoke(cli, [_OPP_ID])
        machine = runner.invoke(cli, [_OPP_ID, "--json"])

    assert human.exit_code == 0
    assert "no quote lines — nothing to source" in human.output
    assert machine.exit_code == 0
    assert json.loads(machine.output) == []


def test_cli_auth_error_uses_exit_2() -> None:
    runner = CliRunner()
    with patch("fieldkit.commands.golive.cli.get_sf_session_id", side_effect=SFAuthError("expired")):
        result = runner.invoke(cli, [_OPP_ID])

    assert result.exit_code == 2
    assert "Auth error" in result.output


def test_cli_unresolved_opportunity_uses_exit_3() -> None:
    runner = CliRunner()
    with (
        patch("fieldkit.commands.golive.cli.get_sf_session_id", return_value="sid"),
        patch("fieldkit.commands.golive.cli.get_sf_rest_base_url", return_value="https://example.my.salesforce.com"),
        patch("fieldkit.commands.golive.cli.SFDirectClient") as client_cls,
        patch("fieldkit.commands.golive.cli.resolve_opportunity_reference", return_value=None),
    ):
        client_cls.return_value = _client()
        result = runner.invoke(cli, ["12345678"])

    assert result.exit_code == 3
    assert "Opportunity '12345678' was not found" in result.output


def test_dispatcher_nonexistent_direct_id_uses_clean_exit_3(capsys: pytest.CaptureFixture[str]) -> None:
    missing_id = "006AAAAAAAAAAAAAAA"
    with (
        patch("fieldkit.commands.golive.cli.get_sf_session_id", return_value="sid"),
        patch("fieldkit.commands.golive.cli.get_sf_rest_base_url", return_value="https://example.my.salesforce.com"),
        patch("fieldkit.commands.golive.cli.SFDirectClient") as client_cls,
        patch(
            "fieldkit.commands.golive.cli.resolve_opportunity_reference",
            side_effect=SFNotFoundError("missing"),
        ),
    ):
        client_cls.return_value = _client()
        result = main(["golive", missing_id])

    captured = capsys.readouterr()
    assert result == 3
    assert f"Opportunity '{missing_id}' was not found" in captured.err
