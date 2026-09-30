"""Durable reprocess intent is bound to one exact existing artifact."""

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.errors import LLMError, LLMErrorCategory
from fieldkit.ingest.db import init_db
from fieldkit.ingest.reprocess_journal import (
    PreparedReprocess,
    complete_reprocess,
    decode_reprocess,
    load_reprocess,
    reprocess_checkpoint_key,
    save_reprocess,
)

pytestmark = pytest.mark.unit


def _original_note() -> str:
    return "---\nsource_id: source-1\npipeline: transcript-ingest\npipeline_version: 0.1.0\n---\nOriginal\n"


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = init_db(tmp_path / "pipeline.db")
    conn.execute(
        "INSERT INTO pipelines (pipeline_id, description, version, source_format) VALUES (?, ?, ?, ?)",
        ("transcript-ingest", "Transcript", "0.1.0", "text"),
    )
    conn.execute(
        "INSERT INTO sources (source_id, pipeline_id, file_path, status) VALUES (?, ?, ?, ?)",
        ("source-1", "transcript-ingest", "source", "processed"),
    )
    conn.execute(
        "INSERT INTO artifacts (artifact_id, source_id, pipeline_id, artifact_type, content_path, pipeline_version) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "artifact-1",
            "source-1",
            "transcript-ingest",
            "vault_note",
            str(tmp_path / "accounts/acme/meetings/note.md"),
            "0.1.0",
        ),
    )
    conn.commit()
    note = tmp_path / "accounts/acme/meetings/note.md"
    note.parent.mkdir(parents=True)
    note.write_text(_original_note(), encoding="utf-8")
    note.chmod(0o600)
    yield conn
    conn.close()


def _intent(root: Path) -> PreparedReprocess:
    return PreparedReprocess(
        schema_version=1,
        artifact_id="artifact-1",
        source_id="source-1",
        artifact_type="vault_note",
        recorded_artifact_path=str(root / "accounts/acme/meetings/note.md"),
        workspace_root=str(root),
        relative_path="accounts/acme/meetings/note.md",
        from_version="0.1.0",
        to_version="0.2.0",
        original_sha256=hashlib.sha256(_original_note().encode("utf-8")).hexdigest(),
        mode=0o600,
        note_content="---\nsource_id: source-1\npipeline: transcript-ingest\npipeline_version: 0.2.0\n---\nReplacement\n",
    )


@pytest.mark.parametrize("stage", ["stage1_clean", "stage2_extract"])
@pytest.mark.parametrize("category,expected", [("auth", 2), ("rate-limit", 1), ("general", 3)])
def test_reprocess_provider_failure_preserves_original_artifact(
    connection: sqlite3.Connection,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    stage: str,
    category: LLMErrorCategory,
    expected: int,
) -> None:
    from fieldkit.__main__ import main
    from fieldkit.ingest.docs import GeminiDocContent
    from fieldkit.ingest.pipeline import Stage1Result, TranscriptMeta
    from fieldkit.ingest.router import Confidence, RouteResult

    failure = LLMError("provider-payload-sentinel", category)
    failure.__cause__ = RuntimeError("provider-cause-sentinel")
    service = MagicMock()
    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.config.get_fieldkit_data", return_value=tmp_path / "runtime"),
        patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=service),
        patch(
            "fieldkit.ingest.docs.fetch_gemini_doc", return_value=GeminiDocContent("source-1", "Note", "Body", "Body")
        ),
        patch("fieldkit.ingest.router.route_by_domains", return_value=RouteResult(["acme"], Confidence.NONE, False)),
        patch("fieldkit.ingest.pipeline.stage1_clean", return_value=Stage1Result("Cleaned")) as clean,
        patch("fieldkit.ingest.pipeline.stage2_extract", return_value=TranscriptMeta()) as extract,
    ):
        (clean if stage == "stage1_clean" else extract).side_effect = failure
        result = main(["ingest", "reprocess", "--pipeline", "transcript-ingest", "--from-version", "0.1.0"])
    assert result == expected
    assert connection.execute("SELECT pipeline_version FROM artifacts").fetchone()[0] == "0.1.0"
    assert connection.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 0
    note = tmp_path / "accounts/acme/meetings/note.md"
    assert note.read_text(encoding="utf-8") == _original_note()
    assert note.stat().st_mode & 0o777 == 0o600
    service.close.assert_called_once()
    captured = capsys.readouterr()
    assert "provider-payload-sentinel" not in captured.out + captured.err + caplog.text
    assert "provider-cause-sentinel" not in captured.out + captured.err + caplog.text


def test_reprocess_journal_commits_exact_intent(connection: sqlite3.Connection, tmp_path: Path) -> None:
    intent = _intent(tmp_path)
    assert save_reprocess(connection, intent) == intent
    assert not connection.in_transaction
    assert load_reprocess(connection, intent.artifact_id) == intent
    assert save_reprocess(connection, intent) == intent
    assert connection.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 1
    assert connection.execute("SELECT pipeline_version FROM artifacts").fetchone()[0] == "0.1.0"


@pytest.mark.parametrize("conflict", [None, "content", "mode", "duplicate", "identity"])
def test_prepare_reprocess_saves_before_publication(
    connection: sqlite3.Connection, tmp_path: Path, conflict: str | None
) -> None:
    from fieldkit.ingest.db import get_artifacts_for_reprocess
    from fieldkit.ingest.reprocess_note import capture_reprocess_note
    from fieldkit.ingest.reprocess_replay import prepare_reprocess

    artifact = get_artifacts_for_reprocess(connection, "transcript-ingest")[0]
    note = capture_reprocess_note(artifact, tmp_path)
    expected = _intent(tmp_path)
    if conflict == "content":
        note.path.write_text(_original_note() + "Edited\n", encoding="utf-8")
    elif conflict == "mode":
        note.path.chmod(0o644)
    elif conflict == "identity":
        replacement = tmp_path / "replacement.md"
        replacement.write_bytes(note.path.read_bytes())
        replacement.chmod(0o600)
        replacement.replace(note.path)
    elif conflict == "duplicate":
        connection.execute(
            "INSERT INTO artifacts (artifact_id, source_id, pipeline_id, artifact_type, content_path) "
            "VALUES (?, ?, ?, ?, ?)",
            ("duplicate", "source-1", "transcript-ingest", "vault_note", artifact.content_path),
        )
        connection.commit()
    before = note.path.read_bytes()
    if conflict is None:
        result = prepare_reprocess(
            connection,
            artifact=artifact,
            note=note,
            content=expected.note_content,
            to_version="0.2.0",
            runtime_root=tmp_path / "runtime",
        )
        assert result == expected
        assert load_reprocess(connection, artifact.artifact_id) == expected
    else:
        with pytest.raises(ValueError, match=r"changed|Ambiguous"):
            prepare_reprocess(
                connection,
                artifact=artifact,
                note=note,
                content=expected.note_content,
                to_version="0.2.0",
                runtime_root=tmp_path / "runtime",
            )
        assert load_reprocess(connection, artifact.artifact_id) is None
    assert note.path.read_bytes() == before
    assert (
        connection.execute(
            "SELECT pipeline_version FROM artifacts WHERE artifact_id = ?", (artifact.artifact_id,)
        ).fetchone()[0]
        == "0.1.0"
    )


def test_prepare_reprocess_holds_note_lock_during_save(connection: sqlite3.Connection, tmp_path: Path) -> None:
    from fieldkit.ingest.db import get_artifacts_for_reprocess
    from fieldkit.ingest.reprocess_note import capture_reprocess_note
    from fieldkit.ingest.reprocess_replay import prepare_reprocess
    from fieldkit.util.atomic import PathLockTimeoutError, exclusive_file_lock, prepare_runtime_lock_path

    artifact = get_artifacts_for_reprocess(connection, "transcript-ingest")[0]
    note = capture_reprocess_note(artifact, tmp_path)
    expected = _intent(tmp_path)
    runtime = tmp_path / "runtime"
    lock = prepare_runtime_lock_path(note.path, runtime, "meeting-note")

    def save_while_locked(conn: sqlite3.Connection, intent: PreparedReprocess) -> PreparedReprocess:
        with pytest.raises(PathLockTimeoutError), exclusive_file_lock(lock, timeout_seconds=0):
            pytest.fail("Preparation released the note lock before journal save")
        return save_reprocess(conn, intent)

    with patch("fieldkit.ingest.reprocess_replay.save_reprocess", side_effect=save_while_locked):
        result = prepare_reprocess(
            connection,
            artifact=artifact,
            note=note,
            content=expected.note_content,
            to_version="0.2.0",
            runtime_root=runtime,
        )
    assert result == expected
    assert load_reprocess(connection, artifact.artifact_id) == expected


def test_fresh_reprocess_retains_recovery_after_completion_failure(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from fieldkit.commands.ingest.reprocess import _reprocess_one_artifact, _reprocess_transcript_ingest
    from fieldkit.ingest.db import get_artifacts_for_reprocess
    from fieldkit.ingest.docs import GeminiDocContent
    from fieldkit.ingest.pipeline import Stage1Result, TranscriptMeta
    from fieldkit.ingest.router import Confidence, RouteResult

    artifact = get_artifacts_for_reprocess(connection, "transcript-ingest")[0]
    expected = _intent(tmp_path)
    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.config.get_fieldkit_data", return_value=tmp_path / "runtime"),
        patch(
            "fieldkit.ingest.docs.fetch_gemini_doc", return_value=GeminiDocContent("source-1", "Note", "Body", "Body")
        ),
        patch("fieldkit.ingest.router.route_by_domains", return_value=RouteResult(["acme"], Confidence.NONE, False)),
        patch("fieldkit.ingest.pipeline.stage1_clean", return_value=Stage1Result("Cleaned")),
        patch("fieldkit.ingest.pipeline.stage2_extract", return_value=TranscriptMeta()),
        patch("fieldkit.ingest.pipeline.render_vault_note", return_value=expected.note_content),
        patch(
            "fieldkit.ingest.reprocess_replay.complete_reprocess",
            side_effect=sqlite3.OperationalError("synthetic failure"),
        ),
    ):
        result = _reprocess_one_artifact(art=artifact, service=object(), conn=connection, pipeline_version="0.2.0")
    assert result is False
    assert load_reprocess(connection, artifact.artifact_id) == expected
    assert expected.path.read_text(encoding="utf-8") == expected.note_content
    assert connection.execute("SELECT pipeline_version FROM artifacts").fetchone()[0] == "0.1.0"
    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
        patch("fieldkit.config.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.config.get_fieldkit_data", return_value=tmp_path / "runtime"),
        patch("fieldkit.commands.ingest.reprocess.require_optional_profile") as profile,
        patch("fieldkit.ingest.docs.get_docs_service") as service,
        patch("fieldkit.ingest.reprocess_replay.atomic_text_write") as write,
    ):
        recovered = _reprocess_transcript_ingest(
            spec=SimpleNamespace(version="0.2.0"),
            from_version="0.1.0",
            dry_run=False,
            interactive=False,
            limit=None,
        )
    assert recovered == 0
    profile.assert_not_called()
    service.assert_not_called()
    write.assert_not_called()
    assert load_reprocess(connection, artifact.artifact_id) is None
    assert connection.execute("SELECT pipeline_version FROM artifacts").fetchone()[0] == "0.2.0"


def test_new_recovery_stops_before_second_fresh_artifact(connection: sqlite3.Connection, tmp_path: Path) -> None:
    from fieldkit.commands.ingest.reprocess import _reprocess_transcript_ingest
    from fieldkit.ingest.docs import GeminiDocContent
    from fieldkit.ingest.pipeline import Stage1Result, TranscriptMeta
    from fieldkit.ingest.router import Confidence, RouteResult

    first = _intent(tmp_path)
    second = tmp_path / "accounts/acme/meetings/second.md"
    second_content = _original_note().replace("source-1", "source-2")
    second.write_text(second_content, encoding="utf-8")
    connection.execute(
        "INSERT INTO sources (source_id, pipeline_id, file_path, status) VALUES (?, ?, ?, ?)",
        ("source-2", "transcript-ingest", "second", "processed"),
    )
    connection.execute(
        "INSERT INTO artifacts (artifact_id, source_id, pipeline_id, artifact_type, content_path, pipeline_version, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("artifact-2", "source-2", "transcript-ingest", "vault_note", str(second), "0.1.0", "9999-01-01"),
    )
    connection.commit()
    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
        patch("fieldkit.config.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.config.get_fieldkit_data", return_value=tmp_path / "runtime"),
        patch("fieldkit.commands.ingest.reprocess.require_optional_profile"),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=MagicMock()),
        patch(
            "fieldkit.ingest.docs.fetch_gemini_doc", return_value=GeminiDocContent("source-1", "Note", "Body", "Body")
        ) as fetch,
        patch("fieldkit.ingest.router.route_by_domains", return_value=RouteResult(["acme"], Confidence.NONE, False)),
        patch("fieldkit.ingest.pipeline.stage1_clean", return_value=Stage1Result("Cleaned")),
        patch("fieldkit.ingest.pipeline.stage2_extract", return_value=TranscriptMeta()),
        patch("fieldkit.ingest.pipeline.render_vault_note", return_value=first.note_content),
        patch(
            "fieldkit.ingest.reprocess_replay.complete_reprocess",
            side_effect=sqlite3.OperationalError("synthetic failure"),
        ),
    ):
        result = _reprocess_transcript_ingest(
            spec=SimpleNamespace(version="0.2.0"),
            from_version="0.1.0",
            dry_run=False,
            interactive=False,
            limit=None,
        )
    assert result == 1
    fetch.assert_called_once()
    assert fetch.call_args.args[1] == "source-1"
    assert load_reprocess(connection, "artifact-1") == first
    assert first.path.read_text(encoding="utf-8") == first.note_content
    assert load_reprocess(connection, "artifact-2") is None
    assert second.read_text(encoding="utf-8") == second_content
    assert (
        connection.execute("SELECT pipeline_version FROM artifacts WHERE artifact_id = ?", ("artifact-2",)).fetchone()[
            0
        ]
        == "0.1.0"
    )


@pytest.mark.parametrize("dry_run", [False, True])
def test_reprocess_command_recovers_without_credentials(
    connection: sqlite3.Connection, tmp_path: Path, dry_run: bool
) -> None:
    from fieldkit.commands.ingest.reprocess import _reprocess_transcript_ingest

    intent = _intent(tmp_path)
    assert save_reprocess(connection, intent) == intent
    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
        patch("fieldkit.config.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.ingest.reprocess.require_optional_profile") as profile,
        patch("fieldkit.ingest.docs.get_docs_service") as service,
        patch("fieldkit.commands.ingest.reprocess._reprocess_one_artifact") as fresh,
    ):
        result = _reprocess_transcript_ingest(
            spec=SimpleNamespace(version="0.2.0"),
            from_version="0.1.0",
            dry_run=dry_run,
            interactive=False,
            limit=None,
        )
    assert result == (1 if dry_run else 0)
    profile.assert_not_called()
    service.assert_not_called()
    fresh.assert_not_called()
    assert intent.path.read_text(encoding="utf-8") == (_original_note() if dry_run else intent.note_content)
    assert load_reprocess(connection, intent.artifact_id) == (intent if dry_run else None)


@pytest.mark.parametrize("excluded_by", ["version", "account", "limit", "missing_version"])
def test_reprocess_excluded_recovery_blocks_fresh_work(
    connection: sqlite3.Connection, tmp_path: Path, excluded_by: str
) -> None:
    from fieldkit.commands.ingest.reprocess import _reprocess_transcript_ingest

    intent = _intent(tmp_path)
    assert save_reprocess(connection, intent) == intent
    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
        patch("fieldkit.config.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.ingest.reprocess.require_optional_profile") as profile,
        patch("fieldkit.ingest.docs.get_docs_service") as service,
        patch("fieldkit.commands.ingest.reprocess._reprocess_one_artifact") as fresh,
    ):
        result = _reprocess_transcript_ingest(
            spec=SimpleNamespace(version="0.2.0"),
            from_version=None if excluded_by == "missing_version" else "9.9" if excluded_by == "version" else "0.1.0",
            account="other" if excluded_by == "account" else None,
            limit=0 if excluded_by == "limit" else None,
            dry_run=False,
            interactive=False,
        )
    assert result == 1
    profile.assert_not_called()
    service.assert_not_called()
    fresh.assert_not_called()
    assert load_reprocess(connection, intent.artifact_id) == intent
    assert intent.path.read_text(encoding="utf-8") == _original_note()


@pytest.mark.parametrize("outcome", ["failure", "skip", "quit"])
def test_incomplete_recovery_blocks_following_fresh_artifact(
    connection: sqlite3.Connection, tmp_path: Path, outcome: str
) -> None:
    from fieldkit.commands.ingest.reprocess import _reprocess_transcript_ingest

    intent = _intent(tmp_path)
    assert save_reprocess(connection, intent) == intent
    connection.execute(
        "INSERT INTO artifacts (artifact_id, source_id, pipeline_id, artifact_type, content_path, pipeline_version) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "fresh",
            "source-1",
            "transcript-ingest",
            "vault_note",
            str(tmp_path / "accounts/acme/meetings/fresh.md"),
            "0.1.0",
        ),
    )
    connection.commit()
    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
        patch("fieldkit.config.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.commands.ingest.reprocess.require_optional_profile") as profile,
        patch("fieldkit.ingest.docs.get_docs_service") as service,
        patch("fieldkit.commands.ingest.reprocess._reprocess_one_artifact") as fresh,
        patch("fieldkit.ingest.reprocess_replay.replay_reprocess", side_effect=OSError("synthetic failure")),
        patch(
            "fieldkit.commands.ingest.reprocess._prompt_reprocess_choice",
            return_value="n" if outcome == "skip" else "q",
        ),
    ):
        result = _reprocess_transcript_ingest(
            spec=SimpleNamespace(version="0.2.0"),
            from_version="0.1.0",
            limit=None,
            dry_run=False,
            interactive=outcome != "failure",
        )
    assert result == 1
    profile.assert_not_called()
    service.assert_not_called()
    fresh.assert_not_called()
    assert load_reprocess(connection, intent.artifact_id) == intent


def test_reprocess_command_recovers_recorded_version_without_repreparing(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from fieldkit.commands.ingest.reprocess import _reprocess_transcript_ingest

    intent = _intent(tmp_path)
    assert save_reprocess(connection, intent) == intent
    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
        patch("fieldkit.config.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.ingest.docs.get_docs_service") as service,
        patch("fieldkit.commands.ingest.reprocess._reprocess_one_artifact") as fresh,
    ):
        result = _reprocess_transcript_ingest(
            spec=SimpleNamespace(version="0.3.0"),
            from_version="0.1.0",
            dry_run=False,
            interactive=False,
            limit=None,
        )
    assert result == 1
    service.assert_not_called()
    fresh.assert_not_called()
    assert load_reprocess(connection, intent.artifact_id) is None
    assert connection.execute("SELECT pipeline_version FROM artifacts").fetchone()[0] == "0.2.0"


def test_reprocess_preflight_is_read_only(connection: sqlite3.Connection, tmp_path: Path) -> None:
    from fieldkit.ingest.reprocess_replay import preflight_reprocess

    intent = _intent(tmp_path)
    assert save_reprocess(connection, intent) == intent
    result = preflight_reprocess(connection, tmp_path)
    assert result == (intent,)
    assert intent.path.read_text(encoding="utf-8") == _original_note()
    assert load_reprocess(connection, intent.artifact_id) == intent
    assert connection.execute("SELECT pipeline_version FROM artifacts").fetchone()[0] == "0.1.0"


def test_reprocess_preflight_rejects_later_invalid_journal_before_any_write(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    from fieldkit.ingest.reprocess_replay import preflight_reprocess

    intent = _intent(tmp_path)
    assert save_reprocess(connection, intent) == intent
    connection.execute(
        "INSERT INTO checkpoints (checkpoint_id, pipeline_id, key, value) VALUES (?, ?, ?, ?)",
        ("invalid", "transcript-ingest", "prepared-reprocess:v9:unknown", "{}"),
    )
    connection.commit()
    with (
        patch("fieldkit.ingest.reprocess_replay.atomic_text_write") as write,
        pytest.raises(ValueError, match="Invalid prepared reprocess"),
    ):
        preflight_reprocess(connection, tmp_path)
    write.assert_not_called()
    assert intent.path.read_text(encoding="utf-8") == _original_note()
    assert load_reprocess(connection, intent.artifact_id) == intent


@pytest.mark.parametrize("alias", [False, True])
def test_reprocess_preflight_rejects_duplicate_artifact_target(
    connection: sqlite3.Connection, tmp_path: Path, alias: bool
) -> None:
    from fieldkit.ingest.reprocess_replay import preflight_reprocess

    intent = _intent(tmp_path)
    assert save_reprocess(connection, intent) == intent
    path = intent.path
    if alias:
        link = tmp_path / "alias"
        link.symlink_to(tmp_path, target_is_directory=True)
        path = link / intent.relative_path
    connection.execute(
        "INSERT INTO artifacts (artifact_id, source_id, pipeline_id, artifact_type, content_path, pipeline_version) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("other-artifact", "source-1", "transcript-ingest", "vault_note", str(path), "0.1.0"),
    )
    connection.commit()
    with pytest.raises(ValueError, match="Ambiguous"):
        preflight_reprocess(connection, tmp_path)
    assert intent.path.read_text(encoding="utf-8") == _original_note()
    assert load_reprocess(connection, intent.artifact_id) == intent


@pytest.mark.parametrize("bound", ["MAX_REPROCESS_RECORDS", "MAX_REPROCESS_BATCH_BYTES"])
def test_reprocess_preflight_bounds_retained_batch(connection: sqlite3.Connection, tmp_path: Path, bound: str) -> None:
    from fieldkit.ingest.reprocess_replay import preflight_reprocess

    intent = _intent(tmp_path)
    assert save_reprocess(connection, intent) == intent
    with patch("fieldkit.ingest.reprocess_journal." + bound, 0), pytest.raises(ValueError, match="bound"):
        preflight_reprocess(connection, tmp_path)
    assert intent.path.read_text(encoding="utf-8") == _original_note()


def test_reprocess_replay_applies_then_completes(connection: sqlite3.Connection, tmp_path: Path) -> None:
    from fieldkit.ingest.reprocess_replay import replay_reprocess

    intent = _intent(tmp_path)
    assert save_reprocess(connection, intent) == intent
    result = replay_reprocess(connection, intent.artifact_id, tmp_path, runtime_root=tmp_path / "runtime")
    assert result == Path(intent.recorded_artifact_path)
    assert result.read_text(encoding="utf-8") == intent.note_content
    assert result.stat().st_mode & 0o777 == intent.mode
    assert load_reprocess(connection, intent.artifact_id) is None
    assert connection.execute("SELECT pipeline_version FROM artifacts").fetchone()[0] == "0.2.0"


def test_reprocess_replay_recovers_after_file_before_database(connection: sqlite3.Connection, tmp_path: Path) -> None:
    from fieldkit.ingest.reprocess_replay import replay_reprocess

    intent = _intent(tmp_path)
    assert save_reprocess(connection, intent) == intent
    with (
        patch(
            "fieldkit.ingest.reprocess_replay.complete_reprocess", side_effect=RuntimeError("synthetic interruption")
        ),
        pytest.raises(RuntimeError, match="synthetic interruption"),
    ):
        replay_reprocess(connection, intent.artifact_id, tmp_path, runtime_root=tmp_path / "runtime")
    assert Path(intent.recorded_artifact_path).read_text(encoding="utf-8") == intent.note_content
    assert load_reprocess(connection, intent.artifact_id) == intent
    assert connection.execute("SELECT pipeline_version FROM artifacts").fetchone()[0] == "0.1.0"
    with patch("fieldkit.ingest.reprocess_replay.atomic_text_write") as write:
        result = replay_reprocess(connection, intent.artifact_id, tmp_path, runtime_root=tmp_path / "runtime")
    assert result == Path(intent.recorded_artifact_path)
    write.assert_not_called()
    assert load_reprocess(connection, intent.artifact_id) is None
    assert connection.execute("SELECT pipeline_version FROM artifacts").fetchone()[0] == "0.2.0"


def test_reprocess_replay_holds_note_lock_through_completion(connection: sqlite3.Connection, tmp_path: Path) -> None:
    from fieldkit.ingest.reprocess_replay import replay_reprocess
    from fieldkit.util.atomic import PathLockTimeoutError, exclusive_file_lock, prepare_runtime_lock_path

    intent = _intent(tmp_path)
    assert save_reprocess(connection, intent) == intent
    runtime = tmp_path / "runtime"
    lock = prepare_runtime_lock_path(Path(intent.recorded_artifact_path), runtime, "meeting-note")

    def finish(conn: sqlite3.Connection, retained: PreparedReprocess) -> None:
        with pytest.raises(PathLockTimeoutError), exclusive_file_lock(lock, timeout_seconds=0):
            pytest.fail("Note lock released before database completion")
        complete_reprocess(conn, retained)

    with patch("fieldkit.ingest.reprocess_replay.complete_reprocess", side_effect=finish):
        result = replay_reprocess(connection, intent.artifact_id, tmp_path, runtime_root=runtime)
    assert result == Path(intent.recorded_artifact_path)
    assert load_reprocess(connection, intent.artifact_id) is None


@pytest.mark.parametrize("mutation", ["edit", "mode", "root", "missing", "artifact"])
def test_reprocess_replay_refuses_changed_state(connection: sqlite3.Connection, tmp_path: Path, mutation: str) -> None:
    from fieldkit.ingest.reprocess_replay import replay_reprocess

    intent = _intent(tmp_path)
    assert save_reprocess(connection, intent) == intent
    path = Path(intent.recorded_artifact_path)
    workspace = tmp_path
    if mutation == "edit":
        path.write_text(_original_note().replace("Original", "User edit"), encoding="utf-8")
    elif mutation == "mode":
        path.chmod(0o640)
    elif mutation == "root":
        workspace = tmp_path / "other"
        workspace.mkdir()
    elif mutation == "missing":
        path.unlink()
    else:
        connection.execute("UPDATE artifacts SET pipeline_version = '0.3.0'")
        connection.commit()
    before = path.read_bytes() if path.exists() else None
    with pytest.raises((ValueError, FileNotFoundError)):
        replay_reprocess(connection, intent.artifact_id, workspace, runtime_root=tmp_path / "runtime")
    assert (path.read_bytes() if path.exists() else None) == before
    assert load_reprocess(connection, intent.artifact_id) == intent


def test_reprocess_journal_refuses_changed_decisions(connection: sqlite3.Connection, tmp_path: Path) -> None:
    intent = _intent(tmp_path)
    assert save_reprocess(connection, intent) == intent
    changed = intent.model_copy(update={"note_content": intent.note_content.replace("Replacement", "Different")})
    with pytest.raises(ValueError, match="Conflicting"):
        save_reprocess(connection, changed)
    assert load_reprocess(connection, intent.artifact_id) == intent


def test_reprocess_completion_is_atomic(connection: sqlite3.Connection, tmp_path: Path) -> None:
    intent = _intent(tmp_path)
    assert save_reprocess(connection, intent) == intent
    assert complete_reprocess(connection, intent) is None
    assert load_reprocess(connection, intent.artifact_id) is None
    assert connection.execute("SELECT pipeline_version FROM artifacts").fetchone()[0] == "0.2.0"
    assert not connection.in_transaction


def test_reprocess_completion_rolls_back_when_journal_delete_fails(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    intent = _intent(tmp_path)
    assert save_reprocess(connection, intent) == intent
    connection.execute(
        "CREATE TRIGGER refuse_journal_delete BEFORE DELETE ON checkpoints BEGIN SELECT RAISE(ABORT, 'synthetic'); END"
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="synthetic"):
        complete_reprocess(connection, intent)
    assert load_reprocess(connection, intent.artifact_id) == intent
    assert connection.execute("SELECT pipeline_version FROM artifacts").fetchone()[0] == "0.1.0"
    assert not connection.in_transaction


def test_reprocess_completion_refuses_changed_artifact(connection: sqlite3.Connection, tmp_path: Path) -> None:
    intent = _intent(tmp_path)
    assert save_reprocess(connection, intent) == intent
    connection.execute("UPDATE artifacts SET pipeline_version = '0.3.0'")
    connection.commit()
    with pytest.raises(ValueError, match="artifact"):
        complete_reprocess(connection, intent)
    assert load_reprocess(connection, intent.artifact_id) == intent
    assert connection.execute("SELECT pipeline_version FROM artifacts").fetchone()[0] == "0.3.0"


@pytest.mark.parametrize(
    "field,value",
    [("source_id", "other"), ("pipeline_version", "0.0.0"), ("content_path", "elsewhere"), ("pipeline_id", "other")],
)
def test_reprocess_journal_refuses_stale_artifact(
    connection: sqlite3.Connection,
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    if field == "source_id":
        connection.execute(
            "INSERT INTO sources (source_id, pipeline_id, file_path, status) VALUES (?, ?, ?, ?)",
            ("other", "transcript-ingest", "other", "processed"),
        )
    elif field == "pipeline_id":
        connection.execute(
            "INSERT INTO pipelines (pipeline_id, description, version, source_format) VALUES (?, ?, ?, ?)",
            ("other", "Other", "0.1.0", "text"),
        )
    # Field names are the fixed parameterized cases, never external input.
    connection.execute(f"UPDATE artifacts SET {field} = ?", (value,))
    connection.commit()
    with pytest.raises(ValueError, match="artifact"):
        save_reprocess(connection, _intent(tmp_path))
    assert connection.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 0


def test_reprocess_journal_does_not_commit_caller_transaction(connection: sqlite3.Connection, tmp_path: Path) -> None:
    connection.execute("UPDATE sources SET status = 'pending'")
    with pytest.raises(ValueError, match="idle"):
        save_reprocess(connection, _intent(tmp_path))
    assert connection.in_transaction
    connection.rollback()
    assert connection.execute("SELECT status FROM sources").fetchone()[0] == "processed"


def test_reprocess_journal_revalidates_forged_model(connection: sqlite3.Connection, tmp_path: Path) -> None:
    forged = _intent(tmp_path).model_copy(update={"note_content": "SENSITIVE INVALID NOTE"})
    with pytest.raises(ValueError, match=r"^Invalid prepared reprocess output$"):
        save_reprocess(connection, forged)
    assert not connection.in_transaction
    assert connection.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 0


def test_reprocess_journal_refuses_uncompleted_source(connection: sqlite3.Connection, tmp_path: Path) -> None:
    connection.execute("UPDATE sources SET status = 'in_progress'")
    connection.commit()
    with pytest.raises(ValueError, match="artifact"):
        save_reprocess(connection, _intent(tmp_path))
    assert connection.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 0


@pytest.mark.parametrize("mutation", ["outside", "dot", "dot_dot", "repeated_separator", "null_root", "null_recorded"])
def test_reprocess_journal_rejects_split_path_identity(tmp_path: Path, mutation: str) -> None:
    data = _intent(tmp_path).model_dump()
    if mutation == "outside":
        data["recorded_artifact_path"] = str(tmp_path / "outside.md")
    elif mutation == "dot":
        data["workspace_root"] = str(tmp_path) + "/."
    elif mutation == "dot_dot":
        data["workspace_root"] = str(tmp_path) + "/other/.."
    elif mutation == "null_root":
        data["workspace_root"] = str(tmp_path) + "\x00"
    elif mutation == "null_recorded":
        data["recorded_artifact_path"] = str(tmp_path) + "\x00/accounts/acme/meetings/note.md"
    else:
        data["recorded_artifact_path"] = str(tmp_path) + "//accounts/acme/meetings/note.md"
    with pytest.raises(ValueError, match=r"^Invalid prepared reprocess output$"):
        decode_reprocess(json.dumps(data))


def test_reprocess_journal_refuses_noncanonical_retained_json(connection: sqlite3.Connection, tmp_path: Path) -> None:
    intent = _intent(tmp_path)
    assert save_reprocess(connection, intent) == intent
    modified = json.dumps(intent.model_dump(), sort_keys=True, indent=2)
    connection.execute("UPDATE checkpoints SET value = ?", (modified,))
    connection.commit()
    with pytest.raises(ValueError, match="Noncanonical"):
        save_reprocess(connection, intent)
    with pytest.raises(ValueError, match="Noncanonical"):
        complete_reprocess(connection, intent)
    assert connection.execute("SELECT value FROM checkpoints").fetchone()[0] == modified
    assert connection.execute("SELECT pipeline_version FROM artifacts").fetchone()[0] == "0.1.0"


def test_reprocess_journal_refuses_unrelated_recorded_root(connection: sqlite3.Connection, tmp_path: Path) -> None:
    intent = _intent(tmp_path).model_copy(update={"workspace_root": str(tmp_path / "other")})
    (tmp_path / "other").mkdir()
    with pytest.raises(ValueError, match="workspace"):
        save_reprocess(connection, intent)
    assert connection.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 0


def test_reprocess_journal_and_replay_preserve_root_alias(connection: sqlite3.Connection, tmp_path: Path) -> None:
    from fieldkit.ingest.reprocess_replay import replay_reprocess

    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    intent = _intent(tmp_path).model_copy(
        update={"recorded_artifact_path": str(alias / "accounts/acme/meetings/note.md")}
    )
    connection.execute("UPDATE artifacts SET content_path = ?", (intent.recorded_artifact_path,))
    connection.commit()
    assert save_reprocess(connection, intent) == intent
    result = replay_reprocess(connection, intent.artifact_id, alias, runtime_root=tmp_path / "runtime")
    assert result == intent.path
    assert result.read_text(encoding="utf-8") == intent.note_content
    assert connection.execute("SELECT content_path FROM artifacts").fetchone()[0] == intent.recorded_artifact_path


def test_reprocess_journal_refuses_descendant_alias(connection: sqlite3.Connection, tmp_path: Path) -> None:
    intent = _intent(tmp_path)
    parent = intent.path.parent
    moved = tmp_path / "moved"
    parent.rename(moved)
    parent.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ValueError, match="redirects"):
        save_reprocess(connection, intent)
    assert connection.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 0


def test_reprocess_journal_binds_checkpoint_identity(connection: sqlite3.Connection, tmp_path: Path) -> None:
    intent = _intent(tmp_path)
    assert save_reprocess(connection, intent) == intent
    connection.execute("UPDATE checkpoints SET key = ?", (reprocess_checkpoint_key("other-artifact"),))
    connection.commit()
    with pytest.raises(ValueError, match="identity"):
        load_reprocess(connection, "other-artifact")


def test_reprocess_journal_load_bounds_before_decode(connection: sqlite3.Connection, tmp_path: Path) -> None:
    intent = _intent(tmp_path)
    assert save_reprocess(connection, intent) == intent
    with (
        patch("fieldkit.ingest.reprocess_journal.MAX_REPROCESS_BYTES", 32),
        patch("fieldkit.ingest.reprocess_journal.decode_reprocess") as decode,
        pytest.raises(ValueError, match="Invalid prepared"),
    ):
        load_reprocess(connection, intent.artifact_id)
    decode.assert_not_called()


@pytest.mark.parametrize(
    "mutation",
    ["duplicate", "bool_version", "unknown", "traversal", "wrong_source", "wrong_version", "reserved_marker"],
)
def test_reprocess_journal_decoder_fails_closed(tmp_path: Path, mutation: str) -> None:
    raw = _intent(tmp_path).model_dump_json()
    data = json.loads(raw)
    if mutation == "duplicate":
        raw = raw.replace('"schema_version":1', '"schema_version":1,"schema_version":1')
    else:
        if mutation == "bool_version":
            data["schema_version"] = True
        elif mutation == "unknown":
            data["unexpected"] = "private payload"
        elif mutation == "traversal":
            data["relative_path"] = "accounts/acme/meetings/../note.md"
        elif mutation == "wrong_source":
            data["note_content"] = data["note_content"].replace("source-1", "source-2")
        elif mutation == "wrong_version":
            data["note_content"] = data["note_content"].replace("0.2.0", "0.3.0")
        else:
            data["note_content"] += "fieldkit-ingest-note:"
        raw = json.dumps(data)
    with pytest.raises(ValueError, match=r"^Invalid prepared reprocess output$"):
        decode_reprocess(raw)
