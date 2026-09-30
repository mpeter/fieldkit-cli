"""Apply retained ingest intent before committing source completion."""

import sqlite3
from pathlib import Path

from fieldkit.ingest.note_effect import publish_prepared_note
from fieldkit.ingest.prepared import ReplayIntent, complete_prepared, load_prepared
from fieldkit.ingest.pursuit_effect import publish_prepared_pursuit
from fieldkit.ingest.task_effect import publish_prepared_tasks


def recover_interrupted_sources(conn: sqlite3.Connection) -> int:
    """Requeue interrupted transcript claims under the exclusive run lock.

    Keep retained intent unchanged. Claims without intent can prepare afresh;
    claims with intent must replay it. Never use elapsed time to steal work.
    """
    if conn.in_transaction:
        raise ValueError("Prepared recovery requires an idle connection")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        inconsistent = conn.execute(
            "SELECT 1 FROM sources s JOIN artifacts a ON a.source_id = s.source_id "
            "WHERE s.pipeline_id = 'transcript-ingest' AND s.status = 'in_progress' LIMIT 1"
        ).fetchone()
        if inconsistent is not None:
            raise ValueError("Interrupted source has conflicting completion evidence")
        result = conn.execute(
            "UPDATE sources SET status = 'pending' WHERE pipeline_id = 'transcript-ingest' AND status = 'in_progress'"
        )
    return result.rowcount


def replay_prepared(conn: sqlite3.Connection, source_id: str, workspace: Path) -> Path:
    """Replay a claimed source under the caller's exclusive transcript-run lock.

    Intent must already be committed. Any failure leaves that intent available
    for retry; only successful application of every effect permits completion.
    No credentials, classifiers, or model calls participate in replay.
    """
    if conn.in_transaction:
        raise ValueError("Prepared replay requires an idle connection")
    prepared = load_prepared(conn, source_id)
    state = conn.execute("SELECT pipeline_id, status FROM sources WHERE source_id = ?", (source_id,)).fetchone()
    if prepared is None or state is None or tuple(state) != (prepared.pipeline_id, "in_progress"):
        raise ValueError("Prepared replay requires retained intent and a claimed source")
    intent = ReplayIntent(prepared)
    publish_prepared_note(intent, workspace)
    for slug in prepared.pursuits:
        publish_prepared_pursuit(intent, workspace, slug)
    publish_prepared_tasks(intent, workspace)
    return complete_prepared(conn, prepared, workspace)
