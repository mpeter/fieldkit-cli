"""Ambient transcript source validation and readiness contracts."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from fieldkit.commands.ingest.discover import cli as discover_cli
from fieldkit.commands.ingest.registry import PIPELINES
from fieldkit.commands.ingest.run import cli as run_cli
from fieldkit.errors import LLMError
from fieldkit.ingest.ambient import (
    AmbientSourceError,
    discover_ambient_snapshots,
    load_ambient_snapshot,
    register_ambient_snapshots,
)
from fieldkit.ingest.ambient_pipeline import AmbientOutcome, process_ambient_source
from fieldkit.ingest.db import get_db, init_db
from fieldkit.ingest.router import Confidence, RouteResult, route_by_content

pytestmark = pytest.mark.unit


def _write_session(path: Path, texts: list[str]) -> None:
    lines = [
        json.dumps(
            {
                "text": text,
                "started_at": f"2026-09-10T12:00:{index:02d}+00:00",
                "ended_at": f"2026-09-10T12:00:{index + 1:02d}+00:00",
                "segment_id": f"segment-{index}",
                "speaker": f"spk_{index % 2}",
            }
        )
        for index, text in enumerate(texts)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_discovery_excludes_newest_session_by_default(tmp_path: Path) -> None:
    older = tmp_path / "session-20260910T120000Z.jsonl"
    newest = tmp_path / "session-20260910T130000Z.jsonl"
    _write_session(older, ["older"])
    _write_session(newest, ["newest"])

    result = discover_ambient_snapshots(tmp_path)

    assert [snapshot.path for snapshot in result] == [older]


def test_discovery_include_latest_reads_all_stable_sessions(tmp_path: Path) -> None:
    older = tmp_path / "session-20260910T120000Z.jsonl"
    newest = tmp_path / "session-20260910T130000Z.jsonl"
    _write_session(older, ["older"])
    _write_session(newest, ["newest"])

    result = discover_ambient_snapshots(tmp_path, include_latest=True)

    assert [snapshot.path for snapshot in result] == [older, newest]


def test_discovery_with_one_file_returns_none_without_override(tmp_path: Path) -> None:
    _write_session(tmp_path / "session-20260910T120000Z.jsonl", ["active"])

    result = discover_ambient_snapshots(tmp_path)

    assert result == []


def test_snapshot_is_content_addressed_and_renders_speaker_lines(tmp_path: Path) -> None:
    path = tmp_path / "session-20260910T120000Z.jsonl"
    _write_session(path, ["First statement", "Second statement"])

    result = load_ambient_snapshot(path, ambient_root=tmp_path)

    assert result.source_id.startswith("ambient:")
    assert result.file_hash == result.source_id.removeprefix("ambient:")
    assert result.relative_path == Path(path.name)
    assert result.meeting_date == datetime(2026, 9, 10, tzinfo=UTC).date()
    assert "spk_0: First statement" in result.transcript
    assert "spk_1: Second statement" in result.transcript


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        pytest.param("not-json\n", "line 1 is not valid JSON", id="invalid-json"),
        pytest.param(json.dumps({"text": "hello"}) + "\n", "missing required", id="missing-fields"),
        pytest.param(
            json.dumps(
                {
                    "text": "hello",
                    "started_at": "not-a-date",
                    "ended_at": "2026-09-10T12:00:01+00:00",
                    "segment_id": "segment-1",
                }
            )
            + "\n",
            "invalid timestamp",
            id="invalid-time",
        ),
    ],
)
def test_snapshot_rejects_malformed_segments(tmp_path: Path, payload: str, match: str) -> None:
    path = tmp_path / "session-20260910T120000Z.jsonl"
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(AmbientSourceError, match=match):
        load_ambient_snapshot(path, ambient_root=tmp_path)


def test_snapshot_rejects_path_outside_ambient_root(tmp_path: Path) -> None:
    root = tmp_path / "ambient"
    root.mkdir()
    outside = tmp_path / "session-20260910T120000Z.jsonl"
    _write_session(outside, ["outside"])

    with pytest.raises(AmbientSourceError, match="outside ambient root"):
        load_ambient_snapshot(outside, ambient_root=root)


def test_snapshot_rejects_oversized_file_before_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "session-20260910T120000Z.jsonl"
    _write_session(path, ["hello"])
    monkeypatch.setattr("fieldkit.ingest.ambient.MAX_AMBIENT_SOURCE_BYTES", 1)

    with pytest.raises(AmbientSourceError, match="exceeds"):
        load_ambient_snapshot(path, ambient_root=tmp_path)


def test_snapshot_rejects_rendered_transcript_over_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "session-20260910T120000Z.jsonl"
    _write_session(path, ["hello"])
    monkeypatch.setattr("fieldkit.ingest.ambient.MAX_AMBIENT_TRANSCRIPT_CHARS", 1)

    with pytest.raises(AmbientSourceError, match="rendered characters"):
        load_ambient_snapshot(path, ambient_root=tmp_path)


def test_snapshot_rejects_non_utf8_input(tmp_path: Path) -> None:
    path = tmp_path / "session-20260910T120000Z.jsonl"
    path.write_bytes(b"\xff\xfe")

    with pytest.raises(AmbientSourceError, match="valid UTF-8"):
        load_ambient_snapshot(path, ambient_root=tmp_path)


def test_snapshot_rejects_file_mutation_during_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "session-20260910T120000Z.jsonl"
    _write_session(path, ["before"])
    real_read_bytes = Path.read_bytes

    def mutate_after_read(target: Path) -> bytes:
        payload = real_read_bytes(target)
        target.write_bytes(payload + b"\n")
        return payload

    monkeypatch.setattr(Path, "read_bytes", mutate_after_read)

    with pytest.raises(AmbientSourceError, match="changed while reading"):
        load_ambient_snapshot(path, ambient_root=tmp_path)


def test_register_snapshots_is_idempotent_and_retains_relative_provenance(tmp_path: Path) -> None:
    ambient_root = tmp_path / "home" / "scratch" / "ambient"
    ambient_root.mkdir(parents=True)
    path = ambient_root / "session-20260910T120000Z.jsonl"
    _write_session(path, ["customer discussion"])
    snapshot = load_ambient_snapshot(path, ambient_root=ambient_root)
    conn = init_db(tmp_path / "pipeline.db", pipelines=PIPELINES)

    first = register_ambient_snapshots(conn, [snapshot])
    second = register_ambient_snapshots(conn, [snapshot])
    row = conn.execute("SELECT source_id, pipeline_id, file_path, file_hash, status FROM sources").fetchone()

    assert first == [snapshot]
    assert second == []
    assert dict(row) == {
        "source_id": snapshot.source_id,
        "pipeline_id": "ambient-transcript-ingest",
        "file_path": path.name,
        "file_hash": snapshot.file_hash,
        "status": "pending",
    }
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
    conn.close()


def test_discover_cli_dry_run_hides_transcript_text_and_excludes_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ambient_root = tmp_path / "scratch" / "ambient"
    ambient_root.mkdir(parents=True)
    _write_session(ambient_root / "session-20260910T120000Z.jsonl", ["private older words"])
    _write_session(ambient_root / "session-20260910T130000Z.jsonl", ["private newest words"])
    monkeypatch.setattr("fieldkit.config.get_fieldkit_home", lambda: tmp_path)

    result = CliRunner().invoke(
        discover_cli,
        ["--pipeline", "ambient-transcript-ingest", "--dry-run", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["count"] == 1
    assert payload["items"][0]["file"] == "session-20260910T120000Z.jsonl"
    assert "private older words" not in result.output
    assert "private newest words" not in result.output


def test_discover_cli_rejects_latest_override_for_other_pipeline() -> None:
    result = CliRunner().invoke(discover_cli, ["--pipeline", "transcript-ingest", "--include-latest"])

    assert result.exit_code == 1
    assert "only valid for ambient-transcript-ingest" in result.output


def test_discover_cli_registers_valid_files_and_reports_malformed_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ambient_root = tmp_path / "scratch" / "ambient"
    ambient_root.mkdir(parents=True)
    _write_session(ambient_root / "session-20260910T120000Z.jsonl", ["valid"])
    (ambient_root / "session-20260910T130000Z.jsonl").write_text("not-json\n", encoding="utf-8")
    _write_session(ambient_root / "session-20260910T140000Z.jsonl", ["active"])
    monkeypatch.setattr("fieldkit.config.get_fieldkit_home", lambda: tmp_path)

    result = CliRunner().invoke(
        discover_cli,
        ["--pipeline", "ambient-transcript-ingest", "--dry-run", "--json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["count"] == 1
    assert payload["items"][0]["file"] == "session-20260910T120000Z.jsonl"
    assert payload["failed"][0]["file"] == "session-20260910T130000Z.jsonl"
    assert "not-json" not in result.output


def test_content_routing_requires_whole_word_account_keywords(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "accounts.yaml").write_text(
        "accounts:\n  acme-corp:\n    keywords: [acme]\n  beta-co:\n    keywords: [beta]\n",
        encoding="utf-8",
    )

    single = route_by_content("We reviewed the ACME rollout.", data_root=tmp_path)
    embedded = route_by_content("The macmeup token is unrelated.", data_root=tmp_path)
    ambiguous = route_by_content("ACME and beta joined the call.", data_root=tmp_path)

    assert single.accounts == ["acme-corp"]
    assert single.confidence is Confidence.LOW
    assert embedded.accounts == ["unknown"]
    assert ambiguous.accounts == ["acme-corp", "beta-co"]


def _registered_ambient_source(tmp_path: Path, texts: list[str]) -> tuple[sqlite3.Connection, str]:
    ambient_root = tmp_path / "scratch" / "ambient"
    ambient_root.mkdir(parents=True)
    path = ambient_root / "session-20260910T120000Z.jsonl"
    _write_session(path, texts)
    snapshot = load_ambient_snapshot(path, ambient_root=ambient_root)
    conn = init_db(tmp_path / "pipeline.db", pipelines=PIPELINES)
    register_ambient_snapshots(conn, [snapshot])
    return conn, snapshot.source_id


def test_process_noise_records_outcome_without_meeting_note(tmp_path: Path) -> None:
    conn, source_id = _registered_ambient_source(tmp_path, ["background noise"])

    result = process_ambient_source(conn, source_id=source_id, fieldkit_home=tmp_path, pipeline_version="0.1.0")

    assert result.status == "skipped"
    assert result.reason == "noise"
    artifact = conn.execute("SELECT artifact_type, content_path FROM artifacts").fetchone()
    assert tuple(artifact) == ("ambient-noise", None)
    assert list((tmp_path / "accounts").glob("**/*.md")) == []
    conn.close()


def test_process_ambiguous_route_defers_and_restores_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn, source_id = _registered_ambient_source(tmp_path, ["customer discussion " * 10])
    monkeypatch.setattr(
        "fieldkit.ingest.ambient_pipeline.route_by_content",
        lambda _content: RouteResult(accounts=["acme-corp", "beta-co"], confidence=Confidence.LOW, is_internal=False),
    )

    result = process_ambient_source(conn, source_id=source_id, fieldkit_home=tmp_path, pipeline_version="0.1.0")

    assert result.status == "deferred"
    assert conn.execute("SELECT status FROM sources WHERE source_id = ?", (source_id,)).fetchone()[0] == "pending"
    assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
    conn.close()


def test_process_single_account_writes_ambient_provenance_note(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn, source_id = _registered_ambient_source(tmp_path, ["customer discussion " * 10])
    monkeypatch.setattr(
        "fieldkit.ingest.ambient_pipeline.route_by_content",
        lambda _content: RouteResult(accounts=["acme-corp"], confidence=Confidence.LOW, is_internal=False),
    )
    monkeypatch.setenv("NO_LLM", "1")

    result = process_ambient_source(conn, source_id=source_id, fieldkit_home=tmp_path, pipeline_version="0.1.0")

    assert result.status == "processed"
    assert result.content_path is not None
    note = Path(result.content_path).read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(note.split("---", 2)[1])
    assert "pipeline: ambient-transcript-ingest" in note
    assert "source_format: ambient-jsonl" in note
    assert "source_path: session-20260910T120000Z.jsonl" in note
    assert "customer discussion" in note
    assert str(tmp_path) not in note
    assert frontmatter["accounts"] == ["acme-corp"]
    assert frontmatter["pursuits"] == []
    assert frontmatter["routing_confidence"] == "low"
    conn.close()


def test_run_cli_reports_deferred_in_order_and_exits_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "pipeline.db"
    conn = init_db(db_path, pipelines=PIPELINES)
    for source_id in ("ambient:first", "ambient:second"):
        conn.execute(
            "INSERT INTO sources (source_id, pipeline_id, file_path, status) VALUES (?, ?, ?, 'pending')",
            (source_id, "ambient-transcript-ingest", f"{source_id}.jsonl"),
        )
    conn.commit()
    conn.close()
    monkeypatch.setattr("fieldkit.ingest.db.get_db_path", lambda: db_path)
    monkeypatch.setattr("fieldkit.config.get_fieldkit_home", lambda: tmp_path)
    monkeypatch.setattr(
        "fieldkit.ingest.ambient_pipeline.process_ambient_source",
        lambda _conn, *, source_id, fieldkit_home, pipeline_version: AmbientOutcome(
            source_id, "deferred", reason="account route is not unique"
        ),
    )

    result = CliRunner().invoke(run_cli, ["--pipeline", "ambient-transcript-ingest", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["deferred"] == ["ambient:first", "ambient:second"]
    assert payload["failed"] == []
    assert payload["processed"] == []


def test_run_cli_dry_run_lists_pending_ids_without_reading_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "pipeline.db"
    conn = init_db(db_path, pipelines=PIPELINES)
    conn.execute(
        "INSERT INTO sources (source_id, pipeline_id, file_path, status) VALUES (?, ?, ?, 'pending')",
        ("ambient:pending", "ambient-transcript-ingest", "session.jsonl"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("fieldkit.ingest.db.get_db_path", lambda: db_path)

    result = CliRunner().invoke(
        run_cli,
        ["--pipeline", "ambient-transcript-ingest", "--dry-run", "--json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.output)["pending"] == ["ambient:pending"]


def test_run_cli_reports_noise_as_successful_skip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn, source_id = _registered_ambient_source(tmp_path, ["background noise"])
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    conn.close()
    monkeypatch.setattr("fieldkit.ingest.db.get_db_path", lambda: db_path)
    monkeypatch.setattr("fieldkit.config.get_fieldkit_home", lambda: tmp_path)

    result = CliRunner().invoke(run_cli, ["--pipeline", "ambient-transcript-ingest", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["skipped"] == [source_id]
    assert payload["processed"] == []


def test_concurrent_processors_create_at_most_one_artifact(tmp_path: Path) -> None:
    conn, source_id = _registered_ambient_source(tmp_path, ["background noise"])
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    conn.close()

    def process() -> AmbientOutcome:
        worker_conn = get_db(db_path)
        try:
            return process_ambient_source(
                worker_conn,
                source_id=source_id,
                fieldkit_home=tmp_path,
                pipeline_version="0.1.0",
            )
        finally:
            worker_conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: process(), range(2)))

    verify_conn = get_db(db_path)
    try:
        assert [result.status for result in results] == ["skipped", "skipped"]
        assert sorted(result.reason for result in results if result.reason is not None) == [
            "noise",
            "source was already claimed",
        ]
        assert verify_conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1
    finally:
        verify_conn.close()


def test_llm_failure_marks_source_failed_instead_of_leaving_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, source_id = _registered_ambient_source(tmp_path, ["customer discussion " * 10])
    monkeypatch.setattr(
        "fieldkit.ingest.ambient_pipeline.route_by_content",
        lambda _content: RouteResult(accounts=["acme-corp"], confidence=Confidence.LOW, is_internal=False),
    )
    monkeypatch.setattr(
        "fieldkit.ingest.ambient_pipeline.stage1_clean",
        lambda _content: (_ for _ in ()).throw(LLMError("provider unavailable", category="general")),
    )

    result = process_ambient_source(conn, source_id=source_id, fieldkit_home=tmp_path, pipeline_version="0.1.0")

    assert result.status == "failed"
    assert conn.execute("SELECT status FROM sources WHERE source_id = ?", (source_id,)).fetchone()[0] == "failed"
    conn.close()


def test_llm_rate_limit_defers_source_for_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn, source_id = _registered_ambient_source(tmp_path, ["customer discussion " * 10])
    monkeypatch.setattr(
        "fieldkit.ingest.ambient_pipeline.route_by_content",
        lambda _content: RouteResult(accounts=["acme-corp"], confidence=Confidence.LOW, is_internal=False),
    )
    monkeypatch.setattr(
        "fieldkit.ingest.ambient_pipeline.stage1_clean",
        lambda _content: (_ for _ in ()).throw(LLMError("try later", category="rate-limit")),
    )

    result = process_ambient_source(conn, source_id=source_id, fieldkit_home=tmp_path, pipeline_version="0.1.0")

    assert result.status == "deferred"
    assert conn.execute("SELECT status FROM sources WHERE source_id = ?", (source_id,)).fetchone()[0] == "pending"
    conn.close()


def test_llm_auth_failure_propagates_to_global_exit_mapper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn, source_id = _registered_ambient_source(tmp_path, ["customer discussion " * 10])
    monkeypatch.setattr(
        "fieldkit.ingest.ambient_pipeline.route_by_content",
        lambda _content: RouteResult(accounts=["acme-corp"], confidence=Confidence.LOW, is_internal=False),
    )
    monkeypatch.setattr(
        "fieldkit.ingest.ambient_pipeline.stage1_clean",
        lambda _content: (_ for _ in ()).throw(LLMError("login required", category="auth")),
    )

    with pytest.raises(LLMError, match="login required"):
        process_ambient_source(conn, source_id=source_id, fieldkit_home=tmp_path, pipeline_version="0.1.0")

    assert conn.execute("SELECT status FROM sources WHERE source_id = ?", (source_id,)).fetchone()[0] == "pending"
    conn.close()


def test_failed_atomic_write_removes_plaintext_temporary_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn, source_id = _registered_ambient_source(tmp_path, ["customer discussion " * 10])
    monkeypatch.setattr(
        "fieldkit.ingest.ambient_pipeline.route_by_content",
        lambda _content: RouteResult(accounts=["acme-corp"], confidence=Confidence.LOW, is_internal=False),
    )
    monkeypatch.setenv("NO_LLM", "1")
    monkeypatch.setattr(Path, "replace", lambda _self, _target: (_ for _ in ()).throw(OSError("disk full")))

    result = process_ambient_source(conn, source_id=source_id, fieldkit_home=tmp_path, pipeline_version="0.1.0")

    assert result.status == "failed"
    meetings = tmp_path / "accounts" / "acme-corp" / "meetings"
    assert list(meetings.glob(".*.tmp")) == []
    assert list(meetings.glob("*.md")) == []
    conn.close()
