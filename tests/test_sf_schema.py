"""Unit tests for the safe Salesforce schema reference command."""

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

import fieldkit.sf.client as sf_client
from fieldkit.commands.sf.schema import cli
from fieldkit.sf.client import SFAPIError, SFAuthError, SFDataAccessError, SFDirectClient, SFNotFoundError
from fieldkit.sf.schema import MAX_SAMPLE_RECORDS, classify_observed_population, collect_schema_reference

pytestmark = pytest.mark.unit

_RECORD_ID = "001000000000000AAA"


def _describe_field(name: str, **overrides: object) -> dict[str, object]:
    """Return a field entry shaped like a real describe response (no per-field ``queryable``)."""
    entry: dict[str, object] = {
        "name": name,
        "label": f"{name} label",
        "type": "string",
        "deprecatedAndHidden": False,
        "compoundFieldName": None,
    }
    entry.update(overrides)
    return entry


def _describe(*fields: object) -> dict[str, object]:
    """Return a describe response; ``queryable`` is reported on the object only."""
    return {"name": "Account", "queryable": True, "fields": list(fields)}


@pytest.fixture(autouse=True)
def _zero_retry_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep retry counts and error paths while avoiding real backoff in unit tests."""
    monkeypatch.setattr(
        sf_client,
        "_sf_request_idempotent",
        sf_client._sf_request_idempotent.with_policy(wait_min=0, wait_max=0),
    )


def test_describe_sobject_requests_metadata() -> None:
    response = MagicMock(status_code=200, headers={"content-type": "application/json"})
    response.json.return_value = {"fields": []}
    mock_http = MagicMock()
    mock_http.request.return_value = response
    with (
        patch("httpx.Client", return_value=mock_http),
        SFDirectClient(session_id="sid", base_url="https://sf.example.com") as client,
    ):
        result = client.describe_sobject("Account")
    assert result == {"fields": []}
    assert mock_http.request.call_args.args[1].endswith("/sobjects/Account/describe")


@pytest.mark.parametrize(
    "status,error_type",
    [(401, SFAuthError), (403, SFDataAccessError), (404, SFNotFoundError), (500, SFAPIError)],
)
def test_describe_sobject_maps_errors(status: int, error_type: type[Exception]) -> None:
    response = MagicMock(status_code=status, text="error")
    mock_http = MagicMock()
    mock_http.request.return_value = response
    with (
        patch("httpx.Client", return_value=mock_http),
        pytest.raises(error_type, match=r"."),
        SFDirectClient(session_id="sid", base_url="https://sf.example.com") as client,
    ):
        client.describe_sobject("Account")


def test_describe_sobject_maps_html_login_to_auth_error() -> None:
    response = MagicMock(status_code=200, headers={"content-type": "text/html"})
    mock_http = MagicMock()
    mock_http.request.return_value = response
    with (
        patch("httpx.Client", return_value=mock_http),
        pytest.raises(SFAuthError, match="HTML login"),
        SFDirectClient(session_id="sid", base_url="https://sf.example.com") as client,
    ):
        client.describe_sobject("Account")


def test_classify_observed_population_handles_falsey_values_and_nulls() -> None:
    result = classify_observed_population(
        ("Populated", "Null", "Missing"), [{"Populated": 0, "Null": None}, {"Populated": False}]
    )
    assert result == {"Populated": "observed-populated", "Null": "observed-null", "Missing": "not-sampled"}


def test_collect_schema_reference_chunks_and_omits_values() -> None:
    client = MagicMock()
    client.describe_sobject.return_value = _describe(*[_describe_field(f"Field{i}") for i in range(51)])
    client.fetch_sobject.return_value = {"Field0": "SECRET-SENTINEL", "Field1": None}
    reference = collect_schema_reference(client, "Account", (_RECORD_ID,))
    assert len(reference.fields) == 51
    assert client.fetch_sobject.call_count == 2
    assert reference.fields[0].population == "observed-populated"
    assert "SECRET-SENTINEL" not in repr(reference)


def test_collect_schema_reference_rejects_oversized_sample() -> None:
    client = MagicMock()
    with pytest.raises(ValueError, match="At most"):
        collect_schema_reference(client, "Account", (_RECORD_ID,) * (MAX_SAMPLE_RECORDS + 1))
    assert not client.describe_sobject.called


def test_collect_schema_reference_rejects_describe_without_field_list() -> None:
    client = MagicMock()
    client.describe_sobject.return_value = {"fields": None}

    with pytest.raises(SFAPIError, match="did not contain a fields list"):
        collect_schema_reference(client, "Account", (_RECORD_ID,))


def test_collect_schema_reference_lists_fields_without_per_field_queryable() -> None:
    """Regression for #87: real describe entries carry no per-field ``queryable``."""
    client = MagicMock()
    client.describe_sobject.return_value = _describe(
        _describe_field("Id", type="id"),
        _describe_field("Name"),
        _describe_field("BillingStreet", compoundFieldName="BillingAddress"),
        _describe_field("BillingAddress", type="address"),
    )
    client.fetch_sobject.return_value = {"Id": _RECORD_ID, "Name": None, "BillingAddress": {"city": "Springfield"}}

    reference = collect_schema_reference(client, "Account", (_RECORD_ID,))

    assert [field.name for field in reference.fields] == ["Id", "Name", "BillingStreet", "BillingAddress"]
    assert [field.population for field in reference.fields] == [
        "observed-populated",
        "observed-null",
        "not-sampled",
        "observed-populated",
    ]
    client.fetch_sobject.assert_called_once_with("Account", _RECORD_ID, "Id,Name,BillingStreet,BillingAddress")


def test_collect_schema_reference_excludes_hidden_and_nameless_fields() -> None:
    client = MagicMock()
    client.describe_sobject.return_value = _describe(
        "not-field-metadata",
        _describe_field("Visible"),
        _describe_field("Retired__c", deprecatedAndHidden=True),
        _describe_field("Flagged", deprecatedAndHidden=False),
        {"name": 42, "label": "Numeric name"},
    )
    client.fetch_sobject.return_value = {"Visible": "x"}

    reference = collect_schema_reference(client, "Account", (_RECORD_ID,))

    assert [field.name for field in reference.fields] == ["Visible", "Flagged"]
    client.fetch_sobject.assert_called_once_with("Account", _RECORD_ID, "Visible,Flagged")


def test_collect_schema_reference_rejects_describe_with_no_eligible_fields() -> None:
    client = MagicMock()
    client.describe_sobject.return_value = _describe(
        _describe_field("Retired__c", deprecatedAndHidden=True),
        _describe_field("Old__c", deprecatedAndHidden=True),
    )

    with pytest.raises(SFDataAccessError, match="no eligible fields"):
        collect_schema_reference(client, "Account", (_RECORD_ID,))

    assert not client.fetch_sobject.called


def test_cli_exits_data_error_when_no_field_is_eligible() -> None:
    client = MagicMock()
    client.describe_sobject.return_value = _describe(_describe_field("Retired__c", deprecatedAndHidden=True))
    with (
        patch("fieldkit.commands.sf.schema.get_sf_session_id", return_value="sid"),
        patch("fieldkit.commands.sf.schema.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.schema.SFDirectClient") as client_class,
    ):
        client_class.return_value.__enter__.return_value = client
        result = CliRunner().invoke(cli, ["Account", "--record-id", _RECORD_ID])
    assert result.exit_code == 3
    assert "no eligible fields" in result.output
    assert not client.fetch_sobject.called


def test_cli_renders_safe_deterministic_output() -> None:
    client = MagicMock()
    client.describe_sobject.return_value = _describe(_describe_field("Name", label="Account Name"))
    client.fetch_sobject.return_value = {"Name": "SECRET-SENTINEL"}
    with (
        patch("fieldkit.commands.sf.schema.get_sf_session_id", return_value="sid"),
        patch("fieldkit.commands.sf.schema.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.schema.SFDirectClient") as client_class,
    ):
        client_class.return_value.__enter__.return_value = client
        result = CliRunner().invoke(cli, ["Account", "--record-id", _RECORD_ID, "--record-id", _RECORD_ID])
    assert result.exit_code == 0
    assert "Sampled records: 2" in result.output
    assert "observed-populated" in result.output
    assert "SECRET-SENTINEL" not in result.output


def test_cli_json_renders_safe_structured_reference() -> None:
    client = MagicMock()
    client.describe_sobject.return_value = _describe(_describe_field("Name", label="Account Name"))
    client.fetch_sobject.return_value = {"Name": "SECRET-SENTINEL"}
    with (
        patch("fieldkit.commands.sf.schema.get_sf_session_id", return_value="sid"),
        patch("fieldkit.commands.sf.schema.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.schema.SFDirectClient") as client_class,
    ):
        client_class.return_value.__enter__.return_value = client
        result = CliRunner().invoke(cli, ["Account", "--record-id", _RECORD_ID, "--json"])

    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert payload == {
        "fields": [
            {
                "field_type": "string",
                "label": "Account Name",
                "name": "Name",
                "population": "observed-populated",
            }
        ],
        "sample_count": 1,
        "sobject_type": "Account",
    }
    assert "SECRET-SENTINEL" not in result.output


def test_cli_help_describes_explicit_record_ids() -> None:
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "--record-id ID" in result.output


def test_cli_rejects_oversized_sample_before_client_construction() -> None:
    arguments = ["Account"] + ["--record-id", _RECORD_ID] * (MAX_SAMPLE_RECORDS + 1)
    with patch("fieldkit.commands.sf.schema.SFDirectClient") as client_class:
        result = CliRunner().invoke(cli, arguments)
    assert result.exit_code == 3
    assert not client_class.called


def test_cli_missing_session_is_auth_failure() -> None:
    with patch("fieldkit.commands.sf.schema.get_sf_session_id", return_value=""):
        result = CliRunner().invoke(cli, ["Account", "--record-id", _RECORD_ID])
    assert result.exit_code == 2
    assert isinstance(result.exception, SystemExit)


@pytest.mark.parametrize("arguments", [["Bad/Object", "--record-id", _RECORD_ID], ["Account", "--record-id", "bad"]])
def test_cli_rejects_invalid_identifiers_before_client_construction(arguments: list[str]) -> None:
    with patch("fieldkit.commands.sf.schema.SFDirectClient") as client_class:
        result = CliRunner().invoke(cli, arguments)
    assert result.exit_code == 2
    assert not client_class.called


@pytest.mark.parametrize(
    "error,expected",
    [
        (SFAuthError("expired"), 2),
        (SFDataAccessError("forbidden"), 3),
        (SFNotFoundError("missing"), 3),
        (SFAPIError("failed"), 1),
    ],
)
def test_cli_maps_auth_not_found_and_api_failures(error: Exception, expected: int) -> None:
    client = MagicMock()
    client.describe_sobject.side_effect = error
    with (
        patch("fieldkit.commands.sf.schema.get_sf_session_id", return_value="sid"),
        patch("fieldkit.commands.sf.schema.get_sf_rest_base_url", return_value="https://sf.example.com"),
        patch("fieldkit.commands.sf.schema.SFDirectClient") as client_class,
    ):
        client_class.return_value.__enter__.return_value = client
        result = CliRunner().invoke(cli, ["Account", "--record-id", _RECORD_ID])
    assert result.exit_code == expected
