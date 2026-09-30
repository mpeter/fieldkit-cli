"""Tests for routines/watch_slack_threads.py.

Covers:
- Auth expiry path: SlackAuthError raised → write_slack_error_alert() writes a
  sanitized named artifact and main() exits 2
- Unanswered thread >48h: classify_message() returns alert dict; alert written to
  slack-thread-alerts.md
- Recent thread (<48h): classify_message() returns None (no alert)
- Self-sent message: classify_message() returns None (sender == current_username)
- _is_auth_error() signal detection (various auth error strings)
- write_slack_error_alert() dry_run=True writes nothing
- append_thread_alert() output format (account, channel, age, sender, timestamp)
- append_thread_alert() dry_run=True writes nothing
- load_state() / save_state() round-trip
- load_state() returns {} for missing/corrupt file
- _ts_to_datetime() epoch string → UTC datetime conversion
- main() auth expiry handled with exit 2 and a named error artifact written
"""

import datetime
import json
import logging
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from fieldkit.watch.status import RunStatusWriteResult, WatcherRunResult
from fieldkit.watch.status import write_run_status as real_write_run_status

# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------

_TOOLS_ROOT = Path(__file__).resolve().parents[1]

from click.testing import CliRunner  # noqa: E402

import fieldkit.watch.slack_thread_classification as classification  # noqa: E402
import fieldkit.watch.slack_threads as wst  # noqa: E402
from fieldkit.commands.watch.slack_threads import cli  # noqa: E402
from fieldkit.config import TIMEOUT_MCP_TOOL, TIMEOUT_PROCESS_KILL_GRACE  # noqa: E402
from fieldkit.errors import FieldkitError  # noqa: E402
from fieldkit.util.bounded_process import (  # noqa: E402
    BoundedProcessBytesResult,
    BoundedProcessError,
    ProcessFailureReason,
)
from fieldkit.watch.slack_thread_classification import SlackAccountThread  # noqa: E402


@pytest.mark.unit
def test_slack_auth_error_inherits_from_fieldkit_error() -> None:
    error = wst.SlackAuthError("failure")
    assert isinstance(error, FieldkitError)


pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "payload",
    [
        {"error": "fictional private provider payload"},
        {},
        {"total": 0},
        {"matches": []},
        {"total": None, "matches": []},
        {"total": False, "matches": []},
        {"total": "0", "matches": []},
        {"total": 0.0, "matches": []},
        {"total": -1, "matches": []},
        {"total": 0, "matches": None},
        {"total": 0, "matches": {}},
        {"total": 1, "matches": [None]},
        {"total": 1, "matches": ["fictional private provider payload"]},
        {"total": 0, "matches": [], "error": "fictional private provider payload"},
        [],
        None,
    ],
)
def test_decoded_search_envelope_fails_closed(payload: object) -> None:
    with (
        patch.object(wst, "_run_slack_search_once", return_value=(0, json.dumps(payload).encode(), b"")),
        pytest.raises(RuntimeError, match="slackcli returned an invalid search response") as error,
    ):
        wst.run_slack_search("acme", limit=50)
    assert "fictional private provider payload" not in str(error.value)


@pytest.mark.parametrize(
    "payload", [{"total": 0, "matches": []}, {"query": "acme", "total": 0, "matches": [], "page": 1, "pages": 1}]
)
def test_valid_zero_match_envelope_is_success(payload: dict[str, Any]) -> None:
    with patch.object(wst, "_run_slack_search_once", return_value=(0, json.dumps(payload).encode(), b"")):
        result = wst.run_slack_search("acme", limit=50)
    assert result == payload


@pytest.mark.parametrize("stdout", [b"", b" \n"])
def test_successful_producer_zero_match_empty_stdout_is_success(stdout: bytes) -> None:
    with patch.object(wst, "_run_slack_search_once", return_value=(0, stdout, b"No messages found")):
        result = wst.run_slack_search("acme", limit=50)
    assert result == {"query": "acme", "total": 0, "matches": []}


def test_unsuccessful_empty_stdout_cannot_manufacture_zero_match_success() -> None:
    with (
        patch.object(wst, "_run_slack_search_once", return_value=(1, b"", b"fictional private provider payload")),
        pytest.raises(RuntimeError, match="slackcli search failed") as error,
    ):
        wst.run_slack_search("acme", limit=50)
    assert "fictional private provider payload" not in str(error.value)


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("completed_accounts", [0, 1])
@pytest.mark.parametrize(
    "payload",
    [
        {"error": "fictional private provider payload"},
        {"matches": []},
        {"total": False, "matches": []},
        {"total": 0, "matches": {}},
        None,
    ],
)
def test_decoded_provider_error_is_incomplete_at_domain_and_real_cli(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    dry_run: bool,
    completed_accounts: int,
    payload: object,
) -> None:
    from fieldkit.__main__ import main

    payloads = [(0, b'{"total":0,"matches":[]}', b"")] * completed_accounts
    payloads.append((0, json.dumps(payload).encode(), b""))
    accounts = {"acme": {}, "other": {}} if completed_accounts else {"acme": {}}
    with (
        patch.object(wst, "get_accounts_config", return_value={"accounts": accounts}),
        patch.object(wst, "load_current_username", return_value=None),
        patch.object(wst, "load_state", return_value={}),
        patch.object(wst, "_run_slack_search_once", side_effect=payloads),
        patch.object(wst, "get_watchers_dir", return_value=tmp_path),
        patch.object(wst, "_alerts_file", return_value=tmp_path / "alerts.md"),
        patch.object(wst, "_state_file", return_value=tmp_path / "state.json"),
        patch("fieldkit.watch.status.get_fieldkit_home", return_value=tmp_path),
        patch.object(wst, "write_run_status", real_write_run_status),
    ):
        result = wst._run_slack_threads(threshold_hours=48, account=None, limit=50, dry_run=dry_run)
        with patch.object(wst, "_run_slack_search_once", side_effect=payloads):
            exit_code = main(["watch", "run", "slack-threads"] + (["--dry-run"] if dry_run else []))

    assert result.run == WatcherRunResult(
        "partial" if completed_accounts else "fatal", False, None if dry_run else "written"
    )
    assert result.provider_error is True
    assert result.auth_error is False
    assert result.records_checked == completed_accounts
    assert result.failures == 1
    assert exit_code == 1
    assert "fictional private provider payload" not in caplog.text
    assert not (tmp_path / "state.json").exists()
    if dry_run:
        assert list(tmp_path.iterdir()) == []
    else:
        saved = json.loads((tmp_path / "watchers/watcher-run-status.json").read_text(encoding="utf-8"))["slack-threads"]
        assert saved["outcome"] == result.run.outcome
        assert saved["failures"] == 1
        assert "fictional private provider payload" not in (tmp_path / "alerts.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("error,code", [(RuntimeError("provider"), 1), (wst.SlackAuthError("auth"), 2)])
def test_interrupted_scan_retains_partial_but_is_incomplete(tmp_path: Path, error: Exception, code: int) -> None:
    with (
        patch.object(wst, "get_accounts_config", return_value={"accounts": {"acme": {}, "other": {}}}),
        patch.object(wst, "load_current_username", return_value=None),
        patch.object(wst, "load_state", return_value={}),
        patch.object(wst, "scan_account_threads", side_effect=[[], error]),
        patch.object(wst, "get_watchers_dir", return_value=tmp_path),
        patch.object(wst, "_alerts_file", return_value=tmp_path / "alerts.md"),
        patch.object(wst, "_state_file", return_value=tmp_path / "state.json"),
        patch("fieldkit.watch.status.get_fieldkit_home", return_value=tmp_path),
        patch.object(wst, "write_run_status", real_write_run_status),
    ):
        result = wst._run_slack_threads(threshold_hours=48, account=None, limit=50, dry_run=False)
    assert result.run.outcome == "partial"
    assert result.run.exit_code == code
    assert result.run.completed is False
    assert result.run.status_write == "written"
    assert result.run.completed_partial is False
    assert result.records_checked == 1
    assert result.failures == 1
    assert result.alerts_generated == 1
    recorded = json.loads((tmp_path / "watchers" / "watcher-run-status.json").read_text(encoding="utf-8"))[
        "slack-threads"
    ]
    assert recorded["outcome"] == result.run.outcome
    assert recorded["failures"] == result.failures
    if code == 1:
        assert not (tmp_path / "state.json").exists()


@pytest.mark.parametrize("auth,expected", [(True, 5), (False, 4)])
def test_each_interrupted_scan_operation_failure_is_counted(tmp_path: Path, auth: bool, expected: int) -> None:
    error = wst.SlackAuthError("auth") if auth else RuntimeError("provider")
    with (
        patch.object(wst, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch.object(wst, "load_current_username", return_value=None),
        patch.object(wst, "load_state", return_value={}),
        patch.object(wst, "scan_account_threads", side_effect=error),
        patch.object(wst, "write_slack_error_alert", side_effect=OSError("alert")) as alert,
        patch.object(wst, "save_state", side_effect=OSError("state")) as state,
        patch.object(wst, "append_run_summary", side_effect=OSError("summary")) as summary,
        patch.object(wst, "write_run_status", return_value="failed"),
    ):
        result = wst._run_slack_threads(threshold_hours=48, account=None, limit=50, dry_run=False)
    assert result.failures == expected
    assert result.run.outcome == "fatal"
    assert result.run.exit_code == (2 if auth else 1)
    alert.assert_called_once()
    summary.assert_called_once()
    assert state.call_count == int(auth)


@pytest.mark.parametrize("target", ["state", "status"])
def test_actual_required_replacement_failure_is_fatal(tmp_path: Path, target: str) -> None:
    state_path = tmp_path / "state.json"
    status_path = tmp_path / "watchers" / "watcher-run-status.json"
    failed_path = state_path if target == "state" else status_path
    failed_path.parent.mkdir(parents=True, exist_ok=True)
    failed_path.write_text("{}\n", encoding="utf-8")
    previous = failed_path.read_bytes()
    replace = Path.replace

    def fail_required_replacement(path: Path, destination: str | Path) -> Path:
        if Path(destination) == failed_path:
            raise OSError("replacement failed")
        return replace(path, destination)

    with (
        patch.object(wst, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch.object(wst, "load_current_username", return_value=None),
        patch.object(wst, "load_state", return_value={}),
        patch.object(wst, "scan_account_threads", return_value=[]),
        patch.object(wst, "get_watchers_dir", return_value=tmp_path),
        patch.object(wst, "_alerts_file", return_value=tmp_path / "alerts.md"),
        patch.object(wst, "_state_file", return_value=state_path),
        patch("fieldkit.watch.status.get_fieldkit_home", return_value=tmp_path),
        patch.object(wst, "write_run_status", real_write_run_status),
        patch.object(Path, "replace", fail_required_replacement),
    ):
        result = wst._run_slack_threads(threshold_hours=48, account=None, limit=50, dry_run=False)
    assert result.run.outcome == "fatal"
    assert result.run.completed is True
    assert result.run.exit_code == 1
    assert result.run.status_write == ("failed" if target == "status" else "written")
    assert result.failures == 1
    assert result.records_checked == 1
    assert failed_path.read_bytes() == previous


def test_failed_thread_alerts_are_each_attempted_and_not_counted_as_published() -> None:
    thread = _make_thread_append_thread_alert()
    with (
        patch.object(wst, "scan_account_threads", return_value=[thread, thread]),
        patch.object(wst, "append_thread_alert", side_effect=OSError("alert")) as alert,
    ):
        result = wst._scan_all_accounts(
            {"acme": {}},
            account_filter=None,
            current_username=None,
            threshold_hours=48,
            limit=50,
            now_utc=datetime.datetime.now(datetime.UTC),
            run_ts="2026-09-29T12:00:00Z",
            updated_state={},
            previous_state={},
            dry_run=False,
        )
    assert result == (1, 0, None, 2)
    assert alert.call_count == 2


def test_state_paths_use_the_configured_roots(tmp_path: Path) -> None:
    """Path helpers keep Slack state and identity configuration in their roots."""
    with (
        patch.object(wst, "get_fieldkit_home", return_value=tmp_path / "home"),
        patch.object(wst, "get_watchers_dir", return_value=tmp_path / "watchers"),
    ):
        wst._identity_config.cache_clear()
        wst._alerts_file.cache_clear()
        wst._state_file.cache_clear()
        identity_path = wst._identity_config()
        alerts_path = wst._alerts_file()
        state_path = wst._state_file()

    assert identity_path == tmp_path / "home" / "config" / "identity.yaml"
    assert alerts_path == tmp_path / "watchers" / "slack-thread-alerts.md"
    assert state_path == tmp_path / "watchers" / "slack-thread-state.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW_UTC = datetime.datetime(2026, 5, 26, 9, 0, 0, tzinfo=datetime.UTC)
_NOW_TS = _NOW_UTC.timestamp()


def _epoch_str(hours_ago: float) -> str:
    """Return a Slack epoch-string for a message sent hours_ago."""
    ts = _NOW_TS - (hours_ago * 3600.0)
    return f"{ts:.6f}"


def _make_slack_msg(
    *,
    hours_ago: float = 72.0,
    username: str = "alice",
    user: str = "U001",
    channel_name: str = "acme-general",
    channel_id: str = "C001",
    text: str = "Has anyone seen the contract?",
    permalink: str = "https://app.slack.com/archives/C001/p1234567890",
) -> dict[str, object]:
    """Build a minimal Slack search match dict."""
    return {
        "ts": _epoch_str(hours_ago),
        "username": username,
        "user": user,
        "channel": {"name": channel_name, "id": channel_id},
        "text": text,
        "permalink": permalink,
    }


# ---------------------------------------------------------------------------
# historic regression: subprocess retry
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_slack_search_retries_timeout_then_succeeds() -> None:
    """historic regression: a subprocess.TimeoutExpired on slackcli is retried and recovers."""
    call_count = {"n": 0}

    def _fake_run(*_args: Any, **_kwargs: Any) -> BoundedProcessBytesResult:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise BoundedProcessError("private timeout detail", reason="timeout")
        return BoundedProcessBytesResult(0, b'{"query": "test", "total": 0, "matches": []}', b"")

    with patch.object(wst, "run_bounded_process_bytes", side_effect=_fake_run), patch("time.sleep"):
        result = wst.run_slack_search("test", limit=10)

    assert call_count["n"] == 2, "Expected exactly 2 subprocess calls (1 timeout + 1 success)"
    assert result == {"query": "test", "total": 0, "matches": []}


@pytest.mark.unit
def test_run_slack_search_all_retries_exhausted_raises_runtime_error() -> None:
    """historic regression: persistent subprocess.TimeoutExpired still raises after retries."""
    call_count = {"n": 0}

    def _fake_run(*_args: Any, **_kwargs: Any) -> BoundedProcessBytesResult:
        call_count["n"] += 1
        raise BoundedProcessError("private timeout detail", reason="timeout")

    with (
        patch.object(wst, "run_bounded_process_bytes", side_effect=_fake_run),
        patch("time.sleep"),
        pytest.raises(RuntimeError, match="slackcli timed out after 60s"),
    ):
        wst.run_slack_search("test", limit=10)

    assert call_count["n"] == 3, "Expected exactly 3 attempts (stop_after_attempt(3))"


@pytest.mark.parametrize(
    ("reason", "exception_type", "message"),
    [
        ("start", FileNotFoundError, "slackcli"),
        ("pipes", RuntimeError, "output pipes"),
        ("overflow", RuntimeError, "bounded capture"),
        ("cleanup", RuntimeError, "cleanup failed"),
    ],
)
def test_run_slack_search_maps_bounded_process_failures_without_private_payload(
    reason: ProcessFailureReason,
    exception_type: type[BaseException],
    message: str,
) -> None:
    failure = BoundedProcessError("private provider path and payload", reason=reason)
    with (
        patch.object(wst, "run_bounded_process_bytes", side_effect=failure),
        pytest.raises(exception_type, match=message) as captured,
    ):
        wst._run_slack_search_once(["slackcli"])

    assert "private" not in str(captured.value)


def test_run_slack_search_uses_canonical_bounded_process_contract() -> None:
    completed = BoundedProcessBytesResult(0, b"stdout", b"stderr")
    command = ["slackcli", "search", "messages", "acme"]

    with patch.object(wst, "run_bounded_process_bytes", return_value=completed) as run_process:
        result = wst._run_slack_search_once(command)

    assert result == (0, b"stdout", b"stderr")
    run_process.assert_called_once_with(
        command,
        timeout=TIMEOUT_MCP_TOOL,
        stdout_limit=wst._MAX_SLACKCLI_STREAM_BYTES,
        stderr_limit=wst._MAX_SLACKCLI_STREAM_BYTES,
        cleanup_timeout=TIMEOUT_PROCESS_KILL_GRACE,
    )


def test_run_slack_search_timeout_kills_descendant_pipe_holders() -> None:
    """A timed-out search cannot hang on a descendant that inherited its pipes."""
    child = "import time; time.sleep(60)"
    parent = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(60)"
    started = time.monotonic()

    with (
        patch.object(wst, "TIMEOUT_MCP_TOOL", 0.05),
        patch("time.sleep"),
        pytest.raises(subprocess.TimeoutExpired),
    ):
        wst._run_slack_search_once([sys.executable, "-c", parent])

    assert time.monotonic() - started < 5


def test_run_slack_search_wrapper_does_not_create_tempfiles() -> None:
    """The real subprocess wrapper must remain filesystem-free for dry runs."""
    payload = json.dumps({"query": "test", "total": 0, "matches": [], "padding": "x" * 100_000})
    with patch.object(tempfile, "TemporaryFile", side_effect=AssertionError("tempfile write")):
        returncode, stdout, stderr = wst._run_slack_search_once(
            [sys.executable, "-c", f"import sys; sys.stdout.write({payload!r})"]
        )

    assert returncode == 0
    parsed = json.loads(stdout)
    assert parsed["query"] == "test"
    assert len(parsed["padding"]) == 100_000
    assert stderr == b""


@pytest.mark.parametrize(
    ("returncode", "stderr", "exception_type"),
    [
        (1, b"invalid_auth token=secret-customer-value", wst.SlackAuthError),
        (7, b"provider rejected private-account-name", RuntimeError),
    ],
)
def test_slack_search_exceptions_do_not_expose_provider_output(
    returncode: int, stderr: bytes, exception_type: type[Exception]
) -> None:
    with (
        patch.object(wst, "_run_slack_search_once", return_value=(returncode, b"", stderr)),
        pytest.raises(exception_type) as captured,
    ):
        wst.run_slack_search("private query", limit=10)

    diagnostic = str(captured.value)
    assert "secret-customer-value" not in diagnostic
    assert "private-account-name" not in diagnostic
    assert "private query" not in diagnostic


def test_account_scan_logs_only_counts_not_account_query_or_channel(caplog: pytest.LogCaptureFixture) -> None:
    search_result = {
        "total": 1,
        "matches": [_make_slack_msg(channel_name="private-customer-channel", text="private customer message")],
    }
    caplog.set_level(logging.DEBUG, logger=wst.__name__)

    with patch.object(wst, "run_slack_search", return_value=search_result):
        result = wst.scan_account_threads(
            "private-account",
            {"keywords": ["private customer query"], "account_channels": ["expected-channel"]},
            current_username="tester",
            threshold_hours=48,
            search_limit=50,
            now_utc=_NOW_UTC,
        )

    assert result == []
    logged = caplog.text
    for private_value in (
        "private-account",
        "private customer query",
        "private-customer-channel",
        "private customer message",
    ):
        assert private_value not in logged
    assert "total=1 inspecting=1" in logged


# ---------------------------------------------------------------------------
# _is_auth_error
# ---------------------------------------------------------------------------


# ── TestIsAuthError (flattened) ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "returncode,output,expected",
    [
        (1, "not authenticated", True),
        (1, "auth_required error", True),
        (1, "invalid_auth: bad token", True),
        (1, "token_revoked", True),
        (1, "not_authed", True),
        (1, "token expired", True),
        (1, "account_inactive", True),
        (1, "login required", True),
        (1, "plugin initialization failed", False),
        (1, "catalog ingestion failed", False),
        (0, "success", False),
        (1, "unrelated error", False),
        (2, "command not found", False),
    ],
)
def test_is_auth_error_signal_detection(returncode: int, output: str, expected: bool) -> None:
    assert wst._is_auth_error(returncode, output) == expected


# ---------------------------------------------------------------------------
# _ts_to_datetime
# ---------------------------------------------------------------------------


# ── TestTsToDatetime (flattened) ─────────────────────────────────────────────


def test_ts_to_datetime_valid_epoch_string() -> None:
    ts = "1716000000.123456"
    result = classification._ts_to_datetime(ts)
    assert result is not None
    assert result.tzinfo == datetime.UTC
    assert isinstance(result, datetime.datetime)


def test_ts_to_datetime_returns_none_for_none() -> None:
    assert classification._ts_to_datetime(None) is None


def test_ts_to_datetime_returns_none_for_invalid_string() -> None:
    assert classification._ts_to_datetime("not-a-number") is None


# ---------------------------------------------------------------------------
# classify_message
# ---------------------------------------------------------------------------


# ── TestClassifyMessage (flattened) ─────────────────────────────────────────────


def test_classify_message_unanswered_thread_over_threshold_returns_result() -> None:
    msg = _make_slack_msg(hours_ago=72, username="alice")
    result = classification.classify_message(
        msg,
        current_username="testuser",
        threshold_hours=48,
        now_utc=_NOW_UTC,
    )
    assert result is not None
    assert result["channel"] == "acme-general"
    assert result["sender_username"] == "alice"
    assert result["age_hours"] == pytest.approx(72.0, abs=0.1)


def test_classify_message_recent_message_below_threshold_returns_none() -> None:
    msg = _make_slack_msg(hours_ago=12)
    result = classification.classify_message(
        msg,
        current_username="testuser",
        threshold_hours=48,
        now_utc=_NOW_UTC,
    )
    assert result is None


def test_classify_message_self_sent_message_returns_none() -> None:
    """Messages from the current user should not trigger an alert."""
    msg = _make_slack_msg(hours_ago=72, username="testuser")
    result = classification.classify_message(
        msg,
        current_username="testuser",
        threshold_hours=48,
        now_utc=_NOW_UTC,
    )
    assert result is None


def test_classify_message_self_sent_by_user_id_returns_none() -> None:
    """Self-detection also works when sender matched by user ID."""
    msg = _make_slack_msg(hours_ago=72, username="", user="testuser")
    result = classification.classify_message(
        msg,
        current_username="testuser",
        threshold_hours=48,
        now_utc=_NOW_UTC,
    )
    assert result is None


def test_classify_message_no_current_username_does_not_filter() -> None:
    """Without a current_username, all old-enough messages are returned."""
    msg = _make_slack_msg(hours_ago=72, username="alice")
    result = classification.classify_message(
        msg,
        current_username=None,
        threshold_hours=48,
        now_utc=_NOW_UTC,
    )
    assert result is not None


def test_classify_message_exactly_at_threshold_returns_result() -> None:
    """Message age == threshold is NOT skipped (skip condition is strict <)."""
    msg = _make_slack_msg(hours_ago=48.0)
    result = classification.classify_message(
        msg,
        current_username="testuser",
        threshold_hours=48,
        now_utc=_NOW_UTC,
    )
    assert result is not None  # age == threshold triggers alert


def test_classify_message_one_second_past_threshold_returns_result() -> None:  # pii-guard: ignore
    msg = _make_slack_msg(hours_ago=48.01)
    result = classification.classify_message(
        msg,
        current_username="testuser",
        threshold_hours=48,
        now_utc=_NOW_UTC,
    )
    assert result is not None


def test_classify_message_result_contains_expected_fields() -> None:
    msg = _make_slack_msg(hours_ago=96, username="bob", channel_name="globalpay-tech")
    result = classification.classify_message(
        msg,
        current_username="testuser",
        threshold_hours=48,
        now_utc=_NOW_UTC,
    )
    assert result is not None
    assert set(result) == {
        "channel",
        "channel_id",
        "ts",
        "message_dt",
        "age_hours",
        "sender_username",
        "permalink",
        "text_snippet",
    }


def test_classify_message_missing_ts_returns_none() -> None:
    msg = _make_slack_msg(hours_ago=72)
    del msg["ts"]
    result = classification.classify_message(
        msg,
        current_username="testuser",
        threshold_hours=48,
        now_utc=_NOW_UTC,
    )
    assert result is None


def test_classify_message_text_snippet_truncated_at_120_chars() -> None:
    long_text = "x" * 200
    msg = _make_slack_msg(hours_ago=72, text=long_text)
    result = classification.classify_message(
        msg,
        current_username="testuser",
        threshold_hours=48,
        now_utc=_NOW_UTC,
    )
    assert result is not None
    assert len(result["text_snippet"]) <= 120


# ---------------------------------------------------------------------------
# write_slack_error_alert
# ---------------------------------------------------------------------------


# ── TestWriteAuthErrorAlert (flattened) ─────────────────────────────────────────────


def test_write_slack_error_alert_writes_named_error_entry(tmp_path: Path) -> None:
    alerts_file = tmp_path / "slack-thread-alerts.md"
    with (
        patch.object(wst, "_alerts_file", return_value=alerts_file),
        patch.object(wst, "get_watchers_dir", return_value=tmp_path),
    ):
        wst.write_slack_error_alert(dry_run=False, failure_kind="auth")

    content = alerts_file.read_text()
    assert "auth expired" in content.lower() or "auth-error" in content
    assert "slackcli" in content
    assert "Slack authentication failed; run slackcli auth login." in content


def test_write_slack_error_alert_dry_run_does_not_write_file(tmp_path: Path) -> None:
    alerts_file = tmp_path / "slack-thread-alerts.md"
    with (
        patch.object(wst, "_alerts_file", return_value=alerts_file),
        patch.object(wst, "get_watchers_dir", return_value=tmp_path),
    ):
        wst.write_slack_error_alert(dry_run=True, failure_kind="auth")

    assert not alerts_file.exists()


def test_write_slack_error_alert_uses_sanitized_detail(tmp_path: Path) -> None:
    alerts_file = tmp_path / "slack-thread-alerts.md"
    with (
        patch.object(wst, "_alerts_file", return_value=alerts_file),
        patch.object(wst, "get_watchers_dir", return_value=tmp_path),
    ):
        wst.write_slack_error_alert(dry_run=False, failure_kind="auth")

    content = alerts_file.read_text()
    assert "Slack authentication failed; run slackcli auth login." in content


# ---------------------------------------------------------------------------
# append_thread_alert
# ---------------------------------------------------------------------------


# ── TestAppendThreadAlert (flattened) ─────────────────────────────────────────────


def _make_thread_append_thread_alert(
    *,
    account: str = "acme",
    channel: str = "general",
    age_hours: float = 72.0,
    sender: str = "alice",
) -> SlackAccountThread:
    return {
        "account": account,
        "channel": channel,
        "channel_id": "C001",
        "ts": _epoch_str(age_hours),
        "message_dt": "2026-05-23T09:00:00Z",
        "age_hours": age_hours,
        "sender_username": sender,
        "permalink": "https://app.slack.com/archives/C001/p1234",
        "text_snippet": "Let me know when the contract is ready.",
    }


def test_append_thread_alert_alert_written_to_file(tmp_path: Path) -> None:
    alerts_file = tmp_path / "slack-thread-alerts.md"
    thread = _make_thread_append_thread_alert()
    with (
        patch.object(wst, "_alerts_file", return_value=alerts_file),
        patch.object(wst, "get_watchers_dir", return_value=tmp_path),
    ):
        wst.append_thread_alert(thread, dry_run=False)

    content = alerts_file.read_text()
    assert "acme" in content  # pii-guard: ignore
    assert "general" in content
    assert "72" in content
    assert "alice" in content


def test_thread_alert_write_failure_marks_scan_partial(caplog: pytest.LogCaptureFixture) -> None:
    thread = _make_thread_append_thread_alert()
    with (
        patch.object(wst, "scan_account_threads", return_value=[thread]),
        patch.object(wst, "append_thread_alert", side_effect=OSError("/private/alerts/path")),
        caplog.at_level(logging.ERROR),
    ):
        checked, alerts, failure_kind, persistence_failed = wst._scan_all_accounts(
            {"acme": {}},
            account_filter=None,
            current_username="tester",
            threshold_hours=48,
            limit=50,
            now_utc=datetime.datetime.now(datetime.UTC),
            run_ts="2026-09-27T12:00:00Z",
            updated_state={},
            previous_state={},
            dry_run=False,
        )

    assert checked == 1
    assert alerts == 0
    assert failure_kind is None
    assert persistence_failed == 1
    assert "/private/alerts/path" not in caplog.text


def test_append_thread_alert_dry_run_does_not_write_file(tmp_path: Path) -> None:
    alerts_file = tmp_path / "slack-thread-alerts.md"
    thread = _make_thread_append_thread_alert()
    with (
        patch.object(wst, "_alerts_file", return_value=alerts_file),  # pii-guard: ignore
        patch.object(wst, "get_watchers_dir", return_value=tmp_path),
    ):
        wst.append_thread_alert(thread, dry_run=True)

    assert not alerts_file.exists()


def test_append_thread_alert_alert_appends_on_second_call(tmp_path: Path) -> None:
    alerts_file = tmp_path / "slack-thread-alerts.md"
    t1 = _make_thread_append_thread_alert(account="acme", channel="general")
    t2 = _make_thread_append_thread_alert(account="globalpay", channel="tech")
    with (
        patch.object(wst, "_alerts_file", return_value=alerts_file),
        patch.object(wst, "get_watchers_dir", return_value=tmp_path),
    ):
        wst.append_thread_alert(t1, dry_run=False)
        wst.append_thread_alert(t2, dry_run=False)

    content = alerts_file.read_text()
    assert "acme" in content
    assert "globalpay" in content


def test_append_thread_alert_timestamp_present_in_alert(tmp_path: Path) -> None:
    import re

    alerts_file = tmp_path / "slack-thread-alerts.md"
    thread = _make_thread_append_thread_alert()
    with (
        patch.object(wst, "_alerts_file", return_value=alerts_file),
        patch.object(wst, "get_watchers_dir", return_value=tmp_path),
    ):
        wst.append_thread_alert(thread, dry_run=False)

    content = alerts_file.read_text()
    assert re.search(r"\d{4}-\d{2}-\d{2}", content)


# ---------------------------------------------------------------------------
# load_state / save_state
# ---------------------------------------------------------------------------


# ── TestStateRoundTrip (flattened) ─────────────────────────────────────────────


def test_save_and_load(tmp_path: Path) -> None:
    state_file = tmp_path / "slack-thread-state.json"
    watchers_dir = tmp_path
    data = {
        "acme": {
            "checked_at": "2026-05-26T07:10:00Z",
            "unanswered_threads": 2,
            "threads": [],
        }
    }
    with (
        patch.object(wst, "_state_file", return_value=state_file),
        patch.object(wst, "get_watchers_dir", return_value=watchers_dir),
    ):
        wst.save_state(data)
        loaded = wst.load_state()
    assert loaded == data


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    state_file = tmp_path / "nonexistent.json"
    with patch.object(wst, "_state_file", return_value=state_file):
        assert wst.load_state() == {}


def test_load_corrupt_file_returns_empty(tmp_path: Path) -> None:
    state_file = tmp_path / "corrupt.json"
    state_file.write_text("NOT VALID JSON {{", encoding="utf-8")
    with patch.object(wst, "_state_file", return_value=state_file):
        assert wst.load_state() == {}


def test_save_state_oserror_propagates(tmp_path: Path) -> None:
    """save_state propagates storage failures from the shared persistence helper."""
    state_file = tmp_path / "state.json"
    with (
        patch.object(wst, "_state_file", return_value=state_file),
        patch.object(wst, "merge_state", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full") as exc_info,
    ):
        wst.save_state({"key": "value"})
    assert exc_info.type is OSError


def test_state_write_failure_is_fatal(tmp_path: Path) -> None:
    """A completed Slack scan must fail closed when its state cannot persist."""
    config = {"accounts": {"acme": {"keywords": ["Acme"]}}}
    with (
        patch.object(wst, "get_accounts_config", return_value=config),
        patch.object(wst, "load_current_username", return_value="operator"),
        patch.object(wst, "load_state", return_value={}),
        patch.object(wst, "_scan_all_accounts", return_value=(1, 0, None, False)),
        patch.object(wst, "_persist_and_summarise", return_value=True),
        patch.object(wst, "write_run_status", return_value="written") as write_status,
    ):
        rc = wst._run_slack_threads(threshold_hours=48, account=None, limit=50, dry_run=False)

    assert rc.run.outcome == "fatal"
    assert rc.run.exit_code == 1
    assert write_status.call_args.kwargs["outcome"] == "fatal"
    assert write_status.call_args.kwargs["failures"] == 1


@pytest.mark.parametrize("failure", [json.JSONDecodeError("bad", "[", 0), TypeError("non-object root")])
def test_state_persistence_shape_failures_are_bounded(failure: Exception, caplog: pytest.LogCaptureFixture) -> None:
    with (
        patch.object(wst, "save_state", side_effect=failure),
        patch.object(wst, "append_run_summary"),
        caplog.at_level(logging.ERROR),
    ):
        failed = wst._persist_and_summarise(
            updated_state={"acme": {}},
            previous_state={},
            failure_kind=None,
            dry_run=False,
            run_ts="2026-09-27T12:00:00Z",
            checked_accounts=1,
            total_alerts=0,
            elapsed=0.1,
        )

    assert failed == 1
    assert "non-object root" not in caplog.text
    assert "Traceback" not in caplog.text


# ---------------------------------------------------------------------------
# main() — auth expiry path: exit 2, named error artifact written
# ---------------------------------------------------------------------------


# ── TestMainAuthExpiry (flattened) ─────────────────────────────────────────────


def _make_accounts_yaml_main_auth_expiry(tmp_path: Path) -> Path:
    p = tmp_path / "accounts.yaml"
    p.write_text("accounts:\n  acme:\n    keywords:\n      - Acme Corp\n", encoding="utf-8")
    return p


def test_auth_error_exits_two(tmp_path: Path) -> None:
    accounts_yaml = _make_accounts_yaml_main_auth_expiry(tmp_path)
    alerts_file = tmp_path / "slack-thread-alerts.md"
    state_file = tmp_path / "slack-thread-state.json"

    with (
        patch("fieldkit.config._accounts.get_config_path", return_value=accounts_yaml),
        patch("fieldkit.watch.slack_threads.get_fieldkit_home", return_value=tmp_path),
        patch.object(wst, "get_watchers_dir", return_value=tmp_path),
        patch.object(wst, "_alerts_file", return_value=alerts_file),
        patch.object(wst, "_state_file", return_value=state_file),
        patch.object(
            wst,
            "run_slack_search",
            side_effect=wst.SlackAuthError("not_authed: token expired"),
        ),
    ):
        result = CliRunner().invoke(cli, [])

    assert result.exit_code == 2


def test_auth_error_writes_named_error_artifact(tmp_path: Path) -> None:
    accounts_yaml = _make_accounts_yaml_main_auth_expiry(tmp_path)
    alerts_file = tmp_path / "slack-thread-alerts.md"
    state_file = tmp_path / "slack-thread-state.json"

    with (
        patch("fieldkit.config._accounts.get_config_path", return_value=accounts_yaml),
        patch("fieldkit.watch.slack_threads.get_fieldkit_home", return_value=tmp_path),
        patch.object(wst, "get_watchers_dir", return_value=tmp_path),
        patch.object(wst, "_alerts_file", return_value=alerts_file),
        patch.object(wst, "_state_file", return_value=state_file),
        patch.object(
            wst,
            "run_slack_search",
            side_effect=wst.SlackAuthError("invalid_auth: workspace inactive"),
        ),
    ):
        CliRunner().invoke(cli, [])

    # Named error artifact must exist
    assert alerts_file.exists()
    content = alerts_file.read_text()
    assert "auth" in content.lower()
    # Should NOT be empty — the named error entry is required
    assert len(content.strip()) > 0


def test_auth_error_writes_error_to_state_json(tmp_path: Path) -> None:
    accounts_yaml = _make_accounts_yaml_main_auth_expiry(tmp_path)
    alerts_file = tmp_path / "slack-thread-alerts.md"
    state_file = tmp_path / "slack-thread-state.json"

    with (
        patch("fieldkit.config._accounts.get_config_path", return_value=accounts_yaml),
        patch("fieldkit.watch.slack_threads.get_fieldkit_home", return_value=tmp_path),
        patch.object(wst, "get_watchers_dir", return_value=tmp_path),
        patch.object(wst, "_alerts_file", return_value=alerts_file),
        patch.object(wst, "_state_file", return_value=state_file),
        patch.object(
            wst,
            "run_slack_search",
            side_effect=wst.SlackAuthError("token_revoked"),
        ),
    ):
        CliRunner().invoke(cli, [])

    # State JSON should record the auth error
    assert state_file.exists()
    state = json.loads(state_file.read_text())
    assert "__auth_error__" in state
    assert state["__auth_error__"]["error"] == "auth_expired"


# ---------------------------------------------------------------------------
# main() — unanswered thread writes alert
# ---------------------------------------------------------------------------


# ── TestMainThreadAlert (flattened) ─────────────────────────────────────────────


def _make_accounts_yaml_main_thread_alert(tmp_path: Path) -> Path:
    p = tmp_path / "accounts.yaml"
    p.write_text("accounts:\n  acme:\n    keywords:\n      - Acme Corp\n", encoding="utf-8")
    return p


def _stale_search_result_main_thread_alert() -> dict[str, object]:
    """Return a mocked run_slack_search result with one stale message."""
    ts = _epoch_str(72)
    return {
        "query": "Acme Corp",
        "total": 1,
        "matches": [
            {
                "ts": ts,
                "username": "bob",
                "user": "U002",
                "channel": {"name": "acme-general", "id": "C001"},
                "text": "Has anyone seen the contract?",
                "permalink": "https://app.slack.com/archives/C001/p1234",
            }
        ],
    }


def test_unanswered_thread_writes_alert(tmp_path: Path) -> None:
    alerts_file = tmp_path / "slack-thread-alerts.md"
    state_file = tmp_path / "slack-thread-state.json"
    acme_config = {"accounts": {"acme": {"keywords": ["Acme Corp"]}}}

    with (
        patch("fieldkit.watch.slack_threads.get_accounts_config", return_value=acme_config),
        patch.object(wst, "get_watchers_dir", return_value=tmp_path),
        patch.object(wst, "_alerts_file", return_value=alerts_file),
        patch.object(wst, "_state_file", return_value=state_file),
        patch.object(wst, "run_slack_search", return_value=_stale_search_result_main_thread_alert()),
        patch.object(wst, "load_current_username", return_value="testuser"),
    ):
        result = CliRunner().invoke(cli, ["--threshold-hours", "48"])

    assert result.exit_code == 0
    assert alerts_file.exists()
    content = alerts_file.read_text()
    assert "acme" in content
    assert "bob" in content


def test_state_updated_after_successful_scan(tmp_path: Path) -> None:
    alerts_file = tmp_path / "slack-thread-alerts.md"
    state_file = tmp_path / "slack-thread-state.json"
    acme_config = {"accounts": {"acme": {"keywords": ["Acme Corp"]}}}

    with (
        patch("fieldkit.watch.slack_threads.get_accounts_config", return_value=acme_config),
        patch.object(wst, "get_watchers_dir", return_value=tmp_path),
        patch.object(wst, "_alerts_file", return_value=alerts_file),
        patch.object(wst, "_state_file", return_value=state_file),
        patch.object(wst, "run_slack_search", return_value=_stale_search_result_main_thread_alert()),
        patch.object(wst, "load_current_username", return_value="testuser"),
    ):
        CliRunner().invoke(cli, ["--threshold-hours", "48"])

    assert state_file.exists()
    state = json.loads(state_file.read_text())
    assert "acme" in state
    assert state["acme"]["unanswered_threads"] == 1


# ---------------------------------------------------------------------------
# historic regression: Config-driven bot filter
# ---------------------------------------------------------------------------


# ── TestBug093BotFilter (flattened) ─────────────────────────────────────────────


def test_load_bot_patterns_merges_config() -> None:
    """load_bot_patterns() merges hardcoded defaults with config extras."""
    config = {"slack_bot_patterns": ["aap-911", "timecard reminder"]}
    patterns = classification.load_bot_patterns(config)
    # Hardcoded defaults must be present
    assert "bot" in patterns
    assert "google drive" in patterns
    # Config extras must be present
    assert "aap-911" in patterns
    assert "timecard reminder" in patterns


def test_load_bot_patterns_empty_config() -> None:
    """load_bot_patterns() returns
    hardcoded defaults when config is empty."""
    patterns = classification.load_bot_patterns({})
    assert "bot" in patterns
    assert "google drive" in patterns


def test_classify_message_filters_config_bot() -> None:
    """classify_message excludes messages from config-driven bot patterns."""
    now = datetime.datetime(2026, 6, 7, 12, 0, 0, tzinfo=datetime.UTC)
    msg = {
        "ts": str((now - datetime.timedelta(hours=72)).timestamp()),
        "username": "aap-911 monitor",
        "user": "UBOT001",
    }
    result = classification.classify_message(
        msg,
        current_username="me",
        threshold_hours=48,
        now_utc=now,
        extra_bot_patterns=frozenset({"aap-911"}),
    )
    assert result is None, "aap-911 bot should be filtered out"


def test_classify_message_allows_non_bot_sender() -> None:
    """classify_message does NOT filter a real sender not matching bot patterns."""
    now = datetime.datetime(2026, 6, 7, 12, 0, 0, tzinfo=datetime.UTC)
    msg = {
        "ts": str((now - datetime.timedelta(hours=72)).timestamp()),
        "username": "john.doe",
        "user": "U001",
        "channel": {"name": "acme-general", "id": "C001"},
    }
    result = classification.classify_message(
        msg,
        current_username="me",
        threshold_hours=48,
        now_utc=now,
        extra_bot_patterns=frozenset({"aap-911"}),
    )
    assert result is not None, "Real sender should not be filtered"


# ---------------------------------------------------------------------------
# historic regression: Channel-name-first attribution
# ---------------------------------------------------------------------------


# ── TestBug094ChannelAttribution (flattened) ─────────────────────────────────────────────


def _make_msg_bug094_channel_attribution(
    ts_offset_hours: int, channel_name: str, username: str = "other.person"
) -> dict[str, object]:
    now = datetime.datetime(2026, 6, 7, 12, 0, 0, tzinfo=datetime.UTC)
    ts = (now - datetime.timedelta(hours=ts_offset_hours)).timestamp()
    return {
        "ts": str(ts),
        "username": username,
        "user": "U002",
        "channel": {"name": channel_name, "id": "C002"},
    }


def test_channel_match_accepts_thread() -> None:
    """_channel_matches_account returns True when channel name contains keyword."""
    assert classification._channel_matches_account("acme-general", ["acme"])
    assert classification._channel_matches_account("ACME-Support", ["acme"])  # case-insensitive


def test_channel_no_match_rejects_thread() -> None:
    """_channel_matches_account returns False when channel name has no keyword match."""
    assert not classification._channel_matches_account("other-company-general", ["acme"])


def test_scan_account_threads_filters_by_channel() -> None:
    """scan_account_threads skips threads not in account channels when configured."""
    now = datetime.datetime(2026, 6, 7, 12, 0, 0, tzinfo=datetime.UTC)
    # Two messages: one in acme channel, one in unrelated channel
    msg_acme = _make_msg_bug094_channel_attribution(ts_offset_hours=72, channel_name="acme-general")
    msg_other = _make_msg_bug094_channel_attribution(ts_offset_hours=72, channel_name="unrelated-biz")

    search_result = {"total": 2, "matches": [msg_acme, msg_other]}
    account_cfg = {
        "keywords": ["acme"],
        "account_channels": ["acme"],  # historic regression filter
    }

    with patch.object(wst, "run_slack_search", return_value=search_result):
        threads = wst.scan_account_threads(
            "acme",
            account_cfg,
            current_username="me",
            threshold_hours=48,
            search_limit=50,
            now_utc=now,
        )

    assert len(threads) == 1, "Only the acme-channel thread should be returned"
    assert threads[0]["channel"] == "acme-general"


def test_scan_account_threads_no_channel_filter_uses_account_key_fallback() -> None:
    """historic regression: when account_channels is not set, fall back to account_key as filter.

    The account key (hyphens replaced with spaces) is used as the channel
    keyword so attribution filtering is always active.  Channels that don't
    contain the account key substring are skipped.
    """
    now = datetime.datetime(2026, 6, 7, 12, 0, 0, tzinfo=datetime.UTC)
    # "acme-general" contains "acme" → kept; "unrelated-biz" does not → skipped.
    msg_match = _make_msg_bug094_channel_attribution(ts_offset_hours=72, channel_name="acme-general")
    msg_no_match = _make_msg_bug094_channel_attribution(ts_offset_hours=72, channel_name="unrelated-biz")
    search_result = {"total": 2, "matches": [msg_match, msg_no_match]}
    account_cfg = {"keywords": ["acme"]}  # no account_channels key → fallback to "acme"

    with patch.object(wst, "run_slack_search", return_value=search_result):
        threads = wst.scan_account_threads(
            "acme",
            account_cfg,
            current_username="me",
            threshold_hours=48,
            search_limit=50,
            now_utc=now,
        )

    assert len(threads) == 1, "Only the acme-channel thread should be returned via fallback"
    assert threads[0]["channel"] == "acme-general"


def test_scan_account_threads_account_key_hyphen_to_space_fallback() -> None:
    """historic regression: account key with hyphens is converted to spaces for channel matching."""
    now = datetime.datetime(2026, 6, 7, 12, 0, 0, tzinfo=datetime.UTC)
    # account_key = "acme-corp" → fallback keyword = "acme corp"
    # channel "acme corp general" contains "acme corp" → kept
    msg_match = _make_msg_bug094_channel_attribution(ts_offset_hours=72, channel_name="acme corp general")
    msg_no_match = _make_msg_bug094_channel_attribution(ts_offset_hours=72, channel_name="other-channel")
    search_result = {"total": 2, "matches": [msg_match, msg_no_match]}
    account_cfg = {"keywords": ["acme corp"]}  # no account_channels

    with patch.object(wst, "run_slack_search", return_value=search_result):
        threads = wst.scan_account_threads(
            "acme-corp",
            account_cfg,
            current_username="me",
            threshold_hours=48,
            search_limit=50,
            now_utc=now,
        )

    assert len(threads) == 1
    assert threads[0]["channel"] == "acme corp general"


# ---------------------------------------------------------------------------
# historic regression: Alert deduplication — no duplicate writes on re-run
# ---------------------------------------------------------------------------


# ── TestBug121AlertDedup (flattened) ─────────────────────────────────────────────


def _make_thread_bug121_alert_dedup(
    *,  # pii-guard: ignore
    account: str = "acme",
    channel: str = "general",
    age_hours: float = 72.0,
    sender: str = "alice",
) -> SlackAccountThread:
    return {
        "account": account,
        "channel": channel,
        "channel_id": "C001",
        "ts": _epoch_str(age_hours),
        "message_dt": "2026-05-23T09:00:00Z",  # pii-guard: ignore
        "age_hours": age_hours,
        "sender_username": sender,
        "permalink": "https://app.slack.com/archives/C001/p1234",
        "text_snippet": "Let me know when the contract is ready.",
    }


def test_duplicate_thread_alert_not_written_on_second_call(tmp_path: Path) -> None:
    """Second call to append_thread_alert for same account/channel/date is a no-op."""
    alerts_file = tmp_path / "slack-thread-alerts.md"
    thread = _make_thread_bug121_alert_dedup(account="acme", channel="general")

    with (
        patch.object(wst, "_alerts_file", return_value=alerts_file),
        patch.object(wst, "get_watchers_dir", return_value=tmp_path),
    ):
        # First write — should succeed and create the file.
        wst.append_thread_alert(thread, dry_run=False)
        content_after_first = alerts_file.read_text(encoding="utf-8")

        # Second write — same thread, same day — must be a no-op.
        wst.append_thread_alert(thread, dry_run=False)
        content_after_second = alerts_file.read_text(encoding="utf-8")

    # File content must be identical after the second call.
    assert content_after_first == content_after_second, "append_thread_alert wrote a duplicate entry on the second call"
    # Sanity: the first write did produce content.
    assert "acme" in content_after_first
    assert "general" in content_after_first


def test_different_account_channel_still_written(tmp_path: Path) -> None:
    """A second alert for a *different* account/channel is still written."""
    alerts_file = tmp_path / "slack-thread-alerts.md"
    t1 = _make_thread_bug121_alert_dedup(account="acme", channel="general")
    t2 = _make_thread_bug121_alert_dedup(account="globalpay", channel="tech")

    with (
        patch.object(wst, "_alerts_file", return_value=alerts_file),
        patch.object(wst, "get_watchers_dir", return_value=tmp_path),
    ):
        wst.append_thread_alert(t1, dry_run=False)
        wst.append_thread_alert(t2, dry_run=False)

    content = alerts_file.read_text(encoding="utf-8")
    assert "acme" in content
    assert "globalpay" in content


def test_duplicate_auth_error_alert_not_written_on_second_call(tmp_path: Path) -> None:
    """Second call to write_slack_error_alert on the same date is a no-op."""
    alerts_file = tmp_path / "slack-thread-alerts.md"

    with (
        patch.object(wst, "_alerts_file", return_value=alerts_file),
        patch.object(wst, "get_watchers_dir", return_value=tmp_path),
    ):
        # First write — should succeed.
        wst.write_slack_error_alert(dry_run=False, failure_kind="auth")
        content_after_first = alerts_file.read_text(encoding="utf-8")

        # Second write — same date — must be a no-op.
        wst.write_slack_error_alert(dry_run=False, failure_kind="auth")
        content_after_second = alerts_file.read_text(encoding="utf-8")

    assert content_after_first == content_after_second, (
        "write_slack_error_alert wrote a duplicate entry on the second call"
    )
    assert "auth" in content_after_first.lower()


# ---------------------------------------------------------------------------
# historic regression: Auth error with 0 accounts checked → "fatal" outcome
# ---------------------------------------------------------------------------


# ── TestBug090FatalOutcome (flattened) ─────────────────────────────────────────────  # pii-guard: ignore


def _make_accounts_yaml_bug090_fatal_outcome(tmp_path: Path) -> Path:
    p = tmp_path / "accounts.yaml"
    p.write_text("accounts:\n  acme:\n    keywords:\n      - Acme Corp\n", encoding="utf-8")
    return p


def test_auth_error_with_zero_accounts_produces_fatal_outcome(tmp_path: Path) -> None:
    """When SlackAuthError fires on the first account (checked_accounts==0),
    write_run_status must be called with outcome='fatal'."""  # pii-guard: ignore
    accounts_yaml = _make_accounts_yaml_bug090_fatal_outcome(tmp_path)
    alerts_file = tmp_path / "slack-thread-alerts.md"
    state_file = tmp_path / "slack-thread-state.json"

    captured_outcome: list[str] = []

    def _capture_write_run_status(**kwargs: object) -> RunStatusWriteResult:
        captured_outcome.append(str(kwargs.get("outcome", "")))
        return "written"

    with (
        patch("fieldkit.config._accounts.get_config_path", return_value=accounts_yaml),
        patch("fieldkit.watch.slack_threads.get_fieldkit_home", return_value=tmp_path),
        patch.object(wst, "get_watchers_dir", return_value=tmp_path),
        patch.object(wst, "_alerts_file", return_value=alerts_file),
        patch.object(wst, "_state_file", return_value=state_file),
        patch.object(
            wst,
            "run_slack_search",
            side_effect=wst.SlackAuthError("not_authed: token expired"),
        ),
        patch("fieldkit.watch.slack_threads.write_run_status", side_effect=_capture_write_run_status),
    ):
        result = CliRunner().invoke(cli, [])

    assert result.exit_code == 2, "authentication failures require user action"
    assert len(captured_outcome) == 1, "write_run_status should be called exactly once"
    assert captured_outcome[0] == "fatal", (
        f"Expected outcome='fatal' when auth error fires before any account is checked, "
        f"got outcome={captured_outcome[0]!r}"
    )


def test_auth_error_after_some_accounts_produces_partial_outcome(tmp_path: Path) -> None:
    """When SlackAuthError fires after at least one account succeeds,
    outcome must be 'partial' (some data was collected)."""
    # Two accounts: first succeeds, second raises auth error.
    accounts_yaml = tmp_path / "accounts.yaml"
    accounts_yaml.write_text(
        "accounts:\n  acme:\n    keywords:\n      - Acme Corp\n  globalpay:\n    keywords:\n      - Globalpay\n",
        encoding="utf-8",
    )
    alerts_file = tmp_path / "slack-thread-alerts.md"
    state_file = tmp_path / "slack-thread-state.json"

    captured_outcome: list[str] = []

    def _capture_write_run_status(**kwargs: object) -> RunStatusWriteResult:
        captured_outcome.append(str(kwargs.get("outcome", "")))
        return "written"

    # First call returns empty results (acme succeeds), second raises auth error (globalpay fails).
    empty_result = {"query": "Acme Corp", "total": 0, "matches": []}
    call_count = 0

    def _side_effect(*_args: object, **_kwargs: object) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return empty_result
        raise wst.SlackAuthError("token_revoked on second account")

    with (
        patch("fieldkit.config._accounts.get_config_path", return_value=accounts_yaml),
        patch("fieldkit.watch.slack_threads.get_fieldkit_home", return_value=tmp_path),
        patch.object(wst, "get_watchers_dir", return_value=tmp_path),
        patch.object(wst, "_alerts_file", return_value=alerts_file),
        patch.object(wst, "_state_file", return_value=state_file),
        patch.object(wst, "run_slack_search", side_effect=_side_effect),
        patch("fieldkit.watch.slack_threads.write_run_status", side_effect=_capture_write_run_status),
    ):
        result = CliRunner().invoke(cli, [])
    # pii-guard: ignore
    assert result.exit_code == 2
    assert len(captured_outcome) == 1
    assert captured_outcome[0] == "partial", (
        f"Expected outcome='partial' when auth error fires after one account succeeded, "
        f"got outcome={captured_outcome[0]!r}"
    )


def test_dry_run_creates_no_logs_status_state_or_alert_files(tmp_path: Path) -> None:
    """A Slack dry run is observational even at the shared logger/status layers."""
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    with (
        patch.object(wst, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch.object(wst, "load_current_username", return_value="tester"),
        patch.object(wst, "load_state", return_value={}),
        patch.object(wst, "_scan_all_accounts", return_value=(1, 0, None, False)),
        patch("fieldkit.watch.logging.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.slack_threads.get_fieldkit_home", return_value=tmp_path),
        patch.object(wst, "get_watchers_dir", return_value=tmp_path / "watchers"),
        patch.object(wst, "write_run_status", return_value="written") as write_status,
    ):
        result = wst._run_slack_threads(threshold_hours=48, account=None, limit=50, dry_run=True)

    assert result.run.exit_code == 0
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before
    write_status.assert_not_called()


@pytest.mark.parametrize(
    ("accounts", "account_filter"),
    [
        ({"acme": "not-a-mapping"}, None),
        ({"acme": {"keywords": "acme"}}, None),
        ({"acme": {"keywords": [["nested"]]}}, None),
        ({"acme": {"account_channels": "acme-channel"}}, None),
        ({"acme": {"account_channels": [["nested"]]}}, None),
        ({"acme": {"slack_watch": "false"}}, None),
        ({"acme": {"internal": "false"}}, None),
        ({"acme": {"slack_watch": False}}, None),
        ({"acme": {"internal": True}}, "acme"),
    ],
)
def test_invalid_or_ineligible_selected_config_exits_data_without_scanning(
    accounts: dict[str, object], account_filter: str | None
) -> None:
    with (
        patch.object(wst, "get_accounts_config", return_value={"accounts": accounts}),
        patch.object(wst, "_scan_all_accounts") as scan,
    ):
        result = wst._run_slack_threads(
            threshold_hours=48,
            account=account_filter,
            limit=50,
            dry_run=True,
        )

    assert result.run == WatcherRunResult("fatal", False, None, 3)
    scan.assert_not_called()


def test_filtered_valid_account_ignores_invalid_unselected_account() -> None:
    accounts = {"acme": {"keywords": ["Acme"]}, "broken": "not-a-mapping"}
    with (
        patch.object(wst, "get_accounts_config", return_value={"accounts": accounts}),
        patch.object(wst, "load_current_username", return_value="tester"),
        patch.object(wst, "load_state", return_value={}),
        patch.object(wst, "_scan_all_accounts", return_value=(1, 0, None, False)) as scan,
        patch.object(wst, "_persist_and_summarise", return_value=False),
    ):
        result = wst._run_slack_threads(
            threshold_hours=48,
            account="acme",
            limit=50,
            dry_run=True,
        )

    assert result.run.exit_code == 0
    assert scan.call_args.args[0] == {"acme": {"keywords": ["Acme"]}}


def test_run_status_disk_failure_exits_partial() -> None:
    with (
        patch.object(wst, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch.object(wst, "load_current_username", return_value="tester"),
        patch.object(wst, "load_state", return_value={}),
        patch.object(wst, "_scan_all_accounts", return_value=(1, 0, None, False)),
        patch.object(wst, "_persist_and_summarise", return_value=False),
        patch.object(wst, "write_run_status", return_value="failed"),
    ):
        result = wst._run_slack_threads(threshold_hours=48, account=None, limit=50, dry_run=False)

    assert result.run.outcome == "fatal"
    assert result.run.exit_code == 1
    assert result.run.status_write == "failed"
    assert result.failures == 1


def test_cli_json_uses_adapter_renderer_not_domain_print() -> None:
    with (
        patch.object(wst, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch.object(wst, "load_current_username", return_value="tester"),
        patch.object(wst, "load_state", return_value={}),
        patch.object(wst, "_scan_all_accounts", return_value=(1, 0, None, False)),
        patch.object(wst, "_persist_and_summarise", return_value=False),
        patch.object(wst, "write_run_status", return_value="written"),
        patch("builtins.print", side_effect=AssertionError("domain print")),
    ):
        result = CliRunner().invoke(cli, ["--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["watcher"] == "slack-threads"


def test_cli_json_renders_provider_failure_as_partial() -> None:
    with (
        patch.object(wst, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch.object(wst, "load_current_username", return_value="tester"),
        patch.object(wst, "load_state", return_value={}),
        patch.object(wst, "_scan_all_accounts", return_value=(1, 0, "provider", False)),
        patch.object(wst, "_persist_and_summarise", return_value=False),
        patch.object(wst, "write_run_status", return_value="written"),
    ):
        result = CliRunner().invoke(cli, ["--json"])

    payload = json.loads(result.stdout)
    assert result.exit_code == 1
    assert payload["outcome"] == "partial"
    assert payload["provider_error"] is True
    assert payload["auth_error"] is False


@pytest.mark.parametrize(
    ("scan_result", "expected_result"),
    [
        ((0, 0, "auth", False), 2),
        ((1, 0, "auth", False), 2),
        ((0, 0, "provider", False), 1),
        ((1, 0, "provider", False), 1),
        ((1, 0, None, True), 1),
    ],
)
def test_run_exit_distinguishes_auth_and_partial_failures(
    tmp_path: Path,
    scan_result: tuple[int, int, str | None, bool],
    expected_result: object,
) -> None:
    with (
        patch.object(wst, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch.object(wst, "load_current_username", return_value="tester"),
        patch.object(wst, "load_state", return_value={}),
        patch.object(wst, "_scan_all_accounts", return_value=scan_result),
        patch.object(wst, "_persist_and_summarise", return_value=scan_result[3]),
        patch.object(wst, "write_run_status", return_value="written"),
        patch("fieldkit.watch.logging.get_fieldkit_home", return_value=tmp_path),
    ):
        result = wst._run_slack_threads(threshold_hours=48, account=None, limit=50, dry_run=False)

    assert result.run.exit_code == expected_result


@pytest.mark.parametrize(
    ("threshold_hours", "limit", "limit_per_account"),
    [(0, 50, None), (-1, 50, None), (48, 0, None), (48, -1, None), (48, 50, 0), (48, 50, -1)],
)
def test_non_positive_numeric_inputs_are_invalid_before_config_or_provider_calls(
    threshold_hours: int, limit: int, limit_per_account: int | None
) -> None:
    with (
        patch.object(wst, "get_accounts_config") as load_config,
        patch.object(wst, "run_slack_search") as search,
    ):
        result = wst._run_slack_threads(
            threshold_hours=threshold_hours,
            account=None,
            limit=limit,
            limit_per_account=limit_per_account,
            dry_run=True,
        )

    assert result.run == WatcherRunResult("fatal", False, None, 3)
    load_config.assert_not_called()
    search.assert_not_called()


@pytest.mark.parametrize(
    "args",
    [
        ["--threshold-hours", "0"],
        ["--limit", "0"],
        ["--limit-per-account", "-1"],
    ],
)
def test_cli_non_positive_numeric_inputs_exit_data(args: list[str]) -> None:
    with patch.object(wst, "get_accounts_config") as load_config:
        result = CliRunner().invoke(cli, args)

    assert result.exit_code == 3
    load_config.assert_not_called()


@pytest.mark.parametrize(
    ("args", "config_or_error"),
    [
        (["--threshold-hours", "0"], {"accounts": {"private-account": {}}}),
        (["--limit", "0"], {"accounts": {"private-account": {}}}),
        (["--limit-per-account", "0"], {"accounts": {"private-account": {}}}),
        ([], RuntimeError("private config path")),
        ([], {}),
        ([], {"accounts": "private malformed accounts"}),
        (["--account", "private-missing"], {"accounts": {"private-account": {}}}),
        ([], {"accounts": {"private-account": {"keywords": "private-keyword"}}}),
        ([], {"accounts": {"private-account": {"slack_watch": False}}}),
    ],
)
def test_cli_json_validation_failures_are_sanitized_and_write_free(
    tmp_path: Path,
    args: list[str],
    config_or_error: object,
) -> None:
    load_error = config_or_error if isinstance(config_or_error, RuntimeError) else None
    config = {} if load_error is not None else config_or_error
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    with (
        patch.object(wst, "get_accounts_config", return_value=config, side_effect=load_error) as load_config,
        patch.object(wst, "_scan_all_accounts") as scan,
        patch.object(wst, "run_slack_search") as search,
        patch.object(wst, "_persist_and_summarise") as persist,
        patch.object(wst, "write_run_status", return_value="written") as write_status,
        patch("fieldkit.watch.logging.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.slack_threads.get_fieldkit_home", return_value=tmp_path),
        patch.object(wst, "get_watchers_dir", return_value=tmp_path / "watchers"),
    ):
        result = CliRunner().invoke(cli, ["--json", *args])

    assert result.exit_code == 3
    assert result.exception is not None
    payload = json.loads(result.stdout)
    assert payload == {
        "watcher": "slack-threads",
        "outcome": "fatal",
        "records_checked": 0,
        "alerts_generated": 0,
        "failures": 1,
        "auth_error": False,
        "provider_error": False,
        "elapsed_seconds": payload["elapsed_seconds"],
        "dry_run": False,
    }
    assert isinstance(payload["elapsed_seconds"], float)
    assert "private" not in result.output.lower()
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before
    scan.assert_not_called()
    search.assert_not_called()
    persist.assert_not_called()
    write_status.assert_not_called()
    if args and args[0] != "--account":
        load_config.assert_not_called()


def test_summary_write_failure_is_partial_with_bounded_diagnostic(caplog: pytest.LogCaptureFixture) -> None:
    with (
        patch.object(wst, "get_accounts_config", return_value={"accounts": {"acme": {}}}),
        patch.object(wst, "load_current_username", return_value="tester"),
        patch.object(wst, "load_state", return_value={}),
        patch.object(wst, "_scan_all_accounts", return_value=(1, 0, None, False)),
        patch.object(wst, "save_state"),
        patch.object(wst, "append_run_summary", side_effect=OSError("/private/status/path")),
        caplog.at_level(logging.ERROR),
    ):
        result = wst._run_slack_threads(threshold_hours=48, account=None, limit=50, dry_run=False)

    assert result.run.outcome == "fatal"
    assert result.run.exit_code == 1
    assert "/private/status/path" not in caplog.text


# ---------------------------------------------------------------------------
# historic regression: RuntimeError in scan loop surfaces alert and breaks (not silent continue)
# ---------------------------------------------------------------------------


# ── TestBug122RuntimeErrorAlert (flattened) ─────────────────────────────────────────────


def _make_accounts_yaml_bug122_runtime_error_alert(tmp_path: Path) -> Path:
    p = tmp_path / "accounts.yaml"
    p.write_text("accounts:\n  acme:\n    keywords:\n      - Acme Corp\n", encoding="utf-8")
    return p


def test_runtime_error_writes_alert(tmp_path: Path) -> None:
    """RuntimeError must trigger a sanitized provider alert (not silently continue)."""
    accounts_yaml = _make_accounts_yaml_bug122_runtime_error_alert(tmp_path)
    alerts_file = tmp_path / "slack-thread-alerts.md"
    state_file = tmp_path / "slack-thread-state.json"

    with (
        patch("fieldkit.config._accounts.get_config_path", return_value=accounts_yaml),
        patch("fieldkit.watch.slack_threads.get_fieldkit_home", return_value=tmp_path),
        patch.object(wst, "get_watchers_dir", return_value=tmp_path),
        patch.object(wst, "_alerts_file", return_value=alerts_file),
        patch.object(wst, "_state_file", return_value=state_file),
        patch.object(
            wst,
            "run_slack_search",
            side_effect=RuntimeError("slackcli not found — install with: brew install"),
        ),
    ):
        result = CliRunner().invoke(cli, [])

    assert result.exit_code == 1, "provider search failures are retryable partial results"
    assert alerts_file.exists(), "Alert file must be written on RuntimeError"
    content = alerts_file.read_text(encoding="utf-8")
    assert "provider-error" in content
    assert "Slack search failed" in content
    assert "slackcli not found" not in content


def test_runtime_error_stops_scanning_remaining_accounts(tmp_path: Path) -> None:
    """After RuntimeError, the scan loop must break — not continue to next account."""
    accounts_yaml = tmp_path / "accounts.yaml"
    accounts_yaml.write_text(
        "accounts:\n  acme:\n    keywords:\n      - Acme\n  globalpay:\n    keywords:\n      - Globalpay\n",
        encoding="utf-8",
    )
    alerts_file = tmp_path / "slack-thread-alerts.md"
    state_file = tmp_path / "slack-thread-state.json"

    call_count = 0

    def _side_effect(*_args: object, **_kwargs: object) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("slackcli timed out after 60s")

    with (
        patch("fieldkit.config._accounts.get_config_path", return_value=accounts_yaml),
        patch("fieldkit.watch.slack_threads.get_fieldkit_home", return_value=tmp_path),
        patch.object(wst, "get_watchers_dir", return_value=tmp_path),
        patch.object(wst, "_alerts_file", return_value=alerts_file),
        patch.object(wst, "_state_file", return_value=state_file),
        patch.object(wst, "run_slack_search", side_effect=_side_effect),
    ):
        CliRunner().invoke(cli, [])

    # Only the first account should have been attempted — break stops the loop.
    assert call_count == 1, f"Expected 1 slackcli call (break after RuntimeError), got {call_count}"


def test_runtime_error_sets_auth_error_flag(tmp_path: Path) -> None:
    """RuntimeError must set auth_error=True so outcome is reported correctly."""
    accounts_yaml = _make_accounts_yaml_bug122_runtime_error_alert(tmp_path)
    alerts_file = tmp_path / "slack-thread-alerts.md"
    state_file = tmp_path / "slack-thread-state.json"

    captured_outcome: list[str] = []

    def _capture(**kwargs: object) -> None:
        captured_outcome.append(str(kwargs.get("outcome", "")))

    with (
        patch("fieldkit.config._accounts.get_config_path", return_value=accounts_yaml),
        patch("fieldkit.watch.slack_threads.get_fieldkit_home", return_value=tmp_path),
        patch.object(wst, "get_watchers_dir", return_value=tmp_path),
        patch.object(wst, "_alerts_file", return_value=alerts_file),
        patch.object(wst, "_state_file", return_value=state_file),
        patch.object(wst, "run_slack_search", side_effect=RuntimeError("timeout")),
        patch("fieldkit.watch.slack_threads.write_run_status", side_effect=_capture),
    ):
        CliRunner().invoke(cli, [])

    # auth_error=True with checked_accounts=0 → outcome="fatal"
    assert captured_outcome == ["fatal"], (
        f"Expected outcome='fatal' for RuntimeError before any account checked, got {captured_outcome!r}"
    )


# ---------------------------------------------------------------------------
# historic regression: _is_bot_message detects Slack Workflow Builder (app_id field)
# ---------------------------------------------------------------------------


# ── TestBug003WorkflowBuilderFilter (flattened) ─────────────────────────────────────────────


def test_app_id_message_is_bot() -> None:
    """Message with app_id field is identified as a bot message."""
    msg = {"app_id": "A0123456789", "username": "workflow-bot", "user": "U999"}
    assert classification._is_bot_message(msg, "workflow-bot"), "Message with app_id should be classified as bot"


def test_bot_id_message_is_bot() -> None:
    """Existing bot_id detection still works after historic regression fix."""
    msg = {"bot_id": "B0123456789", "username": "some-bot", "user": "U999"}
    assert classification._is_bot_message(msg, "some-bot")


def test_subtype_bot_message_is_bot() -> None:
    """subtype=bot_message detection still works after historic regression fix."""
    msg = {"subtype": "bot_message", "username": "some-bot", "user": "U999"}
    assert classification._is_bot_message(msg, "some-bot")


def test_human_message_without_app_id_not_bot() -> None:
    """Normal human message without app_id/bot_id is not classified as bot."""
    msg = {"username": "alice", "user": "U001"}
    assert not classification._is_bot_message(msg, "alice")


def test_app_id_message_filtered_in_classify() -> None:
    """classify_message returns None for a Workflow Builder message (app_id)."""
    now = datetime.datetime(2026, 6, 7, 12, 0, 0, tzinfo=datetime.UTC)
    msg = {
        "ts": str((now - datetime.timedelta(hours=72)).timestamp()),
        "app_id": "A0123456789",
        "username": "workflow-builder",
        "user": "UBOT002",
        "channel": {"name": "acme-general", "id": "C001"},
    }
    result = classification.classify_message(
        msg,
        current_username="me",
        threshold_hours=48,
        now_utc=now,
    )
    assert result is None, "Workflow Builder message (app_id) should be filtered out"


# ---------------------------------------------------------------------------  # pii-guard: ignore
# historic regression: New bot patterns in _BOT_USERNAME_PATTERNS
# ---------------------------------------------------------------------------


# ── TestBug093NewBotPatterns (flattened) ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "username",
    [
        "aap-911 alert",
        "escalation bot",
        "pagerduty notification",
        "opsgenie alert",
        "jira automation",
    ],
)
def test_new_bot_patterns_are_filtered(username: str) -> None:
    """Each new bot pattern substring must match in _is_bot_message."""
    msg: dict[str, Any] = {"username": username, "user": "UBOT"}
    assert classification._is_bot_message(msg, username), f"Username {username!r} should be identified as a bot"


def test_new_patterns_present_in_constant() -> None:
    """All five new patterns must be in _BOT_USERNAME_PATTERNS."""
    for pattern in ("aap-911", "escalation", "pagerduty", "opsgenie", "jira"):
        assert pattern in classification._BOT_USERNAME_PATTERNS, (
            f"Pattern {pattern!r} missing from _BOT_USERNAME_PATTERNS"
        )


def test_existing_patterns_still_present() -> None:
    """Original patterns must not have been removed."""
    for pattern in ("bot", "google drive", "google calendar", "google docs"):
        assert pattern in classification._BOT_USERNAME_PATTERNS, (
            f"Original pattern {pattern!r} missing from _BOT_USERNAME_PATTERNS"
        )


# ---------------------------------------------------------------------------
# implementation change: Internal channel filter
# ---------------------------------------------------------------------------


# ── TestEnh100InternalChannelFilter (flattened) ─────────────────────────────────────────────


def _make_msg_enh100_internal_channel_filter(channel_name: str, ts_offset_hours: int = 72) -> dict[str, Any]:
    now = datetime.datetime(2026, 6, 7, 12, 0, 0, tzinfo=datetime.UTC)
    ts = (now - datetime.timedelta(hours=ts_offset_hours)).timestamp()  # pii-guard: ignore
    return {
        "ts": str(ts),  # pii-guard: ignore
        "username": "colleague",
        "user": "U003",
        "channel": {"name": channel_name, "id": "C003"},
    }  # pii-guard: ignore


def test_internal_channel_patterns_constant_exists() -> None:
    """_INTERNAL_CHANNEL_PATTERNS must be defined and non-empty."""
    assert hasattr(classification, "_INTERNAL_CHANNEL_PATTERNS")
    assert len(classification._INTERNAL_CHANNEL_PATTERNS) > 0


@pytest.mark.parametrize(
    "channel_name",
    [
        "example-internal-general",
        "internal-team-engineering",
        "product-internal",
        "platform-team-ops",
    ],
)
def test_internal_channels_are_skipped(channel_name: str) -> None:
    """Threads in internal channels must not appear in scan results."""
    now = datetime.datetime(2026, 6, 7, 12, 0, 0, tzinfo=datetime.UTC)
    msg = _make_msg_enh100_internal_channel_filter(channel_name)
    search_result = {"total": 1, "matches": [msg]}
    account_cfg = {"keywords": ["acme"], "account_channels": [channel_name]}

    with patch.object(wst, "run_slack_search", return_value=search_result):
        threads = wst.scan_account_threads(
            channel_name,  # use channel_name as account_key so fallback matches
            account_cfg,
            current_username="me",
            threshold_hours=48,
            search_limit=50,
            now_utc=now,
        )

    assert len(threads) == 0, f"Thread in internal channel {channel_name!r} should be filtered out"


def test_external_channel_not_filtered() -> None:
    """Threads in non-internal channels must still be returned."""
    now = datetime.datetime(2026, 6, 7, 12, 0, 0, tzinfo=datetime.UTC)
    msg = _make_msg_enh100_internal_channel_filter("acme-general")
    search_result = {"total": 1, "matches": [msg]}
    account_cfg = {"keywords": ["acme"], "account_channels": ["acme"]}

    with patch.object(wst, "run_slack_search", return_value=search_result):
        threads = wst.scan_account_threads(
            "acme",
            account_cfg,
            current_username="me",
            threshold_hours=48,
            search_limit=50,
            now_utc=now,
        )

    assert len(threads) == 1, "Non-internal channel thread should not be filtered"


def test_internal_pattern_check_is_case_insensitive() -> None:
    """Internal channel check must be case-insensitive (channel names are lowercased)."""
    now = datetime.datetime(2026, 6, 7, 12, 0, 0, tzinfo=datetime.UTC)
    # Channel name with uppercase — should still be caught after lowercasing
    msg = _make_msg_enh100_internal_channel_filter("Example-General")
    search_result = {"total": 1, "matches": [msg]}
    account_cfg = {"keywords": ["example-internal"], "account_channels": ["example-internal"]}

    with patch.object(wst, "run_slack_search", return_value=search_result):
        threads = wst.scan_account_threads(
            "example-internal",
            account_cfg,
            current_username="me",
            threshold_hours=48,
            search_limit=50,
            now_utc=now,
        )

    assert len(threads) == 0, "Internal channel check must be case-insensitive"


# ---------------------------------------------------------------------------
# _build_thread_result — branch coverage
# ---------------------------------------------------------------------------


# ── TestBuildThreadResult (flattened) ─────────────────────────────────────────────


def _make_msg_dt_build_thread_result() -> datetime.datetime:
    return datetime.datetime(2026, 5, 23, 9, 0, 0, tzinfo=datetime.UTC)


def test_build_thread_result_returns_expected_fields() -> None:
    """Result dict contains all required fields."""
    msg = _make_slack_msg(hours_ago=72)
    msg_dt = _make_msg_dt_build_thread_result()
    result = classification._build_thread_result(msg, msg_dt, 72.0, "alice", "U001")
    assert "channel" in result
    assert "channel_id" in result
    assert "ts" in result
    assert "message_dt" in result
    assert "age_hours" in result
    assert "sender_username" in result
    assert "permalink" in result
    assert "text_snippet" in result


def test_build_thread_result_permalink_from_msg_used_when_present() -> None:
    """When msg has a permalink, it is used directly."""
    msg = _make_slack_msg(hours_ago=72, permalink="https://app.slack.com/archives/C001/p9999")
    msg_dt = _make_msg_dt_build_thread_result()
    result = classification._build_thread_result(msg, msg_dt, 72.0, "alice", "U001")
    assert result["permalink"] == "https://app.slack.com/archives/C001/p9999"


def test_build_thread_result_permalink_generated_when_missing() -> None:
    """When msg has no permalink but has channel_id and ts, permalink is generated."""
    msg = _make_slack_msg(hours_ago=72, permalink="")
    msg_dt = _make_msg_dt_build_thread_result()
    result = classification._build_thread_result(msg, msg_dt, 72.0, "alice", "U001")
    # Should have generated a permalink via _permalink_for
    assert result["permalink"].startswith("https://app.slack.com/archives/")
    assert "C001" in result["permalink"]


def test_build_thread_result_permalink_empty_when_no_channel_id() -> None:
    """When msg has no permalink and no channel_id, permalink stays empty."""
    msg = {
        "ts": _epoch_str(72),
        "username": "alice",
        "user": "U001",
        "channel": {"name": "acme-general"},  # no "id" key
        "text": "hello",
        "permalink": "",
    }
    msg_dt = _make_msg_dt_build_thread_result()
    result = classification._build_thread_result(msg, msg_dt, 72.0, "alice", "U001")
    # No channel_id → permalink stays empty (no _permalink_for call)
    assert result["permalink"] == ""


def test_build_thread_result_text_snippet_truncated_to_120_chars() -> None:
    """Text longer than 120 chars is truncated to 117 + '...'."""
    long_text = "A" * 200
    msg = _make_slack_msg(hours_ago=72, text=long_text)
    msg_dt = _make_msg_dt_build_thread_result()
    result = classification._build_thread_result(msg, msg_dt, 72.0, "alice", "U001")
    assert len(result["text_snippet"]) == 120
    assert result["text_snippet"].endswith("...")


def test_build_thread_result_text_snippet_not_truncated_when_short() -> None:
    """Text ≤ 120 chars is not truncated."""
    short_text = "Short message."
    msg = _make_slack_msg(hours_ago=72, text=short_text)
    msg_dt = _make_msg_dt_build_thread_result()
    result = classification._build_thread_result(msg, msg_dt, 72.0, "alice", "U001")
    assert result["text_snippet"] == short_text


def test_build_thread_result_text_snippet_exactly_120_chars_not_truncated() -> None:
    """Text exactly 120 chars is not truncated."""
    exact_text = "B" * 120
    msg = _make_slack_msg(hours_ago=72, text=exact_text)
    msg_dt = _make_msg_dt_build_thread_result()
    result = classification._build_thread_result(msg, msg_dt, 72.0, "alice", "U001")
    assert result["text_snippet"] == exact_text
    assert not result["text_snippet"].endswith("...")


def test_build_thread_result_sender_username_falls_back_to_user_id() -> None:
    """When sender_username is empty, sender_user_id is used."""
    msg = _make_slack_msg(hours_ago=72, username="", user="U999")
    msg_dt = _make_msg_dt_build_thread_result()
    result = classification._build_thread_result(msg, msg_dt, 72.0, "", "U999")
    assert result["sender_username"] == "U999"


def test_build_thread_result_sender_username_falls_back_to_unknown() -> None:
    """When both sender_username and sender_user_id are empty, 'unknown' is used."""
    msg = _make_slack_msg(hours_ago=72, username="", user="")
    msg_dt = _make_msg_dt_build_thread_result()
    result = classification._build_thread_result(msg, msg_dt, 72.0, "", "")
    assert result["sender_username"] == "unknown"


def test_build_thread_result_channel_name_falls_back_to_channel_id() -> None:
    """When channel has no 'name', channel 'id' is used as channel name."""
    msg = {
        "ts": _epoch_str(72),
        "username": "alice",
        "user": "U001",
        "channel": {"id": "C999"},  # no "name" key
        "text": "hello",
        "permalink": "https://app.slack.com/archives/C999/p1234",
    }
    msg_dt = _make_msg_dt_build_thread_result()
    result = classification._build_thread_result(msg, msg_dt, 72.0, "alice", "U001")
    assert result["channel"] == "C999"


def test_build_thread_result_channel_falls_back_to_unknown_when_no_name_or_id() -> None:
    """When channel dict has neither 'name' nor 'id', 'unknown' is used."""
    msg = {
        "ts": _epoch_str(72),
        "username": "alice",
        "user": "U001",
        "channel": {},
        "text": "hello",
        "permalink": "",
    }
    msg_dt = _make_msg_dt_build_thread_result()
    result = classification._build_thread_result(msg, msg_dt, 72.0, "alice", "U001")
    assert result["channel"] == "unknown"


def test_build_thread_result_missing_channel_key_uses_unknown() -> None:
    """When msg has no 'channel' key at all, 'unknown' is used."""
    msg = {
        "ts": _epoch_str(72),
        "username": "alice",
        "user": "U001",
        "text": "hello",
        "permalink": "",
    }
    msg_dt = _make_msg_dt_build_thread_result()
    result = classification._build_thread_result(msg, msg_dt, 72.0, "alice", "U001")
    assert result["channel"] == "unknown"


def test_build_thread_result_age_hours_rounded_to_one_decimal() -> None:
    """age_hours is rounded to 1 decimal place."""
    msg = _make_slack_msg(hours_ago=72)
    msg_dt = _make_msg_dt_build_thread_result()
    result = classification._build_thread_result(msg, msg_dt, 72.123456, "alice", "U001")
    assert result["age_hours"] == 72.1


def test_build_thread_result_message_dt_formatted_as_iso_string() -> None:
    """message_dt is formatted as a UTC ISO string."""
    msg = _make_slack_msg(hours_ago=72)
    msg_dt = datetime.datetime(2026, 5, 23, 9, 0, 0, tzinfo=datetime.UTC)
    result = classification._build_thread_result(msg, msg_dt, 72.0, "alice", "U001")
    assert result["message_dt"] == "2026-05-23T09:00:00Z"


@pytest.mark.unit
def test_run_slack_search_does_not_retry_missing_binary() -> None:
    """historic regression: a missing slackcli binary is permanent — retrying wastes ~6s of backoff.

    The predicate matches only subprocess.TimeoutExpired. FileNotFoundError means the
    environment is wrong, not that the call was unlucky, and no number of attempts will
    conjure the binary.
    """
    call_count = {"n": 0}

    def _fake_popen(cmd: list[str], **kwargs: Any) -> Any:
        call_count["n"] += 1
        raise FileNotFoundError("slackcli: command not found")

    with (
        patch("subprocess.Popen", side_effect=_fake_popen),
        patch("time.sleep"),
        pytest.raises((FileNotFoundError, RuntimeError)),
    ):
        wst.run_slack_search("test", limit=10)

    assert call_count["n"] == 1, "a missing binary is permanent and must be attempted exactly once"
