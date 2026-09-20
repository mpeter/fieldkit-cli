"""Shared lazy-loading Click group for fieldkit command groups.

implementation change: Replaces copy-pasted _LazyGroup classes in sf, gmail, and pursuit
CLI modules with a single canonical implementation.

Usage in a command group's cli.py::

    import importlib
    from fieldkit.commands._lazy import make_lazy_group

    _COMMANDS = {
        "some-cmd": "fieldkit.commands.mygroup.some_cmd",
    }

    _LazyGroup = make_lazy_group(_COMMANDS)
"""

import importlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import click

from fieldkit.config.optional_dependencies import (
    SKIP_OPTIONAL_PROFILE_CHECKS_META_KEY,
    require_optional_profile,
)


@dataclass(frozen=True)
class LazyCommand:
    """Declarative import boundary for one lazily loaded Click command."""

    module_path: str
    attribute: str = "cli"
    profile: str | None = None
    import_roots: tuple[str, ...] = ()
    description: str | None = None


def _load_command(entry: str | LazyCommand, ctx: click.Context, command: str) -> click.Command:
    """Load one declared command after enforcing its execution-time profile."""
    spec = LazyCommand(entry) if isinstance(entry, str) else entry
    introspecting = ctx.meta.get(SKIP_OPTIONAL_PROFILE_CHECKS_META_KEY) is True
    if spec.profile is not None and not introspecting:
        require_optional_profile(command, spec.profile, spec.import_roots)
    mod: Any = importlib.import_module(spec.module_path)
    cli_obj: click.Command = getattr(mod, spec.attribute)
    return cli_obj


def make_lazy_group(commands: Mapping[str, str | LazyCommand], *, command_prefix: str = "") -> type[click.Group]:
    """Return a _LazyGroup subclass bound to the given commands dict.

    Args:
        commands: Mapping of command name → dotted module path. The module at
                  each path must expose a ``cli`` attribute that is a
                  ``click.Command`` (or ``click.Group``).

    Returns:
        A new ``click.Group`` subclass that lazy-imports the command module
        on first access. Each group module gets its own class so the
        ``commands`` closure is isolated.
    """

    class _LazyGroup(click.Group):
        """Click group that imports submodule commands only when invoked."""

        def list_commands(self, ctx: click.Context) -> list[str]:
            return sorted(commands)

        def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
            descriptions = {
                name: entry.description
                for name, entry in commands.items()
                if isinstance(entry, LazyCommand) and entry.description is not None
            }
            if len(descriptions) != len(commands):
                super().format_commands(ctx, formatter)
                return
            with formatter.section("Commands"):
                formatter.write_dl([(name, descriptions[name]) for name in sorted(descriptions)])

        def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
            entry = commands.get(cmd_name)
            if entry is None:
                return None
            return _load_command(entry, ctx, f"{command_prefix} {cmd_name}".strip())

    return _LazyGroup
