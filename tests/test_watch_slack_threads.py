"""Tests for routines/watch_slack_threads.py.

Covers:
- Auth expiry path: SlackAuthError raised → write_auth_error_alert() writes named
  error artifact and main() exits 0 (not 1)
- Unanswered thread >48h: classify_message() returns alert dict; alert written to
  slack-thread-alerts.md
- Recent thread (<48h): classify_message() returns None (no alert)
- Self-sent message: classify_message() returns None (sender == current_username)
- _is_auth_error() signal detection (various auth error strings)
- write_auth_error_alert() dry_run=True writes nothing
- append_thread_alert() output format (account, channel, age, sender, timestamp)
- append_thread_alert() dry_run=True writes nothing
- load_state() / save_state() round-trip
- load_state() returns {} for missing/corrupt file
- _ts_to_datetime() epoch string → UTC datetime conversion
- main() auth expiry handled with exit 0 and named error artifact written
"""

import datetime
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------

_TOOLS_ROOT = Path(__file__).resolve().parents[1]

from click.testing import CliRunner  # noqa: E402

import fieldkit.watch.slack_thread_classification as classification  # noqa: E402
import fieldkit.watch.slack_threads as wst  # noqa: E402
from fieldkit.commands.watch.slack_threads import cli  # noqa: E402
from fieldkit.errors import FieldkitError  # noqa: E402


@pytest.mark.unit
def test_slack_auth_error_inherits_from_fieldkit_error() -> None:
    error = wst.SlackAuthError("failure")
    assert isinstance(error, FieldkitError)


pytestmark = pytest.mark.unit


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
) -> dict:
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

    def _fake_run(cmd: list[str], **kwargs: Any) -> Any:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)
        stdout_tmp = kwargs["stdout"]
        stdout_tmp.write(b'{"query": "test", "total": 0, "matches": []}')
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=None, stderr=b"")

    with patch.object(wst.subprocess, "run", side_effect=_fake_run), patch("time.sleep"):
        result = wst.run_slack_search("test", limit=10)

    assert call_count["n"] == 2, "Expected exactly 2 subprocess calls (1 timeout + 1 success)"
    assert result == {"query": "test", "total": 0, "matches": []}


@pytest.mark.unit
def test_run_slack_search_all_retries_exhausted_raises_runtime_error() -> None:
    """historic regression: persistent subprocess.TimeoutExpired still raises after retries."""
    call_count = {"n": 0}

    def _fake_run(cmd: list[str], **kwargs: Any) -> Any:
        call_count["n"] += 1
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)

    with (
        patch.object(wst.subprocess, "run", side_effect=_fake_run),
        patch("time.sleep"),
        pytest.raises(RuntimeError, match="slackcli timed out after 60s"),
    ):
        wst.run_slack_search("test", limit=10)

    assert call_count["n"] == 3, "Expected exactly 3 attempts (stop_after_attempt(3))"


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
# write_auth_error_alert
# ---------------------------------------------------------------------------


# ── TestWriteAuthErrorAlert (flattened) ─────────────────────────────────────────────


def test_write_auth_error_alert_writes_named_error_entry(tmp_path: Path) -> None:
    alerts_file = tmp_path / "slack-thread-alerts.md"
    with (
        patch.object(wst, "_alerts_file", return_value=alerts_file),
        patch.object(wst, "get_watchers_dir", return_value=tmp_path),
    ):
        wst.write_auth_error_alert("token_revoked: expired", dry_run=False)

    content = alerts_file.read_text()
    assert "auth expired" in content.lower() or "auth-error" in content
    assert "slackcli" in content
    assert "token_revoked: expired" in content


def test_write_auth_error_alert_dry_run_does_not_write_file(tmp_path: Path) -> None:
    alerts_file = tmp_path / "slack-thread-alerts.md"
    with (
        patch.object(wst, "_alerts_file", return_value=alerts_file),
        patch.object(wst, "get_watchers_dir", return_value=tmp_path),
    ):
        wst.write_auth_error_alert("not_authed", dry_run=True)

    assert not alerts_file.exists()


def test_write_auth_error_alert_error_message_truncated_to_200_chars(tmp_path: Path) -> None:
    long_msg = "A" * 500
    alerts_file = tmp_path / "slack-thread-alerts.md"
    with (
        patch.object(wst, "_alerts_file", return_value=alerts_file),
        patch.object(wst, "get_watchers_dir", return_value=tmp_path),
    ):
        wst.write_auth_error_alert(long_msg, dry_run=False)

    content = alerts_file.read_text()
    # The detail line should have at most 200 A's in it
    assert "A" * 201 not in content


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
) -> dict:
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
        result = wst.save_state(data)
        loaded = wst.load_state()
    assert result is None
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
        patch.object(wst, "load_accounts_config", return_value=config),
        patch.object(wst, "load_current_username", return_value="operator"),
        patch.object(wst, "load_state", return_value={}),
        patch.object(wst, "_scan_all_accounts", return_value=(1, 0, False, False)),
        patch.object(wst, "_persist_and_summarise", return_value=True),
        patch.object(wst, "write_run_status") as write_status,
    ):
        rc = wst._run_slack_threads(threshold_hours=48, account=None, limit=50, dry_run=False)

    assert rc == 1
    assert write_status.call_args.kwargs["outcome"] == "fatal"
    assert write_status.call_args.kwargs["failures"] == 1


# ---------------------------------------------------------------------------
# main() — auth expiry path: exit 0, named error artifact written
# ---------------------------------------------------------------------------


# ── TestMainAuthExpiry (flattened) ─────────────────────────────────────────────


def _make_accounts_yaml_main_auth_expiry(tmp_path: Path) -> Path:
    p = tmp_path / "accounts.yaml"
    p.write_text("accounts:\n  acme:\n    keywords:\n      - Acme Corp\n", encoding="utf-8")
    return p


def test_auth_error_exits_zero(tmp_path: Path) -> None:
    accounts_yaml = _make_accounts_yaml_main_auth_expiry(tmp_path)
    alerts_file = tmp_path / "slack-thread-alerts.md"
    state_file = tmp_path / "slack-thread-state.json"

    with (
        patch.object(wst, "_accounts_config", return_value=accounts_yaml),
        patch.object(wst, "load_accounts_config", side_effect=lambda: yaml.safe_load(accounts_yaml.read_text())),
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

    assert result.exit_code == 0


def test_auth_error_writes_named_error_artifact(tmp_path: Path) -> None:
    accounts_yaml = _make_accounts_yaml_main_auth_expiry(tmp_path)
    alerts_file = tmp_path / "slack-thread-alerts.md"
    state_file = tmp_path / "slack-thread-state.json"

    with (
        patch.object(wst, "_accounts_config", return_value=accounts_yaml),
        patch.object(wst, "load_accounts_config", side_effect=lambda: yaml.safe_load(accounts_yaml.read_text())),
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
        patch.object(wst, "_accounts_config", return_value=accounts_yaml),
        patch.object(wst, "load_accounts_config", side_effect=lambda: yaml.safe_load(accounts_yaml.read_text())),
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


def _stale_search_result_main_thread_alert() -> dict:
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
) -> dict:
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
) -> dict:
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
    """Second call to write_auth_error_alert on the same date is a no-op."""
    alerts_file = tmp_path / "slack-thread-alerts.md"

    with (
        patch.object(wst, "_alerts_file", return_value=alerts_file),
        patch.object(wst, "get_watchers_dir", return_value=tmp_path),
    ):
        # First write — should succeed.
        wst.write_auth_error_alert("token_revoked: expired", dry_run=False)
        content_after_first = alerts_file.read_text(encoding="utf-8")

        # Second write — same date — must be a no-op.
        wst.write_auth_error_alert("token_revoked: expired again", dry_run=False)
        content_after_second = alerts_file.read_text(encoding="utf-8")

    assert content_after_first == content_after_second, (
        "write_auth_error_alert wrote a duplicate entry on the second call"
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

    def _capture_write_run_status(**kwargs: object) -> None:
        captured_outcome.append(str(kwargs.get("outcome", "")))

    with (
        patch.object(wst, "_accounts_config", return_value=accounts_yaml),
        patch.object(wst, "load_accounts_config", side_effect=lambda: yaml.safe_load(accounts_yaml.read_text())),
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

    assert result.exit_code == 0, "cli must still exit 0 on auth error"
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

    def _capture_write_run_status(**kwargs: object) -> None:
        captured_outcome.append(str(kwargs.get("outcome", "")))

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
        patch.object(wst, "_accounts_config", return_value=accounts_yaml),
        patch.object(wst, "load_accounts_config", side_effect=lambda: yaml.safe_load(accounts_yaml.read_text())),
        patch("fieldkit.watch.slack_threads.get_fieldkit_home", return_value=tmp_path),
        patch.object(wst, "get_watchers_dir", return_value=tmp_path),
        patch.object(wst, "_alerts_file", return_value=alerts_file),
        patch.object(wst, "_state_file", return_value=state_file),
        patch.object(wst, "run_slack_search", side_effect=_side_effect),
        patch("fieldkit.watch.slack_threads.write_run_status", side_effect=_capture_write_run_status),
    ):
        result = CliRunner().invoke(cli, [])
    # pii-guard: ignore
    assert result.exit_code == 0
    assert len(captured_outcome) == 1
    assert captured_outcome[0] == "partial", (
        f"Expected outcome='partial' when auth error fires after one account succeeded, "
        f"got outcome={captured_outcome[0]!r}"
    )


# ---------------------------------------------------------------------------
# historic regression: RuntimeError in scan loop surfaces alert and breaks (not silent continue)
# ---------------------------------------------------------------------------


# ── TestBug122RuntimeErrorAlert (flattened) ─────────────────────────────────────────────


def _make_accounts_yaml_bug122_runtime_error_alert(tmp_path: Path) -> Path:
    p = tmp_path / "accounts.yaml"
    p.write_text("accounts:\n  acme:\n    keywords:\n      - Acme Corp\n", encoding="utf-8")
    return p


def test_runtime_error_writes_alert(tmp_path: Path) -> None:
    """RuntimeError must trigger write_auth_error_alert (not silently continue)."""
    accounts_yaml = _make_accounts_yaml_bug122_runtime_error_alert(tmp_path)
    alerts_file = tmp_path / "slack-thread-alerts.md"
    state_file = tmp_path / "slack-thread-state.json"

    with (
        patch.object(wst, "_accounts_config", return_value=accounts_yaml),
        patch.object(wst, "load_accounts_config", side_effect=lambda: yaml.safe_load(accounts_yaml.read_text())),
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

    assert result.exit_code == 0, "cli must still exit 0 after RuntimeError"
    assert alerts_file.exists(), "Alert file must be written on RuntimeError"
    content = alerts_file.read_text(encoding="utf-8")
    assert "slackcli" in content.lower() or "auth" in content.lower(), "Alert must contain error detail"


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
        patch.object(wst, "_accounts_config", return_value=accounts_yaml),
        patch.object(wst, "load_accounts_config", side_effect=lambda: yaml.safe_load(accounts_yaml.read_text())),
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
        patch.object(wst, "_accounts_config", return_value=accounts_yaml),
        patch.object(wst, "load_accounts_config", side_effect=lambda: yaml.safe_load(accounts_yaml.read_text())),
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

    def _fake_run(cmd: list[str], **kwargs: Any) -> Any:
        call_count["n"] += 1
        raise FileNotFoundError("slackcli: command not found")

    with (
        patch.object(wst.subprocess, "run", side_effect=_fake_run),
        patch("time.sleep"),
        pytest.raises((FileNotFoundError, RuntimeError)),
    ):
        wst.run_slack_search("test", limit=10)

    assert call_count["n"] == 1, "a missing binary is permanent and must be attempted exactly once"
