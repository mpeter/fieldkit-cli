"""Bounded relationship-decay reads from an exact Gmail account scope."""

from __future__ import annotations

import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fieldkit.gmail.addresses import parse_address_header
from fieldkit.gmail.constants import AUTOMATION_NOISE_DOMAINS
from fieldkit.gmail.exceptions import GmailSchemaError
from fieldkit.gmail.query_support import (
    NOISE_REGEX,
    ConfiguredAccountScope,
    SQLiteQueryBudget,
    domain_matches_boundary,
    markdown_cell,
    normalize_domain,
    scan_row_budget,
    sqlite_query_budget,
)

COLD_DAYS = 90
WARM_DAYS = 30
MAX_THREAD_ID_CHARS = 512
_MESSAGE_QUERY_BATCH_SIZE = 100


@dataclass(frozen=True)
class ContactDecay:
    """One bounded, account-specific contact recency result."""

    email: str
    name: str
    messages: int
    threads: int
    days_any: int
    days_sent: int
    days_first: int
    msgs_sent: int
    msgs_recv: int
    signal: str
    warmth: str


@dataclass(frozen=True)
class DecayReport:
    """One decay query and whether its result or work budget was truncated."""

    account: str
    as_of: str
    contacts: tuple[ContactDecay, ...]
    days_threshold: int
    show_all: bool
    threads_found: bool
    truncated: bool
    scan_truncated: bool
    scanned_rows: int


@dataclass(frozen=True, kw_only=True)
class DecayQuery:
    """Validated caller choices for one bounded account-scoped decay read."""

    days_threshold: int
    show_all: bool
    domain_filter: str | None
    min_messages: int
    limit: int
    max_age_days: int | None
    now: datetime | None = None


@dataclass
class _ContactStats:
    """Mutable aggregation for one contact within the selected account rows."""

    display_name: str = ""
    last_any: int = 0
    first_any: int = 0
    last_sent: int = 0
    msgs_sent: int = 0
    msgs_recv: int = 0
    messages: int = 0
    threads: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class _ContactEvent:
    name: str
    email: str
    sent_by_contact: bool


@dataclass
class _ScanState:
    row_budget: int
    scanned_rows: int = 0
    truncated: bool = False

    @property
    def remaining(self) -> int:
        return self.row_budget - self.scanned_rows


def _message_epoch(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GmailSchemaError("Gmail cache contains invalid message date data")
    return value


def _thread_id(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_THREAD_ID_CHARS:
        raise GmailSchemaError("Gmail cache contains invalid thread identifier data")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise GmailSchemaError("Gmail cache contains invalid thread identifier data")
    return value


def _is_internal_or_noise(email: str, excluded_domains: tuple[str, ...]) -> bool:
    if domain_matches_boundary(email.rpartition("@")[2], excluded_domains):
        return True
    return bool(NOISE_REGEX.search(email))


def is_internal_or_noise(email: str, *, internal_domains: tuple[str, ...]) -> bool:
    """Return whether an already-normalized mailbox is internal or automated."""
    excluded_domains = tuple(
        dict.fromkeys((*internal_domains, *(domain.casefold() for domain in AUTOMATION_NOISE_DOMAINS)))
    )
    return _is_internal_or_noise(email.casefold(), excluded_domains)


def _preferred_name(current: str, candidate: str) -> str:
    """Choose one deterministic bounded display name for a repeated mailbox."""
    return max((current, candidate), key=lambda value: (bool(value), len(value), value.casefold(), value))


def _deduplicated_addresses(addresses: tuple[tuple[str, str], ...]) -> dict[str, str]:
    deduplicated: dict[str, str] = {}
    for name, email in addresses:
        deduplicated[email] = _preferred_name(deduplicated.get(email, ""), name)
    return deduplicated


def _record(
    stats: dict[str, _ContactStats],
    event: _ContactEvent,
    epoch: int,
    thread_id: str,
    contact_budget: int,
) -> bool:
    if event.email not in stats and len(stats) >= contact_budget:
        return False
    contact = stats.setdefault(event.email, _ContactStats())
    contact.messages += 1
    contact.threads.add(thread_id)
    if event.name and (not contact.display_name or epoch >= contact.last_any):
        contact.display_name = event.name
    if epoch:
        contact.last_any = max(contact.last_any, epoch)
        contact.first_any = epoch if contact.first_any == 0 else min(contact.first_any, epoch)
    if event.sent_by_contact:
        contact.msgs_sent += 1
        if epoch:
            contact.last_sent = max(contact.last_sent, epoch)
    else:
        contact.msgs_recv += 1
    return True


def _signal(days: int) -> str:
    if days >= COLD_DAYS:
        return "COLD"
    if days >= WARM_DAYS:
        return "cooling"
    return "active"


def _warmth(days: int, msgs_sent: int, msgs_recv: int) -> str:
    if days >= COLD_DAYS:
        return "cold"
    total = msgs_sent + msgs_recv
    if days < WARM_DAYS and total > 0 and msgs_sent / total >= 0.3:
        return "warm"
    return "lukewarm"


def _contact_result(stats: _ContactStats, email: str, *, now_epoch: int) -> ContactDecay:
    days_any = (now_epoch - stats.last_any) // 86400 if stats.last_any else 9999
    days_sent = (now_epoch - stats.last_sent) // 86400 if stats.last_sent else 9999
    days_first = (now_epoch - stats.first_any) // 86400 if stats.first_any else 0
    return ContactDecay(
        email=email,
        name=stats.display_name,
        messages=stats.messages,
        threads=len(stats.threads),
        days_any=days_any,
        days_sent=days_sent,
        days_first=days_first,
        msgs_sent=stats.msgs_sent,
        msgs_recv=stats.msgs_recv,
        signal=_signal(days_any),
        warmth=_warmth(days_any, stats.msgs_sent, stats.msgs_recv),
    )


def _selected_domains(
    scope: ConfiguredAccountScope,
    query: DecayQuery,
) -> tuple[str, ...] | None:
    if query.domain_filter is not None:
        return (normalize_domain(query.domain_filter, field="decay domain filter"),)
    return None if query.show_all else scope.domains


def _select_thread_ids(
    connection: sqlite3.Connection,
    scope: ConfiguredAccountScope,
    scan: _ScanState,
    budget: SQLiteQueryBudget,
) -> list[str]:
    try:
        rows = connection.execute(
            """
            SELECT thread_id FROM thread_accounts
            WHERE account = ? ORDER BY thread_id LIMIT ?
            """,
            (scope.key, scan.row_budget + 1),
        ).fetchall()
    except sqlite3.OperationalError:
        if not budget.exhausted:
            raise
        scan.truncated = True
        return []
    if len(rows) > scan.row_budget:
        scan.truncated = True
        rows = rows[: scan.row_budget]
    thread_ids = [_thread_id(row[0]) for row in rows]
    scan.scanned_rows += len(thread_ids)
    return thread_ids


def _message_events(row: sqlite3.Row) -> tuple[_ContactEvent, ...]:
    senders = _deduplicated_addresses(parse_address_header(row[0], field="sender address"))
    recipients = _deduplicated_addresses(
        (
            *parse_address_header(row[1], field="recipient address"),
            *parse_address_header(row[2], field="recipient address"),
        )
    )
    return (
        *(_ContactEvent(name, email, True) for email, name in senders.items()),
        *(_ContactEvent(name, email, False) for email, name in recipients.items() if email not in senders),
    )


def _read_message_batch(
    connection: sqlite3.Connection,
    thread_ids: list[str],
    scan: _ScanState,
    budget: SQLiteQueryBudget,
) -> tuple[list[sqlite3.Row], bool]:
    placeholders = ",".join("?" for _ in thread_ids)
    try:
        rows = connection.execute(
            f"""
            SELECT from_addr, to_addr, cc_addr, date_epoch, thread_id
            FROM messages WHERE thread_id IN ({placeholders})
            ORDER BY date_epoch, message_id LIMIT ?
            """,
            (*thread_ids, scan.remaining + 1),
        ).fetchall()
    except sqlite3.OperationalError:
        if not budget.exhausted:
            raise
        scan.truncated = True
        return [], False
    if len(rows) > scan.remaining:
        scan.truncated = True
        rows = rows[: scan.remaining]
    scan.scanned_rows += len(rows)
    return rows, True


def _aggregate_messages(
    connection: sqlite3.Connection,
    thread_ids: list[str],
    stats: dict[str, _ContactStats],
    scan: _ScanState,
    budget: SQLiteQueryBudget,
) -> None:
    if thread_ids and scan.remaining <= 0:
        scan.truncated = True
        return
    for offset in range(0, len(thread_ids), _MESSAGE_QUERY_BATCH_SIZE):
        batch = thread_ids[offset : offset + _MESSAGE_QUERY_BATCH_SIZE]
        rows, complete = _read_message_batch(connection, batch, scan, budget)
        if not complete:
            break
        for row in rows:
            epoch = _message_epoch(row[3])
            thread_id = _thread_id(row[4])
            for event in _message_events(row):
                if not _record(stats, event, epoch, thread_id, scan.row_budget):
                    scan.truncated = True
        if scan.remaining <= 0:
            if offset + _MESSAGE_QUERY_BATCH_SIZE < len(thread_ids):
                scan.truncated = True
            break


def _filtered_contacts(
    stats: dict[str, _ContactStats],
    scope: ConfiguredAccountScope,
    query: DecayQuery,
    now_epoch: int,
) -> list[ContactDecay]:
    excluded_domains = tuple(
        dict.fromkeys((*scope.internal_domains, *(domain.casefold() for domain in AUTOMATION_NOISE_DOMAINS)))
    )
    selected_domains = _selected_domains(scope, query)
    contacts: list[ContactDecay] = []
    for email, contact_stats in stats.items():
        if _is_internal_or_noise(email, excluded_domains):
            continue
        if selected_domains is not None and email.rpartition("@")[2] not in selected_domains:
            continue
        contact = _contact_result(contact_stats, email, now_epoch=now_epoch)
        if contact.messages < query.min_messages:
            continue
        if not query.show_all and contact.days_any < query.days_threshold:
            continue
        if query.max_age_days is not None and contact.days_any > query.max_age_days:
            continue
        contacts.append(contact)
    contacts.sort(
        key=lambda item: (item.days_any if query.show_all else -item.days_any, item.email.casefold(), item.email)
    )
    return contacts


def query_decay(
    connection: sqlite3.Connection,
    scope: ConfiguredAccountScope,
    query: DecayQuery,
) -> DecayReport:
    """Return a bounded decay report from threads tagged to exactly one account."""
    current = query.now or datetime.now(UTC)
    scan = _ScanState(scan_row_budget(query.limit))
    stats: dict[str, _ContactStats] = {}
    with sqlite_query_budget(connection, row_budget=scan.row_budget) as budget:
        thread_ids = _select_thread_ids(connection, scope, scan, budget)
        _aggregate_messages(connection, thread_ids, stats, scan, budget)
    contacts = _filtered_contacts(stats, scope, query, int(current.timestamp()))
    return DecayReport(
        account=scope.key,
        as_of=current.date().isoformat(),
        contacts=tuple(contacts[: query.limit]),
        days_threshold=query.days_threshold,
        show_all=query.show_all,
        threads_found=bool(thread_ids),
        truncated=len(contacts) > query.limit,
        scan_truncated=scan.truncated,
        scanned_rows=scan.scanned_rows,
    )


def render_decay_text(report: DecayReport) -> str:
    """Render one already-sanitized report without writing to a stream."""
    if not report.threads_found:
        body = f"No threads found for account: {report.account}"
    elif not report.contacts:
        body = (
            "No external contacts found for this account."
            if report.show_all
            else f"No contacts silent for more than {report.days_threshold} days."
        )
    else:
        rows = ["Email | Name | Silent | First | They wrote | Msgs | Signal | Warmth"]
        for contact in report.contacts:
            they_wrote = f"{contact.days_sent}d" if contact.days_sent < 9999 else "never"
            first = f"{contact.days_first}d" if contact.days_first > 0 else "—"
            email = markdown_cell(contact.email)
            name = markdown_cell(contact.name)
            rows.append(
                f"{email} | {name} | {contact.days_any}d | {first} | "
                f"{they_wrote} | {contact.messages} | {contact.signal} | {contact.warmth}"
            )
        body = "\n".join(rows)
    title = f"Relationship decay: {report.account}"
    if not report.show_all:
        title += f" (silent >{report.days_threshold}d)"
    suffix: list[str] = []
    if report.truncated:
        suffix.append("Results were truncated by the requested contact limit.")
    if report.scan_truncated:
        suffix.append("The bounded cache scan was incomplete; retry with a narrower account scope.")
    return "\n\n".join(part for part in (title, f"As of: {report.as_of}", body, *suffix) if part)
