"""sf_pipeline set-field — Write an approved field on a Salesforce record.

Only fields in the explicit allowlist below may be written. Any attempt to
write an unlisted field is rejected before touching Salesforce.

Usage:
    fieldkit sf set-field <record_id> <field_api_name> <value>
    fieldkit sf set-field --sobject SBQQ__Quote__c <quote_id> <field> <value>
    fieldkit sf set-field --confirm <record_id> <field_api_name> <value>

Run with --list-fields to see all allowed fields per object.
"""

import datetime as _dt
import json
import math
import re
from collections.abc import Callable
from typing import Any, cast

import click

from fieldkit.cli_exit import EXIT_AUTH, EXIT_DATA, EXIT_PARTIAL
from fieldkit.cli_registry import declare_write
from fieldkit.commands.sf._util import stdin_is_interactive
from fieldkit.config import get_sf_rest_base_url, get_sf_session_id
from fieldkit.sf.client import SFAPIError, SFAuthError, SFDirectClient, SFNotFoundError

LOG_PREFIX = "[sf-set-field]"

# ---------------------------------------------------------------------------
# Allowlist — the ONLY fields that may be written via this command.
# Add new fields here after explicit human review and approval.
# ---------------------------------------------------------------------------

ALLOWED_FIELDS: dict[str, dict[str, str]] = {
    "Opportunity": {
        "CloseDate": "Close Date",
        "Next_Steps__c": "Next Steps",
        "Bill_To_Account__c": "Bill To Account #",
        "Country_of_Order__c": "Country of Order",
        "EBS_Account_Name__c": "Bill To EBS Account Name",
        "EBS_Bill_to_Account_Alias__c": "EBS Bill to Account Alias",
        "Identify_Pain_Long__c": "Identify Pain Long",
        "RouteToMarket__c": "Route To Market",
    },
    "SBQQ__Quote__c": {
        "SBQQ__StartDate__c": "Start Date",
        "SBQQ__EndDate__c": "End Date",
        "SBQQ__ExpirationDate__c": "Expires On",
        "Approval_Comments__c": "Business Justification",
        "Gross_Margin__c": "Gross Margin",
    },
}

_NAME_FIELD: dict[str, str] = {
    "Opportunity": "Name",
    "SBQQ__Quote__c": "Name",
}


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_iso_date(value: str) -> str | None:
    """Accept only ISO-8601 calendar dates (YYYY-MM-DD)."""
    stripped = value.strip()
    if not _ISO_DATE_RE.match(stripped):
        return f"'{value}' is not a valid ISO-8601 date (expected YYYY-MM-DD)."
    try:
        _dt.date.fromisoformat(stripped)
    except ValueError:
        return f"'{value}' is not a valid ISO-8601 date (expected YYYY-MM-DD)."
    return None


def _validate_number(value: str) -> str | None:
    """Accept only finite values that parse as a float."""
    try:
        parsed = float(value.strip())
    except ValueError:
        return f"'{value}' is not a valid number."
    if not math.isfinite(parsed):
        return f"'{value}' is not a valid number."
    return None


# Optional per-field validators: callable(value) -> error_str | None
# Absent field = no validation beyond the allowlist.
_FIELD_VALIDATORS: dict[str, dict[str, Callable[[str], str | None]]] = {
    "Opportunity": {
        "CloseDate": _validate_iso_date,
    },
    "SBQQ__Quote__c": {
        "Gross_Margin__c": _validate_number,
    },
}


def _print_field_list() -> None:
    """Print all allowed fields per object and exit."""
    click.echo("Allowed fields per Salesforce object:\n")
    for obj, fields in ALLOWED_FIELDS.items():
        click.echo(f"{obj}:")
        for api, label in fields.items():
            click.echo(f"  {api:<40} {label}")
        click.echo()


def _validate_allowlist(sobject: str, field: str) -> None:
    """Check sobject and field are in the allowlist; exit with code 1 if not."""
    allowed = ALLOWED_FIELDS.get(sobject)
    if allowed is None:
        click.echo(
            f"{LOG_PREFIX} Object '{sobject}' is not in the allowlist.\nAllowed objects: {', '.join(ALLOWED_FIELDS)}",
            err=True,
        )
        raise SystemExit(EXIT_PARTIAL)
    if field not in allowed:
        click.echo(
            f"{LOG_PREFIX} Field '{field}' is not allowed on {sobject}.\n"
            f"Run 'fieldkit sf set-field --list-fields' to see allowed fields.",
            err=True,
        )
        raise SystemExit(EXIT_PARTIAL)


def _fetch_record_data(
    client: SFDirectClient,
    sobject: str,
    record_id: str,
    fetch_fields: str,
) -> dict[str, Any]:
    """Fetch the record from SF; exits with appropriate code on error."""
    try:
        if sobject == "Opportunity":
            # fetch_record() returns OpportunitySObject (TypedDict); cast to
            # dict[str, Any] here because this function mixes Opportunity and
            # generic sobject paths that both return arbitrary field sets.
            return cast(dict[str, Any], client.fetch_record(record_id, fields=fetch_fields))
        return client.fetch_sobject(sobject, record_id, fetch_fields)
    except SFAuthError as exc:
        click.echo(f"{LOG_PREFIX} {exc}", err=True)
        raise
    except SFNotFoundError:
        click.echo(f"{LOG_PREFIX} {sobject} {record_id} not found.", err=True)
        raise SystemExit(EXIT_DATA) from None
    except SFAPIError as exc:
        click.echo(f"{LOG_PREFIX} Fetch failed: {exc}", err=True)
        raise SystemExit(EXIT_PARTIAL) from None


def _write_field(
    client: SFDirectClient,
    sobject: str,
    record_id: str,
    field: str,
    value: str,
    label: str,
    quiet: bool = False,
) -> None:
    """Write the field value to SF; exits with appropriate code on error.

    ``quiet`` suppresses the stdout confirmation line so JSON mode owns stdout.
    """
    try:
        if sobject == "Opportunity":
            client.update_opportunity_fields(record_id, {field: value})
        else:
            client.update_sobject_fields(sobject, record_id, {field: value})
    except SFAuthError as exc:
        click.echo(f"{LOG_PREFIX} Auth error: {exc}", err=True)
        raise
    except SFAPIError as exc:
        click.echo(f"{LOG_PREFIX} API error: {exc}", err=True)
        raise SystemExit(EXIT_PARTIAL) from None
    if not quiet:
        click.echo(f"✓ {field} ({label}) updated.")


def _allowed_fields_help() -> str:
    lines = []
    for obj, fields in ALLOWED_FIELDS.items():
        lines.append(f"\b\n{obj}:")
        for api, label in fields.items():
            lines.append(f"  {api}  ({label})")
    return "\n".join(lines)


@declare_write("external")
@click.command(name="set-field", context_settings={"ignore_unknown_options": True})
@click.argument("record_id", required=False)
@click.argument("field", required=False)
@click.argument("value", required=False, type=click.UNPROCESSED)
@click.option(
    "--sobject",
    default="Opportunity",
    show_default=True,
    help="Salesforce object API name.",
)
@click.option(
    "--confirm",
    is_flag=True,
    default=False,
    help="Actually write to Salesforce. Without this flag the command only previews.",
)
@click.option(
    "--list-fields",
    "list_fields",
    is_flag=True,
    default=False,
    help="List all allowed fields per object and exit.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the write outcome as JSON.")
@click.pass_context
def cli(
    ctx: click.Context,
    record_id: str | None,
    field: str | None,
    value: str | None,
    sobject: str,
    confirm: bool,
    list_fields: bool,
    as_json: bool,
) -> None:
    """Write an approved field on a Salesforce record.

    Only explicitly approved fields may be written. Use --list-fields to see
    the full allowlist. Any other field name is rejected before touching SF.

    \b
    Allowed fields — Opportunity:
      CloseDate                Close Date (YYYY-MM-DD)
      Next_Steps__c            Next Steps
      Bill_To_Account__c       Bill To Account #
      Country_of_Order__c      Country of Order
      EBS_Account_Name__c      Bill To EBS Account Name
      EBS_Bill_to_Account_Alias__c  EBS Bill to Account Alias
      Identify_Pain_Long__c    Identify Pain Long
      RouteToMarket__c         Route To Market

    \b
    Allowed fields — SBQQ__Quote__c:
      SBQQ__StartDate__c       Start Date
      SBQQ__EndDate__c         End Date
      SBQQ__ExpirationDate__c  Expires On
      Approval_Comments__c     Business Justification
      Gross_Margin__c          Gross Margin (number)
    """
    if list_fields:
        if as_json:
            click.echo(json.dumps({"allowed_fields": ALLOWED_FIELDS}, indent=2, default=str))
            return
        _print_field_list()
        return

    if not record_id or not field or not value:
        # historic regression: missing required args → exit 2 (usage error convention)
        click.echo(ctx.get_help())
        ctx.exit(2)

    _validate_allowlist(sobject, field)

    # Run field-level validator if one is registered.
    validators = _FIELD_VALIDATORS.get(sobject, {})
    if field in validators:
        error = validators[field](value)
        if error:
            raise click.BadParameter(error, param_hint=repr(field))

    allowed = ALLOWED_FIELDS[sobject]

    sid = get_sf_session_id()
    if not sid:
        click.echo(f"{LOG_PREFIX} No SF session. Run: fieldkit auth sf", err=True)
        raise SystemExit(EXIT_AUTH)

    base_url = get_sf_rest_base_url()
    name_field = _NAME_FIELD.get(sobject, "Name")
    fetch_fields = f"Id,{name_field},{field}" if field != name_field else f"Id,{field}"

    with SFDirectClient(session_id=sid, base_url=base_url) as client:
        data = _fetch_record_data(client, sobject, record_id, fetch_fields)

        record_name = data.get(name_field, record_id)
        current = data.get(field)
        label = allowed[field]

        def _emit(outcome: str, written: bool) -> None:
            click.echo(
                json.dumps(
                    {
                        "sobject": sobject,
                        "record_id": record_id,
                        "record_name": record_name,
                        "field": field,
                        "label": label,
                        "current": current,
                        "new_value": value,
                        "written": written,
                        "outcome": outcome,
                    },
                    indent=2,
                    default=str,
                )
            )

        if not as_json:
            click.echo(f"Object      : {sobject}")
            click.echo(f"Record      : {record_name} ({record_id})")
            click.echo(f"Field       : {field}  ({label})")
            click.echo(f"Current     : {current!r}")
            click.echo(f"New value   : {value!r}")

        if not confirm:
            if as_json:
                _emit("preview", written=False)
                return
            click.echo("[preview] Pass --confirm to write this to Salesforce.")
            return

        # Physical keyboard gate — only when stdin is a real TTY
        if stdin_is_interactive():
            click.echo()
            response = click.prompt("Type 'yes' to confirm write to Salesforce", default="no")
            if response.strip().lower() != "yes":
                if as_json:
                    _emit("aborted", written=False)
                    return
                click.echo("Aborted — no changes written.")
                return

        _write_field(client, sobject, record_id, field, value, label, quiet=as_json)
        if as_json:
            _emit("written", written=True)
