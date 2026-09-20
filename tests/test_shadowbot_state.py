"""State-selection and persistence tests for the ShadowBot client."""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.shadowbot.auth import ShadowbotAuthError
from fieldkit.shadowbot.client import ShadowbotClient, ShadowbotQueryError
from fieldkit.shadowbot.state import (
    ShadowbotStateError,
    StateTarget,
    load_thread_id,
    save_thread_id,
    select_state_target,
)


@pytest.fixture(autouse=True)
def _clear_state_selectors(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in ("SHADOWBOT_STATE_FILE", "SHADOWBOT_SESSION_ID", "CLAUDE_SESSION_ID"):
        monkeypatch.delenv(variable, raising=False)


def _patch_state_dir(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setattr("fieldkit.shadowbot.state.get_state_dir", lambda: path)


# ---------------------------------------------------------------------------
# State target and persistence tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_state_file_permissions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_save_thread_id creates a new state file with 0o600 permissions."""
    if os.getuid() == 0:
        pytest.skip("Running as root — permission checks are not meaningful")

    _patch_state_dir(monkeypatch, tmp_path)

    target = select_state_target()
    try:
        save_thread_id(target, "test-thread-id")
    finally:
        target.close()

    state_file = tmp_path / "shadowbot-state.json"
    assert state_file.exists()
    mode = oct(state_file.stat().st_mode & 0o777)
    assert mode == oct(0o600), f"Expected 0o600, got {mode}"


@pytest.mark.unit
def test_state_file_permissions_overwrite(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_save_thread_id overwrites an existing 0o644 state file and enforces 0o600."""
    if os.getuid() == 0:
        pytest.skip("Running as root — permission checks are not meaningful")

    _patch_state_dir(monkeypatch, tmp_path)

    # Create existing state file with broad permissions
    state_file = tmp_path / "shadowbot-state.json"
    state_file.write_text('{"thread_id": "old"}', encoding="utf-8")
    state_file.chmod(0o644)

    target = select_state_target()
    try:
        save_thread_id(target, "new-thread-id")
    finally:
        target.close()

    mode = oct(state_file.stat().st_mode & 0o777)
    assert mode == oct(0o600), f"Expected 0o600 after overwrite, got {mode}"
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["thread_id"] == "new-thread-id"


@pytest.mark.unit
def test_state_target_explicit_precedes_both_session_selectors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_state_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("SHADOWBOT_STATE_FILE", "custom.json")
    monkeypatch.setenv("SHADOWBOT_SESSION_ID", "shadow-session")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "claude-session")

    target = select_state_target()
    try:
        assert target.filename == "custom.json"
    finally:
        target.close()


@pytest.mark.unit
def test_state_target_shadowbot_session_precedes_claude_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_state_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("SHADOWBOT_SESSION_ID", "shadow-session")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "claude-session")

    target = select_state_target()
    try:
        assert target.filename == "shadowbot-state-shadow-session.json"
    finally:
        target.close()


@pytest.mark.unit
def test_state_target_derives_exact_distinct_session_filenames(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_state_dir(monkeypatch, tmp_path)
    filenames: list[str] = []
    for session_id in ("alpha-session", "beta-session"):
        monkeypatch.setenv("SHADOWBOT_SESSION_ID", session_id)
        target = select_state_target()
        try:
            filenames.append(target.filename)
        finally:
            target.close()

    assert filenames == ["shadowbot-state-alpha-session.json", "shadowbot-state-beta-session.json"]


@pytest.mark.unit
def test_state_target_uses_claude_compatibility_selector(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_state_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "claude-session")

    target = select_state_target()
    try:
        assert target.filename == "shadowbot-state-claude-session.json"
    finally:
        target.close()


@pytest.mark.unit
def test_state_target_uses_global_default_when_selector_keys_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_state_dir(monkeypatch, tmp_path)

    target = select_state_target()
    try:
        assert target.filename == "shadowbot-state.json"
    finally:
        target.close()


@pytest.mark.unit
@pytest.mark.parametrize("value", ["", " ", "bad\n", "x" * 129])
def test_state_target_rejects_present_empty_whitespace_newline_and_overlength_selectors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str
) -> None:
    _patch_state_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("SHADOWBOT_SESSION_ID", value)

    with pytest.raises(ShadowbotStateError, match="Invalid SHADOWBOT_SESSION_ID"):
        select_state_target()


@pytest.mark.unit
def test_state_target_rejects_invalid_higher_priority_without_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_state_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("SHADOWBOT_STATE_FILE", " ")
    monkeypatch.setenv("SHADOWBOT_SESSION_ID", "valid-session")

    with pytest.raises(ShadowbotStateError, match="Invalid SHADOWBOT_STATE_FILE"):
        select_state_target()


@pytest.mark.unit
@pytest.mark.parametrize("explicit", ["custom.json", "absolute"])
def test_state_target_accepts_relative_and_absolute_direct_children(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, explicit: str
) -> None:
    _patch_state_dir(monkeypatch, tmp_path)
    value = str(tmp_path / "custom.json") if explicit == "absolute" else "custom.json"
    monkeypatch.setenv("SHADOWBOT_STATE_FILE", value)

    target = select_state_target()
    try:
        assert target.filename == "custom.json"
    finally:
        target.close()


@pytest.mark.unit
@pytest.mark.parametrize("explicit", [".", "nested/state.json", "../state.json", "/tmp/outside.json"])
def test_state_target_rejects_root_nested_traversal_and_absolute_escape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, explicit: str
) -> None:
    _patch_state_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("SHADOWBOT_STATE_FILE", explicit)

    with pytest.raises(ShadowbotStateError, match="SHADOWBOT_STATE_FILE"):
        select_state_target()


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_state_target_rejects_symlink_directory_fifo_and_non_regular_targets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str
) -> None:
    _patch_state_dir(monkeypatch, tmp_path)
    target_path = tmp_path / "custom.json"
    if kind == "symlink":
        target_path.symlink_to(tmp_path / "missing")
    elif kind == "directory":
        target_path.mkdir()
    else:
        os.mkfifo(target_path)
    monkeypatch.setenv("SHADOWBOT_STATE_FILE", target_path.name)

    with pytest.raises(ShadowbotStateError, match="regular file"):
        select_state_target()


@pytest.mark.unit
def test_state_target_creates_missing_root_mode_0700_then_opens_nofollow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "missing" / "state"
    _patch_state_dir(monkeypatch, state_dir)

    target = select_state_target()
    try:
        assert target.filename == "shadowbot-state.json"
        assert state_dir.is_dir()
        if os.geteuid() != 0:
            assert state_dir.stat().st_mode & 0o777 == 0o700
    finally:
        target.close()


@pytest.mark.unit
def test_state_target_is_selected_before_new_thread_network_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_state_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("SHADOWBOT_SESSION_ID", "bad/value")
    with (
        patch("httpx.Client") as client_class,
        pytest.raises(ShadowbotQueryError, match="Invalid SHADOWBOT_SESSION_ID"),
    ):
        ShadowbotClient("token").query("hello", new_thread=True)

    client_class.assert_not_called()


@pytest.mark.unit
def test_load_missing_state_returns_none_and_malformed_state_degrades(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_state_dir(monkeypatch, tmp_path)
    target = select_state_target()
    try:
        missing_result = load_thread_id(target)
        (tmp_path / target.filename).write_text("not-json", encoding="utf-8")
        malformed_result = load_thread_id(target)
    finally:
        target.close()

    assert missing_result is None
    assert malformed_result is None


@pytest.mark.unit
def test_load_rejects_fifo_swapped_in_after_selection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_state_dir(monkeypatch, tmp_path)
    target = select_state_target()
    os.mkfifo(tmp_path / target.filename)
    try:
        with pytest.raises(ShadowbotStateError, match="regular file"):
            load_thread_id(target)
    finally:
        target.close()


@pytest.mark.unit
def test_save_uses_selected_directory_descriptor_and_mode_0600(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_state_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("SHADOWBOT_SESSION_ID", "one")
    target = select_state_target()
    try:
        save_thread_id(target, "thread-one")
        state_path = tmp_path / "shadowbot-state-one.json"
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        assert saved["thread_id"] == "thread-one"
        if os.geteuid() != 0:
            assert state_path.stat().st_mode & 0o777 == 0o600
    finally:
        target.close()


@pytest.mark.unit
def test_save_remains_pinned_when_root_path_is_replaced(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _patch_state_dir(monkeypatch, state_dir)
    target = select_state_target()
    moved_dir = tmp_path / "original-state"
    state_dir.rename(moved_dir)
    state_dir.mkdir()
    try:
        save_thread_id(target, "pinned-thread")
    finally:
        target.close()

    assert json.loads((moved_dir / "shadowbot-state.json").read_text())["thread_id"] == "pinned-thread"
    assert not (state_dir / "shadowbot-state.json").exists()


@pytest.mark.unit
@pytest.mark.parametrize("operation", ["open", "fchmod", "write", "fsync", "replace"])
def test_save_cleans_temp_and_chains_each_filesystem_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, operation: str
) -> None:
    _patch_state_dir(monkeypatch, tmp_path)
    target = select_state_target()
    monkeypatch.setattr(f"fieldkit.shadowbot.state.os.{operation}", MagicMock(side_effect=OSError(operation)))
    try:
        with pytest.raises(ShadowbotStateError, match="Failed to save") as exc_info:
            save_thread_id(target, "thread")
        assert isinstance(exc_info.value.__cause__, OSError)
        assert list(tmp_path.glob(".shadowbot-tmp-*")) == []
    finally:
        target.close()


@pytest.mark.unit
def test_save_preserves_write_failure_when_cleanup_also_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_state_dir(monkeypatch, tmp_path)
    target = select_state_target()
    monkeypatch.setattr("fieldkit.shadowbot.state.os.write", MagicMock(side_effect=OSError("disk full")))
    monkeypatch.setattr("fieldkit.shadowbot.state.os.unlink", MagicMock(side_effect=OSError("cleanup denied")))
    try:
        with pytest.raises(ShadowbotStateError, match="Failed to save") as exc_info:
            save_thread_id(target, "thread")
    finally:
        target.close()

    assert isinstance(exc_info.value.__cause__, OSError)
    assert str(exc_info.value.__cause__) == "disk full"
    assert any("cleanup denied" in note for note in exc_info.value.__cause__.__notes__)


@pytest.mark.unit
def test_query_preserves_auth_error_when_state_close_also_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_state_dir(monkeypatch, tmp_path)
    with (
        patch.object(
            ShadowbotClient,
            "_query_with_stale_thread_recovery",
            side_effect=ShadowbotAuthError("expired"),
        ),
        patch.object(StateTarget, "close", side_effect=ShadowbotStateError("close failed")),
        pytest.raises(ShadowbotAuthError, match="expired") as exc_info,
    ):
        ShadowbotClient("token").query("hello")

    assert any("close failed" in note for note in exc_info.value.__notes__)


@pytest.mark.unit
def test_state_selection_failure_closes_root_and_preserves_validation_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_state_dir(monkeypatch, tmp_path)
    (tmp_path / "custom.json").mkdir()
    monkeypatch.setenv("SHADOWBOT_STATE_FILE", "custom.json")
    with (
        patch("fieldkit.shadowbot.state.os.close", wraps=os.close) as close,
        pytest.raises(ShadowbotStateError, match="regular file"),
    ):
        select_state_target()

    close.assert_called_once()


@pytest.mark.unit
def test_state_read_open_failure_is_chained(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_state_dir(monkeypatch, tmp_path)
    target = select_state_target()
    monkeypatch.setattr("fieldkit.shadowbot.state.os.open", MagicMock(side_effect=PermissionError("denied")))
    try:
        with pytest.raises(ShadowbotStateError, match="Failed to open ShadowBot state file") as exc_info:
            load_thread_id(target)
    finally:
        target.close()

    assert isinstance(exc_info.value.__cause__, PermissionError)
