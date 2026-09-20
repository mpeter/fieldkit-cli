"""fieldkit init CLI group — first-run configuration wizard."""

from pathlib import Path

import click

from fieldkit.commands.init.minimal import initialize_minimal
from fieldkit.commands.init.wizard import _run_wizard


def _select_initialization(ctx: click.Context, answers: Path | None, minimal_home: Path | None) -> int | None:
    """Validate the requested initialization mode and return its exit code."""
    if ctx.invoked_subcommand is not None:
        if answers is not None or minimal_home is not None:
            raise click.UsageError("--answers and --minimal cannot be used with an init subcommand")
        return None
    if answers is not None and minimal_home is not None:
        raise click.UsageError("--answers and --minimal are mutually exclusive")
    return initialize_minimal(minimal_home) if minimal_home is not None else _run_wizard(answers)


@click.group(
    name="init",
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
    invoke_without_command=True,
)
@click.option(
    "--answers",
    type=click.Path(exists=True, dir_okay=False, path_type=Path, resolve_path=True),
    help="Initialize without prompts using answers from a YAML file.",
)
@click.option(
    "--minimal",
    "minimal_home",
    type=click.Path(file_okay=False, path_type=Path, resolve_path=True),
    metavar="PATH",
    help="Create a generic offline workspace at PATH without prompts or integrations.",
)
@click.pass_context
def cli(ctx: click.Context, answers: Path | None, minimal_home: Path | None) -> None:
    """First-run configuration wizard. Safe to re-run to update settings."""
    code = _select_initialization(ctx, answers, minimal_home)
    if code is None:
        return
    ctx.exit(code)


from fieldkit.commands.init.migrate import migrate_cmd  # noqa: E402

cli.add_command(migrate_cmd)
