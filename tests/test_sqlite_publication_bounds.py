"""Resource and exceptional-exit contracts for committed SQLite readers."""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sysconfig
import time
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fieldkit import _sqlite_publication_storage as storage
from fieldkit import sqlite_publication as publication
from fieldkit.util import json_decode

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("action", [-1, *range(34), 2**31])
def test_metadata_authorizer_union_preserves_the_original_action_policy(action: int) -> None:
    original_schema_actions = {
        sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_ANALYZE,
        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_CREATE_TEMP_INDEX,
        sqlite3.SQLITE_CREATE_TEMP_TABLE,
        sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
        sqlite3.SQLITE_CREATE_TEMP_VIEW,
        sqlite3.SQLITE_CREATE_TRIGGER,
        sqlite3.SQLITE_CREATE_VIEW,
        sqlite3.SQLITE_CREATE_VTABLE,
        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_DROP_TEMP_INDEX,
        sqlite3.SQLITE_DROP_TEMP_TABLE,
        sqlite3.SQLITE_DROP_TEMP_TRIGGER,
        sqlite3.SQLITE_DROP_TEMP_VIEW,
        sqlite3.SQLITE_DROP_TRIGGER,
        sqlite3.SQLITE_DROP_VIEW,
        sqlite3.SQLITE_DROP_VTABLE,
        sqlite3.SQLITE_REINDEX,
    }
    original_metadata_actions = {
        sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_DROP_TRIGGER,
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
    }

    assert (action in storage._METADATA_AUTHORIZER_ACTIONS) == (
        action in original_schema_actions or action in original_metadata_actions
    )


@pytest.mark.parametrize("depth", [64, 65])
def test_metadata_has_an_explicit_container_depth_policy(tmp_path: Path, depth: int) -> None:
    metadata = tmp_path / "metadata.json"
    raw = b'{"value":' + b"[" * (depth - 1) + b"0" + b"]" * (depth - 1) + b"}\n"
    metadata.write_bytes(raw)
    metadata.chmod(0o600)

    if depth == 64:
        result = storage._strict_json(metadata)
        assert result == (json.loads(raw), raw)
    else:
        with pytest.raises(storage._StorageFailure, match=r"^SQLite publication metadata is unverified$") as caught:
            storage._strict_json(metadata)
        assert caught.value.reason == "unverified"
        assert caught.value.__cause__ is None


@pytest.mark.parametrize("version", ["3.11", "3.14"])
@pytest.mark.parametrize("case", ["depth64", "depth65", "deep", "injected"])
def test_supported_interpreters_enforce_the_actual_product_depth_policy(
    tmp_path: Path, version: str, case: str
) -> None:
    interpreter = shutil.which(f"python{version}")
    if interpreter is None:
        pytest.skip(f"Python {version} is not installed")
    arrays = {"depth64": 63, "depth65": 64, "deep": 1_500, "injected": 0}[case]
    metadata = tmp_path / "metadata.json"
    metadata.write_bytes(b'{"value":' + b"[" * arrays + b"0" + b"]" * arrays + b"}\n")
    metadata.chmod(0o600)
    script = """
import json
import sys
from pathlib import Path
from fieldkit._sqlite_publication_storage import _StorageFailure, _strict_json
assert f"{sys.version_info.major}.{sys.version_info.minor}" == sys.argv[3]
if sys.argv[2] == "injected":
    def fail_parser(*args, **kwargs):
        raise RecursionError("private-parser-detail")
    json.loads = fail_parser
try:
    result = _strict_json(Path(sys.argv[1]))
except _StorageFailure as error:
    assert str(error) == "SQLite publication metadata is unverified"
    assert error.reason == "unverified"
    assert error.__cause__ is None
    print("rejected")
else:
    raw = Path(sys.argv[1]).read_bytes()
    assert result == (json.loads(raw), raw)
    print("accepted")
"""
    result = subprocess.run(
        [interpreter, "-c", script, str(metadata), case, version],
        env={
            "PYTHONPATH": os.pathsep.join(
                (str(Path(publication.__file__).resolve().parents[1]), sysconfig.get_path("purelib"))
            ),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ("accepted\n" if case == "depth64" else "rejected\n")


@pytest.mark.parametrize("value", [None, 0, "scalar", {"items": [0] * 1_000}])
def test_json_depth_helper_accepts_scalars_and_wide_shallow_values(value: object) -> None:
    json_decode.require_json_container_depth(value, maximum_depth=2)


def test_json_depth_helper_rejects_a_cycle_without_recursive_traversal() -> None:
    value: list[object] = []
    value.append(value)

    with pytest.raises(ValueError, match=r"^JSON input exceeds nesting limit$"):
        json_decode.require_json_container_depth(value, maximum_depth=64)


@pytest.mark.parametrize("maximum_depth", [0, -1, True])
def test_json_depth_helper_rejects_invalid_limits(maximum_depth: int) -> None:
    with pytest.raises(ValueError, match=r"^JSON container depth limit must be a positive integer$"):
        json_decode.require_json_container_depth({}, maximum_depth=maximum_depth)


@pytest.mark.parametrize("injected", [False, True])
def test_metadata_recursion_is_a_fixed_typed_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, injected: bool
) -> None:
    metadata = tmp_path / "metadata.json"
    metadata.write_bytes(b'{"value":' + b"[" * 1_500 + b"0" + b"]" * 1_500 + b"}")
    metadata.chmod(0o600)
    if injected:

        def recursive_parser(*_args: object, **_kwargs: object) -> object:
            raise RecursionError("private-parser-detail")

        monkeypatch.setattr(json, "loads", recursive_parser)

    with pytest.raises(storage._StorageFailure, match=r"^SQLite publication metadata is unverified$") as caught:
        storage._strict_json(metadata)

    assert caught.value.reason == "unverified"
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("raw", [b'{"value":1,"value":2}', b'{"value":NaN}', b'{"value":Infinity}'])
def test_metadata_still_rejects_ambiguous_or_nonfinite_json(tmp_path: Path, raw: bytes) -> None:
    metadata = tmp_path / "metadata.json"
    metadata.write_bytes(raw)
    metadata.chmod(0o600)

    with pytest.raises(storage._StorageFailure, match="metadata is unverified") as caught:
        storage._strict_json(metadata)

    assert caught.value.reason == "unverified"


def test_metadata_returns_the_exact_acquired_bytes(tmp_path: Path) -> None:
    metadata = tmp_path / "metadata.json"
    raw = b' { "value" : 1 }\n'
    metadata.write_bytes(raw)
    metadata.chmod(0o600)

    result = storage._strict_json(metadata)

    assert result == ({"value": 1}, raw)


@pytest.mark.parametrize("bound", ["rows", "bytes", "deadline"])
def test_schema_stops_iteration_at_the_first_exceeded_bound(monkeypatch: pytest.MonkeyPatch, bound: str) -> None:
    consumed: list[int] = []

    def rows() -> Iterator[tuple[str, str, str, str]]:
        consumed.append(1)
        yield ("table", "records", "records", "CREATE TABLE records(value INTEGER)")
        consumed.append(2)
        yield ("view", "record_view", "record_view", "CREATE VIEW record_view AS SELECT value FROM records")
        raise AssertionError("schema consumed beyond the failed bound")

    connection = MagicMock(spec=sqlite3.Connection)
    cursor = MagicMock(spec=sqlite3.Cursor)
    cursor.__iter__.side_effect = rows
    cursor.fetchall.side_effect = AssertionError("schema materialized before enforcing its bound")
    connection.execute.return_value = cursor
    if bound == "rows":
        monkeypatch.setattr(storage, "_MAX_SCHEMA_ROWS", 1)
    elif bound == "bytes":
        monkeypatch.setattr(storage, "_MAX_SCHEMA_BYTES", 1)
    else:
        monkeypatch.setattr(time, "monotonic", lambda: 2.0)

    with pytest.raises(storage._StorageFailure, match="bound") as caught:
        storage._schema_digest(connection, deadline=1.0 if bound == "deadline" else float("inf"))

    assert caught.value.reason == "resource"
    assert len(consumed) <= (2 if bound == "rows" else 1)
    cursor.fetchall.assert_not_called()
    connection.set_progress_handler.assert_any_call(None, 0)


def test_schema_digest_preserves_the_canonical_supported_encoding() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE records(value INTEGER)")
        expected_rows = [
            list(row)
            for row in connection.execute(
                "SELECT type, name, tbl_name, COALESCE(sql, '') FROM sqlite_schema "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name, tbl_name, sql"
            )
        ]
        expected = hashlib.sha256(
            (
                json.dumps({"schema": expected_rows}, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
            ).encode()
        ).hexdigest()

        result = storage._schema_digest(connection, deadline=time.monotonic() + 5)

        assert result == expected
        assert connection.execute("SELECT COUNT(*) FROM records").fetchone() == (0,)
    finally:
        connection.close()


def _published(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "publication"
    with publication.sqlite_publication_writer(tmp_path / "source.sqlite", root, kind="test-records") as writer:
        writer.connection.execute("CREATE TABLE records(value INTEGER)")
        writer.connection.execute("INSERT INTO records VALUES (7)")
    assert writer.receipt is not None
    return root, writer.receipt.schema_digest


@pytest.mark.parametrize("phase", ["commit", "backup", "receipt", "retirement", "ready"])
def test_writer_fault_phase_preserves_the_existing_readiness_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    root, schema = _published(tmp_path)
    operation_name = {
        "commit": "_publish_backup",
        "backup": "_publish_backup",
        "receipt": "_atomic_private_json",
        "retirement": "_remove_superseded_artifacts",
        "ready": "_atomic_private_json",
    }[phase]
    operation = MagicMock(wraps=getattr(storage, operation_name))

    def fail_after_operation(*args: object, **kwargs: object) -> object:
        if phase == "commit":
            raise storage._StorageFailure("injected storage failure", reason="resource")
        result = operation(*args, **kwargs)
        if phase in {"receipt", "ready"}:
            path, payload = args
            assert isinstance(path, Path)
            assert isinstance(payload, dict)
            selected = path.name == "receipt.json" if phase == "receipt" else payload.get("status") == "ready"
            if not selected:
                return result
        raise storage._StorageFailure("injected storage failure", reason="resource")

    monkeypatch.setattr(storage, operation_name, fail_after_operation)
    writers: list[publication.SQLitePublicationWriter] = []
    with (
        pytest.raises(publication.SQLitePublicationError, match="injected storage failure") as caught,
        publication.sqlite_publication_writer(
            tmp_path / "source.sqlite", root, kind="test-records", expected_schema_digest=schema
        ) as writer,
    ):
        writers.append(writer)
        writer.connection.execute("INSERT INTO records VALUES (8)")

    assert caught.value.reason == "resource"
    assert len(writers) == 1
    assert writers[0].receipt is None
    assert list((root / "staging").iterdir()) == []
    source = sqlite3.connect(f"{(tmp_path / 'source.sqlite').as_uri()}?mode=ro", uri=True)
    try:
        assert source.execute("SELECT generation FROM _fieldkit_publication").fetchone() == (2,)
    finally:
        source.close()
    state = json.loads((root / "state.json").read_bytes())
    if phase == "ready":
        assert state["status"] == "ready"
        reader = publication.open_published_sqlite(root, expected_kind="test-records", expected_schema_digest=schema)
        try:
            assert isinstance(reader, sqlite3.Connection)
            assert [row[0] for row in reader.execute("SELECT value FROM records ORDER BY value")] == [7, 8]
        finally:
            reader.close()
    else:
        assert state["status"] == "updating"
        with pytest.raises(publication.SQLitePublicationError, match="incomplete"):
            publication.open_published_sqlite(root, expected_kind="test-records", expected_schema_digest=schema)


@pytest.mark.parametrize("original", [KeyboardInterrupt("interrupted"), SystemExit(17), RuntimeError("caller failure")])
def test_writer_preserves_the_original_caller_failure(tmp_path: Path, original: BaseException) -> None:
    root, schema = _published(tmp_path)
    with (
        pytest.raises(type(original), match=r"interrupted|17|caller failure") as caught,
        publication.sqlite_publication_writer(tmp_path / "source.sqlite", root, kind="test-records"),
    ):
        raise original

    assert caught.value is original
    with pytest.raises(publication.SQLitePublicationError, match="incomplete"):
        publication.open_published_sqlite(root, expected_kind="test-records", expected_schema_digest=schema)


def test_receipt_and_ready_bytes_preserve_the_canonical_protocol(tmp_path: Path) -> None:
    root, schema = _published(tmp_path)
    receipt = json.loads((root / "receipt.json").read_bytes())
    assert receipt["schema_digest"] == schema
    expected_receipt = {
        "version": 1,
        "kind": "test-records",
        "database_uuid": receipt["database_uuid"],
        "generation": 1,
        "schema_digest": schema,
        "artifact": {
            "path": f"artifacts/{receipt['artifact']['sha256']}.sqlite",
            "sha256": receipt["artifact"]["sha256"],
            "size": receipt["artifact"]["size"],
        },
    }
    expected_bytes = (json.dumps(expected_receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
    assert (root / "receipt.json").read_bytes() == expected_bytes
    expected_state = {
        "version": 1,
        "kind": "test-records",
        "status": "ready",
        "generation": 1,
        "receipt_sha256": hashlib.sha256(expected_bytes).hexdigest(),
    }
    assert (root / "state.json").read_bytes() == (
        json.dumps(expected_state, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


@pytest.mark.parametrize("entrypoint", ["reader", "writer"])
@pytest.mark.parametrize("operation", ["_ensure_private_directory", "_publication_lock"])
def test_public_entrypoints_normalize_private_setup_and_lock_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entrypoint: str, operation: str
) -> None:
    root, schema = _published(tmp_path)
    before = {name: (root / name).read_bytes() for name in ("identity.json", "state.json", "receipt.json")}
    source_before = (tmp_path / "source.sqlite").read_bytes()

    def fail(*_args: object, **_kwargs: object) -> None:
        raise storage._StorageFailure("injected storage failure", reason="active")

    monkeypatch.setattr(storage, operation, fail)
    with pytest.raises(publication.SQLitePublicationError, match=r"^injected storage failure$") as caught:
        if entrypoint == "reader":
            publication.open_published_sqlite(root, expected_kind="test-records", expected_schema_digest=schema)
        else:
            with publication.sqlite_publication_writer(tmp_path / "source.sqlite", root, kind="test-records"):
                pytest.fail("failed storage setup entered caller mutation")

    assert caught.value.reason == "active"
    assert caught.value.__cause__ is None
    assert {name: (root / name).read_bytes() for name in before} == before
    assert (tmp_path / "source.sqlite").read_bytes() == source_before


def test_reader_normalizes_private_validation_failure_after_connection_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, schema = _published(tmp_path)
    connections: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def connect(database: str, *, uri: bool, isolation_level: None, timeout: float) -> sqlite3.Connection:
        connection = real_connect(database, uri=uri, isolation_level=isolation_level, timeout=timeout)
        connections.append(connection)
        return connection

    def fail_validation(*_args: object, **_kwargs: object) -> str:
        raise storage._StorageFailure("injected storage failure", reason="resource")

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(storage, "_validate_sqlite", fail_validation)
    with pytest.raises(publication.SQLitePublicationError, match=r"^injected storage failure$") as caught:
        publication.open_published_sqlite(root, expected_kind="test-records", expected_schema_digest=schema)

    assert caught.value.reason == "resource"
    assert caught.value.__cause__ is None
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connections[0].execute("SELECT 1")


@pytest.mark.parametrize("phase", ["validation", "lock-exit"])
@pytest.mark.parametrize("category", ["keyboard", "system", "ordinary"])
def test_reader_closes_an_acquired_connection_on_every_exceptional_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, category: str
) -> None:
    root, schema = _published(tmp_path)
    errors: dict[str, BaseException] = {
        "keyboard": KeyboardInterrupt("interrupted"),
        "system": SystemExit(17),
        "ordinary": RuntimeError("injected reader failure"),
    }
    original = errors[category]
    connections: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def connect(database: str, *, uri: bool, isolation_level: None, timeout: float) -> sqlite3.Connection:
        connection = real_connect(database, uri=uri, isolation_level=isolation_level, timeout=timeout)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    if phase == "validation":

        def fail_validation(*_args: object, **_kwargs: object) -> str:
            raise original

        monkeypatch.setattr(storage, "_validate_sqlite", fail_validation)
    else:
        real_lock = storage._publication_lock

        @contextmanager
        def fail_lock_exit(path: Path, *, timeout_seconds: float, create: bool) -> Generator[None, None, None]:
            with real_lock(path, timeout_seconds=timeout_seconds, create=create):
                yield
            raise original

        monkeypatch.setattr(storage, "_publication_lock", fail_lock_exit)

    with pytest.raises(type(original), match=r"interrupted|17|injected reader failure") as caught:
        publication.open_published_sqlite(root, expected_kind="test-records", expected_schema_digest=schema)

    assert caught.value is original
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connections[0].execute("SELECT 1")


@pytest.mark.parametrize("original", [KeyboardInterrupt("interrupted"), SystemExit(17), RuntimeError("reader failure")])
def test_reader_cleanup_failure_does_not_replace_the_original_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, original: BaseException
) -> None:
    root, schema = _published(tmp_path)
    closed: list[bool] = []
    real_connect = sqlite3.connect

    class FailedClose(sqlite3.Connection):
        def close(self) -> None:
            super().close()
            closed.append(True)
            raise OSError("private-close-detail")

    def connect(database: str, *, uri: bool, isolation_level: None, timeout: float) -> sqlite3.Connection:
        return real_connect(database, uri=uri, isolation_level=isolation_level, timeout=timeout, factory=FailedClose)

    def fail_validation(*_args: object, **_kwargs: object) -> str:
        raise original

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(storage, "_validate_sqlite", fail_validation)
    with pytest.raises(type(original), match=r"interrupted|17|reader failure") as caught:
        publication.open_published_sqlite(root, expected_kind="test-records", expected_schema_digest=schema)

    assert caught.value is original
    assert closed == [True]
    assert original.__notes__ == ["SQLite publication connection cleanup failed"]


def test_reader_success_keeps_the_returned_connection_open(tmp_path: Path) -> None:
    root, schema = _published(tmp_path)
    connection = publication.open_published_sqlite(root, expected_kind="test-records", expected_schema_digest=schema)
    try:
        assert connection.execute("SELECT value FROM records").fetchone()[0] == 7
    finally:
        connection.close()
