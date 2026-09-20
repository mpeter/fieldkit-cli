"""Relationship decay domain logic — last email contact per person, per account.

Extracted from commands/gmail/decay.py (historic regression) so that collect.py and other
callers can import from the domain layer instead of from commands/.

historic regression: connect() raises GmailDbNotFoundError instead of calling sys.exit(1).
"""

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fieldkit.config import build_domain_account_map, get_internal_domains
from fieldkit.gmail.constants import AUTOMATION_NOISE_DOMAINS
from fieldkit.gmail.discover import NOISE_REGEX
from fieldkit.gmail.exceptions import GmailDbNotFoundError, GmailIndexMissingError

log = logging.getLogger(__name__)

# Thresholds for decay signal
COLD_DAYS = 90
WARM_DAYS = 30


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection to the Gmail cache database.

    Args:
        db_path: Path to the gmail.db SQLite file.

    Raises:
        GmailDbNotFoundError: When the database file does not exist.
    """
    if not db_path.exists():
        raise GmailDbNotFoundError(f"Gmail database not found: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _internal_domain_set() -> set[str]:
    """Return internal domains at call time so config changes take effect without restart."""
    return set(get_internal_domains()) | set(AUTOMATION_NOISE_DOMAINS)


def is_internal_or_noise(email: str) -> bool:
    """Return True if the email belongs to an internal domain or matches a noise pattern."""
    email = email.lower()
    domain = email.split("@")[-1] if "@" in email else ""
    if domain in _internal_domain_set():
        return True
    return bool(NOISE_REGEX.search(email))


def _extract_email(addr: str) -> str:
    """Pull bare email from 'Name <email>' or return as-is."""
    addr = addr.strip()
    if "<" in addr and addr.endswith(">"):
        return addr.split("<")[-1].rstrip(">").strip().lower()
    return addr.lower()


def _get_account_thread_ids(conn: sqlite3.Connection, account: str) -> list[str]:
    """Return all thread IDs tagged for *account*.

    Raises:
        GmailIndexMissingError: if the thread_accounts table does not exist (historic regression).
            Run 'fieldkit sync' to build the index.
    """
    from fieldkit.gmail.discover import table_exists  # lazy — avoid circular at module level

    if not table_exists(conn, "thread_accounts"):
        raise GmailIndexMissingError(
            "thread_accounts table missing from gmail.db.\nRun 'fieldkit sync' to build the index, then retry."
        )
    return [
        r[0]
        for r in conn.execute(
            "SELECT thread_id FROM thread_accounts WHERE LOWER(account) = ?", [account.lower()]
        ).fetchall()
    ]


def _record_address(
    person_stats: dict[str, dict[str, Any]],
    email: str,
    date_epoch: int,
    thread_id: str,
    sent_by_them: bool,
) -> None:
    """Accumulate one address event into *person_stats*."""
    if not email or "@" not in email:
        return
    if email not in person_stats:
        person_stats[email] = {
            "last_any": 0,
            "first_any": 0,
            "last_sent": 0,
            "last_recv": 0,
            "msgs_sent": 0,  # messages they sent us
            "msgs_recv": 0,  # messages we sent them
            "threads": set(),
            "messages": 0,
            "display_name": "",
        }
    s = person_stats[email]
    s["messages"] += 1
    s["threads"].add(thread_id)
    if date_epoch:
        if date_epoch > s["last_any"]:
            s["last_any"] = date_epoch
        if s["first_any"] == 0 or date_epoch < s["first_any"]:
            s["first_any"] = date_epoch
    if sent_by_them:
        s["msgs_sent"] += 1
        if date_epoch and date_epoch > s["last_sent"]:
            s["last_sent"] = date_epoch
    else:
        s["msgs_recv"] += 1
        if date_epoch and date_epoch > s["last_recv"]:
            s["last_recv"] = date_epoch


def _process_message_row(
    person_stats: dict[str, dict[str, Any]],
    row: Any,
) -> None:
    """Record sender and recipients from a single message row into *person_stats*."""
    from_e = _extract_email(row["from_addr"] or "")
    to_addrs = [_extract_email(a) for a in (row["to_addr"] or "").split(",") if a.strip()]
    cc_addrs = [_extract_email(a) for a in (row["cc_addr"] or "").split(",") if a.strip()]
    epoch = row["date_epoch"] or 0
    tid = row["thread_id"]

    if from_e:
        _record_address(person_stats, from_e, epoch, tid, sent_by_them=True)
    for e in to_addrs + cc_addrs:
        if e and e != from_e:
            _record_address(person_stats, e, epoch, tid, sent_by_them=False)


def _enrich_display_names(
    conn: sqlite3.Connection,
    person_stats: dict[str, dict[str, Any]],
) -> None:
    """Populate display_name for each entry in *person_stats* from the people table (in-place).

    Silently skips enrichment when the people table does not exist (historic regression).
    The table is built by 'fieldkit sync'; if absent, display_name stays blank.
    """
    if not person_stats:
        return
    from fieldkit.gmail.discover import table_exists  # lazy import

    if not table_exists(conn, "people"):
        return  # graceful degradation — names will be blank, not a crash
    p_placeholders = ",".join("?" * len(person_stats))
    name_rows = conn.execute(
        f"SELECT email, display_name FROM people WHERE email IN ({p_placeholders})",
        list(person_stats.keys()),
    ).fetchall()
    for nr in name_rows:
        if nr["email"] in person_stats:
            person_stats[nr["email"]]["display_name"] = nr["display_name"] or ""


def _aggregate_message_contacts(conn: sqlite3.Connection, thread_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Pull messages for *thread_ids* and aggregate contact stats per email.

    Returns person_stats dict: email → {last_any, last_sent, last_recv, threads, messages, display_name}.
    """
    placeholders = ",".join("?" * len(thread_ids))
    msg_rows = conn.execute(
        f"""
        SELECT from_addr, to_addr, cc_addr, date_epoch, thread_id
        FROM   messages
        WHERE  thread_id IN ({placeholders})
        """,
        thread_ids,
    ).fetchall()

    person_stats: dict[str, dict[str, Any]] = {}
    for row in msg_rows:
        _process_message_row(person_stats, row)

    _enrich_display_names(conn, person_stats)
    return person_stats


def _build_contact_list(
    person_stats: dict[str, dict[str, Any]],
    *,
    now_epoch: int,
    days_threshold: int,
    show_all: bool,
    domain_filter: str | set[str] | None,
    min_messages: int,
    limit: int | None,
    max_age_days: int | None,
) -> list[dict[str, Any]]:
    """Filter, score, sort, and limit person_stats into a renderable contact list.

    Args:
        person_stats: Aggregated contact stats keyed by email address.
        now_epoch: Current time as a Unix timestamp (seconds).
        days_threshold: Minimum days silent to be considered stale (lower bound).
        show_all: When True, bypass staleness and age filters entirely.
        domain_filter: Restrict output to contacts at this domain or set of domains.
        min_messages: Minimum message count required to include a contact.
        limit: Cap the number of results returned.
        max_age_days: Exclude contacts silent for more than this many days (upper bound).
            None means no upper-bound cutoff. Bypassed when show_all=True.
    """
    contacts = []
    for email, s in person_stats.items():
        if is_internal_or_noise(email):
            continue
        if domain_filter is not None:
            contact_domain = email.split("@")[-1] if "@" in email else ""
            if isinstance(domain_filter, set):
                if contact_domain not in domain_filter:
                    continue
            elif not email.endswith(f"@{domain_filter}"):
                continue
        if s["messages"] < min_messages:
            continue

        last_any = s["last_any"]
        last_sent = s["last_sent"]
        days_any = (now_epoch - last_any) // 86400 if last_any else 9999
        days_sent = (now_epoch - last_sent) // 86400 if last_sent else 9999

        if not show_all and days_any < days_threshold:
            continue

        # implementation change: exclude contacts silent longer than max_age_days — inactive relationships,
        # not stale contacts requiring action. Bypassed when show_all=True (--all shows all).
        # Replaces the hardcoded 1460-day cutoff from implementation change with a configurable parameter.
        if not show_all and max_age_days is not None and days_any > max_age_days:
            continue

        first_any = s["first_any"]
        days_first = (now_epoch - first_any) // 86400 if first_any else 0
        contacts.append(
            {
                "email": email,
                "name": s["display_name"],
                "messages": s["messages"],
                "threads": len(s["threads"]),
                "days_any": days_any,
                "days_sent": days_sent,
                "days_first": days_first,
                "msgs_sent": s["msgs_sent"],
                "msgs_recv": s["msgs_recv"],
            }
        )

    # With --all: sort ascending (most recent first) so active contacts appear at top.
    # Without --all (stale-only): sort descending (most silent first).
    contacts.sort(key=lambda x: x["days_any"] if show_all else -x["days_any"])
    if limit is not None:
        contacts = contacts[:limit]
    return contacts


def _decay_signal(days: int) -> str:
    """Return a decay signal label for the given days-silent count."""
    if days >= COLD_DAYS:
        return "COLD"
    if days >= WARM_DAYS:
        return "cooling"
    return "active"


def _warmth_tier(c: dict[str, Any]) -> str:
    """Classify a contact into a warmth tier based on recency and initiation ratio.

    Tiers:
      warm     — active, two-way, and they initiate (inbound-led relationship)
      lukewarm — active or cooling but mostly outbound (we write more)
      cold     — COLD signal, or they've never written back
    """
    days = c["days_any"]
    msgs_sent = c["msgs_sent"]  # they sent to us
    msgs_recv = c["msgs_recv"]  # we sent to them
    total = msgs_sent + msgs_recv

    if days >= COLD_DAYS:
        return "cold"
    # Two-way check: they sent at least 30% of messages
    two_way = total > 0 and (msgs_sent / total) >= 0.3
    if days < WARM_DAYS and two_way:
        return "warm"
    return "lukewarm"


def _print_contact_table(contacts: list[dict[str, Any]]) -> None:
    """Print the formatted decay table for *contacts*."""
    col_email = 36
    col_name = 20
    header = (
        f"  {'Email':<{col_email}}  {'Name':<{col_name}}"
        f"  {'Silent':>6}  {'First':>6}  {'They→':>5}  {'Msgs':>4}  {'Signal':<8}  Warmth"
    )
    print(header)
    print("  " + "-" * (len(header) + 2))

    for c in contacts:
        days = c["days_any"]
        signal = _decay_signal(days)
        warmth = _warmth_tier(c)
        they_wrote = f"{c['days_sent']}d" if c.get("days_sent", 9999) < 9999 else "never"
        days_first = f"{c['days_first']}d" if c.get("days_first", 0) > 0 else "—"
        print(
            f"  {c['email'][:col_email]:<{col_email}}  "
            f"{c['name'][:col_name]:<{col_name}}  "
            f"{str(days) + 'd':>6}  "
            f"{days_first:>6}  "
            f"{they_wrote:>5}  "
            f"{c['messages']:>4}  "
            f"{signal:<8}  "
            f"{warmth}"
        )

    cold = sum(1 for c in contacts if c["days_any"] >= COLD_DAYS)
    cooling = sum(1 for c in contacts if WARM_DAYS <= c["days_any"] < COLD_DAYS)
    active = sum(1 for c in contacts if c["days_any"] < WARM_DAYS)
    print(f"\n  Summary: {cold} COLD  {cooling} cooling  {active} active  (of {len(contacts)} shown)")


def decay_report(
    conn: sqlite3.Connection,
    *,
    account: str,
    days_threshold: int,
    show_all: bool,
    domain_filter: str | set[str] | None = None,
    min_messages: int = 1,
    limit: int | None = None,
    max_age_days: int | None = 365,
    as_json: bool = False,
) -> None:
    """Print a contact decay report showing contacts going cold for a given account.

    Args:
        conn: Open SQLite connection to the Gmail cache database.
        account: Account slug to report on (matched against thread_accounts.account).
        days_threshold: Minimum days silent to flag a contact as stale.
        show_all: When True, show all contacts regardless of staleness or age.
        domain_filter: Restrict output to contacts at this domain or set of domains.
            When None, auto-resolves from accounts.yaml via build_domain_account_map().
        min_messages: Minimum message count required to include a contact.
        limit: Cap the number of results returned (None = unlimited).
        max_age_days: Exclude contacts silent for more than this many days (upper bound).
            Default 365. None means no cutoff. Bypassed when show_all=True.

    historic regression: domain_filter now accepts str | set[str] | None so callers can pass a
    full domain set for multi-domain accounts.
    historic regression: When show_all=True, resolved_domain_filter is forced to None before the
    build_domain_account_map() call so --all truly returns all contacts regardless of
    any auto-resolved domain filter.
    implementation change: max_age_days replaces the hardcoded 1460-day cutoff (implementation change) with a
    configurable upper bound (default 365 days).
    """
    now_epoch = int(datetime.now(UTC).timestamp())

    # historic regression: --all must bypass domain filtering entirely.
    if show_all:
        resolved_domain_filter: str | set[str] | None = None
    elif domain_filter is not None:
        # Explicit domain filter passed by caller — use it directly.
        resolved_domain_filter = domain_filter
    else:
        # Auto-lookup account domains from accounts.yaml when no explicit domain filter set.
        resolved_domain_filter = None
        domain_account_map = build_domain_account_map()
        account_domains = {d for d, slug in domain_account_map.items() if slug == account}
        if account_domains:
            resolved_domain_filter = account_domains
        else:
            log.warning(
                "decay: no domains configured for account %r in accounts.yaml — "
                "results include all external contacts. Add a 'domains' list to filter by domain.",
                account,
            )

    thread_ids = _get_account_thread_ids(conn, account)
    if not thread_ids:
        _emit_decay_result(
            account, [], days_threshold=days_threshold, show_all=show_all, as_json=as_json, threads=False
        )
        return

    person_stats = _aggregate_message_contacts(conn, thread_ids)
    contacts = _build_contact_list(
        person_stats,
        now_epoch=now_epoch,
        days_threshold=days_threshold,
        show_all=show_all,
        domain_filter=resolved_domain_filter,
        min_messages=min_messages,
        limit=limit,
        max_age_days=max_age_days,
    )

    _emit_decay_result(
        account, contacts, days_threshold=days_threshold, show_all=show_all, as_json=as_json, threads=True
    )


def _emit_decay_result(
    account: str,
    contacts: list[dict[str, Any]],
    *,
    days_threshold: int,
    show_all: bool,
    as_json: bool,
    threads: bool,
) -> None:
    """Render one collected decay result in machine or human form."""
    if as_json:
        print(
            json.dumps(
                {
                    "account": account,
                    "as_of": datetime.now(UTC).date().isoformat(),
                    "contacts": [
                        {
                            **contact,
                            "signal": _decay_signal(contact["days_any"]),
                            "warmth": _warmth_tier(contact),
                        }
                        for contact in contacts
                    ],
                },
                sort_keys=True,
            )
        )
        return

    if not threads:
        print(f"\nNo threads found for account: {account}")
        return

    title = f"Relationship decay: {account}"
    if not show_all:
        title += f" (silent >{days_threshold}d)"
    print(f"\n{title}\n")
    print(f"As of: {datetime.now(UTC).strftime('%Y-%m-%d')}\n")

    if not contacts:
        if show_all:
            print("  No external contacts found for this account.")
        else:
            print(f"  No contacts silent for more than {days_threshold} days.")
            print("  Run with --all to see everyone, or --days N to adjust threshold.")
        return

    _print_contact_table(contacts)


# Re-export get_gmail_db_path for convenience of CLI callers that previously
# imported it from this module's namespace indirectly.
__all__ = [
    "COLD_DAYS",
    "WARM_DAYS",
    "connect",
    "decay_report",
    "is_internal_or_noise",
]
