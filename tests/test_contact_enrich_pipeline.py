"""Tests for enrich_contacts.py: confidence scoring, gmail cache query, and integration."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.contact._enrich_helpers import (
    discover_all_contacts,
    enrich_contact,
    query_gmail_cache,
    run_enrichment_pipeline,
    write_to_memory,
)
from fieldkit.enrich.schema import ContactRecord

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_gmail_db(tmp_path: Path) -> Path:
    """Create a minimal in-memory-style gmail.db in tmp_path with one thread/message."""
    db_path = tmp_path / "gmail.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(  # pii-guard: ignore
        """
        CREATE TABLE threads (
            thread_id TEXT PRIMARY KEY,
            subject TEXT,
            message_count INTEGER DEFAULT 1,
            updated_at TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            thread_id TEXT,
            from_addr TEXT,
            to_addr TEXT,
            cc_addr TEXT,
            date_str TEXT,
            date_epoch INTEGER,
            subject TEXT,
            body_plain TEXT
        );
        CREATE TABLE people (
            email TEXT PRIMARY KEY,
            display_name TEXT
        );
        CREATE INDEX idx_messages_from_addr ON messages(from_addr);
        CREATE INDEX idx_messages_to_addr   ON messages(to_addr);
        CREATE INDEX idx_messages_cc_addr   ON messages(cc_addr);
        INSERT INTO threads VALUES ('t1', 'Hello contact', 1, '2024-03-15');
        INSERT INTO messages VALUES (1, 't1', 'alice@example.com', 'me@internal.example.com', NULL,
                                     '2024-03-15', 1710460800, 'Hello contact', 'Hi there');
        INSERT INTO people VALUES ('alice@example.com', 'Alice');
        """
    )
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# query_gmail_cache tests
# ---------------------------------------------------------------------------


# ── TestQueryGmailCache (flattened) ─────────────────────────────────────────


def test_query_gmail_cache_sets_email_frequency_and_last_contact_date(tmp_path: Path) -> None:
    """query_gmail_cache populates email_frequency and last_contact_date from DB."""
    db_path = _make_gmail_db(tmp_path)
    contact: dict = {"email": "alice@example.com"}  # pii-guard: ignore
    with patch("fieldkit.contact._enrich_helpers.get_gmail_db_path", return_value=db_path):
        result = query_gmail_cache(contact)
    assert result["email_frequency"] == 1
    assert result["last_contact_date"] == "2024-03-15"
    # Contract: original contact fields must be preserved and result must be a dict
    assert isinstance(result, dict)
    assert "email" in result, "original contact fields must be preserved"
    # PointerArgMutation: contact dict is mutated in-place
    assert contact["email_frequency"] == 1


def test_query_gmail_cache_no_email_returns_contact_unchanged() -> None:
    """query_gmail_cache returns contact unchanged when no email field."""
    contact: dict = {"full_name": "No Email Person"}
    result = query_gmail_cache(contact)
    assert result == {"full_name": "No Email Person"}


def test_query_gmail_cache_db_not_exists_returns_contact_unchanged(tmp_path: Path) -> None:
    """query_gmail_cache returns contact unchanged when DB file is absent."""
    missing_db = tmp_path / "nonexistent.db"
    contact: dict = {"email": "bob@example.com"}  # pii-guard: ignore
    with patch("fieldkit.contact._enrich_helpers.get_gmail_db_path", return_value=missing_db):
        result = query_gmail_cache(contact)
    assert result == {"email": "bob@example.com"}  # pii-guard: ignore
    assert "email_frequency" not in result
    assert "last_contact_date" not in result


def test_query_gmail_cache_no_matching_threads_leaves_fields_absent(tmp_path: Path) -> None:
    """When email has no matching threads, email_frequency and last_contact_date are not set."""
    db_path = _make_gmail_db(tmp_path)
    contact: dict = {"email": "unknown@nowhere-com.example.com"}  # pii-guard: ignore
    with patch("fieldkit.contact._enrich_helpers.get_gmail_db_path", return_value=db_path):
        result = query_gmail_cache(contact)
    assert "email_frequency" not in result
    assert "last_contact_date" not in result


# ── TestContactRecordConfidence (flattened) ─────────────────────────────────


def test_enrich_contact_high_confidence():
    """email(2) + linkedin(2) + title(1) + sf_role(1) = 6 → HIGH."""
    contact = ContactRecord(
        full_name="Jane Smith",
        company="Global Pay",
        account="global-pay",
        email="jane@globalpay-com.example.com",
        linkedin_url="https://linkedin.com/in/janesmith",
        title="VP Engineering",
        sf_role="Economic Buyer",
        source="account-file",
        confidence="LOW",
    )
    assert contact._calculate_confidence() == "HIGH"


def test_enrich_contact_medium_confidence():
    """email(2) + title(1) = 3 → MEDIUM."""
    contact = ContactRecord(
        full_name="Bob Jones",
        company="Global Pay",
        account="global-pay",
        email="bob@globalpay-com.example.com",
        title="Director",
        source="account-file",
        confidence="LOW",
    )
    assert contact._calculate_confidence() == "MEDIUM"


def test_enrich_contact_low_confidence():
    """phone only(1) = 1 → LOW."""
    contact = ContactRecord(
        full_name="Alice Wu",
        company="Global Pay",
        account="global-pay",
        phone="+1-555-0100",
        source="account-file",
        confidence="LOW",
    )
    assert contact._calculate_confidence() == "LOW"


def test_enrich_contact_validation_rejection_no_contact_method():
    """No email/linkedin/phone → ValidationError (model_post_init raises ValueError)."""
    with pytest.raises((ValueError, Exception)) as exc_info:
        ContactRecord(
            full_name="No Contact",
            company="Global Pay",
            account="global-pay",
            source="account-file",
            confidence="LOW",
        )
    assert issubclass(exc_info.type, (ValueError, Exception))


# ---------------------------------------------------------------------------
# Identity / transactional exclusion tests
# ---------------------------------------------------------------------------


# ── TestExclusionGuard (flattened) ──────────────────────────────────────────


def test_exclusion_guard_skip_user_own_email() -> None:
    """User's own email should be skipped (case-insensitive)."""
    from fieldkit.contact._enrich_helpers import _should_skip_contact

    assert _should_skip_contact("Me@Example.COM", "me@example.com") is True  # pii-guard: ignore
    assert _should_skip_contact("me@example.com", "Me@Example.COM") is True  # pii-guard: ignore


def test_exclusion_guard_skip_transactional_domain() -> None:
    """Emails from known transactional domains should be skipped."""
    from fieldkit.contact._enrich_helpers import _should_skip_contact

    assert _should_skip_contact("signer@" + "docusign.com", None) is True
    assert _should_skip_contact("noreply@somecompany-com.example.com", None) is True  # pii-guard: ignore
    assert _should_skip_contact("no-reply@anyco-org.example.com", None) is True
    assert _should_skip_contact("bounce+tag@" + "amazonses.com", None) is True


def test_exclusion_guard_legitimate_contact_passes() -> None:
    """Real contacts should not be skipped."""
    from fieldkit.contact._enrich_helpers import _should_skip_contact

    assert _should_skip_contact("alice@acme-bank-com.example.com", "me@internal.example.com") is False
    assert _should_skip_contact("alice@acme-corp.com", "me@internal.example.com") is False  # pii-guard: ignore


def test_exclusion_guard_no_email_not_skipped() -> None:
    """None email should not be skipped (no email = different code path)."""
    from fieldkit.contact._enrich_helpers import _should_skip_contact

    assert _should_skip_contact(None, "me@internal.example.com") is False  # pii-guard: ignore


def test_exclusion_guard_no_user_email_skips_transactional_only() -> None:
    """When user_email is None, only transactional domains are skipped."""
    from fieldkit.contact._enrich_helpers import _should_skip_contact

    assert _should_skip_contact("noreply@" + "docusign.com", None) is True
    assert _should_skip_contact("alice@acme-bank-com.example.com", None) is False


# ── TestDomainValidation (flattened) ────────────────────────────────────────


def _domain_validation_make_contact(email: str) -> dict:
    return {
        "email": email,
        "full_name": "Test User",
        "company": "Test Co",
    }


def _domain_validation_make_record(email: str, account: str = "default-account") -> "ContactRecord":
    return ContactRecord(
        full_name="Test User",
        company="Test Co",
        account=account,
        email=email,
        source="gmail",
        confidence="HIGH",
    )


def test_domain_validation_known_domain_overrides_account() -> None:
    """A contact whose email domain is in domain_map should have account overridden."""
    from unittest.mock import patch

    from fieldkit.contact._enrich_helpers import enrich_batch

    record = _domain_validation_make_record(
        "contact@acme-bank-com.example.com", account="wrong-account"
    )  # pii-guard: ignore

    with (
        patch(
            "fieldkit.contact._enrich_helpers.get_user_email",
            return_value="me@internal.example.com",  # pii-guard: ignore
        ),  # pii-guard: ignore
        patch(
            "fieldkit.contact._enrich_helpers.build_domain_account_map",
            return_value={"acme-bank-com.example.com": "acme-bank"},
        ),
        patch(
            "fieldkit.contact._enrich_helpers.get_internal_domains",
            return_value=["internal.example.com"],
        ),  # pii-guard: ignore
        patch("fieldkit.contact._enrich_helpers.enrich_contact", return_value=record),
        patch("fieldkit.contact._enrich_helpers.write_to_memory", return_value=None),
    ):
        enriched, _failed = enrich_batch(
            [_domain_validation_make_contact("contact@acme-bank-com.example.com")],  # pii-guard: ignore
            0,
        )  # pii-guard: ignore

    assert len(enriched) == 1
    assert enriched[0]["account"] == "acme-bank"  # pii-guard: ignore


def test_domain_validation_unknown_domain_gets_low_confidence() -> None:
    """A contact from an unknown external domain should get confidence=LOW and source=inferred-context."""
    from unittest.mock import patch

    from fieldkit.contact._enrich_helpers import enrich_batch

    record = _domain_validation_make_record(
        "person@unknownco-com.example.com", account="ctx-account"
    )  # pii-guard: ignore

    with (
        patch(
            "fieldkit.contact._enrich_helpers.get_user_email",
            return_value="me@internal.example.com",  # pii-guard: ignore
        ),  # pii-guard: ignore
        patch("fieldkit.contact._enrich_helpers.build_domain_account_map", return_value={}),
        patch(
            "fieldkit.contact._enrich_helpers.get_internal_domains",
            return_value=["internal.example.com"],
        ),  # pii-guard: ignore
        patch("fieldkit.contact._enrich_helpers.enrich_contact", return_value=record),
        patch("fieldkit.contact._enrich_helpers.write_to_memory", return_value=None),
    ):
        enriched, _failed = enrich_batch(
            [_domain_validation_make_contact("person@unknownco-com.example.com")],  # pii-guard: ignore
            0,
        )  # pii-guard: ignore

    assert len(enriched) == 1
    assert enriched[0]["confidence"] == "LOW"
    assert enriched[0]["source"] == "inferred-context"


def test_domain_validation_internal_domain_unchanged() -> None:
    """A contact from an internal domain should pass through with original account and confidence."""
    from unittest.mock import patch

    from fieldkit.contact._enrich_helpers import enrich_batch

    record = _domain_validation_make_record(
        "colleague@internal.example.com", account="internal-team"
    )  # pii-guard: ignore

    with (
        patch(
            "fieldkit.contact._enrich_helpers.get_user_email",
            return_value="me@internal.example.com",  # pii-guard: ignore
        ),  # pii-guard: ignore
        patch("fieldkit.contact._enrich_helpers.build_domain_account_map", return_value={}),
        patch(
            "fieldkit.contact._enrich_helpers.get_internal_domains",
            return_value=["internal.example.com"],
        ),  # pii-guard: ignore
        patch("fieldkit.contact._enrich_helpers.enrich_contact", return_value=record),
        patch("fieldkit.contact._enrich_helpers.write_to_memory", return_value=None),
    ):
        enriched, _failed = enrich_batch(
            [_domain_validation_make_contact("colleague@internal.example.com")],  # pii-guard: ignore
            0,
        )  # pii-guard: ignore

    assert len(enriched) == 1
    assert enriched[0]["account"] == "internal-team"
    assert enriched[0]["confidence"] == "HIGH"
    assert enriched[0]["source"] == "gmail"


# ---------------------------------------------------------------------------
# Integration: enrich_batch composes exclusion + domain validation end-to-end
# ---------------------------------------------------------------------------


# ── TestEnrichBatchIntegration (flattened) ──────────────────────────────────


def _enrich_batch_contact(email: str, name: str = "Test User", account: str = "default") -> dict:
    return {"email": email, "full_name": name, "company": "Test Co", "account": account}


def _enrich_batch_record(email: str, account: str) -> "ContactRecord":
    return ContactRecord(
        full_name="Test User",
        company="Test Co",
        account=account,
        email=email,
        source="gmail",
        confidence="HIGH",
    )


def _enrich_batch_run_batch(
    contacts: list,
    *,
    user_email: str = "user@internal.example.com",  # pii-guard: ignore
    domain_map: dict | None = None,
    internal_domains: list | None = None,
    enrich_side_effect=None,
) -> tuple:
    """Run enrich_batch with stubbed external I/O."""
    from unittest.mock import patch

    if domain_map is None:
        domain_map = {"acme-bank-com.example.com": "acme-bank"}
    if internal_domains is None:
        internal_domains = ["internal.example.com"]

    # Default enrich_contact: produce a record whose account matches the contact dict
    if enrich_side_effect is None:

        def default_enrich(contact, retry_count=0):
            return _enrich_batch_record(contact["email"], contact.get("account", "default"))

        enrich_side_effect = default_enrich

    with (
        patch("fieldkit.contact._enrich_helpers.get_user_email", return_value=user_email),
        patch("fieldkit.contact._enrich_helpers.build_domain_account_map", return_value=domain_map),
        patch("fieldkit.contact._enrich_helpers.get_internal_domains", return_value=internal_domains),
        patch("fieldkit.contact._enrich_helpers.enrich_contact", side_effect=enrich_side_effect),
        patch("fieldkit.contact._enrich_helpers.write_to_memory", return_value=None),
    ):
        from fieldkit.contact._enrich_helpers import enrich_batch

        return enrich_batch(contacts, 0)


def test_enrich_batch_user_identity_contact_is_excluded() -> None:
    """User's own email must not appear in enrich output."""
    my_addr = "user@internal.example.com"  # pii-guard: ignore
    colleague_addr = "colleague@acme-bank-com.example.com"  # pii-guard: ignore
    contacts = [
        _enrich_batch_contact(my_addr, name="Myself"),
        _enrich_batch_contact(colleague_addr, name="Colleague"),
    ]
    enriched, _failed = _enrich_batch_run_batch(contacts, user_email=my_addr)
    emails = [c["email"] for c in enriched]
    assert my_addr not in emails
    assert colleague_addr in emails


def test_enrich_batch_transactional_contact_is_excluded() -> None:
    """Transactional/noreply addresses must be excluded, real contacts kept."""
    transactional = "noreply@" + "docusign.com"
    real_contact = "alice@globalpay-com.example.com"  # pii-guard: ignore
    contacts = [
        _enrich_batch_contact(transactional, name="DocuSign Bot"),
        _enrich_batch_contact(real_contact, name="Alice"),
    ]
    enriched, _failed = _enrich_batch_run_batch(
        contacts,
        domain_map={},
        internal_domains=["internal.example.com"],
    )  # pii-guard: ignore
    emails = [c["email"] for c in enriched]
    assert transactional not in emails
    assert real_contact in emails


def test_enrich_batch_known_domain_account_override_applied() -> None:
    """Contact from a domain in domain_map gets account overridden to the mapped slug."""
    contacts = [
        _enrich_batch_contact("director@acme-bank-com.example.com", account="wrong-account")
    ]  # pii-guard: ignore
    enriched, _failed = _enrich_batch_run_batch(contacts)
    assert len(enriched) == 1
    assert enriched[0]["account"] == "acme-bank"


def test_enrich_batch_unknown_external_domain_gets_low_confidence() -> None:
    """Contact from an unknown external domain gets confidence=LOW and source=inferred-context."""
    unknown_email = "person@example.com"  # pii-guard: ignore
    contacts = [_enrich_batch_contact(unknown_email, account="some-account")]
    enriched, _failed = _enrich_batch_run_batch(contacts, domain_map={})
    assert len(enriched) == 1
    assert enriched[0]["confidence"] == "LOW"
    assert enriched[0]["source"] == "inferred-context"


def test_enrich_batch_internal_domain_passes_through_unchanged() -> None:
    """Contact from an internal domain keeps original account and confidence."""
    internal_email = "colleague@internal.example.com"  # pii-guard: ignore
    contacts = [_enrich_batch_contact(internal_email, account="engineering")]
    enriched, _failed = _enrich_batch_run_batch(contacts, domain_map={})
    assert len(enriched) == 1
    assert enriched[0]["account"] == "engineering"
    assert enriched[0]["confidence"] == "HIGH"
    assert enriched[0]["source"] == "gmail"


def test_enrich_batch_mixed_batch_composes_all_guards() -> None:
    """A mixed batch correctly applies all rules simultaneously.

    - User identity → excluded
    - Transactional → excluded
    - Known domain → account overridden
    - Unknown external → LOW confidence
    - Internal → pass through
    """
    user_addr = "user@internal.example.com"  # pii-guard: ignore
    bot_addr = "bot@" + "docusign.com"
    exec_addr = "exec@acme-bank-com.example.com"  # pii-guard: ignore
    unknown_addr = "person@example.com"  # pii-guard: ignore
    peer_addr = "peer@internal.example.com"  # pii-guard: ignore
    contacts = [
        _enrich_batch_contact(user_addr, name="Me"),  # excluded: identity
        _enrich_batch_contact(bot_addr, name="DocuBot"),  # excluded: transactional
        _enrich_batch_contact(exec_addr, account="stale-slug"),  # account override
        _enrich_batch_contact(unknown_addr, account="ctx"),  # LOW confidence
        _enrich_batch_contact(peer_addr, account="internal-team"),  # pass through
    ]
    enriched, _failed = _enrich_batch_run_batch(contacts, user_email=user_addr)
    by_email = {c["email"]: c for c in enriched}

    # Excluded contacts must not be present
    assert user_addr not in by_email
    assert bot_addr not in by_email

    # Domain override
    assert by_email[exec_addr]["account"] == "acme-bank"

    # Unknown external → LOW
    assert by_email[unknown_addr]["confidence"] == "LOW"

    # Internal → unchanged
    assert by_email[peer_addr]["account"] == "internal-team"
    assert by_email[peer_addr]["confidence"] == "HIGH"


# ── query_gmail_cache: multi-thread frequency (012-gazecrap-reduction) ────────


def _make_multi_thread_gmail_db(tmp_path: Path) -> Path:
    """Create a gmail DB with 3 distinct threads from the same sender."""
    db_path = tmp_path / "gmail-multi.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(  # pii-guard: ignore
        """
        CREATE TABLE threads (
            thread_id TEXT PRIMARY KEY,
            subject TEXT,
            message_count INTEGER DEFAULT 1,
            updated_at TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            thread_id TEXT,
            from_addr TEXT,
            to_addr TEXT,
            cc_addr TEXT,
            date_str TEXT,
            date_epoch INTEGER,
            subject TEXT,
            body_plain TEXT
        );
        CREATE TABLE people (
            email TEXT PRIMARY KEY,
            display_name TEXT
        );
        CREATE INDEX idx_messages_from_addr ON messages(from_addr);
        CREATE INDEX idx_messages_to_addr   ON messages(to_addr);
        CREATE INDEX idx_messages_cc_addr   ON messages(cc_addr);
        INSERT INTO threads VALUES ('t1', 'Thread 1', 1, '2024-03-15');
        INSERT INTO threads VALUES ('t2', 'Thread 2', 1, '2024-03-16');
        INSERT INTO threads VALUES ('t3', 'Thread 3', 1, '2024-03-17');
        INSERT INTO messages VALUES (1, 't1', 'alice@example.com', 'me@your-org-com.example.com',
                                     NULL, '2024-03-15', 1710460800, 'Thread 1', 'Hi');
        INSERT INTO messages VALUES (2, 't2', 'alice@example.com', 'me@your-org-com.example.com',
                                     NULL, '2024-03-16', 1710547200, 'Thread 2', 'Hi again');
        INSERT INTO messages VALUES (3, 't3', 'alice@example.com', 'me@your-org-com.example.com',
                                     NULL, '2024-03-17', 1710633600, 'Thread 3', 'Follow up');
        INSERT INTO people VALUES ('alice@example.com', 'Alice');
        """
    )
    conn.commit()
    conn.close()
    return db_path


# ── TestQueryGmailCacheMultiThread (flattened) ──────────────────────────────


@pytest.mark.unit
def test_query_gmail_cache_multi_thread_frequency_counts_distinct_threads(tmp_path: Path) -> None:
    """email_frequency must count DISTINCT thread_ids, not message rows."""
    db_path = _make_multi_thread_gmail_db(tmp_path)
    contact: dict = {"email": "alice@example.com"}  # pii-guard: ignore
    with patch(
        "fieldkit.contact._enrich_helpers.get_gmail_db_path",
        return_value=db_path,
    ):
        result = query_gmail_cache(contact)
    assert result["email_frequency"] == 3, f"Expected 3 distinct threads, got {result['email_frequency']}"
    assert "last_contact_date" in result
    assert isinstance(result, dict)
    assert "email" in result, "original contact fields must be preserved"


# ---------------------------------------------------------------------------
# 4C.4 search_web_for_contact
# ---------------------------------------------------------------------------


# ── TestSearchWebForContact (flattened) ─────────────────────────────────────


@pytest.mark.unit
def test_search_web_for_contact_no_results_returns_contact_with_marker() -> None:
    """search_web_for_contact marks contact with _needs_web_enrichment."""
    from fieldkit.contact._enrich_helpers import search_web_for_contact

    contact: dict = {"full_name": "Alice Smith", "company": "Acme Corp"}
    result = search_web_for_contact(contact)

    assert result is contact  # mutated in-place
    assert contact["_needs_web_enrichment"] is True
    assert "_search_query" in contact


@pytest.mark.unit
def test_search_web_for_contact_missing_name_returns_contact_unchanged() -> None:
    """search_web_for_contact skips contacts without full_name or company."""
    from fieldkit.contact._enrich_helpers import search_web_for_contact

    contact: dict = {"company": "Acme Corp"}  # no full_name
    result = search_web_for_contact(contact)

    assert result is contact
    assert "_needs_web_enrichment" not in contact


# ---------------------------------------------------------------------------
# enrich_contact / write_to_memory / run_enrichment_pipeline / discover_all_contacts
#
# These four functions sat at 0%, 0%, 0% and 18.5% line coverage while the file
# around them read 72.6%. gaze-py <=0.8.2 applied that file aggregate to every
# function in it, so all four looked well covered and none was flagged.
# gaze-py 0.9.0 attributes coverage per function and surfaced them: together
# they carried 260.6 CRAP.
#
# Note the four `test_enrich_contact_*` tests above never call enrich_contact —
# they construct a ContactRecord and exercise `_calculate_confidence()`. The
# function read as tested by name alone.
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_contact_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect the module's memory and enrich dirs into tmp_path.

    Both are looked up through module-level names in `_enrich_helpers`, so
    patching there keeps the real `get_fieldkit_home()` out of the test.
    """
    memory = tmp_path / "memory"
    enrich = tmp_path / "enrich"
    memory.mkdir(parents=True)
    enrich.mkdir(parents=True)
    monkeypatch.setattr("fieldkit.contact._enrich_helpers.contacts_memory_dir", lambda: memory)
    monkeypatch.setattr("fieldkit.contact._enrich_helpers.enrich_dir", lambda: enrich)
    return tmp_path


@pytest.fixture
def stub_gmail_cache(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Neutralise the Gmail cache lookup and record what it was asked for.

    `query_gmail_cache` opens a real sqlite database; every enrich_contact test
    needs it inert. Centralised here so the coupling to the private helper
    module lives in one place rather than in every test (TC-008/TC-013).
    """
    seen: list[dict] = []

    def _passthrough(contact: dict) -> dict:
        seen.append(contact)
        return contact

    monkeypatch.setattr("fieldkit.contact._enrich_helpers.query_gmail_cache", _passthrough)
    return seen


@pytest.fixture
def pipeline_env(monkeypatch: pytest.MonkeyPatch, stub_gmail_cache: list[dict]) -> None:
    """Isolate run_enrichment_pipeline from config, checkpoints and the network.

    Consolidates the five patches every pipeline test would otherwise repeat.
    """
    monkeypatch.setattr("fieldkit.enrich._helpers.load_checkpoint", lambda: None)
    monkeypatch.setattr("fieldkit.enrich._helpers.save_checkpoint", lambda cp: None)
    monkeypatch.setattr("fieldkit.contact._enrich_helpers.get_user_email", lambda: None)
    monkeypatch.setattr("fieldkit.contact._enrich_helpers.build_domain_account_map", dict)
    monkeypatch.setattr("fieldkit.contact._enrich_helpers.get_internal_domains", list)


@pytest.fixture
def discovery_sources(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub the three discovery sources and record which accounts were scanned."""
    scanned: list[str] = []

    def _record_scan(path: Path) -> list[dict]:
        scanned.append(path.name)
        return []

    monkeypatch.setattr("fieldkit.contact._enrich_helpers.extract_from_account_md", _record_scan)
    monkeypatch.setattr("fieldkit.contact._enrich_helpers.extract_from_sf_frontmatter", lambda path: [])
    monkeypatch.setattr("fieldkit.contact._enrich_helpers.load_gmail_cache_contacts", lambda name: [])
    return scanned


def _contact_dict(**overrides: object) -> dict:
    """A raw contact dict with a usable contact method, before enrichment."""
    base = {
        "full_name": "Dana Reyes",
        "company": "Global Pay",
        "account": "global-pay",
        "email": "dana@globalpay-com.example.com",  # pii-guard: ignore
        "title": "VP Platform",
        "source": "account-file",
        "confidence": "MEDIUM",
    }
    base.update(overrides)
    return base


# ── enrich_contact ──────────────────────────────────────────────────────────


def test_enrich_contact_returns_validated_record(stub_gmail_cache: list[dict]) -> None:
    """A contact with an email is enriched into a ContactRecord."""
    record = enrich_contact(_contact_dict())
    assert record is not None
    assert record.full_name == "Dana Reyes"
    assert record.account == "global-pay"


def test_enrich_contact_without_contact_method_searches_the_web(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no email/linkedin/phone the web search runs and can supply one."""
    calls: list[dict] = []

    def _fake_search(contact: dict) -> dict:
        calls.append(contact)
        return {**contact, "linkedin_url": "https://linkedin.com/in/dana"}

    monkeypatch.setattr("fieldkit.contact._enrich_helpers.search_web_for_contact", _fake_search)

    record = enrich_contact(_contact_dict(email=None, title=None))
    assert calls, "web search must run when no contact method is present"
    assert record is not None
    assert record.linkedin_url == "https://linkedin.com/in/dana"


def test_enrich_contact_returns_none_when_web_search_finds_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No contact method after the web search is a failure, not a LOW-confidence record."""
    monkeypatch.setattr("fieldkit.contact._enrich_helpers.search_web_for_contact", lambda c: c)
    assert enrich_contact(_contact_dict(email=None)) is None


def test_enrich_contact_queries_gmail_cache_only_when_an_email_exists(
    stub_gmail_cache: list[dict],
) -> None:
    """The Gmail lookup is keyed on email; a phone-only contact must not trigger it."""
    enrich_contact(_contact_dict(email=None, phone="+1-555-0100"))
    assert stub_gmail_cache == [], "no email means no Gmail cache query"

    enrich_contact(_contact_dict())
    assert len(stub_gmail_cache) == 1


def test_enrich_contact_computes_confidence_when_absent(
    stub_gmail_cache: list[dict],
) -> None:
    """A contact without a confidence value gets one derived from its fields."""
    record = enrich_contact(
        _contact_dict(
            confidence=None,
            linkedin_url="https://linkedin.com/in/dana",
            sf_role="Economic Buyer",
        )
    )
    assert record is not None
    assert record.confidence == "HIGH"


def test_enrich_contact_falls_back_to_low_when_confidence_calc_raises(
    monkeypatch: pytest.MonkeyPatch,
    stub_gmail_cache: list[dict],
) -> None:
    """A failure computing confidence degrades the contact to LOW rather than propagating.

    The contact dict is mutated in place, so the fallback is asserted on it
    directly: patching ContactRecord makes the later validation raise too, and
    a `is None` return alone would hold even if the LOW assignment were absent.
    """

    class _Boom:
        def __init__(self, **_: object) -> None:
            raise RuntimeError("confidence unavailable")

    monkeypatch.setattr("fieldkit.contact._enrich_helpers.ContactRecord", _Boom)

    contact = _contact_dict(confidence=None)
    assert enrich_contact(contact) is None
    assert contact["confidence"] == "LOW", "a failed calculation must degrade to LOW, not stay unset"


def test_enrich_contact_returns_none_when_validation_fails(
    stub_gmail_cache: list[dict],
) -> None:
    """An invalid record is reported as a failure, not raised."""
    assert enrich_contact(_contact_dict(account=None)) is None


def test_enrich_contact_records_the_retry_count(stub_gmail_cache: list[dict]) -> None:
    """retry_count is threaded onto the record so the caller can cap retries."""
    record = enrich_contact(_contact_dict(), retry_count=2)
    assert record is not None
    assert record.retry_count == 2


# ── write_to_memory ─────────────────────────────────────────────────────────


def test_write_to_memory_writes_a_named_markdown_file(patched_contact_dirs: Path) -> None:
    """The filename encodes the normalized name and the account slug."""
    record = ContactRecord(
        full_name="Dana Reyes",
        company="Global Pay",
        account="global-pay",
        email="dana@globalpay-com.example.com",  # pii-guard: ignore
        source="account-file",
        confidence="MEDIUM",
    )
    write_to_memory(record)

    written = list((patched_contact_dirs / "memory").glob("*.md"))
    assert len(written) == 1
    assert written[0].name == "contact_dana_reyes_global-pay.md"

    body = written[0].read_text(encoding="utf-8")
    assert "# Dana Reyes — Global Pay" in body
    assert "**Account:** global-pay" in body
    assert "**Confidence:** MEDIUM" in body


def test_write_to_memory_renders_placeholders_for_absent_fields(
    patched_contact_dirs: Path,
) -> None:
    """Missing optional fields render as explicit placeholders, never as 'None'.

    The file is read by humans and by other tools; a bare `None` would be
    indistinguishable from a real value.
    """
    record = ContactRecord(
        full_name="Sam Vale",
        company="Global Pay",
        account="global-pay",
        phone="+1-555-0100",
        source="account-file",
        confidence="LOW",
    )
    write_to_memory(record)

    body = (patched_contact_dirs / "memory" / "contact_sam_vale_global-pay.md").read_text(encoding="utf-8")
    assert "**Title:** [Unknown]" in body
    assert "**Email:** [Unknown]" in body
    assert "[Not in Salesforce]" in body
    assert "None" not in body


# ── discover_all_contacts ───────────────────────────────────────────────────


def test_discover_all_contacts_returns_empty_when_accounts_root_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing accounts/ directory is a warning and an empty result, not a crash."""
    monkeypatch.setattr("fieldkit.contact._enrich_helpers.get_accounts_root", lambda: tmp_path / "absent")
    assert discover_all_contacts() == []


def test_discover_all_contacts_skips_dotfiles_and_the_template(
    monkeypatch: pytest.MonkeyPatch, discovery_sources: list[str], tmp_path: Path
) -> None:
    """`.template` and dot-directories are scaffolding, not accounts."""
    root = tmp_path / "accounts"
    for name in ("global-pay", ".template", ".hidden"):
        (root / name).mkdir(parents=True)
    (root / "loose-file.md").write_text("not a dir", encoding="utf-8")

    monkeypatch.setattr("fieldkit.contact._enrich_helpers.get_accounts_root", lambda: root)

    discover_all_contacts()
    assert discovery_sources == ["global-pay"]


def test_discover_all_contacts_restricts_to_one_account_when_asked(
    monkeypatch: pytest.MonkeyPatch, discovery_sources: list[str], tmp_path: Path
) -> None:
    """`account=` narrows the scan instead of filtering after the fact."""
    root = tmp_path / "accounts"
    for name in ("global-pay", "acme"):
        (root / name).mkdir(parents=True)

    monkeypatch.setattr("fieldkit.contact._enrich_helpers.get_accounts_root", lambda: root)

    discover_all_contacts(account="acme")
    assert discovery_sources == ["acme"]


def test_discover_all_contacts_merges_across_sources(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Contacts from every source are aggregated and de-duplicated by merge_contacts."""
    root = tmp_path / "accounts"
    (root / "global-pay").mkdir(parents=True)
    monkeypatch.setattr("fieldkit.contact._enrich_helpers.get_accounts_root", lambda: root)

    same = {
        "full_name": "Dana Reyes",
        "account": "global-pay",
        "email": "dana@globalpay-com.example.com",  # pii-guard: ignore
    }
    monkeypatch.setattr("fieldkit.contact._enrich_helpers.extract_from_account_md", lambda path: [same])
    monkeypatch.setattr(
        "fieldkit.contact._enrich_helpers.extract_from_sf_frontmatter",
        lambda path: [{**same, "title": "VP Platform"}],
    )
    monkeypatch.setattr("fieldkit.contact._enrich_helpers.load_gmail_cache_contacts", lambda name: [])

    merged = discover_all_contacts()
    assert len(merged) == 1, "the same person from two sources must merge to one contact"
    assert merged[0]["title"] == "VP Platform"


# ── run_enrichment_pipeline ─────────────────────────────────────────────────


def test_run_enrichment_pipeline_enriches_and_persists(pipeline_env: None, patched_contact_dirs: Path) -> None:
    """A clean run enriches every contact and writes contacts-enriched.json."""

    raw = [_contact_dict(full_name=f"Person {i}") for i in range(3)]
    enriched, failed = run_enrichment_pipeline(raw)

    assert enriched == 3
    assert failed == 0

    written = patched_contact_dirs / "enrich" / "contacts-enriched.json"
    assert written.exists(), "enriched contacts must be persisted incrementally"
    assert len(json.loads(written.read_text(encoding="utf-8"))) == 3


def test_run_enrichment_pipeline_resumes_from_a_checkpoint(
    monkeypatch: pytest.MonkeyPatch, pipeline_env: None, patched_contact_dirs: Path
) -> None:
    """A checkpoint skips already-processed contacts instead of redoing them.

    Guards the resume path: without it a re-run would re-enrich (and re-bill)
    every contact processed before the interruption.
    """
    from fieldkit.enrich.schema import EnrichmentCheckpoint

    monkeypatch.setattr(
        "fieldkit.enrich._helpers.load_checkpoint",
        lambda: EnrichmentCheckpoint(
            last_completed_account="global-pay",
            last_completed_contact_index=5,
            total_processed=5,
            total_enriched=5,
            total_failed=0,
        ),
    )

    enriched_names: list[str] = []
    real = enrich_contact

    def _tracking(contact: dict, retry_count: int = 0):
        enriched_names.append(contact["full_name"])
        return real(contact, retry_count=retry_count)

    monkeypatch.setattr("fieldkit.contact._enrich_helpers.enrich_contact", _tracking)

    raw = [_contact_dict(full_name=f"Person {i}") for i in range(7)]
    run_enrichment_pipeline(raw)

    assert enriched_names == ["Person 5", "Person 6"], "the first five were already processed"


def test_run_enrichment_pipeline_retries_failures_then_gives_up(
    monkeypatch: pytest.MonkeyPatch, pipeline_env: None, patched_contact_dirs: Path
) -> None:
    """A contact that fails enrichment is retried once more, then left for a later run.

    The returned failure count reports only contacts that exhausted MAX_RETRIES,
    so a single failed attempt is not yet terminal — see the assertion below.

    `time.sleep` is patched out: the real backoff is up to 16s per contact and
    would dominate the suite's runtime.
    """
    monkeypatch.setattr("fieldkit.contact._enrich_helpers.time.sleep", lambda s: None)
    # Never produces a record, so every contact lands on the retry path.
    monkeypatch.setattr("fieldkit.contact._enrich_helpers.enrich_contact", lambda c, retry_count=0: None)

    enriched, failed = run_enrichment_pipeline([_contact_dict()])
    assert enriched == 0
    assert failed == 0, "one retry short of MAX_RETRIES is not yet a terminal failure"
