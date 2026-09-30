"""Prepared intent commits durably once, without replacing prior decisions."""

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.commands.ingest.run import _process_one_source, _run_processing_loop, _run_transcript_ingest
from fieldkit.errors import LLMError, LLMErrorCategory
from fieldkit.ingest import prepared as store
from fieldkit.ingest.db import init_db
from fieldkit.ingest.docs import GeminiDocContent
from fieldkit.ingest.paths import compute_vault_path
from fieldkit.ingest.pipeline import TranscriptMeta
from fieldkit.ingest.preparation import _CleanResult
from fieldkit.ingest.prepared import ReplayIntent
from fieldkit.ingest.replay import recover_interrupted_sources, replay_prepared
from fieldkit.ingest.router import Confidence, RouteResult
from fieldkit.ingest.sources import SourceRecord
from fieldkit.util.atomic import exclusive_path_lock

pytestmark = pytest.mark.unit
_CRASH_PROBE_TIMEOUT_SECONDS = 15

_CRASH_PROBE = """
import os
import signal
import sys
from pathlib import Path
from unittest.mock import patch
from fieldkit.ingest.db import get_db
from fieldkit.ingest import replay
from fieldkit.ingest import note_effect
from fieldkit.ingest import task_effect
from fieldkit.pursuit import io

root = Path(sys.argv[1])
boundary = sys.argv[2]
io.get_fieldkit_data = lambda: root / 'runtime'
note_effect.get_fieldkit_data = lambda: root / 'runtime'
task_effect.get_fieldkit_data = lambda: root / 'runtime'
function_name, occurrence = boundary.split(':')
original = getattr(replay, function_name)
calls = 0
def kill_after(*args, **kwargs):
    global calls
    if occurrence == 'before':
        os.kill(os.getpid(), signal.SIGKILL)
    result = original(*args, **kwargs)
    calls += 1
    if calls == int(occurrence):
        os.kill(os.getpid(), signal.SIGKILL)
    return result
with patch.object(replay, function_name, kill_after):
    replay.replay_prepared(get_db(root / 'pipeline.db'), 'source-1', root)
raise AssertionError('Crash boundary was not reached')
"""


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = init_db(tmp_path / "pipeline.db")
    conn.execute(
        "INSERT INTO pipelines (pipeline_id, description, version, source_format) VALUES (?, ?, ?, ?)",
        ("transcript-ingest", "Transcript", "0.1.0", "text"),
    )
    conn.execute(
        "INSERT INTO sources (source_id, pipeline_id, file_path, status) VALUES (?, ?, ?, ?)",
        ("source-1", "transcript-ingest", "source", "in_progress"),
    )
    conn.commit()
    yield conn
    conn.close()


def _prepared(title: str = "Meeting") -> store.PreparedMeeting:
    return store.decode_prepared(
        json.dumps(
            {
                "schema_version": 1,
                "pipeline_id": "transcript-ingest",
                "source_id": "source-1",
                "pipeline_version": "0.1.0",
                "vault_relative_path": compute_vault_path(
                    Path(), "acme", "2026-09-27", title, source_id="source-1"
                ).as_posix(),
                "note_content": "Meeting",
                "note_sha256": hashlib.sha256(b"Meeting").hexdigest(),
                "account": "acme",
                "meeting_date": "2026-09-27",
                "meeting_title": title,
                "pursuits": [],
                "action_items": [],
                "tasks": [],
                "degraded": False,
            }
        )
    )


@pytest.mark.parametrize("pursuit_count", [0, 1, 8])
def test_replay_payload_validation_and_hashing_do_not_scale_with_pursuits(
    connection: sqlite3.Connection, tmp_path: Path, pursuit_count: int
) -> None:
    slugs = tuple(f"project-{index}" for index in range(pursuit_count))
    prepared = _prepared().model_copy(update={"pursuits": slugs})
    assert store.save_prepared(connection, prepared) == prepared
    for slug in slugs:
        pursuit = tmp_path / "accounts/acme/pursuits" / f"{slug}.md"
        pursuit.parent.mkdir(parents=True, exist_ok=True)
        pursuit.write_text("---\nstage: qualify\n---\n\n## Activity Log\n", encoding="utf-8")
    with (
        patch("fieldkit.pursuit.io.get_fieldkit_data", return_value=tmp_path / "runtime"),
        patch.object(store, "decode_prepared", wraps=store.decode_prepared) as decode,
        patch.object(store, "prepared_digest", wraps=store.prepared_digest) as digest,
    ):
        result = replay_prepared(connection, prepared.source_id, tmp_path)
    assert result == tmp_path / prepared.vault_relative_path
    # Load, replay boundary, completion input, and retained-record recheck.
    assert decode.call_count == 4
    # One replay fingerprint and one independent completion fingerprint.
    assert digest.call_count == 2


def test_replay_intent_revalidates_copied_models_and_retains_canonical_instance() -> None:
    original = _prepared()
    with (
        patch.object(store, "decode_prepared", wraps=store.decode_prepared) as decode,
        patch.object(store, "prepared_digest", wraps=store.prepared_digest) as digest,
    ):
        intent = ReplayIntent(original)
    assert intent.prepared == original
    assert intent.prepared is not original
    assert intent.digest == store.prepared_digest(original)
    decode.assert_called_once()
    digest.assert_called_once_with(intent.prepared)
    invalid = original.model_copy(update={"source_id": "../invalid"})
    with pytest.raises(ValueError, match="Invalid prepared ingest output"):
        ReplayIntent(invalid)


def test_save_is_durable_and_identical_retry_does_not_write(connection: sqlite3.Connection, tmp_path: Path) -> None:
    prepared = _prepared()
    assert store.save_prepared(connection, prepared) == prepared
    changes = connection.total_changes
    assert store.save_prepared(connection, prepared) == prepared
    assert connection.total_changes == changes
    with closing(sqlite3.connect(tmp_path / "pipeline.db")) as reader:
        assert store.load_prepared(reader, "source-1") == prepared
        assert reader.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 1


def test_replay_commits_only_after_file_effects(connection: sqlite3.Connection, tmp_path: Path) -> None:
    prepared = _prepared()
    assert store.save_prepared(connection, prepared) == prepared
    path = replay_prepared(connection, "source-1", tmp_path)
    assert path == tmp_path / prepared.vault_relative_path
    assert "fieldkit-ingest-note:v1:" in path.read_text(encoding="utf-8")
    assert store.load_prepared(connection, "source-1") is None
    assert connection.execute("SELECT status FROM sources").fetchone()[0] == "processed"


def test_source_processor_replays_before_fetching(connection: sqlite3.Connection, tmp_path: Path) -> None:
    prepared = _prepared().model_copy(update={"degraded": True})
    assert store.save_prepared(connection, prepared) == prepared
    source = SourceRecord("source-1", "transcript-ingest", "", "Changed live title", None, "", "", "")
    with patch("fieldkit.commands.ingest.run._fetch_doc_for_run", side_effect=AssertionError("Must not fetch")):
        result = _process_one_source(
            src=source, service=object(), conn=connection, data_root=tmp_path, pipeline_version="new-version"
        )
    assert result.completed is True
    assert result.degraded is True
    assert connection.execute("SELECT pipeline_version FROM artifacts").fetchone()[0] == prepared.pipeline_version


def test_resumed_source_hides_private_replay_failure(
    connection: sqlite3.Connection, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared = _prepared()
    assert store.save_prepared(connection, prepared) == prepared
    source = SourceRecord("source-1", "transcript-ingest", "", "Meeting", None, "", "", "")
    with patch("fieldkit.commands.ingest.run.replay_prepared", side_effect=OSError("private-payload", str(tmp_path))):
        result = _process_one_source(
            src=source, service=None, conn=connection, data_root=tmp_path, pipeline_version="0.1.0"
        )
    assert result.completed is False
    diagnostic = capsys.readouterr().err
    assert "Required meeting writeback failed" in diagnostic
    assert "private-payload" not in diagnostic
    assert str(tmp_path) not in diagnostic
    assert store.load_prepared(connection, "source-1") == prepared


@pytest.mark.parametrize("rendered", ["No frontmatter", "---\n- item\n---\nNote", '---\ntitle: "bad\\q"\n---\nNote'])
def test_invalid_note_without_tasks_never_saves_intent(
    connection: sqlite3.Connection, tmp_path: Path, rendered: str
) -> None:
    source = SourceRecord("source-1", "transcript-ingest", "", "Meeting", None, "", "", "")
    doc = GeminiDocContent("source-1", "Meeting", "Notes", None)
    with (
        patch("fieldkit.commands.ingest.run._fetch_doc_for_run", return_value=doc),
        patch(
            "fieldkit.ingest.preparation._route_source", return_value=RouteResult(["unknown"], Confidence.HIGH, False)
        ),
        patch(
            "fieldkit.ingest.preparation.clean_and_extract_transcript",
            return_value=_CleanResult("Notes", TranscriptMeta(), False),
        ),
        patch("fieldkit.ingest.pipeline.render_vault_note", return_value=rendered),
        patch("fieldkit.ingest.preparation.classify_meeting_tasks", return_value=()) as classify,
        patch("fieldkit.commands.ingest.run.replay_prepared") as replay,
        pytest.raises(ValueError, match="Invalid meeting frontmatter"),
    ):
        _process_one_source(src=source, service=object(), conn=connection, data_root=tmp_path, pipeline_version="0.1.0")
    classify.assert_not_called()
    replay.assert_not_called()
    assert store.load_prepared(connection, "source-1") is None
    assert not (tmp_path / "accounts").exists()


def test_invalid_pursuit_intent_never_reaches_checkpoint(connection: sqlite3.Connection) -> None:
    prepared = _prepared().model_copy(
        update={
            "meeting_title": " ",
            "pursuits": ("project",),
            "vault_relative_path": compute_vault_path(
                Path(), "acme", "2026-09-27", " ", source_id="source-1"
            ).as_posix(),
        }
    )
    with pytest.raises(ValueError, match="Invalid prepared ingest output"):
        store.save_prepared(connection, prepared)
    assert connection.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
    assert connection.in_transaction is False


def test_new_source_commits_intent_before_output(connection: sqlite3.Connection, tmp_path: Path) -> None:
    source = SourceRecord("source-1", "transcript-ingest", "", "Meeting", None, "", "", "")
    doc = GeminiDocContent("source-1", "Meeting", "Notes", None)
    with (
        patch("fieldkit.commands.ingest.run._fetch_doc_for_run", return_value=doc),
        patch(
            "fieldkit.ingest.preparation._route_source", return_value=RouteResult(["unknown"], Confidence.HIGH, False)
        ),
        patch(
            "fieldkit.ingest.preparation.clean_and_extract_transcript",
            return_value=_CleanResult("Notes", TranscriptMeta(), False),
        ),
        patch("fieldkit.ingest.pipeline.render_vault_note", return_value="---\ntype: meeting\n---\nMeeting"),
        patch("fieldkit.ingest.preparation.classify_meeting_tasks", return_value=()),
        patch("fieldkit.commands.ingest.run.replay_prepared", side_effect=OSError("Interrupted before output")),
    ):
        result = _process_one_source(
            src=source, service=object(), conn=connection, data_root=tmp_path, pipeline_version="0.1.0"
        )
    assert result.completed is False
    retained = store.load_prepared(connection, "source-1")
    assert retained is not None
    assert retained.note_content == "---\ntype: meeting\n---\nMeeting"
    assert not (tmp_path / "accounts").exists()
    assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
    assert replay_prepared(connection, "source-1", tmp_path) == tmp_path / retained.vault_relative_path


@pytest.mark.parametrize("interactive", [False, True])
def test_replay_batch_needs_no_google_credentials(
    connection: sqlite3.Connection, tmp_path: Path, interactive: bool
) -> None:
    prepared = _prepared()
    assert store.save_prepared(connection, prepared) == prepared
    connection.execute("UPDATE sources SET status = 'pending'")
    connection.commit()
    source = SourceRecord("source-1", "transcript-ingest", "", "Meeting", None, "", "", "")
    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
        patch("fieldkit.commands.ingest.run._prompt_process_choice", return_value="y"),
        patch("fieldkit.ingest.docs.get_docs_service", side_effect=AssertionError("Must not authenticate")) as service,
        patch(
            "fieldkit.commands.ingest.run.require_optional_profile",
            side_effect=AssertionError("Must not require Google"),
        ) as profile,
    ):
        result = _run_processing_loop(
            [source], conn=connection, pipeline_version="new-version", interactive=interactive
        )
    assert result == 0
    service.assert_not_called()
    profile.assert_not_called()
    assert connection.execute("SELECT status FROM sources").fetchone()[0] == "processed"


@pytest.mark.parametrize("effect", ["publish_prepared_note", "publish_prepared_tasks"])
def test_replay_failure_retains_intent(connection: sqlite3.Connection, tmp_path: Path, effect: str) -> None:
    prepared = _prepared()
    assert store.save_prepared(connection, prepared) == prepared
    with (
        patch(f"fieldkit.ingest.replay.{effect}", side_effect=ValueError("Refused effect")),
        pytest.raises(ValueError, match="Refused effect"),
    ):
        replay_prepared(connection, "source-1", tmp_path)
    assert store.load_prepared(connection, "source-1") == prepared
    assert connection.execute("SELECT status FROM sources").fetchone()[0] == "in_progress"
    assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
    assert replay_prepared(connection, "source-1", tmp_path) == tmp_path / prepared.vault_relative_path


def test_replay_without_journal_has_no_file_effects(connection: sqlite3.Connection, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires retained intent"):
        replay_prepared(connection, "source-1", tmp_path)
    assert not (tmp_path / "accounts").exists()


@pytest.mark.parametrize("stage", ["stage1_clean", "stage2_extract"])
@pytest.mark.parametrize("category,expected", [("auth", 2), ("rate-limit", 1), ("general", 3)])
def test_command_provider_failure_does_not_publish(
    connection: sqlite3.Connection,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    stage: str,
    category: LLMErrorCategory,
    expected: int,
) -> None:
    from fieldkit.__main__ import main
    from fieldkit.ingest.pipeline import Stage1Result

    failure = LLMError("provider-payload-sentinel", category)
    failure.__cause__ = RuntimeError("provider-cause-sentinel")
    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
        patch("fieldkit.gmail.discover.get_gmail_db_path", return_value=tmp_path / "gmail.db"),
        patch("fieldkit.gmail.discover.scan_gemini_candidates", return_value=[]),
        patch("fieldkit.commands.ingest.run.require_optional_profile"),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=MagicMock()),
        patch(
            "fieldkit.commands.ingest.run._fetch_doc_for_run",
            return_value=GeminiDocContent("source-1", "Meeting", "Notes", None),
        ),
        patch(
            "fieldkit.ingest.preparation._route_source", return_value=RouteResult(["unknown"], Confidence.HIGH, False)
        ),
        patch("fieldkit.ingest.pipeline.stage1_clean", return_value=Stage1Result("cleaned")) as clean,
        patch("fieldkit.ingest.pipeline.stage2_extract", return_value=TranscriptMeta()) as extract,
    ):
        (clean if stage == "stage1_clean" else extract).side_effect = failure
        result = main(["ingest", "run", "--pipeline", "transcript-ingest"])
    assert result == expected
    assert connection.execute("SELECT status FROM sources").fetchone()[0] == "in_progress"
    assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
    assert store.load_prepared(connection, "source-1") is None
    assert not (tmp_path / "accounts").exists()
    captured = capsys.readouterr()
    assert "provider-payload-sentinel" not in captured.out + captured.err + caplog.text
    assert "provider-cause-sentinel" not in captured.out + captured.err + caplog.text


def test_command_retries_checkpointless_claim_after_credentials_are_repaired(
    connection: sqlite3.Connection, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from fieldkit.__main__ import main

    doc = GeminiDocContent("source-1", "Meeting", "Notes", None)
    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
        patch("fieldkit.gmail.discover.get_gmail_db_path", return_value=tmp_path / "gmail.db"),
        patch("fieldkit.gmail.discover.scan_gemini_candidates", return_value=[]),
        patch("fieldkit.commands.ingest.run.require_optional_profile"),
    ):
        with patch("fieldkit.config.get_google_token_path", return_value=tmp_path / "private-token-path"):
            failed = main(["ingest", "run", "--pipeline", "transcript-ingest"])
        assert failed == 2
        assert connection.execute("SELECT status FROM sources").fetchone()[0] == "in_progress"
        assert store.load_prepared(connection, "source-1") is None
        assert not (tmp_path / "accounts").exists()
        diagnostic = capsys.readouterr().err
        assert "private-token-path" not in diagnostic
        assert "fieldkit auth google" in diagnostic
        with (
            patch("fieldkit.ingest.docs.get_docs_service", return_value=MagicMock()),
            patch("fieldkit.commands.ingest.run._fetch_doc_for_run", return_value=doc),
            patch(
                "fieldkit.ingest.preparation._route_source",
                return_value=RouteResult(["unknown"], Confidence.HIGH, False),
            ),
            patch(
                "fieldkit.ingest.preparation.clean_and_extract_transcript",
                return_value=_CleanResult("Notes", TranscriptMeta(), False),
            ),
            patch("fieldkit.ingest.pipeline.render_vault_note", return_value="---\ntype: meeting\n---\nMeeting"),
            patch("fieldkit.ingest.preparation.classify_meeting_tasks", return_value=()),
        ):
            recovered = _run_transcript_ingest(
                spec=SimpleNamespace(version="0.1.0"), dry_run=False, limit=None, interactive=False
            )
    assert recovered == 0
    assert connection.execute("SELECT status FROM sources").fetchone()[0] == "processed"
    assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 1
    assert store.load_prepared(connection, "source-1") is None


@pytest.mark.parametrize("mode", ["recover", "dry_run", "busy"])
def test_command_recovers_real_interrupted_state_only_under_lock(
    connection: sqlite3.Connection, tmp_path: Path, mode: str
) -> None:
    prepared = _prepared()
    assert store.save_prepared(connection, prepared) == prepared
    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
        patch("fieldkit.gmail.discover.get_gmail_db_path", return_value=tmp_path / "gmail.db"),
        patch("fieldkit.gmail.discover.scan_gemini_candidates", return_value=[]),
        patch("fieldkit.ingest.docs.get_docs_service", side_effect=AssertionError("Must not authenticate")),
        patch(
            "fieldkit.commands.ingest.run.require_optional_profile",
            side_effect=AssertionError("Must not require Google"),
        ),
        exclusive_path_lock(tmp_path / "pipeline.db.transcript-run") if mode == "busy" else nullcontext(),
    ):
        result = _run_transcript_ingest(
            spec=SimpleNamespace(version="new-version"), dry_run=mode == "dry_run", limit=None, interactive=False
        )
    assert result == (1 if mode == "busy" else 0)
    status = connection.execute("SELECT status FROM sources").fetchone()[0]
    if mode == "recover":
        assert status == "processed"
        assert store.load_prepared(connection, "source-1") is None
        assert (tmp_path / prepared.vault_relative_path).is_file()
    else:
        assert status == "in_progress"
        assert store.load_prepared(connection, "source-1") == prepared
        assert not (tmp_path / "accounts").exists()


@pytest.mark.parametrize("journaled", [False, True])
def test_recovery_requeues_claim_without_changing_intent(connection: sqlite3.Connection, journaled: bool) -> None:
    prepared = _prepared()
    if journaled:
        assert store.save_prepared(connection, prepared) == prepared
    assert recover_interrupted_sources(connection) == 1
    assert connection.execute("SELECT status FROM sources").fetchone()[0] == "pending"
    assert store.load_prepared(connection, "source-1") == (prepared if journaled else None)
    assert recover_interrupted_sources(connection) == 0


def test_recovery_artifact_conflict_preserves_all_claims(connection: sqlite3.Connection) -> None:
    prepared = _prepared()
    assert store.save_prepared(connection, prepared) == prepared
    connection.execute(
        "INSERT INTO sources (source_id, pipeline_id, file_path, status) VALUES (?, ?, ?, ?)",
        ("source-2", "transcript-ingest", "source", "in_progress"),
    )
    connection.execute(
        "INSERT INTO artifacts (artifact_id, source_id, pipeline_id, artifact_type, content_path, pipeline_version) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("conflict", "source-2", "transcript-ingest", "vault_note", "note.md", "0.1.0"),
    )
    connection.commit()
    with pytest.raises(ValueError, match="conflicting completion evidence"):
        recover_interrupted_sources(connection)
    assert [tuple(row) for row in connection.execute("SELECT source_id, status FROM sources ORDER BY source_id")] == [
        ("source-1", "in_progress"),
        ("source-2", "in_progress"),
    ]
    assert store.load_prepared(connection, "source-1") == prepared
    assert connection.in_transaction is False


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL crash probe requires POSIX")
@pytest.mark.parametrize(
    "boundary",
    [
        "publish_prepared_note:before",
        "publish_prepared_note:1",
        "publish_prepared_pursuit:1",
        "publish_prepared_pursuit:2",
        "publish_prepared_tasks:1",
        "complete_prepared:before",
        "complete_prepared:1",
    ],
)
def test_killed_replay_converges_from_durable_boundary(
    connection: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    monkeypatch.setattr("fieldkit.pursuit.io.get_fieldkit_data", lambda: tmp_path / "runtime")
    task = store.PreparedTask(
        text="Send proposal", cls="my_task", owner="Alice", rationale="Assigned", pursuit_label="Acme"
    )
    prepared = _prepared().model_copy(
        update={
            "pursuits": ("first", "second"),
            "action_items": (task.text,),
            "tasks": (task,),
        }
    )
    assert store.save_prepared(connection, prepared) == prepared
    pursuits = tmp_path / "accounts/acme/pursuits"
    pursuits.mkdir(parents=True)
    for slug in prepared.pursuits:
        (pursuits / f"{slug}.md").write_text("---\nstage: qualify\n---\n\n## Activity Log\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-c", _CRASH_PROBE, str(tmp_path), boundary],
        cwd=tmp_path,
        env={
            "HOME": str(tmp_path),
            "PATH": os.defpath,
            "PYTHONPATH": str(Path(store.__file__).parents[2]),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=_CRASH_PROBE_TIMEOUT_SECONDS,
    )
    assert result.returncode == -signal.SIGKILL, result.stderr
    with closing(init_db(tmp_path / "pipeline.db")) as reopened:
        if boundary == "complete_prepared:1":
            assert store.load_prepared(reopened, "source-1") is None
        else:
            assert store.load_prepared(reopened, "source-1") == prepared
            assert reopened.execute("SELECT status FROM sources").fetchone()[0] == "in_progress"
        with (
            patch("fieldkit.config.get_fieldkit_home", return_value=tmp_path),
            patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
            patch("fieldkit.gmail.discover.get_gmail_db_path", return_value=tmp_path / "gmail.db"),
            patch("fieldkit.gmail.discover.scan_gemini_candidates", return_value=[]),
            patch(
                "fieldkit.ingest.docs.get_docs_service", side_effect=AssertionError("Must not authenticate")
            ) as service,
            patch(
                "fieldkit.ingest.preparation.classify_meeting_tasks", side_effect=AssertionError("Must not reclassify")
            ) as classify,
        ):
            recovered = _run_transcript_ingest(
                spec=SimpleNamespace(version="changed-version"), dry_run=False, limit=None, interactive=False
            )
        assert recovered == 0
        service.assert_not_called()
        classify.assert_not_called()
        assert reopened.execute("SELECT status FROM sources").fetchone()[0] == "processed"
        assert reopened.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 1
        assert store.load_prepared(reopened, "source-1") is None
    assert (tmp_path / prepared.vault_relative_path).read_text(encoding="utf-8").count("fieldkit-ingest-note:v1:") == 1
    assert (tmp_path / "TASKS.md").read_text(encoding="utf-8").count("fieldkit-task:v1:") == 1
    for slug in prepared.pursuits:
        assert (pursuits / f"{slug}.md").read_text(encoding="utf-8").count("fieldkit-ingest-pursuit:v1:") == 1


def test_replay_missing_pursuit_then_repair_preserves_note_edits(
    connection: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("fieldkit.pursuit.io.get_fieldkit_data", lambda: tmp_path / "runtime")
    task = store.PreparedTask(
        text="Send proposal", cls="my_task", owner="Alice", rationale="Assigned", pursuit_label="Acme"
    )
    prepared = _prepared().model_copy(update={"pursuits": ("project",), "action_items": (task.text,), "tasks": (task,)})
    assert store.save_prepared(connection, prepared) == prepared
    with pytest.raises(FileNotFoundError, match="project"):
        replay_prepared(connection, "source-1", tmp_path)
    assert store.load_prepared(connection, "source-1") == prepared
    assert connection.execute("SELECT status FROM sources").fetchone()[0] == "in_progress"
    assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
    assert not (tmp_path / "TASKS.md").exists()
    note = tmp_path / prepared.vault_relative_path
    edited = note.read_text(encoding="utf-8").replace("Meeting", "Human revision")
    note.write_text(edited, encoding="utf-8")
    pursuit = tmp_path / "accounts/acme/pursuits/project.md"
    pursuit.parent.mkdir(parents=True)
    pursuit.write_text("---\nstage: qualify\n---\n\n## Activity Log\n", encoding="utf-8")
    assert replay_prepared(connection, "source-1", tmp_path) == note
    assert note.read_text(encoding="utf-8") == edited
    assert pursuit.read_text(encoding="utf-8").count("fieldkit-ingest-pursuit:v1:") == 1
    assert (tmp_path / "TASKS.md").read_text(encoding="utf-8").count("fieldkit-task:v1:") == 1
    assert store.load_prepared(connection, "source-1") is None
    assert connection.execute("SELECT status FROM sources").fetchone()[0] == "processed"
    assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 1


@pytest.mark.parametrize("status", ["pending", "processed", "failed"])
def test_replay_rejects_unclaimed_source_before_file_effects(
    connection: sqlite3.Connection, tmp_path: Path, status: str
) -> None:
    prepared = _prepared()
    assert store.save_prepared(connection, prepared) == prepared
    connection.execute("UPDATE sources SET status = ?", (status,))
    connection.commit()
    with pytest.raises(ValueError, match="requires retained intent and a claimed source"):
        replay_prepared(connection, "source-1", tmp_path)
    assert not (tmp_path / "accounts").exists()
    assert store.load_prepared(connection, "source-1") == prepared


def test_conflicting_retry_preserves_record(connection: sqlite3.Connection) -> None:
    original = _prepared()
    assert store.save_prepared(connection, original) == original
    with pytest.raises(ValueError, match="Conflicting prepared ingest output"):
        store.save_prepared(connection, _prepared("Changed"))
    assert store.load_prepared(connection, "source-1") == original


@pytest.mark.parametrize("status", ["pending", "processed", "failed", "missing"])
def test_save_requires_claimed_source(connection: sqlite3.Connection, status: str) -> None:
    if status == "missing":
        connection.execute("DELETE FROM sources")
    else:
        connection.execute("UPDATE sources SET status = ?", (status,))
    connection.commit()
    with pytest.raises(ValueError, match="Prepared output requires a claimed transcript source"):
        store.save_prepared(connection, _prepared())
    assert connection.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 0


def test_save_does_not_commit_caller_transaction(connection: sqlite3.Connection) -> None:
    connection.execute("UPDATE sources SET meeting_title = 'uncommitted'")
    with pytest.raises(ValueError, match="Prepared output requires an idle connection"):
        store.save_prepared(connection, _prepared())
    assert connection.in_transaction
    connection.rollback()
    assert connection.execute("SELECT meeting_title FROM sources").fetchone()[0] is None


def test_missing_record_returns_none(connection: sqlite3.Connection) -> None:
    assert store.load_prepared(connection, "source-1") is None


@pytest.mark.parametrize("corrupt", [None, "{}", '{"source_id":"different"}'])
def test_corrupt_record_is_not_replaced(connection: sqlite3.Connection, corrupt: str | None) -> None:
    prepared = _prepared()
    assert store.save_prepared(connection, prepared) == prepared
    connection.execute("UPDATE checkpoints SET value = ?", (corrupt,))
    connection.commit()
    with pytest.raises(ValueError, match="Invalid prepared ingest output"):
        store.save_prepared(connection, prepared)
    assert connection.execute("SELECT value FROM checkpoints").fetchone()[0] == corrupt


def test_save_rejects_source_from_other_pipeline(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO pipelines (pipeline_id, description, version, source_format) VALUES (?, ?, ?, ?)",
        ("other", "Other", "1", "text"),
    )
    connection.execute("UPDATE sources SET pipeline_id = 'other'")
    connection.commit()
    with pytest.raises(ValueError, match="Prepared output requires a claimed transcript source"):
        store.save_prepared(connection, _prepared())
    assert connection.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 0


def test_load_rejects_valid_record_with_wrong_identity(connection: sqlite3.Connection) -> None:
    prepared = _prepared()
    assert store.save_prepared(connection, prepared) == prepared
    payload = prepared.model_dump(mode="json")
    payload["source_id"] = "source-2"
    payload["vault_relative_path"] = compute_vault_path(
        Path(), "acme", "2026-09-27", "Meeting", source_id="source-2"
    ).as_posix()
    raw = json.dumps(payload)
    connection.execute("UPDATE checkpoints SET value = ?", (raw,))
    connection.commit()
    with pytest.raises(ValueError, match="Invalid prepared ingest output identity"):
        store.load_prepared(connection, "source-1")
    assert connection.execute("SELECT value FROM checkpoints").fetchone()[0] == raw


def test_insert_failure_rolls_back_without_changing_source(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TRIGGER reject_checkpoint BEFORE INSERT ON checkpoints "
        "BEGIN SELECT RAISE(ABORT, 'checkpoint unavailable'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="checkpoint unavailable"):
        store.save_prepared(connection, _prepared())
    assert not connection.in_transaction
    assert store.load_prepared(connection, "source-1") is None
    assert connection.execute("SELECT status FROM sources").fetchone()[0] == "in_progress"


@pytest.mark.parametrize("conflicting", [False, True])
def test_concurrent_saves_retain_one_decision(
    connection: sqlite3.Connection, tmp_path: Path, conflicting: bool
) -> None:
    barrier = Barrier(2, timeout=5)

    def save(title: str) -> str:
        conn = sqlite3.connect(tmp_path / "pipeline.db", timeout=5)
        try:
            barrier.wait()
            try:
                return store.save_prepared(conn, _prepared(title)).meeting_title
            except ValueError as error:
                assert str(error) == "Conflicting prepared ingest output"
                return "conflict"
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(save, "Meeting")
        second = pool.submit(save, "Changed" if conflicting else "Meeting")
        results = [first.result(timeout=10), second.result(timeout=10)]
    assert results.count("conflict") == int(conflicting)
    retained = store.load_prepared(connection, "source-1")
    assert retained is not None
    assert retained.meeting_title in results
    assert connection.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 1


@pytest.mark.integration
def test_parallel_replay_preserves_shared_pursuit_and_tasks(connection: sqlite3.Connection, tmp_path: Path) -> None:
    from fieldkit.commands.ingest.run import _run_parallel_loop
    from fieldkit.ingest.note_effect import publish_prepared_note
    from fieldkit.ingest.pursuit_effect import publish_prepared_pursuit
    from fieldkit.ingest.task_effect import publish_prepared_tasks

    connection.execute(
        "INSERT INTO sources (source_id, pipeline_id, file_path, status) VALUES (?, ?, ?, ?)",
        ("source-2", "transcript-ingest", "source", "in_progress"),
    )
    connection.commit()
    records = []
    for source_id in ("source-1", "source-2"):
        record = _prepared().model_copy(
            update={
                "source_id": source_id,
                "vault_relative_path": compute_vault_path(
                    Path(), "acme", "2026-09-27", "Meeting", source_id=source_id
                ).as_posix(),
                "pursuits": ("shared",),
                "action_items": ("Send proposal",),
                "tasks": (
                    store.PreparedTask(
                        text="Send proposal", cls="my_task", owner="Alice", rationale="Assigned", pursuit_label="Acme"
                    ),
                ),
            }
        )
        assert store.save_prepared(connection, record) == record
        records.append(record)
    connection.execute("UPDATE sources SET status = 'pending'")
    connection.commit()
    pursuit = tmp_path / "accounts/acme/pursuits/shared.md"
    pursuit.parent.mkdir(parents=True)
    pursuit.write_text("---\nstage: qualify\n---\n\n## Activity Log\n", encoding="utf-8")
    barrier = Barrier(2, timeout=5)

    def publish_together(record: ReplayIntent, workspace: Path) -> Path:
        barrier.wait()
        return publish_prepared_note(record, workspace)

    sources = [
        SourceRecord(record.source_id, "transcript-ingest", "", "Meeting", None, "", "", "") for record in records
    ]
    with (
        patch("fieldkit.pursuit.io.get_fieldkit_data", return_value=tmp_path / "runtime"),
        patch("fieldkit.commands.ingest.run._dynamic_worker_count", return_value=2),
        patch("fieldkit.ingest.replay.publish_prepared_note", side_effect=publish_together),
        patch("fieldkit.ingest.docs.get_docs_service", side_effect=AssertionError("Must not authenticate")),
        exclusive_path_lock(tmp_path / "pipeline.db.transcript-run"),
    ):
        result = _run_parallel_loop(
            sources, data_root=tmp_path, db_path=tmp_path / "pipeline.db", pipeline_version="changed-version"
        )
        assert result == (2, 0, 0, 0)
        for record in records:
            assert publish_prepared_pursuit(ReplayIntent(record), tmp_path, "shared") is False
            assert publish_prepared_tasks(ReplayIntent(record), tmp_path) == (0, 0)

    assert [tuple(row) for row in connection.execute("SELECT source_id, status FROM sources ORDER BY source_id")] == [
        ("source-1", "processed"),
        ("source-2", "processed"),
    ]
    assert connection.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 0
    assert [
        tuple(row)
        for row in connection.execute("SELECT source_id, count(*) FROM artifacts GROUP BY source_id ORDER BY source_id")
    ] == [
        ("source-1", 1),
        ("source-2", 1),
    ]
    assert pursuit.read_text(encoding="utf-8").count("fieldkit-ingest-pursuit:v1:") == 2
    assert (tmp_path / "TASKS.md").read_text(encoding="utf-8").count("fieldkit-task:v1:") == 2
    for record in records:
        assert (tmp_path / record.vault_relative_path).read_text(encoding="utf-8").count(
            "fieldkit-ingest-note:v1:"
        ) == 1


@pytest.mark.parametrize("kind", ["text", "blob", "unicode"])
def test_load_rejects_oversized_or_binary_value_before_decoding(connection: sqlite3.Connection, kind: str) -> None:
    prepared = _prepared()
    assert store.save_prepared(connection, prepared) == prepared
    raw: str | bytes
    if kind == "blob":
        raw = prepared.model_dump_json().encode("utf-8")
    elif kind == "unicode":
        raw = "é" * (store.MAX_PREPARED_BYTES // 2 + 1)
    else:
        raw = "x" * (store.MAX_PREPARED_BYTES + 1)
    connection.execute("UPDATE checkpoints SET value = ?", (raw,))
    connection.commit()
    with (
        patch.object(store, "decode_prepared") as decode,
        pytest.raises(ValueError, match="Invalid prepared ingest output"),
    ):
        store.load_prepared(connection, "source-1")
    assert decode.call_count == 0


def test_completion_commits_artifact_status_and_checkpoint_together(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    prepared = _prepared()
    assert store.save_prepared(connection, prepared) == prepared
    path = store.complete_prepared(connection, prepared, tmp_path)
    assert path == tmp_path / prepared.vault_relative_path
    assert store.complete_prepared(connection, prepared, tmp_path) == path
    with closing(sqlite3.connect(tmp_path / "pipeline.db")) as reader:
        assert reader.execute("SELECT status FROM sources").fetchone()[0] == "processed"
        assert reader.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 0
        rows = reader.execute("SELECT content_path, pipeline_version FROM artifacts").fetchall()
        assert rows == [(str(path), "0.1.0")]


@pytest.mark.parametrize("write", ["INSERT ON artifacts", "UPDATE ON sources", "DELETE ON checkpoints"])
def test_completion_failure_retains_checkpoint_and_claim(
    connection: sqlite3.Connection, tmp_path: Path, write: str
) -> None:
    prepared = _prepared()
    assert store.save_prepared(connection, prepared) == prepared
    connection.execute(
        f"CREATE TRIGGER reject_completion BEFORE {write} BEGIN SELECT RAISE(ABORT, 'completion rejected'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="completion rejected"):
        store.complete_prepared(connection, prepared, tmp_path)
    with closing(sqlite3.connect(tmp_path / "pipeline.db")) as reader:
        assert store.load_prepared(reader, "source-1") == prepared
        assert reader.execute("SELECT status FROM sources").fetchone()[0] == "in_progress"
        assert reader.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0


@pytest.mark.parametrize("status", ["pending", "failed", "processed"])
def test_completion_rejects_unclaimed_source_without_losing_intent(
    connection: sqlite3.Connection, tmp_path: Path, status: str
) -> None:
    prepared = _prepared()
    assert store.save_prepared(connection, prepared) == prepared
    connection.execute("UPDATE sources SET status = ?", (status,))
    connection.commit()
    with pytest.raises(ValueError, match="Invalid prepared completion state"):
        store.complete_prepared(connection, prepared, tmp_path)
    assert store.load_prepared(connection, "source-1") == prepared
    assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0


def test_completion_rejects_changed_intent(connection: sqlite3.Connection, tmp_path: Path) -> None:
    prepared = _prepared()
    assert store.save_prepared(connection, prepared) == prepared
    with pytest.raises(ValueError, match="Invalid prepared completion state"):
        store.complete_prepared(connection, _prepared("Changed"), tmp_path)
    assert store.load_prepared(connection, "source-1") == prepared


def test_completion_rejects_missing_checkpoint(connection: sqlite3.Connection, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Invalid prepared completion state"):
        store.complete_prepared(connection, _prepared(), tmp_path)
    assert connection.execute("SELECT status FROM sources").fetchone()[0] == "in_progress"


def test_completion_preserves_caller_transaction(connection: sqlite3.Connection, tmp_path: Path) -> None:
    connection.execute("UPDATE sources SET meeting_title = 'uncommitted'")
    with pytest.raises(ValueError, match="Prepared output requires an idle connection"):
        store.complete_prepared(connection, _prepared(), tmp_path)
    assert connection.in_transaction
    connection.rollback()
    assert connection.execute("SELECT meeting_title FROM sources").fetchone()[0] is None


@pytest.mark.parametrize("write", ["INSERT ON artifacts", "UPDATE ON sources", "DELETE ON checkpoints"])
def test_completion_rejects_silently_ignored_write(connection: sqlite3.Connection, tmp_path: Path, write: str) -> None:
    prepared = _prepared()
    assert store.save_prepared(connection, prepared) == prepared
    connection.execute(f"CREATE TRIGGER ignore_completion BEFORE {write} BEGIN SELECT RAISE(IGNORE); END")
    with pytest.raises(ValueError, match="Invalid prepared completion transition"):
        store.complete_prepared(connection, prepared, tmp_path)
    assert store.load_prepared(connection, "source-1") == prepared
    assert connection.execute("SELECT status FROM sources").fetchone()[0] == "in_progress"
    assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0


def test_completion_retry_rejects_changed_artifact(connection: sqlite3.Connection, tmp_path: Path) -> None:
    prepared = _prepared()
    assert store.save_prepared(connection, prepared) == prepared
    assert store.complete_prepared(connection, prepared, tmp_path) == tmp_path / prepared.vault_relative_path
    connection.execute("UPDATE artifacts SET pipeline_version = 'different'")
    connection.commit()
    with pytest.raises(ValueError, match="Invalid prepared completion state"):
        store.complete_prepared(connection, prepared, tmp_path)
    assert connection.execute("SELECT pipeline_version FROM artifacts").fetchone()[0] == "different"
