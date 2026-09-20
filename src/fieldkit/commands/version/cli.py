"""fieldkit version CLI group — version info and feature introspection."""

import click

from fieldkit.commands.version import main as _main


@click.command(
    name="version",
    context_settings={"help_option_names": ["-h", "--help"], "allow_extra_args": True, "ignore_unknown_options": True},
)
@click.option("--features", "-f", is_flag=True, help="Show active capabilities.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
def cli(features: bool, as_json: bool) -> None:
    """Print version, Python, and platform info.

    Use --features to see active capabilities (CLI groups, skills, services).
    Use --json for machine-readable output.
    """
    argv: list[str] = []
    if features:
        argv.append("--features")
    if as_json:
        argv.append("--json")
    raise SystemExit(_main(argv))
