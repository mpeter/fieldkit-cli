import importlib
import logging
import sys

import click

from fieldkit.cli_exit import handle_cli_exception
from fieldkit.config.dotenv import load_dotenv_safe
from fieldkit.config.optional_dependencies import (
    SKIP_OPTIONAL_PROFILE_CHECKS_META_KEY,
    require_optional_profile,
)

logging.getLogger("dotenv.main").setLevel(logging.CRITICAL)

_COMMANDS: dict[str, tuple[str, str]] = {
    "auth": ("Salesforce, Google, and ShadowBot credential setup", "fieldkit.commands.auth.cli"),
    "autonomy": ("Read-only autonomous-operation status", "fieldkit.commands.autonomy.cli"),
    "brief": ("Morning brief generator", "fieldkit.commands.brief.cli"),
    "commands": ("Machine-readable command registry — the CLI surface for agents", "fieldkit.commands.commands.cli"),
    "companion": ("Agent companion loop — feed, allowed, run", "fieldkit.commands.companion.cli"),
    "contact": ("Contact discovery, enrichment, and reporting", "fieldkit.commands.contact.cli"),
    "driver": ("Autonomous brief-execution driver loop", "fieldkit.commands.driver.cli"),
    "doctor": ("Health checks for Salesforce, Gmail, Google, and ShadowBot", "fieldkit.commands.doctor.cli"),
    "gmail": ("Gmail cache pipeline", "fieldkit.commands.gmail.cli"),
    "gtask": ("Google Tasks — previewable create and complete actions", "fieldkit.commands.gtask.cli"),
    "golive": ("Deterministic go-live revenue sourcing", "fieldkit.commands.golive.cli"),
    "health": ("Nightly repo-health sensor — senses main, files regressions", "fieldkit.commands.health.cli"),
    "ingest": ("Source provenance and ingestion pipeline", "fieldkit.commands.ingest.cli"),
    "issue": ("GitHub Issues-backed bug/enhancement tracker", "fieldkit.commands.issue.cli"),
    "meeting": ("Pursuit Workbook — Google Docs integration for pursuits", "fieldkit.commands.meeting.cli"),
    "pipeline": ("Pipeline review report generator", "fieldkit.commands.pipeline.cli"),
    "pursuit": ("Pursuit file management", "fieldkit.commands.pursuit.cli"),
    "completion": ("Shell completion script generator (bash/zsh/fish)", "fieldkit.commands.completion.cli"),
    "init": ("First-run configuration wizard", "fieldkit.commands.init.cli"),
    "sf": ("Salesforce pipeline", "fieldkit.commands.sf.cli"),
    "shadowbot": ("ShadowBot sales assistant", "fieldkit.commands.shadowbot.cli"),
    "skill": ("Skill discovery, inspection, and install", "fieldkit.commands.skill.cli"),
    "sync": ("Ordered full data pipeline runner", "fieldkit.commands.datasync.cli"),
    "version": ("Version info and feature introspection", "fieldkit.commands.version.cli"),
    "watch": ("Account health watchers", "fieldkit.commands.watch.cli"),
    "web": ("Local web dashboard — brief, pipeline, alerts, chat", "fieldkit.commands.web.cli"),
}

_OPTIONAL_COMMAND_PROFILES: dict[str, tuple[str, tuple[str, ...]]] = {
    "driver": ("llm", ("agentplatform", "litellm", "openai", "vertexai")),
    "meeting": (
        "google",
        ("google.auth", "google.oauth2", "google_auth_httplib2", "google_auth_oauthlib", "googleapiclient"),
    ),
    "web": ("web", ("fastapi", "uvicorn")),
}


def _require_command_profile(ctx: click.Context, cmd_name: str) -> None:
    """Enforce an optional top-level command profile outside introspection."""
    profile_spec = _OPTIONAL_COMMAND_PROFILES.get(cmd_name)
    if profile_spec is None or ctx.meta.get(SKIP_OPTIONAL_PROFILE_CHECKS_META_KEY) is True:
        return
    profile, import_roots = profile_spec
    require_optional_profile(cmd_name, profile, import_roots)


class _LazyGroup(click.Group):
    """Click Group that lazy-loads subgroups from _COMMANDS on first access."""

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Group | None:
        if cmd_name not in _COMMANDS:
            return None
        _require_command_profile(ctx, cmd_name)
        _description, module_path = _COMMANDS[cmd_name]
        module = importlib.import_module(module_path)
        cli_obj: click.Group = module.cli
        return cli_obj

    def list_commands(self, ctx: click.Context) -> list[str]:
        return sorted(_COMMANDS)

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        with formatter.section("Commands"):
            formatter.write_dl([(name, desc) for name, (desc, _) in sorted(_COMMANDS.items())])


@click.group(
    cls=_LazyGroup,
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
)
@click.pass_context
@click.version_option(
    None,
    "--version",
    "-V",
    package_name="fieldkit-cli",
    prog_name="fieldkit",
    message="%(prog)s %(version)s",
)
def cli(ctx: click.Context) -> None:
    """fieldkit — AE productivity CLI.

    Run 'fieldkit init' to configure fieldkit for the first time.
    Run 'fieldkit version --features' to see active capabilities.
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


def main(argv: list[str] | None = None) -> int:
    """Invoke the CLI. Returns exit code (0 = success)."""
    load_dotenv_safe()
    # implementation change: centralise logging.basicConfig() here instead of in individual
    # group callbacks. Previously gmail/cli.py called it in the group callback —
    # which ran even on --help invocations, before any real command executed.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    args = argv if argv is not None else sys.argv[1:]
    try:
        result = cli.main(args, standalone_mode=False, prog_name="fieldkit")
        return result if type(result) is int else 0
    except click.exceptions.Exit as exc:
        # Exit(None) must not silently become 0 — treat as generic failure (1).
        return exc.exit_code if isinstance(exc.exit_code, int) else 1
    except click.exceptions.UsageError as exc:
        # UsageError (unknown command, bad flag, wrong args) → EXIT_DATA (3).
        # A malformed invocation is a caller data error; retrying without fixing it will not help.
        click.echo(f"Error: {exc.format_message()}", err=True)
        return 3
    except SystemExit as exc:
        # sys.exit(None) is equivalent to exit 0, but an explicit Exit(None) is not
        # a clean exit — treat as failure (1).
        return exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:  # noqa: BLE001  # dispatcher backstop — broad catch is intentional
        # Catches any domain exception that escapes a leaf command not wrapped in cli_main().
        # Groups that do wrap cli_main() (brief, meeting) convert their exceptions to SystemExit
        # before reaching here, so the except SystemExit clause above handles them — no
        # double-mapping occurs. KeyboardInterrupt (BaseException) is NOT caught here and
        # propagates to exit 130. See: fieldkit.cli_exit.handle_cli_exception for the mapping.
        return handle_cli_exception(exc)


if __name__ == "__main__":
    sys.exit(main())
