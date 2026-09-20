"""Tests for fieldkit/watch/logging.py — per-run log file setup."""

import logging
import os
import time
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from fieldkit.watch.logging import (
    _KEEP_DAYS,
    _LOG_FILENAME_RE,
    _alert_key_exists,
    _prune_old_logs,
    _prune_state,
    list_recent_logs,
    setup_watcher_logging,
    teardown_watcher_logging,
)

pytestmark = pytest.mark.unit


@pytest.fixture()
def logs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Patch get_fieldkit_home() so log files land in tmp_path."""
    monkeypatch.setattr("fieldkit.watch.logging.get_fieldkit_home", lambda: tmp_path)
    return tmp_path / "logs" / "watchers"


# ---------------------------------------------------------------------------
# setup_watcher_logging
# ---------------------------------------------------------------------------


def test_setup_creates_log_file(logs_dir: Path) -> None:
    """setup_watcher_logging creates a timestamped log file in the correct dir."""
    result = setup_watcher_logging("test-watcher")
    assert result[0].exists(), "Log file must exist after setup"
    log_path, handler, state = result
    try:
        assert log_path.parent == logs_dir
        assert _LOG_FILENAME_RE.match(log_path.name), f"Unexpected filename: {log_path.name}"
        assert log_path.name.startswith("test-watcher-")
    finally:
        teardown_watcher_logging(handler, state)


def test_setup_installs_file_handler_on_watcher_namespace(logs_dir: Path) -> None:
    """setup_watcher_logging owns only the fieldkit.watch namespace."""
    root = logging.getLogger()
    watcher_logger = logging.getLogger("fieldkit.watch")
    root_handlers = list(root.handlers)

    result = setup_watcher_logging("test-watcher")
    assert result[1] in watcher_logger.handlers
    _log_path, handler, state = result
    try:
        assert handler not in root.handlers
        assert root.handlers == root_handlers
        assert handler.level == logging.DEBUG
        assert watcher_logger.level == logging.DEBUG
        assert watcher_logger.propagate is False
    finally:
        teardown_watcher_logging(handler, state)


def test_teardown_removes_handler(logs_dir: Path) -> None:
    """teardown_watcher_logging removes the handler from the watcher namespace."""
    watcher_logger = logging.getLogger("fieldkit.watch")
    result = setup_watcher_logging("test-watcher")
    assert result[1] in watcher_logger.handlers
    _log_path, handler, state = result
    teardown_watcher_logging(handler, state)
    assert handler not in watcher_logger.handlers


def test_setup_leaves_root_handlers_active_for_unrelated_logs(logs_dir: Path) -> None:
    """historic regression: watcher setup must not disturb process root handlers."""
    root = logging.getLogger()
    stream = StringIO()
    stream_handler = logging.StreamHandler(stream)
    root.addHandler(stream_handler)
    handlers_before = list(root.handlers)

    result = setup_watcher_logging("test-watcher")
    assert result[1] not in root.handlers
    log_path, file_handler, state = result
    try:
        logging.getLogger("unrelated.component").warning("unrelated message")
        file_handler.flush()
        assert root.handlers == handlers_before
        assert "unrelated message" in stream.getvalue()
        assert "unrelated message" not in log_path.read_text(encoding="utf-8")
    finally:
        teardown_watcher_logging(file_handler, state)
        if stream_handler in root.handlers:
            root.removeHandler(stream_handler)


def test_log_entries_appear_in_file(logs_dir: Path) -> None:
    """Watcher namespace entries appear in the file without reaching root."""
    root = logging.getLogger()
    root_stream = StringIO()
    root_handler = logging.StreamHandler(root_stream)
    root.addHandler(root_handler)
    result = setup_watcher_logging("test-watcher")
    assert result[0].exists()
    log_path, handler, state = result
    try:
        logging.getLogger("fieldkit.watch.test_module").info("hello from test")
        handler.flush()
        content = log_path.read_text(encoding="utf-8")
        assert "hello from test" in content
        assert "fieldkit.watch.test_module" in content
        assert "hello from test" not in root_stream.getvalue()
    finally:
        teardown_watcher_logging(handler, state)
        root.removeHandler(root_handler)


def test_morning_brief_orchestrator_uses_watcher_namespace() -> None:
    """The commands-layer watcher adapter remains inside the logging boundary."""
    from fieldkit.commands.brief import generate

    assert generate.log.name == "fieldkit.watch.morning_brief.generate"


def test_teardown_restores_watcher_logger_configuration(logs_dir: Path) -> None:
    """Teardown restores non-default namespace configuration."""
    watcher_logger = logging.getLogger("fieldkit.watch")
    prior_level = watcher_logger.level
    prior_propagate = watcher_logger.propagate
    watcher_logger.setLevel(logging.ERROR)
    watcher_logger.propagate = True

    result = setup_watcher_logging("test-watcher")
    assert result[2].level == logging.ERROR
    _log_path, handler, state = result
    teardown_watcher_logging(handler, state)

    assert watcher_logger.level == logging.ERROR
    assert watcher_logger.propagate is True
    watcher_logger.setLevel(prior_level)
    watcher_logger.propagate = prior_propagate


def test_filename_pattern() -> None:
    """_LOG_FILENAME_RE matches expected filenames and rejects others."""
    assert _LOG_FILENAME_RE.match("backstory-health-2026-06-07-143022.log")
    assert _LOG_FILENAME_RE.match("slack-threads-2026-01-01-000000.log")
    assert not _LOG_FILENAME_RE.match("backstory-health.log")
    assert not _LOG_FILENAME_RE.match("other-file.txt")


# ---------------------------------------------------------------------------
# _prune_old_logs
# ---------------------------------------------------------------------------


def test_prune_removes_old_files(tmp_path: Path) -> None:
    """_prune_old_logs deletes files older than keep_days."""
    logs_dir = tmp_path / "logs" / "watchers"
    logs_dir.mkdir(parents=True)

    old_file = logs_dir / "test-watcher-2020-01-01-000000.log"
    old_file.write_text("old", encoding="utf-8")
    # Set mtime to > 7 days ago
    old_mtime = time.time() - ((_KEEP_DAYS + 1) * 86400)
    os.utime(old_file, (old_mtime, old_mtime))

    recent_file = logs_dir / "test-watcher-2026-06-07-120000.log"
    recent_file.write_text("recent", encoding="utf-8")

    _prune_old_logs(logs_dir, "test-watcher")

    assert not old_file.exists(), "Old file should be pruned"
    assert recent_file.exists(), "Recent file should be kept"


def test_prune_only_affects_watcher_prefix(tmp_path: Path) -> None:
    """_prune_old_logs only prunes files for the given watcher name."""
    logs_dir = tmp_path / "logs" / "watchers"
    logs_dir.mkdir(parents=True)

    other_old = logs_dir / "other-watcher-2020-01-01-000000.log"
    other_old.write_text("old", encoding="utf-8")
    old_mtime = time.time() - ((_KEEP_DAYS + 1) * 86400)
    os.utime(other_old, (old_mtime, old_mtime))

    _prune_old_logs(logs_dir, "test-watcher")

    assert other_old.exists(), "Files for other watchers must not be pruned"


# ---------------------------------------------------------------------------
# list_recent_logs
# ---------------------------------------------------------------------------


def test_list_recent_logs_empty(logs_dir: Path) -> None:
    """list_recent_logs returns empty list when no logs dir or files."""
    result = list_recent_logs(watcher_name="no-such-watcher")
    assert result == []


def test_list_recent_logs_filters_by_watcher(logs_dir: Path) -> None:
    """list_recent_logs returns only files matching the watcher prefix."""
    logs_dir.mkdir(parents=True)
    f1 = logs_dir / "backstory-health-2026-06-07-100000.log"
    f2 = logs_dir / "backstory-health-2026-06-07-110000.log"
    f3 = logs_dir / "pursuit-stalls-2026-06-07-100000.log"
    for f in (f1, f2, f3):
        f.write_text("x", encoding="utf-8")

    result = list_recent_logs(watcher_name="backstory-health")
    names = {p.name for p in result}
    assert f1.name in names
    assert f2.name in names
    assert f3.name not in names


def test_list_recent_logs_newest_first(logs_dir: Path) -> None:
    """list_recent_logs returns files sorted newest-mtime first."""
    logs_dir.mkdir(parents=True)
    f1 = logs_dir / "test-watcher-2026-06-07-100000.log"
    f2 = logs_dir / "test-watcher-2026-06-07-110000.log"
    f1.write_text("a", encoding="utf-8")
    f2.write_text("b", encoding="utf-8")
    # Set mtimes explicitly
    os.utime(f1, (1000, 1000))
    os.utime(f2, (2000, 2000))

    result = list_recent_logs(watcher_name="test-watcher")
    assert result[0].name == f2.name, "Newer file should be first"
    assert result[1].name == f1.name


def test_list_recent_logs_deduplicates_same_second(logs_dir: Path) -> None:
    """implementation change: two files with the same watcher+date+HHMMSS prefix (different microseconds)
    → list_recent_logs returns only one entry (the most-recent by mtime).
    """
    logs_dir.mkdir(parents=True)

    # Two files with the same second prefix, differing only in microseconds
    f_older = logs_dir / "backstory-health-2026-06-11-143022000001.log"
    f_newer = logs_dir / "backstory-health-2026-06-11-143022000099.log"
    f_older.write_text("older run", encoding="utf-8")
    f_newer.write_text("newer run", encoding="utf-8")

    # Set mtimes so f_newer is definitively newer
    os.utime(f_older, (1000, 1000))
    os.utime(f_newer, (2000, 2000))

    result = list_recent_logs(watcher_name="backstory-health")

    assert len(result) == 1, (
        f"Expected 1 deduplicated entry for same-second files, got {len(result)}: {[p.name for p in result]}"
    )
    assert result[0].name == f_newer.name, f"Expected the newer file to be kept, got: {result[0].name}"


# ---------------------------------------------------------------------------
# _alert_key_exists
# ---------------------------------------------------------------------------


# ── TestAlertKeyExists (flattened) ──────────────────────────────────────────


def test_alert_key_exists_returns_false_when_file_missing(tmp_path: Path) -> None:
    """Missing alert file returns False immediately."""
    missing = tmp_path / "alerts.md"
    assert _alert_key_exists(missing, "2026-06-10", "my-deal") is False


def test_alert_key_exists_returns_false_when_file_empty(tmp_path: Path) -> None:
    """Empty alert file returns False."""
    f = tmp_path / "alerts.md"
    f.write_text("", encoding="utf-8")
    assert _alert_key_exists(f, "2026-06-10", "my-deal") is False


def test_alert_key_exists_returns_false_when_date_not_present(tmp_path: Path) -> None:
    """Date header absent → False even if slug appears elsewhere."""
    f = tmp_path / "alerts.md"
    f.write_text(
        "# Alerts\n\n## 2026-06-09 — acme/my-deal — stalled\n\nsome content\n",
        encoding="utf-8",
    )
    assert _alert_key_exists(f, "2026-06-10", "my-deal") is False


def test_alert_key_exists_returns_false_when_date_present_but_slug_absent(tmp_path: Path) -> None:
    """Date header matches but slug is not in the next 10 lines → False."""
    f = tmp_path / "alerts.md"
    f.write_text(
        "# Alerts\n\n## 2026-06-10 — acme/other-deal — stalled\n\nsome content\n",
        encoding="utf-8",
    )
    assert _alert_key_exists(f, "2026-06-10", "my-deal") is False


def test_alert_key_exists_returns_true_when_date_and_slug_present(tmp_path: Path) -> None:
    """Both date header and slug found within lookahead → True."""
    f = tmp_path / "alerts.md"
    f.write_text(
        "# Alerts\n\n"
        "## 2026-06-10 — acme/my-deal — stalled in discovery\n\n"
        "- **Pursuit:** my-deal\n"
        "- **Stage:** discovery\n",
        encoding="utf-8",
    )
    assert _alert_key_exists(f, "2026-06-10", "my-deal") is True


def test_alert_key_exists_slug_in_heading_line_itself(tmp_path: Path) -> None:
    """The slug appearing on the heading line itself is NOT counted (only lookahead lines)."""
    # The date header line starts at idx; we search idx+1 onward.
    # If the slug is ONLY in the heading line, it must not match.
    f = tmp_path / "alerts.md"
    # Heading has the date but does NOT have the slug in lookahead lines
    f.write_text(
        "# Alerts\n\n## 2026-06-10 — some other content\n\nno slug here\n",
        encoding="utf-8",
    )
    assert _alert_key_exists(f, "2026-06-10", "my-deal") is False


def test_alert_key_exists_slug_in_large_block_is_found(tmp_path: Path) -> None:
    """Spec 043 G2b: slug in a block > 50 lines is found (scan-to-next-header).

    The old fixed 50-line lookahead missed slugs beyond line 50.
    The new scan-to-next-date-header logic finds them regardless of block size.
    """
    lines = ["# Alerts\n", "\n", "## 2026-06-10 — something\n"]
    # 60 filler lines (beyond old 50-line window)
    lines += ["- filler\n"] * 60
    # Slug appears after the old window limit — must still be found
    lines += ["my-deal appears here\n"]
    f = tmp_path / "alerts.md"
    f.write_text("".join(lines), encoding="utf-8")
    # New behavior: scans to EOF (no next date header) — slug IS found
    assert _alert_key_exists(f, "2026-06-10", "my-deal") is True


def test_alert_key_exists_slug_in_different_date_block_not_found(tmp_path: Path) -> None:
    """Spec 043 G2b: slug in a different date block is not counted.

    The scan stops at the next date header, so a slug in the next block
    does not produce a false positive for the target date.
    """
    lines = ["# Alerts\n", "\n", "## 2026-06-10 — something\n"]
    lines += ["- filler\n"] * 5
    lines += ["## 2026-06-09 — previous day\n"]  # next date header — scan stops
    lines += ["my-deal appears in wrong block\n"]
    f = tmp_path / "alerts.md"
    f.write_text("".join(lines), encoding="utf-8")
    # Slug is in the 2026-06-09 block, not 2026-06-10 — must NOT be found
    assert _alert_key_exists(f, "2026-06-10", "my-deal") is False


def test_alert_key_exists_multiple_date_blocks_finds_correct_one(tmp_path: Path) -> None:
    """When multiple date blocks exist only the matching date is searched."""
    f = tmp_path / "alerts.md"
    f.write_text(
        "# Alerts\n\n"
        "## 2026-06-09 — acme/my-deal — stalled\n\n"
        "- **Pursuit:** my-deal\n\n"
        "## 2026-06-10 — acme/other-deal — stalled\n\n"
        "- **Pursuit:** other-deal\n",
        encoding="utf-8",
    )
    # slug "my-deal" is in the 2026-06-09 block only
    assert _alert_key_exists(f, "2026-06-09", "my-deal") is True
    assert _alert_key_exists(f, "2026-06-10", "my-deal") is False
    assert _alert_key_exists(f, "2026-06-10", "other-deal") is True


def test_alert_key_exists_date_header_must_be_line_start(tmp_path: Path) -> None:
    """A date string inside a line body (not at line start with ##) does not match."""
    f = tmp_path / "alerts.md"
    f.write_text(
        "# Alerts\n\nSome text mentioning 2026-06-10 in the middle\n\n- **Pursuit:** my-deal\n",
        encoding="utf-8",
    )
    assert _alert_key_exists(f, "2026-06-10", "my-deal") is False


def test_alert_key_exists_slug_not_matched_as_substring_of_longer_slug(tmp_path: Path) -> None:
    """Adversary low: 'deal' must not match inside 'big-deal' (false positive)."""
    f = tmp_path / "alerts.md"
    f.write_text(
        "# Alerts\n\n## 2026-06-10 — acme/big-deal — stalled\n\n- **Pursuit:** big-deal\n",
        encoding="utf-8",
    )
    # "deal" is a substring of "big-deal" — must NOT match
    assert _alert_key_exists(f, "2026-06-10", "deal") is False


def test_alert_key_exists_slug_matched_when_bounded_by_non_slug_chars(tmp_path: Path) -> None:
    """Slug matches when surrounded by non-alphanumeric-non-hyphen characters."""
    f = tmp_path / "alerts.md"
    f.write_text(
        "# Alerts\n\n## 2026-06-10 — acme/deal — stalled\n\n- **Pursuit:** deal\n",
        encoding="utf-8",
    )
    # "deal" appears bounded by ":" and newline — must match
    assert _alert_key_exists(f, "2026-06-10", "deal") is True


def test_alert_key_exists_slug_matched_at_start_of_line(tmp_path: Path) -> None:
    """Slug at the very start of a lookahead line is matched correctly."""
    f = tmp_path / "alerts.md"
    f.write_text(
        "# Alerts\n\n## 2026-06-10 — something\n\nmy-deal is stalled\n",
        encoding="utf-8",
    )
    assert _alert_key_exists(f, "2026-06-10", "my-deal") is True


# ---------------------------------------------------------------------------
# _prune_state
# ---------------------------------------------------------------------------


# ── TestPruneState (flattened) ──────────────────────────────────────────────


def _prune_state_make_pursuit(data_root: Path, account: str, stem: str) -> Path:
    """Create a minimal pursuit file and return its path."""
    pursuit_dir = data_root / "accounts" / account / "pursuits"
    pursuit_dir.mkdir(parents=True, exist_ok=True)
    p = pursuit_dir / f"{stem}.md"
    p.write_text(f"---\nstage: discovery\n---\n# {stem}\n", encoding="utf-8")
    return p


def test_prune_state_keeps_entries_for_existing_files(tmp_path: Path) -> None:
    """Entries whose pursuit file exists on disk are retained."""
    _prune_state_make_pursuit(tmp_path, "acme", "deal-a")
    state: dict[str, Any] = {
        "acme/deal-a": {"stage": "discovery"},
    }
    pruned, removed = _prune_state(state, tmp_path)
    assert removed == 0
    assert "acme/deal-a" in pruned


def test_prune_state_removes_entries_for_missing_files(tmp_path: Path) -> None:
    """Entries whose pursuit file is absent are removed."""
    state: dict[str, Any] = {
        "acme/deleted-deal": {"stage": "proposal"},
    }
    pruned, removed = _prune_state(state, tmp_path)
    assert removed == 1
    assert "acme/deleted-deal" not in pruned


def test_prune_state_mixed_existing_and_missing(tmp_path: Path) -> None:
    """Only missing-file entries are removed; existing ones are kept."""
    _prune_state_make_pursuit(tmp_path, "acme", "live-deal")
    state: dict[str, Any] = {
        "acme/live-deal": {"stage": "discovery"},
        "acme/gone-deal": {"stage": "proposal"},
        "other-account/also-gone": {"stage": "poc"},
    }
    pruned, removed = _prune_state(state, tmp_path)
    assert removed == 2
    assert "acme/live-deal" in pruned
    assert "acme/gone-deal" not in pruned
    assert "other-account/also-gone" not in pruned


def test_prune_state_empty_state_returns_zero_removed(tmp_path: Path) -> None:
    """Empty state dict returns (empty_dict, 0)."""
    state: dict[str, Any] = {}
    pruned, removed = _prune_state(state, tmp_path)
    assert pruned == {}
    assert removed == 0


def test_prune_state_all_entries_exist_returns_zero_removed(tmp_path: Path) -> None:
    """When all files exist, removed_count is 0."""
    _prune_state_make_pursuit(tmp_path, "acme", "deal-x")
    _prune_state_make_pursuit(tmp_path, "acme", "deal-y")
    state: dict[str, Any] = {
        "acme/deal-x": {"stage": "discovery"},
        "acme/deal-y": {"stage": "proposal"},
    }
    pruned, removed = _prune_state(state, tmp_path)
    assert removed == 0
    assert len(pruned) == 2


def test_prune_state_malformed_key_is_kept(tmp_path: Path) -> None:
    """A state key without a slash is kept (no data loss on bad keys)."""
    state: dict[str, Any] = {
        "bad-key-no-slash": {"stage": "discovery"},
    }
    pruned, removed = _prune_state(state, tmp_path)
    assert removed == 0
    assert "bad-key-no-slash" in pruned


def test_prune_state_preserves_entry_values(tmp_path: Path) -> None:
    """Entry values are not mutated during pruning."""
    _prune_state_make_pursuit(tmp_path, "acme", "deal-a")
    original_entry: dict[str, Any] = {"stage": "discovery", "alerted_days_tier": 7}
    state: dict[str, Any] = {"acme/deal-a": original_entry}
    pruned, _ = _prune_state(state, tmp_path)
    assert pruned["acme/deal-a"] == original_entry


def test_prune_state_returns_correct_tuple_type(tmp_path: Path) -> None:
    """Return value is always a (dict, int) tuple."""
    state: dict[str, Any] = {}
    result = _prune_state(state, tmp_path)
    assert isinstance(result, tuple)
    assert len(result) == 2
    assert isinstance(result[0], dict)
    assert isinstance(result[1], int)


def test_prune_state_rejects_key_with_forward_slash_in_account(tmp_path: Path) -> None:
    """B12: account component containing '/' is rejected — not kept, not counted."""
    state: dict[str, Any] = {
        "../../../etc/passwd": {"stage": "discovery"},
    }
    pruned, removed = _prune_state(state, tmp_path)
    # The key splits into ("../../../etc", "passwd") — account contains "/"
    # so it must be silently dropped (neither kept nor counted as a normal removal).
    assert pruned == {}
    assert removed == 0


def test_prune_state_rejects_key_with_dotdot_in_account(tmp_path: Path) -> None:
    """B12: '..' as the account component resolves outside accounts/ — rejected.

    key = "../sensitive" splits into account="..", pursuit_stem="sensitive".
    Neither component contains a literal "/" or "\\", so the separator check
    passes.  However, data_root/accounts/../pursuits/sensitive.md resolves
    to data_root/pursuits/sensitive.md — outside the accounts/ subtree —
    so the resolve() guard fires and the entry is silently dropped.
    """
    state: dict[str, Any] = {
        "../sensitive": {"stage": "discovery"},
    }
    # Splits into ("..", "sensitive") — no literal "/" in either component,
    # but the resolved path escapes the accounts dir.
    pruned, removed = _prune_state(state, tmp_path)
    assert pruned == {}
    assert removed == 0


def test_prune_state_legitimate_key_still_works_after_guards(tmp_path: Path) -> None:
    """B12: guards do not break normal operation for well-formed keys."""
    _prune_state_make_pursuit(tmp_path, "acme-corp", "my-deal")
    state: dict[str, Any] = {"acme-corp/my-deal": {"stage": "discovery"}}
    pruned, removed = _prune_state(state, tmp_path)
    assert removed == 0
    assert "acme-corp/my-deal" in pruned
