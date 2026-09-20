#!/usr/bin/env python3
"""Process supervision and Git discovery for transactional hook installation."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import FrameType

GIT_REPOSITORY_ENVIRONMENT = frozenset(
    {
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_SYSTEM",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_WORK_TREE",
    }
)
GIT_REPOSITORY_ENVIRONMENT_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
GIT_LOCATION_ENVIRONMENT = frozenset({"GIT_COMMON_DIR", "GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"})


class InstallError(RuntimeError):
    """A user-facing hook installation failure."""


class InstallInterrupted(BaseException):
    """A termination signal received while hook installation is active."""

    def __init__(self, received_signal: signal.Signals) -> None:
        super().__init__(received_signal.name)
        self.received_signal = received_signal


class TerminationSignals:
    """Record signals during a critical section and deliver the first afterward."""

    def __init__(self) -> None:
        self._defer_depth = 0
        self._pending: signal.Signals | None = None
        self._received: signal.Signals | None = None

    def handle(self, signum: int, _frame: FrameType | None) -> None:
        """Record the first termination request for delivery at a safe checkpoint."""
        received = signal.Signals(signum)
        if self._pending is None and self._received is None:
            self._pending = received

    def checkpoint(self) -> None:
        """Deliver a recorded signal when no critical section is active."""
        if self._defer_depth == 0 and self._pending is not None:
            pending = self._pending
            self._pending = None
            self._received = pending
            raise InstallInterrupted(pending)

    def attach_to(self, exc: BaseException) -> None:
        """Attach a recorded signal to an error that remains primary."""
        if self._pending is None:
            return
        pending = self._pending
        self._pending = None
        self._received = pending
        exc.add_note(f"termination requested by {pending.name}")

    @contextmanager
    def defer(self) -> Iterator[None]:
        """Defer the first handled signal until the outermost critical section exits."""
        escaping: BaseException | None = None
        self._defer_depth += 1
        try:
            yield
        except BaseException as exc:
            escaping = exc
            raise
        finally:
            self._defer_depth -= 1
            if self._defer_depth == 0 and self._pending is not None:
                if escaping is None:
                    self.checkpoint()
                else:
                    self.attach_to(escaping)


def _subprocess_environment() -> dict[str, str]:
    """Return the current environment without inherited Git repository selectors."""
    return {
        key: value
        for key, value in os.environ.items()
        if key not in GIT_REPOSITORY_ENVIRONMENT and not key.startswith(GIT_REPOSITORY_ENVIRONMENT_PREFIXES)
    }


def _git_config_environment() -> dict[str, str]:
    """Keep caller configuration selectors while removing repository routing."""
    return {key: value for key, value in os.environ.items() if key not in GIT_LOCATION_ENVIRONMENT}


def _terminate_process_group(
    process: subprocess.Popen[bytes] | subprocess.Popen[str], *, kill_after_seconds: float
) -> list[BaseException]:
    """Terminate and reap a subprocess group despite interruptions during cleanup."""
    cleanup_errors: list[BaseException] = []
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except BaseException as exc:  # noqa: BLE001 - cleanup must continue through interruption
        cleanup_errors.append(exc)
    try:
        process.wait(timeout=kill_after_seconds)
    except subprocess.TimeoutExpired:
        pass
    except BaseException as exc:  # noqa: BLE001 - SIGKILL and reap must still run
        cleanup_errors.append(exc)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except BaseException as exc:  # noqa: BLE001 - the final reap must still run
        cleanup_errors.append(exc)
    reap_deadline = time.monotonic() + kill_after_seconds
    while True:
        remaining = reap_deadline - time.monotonic()
        if remaining <= 0:
            details = "; ".join(_failure_description(exc) for exc in cleanup_errors)
            suffix = f"; cleanup interruptions: {details}" if details else ""
            raise InstallError(f"command did not exit after termination{suffix}")
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            details = "; ".join(_failure_description(exc) for exc in cleanup_errors)
            suffix = f"; cleanup interruptions: {details}" if details else ""
            raise InstallError(f"command did not exit after termination{suffix}") from None
        except BaseException as exc:  # noqa: BLE001 - retry reap until the bounded deadline
            cleanup_errors.append(exc)
            continue
        break
    return cleanup_errors


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    kill_after_seconds: float,
    environment: Mapping[str, str] | None = None,
    termination_signals: TerminationSignals | None = None,
) -> None:
    """Run one installer command and terminate its process group on timeout."""
    subprocess_environment = _subprocess_environment()
    if environment is not None:
        subprocess_environment.update(environment)
    process: subprocess.Popen[bytes] | None = None
    try:
        if termination_signals is None:
            process = subprocess.Popen(command, cwd=cwd, env=subprocess_environment, start_new_session=True)
        else:
            with termination_signals.defer():
                process = subprocess.Popen(command, cwd=cwd, env=subprocess_environment, start_new_session=True)
    except BaseException as exc:
        if process is not None:
            for cleanup_error in _terminate_process_group(process, kill_after_seconds=kill_after_seconds):
                exc.add_note(_failure_description(cleanup_error))
        if isinstance(exc, OSError):
            raise InstallError(f"cannot run {command[0]}: {exc}") from exc
        raise
    assert process is not None
    try:
        deadline = time.monotonic() + timeout_seconds
        while True:
            if termination_signals is not None:
                termination_signals.checkpoint()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout_seconds)
            try:
                return_code = process.wait(timeout=min(0.05, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        if termination_signals is not None:
            termination_signals.checkpoint()
    except subprocess.TimeoutExpired:
        try:
            cleanup_errors = _terminate_process_group(process, kill_after_seconds=kill_after_seconds)
        except InstallError as exc:
            raise InstallError(f"command cleanup failed for {' '.join(command)}: {exc}") from exc
        error = InstallError(f"command timed out after {timeout_seconds:g}s: {' '.join(command)}")
        for cleanup_error in cleanup_errors:
            error.add_note(_failure_description(cleanup_error))
        raise error from None
    except BaseException as exc:
        try:
            cleanup_errors = _terminate_process_group(process, kill_after_seconds=kill_after_seconds)
        except InstallError as cleanup_error:
            exc.add_note(str(cleanup_error))
        else:
            for recorded_error in cleanup_errors:
                exc.add_note(_failure_description(recorded_error))
        raise
    if return_code != 0:
        raise InstallError(f"command failed with exit {return_code}: {' '.join(command)}")


def _git_output(
    arguments: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    kill_after_seconds: float,
    termination_signals: TerminationSignals | None = None,
    environment: Mapping[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run a bounded Git query in a disposable process group."""
    command = ["git", *arguments]
    query_environment = dict(environment) if environment is not None else _subprocess_environment()
    process: subprocess.Popen[str] | None = None
    try:
        if termination_signals is None:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=query_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        else:
            with termination_signals.defer():
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=query_environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
    except BaseException as exc:
        if process is not None:
            for cleanup_error in _terminate_process_group(process, kill_after_seconds=kill_after_seconds):
                exc.add_note(_failure_description(cleanup_error))
        if isinstance(exc, OSError):
            raise InstallError(f"cannot query Git: {exc}") from exc
        raise
    assert process is not None
    try:
        deadline = time.monotonic() + timeout_seconds
        while True:
            if termination_signals is not None:
                termination_signals.checkpoint()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout_seconds)
            try:
                stdout, stderr = process.communicate(timeout=min(0.05, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        if termination_signals is not None:
            termination_signals.checkpoint()
    except subprocess.TimeoutExpired:
        try:
            cleanup_errors = _terminate_process_group(process, kill_after_seconds=kill_after_seconds)
        except InstallError as exc:
            raise InstallError(f"Git query cleanup failed for {' '.join(command)}: {exc}") from exc
        error = InstallError(f"Git query timed out after {timeout_seconds:g}s: {' '.join(command)}")
        for cleanup_error in cleanup_errors:
            error.add_note(_failure_description(cleanup_error))
        raise error from None
    except BaseException as exc:
        try:
            cleanup_errors = _terminate_process_group(process, kill_after_seconds=kill_after_seconds)
        except InstallError as cleanup_error:
            exc.add_note(str(cleanup_error))
        else:
            for recorded_error in cleanup_errors:
                exc.add_note(_failure_description(recorded_error))
        raise
    return process.returncode, stdout.strip(), stderr.strip()


def _hooks_directory(
    cwd: Path,
    *,
    timeout_seconds: float,
    kill_after_seconds: float,
    termination_signals: TerminationSignals | None = None,
) -> Path:
    """Return the absolute shared hooks directory and reject custom routing."""
    for environment in (_git_config_environment(), _subprocess_environment()):
        configured_return_code, _, configured_error = _git_output(
            ["config", "--get", "core.hooksPath"],
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            kill_after_seconds=kill_after_seconds,
            termination_signals=termination_signals,
            environment=environment,
        )
        if configured_return_code == 0:
            raise InstallError("core.hooksPath is not supported by pre-commit; unset it before installing hooks")
        if configured_return_code != 1:
            detail = f": {configured_error}" if configured_error else ""
            raise InstallError(f"cannot inspect Git hook routing{detail}")
    common_dir_return_code, common_dir_output, common_dir_error = _git_output(
        ["rev-parse", "--git-common-dir"],
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        kill_after_seconds=kill_after_seconds,
        termination_signals=termination_signals,
    )
    if common_dir_return_code != 0:
        detail = f": {common_dir_error}" if common_dir_error else ""
        raise InstallError(f"cannot resolve the Git hooks directory{detail}")
    common_dir = Path(common_dir_output)
    if not common_dir.is_absolute():
        common_dir = (cwd / common_dir).resolve()
    return common_dir / "hooks"


@contextmanager
def _termination_signals_as_interrupts() -> Iterator[TerminationSignals]:
    """Convert the first termination signal into an exception so cleanup runs."""
    previous_handlers: dict[signal.Signals, int | Callable[[int, FrameType | None], object] | None] = {}
    termination_signals = TerminationSignals()

    for handled_signal in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        previous_handlers[handled_signal] = signal.signal(handled_signal, termination_signals.handle)
    escaping: BaseException | None = None
    try:
        yield termination_signals
    except BaseException as exc:
        escaping = exc
        raise
    finally:
        for handled_signal, previous_handler in previous_handlers.items():
            signal.signal(handled_signal, previous_handler)
        if escaping is None:
            termination_signals.checkpoint()
        else:
            termination_signals.attach_to(escaping)


def _failure_description(exc: BaseException) -> str:
    """Describe an exception, including diagnostic notes and its chained cause."""
    seen: set[int] = set()
    parts: list[str] = []
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        detail = str(current)
        description = f"{type(current).__name__}: {detail}" if detail else type(current).__name__
        notes = getattr(current, "__notes__", ())
        if notes:
            description += f" (notes: {'; '.join(notes)})"
        parts.append(description)
        current = current.__cause__
    return " caused by ".join(parts)
