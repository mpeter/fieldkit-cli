"""Tests for fieldkit.watch.draft_queue (domain) and fieldkit.commands.watch.draft_queue (CLI+MCP).

Covers:
- MCP unavailable → exit 1 with error logged
- Successful draft fetch → alerts file written with correct format
- dry-run mode → no file written
- Empty draft list → 'No stale drafts found.' written
- parse_drafts: flat dict, nested payload.headers, list shapes
- _format_age: known age, missing date, zero age

Pure data-processing functions (parse_drafts, write_alerts, _format_age, _extract_header)
are patched/imported via fieldkit.watch.draft_queue.

MCP integration (_run_draft_queue, MCPSession, write_run_status, watcher_logging,
get_user_email_from_env) are patched via fieldkit.commands.watch.draft_queue since
_run_draft_queue stays in the commands module until slice 2.5 (implementation change).
"""

import json
import os
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import fieldkit.watch.draft_queue as dq
from fieldkit.commands.watch import draft_queue as dq_cmd
from fieldkit.config import ConfigError
from fieldkit.watch.mcp import MCPAuthError
from fieldkit.watch.status import write_run_status as real_write_run_status

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _configured_draft_queue_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dq, "get_mcp_endpoint", lambda _name: "https://gateway.example.com/draft-queue")


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _sample_msg(
    subject: str = "Hello",
    to: str = "bob@example.com",  # pii-guard: ignore
    age_ms: int = 200000000,  # pii-guard: ignore
) -> dict[str, Any]:  # pii-guard: ignore
    """Flat-shape message dict (no payload.headers nesting)."""
    return {
        "id": "draft-001",
        "internalDate": str(age_ms),
        "subject": subject,
        "to": to,
    }


def _nested_msg(
    subject: str = "Nested",
    to: str = "alice@example.com",  # pii-guard: ignore
    age_ms: int = 300000000,  # pii-guard: ignore
) -> dict[str, Any]:  # pii-guard: ignore
    """Nested payload.headers shape matching full Gmail API response."""
    return {
        "id": "draft-002",
        "internalDate": str(age_ms),
        "payload": {
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "To", "value": to},
            ]
        },
    }


# ---------------------------------------------------------------------------
# parse_drafts
# ---------------------------------------------------------------------------


# ── TestParseDrafts (flattened) ─────────────────────────────────────────────


def test_parse_drafts_list_of_flat_dicts() -> None:
    msgs = [_sample_msg()]
    drafts = dq.parse_drafts(msgs)
    assert len(drafts) == 1
    assert drafts[0]["subject"] == "Hello"
    assert drafts[0]["to"] == "bob@example.com"  # pii-guard: ignore
    assert drafts[0]["draft_id"] == "draft-001"


def test_parse_drafts_list_of_nested_dicts() -> None:
    msgs = [_nested_msg()]
    drafts = dq.parse_drafts(msgs)
    assert len(drafts) == 1
    assert drafts[0]["subject"] == "Nested"
    assert drafts[0]["to"] == "alice@example.com"  # pii-guard: ignore


def test_parse_drafts_dict_with_messages_key() -> None:
    raw = {"messages": [_sample_msg("Q1"), _nested_msg("Q2")]}
    drafts = dq.parse_drafts(raw)
    assert len(drafts) == 2


def test_parse_drafts_dict_with_results_key() -> None:
    raw = {"results": [_sample_msg("R1")]}
    drafts = dq.parse_drafts(raw)
    assert len(drafts) == 1


@pytest.mark.parametrize(
    "message",
    [
        {"id": "draft-one", "draft_id": "draft-two"},
        {"id": "draft-one", "subject": "first", "Subject": "second"},
        {"id": "draft-one", "to": "first@example.com", "To": "second@example.com"},
        {
            "id": "draft-one",
            "subject": "flat",
            "payload": {"headers": [{"name": "Subject", "value": "nested"}]},
        },
        {
            "id": "draft-one",
            "payload": {
                "headers": [
                    {"name": "To", "value": "first@example.com"},
                    {"name": "to", "value": "second@example.com"},
                ]
            },
        },
    ],
)
def test_parse_drafts_rejects_semantically_duplicate_aliases(message: dict[str, object]) -> None:
    with pytest.raises(RuntimeError, match="invalid draft search response"):
        dq.parse_drafts({"messages": [message]})


def test_parse_drafts_empty_list() -> None:
    assert dq.parse_drafts([]) == []


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"unknown": []},
        {"messages": None},
        {"messages": [], "error": "credential expired"},
        {"messages": [], "nextPageToken": "more"},
        ["not-a-message"],
    ],
)
def test_parse_drafts_rejects_malformed_provider_payload(raw: object) -> None:
    with pytest.raises(RuntimeError, match="invalid draft search response"):
        dq.parse_drafts(raw)


@pytest.mark.parametrize(
    "message",
    [
        {"payload": {"headers": [{"name": 1, "value": "fixture"}]}, "id": "draft-1"},
        {"payload": {"headers": [{"name": "Subject", "value": 1}]}, "id": "draft-1"},
        {"payload": {"headers": [{"name": "Subject"}]}, "id": "draft-1"},
        {"subject": 1, "id": "draft-1"},
        {"to": ["recipient@example.com"], "id": "draft-1"},
        {"subject": "No identity"},
        {"id": 1, "subject": "Invalid identity"},
    ],
)
def test_parse_drafts_rejects_malformed_message_fields(message: dict[str, object]) -> None:
    with pytest.raises(RuntimeError, match="invalid draft search response"):
        dq.parse_drafts([message])


def test_parse_drafts_valid_wrapped_empty_list() -> None:
    assert dq.parse_drafts({"messages": []}) == []


def test_parse_drafts_missing_subject_defaults() -> None:
    msg: dict[str, Any] = {"id": "x", "internalDate": "100000000"}
    drafts = dq.parse_drafts([msg])
    assert drafts[0]["subject"] == "(no subject)"


def test_parse_drafts_missing_to_defaults() -> None:
    msg: dict[str, Any] = {"id": "x", "internalDate": "100000000", "subject": "Hi"}
    drafts = dq.parse_drafts([msg])
    assert drafts[0]["to"] == "(unknown recipient)"


def test_parse_drafts_invalid_internal_date() -> None:
    msg = _sample_msg()
    msg["internalDate"] = "not-a-number"
    with pytest.raises(RuntimeError, match="invalid draft search response"):
        dq.parse_drafts([msg])


@pytest.mark.parametrize("internal_date", [False, True, -1, "-1", 1.5, "9" * 400, 10**400])
def test_parse_drafts_rejects_unusable_internal_date(internal_date: object) -> None:
    msg = _sample_msg()
    msg["internalDate"] = internal_date

    with pytest.raises(RuntimeError, match="invalid draft search response"):
        dq.parse_drafts([msg])


# ---------------------------------------------------------------------------
# _format_age
# ---------------------------------------------------------------------------


# ── TestFormatAge (flattened) ───────────────────────────────────────────────


def test_format_age_none_returns_unknown() -> None:
    assert dq._format_age(None) == "unknown age"


def test_format_age_large_ms_produces_days() -> None:
    from datetime import UTC, datetime, timedelta

    two_days_ago = datetime.now(UTC) - timedelta(days=2, hours=3)
    ms = int(two_days_ago.timestamp() * 1000)
    result = dq._format_age(ms)
    assert result.startswith("2d")


def test_format_age_hours_only() -> None:
    from datetime import UTC, datetime, timedelta

    two_hours_ago = datetime.now(UTC) - timedelta(hours=2)
    ms = int(two_hours_ago.timestamp() * 1000)
    result = dq._format_age(ms)
    assert result == "2h"


# ---------------------------------------------------------------------------
# write_alerts — dry-run
# ---------------------------------------------------------------------------


# ── TestWriteAlertsDryRun (flattened) ───────────────────────────────────────


def test_write_alerts_dry_run_does_not_write_file(tmp_path: Path) -> None:
    alerts_path = tmp_path / "watchers" / "draft-queue-alerts.md"

    with (
        patch.object(dq, "_alerts_file", return_value=alerts_path),
        patch.object(dq, "get_watchers_dir", return_value=tmp_path / "watchers"),
    ):
        dq.write_alerts([], dry_run=True)

    assert not alerts_path.exists()


def test_write_alerts_dry_run_with_drafts_no_file(tmp_path: Path) -> None:
    alerts_path = tmp_path / "watchers" / "draft-queue-alerts.md"
    drafts = [{"subject": "X", "to": "y@z.example.com", "age": "3d 0h", "draft_id": "d1"}]

    with (
        patch.object(dq, "_alerts_file", return_value=alerts_path),
        patch.object(dq, "get_watchers_dir", return_value=tmp_path / "watchers"),
    ):
        dq.write_alerts(drafts, dry_run=True)

    assert not alerts_path.exists()


# ---------------------------------------------------------------------------
# write_alerts — real writes
# ---------------------------------------------------------------------------


# ── TestWriteAlertsRealWrite (flattened) ────────────────────────────────────


def test_write_alerts_creates_file_with_header_if_missing(tmp_path: Path) -> None:
    watchers_dir = tmp_path / "watchers"
    alerts_path = watchers_dir / "draft-queue-alerts.md"

    with (
        patch.object(dq, "_alerts_file", return_value=alerts_path),
        patch.object(dq, "get_watchers_dir", return_value=watchers_dir),
    ):
        dq.write_alerts([], dry_run=False)

    content = alerts_path.read_text(encoding="utf-8")
    assert "Draft Queue Alerts" in content
    assert "No stale drafts found." in content


def test_write_alerts_appends_draft_rows(tmp_path: Path) -> None:
    watchers_dir = tmp_path / "watchers"
    alerts_path = watchers_dir / "draft-queue-alerts.md"
    drafts = [{"subject": "Proposal", "to": "cto@acme-corp.example.com", "age": "2d 5h", "draft_id": "abc123"}]

    with (
        patch.object(dq, "_alerts_file", return_value=alerts_path),
        patch.object(dq, "get_watchers_dir", return_value=watchers_dir),
    ):
        dq.write_alerts(drafts, dry_run=False)

    content = alerts_path.read_text(encoding="utf-8")
    assert "Proposal" in content
    assert "cto@acme-corp.example.com" in content
    assert "2d 5h" in content
    assert "abc123" in content


def test_write_alerts_uses_one_atomic_replacement(tmp_path: Path) -> None:
    watchers_dir = tmp_path / "watchers"
    alerts_path = watchers_dir / "draft-queue-alerts.md"
    alerts_path.parent.mkdir(parents=True)
    alerts_path.write_text("# existing\n", encoding="utf-8")
    atomic_write = MagicMock()
    with (
        patch.object(dq, "_alerts_file", return_value=alerts_path),
        patch.object(dq, "get_watchers_dir", return_value=watchers_dir),
        patch.object(dq, "atomic_text_write", atomic_write),
    ):
        dq.write_alerts([], dry_run=False)

    atomic_write.assert_called_once()
    written_path, written_text = atomic_write.call_args.args
    assert written_path == alerts_path
    assert written_text.startswith("# existing")
    assert "No stale drafts found." in written_text


def test_write_alerts_atomic_failure_preserves_existing_snapshot(tmp_path: Path) -> None:
    watchers_dir = tmp_path / "watchers"
    alerts_path = watchers_dir / "draft-queue-alerts.md"
    alerts_path.parent.mkdir(parents=True)
    alerts_path.write_text("# existing snapshot\n", encoding="utf-8")

    with (
        patch.object(dq, "_alerts_file", return_value=alerts_path),
        patch.object(dq, "get_watchers_dir", return_value=watchers_dir),
        patch.object(dq, "atomic_text_write", side_effect=OSError("private path")),
        pytest.raises(OSError),
    ):
        dq.write_alerts([], dry_run=False)

    assert alerts_path.read_text(encoding="utf-8") == "# existing snapshot\n"


def test_run_draft_queue_persistence_failure_is_fatal_json(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    session = MagicMock()
    session.call_tool.return_value = []
    write_status = MagicMock(return_value="written")
    monkeypatch.setattr("fieldkit.watch.mcp.MCPSession", lambda _endpoint: session)
    monkeypatch.setattr(dq, "_resolve_user_email", lambda: "user@example.com")
    monkeypatch.setattr(dq, "write_alerts", MagicMock(side_effect=OSError("private path")))
    monkeypatch.setattr(dq, "write_run_status", write_status)
    monkeypatch.setattr(dq, "watcher_logging", MagicMock())

    result = dq._run_draft_queue(dry_run=False, as_json=True)

    assert result.exit_code == 1
    assert json.loads(capsys.readouterr().out)["outcome"] == "fatal"
    assert write_status.call_args.kwargs["failures"] == 1


def test_write_alerts_empty_drafts_writes_no_stale_message(tmp_path: Path) -> None:
    watchers_dir = tmp_path / "watchers"
    alerts_path = watchers_dir / "draft-queue-alerts.md"

    with (
        patch.object(dq, "_alerts_file", return_value=alerts_path),
        patch.object(dq, "get_watchers_dir", return_value=watchers_dir),
    ):
        dq.write_alerts([], dry_run=False)

    content = alerts_path.read_text(encoding="utf-8")
    assert "No stale drafts found." in content


def test_write_alerts_pending_outbox_section_present(tmp_path: Path) -> None:
    watchers_dir = tmp_path / "watchers"
    alerts_path = watchers_dir / "draft-queue-alerts.md"

    with (
        patch.object(dq, "_alerts_file", return_value=alerts_path),
        patch.object(dq, "get_watchers_dir", return_value=watchers_dir),
    ):
        dq.write_alerts([], dry_run=False)

    content = alerts_path.read_text(encoding="utf-8")
    assert "Pending Outbox" in content


# ---------------------------------------------------------------------------
# _run_draft_queue — integration-level tests with mocked MCPSession
# ---------------------------------------------------------------------------


# ── TestRunDraftQueue (flattened) ───────────────────────────────────────────


@pytest.mark.parametrize("operation", ["initialize", "call_tool"])
@pytest.mark.parametrize("status_write, failures", [("written", 1), ("failed", 2)])
def test_provider_failure_carries_exact_status_and_count(
    operation: str, status_write: str, failures: int, capsys: pytest.CaptureFixture[str]
) -> None:
    session = MagicMock()
    getattr(session, operation).side_effect = RuntimeError("provider unavailable")
    with (
        patch("fieldkit.watch.mcp.MCPSession", return_value=session),
        patch.object(dq, "_resolve_user_email", return_value="user@example.com"),
        patch.object(dq, "write_run_status", return_value=status_write),
        patch.object(dq, "watcher_logging"),
        patch.object(dq, "write_alerts") as alerts,
    ):
        result = dq._run_draft_queue(dry_run=False, as_json=True)
    assert result.outcome == "fatal"
    assert result.completed is False
    assert result.status_write == status_write
    assert json.loads(capsys.readouterr().out)["failures"] == failures
    alerts.assert_not_called()


@pytest.mark.parametrize("fail_status", [False, True])
def test_real_empty_scan_publishes_alert_and_carries_status_result(
    fail_status: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from fieldkit.watch import status

    session = MagicMock()
    session.call_tool.return_value = []
    alerts = tmp_path / "watchers" / "draft-queue-alerts.md"
    target = tmp_path / "watchers" / "watcher-run-status.json"
    monkeypatch.setattr(status, "get_fieldkit_home", lambda: tmp_path)
    if fail_status:
        target.parent.mkdir()
        target.write_text('{"draft-queue": {"outcome": "ok"}}', encoding="utf-8")

        def fail_destination(path: Path, *args: object, **kwargs: object) -> None:
            assert path == target
            raise OSError("destination unavailable")

        monkeypatch.setattr(status, "locked_json_update", fail_destination)
    with (
        patch("fieldkit.watch.mcp.MCPSession", return_value=session),
        patch.object(dq, "_resolve_user_email", return_value="user@example.com"),
        patch.object(dq, "get_watchers_dir", return_value=alerts.parent),
        patch.object(dq, "_alerts_file", return_value=alerts),
        patch.object(dq, "write_run_status", real_write_run_status),
        patch.object(dq, "watcher_logging"),
    ):
        result = dq._run_draft_queue(dry_run=False, as_json=True)
    assert result.outcome == ("fatal" if fail_status else "ok")
    assert result.completed is True
    assert result.status_write == ("failed" if fail_status else "written")
    assert result.exit_code == int(fail_status)
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == result.outcome
    assert payload["failures"] == int(fail_status)
    assert "No stale drafts found." in alerts.read_text(encoding="utf-8")
    persisted = json.loads(target.read_text(encoding="utf-8"))["draft-queue"]
    assert persisted["outcome"] == "ok"
    if not fail_status:
        assert persisted["records_checked"] == 0
    session.close.assert_called_once()


def test_scoped_draft_scan_preserves_global_snapshot(tmp_path: Path) -> None:
    session = MagicMock()
    session.call_tool.return_value = []
    alerts = tmp_path / "draft-queue-alerts.md"
    alerts.write_text("previous global snapshot\n", encoding="utf-8")
    with (
        patch("fieldkit.watch.mcp.MCPSession", return_value=session),
        patch.object(dq, "_resolve_user_email", return_value="user@example.com"),
        patch.object(dq, "_alerts_file", return_value=alerts),
        patch.object(dq, "write_run_status", return_value="written"),
        patch.object(dq, "watcher_logging"),
        patch.object(dq, "write_alerts") as writer,
    ):
        result = dq._run_draft_queue(dry_run=False, account="acme")
    assert result.outcome == "ok"
    assert result.completed is True
    assert result.status_write == "written"
    writer.assert_not_called()
    assert alerts.read_text(encoding="utf-8") == "previous global snapshot\n"


@pytest.mark.parametrize("publication", ["written", "failed", "scoped"])
def test_run_counts_only_published_draft_alerts(
    tmp_path: Path, publication: str, capsys: pytest.CaptureFixture[str]
) -> None:
    from fieldkit.watch.status import WatcherRunResult

    session = MagicMock()
    session.call_tool.return_value = [_sample_msg()]
    alerts = tmp_path / "draft-queue-alerts.md"
    before = "previous global snapshot\n"
    alerts.write_text(before, encoding="utf-8")
    with (
        patch("fieldkit.watch.mcp.MCPSession", return_value=session),
        patch.object(dq, "_resolve_user_email", return_value="user@example.com"),
        patch.object(dq, "_alerts_file", return_value=alerts),
        patch.object(dq, "get_watchers_dir", return_value=tmp_path),
        patch.object(dq, "write_run_status", return_value="written") as writer,
        patch.object(dq, "watcher_logging"),
        patch("fieldkit.ingest.router.route_by_domains") as route,
        patch.object(dq, "atomic_text_write", side_effect=OSError("publication failed"))
        if publication == "failed"
        else nullcontext(),
    ):
        route.return_value.accounts = {"acme"}
        result = dq._run_draft_queue(dry_run=False, account="acme" if publication == "scoped" else None, as_json=True)
    assert result == WatcherRunResult("fatal" if publication == "failed" else "ok", True, "written")
    payload = json.loads(capsys.readouterr().out)
    expected_alerts = int(publication == "written")
    assert payload["records_checked"] == 1
    assert payload["alerts_generated"] == expected_alerts
    assert payload["failures"] == int(publication == "failed")
    assert writer.call_args.kwargs["alerts_generated"] == expected_alerts
    if publication == "written":
        assert "draft-001" in alerts.read_text(encoding="utf-8")
    else:
        assert alerts.read_text(encoding="utf-8") == before


def _run_draft_queue_mock_status(**kwargs: Any) -> MagicMock:
    return MagicMock(return_value="written")


def test_run_draft_queue_mcp_unavailable_returns_exit_1(tmp_path: Path) -> None:
    """MCPSession.initialize raising RuntimeError → exit 1."""
    watchers_dir = tmp_path / "watchers"
    alerts_path = watchers_dir / "draft-queue-alerts.md"

    mock_session = MagicMock()
    mock_session.initialize.side_effect = RuntimeError("Connection refused")

    with (
        patch.dict(os.environ, {"FIELDKIT_USER_EMAIL": "ae@example.com"}),  # pii-guard: ignore
        patch("fieldkit.watch.mcp.MCPSession", return_value=mock_session),
        patch.object(dq, "_alerts_file", return_value=alerts_path),
        patch.object(dq, "get_watchers_dir", return_value=watchers_dir),
        patch("fieldkit.watch.draft_queue.watcher_logging"),
        patch("fieldkit.watch.draft_queue.write_run_status", return_value="written"),
    ):
        result = dq._run_draft_queue(dry_run=False)

    assert result.exit_code == 1


def test_run_draft_queue_missing_endpoint_is_invalid_configuration(tmp_path: Path) -> None:
    """An absent optional endpoint is not reported as a successful empty queue."""
    with (
        patch.object(dq, "get_mcp_endpoint", return_value=None),
        patch("fieldkit.watch.mcp.MCPSession") as session,
        patch("fieldkit.watch.draft_queue.watcher_logging"),
        pytest.raises(ConfigError, match=r"mcp_endpoints\.draft_queue"),
    ):
        dq._run_draft_queue(dry_run=False)

    session.assert_not_called()


def test_run_draft_queue_auth_failure_propagates_for_exit_two(tmp_path: Path) -> None:
    """A credential rejection must reach the canonical authentication boundary."""
    session = MagicMock()
    session.initialize.side_effect = MCPAuthError("authentication failed")

    with (
        patch.dict(os.environ, {"FIELDKIT_USER_EMAIL": "ae@example.com"}),  # pii-guard: ignore
        patch("fieldkit.watch.mcp.MCPSession", return_value=session),
        patch("fieldkit.watch.draft_queue.watcher_logging"),
        pytest.raises(MCPAuthError, match="authentication failed"),
    ):
        dq._run_draft_queue(dry_run=False)


def test_run_draft_queue_mcp_unavailable_does_not_write_alerts(tmp_path: Path) -> None:
    """When MCP fails to initialize, no alerts file should be written."""
    watchers_dir = tmp_path / "watchers"
    alerts_path = watchers_dir / "draft-queue-alerts.md"

    mock_session = MagicMock()
    mock_session.initialize.side_effect = RuntimeError("Connection refused")

    with (
        patch.dict(os.environ, {"FIELDKIT_USER_EMAIL": "ae@example.com"}),  # pii-guard: ignore
        patch("fieldkit.watch.mcp.MCPSession", return_value=mock_session),
        patch.object(dq, "_alerts_file", return_value=alerts_path),
        patch.object(dq, "get_watchers_dir", return_value=watchers_dir),
        patch("fieldkit.watch.draft_queue.watcher_logging"),
        patch("fieldkit.watch.draft_queue.write_run_status", return_value="written"),
    ):
        dq._run_draft_queue(dry_run=False)

    assert not alerts_path.exists()


def test_run_draft_queue_successful_fetch_writes_alerts_exit_0(tmp_path: Path) -> None:
    """Successful MCP fetch → alerts written, exit 0."""
    watchers_dir = tmp_path / "watchers"
    alerts_path = watchers_dir / "draft-queue-alerts.md"

    from datetime import UTC, datetime, timedelta

    old_ms = int((datetime.now(UTC) - timedelta(days=2)).timestamp() * 1000)
    fake_drafts = [{"id": "d1", "internalDate": str(old_ms), "subject": "Follow up", "to": "x@y.example.com"}]

    mock_session = MagicMock()
    mock_session.initialize.return_value = None
    mock_session.call_tool.return_value = fake_drafts

    with (
        patch.dict(os.environ, {"FIELDKIT_USER_EMAIL": "ae@example.com"}),  # pii-guard: ignore
        patch("fieldkit.watch.mcp.MCPSession", return_value=mock_session),
        patch.object(dq, "_alerts_file", return_value=alerts_path),
        patch.object(dq, "get_watchers_dir", return_value=watchers_dir),
        patch("fieldkit.watch.draft_queue.watcher_logging"),
        patch("fieldkit.watch.draft_queue.write_run_status", return_value="written"),
    ):
        result = dq._run_draft_queue(dry_run=False)

    assert result.exit_code == 0
    content = alerts_path.read_text(encoding="utf-8")
    assert "Follow up" in content
    assert "x@y.example.com" in content


def test_run_draft_queue_call_tool_error_returns_exit_1(tmp_path: Path) -> None:
    """call_tool raising RuntimeError → exit 1, no alerts written."""
    watchers_dir = tmp_path / "watchers"
    alerts_path = watchers_dir / "draft-queue-alerts.md"

    mock_session = MagicMock()
    mock_session.initialize.return_value = None
    mock_session.call_tool.side_effect = RuntimeError("tool error")

    with (
        patch.dict(os.environ, {"FIELDKIT_USER_EMAIL": "ae@example.com"}),  # pii-guard: ignore
        patch("fieldkit.watch.mcp.MCPSession", return_value=mock_session),
        patch.object(dq, "_alerts_file", return_value=alerts_path),
        patch.object(dq, "get_watchers_dir", return_value=watchers_dir),
        patch("fieldkit.watch.draft_queue.watcher_logging"),
        patch("fieldkit.watch.draft_queue.write_run_status", return_value="written"),
    ):
        result = dq._run_draft_queue(dry_run=False)

    assert result.exit_code == 1
    assert not alerts_path.exists()


@pytest.mark.parametrize(
    "provider_response",
    [
        None,
        {"messages": [], "error": "private credential failure"},
        {"messages": [], "nextPageToken": "private-page-token"},
        [{"id": "draft-1", "payload": {"headers": [{"name": 1, "value": "fixture"}]}}],
    ],
)
def test_run_draft_queue_malformed_payload_is_failure_without_alert_success_write(
    tmp_path: Path, provider_response: object
) -> None:
    mock_session = MagicMock()
    mock_session.initialize.return_value = None
    mock_session.call_tool.return_value = provider_response
    write_alerts = MagicMock()
    write_status = MagicMock(return_value="written")

    with (
        patch.dict(os.environ, {"FIELDKIT_USER_EMAIL": "ae@example.com"}),  # pii-guard: ignore
        patch("fieldkit.watch.mcp.MCPSession", return_value=mock_session),
        patch("fieldkit.watch.draft_queue.write_alerts", write_alerts),
        patch("fieldkit.watch.draft_queue.watcher_logging"),
        patch("fieldkit.watch.draft_queue.write_run_status", write_status),
    ):
        result = dq._run_draft_queue(dry_run=False)

    assert result.exit_code == 1
    write_alerts.assert_not_called()
    assert write_status.call_args.kwargs["outcome"] == "fatal"


def test_run_draft_queue_dry_run_returns_exit_0_no_mcp(tmp_path: Path) -> None:
    """dry-run skips MCP and leaves no alert, status, or log files."""
    watchers_dir = tmp_path / "watchers"
    alerts_path = watchers_dir / "draft-queue-alerts.md"

    mock_session_cls = MagicMock()

    with (
        patch("fieldkit.watch.mcp.MCPSession", mock_session_cls),
        patch.object(dq, "_alerts_file", return_value=alerts_path),
        patch.object(dq, "get_watchers_dir", return_value=watchers_dir),
        patch("fieldkit.watch.logging.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.draft_queue.write_run_status", return_value="skipped"),
    ):
        result = dq._run_draft_queue(dry_run=True)

    assert result.exit_code == 0
    assert result.completed is True
    assert result.status_write == "skipped"
    mock_session_cls.assert_not_called()
    assert not alerts_path.exists()
    assert list(tmp_path.rglob("*")) == []


def test_run_draft_queue_empty_mcp_response_writes_no_stale_message(tmp_path: Path) -> None:
    """Empty draft list from MCP → file written with 'No stale drafts found.'"""
    watchers_dir = tmp_path / "watchers"
    alerts_path = watchers_dir / "draft-queue-alerts.md"

    mock_session = MagicMock()
    mock_session.initialize.return_value = None
    mock_session.call_tool.return_value = []

    with (
        patch.dict(os.environ, {"FIELDKIT_USER_EMAIL": "ae@example.com"}),  # pii-guard: ignore
        patch("fieldkit.watch.mcp.MCPSession", return_value=mock_session),
        patch.object(dq, "_alerts_file", return_value=alerts_path),
        patch.object(dq, "get_watchers_dir", return_value=watchers_dir),
        patch("fieldkit.watch.draft_queue.watcher_logging"),
        patch("fieldkit.watch.draft_queue.write_run_status", return_value="written"),
    ):
        result = dq._run_draft_queue(dry_run=False)

    assert result.exit_code == 0
    content = alerts_path.read_text(encoding="utf-8")
    assert "No stale drafts found." in content


# ---------------------------------------------------------------------------
# B7: FIELDKIT_USER_EMAIL missing — error message and no MCP session opened
# ---------------------------------------------------------------------------


# ── TestMissingUserEmail (flattened) ────────────────────────────────────────


def test_resolve_user_email_missing_email_is_invalid_config(tmp_path: Path) -> None:
    """No email from any source is invalid configuration before MCP access.

    Patches out FIELDKIT_USER_EMAIL, USER, and the config fallback so that
    _resolve_user_email() returns an empty string — the only condition that
    triggers exit 1.  (Previously only FIELDKIT_USER_EMAIL was patched; the
    USER env-var fallback added by historic regression would resolve a valid email and
    the test would spuriously pass with exit 0.)
    """
    watchers_dir = tmp_path / "watchers"
    alerts_path = watchers_dir / "draft-queue-alerts.md"
    mock_session_cls = MagicMock()

    with (
        # Stub get_user_email_from_env to return None so _resolve_user_email() → ""
        patch("fieldkit.watch.draft_queue.get_user_email_from_env", return_value=None),
        patch("fieldkit.watch.mcp.MCPSession", mock_session_cls),
        patch.object(dq, "_alerts_file", return_value=alerts_path),
        patch.object(dq, "get_watchers_dir", return_value=watchers_dir),
        patch("fieldkit.watch.draft_queue.watcher_logging"),
        patch("fieldkit.watch.draft_queue.write_run_status", return_value="written"),
        pytest.raises(ConfigError, match="requires a user email"),
    ):
        dq._run_draft_queue(dry_run=False)

    # B8: MCPSession must never be instantiated when email is missing
    mock_session_cls.assert_not_called()


def test_resolve_user_email_missing_email_guidance_is_clear(tmp_path: Path) -> None:
    """The bounded configuration error names the supported setting."""

    watchers_dir = tmp_path / "watchers"
    alerts_path = watchers_dir / "draft-queue-alerts.md"

    with (
        # Stub get_user_email_from_env to return None so _resolve_user_email() → ""
        patch("fieldkit.watch.draft_queue.get_user_email_from_env", return_value=None),
        patch("fieldkit.watch.mcp.MCPSession", MagicMock()),
        patch.object(dq, "_alerts_file", return_value=alerts_path),
        patch.object(dq, "get_watchers_dir", return_value=watchers_dir),
        patch("fieldkit.watch.draft_queue.watcher_logging"),
        patch("fieldkit.watch.draft_queue.write_run_status", return_value="written"),
        pytest.raises(ConfigError, match="FIELDKIT_USER_EMAIL"),
    ):
        dq._run_draft_queue(dry_run=False)


# ---------------------------------------------------------------------------
# historic regression: dry-run must not log alert content to log.info (appears exactly once)
# ---------------------------------------------------------------------------


# ── TestBug209DryRunNoDuplicateContent (flattened) ──────────────────────────


def test_run_draft_queue_dry_run_content_appears_exactly_once_in_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Alert content must appear exactly once (via click.echo), not duplicated via log.info."""
    alerts_path = tmp_path / "watchers" / "draft-queue-alerts.md"
    drafts = [
        {
            "subject": "Unique Subject XYZ",
            "to": "test@example.com",  # pii-guard: ignore
            "age": "2d 0h",
            "draft_id": "d99",
        }  # pii-guard: ignore
    ]  # pii-guard: ignore

    with (
        patch.object(dq, "_alerts_file", return_value=alerts_path),
        patch.object(dq, "get_watchers_dir", return_value=tmp_path / "watchers"),
    ):
        dq.write_alerts(drafts, dry_run=True)

    captured = capsys.readouterr()
    # Content must appear exactly once in stdout
    assert captured.out.count("Unique Subject XYZ") == 1, (
        f"Expected alert content exactly once in stdout, got {captured.out.count('Unique Subject XYZ')} occurrences"
    )


def test_run_draft_queue_dry_run_log_info_does_not_contain_alert_text(tmp_path: Path) -> None:
    """log.info in dry-run path must NOT receive the alert_text as an argument."""
    alerts_path = tmp_path / "watchers" / "draft-queue-alerts.md"
    drafts = [
        {"subject": "Secret Content", "to": "test@example.com", "age": "1d 0h", "draft_id": "d1"}  # pii-guard: ignore
    ]  # pii-guard: ignore

    log_calls: list[Any] = []

    with (
        patch.object(dq, "_alerts_file", return_value=alerts_path),
        patch.object(dq, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(dq.log, "info", side_effect=lambda *args, **kw: log_calls.append(args)),
    ):
        dq.write_alerts(drafts, dry_run=True)

    # None of the log.info calls should contain the alert text body
    for call_args in log_calls:
        for arg in call_args:
            assert "Secret Content" not in str(arg), f"Alert content leaked into log.info: {call_args!r}"


# ---------------------------------------------------------------------------
# C2 (implementation note): _resolve_user_email uses get_user_email_from_env
# ---------------------------------------------------------------------------


# ── TestResolveUserEmail (flattened) ────────────────────────────────────────


def test_resolve_user_email_returns_email_from_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """FIELDKIT_USER_EMAIL set → _resolve_user_email() returns that value."""
    monkeypatch.setenv("FIELDKIT_USER_EMAIL", "you@example.com")  # pii-guard: ignore
    assert dq._resolve_user_email() == "you@example.com"  # pii-guard: ignore


def test_resolve_user_email_returns_empty_string_when_no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env vars set → _resolve_user_email() returns empty string."""
    monkeypatch.delenv("FIELDKIT_USER_EMAIL", raising=False)
    monkeypatch.delenv("USER", raising=False)
    # Patch get_user_email_from_env to return None (no config available)
    with patch("fieldkit.watch.draft_queue.get_user_email_from_env", return_value=None):
        result = dq._resolve_user_email()
    assert result == ""


def test_resolve_user_email_no_hardcoded_org_domain_in_source() -> None:
    """implementation note: no hardcoded org email domain must appear in draft_queue.py source."""
    import inspect

    # Split the forbidden literal so pii-guard does not flag this test file itself.
    _forbidden = "red" + "hat"  # pii-guard: ignore
    # Check both the domain module and the commands module
    source_domain = inspect.getsource(dq)
    source_cmd = inspect.getsource(dq_cmd)
    assert _forbidden not in source_domain, (
        "Hardcoded org domain found in watch/draft_queue.py — use get_user_email_from_env()"
    )
    assert _forbidden not in source_cmd, (
        "Hardcoded org domain found in commands/watch/draft_queue.py — use get_user_email_from_env()"
    )
