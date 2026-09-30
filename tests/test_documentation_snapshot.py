"""Fixed-tree documentation verification input tests."""

import io
import os
import selectors
import subprocess
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from scripts import documentation_snapshot
from scripts.documentation_snapshot import _stream_git, materialize_candidate
from scripts.install_git_hooks import _move_no_replace as real_move_no_replace

pytestmark = pytest.mark.unit


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments), cwd=repository, check=True, capture_output=True, text=True, timeout=10
    ).stdout.strip()


def _git_input(repository: Path, content: bytes, *arguments: str) -> str:
    return (
        subprocess.run(("git", *arguments), cwd=repository, check=True, capture_output=True, input=content, timeout=10)
        .stdout.decode()
        .strip()
    )


def _process_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    proc_stat = Path(f"/proc/{pid}/stat")
    return not proc_stat.exists() or proc_stat.read_text(encoding="utf-8").split()[2] != "Z"


def _wait_for_process_exit(pid: int) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and _process_running(pid):
        time.sleep(0.02)
    assert not _process_running(pid)


@pytest.fixture
def candidate(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "source"
    repository.mkdir()
    _git(repository, "init", "-q")
    (repository / "example.md").write_text("committed behavior\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "-c", "user.name=fieldkit test", "-c", "user.email=test@example.com", "commit", "-qm", "candidate")
    return repository, _git(repository, "rev-parse", "HEAD")


def test_snapshot_uses_committed_bytes_without_private_history(candidate: tuple[Path, str], tmp_path: Path) -> None:
    repository, revision = candidate
    (repository / "example.md").write_text("changing source\n", encoding="utf-8")
    destination = tmp_path / "snapshot"

    materialize_candidate(repository, revision, destination)

    assert destination.stat().st_mode & 0o777 == 0o700
    assert (destination / "example.md").read_text(encoding="utf-8") == "committed behavior\n"
    assert not (destination / ".git").exists()


@pytest.mark.parametrize("link_target", ["/private/example", "../outside"])
def test_snapshot_rejects_escaping_symlink(candidate: tuple[Path, str], tmp_path: Path, link_target: str) -> None:
    repository, _ = candidate
    (repository / "escape").symlink_to(link_target)
    _git(repository, "add", "escape")
    _git(repository, "-c", "user.name=fieldkit test", "-c", "user.email=test@example.com", "commit", "-qm", "unsafe")

    with pytest.raises(ValueError, match="unsafe symlink"):
        materialize_candidate(repository, _git(repository, "rev-parse", "HEAD"), tmp_path / "snapshot")


def test_snapshot_preserves_safe_symlink_and_executable(candidate: tuple[Path, str], tmp_path: Path) -> None:
    repository, _ = candidate
    (repository / "link").symlink_to("example.md")
    (repository / "example.md").chmod(0o755)
    _git(repository, "add", ".")
    _git(repository, "-c", "user.name=fieldkit test", "-c", "user.email=test@example.com", "commit", "-qm", "modes")
    destination = tmp_path / "snapshot"

    materialize_candidate(repository, _git(repository, "rev-parse", "HEAD"), destination)

    assert (destination / "link").is_symlink()
    assert (destination / "link").readlink() == Path("example.md")
    assert (destination / "example.md").stat().st_mode & 0o777 == 0o755


def test_snapshot_rejects_tree_object_identity(candidate: tuple[Path, str], tmp_path: Path) -> None:
    repository, revision = candidate

    with pytest.raises(ValueError, match="requires a commit object"):
        materialize_candidate(repository, _git(repository, "rev-parse", f"{revision}^{{tree}}"), tmp_path / "snapshot")


def test_snapshot_preserves_existing_destination(candidate: tuple[Path, str], tmp_path: Path) -> None:
    repository, revision = candidate
    destination = tmp_path / "snapshot"
    destination.mkdir()
    sentinel = destination / "owned.md"
    sentinel.write_text("preserve\n", encoding="utf-8")

    with pytest.raises(ValueError, match="destination must be new"):
        materialize_candidate(repository, revision, destination)

    assert sentinel.read_text(encoding="utf-8") == "preserve\n"


def test_snapshot_rejects_export_ignored_missing_inputs(candidate: tuple[Path, str], tmp_path: Path) -> None:
    repository, _ = candidate
    (repository / ".gitattributes").write_text("example.md export-ignore\n", encoding="utf-8")
    _git(repository, "add", ".gitattributes")
    _git(
        repository, "-c", "user.name=fieldkit test", "-c", "user.email=test@example.com", "commit", "-qm", "attributes"
    )

    with pytest.raises(ValueError, match="does not match committed tree"):
        materialize_candidate(repository, _git(repository, "rev-parse", "HEAD"), tmp_path / "snapshot")


def test_snapshot_ignores_git_replacement_objects(candidate: tuple[Path, str], tmp_path: Path) -> None:
    repository, revision = candidate
    original_blob = _git(repository, "rev-parse", f"{revision}:example.md")
    replacement_blob = _git_input(repository, b"replacement bytes\n", "hash-object", "-w", "--stdin")
    _git(repository, "replace", original_blob, replacement_blob)
    destination = tmp_path / "snapshot"

    materialize_candidate(repository, revision, destination)

    assert (destination / "example.md").read_text(encoding="utf-8") == "committed behavior\n"


@pytest.mark.parametrize("redirect", ["GIT_DIR", "GIT_COMMON_DIR"])
def test_snapshot_rejects_inherited_git_repository_redirect(
    candidate: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, redirect: str
) -> None:
    repository, _ = candidate
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    _git(attacker, "init", "-q")
    (attacker / "attacker.md").write_text("wrong tree\n", encoding="utf-8")
    _git(attacker, "add", ".")
    _git(attacker, "-c", "user.name=fieldkit test", "-c", "user.email=test@example.com", "commit", "-qm", "wrong")
    attacker_revision = _git(attacker, "rev-parse", "HEAD")
    monkeypatch.setenv(redirect, str(attacker / ".git"))

    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        materialize_candidate(repository, attacker_revision, tmp_path / "snapshot")

    assert not (tmp_path / "snapshot").exists()


def test_snapshot_stops_archive_at_hard_byte_limit(
    candidate: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, _ = candidate
    (repository / ".gitattributes").write_text("expanded.txt export-subst\n", encoding="utf-8")
    (repository / "expanded.txt").write_text("$Format:%B$\n", encoding="utf-8")
    _git(repository, "add", ".")
    message = "X" * 100_000
    _git(repository, "-c", "user.name=fieldkit test", "-c", "user.email=test@example.com", "commit", "-qm", message)
    monkeypatch.setattr("scripts.documentation_snapshot._MAX_TREE_BYTES", 1024)
    monkeypatch.setattr("scripts.documentation_snapshot._MAX_ENTRIES", 4)
    destination = tmp_path / "snapshot"

    with pytest.raises(ValueError, match="archive exceeds verification bound"):
        materialize_candidate(repository, _git(repository, "rev-parse", "HEAD"), destination)

    assert not destination.exists()
    assert list(tmp_path.glob(".snapshot.*")) == []


def test_snapshot_keeps_group_owner_unreaped_until_cleanup(
    candidate: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fieldkit.util.bounded_process import terminate_process_group

    repository, revision = candidate
    destination = tmp_path / "snapshot"
    cleaned: list[int] = []

    def cleanup(process: subprocess.Popen[bytes], *, cleanup_timeout: float) -> None:
        assert process.returncode is None
        terminate_process_group(process, cleanup_timeout=cleanup_timeout)
        assert process.returncode == 0
        cleaned.append(process.pid)

    monkeypatch.setattr(documentation_snapshot, "terminate_process_group", cleanup)

    materialize_candidate(repository, revision, destination)

    assert cleaned
    assert (destination / "example.md").read_text(encoding="utf-8") == "committed behavior\n"


def test_snapshot_rejects_empty_symlink_target_without_partial_destination(
    candidate: tuple[Path, str], tmp_path: Path
) -> None:
    repository, _ = candidate
    empty_blob = _git_input(repository, b"", "hash-object", "-w", "--stdin")
    _git(repository, "update-index", "--add", "--cacheinfo", f"120000,{empty_blob},empty-link")
    _git(repository, "-c", "user.name=fieldkit test", "-c", "user.email=test@example.com", "commit", "-qm", "empty")
    destination = tmp_path / "snapshot"

    with pytest.raises(ValueError, match="unsafe symlink"):
        materialize_candidate(repository, _git(repository, "rev-parse", "HEAD"), destination)

    assert not destination.exists()
    assert list(tmp_path.glob(".snapshot.*")) == []


@pytest.mark.parametrize("operation", ["write_bytes", "chmod", "symlink_to"])
def test_snapshot_failure_never_exposes_partial_destination(
    candidate: tuple[Path, str], tmp_path: Path, operation: str
) -> None:
    repository, _ = candidate
    (repository / "link").symlink_to("example.md")
    (repository / "example.md").chmod(0o755)
    _git(repository, "add", ".")
    _git(repository, "-c", "user.name=fieldkit test", "-c", "user.email=test@example.com", "commit", "-qm", "modes")
    destination = tmp_path / "snapshot"

    with (
        patch.object(Path, operation, side_effect=OSError("injected publication failure")),
        pytest.raises(OSError, match="injected publication failure"),
    ):
        materialize_candidate(repository, _git(repository, "rev-parse", "HEAD"), destination)

    assert not destination.exists()
    assert list(tmp_path.glob(".snapshot.*")) == []


def test_stream_timeout_kills_same_group_descendant_after_leader_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    pid_file = tmp_path / "child.pid"
    git = binary_dir / "git"
    git.write_text('#!/bin/sh\nsleep 30 &\nprintf \'%s\' "$!" > "$SNAP_CHILD_PID"\nexit 0\n', encoding="utf-8")
    git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("SNAP_CHILD_PID", str(pid_file))
    monkeypatch.setattr("scripts.documentation_snapshot._GIT_TIMEOUT_SECONDS", 0.1)

    with pytest.raises(ValueError, match="time bound"):
        _stream_git(tmp_path, ("ignored",), io.BytesIO(), max_bytes=100, bound_name="test output")

    child_pid = int(pid_file.read_text(encoding="utf-8"))
    _wait_for_process_exit(child_pid)


def test_stream_timeout_returns_bounded_when_detached_child_holds_pipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    pid_file = tmp_path / "detached.pid"
    git = binary_dir / "git"
    git.write_text(
        "#!/bin/sh\nsetsid sh -c 'sleep 0.4' &\nprintf '%s' \"$!\" > \"$SNAP_CHILD_PID\"\nexit 0\n",
        encoding="utf-8",
    )
    git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("SNAP_CHILD_PID", str(pid_file))
    monkeypatch.setattr("scripts.documentation_snapshot._GIT_TIMEOUT_SECONDS", 0.1)
    started = time.monotonic()

    with pytest.raises(ValueError, match="time bound"):
        _stream_git(tmp_path, ("ignored",), io.BytesIO(), max_bytes=100, bound_name="test output")

    assert time.monotonic() - started < 1
    _wait_for_process_exit(int(pid_file.read_text(encoding="utf-8")))


def test_snapshot_atomic_publication_preserves_raced_destination(candidate: tuple[Path, str], tmp_path: Path) -> None:
    repository, revision = candidate
    destination = tmp_path / "snapshot"

    def race_destination(source: Path, target: Path) -> None:
        target.mkdir()
        real_move_no_replace(source, target)

    with (
        patch("scripts.documentation_snapshot._move_no_replace", side_effect=race_destination),
        pytest.raises(OSError),
    ):
        materialize_candidate(repository, revision, destination)

    assert destination.is_dir()
    assert list(destination.iterdir()) == []


def test_cleanup_wait_is_finite_after_kill_and_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    process = Mock(spec=subprocess.Popen)
    process.pid = 424242
    observed: list[float] = []

    def wait(*, timeout: float | None = None) -> int:
        assert timeout is not None and 0 <= timeout <= 3
        observed.append(timeout)
        raise subprocess.TimeoutExpired("PRIVATE-CLEANUP-SENTINEL", timeout)

    process.wait.side_effect = wait
    monkeypatch.setattr(os, "killpg", lambda *_args: None)

    with pytest.raises(ValueError, match=r"^candidate Git cleanup did not complete$") as error:
        documentation_snapshot._stop_process(process)

    assert observed
    assert "PRIVATE-CLEANUP-SENTINEL" not in str(error.value)
    assert error.value.__suppress_context__


def test_incomplete_cleanup_never_publishes_snapshot(
    candidate: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fieldkit.util.bounded_process import BoundedProcessError, terminate_process_group

    repository, revision = candidate
    destination = tmp_path / "snapshot"

    def incomplete(process: subprocess.Popen[bytes], *, cleanup_timeout: float) -> None:
        terminate_process_group(process, cleanup_timeout=cleanup_timeout)
        raise BoundedProcessError("PRIVATE-CLEANUP-SENTINEL", reason="cleanup")

    monkeypatch.setattr(documentation_snapshot, "terminate_process_group", incomplete, raising=False)

    with pytest.raises(ValueError, match=r"^candidate Git cleanup did not complete$"):
        materialize_candidate(repository, revision, destination)

    assert not destination.exists()
    assert list(tmp_path.glob(".snapshot.*")) == []


@pytest.mark.parametrize("failure", ["allocation", "registration"])
def test_selector_allocation_failure_cleans_child_without_publication(
    candidate: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    repository, revision = candidate
    destination = tmp_path / "snapshot"
    real_stop = documentation_snapshot._stop_process
    real_selector = selectors.DefaultSelector
    cleaned: list[subprocess.Popen[bytes]] = []
    allocated: list[selectors.BaseSelector] = []

    def stop(process: subprocess.Popen[bytes]) -> None:
        real_stop(process)
        cleaned.append(process)

    def fail_registration(*_args: object, **_kwargs: object) -> None:
        raise OSError("PRIVATE-SELECTOR-SENTINEL")

    def unavailable() -> selectors.BaseSelector:
        if failure == "allocation":
            raise OSError("PRIVATE-SELECTOR-SENTINEL")
        selector = real_selector()
        allocated.append(selector)
        monkeypatch.setattr(selector, "register", fail_registration)
        return selector

    monkeypatch.setattr(documentation_snapshot, "_stop_process", stop)
    monkeypatch.setattr(selectors, "DefaultSelector", unavailable)

    try:
        with pytest.raises(ValueError, match=r"^candidate Git output selector is unavailable$") as error:
            materialize_candidate(repository, revision, destination)
        assert error.value.__suppress_context__
    finally:
        assert cleaned, "selector failure skipped owned child cleanup"
        assert all(
            process.returncode is not None and process.stdout is not None and process.stdout.closed
            for process in cleaned
        )
        assert all(selector.get_map() is None for selector in allocated)
        assert not destination.exists()
        assert list(tmp_path.glob(".snapshot.*")) == []
    assert list(tmp_path.glob(".snapshot.*")) == []
