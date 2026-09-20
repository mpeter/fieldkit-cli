"""auth/google.py — standalone entry point for the Gmail OAuth consent flow.

Delegates entirely to ``fieldkit.gmail.auth.get_gmail_service()``,
which already resolves credentials, runs the interactive consent flow when
needed, and caches the token — this command just gives that flow an explicit
name instead of triggering it implicitly as a ``gmail sync`` side effect.
"""

import click

from fieldkit.cli_exit import EXIT_AUTH
from fieldkit.errors import GmailAuthError

LOG_PREFIX = "[auth-google]"


def get_gmail_service() -> object:
    """Load the Google integration only when authentication is invoked."""
    from fieldkit.gmail.auth import get_gmail_service as load_service

    return load_service()


@click.command(name="google")
def auth_google_cmd() -> None:
    """Authenticate with Google OAuth for Gmail access.

    Runs the same credential resolution and consent flow that ``gmail sync``
    falls back to automatically — use this to authenticate up front instead
    of waiting for the first sync to prompt.
    """
    try:
        get_gmail_service()
    except GmailAuthError as exc:
        click.echo(f"{LOG_PREFIX} Auth error: {exc}", err=True)
        raise SystemExit(EXIT_AUTH) from exc

    click.echo(f"{LOG_PREFIX} Google auth active.", err=True)
