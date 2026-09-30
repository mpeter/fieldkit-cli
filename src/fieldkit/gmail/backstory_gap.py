"""Bounded CRM-review candidates from the ready published Gmail cache."""

from __future__ import annotations

import sqlite3
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from fieldkit.config import ConfigError
from fieldkit.gmail.addresses import parse_address_header
from fieldkit.gmail.exceptions import GmailSchemaError
from fieldkit.gmail.query_support import NOISE_REGEX
from fieldkit.gmail.query_support import configured_account_scope as _configured_account_scope
from fieldkit.gmail.query_support import scan_row_budget as _scan_row_budget
from fieldkit.gmail.query_support import sqlite_query_budget as _sqlite_query_budget

MAX_EMAIL_CHARS = 320
MAX_NAME_CHARS = 160
_LAST_SEEN_FORMATS = ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class AccountScope:
    """One validated account and its candidate threshold."""

    key: str
    domains: tuple[str, ...]
    min_messages: int


@dataclass(frozen=True)
class ContactCandidate:
    """One bounded contact that still requires a separate CRM comparison."""

    account: str
    min_messages: int
    email: str
    name: str
    message_count: int
    thread_count: int
    meeting_count: int
    slack_message_count: int
    last_seen: str


@dataclass(frozen=True)
class CandidateReport:
    """Bounded candidate results and whether additional rows were omitted."""

    items: tuple[ContactCandidate, ...]
    truncated: bool
    scan_truncated: bool
    scanned_rows: int


def is_noise(email: str) -> bool:
    """Return whether an address matches a maintained automation pattern."""
    return bool(NOISE_REGEX.search(email))


def _threshold(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError("accounts.yaml has an invalid backstory-gap threshold")
    return value


def account_scopes(
    config: Mapping[str, object],
    *,
    account: str | None,
    min_messages: int | None,
) -> tuple[AccountScope, ...]:
    """Validate account configuration and select deterministic report scopes."""
    if min_messages is not None:
        min_messages = _threshold(min_messages)
    raw_accounts = config.get("accounts")
    if not isinstance(raw_accounts, Mapping) or not raw_accounts:
        raise ConfigError("accounts.yaml must define accounts for backstory-gap")
    if any(not isinstance(key, str) or not isinstance(value, Mapping) for key, value in raw_accounts.items()):
        raise ConfigError("accounts.yaml has invalid backstory-gap account entries")
    accounts = {
        key: value for key, value in raw_accounts.items() if isinstance(key, str) and isinstance(value, Mapping)
    }
    if account is not None and account not in accounts:
        raise ConfigError("Unknown account; choose one configured account")
    selected = (account,) if account is not None else tuple(sorted(accounts))
    scopes: list[AccountScope] = []
    for key in selected:
        entry = accounts[key]
        threshold = min_messages if min_messages is not None else _threshold(entry.get("blindspots_min_messages", 20))
        configured = _configured_account_scope(config, key)
        scopes.append(AccountScope(configured.key, configured.domains, threshold))
    return tuple(scopes)


def _bounded_text(value: object, *, maximum: int, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > maximum:
        raise GmailSchemaError(f"Gmail cache contains invalid {field} data")
    return "".join(" " if unicodedata.category(character).startswith("C") else character for character in value).strip()


def _count(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GmailSchemaError(f"Gmail cache contains invalid {field} data")
    return value


def _contact_mailbox(value: object) -> str:
    email = _bounded_text(value, maximum=MAX_EMAIL_CHARS, field="contact")
    parsed = parse_address_header(email, field="contact")
    if len(parsed) != 1 or parsed[0][0] or parsed[0][1] != email.casefold():
        raise GmailSchemaError("Gmail cache contains invalid contact data")
    return parsed[0][1]


def _last_seen_date(value: object) -> str:
    last_seen = _bounded_text(value, maximum=64, field="contact date")
    if not last_seen:
        return ""
    for date_format in _LAST_SEEN_FORMATS:
        try:
            return datetime.strptime(last_seen, date_format).date().isoformat()
        except ValueError:
            continue
    raise GmailSchemaError("Gmail cache contains invalid contact date data")


def _candidate(row: sqlite3.Row, scope: AccountScope) -> ContactCandidate:
    email = _contact_mailbox(row["email"])
    return ContactCandidate(
        account=scope.key,
        min_messages=scope.min_messages,
        email=email,
        name=_bounded_text(row["display_name"], maximum=MAX_NAME_CHARS, field="contact name"),
        message_count=_count(row["message_count"], "message count"),
        thread_count=_count(row["thread_count"], "thread count"),
        meeting_count=_count(row["meeting_count"], "meeting count"),
        slack_message_count=_count(row["slack_message_count"], "Slack count"),
        last_seen=_last_seen_date(row["last_seen"]),
    )


def query_candidates(
    connection: sqlite3.Connection,
    scopes: tuple[AccountScope, ...],
    *,
    limit: int,
) -> CandidateReport:
    """Return at most *limit* deterministic candidates across all scopes."""
    row_budget = _scan_row_budget(limit)
    selected: list[ContactCandidate] = []
    scanned_rows = 0
    scan_truncated = False
    with _sqlite_query_budget(connection, row_budget=row_budget) as budget:
        for scope_index, scope in enumerate(scopes):
            if scope_index >= row_budget:
                scan_truncated = True
                break
            remaining = row_budget - scanned_rows
            if remaining <= 0:
                scan_truncated = True
                break
            placeholders = ",".join("?" for _ in scope.domains)
            try:
                rows = connection.execute(
                    f"""
                    SELECT email, display_name, message_count,
                           COALESCE(thread_count, 0) AS thread_count,
                           COALESCE(meeting_count, 0) AS meeting_count,
                           COALESCE(slack_message_count, 0) AS slack_message_count,
                           last_seen
                    FROM people
                    WHERE is_internal = 0
                      AND account = ?
                      AND message_count >= ?
                      AND LOWER(SUBSTR(email, INSTR(email, '@') + 1)) IN ({placeholders})
                    ORDER BY message_count DESC, LOWER(email), email
                    LIMIT ?
                    """,
                    (scope.key, scope.min_messages, *scope.domains, remaining + 1),
                ).fetchall()
            except sqlite3.OperationalError:
                if not budget.exhausted:
                    raise
                scan_truncated = True
                break
            if len(rows) > remaining:
                scan_truncated = True
                rows = rows[:remaining]
            scanned_rows += len(rows)
            for row in rows:
                candidate = _candidate(row, scope)
                if not is_noise(candidate.email):
                    selected.append(candidate)
                selected.sort(
                    key=lambda item: (
                        -item.message_count,
                        item.email.casefold(),
                        item.email,
                        item.account,
                    )
                )
                del selected[limit + 1 :]
    return CandidateReport(
        tuple(selected[:limit]),
        len(selected) > limit,
        scan_truncated,
        scanned_rows,
    )
