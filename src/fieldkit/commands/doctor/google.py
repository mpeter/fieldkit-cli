"""fieldkit doctor google — Google OAuth credential health check (gmail + docs + drive)."""

import os
from pathlib import Path

import click

from fieldkit.cli_exit import EXIT_AUTH, EXIT_SUCCESS
from fieldkit.commands.doctor._result import DoctorResult
from fieldkit.config import ConfigError, get_fieldkit_home, get_google_token_path
from fieldkit.google_oauth import refresh_google_credentials


def _load_env(env_file: Path) -> None:
    """Load a .env file into os.environ if it exists (simple KEY=VALUE parser).

    Handles both plain ``KEY=VALUE`` and shell ``export KEY=VALUE`` syntax.
    """
    if not env_file.is_file():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


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
            from google.oauth2.credentials import Credentials

            creds = Credentials.from_authorized_user_file(str(token_path))  # type: ignore[no-untyped-call]
            if creds.expired and creds.refresh_token:
                refresh_google_credentials(creds, token_path)
                return DoctorResult("google", healthy=True, configured=True, message="token refreshed successfully")
            if creds.valid:
                return DoctorResult("google", healthy=True, configured=True, message="token valid")
            return DoctorResult(
                "google",
                healthy=False,
                configured=True,
                message="token expired and no refresh token — run 'fieldkit auth google'",
            )
        except Exception as exc:  # noqa: BLE001
            return DoctorResult(
                "google",
                healthy=False,
                configured=True,
                message=f"token invalid or refresh failed ({exc}) — run 'fieldkit auth google'",
            )

    try:
        data_root = get_fieldkit_home()
        env_file = data_root / ".env"
    except ConfigError:
        env_file = Path(".env")
    _load_env(env_file)

    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    if client_id and client_secret:
        return DoctorResult(
            "google",
            healthy=False,
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
        configured=False,
        message=f"{', '.join(missing)} not set — run 'fieldkit auth google'",
    )


@click.command(name="google", context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit result as JSON.")
def doctor_google_cmd(as_json: bool) -> None:
    """Check Google OAuth credential health (gmail + docs + drive).

    Exits 0 when the token is valid or was refreshed, 2 when not configured or expired.
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
    raise SystemExit(EXIT_SUCCESS if result.healthy else EXIT_AUTH)
