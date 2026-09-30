"""Bounded sidecar locks preserve existing data and release on every exit."""

import errno
import math
import select
import stat
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import pytest

from fieldkit.util.atomic import (
    PathLockTimeoutError,
    exclusive_file_lock,
    exclusive_path_lock,
    prepare_runtime_lock_path,
)

pytestmark = pytest.mark.unit
_LOCK_PROBE_TIMEOUT_SECONDS = 10


@pytest.mark.parametrize("redirect", ["locks", "locks/tasks"])
def test_runtime_lock_refuses_redirected_directories(tmp_path: Path, redirect: str) -> None:
    runtime = tmp_path / "runtime"
    outside = tmp_path / "outside"
    outside.mkdir()
    redirected = runtime / redirect
    redirected.parent.mkdir(parents=True)
    redirected.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="Runtime lock directory redirects"):
        lock = prepare_runtime_lock_path(tmp_path / "TASKS.md", runtime, "tasks")
        with exclusive_file_lock(lock, timeout_seconds=0):
            pytest.fail("Redirected runtime lock was acquired")
    assert list(outside.iterdir()) == []


def test_runtime_root_alias_uses_the_same_lock(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(runtime, target_is_directory=True)
    target = tmp_path / "TASKS.md"
    direct = prepare_runtime_lock_path(target, runtime, "tasks")
    assert prepare_runtime_lock_path(target, alias, "tasks") == direct
    with (
        exclusive_file_lock(direct, timeout_seconds=0),
        pytest.raises(PathLockTimeoutError, match="Timed out acquiring path lock"),
        exclusive_file_lock(prepare_runtime_lock_path(target, alias, "tasks"), timeout_seconds=0),
    ):
        pytest.fail("Runtime alias split lock ownership")


_LOCK_PROBE = """
import sys
from pathlib import Path
from fieldkit.util.atomic import exclusive_path_lock, PathLockTimeoutError
try:
    with exclusive_path_lock(Path(sys.argv[1]), timeout_seconds=0):
        sys.stdout.write('acquired')
except PathLockTimeoutError:
    sys.stdout.write('busy')
"""


@pytest.mark.parametrize("timeout", [0.0, 0.05])
def test_lock_contention_has_bounded_wait(tmp_path: Path, timeout: float) -> None:
    target = tmp_path / "state.json"
    target.write_text("untouched", encoding="utf-8")
    with exclusive_path_lock(target):
        started = time.monotonic()
        with (
            pytest.raises(PathLockTimeoutError, match="Timed out acquiring path lock"),
            exclusive_path_lock(target, timeout_seconds=timeout),
        ):
            pytest.fail("Contending writer entered critical section")
        elapsed = time.monotonic() - started
    assert timeout <= elapsed < 2
    assert target.read_text(encoding="utf-8") == "untouched"


def test_lock_releases_after_body_failure_and_keeps_inode(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    with pytest.raises(ValueError, match="body failure"), exclusive_path_lock(target, timeout_seconds=0):
        raise ValueError("body failure")
    sidecar = target.with_suffix(".json.lock")
    inode = sidecar.stat().st_ino
    with exclusive_path_lock(target, timeout_seconds=0):
        assert sidecar.stat().st_ino == inode
    assert not target.exists()
    assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600


@pytest.mark.parametrize("timeout", [-1, math.inf, math.nan])
def test_invalid_timeout_is_rejected_before_filesystem_effects(tmp_path: Path, timeout: float) -> None:
    with (
        pytest.raises(ValueError, match="Invalid path lock timeout"),
        exclusive_path_lock(tmp_path / "nested/state", timeout_seconds=timeout),
    ):
        pytest.fail("Invalid timeout entered critical section")
    assert list(tmp_path.iterdir()) == []


def test_lock_rejects_symlink_sidecar(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    other = tmp_path / "other"
    other.write_text("untouched", encoding="utf-8")
    target.with_suffix(".json.lock").symlink_to(other)
    with pytest.raises(OSError) as caught, exclusive_path_lock(target, timeout_seconds=0):
        pytest.fail("Symlink lock accepted")
    assert caught.value.errno == errno.ELOOP
    assert other.read_text(encoding="utf-8") == "untouched"


def test_lock_refuses_another_process_then_allows_it_after_release(tmp_path: Path) -> None:
    target = tmp_path / "pipeline.db"
    argv = [sys.executable, "-c", _LOCK_PROBE, str(target)]
    with exclusive_path_lock(target):
        busy = subprocess.run(argv, check=True, capture_output=True, text=True, timeout=_LOCK_PROBE_TIMEOUT_SECONDS)
    assert busy.stdout == "busy"
    released = subprocess.run(argv, check=True, capture_output=True, text=True, timeout=_LOCK_PROBE_TIMEOUT_SECONDS)
    assert released.stdout == "acquired"
    assert not target.exists()


_WAITING_PROBE = """
import sys
from pathlib import Path
from fieldkit.util import atomic
original = atomic.fcntl.flock
def observed(fd, operation):
    try:
        return original(fd, operation)
    except BlockingIOError:
        sys.stdout.write('waiting\\n')
        sys.stdout.flush()
        raise
atomic.fcntl.flock = observed
with atomic.exclusive_path_lock(Path(sys.argv[1]), timeout_seconds=5):
    sys.stdout.write('acquired\\n')
"""


def test_waiting_process_acquires_after_holder_releases(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    with ExitStack() as locks:
        locks.enter_context(exclusive_path_lock(target))
        with subprocess.Popen(
            [sys.executable, "-c", _WAITING_PROBE, str(target)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as child:
            try:
                assert child.stdout is not None
                readable, _, _ = select.select([child.stdout], [], [], _LOCK_PROBE_TIMEOUT_SECONDS)
                assert readable
                assert child.stdout.readline() == "waiting\n"
                locks.close()
                output, errors = child.communicate(timeout=_LOCK_PROBE_TIMEOUT_SECONDS)
                assert child.returncode == 0, errors
                assert output.endswith("acquired\n")
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=_LOCK_PROBE_TIMEOUT_SECONDS)
    assert not target.exists()
