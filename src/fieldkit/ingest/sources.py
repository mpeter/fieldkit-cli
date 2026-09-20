"""Ingest pipeline source lifecycle — registration, claiming, and querying of
pipeline sources in pipeline.db. MUST NOT import from `fieldkit.gmail`.
"""

import logging
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from fieldkit.ingest.constants import GEMINI_TRANSCRIPT_PIPELINE, SourceStatus

logger = logging.getLogger(__name__)


# ── protocols ──────────────────────────────────────────────────────────────


class _CandidateProtocol(Protocol):
    """Structural interface for ingest candidates produced by gmail scanning.

    Note: fields are declared as @property (read-only) so that frozen dataclasses
    (like GmailCandidate) satisfy this Protocol under mypy --strict.  Plain instance
    variable annotations would require settable attributes, which frozen dataclasses
    do not provide.
    """

    @property
    def source_id(self) -> str: ...

    @property
    def doc_url(self) -> str: ...

    @property
    def subject(self) -> str: ...

    @property
    def meeting_title(self) -> str: ...

    @property
    def meeting_date(self) -> datetime | None: ...

    @property
    def email_message_id(self) -> str: ...


class _RoutableCandidateProtocol(_CandidateProtocol, Protocol):
    """Candidate metadata required for account-scoped discovery."""

    @property
    def recipient_addresses(self) -> tuple[str, ...]: ...


# ── data model ─────────────────────────────────────────────────────────────


@dataclass
class SourceRecord:
    """Represents a registered pipeline source in pipeline.db.

    Describes a document or artifact registered for ingest processing.
    """

    source_id: str  # Google Doc ID (primary key in sources table)
    pipeline_id: str  # Always 'transcript-ingest' for this flow
    subject: str  # Raw email subject line
    meeting_title: str  # Parsed title from subject
    meeting_date: datetime | None  # Parsed date from subject (None if unparseable)
    doc_url: str  # Full Google Doc URL
    email_message_id: str  # gmail.db message_id
    discovered_at: str  # ISO datetime string


# ── internal helpers ───────────────────────────────────────────────────────


def _try_register_source(
    pipeline_db_conn: sqlite3.Connection,
    candidate: _CandidateProtocol,
    now_iso: str,
) -> "SourceRecord | None":
    """Attempt idempotent INSERT of a discovered source. Returns SourceRecord if newly inserted."""
    meeting_date_str = candidate.meeting_date.strftime("%Y-%m-%d") if candidate.meeting_date else None

    cursor = pipeline_db_conn.execute(
        """
        INSERT OR IGNORE INTO sources
            (source_id, pipeline_id, file_path, status, discovered_at, meeting_title, meeting_date)
        VALUES (?, ?, ?, 'pending', ?, ?, ?)
        """,
        (
            candidate.source_id,
            GEMINI_TRANSCRIPT_PIPELINE,
            "",
            now_iso,
            candidate.meeting_title or None,
            meeting_date_str,
        ),
    )
    if cursor.rowcount == 0:
        return None

    return SourceRecord(
        source_id=candidate.source_id,
        pipeline_id=GEMINI_TRANSCRIPT_PIPELINE,
        subject=candidate.subject,
        meeting_title=candidate.meeting_title,
        meeting_date=candidate.meeting_date,
        doc_url=candidate.doc_url,
        email_message_id=candidate.email_message_id,
        discovered_at=now_iso,
    )


# ── public API ─────────────────────────────────────────────────────────────


def filter_gemini_candidates_for_account(
    candidates: Sequence[_RoutableCandidateProtocol], account: str | None
) -> list[_RoutableCandidateProtocol]:
    """Return candidates addressed to *account*, or all candidates when unscoped."""
    if account is None:
        return list(candidates)

    from fieldkit.ingest.router import route_by_domains

    return [
        candidate
        for candidate in candidates
        if account in route_by_domains(list(candidate.recipient_addresses)).accounts
    ]


def discover_gemini_sources(
    pipeline_db_conn: sqlite3.Connection,
    candidates: Sequence[_CandidateProtocol],
) -> list[SourceRecord]:
    """Register candidates as pending sources in pipeline.db."""
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        newly_discovered = [
            record
            for candidate in candidates
            if (record := _try_register_source(pipeline_db_conn, candidate, now_iso)) is not None
        ]
        pipeline_db_conn.commit()
    except Exception:
        pipeline_db_conn.rollback()
        raise

    return newly_discovered


def mark_source_status(conn: sqlite3.Connection, source_id: str, status: SourceStatus) -> None:
    """Persist a source status transition with its processing timestamp."""
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "UPDATE sources SET status = ?, processed_at = ? WHERE source_id = ?",
        (status, now_iso, source_id),
    )
    conn.commit()


def insert_vault_note_artifact(
    conn: sqlite3.Connection, source_id: str, pipeline_version: str, content_path: str
) -> None:
    """Record the vault note produced for a processed transcript source."""
    artifact_id = str(uuid.uuid4())
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """
        INSERT INTO artifacts
            (artifact_id, source_id, pipeline_id, artifact_type, content_path,
             created_at, pipeline_version)
        VALUES (?, ?, 'transcript-ingest', 'vault_note', ?, ?, ?)
        """,
        (artifact_id, source_id, content_path, now_iso, pipeline_version),
    )
    conn.commit()


def claim_pending_source(conn: sqlite3.Connection, source_id: str) -> bool:
    """Atomically claim a pending source for processing.

    The caller's connection MUST have `busy_timeout` set (>= 1000ms) to ensure
    a concurrent claimer returns `False` rather than raising
    `OperationalError: database is locked`.
    """
    cursor = conn.execute(
        "UPDATE sources SET status = 'in_progress' WHERE source_id = ? AND status = 'pending'",
        (source_id,),
    )
    conn.commit()
    return cursor.rowcount == 1


def get_pending_sources(
    conn: sqlite3.Connection,
    pipeline_id: str,
    limit: int | None = None,
) -> list[SourceRecord]:
    """Return pending sources for a pipeline from pipeline.db."""
    query = """
        SELECT source_id, pipeline_id, file_path, discovered_at,
               meeting_title, meeting_date
        FROM sources
        WHERE pipeline_id = ? AND status = 'pending'
        ORDER BY discovered_at ASC
    """
    params: list[object] = [pipeline_id]
    if limit is not None:
        query += " LIMIT ?"
        params.append(int(limit))

    rows = conn.execute(query, params).fetchall()

    def _parse_date(date_str: str | None) -> datetime | None:
        if not date_str:
            return None
        try:
            return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            return None

    return [
        SourceRecord(
            source_id=row["source_id"],
            pipeline_id=row["pipeline_id"],
            subject="",
            meeting_title=row["meeting_title"] or "",
            meeting_date=_parse_date(row["meeting_date"]),
            doc_url=f"https://docs.google.com/document/d/{row['source_id']}",
            email_message_id="",
            discovered_at=row["discovered_at"],
        )
        for row in rows
    ]
