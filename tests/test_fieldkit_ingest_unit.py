"""Unit tests for fieldkit.ingest — in-process coverage.

Supplements subprocess smoke tests in test_ingest_cli.py.
All subcommand dispatch tests use CliRunner so no real I/O or DB access occurs.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from fieldkit.commands.ingest.cli import cli
from fieldkit.commands.ingest.run import _process_one_source, _ProcessResult, _run_processing_loop
from fieldkit.ingest.sources import SourceRecord

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# --help / no-args paths
# ---------------------------------------------------------------------------


def test_ingest_help_flag_returns_0() -> None:
    """cli(['--help']) exits 0 and prints usage."""
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "Usage" in result.output


def test_ingest_no_args_returns_nonzero() -> None:
    """cli([]) exits non-zero (no subcommand given)."""
    runner = CliRunner()
    result = runner.invoke(cli, [])
    assert result.exit_code != 0


def test_ingest_no_args_prints_usage() -> None:
    """cli([]) still prints usage text."""
    runner = CliRunner()
    result = runner.invoke(cli, [])
    assert "Usage" in result.output


def test_ingest_help_lists_subcommands() -> None:
    """--help output lists all known subcommands."""
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    for subcmd in ("status", "run", "discover", "reprocess", "backfill"):
        assert subcmd in result.output, f"Missing subcommand {subcmd!r} in --help output"


# ---------------------------------------------------------------------------
# unknown subcommand
# ---------------------------------------------------------------------------


def test_ingest_bogus_subcommand_returns_nonzero() -> None:
    """cli(['bogus']) exits non-zero."""
    runner = CliRunner()
    result = runner.invoke(cli, ["bogus"])
    assert result.exit_code != 0


def test_ingest_bogus_subcommand_prints_error() -> None:
    """cli(['bogus']) prints an error mentioning the unknown subcommand."""
    runner = CliRunner()
    result = runner.invoke(cli, ["bogus"])
    assert "bogus" in result.output


def test_ingest_bogus_subcommand_lists_available() -> None:
    """cli(['bogus']) output directs user to get help (Click's standard error format)."""
    runner = CliRunner()
    result = runner.invoke(cli, ["bogus"])
    # Click prints "Try 'ingest -h' for help." (group uses -h as help flag)
    assert "-h" in result.output


# ---------------------------------------------------------------------------
# known subcommands — dispatch smoke tests via CliRunner
# ---------------------------------------------------------------------------


def test_ingest_status_dispatches_to_module() -> None:
    """cli(['status', '--help']) dispatches to status subcommand and exits 0."""
    runner = CliRunner()
    result = runner.invoke(cli, ["status", "--help"])
    assert result.exit_code == 0


def test_ingest_status_forwards_extra_args() -> None:
    """cli(['status', '--help']) returns help text for the status subcommand."""
    runner = CliRunner()
    result = runner.invoke(cli, ["status", "--help"])
    assert result.exit_code == 0
    assert "Usage" in result.output


def test_ingest_run_dispatches_to_module() -> None:
    """cli(['run', '--help']) dispatches to run subcommand and exits 0."""
    runner = CliRunner()
    result = runner.invoke(cli, ["run", "--help"])
    assert result.exit_code == 0


def test_ingest_discover_dispatches_to_module() -> None:
    """cli(['discover', '--help']) dispatches to discover subcommand and exits 0."""
    runner = CliRunner()
    result = runner.invoke(cli, ["discover", "--help"])
    assert result.exit_code == 0


def test_ingest_reprocess_dispatches_to_module() -> None:
    """cli(['reprocess', '--help']) dispatches to reprocess subcommand and exits 0."""
    runner = CliRunner()
    result = runner.invoke(cli, ["reprocess", "--help"])
    assert result.exit_code == 0


def test_ingest_backfill_dispatches_to_module() -> None:
    """cli(['backfill', '--help']) dispatches to backfill subcommand and exits 0."""
    runner = CliRunner()
    result = runner.invoke(cli, ["backfill", "--help"])
    assert result.exit_code == 0


def test_ingest_subcommand_nonzero_propagated() -> None:
    """Non-zero exit from a leaf command propagates through cli."""
    runner = CliRunner()
    # Invoke a subcommand with invalid args to trigger a non-zero exit
    result = runner.invoke(cli, ["status", "--no-such-option"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Task 6.6 — _sync_action_items_to_tasks: missing TASKS.md logs warning, no crash
# ---------------------------------------------------------------------------


def test_sync_action_items_to_tasks_no_tasks_md(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_sync_action_items_to_tasks does not raise when TASKS.md is absent.

    The function wraps its entire body in ``except Exception`` to avoid blocking
    the ingest pipeline.  We stub the lazy fieldkit imports it pulls in, point
    data_root at a tmp dir with no TASKS.md, and verify no exception escapes.
    """
    import sys
    import types

    from fieldkit.ingest.writeback import _sync_action_items_to_tasks

    # Stub the fieldkit modules imported lazily inside _sync_action_items_to_tasks.
    # classify_action_items returns an empty list → append_to_tasks is never called,
    # so the missing TASKS.md is never touched.  The function must still not raise.
    fake_classifier = types.ModuleType("fieldkit.tasks.classifier")
    fake_classifier.classify_action_items = lambda *a, **kw: []  # type: ignore[attr-defined]

    fake_writer = types.ModuleType("fieldkit.tasks.writer")
    fake_writer.append_to_tasks = lambda *a, **kw: (0, 0)  # type: ignore[attr-defined]

    # fieldkit.config is already imported; patch the functions it exposes
    import fieldkit.config as aeos_config

    monkeypatch.setattr(aeos_config, "get_user_name", lambda: "Test User", raising=False)
    monkeypatch.setattr(aeos_config, "get_user_email", lambda: "test@example.com", raising=False)  # pii-guard: ignore
    monkeypatch.setattr(aeos_config, "get_accounts_config", lambda: {}, raising=False)

    monkeypatch.setitem(sys.modules, "fieldkit.tasks.classifier", fake_classifier)
    monkeypatch.setitem(sys.modules, "fieldkit.tasks.writer", fake_writer)

    # data_root has no TASKS.md — the function must swallow any resulting error
    data_root = tmp_path  # type: ignore[assignment]
    vault_path = data_root / "accounts" / "acme" / "meetings" / "2026-06-01-test.md"  # type: ignore[operator]

    try:
        _sync_action_items_to_tasks(
            action_items=["Send the proposal"],
            pursuits=[],
            account="acme",
            meeting_date="2026-06-01",
            meeting_title="Test Meeting",
            data_root=data_root,  # type: ignore[arg-type]
            vault_path=vault_path,  # type: ignore[arg-type]
        )
    except Exception as exc:
        pytest.fail(f"_sync_action_items_to_tasks raised unexpectedly: {exc}")


# ---------------------------------------------------------------------------
# Task 6.7 — _process_one_source returns False on pipeline failure
# ---------------------------------------------------------------------------


def test_process_one_source_returns_error_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """_process_one_source returns False when the doc fetch step raises an exception.

    We mock _fetch_doc_for_run to return None (the error path), which causes
    _process_one_source to return False immediately.
    """
    import fieldkit.commands.ingest.run as run_mod

    # _fetch_doc_for_run returning None signals a fetch failure
    monkeypatch.setattr(run_mod, "_fetch_doc_for_run", lambda *args, **kwargs: None)

    source = SourceRecord(
        source_id="doc-abc123",
        pipeline_id="transcript-ingest",
        subject="Test Meeting",
        meeting_title="Test Meeting",
        meeting_date=None,
        doc_url="https://docs.google.com/document/d/doc-abc123",
        email_message_id="msg-abc123",
        discovered_at="2026-06-19T00:00:00Z",
    )
    conn = sqlite3.connect(":memory:")

    result = _process_one_source(
        src=source,
        service=object(),
        conn=conn,
        data_root=Path("/tmp/fake-data"),
        pipeline_version="0.1.0",
    )
    conn.close()

    assert result.completed is False
    assert result.degraded is False


# ---------------------------------------------------------------------------
# Task 6.8 — _run_processing_loop always returns 0
# ---------------------------------------------------------------------------


def _make_fake_service() -> object:
    """Return a sentinel object standing in for a Google Docs service."""
    return object()


def test_run_processing_loop_returns_zero_on_all_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """_run_processing_loop returns 0 when all sources process successfully."""
    import pathlib
    import sys
    import types

    import fieldkit.commands.ingest.run as run_mod

    # Patch the lazy imports inside _run_processing_loop
    fake_docs = types.ModuleType("fieldkit.ingest.docs")
    fake_docs.get_docs_service = _make_fake_service  # type: ignore[attr-defined]

    fake_db = types.ModuleType("fieldkit.ingest.db")
    fake_db.get_db_path = lambda: ":memory:"  # type: ignore[attr-defined]
    fake_db.get_db = lambda path: sqlite3.connect(":memory:")  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "fieldkit.ingest.docs", fake_docs)
    monkeypatch.setitem(sys.modules, "fieldkit.ingest.db", fake_db)
    monkeypatch.setattr("fieldkit.ingest.sources.claim_pending_source", lambda conn, source_id: True)

    # Patch get_fieldkit_home inside the run module
    monkeypatch.setattr("fieldkit.config.get_fieldkit_home", lambda: pathlib.Path("/tmp/fake-data"))

    # _process_one_source always succeeds
    monkeypatch.setattr(
        run_mod,
        "_process_one_source",
        lambda **kwargs: _ProcessResult(completed=True, degraded=False),
    )

    source = SourceRecord(
        source_id="doc-success-1",
        pipeline_id="transcript-ingest",
        subject="Meeting A",
        meeting_title="Meeting A",
        meeting_date=datetime(2026, 6, 19, tzinfo=UTC),
        doc_url="https://docs.google.com/document/d/doc-success-1",
        email_message_id="msg-success-1",
        discovered_at="2026-06-19T00:00:00Z",
    )
    conn = sqlite3.connect(":memory:")

    rc = _run_processing_loop(
        [source],
        conn=conn,
        pipeline_version="0.1.0",
        interactive=False,
    )
    conn.close()

    assert rc == 0, f"_run_processing_loop must return 0 on success, got {rc}"


def test_run_processing_loop_preserves_human_exit_on_item_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Human mode preserves its historical zero exit after reporting item failures."""
    import sys
    import types

    import fieldkit.commands.ingest.run as run_mod

    fake_docs = types.ModuleType("fieldkit.ingest.docs")
    fake_docs.get_docs_service = _make_fake_service  # type: ignore[attr-defined]

    fake_db = types.ModuleType("fieldkit.ingest.db")
    fake_db.get_db_path = lambda: ":memory:"  # type: ignore[attr-defined]
    fake_db.get_db = lambda path: sqlite3.connect(":memory:")  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "fieldkit.ingest.docs", fake_docs)
    monkeypatch.setitem(sys.modules, "fieldkit.ingest.db", fake_db)
    monkeypatch.setattr("fieldkit.ingest.sources.claim_pending_source", lambda conn, source_id: True)
    monkeypatch.setattr("fieldkit.config.get_fieldkit_home", lambda: __import__("pathlib").Path("/tmp/fake-data"))

    # _process_one_source always fails (returns False)
    monkeypatch.setattr(
        run_mod,
        "_process_one_source",
        lambda **kwargs: _ProcessResult(completed=False, degraded=False),
    )

    source = SourceRecord(
        source_id="doc-fail-1",
        pipeline_id="transcript-ingest",
        subject="Meeting B",
        meeting_title="Meeting B",
        meeting_date=None,
        doc_url="https://docs.google.com/document/d/doc-fail-1",
        email_message_id="msg-fail-1",
        discovered_at="2026-06-19T00:00:00Z",
    )
    conn = sqlite3.connect(":memory:")

    rc = _run_processing_loop(
        [source],
        conn=conn,
        pipeline_version="0.1.0",
        interactive=False,
    )
    conn.close()

    assert rc == 0


# ---------------------------------------------------------------------------
# Additional _sync_action_items_to_tasks branch-coverage tests
# ---------------------------------------------------------------------------


def _make_vault_file(tmp_path: pytest.TempPathFactory, rh_attendees: list, customer_attendees: list) -> object:
    """Create a minimal vault .md file with frontmatter attendee lists."""

    vault_path = tmp_path / "accounts" / "acme" / "meetings" / "2026-06-01-test.md"  # type: ignore[operator]
    vault_path.parent.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]

    rh_yaml = "\n".join(f"  - {n}" for n in rh_attendees) if rh_attendees else ""
    cust_yaml = "\n".join(f"  - {n}" for n in customer_attendees) if customer_attendees else ""

    content = "---\n"
    if rh_attendees:
        content += f"attendees_internal:\n{rh_yaml}\n"
    if customer_attendees:
        content += f"attendees_external:\n{cust_yaml}\n"
    content += "---\nMeeting content here.\n"

    vault_path.write_text(content, encoding="utf-8")  # type: ignore[union-attr]
    return vault_path


def test_sync_action_items_reads_attendees_from_vault(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_sync_action_items_to_tasks reads attendee lists from vault frontmatter."""
    import sys
    import types

    from fieldkit.ingest.writeback import _sync_action_items_to_tasks

    # Track what classify_action_items was called with
    captured_kwargs: dict = {}

    def _fake_classify(items, **kwargs):  # type: ignore[no-untyped-def]
        captured_kwargs.update(kwargs)
        return []

    fake_classifier = types.ModuleType("fieldkit.tasks.classifier")
    fake_classifier.classify_action_items = _fake_classify  # type: ignore[attr-defined]
    fake_writer = types.ModuleType("fieldkit.tasks.writer")
    fake_writer.append_to_tasks = lambda *a, **kw: (0, 0)  # type: ignore[attr-defined]

    import fieldkit.config as aeos_config

    monkeypatch.setattr(aeos_config, "get_user_name", lambda: "Test User", raising=False)
    monkeypatch.setattr(aeos_config, "get_user_email", lambda: "test@example.com", raising=False)  # pii-guard: ignore
    monkeypatch.setattr(aeos_config, "get_accounts_config", lambda: {}, raising=False)
    monkeypatch.setitem(sys.modules, "fieldkit.tasks.classifier", fake_classifier)
    monkeypatch.setitem(sys.modules, "fieldkit.tasks.writer", fake_writer)

    vault_path = _make_vault_file(
        tmp_path,  # type: ignore[arg-type]
        rh_attendees=["Dave Rh"],
        customer_attendees=["Alice Customer"],
    )

    _sync_action_items_to_tasks(
        action_items=["Send the proposal"],
        pursuits=["acme-deal"],
        account="acme",
        meeting_date="2026-06-01",
        meeting_title="Test Meeting",
        data_root=tmp_path,  # type: ignore[arg-type]
        vault_path=vault_path,  # type: ignore[arg-type]
    )

    # Verify attendee lists were extracted from vault frontmatter
    assert "Dave Rh" in captured_kwargs.get("internal_team_names", [])
    assert "Alice Customer" in captured_kwargs.get("stakeholder_names", [])


def test_sync_action_items_with_pursuit_label_override(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_sync_action_items_to_tasks builds pursuit_label with display overrides."""
    import sys
    import types

    from fieldkit.ingest.writeback import _sync_action_items_to_tasks

    captured_kwargs: dict = {}

    def _fake_classify(items, **kwargs):  # type: ignore[no-untyped-def]
        captured_kwargs.update(kwargs)
        return []

    fake_classifier = types.ModuleType("fieldkit.tasks.classifier")
    fake_classifier.classify_action_items = _fake_classify  # type: ignore[attr-defined]
    fake_writer = types.ModuleType("fieldkit.tasks.writer")
    fake_writer.append_to_tasks = lambda *a, **kw: (0, 0)  # type: ignore[attr-defined]

    import fieldkit.config as aeos_config

    monkeypatch.setattr(aeos_config, "get_user_name", lambda: "Test User", raising=False)
    monkeypatch.setattr(aeos_config, "get_user_email", lambda: "test@example.com", raising=False)  # pii-guard: ignore
    monkeypatch.setattr(aeos_config, "get_accounts_config", lambda: {}, raising=False)
    monkeypatch.setitem(sys.modules, "fieldkit.tasks.classifier", fake_classifier)
    monkeypatch.setitem(sys.modules, "fieldkit.tasks.writer", fake_writer)

    vault_path = tmp_path / "meetings" / "test.md"  # type: ignore[operator]
    vault_path.parent.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
    vault_path.write_text("---\n---\nContent\n", encoding="utf-8")  # type: ignore[union-attr]

    _sync_action_items_to_tasks(
        action_items=["Send the proposal"],
        pursuits=["rhoai"],  # known abbreviation override
        account="acme",
        meeting_date="2026-06-01",
        meeting_title="Test Meeting",
        data_root=tmp_path,  # type: ignore[arg-type]
        vault_path=vault_path,  # type: ignore[arg-type]
    )

    # "rhoai" should be display-overridden to "RHOAI"
    label = captured_kwargs.get("pursuit_label", "")
    assert "RHOAI" in label


def test_sync_action_items_no_pursuits_uses_account_display(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When pursuits list is empty, pursuit_label uses account display name only."""
    import sys
    import types

    from fieldkit.ingest.writeback import _sync_action_items_to_tasks

    captured_kwargs: dict = {}

    def _fake_classify(items, **kwargs):  # type: ignore[no-untyped-def]
        captured_kwargs.update(kwargs)
        return []

    fake_classifier = types.ModuleType("fieldkit.tasks.classifier")
    fake_classifier.classify_action_items = _fake_classify  # type: ignore[attr-defined]
    fake_writer = types.ModuleType("fieldkit.tasks.writer")
    fake_writer.append_to_tasks = lambda *a, **kw: (0, 0)  # type: ignore[attr-defined]

    import fieldkit.config as aeos_config

    monkeypatch.setattr(aeos_config, "get_user_name", lambda: "Test User", raising=False)
    monkeypatch.setattr(aeos_config, "get_user_email", lambda: "test@example.com", raising=False)  # pii-guard: ignore
    monkeypatch.setattr(aeos_config, "get_accounts_config", lambda: {}, raising=False)
    monkeypatch.setitem(sys.modules, "fieldkit.tasks.classifier", fake_classifier)
    monkeypatch.setitem(sys.modules, "fieldkit.tasks.writer", fake_writer)

    vault_path = tmp_path / "meetings" / "test.md"  # type: ignore[operator]
    vault_path.parent.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
    vault_path.write_text("---\n---\nContent\n", encoding="utf-8")  # type: ignore[union-attr]

    _sync_action_items_to_tasks(
        action_items=["Send the proposal"],
        pursuits=[],  # empty pursuits
        account="acme-corp",
        meeting_date="2026-06-01",
        meeting_title="Test Meeting",
        data_root=tmp_path,  # type: ignore[arg-type]
        vault_path=vault_path,  # type: ignore[arg-type]
    )

    # With no pursuits, label should just be the account display name
    label = captured_kwargs.get("pursuit_label", "")
    assert "/" not in label  # no pursuit suffix
    assert "Acme" in label or "acme" in label.lower()


def test_sync_action_items_my_task_items_written_to_tasks_md(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MY_TASK items from classify_action_items are written to TASKS.md."""
    import sys
    import types

    from fieldkit.ingest.writeback import _sync_action_items_to_tasks
    from fieldkit.tasks.classifier import ClassifiedItem, ItemClass

    my_task_item = ClassifiedItem(
        text="Send the proposal to Alice",
        cls=ItemClass.MY_TASK,
        owner="Test User",
        rationale="Owner matches AE",
        pursuit_label="Acme / RHOAI",
    )

    fake_classifier = types.ModuleType("fieldkit.tasks.classifier")
    fake_classifier.classify_action_items = lambda *a, **kw: [my_task_item]  # type: ignore[attr-defined]
    fake_classifier.ClassifiedItem = ClassifiedItem  # type: ignore[attr-defined]
    fake_classifier.ItemClass = ItemClass  # type: ignore[attr-defined]

    append_calls: list = []

    def _fake_append(items, path, **kwargs):  # type: ignore[no-untyped-def]
        append_calls.append((items, path))
        return (1, 0)

    fake_writer = types.ModuleType("fieldkit.tasks.writer")
    fake_writer.append_to_tasks = _fake_append  # type: ignore[attr-defined]

    import fieldkit.config as aeos_config

    monkeypatch.setattr(aeos_config, "get_user_name", lambda: "Test User", raising=False)
    monkeypatch.setattr(aeos_config, "get_user_email", lambda: "test@example.com", raising=False)  # pii-guard: ignore
    monkeypatch.setattr(aeos_config, "get_accounts_config", lambda: {}, raising=False)
    monkeypatch.setitem(sys.modules, "fieldkit.tasks.classifier", fake_classifier)
    monkeypatch.setitem(sys.modules, "fieldkit.tasks.writer", fake_writer)

    vault_path = tmp_path / "meetings" / "test.md"  # type: ignore[operator]
    vault_path.parent.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
    vault_path.write_text("---\n---\nContent\n", encoding="utf-8")  # type: ignore[union-attr]

    _sync_action_items_to_tasks(
        action_items=["Send the proposal to Alice"],
        pursuits=["rhoai"],
        account="acme",
        meeting_date="2026-06-01",
        meeting_title="Test Meeting",
        data_root=tmp_path,  # type: ignore[arg-type]
        vault_path=vault_path,  # type: ignore[arg-type]
    )

    # append_to_tasks should have been called with the MY_TASK item
    assert len(append_calls) == 1
    items_passed, _ = append_calls[0]
    assert any(i.cls == ItemClass.MY_TASK for i in items_passed)


def test_sync_action_items_drop_items_not_written(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DROP items from classify_action_items are NOT written to TASKS.md."""
    import sys
    import types

    from fieldkit.ingest.writeback import _sync_action_items_to_tasks
    from fieldkit.tasks.classifier import ClassifiedItem, ItemClass

    drop_item = ClassifiedItem(
        text="Team: update Jira sprint board",
        cls=ItemClass.DROP,
        owner="Team",
        rationale="Internal mechanics",
        pursuit_label="Acme / RHOAI",
    )

    fake_classifier = types.ModuleType("fieldkit.tasks.classifier")
    fake_classifier.classify_action_items = lambda *a, **kw: [drop_item]  # type: ignore[attr-defined]
    fake_classifier.ClassifiedItem = ClassifiedItem  # type: ignore[attr-defined]
    fake_classifier.ItemClass = ItemClass  # type: ignore[attr-defined]

    append_calls: list = []

    def _fake_append(items, path, **kwargs):  # type: ignore[no-untyped-def]
        append_calls.append(items)
        return (0, 0)

    fake_writer = types.ModuleType("fieldkit.tasks.writer")
    fake_writer.append_to_tasks = _fake_append  # type: ignore[attr-defined]

    import fieldkit.config as aeos_config

    monkeypatch.setattr(aeos_config, "get_user_name", lambda: "Test User", raising=False)
    monkeypatch.setattr(aeos_config, "get_user_email", lambda: "test@example.com", raising=False)  # pii-guard: ignore
    monkeypatch.setattr(aeos_config, "get_accounts_config", lambda: {}, raising=False)
    monkeypatch.setitem(sys.modules, "fieldkit.tasks.classifier", fake_classifier)
    monkeypatch.setitem(sys.modules, "fieldkit.tasks.writer", fake_writer)

    vault_path = tmp_path / "meetings" / "test.md"  # type: ignore[operator]
    vault_path.parent.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
    vault_path.write_text("---\n---\nContent\n", encoding="utf-8")  # type: ignore[union-attr]

    _sync_action_items_to_tasks(
        action_items=["Team: update Jira sprint board"],
        pursuits=["rhoai"],
        account="acme",
        meeting_date="2026-06-01",
        meeting_title="Test Meeting",
        data_root=tmp_path,  # type: ignore[arg-type]
        vault_path=vault_path,  # type: ignore[arg-type]
    )

    # append_to_tasks should NOT have been called (no MY_TASK items)
    assert len(append_calls) == 0
