"""Preview-first Google Tasks commands."""

import json
from typing import Literal

import click

from fieldkit.cli_registry import declare_write
from fieldkit.gtask.client import GTaskValidationError, TaskMutation, complete_task, create_task


def _emit(result: TaskMutation, *, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(result.to_dict(), sort_keys=True))
    elif result.confirmed:
        click.echo(f"Task {result.task_id} {result.operation}d.")
    else:
        click.echo(f"Preview: task {result.operation}; no write occurred. Pass --confirm to apply it.")


def _reject_conflicting_flags(dry_run: bool, confirm: bool) -> None:
    if dry_run and confirm:
        raise click.UsageError("--dry-run and --confirm are mutually exclusive")


@click.group()
def cli() -> None:
    """Preview and apply Google Tasks changes."""


@declare_write("external")
@cli.command("create")
@click.argument("title")
@click.option("--section", type=click.Choice(["today", "active"]), required=True)
@click.option("--account")
@click.option("--due", metavar="YYYY-MM-DD")
@click.option("--dry-run", is_flag=True, help="Preview without writing (the default).")
@click.option("--confirm", is_flag=True, help="Create the task.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON only.")
def create(
    title: str,
    section: Literal["today", "active"],
    account: str | None,
    due: str | None,
    dry_run: bool,
    confirm: bool,
    as_json: bool,
) -> None:
    """Preview or create a task named TITLE."""
    _reject_conflicting_flags(dry_run, confirm)
    try:
        result = create_task(title, section=section, account=account, due=due, confirm=confirm)
    except GTaskValidationError as exc:
        raise click.BadParameter(str(exc)) from exc
    _emit(result, as_json=as_json)


@declare_write("external")
@cli.command("complete")
@click.argument("task_id")
@click.option("--dry-run", is_flag=True, help="Preview without writing (the default).")
@click.option("--confirm", is_flag=True, help="Complete the task.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON only.")
def complete(task_id: str, dry_run: bool, confirm: bool, as_json: bool) -> None:
    """Preview or complete TASK_ID."""
    _reject_conflicting_flags(dry_run, confirm)
    try:
        result = complete_task(task_id, confirm=confirm)
    except GTaskValidationError as exc:
        raise click.BadParameter(str(exc)) from exc
    _emit(result, as_json=as_json)
