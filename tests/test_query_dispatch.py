"""Tests for _SuggestingGroup.resolve_command in fieldkit.commands.gmail.query.

Covers the custom Click group that converts unknown subcommands into
actionable 'did you mean' suggestions.
"""

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 4F.4 resolve_command
# ---------------------------------------------------------------------------


# ── TestResolveCommand (flattened) ──────────────────────────────────────────


@pytest.mark.unit
def test_resolve_command_resolve_command_known_subcommand() -> None:
    """resolve_command returns the click Command object for a known subcommand."""
    import click

    from fieldkit.commands.gmail.query import cli

    # resilient_parsing=True suppresses NoArgsIsHelpError when args=[].
    ctx = cli.make_context("query", [], resilient_parsing=True)
    name, cmd, _args = cli.resolve_command(ctx, ["person"])
    ctx.close()

    assert name is not None
    assert cmd is not None
    assert isinstance(cmd, click.Command)


@pytest.mark.unit
def test_resolve_command_resolve_command_unknown_raises_usage_error() -> None:
    """resolve_command raises click.UsageError with a suggestion for unknown subcommands."""
    import click

    from fieldkit.commands.gmail.query import cli

    # Build a bare context directly on _SuggestingGroup to avoid NoArgsIsHelpError
    # (triggered by make_context with empty args) while keeping resilient_parsing=False
    # so UsageError propagates normally.
    ctx = click.Context(cli, info_name="query")
    with pytest.raises(click.UsageError, match="is not a query subcommand"):
        cli.resolve_command(ctx, ["totally_unknown_subcommand"])
    ctx.close()
