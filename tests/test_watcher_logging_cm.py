"""Tests for watcher_logging() context manager (spec 043).

Verifies that teardown_watcher_logging() is called even when the watcher
body raises an exception — preventing permanent logging suppression (historic regression).
"""

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fieldkit.watch.logging import WatcherLoggerState


@pytest.mark.unit
def test_teardown_called_after_exception(tmp_path: Path) -> None:
    """teardown_watcher_logging must be called even when body raises."""
    from fieldkit.watch.logging import watcher_logging

    teardown_calls: list[bool] = []

    def fake_setup(name: str) -> tuple[Path, MagicMock, WatcherLoggerState]:
        return tmp_path / "log.md", MagicMock(spec=logging.FileHandler), WatcherLoggerState(logging.WARNING, True)

    def fake_teardown(
        log_handler: logging.FileHandler,
        state: WatcherLoggerState,
    ) -> None:
        teardown_calls.append(True)

    with (
        patch("fieldkit.watch.logging.setup_watcher_logging", side_effect=fake_setup),
        patch("fieldkit.watch.logging.teardown_watcher_logging", side_effect=fake_teardown),
        pytest.raises(ValueError, match=r"test error"),
        watcher_logging("test-watcher"),
    ):
        raise ValueError("test error")

    assert len(teardown_calls) == 1, "teardown must be called even after exception"


@pytest.mark.unit
def test_teardown_called_on_success(tmp_path: Path) -> None:
    """teardown_watcher_logging must be called on normal exit too."""
    from fieldkit.watch.logging import watcher_logging

    teardown_calls: list[bool] = []

    def fake_setup(name: str) -> tuple[Path, MagicMock, WatcherLoggerState]:
        return tmp_path / "log.md", MagicMock(spec=logging.FileHandler), WatcherLoggerState(logging.WARNING, True)

    def fake_teardown(
        log_handler: logging.FileHandler,
        state: WatcherLoggerState,
    ) -> None:
        teardown_calls.append(True)

    with (
        patch("fieldkit.watch.logging.setup_watcher_logging", side_effect=fake_setup),
        patch("fieldkit.watch.logging.teardown_watcher_logging", side_effect=fake_teardown),
        watcher_logging("test-watcher"),
    ):
        pass  # no exception

    assert len(teardown_calls) == 1, "teardown must be called on normal exit"


@pytest.mark.unit
def test_watcher_logging_passes_name_to_setup(tmp_path: Path) -> None:
    """watcher_logging() must pass the watcher name to setup_watcher_logging."""
    from fieldkit.watch.logging import watcher_logging

    setup_calls: list[str] = []

    def fake_setup(name: str) -> tuple[Path, MagicMock, WatcherLoggerState]:
        setup_calls.append(name)
        return tmp_path / "log.md", MagicMock(spec=logging.FileHandler), WatcherLoggerState(logging.WARNING, True)

    def fake_teardown(
        log_handler: logging.FileHandler,
        state: WatcherLoggerState,
    ) -> None:
        pass

    with (
        patch("fieldkit.watch.logging.setup_watcher_logging", side_effect=fake_setup),
        patch("fieldkit.watch.logging.teardown_watcher_logging", side_effect=fake_teardown),
        watcher_logging("my-watcher"),
    ):
        pass

    assert setup_calls == ["my-watcher"]


@pytest.mark.unit
def test_exception_restores_named_logger_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """historic regression: exceptional cleanup restores the watcher namespace exactly."""
    from fieldkit.watch.logging import watcher_logging

    monkeypatch.setattr("fieldkit.watch.logging.get_fieldkit_home", lambda: tmp_path)
    watcher_logger = logging.getLogger("fieldkit.watch")
    prior_level = watcher_logger.level
    prior_propagate = watcher_logger.propagate
    prior_handlers = list(watcher_logger.handlers)
    watcher_logger.setLevel(logging.ERROR)
    watcher_logger.propagate = True

    try:
        with pytest.raises(RuntimeError, match="watcher failed"), watcher_logging("test-watcher"):
            raise RuntimeError("watcher failed")

        assert watcher_logger.level == logging.ERROR
        assert watcher_logger.propagate is True
        assert watcher_logger.handlers == prior_handlers
    finally:
        watcher_logger.setLevel(prior_level)
        watcher_logger.propagate = prior_propagate
