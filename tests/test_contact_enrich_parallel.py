"""Concurrency contract for bounded contact enrichment batches."""

import sqlite3
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.contact._enrich_helpers import BATCH_SIZE, enrich_batch
from fieldkit.enrich.schema import ContactRecord

pytestmark = pytest.mark.unit


def _contact(index: int) -> dict[str, object]:
    return {
        "full_name": f"Contact {index}",
        "company": "Example Corp",
        "account": "example",
        "email": f"contact{index}@example.com",
        "source": "gmail",
    }


def _record(contact: dict[str, object]) -> ContactRecord:
    return ContactRecord(
        full_name=str(contact["full_name"]),
        company=str(contact["company"]),
        account=str(contact["account"]),
        email=str(contact["email"]),
        linkedin_url=None,
        phone=None,
        title=None,
        source="gmail",
        last_contact_date=None,
        email_frequency=None,
        sf_role=None,
        confidence="HIGH",
    )


@pytest.fixture(autouse=True)
def _batch_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fieldkit.contact._enrich_helpers.get_user_email", lambda: None)
    monkeypatch.setattr("fieldkit.contact._enrich_helpers.build_domain_account_map", dict)
    monkeypatch.setattr("fieldkit.contact._enrich_helpers.get_internal_domains", lambda: ["example.com"])


def test_enrich_batch_overlaps_workers_but_serializes_writes_in_input_order() -> None:
    contacts = [_contact(index) for index in range(3)]
    barrier = threading.Barrier(len(contacts))
    contact_two_done = threading.Event()
    contact_one_done = threading.Event()
    worker_threads: set[int] = set()
    completed: list[str] = []
    write_threads: list[int] = []
    written: list[str] = []
    coordinator_thread = threading.get_ident()

    def enrich(contact: dict[str, object], retry_count: int = 0) -> ContactRecord:
        del retry_count
        worker_threads.add(threading.get_ident())
        barrier.wait(timeout=2)
        index = int(str(contact["full_name"]).split()[-1])
        if index == 1:
            assert contact_two_done.wait(timeout=2)
        elif index == 0:
            assert contact_one_done.wait(timeout=2)
        completed.append(str(contact["email"]))
        if index == 2:
            contact_two_done.set()
        elif index == 1:
            contact_one_done.set()
        return _record(contact)

    def write(record: ContactRecord) -> None:
        write_threads.append(threading.get_ident())
        written.append(record.email or "")

    with (
        patch("fieldkit.contact._enrich_helpers.enrich_contact", side_effect=enrich),
        patch("fieldkit.contact._enrich_helpers.write_to_memory", side_effect=write),
    ):
        enriched, failed = enrich_batch(contacts, 0)

    input_emails = [str(contact["email"]) for contact in contacts]
    assert len(worker_threads) > 1
    assert completed != input_emails
    assert [record["email"] for record in enriched] == input_emails
    assert written == input_emails
    assert write_threads == [coordinator_thread] * len(contacts)
    assert failed == []


def test_enrich_batch_caps_executor_at_batch_size() -> None:
    contacts = [_contact(index) for index in range(BATCH_SIZE)]
    executor = MagicMock()
    executor.return_value.__enter__.return_value.map.return_value = [_record(contact) for contact in contacts]

    with (
        patch("fieldkit.contact._enrich_helpers.ThreadPoolExecutor", executor),
        patch("fieldkit.contact._enrich_helpers.write_to_memory"),
    ):
        enriched, failed = enrich_batch(contacts, 0)

    executor.assert_called_once_with(max_workers=BATCH_SIZE)
    assert len(enriched) == BATCH_SIZE
    assert failed == []


def test_enrich_batch_avoids_executor_when_every_contact_is_excluded() -> None:
    contacts = [_contact(index) for index in range(2)]
    executor = MagicMock()

    with (
        patch("fieldkit.contact._enrich_helpers._should_skip_contact", return_value=True),
        patch("fieldkit.contact._enrich_helpers.ThreadPoolExecutor", executor),
    ):
        enriched, failed = enrich_batch(contacts, 0)

    executor.assert_not_called()
    assert enriched == []
    assert failed == []


def test_enrich_batch_worker_failure_prevents_memory_writes() -> None:
    contacts = [_contact(index) for index in range(2)]

    def enrich(contact: dict[str, object], retry_count: int = 0) -> ContactRecord:
        del retry_count
        if contact["full_name"] == "Contact 1":
            raise RuntimeError("worker failed")
        return _record(contact)

    with (
        patch("fieldkit.contact._enrich_helpers.enrich_contact", side_effect=enrich),
        patch("fieldkit.contact._enrich_helpers.write_to_memory") as write,
        pytest.raises(RuntimeError, match="worker failed"),
    ):
        enrich_batch(contacts, 0)

    write.assert_not_called()


def test_enrich_batch_prepares_gmail_once_before_read_only_workers(tmp_path: Path) -> None:
    db_path = tmp_path / "gmail.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE messages (
            message_id TEXT PRIMARY KEY, thread_id TEXT, date_epoch INTEGER,
            date_str TEXT, from_addr TEXT, to_addr TEXT, cc_addr TEXT,
            subject TEXT, snippet TEXT, body_plain TEXT
        );
        CREATE TABLE people (email TEXT PRIMARY KEY, display_name TEXT);
        INSERT INTO messages VALUES
            ('m1', 't1', 1710460800, '2024-03-15', 'contact0@example.com',
             'contact1@example.com', NULL, 'Hello', '', '');
        """
    )
    conn.commit()
    conn.close()
    coordinator_thread = threading.get_ident()
    migration_threads: list[int] = []

    def record_migration(_conn: sqlite3.Connection) -> None:
        migration_threads.append(threading.get_ident())

    with (
        patch("fieldkit.contact._enrich_helpers.get_gmail_db_path", return_value=db_path),
        patch("fieldkit.gmail.query_domain._ensure_indexes", side_effect=record_migration),
        patch("fieldkit.gmail.query_domain._ensure_schema", side_effect=record_migration),
        patch("fieldkit.contact._enrich_helpers.write_to_memory"),
    ):
        enriched, failed = enrich_batch([_contact(0), _contact(1)], 0)

    assert migration_threads == [coordinator_thread, coordinator_thread]
    assert [record["email_frequency"] for record in enriched] == [1, 1]
    assert failed == []
