"""``fieldkit sf schema`` — safe reference for an explicitly supplied Salesforce sample."""

import json

import click

from fieldkit.cli_exit import EXIT_DATA, EXIT_PARTIAL, cli_main
from fieldkit.config import get_sf_rest_base_url, get_sf_session_id
from fieldkit.sf.client import SFAPIError, SFAuthError, SFDataAccessError, SFDirectClient, SFNotFoundError
from fieldkit.sf.schema import (
    MAX_SAMPLE_RECORDS,
    SchemaReference,
    collect_schema_reference,
    is_valid_record_id,
    is_valid_sobject_api_name,
)


class _SObjectApiName(click.ParamType[str]):
    """Validate an sObject API name before it reaches a request URL."""

    name = "sObject API name"

    def convert(self, value: str, param: click.Parameter | None, ctx: click.Context | None) -> str:
        if not is_valid_sobject_api_name(value):
            self.fail("must start with a letter and contain only letters, digits, or underscores.", param, ctx)
        return value


class _RecordId(click.ParamType[str]):
    """Validate a Salesforce record ID before it reaches a request URL."""

    name = "record ID"

    def convert(self, value: str, param: click.Parameter | None, ctx: click.Context | None) -> str:
        if not is_valid_record_id(value):
            self.fail("must be 15 or 18 alphanumeric characters.", param, ctx)
        return value


_SOBJECT_API_NAME = _SObjectApiName()
_RECORD_ID = _RecordId()


@click.command(name="schema", context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("sobject", type=_SOBJECT_API_NAME)
@click.option(
    "--record-id",
    "record_ids",
    type=_RECORD_ID,
    multiple=True,
    required=True,
    metavar="ID",
    help="Explicit Salesforce record ID to inspect. Repeat for each sample.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit the schema reference as JSON.")
def cli(sobject: str, record_ids: tuple[str, ...], as_json: bool) -> None:
    """Render safe metadata and observed population for explicit records only."""
    with cli_main():
        if len(record_ids) > MAX_SAMPLE_RECORDS:
            click.echo(f"ERROR: At most {MAX_SAMPLE_RECORDS} --record-id options may be supplied.", err=True)
            raise SystemExit(EXIT_DATA)
        session_id = get_sf_session_id()
        if not session_id:
            raise SFAuthError("No Salesforce session. Run: fieldkit auth sf")

        try:
            with SFDirectClient(session_id=session_id, base_url=get_sf_rest_base_url()) as client:
                reference = collect_schema_reference(client, sobject, record_ids)
        except (SFDataAccessError, SFNotFoundError) as exc:
            click.echo(f"SF schema reference unavailable: {exc}", err=True)
            raise SystemExit(EXIT_DATA) from None
        except SFAPIError as exc:
            click.echo(f"SF schema request failed: {exc}", err=True)
            raise SystemExit(EXIT_PARTIAL) from None

        _emit_reference(reference, as_json=as_json)


def _emit_reference(reference: SchemaReference, *, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(_reference_payload(reference), sort_keys=True))
        return
    _render_reference(reference)


def _reference_payload(reference: SchemaReference) -> dict[str, object]:
    """Return the value-free schema reference as JSON-safe data."""
    return {
        "sobject_type": reference.sobject_type,
        "sample_count": reference.sample_count,
        "fields": [
            {
                "name": field.name,
                "label": field.label,
                "field_type": field.field_type,
                "population": field.population,
            }
            for field in reference.fields
        ],
    }


def _render_reference(reference: SchemaReference) -> None:
    """Render deterministic value-free schema output."""
    click.echo(f"Object: {reference.sobject_type}")
    click.echo(f"Sampled records: {reference.sample_count}")
    click.echo("Fields:")
    for field in reference.fields:
        click.echo(f"{field.name}\t{field.label}\t{field.field_type}\t{field.population}")
