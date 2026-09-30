"""Bounded process-group cleanup and resource-limited tool execution."""

import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

ADDRESS_SPACE_LIMIT = 4 * 1024**3
_KILL_GRACE_SECONDS = 1.0


class ProcessError(RuntimeError):
    """A child could not run or be contained within its execution policy."""


def failure_description(exc: BaseException) -> str:
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


def terminate_process_group(
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
            details = "; ".join(failure_description(exc) for exc in cleanup_errors)
            suffix = f"; cleanup interruptions: {details}" if details else ""
            raise ProcessError(f"command did not exit after termination{suffix}")
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            details = "; ".join(failure_description(exc) for exc in cleanup_errors)
            suffix = f"; cleanup interruptions: {details}" if details else ""
            raise ProcessError(f"command did not exit after termination{suffix}") from None
        except BaseException as exc:  # noqa: BLE001 - retry reap until the bounded deadline
            cleanup_errors.append(exc)
            continue
        break
    return cleanup_errors


def run_bounded(
    command: Sequence[str], *, cwd: Path, timeout_seconds: float, output_limit: int
) -> subprocess.CompletedProcess[str]:
    """Capture bounded output from a resource-limited child, never through pipes."""
    if os.name != "posix" or not command or not Path(command[0]).is_absolute():
        raise ProcessError("resource-limited execution requires POSIX and an absolute executable")
    if output_limit <= 0 or timeout_seconds <= 0:
        raise ProcessError("execution limits must be positive")
    launcher = Path(__file__).with_name("resource_limited_exec.py")
    argv = [sys.executable, "-I", str(launcher), str(ADDRESS_SPACE_LIMIT), str(output_limit + 1), *command]
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(argv, cwd=cwd, stdout=stdout, stderr=stderr, start_new_session=True)
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except BaseException as exc:
            try:
                errors = terminate_process_group(process, kill_after_seconds=_KILL_GRACE_SECONDS)
                for error in errors:
                    exc.add_note(failure_description(error))
            except ProcessError as error:
                exc.add_note(str(error))
            if isinstance(exc, subprocess.TimeoutExpired):
                raise ProcessError("command timed out") from exc
            raise
        errors = terminate_process_group(process, kill_after_seconds=_KILL_GRACE_SECONDS)
        if errors:
            raise ProcessError("command cleanup failed: " + "; ".join(failure_description(error) for error in errors))
        stdout.seek(0)
        stderr.seek(0)
        output = stdout.read(output_limit + 1)
        diagnostics = stderr.read(output_limit + 1)
        if len(output) > output_limit or len(diagnostics) > output_limit:
            raise ProcessError("command exceeded its output limit")
    try:
        return subprocess.CompletedProcess(command, returncode, output.decode("utf-8"), diagnostics.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ProcessError("command output is not valid UTF-8") from exc
