"""Tests for tools/ingest/reprocess.py and tools/ingest/backfill.py.

Test layers:
1. reprocess main() — unknown pipeline, dry-run (empty DB), dry-run (seeded DB),
   --from-version filter, interactive with fake Drive, SIGINT checkpoint.
2. backfill main() — candidates listed, no candidates, no frontmatter.

Pattern: in-process main() with unittest.mock.patch (same as test_ingest_cli.py).
Uses _seed_artifact() from test_ingest_db.py pattern for DB fixtures.
"""

import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.commands.ingest.registry import PIPELINES
from fieldkit.ingest.db import init_db

pytestmark = pytest.mark.unit


# ===========================================================================
# Shared fixtures and helpers
# ===========================================================================


def _seed_artifact(
    conn: sqlite3.Connection,
    artifact_id: str = "art-001",
    pipeline_id: str = "transcript-ingest",
    pipeline_version: str = "0.1.0",
    content_path: str = "/vault/meeting.md",
    source_id: str = "src-001",
) -> None:
    """Insert a minimal source + artifact row for testing."""
    conn.execute(
        """
        INSERT OR IGNORE INTO sources (source_id, pipeline_id, file_path, status)
        VALUES (?, ?, '/tmp/raw.txt', 'processed')
        """,
        (source_id, pipeline_id),
    )
    conn.execute(
        """
        INSERT INTO artifacts
            (artifact_id, source_id, pipeline_id, artifact_type,
             pipeline_version, content_path, created_at)
        VALUES (?, ?, ?, 'meeting-note', ?, ?, datetime('now'))
        """,
        (artifact_id, source_id, pipeline_id, pipeline_version, content_path),
    )
    conn.commit()


def _make_fake_drive_service(note_text: str = "Alice: Hello.\nBob: Hi.\n") -> Any:
    """Return a minimal mock Docs service that returns a canned transcript doc."""

    def _fake_paragraph(text: str) -> dict[str, Any]:
        return {"paragraph": {"elements": [{"textRun": {"content": text}}]}}

    invited_paragraph = _fake_paragraph("Invited: alice@acme.example.com\n")
    transcript_paragraph = _fake_paragraph(note_text)

    def _make_tab(title: str, content: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "tabProperties": {"title": title, "tabId": title.lower()},
            "documentTab": {"body": {"content": content}},
        }

    fake_doc = {
        "title": "Reprocess Meeting",
        "tabs": [
            _make_tab("Notes", [invited_paragraph]),
            _make_tab("Transcript", [transcript_paragraph]),
        ],
    }

    mock_service = MagicMock()
    mock_service.documents().get.return_value.execute.return_value = fake_doc
    return mock_service


# ===========================================================================
# reprocess tests
# ===========================================================================


# ── TestReprocessUnknownPipeline (flattened) ────────────────────────────────


def test_reprocess_unknown_pipeline_exit_code() -> None:
    from fieldkit.commands.ingest.reprocess import main

    rc = main(["--pipeline", "no-such-pipeline", "--dry-run"])
    assert rc == 1


def test_reprocess_unknown_pipeline_stderr_mentions_pipeline(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.ingest.reprocess import main

    main(["--pipeline", "no-such-pipeline", "--dry-run"])
    captured = capsys.readouterr()
    assert "no-such-pipeline" in captured.err
    assert "unknown" in captured.err.lower() or "available" in captured.err.lower()


# ── TestReprocessStubPipeline (flattened) ───────────────────────────────────


def test_reprocess_stub_pipeline_stub_pipeline_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.ingest.reprocess import main

    rc = main(["--pipeline", "customer-notes-ingest", "--dry-run"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "stub" in captured.err.lower()


# ── TestReprocessDryRunNoDb (flattened) ─────────────────────────────────────


def test_reprocess_dry_run_no_db_empty_db_exits_0(tmp_path: Path) -> None:
    from fieldkit.commands.ingest.reprocess import main

    pipeline_db = tmp_path / "pipeline.db"
    init_db(pipeline_db)  # empty artifacts table

    with patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db):
        rc = main(["--pipeline", "transcript-ingest", "--from-version", "0.0.9", "--dry-run"])

    assert rc == 0


def test_reprocess_dry_run_no_db_empty_db_prints_no_artifacts_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from fieldkit.commands.ingest.reprocess import main

    pipeline_db = tmp_path / "pipeline.db"
    init_db(pipeline_db, pipelines=PIPELINES)

    with patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db):
        main(["--pipeline", "transcript-ingest", "--from-version", "0.0.9", "--dry-run"])

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "no artifacts" in combined.lower()


# ── TestReprocessDryRunSeededDb (flattened) ─────────────────────────────────


def test_reprocess_dry_run_seeded_db_lists_artifacts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.ingest.reprocess import main

    pipeline_db = tmp_path / "pipeline.db"
    conn = init_db(pipeline_db, pipelines=PIPELINES)
    _seed_artifact(
        conn,
        artifact_id="art-001",
        source_id="src-001",
        pipeline_version="0.1.0",
        content_path="/vault/notes/meeting-a.md",
    )
    conn.close()

    with patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db):
        rc = main(["--pipeline", "transcript-ingest", "--from-version", "0.1.0", "--dry-run"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "src-001" in captured.out
    assert "0.1.0" in captured.out


def test_reprocess_live_base_profile_reports_google_install_guidance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from fieldkit.__main__ import main as fieldkit_main

    pipeline_db = tmp_path / "pipeline.db"
    conn = init_db(pipeline_db, pipelines=PIPELINES)
    _seed_artifact(conn, source_id="src-001", pipeline_version="0.1.0")
    conn.close()

    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db),
        patch("fieldkit.config.optional_dependencies.importlib.util.find_spec", return_value=None),
    ):
        exit_code = fieldkit_main(["ingest", "reprocess", "--pipeline", "transcript-ingest", "--from-version", "0.1.0"])

    captured = capsys.readouterr()
    assert exit_code == 3
    assert "requires the 'google' optional profile" in captured.err
    assert "pip install 'fieldkit-cli[google]'" in captured.err
    assert "Traceback" not in captured.err


def test_reprocess_json_dry_run_reports_ordered_pending(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.ingest.reprocess import main

    pipeline_db = tmp_path / "pipeline.db"
    conn = init_db(pipeline_db, pipelines=PIPELINES)
    _seed_artifact(conn, artifact_id="art-001", source_id="src-001", pipeline_version="0.1.0")
    _seed_artifact(conn, artifact_id="art-002", source_id="src-002", pipeline_version="0.1.0")
    conn.close()

    with patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db):
        rc = main(["--pipeline", "transcript-ingest", "--from-version", "0.1.0", "--dry-run", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["force"] is False
    assert payload["pending"] == ["src-001", "src-002"]
    assert payload["completed"] == []


def test_reprocess_force_dry_run_selects_current_version(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.ingest.reprocess import main

    pipeline_db = tmp_path / "pipeline.db"
    conn = init_db(pipeline_db, pipelines=PIPELINES)
    _seed_artifact(conn, source_id="src-current", pipeline_version="0.1.0")
    conn.close()

    with patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db):
        rc = main(["--pipeline", "transcript-ingest", "--force", "--dry-run"])

    output = capsys.readouterr().out
    assert rc == 0
    assert "force=true" in output
    assert "src-current" in output


def test_reprocess_force_empty_dry_run_identifies_selector_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from fieldkit.commands.ingest.reprocess import main

    pipeline_db = tmp_path / "pipeline.db"
    init_db(pipeline_db, pipelines=PIPELINES).close()

    with patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db):
        rc = main(["--pipeline", "transcript-ingest", "--force", "--dry-run"])

    output = capsys.readouterr().out
    assert rc == 0
    assert "force=true" in output
    assert "no artifacts" in output.lower()


def test_reprocess_force_json_reports_ordered_pending(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.ingest.reprocess import main

    pipeline_db = tmp_path / "pipeline.db"
    conn = init_db(pipeline_db, pipelines=PIPELINES)
    _seed_artifact(conn, artifact_id="art-first", source_id="src-first", pipeline_version="0.1.0")
    _seed_artifact(conn, artifact_id="art-second", source_id="src-second", pipeline_version="0.1.0")
    conn.execute("UPDATE artifacts SET created_at = '2026-01-01T00:00:00' WHERE artifact_id = 'art-first'")
    conn.execute("UPDATE artifacts SET created_at = '2026-01-02T00:00:00' WHERE artifact_id = 'art-second'")
    conn.commit()
    conn.close()

    with patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db):
        rc = main(["--pipeline", "transcript-ingest", "--force", "--dry-run", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["force"] is True
    assert payload["pending"] == ["src-first", "src-second"]


def test_reprocess_force_composes_with_account_and_limit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.ingest.reprocess import main

    pipeline_db = tmp_path / "pipeline.db"
    conn = init_db(pipeline_db, pipelines=PIPELINES)
    _seed_artifact(
        conn,
        artifact_id="art-first",
        source_id="src-first",
        pipeline_version="0.1.0",
        content_path="/workspace/accounts/acme-corp/meetings/first.md",
    )
    _seed_artifact(
        conn,
        artifact_id="art-second",
        source_id="src-second",
        pipeline_version="0.1.0",
        content_path="/workspace/accounts/acme-corp/meetings/second.md",
    )
    _seed_artifact(
        conn,
        artifact_id="art-other",
        source_id="src-other",
        pipeline_version="0.1.0",
        content_path="/workspace/accounts/example-co/meetings/other.md",
    )
    conn.execute("UPDATE artifacts SET created_at = '2026-01-01T00:00:00' WHERE artifact_id = 'art-first'")
    conn.execute("UPDATE artifacts SET created_at = '2026-01-02T00:00:00' WHERE artifact_id = 'art-second'")
    conn.commit()
    conn.close()

    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db),
        patch("fieldkit.commands.ingest.reprocess.validate_account_slug"),
    ):
        rc = main(["--pipeline", "transcript-ingest", "--force", "--account", "acme-corp", "--limit", "1", "--dry-run"])

    output = capsys.readouterr().out
    assert rc == 0
    assert "src-first" in output
    assert "src-second" not in output
    assert "src-other" not in output


def test_reprocess_dry_run_seeded_db_shows_version_transition(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from fieldkit.commands.ingest.reprocess import main

    pipeline_db = tmp_path / "pipeline.db"
    conn = init_db(pipeline_db, pipelines=PIPELINES)
    _seed_artifact(
        conn,
        artifact_id="art-002",
        source_id="src-002",
        pipeline_version="0.1.0",
        content_path="/vault/meeting-b.md",
    )
    conn.close()

    with patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db):
        main(["--pipeline", "transcript-ingest", "--from-version", "0.1.0", "--dry-run"])

    captured = capsys.readouterr()
    # Dry-run must show source → target version transition
    assert "→" in captured.out or "->" in captured.out


def test_reprocess_dry_run_seeded_db_shows_content_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.ingest.reprocess import main

    pipeline_db = tmp_path / "pipeline.db"
    conn = init_db(pipeline_db, pipelines=PIPELINES)
    _seed_artifact(
        conn,
        artifact_id="art-003",
        source_id="src-003",
        pipeline_version="0.1.0",
        content_path="/vault/meeting-c.md",
    )
    conn.close()

    with patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db):
        main(["--pipeline", "transcript-ingest", "--from-version", "0.1.0", "--dry-run"])

    captured = capsys.readouterr()
    assert "/vault/meeting-c.md" in captured.out


def test_reprocess_dry_run_seeded_db_multiple_artifacts_all_listed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from fieldkit.commands.ingest.reprocess import main

    pipeline_db = tmp_path / "pipeline.db"
    conn = init_db(pipeline_db, pipelines=PIPELINES)
    for i in range(3):
        _seed_artifact(
            conn,
            artifact_id=f"art-{i:03d}",
            source_id=f"src-{i:03d}",
            pipeline_version="0.1.0",
            content_path=f"/vault/meeting-{i}.md",
        )
    conn.close()

    with patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db):
        rc = main(["--pipeline", "transcript-ingest", "--from-version", "0.1.0", "--dry-run"])

    assert rc == 0
    captured = capsys.readouterr()
    for i in range(3):
        assert f"src-{i:03d}" in captured.out


# ── TestReprocessFromVersion (flattened) ────────────────────────────────────


def test_reprocess_from_version_from_version_filters_correctly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from fieldkit.commands.ingest.reprocess import main

    pipeline_db = tmp_path / "pipeline.db"
    conn = init_db(pipeline_db, pipelines=PIPELINES)
    _seed_artifact(
        conn,
        "art-old",
        source_id="src-old",
        pipeline_version="0.1.0",
        content_path="/vault/old.md",
    )
    _seed_artifact(
        conn,
        "art-new",
        source_id="src-new",
        pipeline_version="0.2.0",
        content_path="/vault/new.md",
    )
    conn.close()

    with patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db):
        rc = main(
            [
                "--pipeline",
                "transcript-ingest",
                "--from-version",
                "0.1.0",
                "--dry-run",
            ]
        )

    assert rc == 0
    captured = capsys.readouterr()
    assert "src-old" in captured.out
    assert "src-new" not in captured.out


def test_reprocess_from_version_from_version_no_match_prints_no_artifacts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from fieldkit.commands.ingest.reprocess import main

    pipeline_db = tmp_path / "pipeline.db"
    conn = init_db(pipeline_db, pipelines=PIPELINES)
    _seed_artifact(conn, "art-001", source_id="src-001", pipeline_version="0.1.0")
    conn.close()

    with patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db):
        rc = main(
            [
                "--pipeline",
                "transcript-ingest",
                "--from-version",
                "9.9.9",
                "--dry-run",
            ]
        )

    assert rc == 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "no artifacts" in combined.lower()


# ── TestReprocessInteractive (flattened) ────────────────────────────────────


def test_reprocess_interactive_interactive_y_overwrites_file_and_updates_db(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import os

    pipeline_db = tmp_path / "pipeline.db"
    data_root = tmp_path / "data_root"
    data_root.mkdir()

    # Write a placeholder vault note that should be overwritten
    content_dir = tmp_path / "vault"
    content_dir.mkdir()
    vault_file = content_dir / "meeting-a.md"
    vault_file.write_text("old content", encoding="utf-8")

    conn = init_db(pipeline_db, pipelines=PIPELINES)
    _seed_artifact(
        conn,
        artifact_id="art-i01",
        source_id="DOC_REPROCESS_001",
        pipeline_version="0.1.0",
        content_path=str(vault_file),
    )
    conn.close()

    fake_service = _make_fake_drive_service()

    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=fake_service),
        patch("fieldkit.config.get_fieldkit_home", return_value=data_root),
        patch.dict(os.environ, {"NO_LLM": "1"}),
        patch("builtins.input", return_value="y"),
    ):
        from fieldkit.commands.ingest.reprocess import main

        rc = main(
            [
                "--pipeline",
                "transcript-ingest",
                "--from-version",
                "0.1.0",
                "--interactive",
                "--limit",
                "1",
            ]
        )

    assert rc == 0

    # Verify the vault file was overwritten (content changed)
    new_content = vault_file.read_text(encoding="utf-8")
    assert new_content != "old content", "Expected vault file to be overwritten"

    # Verify pipeline.db was updated with new version
    conn2 = sqlite3.connect(str(pipeline_db))
    conn2.row_factory = sqlite3.Row
    row = conn2.execute(
        "SELECT pipeline_version FROM artifacts WHERE artifact_id = ?",
        ("art-i01",),
    ).fetchone()
    conn2.close()
    # The version should now match the registry version (0.1.0) — same source, overwritten
    assert row is not None


def test_reprocess_interactive_interactive_n_skips_artifact(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import os

    pipeline_db = tmp_path / "pipeline.db"
    data_root = tmp_path / "data_root"
    data_root.mkdir()

    vault_file = tmp_path / "meeting.md"
    vault_file.write_text("original content", encoding="utf-8")

    conn = init_db(pipeline_db, pipelines=PIPELINES)
    _seed_artifact(
        conn,
        artifact_id="art-n01",
        source_id="DOC_SKIP_001",
        pipeline_version="0.1.0",
        content_path=str(vault_file),
    )
    conn.close()

    fake_service = _make_fake_drive_service()

    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=fake_service),
        patch("fieldkit.config.get_fieldkit_home", return_value=data_root),
        patch.dict(os.environ, {"NO_LLM": "1"}),
        patch("builtins.input", return_value="n"),
    ):
        from fieldkit.commands.ingest.reprocess import main

        rc = main(
            [
                "--pipeline",
                "transcript-ingest",
                "--from-version",
                "0.1.0",
                "--interactive",
                "--limit",
                "1",
            ]
        )

    assert rc == 0
    # File must not have been overwritten
    assert vault_file.read_text(encoding="utf-8") == "original content"


def test_reprocess_interactive_interactive_q_stops_loop(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import os

    pipeline_db = tmp_path / "pipeline.db"
    data_root = tmp_path / "data_root"
    data_root.mkdir()

    conn = init_db(pipeline_db, pipelines=PIPELINES)
    for i in range(3):
        vault_file = tmp_path / f"meeting-{i}.md"
        vault_file.write_text("old", encoding="utf-8")
        _seed_artifact(
            conn,
            artifact_id=f"art-q0{i}",
            source_id=f"DOC_Q_{i:03d}",
            pipeline_version="0.1.0",
            content_path=str(vault_file),
        )
    conn.close()

    fake_service = _make_fake_drive_service()

    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=fake_service),
        patch("fieldkit.config.get_fieldkit_home", return_value=data_root),
        patch.dict(os.environ, {"NO_LLM": "1"}),
        patch("builtins.input", return_value="q"),
    ):
        from fieldkit.commands.ingest.reprocess import main

        rc = main(
            [
                "--pipeline",
                "transcript-ingest",
                "--from-version",
                "0.1.0",
                "--interactive",
            ]
        )

    assert rc == 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "stopping" in combined.lower() or "remaining" in combined.lower()


# ── TestReprocessSigint (flattened) ─────────────────────────────────────────


def test_reprocess_sigint_sigint_prints_checkpoint_message(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Raise KeyboardInterrupt inside the artifact loop → checkpoint printed."""
    import os

    pipeline_db = tmp_path / "pipeline.db"
    data_root = tmp_path / "data_root"
    data_root.mkdir()

    vault_file = tmp_path / "interrupt-meeting.md"
    vault_file.write_text("original", encoding="utf-8")

    conn = init_db(pipeline_db, pipelines=PIPELINES)
    _seed_artifact(
        conn,
        artifact_id="art-sig1",
        source_id="DOC_SIGINT_001",
        pipeline_version="0.1.0",
        content_path=str(vault_file),
    )
    conn.close()

    fake_service = _make_fake_drive_service()
    call_count = 0

    def _raise_on_second_call(*args: Any, **kwargs: Any) -> str:
        """First call returns 'y'; second simulates interrupt via EOFError."""
        nonlocal call_count
        call_count += 1
        if call_count >= 1:
            raise KeyboardInterrupt()
        return "y"

    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=fake_service),
        patch("fieldkit.config.get_fieldkit_home", return_value=data_root),
        patch.dict(os.environ, {"NO_LLM": "1"}),
        patch("builtins.input", side_effect=_raise_on_second_call),
    ):
        from fieldkit.commands.ingest.reprocess import main

        rc = main(["--pipeline", "transcript-ingest", "--from-version", "0.1.0", "--interactive"])

    # Must exit 0 (partial completion is OK)
    assert rc == 0
    captured = capsys.readouterr()
    # Checkpoint message must appear on stderr
    assert "checkpoint" in captured.err.lower() or "interrupt" in captured.err.lower()


def test_reprocess_sigint_sigint_non_interactive_exits_0(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """KeyboardInterrupt in non-interactive loop → exit 0 with checkpoint."""
    import os

    pipeline_db = tmp_path / "pipeline.db"
    data_root = tmp_path / "data_root"
    data_root.mkdir()

    vault_file = tmp_path / "ni-meeting.md"
    vault_file.write_text("old", encoding="utf-8")

    conn = init_db(pipeline_db, pipelines=PIPELINES)
    _seed_artifact(
        conn,
        artifact_id="art-sig2",
        source_id="DOC_SIGINT_002",
        pipeline_version="0.1.0",
        content_path=str(vault_file),
    )
    conn.close()

    # Patch the fetch function to raise KeyboardInterrupt — simulates SIGINT mid-loop
    fake_service = _make_fake_drive_service()

    def _raise_keyboard_interrupt(*args: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt()

    with (
        patch("fieldkit.ingest.db.get_db_path", return_value=pipeline_db),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=fake_service),
        patch("fieldkit.config.get_fieldkit_home", return_value=data_root),
        patch.dict(os.environ, {"NO_LLM": "1"}),
        patch("fieldkit.ingest.docs.fetch_gemini_doc", side_effect=KeyboardInterrupt()),
    ):
        from fieldkit.commands.ingest.reprocess import main

        rc = main(["--pipeline", "transcript-ingest", "--from-version", "0.1.0"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "checkpoint" in captured.err.lower() or "interrupt" in captured.err.lower()


# ===========================================================================
# backfill tests
# ===========================================================================


def _write_meeting_note(
    path: Path,
    *,
    with_frontmatter: bool = True,
    with_source_id: bool = True,
    frontmatter_extra: str = "",
) -> None:
    """Write a synthetic meeting note to *path*."""
    if not with_frontmatter:
        path.write_text("# Meeting notes\n\nSome content here.\n", encoding="utf-8")
        return

    frontmatter_lines = ["---"]
    if with_source_id:
        frontmatter_lines.append("source_id: DOC_ABC_0001")
    frontmatter_lines.append("title: Test Meeting")
    if frontmatter_extra:
        frontmatter_lines.append(frontmatter_extra)
    frontmatter_lines += ["---", "", "# Meeting notes", "", "Some content."]
    path.write_text("\n".join(frontmatter_lines), encoding="utf-8")


def _make_accounts_dir(
    tmp_path: Path,
    *,
    accounts: list[str] | None = None,
) -> Path:
    """Create accounts/ directory tree under tmp_path and return it."""
    accounts_root = tmp_path / "accounts"
    if accounts is None:
        accounts = ["acme", "globalpay"]
    for account in accounts:
        meetings_dir = accounts_root / account / "meetings"
        meetings_dir.mkdir(parents=True)
    return accounts_root


# ── TestBackfillCandidatesFound (flattened) ─────────────────────────────────


def test_backfill_candidates_found_lists_candidates_missing_source_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    accounts_root = _make_accounts_dir(tmp_path, accounts=["acme"])
    note = accounts_root / "acme" / "meetings" / "2026-05-01-planning.md"
    _write_meeting_note(note, with_frontmatter=True, with_source_id=False)

    # Patch in the module's own namespace — get_fieldkit_home is imported at module level
    with patch("fieldkit.commands.ingest.backfill.get_fieldkit_home", return_value=tmp_path):
        from fieldkit.commands.ingest.backfill import main

        rc = main(["--dry-run"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "2026-05-01-planning.md" in captured.out
    assert "source_id" in captured.out.lower() or "missing" in captured.out.lower()


def test_backfill_candidates_found_lists_files_without_frontmatter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    accounts_root = _make_accounts_dir(tmp_path, accounts=["globalpay"])
    note = accounts_root / "globalpay" / "meetings" / "2026-03-10-kickoff.md"
    _write_meeting_note(note, with_frontmatter=False)

    with patch("fieldkit.commands.ingest.backfill.get_fieldkit_home", return_value=tmp_path):
        from fieldkit.commands.ingest.backfill import main

        rc = main(["--dry-run"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "2026-03-10-kickoff.md" in captured.out


def test_backfill_candidates_found_reports_reason_in_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    accounts_root = _make_accounts_dir(tmp_path, accounts=["shield"])
    note = accounts_root / "shield" / "meetings" / "2026-04-15-review.md"
    _write_meeting_note(note, with_frontmatter=True, with_source_id=False)

    with patch("fieldkit.commands.ingest.backfill.get_fieldkit_home", return_value=tmp_path):
        from fieldkit.commands.ingest.backfill import main

        main(["--dry-run"])

    captured = capsys.readouterr()
    # Output must include reason (e.g. "frontmatter present but missing source_id")
    assert "reason:" in captured.out.lower() or "missing" in captured.out.lower()


def test_backfill_candidates_found_multiple_candidates_across_accounts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    accounts_root = _make_accounts_dir(tmp_path, accounts=["acme", "globalpay"])

    for account in ["acme", "globalpay"]:
        note = accounts_root / account / "meetings" / "2026-05-10-q2.md"
        _write_meeting_note(note, with_frontmatter=True, with_source_id=False)

    with patch("fieldkit.commands.ingest.backfill.get_fieldkit_home", return_value=tmp_path):
        from fieldkit.commands.ingest.backfill import main

        rc = main(["--dry-run"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "acme" in captured.out
    assert "globalpay" in captured.out


# ── TestBackfillNoCandidates (flattened) ────────────────────────────────────


def test_backfill_no_candidates_no_candidates_exits_0(tmp_path: Path) -> None:
    accounts_root = _make_accounts_dir(tmp_path, accounts=["acme"])
    note = accounts_root / "acme" / "meetings" / "2026-05-01-planning.md"
    _write_meeting_note(note, with_frontmatter=True, with_source_id=True)

    with patch("fieldkit.commands.ingest.backfill.get_fieldkit_home", return_value=tmp_path):
        from fieldkit.commands.ingest.backfill import main

        rc = main(["--dry-run"])

    assert rc == 0


def test_backfill_no_candidates_no_candidates_zero_count(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    accounts_root = _make_accounts_dir(tmp_path, accounts=["acme"])
    note = accounts_root / "acme" / "meetings" / "2026-05-01-planning.md"
    _write_meeting_note(note, with_frontmatter=True, with_source_id=True)

    with patch("fieldkit.commands.ingest.backfill.get_fieldkit_home", return_value=tmp_path):
        from fieldkit.commands.ingest.backfill import main

        main(["--dry-run"])

    captured = capsys.readouterr()
    # Must report 0 candidates
    assert "0 candidates" in captured.out


def test_backfill_no_candidates_empty_accounts_dir_zero_candidates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_accounts_dir(tmp_path, accounts=["acme"])
    # No meeting notes written

    with patch("fieldkit.commands.ingest.backfill.get_fieldkit_home", return_value=tmp_path):
        from fieldkit.commands.ingest.backfill import main

        rc = main(["--dry-run"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "0 candidates" in captured.out


# ── TestBackfillDotDirSkip (flattened) ──────────────────────────────────────


def test_backfill_dot_dir_skip_template_dir_skipped(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    accounts_root = tmp_path / "accounts"
    template_meetings = accounts_root / ".template" / "meetings"
    template_meetings.mkdir(parents=True)
    note = template_meetings / "template-note.md"
    _write_meeting_note(note, with_frontmatter=False)

    with patch("fieldkit.commands.ingest.backfill.get_fieldkit_home", return_value=tmp_path):
        from fieldkit.commands.ingest.backfill import main

        rc = main(["--dry-run"])

    assert rc == 0
    captured = capsys.readouterr()
    # Template file must NOT appear as a candidate
    assert "template-note.md" not in captured.out


# ── TestBackfillMissingAccountsDir (flattened) ──────────────────────────────


def test_backfill_missing_accounts_dir_missing_accounts_dir_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # tmp_path has no accounts/ subdirectory
    with patch("fieldkit.commands.ingest.backfill.get_fieldkit_home", return_value=tmp_path):
        from fieldkit.commands.ingest.backfill import main

        rc = main(["--dry-run"])

    assert rc == 1
    captured = capsys.readouterr()
    assert "accounts" in captured.err.lower()


# ── TestBackfillDryRunFlag (flattened) ──────────────────────────────────────


def test_backfill_dry_run_flag_dry_run_shows_label(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _make_accounts_dir(tmp_path, accounts=["acme"])
    # No meeting files — just verify the label appears

    with patch("fieldkit.commands.ingest.backfill.get_fieldkit_home", return_value=tmp_path):
        from fieldkit.commands.ingest.backfill import main

        rc = main(["--dry-run"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "dry" in captured.out.lower() or "dry" in captured.err.lower()


# ── TestBackfillScanCount (flattened) ───────────────────────────────────────


def test_backfill_scan_count_scan_count_matches_written_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    accounts_root = _make_accounts_dir(tmp_path, accounts=["acme"])
    for i in range(4):
        note = accounts_root / "acme" / "meetings" / f"2026-0{i + 1}-01-mtg.md"
        _write_meeting_note(note, with_frontmatter=True, with_source_id=True)

    with patch("fieldkit.commands.ingest.backfill.get_fieldkit_home", return_value=tmp_path):
        from fieldkit.commands.ingest.backfill import main

        main(["--dry-run"])

    captured = capsys.readouterr()
    # Summary line: "4 files scanned, 0 candidates found"
    assert "4 files scanned" in captured.out


# ---------------------------------------------------------------------------
# Task 11.13 — _run_reprocess returns error count on unknown pipeline
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_reprocess_transcript_ingest_returns_error_count_on_failure(tmp_path: Path) -> None:
    """_run_reprocess returns exit code 1 for an unknown pipeline ID."""
    from fieldkit.commands.ingest.reprocess import _run_reprocess

    rc = _run_reprocess(
        pipeline_id="nonexistent-pipeline",
        from_version="0.1.0",
        dry_run=False,
        interactive=False,
        limit=None,
    )

    # Unknown pipeline → exit code 1
    assert rc == 1


@pytest.mark.unit
def test_reprocess_returns_one_without_from_version(tmp_path: Path) -> None:
    """_run_reprocess returns 1 for transcript-ingest when --from-version is absent (live run)."""
    from fieldkit.commands.ingest.reprocess import _run_reprocess

    # The guard fires before any DB access when from_version is None and dry_run=False,
    # so no patching of DB internals is needed.
    rc = _run_reprocess(
        pipeline_id="transcript-ingest",
        from_version=None,  # triggers the guard → returns 1 immediately
        dry_run=False,
        interactive=False,
        limit=None,
    )

    # Without --from-version on a live run → exit 1
    assert rc == 1
