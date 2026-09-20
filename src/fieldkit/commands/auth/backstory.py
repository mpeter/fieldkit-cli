"""Backstory authentication command."""

import click

from fieldkit.backstory.auth import authenticate
from fieldkit.cli_registry import declare_write


@declare_write(
    "external",
    confirm_exempt="invocation is the explicit interactive request to replace the canonical Backstory registration",
)
@click.command(name="backstory")
def auth_backstory_cmd() -> None:
    """Authenticate Backstory through MCPJungle's browser OAuth flow.

    MCPJungle stores and refreshes the upstream token. Rerun this command to
    replace the Backstory registration and authorize again.
    """
    click.echo("Replacing the Backstory registration; complete authorization in the browser.", err=True)
    authenticate()
    click.echo("✓ Backstory auth active.")
