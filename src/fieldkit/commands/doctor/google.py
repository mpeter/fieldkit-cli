"""fieldkit doctor google — Google OAuth credential health check (gmail + docs + drive)."""

from pathlib import Path

import click

from fieldkit.commands.doctor._result import DoctorResult
from fieldkit.config import ConfigError, get_fieldkit_home, get_google_token_path, resolve_oauth_credentials
from fieldkit.config.dotenv import load_dotenv_safe
from fieldkit.errors import GmailAuthError, GoogleCredentialRefreshRetryableError
from fieldkit.google_oauth import refresh_google_credentials


def check_google() -> DoctorResult:
    """Check Google OAuth credentials using the same resolution path as get_gmail_service().

    Short-circuit: if the token file already exists, the credentials used to create it
    were functional at auth time — reported healthy without probing env vars (D1 from
    design.md). When the token file is absent, load the data-root .env (matching the
    resolution order in get_gmail_service()) before checking env vars.
    """
    token_path = get_google_token_path()
    if token_path.is_file():
        try:
            from google.auth.exceptions import TransportError
            from google.oauth2.credentials import Credentials

            creds = Credentials.from_authorized_user_file(str(token_path))  # type: ignore[no-untyped-call]
            expired = bool(creds.expired)
            refresh_token = creds.refresh_token
            valid = bool(creds.valid)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            return DoctorResult(
                "google",
                healthy=False,
                failure_kind="data",
                configured=True,
                message="token file is invalid or unreadable",
            )
        if expired and refresh_token:
            try:
                refresh_google_credentials(creds, token_path)
                return DoctorResult("google", healthy=True, configured=True, message="token refreshed successfully")
            except GoogleCredentialRefreshRetryableError:
                return DoctorResult(
                    "google",
                    healthy=False,
                    failure_kind="retryable",
                    configured=True,
                    message="token refresh is temporarily unavailable — retry later",
                )
            except GmailAuthError:
                return DoctorResult(
                    "google",
                    healthy=False,
                    failure_kind="auth",
                    configured=True,
                    message="token refresh was rejected — run 'fieldkit auth google'",
                )
            except (ConnectionError, RuntimeError, TimeoutError, TransportError):
                return DoctorResult(
                    "google",
                    healthy=False,
                    failure_kind="retryable",
                    configured=True,
                    message="token refresh is temporarily unavailable — retry later",
                )
            except (KeyError, OSError, TypeError, ValueError):
                return DoctorResult(
                    "google",
                    healthy=False,
                    failure_kind="data",
                    configured=True,
                    message="token file could not be updated safely",
                )
        if valid:
            return DoctorResult("google", healthy=True, configured=True, message="token valid")
        return DoctorResult(
            "google",
            healthy=False,
            failure_kind="auth",
            configured=True,
            message="token expired and no refresh token — run 'fieldkit auth google'",
        )

    try:
        data_root = get_fieldkit_home()
        env_file = data_root / ".env"
    except ConfigError:
        env_file = Path(".env")
    load_dotenv_safe(dotenv_path=env_file, override=False)

    client_id, client_secret = resolve_oauth_credentials()
    if client_id and client_secret:
        return DoctorResult(
            "google",
            healthy=False,
            failure_kind="auth",
            configured=True,
            message="credentials set but no token yet — run 'fieldkit auth google'",
        )

    missing = []
    if not client_id:
        missing.append("GOOGLE_OAUTH_CLIENT_ID")
    if not client_secret:
        missing.append("GOOGLE_OAUTH_CLIENT_SECRET")
    return DoctorResult(
        "google",
        healthy=False,
        failure_kind="auth",
        configured=False,
        message=f"{', '.join(missing)} not set — run 'fieldkit auth google'",
    )


@click.command(name="google", context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit result as JSON.")
def doctor_google_cmd(as_json: bool) -> None:
    """Check Google OAuth credential health (gmail + docs + drive).

    Exits 0 when healthy, 1 for retryable provider failures, 2 for authentication,
    and 3 for invalid or unsafe local token data.
    """
    result = check_google()
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
    raise SystemExit(result.exit_code)
