"""Committed-generation contracts for the SQLite publication foundation."""

import json
import multiprocessing
import os
import signal
import sqlite3
import stat
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fieldkit.sqlite_publication import (
    SQLitePublicationError,
    open_published_sqlite,
    sqlite_publication_writer,
)
from fieldkit.util.atomic import exclusive_file_lock

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("mismatch", ["generation", "identity", "source-generation"])
def test_conditional_writer_refuses_changed_generation_before_mutation(
    tmp_path: Path,
    mismatch: str,
) -> None:
    source = tmp_path / "source.db"
    publication = tmp_path / "published"
    with sqlite_publication_writer(source, publication, kind="test-records") as initial:
        initial.connection.execute("CREATE TABLE records(id INTEGER PRIMARY KEY, value INTEGER NOT NULL)")
        initial.connection.execute("INSERT INTO records(value) VALUES (7)")
    assert initial.receipt is not None
    expected = initial.receipt
    if mismatch == "generation":
        expected = replace(expected, generation=expected.generation + 1)
    elif mismatch == "identity":
        expected = replace(expected, database_uuid="00000000-0000-0000-0000-000000000000")
    else:
        with sqlite3.connect(source) as connection:
            connection.execute("UPDATE _fieldkit_publication SET generation = generation + 1")
    before = {name: (publication / name).read_bytes() for name in ("state.json", "receipt.json")}
    source_before = source.read_bytes()

    with (
        pytest.raises(SQLitePublicationError, match="changed before conditional update"),
        sqlite_publication_writer(source, publication, kind="test-records", expected_receipt=expected) as writer,
    ):
        writer.connection.execute("DELETE FROM records")

    assert source.read_bytes() == source_before
    assert {name: (publication / name).read_bytes() for name in before} == before
    assert _read_values(publication, initial.receipt.schema_digest) == [7]


def _publish_records(source: Path, publication: Path, values: tuple[int, ...] = (1,)) -> str:
    with sqlite_publication_writer(source, publication, kind="test-records") as writer:
        writer.connection.execute("CREATE TABLE IF NOT EXISTS records (id INTEGER PRIMARY KEY, value INTEGER NOT NULL)")
        writer.connection.execute("DELETE FROM records")
        writer.connection.executemany("INSERT INTO records(value) VALUES (?)", ((value,) for value in values))
    assert writer.receipt is not None
    return writer.receipt.schema_digest


def _read_values(publication: Path, schema_digest: str) -> list[int]:
    with open_published_sqlite(
        publication,
        expected_kind="test-records",
        expected_schema_digest=schema_digest,
    ) as connection:
        rows = connection.execute("SELECT value FROM records ORDER BY id").fetchall()
    return [int(row[0]) for row in rows]


def _leave_alias_hot_journal(database: str, alias: str, ready: Any) -> None:
    os.link(database, alias)
    connection = sqlite3.connect(alias)
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("PRAGMA cache_size=1")
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("UPDATE records SET value = 2")
    ready.set()
    signal.pause()


def _crash_during_managed_transaction(source: str, publication: str, ready: Any) -> None:
    with sqlite_publication_writer(Path(source), Path(publication), kind="test-records") as writer:
        writer.connection.execute("CREATE TABLE records (value INTEGER)")
        writer.connection.execute("INSERT INTO records VALUES (9)")
        ready.set()
        os._exit(17)


def _rewrite_receipt(publication: Path, rendered: str) -> None:
    receipt_path = publication / "receipt.json"
    receipt_path.write_text(rendered, encoding="utf-8")
    state_path = publication / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["receipt_sha256"] = sha256(rendered.encode("utf-8")).hexdigest()
    state_path.write_text(json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def test_writer_marks_updating_before_caller_mutation_and_publishes_ready_generation(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    publication = tmp_path / "published"

    with sqlite_publication_writer(source, publication, kind="test-records") as writer:
        state = json.loads((publication / "state.json").read_text(encoding="utf-8"))
        assert state == {"kind": "test-records", "status": "updating", "version": 1}
        writer.connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value INTEGER NOT NULL)")
        writer.connection.execute("INSERT INTO records(value) VALUES (7)")

    assert writer.receipt is not None
    assert writer.receipt.generation == 1
    assert _read_values(publication, writer.receipt.schema_digest) == [7]
    ready = json.loads((publication / "state.json").read_text(encoding="utf-8"))
    assert ready["status"] == "ready"
    assert ready["generation"] == 1


def test_writer_rejects_invalid_expected_schema_before_creating_storage(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    publication = tmp_path / "published"

    with (
        pytest.raises(ValueError, match="schema digest"),
        sqlite_publication_writer(
            source,
            publication,
            kind="test-records",
            expected_schema_digest="not-a-digest",
        ),
    ):
        pass

    assert not source.exists()
    assert not publication.exists()


def test_writer_accepts_dml_when_schema_matches_expected_digest(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    publication = tmp_path / "published"
    schema_digest = _publish_records(source, publication, (1,))

    with sqlite_publication_writer(
        source,
        publication,
        kind="test-records",
        expected_schema_digest=schema_digest,
    ) as writer:
        writer.connection.execute("UPDATE records SET value = 2")

    assert _read_values(publication, schema_digest) == [2]


def test_writer_rejects_schema_change_before_replacing_published_artifact(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    publication = tmp_path / "published"
    schema_digest = _publish_records(source, publication, (1,))
    artifact_before = set((publication / "artifacts").iterdir())

    with (
        pytest.raises(SQLitePublicationError, match="schema") as captured,
        sqlite_publication_writer(
            source,
            publication,
            kind="test-records",
            expected_schema_digest=schema_digest,
        ) as writer,
    ):
        writer.connection.execute("CREATE TABLE unexpected (value INTEGER)")

    assert captured.value.reason == "unverified"
    assert set((publication / "artifacts").iterdir()) == artifact_before
    state = json.loads((publication / "state.json").read_text(encoding="utf-8"))
    assert state == {"kind": "test-records", "status": "updating", "version": 1}


def test_writer_rejects_trigger_targeting_internal_metadata_and_remains_retryable(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    publication = tmp_path / "published"
    schema_digest = _publish_records(source, publication, (1,))
    raw = sqlite3.connect(source)
    database_uuid_before = raw.execute("SELECT database_uuid FROM _fieldkit_publication").fetchone()[0]
    raw.close()

    with (
        pytest.raises(SQLitePublicationError, match="transaction") as captured,
        sqlite_publication_writer(source, publication, kind="test-records") as writer,
    ):
        writer.connection.execute(
            "CREATE TRIGGER corrupt_publication_identity "
            "AFTER UPDATE OF generation ON _fieldkit_publication "
            "BEGIN "
            "UPDATE _fieldkit_publication SET database_uuid = "
            "'00000000-0000-4000-8000-000000000000'; "
            "END"
        )

    assert captured.value.reason == "unverified"
    raw = sqlite3.connect(source)
    try:
        assert raw.execute("SELECT database_uuid FROM _fieldkit_publication").fetchone()[0] == database_uuid_before
        assert (
            raw.execute(
                "SELECT COUNT(*) FROM sqlite_schema WHERE type = 'trigger' AND name = 'corrupt_publication_identity'"
            ).fetchone()[0]
            == 0
        )
    finally:
        raw.close()

    with sqlite_publication_writer(
        source,
        publication,
        kind="test-records",
        expected_schema_digest=schema_digest,
    ) as retry:
        retry.connection.execute("UPDATE records SET value = 3")
    assert _read_values(publication, schema_digest) == [3]


def test_writer_allows_user_trigger_and_harmless_metadata_read(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    publication = tmp_path / "published"
    _publish_records(source, publication, (1,))

    with sqlite_publication_writer(source, publication, kind="test-records") as writer:
        metadata = writer.connection.execute(
            "SELECT database_uuid, kind, generation FROM _fieldkit_publication"
        ).fetchone()
        writer.connection.execute("CREATE TABLE record_audit (value INTEGER NOT NULL)")
        writer.connection.execute(
            "CREATE TRIGGER audit_record_update AFTER UPDATE ON records "
            "BEGIN INSERT INTO record_audit(value) VALUES (NEW.value); END"
        )
        writer.connection.execute("UPDATE records SET value = 4")

    assert metadata is not None
    assert metadata[1] == "test-records"
    assert writer.receipt is not None
    with open_published_sqlite(
        publication,
        expected_kind="test-records",
        expected_schema_digest=writer.receipt.schema_digest,
    ) as connection:
        assert connection.execute("SELECT value FROM records").fetchone()[0] == 4
        assert connection.execute("SELECT value FROM record_audit").fetchone()[0] == 4


def test_mutation_cursor_iteration_does_not_expose_transaction_controls(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    publication = tmp_path / "published"
    schema_digest = _publish_records(source, publication, (1, 2))

    with sqlite_publication_writer(
        source,
        publication,
        kind="test-records",
        expected_schema_digest=schema_digest,
    ) as writer:
        cursor = writer.connection.execute("SELECT value FROM records ORDER BY id")
        iterator = iter(cursor)
        assert not hasattr(iterator, "connection")
        assert [int(row[0]) for row in iterator] == [1, 2]
        writer.connection.execute("UPDATE records SET value = value + 1")

    assert _read_values(publication, schema_digest) == [2, 3]


def test_new_source_is_private_before_sqlite_opens_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.db"
    publication = tmp_path / "published"
    real_connect = sqlite3.connect
    observed_modes: list[int] = []

    def inspecting_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = real_connect(database, *args, **kwargs)
        assert isinstance(connection, sqlite3.Connection)
        if Path(database) == source:
            observed_modes.append(stat.S_IMODE(source.stat().st_mode))
        return connection

    monkeypatch.setattr(sqlite3, "connect", inspecting_connect)
    previous_umask = os.umask(0o022)
    try:
        with sqlite_publication_writer(source, publication, kind="test-records") as writer:
            writer.connection.execute("CREATE TABLE records (value INTEGER)")
    finally:
        os.umask(previous_umask)

    assert observed_modes == [0o600]
    assert stat.S_IMODE(source.stat().st_mode) == 0o600


def test_new_source_is_removed_if_initial_sqlite_open_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.db"
    publication = tmp_path / "published"
    real_connect = sqlite3.connect

    def failing_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        if Path(database) == source:
            assert source.exists()
            assert stat.S_IMODE(source.stat().st_mode) == 0o600
            raise sqlite3.OperationalError("fixture open failure")
        connection = real_connect(database, *args, **kwargs)
        assert isinstance(connection, sqlite3.Connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", failing_connect)

    with (
        pytest.raises(SQLitePublicationError, match="unavailable") as captured,
        sqlite_publication_writer(source, publication, kind="test-records"),
    ):
        pass

    assert captured.value.reason == "unverified"
    assert not source.exists()
    assert json.loads((publication / "state.json").read_text(encoding="utf-8"))["status"] == "updating"


def test_next_writer_removes_one_bounded_staging_residual_before_mutation(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    publication = tmp_path / "published"
    publication.mkdir(mode=0o700)
    staging_root = publication / "staging"
    staging_root.mkdir(mode=0o700)
    staging = staging_root / "generation-crashed"
    staging.mkdir(mode=0o700)
    (staging / "backup.sqlite").write_bytes(b"private partial bytes")
    (staging / "backup.sqlite").chmod(0o600)

    with sqlite_publication_writer(source, publication, kind="test-records") as writer:
        assert list((publication / "staging").iterdir()) == []
        writer.connection.execute("CREATE TABLE records (value INTEGER)")

    assert writer.receipt is not None
    assert list((publication / "staging").iterdir()) == []


def test_second_publication_increments_embedded_generation(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    publication = tmp_path / "published"
    first_schema = _publish_records(source, publication, (1,))

    with sqlite_publication_writer(source, publication, kind="test-records") as writer:
        writer.connection.execute("UPDATE records SET value = 2")

    assert writer.receipt is not None
    assert writer.receipt.generation == 2
    assert writer.receipt.schema_digest == first_schema
    assert _read_values(publication, first_schema) == [2]
    assert len(list((publication / "artifacts").iterdir())) == 1


def test_writer_rejects_current_artifact_as_source_before_changing_publication(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    publication = tmp_path / "published"
    schema_digest = _publish_records(source, publication, (1,) * 5_000)
    receipt_before = (publication / "receipt.json").read_bytes()
    state_before = (publication / "state.json").read_bytes()
    receipt = json.loads(receipt_before)
    artifact = publication / receipt["artifact"]["path"]
    artifact_before = artifact.read_bytes()

    with (
        pytest.raises(SQLitePublicationError, match="publication artifact") as captured,
        sqlite_publication_writer(artifact, publication, kind="test-records"),
    ):
        pass

    assert captured.value.reason == "unverified"
    traversing_artifact = artifact.parent / ".." / "artifacts" / artifact.name
    with (
        pytest.raises(SQLitePublicationError, match="publication artifact") as traversing,
        sqlite_publication_writer(traversing_artifact, publication, kind="test-records"),
    ):
        pass
    assert traversing.value.reason == "unverified"
    assert artifact.read_bytes() == artifact_before
    assert (publication / "receipt.json").read_bytes() == receipt_before
    assert (publication / "state.json").read_bytes() == state_before
    assert _read_values(publication, schema_digest) == [1] * 5_000


def test_writer_rejects_artifact_inode_alias_outside_publication(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    publication = tmp_path / "published"
    _publish_records(source, publication)
    receipt = json.loads((publication / "receipt.json").read_text(encoding="utf-8"))
    artifact = publication / receipt["artifact"]["path"]
    alias = tmp_path / "artifact-alias.sqlite"
    os.link(artifact, alias)
    state_before = (publication / "state.json").read_bytes()

    with (
        pytest.raises(SQLitePublicationError, match="publication artifact") as captured,
        sqlite_publication_writer(alias, publication, kind="test-records"),
    ):
        pass

    assert captured.value.reason == "unverified"
    assert (publication / "state.json").read_bytes() == state_before


def test_publication_identity_rejects_different_managed_database_before_state_change(tmp_path: Path) -> None:
    source_a = tmp_path / "a.db"
    source_b = tmp_path / "b.db"
    publication_a = tmp_path / "published-a"
    publication_b = tmp_path / "published-b"
    _publish_records(source_a, publication_a, (1,))
    _publish_records(source_b, publication_b, (2,))
    identity_before = (publication_a / "identity.json").read_bytes()
    receipt_before = (publication_a / "receipt.json").read_bytes()
    state_before = (publication_a / "state.json").read_bytes()

    with (
        pytest.raises(SQLitePublicationError, match="identity") as captured,
        sqlite_publication_writer(source_b, publication_a, kind="test-records"),
    ):
        pass

    assert captured.value.reason == "unverified"
    assert (publication_a / "identity.json").read_bytes() == identity_before
    assert (publication_a / "receipt.json").read_bytes() == receipt_before
    assert (publication_a / "state.json").read_bytes() == state_before


def test_open_reader_keeps_its_immutable_generation_when_next_one_is_published(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    publication = tmp_path / "published"
    schema_digest = _publish_records(source, publication, (1,))
    first_reader = open_published_sqlite(
        publication,
        expected_kind="test-records",
        expected_schema_digest=schema_digest,
    )
    try:
        with sqlite_publication_writer(source, publication, kind="test-records") as writer:
            writer.connection.execute("UPDATE records SET value = 2")

        assert [row[0] for row in first_reader.execute("SELECT value FROM records").fetchall()] == [1]
        assert _read_values(publication, schema_digest) == [2]
        assert len(list((publication / "artifacts").iterdir())) == 1
    finally:
        first_reader.close()


def test_caller_cannot_commit_inside_publication_transaction(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    publication = tmp_path / "published"

    with (
        pytest.raises(SQLitePublicationError, match="transaction") as captured,
        sqlite_publication_writer(source, publication, kind="test-records") as writer,
    ):
        writer.connection.execute("CREATE TABLE records (value INTEGER)")
        writer.connection.execute("COMMIT")

    assert captured.value.reason == "unverified"
    state = json.loads((publication / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "updating"


def test_writer_exposes_only_restricted_mutation_facade(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    publication = tmp_path / "published"

    with sqlite_publication_writer(source, publication, kind="test-records") as writer:
        cursor = writer.connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value INTEGER)")
        assert cursor.rowcount == -1
        for forbidden in (
            "_connection",
            "backup",
            "close",
            "commit",
            "isolation_level",
            "rollback",
            "set_authorizer",
            "executescript",
        ):
            assert not hasattr(writer.connection, forbidden)
        assert not hasattr(cursor, "connection")
        assert not hasattr(cursor, "_cursor")
        writer.connection.execute("INSERT INTO records(value) VALUES (1)")

    assert writer.receipt is not None
    assert _read_values(publication, writer.receipt.schema_digest) == [1]


def test_caller_cannot_mutate_internal_publication_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    publication = tmp_path / "published"

    with (
        pytest.raises(SQLitePublicationError, match="transaction") as captured,
        sqlite_publication_writer(source, publication, kind="test-records") as writer,
    ):
        writer.connection.execute("UPDATE _fieldkit_publication SET generation = 99")

    assert captured.value.reason == "unverified"
    assert json.loads((publication / "state.json").read_text(encoding="utf-8"))["status"] == "updating"


def test_writer_exception_leaves_nonready_state_and_no_reader_fallback(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    publication = tmp_path / "published"

    with (
        pytest.raises(RuntimeError, match="stop"),
        sqlite_publication_writer(source, publication, kind="test-records") as writer,
    ):
        writer.connection.execute("CREATE TABLE records (value INTEGER)")
        raise RuntimeError("stop")

    with pytest.raises(SQLitePublicationError, match="incomplete") as captured:
        open_published_sqlite(
            publication,
            expected_kind="test-records",
            expected_schema_digest="0" * 64,
        )
    assert captured.value.reason == "unverified"


def test_process_crash_leaves_durable_nonready_state(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    publication = tmp_path / "published"
    ready = multiprocessing.Event()
    process = multiprocessing.Process(
        target=_crash_during_managed_transaction,
        args=(str(source), str(publication), ready),
    )
    process.start()
    assert ready.wait(10)
    process.join(10)
    assert not process.is_alive()
    assert process.exitcode == 17
    assert json.loads((publication / "state.json").read_text(encoding="utf-8"))["status"] == "updating"

    with pytest.raises(SQLitePublicationError) as captured:
        open_published_sqlite(
            publication,
            expected_kind="test-records",
            expected_schema_digest="0" * 64,
        )
    assert captured.value.reason == "unverified"


def test_active_writer_is_retryable_and_never_uses_previous_generation(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    publication = tmp_path / "published"
    schema_digest = _publish_records(source, publication)

    lock_path = publication / "publication.lock"
    with (
        exclusive_file_lock(lock_path, timeout_seconds=0),
        pytest.raises(SQLitePublicationError, match="active") as captured,
    ):
        open_published_sqlite(
            publication,
            expected_kind="test-records",
            expected_schema_digest=schema_digest,
            timeout_seconds=0,
        )
    assert captured.value.reason == "active"


def test_alias_hot_journal_cannot_change_last_published_generation(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    alias = tmp_path / "alias.db"
    publication = tmp_path / "published"
    with sqlite_publication_writer(source, publication, kind="test-records") as writer:
        writer.connection.execute(
            "CREATE TABLE records (id INTEGER PRIMARY KEY, value INTEGER NOT NULL, padding TEXT NOT NULL)"
        )
        writer.connection.executemany(
            "INSERT INTO records(value, padding) VALUES (1, ?)",
            (("x" * 400,) for _ in range(5_000)),
        )
    assert writer.receipt is not None
    schema_digest = writer.receipt.schema_digest
    ready = multiprocessing.Event()
    process = multiprocessing.Process(target=_leave_alias_hot_journal, args=(str(source), str(alias), ready))
    process.start()
    try:
        assert ready.wait(10)
        assert process.pid is not None
        os.kill(process.pid, signal.SIGKILL)
        process.join(10)
        assert not process.is_alive()
        alias.unlink()
        assert (tmp_path / "alias.db-journal").exists()
        raw = sqlite3.connect(f"{source.as_uri()}?mode=ro&immutable=1", uri=True)
        try:
            raw_groups = raw.execute("SELECT value, COUNT(*) FROM records GROUP BY value ORDER BY value").fetchall()
        finally:
            raw.close()
        assert len(raw_groups) == 2
        assert {row[0] for row in raw_groups} == {1, 2}

        # The live main file is now untrusted. The reader does not inspect or
        # copy it; it returns the independently published committed generation.
        assert _read_values(publication, schema_digest) == [1] * 5_000
    finally:
        if process.is_alive():
            process.kill()
            process.join(10)


def test_reader_rejects_malformed_artifact_before_returning_connection(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    publication = tmp_path / "published"
    schema_digest = _publish_records(source, publication)
    receipt = json.loads((publication / "receipt.json").read_text(encoding="utf-8"))
    artifact = publication / receipt["artifact"]["path"]
    artifact.write_bytes(bytes(artifact.stat().st_size))

    with pytest.raises(SQLitePublicationError, match="unverified") as captured:
        open_published_sqlite(
            publication,
            expected_kind="test-records",
            expected_schema_digest=schema_digest,
        )
    assert captured.value.reason == "unverified"


def test_reader_rejects_unexpected_schema_without_opening_live_source(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    publication = tmp_path / "published"
    _publish_records(source, publication)

    with pytest.raises(SQLitePublicationError, match="generation") as captured:
        open_published_sqlite(
            publication,
            expected_kind="test-records",
            expected_schema_digest="0" * 64,
        )
    assert captured.value.reason == "unverified"


@pytest.mark.parametrize(
    "replacement",
    [
        '{"version":1,"status":"ready","status":"updating"}',
        '{"version":1,"status":"ready","kind":"test-records","generation":NaN,"receipt_sha256":"x"}',
        '{"version":1,"status":"ready","kind":"test-records","generation":1,"receipt_sha256":"x","extra":1}',
    ],
)
def test_reader_rejects_ambiguous_or_unknown_state_json(tmp_path: Path, replacement: str) -> None:
    source = tmp_path / "source.db"
    publication = tmp_path / "published"
    schema_digest = _publish_records(source, publication)
    (publication / "state.json").write_text(replacement, encoding="utf-8")

    with pytest.raises(SQLitePublicationError, match="unverified") as captured:
        open_published_sqlite(
            publication,
            expected_kind="test-records",
            expected_schema_digest=schema_digest,
        )
    assert captured.value.reason == "unverified"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda rendered: rendered.replace('"path":', '"path":"ignored","path":', 1),
        lambda rendered: rendered.replace('"size":', '"unknown":1,"size":', 1),
        lambda rendered: rendered.replace('"artifacts/', '"../', 1),
    ],
)
def test_reader_rejects_ambiguous_unknown_or_traversing_receipt(
    tmp_path: Path,
    mutate: Any,
) -> None:
    source = tmp_path / "source.db"
    publication = tmp_path / "published"
    schema_digest = _publish_records(source, publication)
    receipt_path = publication / "receipt.json"
    _rewrite_receipt(publication, mutate(receipt_path.read_text(encoding="utf-8")))

    with pytest.raises(SQLitePublicationError) as captured:
        open_published_sqlite(
            publication,
            expected_kind="test-records",
            expected_schema_digest=schema_digest,
        )
    assert captured.value.reason == "unverified"


def test_reader_rejects_redirected_or_hardlinked_artifact(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    publication = tmp_path / "published"
    schema_digest = _publish_records(source, publication)
    receipt = json.loads((publication / "receipt.json").read_text(encoding="utf-8"))
    artifact = publication / receipt["artifact"]["path"]
    alias = tmp_path / "artifact-alias.db"
    os.link(artifact, alias)

    with pytest.raises(SQLitePublicationError, match="unverified") as captured:
        open_published_sqlite(
            publication,
            expected_kind="test-records",
            expected_schema_digest=schema_digest,
        )
    assert captured.value.reason == "unverified"


def test_reader_rejects_artifact_sidecar_even_when_it_is_a_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    publication = tmp_path / "published"
    schema_digest = _publish_records(source, publication)
    receipt = json.loads((publication / "receipt.json").read_text(encoding="utf-8"))
    artifact = publication / receipt["artifact"]["path"]
    Path(f"{artifact}-wal").symlink_to(tmp_path / "missing-target")

    with pytest.raises(SQLitePublicationError, match="sidecars") as captured:
        open_published_sqlite(
            publication,
            expected_kind="test-records",
            expected_schema_digest=schema_digest,
        )
    assert captured.value.reason == "unverified"


def test_reader_rejects_publication_storage_not_owned_by_effective_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.db"
    publication = tmp_path / "published"
    schema_digest = _publish_records(source, publication)
    monkeypatch.setattr("fieldkit._sqlite_publication_storage._effective_user_id", lambda: os.geteuid() + 1)

    with pytest.raises(SQLitePublicationError, match="owned") as captured:
        open_published_sqlite(
            publication,
            expected_kind="test-records",
            expected_schema_digest=schema_digest,
        )

    assert captured.value.reason == "unverified"


def test_reader_does_not_create_a_missing_publication_lock(tmp_path: Path) -> None:
    publication = tmp_path / "published"
    publication.mkdir(mode=0o700)

    with pytest.raises(SQLitePublicationError, match="lock") as captured:
        open_published_sqlite(
            publication,
            expected_kind="test-records",
            expected_schema_digest="0" * 64,
        )
    assert captured.value.reason == "unverified"
    assert not (publication / "publication.lock").exists()


def test_writer_rejects_publication_root_through_symlinked_ancestor(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)

    with (
        pytest.raises(SQLitePublicationError, match="redirected") as captured,
        sqlite_publication_writer(tmp_path / "source.db", alias / "published", kind="test-records"),
    ):
        pass
    assert captured.value.reason == "unverified"
    assert not (tmp_path / "source.db").exists()


def test_writer_enforces_deadline_before_commit(tmp_path: Path) -> None:
    publication = tmp_path / "published"
    with (
        pytest.raises(SQLitePublicationError, match="deadline") as captured,
        sqlite_publication_writer(
            tmp_path / "source.db",
            publication,
            kind="test-records",
            timeout_seconds=0,
        ) as writer,
    ):
        writer.connection.execute("CREATE TABLE records (value INTEGER)")
    assert captured.value.reason == "resource"
    assert json.loads((publication / "state.json").read_text(encoding="utf-8"))["status"] == "updating"


def test_writer_fails_before_backup_when_private_storage_is_insufficient(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("fieldkit._sqlite_publication_storage.shutil.disk_usage", lambda path: SimpleNamespace(free=0))
    publication = tmp_path / "published"

    with (
        pytest.raises(SQLitePublicationError, match="storage") as captured,
        sqlite_publication_writer(tmp_path / "source.db", publication, kind="test-records") as writer,
    ):
        writer.connection.execute("CREATE TABLE records (value INTEGER)")
    assert captured.value.reason == "resource"
    assert json.loads((publication / "state.json").read_text(encoding="utf-8"))["status"] == "updating"


def test_existing_unmanaged_database_is_not_silently_adopted(tmp_path: Path) -> None:
    source = tmp_path / "external.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE records (value INTEGER)")
        connection.execute("INSERT INTO records VALUES (1)")
    source.chmod(0o600)

    with (
        pytest.raises(SQLitePublicationError, match="import") as captured,
        sqlite_publication_writer(source, tmp_path / "published", kind="test-records"),
    ):
        pass
    assert captured.value.reason == "unverified"
