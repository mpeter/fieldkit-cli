"""Regression coverage for missing scaffold amounts in forecasts."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from fieldkit.__main__ import main
from fieldkit.commands.pursuit.create_cmd import cli as create_cli
from fieldkit.commands.pursuit.forecast import DealRow, _parse_deal_row, cli, compute_forecast
from fieldkit.pursuit.io import write_frontmatter_raw

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("content", [None, "# No frontmatter\n"])
def test_forecast_ignores_unreadable_or_unstructured_file(tmp_path: Path, content: str | None) -> None:
    path = tmp_path / "platform-pilot.md"
    if content is not None:
        path.write_text(content, encoding="utf-8")

    assert _parse_deal_row(path, tmp_path) is None


def test_forecast_deal_outside_accounts_uses_filename(tmp_path: Path) -> None:
    path = tmp_path / "platform-pilot.md"
    write_frontmatter_raw(
        path,
        {"stage": "validate", "sf_consulting_acv": "", "sf_acv": 100000},
        "\n# Platform Pilot\n",
        create=True,
    )

    assert _parse_deal_row(path, tmp_path / "accounts") == DealRow(
        relative_path="platform-pilot.md",
        name="platform-pilot",
        stage="validate",
        acv=100000,
        close_date_str="",
        weight=0.25,
    )


@pytest.mark.parametrize(
    ("contract_type", "gross", "net", "arr", "expected"),
    [
        (None, "", 100000, None, 100000),
        ("standard", None, 100000, None, 100000),
        ("standard", "absent", 100000, None, 100000),
        ("standard", "   ", 100000, None, 100000),
        ("standard", 0, 100000, 200000, 0),
        ("standard", "0", 100000, 200000, 0),
        ("standard", "$150,000.00", 100000, 200000, 150000),
        ("fixed_price", 150000, 100000, 200000, 100000),
        ("fixed_price", 150000, 0, 200000, 0),
        ("fixed_price", 150000, "", 200000, 150000),
        ("fixed_price", "", None, "$200,000", 200000),
        ("standard", "", "", 200000, 200000),
        ("standard", "", None, None, 0),
    ],
)
def test_forecast_component_fallback(
    tmp_path: Path,
    contract_type: str | None,
    gross: str | int | None,
    net: str | int | None,
    arr: str | int | None,
    expected: int,
) -> None:
    path = tmp_path / "accounts/acme-fictional/pursuits/platform-pilot.md"
    path.parent.mkdir(parents=True)
    fm: dict[str, object] = {
        "stage": "validate",
        "sf_contract_type": contract_type,
        "sf_acv": net,
        "sf_arr": arr,
    }
    if gross != "absent":
        fm["sf_consulting_acv"] = gross
    write_frontmatter_raw(path, fm, "\n# Platform Pilot\n", create=True)
    before = path.read_bytes()

    result = compute_forecast(tmp_path, quota=200000)

    assert result.best_case == expected
    assert result.weighted == expected * 0.25
    assert result.commit == 0
    assert result.quota == 200000
    assert result.deals[0].acv == expected
    assert path.read_bytes() == before


@pytest.mark.parametrize("field", ["sf_consulting_acv", "sf_acv", "sf_arr"])
def test_forecast_malformed_amount_warns(tmp_path: Path, field: str, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "accounts/acme-fictional/pursuits/platform-pilot.md"
    path.parent.mkdir(parents=True)
    fm: dict[str, object] = {"stage": "validate", "sf_arr": 200000, field: "not-a-number"}
    write_frontmatter_raw(path, fm, "\n# Platform Pilot\n", create=True)
    before = path.read_bytes()

    result = compute_forecast(tmp_path)

    assert result.best_case == (0 if field == "sf_arr" else 200000)
    diagnostic = capsys.readouterr()
    assert diagnostic.out == ""
    assert "Malformed forecast amount" in diagnostic.err
    assert field in diagnostic.err
    assert "not-a-number" not in diagnostic.err
    assert path.read_bytes() == before


@pytest.mark.parametrize("field", ["sf_consulting_acv", "sf_acv", "sf_arr"])
def test_forecast_malformed_json_diagnostics_are_sanitized(tmp_path: Path, field: str) -> None:
    path = tmp_path / "accounts/acme-fictional/pursuits/platform-pilot.md"
    path.parent.mkdir(parents=True)
    malformed = f"sensitive-value-{tmp_path / 'home' / 'private-note'}"
    fm: dict[str, object] = {"stage": "validate", "sf_arr": 200000, field: malformed}
    write_frontmatter_raw(path, fm, "\n# Platform Pilot\n", create=True)
    before = path.read_bytes()
    with patch("fieldkit.commands.pursuit.forecast.get_fieldkit_home", return_value=tmp_path):
        result = CliRunner().invoke(cli, ["--json", "--quota", "200000"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    expected = 0 if field == "sf_arr" else 200000
    assert payload["best_case"] == expected
    assert payload["weighted"] == expected * 0.25
    assert "Malformed forecast amount" in result.stderr
    assert field in result.stderr
    assert malformed not in result.output
    assert str(tmp_path) not in result.output
    assert "UserWarning" not in result.output
    assert path.read_bytes() == before


@pytest.mark.parametrize("field", ["sf_consulting_acv", "sf_acv", "sf_arr"])
@pytest.mark.parametrize("shape", ["mapping", "list"])
def test_forecast_dispatcher_rejects_structured_amount_without_disclosure(
    tmp_path: Path, field: str, shape: str, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "accounts/acme-fictional/pursuits/platform-pilot.md"
    path.parent.mkdir(parents=True)
    sensitive_value = f"sensitive-value-{tmp_path / 'home' / 'private-note'}"
    malformed: object = {"note": sensitive_value} if shape == "mapping" else [sensitive_value]
    fm: dict[str, object] = {"stage": "validate", "sf_arr": 200000, field: malformed}
    write_frontmatter_raw(path, fm, "\n# Platform Pilot\n", create=True)
    before = path.read_bytes()
    with patch("fieldkit.commands.pursuit.forecast.get_fieldkit_home", return_value=tmp_path):
        result = main(["pursuit", "forecast", "--json", "--quota", "200000"])

    assert result == 3
    diagnostic = capsys.readouterr()
    assert diagnostic.out == ""
    assert sensitive_value not in diagnostic.err
    assert str(tmp_path) not in diagnostic.err
    assert "Traceback" not in diagnostic.err
    assert "ValidationError" not in diagnostic.err
    assert "Invalid forecast amount" in diagnostic.err
    assert field in diagnostic.err
    assert path.read_bytes() == before


@pytest.mark.parametrize("net", [100000, 0])
def test_scaffold_populate_net_forecast_json(tmp_path: Path, net: int) -> None:
    (tmp_path / "accounts/acme-fictional").mkdir(parents=True)
    runner = CliRunner()
    with patch("fieldkit.commands.pursuit.create_cmd._data_root", return_value=tmp_path):
        created = runner.invoke(
            create_cli,
            ["--account", "acme-fictional", "--name", "Platform Pilot", "--stage", "validate", "--json"],
        )
    assert created.exit_code == 0, created.output
    path = tmp_path / "accounts/acme-fictional/pursuits/platform-pilot.md"
    scaffold = path.read_text(encoding="utf-8")
    assert 'sf_consulting_acv: ""' in scaffold
    assert 'sf_acv: ""' in scaffold
    path.write_text(scaffold.replace('sf_acv: ""', f"sf_acv: {net}"), encoding="utf-8")
    before = path.read_bytes()
    with patch("fieldkit.commands.pursuit.forecast.get_fieldkit_home", return_value=tmp_path):
        result = runner.invoke(cli, ["--json", "--quota", "200000"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["deals"][0]["acv"] == net
    assert payload["best_case"] == net
    assert payload["weighted"] == net * 0.25
    assert payload["quota"] == 200000
    assert ("$0 ACV" in result.stderr) == (net == 0)
    if net:
        assert result.stderr == ""
    assert path.read_bytes() == before
