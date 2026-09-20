"""Tests for fieldkit.ingest.run — _run_processing_loop, _process_one_source,
_sync_action_items_to_tasks branch coverage."""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.commands.ingest.run import (
    _dynamic_worker_count,
    _ProcessResult,
)
from fieldkit.ingest.pipeline import infer_meeting_date
from fieldkit.ingest.sources import SourceRecord
from fieldkit.ingest.writeback import (
    MeetingWriteback,
    WritebackNotice,
    _append_meeting_to_pursuits,
    _sync_action_items_to_tasks,
    apply_meeting_writebacks,
)
from fieldkit.pursuit.io import write_frontmatter_raw

pytestmark = pytest.mark.unit


def test_apply_meeting_writebacks_coordinates_pursuit_and_task_writes(tmp_path: Path) -> None:
    """The completed-meeting use case performs both writes and returns task notices."""
    request = MeetingWriteback(
        pursuits=("deal-a",),
        action_items=("Follow up",),
        account="acme",
        meeting_date="2026-06-19",
        meeting_title="Standup",
        data_root=tmp_path,
        vault_path=tmp_path / "meeting.md",
    )
    notice = WritebackNotice("  Tasks: +1 active → TASKS.md")

    with (
        patch("fieldkit.ingest.writeback._append_meeting_to_pursuits") as append_meeting,
        patch("fieldkit.ingest.writeback._sync_action_items_to_tasks", return_value=(notice,)) as sync_tasks,
    ):
        notices = apply_meeting_writebacks(request)

    append_meeting.assert_called_once()
    sync_tasks.assert_called_once()
    assert notices == (notice,)


def _make_source(source_id: str, title: str, *, dated: bool = True) -> SourceRecord:
    return SourceRecord(
        source_id=source_id,
        pipeline_id="transcript-ingest",
        subject=title,
        meeting_title=title,
        meeting_date=datetime(2026, 6, 19, tzinfo=UTC) if dated else None,
        doc_url=f"https://docs.google.com/document/d/{source_id}",
        email_message_id=f"msg-{source_id}",
        discovered_at="2026-06-19T00:00:00Z",
    )


def test_run_json_dry_run_reports_ordered_pending(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from fieldkit.commands.ingest._output import json_output
    from fieldkit.commands.ingest.run import _run_transcript_ingest

    sources = [
        SimpleNamespace(source_id="src-002", meeting_date=None, meeting_title="Second"),
        SimpleNamespace(source_id="src-001", meeting_date=None, meeting_title="First"),
    ]
    conn = MagicMock()
    with (
        json_output(True),
        patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
        patch("fieldkit.ingest.db.init_db", return_value=conn),
        patch("fieldkit.gmail.discover.get_gmail_db_path", return_value=tmp_path / "gmail.db"),
        patch("fieldkit.gmail.discover.scan_gemini_candidates", return_value=[]),
        patch("fieldkit.ingest.sources.discover_gemini_sources", return_value=[]),
        patch("fieldkit.ingest.sources.get_pending_sources", return_value=sources),
    ):
        rc = _run_transcript_ingest(
            spec=SimpleNamespace(version="0.1.0"), dry_run=True, limit=None, interactive=False, as_json=True
        )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["pending"] == ["src-002", "src-001"]
    conn.close.assert_called_once_with()


@pytest.mark.parametrize(
    ("argv", "runner_target"),
    [
        (
            ["ingest", "run", "--pipeline", "transcript-ingest", "--interactive", "--json"],
            "fieldkit.commands.ingest.run._run_run",
        ),
        (
            ["ingest", "reprocess", "--pipeline", "transcript-ingest", "--interactive", "--json"],
            "fieldkit.commands.ingest.reprocess._run_reprocess",
        ),
    ],
)
def test_ingest_json_rejects_interactive_before_runner(argv: list[str], runner_target: str) -> None:
    from fieldkit.__main__ import main

    with patch(runner_target) as mock_runner:
        exit_code = main(argv)

    assert exit_code == 3
    mock_runner.assert_not_called()


# ---------------------------------------------------------------------------
# infer_meeting_date  (deduplicated from the CLI-layer parser in historic regression)
# ---------------------------------------------------------------------------


def test_infer_meeting_date_slash_format() -> None:
    assert infer_meeting_date("Meeting - 2026/05/26 14:31 EDT") == "2026-05-26"


def test_infer_meeting_date_dash_format() -> None:
    assert infer_meeting_date("Call - 2026-01-15 09:00") == "2026-01-15"


def test_infer_meeting_date_long_form() -> None:
    """The format the pipeline-side parser knew, before the two were merged."""
    assert infer_meeting_date("Quarterly Review - May 26, 2026") == "2026-05-26"


@pytest.mark.parametrize(
    "title",
    ["No date in this title", "", "Weekly Standup", "Bad date 2026/13/45"],
    ids=["no-date", "empty", "plain-title", "impossible-date"],
)
def test_infer_meeting_date_returns_none_when_absent(title: str) -> None:
    """None, not today.

    Returning today made "this title has no date" indistinguishable from "this meeting
    was today", which is what let ``ingest reprocess`` silently re-stamp historical
    notes (historic regression). The caller now decides what an absent date means.
    """
    assert infer_meeting_date(title) is None


# ---------------------------------------------------------------------------
# _dynamic_worker_count
# ---------------------------------------------------------------------------


# ── TestDynamicWorkerCount (flattened) ──────────────────────────────────────


def test_dynamic_worker_count_small_queue_one_worker() -> None:
    assert _dynamic_worker_count(1) == 1


def test_dynamic_worker_count_five_items_one_worker() -> None:
    assert _dynamic_worker_count(5) == 1


def test_dynamic_worker_count_six_items_two_workers() -> None:
    # 6 * 3 / 15 = 1.2, ceil = 2
    assert _dynamic_worker_count(6) == 2


def test_dynamic_worker_count_capped_at_max() -> None:
    # Very large queue — should be capped at 8
    assert _dynamic_worker_count(1000) == 8


def test_dynamic_worker_count_zero_items() -> None:
    # 0 items — should return 1 (minimum)
    assert _dynamic_worker_count(0) == 1


# ---------------------------------------------------------------------------
# _append_meeting_to_pursuits
# ---------------------------------------------------------------------------


# ── TestAppendMeetingToPursuits (flattened) ─────────────────────────────────


def test_append_meeting_to_pursuits_skips_missing_pursuit(tmp_path: Path) -> None:
    """Missing pursuit file is silently skipped."""
    _append_meeting_to_pursuits(
        pursuits=["nonexistent-deal"],
        account="acme",
        meeting_date="2026-06-19",
        meeting_title="Sync",
        vault_path=tmp_path / "meetings" / "note.md",
        data_root=tmp_path,
    )
    # No error raised


def test_append_meeting_to_pursuits_appends_to_existing_activity_log(tmp_path: Path) -> None:
    pursuit_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    pursuit_file = pursuit_dir / "my-deal.md"
    pursuit_file.write_text("---\nstage: discover\n---\n\n## Activity Log\n", encoding="utf-8")

    vault_path = tmp_path / "accounts" / "acme" / "meetings" / "note.md"
    vault_path.parent.mkdir(parents=True, exist_ok=True)
    vault_path.write_text("# Meeting Note", encoding="utf-8")

    with patch("fieldkit.ingest.writeback.write_frontmatter_raw", wraps=write_frontmatter_raw) as writer:
        _append_meeting_to_pursuits(
            pursuits=["my-deal"],
            account="acme",
            meeting_date="2026-06-19",
            meeting_title="Q2 Sync",
            vault_path=vault_path,
            data_root=tmp_path,
        )

    content = pursuit_file.read_text(encoding="utf-8")
    assert "Q2 Sync" in content
    assert "2026-06-19" in content
    assert writer.call_count == 1
    assert writer.call_args.args[0] == pursuit_file
    assert writer.call_args.kwargs["expected_mtime"] is not None


def test_append_meeting_to_pursuit_canonicalizes_historical_qualification(tmp_path: Path) -> None:
    """Meeting ingestion canonicalizes historical qualification state."""
    pursuit_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    pursuit_file = pursuit_dir / "deal.md"
    pursuit_file.write_text(
        "---\nstage: discover\nmeddpicc:\n  champion: 3\n---\n\n## Activity Log\n",
        encoding="utf-8",
    )
    vault_path = tmp_path / "accounts" / "acme" / "meetings" / "note.md"
    vault_path.parent.mkdir(parents=True)
    vault_path.write_text("# Meeting Note", encoding="utf-8")

    _append_meeting_to_pursuits(
        pursuits=["deal"],
        account="acme",
        meeting_date="2026-06-19",
        meeting_title="Q2 Sync",
        vault_path=vault_path,
        data_root=tmp_path,
    )

    content = pursuit_file.read_text(encoding="utf-8")
    assert "\nmeddpicc:" not in content
    assert "legacy_meddpicc:" in content
    assert "champion: 3" in content
    assert "Q2 Sync" in content


def test_append_meeting_to_pursuit_rejects_missing_frontmatter(tmp_path: Path) -> None:
    """Meeting ingestion refuses pursuits without valid frontmatter."""
    from fieldkit.errors import FieldkitError

    pursuit_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    pursuit_file = pursuit_dir / "deal.md"
    original = "# Deal without frontmatter\n"
    pursuit_file.write_text(original, encoding="utf-8")

    with pytest.raises(FieldkitError, match="without valid frontmatter"):
        _append_meeting_to_pursuits(
            pursuits=["deal"],
            account="acme",
            meeting_date="2026-06-19",
            meeting_title="Q2 Sync",
            vault_path=tmp_path / "accounts" / "acme" / "meetings" / "note.md",
            data_root=tmp_path,
        )

    assert pursuit_file.read_text(encoding="utf-8") == original


def test_append_meeting_to_pursuits_creates_activity_log_when_absent(tmp_path: Path) -> None:
    pursuit_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    pursuit_file = pursuit_dir / "new-deal.md"
    pursuit_file.write_text("---\nstage: discover\n---\n\n# Deal Title\n", encoding="utf-8")

    vault_path = tmp_path / "accounts" / "acme" / "meetings" / "note.md"
    vault_path.parent.mkdir(parents=True, exist_ok=True)
    vault_path.write_text("# Meeting Note", encoding="utf-8")

    _append_meeting_to_pursuits(
        pursuits=["new-deal"],
        account="acme",
        meeting_date="2026-06-19",
        meeting_title="Kickoff",
        vault_path=vault_path,
        data_root=tmp_path,
    )

    content = pursuit_file.read_text(encoding="utf-8")
    assert "## Activity Log" in content
    assert "Kickoff" in content


def test_append_meeting_to_pursuits_idempotent_second_write_skipped(tmp_path: Path) -> None:
    """Re-running for the same meeting does not duplicate entries."""
    pursuit_dir = tmp_path / "accounts" / "acme" / "pursuits"
    pursuit_dir.mkdir(parents=True)
    pursuit_file = pursuit_dir / "deal.md"
    vault_path = tmp_path / "accounts" / "acme" / "meetings" / "meeting-note.md"
    vault_path.parent.mkdir(parents=True, exist_ok=True)
    vault_path.write_text("# Meeting Note", encoding="utf-8")

    # Pre-populate with the vault path already linked
    pursuit_file.write_text(
        "---\nstage: discover\n---\n\n## Activity Log\n- 2026-06-19 — Meeting note: [Sync](../meetings/meeting-note.md)\n",
        encoding="utf-8",
    )

    _append_meeting_to_pursuits(
        pursuits=["deal"],
        account="acme",
        meeting_date="2026-06-19",
        meeting_title="Sync",
        vault_path=vault_path,
        data_root=tmp_path,
    )

    content = pursuit_file.read_text(encoding="utf-8")
    # Should appear only once
    assert content.count("meeting-note.md") == 1


# ---------------------------------------------------------------------------
# _sync_action_items_to_tasks
# ---------------------------------------------------------------------------


# ── TestSyncActionItemsToTasks (flattened) ──────────────────────────────────


def _sync_action_items_to_tasks_make_vault(tmp_path: Path, internal_attendees: list[str] | None = None) -> Path:
    """Write a minimal vault note with optional attendees frontmatter."""
    vault = tmp_path / "note.md"
    attendees = internal_attendees or ["Alice Smith", "Bob Jones"]
    fm = (
        "---\nattendees_internal:\n"
        + "".join(f"  - {n}\n" for n in attendees)
        + "attendees_external:\n  - External Person\n---\n\n# Note\n"
    )
    vault.write_text(fm, encoding="utf-8")
    return vault


def test_sync_action_items_to_tasks_no_action_items_does_nothing(tmp_path: Path) -> None:
    """Empty action_items list → no tasks written, no error."""
    vault = _sync_action_items_to_tasks_make_vault(tmp_path)
    with (
        patch("fieldkit.tasks.classifier.classify_action_items", return_value=[]),
        patch("fieldkit.config.get_user_name", return_value="Alice Smith"),
        patch("fieldkit.config.get_user_email", return_value="alice@example.com"),  # pii-guard: ignore
        patch("fieldkit.config.get_accounts_config", return_value={"accounts": {"acme": {}}}),
    ):
        _sync_action_items_to_tasks(
            action_items=[],
            pursuits=["deal"],
            account="acme",
            meeting_date="2026-06-19",
            meeting_title="Sync",
            data_root=tmp_path,
            vault_path=vault,
        )
    # Should not error even with empty list


def test_sync_action_items_to_tasks_classify_called_with_items(tmp_path: Path) -> None:
    vault = _sync_action_items_to_tasks_make_vault(tmp_path)

    classified_item = MagicMock()
    classified_item.cls = MagicMock()
    classified_item.cls.value = "my_task"

    with (
        patch("fieldkit.config.get_user_name", return_value="Alice Smith"),
        patch("fieldkit.config.get_user_email", return_value="alice@example.com"),  # pii-guard: ignore
        patch("fieldkit.config.get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.tasks.classifier.classify_action_items", return_value=[classified_item]) as mock_classify,
        patch("fieldkit.tasks.writer.append_to_tasks", return_value=(1, [])),
    ):
        _sync_action_items_to_tasks(
            action_items=["Follow up with CTO"],
            pursuits=["deal"],
            account="acme",
            meeting_date="2026-06-19",
            meeting_title="Q2 Sync",
            data_root=tmp_path,
            vault_path=vault,
        )
    mock_classify.assert_called_once()


def test_sync_action_items_to_tasks_my_task_items_appended(tmp_path: Path) -> None:
    vault = _sync_action_items_to_tasks_make_vault(tmp_path)

    my_task = MagicMock()
    my_task.cls = MagicMock()
    my_task.cls.value = "my_task"

    waiting_on = MagicMock()
    waiting_on.cls = MagicMock()
    waiting_on.cls.value = "waiting_on"

    with (
        patch("fieldkit.config.get_user_name", return_value="Alice Smith"),
        patch("fieldkit.config.get_user_email", return_value="alice@example.com"),  # pii-guard: ignore
        patch("fieldkit.config.get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.tasks.classifier.classify_action_items", return_value=[my_task, waiting_on]),
        patch("fieldkit.tasks.writer.append_to_tasks", return_value=(1, [])) as mock_append,
    ):
        _sync_action_items_to_tasks(
            action_items=["Do X", "Wait for Y"],
            pursuits=["deal"],
            account="acme",
            meeting_date="2026-06-19",
            meeting_title="Q2 Sync",
            data_root=tmp_path,
            vault_path=vault,
        )
    # Only my_task items passed to append_to_tasks
    mock_append.assert_called_once()
    appended_items = mock_append.call_args[0][0]
    assert len(appended_items) == 1


def test_sync_action_items_to_tasks_exception_swallowed_gracefully(tmp_path: Path) -> None:
    """Errors during task sync must not propagate — pipeline must continue."""
    vault = _sync_action_items_to_tasks_make_vault(tmp_path)
    with patch("fieldkit.config.get_user_name", side_effect=RuntimeError("config gone")):
        # Should not raise
        _sync_action_items_to_tasks(
            action_items=["Anything"],
            pursuits=[],
            account="acme",
            meeting_date="2026-06-19",
            meeting_title="Sync",
            data_root=tmp_path,
            vault_path=vault,
        )


def test_sync_action_items_to_tasks_no_pursuits_uses_account_display(tmp_path: Path) -> None:
    """When pursuits=[], the label uses account display name only."""
    vault = _sync_action_items_to_tasks_make_vault(tmp_path)
    captured_labels: list[str] = []

    classified_item = MagicMock()
    classified_item.cls = MagicMock()
    classified_item.cls.value = "my_task"

    def capture_classify(items: list[str], **kwargs: Any) -> list[Any]:
        captured_labels.append(kwargs.get("pursuit_label", ""))
        return [classified_item]

    with (
        patch("fieldkit.config.get_user_name", return_value="Alice Smith"),
        patch("fieldkit.config.get_user_email", return_value="alice@example.com"),  # pii-guard: ignore
        patch("fieldkit.config.get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch("fieldkit.tasks.classifier.classify_action_items", side_effect=capture_classify),
        patch("fieldkit.tasks.writer.append_to_tasks", return_value=(0, [])),
    ):
        _sync_action_items_to_tasks(
            action_items=["X"],
            pursuits=[],
            account="acme",
            meeting_date="2026-06-19",
            meeting_title="Q2",
            data_root=tmp_path,
            vault_path=vault,
        )
    assert len(captured_labels) == 1
    assert "Acme" in captured_labels[0] or "acme" in captured_labels[0].lower()


def test_sync_action_items_to_tasks_display_override_applied(tmp_path: Path) -> None:
    """Known account abbreviations (e.g. 'acme-bank') use the override display form."""
    vault = _sync_action_items_to_tasks_make_vault(tmp_path)
    captured_labels: list[str] = []

    classified_item = MagicMock()
    classified_item.cls = MagicMock()
    classified_item.cls.value = "my_task"

    def capture_classify(items: list[str], **kwargs: Any) -> list[Any]:
        captured_labels.append(kwargs.get("pursuit_label", ""))
        return [classified_item]

    with (
        patch("fieldkit.config.get_user_name", return_value="Alice Smith"),
        patch("fieldkit.config.get_user_email", return_value="alice@example.com"),  # pii-guard: ignore
        patch("fieldkit.config.get_accounts_config", return_value={"accounts": {"acme-bank": {}}}),  # pii-guard: ignore
        patch("fieldkit.tasks.classifier.classify_action_items", side_effect=capture_classify),
        patch("fieldkit.tasks.writer.append_to_tasks", return_value=(0, [])),
    ):
        _sync_action_items_to_tasks(
            action_items=["X"],
            pursuits=[],
            account="acme-bank",  # pii-guard: ignore
            meeting_date="2026-06-19",
            meeting_title="Q2",
            data_root=tmp_path,
            vault_path=vault,
        )
    assert len(captured_labels) == 1
    assert "Acme Bank" in captured_labels[0]  # pii-guard: ignore


# ---------------------------------------------------------------------------
# _run_processing_loop — error handling
# ---------------------------------------------------------------------------


# ── TestRunProcessingLoop (flattened) ───────────────────────────────────────


def test_run_processing_loop_auth_error_returns_1(tmp_path: Path) -> None:
    """FileNotFoundError from get_docs_service returns exit code 1."""
    from fieldkit.commands.ingest.run import _run_processing_loop

    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
        patch("fieldkit.ingest.docs.get_docs_service", side_effect=FileNotFoundError("token not found")),
    ):
        src = _make_source("abc123", "Test", dated=False)
        rc = _run_processing_loop(
            [src],
            conn=MagicMock(),
            pipeline_version="0.1.0",
            interactive=False,
        )
    assert rc == 1


def test_run_processing_loop_interactive_quit_stops_loop(tmp_path: Path) -> None:
    """Interactive mode: 'q' response stops processing immediately."""
    from fieldkit.commands.ingest.run import _run_processing_loop

    mock_service = MagicMock()
    src = _make_source("src001", "Test Meeting")

    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=mock_service),
        patch("fieldkit.commands.ingest.run._prompt_process_choice", return_value="q"),
        patch("fieldkit.commands.ingest.run._process_one_source") as mock_process,
    ):
        rc = _run_processing_loop(
            [src],
            conn=MagicMock(),
            pipeline_version="0.1.0",
            interactive=True,
        )
    mock_process.assert_not_called()
    assert rc == 0


def test_run_processing_loop_interactive_skip_increments_skipped(tmp_path: Path) -> None:
    """Interactive mode: 'n' response skips the source."""
    from fieldkit.commands.ingest.run import _run_processing_loop

    mock_service = MagicMock()
    src = _make_source("src002", "Test Meeting")

    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=mock_service),
        patch("fieldkit.commands.ingest.run._prompt_process_choice", return_value="n"),
        patch("fieldkit.commands.ingest.run._process_one_source") as mock_process,
    ):
        rc = _run_processing_loop(
            [src],
            conn=MagicMock(),
            pipeline_version="0.1.0",
            interactive=True,
        )
    mock_process.assert_not_called()
    assert rc == 0


def test_run_processing_loop_interactive_yes_calls_process(tmp_path: Path) -> None:
    """Interactive mode: 'y' response processes the source."""
    from fieldkit.commands.ingest.run import _run_processing_loop

    mock_service = MagicMock()
    src = _make_source("src003", "Test Meeting")

    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=mock_service),
        patch("fieldkit.commands.ingest.run._prompt_process_choice", return_value="y"),
        patch(
            "fieldkit.commands.ingest.run._process_one_source",
            return_value=_ProcessResult(completed=True, degraded=False),
        ) as mock_process,
    ):
        rc = _run_processing_loop(
            [src],
            conn=MagicMock(),
            pipeline_version="0.1.0",
            interactive=True,
        )
    mock_process.assert_called_once()
    assert rc == 0


def test_run_processing_loop_parallel_process_error_counted(tmp_path: Path) -> None:
    """Parallel mode: failed _process_one_source increments n_errors."""
    from fieldkit.commands.ingest.run import _run_processing_loop

    mock_service = MagicMock()
    src = _make_source("src004", "Broken Meeting")

    def fake_open_db(path: Any) -> MagicMock:
        conn = MagicMock()
        conn.close = MagicMock()
        return conn

    with (
        patch("fieldkit.config.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.ingest.db.get_db_path", return_value=tmp_path / "pipeline.db"),
        patch("fieldkit.ingest.docs.get_docs_service", return_value=mock_service),
        patch("fieldkit.ingest.db.get_db", side_effect=fake_open_db),
        patch(
            "fieldkit.commands.ingest.run._process_one_source",
            return_value=_ProcessResult(completed=False, degraded=False),
        ),
    ):
        rc = _run_processing_loop(
            [src],
            conn=MagicMock(),
            pipeline_version="0.1.0",
            interactive=False,
        )
    assert rc == 0  # loop always returns 0; errors are counted in summary
