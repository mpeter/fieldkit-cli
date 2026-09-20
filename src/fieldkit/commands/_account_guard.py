"""fieldkit.commands._account_guard — Shared CLI-boundary slug validation.

Validates an ``--account`` slug against the configured account list and exits
with EXIT_DATA (3) if the slug is unknown.  Lives in ``commands/`` (not the
domain layer) because it emits user-facing output via Click and raises
SystemExit directly — both are CLI concerns, not domain concerns.

Usage::

    from fieldkit.commands._account_guard import validate_account_slug

    def cli(account: str | None, ...) -> None:
        validate_account_slug(account)
        ...
"""

import click

import fieldkit.config as config
from fieldkit.cli_exit import EXIT_DATA


def validate_account_slug(account: str | None) -> None:
    """Validate *account* against configured accounts; raise SystemExit(3) if unknown.

    A ``None`` value is always accepted (no ``--account`` flag provided).

    Args:
        account: Account slug string from the ``--account`` CLI option, or ``None``.

    Raises:
        SystemExit(3): If *account* is not ``None`` and not in the configured account list.
    """
    if account is None:
        return
    known = config.get_account_names()
    if account not in known:
        slugs = ", ".join(sorted(known)) if known else "(none configured)"
        click.echo(f"Error: unknown account '{account}'. Known slugs: {slugs}", err=True)
        raise SystemExit(EXIT_DATA)
