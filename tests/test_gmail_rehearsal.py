"""Offline safety contracts for the pending Gmail rehearsal recorder."""

import json
import sys
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from types import TracebackType
from unittest.mock import patch

import pytest

from fieldkit.util.bounded_process import BoundedProcessBytesResult, BoundedProcessError
from scripts import gmail_rehearsal as recorder
from scripts.gmail_rehearsal_observation import GmailCacheObservation

pytestmark = pytest.mark.unit


@pytest.fixture
def inputs(tmp_path: Path) -> dict[str, object]:
    token = tmp_path / "original-token.json"
    token.write_text('{"token":"synthetic-secret"}', encoding="utf-8")
    token.chmod(0o600)
    cache = tmp_path / "original-cache"
    cache.mkdir()
    (cache / "gmail.db").write_bytes(b"original-cache")
    return {"token": token, "cache": cache}


def invoke(inputs: dict[str, object], executable: Path) -> recorder.GmailRehearsal:
    assert isinstance(inputs["token"], Path)
    assert isinstance(inputs["cache"], Path)
    return recorder.run_gmail_rehearsal(
        executable=executable,
        credential_path=inputs["token"],
        original_cache=inputs["cache"],
        test_account="synthetic@example.com",
        since="2026-09-01",
        max_messages=3,
    )


def payload(**updates: object) -> bytes:
    data = {
        "mode": "since",
        "added": 2,
        "failed": 0,
        "not_found": 0,
        "unresolved": 0,
        "partial": False,
        "retry": {"checkpoint": "synthetic-secret"},
        "body": "private-content",
        "email": "private@example.com",
    }
    data.update(updates)
    return json.dumps(data).encode()


def test_fake_executable_observes_fixed_argv_and_isolation(
    inputs: dict[str, object], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "fake-fieldkit"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "assert sys.argv[1:4] == ['gmail', 'sync', '--db']\n"
        "assert sys.argv[5:] == ['--since', '2026-09-01', '--max-messages', '3', '--json']\n"
        "assert os.environ['PYTHON_DOTENV_DISABLED'] == '1'\n"
        "assert not any(k in os.environ for k in ['AMBIENT_SECRET', 'PYTHONPATH', 'HTTPS_PROXY', 'GOOGLE_APPLICATION_CREDENTIALS'])\n"
        "root = pathlib.Path(os.environ['HOME']).parent\n"
        "assert pathlib.Path.cwd() == root / 'workspace'\n"
        "for key in ['HOME','XDG_CONFIG_HOME','XDG_CACHE_HOME','XDG_DATA_HOME','XDG_STATE_HOME','XDG_RUNTIME_DIR','TMPDIR']:\n"
        " p = pathlib.Path(os.environ[key]); assert p.is_relative_to(root) and p.stat().st_mode & 0o777 == 0o700\n"
        "config = json.loads((root / 'config/fieldkit/config.yaml').read_text())\n"
        "token = pathlib.Path(config['gmail_token'])\n"
        'assert token.read_text() == \'{"token":"synthetic-secret"}\'\n'
        "assert token.stat().st_mode & 0o777 == 0o600\n"
        "assert not pathlib.Path(sys.argv[4]).exists()\n"
        "pathlib.Path(sys.argv[4]).write_bytes(b'isolated-cache')\n"
        "token.write_text('synthetic-refreshed-token')\n"
        f"sys.stdout.write({payload().decode()!r})\n"
        "sys.stderr.write('synthetic-secret private@example.com')\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    for key in ("AMBIENT_SECRET", "PYTHONPATH", "HTTPS_PROXY", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.setenv(key, "must-not-inherit")
    (tmp_path / ".env").write_text("AMBIENT_SECRET=must-not-load", encoding="utf-8")
    observed_paths: list[Path] = []

    def observe(db: Path) -> GmailCacheObservation:
        observed_paths.append(db)
        assert db.read_bytes() == b"isolated-cache"
        return GmailCacheObservation(2, False, None, False, True)

    with patch.object(recorder, "observe_gmail_cache", side_effect=observe):
        result = invoke(inputs, executable)
    assert result.run_complete is True
    assert result.status == "pending" and result.release_approved is False
    assert result.credential_preservation == result.cache_preservation == "unchanged"
    retained = json.dumps(asdict(result))
    assert not any(secret in retained for secret in ("synthetic-secret", "private-content", "@", "checkpoint_key"))
    assert not observed_paths[0].parent.parent.exists()


@pytest.mark.parametrize(
    "maximum,since", [(0, "2026-09-01"), (-1, "2026-09-01"), (True, "2026-09-01"), (1, "2026-02-30"), (1, "20260901")]
)
def test_preflight_never_launches(inputs: dict[str, object], maximum: int, since: str) -> None:
    assert isinstance(inputs["token"], Path)
    with patch.object(recorder, "run_bounded_process_bytes") as run, pytest.raises(ValueError, match=r"positive|date"):
        recorder.run_gmail_rehearsal(
            executable=Path(sys.executable),
            credential_path=inputs["token"],
            test_account="test",
            since=since,
            max_messages=maximum,
        )
    run.assert_not_called()


@pytest.mark.parametrize("reason", ["timeout", "overflow", "cleanup", "start"])
def test_process_failure_is_sanitized(inputs: dict[str, object], reason: str) -> None:
    error = BoundedProcessError(
        "private@example.com synthetic-secret",
        reason="timeout"
        if reason == "timeout"
        else "overflow"
        if reason == "overflow"
        else "cleanup"
        if reason == "cleanup"
        else "start",
    )
    with patch.object(recorder, "run_bounded_process_bytes", side_effect=error):
        result = invoke(inputs, Path(sys.executable))
    assert result.run_complete is False
    assert result.failure == ("process" if reason == "start" else reason)
    assert "synthetic-secret" not in repr(result)


@pytest.mark.parametrize(
    "returncode,stdout,expected",
    [
        (2, payload(), "nonzero"),
        (0, b"invalid synthetic-secret", "json"),
        (0, payload(partial=True, failed=1, unresolved=1), "partial"),
        (0, payload(unresolved=1), "json"),
        (0, payload(added=True), "json"),
        (0, payload(added=4), "json"),
        (0, b'{"mode":"since","mode":"since"}', "json"),
    ],
)
def test_failed_sync_never_passes(inputs: dict[str, object], returncode: int, stdout: bytes, expected: str) -> None:
    with patch.object(
        recorder,
        "run_bounded_process_bytes",
        return_value=BoundedProcessBytesResult(returncode, stdout, b"synthetic-secret"),
    ):
        result = invoke(inputs, Path(sys.executable))
    assert result.run_complete is False
    assert result.failure == expected
    assert result.release_approved is False


def test_unknown_cache_preservation_and_observation_failure_remain_pending(inputs: dict[str, object]) -> None:
    assert isinstance(inputs["token"], Path)
    with (
        patch.object(recorder, "run_bounded_process_bytes", return_value=BoundedProcessBytesResult(0, payload(), b"")),
        patch.object(recorder, "observe_gmail_cache", side_effect=ValueError("synthetic-secret")),
    ):
        result = recorder.run_gmail_rehearsal(
            executable=Path(sys.executable),
            credential_path=inputs["token"],
            test_account="test",
            since="2026-09-01",
            max_messages=3,
        )
    assert result.run_complete is False
    assert result.cache_preservation == "unverifiable"
    assert result.failure == "observation"


@pytest.mark.parametrize("unsafe", ["mode", "symlink", "large", "parent-symlink"])
def test_unsafe_credential_rejected(inputs: dict[str, object], tmp_path: Path, unsafe: str) -> None:
    token = inputs["token"]
    assert isinstance(token, Path)
    if unsafe == "mode":
        token.chmod(0o644)
    elif unsafe == "large":
        token.write_bytes(b"x" * (recorder.TOKEN_LIMIT_BYTES + 1))
    elif unsafe == "symlink":
        link = tmp_path / "alias.json"
        link.symlink_to(token)
        inputs["token"] = link
    else:
        link = tmp_path / "alias-directory"
        link.symlink_to(tmp_path, target_is_directory=True)
        inputs["token"] = link / token.name
    with pytest.raises(ValueError, match="credential"):
        invoke(inputs, Path(sys.executable))


@pytest.mark.parametrize("changed", ["token", "cache"])
def test_detects_original_mutation(inputs: dict[str, object], changed: str) -> None:
    def run(*args: object, **kwargs: object) -> BoundedProcessBytesResult:
        target = inputs[changed]
        assert isinstance(target, Path)
        (target if changed == "token" else target / "new-sidecar").write_bytes(b"changed")
        return BoundedProcessBytesResult(0, payload(), b"")

    with (
        patch.object(recorder, "run_bounded_process_bytes", side_effect=run),
        patch.object(recorder, "observe_gmail_cache", return_value=GmailCacheObservation(2, False, None, False, True)),
    ):
        result = invoke(inputs, Path(sys.executable))
    assert result.run_complete is False
    assert (result.credential_preservation if changed == "token" else result.cache_preservation) == "changed"


@pytest.mark.parametrize(
    "observed",
    [
        GmailCacheObservation(0, False, None, False, True),
        GmailCacheObservation(4, False, None, False, True),
        GmailCacheObservation(1, False, None, False, True),
        GmailCacheObservation(2, True, None, False, True),
        GmailCacheObservation(2, False, None, True, True),
    ],
)
def test_inconsistent_cache_observation_never_completes(
    inputs: dict[str, object], observed: GmailCacheObservation
) -> None:
    with (
        patch.object(recorder, "run_bounded_process_bytes", return_value=BoundedProcessBytesResult(0, payload(), b"")),
        patch.object(recorder, "observe_gmail_cache", return_value=observed),
    ):
        result = invoke(inputs, Path(sys.executable))
    assert result.run_complete is False
    assert result.failure == "observation"


@pytest.mark.parametrize("kind", ["timeout", "overflow", "nonzero"])
def test_real_fake_child_failure_is_bounded(
    inputs: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    executable = tmp_path / "fake-failing-fieldkit"
    programs = {
        "timeout": "import time; time.sleep(10)",
        "overflow": "import sys; sys.stdout.write('x' * 100000)",
        "nonzero": "import sys; sys.stderr.write('synthetic-secret'); sys.exit(2)",
    }
    executable.write_text(f"#!{sys.executable}\n{programs[kind]}\n", encoding="utf-8")
    executable.chmod(0o700)
    if kind == "timeout":
        monkeypatch.setattr(recorder, "SYNC_TIMEOUT_SECONDS", 0.05)
    result = invoke(inputs, executable)
    assert result.run_complete is False
    assert result.failure == kind
    assert "synthetic-secret" not in repr(result)


def test_inventory_bound_is_explicitly_unverifiable(inputs: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recorder, "INVENTORY_BYTE_LIMIT", 1)
    with (
        patch.object(
            recorder, "run_bounded_process_bytes", return_value=BoundedProcessBytesResult(0, payload(), b"")
        ) as run,
        patch.object(recorder, "observe_gmail_cache", return_value=GmailCacheObservation(2, False, None, False, True)),
    ):
        result = invoke(inputs, Path(sys.executable))
    assert result.run_complete is False
    assert result.cache_preservation == "unverifiable"
    assert run.call_args.kwargs["timeout"] == 120.0
    assert run.call_args.kwargs["cleanup_timeout"] == 5.0
    assert run.call_args.kwargs["stdout_limit"] == run.call_args.kwargs["stderr_limit"] == 64 * 1024


@pytest.mark.parametrize("stage", ["creation", "setup", "teardown"])
def test_filesystem_failures_are_sanitized_and_preservation_is_checked(
    inputs: dict[str, object],
    stage: str,
) -> None:
    sentinel = "/private/sentinel/token-and-cache-path"

    class TeardownErrorDirectory(TemporaryDirectory[str]):
        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            super().__exit__(exc_type, exc_value, traceback)
            cache = inputs["cache"]
            assert isinstance(cache, Path)
            (cache / "changed-during-teardown").write_bytes(b"changed")
            raise OSError(sentinel)

    with ExitStack() as stack:
        if stage == "creation":
            stack.enter_context(patch.object(recorder, "TemporaryDirectory", side_effect=OSError(sentinel)))
        elif stage == "setup":
            stack.enter_context(patch.object(Path, "write_text", side_effect=OSError(sentinel)))
        else:
            stack.enter_context(patch.object(recorder, "TemporaryDirectory", TeardownErrorDirectory))
        credential_reads = stack.enter_context(
            patch.object(recorder, "_credential_snapshot", wraps=recorder._credential_snapshot)
        )
        cache_reads = stack.enter_context(patch.object(recorder, "_cache_inventory", wraps=recorder._cache_inventory))
        run = stack.enter_context(
            patch.object(
                recorder, "run_bounded_process_bytes", return_value=BoundedProcessBytesResult(0, payload(), b"")
            )
        )
        stack.enter_context(
            patch.object(
                recorder, "observe_gmail_cache", return_value=GmailCacheObservation(2, False, None, False, True)
            )
        )
        result = invoke(inputs, Path(sys.executable))
    assert result.run_complete is False
    assert result.failure == ("teardown" if stage == "teardown" else "setup")
    assert result.status == "pending" and result.release_approved is False
    assert result.credential_preservation == "unchanged"
    assert result.cache_preservation == ("changed" if stage == "teardown" else "unchanged")
    assert credential_reads.call_count == cache_reads.call_count == 2
    assert sentinel not in json.dumps(asdict(result))
    assert run.call_count == (1 if stage == "teardown" else 0)


def test_valid_paused_scan_preserves_larger_scanned_count(inputs: dict[str, object]) -> None:
    with (
        patch.object(recorder, "run_bounded_process_bytes", return_value=BoundedProcessBytesResult(0, payload(), b"")),
        patch.object(recorder, "observe_gmail_cache", return_value=GmailCacheObservation(2, True, 5, False, True)),
    ):
        result = invoke(inputs, Path(sys.executable))
    assert result.run_complete is True
    assert result.failure is None
    assert result.observation == recorder.CacheObservation(2, True, 5, False, True)
    assert result.credential_preservation == result.cache_preservation == "unchanged"
    assert result.status == "pending" and result.release_approved is False
