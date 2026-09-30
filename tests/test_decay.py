"""Domain tests for account-scoped Gmail relationship decay."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from inspect import Parameter, signature

import pytest

from fieldkit.gmail.decay_domain import DecayQuery, query_decay, render_decay_text
from fieldkit.gmail.query_support import ConfiguredAccountScope

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 9, 28, tzinfo=UTC)
_SCOPE = ConfiguredAccountScope(
    "acme-corp",
    ("acme-corp.example.com",),
    ("internal.example.com",),
)


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE thread_accounts (thread_id TEXT, account TEXT);
        CREATE TABLE messages (
            message_id TEXT, thread_id TEXT, from_addr TEXT, to_addr TEXT,
            cc_addr TEXT, date_epoch INTEGER
        );
        CREATE TABLE people (email TEXT, display_name TEXT);
        """
    )
    return connection


def test_decay_query_options_are_keyword_only() -> None:
    parameters = tuple(signature(DecayQuery).parameters.values())

    assert parameters
    assert all(parameter.kind is Parameter.KEYWORD_ONLY for parameter in parameters)
    with pytest.raises(TypeError):
        signature(DecayQuery).bind(90, False, None, 1, 10, 365)


def _insert_message(
    connection: sqlite3.Connection,
    *,
    message_id: str,
    sender: str,
    recipient: str = "me@internal.example.com",
    cc: str = "",
    days_ago: int,
    thread_id: str = "ta",
) -> None:
    connection.execute("INSERT OR IGNORE INTO thread_accounts VALUES (?, 'acme-corp')", (thread_id,))
    connection.execute(
        "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)",
        (message_id, thread_id, sender, recipient, cc, int((_NOW - timedelta(days=days_ago)).timestamp())),
    )


def _report(
    connection: sqlite3.Connection,
    *,
    show_all: bool = False,
    domain_filter: str | None = None,
    min_messages: int = 1,
    limit: int = 10,
    max_age_days: int | None = 365,
):
    return query_decay(
        connection,
        _SCOPE,
        DecayQuery(
            days_threshold=90,
            show_all=show_all,
            domain_filter=domain_filter,
            min_messages=min_messages,
            limit=limit,
            max_age_days=max_age_days,
            now=_NOW,
        ),
    )


def test_decay_filters_internal_noise_and_other_account_domains() -> None:
    connection = _connection()
    _insert_message(connection, message_id="m1", sender="buyer@acme-corp.example.com", days_ago=120)
    _insert_message(connection, message_id="m2", sender="noreply@acme-corp.example.com", days_ago=120, thread_id="tb")
    _insert_message(connection, message_id="m3", sender="staff@internal.example.com", days_ago=120, thread_id="tc")
    _insert_message(connection, message_id="m4", sender="buyer@globex.example.com", days_ago=120, thread_id="td")

    report = _report(connection)
    connection.close()

    assert tuple(contact.email for contact in report.contacts) == ("buyer@acme-corp.example.com",)


def test_show_all_bypasses_recency_and_default_domain_but_preserves_max_age() -> None:
    connection = _connection()
    _insert_message(connection, message_id="m1", sender="recent@globex.example.com", days_ago=5)
    _insert_message(connection, message_id="m2", sender="old@acme-corp.example.com", days_ago=400, thread_id="tb")

    report = _report(connection, show_all=True)
    connection.close()

    assert tuple(contact.email for contact in report.contacts) == ("recent@globex.example.com",)


def test_zero_max_age_disables_cutoff_when_showing_all() -> None:
    connection = _connection()
    _insert_message(connection, message_id="m1", sender="old@acme-corp.example.com", days_ago=400)

    report = _report(connection, show_all=True, max_age_days=None)
    connection.close()

    assert tuple(contact.email for contact in report.contacts) == ("old@acme-corp.example.com",)


def test_decay_applies_minimum_messages_and_contact_limit() -> None:
    connection = _connection()
    _insert_message(connection, message_id="m1", sender="one@acme-corp.example.com", days_ago=120)
    _insert_message(connection, message_id="m2", sender="two@acme-corp.example.com", days_ago=130, thread_id="tb")
    _insert_message(connection, message_id="m3", sender="two@acme-corp.example.com", days_ago=125, thread_id="tb")
    _insert_message(connection, message_id="m4", sender="three@acme-corp.example.com", days_ago=140, thread_id="tc")
    _insert_message(connection, message_id="m5", sender="three@acme-corp.example.com", days_ago=135, thread_id="tc")

    report = _report(connection, min_messages=2, limit=1)
    connection.close()

    assert tuple(contact.email for contact in report.contacts) == ("three@acme-corp.example.com",)
    assert report.truncated is True


def test_explicit_domain_filter_overrides_configured_account_domains() -> None:
    connection = _connection()
    _insert_message(connection, message_id="m1", sender="buyer@acme-corp.example.com", days_ago=120)
    _insert_message(
        connection, message_id="m2", sender="buyer@subsidiary.example.com.com", days_ago=120, thread_id="tb"
    )

    report = _report(connection, domain_filter="subsidiary.example.com.com")
    connection.close()

    assert tuple(contact.email for contact in report.contacts) == ("buyer@subsidiary.example.com.com",)


def test_explicit_domain_filter_still_applies_when_showing_all_recency() -> None:
    connection = _connection()
    _insert_message(connection, message_id="m1", sender="buyer@acme-corp.example.com", days_ago=5)
    _insert_message(
        connection,
        message_id="m2",
        sender="buyer@subsidiary.example.com.com",
        days_ago=5,
        thread_id="tb",
    )

    report = _report(connection, show_all=True, domain_filter="subsidiary.example.com.com")
    connection.close()

    assert tuple(contact.email for contact in report.contacts) == ("buyer@subsidiary.example.com.com",)


def test_render_decay_text_preserves_valid_quoted_comma_name() -> None:
    connection = _connection()
    _insert_message(
        connection,
        message_id="m1",
        sender='"Doe, Jane" <Jane@Acme-Corp.example.com>',
        days_ago=120,
    )

    report = _report(connection)
    rendered = render_decay_text(report)
    connection.close()

    assert report.contacts[0].name == "Doe, Jane"
    assert "jane@acme-corp.example.com" in rendered
    assert "As of: 2026-09-28" in rendered


def test_render_decay_text_escapes_active_markup_and_table_delimiters() -> None:
    connection = _connection()
    _insert_message(
        connection,
        message_id="m1",
        sender='"<img src=x>|Admin" <buyer@acme-corp.example.com>',
        days_ago=120,
    )

    rendered = render_decay_text(_report(connection))
    connection.close()

    assert "<img" not in rendered
    assert "&lt;img src=x&gt;\\|Admin" in rendered


def test_render_decay_text_escapes_markdown_links_and_images() -> None:
    connection = _connection()
    _insert_message(
        connection,
        message_id="m1",
        sender='"![Admin](https://private.example)" <buyer@acme-corp.example.com>',
        days_ago=120,
    )

    rendered = render_decay_text(_report(connection))
    connection.close()

    assert "![Admin](" not in rendered
    assert r"\!\[Admin\]\(https://private.example\)" in rendered


@pytest.mark.parametrize(
    ("sender", "expected_email", "expected_name"),
    [
        ("alice@acme-corp.example.com (Alice)", "alice@acme-corp.example.com", "Alice"),
        ("Friends: Alice <alice@acme-corp.example.com>;", "alice@acme-corp.example.com", "Alice"),
        ('"Doe, Jane" <Jane@Acme-Corp.example.com>', "jane@acme-corp.example.com", "Doe, Jane"),
    ],
)
def test_decay_uses_canonical_rfc_address_parsing(
    sender: str,
    expected_email: str,
    expected_name: str,
) -> None:
    connection = _connection()
    _insert_message(connection, message_id="m1", sender=sender, days_ago=120)

    report = _report(connection)
    connection.close()

    assert report.contacts[0].email == expected_email
    assert report.contacts[0].name == expected_name


@pytest.mark.parametrize(
    ("sender", "recipient", "cc", "expected_name", "expected_sent"),
    [
        (
            "me@internal.example.com",
            "A <buyer@acme-corp.example.com>, Alice Buyer <buyer@acme-corp.example.com>",
            "",
            "Alice Buyer",
            0,
        ),
        (
            "me@internal.example.com",
            "A <buyer@acme-corp.example.com>",
            "Alice Buyer <buyer@acme-corp.example.com>",
            "Alice Buyer",
            0,
        ),
        (
            "Sender <buyer@acme-corp.example.com>",
            "Friends: Recipient Alias <buyer@acme-corp.example.com>;",
            "",
            "Sender",
            1,
        ),
    ],
)
def test_decay_counts_each_mailbox_once_per_physical_message(
    sender: str,
    recipient: str,
    cc: str,
    expected_name: str,
    expected_sent: int,
) -> None:
    connection = _connection()
    _insert_message(
        connection,
        message_id="m1",
        sender=sender,
        recipient=recipient,
        cc=cc,
        days_ago=120,
    )

    report = _report(connection, show_all=True)
    filtered = _report(connection, show_all=True, min_messages=2)
    connection.close()

    assert len(report.contacts) == 1
    assert report.contacts[0].messages == 1
    assert report.contacts[0].msgs_sent == expected_sent
    assert report.contacts[0].msgs_recv == 1 - expected_sent
    assert report.contacts[0].name == expected_name
    assert filtered.contacts == ()


def test_no_account_threads_is_truthful_empty_success() -> None:
    connection = _connection()

    report = _report(connection)
    rendered = render_decay_text(report)
    connection.close()

    assert report.contacts == ()
    assert report.threads_found is False
    assert "No threads found for account: acme-corp" in rendered
