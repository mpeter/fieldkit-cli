"""Unit tests for lib/gmail_discover.py — Gmail source discovery layer.

Strategy:
- tmp_path for both gmail.db (minimal test schema with fake Gemini emails)
  and pipeline.db so no real data is touched.
- All tests are pure unit tests — no network, no real gmail.db.
"""

import sqlite3
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Self
from unittest.mock import patch

import pytest

from fieldkit.commands.ingest.registry import PIPELINES
from fieldkit.config import ConfigError
from fieldkit.errors import GmailSyncPartialError, SQLiteSnapshotError
from fieldkit.gmail.discover import (
    GmailCandidate,
    _extract_doc_id,
    _parse_subject,
    get_gmail_db_path,
    scan_gemini_candidates,
)
from fieldkit.gmail.exceptions import GmailDbNotFoundError, GmailSchemaError
from fieldkit.gmail.publication import GMAIL_QUERY_READY_KEY, apply_gmail_page, initialize_gmail_publication
from fieldkit.ingest.db import init_db
from fieldkit.ingest.sources import filter_gemini_candidates_for_account
from fieldkit.sqlite_publication import SQLiteMutationConnection

pytestmark = pytest.mark.unit

# ── gmail.db fixture helpers ────────────────────────────────────────────────


class _PublishedGmailFixture:
    """Small write facade that keeps legacy fixture setup on managed generations."""

    def __init__(self, path: Path) -> None:
        self.path = path
        initialize_gmail_publication(path)
        self.execute(
            "INSERT OR REPLACE INTO sync_state(key, value) VALUES (?, 'true')",
            (GMAIL_QUERY_READY_KEY,),
        )

    def execute(self, statement: str, parameters: tuple[object, ...] = ()) -> None:
        def mutation(connection: SQLiteMutationConnection) -> None:
            connection.execute(statement, parameters)

        apply_gmail_page(self.path, mutation)

    def commit(self) -> None:
        """Match sqlite fixture call sites; each statement is already published."""

    def close(self) -> None:
        """Match sqlite fixture call sites; publications own their connections."""


def _make_gmail_db(path: Path) -> _PublishedGmailFixture:
    """Create a query-ready managed Gmail publication at *path*."""
    return _PublishedGmailFixture(path)


def _insert_gemini_email(
    gmail_conn: _PublishedGmailFixture,
    *,
    message_id: str,
    subject: str = 'Notes: "Test Meeting" May 1, 2026',
    doc_id: str = "DocID_ABC123",
    body_html: str | None = None,
    body_plain: str | None = None,
    date_epoch: int | None = None,
    to_addr: str | None = None,
    cc_addr: str | None = None,
    from_addr: str = "Gemini <gemini-notes@google.com>",
) -> None:
    """Insert a fake Gemini meeting-notes email into the test gmail.db."""
    if date_epoch is None:
        date_epoch = int((datetime.now(UTC) - timedelta(days=1)).timestamp())

    doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
    if body_html is None:
        body_html = f'<p>View your notes: <a href="{doc_url}">Open doc</a></p>'
    if body_plain is None:
        body_plain = f"View your notes: {doc_url}"

    # Ensure a thread exists
    gmail_conn.execute(
        "INSERT OR IGNORE INTO threads (thread_id, subject) VALUES (?, ?)",
        (message_id, subject),
    )
    gmail_conn.execute(
        """
        INSERT OR IGNORE INTO messages
            (message_id, thread_id, from_addr, to_addr, cc_addr, subject, body_html, body_plain, date_epoch)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            message_id,
            from_addr,
            to_addr,
            cc_addr,
            subject,
            body_html,
            body_plain,
            date_epoch,
        ),
    )
    gmail_conn.commit()


def test_scan_requires_exact_gemini_sender_and_parses_rfc_recipients(tmp_path: Path) -> None:
    gmail_db = tmp_path / "gmail.db"
    conn = _make_gmail_db(gmail_db)
    _insert_gemini_email(
        conn,
        message_id="spoofed",
        doc_id="SpoofedDoc",
        from_addr="Attacker <not-gemini-notes@google.com.example.com>",
    )
    _insert_gemini_email(
        conn,
        message_id="valid",
        doc_id="ValidDoc",
        to_addr='"Doe, Jamie" <jamie@acme-corp.example.com>, Team: alex@subsidiary.example.com;',
        cc_addr="Jamie <jamie@acme-corp.example.com>",
    )

    candidates = scan_gemini_candidates(gmail_db, limit=10, max_age_days=None)

    assert [candidate.source_id for candidate in candidates] == ["ValidDoc"]
    assert candidates[0].recipient_addresses == (
        "jamie@acme-corp.example.com",
        "alex@subsidiary.example.com",
        "jamie@acme-corp.example.com",
    )


def test_scan_rejects_multi_mailbox_sender_even_when_expected_mailbox_is_present(tmp_path: Path) -> None:
    gmail_db = tmp_path / "gmail.db"
    conn = _make_gmail_db(gmail_db)
    _insert_gemini_email(
        conn,
        message_id="multi-sender",
        doc_id="MultiSenderDoc",
        from_addr="Attacker <attacker@example.net>, Gemini <gemini-notes@google.com>",
    )

    candidates = scan_gemini_candidates(gmail_db, limit=10, max_age_days=None)

    assert candidates == []


def test_scan_rejects_malformed_sender_with_fixed_schema_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fieldkit.__main__ import main

    gmail_db = tmp_path / "gmail.db"
    conn = _make_gmail_db(gmail_db)
    _insert_gemini_email(
        conn,
        message_id="malformed-sender",
        doc_id="MalformedSenderDoc",
        from_addr="not-an-address",
    )

    with pytest.raises(GmailSchemaError, match="could not be queried safely"):
        scan_gemini_candidates(gmail_db, limit=10, max_age_days=None)

    cli_db = tmp_path / "cli-gmail.db"
    cli_conn = _make_gmail_db(cli_db)
    _insert_gemini_email(
        cli_conn,
        message_id="cli-malformed-sender",
        doc_id="CliMalformedSenderDoc",
        from_addr="not-an-address",
    )
    with patch("fieldkit.gmail.discover.get_gmail_db_path", return_value=cli_db):
        exit_code = main(["ingest", "discover", "--pipeline", "transcript-ingest", "--dry-run"])

    captured = capsys.readouterr()
    assert exit_code == 3
    assert str(tmp_path) not in captured.out + captured.err
    assert "Traceback" not in captured.out + captured.err


def _publish_gemini_email(path: Path, *, message_id: str, doc_id: str) -> None:
    """Publish one query-ready fictional Gemini message through the managed writer."""
    initialize_gmail_publication(path)

    def mutation(connection: SQLiteMutationConnection) -> None:
        connection.execute(
            "INSERT OR REPLACE INTO sync_state(key, value) VALUES (?, 'true')",
            (GMAIL_QUERY_READY_KEY,),
        )
        connection.execute(
            "INSERT INTO threads(thread_id, subject, message_count) VALUES (?, ?, 1)",
            (message_id, "Fictional meeting"),
        )
        connection.execute(
            """
            INSERT INTO messages(
                message_id, thread_id, from_addr, to_addr, cc_addr, subject,
                date_str, date_epoch, labels, body_plain, body_html, size_bytes, snippet
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                message_id,
                "Gemini <gemini-notes@google.com>",
                "seller@example.com",
                "",
                'Notes: "Fictional meeting" May 1, 2026',
                "2026-05-01",
                int(datetime.now(UTC).timestamp()),
                "[]",
                f"https://docs.google.com/document/d/{doc_id}/edit",
                "",
                64,
                "Fictional meeting",
            ),
        )

    apply_gmail_page(path, mutation)


# ── get_gmail_db_path ───────────────────────────────────────────────────────


def test_get_gmail_db_path_ends_with_gmail_db() -> None:
    """Canonical path must end with gmail.db inside the configured data root."""
    p = get_gmail_db_path()
    assert p.name == "gmail.db"
    assert p.parent.name == "data"


def test_get_gmail_db_path_returns_path_object() -> None:
    """get_gmail_db_path() must return an absolute pathlib.Path."""
    result = get_gmail_db_path()
    assert isinstance(result, Path)
    assert result.is_absolute(), "get_gmail_db_path must return an absolute Path"


# ── _extract_doc_id (unit) ──────────────────────────────────────────────────


def test_extract_doc_id_from_html() -> None:
    """Doc ID is extracted from body_html when present."""
    html = '<a href="https://docs.google.com/document/d/DOCID1234/edit">open</a>'
    assert _extract_doc_id(html, "") == "DOCID1234"


def test_extract_doc_id_falls_back_to_plain() -> None:
    """Falls back to body_plain when body_html is empty."""
    plain = "See https://docs.google.com/document/d/PLAINID999 for notes."
    assert _extract_doc_id("", plain) == "PLAINID999"


def test_extract_doc_id_prefers_html_over_plain() -> None:
    """body_html is preferred even when body_plain also has a URL."""
    html = '<a href="https://docs.google.com/document/d/HTML_ID/edit">x</a>'
    plain = "https://docs.google.com/document/d/PLAIN_ID/edit"
    assert _extract_doc_id(html, plain) == "HTML_ID"


def test_extract_doc_id_none_when_no_url() -> None:
    """Returns None when neither body has a Google Docs URL."""
    assert _extract_doc_id("No link here", "Also no link") is None


def test_extract_doc_id_none_when_both_empty() -> None:
    """Returns None when both bodies are empty strings."""
    assert _extract_doc_id("", "") is None


# ── _parse_subject (unit) ───────────────────────────────────────────────────


def test_parse_subject_extracts_title_and_date() -> None:
    """Standard Gemini subject returns correct title and parsed date."""
    title, date = _parse_subject('Notes: "Acme-Corp Weekly" May 4, 2026')
    assert title == "Acme-Corp Weekly"
    assert date == datetime(2026, 5, 4)


def test_parse_subject_handles_abbreviated_month() -> None:
    """Subject with abbreviated month (Apr) is parsed correctly."""
    title, date = _parse_subject('Notes: "Some Meeting" Apr 30, 2026')
    assert title == "Some Meeting"
    assert date == datetime(2026, 4, 30)


def test_parse_subject_strips_whitespace() -> None:
    """Leading/trailing whitespace in title is stripped."""
    title, _ = _parse_subject('Notes: " Padded Title " May 1, 2026')
    assert title == "Padded Title"


def test_parse_subject_returns_none_date_on_malformed() -> None:
    """Non-standard subject returns the raw subject as title and None date."""
    title, date = _parse_subject("Misc notification about a meeting")
    assert title == "Misc notification about a meeting"
    assert date is None


def test_parse_subject_handles_special_chars_in_title() -> None:
    """Meeting titles with dashes and slashes are preserved."""
    title, date = _parse_subject('Notes: "Acme-Corp - RHOAI / Planning" May 12, 2026')
    assert title == "Acme-Corp - RHOAI / Planning"
    assert date is not None


# ── scan_gemini_candidates ──────────────────────────────────────────────────


def test_scan_gemini_candidates_reads_only_the_ready_published_generation(tmp_path: Path) -> None:
    gmail_path = tmp_path / "gmail.db"
    _publish_gemini_email(gmail_path, message_id="published", doc_id="PUBLISHED_DOC")
    source = sqlite3.connect(gmail_path)
    try:
        source.execute(
            """
            INSERT INTO messages(
                message_id, thread_id, from_addr, to_addr, cc_addr, subject,
                date_str, date_epoch, labels, body_plain, body_html, size_bytes, snippet
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "source-only",
                "source-only",
                "gemini-notes@google.com",
                "seller@example.com",
                "",
                "Unpublished source row",
                "2026-05-02",
                int(datetime.now(UTC).timestamp()) + 1,
                "[]",
                "https://docs.google.com/document/d/UNPUBLISHED_DOC/edit",
                "",
                64,
                "Unpublished source row",
            ),
        )
        source.commit()
    finally:
        source.close()

    result = scan_gemini_candidates(gmail_path, limit=10)

    assert [candidate.source_id for candidate in result] == ["PUBLISHED_DOC"]


def test_scan_gemini_candidates_rejects_an_unready_publication(tmp_path: Path) -> None:
    gmail_path = tmp_path / "gmail.db"
    initialize_gmail_publication(gmail_path)

    with pytest.raises(GmailSyncPartialError, match="not ready"):
        scan_gemini_candidates(gmail_path, limit=10)


def test_scan_gemini_candidates_interrupts_sqlite_work_beyond_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gmail_path = tmp_path / "gmail.db"
    _publish_gemini_email(gmail_path, message_id="published", doc_id="PUBLISHED_DOC")

    def add_noise(connection: SQLiteMutationConnection) -> None:
        connection.executemany(
            """
            INSERT INTO messages(
                message_id, thread_id, from_addr, to_addr, cc_addr, subject,
                date_str, date_epoch, labels, body_plain, body_html, size_bytes, snippet
            ) VALUES (?, ?, ?, '', '', '', '', ?, '[]', '', '', 0, '')
            """,
            [(f"noise-{index}", "published", "noise@example.net", 2_000_000_000 - index) for index in range(200)],
        )

    apply_gmail_page(gmail_path, add_noise)
    monkeypatch.setattr("fieldkit.gmail.query_support.DEFAULT_SQLITE_STEPS_PER_ROW", 1)

    with pytest.raises(GmailSyncPartialError, match="work bound"):
        scan_gemini_candidates(gmail_path, limit=1)


def test_scan_gemini_candidates_returns_list_of_candidates(tmp_path: Path) -> None:
    """A populated gmail.db returns GmailCandidate objects with all 6 fields."""
    gmail_path = tmp_path / "gmail.db"
    gmail_conn = _make_gmail_db(gmail_path)
    _insert_gemini_email(
        gmail_conn,
        message_id="msg001",
        doc_id="DOCAAA",
        subject='Notes: "Test Meeting" May 1, 2026',
    )
    gmail_conn.close()

    results = scan_gemini_candidates(gmail_path, limit=None)

    assert len(results) == 1
    cand = results[0]
    assert isinstance(cand, GmailCandidate)
    assert cand.source_id == "DOCAAA"
    assert "docs.google.com" in cand.doc_url
    assert cand.subject == 'Notes: "Test Meeting" May 1, 2026'
    assert cand.meeting_title == "Test Meeting"
    assert cand.meeting_date is not None
    assert cand.email_message_id == "msg001"


def test_scan_gemini_candidates_empty_db_returns_empty_list(tmp_path: Path) -> None:
    """An empty gmail.db returns []."""
    gmail_path = tmp_path / "gmail.db"
    gmail_conn = _make_gmail_db(gmail_path)
    gmail_conn.close()

    results = scan_gemini_candidates(gmail_path, limit=None)
    assert results == []


def test_scan_gemini_candidates_rejects_an_unmanaged_legacy_schema(tmp_path: Path) -> None:
    """Raw legacy databases require explicit import into a managed publication."""
    gmail_path = tmp_path / "gmail.db"
    conn = sqlite3.connect(gmail_path)
    conn.execute(
        """
        CREATE TABLE messages (
            message_id TEXT, from_addr TEXT, subject TEXT, body_html TEXT, body_plain TEXT, date_epoch INTEGER
        )
        """
    )
    conn.execute(
        """
        INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "legacy-message",
            "Gemini <gemini-notes@google.com>",
            "Legacy meeting",
            "https://docs.google.com/document/d/LEGACY_DOC",
            "",
            int((datetime.now(UTC) - timedelta(days=1)).timestamp()),
        ),
    )
    conn.commit()
    conn.close()

    with pytest.raises(SQLiteSnapshotError, match="requires explicit import"):
        scan_gemini_candidates(gmail_path, limit=None)


def test_scan_gemini_candidates_raises_on_wrong_suffix(tmp_path: Path) -> None:
    """ConfigError raised when path does not have a .db suffix (historic regression)."""
    bad_path = tmp_path / "gmail.sqlite"
    bad_path.touch()

    with pytest.raises(ConfigError, match=r"\.db"):
        scan_gemini_candidates(bad_path, limit=None)


def test_scan_gemini_candidates_raises_on_missing_file(tmp_path: Path) -> None:
    """A missing managed Gmail publication is a retryable source failure."""
    missing = tmp_path / "nonexistent.db"

    with pytest.raises(GmailDbNotFoundError, match="has not been published"):
        scan_gemini_candidates(missing, limit=None)


def test_scan_gemini_candidates_limit_caps_results(tmp_path: Path) -> None:
    """limit parameter caps the number of returned candidates."""
    gmail_path = tmp_path / "gmail.db"
    gmail_conn = _make_gmail_db(gmail_path)
    recent_epoch = int((datetime.now(UTC) - timedelta(days=1)).timestamp())
    for i in range(5):
        _insert_gemini_email(
            gmail_conn,
            message_id=f"msg{i:03d}",
            doc_id=f"DOC{i:04d}",
            date_epoch=recent_epoch + i,
        )
    gmail_conn.close()

    results = scan_gemini_candidates(gmail_path, limit=2)
    assert len(results) == 2


def test_preview_scan_preserves_unbounded_historical_limit_policy(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Dry-run preview does not inherit live discovery's retention or default cap."""
    gmail_path = tmp_path / "gmail.db"
    gmail_conn = _make_gmail_db(gmail_path)
    _insert_gemini_email(
        gmail_conn,
        message_id="old-message",
        doc_id="OLD_DOC",
        date_epoch=int((datetime.now(UTC) - timedelta(days=91)).timestamp()),
    )
    gmail_conn.close()

    results = scan_gemini_candidates(
        gmail_path,
        limit=None,
        max_age_days=None,
        default_limit=None,
        require_positive_limit=False,
    )

    assert [candidate.source_id for candidate in results] == ["OLD_DOC"]
    assert "older than 90 days" not in caplog.text


def test_preview_scan_preserves_zero_limit_as_an_empty_preview(tmp_path: Path) -> None:
    """The legacy dry-run query accepted zero without treating it as an error."""
    gmail_path = tmp_path / "gmail.db"
    gmail_conn = _make_gmail_db(gmail_path)
    _insert_gemini_email(gmail_conn, message_id="message", doc_id="DOC")
    gmail_conn.close()

    results = scan_gemini_candidates(
        gmail_path,
        limit=0,
        max_age_days=None,
        default_limit=None,
        require_positive_limit=False,
    )

    assert results == []


def test_filter_gemini_candidates_for_account_uses_recipients(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ingest-domain account filter keeps only matching candidates."""
    gmail_path = tmp_path / "gmail.db"
    gmail_conn = _make_gmail_db(gmail_path)
    _insert_gemini_email(gmail_conn, message_id="msg-acme", doc_id="ACME_DOC", to_addr="team@acme.example.com")
    _insert_gemini_email(gmail_conn, message_id="msg-other", doc_id="OTHER_DOC", cc_addr="team@other.example.com")
    gmail_conn.close()
    monkeypatch.setattr(
        "fieldkit.ingest.router.get_accounts_config",
        lambda **kwargs: {"accounts": {"acme": {"domains": ["acme.example.com"]}}},
    )

    results = filter_gemini_candidates_for_account(scan_gemini_candidates(gmail_path, limit=None), "acme")

    assert [candidate.source_id for candidate in results] == ["ACME_DOC"]


def test_scan_gemini_candidates_includes_email_at_ninety_day_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An email at the inclusive 90-day cutoff is returned."""
    fixed_now = datetime(2026, 8, 23, tzinfo=UTC)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(fixed_now.timestamp(), tz=tz)

    monkeypatch.setattr("fieldkit.gmail.discover.datetime", FrozenDateTime)
    gmail_path = tmp_path / "gmail.db"
    gmail_conn = _make_gmail_db(gmail_path)
    _insert_gemini_email(
        gmail_conn,
        message_id="msg_cutoff",
        doc_id="DOCCUTOFF",
        date_epoch=int((fixed_now - timedelta(days=90)).timestamp()),
    )
    gmail_conn.close()

    results = scan_gemini_candidates(gmail_path, limit=None)

    assert len(results) == 1
    assert results[0].source_id == "DOCCUTOFF"


def test_scan_gemini_candidates_does_not_open_pipeline_db(tmp_path: Path) -> None:
    """scan_gemini_candidates must not create pipeline.db."""
    gmail_path = tmp_path / "gmail.db"
    gmail_conn = _make_gmail_db(gmail_path)
    _insert_gemini_email(gmail_conn, message_id="msg001", doc_id="DOCAAA")
    gmail_conn.close()

    scan_gemini_candidates(gmail_path, limit=None)

    pipeline_db = tmp_path / "pipeline.db"
    assert not pipeline_db.exists(), "scan_gemini_candidates must not create pipeline.db"


def test_scan_gemini_candidates_skips_email_without_doc_url(tmp_path: Path) -> None:
    """Emails with no Google Docs URL are excluded from results."""
    gmail_path = tmp_path / "gmail.db"
    gmail_conn = _make_gmail_db(gmail_path)
    recent_epoch = int((datetime.now(UTC) - timedelta(days=1)).timestamp())
    gmail_conn.execute("INSERT OR IGNORE INTO threads (thread_id, subject) VALUES ('t1', 'no doc')")
    gmail_conn.execute(
        """
        INSERT INTO messages
            (message_id, thread_id, from_addr, subject, body_html, body_plain, date_epoch)
        VALUES ('msg_nodoc', 't1', 'Gemini <gemini-notes@google.com>',
                'Notes: "No Doc Meeting" May 1, 2026', '', 'No link in this email.', ?)
        """,
        (recent_epoch,),
    )
    gmail_conn.commit()
    gmail_conn.close()

    results = scan_gemini_candidates(gmail_path, limit=None)
    assert results == []


# ── schema migration ────────────────────────────────────────────────────────


def test_init_db_migration_adds_pipeline_version_column(tmp_path: Path) -> None:
    """init_db() adds pipeline_version column to artifacts table."""
    db_path = tmp_path / "pipeline.db"
    conn = init_db(db_path, pipelines=PIPELINES)
    cols = [row[1] for row in conn.execute("PRAGMA table_info(artifacts)").fetchall()]
    conn.close()
    assert "pipeline_version" in cols


def test_init_db_migration_is_idempotent(tmp_path: Path) -> None:
    """Calling init_db() twice does not raise on duplicate column migration."""
    db_path = tmp_path / "pipeline.db"
    conn1 = init_db(db_path, pipelines=PIPELINES)
    conn1.close()
    # Second call — must not raise even though column already exists
    conn2 = init_db(db_path, pipelines=PIPELINES)
    assert conn2 is not None
    conn2.close()


def test_init_db_migration_pipeline_version_default(tmp_path: Path) -> None:
    """pipeline_version column defaults to '0.1.0'."""
    db_path = tmp_path / "pipeline.db"
    conn = init_db(db_path, pipelines=PIPELINES)

    # Insert a source so we can insert an artifact
    conn.execute("INSERT INTO sources (source_id, pipeline_id, file_path) VALUES ('s1', 'transcript-ingest', '')")
    conn.execute(
        "INSERT INTO artifacts (artifact_id, source_id, pipeline_id, artifact_type) "
        "VALUES ('a1', 's1', 'transcript-ingest', 'meeting-note')"
    )
    conn.commit()

    row = conn.execute("SELECT pipeline_version FROM artifacts WHERE artifact_id='a1'").fetchone()
    conn.close()

    assert row is not None
    assert row[0] == "0.1.0"
