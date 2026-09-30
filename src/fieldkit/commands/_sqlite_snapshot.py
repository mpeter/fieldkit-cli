"""Private process boundary for the isolated SQLite snapshot worker."""

import json
import sys

import click

from fieldkit._sqlite_snapshot_worker import create_snapshot
from fieldkit.cli_exit import EXIT_DATA, cli_main


def main() -> None:
    """Emit one bounded typed result and use the canonical exit boundary."""
    with cli_main():
        status = create_snapshot(sys.argv[1:])
        click.echo(json.dumps({"status": status}, sort_keys=True))
        if status != "ready":
            raise click.exceptions.Exit(EXIT_DATA)


if __name__ == "__main__":
    main()
