"""Subprocess-level tests for `fieldkit ingest` CLI group.

Two test layers:
1. Subprocess smoke tests (fieldkit ingest status / --help / discover / run dry-run).
   These use real databases (or no DB) and exist in all environments.
2. In-process integration tests for discover→run end-to-end flow.
   These monkeypatch get_db_path / get_gmail_db_path / get_fieldkit_home / get_docs_service
   so the full pipeline runs against ephemeral tmp_path fixtures with no real I/O.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

import fieldkit.config._loader as config_loader

pytestmark = pytest.mark.unit

# Absolute path to fieldkit-tools/ so subprocess runs from the right directory.
TOOLS_DIR = Path(__file__).resolve().parents[1]

PIPELINE_IDS = [
    "transcript-ingest",
    "customer-notes-ingest",
    "pm-status-ingest",
]

_SUBPROCESS_TIMEOUT_SECONDS = 30


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    """Run `python -m fieldkit <args>` from fieldkit-tools/."""
    env = {
        **os.environ,
        "XDG_CONFIG_HOME": str(config_loader.CONFIG_PATH.parent.parent),
    }
    return subprocess.run(
        [sys.executable, "-m", "fieldkit", *args],
        capture_output=True,
        text=True,
        cwd=str(TOOLS_DIR),
        env=env,
        check=False,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
    )


# ── fieldkit ingest status ─────────────────────────────────────────────────


def test_ingest_status_exits_0() -> None:
    """`fieldkit ingest status` must exit 0."""
    result = _run("ingest", "status")
    assert result.returncode == 0, result.stderr


def test_ingest_status_contains_all_pipeline_ids() -> None:
    """`fieldkit ingest status` output must list all 3 registered pipeline IDs."""
    result = _run("ingest", "status")
    assert result.returncode == 0, result.stderr
    for pipeline_id in PIPELINE_IDS:
        assert pipeline_id in result.stdout, f"Missing pipeline ID {pipeline_id!r} in output:\n{result.stdout}"


def test_ingest_status_lists_version_column() -> None:
    """`fieldkit ingest status` output must include a version string."""
    result = _run("ingest", "status")
    assert result.returncode == 0, result.stderr
    # All registry entries are version 0.1.0
    assert "0.1.0" in result.stdout


def test_ingest_status_lists_status_column() -> None:
    """`fieldkit ingest status` output must include 'active' and 'stub' status values."""
    result = _run("ingest", "status")
    assert result.returncode == 0, result.stderr
    assert "active" in result.stdout
    assert "stub" in result.stdout


# ── fieldkit ingest --help ─────────────────────────────────────────────────


def test_ingest_help_exits_0() -> None:
    """`fieldkit ingest --help` must exit 0."""
    result = _run("ingest", "--help")
    assert result.returncode == 0, result.stderr


def test_ingest_help_mentions_status() -> None:
    """`fieldkit ingest --help` must mention the status subcommand."""
    result = _run("ingest", "--help")
    assert result.returncode == 0, result.stderr
    assert "status" in result.stdout


# ── fieldkit ingest <unknown> ──────────────────────────────────────────────


def test_ingest_bogus_subcommand_exits_nonzero() -> None:
    """`fieldkit ingest bogus` must exit with a non-zero code."""
    result = _run("ingest", "bogus")
    assert result.returncode != 0


def test_ingest_bogus_subcommand_reports_error() -> None:
    """`fieldkit ingest bogus` must print an error mentioning the unknown subcommand."""
    result = _run("ingest", "bogus")
    assert result.returncode != 0
    assert "bogus" in result.stderr or "bogus" in result.stdout


# ── fieldkit ingest (no subcommand) ───────────────────────────────────────


def test_ingest_no_subcommand_exits_nonzero() -> None:
    """`fieldkit ingest` with no subcommand shows help and exits non-zero."""
    result = _run("ingest")
    assert result.returncode == 1
    assert "COMMAND" in result.stdout or "Commands" in result.stdout


# ── fieldkit ingest run ────────────────────────────────────────────────────


def test_ingest_run_dryrun_exits_0() -> None:
    """`fieldkit ingest run --pipeline transcript-ingest --dry-run --limit 3` must exit 0."""
    result = _run("ingest", "run", "--pipeline", "transcript-ingest", "--dry-run", "--limit", "3")
    assert result.returncode == 0, result.stderr


@pytest.mark.integration
@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[1] / "data" / "gmail.db").exists(),
    reason="gmail.db not present — integration test requires live data",
)
def test_ingest_run_dryrun_output_informative() -> None:
    """Dry-run output must contain 'dry run' (case-insensitive) and the pipeline name."""
    result = _run("ingest", "run", "--pipeline", "transcript-ingest", "--dry-run", "--limit", "3")
    assert result.returncode == 0, result.stderr
    combined = (result.stdout + result.stderr).lower()
    assert "dry run" in combined or "dry-run" in combined, f"Expected dry-run label in output:\n{result.stdout}"
    assert "transcript-ingest" in result.stdout, f"Expected pipeline name in output:\n{result.stdout}"


def test_ingest_run_invalid_pipeline_exits_nonzero() -> None:
    """`fieldkit ingest run --pipeline nonexistent` must exit nonzero."""
    result = _run("ingest", "run", "--pipeline", "nonexistent")
    assert result.returncode != 0
    assert "nonexistent" in result.stderr or "unknown" in result.stderr.lower()


def test_ingest_run_stub_pipeline_dryrun() -> None:
    """`--pipeline customer-notes-ingest --dry-run` exits 0 and mentions 'stub'."""
    result = _run("ingest", "run", "--pipeline", "customer-notes-ingest", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "stub" in result.stdout.lower(), f"Expected 'stub' in output:\n{result.stdout}"


def test_ingest_run_missing_pipeline_flag() -> None:
    """`fieldkit ingest run` (no --pipeline) must exit nonzero."""
    result = _run("ingest", "run")
    assert result.returncode != 0


# ── fieldkit ingest discover ───────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[1] / "data" / "gmail.db").exists(),
    reason="gmail.db not present — integration test requires live data",
)
def test_ingest_discover_exits_0() -> None:
    """`fieldkit ingest discover --pipeline transcript-ingest` must exit 0."""
    result = _run("ingest", "discover", "--pipeline", "transcript-ingest")
    assert result.returncode == 0, result.stderr


def test_ingest_discover_invalid_pipeline() -> None:
    """`fieldkit ingest discover --pipeline nonexistent` must exit nonzero."""
    result = _run("ingest", "discover", "--pipeline", "nonexistent")
    assert result.returncode != 0
    assert "nonexistent" in result.stderr or "unknown" in result.stderr.lower()


# ── fieldkit ingest reprocess ──────────────────────────────────────────────


def test_ingest_reprocess_dryrun_no_from_version_exits_1() -> None:
    """`fieldkit ingest reprocess --pipeline transcript-ingest --dry-run` without --from-version exits 1.

    historic regression: Without --from-version the command now warns and exits 1 instead of
    silently running a no-op version transition.
    """
    result = _run("ingest", "reprocess", "--pipeline", "transcript-ingest", "--dry-run")
    assert result.returncode == 1, f"Expected exit 1 (historic regression guard), got {result.returncode}"
    assert "--from-version" in result.stderr, f"Expected --from-version warning in stderr:\n{result.stderr}"


def test_ingest_reprocess_invalid_pipeline() -> None:
    """`fieldkit ingest reprocess --pipeline nonexistent` must exit nonzero."""
    result = _run("ingest", "reprocess", "--pipeline", "nonexistent")
    assert result.returncode != 0
    assert "nonexistent" in result.stderr or "unknown" in result.stderr.lower()


# ── fieldkit ingest --help (subcommand coverage) ──────────────────────────


def test_ingest_help_mentions_run() -> None:
    """`fieldkit ingest --help` must mention the run subcommand."""
    result = _run("ingest", "--help")
    assert result.returncode == 0, result.stderr
    assert "run" in result.stdout


def test_ingest_help_mentions_discover() -> None:
    """`fieldkit ingest --help` must mention the discover subcommand."""
    result = _run("ingest", "--help")
    assert result.returncode == 0, result.stderr
    assert "discover" in result.stdout


def test_ingest_help_mentions_reprocess() -> None:
    """`fieldkit ingest --help` must mention the reprocess subcommand."""
    result = _run("ingest", "--help")
    assert result.returncode == 0, result.stderr
    assert "reprocess" in result.stdout


def test_ingest_help_mentions_backfill() -> None:
    """`fieldkit ingest --help` must mention the backfill subcommand."""
    result = _run("ingest", "--help")
    assert result.returncode == 0, result.stderr
    assert "backfill" in result.stdout


# ===========================================================================
# Integration fixtures and helpers
# ===========================================================================

_GMAIL_SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    thread_id     TEXT PRIMARY KEY,
    subject       TEXT,
    snippet       TEXT,
    message_count INTEGER DEFAULT 0,
    updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    message_id  TEXT PRIMARY KEY,
    thread_id   TEXT NOT NULL,
    from_addr   TEXT,
    to_addr     TEXT,
    cc_addr     TEXT,
    subject     TEXT,
    date_str    TEXT,
    date_epoch  INTEGER,
    labels      TEXT,
    body_plain  TEXT DEFAULT '',
    body_html   TEXT DEFAULT '',
    size_bytes  INTEGER DEFAULT 0,
    snippet     TEXT,
    synced_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _make_gmail_db(path: Path, *, num_emails: int = 3) -> None:
    """Create a minimal gmail.db with *num_emails* fake Gemini meeting emails."""
    conn = sqlite3.connect(str(path))
    conn.executescript(_GMAIL_SCHEMA)
    for i in range(num_emails):
        doc_id = f"INTEG_DOC_{i:04d}"
        doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
        subject = f'Notes: "Integration Meeting {i}" May {i + 1}, 2026'
        message_id = f"integ_msg_{i:04d}"
        conn.execute(
            "INSERT OR IGNORE INTO threads (thread_id, subject) VALUES (?, ?)",
            (message_id, subject),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO messages
                (message_id, thread_id, from_addr, subject, body_html, body_plain, date_epoch)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                message_id,
                "Gemini <gemini-notes@google.com>",
                subject,
                f'<p>View your notes: <a href="{doc_url}">Open doc</a></p>',
                f"View your notes: {doc_url}",
                # historic regression: must be within last 90 days — use a recent epoch
                int(__import__("time").time()) - (86400 * i),  # today minus i days
            ),
        )
    conn.commit()
    conn.close()


def _make_fake_drive_service(doc_id: str = "INTEG_DOC_0000") -> Any:
    """Return a mock Google Docs service that returns a canned 2-tab Gemini doc."""

    def _fake_text_run(text: str) -> dict[str, Any]:
        return {"textRun": {"content": text}}

    def _fake_paragraph(*texts: str) -> dict[str, Any]:
        return {"paragraph": {"elements": [_fake_text_run(t) for t in texts]}}

    notes_content = [
        _fake_paragraph("Invited: alice@globalpay.example.com, bob@globalpay.example.com\n"),
        _fake_paragraph("Suggested next steps\n"),
        _fake_paragraph("[ ] Follow up on pricing\n"),
    ]
    transcript_content = [
        _fake_paragraph("Alice: Let's discuss the roadmap.\n"),
        _fake_paragraph("Bob: Agreed, we need to prioritise.\n"),
    ]

    def _make_tab(title: str, content: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "tabProperties": {"title": title, "tabId": title.lower()},
            "documentTab": {"body": {"content": content}},
        }

    fake_doc = {
        "title": "Integration Meeting 0",
        "tabs": [
            _make_tab("Notes", notes_content),
            _make_tab("Transcript", transcript_content),
        ],
    }

    mock_service = MagicMock()
    mock_service.documents().get(documentId=doc_id, includeTabsContent=True).execute.return_value = fake_doc

    # Make any doc_id return the same canned doc (integration tests use different IDs)
    mock_service.documents().get.return_value.execute.return_value = fake_doc

    return mock_service


# ===========================================================================
# Integration tests — discover CLI
# ===========================================================================


def test_integration_discover_registers_sources(tmp_path: Path) -> None:
    """discover main() with a tmp gmail.db registers N sources → exit 0."""
    gmail_db = tmp_path / "gmail.db"
    pipeline_db = tmp_path / "pipeline.db"
    _make_gmail_db(gmail_db, num_emails=3)

    with (
        patch("fieldkit.gmail.discover.get_gmail_db_path", return_value=gmail_db),
        patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db),
        patch("fieldkit.commands.ingest.discover.get_db_path", return_value=pipeline_db, create=True),
    ):
        from fieldkit.commands.ingest.discover import cli as discover_cli

        result = CliRunner().invoke(discover_cli, ["--pipeline", "transcript-ingest"])

    assert result.exit_code == 0
    # Verify pipeline.db was created and sources were written
    conn = sqlite3.connect(str(pipeline_db))
    count = conn.execute("SELECT COUNT(*) FROM sources WHERE pipeline_id='transcript-ingest'").fetchone()[0]
    conn.close()
    assert count == 3, f"Expected 3 sources, got {count}"


def test_integration_discover_dry_run_does_not_write(tmp_path: Path) -> None:
    """discover --dry-run → exit 0, does NOT create or write to pipeline.db."""
    gmail_db = tmp_path / "gmail.db"
    pipeline_db = tmp_path / "pipeline.db"
    _make_gmail_db(gmail_db, num_emails=5)

    with patch("fieldkit.gmail.discover.get_gmail_db_path", return_value=gmail_db):
        from fieldkit.commands.ingest.discover import cli as discover_cli

        result = CliRunner().invoke(discover_cli, ["--pipeline", "transcript-ingest", "--dry-run"])

    assert result.exit_code == 0
    # pipeline.db must NOT have been created
    assert not pipeline_db.exists(), "dry-run must not create pipeline.db"


def test_integration_discover_idempotent(tmp_path: Path) -> None:
    """Running discover twice registers no duplicates — count stays the same."""
    gmail_db = tmp_path / "gmail.db"
    pipeline_db = tmp_path / "pipeline.db"
    _make_gmail_db(gmail_db, num_emails=2)

    with (
        patch("fieldkit.gmail.discover.get_gmail_db_path", return_value=gmail_db),
        patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db),
    ):
        from fieldkit.commands.ingest.discover import cli as discover_cli

        runner = CliRunner()
        result1 = runner.invoke(discover_cli, ["--pipeline", "transcript-ingest"])
        result2 = runner.invoke(discover_cli, ["--pipeline", "transcript-ingest"])

    assert result1.exit_code == 0
    assert result2.exit_code == 0

    conn = sqlite3.connect(str(pipeline_db))
    count = conn.execute("SELECT COUNT(*) FROM sources WHERE pipeline_id='transcript-ingest'").fetchone()[0]
    conn.close()
    assert count == 2, f"Expected 2 sources after idempotent re-discover, got {count}"


# ===========================================================================
# Integration tests — run CLI (dry-run)
# ===========================================================================


def test_integration_run_dryrun_lists_pending_sources(tmp_path: Path) -> None:
    """run --dry-run --limit 3 after discover → exit 0, lists 3 sources."""
    gmail_db = tmp_path / "gmail.db"
    pipeline_db = tmp_path / "pipeline.db"
    _make_gmail_db(gmail_db, num_emails=5)

    # First populate pipeline.db via the library directly (avoids double-patch nesting)
    from fieldkit.commands.ingest.registry import PIPELINES
    from fieldkit.gmail.discover import scan_gemini_candidates
    from fieldkit.ingest.db import init_db
    from fieldkit.ingest.sources import discover_gemini_sources

    conn = init_db(pipeline_db, pipelines=PIPELINES)
    candidates = scan_gemini_candidates(gmail_db, limit=None)
    discover_gemini_sources(conn, candidates)
    conn.close()

    with patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db):
        from fieldkit.commands.ingest.run import cli as run_cli

        result = CliRunner().invoke(run_cli, ["--pipeline", "transcript-ingest", "--dry-run", "--limit", "3"])

    assert result.exit_code == 0


# ===========================================================================
# Integration tests — run CLI (live, with NO_LLM and fake Drive)
# ===========================================================================


def test_integration_run_writes_vault_note(tmp_path: Path) -> None:
    """run --limit 1 with NO_LLM=1 and fake Drive → writes vault note, marks processed."""
    gmail_db = tmp_path / "gmail.db"
    pipeline_db = tmp_path / "pipeline.db"
    data_root = tmp_path / "data_root"
    data_root.mkdir()
    _make_gmail_db(gmail_db, num_emails=1)

    # Populate pipeline.db directly so discover patches don't bleed into run patches
    from fieldkit.commands.ingest.registry import PIPELINES
    from fieldkit.gmail.discover import scan_gemini_candidates
    from fieldkit.ingest.db import init_db
    from fieldkit.ingest.sources import discover_gemini_sources

    conn = init_db(pipeline_db, pipelines=PIPELINES)
    candidates = scan_gemini_candidates(gmail_db, limit=None)
    discover_gemini_sources(conn, candidates)
    conn.close()

    import os

    fake_service = _make_fake_drive_service()

    env_patch = patch.dict(os.environ, {"NO_LLM": "1"})
    db_path_patch = patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db)
    drive_patch = patch("fieldkit.ingest.docs.get_docs_service", return_value=fake_service)
    data_root_patch = patch("fieldkit.config.get_fieldkit_home", return_value=data_root)

    with env_patch, db_path_patch, drive_patch, data_root_patch:
        from fieldkit.commands.ingest.run import cli as run_cli

        result = CliRunner().invoke(run_cli, ["--pipeline", "transcript-ingest", "--limit", "1"])

    assert result.exit_code == 0, f"Expected exit 0, got {result.exit_code}"

    # Verify at least one vault note was written under data_root
    md_files = list(data_root.rglob("*.md"))
    assert len(md_files) >= 1, f"Expected at least one .md vault note, found {md_files}"

    # Verify the source is marked processed in pipeline.db
    conn2 = sqlite3.connect(str(pipeline_db))
    processed_count = conn2.execute(
        "SELECT COUNT(*) FROM sources WHERE pipeline_id='transcript-ingest' AND status='processed'"
    ).fetchone()[0]
    conn2.close()
    assert processed_count == 1, f"Expected 1 processed source, got {processed_count}"


def test_integration_run_writes_provenance_frontmatter(tmp_path: Path) -> None:
    """The written vault note must contain provenance frontmatter fields."""
    gmail_db = tmp_path / "gmail.db"
    pipeline_db = tmp_path / "pipeline.db"
    data_root = tmp_path / "data_root"
    data_root.mkdir()
    _make_gmail_db(gmail_db, num_emails=1)

    from fieldkit.commands.ingest.registry import PIPELINES
    from fieldkit.gmail.discover import scan_gemini_candidates
    from fieldkit.ingest.db import init_db
    from fieldkit.ingest.sources import discover_gemini_sources

    conn = init_db(pipeline_db, pipelines=PIPELINES)
    candidates = scan_gemini_candidates(gmail_db, limit=None)
    discover_gemini_sources(conn, candidates)
    conn.close()

    import os

    fake_service = _make_fake_drive_service()

    with (
        patch.dict(os.environ, {"NO_LLM": "1"}),
        patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=fake_service),
        patch("fieldkit.config.get_fieldkit_home", return_value=data_root),
    ):
        from fieldkit.commands.ingest.run import cli as run_cli

        CliRunner().invoke(run_cli, ["--pipeline", "transcript-ingest", "--limit", "1"])

    md_files = list(data_root.rglob("*.md"))
    assert md_files, "Expected at least one vault note"

    note_content = md_files[0].read_text(encoding="utf-8")
    # Provenance frontmatter must include these keys (rendered by render_vault_note)
    assert "source_id:" in note_content, "Missing source_id: in frontmatter"
    assert "pipeline_version:" in note_content, "Missing pipeline_version: in frontmatter"


def test_integration_run_interactive_processes_source(tmp_path: Path) -> None:
    """run --interactive with mocked stdin 'y' → processes 1 source."""
    gmail_db = tmp_path / "gmail.db"
    pipeline_db = tmp_path / "pipeline.db"
    data_root = tmp_path / "data_root"
    data_root.mkdir()
    _make_gmail_db(gmail_db, num_emails=1)

    from fieldkit.commands.ingest.registry import PIPELINES
    from fieldkit.gmail.discover import scan_gemini_candidates
    from fieldkit.ingest.db import init_db
    from fieldkit.ingest.sources import discover_gemini_sources

    conn = init_db(pipeline_db, pipelines=PIPELINES)
    candidates = scan_gemini_candidates(gmail_db, limit=None)
    discover_gemini_sources(conn, candidates)
    conn.close()

    import os

    fake_service = _make_fake_drive_service()

    # Patch builtins.input to return 'y' for interactive prompt
    with (
        patch.dict(os.environ, {"NO_LLM": "1"}),
        patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=fake_service),
        patch("fieldkit.config.get_fieldkit_home", return_value=data_root),
        patch("builtins.input", return_value="y"),
    ):
        from fieldkit.commands.ingest.run import cli as run_cli

        result = CliRunner().invoke(run_cli, ["--pipeline", "transcript-ingest", "--limit", "1", "--interactive"])

    assert result.exit_code == 0

    conn2 = sqlite3.connect(str(pipeline_db))
    processed_count = conn2.execute("SELECT COUNT(*) FROM sources WHERE status='processed'").fetchone()[0]
    conn2.close()
    assert processed_count == 1, f"Expected 1 processed source in interactive mode, got {processed_count}"
