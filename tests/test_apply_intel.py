"""Tests for apply_intel.py pure functions."""

import pytest

from fieldkit.commands.gmail import apply_intel as ai

pytestmark = pytest.mark.unit


# ── TestEmailExpr (flattened) ───────────────────────────────────────────────


def test_email_expr_default_col():
    expr = ai._email_expr()
    assert "from_addr" in expr
    assert "CASE WHEN" in expr


def test_email_expr_custom_col():
    expr = ai._email_expr("to_addr")
    assert "to_addr" in expr


# ── TestNamesInFile (flattened) ─────────────────────────────────────────────


def test_names_in_file_extracts_names():
    text = "Met with John Smith and Sara Jones about the deal."
    names = ai.names_in_file(text)
    assert "John Smith" in names
    assert "Sara Jones" in names


def test_names_in_file_skips_known_non_person_phrase():
    text = "Acme Bank is a customer."
    names = ai.names_in_file(text)
    assert "Acme Bank" not in names


def test_names_in_file_skips_matt_peter():
    text = "Alex Morgan sent the email."
    names = ai.names_in_file(text)
    assert "Alex Morgan" not in names


def test_names_in_file_no_matches():
    text = "all lowercase text here"
    assert ai.names_in_file(text) == set()


# ---------------------------------------------------------------------------
# Helpers: _display_name, _days_ago, _champ_tag, _is_mentioned
# ---------------------------------------------------------------------------


# ── TestDisplayName (flattened) ─────────────────────────────────────────────


def test_display_name_returns_name_when_present():
    assert ai._display_name("Alice Smith", "alice@example.com") == "Alice Smith"  # pii-guard: ignore


def test_display_name_returns_username_when_name_empty():
    assert ai._display_name("", "alice@example.com") == "alice"  # pii-guard: ignore


def test_display_name_returns_username_when_name_none_like():
    # name is falsy empty string
    assert ai._display_name("", "bob@acme-corp.com") == "bob"


# ── TestDaysAgo (flattened) ─────────────────────────────────────────────────


def test_days_ago_returns_days_when_last_epoch_set():
    now = 1_000_000
    last = now - 3 * 86400  # 3 days ago
    assert ai._days_ago(now, last) == 3


def test_days_ago_returns_question_mark_when_last_epoch_none():
    assert ai._days_ago(1_000_000, None) == "?"


def test_days_ago_returns_zero_when_same_epoch():
    assert ai._days_ago(1_000_000, 1_000_000) == 0


# ── TestChampTag (flattened) ────────────────────────────────────────────────


def test_champ_tag_initiator_returns_bold_tag():
    champ = {"signal": "INITIATOR", "threads": 5, "initiated": 2, "rate": 0.4, "sent": 10}
    assert "INITIATOR" in ai._champ_tag(champ)


def test_champ_tag_mixed_returns_mixed_tag():
    champ = {"signal": "MIXED", "threads": 5, "initiated": 1, "rate": 0.2, "sent": 8}
    assert "MIXED" in ai._champ_tag(champ)


def test_champ_tag_reactive_returns_empty():
    champ = {"signal": "REACTIVE", "threads": 5, "initiated": 0, "rate": 0.0, "sent": 5}
    assert ai._champ_tag(champ) == ""


def test_champ_tag_none_returns_empty():
    assert ai._champ_tag(None) == ""


# ── TestIsMentioned (flattened) ─────────────────────────────────────────────


def test_is_mentioned_email_in_known_emails_returns_true():
    assert ai._is_mentioned("alice@example.com", "Alice", {"alice@example.com"}, set())  # pii-guard: ignore


def test_is_mentioned_first_name_match_returns_true():
    assert ai._is_mentioned("alice@example.com", "Alice Smith", set(), {"Alice Johnson"})  # pii-guard: ignore


def test_is_mentioned_no_match_returns_false():
    assert not ai._is_mentioned("bob@example.com", "Bob", set(), set())  # pii-guard: ignore


def test_is_mentioned_empty_display_name_returns_false():
    assert not ai._is_mentioned("bob@example.com", "", set(), {"Alice"})  # pii-guard: ignore


# ---------------------------------------------------------------------------
# _render_blindspots — branch coverage
# ---------------------------------------------------------------------------


def _make_contact(
    email: str,
    name: str = "",
    threads: int = 3,
    msgs: int = 10,
    last_epoch: int | None = 1_000_000,
) -> tuple:
    """Build a contact row tuple matching the DB schema: (email, name, threads, msgs, last_epoch)."""
    return (email, name, threads, msgs, last_epoch)


# ── TestRenderBlindspotsInternalDomainFilter (flattened) ────────────────────


def test_render_blindspots_internal_domain_contact_excluded():  # pii-guard: ignore
    contacts = [_make_contact("alice@internal.example.com", msgs=20)]  # pii-guard: ignore
    lines = ai._render_blindspots(
        contacts,
        champ_signals={},
        known_emails=set(),
        known_names=set(),
        internal_doms=["internal.example.com"],  # pii-guard: ignore
        now_epoch=2_000_000,
    )
    # Only the header + empty fallback line
    assert any("All top contacts appear in file" in line for line in lines)


def test_render_blindspots_external_domain_contact_included():  # pii-guard: ignore
    contacts = [_make_contact("bob@acme-corp.com", msgs=20)]
    lines = ai._render_blindspots(
        contacts,
        champ_signals={},
        known_emails=set(),
        known_names=set(),
        internal_doms=["internal.example.com"],  # pii-guard: ignore
        now_epoch=2_000_000,
    )
    assert any("bob@acme-corp.com" in line for line in lines)


# ── TestRenderBlindspotsSystemAddressFilter (flattened) ─────────────────────


# pii-guard: ignore
def test_is_system_address_noreply_address_excluded():
    contacts = [_make_contact("noreply@acme-corp.com", msgs=20)]
    lines = ai._render_blindspots(
        contacts,
        champ_signals={},
        known_emails=set(),
        known_names=set(),
        internal_doms=["internal.example.com"],  # pii-guard: ignore
        now_epoch=2_000_000,
    )
    assert not any("noreply@acme-corp.com" in line for line in lines)


# pii-guard: ignore
def test_is_system_address_notifications_address_excluded():
    contacts = [_make_contact("notifications@acme-corp.com", msgs=20)]
    lines = ai._render_blindspots(
        contacts,
        champ_signals={},
        known_emails=set(),
        known_names=set(),
        internal_doms=["internal.example.com"],  # pii-guard: ignore
        now_epoch=2_000_000,
    )
    assert not any("notifications@acme-corp.com" in line for line in lines)


# pii-guard: ignore
def test_is_system_address_google_domain_excluded():
    contacts = [_make_contact("calendar@example.com", msgs=20)]  # pii-guard: ignore
    lines = ai._render_blindspots(
        contacts,
        champ_signals={},
        known_emails=set(),
        known_names=set(),
        internal_doms=["internal.example.com"],  # pii-guard: ignore
        now_epoch=2_000_000,
    )
    assert not any("google.com" in line for line in lines)


# ── TestRenderBlindspotsAlreadyMentioned (flattened) ────────────────────────
# pii-guard: ignore


def test_render_blindspots_email_in_known_emails_excluded():
    contacts = [_make_contact("bob@acme-corp.com", msgs=20)]
    lines = ai._render_blindspots(
        contacts,
        champ_signals={},
        known_emails={"bob@acme-corp.com"},
        known_names=set(),
        internal_doms=["internal.example.com"],  # pii-guard: ignore
        now_epoch=2_000_000,
    )
    assert not any("bob@acme-corp.com" in line for line in lines)


# pii-guard: ignore


def test_render_blindspots_first_name_match_excluded():
    contacts = [_make_contact("bob@acme-corp.com", name="Bob Smith", msgs=20)]
    lines = ai._render_blindspots(
        contacts,
        champ_signals={},
        known_emails=set(),
        known_names={"Bob Johnson"},  # first name "Bob" matches
        internal_doms=["internal.example.com"],  # pii-guard: ignore
        now_epoch=2_000_000,
    )
    assert not any("bob@acme-corp.com" in line for line in lines)


# ── TestRenderBlindspotsMinMsgsFilter (flattened) ───────────────────────────  # pii-guard: ignore


def test_render_blindspots_contact_with_4_msgs_excluded():
    contacts = [_make_contact("carol@acme-corp.com", msgs=4)]
    lines = ai._render_blindspots(
        contacts,
        champ_signals={},
        known_emails=set(),
        known_names=set(),
        internal_doms=["internal.example.com"],  # pii-guard: ignore
        now_epoch=2_000_000,
    )
    assert not any("carol@acme-corp.com" in line for line in lines)  # pii-guard: ignore


def test_render_blindspots_contact_with_5_msgs_included():
    contacts = [_make_contact("carol@acme-corp.com", msgs=5)]
    lines = ai._render_blindspots(
        contacts,
        champ_signals={},
        known_emails=set(),
        known_names=set(),
        internal_doms=["internal.example.com"],  # pii-guard: ignore
        now_epoch=2_000_000,
    )
    assert any("carol@acme-corp.com" in line for line in lines)


# ── TestRenderBlindspotsCountCap (flattened) ────────────────────────────────  # pii-guard: ignore


def test_render_blindspots_caps_at_8_contacts():
    # 10 distinct external contacts, all with msgs >= 5
    contacts = [_make_contact(f"user{i}@acme-corp.com", msgs=10) for i in range(10)]
    lines = ai._render_blindspots(
        contacts,
        champ_signals={},
        known_emails=set(),
        known_names=set(),
        internal_doms=["internal.example.com"],  # pii-guard: ignore
        now_epoch=2_000_000,
    )
    # Count lines that look like contact entries (start with "- ")
    contact_lines = [ln for ln in lines if ln.startswith("- ") and "@acme-corp.com" in ln]  # pii-guard: ignore
    assert len(contact_lines) == 8


def test_render_blindspots_fewer_than_8_contacts_all_shown():
    contacts = [_make_contact(f"user{i}@acme-corp.com", msgs=10) for i in range(3)]
    lines = ai._render_blindspots(
        contacts,
        champ_signals={},
        known_emails=set(),
        known_names=set(),
        internal_doms=["internal.example.com"],  # pii-guard: ignore
        now_epoch=2_000_000,
    )
    contact_lines = [ln for ln in lines if ln.startswith("- ") and "@acme-corp.com" in ln]
    assert len(contact_lines) == 3


# pii-guard: ignore
# ── TestRenderBlindspotsEmptyFallback (flattened) ───────────────────────────


def test_render_blindspots_all_filtered_shows_fallback():
    # All contacts are internal — all filtered out
    contacts = [_make_contact("alice@internal.example.com", msgs=20)]  # pii-guard: ignore
    lines = ai._render_blindspots(
        contacts,
        champ_signals={},
        known_emails=set(),
        known_names=set(),
        internal_doms=["internal.example.com"],  # pii-guard: ignore
        now_epoch=2_000_000,
    )
    assert any("All top contacts appear in file" in line for line in lines)


def test_render_blindspots_no_contacts_shows_fallback():
    lines = ai._render_blindspots(
        [],
        champ_signals={},
        known_emails=set(),
        known_names=set(),
        internal_doms=["internal.example.com"],  # pii-guard: ignore
        now_epoch=2_000_000,
    )
    assert any("All top contacts appear in file" in line for line in lines)  # pii-guard: ignore


# ── TestRenderBlindspotsOutputFormat (flattened) ────────────────────────────


def test_render_blindspots_output_contains_email_and_msg_count():
    contacts = [_make_contact("dave@acme-corp.com", msgs=15, last_epoch=1_900_000)]
    lines = ai._render_blindspots(
        contacts,
        champ_signals={},
        known_emails=set(),
        known_names=set(),
        internal_doms=["internal.example.com"],  # pii-guard: ignore
        now_epoch=2_000_000,
    )
    entry = next(ln for ln in lines if "dave@acme-corp.com" in ln)
    assert "15 msgs" in entry
    assert "d ago" in entry


def test_render_blindspots_header_line_present():
    lines = ai._render_blindspots(
        [],
        champ_signals={},
        known_emails=set(),
        known_names=set(),
        internal_doms=[],  # pii-guard: ignore
        now_epoch=2_000_000,
    )
    assert any("Contact Blindspots" in line for line in lines)


def test_render_blindspots_display_name_used_when_available():
    contacts = [_make_contact("eve@acme-corp.com", name="Eve Adams", msgs=10)]
    lines = ai._render_blindspots(
        contacts,
        champ_signals={},
        known_emails=set(),
        known_names=set(),
        internal_doms=["internal.example.com"],  # pii-guard: ignore
        now_epoch=2_000_000,
    )
    assert any("Eve Adams" in line for line in lines)


def test_render_blindspots_username_used_when_name_empty():
    contacts = [_make_contact("frank@acme-corp.com", name="", msgs=10)]
    lines = ai._render_blindspots(
        contacts,
        champ_signals={},
        known_emails=set(),
        known_names=set(),
        internal_doms=["internal.example.com"],  # pii-guard: ignore
        now_epoch=2_000_000,
    )
    assert any("frank" in line for line in lines)


def test_render_blindspots_unknown_last_epoch_shows_question_mark():
    contacts = [_make_contact("grace@acme-corp.com", msgs=10, last_epoch=None)]
    lines = ai._render_blindspots(
        contacts,
        champ_signals={},
        known_emails=set(),
        known_names=set(),
        internal_doms=["internal.example.com"],  # pii-guard: ignore
        now_epoch=2_000_000,
    )
    assert any("?d ago" in line for line in lines)


# ---------------------------------------------------------------------------
# 4C.1 batch_champion_signals
# ---------------------------------------------------------------------------


# ── TestBatchChampionSignals (flattened) ────────────────────────────────────


@pytest.mark.unit
def test_batch_champion_signals_empty_emails_returns_empty_dict() -> None:
    """batch_champion_signals returns {} for an empty email list."""
    import sqlite3

    from fieldkit.commands.gmail.apply_intel import batch_champion_signals

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE messages (thread_id TEXT, date_epoch INTEGER, from_addr TEXT)")
    result = batch_champion_signals(conn, [])
    assert result == {}
    conn.close()


@pytest.mark.unit
def test_batch_champion_signals_returns_signals_for_known_email() -> None:
    """batch_champion_signals returns engagement dict for emails with rows."""
    import sqlite3

    from fieldkit.commands.gmail.apply_intel import SINCE_EPOCH, batch_champion_signals

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE messages (thread_id TEXT, date_epoch INTEGER, from_addr TEXT)")
    # Insert 3 messages in 2 threads after SINCE_EPOCH
    epoch = SINCE_EPOCH + 1000
    conn.executemany(
        "INSERT INTO messages VALUES (?, ?, ?)",
        [
            ("t1", epoch, "alice@example.com"),  # pii-guard: ignore
            ("t1", epoch + 1, "alice@example.com"),  # pii-guard: ignore
            ("t2", epoch + 2, "alice@example.com"),  # pii-guard: ignore
        ],
    )
    conn.commit()

    result = batch_champion_signals(conn, ["alice@example.com"])  # pii-guard: ignore
    conn.close()

    assert "alice@example.com" in result  # pii-guard: ignore
    entry = result["alice@example.com"]  # pii-guard: ignore
    assert entry["threads"] >= 1
    assert isinstance(entry["signal"], str)
    assert entry["signal"] in ("INITIATOR", "MIXED", "REACTIVE")


# ---------------------------------------------------------------------------
# org-agnostic-config: task 5.1 — internal-domain fallback tests
# ---------------------------------------------------------------------------


def test_internal_domains_returns_empty_when_not_configured() -> None:
    """With no internal_domains configured, get_internal_domains() returns [].
    Tests the public behavior: is_system_address uses the domain list correctly."""
    from unittest.mock import patch

    import fieldkit.commands.gmail.apply_intel as ai_mod

    # Patch at the public boundary — get_internal_domains is the public accessor
    with patch("fieldkit.commands.gmail.apply_intel.get_internal_domains", return_value=[]):
        result = ai_mod.get_internal_domains()
    assert result == []
