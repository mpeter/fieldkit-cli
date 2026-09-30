"""Canonical bounded parsing for Gmail message address headers."""

from __future__ import annotations

import unicodedata
from email import policy
from email.parser import HeaderParser
from email.utils import getaddresses

from fieldkit.gmail.exceptions import GmailSchemaError

MAX_HEADER_BYTES = 64 * 1024
MAX_ADDRESSES_PER_HEADER = 100
MAX_EMAIL_CHARACTERS = 320
MAX_NAME_CHARACTERS = 160


def _invalid(field: str) -> GmailSchemaError:
    return GmailSchemaError(f"Gmail cache contains invalid {field} data")


def _display_name(value: str, *, field: str) -> str:
    if len(value) > MAX_NAME_CHARACTERS:
        raise _invalid(field)
    return "".join(" " if unicodedata.category(character).startswith("C") else character for character in value).strip()


def _mailbox(value: str, *, field: str) -> str:
    mailbox = value.strip().casefold()
    if len(mailbox) > MAX_EMAIL_CHARACTERS or mailbox.count("@") != 1:
        raise _invalid(field)
    local, domain = mailbox.split("@", 1)
    if (
        not local
        or not domain
        or any(character.isspace() or unicodedata.category(character).startswith("C") for character in mailbox)
    ):
        raise _invalid(field)
    return mailbox


def parse_address_header(value: object, *, field: str) -> tuple[tuple[str, str], ...]:
    """Return normalized ``(display_name, mailbox)`` pairs from one header.

    Empty headers and address-less RFC group headers are valid empty results.
    Non-empty parsed mailboxes must contain one bounded, whitespace-free ``@``
    address. All failures use a fixed schema diagnostic without source content.
    """
    if value is None or value == "":
        return ()
    if (
        not isinstance(value, str)
        or len(value.encode("utf-8")) > MAX_HEADER_BYTES
        or any(character in value for character in ("\r", "\n", "\x00"))
    ):
        raise _invalid(field)
    header = HeaderParser(policy=policy.default).parsestr(f"To: {value}\n\n", headersonly=True)["To"]
    if header is None or header.defects:
        raise _invalid(field)
    parsed = tuple((name, mailbox) for name, mailbox in getaddresses([value]) if mailbox)
    if len(parsed) != len(header.addresses):
        raise _invalid(field)
    if len(parsed) > MAX_ADDRESSES_PER_HEADER:
        raise _invalid(field)
    addresses: list[tuple[str, str]] = []
    for raw_name, raw_mailbox in parsed:
        addresses.append(
            (
                _display_name(raw_name, field="contact name"),
                _mailbox(raw_mailbox, field=field),
            )
        )
    if not addresses and not (":" in value and value.rstrip().endswith(";")):
        raise _invalid(field)
    return tuple(addresses)
