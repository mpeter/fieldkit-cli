"""Canonical SQLite predicates for bounded Gmail address headers."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from fieldkit.gmail.addresses import parse_address_header

_ADDRESS_MATCH_FUNCTION = "fieldkit_has_exact_address"
_SINGULAR_ADDRESS_MATCH_FUNCTION = "fieldkit_has_singular_exact_address"
_MESSAGE_ADDRESS_COLUMNS = ("from_addr", "to_addr", "cc_addr")
_ALLOWED_ALIASES = {"", "m.", "m2."}


def _has_exact_address(raw_header: object, target: object) -> int:
    """Return one when *target* is an exact mailbox in *raw_header*."""
    if not isinstance(target, str):
        return 0
    normalized_target = target.strip().casefold()
    if not normalized_target or "\r" in normalized_target or "\n" in normalized_target:
        return 0
    return int(
        any(
            mailbox == normalized_target for _name, mailbox in parse_address_header(raw_header, field="message address")
        )
    )


def _has_singular_exact_address(raw_header: object, target: object) -> int:
    """Return one only for a single parsed mailbox equal to *target*."""
    if not isinstance(target, str):
        return 0
    normalized_target = target.strip().casefold()
    if not normalized_target or "\r" in normalized_target or "\n" in normalized_target:
        return 0
    addresses = parse_address_header(raw_header, field="message address")
    return int(len(addresses) == 1 and addresses[0][1] == normalized_target)


def register_exact_address_matcher(conn: sqlite3.Connection) -> None:
    """Register the bounded exact-address matcher used by Gmail readers."""
    conn.create_function(_ADDRESS_MATCH_FUNCTION, 2, _has_exact_address, deterministic=True)
    conn.create_function(
        _SINGULAR_ADDRESS_MATCH_FUNCTION,
        2,
        _has_singular_exact_address,
        deterministic=True,
    )


def exact_header_address_filter(
    email: str,
    *,
    columns: Sequence[str],
    alias: str = "",
) -> tuple[str, tuple[str, ...]]:
    """Return a canonical exact-mailbox predicate for reviewed message columns."""
    if alias not in _ALLOWED_ALIASES:
        raise ValueError("Unsupported message address alias")
    if not columns or any(column not in _MESSAGE_ADDRESS_COLUMNS for column in columns):
        raise ValueError("Unsupported message address column")
    predicate = " OR ".join(f"{_ADDRESS_MATCH_FUNCTION}({alias}{column}, ?) = 1" for column in columns)
    return f"({predicate})", tuple(email for _column in columns)


def exact_message_address_filter(email: str, *, alias: str = "") -> tuple[str, tuple[str, str, str]]:
    """Return the canonical predicate for any participant in one message."""
    predicate, parameters = exact_header_address_filter(email, columns=_MESSAGE_ADDRESS_COLUMNS, alias=alias)
    return predicate, (parameters[0], parameters[1], parameters[2])


def singular_sender_address_filter(email: str, *, alias: str = "") -> tuple[str, tuple[str]]:
    """Return a predicate requiring exactly one matching sender mailbox."""
    if alias not in _ALLOWED_ALIASES:
        raise ValueError("Unsupported message address alias")
    return f"{_SINGULAR_ADDRESS_MATCH_FUNCTION}({alias}from_addr, ?) = 1", (email,)
