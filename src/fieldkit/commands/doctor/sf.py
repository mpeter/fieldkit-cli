"""fieldkit doctor sf — Salesforce session health check."""

import click

from fieldkit.cli_exit import EXIT_AUTH, EXIT_SUCCESS
from fieldkit.commands.doctor._result import DoctorResult
from fieldkit.config import get_cookie_file


def _live_session_result() -> DoctorResult:
    """Return the health result for an already-configured Salesforce session."""
    from fieldkit.commands.sf.session_check import check_sf_session

    alive, msg = check_sf_session()
    if alive:
        return DoctorResult("sf", healthy=True, configured=True, message=msg)
    return DoctorResult("sf", healthy=False, configured=True, message=f"{msg} — run 'fieldkit auth sf'")


def check_sf() -> DoctorResult:
    """Check whether a Salesforce session cookie is present and still live."""
    cookie_file = get_cookie_file()
    if not cookie_file.exists():
        return DoctorResult("sf", healthy=False, configured=False, message="run 'fieldkit auth sf'")
    return _live_session_result()


@click.command(name="sf", context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit result as JSON.")
def doctor_sf_cmd(as_json: bool) -> None:
    """Check Salesforce session health.

    Exits 0 when the session is active, 2 when expired or not configured.
    """
    result = check_sf()
    if as_json:
        import json as _json

        click.echo(
            _json.dumps(
                {
                    "service": result.service,
                    "healthy": result.healthy,
                    "configured": result.configured,
                    "message": result.message,
                }
            )
        )
    else:
        click.echo(result.render())
    raise SystemExit(EXIT_SUCCESS if result.healthy else EXIT_AUTH)
