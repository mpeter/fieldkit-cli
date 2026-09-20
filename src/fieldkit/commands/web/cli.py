"""fieldkit web — Local web dashboard over fieldkit data.

Usage:
    fieldkit web serve                 Serve on http://127.0.0.1:6096
    fieldkit web serve --port 8000
    fieldkit web token                 Generate and store a bearer token
"""

import json
import secrets
from pathlib import Path

import click

from fieldkit.cli_exit import cli_main

_DEFAULT_TOKEN_PATH = Path("~/.config/fieldkit/web-token").expanduser()


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    """Local web dashboard — brief, pipeline, alerts, chat (PWA-installable)."""


@cli.command("serve")
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind address.")
@click.option("--port", default=6096, show_default=True, type=int, help="Bind port.")
@click.option(
    "--token-file",
    type=click.Path(path_type=Path),
    default=None,
    help="Read a bearer token from this file and require it on all API and write requests. "
    "The listener remains loopback-only. Generate one with: fieldkit web token",
)
def serve(host: str, port: int, token_file: Path | None) -> None:
    """Run the web server (blocking; Ctrl-C to stop).

    Serves the morning brief, pipeline health board, watcher alerts,
    and a fieldkit-context chat at http://<host>:<port>/. The page is
    installable as a PWA (add to home screen on mobile).
    """
    with cli_main():
        from fieldkit.errors import WebDataError
        from fieldkit.web.server import serve as run_server

        token: str | None = None
        if token_file is not None:
            resolved = token_file.expanduser()
            if not resolved.is_file():
                raise WebDataError(f"token file not found: {resolved} — generate one with: fieldkit web token")
            token = resolved.read_text(encoding="utf-8").strip()
            if not token:
                # An empty token would make auth a no-op on a public bind.
                raise WebDataError(f"token file is empty: {resolved} — regenerate with: fieldkit web token")

        click.echo(f"fieldkit web → http://{host}:{port}/  (Ctrl-C to stop)")
        run_server(host=host, port=port, token=token)


@cli.command("token")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable JSON output.")
def token(as_json: bool) -> None:
    """Generate a bearer token and store it at ~/.config/fieldkit/web-token.

    Use with: fieldkit web serve --token-file ~/.config/fieldkit/web-token
    Clients must then send: Authorization: Bearer <token>
    """
    with cli_main():
        value = secrets.token_urlsafe(32)
        _DEFAULT_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Create with 0600 BEFORE writing — write_text-then-chmod leaves a
        # world-readable window (and a 644 file if interrupted between the two).
        _DEFAULT_TOKEN_PATH.touch(mode=0o600)
        _DEFAULT_TOKEN_PATH.chmod(0o600)  # touch() is a no-op on an existing file
        _DEFAULT_TOKEN_PATH.write_text(value + "\n", encoding="utf-8")
        if as_json:
            # The path, never the token: the 0600 file is the one place the
            # secret lives, and stdout is logged, piped, and scrolled back.
            click.echo(json.dumps({"token_path": str(_DEFAULT_TOKEN_PATH), "mode": "0600"}, indent=2, default=str))
            return
        click.echo(f"Token written to {_DEFAULT_TOKEN_PATH} (mode 600).")
        click.echo("Serve with:  fieldkit web serve --token-file ~/.config/fieldkit/web-token")
