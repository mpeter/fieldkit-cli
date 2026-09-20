"""sf_pipeline set-next-steps — Write Next_Steps__c on a Salesforce Opportunity.

Updates a single field on one opportunity record via PATCH. Prints the
current value before writing so you can confirm the change.  Without
--confirm the command previews the change without touching Salesforce.

Usage:
    fieldkit sf set-next-steps <opp_id> <text>
    fieldkit sf set-next-steps --confirm <opp_id> <text>
"""

import json

import click

from fieldkit.cli_exit import EXIT_AUTH, EXIT_PARTIAL
from fieldkit.cli_registry import declare_write
from fieldkit.commands.sf._util import stdin_is_interactive
from fieldkit.config import get_sf_rest_base_url, get_sf_session_id
from fieldkit.sf.client import SFAPIError, SFAuthError, SFDirectClient, SFNotFoundError

LOG_PREFIX = "[sf-set-next-steps]"

_NEXT_STEPS_FIELD = "Next_Steps__c"


@declare_write("external")
@click.command(name="set-next-steps", context_settings={"ignore_unknown_options": True})
@click.argument("opp_id")
@click.argument("text", nargs=-1, type=click.UNPROCESSED)
@click.option(
    "--confirm",
    is_flag=True,
    default=False,
    help="Actually write to Salesforce. Without this flag the command only previews the change.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the write outcome as JSON.")
def cli(opp_id: str, text: tuple[str, ...], confirm: bool, as_json: bool) -> None:
    """Write the Next Steps field on a Salesforce Opportunity.

    OPP_ID is the 15- or 18-character Salesforce Opportunity ID.
    TEXT is the new value for the Next Steps field.

    By default this command is a dry-run — it shows the current and proposed
    values without touching Salesforce. Pass --confirm to actually write.
    """
    # nargs=-1 collects all remaining tokens; join them back into a single string.
    # An explicit guard is needed because nargs=-1 makes the argument optional at
    # the Click level (empty tuple rather than a missing-arg error).
    joined = " ".join(text)
    # historic regression: reject empty or whitespace-only TEXT before any preview or API call.
    if not joined.strip():
        raise click.UsageError("TEXT argument is required")

    sid = get_sf_session_id()
    if not sid:
        click.echo(f"{LOG_PREFIX} No SF session found. Run: fieldkit auth sf", err=True)
        raise SystemExit(EXIT_AUTH)

    base_url = get_sf_rest_base_url()

    with SFDirectClient(session_id=sid, base_url=base_url) as client:
        # Fetch current value first (public API, explicit field list)
        try:
            data = client.fetch_record(opp_id, fields=f"Id,Name,{_NEXT_STEPS_FIELD}")
        except SFAuthError as exc:
            click.echo(f"{LOG_PREFIX} {exc}", err=True)
            raise
        except SFNotFoundError:
            # historic regression: surface a clean error when the opportunity ID is not found
            # in Salesforce rather than propagating a raw exception traceback.
            click.echo(f"ERROR: Opportunity {opp_id!r} not found in Salesforce.", err=True)
            raise SystemExit(EXIT_PARTIAL) from None
        except SFAPIError as exc:
            click.echo(f"{LOG_PREFIX} Failed to fetch current record: {exc}", err=True)
            raise SystemExit(EXIT_PARTIAL) from None

        opp_name = data.get("Name", opp_id)
        current = data.get(_NEXT_STEPS_FIELD) or ""

        def _emit(outcome: str, written: bool) -> None:
            click.echo(
                json.dumps(
                    {
                        "opp_id": opp_id,
                        "opp_name": opp_name,
                        "field": _NEXT_STEPS_FIELD,
                        "current": current,
                        "new_value": joined,
                        "written": written,
                        "outcome": outcome,
                    },
                    indent=2,
                    default=str,
                )
            )

        if not as_json:
            click.echo(f"Opportunity : {opp_name} ({opp_id})")
            click.echo(f"Current     : {current or '(empty)'}")
            click.echo(f"New value   : {joined}")

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

        try:
            client.update_opportunity_fields(opp_id, {_NEXT_STEPS_FIELD: joined})
        except SFAuthError as exc:
            click.echo(f"{LOG_PREFIX} Auth error: {exc}", err=True)
            raise
        except SFAPIError as exc:
            click.echo(f"{LOG_PREFIX} API error: {exc}", err=True)
            raise SystemExit(EXIT_PARTIAL) from None

        if as_json:
            _emit("written", written=True)
            return
        click.echo("✓ Next Steps updated.")
