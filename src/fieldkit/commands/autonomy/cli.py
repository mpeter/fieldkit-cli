"""CLI adapter for fieldkit autonomy status."""

import json
from datetime import UTC, datetime

import click

from fieldkit.autonomy.status import AutonomyStatus, build_status
from fieldkit.cli_exit import cli_main
from fieldkit.cli_registry import declare_write
from fieldkit.config import get_fieldkit_data
from fieldkit.driver.spend import get_daily_developer_spend_total


def _render_status(snapshot: AutonomyStatus) -> None:
    payload = snapshot.to_dict()
    click.echo("Autonomy status")
    for name in ("health", "driver", "admission", "spend"):
        source = payload[name]
        assert isinstance(source, dict)
        click.echo(f"  {name}: {source['state']} — {source['detail']}")
    next_action = payload["next_action"]
    assert isinstance(next_action, dict)
    click.echo(f"Next action: {next_action['kind']} — {next_action['reason']}")


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    """Read-only evidence about fieldkit autonomous operation."""


@declare_write("read-only")
@cli.command("status")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit one machine-readable JSON document.")
def status(as_json: bool) -> None:
    """Summarize health, driver, admission, and spend evidence without acting."""
    with cli_main():
        snapshot = build_status(
            get_fieldkit_data(),
            now=datetime.now(UTC),
            spend_reader=get_daily_developer_spend_total,
        )
        if as_json:
            click.echo(json.dumps(snapshot.to_dict(), indent=2, sort_keys=True))
            return
        _render_status(snapshot)
