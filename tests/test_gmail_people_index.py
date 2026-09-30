"""Unit tests for build_people_index in fieldkit.contact.people_index."""

import json
import sqlite3
from pathlib import Path

import pytest

from fieldkit.contact._enrich_helpers import load_gmail_cache_contacts
from fieldkit.contact.people_index import build_people_index
from fieldkit.contact.people_query import list_people
from fieldkit.contact.resolver import resolve
from fieldkit.errors import GmailSyncPartialError, SQLiteSnapshotError
from fieldkit.gmail.exceptions import GmailSchemaError
from fieldkit.gmail.publication import (
    GMAIL_QUERY_READY_KEY,
    apply_gmail_page,
    initialize_gmail_publication,
    open_gmail_publication,
    publication_root_for,
)
from fieldkit.sqlite_publication import SQLiteMutationConnection

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(path: str | Path) -> Path:
    """Create an empty ready published Gmail cache at *path*."""
    db_path = Path(path)
    initialize_gmail_publication(db_path)

    def mark_ready(connection: SQLiteMutationConnection) -> None:
        connection.execute(
            "INSERT OR REPLACE INTO sync_state(key, value) VALUES (?, 'true')",
            (GMAIL_QUERY_READY_KEY,),
        )

    apply_gmail_page(db_path, mark_ready)
    return db_path


def _insert_message(
    db_path: Path,
    *,
    message_id: str,
    thread_id: str,
    from_addr: str,
    to_addr: str = "",
    cc_addr: str = "",
    date_epoch: int = 1_700_000_000,
    account: str | None = None,
) -> None:
    def insert(connection: SQLiteMutationConnection) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO threads(thread_id, subject, message_count, updated_at) VALUES (?, '', 1, '')",
            (thread_id,),
        )
        connection.execute(
            """
            INSERT INTO messages(message_id, thread_id, from_addr, to_addr, cc_addr, date_epoch)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (message_id, thread_id, from_addr, to_addr, cc_addr, date_epoch),
        )
        if account:
            connection.execute(
                "INSERT INTO thread_accounts(thread_id, account) VALUES (?, ?)",
                (thread_id, account),
            )

    apply_gmail_page(db_path, insert)


def _replace_messages(
    db_path: Path,
    rows: tuple[tuple[str, str, str, str], ...],
) -> None:
    """Replace source messages with (id, thread, sender, account) rows."""

    def replace(connection: SQLiteMutationConnection) -> None:
        connection.execute("DELETE FROM messages")
        connection.execute("DELETE FROM thread_accounts")
        connection.execute("DELETE FROM threads")
        for message_id, thread_id, sender, account in rows:
            connection.execute(
                "INSERT INTO threads(thread_id, subject, message_count, updated_at) VALUES (?, '', 1, '')",
                (thread_id,),
            )
            connection.execute(
                """
                INSERT INTO messages(message_id, thread_id, from_addr, date_epoch)
                VALUES (?, ?, ?, 1700000000)
                """,
                (message_id, thread_id, sender),
            )
            connection.execute(
                "INSERT INTO thread_accounts(thread_id, account) VALUES (?, ?)",
                (thread_id, account),
            )

    apply_gmail_page(db_path, replace)


def _people(db_path: Path) -> tuple[tuple[str, str | None], ...]:
    with open_gmail_publication(db_path) as connection:
        return tuple(
            (str(row[0]), str(row[1]) if row[1] is not None else None)
            for row in connection.execute("SELECT email, account FROM people ORDER BY email")
        )


def _generation(db_path: Path) -> int:
    with open_gmail_publication(db_path) as connection:
        return int(connection.execute("SELECT generation FROM _fieldkit_publication").fetchone()[0])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_empty_messages_table_no_crash(tmp_path: Path) -> None:
    """Empty messages table → completes without crash and inserts 0 contacts."""
    db_file = _make_db(tmp_path / "gmail.db")

    build_people_index(db_file, show_progress=False)

    with open_gmail_publication(db_file) as verify:
        count = verify.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    assert count == 0


@pytest.mark.unit
def test_show_progress_false_suppresses_stderr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """show_progress=False writes nothing to stderr."""
    db_file = _make_db(tmp_path / "gmail.db")
    for i in range(3):
        _insert_message(
            db_file,
            message_id=f"msg{i}",
            thread_id=f"t{i}",
            from_addr=f"person{i}@company.com",  # pii-guard: ignore
        )
    build_people_index(db_file, show_progress=False)

    captured = capsys.readouterr()
    assert captured.err == ""


@pytest.mark.unit
def test_account_filter_still_reconciles_complete_shared_index(tmp_path: Path) -> None:
    """An account-triggered rebuild publishes one complete coherent people index."""
    db_file = _make_db(tmp_path / "gmail.db")

    # Two messages in different accounts
    _insert_message(
        db_file,
        message_id="msg1",
        thread_id="t1",
        from_addr="alice@acme-corp.example.com",
        account="acme",
    )
    _insert_message(
        db_file,
        message_id="msg2",
        thread_id="t2",
        from_addr="bob@globalpay.example.com",
        account="globalpay",
    )
    build_people_index(db_file, account_filter="acme", show_progress=False)

    with open_gmail_publication(db_file) as verify:
        rows = verify.execute("SELECT email FROM people").fetchall()
    emails = {r[0] for r in rows}
    assert "alice@acme-corp.example.com" in emails
    assert "bob@globalpay.example.com" in emails


@pytest.mark.unit
def test_unpublished_database_is_refused_without_mutation(tmp_path: Path) -> None:
    """A raw SQLite file cannot bypass the publication transaction."""
    db_file = tmp_path / "empty.db"
    sqlite3.connect(db_file).close()
    before = db_file.read_bytes()

    with pytest.raises(SQLiteSnapshotError, match="requires explicit import"):
        build_people_index(db_file, show_progress=False)

    assert db_file.read_bytes() == before


@pytest.mark.unit
def test_full_rebuild_reconciles_stale_and_reassigned_people(tmp_path: Path) -> None:
    db_file = _make_db(tmp_path / "gmail.db")
    _replace_messages(
        db_file,
        (
            ("m1", "t1", "Alice <alice@acme-corp.example.com>", "acme"),
            ("m2", "t2", "Bob <bob@globalpay.example.com>", "globalpay"),
        ),
    )
    build_people_index(db_file, show_progress=False)
    assert _people(db_file) == (
        ("alice@acme-corp.example.com", "acme"),
        ("bob@globalpay.example.com", "globalpay"),
    )

    _replace_messages(
        db_file,
        (("m3", "t3", "Alice New <alice.new@globalpay.example.com>", "globalpay"),),
    )
    build_people_index(db_file, show_progress=False)

    assert _people(db_file) == (("alice.new@globalpay.example.com", "globalpay"),)


@pytest.mark.unit
def test_scoped_rebuild_replaces_only_selected_account_rows(tmp_path: Path) -> None:
    db_file = _make_db(tmp_path / "gmail.db")
    _replace_messages(
        db_file,
        (
            ("m1", "t1", "Alice <alice@acme-corp.example.com>", "acme"),
            ("m2", "t2", "Bob <bob@globalpay.example.com>", "globalpay"),
        ),
    )
    build_people_index(db_file, show_progress=False)

    _replace_messages(
        db_file,
        (
            ("m3", "t3", "Avery <avery@acme-corp.example.com>", "acme"),
            ("m2", "t2", "Bob <bob@globalpay.example.com>", "globalpay"),
        ),
    )
    build_people_index(db_file, account_filter="acme", show_progress=False)

    assert _people(db_file) == (
        ("avery@acme-corp.example.com", "acme"),
        ("bob@globalpay.example.com", "globalpay"),
    )


@pytest.mark.unit
def test_empty_scoped_rebuild_removes_only_selected_account_rows(tmp_path: Path) -> None:
    db_file = _make_db(tmp_path / "gmail.db")
    _replace_messages(
        db_file,
        (
            ("m1", "t1", "Alice <alice@acme-corp.example.com>", "acme"),
            ("m2", "t2", "Bob <bob@globalpay.example.com>", "globalpay"),
        ),
    )
    build_people_index(db_file, show_progress=False)
    _replace_messages(
        db_file,
        (("m2", "t2", "Bob <bob@globalpay.example.com>", "globalpay"),),
    )

    build_people_index(db_file, account_filter="acme", show_progress=False)

    assert _people(db_file) == (("bob@globalpay.example.com", "globalpay"),)


@pytest.mark.unit
def test_scoped_rebuild_preserves_shared_contact_from_other_account(tmp_path: Path) -> None:
    db_file = _make_db(tmp_path / "gmail.db")
    shared = "Shared Person <shared@example.com>"
    _replace_messages(
        db_file,
        (
            ("m1", "t1", shared, "acme"),
            ("m2", "t2", shared, "acme"),
            ("m3", "t3", shared, "globalpay"),
            ("m4", "t4", "Other <other@globalpay.example.com>", "globalpay"),
        ),
    )
    build_people_index(db_file, show_progress=False)

    _replace_messages(
        db_file,
        (
            ("m3", "t3", shared, "globalpay"),
            ("m4", "t4", "Other <other@globalpay.example.com>", "globalpay"),
        ),
    )
    build_people_index(db_file, account_filter="acme", show_progress=False)

    with open_gmail_publication(db_file) as connection:
        row = connection.execute(
            "SELECT account, message_count, thread_count FROM people WHERE email = ?",
            ("shared@example.com",),
        ).fetchone()
    assert tuple(row) == ("globalpay", 1, 1)
    assert _people(db_file) == (
        ("other@globalpay.example.com", "globalpay"),
        ("shared@example.com", "globalpay"),
    )


@pytest.mark.unit
def test_people_index_uses_canonical_rfc_address_identities(tmp_path: Path) -> None:
    db_file = _make_db(tmp_path / "gmail.db")
    _replace_messages(
        db_file,
        (
            ("m1", "t1", "alice@example.com (Alice)", "acme"),
            ("m2", "t2", "Group: bob@example.com, carol@example.com;", "acme"),
            ("m3", "t3", '"Doe, Jane" <Jane@Example.COM>', "acme"),
        ),
    )

    build_people_index(db_file, show_progress=False)

    with open_gmail_publication(db_file) as connection:
        rows = tuple(tuple(row) for row in connection.execute("SELECT email, display_name FROM people ORDER BY email"))
    assert rows == (
        ("alice@example.com", "Alice"),
        ("bob@example.com", ""),
        ("carol@example.com", ""),
        ("jane@example.com", "Doe, Jane"),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "malformed",
    ["fictional-private <not-an-address>", "a@example.com fictional-private", "Alice <alice@example.com"],
)
def test_people_index_rejects_malformed_addresses_without_publishing(tmp_path: Path, malformed: str) -> None:
    db_file = _make_db(tmp_path / "gmail.db")
    _replace_messages(db_file, (("m1", "t1", "Alice <alice@example.com>", "acme"),))
    build_people_index(db_file, show_progress=False)
    before_generation = _generation(db_file)
    before_people = _people(db_file)
    _replace_messages(db_file, (("m2", "t2", malformed, "acme"),))
    source_generation = _generation(db_file)

    with pytest.raises(GmailSchemaError, match="invalid message address data") as captured:
        build_people_index(db_file, show_progress=False)

    assert "fictional-private" not in str(captured.value)
    assert source_generation == before_generation + 1
    root = publication_root_for(db_file)
    state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    receipt = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
    assert state["status"] == "updating"
    assert receipt["generation"] == source_generation
    with sqlite3.connect(db_file) as source:
        current_people = tuple(source.execute("SELECT email, account FROM people ORDER BY email"))
    assert current_people == before_people


@pytest.mark.unit
def test_unready_rebuild_is_nonpassing_and_preserves_published_index(tmp_path: Path) -> None:
    db_file = _make_db(tmp_path / "gmail.db")
    _insert_message(db_file, message_id="m1", thread_id="t1", from_addr="alice@acme-corp.example.com", account="acme")
    build_people_index(db_file, show_progress=False)
    before_generation = _generation(db_file)
    before_people = _people(db_file)

    def mark_unready(connection: SQLiteMutationConnection) -> None:
        connection.execute(
            "UPDATE sync_state SET value = 'false' WHERE key = ?",
            (GMAIL_QUERY_READY_KEY,),
        )

    apply_gmail_page(db_file, mark_unready)
    unready_generation = _generation(db_file)
    assert unready_generation == before_generation + 1

    with pytest.raises(GmailSyncPartialError, match="not ready"):
        build_people_index(db_file, show_progress=False)

    assert _generation(db_file) == unready_generation
    assert _people(db_file) == before_people


@pytest.mark.unit
def test_failed_rebuild_rolls_back_without_publishing_partial_people(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_file = _make_db(tmp_path / "gmail.db")
    _insert_message(db_file, message_id="m1", thread_id="t1", from_addr="alice@acme-corp.example.com", account="acme")
    build_people_index(db_file, show_progress=False)
    before_generation = _generation(db_file)
    before_people = _people(db_file)

    def fail_after_base_write(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("fictional-private-index-failure")

    monkeypatch.setattr("fieldkit.contact.people_index._update_thread_and_account_fields", fail_after_base_write)

    with pytest.raises(RuntimeError, match="fictional-private-index-failure"):
        build_people_index(db_file, show_progress=False)

    root = publication_root_for(db_file)
    state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    receipt = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
    assert state["status"] == "updating"
    assert receipt["generation"] == before_generation
    with sqlite3.connect(db_file) as source:
        current_people = tuple(source.execute("SELECT email, account FROM people ORDER BY email"))
    assert current_people == before_people


@pytest.mark.unit
def test_rebuilt_generation_is_immediately_visible_to_all_contact_readers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_file = _make_db(tmp_path / "gmail.db")
    _insert_message(
        db_file,
        message_id="m1",
        thread_id="t1",
        from_addr="Alice <alice@acme-corp.example.com>",
        account="acme",
    )
    build_people_index(db_file, show_progress=False)
    generation = _generation(db_file)
    source_before = db_file.read_bytes()
    monkeypatch.setattr("fieldkit.contact._enrich_helpers.get_gmail_db_path", lambda: db_file)

    listed = list_people(db_file)
    resolved = resolve("alice@acme-corp.example.com", db_file)
    enriched = load_gmail_cache_contacts("acme")

    assert listed[0]["email"] == "alice@acme-corp.example.com"
    assert resolved["type"] == "resolved"
    assert resolved["email"] == "alice@acme-corp.example.com"
    assert enriched[0]["email"] == "alice@acme-corp.example.com"
    assert _generation(db_file) == generation
    assert db_file.read_bytes() == source_before
