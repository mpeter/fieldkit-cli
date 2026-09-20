"""Unit tests for fieldkit.ingest.sources — pipeline source lifecycle.

Covers:
- discover_gemini_sources: registration, idempotency, rollback
- claim_pending_source: atomic claim semantics, double-claim prevention
- get_pending_sources: filtering, ordering, limit
- SourceRecord field completeness
- Concurrent claimer correctness
"""

import sqlite3
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from fieldkit.ingest.constants import SOURCE_STATUS_PROCESSED
from fieldkit.ingest.sources import (
    SourceRecord,
    claim_pending_source,
    discover_gemini_sources,
    get_pending_sources,
    insert_vault_note_artifact,
    mark_source_status,
)

pytestmark = pytest.mark.unit

if TYPE_CHECKING:
    from fieldkit.gmail.discover import GmailCandidate


# ── helpers ────────────────────────────────────────────────────────────────


def _make_pipeline_db(tmp_path: Path) -> sqlite3.Connection:
    """Create a pipeline.db with the full schema and pipeline registry seeded."""
    from fieldkit.commands.ingest.registry import PIPELINES
    from fieldkit.ingest.db import init_db

    db_path = tmp_path / "pipeline.db"
    return init_db(db_path, pipelines=PIPELINES)


def _insert_pending(conn: sqlite3.Connection, source_id: str) -> None:
    """Insert a pending source row for test setup."""
    conn.execute(
        "INSERT OR IGNORE INTO sources (source_id, pipeline_id, file_path, status) "
        "VALUES (?, 'transcript-ingest', '/fake/path', 'pending')",
        (source_id,),
    )
    conn.commit()


def _make_gmail_candidate(
    source_id: str = "DOCAAA",
    doc_url: str = "https://docs.google.com/document/d/DOCAAA",
    subject: str = 'Notes: "Test Meeting" May 1, 2026',
    meeting_title: str = "Test Meeting",
    meeting_date: datetime | None = None,
    email_message_id: str = "msg001",
) -> "GmailCandidate":
    """Return a minimal object satisfying _CandidateProtocol."""
    from fieldkit.gmail.discover import GmailCandidate

    return GmailCandidate(
        source_id=source_id,
        doc_url=doc_url,
        subject=subject,
        meeting_title=meeting_title,
        meeting_date=meeting_date,
        email_message_id=email_message_id,
    )


# ── discover_gemini_sources ─────────────────────────────────────────────────


def test_discover_gemini_sources_registers_new_source(tmp_path: Path) -> None:
    """A new candidate is registered and returned as a SourceRecord."""
    conn = _make_pipeline_db(tmp_path)
    cand = _make_gmail_candidate(source_id="DOCAAA")

    results = discover_gemini_sources(conn, [cand])
    conn.close()

    assert len(results) == 1
    assert results[0].source_id == "DOCAAA"
    assert results[0].pipeline_id == "transcript-ingest"


def test_discover_gemini_sources_multiple_candidates(tmp_path: Path) -> None:
    """Multiple candidates produce multiple SourceRecords."""
    conn = _make_pipeline_db(tmp_path)
    candidates = [_make_gmail_candidate(source_id=f"DOC{i:03d}", email_message_id=f"msg{i}") for i in range(4)]

    results = discover_gemini_sources(conn, candidates)
    conn.close()

    assert len(results) == 4
    ids = {r.source_id for r in results}
    assert ids == {"DOC000", "DOC001", "DOC002", "DOC003"}


def test_discover_gemini_sources_is_idempotent(tmp_path: Path) -> None:
    """Running discover twice registers no duplicates."""
    conn = _make_pipeline_db(tmp_path)
    cand = _make_gmail_candidate(source_id="IDEM_DOC")

    first_run = discover_gemini_sources(conn, [cand])
    second_run = discover_gemini_sources(conn, [cand])

    total = conn.execute("SELECT COUNT(*) FROM sources WHERE pipeline_id='transcript-ingest'").fetchone()[0]
    conn.close()

    assert len(first_run) == 1
    assert len(second_run) == 0  # already registered, nothing new
    assert total == 1


def test_discover_gemini_sources_empty_candidates_returns_empty(tmp_path: Path) -> None:
    """Empty candidates list yields an empty discovered list."""
    conn = _make_pipeline_db(tmp_path)

    result = discover_gemini_sources(conn, [])
    conn.close()

    assert result == []


def test_discover_gemini_sources_rollback_on_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An exception during registration triggers rollback — 0 rows committed."""
    import fieldkit.ingest.sources as sources_mod

    conn = _make_pipeline_db(tmp_path)
    candidates = [_make_gmail_candidate(source_id=f"DOC{i}", email_message_id=f"msg{i}") for i in range(3)]

    call_count = 0
    original_fn = sources_mod._try_register_source

    def _failing_register(
        pipeline_db_conn: sqlite3.Connection, candidate: "GmailCandidate", now_iso: str
    ) -> SourceRecord | None:
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            raise RuntimeError("simulated mid-loop failure")
        return original_fn(pipeline_db_conn, candidate, now_iso)

    monkeypatch.setattr(sources_mod, "_try_register_source", _failing_register)

    with pytest.raises(RuntimeError, match="simulated mid-loop failure"):
        discover_gemini_sources(conn, candidates)

    row_count = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    conn.close()

    # Single commit-at-end means rollback leaves 0 rows.
    assert row_count == 0


def test_discover_gemini_sources_source_record_field_completeness(tmp_path: Path) -> None:
    """SourceRecord constructed from a GmailCandidate has all fields populated correctly."""
    from fieldkit.ingest.constants import GEMINI_TRANSCRIPT_PIPELINE

    conn = _make_pipeline_db(tmp_path)
    meeting_date = datetime(2026, 5, 1)
    cand = _make_gmail_candidate(
        source_id="DOCFULL",
        doc_url="https://docs.google.com/document/d/DOCFULL",
        subject='Notes: "Full Meeting" May 1, 2026',
        meeting_title="Full Meeting",
        meeting_date=meeting_date,
        email_message_id="msg_full",
    )

    results = discover_gemini_sources(conn, [cand])
    conn.close()

    assert len(results) == 1
    rec = results[0]
    assert rec.source_id == "DOCFULL"
    assert rec.pipeline_id == GEMINI_TRANSCRIPT_PIPELINE
    assert rec.doc_url == "https://docs.google.com/document/d/DOCFULL"
    assert rec.email_message_id == "msg_full"
    assert rec.discovered_at != ""
    # ISO datetime format (basic check)
    assert "T" in rec.discovered_at


# ── claim_pending_source ────────────────────────────────────────────────────


def test_claim_pending_source_returns_true_on_success(tmp_path: Path) -> None:
    """claim_pending_source() returns True when it successfully claims a pending source."""
    conn = _make_pipeline_db(tmp_path)
    _insert_pending(conn, "CLAIM_DOC")

    result = claim_pending_source(conn, "CLAIM_DOC")
    conn.close()

    assert result is True


def test_claim_pending_source_updates_status_to_in_progress(tmp_path: Path) -> None:
    """claim_pending_source() sets status to 'in_progress' in the DB."""
    conn = _make_pipeline_db(tmp_path)
    _insert_pending(conn, "STATUS_DOC")
    claim_pending_source(conn, "STATUS_DOC")

    row = conn.execute("SELECT status FROM sources WHERE source_id='STATUS_DOC'").fetchone()
    conn.close()

    assert row is not None
    assert row[0] == "in_progress"


def test_claim_pending_source_returns_false_when_already_claimed(tmp_path: Path) -> None:
    """claim_pending_source() returns False if the source is no longer pending."""
    conn = _make_pipeline_db(tmp_path)
    _insert_pending(conn, "DOUBLE_CLAIM")

    first = claim_pending_source(conn, "DOUBLE_CLAIM")
    second = claim_pending_source(conn, "DOUBLE_CLAIM")
    conn.close()

    assert first is True
    assert second is False


def test_claim_pending_source_returns_false_for_nonexistent(tmp_path: Path) -> None:
    """claim_pending_source() returns False for a source_id that doesn't exist."""
    conn = _make_pipeline_db(tmp_path)

    result = claim_pending_source(conn, "NONEXISTENT_ID")
    conn.close()

    assert result is False


def test_claim_pending_source_double_claim_returns_false(tmp_path: Path) -> None:
    """A second claim attempt on the same source must return False."""
    conn = _make_pipeline_db(tmp_path)
    _insert_pending(conn, "doc-003")
    assert claim_pending_source(conn, "doc-003") is True
    assert claim_pending_source(conn, "doc-003") is False
    conn.close()


def test_claim_pending_source_different_sources_claimed_independently(tmp_path: Path) -> None:
    """Two distinct pending sources can both be claimed successfully."""
    conn = _make_pipeline_db(tmp_path)
    _insert_pending(conn, "doc-a")
    _insert_pending(conn, "doc-b")
    assert claim_pending_source(conn, "doc-a") is True
    assert claim_pending_source(conn, "doc-b") is True
    conn.close()


def test_claim_pending_source_does_not_affect_other_rows(tmp_path: Path) -> None:
    """Claiming one source must not change the status of any other source."""
    conn = _make_pipeline_db(tmp_path)
    _insert_pending(conn, "doc-x")
    _insert_pending(conn, "doc-y")
    claim_pending_source(conn, "doc-x")
    row = conn.execute("SELECT status FROM sources WHERE source_id = ?", ("doc-y",)).fetchone()
    conn.close()
    assert row["status"] == "pending"


# ── processing writebacks ───────────────────────────────────────────────────


def test_mark_source_status_records_status_and_processed_timestamp(tmp_path: Path) -> None:
    """A processing transition is committed with a UTC timestamp."""
    conn = _make_pipeline_db(tmp_path)
    _insert_pending(conn, "STATUS_WRITEBACK")

    mark_source_status(conn, "STATUS_WRITEBACK", SOURCE_STATUS_PROCESSED)

    row = conn.execute("SELECT status, processed_at FROM sources WHERE source_id = ?", ("STATUS_WRITEBACK",)).fetchone()
    conn.close()

    assert row is not None
    assert row["status"] == "processed"
    assert row["processed_at"].endswith("Z")


def test_insert_vault_note_artifact_records_pipeline_version(tmp_path: Path) -> None:
    """A processed source produces one versioned vault-note artifact row."""
    conn = _make_pipeline_db(tmp_path)
    _insert_pending(conn, "ARTIFACT_WRITEBACK")

    insert_vault_note_artifact(conn, "ARTIFACT_WRITEBACK", "1.0.0", "/vault/note.md")

    row = conn.execute(
        "SELECT source_id, pipeline_id, artifact_type, content_path, pipeline_version FROM artifacts"
    ).fetchone()
    conn.close()

    assert row is not None
    assert tuple(row) == ("ARTIFACT_WRITEBACK", "transcript-ingest", "vault_note", "/vault/note.md", "1.0.0")


# ── get_pending_sources ─────────────────────────────────────────────────────


def test_get_pending_sources_returns_pending_records(tmp_path: Path) -> None:
    """get_pending_sources() returns all pending sources for a pipeline."""
    conn = _make_pipeline_db(tmp_path)
    for i in range(3):
        _insert_pending(conn, f"PEND{i}")

    pending = get_pending_sources(conn, "transcript-ingest")
    conn.close()

    assert len(pending) == 3
    assert all(isinstance(r, SourceRecord) for r in pending)


def test_get_pending_sources_excludes_processed(tmp_path: Path) -> None:
    """get_pending_sources() excludes sources that have been marked processed."""
    conn = _make_pipeline_db(tmp_path)
    for i in range(3):
        _insert_pending(conn, f"PROC{i}")

    # Mark one source as processed
    conn.execute("UPDATE sources SET status='processed' WHERE source_id='PROC1'")
    conn.commit()

    pending = get_pending_sources(conn, "transcript-ingest")
    conn.close()

    assert len(pending) == 2
    source_ids = {r.source_id for r in pending}
    assert "PROC1" not in source_ids


def test_get_pending_sources_limit(tmp_path: Path) -> None:
    """get_pending_sources() with limit=1 returns at most 1 record."""
    conn = _make_pipeline_db(tmp_path)
    for i in range(5):
        _insert_pending(conn, f"LIM{i}")

    pending = get_pending_sources(conn, "transcript-ingest", limit=1)
    conn.close()

    assert len(pending) == 1


def test_scan_missing_path_raises_config_error(tmp_path: Path) -> None:
    """ConfigError is raised from scan_gemini_candidates when gmail.db is absent."""
    from fieldkit.config import ConfigError
    from fieldkit.gmail.discover import scan_gemini_candidates

    missing_path = tmp_path / "nonexistent.db"

    with pytest.raises(ConfigError, match=r"gmail\.db not found"):
        scan_gemini_candidates(missing_path, limit=None)


def test_scan_missing_path_error_contains_path(tmp_path: Path) -> None:
    """ConfigError message must include the missing path for diagnostics."""
    from fieldkit.config import ConfigError
    from fieldkit.gmail.discover import scan_gemini_candidates

    missing_path = tmp_path / "nonexistent.db"

    with pytest.raises(ConfigError, match=str(missing_path)):
        scan_gemini_candidates(missing_path, limit=None)


# ── Concurrent claimer test (task 4.2) ──────────────────────────────────────


def test_concurrent_claimer_exactly_one_wins() -> None:
    """Two threads claiming the same pending source: exactly one returns True."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from fieldkit.commands.ingest.registry import PIPELINES
        from fieldkit.ingest.db import init_db

        db_path = Path(tmpdir) / "pipeline.db"
        setup_conn = init_db(db_path, pipelines=PIPELINES)
        setup_conn.execute("PRAGMA journal_mode=WAL")
        setup_conn.execute(
            "INSERT INTO sources (source_id, pipeline_id, file_path, status) "
            "VALUES ('concurrent-doc', 'transcript-ingest', '', 'pending')"
        )
        setup_conn.commit()
        setup_conn.close()

        results: list[bool] = []
        barrier = threading.Barrier(2)

        def _claimer() -> None:
            conn = sqlite3.connect(str(db_path))
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            barrier.wait()  # Force simultaneous entry
            result = claim_pending_source(conn, "concurrent-doc")
            results.append(result)
            conn.close()

        t1 = threading.Thread(target=_claimer)
        t2 = threading.Thread(target=_claimer)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

    assert len(results) == 2
    assert results.count(True) == 1, f"Expected exactly one True, got: {results}"
    assert results.count(False) == 1, f"Expected exactly one False, got: {results}"
