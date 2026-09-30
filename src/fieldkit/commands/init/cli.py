"""fieldkit init CLI group — first-run configuration wizard."""

import json
import os
from contextlib import redirect_stdout
from pathlib import Path

import click

from fieldkit.cli_registry import declare_write
from fieldkit.commands.init.minimal import initialize_minimal
from fieldkit.commands.init.wizard import _run_wizard


def _select_initialization(answers: Path | None, minimal_home: Path | None, *, dry_run: bool = False) -> int:
    """Validate the requested initialization mode and return its exit code."""
    if answers is not None and minimal_home is not None:
        raise click.UsageError("--answers and --minimal are mutually exclusive")
    if dry_run and answers is None and minimal_home is None:
        raise click.UsageError("--dry-run requires --minimal or --answers")
    if minimal_home is not None:
        return initialize_minimal(minimal_home, dry_run=dry_run)
    return _run_wizard(answers, dry_run=dry_run)


@declare_write("workspace")
@click.group(
    name="init",
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
    invoke_without_command=True,
)
@click.option(
    "--answers",
    type=click.Path(path_type=Path),
    help="Initialize without prompts using answers from a YAML file.",
)
@click.option(
    "--minimal",
    "minimal_home",
    type=click.Path(path_type=Path),
    metavar="PATH",
    help="Create a generic offline workspace at PATH without prompts or integrations.",
)
@click.option("--dry-run", is_flag=True, help="Validate non-interactive initialization without writing files.")
@click.option("--json", "as_json", is_flag=True, help="Emit a payload-free non-interactive result as JSON.")
@click.pass_context
def cli(ctx: click.Context, answers: Path | None, minimal_home: Path | None, dry_run: bool, as_json: bool) -> None:
    """First-run configuration wizard. Safe to re-run to update settings."""
    if as_json:
        if answers is None and minimal_home is None:
            raise click.UsageError("--json requires --minimal or --answers")
        with Path(os.devnull).open("w", encoding="utf-8") as quiet_output, redirect_stdout(quiet_output):
            exit_code = _select_initialization(answers, minimal_home, dry_run=dry_run)
        if exit_code == 0:
            click.echo(
                json.dumps(
                    {
                        "status": "preview" if dry_run else "initialized",
                        "mode": "minimal" if minimal_home is not None else "answers",
                        "dry_run": dry_run,
                    },
                    sort_keys=True,
                )
            )
    else:
        exit_code = _select_initialization(answers, minimal_home, dry_run=dry_run)
    ctx.exit(exit_code)
