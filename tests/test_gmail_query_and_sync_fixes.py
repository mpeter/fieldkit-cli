"""Tests for gmail-query-and-sync OpenSpec fixes.

Covers:
  historic regression: champion lookup uses resolve_name() for display-name matching
  historic regression: champion lookup honours --since date filter
  historic regression: system-address filter in _render_blindspots()
  implementation change: scan_pursuit_affiliations() uses get_fieldkit_home() base path
  implementation change: enrich-pursuits --account flag scopes to a single account
"""

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Shared in-memory DB schema (minimal columns used by champion queries)
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE threads (
    thread_id     TEXT PRIMARY KEY,
    subject       TEXT,
    updated_at    TEXT,
    message_count INTEGER DEFAULT 0
);

CREATE TABLE messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id   TEXT,
    from_addr   TEXT,
    to_addr     TEXT,
    cc_addr     TEXT,
    date_epoch  INTEGER,
    date_str    TEXT,
    subject     TEXT,
    body_plain  TEXT
);

CREATE TABLE people (
    email        TEXT PRIMARY KEY,
    display_name TEXT,
    message_count INTEGER DEFAULT 0,
    thread_count  INTEGER DEFAULT 0,
    initiated_count INTEGER DEFAULT 0,
    domain        TEXT,
    account       TEXT,
    is_internal   INTEGER DEFAULT 0,
    meeting_count INTEGER DEFAULT 0,
    first_seen    TEXT,
    last_seen     TEXT
);
"""


def _make_conn() -> sqlite3.Connection:
    """Return a fresh in-memory connection with the minimal schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# historic regression: champion lookup uses resolve_name() for display-name matching
# ---------------------------------------------------------------------------


# ── TestChampionUsesResolveName (flattened) ─────────────────────────────────


def test_cmd_champion_click_champion_lookup_by_display_name_returns_results() -> None:
    """Champion lookup by display name returns results when contact exists in DB."""
    conn = _make_conn()

    # Insert a person whose display_name is 'Jane Doe'
    conn.execute(
        "INSERT INTO people (email, display_name, message_count) VALUES (?, ?, ?)",
        ("jane.doe@acme-corp.com", "Jane Doe", 10),
    )
    # Seed a thread initiated by Jane
    conn.execute(
        "INSERT INTO threads (thread_id, subject, updated_at, message_count) VALUES (?, ?, ?, ?)",
        ("t1", "Project kickoff", "2025-05-01", 2),
    )
    conn.execute(
        "INSERT INTO messages (thread_id, from_addr, to_addr, date_epoch, date_str, subject) VALUES (?, ?, ?, ?, ?, ?)",
        ("t1", "jane.doe@acme-corp.com", "other@acme-corp.com", 1_746_000_000, "2025-05-01", "Project kickoff"),
    )
    conn.commit()

    from fieldkit.commands.gmail.query import query_champion_signals

    result = query_champion_signals(conn, "Jane Doe")

    # Should find Jane by display name, not return "No people matched"
    assert "No people matched" not in result, f"Expected match for 'Jane Doe', got: {result!r}"
    assert "Champion signal" in result, f"Expected 'Champion signal' header, got: {result!r}"


def test_cmd_champion_click_champion_lookup_no_match_returns_no_people_matched() -> None:
    """Champion lookup for unknown name returns 'No people matched' string."""
    conn = _make_conn()

    from fieldkit.commands.gmail.query import query_champion_signals

    result = query_champion_signals(conn, "Nonexistent Person XYZ")
    assert "No people matched" in result, f"Expected 'No people matched', got: {result!r}"


# ---------------------------------------------------------------------------
# historic regression: champion lookup honours --since date filter
# ---------------------------------------------------------------------------


# ── TestChampionSinceFilter (flattened) ─────────────────────────────────────


def test_cmd_champion_click_champion_since_filters_old_messages() -> None:
    """Messages before the since epoch are excluded from champion counts."""
    conn = _make_conn()

    # Two threads: one old (epoch 1_000_000), one recent (epoch 1_750_000_000)
    conn.execute(
        "INSERT INTO threads (thread_id, subject, updated_at, message_count) VALUES (?, ?, ?, ?)",
        ("t-old", "Old thread", "2001-09-09", 1),
    )
    conn.execute(
        "INSERT INTO messages (thread_id, from_addr, to_addr, date_epoch, date_str, subject) VALUES (?, ?, ?, ?, ?, ?)",
        ("t-old", "alice@acme-corp.com", "other@acme-corp.com", 1_000_000, "2001-09-09", "Old thread"),
    )
    conn.execute(
        "INSERT INTO threads (thread_id, subject, updated_at, message_count) VALUES (?, ?, ?, ?)",
        ("t-new", "New thread", "2025-06-01", 1),
    )
    conn.execute(
        "INSERT INTO messages (thread_id, from_addr, to_addr, date_epoch, date_str, subject) VALUES (?, ?, ?, ?, ?, ?)",
        ("t-new", "alice@acme-corp.com", "other@acme-corp.com", 1_750_000_000, "2025-06-01", "New thread"),
    )
    conn.commit()

    from fieldkit.commands.gmail.query import _champion_thread_stats

    # Without since: should see both threads
    _, total_all, sent_all, _ = _champion_thread_stats(conn, ["alice@acme-corp.com"])
    assert total_all == 2, f"Expected 2 total threads without since, got {total_all}"
    assert sent_all == 2, f"Expected 2 sent messages without since, got {sent_all}"

    # With since=1_700_000_000: only the recent thread qualifies
    cutoff = 1_700_000_000
    _, total_filtered, sent_filtered, _ = _champion_thread_stats(conn, ["alice@acme-corp.com"], since=cutoff)
    assert total_filtered == 1, f"Expected 1 thread after since filter, got {total_filtered}"
    assert sent_filtered == 1, f"Expected 1 sent message after since filter, got {sent_filtered}"


def test_cmd_champion_click_query_champion_signals_since_propagated() -> None:
    """query_champion_signals passes since to _champion_thread_stats."""
    conn = _make_conn()

    conn.execute(
        "INSERT INTO people (email, display_name, message_count) VALUES (?, ?, ?)",
        ("bob@acme-corp.com", "Bob Smith", 5),
    )
    # Only an old message — should be excluded by since filter
    conn.execute(
        "INSERT INTO threads (thread_id, subject, updated_at, message_count) VALUES (?, ?, ?, ?)",
        ("t-bob-old", "Old Bob thread", "2001-01-01", 1),
    )
    conn.execute(
        "INSERT INTO messages (thread_id, from_addr, to_addr, date_epoch, date_str, subject) VALUES (?, ?, ?, ?, ?, ?)",
        ("t-bob-old", "bob@acme-corp.com", "other@acme-corp.com", 1_000_000, "2001-01-01", "Old Bob thread"),
    )
    conn.commit()

    from fieldkit.commands.gmail.query import query_champion_signals

    # With a recent since cutoff, Bob's old message is excluded → counts are 0
    result = query_champion_signals(conn, "Bob Smith", since=1_700_000_000)
    # Bob is found (resolve_name works), but thread counts are 0
    assert "Champion signal" in result, f"Expected champion signal header, got: {result!r}"
    assert "Threads involved in : 0" in result, f"Expected 0 threads after since filter, got: {result!r}"


# ---------------------------------------------------------------------------
# historic regression: system-address filter in _render_blindspots()
# ---------------------------------------------------------------------------


# ── TestSystemAddressFilter (flattened) ─────────────────────────────────────


def test_system_address_filter_system_email_patterns_defined() -> None:
    """SYSTEM_EMAIL_PATTERNS is a frozenset with the required patterns."""
    from fieldkit.commands.gmail.apply_intel import SYSTEM_EMAIL_PATTERNS

    assert isinstance(SYSTEM_EMAIL_PATTERNS, frozenset)
    required = {
        "noreply@",
        "no-reply@",
        "notifications@",
        "calendar-notification@",
        "mailer-daemon@",
        "@google.com",
        "@docusign.net",
    }
    assert required <= SYSTEM_EMAIL_PATTERNS, f"Missing patterns: {required - SYSTEM_EMAIL_PATTERNS}"


def test_system_address_filter_is_system_address_noreply() -> None:
    """noreply@ prefix is detected as a system address."""
    from fieldkit.commands.gmail.apply_intel import _is_system_address

    assert _is_system_address("noreply@acme-corp.com") is True


def test_system_address_filter_is_system_address_no_reply() -> None:
    """no-reply@ prefix is detected as a system address."""
    from fieldkit.commands.gmail.apply_intel import _is_system_address

    assert _is_system_address("no-reply@acme-corp.com") is True


def test_system_address_filter_is_system_address_calendar_notification() -> None:
    """calendar-notification@ prefix is detected as a system address."""
    from fieldkit.commands.gmail.apply_intel import _is_system_address

    assert _is_system_address("calendar-notification@acme-corp.com") is True


def test_system_address_filter_is_system_address_google_domain() -> None:
    """@google.com domain is detected as a system address."""
    from fieldkit.commands.gmail.apply_intel import _is_system_address

    assert _is_system_address("notifications@google.com") is True


def test_system_address_filter_is_system_address_docusign() -> None:
    """@docusign.net domain is detected as a system address."""
    from fieldkit.commands.gmail.apply_intel import _is_system_address

    assert _is_system_address("mailer@docusign.net") is True  # pii-guard: ignore


def test_system_address_filter_is_system_address_normal_contact() -> None:
    """A normal business email is not flagged as a system address."""
    from fieldkit.commands.gmail.apply_intel import _is_system_address

    assert _is_system_address("jane.doe@acme-corp.com") is False


def test_system_address_filter_render_blindspots_excludes_system_addresses() -> None:
    """_render_blindspots() filters out system-address emails before ranking."""
    from fieldkit.commands.gmail.apply_intel import _render_blindspots

    now_epoch = 1_750_000_000
    # contacts: (email, name, threads, msgs, last_epoch)
    contacts = [
        ("noreply@acme-corp.com", "", 5, 20, now_epoch - 86400),  # system — should be excluded
        ("calendar-notification@acme-corp.com", "", 3, 15, now_epoch - 86400),  # system
        ("jane.doe@acme-corp.com", "Jane Doe", 4, 10, now_epoch - 86400),  # real contact
        ("mailer-daemon@acme-corp.com", "", 2, 8, now_epoch - 86400),  # system
    ]

    lines = _render_blindspots(
        contacts,
        champ_signals={},
        known_emails=set(),
        known_names=set(),
        internal_doms=["your-org.com"],  # approved fixture domain (R23)
        now_epoch=now_epoch,
    )
    output = "\n".join(lines)

    # System addresses must not appear
    assert "noreply@acme-corp.com" not in output, "noreply@ must be filtered"
    assert "calendar-notification@acme-corp.com" not in output, "calendar-notification@ must be filtered"
    assert "mailer-daemon@acme-corp.com" not in output, "mailer-daemon@ must be filtered"

    # Real contact must appear
    assert "jane.doe@acme-corp.com" in output, "Real contact must appear in blindspots"


# ---------------------------------------------------------------------------
# implementation change: scan_pursuit_affiliations() uses get_accounts_root() base path (FIX 2)
# ---------------------------------------------------------------------------


# ── TestPursuitAffiliationsDataRoot (flattened) ─────────────────────────────


def test_pursuit_affiliations_data_root_affiliations_called_with_data_root(tmp_path: Path) -> None:
    """scan_pursuit_affiliations is called with explicit accounts_root as base_path."""
    fake_data_root = tmp_path / "fieldkit-data"
    fake_data_root.mkdir()

    # Create a pursuit file with a known champion email  # pii-guard: ignore
    accounts_dir = fake_data_root / "accounts"
    pursuit_dir = accounts_dir / "acme-corp" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    pursuit_file = pursuit_dir / "deal.md"
    pursuit_file.write_text(
        "## Key Stakeholders\n\n"
        "| Name | Title | Notes |\n"
        "| ---- | ----- | ----- |\n"
        "| Alice Champion | VP Eng | champion@acme-corp.com |\n",
        encoding="utf-8",
    )

    from fieldkit.contact import resolver as cr

    # Scan with explicit accounts_root — should find Alice
    results = cr.scan_pursuit_affiliations("Alice Champion", accounts_root=accounts_dir)
    assert len(results) == 1, f"Expected 1 affiliation, got {len(results)}"
    assert results[0]["name"] == "Alice Champion"


def test_pursuit_affiliations_data_root_contact_lookup_cli_passes_accounts_root(tmp_path: Path) -> None:
    """contact find passes get_accounts_root() to scan_pursuit_affiliations (FIX 2)."""
    from fieldkit.commands.contact import find_cmd

    fake_accounts_root = tmp_path / "accounts"
    fake_accounts_root.mkdir()

    captured_accounts_root: list[Path] = []

    def _fake_scan(email_or_name: str, accounts_root: Path | None = None) -> list:  # type: ignore[type-arg]
        captured_accounts_root.append(accounts_root)  # type: ignore[arg-type]
        return []

    with (
        patch("fieldkit.commands.contact.find_cmd.get_accounts_root", return_value=fake_accounts_root),
        patch("fieldkit.commands.contact.find_cmd.scan_pursuit_affiliations", side_effect=_fake_scan),
        patch(
            "fieldkit.commands.contact.find_cmd.resolve",
            return_value={"type": "not_found", "email": "q", "candidates": []},
        ),
    ):
        from click.testing import CliRunner

        runner = CliRunner()
        runner.invoke(find_cmd.cli, ["test@acme-corp.com", "--affiliations"])

    assert len(captured_accounts_root) == 1
    assert captured_accounts_root[0] == fake_accounts_root, (
        f"Expected {fake_accounts_root}, got {captured_accounts_root[0]}"
    )


# ---------------------------------------------------------------------------
# implementation change: enrich-pursuits --account flag
# ---------------------------------------------------------------------------


# ── TestEnrichPursuitsAccountFlag (flattened) ───────────────────────────────


def _cmd_account_click_make_accounts(tmp_path: Path, accounts: list[str]) -> Path:
    """Create minimal pursuit files for each account slug."""
    accounts_root = tmp_path / "accounts"
    for acct in accounts:
        pursuit_dir = accounts_root / acct / "pursuits"
        pursuit_dir.mkdir(parents=True)
        (pursuit_dir / "deal.md").write_text(
            "---\ntitle: test\n---\nWhat We're Selling: Ansible automation",
            encoding="utf-8",
        )
    return accounts_root


def test_cmd_account_click_known_slug_processes_only_that_account(tmp_path: Path) -> None:
    """--account with a known slug processes only that account."""
    from fieldkit.commands.gmail import enrich_pursuits as ep

    accounts_root = _cmd_account_click_make_accounts(tmp_path, ["acme-corp", "globalpay"])

    processed: list[str] = []

    def _fake_build(account: str) -> str | None:
        processed.append(account)
        return None  # no pursuits found — avoids file write

    with (
        patch.object(ep, "_get_accounts", return_value=["acme-corp", "globalpay"]),
        patch.object(ep, "get_accounts_root", return_value=accounts_root),
        patch.object(ep, "build_account_report", side_effect=_fake_build),
    ):
        result = ep._run_enrich(account_slug="acme-corp")

    assert result == 0, f"Expected exit 0, got {result}"
    assert processed == ["acme-corp"], f"Expected only acme-corp processed, got {processed}"


def test_cmd_account_click_unknown_slug_warns_and_exits_cleanly(tmp_path: Path) -> None:
    """--account with an unknown slug prints warning and returns non-zero."""
    from fieldkit.commands.gmail import enrich_pursuits as ep

    with patch.object(ep, "_get_accounts", return_value=["acme-corp", "globalpay"]):
        result = ep._run_enrich(account_slug="no-such-account")

    assert result != 0, "Expected non-zero exit for unknown account slug"


def test_cmd_account_click_no_flag_processes_all_accounts(tmp_path: Path) -> None:
    """Without --account, all configured accounts are processed."""
    from fieldkit.commands.gmail import enrich_pursuits as ep

    processed: list[str] = []

    def _fake_build(account: str) -> str | None:
        processed.append(account)
        return None

    with (
        patch.object(ep, "_get_accounts", return_value=["acme-corp", "globalpay"]),
        patch.object(ep, "get_accounts_root", return_value=tmp_path / "accounts"),
        patch.object(ep, "build_account_report", side_effect=_fake_build),
    ):
        result = ep._run_enrich(account_slug=None)

    assert result == 0
    assert set(processed) == {"acme-corp", "globalpay"}, f"Expected both accounts, got {processed}"


def test_cmd_account_click_cli_account_option_accepted() -> None:
    """CLI accepts --account option without error."""
    from click.testing import CliRunner

    from fieldkit.commands.gmail.enrich_pursuits import cli

    with patch("fieldkit.commands.gmail.enrich_pursuits._run_enrich", return_value=0) as mock_run:
        runner = CliRunner()
        result = runner.invoke(cli, ["--account", "acme-corp"])

    assert result.exit_code == 0, f"CLI exited with {result.exit_code}: {result.output}"
    mock_run.assert_called_once_with(account_slug="acme-corp")


# ---------------------------------------------------------------------------
# FIX 1 — _is_system_address() anchored matching (historic regression, review-council fix)
# ---------------------------------------------------------------------------


# ── TestIsSystemAddressAnchored (flattened) ─────────────────────────────────


def _is_noise_fn(email: str) -> bool:
    from fieldkit.commands.gmail.apply_intel import _is_system_address

    return _is_system_address(email)


def test_is_noise_noreply_plus_addressing() -> None:
    """noreply+tag@acme-corp.com must still be detected after stripping plus suffix."""
    assert _is_noise_fn("noreply+updates@acme-corp.com") is True


def test_is_noise_notifications_plus_addressing() -> None:
    assert _is_noise_fn("notifications+digest@fixture-domain.example.com") is True


def test_is_noise_no_false_positive_noreply_in_domain() -> None:
    """user@noreply-fixture-domain.example.com must NOT match — 'noreply' is in the domain, not local."""
    assert _is_noise_fn("user@noreply-fixture-domain.example.com") is False


def test_is_noise_no_false_positive_noreply_suffix_in_local() -> None:
    """updates-noreply@fixture-domain.example.com must NOT match — local base is 'updates-noreply'."""
    assert _is_noise_fn("updates-noreply@fixture-domain.example.com") is False


def test_is_noise_no_false_positive_contains_noreply_in_local() -> None:
    """send-noreply-digest@fixture-domain.example.com must NOT match — base is 'send-noreply-digest'."""
    assert _is_noise_fn("send-noreply-digest@fixture-domain.example.com") is False


def test_is_noise_no_false_positive_google_subdomain() -> None:
    """user@mail.google.com must NOT match — 'mail.google.com' != 'google.com'."""
    assert _is_noise_fn("user@mail.google.com") is False  # pii-guard: ignore


def test_is_noise_no_false_positive_docusign_subdomain() -> None:
    """user@na3.docusign.net must NOT match — subdomain, not exact domain."""
    assert _is_noise_fn("user@na3.docusign.net") is False  # pii-guard: ignore


def test_is_noise_no_at_sign_returns_false() -> None:
    """Malformed email without '@' must return False, not crash."""
    assert _is_noise_fn("notanemail") is False


def test_is_noise_empty_string_returns_false() -> None:
    assert _is_noise_fn("") is False


def test_is_noise_case_insensitive_local() -> None:
    assert _is_noise_fn("NoReply@fixture-domain.example.com") is True


def test_is_noise_case_insensitive_domain() -> None:
    assert _is_noise_fn("user@GOOGLE.COM") is True  # pii-guard: ignore


# ---------------------------------------------------------------------------
# FIX 4 — resolve_oauth_credentials() env-var priority (historic regression, review-council fix)
# ---------------------------------------------------------------------------


# ── TestResolveOauthCredentials (flattened) ─────────────────────────────────


def _resolve_oauth_credentials_fn() -> tuple[str | None, str | None]:
    from fieldkit.gmail.auth import resolve_oauth_credentials

    return resolve_oauth_credentials()


def test_resolve_oauth_credentials_oauth_client_id_takes_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    """GOOGLE_OAUTH_CLIENT_ID must win when both env vars are set."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "oauth-id")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "legacy-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "oauth-secret")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "legacy-secret")

    client_id, client_secret = _resolve_oauth_credentials_fn()

    assert client_id == "oauth-id", "GOOGLE_OAUTH_CLIENT_ID must take priority"
    assert client_secret == "oauth-secret", "GOOGLE_OAUTH_CLIENT_SECRET must take priority"


def test_resolve_oauth_credentials_falls_back_to_legacy_when_oauth_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Falls back to GOOGLE_CLIENT_ID when GOOGLE_OAUTH_CLIENT_ID is not set."""
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "legacy-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "legacy-secret")

    client_id, client_secret = _resolve_oauth_credentials_fn()

    assert client_id == "legacy-id"
    assert client_secret == "legacy-secret"


def test_resolve_oauth_credentials_returns_none_when_no_credentials_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Returns (None, None) when no credential env vars are set."""
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)

    client_id, client_secret = _resolve_oauth_credentials_fn()

    assert client_id is None
    assert client_secret is None


def test_resolve_oauth_credentials_oauth_id_only_legacy_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mixed env: GOOGLE_OAUTH_CLIENT_ID + GOOGLE_CLIENT_SECRET (partial migration)."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "oauth-id")
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "legacy-secret")

    client_id, client_secret = _resolve_oauth_credentials_fn()

    assert client_id == "oauth-id"
    assert client_secret == "legacy-secret"


def test_resolve_oauth_credentials_oauth_id_empty_string_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty string GOOGLE_OAUTH_CLIENT_ID is falsy — falls back to legacy."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "legacy-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "legacy-secret")

    client_id, client_secret = _resolve_oauth_credentials_fn()

    assert client_id == "legacy-id"
    assert client_secret == "legacy-secret"


# ---------------------------------------------------------------------------
# M5 — _normalize_date() RFC 2822 fix (historic regression / historic regression regression tests)
# ---------------------------------------------------------------------------


# ── TestNormalizeDate (flattened) ───────────────────────────────────────────


def _normalize_date_fn(raw: str) -> str:
    from fieldkit.commands.gmail.query import _normalize_date

    return _normalize_date(raw)


def test_normalize_date_normalize_date_rfc2822_with_weekday() -> None:
    """RFC 2822 date with weekday prefix is parsed to YYYY-MM-DD.

    historic regression regression: "Wed, 6 May 2025 10:00:00 +0000" must return
    "2025-05-06", not the corrupt "Wed, 6 Ma" that raw[:10] would produce.
    """
    result = _normalize_date_fn("Wed, 6 May 2025 10:00:00 +0000")
    assert result == "2025-05-06", f"Expected '2025-05-06', got {result!r}"


def test_normalize_date_normalize_date_iso_prefix() -> None:
    """ISO-formatted date string is returned as its first 10 characters."""
    result = _normalize_date_fn("2025-06-01T10:00:00")
    assert result == "2025-06-01", f"Expected '2025-06-01', got {result!r}"


def test_normalize_date_normalize_date_empty_string() -> None:
    """Empty string input returns empty string — no exception."""
    result = _normalize_date_fn("")
    assert result == "", f"Expected '', got {result!r}"


def test_normalize_date_normalize_date_unparseable() -> None:
    """Truncated / unparseable RFC 2822 string returns empty string gracefully.

    historic regression regression: "Wed, 30 Ap" is the truncated form that triggered
    the original bug.  Must return "" rather than raising or returning garbage.
    """
    result = _normalize_date_fn("Wed, 30 Ap")
    assert result == "", f"Expected '' for unparseable input, got {result!r}"
