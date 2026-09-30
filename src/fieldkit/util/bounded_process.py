"""Bounded fixed-argv subprocess execution with process-group cleanup."""

import os
import select
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Literal

_READ_CHUNK_BYTES = 64 * 1024
_DRAIN_POLL_SECONDS = 0.01
ProcessFailureReason = Literal["start", "pipes", "timeout", "overflow", "cleanup"]


class BoundedProcessError(RuntimeError):
    """A fixed-argv child failed without yielding a complete bounded result."""

    def __init__(self, message: str, *, reason: ProcessFailureReason) -> None:
        super().__init__(message)
        self.reason = reason


class ProcessOwnershipLostError(BoundedProcessError):
    """A former PID must no longer be used for signalling or waiting."""


@dataclass(frozen=True)
class BoundedProcessResult:
    """Completed child outcome whose retained streams fit declared limits."""

    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class BoundedProcessBytesResult:
    """Completed child outcome retaining bounded binary streams."""

    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass
class _OutputDrainer:
    descriptor: int | None
    thread: threading.Thread | None = None


def require_unreaped_exit_observation() -> None:
    """Fail before launch unless POSIX can observe an exit without reaping it."""
    required = ("waitid", "P_PID", "WEXITED", "WNOHANG", "WNOWAIT")
    if any(not hasattr(os, name) for name in required):
        raise BoundedProcessError(
            "safe child process-group observation is unavailable",
            reason="start",
        )


def process_exited_unreaped(process: subprocess.Popen[bytes]) -> bool:
    """Observe a child exit while retaining PID/PGID ownership until cleanup."""
    require_unreaped_exit_observation()
    try:
        result = os.waitid(
            os.P_PID,
            process.pid,
            os.WEXITED | os.WNOHANG | os.WNOWAIT,
        )
    except (ChildProcessError, OSError) as exc:
        raise ProcessOwnershipLostError("child process ownership was lost", reason="cleanup") from exc
    return result is not None


def terminate_process_group(process: subprocess.Popen[bytes], *, cleanup_timeout: float) -> None:
    """Terminate an owned isolated process group, then reap its leader once."""
    if not isfinite(cleanup_timeout) or cleanup_timeout <= 0:
        raise ValueError("cleanup timeout must be finite and positive")
    deadline = time.monotonic() + cleanup_timeout
    pid = getattr(process, "pid", None)
    try:
        if isinstance(pid, int) and pid > 0:
            os.killpg(pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except ChildProcessError as exc:
        raise ProcessOwnershipLostError("child process ownership was lost", reason="cleanup") from exc
    except subprocess.TimeoutExpired as exc:
        raise BoundedProcessError("child process cleanup timed out", reason="cleanup") from exc


def _drain(descriptor: int, retained: bytearray, limit: int, overflow: threading.Event, stop: threading.Event) -> None:
    try:
        while not stop.is_set():
            readable, _, _ = select.select([descriptor], [], [], _DRAIN_POLL_SECONDS)
            if not readable:
                continue
            try:
                chunk = os.read(descriptor, _READ_CHUNK_BYTES)
            except BlockingIOError:
                continue
            if not chunk:
                break
            remaining = limit - len(retained)
            if remaining > 0:
                retained.extend(chunk[:remaining])
            if len(chunk) > remaining:
                overflow.set()
    except (OSError, ValueError):
        if not stop.is_set():
            overflow.set()
    finally:
        os.close(descriptor)


def _finish_drainers(
    process: subprocess.Popen[bytes],
    drainers: list[_OutputDrainer],
    stop: threading.Event,
    *,
    deadline: float,
    abandon_output: bool,
) -> None:
    if process.stdout is not None:
        process.stdout.close()
    if process.stderr is not None:
        process.stderr.close()
    if abandon_output:
        stop.set()
    for drainer in drainers:
        if drainer.thread is not None and drainer.thread.ident is not None:
            drainer.thread.join(timeout=max(0.0, deadline - time.monotonic()))
        elif drainer.descriptor is not None:
            os.close(drainer.descriptor)
            drainer.descriptor = None
    if any(drainer.thread is not None and drainer.thread.is_alive() for drainer in drainers):
        stop.set()
        raise BoundedProcessError("support process output cleanup timed out", reason="cleanup")


def run_bounded_process_bytes(
    argv: list[str],
    *,
    timeout: float,
    stdout_limit: int,
    stderr_limit: int,
    cleanup_timeout: float,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    pass_fds: tuple[int, ...] = (),
) -> BoundedProcessBytesResult:
    """Run fixed argv with bounded binary streams and process-group cleanup."""
    if (
        stdout_limit < 0
        or stderr_limit < 0
        or not isfinite(timeout)
        or timeout <= 0
        or not isfinite(cleanup_timeout)
        or cleanup_timeout <= 0
    ):
        raise ValueError("process timeouts must be finite and positive and byte bounds must be non-negative")
    require_unreaped_exit_observation()
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            pass_fds=pass_fds,
        )
    except OSError as exc:
        raise BoundedProcessError("support process could not start", reason="start") from exc
    if process.stdout is None or process.stderr is None:
        terminate_process_group(process, cleanup_timeout=cleanup_timeout)
        raise BoundedProcessError("support process output pipes were unavailable", reason="pipes")

    stdout = bytearray()
    stderr = bytearray()
    overflow = threading.Event()
    stop = threading.Event()
    drainers: list[_OutputDrainer] = []
    failure: ProcessFailureReason | None = None
    cleanup_deadline: float | None = None
    cleanup_completed = False
    try:
        for stream, retained, limit in ((process.stdout, stdout, stdout_limit), (process.stderr, stderr, stderr_limit)):
            descriptor = os.dup(stream.fileno())
            drainer = _OutputDrainer(descriptor)
            drainers.append(drainer)
            os.set_blocking(descriptor, False)
            drainer.thread = threading.Thread(
                target=_drain, args=(descriptor, retained, limit, overflow, stop), daemon=True
            )
            stream.close()
            drainer.thread.start()
        deadline = time.monotonic() + timeout
        while not process_exited_unreaped(process):
            if overflow.is_set():
                failure = "overflow"
                break
            if time.monotonic() >= deadline:
                failure = "timeout"
                break
            time.sleep(0.01)
        cleanup_deadline = time.monotonic() + cleanup_timeout
        terminate_process_group(process, cleanup_timeout=cleanup_timeout)
        cleanup_completed = True
        _finish_drainers(process, drainers, stop, deadline=cleanup_deadline, abandon_output=False)
    except BaseException as exc:
        if cleanup_deadline is None:
            cleanup_deadline = time.monotonic() + cleanup_timeout
        try:
            if not cleanup_completed and not isinstance(exc, ProcessOwnershipLostError):
                remaining = cleanup_deadline - time.monotonic()
                if remaining <= 0:
                    raise BoundedProcessError("support process cleanup timed out", reason="cleanup")
                if process.returncode is None:
                    process_exited_unreaped(process)
                    remaining = cleanup_deadline - time.monotonic()
                    if remaining <= 0:
                        raise BoundedProcessError("support process cleanup timed out", reason="cleanup")
                    terminate_process_group(process, cleanup_timeout=remaining)
        except (BoundedProcessError, OSError, KeyboardInterrupt) as cleanup_error:
            exc.add_note(f"support process cleanup did not complete ({type(cleanup_error).__name__})")
        try:
            _finish_drainers(process, drainers, stop, deadline=cleanup_deadline, abandon_output=True)
        except BoundedProcessError as cleanup_error:
            exc.add_note(str(cleanup_error))
        raise
    if overflow.is_set() and failure is None:
        failure = "overflow"
    if failure == "timeout":
        raise BoundedProcessError("support process timed out", reason="timeout")
    if failure == "overflow":
        raise BoundedProcessError("support process output exceeded its limit", reason="overflow")
    returncode = process.returncode
    if returncode is None:
        raise BoundedProcessError("support process did not terminate", reason="cleanup")
    return BoundedProcessBytesResult(returncode, bytes(stdout), bytes(stderr))


def run_bounded_process(
    argv: list[str],
    *,
    timeout: float,
    stdout_limit: int,
    stderr_limit: int,
    cleanup_timeout: float,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    pass_fds: tuple[int, ...] = (),
) -> BoundedProcessResult:
    """Run fixed argv with bounded UTF-8-decoded streams and group cleanup."""
    result = run_bounded_process_bytes(
        argv,
        timeout=timeout,
        stdout_limit=stdout_limit,
        stderr_limit=stderr_limit,
        cleanup_timeout=cleanup_timeout,
        cwd=cwd,
        env=env,
        pass_fds=pass_fds,
    )
    return BoundedProcessResult(
        result.returncode,
        result.stdout.decode("utf-8", errors="replace"),
        result.stderr.decode("utf-8", errors="replace"),
    )
