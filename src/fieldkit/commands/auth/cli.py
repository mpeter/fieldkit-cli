"""fieldkit auth CLI group — Salesforce, Google, and ShadowBot credential setup."""

import click

from fieldkit.commands._lazy import LazyCommand, make_lazy_group
from fieldkit.config.optional_dependencies import GOOGLE_IMPORT_ROOTS

_COMMANDS: dict[str, str | LazyCommand] = {
    "backstory": LazyCommand(
        "fieldkit.commands.auth.backstory",
        attribute="auth_backstory_cmd",
        description="Authenticate Backstory through MCPJungle's browser OAuth flow.",
    ),
    "google": LazyCommand(
        "fieldkit.commands.auth.google",
        attribute="auth_google_cmd",
        profile="google",
        import_roots=GOOGLE_IMPORT_ROOTS,
        description="Authenticate with Google OAuth for Gmail access.",
    ),
    "sf": LazyCommand(
        "fieldkit.commands.auth.sf",
        attribute="auth_sf_cmd",
        description="Authenticate the Salesforce session via a sid cookie.",
    ),
    "shadowbot": LazyCommand(
        "fieldkit.commands.auth.shadowbot",
        attribute="auth_shadowbot_cmd",
        description="Show ShadowBot auth status, or inject a refresh token.",
    ),
}

_LazyGroup = make_lazy_group(_COMMANDS, command_prefix="auth")


@click.group(
    name="auth",
    cls=_LazyGroup,
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
)
def cli() -> None:
    """Authenticate fieldkit integrations."""
