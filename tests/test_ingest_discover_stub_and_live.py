"""Tests for _report_stub_pipeline and _live_run_transcript_ingest in ingest discover.

Covers CRAP hotspots in commands/ingest/discover.py:
- _report_stub_pipeline (CRAP 20.00, complexity 4)
- _live_run_transcript_ingest (CRAP 15.13, complexity 11)

Both functions perform their imports inside the function body (not at module
top level), so patches below target the DEFINITION sites
(fieldkit.ingest.db.get_db, fieldkit.gmail.discover.scan_gemini_candidates,
...) rather than fieldkit.commands.ingest.discover.* — patching the module
binding would be a no-op here since the name is never an attribute of the
discover module. See src/fieldkit/ingest/AGENTS.md for the pipeline.db /
gmail.db separation this module relies on.
"""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.commands.ingest.discover import (
    _dry_run_transcript_ingest,
    _live_run_transcript_ingest,
    _report_stub_pipeline,
)
from fieldkit.gmail.discover import GmailCandidate
from fieldkit.ingest.sources import SourceRecord

pytestmark = pytest.mark.unit


# ── helpers ──────────────────────────────────────────────────────────────


def _make_stub_conn(count: int, pipeline_id: str = "transcript-ingest") -> MagicMock:
    """Real in-memory pipeline.db wrapped in a MagicMock so close() calls are trackable.

    Using a real sqlite3.Row-backed connection (rather than a hand-mocked
    cursor chain) proves the `row["n"]` typed-Row access from the
    ArtifactRecord/Row pattern documented in ingest/AGENTS.md actually works,
    not just that some mock returned the right number.
    """
    real_conn = sqlite3.connect(":memory:")
    real_conn.row_factory = sqlite3.Row
    real_conn.execute("CREATE TABLE sources (source_id TEXT, pipeline_id TEXT)")
    for i in range(count):
        real_conn.execute(
            "INSERT INTO sources (source_id, pipeline_id) VALUES (?, ?)",
            (f"doc-{i}", pipeline_id),
        )
    real_conn.commit()
    return MagicMock(wraps=real_conn)


def _make_counting_conn(existing_count: int, total_count: int) -> MagicMock:
    """Mock pipeline.db conn whose two identical COUNT(*) queries return
    different values in sequence (before/after discovery)."""
    conn = MagicMock()
    cursor_before = MagicMock()
    cursor_before.fetchone.return_value = (existing_count,)
    cursor_after = MagicMock()
    cursor_after.fetchone.return_value = (total_count,)
    conn.execute.side_effect = [cursor_before, cursor_after]
    return conn


def _make_source_record(source_id: str, *, meeting_title: str = "", subject: str = "") -> SourceRecord:
    return SourceRecord(
        source_id=source_id,
        pipeline_id="transcript-ingest",
        subject=subject,
        meeting_title=meeting_title,
        meeting_date=None,
        doc_url=f"https://docs.google.com/document/d/{source_id}",
        email_message_id="msg-1",
        discovered_at="2026-08-06T00:00:00Z",
    )


# ── _report_stub_pipeline ───────────────────────────────────────────────


def test_report_stub_pipeline_human_reports_count(capsys: pytest.CaptureFixture[str]) -> None:
    conn = _make_stub_conn(3)
    with patch("fieldkit.ingest.db.get_db", return_value=conn):
        result = _report_stub_pipeline("transcript-ingest", "[dry-run] ", as_json=False)

    captured = capsys.readouterr()
    assert captured.out == "[dry-run] Pipeline 'transcript-ingest': 3 registered source(s).\n"
    assert result == 0


def test_report_stub_pipeline_json_reports_count(capsys: pytest.CaptureFixture[str]) -> None:
    conn = _make_stub_conn(5)
    with patch("fieldkit.ingest.db.get_db", return_value=conn):
        result = _report_stub_pipeline("transcript-ingest", "", as_json=True)

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "pipeline_id": "transcript-ingest",
        "stub": True,
        "items": [],
        "count": 0,
        "registered_sources": 5,
    }
    assert result == 0


def test_report_stub_pipeline_none_row_defaults_count_to_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """Empty-table edge case: fetchone() returns a falsy/None row, not just a
    row with n=0 — proves the `row["n"] if row else 0` ternary, not just the
    happy path (a real COUNT(*) query never actually returns None)."""
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = None

    with patch("fieldkit.ingest.db.get_db", return_value=conn):
        result = _report_stub_pipeline("transcript-ingest", "", as_json=False)

    captured = capsys.readouterr()
    assert captured.out == "Pipeline 'transcript-ingest': 0 registered source(s).\n"
    assert result == 0


def test_report_stub_pipeline_closes_connection_via_finally() -> None:
    conn = _make_stub_conn(1)
    with patch("fieldkit.ingest.db.get_db", return_value=conn):
        _report_stub_pipeline("transcript-ingest", "", as_json=False)

    assert conn.close.call_count == 1


def test_report_stub_pipeline_file_not_found_human_message(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("fieldkit.ingest.db.get_db", side_effect=FileNotFoundError("no pipeline.db")):
        result = _report_stub_pipeline("transcript-ingest", "", as_json=False)

    captured = capsys.readouterr()
    assert captured.out == "pipeline.db not initialized. Run: fieldkit ingest status (initializes on first run)\n"
    assert result == 0


def test_report_stub_pipeline_file_not_found_json_shape(capsys: pytest.CaptureFixture[str]) -> None:
    # conn is never opened when get_db() itself raises — nothing to assert
    # a close() call count on, since `conn` is never bound in this branch.
    with patch("fieldkit.ingest.db.get_db", side_effect=FileNotFoundError("no pipeline.db")):
        result = _report_stub_pipeline("transcript-ingest", "", as_json=True)

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "pipeline_id": "transcript-ingest",
        "stub": True,
        "items": [],
        "count": 0,
        "registered_sources": None,
        "note": "pipeline.db not initialized. Run: fieldkit ingest status",
    }
    assert result == 0


# ── _live_run_transcript_ingest ─────────────────────────────────────────


def test_dry_run_delegates_account_filter_to_gmail_discovery(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    candidate = GmailCandidate(
        source_id="doc-1",
        doc_url="https://docs.google.com/document/d/doc-1",
        subject="Customer meeting",
        meeting_title="Customer meeting",
        meeting_date=None,
        email_message_id="message-1",
    )
    with (
        patch("fieldkit.gmail.discover.scan_gemini_candidates", return_value=[candidate]) as scan,
        patch(
            "fieldkit.ingest.sources.filter_gemini_candidates_for_account", return_value=[candidate]
        ) as filter_candidates,
    ):
        result = _dry_run_transcript_ingest(
            "transcript-ingest",
            "[dry-run] ",
            tmp_path / "gmail.db",
            10,
            account="acme",
        )

    assert result == 0
    scan.assert_called_once_with(
        tmp_path / "gmail.db",
        10,
        max_age_days=None,
        default_limit=None,
        require_positive_limit=False,
    )
    filter_candidates.assert_called_once_with([candidate], "acme")
    assert "doc-1" in capsys.readouterr().out


def test_live_run_uses_get_db_when_pipeline_db_exists(tmp_path: Path) -> None:
    db_path = tmp_path / "pipeline.db"
    db_path.touch()
    conn = _make_counting_conn(0, 0)

    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=db_path),
        patch("fieldkit.ingest.db.get_db", return_value=conn) as m_get_db,
        patch("fieldkit.ingest.db.init_db") as m_init_db,
        patch("fieldkit.gmail.discover.scan_gemini_candidates", return_value=[]),
        patch("fieldkit.ingest.sources.discover_gemini_sources", return_value=[]),
    ):
        result = _live_run_transcript_ingest("transcript-ingest", tmp_path / "gmail.db", None)

    m_get_db.assert_called_once_with(db_path)
    m_init_db.assert_not_called()
    assert result == 0


def test_live_run_uses_init_db_when_pipeline_db_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "pipeline.db"  # never created -> exists() is False
    conn = _make_counting_conn(0, 0)

    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=db_path),
        patch("fieldkit.ingest.db.get_db") as m_get_db,
        patch("fieldkit.ingest.db.init_db", return_value=conn) as m_init_db,
        patch("fieldkit.commands.ingest.registry.PIPELINES", []) as m_pipelines,
        patch("fieldkit.gmail.discover.scan_gemini_candidates", return_value=[]),
        patch("fieldkit.ingest.sources.discover_gemini_sources", return_value=[]),
    ):
        result = _live_run_transcript_ingest("transcript-ingest", tmp_path / "gmail.db", None)

    m_init_db.assert_called_once_with(db_path, pipelines=m_pipelines)
    m_get_db.assert_not_called()
    assert result == 0


def test_live_run_db_open_error_returns_one_without_discovery(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch("fieldkit.ingest.db.get_db_path", side_effect=RuntimeError("disk error")),
        patch("fieldkit.gmail.discover.scan_gemini_candidates") as m_scan,
    ):
        result = _live_run_transcript_ingest("transcript-ingest", tmp_path / "gmail.db", None)

    captured = capsys.readouterr()
    assert result == 1
    assert "Error opening pipeline.db: disk error" in captured.err
    m_scan.assert_not_called()


def test_live_run_account_note_printed_when_account_given(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "pipeline.db"
    db_path.touch()
    conn = _make_counting_conn(0, 0)

    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=db_path),
        patch("fieldkit.ingest.db.get_db", return_value=conn),
        patch("fieldkit.gmail.discover.scan_gemini_candidates", return_value=[]),
        patch("fieldkit.ingest.sources.discover_gemini_sources", return_value=[]),
    ):
        _live_run_transcript_ingest(
            "transcript-ingest",
            tmp_path / "gmail.db",
            None,
            account="acme-corp",
        )

    assert "Note: --account=acme-corp scopes output reporting." in capsys.readouterr().err


def test_live_run_account_note_absent_when_account_none(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "pipeline.db"
    db_path.touch()
    conn = _make_counting_conn(0, 0)

    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=db_path),
        patch("fieldkit.ingest.db.get_db", return_value=conn),
        patch("fieldkit.gmail.discover.scan_gemini_candidates", return_value=[]),
        patch("fieldkit.ingest.sources.discover_gemini_sources", return_value=[]),
    ):
        _live_run_transcript_ingest("transcript-ingest", tmp_path / "gmail.db", None, account=None)

    assert "Note: --account=" not in capsys.readouterr().err


def test_live_run_json_payload_shape(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "pipeline.db"
    db_path.touch()
    conn = _make_counting_conn(2, 4)
    rec1 = _make_source_record("doc-1", meeting_title="Kickoff Call", subject="Notes: fallback")
    rec2 = _make_source_record("doc-2", meeting_title="", subject='Notes: "Quarterly Review" Aug 1, 2026')

    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=db_path),
        patch("fieldkit.ingest.db.get_db", return_value=conn),
        patch("fieldkit.gmail.discover.scan_gemini_candidates", return_value=[]),
        patch("fieldkit.ingest.sources.discover_gemini_sources", return_value=[rec1, rec2]),
    ):
        result = _live_run_transcript_ingest(
            "transcript-ingest",
            tmp_path / "gmail.db",
            25,
            account="acme-corp",
            as_json=True,
        )

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "pipeline_id": "transcript-ingest",
        "dry_run": False,
        "items": [
            {"source_id": "doc-1", "title": "Kickoff Call"},
            {"source_id": "doc-2", "title": 'Notes: "Quarterly Review" Aug 1, 2026'},
        ],
        "count": 2,
        "newly_discovered": 2,
        "previously_registered": 2,
        "total_sources": 4,
        "filters": {"account": "acme-corp", "limit": 25},
    }
    assert result == 0


def test_live_run_human_output_truncates_after_five_items(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "pipeline.db"
    db_path.touch()
    conn = _make_counting_conn(0, 6)
    records = [_make_source_record(f"doc-{i}", meeting_title=f"Meeting {i}") for i in range(6)]

    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=db_path),
        patch("fieldkit.ingest.db.get_db", return_value=conn),
        patch("fieldkit.gmail.discover.scan_gemini_candidates", return_value=[]),
        patch("fieldkit.ingest.sources.discover_gemini_sources", return_value=records),
    ):
        _live_run_transcript_ingest("transcript-ingest", tmp_path / "gmail.db", None)

    out = capsys.readouterr().out
    for i in range(5):
        assert f"doc-{i}  Meeting {i}" in out
    assert "doc-5" not in out
    assert "... and 1 more" in out


def test_live_run_human_output_no_truncation_at_exactly_five_items(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "pipeline.db"
    db_path.touch()
    conn = _make_counting_conn(0, 5)
    records = [_make_source_record(f"doc-{i}", meeting_title=f"Meeting {i}") for i in range(5)]

    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=db_path),
        patch("fieldkit.ingest.db.get_db", return_value=conn),
        patch("fieldkit.gmail.discover.scan_gemini_candidates", return_value=[]),
        patch("fieldkit.ingest.sources.discover_gemini_sources", return_value=records),
    ):
        _live_run_transcript_ingest("transcript-ingest", tmp_path / "gmail.db", None)

    out = capsys.readouterr().out
    for i in range(5):
        assert f"doc-{i}  Meeting {i}" in out
    assert "... and" not in out


def test_live_run_human_output_omits_new_sources_header_when_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "pipeline.db"
    db_path.touch()
    conn = _make_counting_conn(3, 3)

    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=db_path),
        patch("fieldkit.ingest.db.get_db", return_value=conn),
        patch("fieldkit.gmail.discover.scan_gemini_candidates", return_value=[]),
        patch("fieldkit.ingest.sources.discover_gemini_sources", return_value=[]),
    ):
        _live_run_transcript_ingest("transcript-ingest", tmp_path / "gmail.db", None)

    assert "New sources" not in capsys.readouterr().out


def test_live_run_file_not_found_during_discovery_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "pipeline.db"
    db_path.touch()
    conn = _make_counting_conn(0, 0)

    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=db_path),
        patch("fieldkit.ingest.db.get_db", return_value=conn),
        patch("fieldkit.gmail.discover.scan_gemini_candidates", side_effect=FileNotFoundError("gmail.db missing")),
    ):
        result = _live_run_transcript_ingest("transcript-ingest", tmp_path / "gmail.db", None)

    captured = capsys.readouterr()
    assert result == 1
    assert "Error: gmail.db missing" in captured.err


def test_live_run_generic_exception_during_discovery_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "pipeline.db"
    db_path.touch()
    conn = _make_counting_conn(0, 0)

    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=db_path),
        patch("fieldkit.ingest.db.get_db", return_value=conn),
        patch("fieldkit.gmail.discover.scan_gemini_candidates", side_effect=ValueError("boom")),
    ):
        result = _live_run_transcript_ingest("transcript-ingest", tmp_path / "gmail.db", None)

    captured = capsys.readouterr()
    assert result == 1
    assert "Error during discovery: boom" in captured.err


def test_live_run_closes_connection_in_happy_path(tmp_path: Path) -> None:
    db_path = tmp_path / "pipeline.db"
    db_path.touch()
    conn = _make_counting_conn(0, 0)

    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=db_path),
        patch("fieldkit.ingest.db.get_db", return_value=conn),
        patch("fieldkit.gmail.discover.scan_gemini_candidates", return_value=[]),
        patch("fieldkit.ingest.sources.discover_gemini_sources", return_value=[]),
    ):
        _live_run_transcript_ingest("transcript-ingest", tmp_path / "gmail.db", None)

    assert conn.close.call_count == 1


def test_live_run_closes_connection_on_file_not_found_path(tmp_path: Path) -> None:
    db_path = tmp_path / "pipeline.db"
    db_path.touch()
    conn = _make_counting_conn(0, 0)

    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=db_path),
        patch("fieldkit.ingest.db.get_db", return_value=conn),
        patch("fieldkit.gmail.discover.scan_gemini_candidates", side_effect=FileNotFoundError("gone")),
    ):
        _live_run_transcript_ingest("transcript-ingest", tmp_path / "gmail.db", None)

    assert conn.close.call_count == 1


def test_live_run_closes_connection_on_generic_exception_path(tmp_path: Path) -> None:
    db_path = tmp_path / "pipeline.db"
    db_path.touch()
    conn = _make_counting_conn(0, 0)

    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=db_path),
        patch("fieldkit.ingest.db.get_db", return_value=conn),
        patch("fieldkit.gmail.discover.scan_gemini_candidates", side_effect=RuntimeError("boom")),
    ):
        _live_run_transcript_ingest("transcript-ingest", tmp_path / "gmail.db", None)

    assert conn.close.call_count == 1
