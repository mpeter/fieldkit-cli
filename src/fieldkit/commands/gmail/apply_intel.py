#!/usr/bin/env python3
"""
Apply Gmail intelligence to pursuit files.

Reads gmail.db directly to append a '## Gmail Signals' section to each pursuit.
"""

import re
import sqlite3
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any

from fieldkit.config import get_accounts_config, get_internal_domains
from fieldkit.gmail.discover import get_gmail_db_path


def _internal_domains() -> list[str]:
    """Return internal domains at call time so config changes take effect without restart."""
    return get_internal_domains()


# historic regression: Patterns for known automated/system senders that must never surface
# in blindspot output.  Kept as a documentation artifact — the actual matching
# logic is in _is_system_address() below, which uses anchored checks instead of
# substring matching to prevent false positives (e.g. "noreply" matching
# "user+noreply@legit-vendor.example.com" via the old "noreply@" substring check).
SYSTEM_EMAIL_PATTERNS: frozenset[str] = frozenset(
    {
        "noreply@",
        "no-reply@",
        "notifications@",
        "calendar-notification@",
        "mailer-daemon@",
        "@google.com",
        "@docusign.net",
    }
)

# Exact local-part bases that identify automated senders (plus-addressing stripped).
_SYSTEM_LOCAL_PARTS: frozenset[str] = frozenset(
    {"noreply", "no-reply", "notifications", "calendar-notification", "mailer-daemon"}
)

# Exact domains that are always treated as system/automated senders.
_SYSTEM_DOMAINS: frozenset[str] = frozenset({"google.com", "docusign.net"})


def _is_system_address(email: str) -> bool:
    """Return True when *email* is a known automated/system sender.

    Uses anchored matching (exact local-part and exact domain) instead of
    substring matching to prevent false positives.  For example, a legitimate
    vendor address like ``updates+noreply@vendor.example.com`` would incorrectly match
    the old ``"noreply@" in email`` substring check.

    Matching rules:
    - Local-part base (before any ``+`` plus-addressing suffix) must be an
      exact member of ``_SYSTEM_LOCAL_PARTS``.
    - OR the domain must be an exact member of ``_SYSTEM_DOMAINS``.
    """
    email_lower = email.lower()
    local, _, domain = email_lower.partition("@")
    if not domain:
        return False
    # Strip plus-addressing suffix before comparing local part.
    local_base = local.split("+")[0]
    if local_base in _SYSTEM_LOCAL_PARTS:
        return True
    return domain in _SYSTEM_DOMAINS


SINCE_EPOCH = int(datetime(2025, 10, 1).timestamp())


@cache
def _account_domains() -> dict[str, list[str]]:
    cfg = get_accounts_config()
    return {name: info.get("domains", []) for name, info in cfg.get("accounts", {}).items()}


def get_db() -> sqlite3.Connection:
    """Open a connection to the Gmail cache database and ensure required indexes exist."""
    db = sqlite3.connect(str(get_gmail_db_path()))
    db.execute("CREATE INDEX IF NOT EXISTS idx_messages_from ON messages(from_addr)")
    return db


def get_account_contacts(db: sqlite3.Connection, account: str, limit: int = 30) -> list[Any]:
    """Get top contacts for an account using thread_accounts for fast lookup."""
    domains = _account_domains().get(account, [])
    if not domains:
        return []
    domain_conditions = " OR ".join("p.email LIKE '%@' || ?" for _ in domains)
    sql = f"""
    SELECT p.email, p.display_name,
           COUNT(DISTINCT m.thread_id) as thread_count,
           COUNT(m.message_id) as msg_count,
           MAX(m.date_epoch) as last_epoch
    FROM thread_accounts ta
    JOIN messages m ON m.thread_id = ta.thread_id
    JOIN people p ON p.email = ({EXTRACT_EMAIL})
    WHERE ta.account = ?
      AND m.date_epoch >= ?
      AND ({domain_conditions})
    GROUP BY p.email
    ORDER BY msg_count DESC
    LIMIT ?
    """
    return db.execute(sql, [account, SINCE_EPOCH, *domains, limit]).fetchall()


def get_pursuit_contacts(db: sqlite3.Connection, account: str, known_emails: set[str], limit: int = 30) -> list[Any]:
    """Get contacts scoped to threads where known pursuit participants appear.

    Finds all threads that include at least one of the known_emails as sender,
    then returns the top customer contacts within those threads only.
    This keeps Gmail signals pursuit-relevant instead of account-wide.
    Falls back to account-wide query when no known_emails are provided.
    """
    domains = _account_domains().get(account, [])
    if not domains or not known_emails:
        return get_account_contacts(db, account, limit)

    email_placeholders = ",".join("?" for _ in known_emails)
    domain_conditions = " OR ".join("p.email LIKE '%@' || ?" for _ in domains)

    sql = f"""
    WITH pursuit_threads AS (
        SELECT DISTINCT m.thread_id
        FROM messages m
        WHERE ({EXTRACT_EMAIL}) IN ({email_placeholders})
          AND m.date_epoch >= ?
    )
    SELECT p.email, p.display_name,
           COUNT(DISTINCT m.thread_id) as thread_count,
           COUNT(m.message_id) as msg_count,
           MAX(m.date_epoch) as last_epoch
    FROM pursuit_threads pt
    JOIN messages m ON m.thread_id = pt.thread_id
    JOIN people p ON p.email = ({EXTRACT_EMAIL})
    WHERE ({domain_conditions})
      AND m.date_epoch >= ?
    GROUP BY p.email
    ORDER BY msg_count DESC
    LIMIT ?
    """
    params = [*list(known_emails), SINCE_EPOCH, *domains, SINCE_EPOCH, limit]
    results = db.execute(sql, params).fetchall()

    # If pursuit-scoped query returns fewer than 5 contacts, fall back to account-wide
    # (pursuit may not have had much email traffic yet)
    if len(results) < 5:
        return get_account_contacts(db, account, limit)
    return results


def _email_expr(col: str = "from_addr") -> str:
    return f"CASE WHEN INSTR({col}, '<') > 0 THEN SUBSTR({col}, INSTR({col}, '<')+1, INSTR({col}, '>') - INSTR({col}, '<') - 1) ELSE {col} END"


EXTRACT_EMAIL = _email_expr()


def batch_champion_signals(db: sqlite3.Connection, emails: list[str]) -> dict[str, dict[str, Any]]:
    """Compute champion engagement signals (thread count, initiation rate, sent count) for a batch of email addresses."""
    if not emails:
        return {}
    placeholders = ",".join("?" for _ in emails)

    initiated = {}
    rows = db.execute(
        f"""
        SELECT {EXTRACT_EMAIL} as email, COUNT(DISTINCT m.thread_id)
        FROM messages m
        JOIN (SELECT thread_id, MIN(date_epoch) AS first_epoch FROM messages GROUP BY thread_id) t
          ON m.thread_id = t.thread_id AND m.date_epoch = t.first_epoch
        WHERE {EXTRACT_EMAIL} IN ({placeholders})
          AND m.date_epoch >= ?
        GROUP BY email
    """,
        [*list(emails), SINCE_EPOCH],
    ).fetchall()
    for email, cnt in rows:
        initiated[email] = cnt

    total_threads = {}
    rows = db.execute(
        f"""
        SELECT {EXTRACT_EMAIL} as email, COUNT(DISTINCT thread_id) FROM messages
        WHERE {EXTRACT_EMAIL} IN ({placeholders}) AND date_epoch >= ?
        GROUP BY email
    """,
        [*list(emails), SINCE_EPOCH],
    ).fetchall()
    for email, cnt in rows:
        total_threads[email] = cnt

    sent_counts = {}
    rows = db.execute(
        f"""
        SELECT {EXTRACT_EMAIL} as email, COUNT(*) FROM messages
        WHERE {EXTRACT_EMAIL} IN ({placeholders}) AND date_epoch >= ?
        GROUP BY email
    """,
        [*list(emails), SINCE_EPOCH],
    ).fetchall()
    for email, cnt in rows:
        sent_counts[email] = cnt

    result = {}
    for email in emails:
        total = total_threads.get(email, 0)
        if total == 0:
            continue
        init = initiated.get(email, 0)
        rate = init / total
        sent = sent_counts.get(email, 0)
        signal = "INITIATOR" if rate >= 0.15 else ("MIXED" if rate >= 0.05 else "REACTIVE")
        result[email] = {
            "threads": total,
            "initiated": init,
            "rate": rate,
            "sent": sent,
            "signal": signal,
        }
    return result


def names_in_file(text: str) -> set[str]:
    """Extract capitalized two-word names from text, excluding known non-person terms."""
    names = set()
    skip = {
        "Acme Bank",
        "Shield Insurance",
        "Open Shift",
        "Data Needed",
        "Last Updated",
        "Close Date",
        "Net New",
        "Alex Morgan",
        "Key Fields",
        "Decision Criteria",
        "Decision Process",
        "Paper Process",
        "Opportunity Summary",
        "Pursuit File",
        "Enterprise Automation",
        "Security Automation",
        "Next Steps",
        "Phase Two",
        "Phase Three",
        "Active Workstreams",
        "Gmail Signals",
        "Contact Blindspots",
        "Champion Candidates",
        "Competitive Intel",
        "Account Contacts",
    }
    for m in re.finditer(r"\b([A-Z][a-z]+ [A-Z][a-z]+)\b", text):
        if m.group(1) not in skip:
            names.add(m.group(1))
    return names


def _display_name(name: str, email: str) -> str:
    """Return display name or username portion of email."""
    return name if name else email.split("@")[0]


def _days_ago(now_epoch: int, last_epoch: int | None) -> int | str:
    """Return days since last_epoch, or '?' if unknown."""
    return (now_epoch - last_epoch) // 86400 if last_epoch else "?"


def _champ_tag(champ: dict[str, Any] | None) -> str:
    """Return a markdown tag for a champion signal, or empty string."""
    if not champ:
        return ""
    if champ["signal"] == "INITIATOR":
        return " — **INITIATOR**"
    if champ["signal"] == "MIXED":
        return " — MIXED"
    return ""


def _is_mentioned(email: str, display: str, known_emails: set[str], known_names: set[str]) -> bool:
    """Return True if this contact is already referenced in the pursuit file."""
    if email in known_emails:
        return True
    first = display.lower().split()[0] if display else ""
    return any(n.split()[0].lower() == first for n in known_names)


def _render_top_contacts(
    contacts: list[Any],
    champ_signals: dict[str, dict[str, Any]],
    known_emails: set[str],
    known_names: set[str],
    now_epoch: int,
) -> list[str]:
    lines: list[str] = ["### Top Account Contacts", ""]
    for email, name, threads, msgs, last_epoch in contacts[:15]:
        display = _display_name(name, email)
        days = _days_ago(now_epoch, last_epoch)
        tag = _champ_tag(champ_signals.get(email))
        marker = " *(in file)*" if _is_mentioned(email, display, known_emails, known_names) else ""
        lines.append(f"- {display} ({email}) — {msgs} msgs, {threads} threads, {days}d ago{tag}{marker}")
    return lines


def _render_champion_candidates(
    contacts: list[Any],
    champ_signals: dict[str, dict[str, Any]],
) -> list[str]:
    lines: list[str] = ["", "### Champion Candidates", ""]
    found = False
    for email, name, _threads, _msgs, _last_epoch in contacts[:20]:
        champ = champ_signals.get(email)
        if champ and champ["signal"] in ("INITIATOR", "MIXED") and champ["sent"] >= 5:
            display = _display_name(name, email)
            found = True
            lines.append(
                f"- **{display}** ({email}): {champ['initiated']}/{champ['threads']} threads initiated"
                f" ({champ['rate']:.0%}), {champ['sent']} sent — {champ['signal']}"
            )
    if not found:
        lines.append("- _No strong initiator signal in top contacts_")
    return lines


def _render_blindspots(
    contacts: list[Any],
    champ_signals: dict[str, dict[str, Any]],
    known_emails: set[str],
    known_names: set[str],
    internal_doms: list[str],
    now_epoch: int,
) -> list[str]:
    lines: list[str] = ["", "### Contact Blindspots", ""]
    count = 0
    for email, name, _threads, msgs, last_epoch in contacts[:25]:
        if any(email.endswith(f"@{d}") for d in internal_doms):
            continue
        # historic regression: exclude known automated/system sender addresses before ranking.
        if _is_system_address(email):
            continue
        display = _display_name(name, email)
        if _is_mentioned(email, display, known_emails, known_names):
            continue
        if msgs < 5:
            continue
        days = _days_ago(now_epoch, last_epoch)
        lines.append(f"- {display} ({email}) — {msgs} msgs, {days}d ago")
        count += 1
        if count >= 8:
            break
    if count == 0:
        lines.append("- _All top contacts appear in file_")
    return lines


def build_signals(pursuit_path: str | Path, account: str, db: sqlite3.Connection) -> str | None:
    """Build a Gmail signals markdown section for a pursuit file based on thread activity."""
    path = Path(pursuit_path)
    if path.stem == "gmail-intel":
        return None

    text = path.read_text(encoding="utf-8")
    known_names = names_in_file(text)
    known_emails = set(re.findall(r"[\w.+-]+@[\w.-]+", text))
    internal_doms = _internal_domains()

    pursuit_emails = {e for e in known_emails if not any(e.endswith(d) for d in internal_doms)}
    contacts = get_pursuit_contacts(db, account, pursuit_emails)
    if not contacts:
        return None

    contact_emails = [email for email, *_ in contacts[:25]]
    champ_signals = batch_champion_signals(db, contact_emails)
    now_epoch = int(datetime.now().timestamp())

    header = [
        "",
        "---",
        "",
        "## Gmail Signals",
        f"_Auto-generated {datetime.now().strftime('%Y-%m-%d')} from email data since 2025-10-01_",
        "",
    ]
    top = _render_top_contacts(contacts, champ_signals, known_emails, known_names, now_epoch)
    champs = _render_champion_candidates(contacts, champ_signals)
    blindspots = _render_blindspots(contacts, champ_signals, known_emails, known_names, internal_doms, now_epoch)

    return "\n".join(header + top + champs + blindspots + [""])


# implementation note: apply-intel CLI stub removed — command was already excluded from
# _COMMANDS dispatcher in gmail/cli.py. Utility functions above remain in use
# by gmail.sync, gmail.enrich_pursuits, and their tests.
