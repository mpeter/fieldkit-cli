"""auth/shadowbot.py — ShadowBot Keycloak token status and recovery."""

import json
import sys
from pathlib import Path

import click

from fieldkit.cli_exit import EXIT_AUTH
from fieldkit.protected_input import SecretInputError, read_secret_file
from fieldkit.shadowbot import auth
from fieldkit.shadowbot.auth import ShadowbotAuthError


def _status_payload() -> dict[str, object]:
    """Return credential-safe ShadowBot health fields for automation."""
    from fieldkit.commands.doctor.shadowbot import check_shadowbot

    result = check_shadowbot()
    state = "authenticated" if result.healthy else "reauthorization-required" if result.configured else "unavailable"
    return {"service": result.service, "state": state, "configured": result.configured, "authenticated": result.healthy}


def stdin_is_interactive() -> bool:
    """Return whether a human can safely answer a credential prompt."""
    return sys.stdin.isatty()


def _prompt_for_refresh_token() -> str:
    """Guide a TTY operator to securely paste a ShadowBot refresh token."""
    click.echo("ShadowBot authorization needs to be renewed.", err=True)
    click.echo("Open the ShadowBot web app and complete SSO if prompted.", err=True)
    click.echo("In DevTools, open Network, refresh the page, and find the token refresh request.", err=True)
    click.echo("Copy only its refresh token value; it will not be displayed or logged.", err=True)
    try:
        return str(click.prompt("Paste refresh token", hide_input=True, err=True)).strip()
    except (click.Abort, EOFError, KeyboardInterrupt):
        click.echo("ShadowBot authorization cancelled.", err=True)
        raise SystemExit(0) from None


def _emit_json_status(refresh_token_file: Path | None) -> None:
    """Emit automation status, rejecting ambiguous credential injection."""
    if refresh_token_file is not None:
        raise click.UsageError("--json cannot be combined with --refresh-token-file")
    click.echo(json.dumps(_status_payload(), sort_keys=True))


def _inject_refresh_token(refresh_token: str, *, recovered: bool) -> None:
    """Save a refresh token and normalize its authentication error exit."""
    try:
        auth.inject_refresh_token(refresh_token)
    except ShadowbotAuthError as exc:
        click.echo(f"Auth error: {exc}", err=True)
        raise SystemExit(EXIT_AUTH) from exc
    if recovered:
        click.echo("✓ ShadowBot refresh token validated and saved.")
        return
    click.echo(f"✓ token saved to {auth.get_token_path()}")


def _recover_authentication(error: ShadowbotAuthError) -> None:
    """Guide an attached human through recovery without prompting automation."""
    if not stdin_is_interactive():
        click.echo(f"Auth required: {error}", err=True)
        click.echo("Reauthorize interactively with: fieldkit auth shadowbot", err=True)
        click.echo(
            "Automation may use an owner-only token file with: fieldkit auth shadowbot --refresh-token-file PATH",
            err=True,
        )
        raise SystemExit(EXIT_AUTH) from error
    _inject_refresh_token(_prompt_for_refresh_token(), recovered=True)


def _report_or_recover_status() -> None:
    """Report an active token or attempt interactive recovery after auth failure."""
    try:
        auth.get_token()
    except ShadowbotAuthError as exc:
        _recover_authentication(exc)
        return
    click.echo("✓ auth active")


@click.command(name="shadowbot")
@click.option(
    "--refresh-token-file",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Read a Keycloak refresh token from an owner-only regular file.",
)
@click.option(
    "--json", "as_json", is_flag=True, default=False, help="Emit credential-safe authentication health as JSON."
)
def auth_shadowbot_cmd(refresh_token_file: Path | None, as_json: bool) -> None:
    """Show ShadowBot auth status, or inject a refresh token.

    Without flags: checks whether a valid token is available and prints
    ``✓ auth active`` on success.

    With ``--refresh-token-file PATH``: reads an owner-only token file, saves
    the token to disk, and prints the path on stdout.
    """
    if as_json:
        _emit_json_status(refresh_token_file)
        return

    if refresh_token_file is not None:
        try:
            _inject_refresh_token(
                read_secret_file(refresh_token_file, label="ShadowBot refresh token"), recovered=False
            )
        except SecretInputError as exc:
            raise click.UsageError(str(exc)) from exc
        return

    _report_or_recover_status()
