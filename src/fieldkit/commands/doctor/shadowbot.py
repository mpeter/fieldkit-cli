"""fieldkit doctor shadowbot — ShadowBot OIDC token health check."""

import click

from fieldkit.cli_exit import EXIT_AUTH, EXIT_SUCCESS
from fieldkit.commands.doctor._result import DoctorResult
from fieldkit.shadowbot.auth import ShadowbotAuthError, get_token, get_token_path


def _live_token_result() -> DoctorResult:
    """Return the health result for an already-configured ShadowBot token."""
    try:
        get_token()
    except ShadowbotAuthError as exc:
        return DoctorResult(
            "shadowbot",
            healthy=False,
            configured=True,
            message=f"{exc} — run 'fieldkit auth shadowbot'",
        )
    return DoctorResult("shadowbot", healthy=True, configured=True, message="token valid")


def check_shadowbot() -> DoctorResult:
    """Check ShadowBot auth by resolving a live access token (refreshing if needed)."""
    token_path = get_token_path()
    if not token_path.exists():
        return DoctorResult("shadowbot", healthy=False, configured=False, message="run 'fieldkit auth shadowbot'")
    return _live_token_result()


@click.command(name="shadowbot", context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit result as JSON.")
def doctor_shadowbot_cmd(as_json: bool) -> None:
    """Check ShadowBot OIDC token health.

    Exits 0 when a valid access token can be resolved, 2 when not configured or expired.
    """
    result = check_shadowbot()
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
