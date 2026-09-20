"""fieldkit shadowbot CLI group — ShadowBot sales assistant.

Subcommands:
  query   Send a prompt to ShadowBot and stream the response

Auth is handled by ``fieldkit auth shadowbot``, not this group.
"""

import json
import math
import sys

import click

from fieldkit.cli_exit import EXIT_AUTH, EXIT_PARTIAL
from fieldkit.cli_registry import declare_write
from fieldkit.config import TIMEOUT_SHADOWBOT_QUERY
from fieldkit.shadowbot import auth
from fieldkit.shadowbot import client as client_module
from fieldkit.shadowbot.auth import ShadowbotAuthError
from fieldkit.shadowbot.client import ShadowbotQueryError

# ---------------------------------------------------------------------------
# Display constants
# ---------------------------------------------------------------------------

UNVERIFIED_HEADER = (
    "[shadowbot] Unverified narration — ShadowBot is an LLM assistant, "
    "not a Salesforce system of record. Verify field values independently."
)
UNVERIFIED_FOOTER = "[shadowbot] End of response. Not SF-verified."


def _positive_finite_timeout(_ctx: click.Context, _param: click.Parameter, value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        raise click.BadParameter("must be a finite positive number of seconds")
    return value


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


@click.group(
    name="shadowbot",
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
    invoke_without_command=True,
)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """ShadowBot sales assistant — query subcommand. Auth: fieldkit auth shadowbot."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        ctx.exit(1)


# ---------------------------------------------------------------------------
# query command
# ---------------------------------------------------------------------------


# POSTs /threads to create a conversation thread (shadowbot/client.py), which is
# the only remote state this command creates. Classified read-only deliberately:
# D3's "external write" means a mutation of a system of record — Salesforce,
# GitHub, Google, Slack — and an ephemeral chat thread is how the question is
# asked, not a record that anything downstream reads back. Declared rather than
# left to inference so the judgement is on the record and can be argued with.
@declare_write("read-only")
@cli.command("query")
@click.argument("prompt", nargs=-1, required=False)
@click.option(
    "--timeout",
    type=float,
    callback=_positive_finite_timeout,
    default=TIMEOUT_SHADOWBOT_QUERY,
    metavar="SECS",
    help=f"Total query deadline in seconds (default: {TIMEOUT_SHADOWBOT_QUERY})",
)
@click.option(
    "--new",
    "new_thread",
    is_flag=True,
    help="Start a new conversation thread (don't reuse previous context)",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable JSON output.")
def cmd_query(prompt: tuple[str, ...], timeout: float, new_thread: bool, as_json: bool) -> None:
    """Send a prompt to ShadowBot and stream the response.

    The prompt may be passed as positional arguments or piped via stdin when
    stdin is not a TTY:

      echo "What are my open opportunities?" | fieldkit shadowbot query
    """
    # Resolve prompt from args or stdin
    prompt_text = " ".join(prompt)
    if not prompt_text:
        if not sys.stdin.isatty():
            prompt_text = sys.stdin.read().strip()
        if not prompt_text:
            click.echo("Error: prompt required", err=True)
            raise SystemExit(EXIT_PARTIAL)

    try:
        token = auth.get_token()
        response = client_module.query(
            prompt_text,
            token=token,
            timeout=timeout,
            new_thread=new_thread,
        )
    except ShadowbotAuthError as exc:
        click.echo(f"Auth required: {exc}", err=True)
        raise SystemExit(EXIT_AUTH) from exc
    except ShadowbotQueryError as exc:
        click.echo(f"Query failed: {exc}", err=True)
        raise SystemExit(EXIT_PARTIAL) from exc

    if as_json:
        # The unverified caveat travels in the document: a machine consumer that
        # never sees the prose banner still has to know this is not SF-verified.
        click.echo(
            json.dumps(
                {
                    "content": response.content,
                    "verified": False,
                    "notice": UNVERIFIED_HEADER,
                    "new_thread": new_thread,
                },
                indent=2,
                default=str,
            )
        )
        return

    click.echo(UNVERIFIED_HEADER, err=False)
    click.echo()
    click.echo(response.content)
    click.echo()
    click.echo(UNVERIFIED_FOOTER, err=False)
