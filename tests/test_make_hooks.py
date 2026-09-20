"""Contract tests for portable linked-worktree-safe ``make hooks`` installation."""

import fcntl
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from pathlib import Path, PurePosixPath

import pytest

from scripts import hook_install_runtime, install_git_hooks

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"
INSTALL_SCRIPT = ROOT / "scripts" / "install_git_hooks.py"
RUNTIME_SCRIPT = ROOT / "scripts" / "hook_install_runtime.py"
_TIMEOUT_SECONDS = 30


def _isolated_env(home: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_SYSTEM",
    ):
        env.pop(key, None)
    for key in tuple(env):
        if key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            env.pop(key)
    env.pop("GIT_CONFIG_COUNT", None)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    if home is not None:
        home.mkdir(exist_ok=True)
        env["HOME"] = str(home)
    return env


def _run(*argv: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=_TIMEOUT_SECONDS,
        env=env or _isolated_env(),
    )


def _make(*arguments: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    installer = cwd / "scripts" / "install_git_hooks.py"
    if not installer.exists():
        installer.parent.mkdir(exist_ok=True)
        shutil.copy2(INSTALL_SCRIPT, installer)
        shutil.copy2(RUNTIME_SCRIPT, installer.parent / "hook_install_runtime.py")
    if env is None:
        env = _isolated_env(cwd / ".test-home")
    return _run(
        "make",
        "-f",
        str(MAKEFILE),
        *arguments,
        "hooks",
        cwd=cwd,
        env=env,
    )


def _repository_with_linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    primary = tmp_path / "primary"
    linked = tmp_path / "linked"
    primary.mkdir()
    assert _run("git", "init", "-q", cwd=primary).returncode == 0
    assert _run("git", "config", "user.email", "developer@example.com", cwd=primary).returncode == 0
    assert _run("git", "config", "user.name", "Test Developer", cwd=primary).returncode == 0
    (primary / "tracked.txt").write_text("fixture\n", encoding="utf-8")
    assert _run("git", "add", "tracked.txt", cwd=primary).returncode == 0
    assert _run("git", "commit", "-qm", "test: seed repository", cwd=primary).returncode == 0
    assert _run("git", "worktree", "add", "-qb", "linked", str(linked), cwd=primary).returncode == 0
    return primary, linked


def _prepare_sources(linked: Path) -> None:
    (linked / "hooks").mkdir()
    (linked / "scripts").mkdir()
    shutil.copy2(ROOT / "hooks" / "pre-push-git-hook", linked / "hooks" / "pre-push-git-hook")
    shutil.copy2(ROOT / "hooks" / "post_commit.py", linked / "hooks" / "post_commit.py")
    (linked / "scripts" / "check_precommit_version.py").write_text("raise SystemExit(0)\n", encoding="utf-8")


def _tool_environment(tmp_path: Path, hooks_dir: Path) -> dict[str, str]:
    tool_bin = tmp_path / "bin"
    tool_bin.mkdir()
    for tool in ("uv", "uvx"):
        stub = tool_bin / tool
        if tool == "uv":
            body = "#!/usr/bin/env python3\nimport os, sys\nos.execv(sys.executable, [sys.executable, *sys.argv[3:]])\n"
        else:
            body = (
                "#!/usr/bin/env python3\n"
                "import os, pathlib, signal, subprocess, sys, time\n"
                "git_dir = os.environ.get('GIT_DIR')\n"
                "hooks = pathlib.Path(git_dir) / 'hooks' if git_dir else pathlib.Path(os.environ['HOOKS_DIR'])\n"
                "if '--hook-type' in sys.argv and 'pre-push' in sys.argv:\n"
                "    destination = hooks / 'pre-push'\n"
                "elif '--hook-type' in sys.argv and 'commit-msg' in sys.argv:\n"
                "    destination = hooks / 'commit-msg'\n"
                "else:\n"
                "    destination = hooks / 'pre-commit'\n"
                "if os.environ.get('INEFFECTIVE') != destination.name:\n"
                "    destination.write_text(destination.name + ' wrapper\\n')\n"
                "    destination.chmod(0o755)\n"
                "failure = os.environ.get('FAILURE')\n"
                "if failure == destination.name:\n"
                "    raise SystemExit(1)\n"
                "if failure == 'timeout' and destination.name == 'pre-commit':\n"
                "    subprocess.Popen([sys.executable, '-c', "
                "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(5)'])\n"
                "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "    time.sleep(5)\n"
            )
        stub.write_text(body, encoding="utf-8")
        stub.chmod(0o755)
    env = _isolated_env(tmp_path / "home")
    env["PATH"] = f"{tool_bin}{os.pathsep}{env['PATH']}"
    env["HOOKS_DIR"] = str(hooks_dir)
    return env


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_hooks_recipe_delegates_to_portable_python() -> None:
    """Verify that hooks recipe delegates to portable python."""
    result = _run("make", "-n", "-f", str(MAKEFILE), "hooks", cwd=ROOT)

    assert result.returncode == 0
    assert "scripts.install_git_hooks" in result.stdout
    assert "--lock-timeout" not in result.stdout
    assert "--command-timeout" not in result.stdout
    assert "--kill-after" not in result.stdout
    assert all(command not in result.stdout for command in ("flock ", "timeout --", "cp -a", "mv -T"))


def test_hooks_recipe_does_not_interpolate_timeout_values_into_shell(tmp_path: Path) -> None:
    """Verify that hooks recipe does not interpolate timeout values into shell."""
    marker = tmp_path / "injected"

    result = _make(f'HOOK_INSTALL_LOCK_TIMEOUT_SECONDS=1"; touch {marker}; #', cwd=tmp_path)

    assert result.returncode != 0
    assert not marker.exists()
    assert "invalid float value" in result.stderr


def test_hook_names_are_derived_from_single_manifest() -> None:
    """Verify that hook names are derived from single manifest."""
    names = tuple(hook.name for hook in install_git_hooks.HOOKS)

    assert names == install_git_hooks.HOOK_NAMES
    assert len(names) == len(set(names))
    assert all((hook.source is None) != (hook.pre_commit_type is None) for hook in install_git_hooks.HOOKS)


def test_empty_hook_manifest_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that empty hook manifest is rejected."""
    monkeypatch.setattr(install_git_hooks, "HOOKS", ())

    with pytest.raises(hook_install_runtime.InstallError, match="hook manifest is empty"):
        install_git_hooks.install(repo_root=tmp_path, lock_timeout=1, command_timeout=2, kill_after=1)


@pytest.mark.parametrize(
    "hook",
    [
        install_git_hooks.HookSpec("invalid", None, None),
        install_git_hooks.HookSpec("invalid", PurePosixPath("source"), "pre-commit"),
    ],
)
def test_ambiguous_hook_manifest_entry_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hook: install_git_hooks.HookSpec
) -> None:
    """Verify that ambiguous hook manifest entry is rejected."""
    monkeypatch.setattr(install_git_hooks, "HOOKS", (hook,))

    with pytest.raises(hook_install_runtime.InstallError, match="must define exactly one source type"):
        install_git_hooks.install(repo_root=tmp_path, lock_timeout=1, command_timeout=2, kill_after=1)


def test_hooks_recipe_ignores_noop_command_overrides(tmp_path: Path) -> None:
    """Verify that hooks recipe ignores noop command overrides."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    env = _tool_environment(tmp_path, hooks_dir)

    result = _make("HOOK_INSTALL_PYTHON=true", "HOOK_INSTALL_SCRIPT=/dev/null", cwd=linked, env=env)

    assert result.returncode == 0
    assert all((hooks_dir / name).is_file() for name in install_git_hooks.HOOK_NAMES)
    assert all(_mode(hooks_dir / name) == 0o755 for name in install_git_hooks.HOOK_NAMES)


def test_hooks_recipe_does_not_evaluate_checkout_path_as_shell(tmp_path: Path) -> None:
    """Verify that hooks recipe does not evaluate checkout path as shell."""
    literal_root = tmp_path / "fieldkit-$(touch${IFS}PWNED)"
    literal_root.mkdir()
    primary, linked = _repository_with_linked_worktree(literal_root)
    _prepare_sources(linked)
    shutil.copy2(INSTALL_SCRIPT, linked / "scripts" / "install_git_hooks.py")
    shutil.copy2(RUNTIME_SCRIPT, linked / "scripts" / "hook_install_runtime.py")
    copied_makefile = linked / "Makefile"
    shutil.copy2(MAKEFILE, copied_makefile)
    hooks_dir = primary / ".git" / "hooks"
    env = _tool_environment(tmp_path, hooks_dir)

    result = _run("make", "-f", str(copied_makefile), "hooks", cwd=linked, env=env)

    assert result.returncode == 0
    assert not (linked / "PWNED").exists()
    assert all((hooks_dir / name).is_file() for name in install_git_hooks.HOOK_NAMES)


def test_hooks_recipe_fails_when_git_cannot_resolve_hooks_directory(tmp_path: Path) -> None:
    """Verify that hooks recipe fails when git cannot resolve hooks directory."""
    environment = _isolated_env(tmp_path / ".test-home")
    environment["GIT_CEILING_DIRECTORIES"] = str(tmp_path.parent)

    result = _make(cwd=tmp_path, env=environment)

    assert result.returncode != 0
    assert "cannot resolve the Git hooks directory" in result.stderr
    assert "hooks installed" not in result.stdout


def test_hooks_recipe_ignores_inherited_git_repository_selectors(tmp_path: Path) -> None:
    """Verify that hooks recipe ignores inherited git repository selectors."""
    intended_root = tmp_path / "intended"
    other_root = tmp_path / "other"
    intended_root.mkdir()
    other_root.mkdir()
    intended_primary, linked = _repository_with_linked_worktree(intended_root)
    other_primary, _ = _repository_with_linked_worktree(other_root)
    _prepare_sources(linked)
    intended_hooks = intended_primary / ".git" / "hooks"
    other_hooks = other_primary / ".git" / "hooks"
    env = _tool_environment(tmp_path, intended_hooks)
    env["GIT_DIR"] = str(other_primary / ".git")
    env["GIT_WORK_TREE"] = str(other_primary)

    result = _make(cwd=linked, env=env)

    assert result.returncode == 0
    assert (intended_hooks / "post-commit").is_file()
    assert not (other_hooks / "post-commit").exists()


def test_hooks_recipe_does_not_allow_git_config_to_hide_hooks_path(tmp_path: Path) -> None:
    """Verify that hooks recipe does not allow git config to hide hooks path."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    assert _run("git", "config", "core.hooksPath", ".githooks", cwd=primary).returncode == 0
    alternate_config = tmp_path / "alternate-git-config"
    alternate_config.write_text("", encoding="utf-8")
    env = _isolated_env(tmp_path / "home")
    env["GIT_CONFIG"] = str(alternate_config)

    result = _make(cwd=linked, env=env)

    assert result.returncode != 0
    assert "core.hooksPath is not supported by pre-commit" in result.stderr


def test_subprocess_environment_removes_all_git_configuration_selectors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that subprocess environment removes all git configuration selectors."""
    selectors = {
        "GIT_CONFIG": "/tmp/config",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_GLOBAL": "/tmp/global",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_PARAMETERS": "'core.hooksPath'='hooks'",
        "GIT_CONFIG_SYSTEM": "/tmp/system",
        "GIT_CONFIG_VALUE_0": "hooks",
    }
    for key, value in selectors.items():
        monkeypatch.setenv(key, value)

    environment = hook_install_runtime._subprocess_environment()

    assert not selectors.keys() & environment.keys()


def test_hooks_recipe_does_not_allow_global_config_override_to_hide_hooks_path(tmp_path: Path) -> None:
    """Verify that hooks recipe does not allow global config override to hide hooks path."""
    _, linked = _repository_with_linked_worktree(tmp_path)
    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    (isolated_home / ".gitconfig").write_text("[core]\n\thooksPath = .githooks\n", encoding="utf-8")
    env = _isolated_env()
    env["HOME"] = str(isolated_home)
    env["GIT_CONFIG_GLOBAL"] = os.devnull

    result = _make(cwd=linked, env=env)

    assert result.returncode != 0
    assert "core.hooksPath is not supported by pre-commit" in result.stderr


def test_hooks_recipe_rejects_hooks_path_from_selected_global_config(tmp_path: Path) -> None:
    """Verify that hooks recipe rejects hooks path from selected global config."""
    _, linked = _repository_with_linked_worktree(tmp_path)
    selected_global = tmp_path / "selected-global-config"
    selected_global.write_text("[core]\n\thooksPath = .selected-hooks\n", encoding="utf-8")
    env = _isolated_env(tmp_path / "home")
    env["GIT_CONFIG_GLOBAL"] = str(selected_global)

    result = _make(cwd=linked, env=env)

    assert result.returncode != 0
    assert "core.hooksPath is not supported by pre-commit" in result.stderr
    assert "hooks installed" not in result.stdout


def test_hooks_directory_supports_git_without_path_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that hooks directory supports git without path format."""
    responses = iter([(1, "", ""), (1, "", ""), (0, "../shared.git", "")])
    arguments: list[list[str]] = []

    def git_output(
        command: list[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        kill_after_seconds: float,
        termination_signals: hook_install_runtime.TerminationSignals | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> tuple[int, str, str]:
        arguments.append(command)
        return next(responses)

    monkeypatch.setattr(hook_install_runtime, "_git_output", git_output)

    hooks_dir = hook_install_runtime._hooks_directory(tmp_path / "checkout", timeout_seconds=1, kill_after_seconds=1)

    assert arguments == [
        ["config", "--get", "core.hooksPath"],
        ["config", "--get", "core.hooksPath"],
        ["rev-parse", "--git-common-dir"],
    ]
    assert hooks_dir == (tmp_path / "shared.git" / "hooks").resolve()


def test_hooks_directory_reports_git_routing_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that hooks directory reports git routing error."""
    monkeypatch.setattr(
        hook_install_runtime,
        "_git_output",
        lambda *args, **kwargs: (128, "", "fatal: bad config line 1 in file"),
    )

    with pytest.raises(hook_install_runtime.InstallError, match="fatal: bad config line 1 in file"):
        hook_install_runtime._hooks_directory(tmp_path, timeout_seconds=1, kill_after_seconds=1)


def test_git_query_timeout_kills_process_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that git query timeout kills process group."""
    tool_bin = tmp_path / "bin"
    tool_bin.mkdir()
    ready = tmp_path / "git-child-ready"
    marker = tmp_path / "surviving-git-child"
    child = (
        "import pathlib,signal,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f"pathlib.Path({str(ready)!r}).write_text('ready');"
        "time.sleep(5);"
        f"pathlib.Path({str(marker)!r}).write_text('survived')"
    )
    git_stub = tool_bin / "git"
    git_stub.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib,signal,subprocess,sys,time\n"
        f"subprocess.Popen([sys.executable,'-c',{child!r}])\n"
        f"ready=pathlib.Path({str(ready)!r})\n"
        "deadline=time.monotonic()+2\n"
        "while not ready.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "time.sleep(5)\n",
        encoding="utf-8",
    )
    git_stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tool_bin}{os.pathsep}{os.environ['PATH']}")

    with pytest.raises(hook_install_runtime.InstallError, match="Git query timed out"):
        hook_install_runtime._hooks_directory(tmp_path, timeout_seconds=0.5, kill_after_seconds=0.1)

    assert ready.exists()
    time.sleep(1.1)
    assert not marker.exists()


@pytest.mark.parametrize(
    ("setting", "value", "message"),
    [
        ("HOOK_INSTALL_LOCK_TIMEOUT_SECONDS", "nan", "lock timeout must be between zero and 3600 seconds"),
        (
            "HOOK_INSTALL_COMMAND_TIMEOUT_SECONDS",
            "inf",
            "command timeout must be greater than zero and at most 3600 seconds",
        ),
        (
            "HOOK_INSTALL_KILL_AFTER_SECONDS",
            "-1",
            "kill-after timeout must be greater than zero and at most 3600 seconds",
        ),
        (
            "HOOK_INSTALL_COMMAND_TIMEOUT_SECONDS",
            "1e308",
            "command timeout must be greater than zero and at most 3600 seconds",
        ),
    ],
)
def test_hooks_recipe_rejects_unbounded_timeouts(tmp_path: Path, setting: str, value: str, message: str) -> None:
    """Verify that hooks recipe rejects unbounded timeouts."""
    result = _make(f"{setting}={value}", cwd=tmp_path)

    assert result.returncode != 0
    assert message in result.stderr
    assert "hooks installed" not in result.stdout


def test_git_query_timeout_reports_cleanup_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that a Git query timeout retains process cleanup diagnostics."""

    class TimedOutProcess:
        pid = 12345

        def communicate(self, timeout: float) -> tuple[str, str]:
            raise subprocess.TimeoutExpired("git", timeout)

    process = TimedOutProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        hook_install_runtime,
        "_terminate_process_group",
        lambda *args, **kwargs: [PermissionError("cannot signal process group")],
    )

    with pytest.raises(hook_install_runtime.InstallError, match="Git query timed out") as exc_info:
        hook_install_runtime._git_output(
            ["rev-parse", "--git-common-dir"],
            cwd=tmp_path,
            timeout_seconds=0.01,
            kill_after_seconds=0.01,
        )

    assert exc_info.value.__notes__ == ["PermissionError: cannot signal process group"]


@pytest.mark.parametrize("configured_path", ["", "   ", "custom-hooks"])
def test_hooks_recipe_rejects_any_configured_hooks_path(tmp_path: Path, configured_path: str) -> None:
    """Verify that hooks recipe rejects any configured hooks path."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    assert _run("git", "config", "core.hooksPath", configured_path, cwd=primary).returncode == 0

    result = _make(cwd=linked)

    assert result.returncode != 0
    assert "core.hooksPath is not supported by pre-commit" in result.stderr
    assert "hooks installed" not in result.stdout


def test_hook_install_times_out_when_shared_lock_is_held(tmp_path: Path) -> None:
    """Verify that hook install times out when shared lock is held."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    lock_path = primary / ".git" / "hooks" / ".fieldkit-install.lock"

    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _make("HOOK_INSTALL_LOCK_TIMEOUT_SECONDS=0", cwd=linked)

    assert result.returncode != 0
    assert "timed out waiting for another hook installation" in result.stderr
    assert "hooks installed" not in result.stdout


def test_hook_install_rejects_symlinked_shared_hooks_directory(tmp_path: Path) -> None:
    """Verify that hook install rejects symlinked shared hooks directory."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    hooks_dir = primary / ".git" / "hooks"
    external_hooks = tmp_path / "external-hooks"
    external_hooks.mkdir()
    sentinel = external_hooks / "pre-push"
    sentinel.write_text("external hook\n", encoding="utf-8")
    shutil.rmtree(hooks_dir)
    hooks_dir.symlink_to(external_hooks, target_is_directory=True)

    result = _make(cwd=linked)

    assert result.returncode != 0
    assert "shared hooks path is not a directory" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "external hook\n"
    assert sorted(path.name for path in external_hooks.iterdir()) == ["pre-push"]


def test_hook_install_revalidates_directory_after_creation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that hook install revalidates directory after creation."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    external_hooks = tmp_path / "external-hooks"
    external_hooks.mkdir()
    shutil.rmtree(hooks_dir)
    env = _tool_environment(tmp_path, hooks_dir)
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setenv("HOOKS_DIR", env["HOOKS_DIR"])
    real_mkdir = Path.mkdir

    def create_then_replace(
        path: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        real_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)
        if path == hooks_dir:
            path.rmdir()
            path.symlink_to(external_hooks, target_is_directory=True)

    monkeypatch.setattr(Path, "mkdir", create_then_replace)

    with pytest.raises(hook_install_runtime.InstallError, match="shared hooks path is not a directory"):
        install_git_hooks.install(repo_root=linked, lock_timeout=1, command_timeout=2, kill_after=1)

    assert not any(external_hooks.iterdir())


@pytest.mark.parametrize("destination_type", ["directory", "fifo"])
def test_hook_install_rejects_special_destination_before_mutation(tmp_path: Path, destination_type: str) -> None:
    """Verify that hook install rejects special destination before mutation."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    hooks_dir = primary / ".git" / "hooks"
    pre_push = hooks_dir / "pre-push"
    pre_push.write_text("existing hook\n", encoding="utf-8")
    special_destination = hooks_dir / "post-commit"
    if destination_type == "directory":
        special_destination.mkdir()
    else:
        os.mkfifo(special_destination)

    result = _make(cwd=linked)

    assert result.returncode != 0
    assert "destination is not a regular file or symlink" in result.stderr
    assert pre_push.read_text(encoding="utf-8") == "existing hook\n"


def test_hook_install_rejects_empty_custom_hook_before_mutation(tmp_path: Path) -> None:
    """Verify that hook install rejects empty custom hook before mutation."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    pre_push = hooks_dir / "pre-push"
    pre_push.write_text("existing hook\n", encoding="utf-8")
    (linked / "hooks" / "pre-push-git-hook").write_bytes(b"")
    env = _tool_environment(tmp_path, hooks_dir)

    result = _make(cwd=linked, env=env)

    assert result.returncode != 0
    assert "custom hook source must be a non-empty regular file" in result.stderr
    assert pre_push.read_text(encoding="utf-8") == "existing hook\n"


def test_hook_install_rejects_source_changed_during_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that hook install rejects source changed during staging."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    pre_push = hooks_dir / "pre-push"
    pre_push.write_text("existing hook\n", encoding="utf-8")
    source = linked / "hooks" / "pre-push-git-hook"
    real_fstat = os.fstat
    source_calls: dict[int, int] = {}

    def mutate_before_second_stat(descriptor: int) -> os.stat_result:
        descriptor_stat = real_fstat(descriptor)
        source_stat = source.stat()
        if (descriptor_stat.st_dev, descriptor_stat.st_ino) != (source_stat.st_dev, source_stat.st_ino):
            return descriptor_stat
        source_calls[descriptor] = source_calls.get(descriptor, 0) + 1
        if source_calls[descriptor] == 2:
            content = source.read_bytes()
            source.write_bytes(content[::-1])
            return real_fstat(descriptor)
        return descriptor_stat

    monkeypatch.setattr(os, "fstat", mutate_before_second_stat)

    with pytest.raises(hook_install_runtime.InstallError, match="custom hook source changed while being copied"):
        install_git_hooks.install(repo_root=linked, lock_timeout=1, command_timeout=2, kill_after=1)

    assert pre_push.read_text(encoding="utf-8") == "existing hook\n"
    assert not list(hooks_dir.glob(".*.fieldkit.*"))


def test_hook_install_rejects_atomic_source_replacement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that hook install rejects atomic source replacement."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    pre_push = hooks_dir / "pre-push"
    pre_push.write_text("existing hook\n", encoding="utf-8")
    source = linked / "hooks" / "pre-push-git-hook"
    real_lstat = Path.lstat
    injected = False

    def replace_before_path_check(path: Path) -> os.stat_result:
        nonlocal injected
        if path == source and not injected:
            injected = True
            replacement = source.with_suffix(".replacement")
            replacement.write_bytes(source.read_bytes())
            replacement.replace(source)
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", replace_before_path_check)

    with pytest.raises(hook_install_runtime.InstallError, match="custom hook source changed while being copied"):
        install_git_hooks.install(repo_root=linked, lock_timeout=1, command_timeout=2, kill_after=1)

    assert injected
    assert pre_push.read_text(encoding="utf-8") == "existing hook\n"
    assert not list(hooks_dir.glob(".*.fieldkit.*"))


def test_hook_install_rejects_ineffective_package_tool_success(tmp_path: Path) -> None:
    """Verify that hook install rejects ineffective package tool success."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    env = _tool_environment(tmp_path, hooks_dir)
    env["INEFFECTIVE"] = "commit-msg"

    result = _make(cwd=linked, env=env)

    assert result.returncode != 0
    assert "cannot read custom hook source commit-msg" in result.stderr
    assert "hooks installed" not in result.stdout
    assert not (hooks_dir / "commit-msg").exists()


def test_hook_install_does_not_accept_stale_hook_after_ineffective_tool_success(tmp_path: Path) -> None:
    """Verify that hook install does not accept stale hook after ineffective tool success."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    stale_hook = hooks_dir / "commit-msg"
    stale_hook.write_text("stale executable hook\n", encoding="utf-8")
    stale_hook.chmod(0o755)
    env = _tool_environment(tmp_path, hooks_dir)
    env["INEFFECTIVE"] = "commit-msg"

    result = _make(cwd=linked, env=env)

    assert result.returncode != 0
    assert "cannot read custom hook source commit-msg" in result.stderr
    assert "hooks installed" not in result.stdout
    assert stale_hook.read_text(encoding="utf-8") == "stale executable hook\n"
    assert _mode(stale_hook) == 0o755


def test_hook_install_normalizes_wrapper_modes_under_permissive_umask(tmp_path: Path) -> None:
    """Verify that hook install normalizes wrapper modes under permissive umask."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    env = _tool_environment(tmp_path, hooks_dir)
    previous_umask = os.umask(0o002)
    try:
        result = _make(cwd=linked, env=env)
    finally:
        os.umask(previous_umask)

    assert result.returncode == 0
    assert all(_mode(hooks_dir / name) == 0o755 for name in install_git_hooks.HOOK_NAMES)


def test_hook_install_creates_private_hooks_directory_under_open_umask(tmp_path: Path) -> None:
    """Verify that hook install creates private hooks directory under open umask."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    shutil.rmtree(hooks_dir)
    env = _tool_environment(tmp_path, hooks_dir)
    previous_umask = os.umask(0)
    try:
        result = _make(cwd=linked, env=env)
    finally:
        os.umask(previous_umask)

    assert result.returncode == 0
    assert _mode(hooks_dir) == 0o700


def test_hook_install_rejects_group_writable_hooks_directory(tmp_path: Path) -> None:
    """Verify that hook install rejects group writable hooks directory."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    hooks_dir = primary / ".git" / "hooks"
    hooks_dir.chmod(0o775)

    result = _make(cwd=linked)

    assert result.returncode != 0
    assert "shared hooks directory is group/world writable" in result.stderr


def test_hook_install_rejects_oversized_existing_hook(tmp_path: Path) -> None:
    """Verify that snapshot inspection is bounded for existing hooks."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    (hooks_dir / "pre-commit").write_bytes(b"x" * (install_git_hooks.MAX_CUSTOM_HOOK_BYTES + 1))

    result = _make(cwd=linked)

    assert result.returncode != 0
    assert "existing hook exceeds" in result.stderr


def test_path_identity_rejects_oversized_replacement_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that descriptor validation catches a large replacement after lstat."""
    hook = tmp_path / "pre-commit"
    hook.write_text("small\n", encoding="utf-8")
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"x" * (install_git_hooks.MAX_CUSTOM_HOOK_BYTES + 1))
    real_open = os.open

    def replace_then_open(path: Path, flags: int) -> int:
        replacement.replace(path)
        return real_open(path, flags)

    monkeypatch.setattr(os, "open", replace_then_open)

    with pytest.raises(hook_install_runtime.InstallError, match="path changed while being inspected"):
        install_git_hooks._path_identity(hook)


def test_anchored_directory_rechecks_writable_mode(tmp_path: Path) -> None:
    """Verify that anchoring rejects a directory made writable after initial validation."""
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir(mode=0o700)
    install_git_hooks._validate_hooks_directory(hooks_dir)
    hooks_dir.chmod(0o777)

    with (
        pytest.raises(hook_install_runtime.InstallError, match="group/world writable"),
        install_git_hooks._anchored_directory(hooks_dir, hook_install_runtime.TerminationSignals()),
    ):
        pytest.fail("unsafe directory was accepted")


def test_hook_publication_never_removes_the_existing_complete_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that hook publication never removes the existing complete set."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    destinations = [hooks_dir / name for name in install_git_hooks.HOOK_NAMES]
    for destination in destinations:
        destination.write_text(f"existing {destination.name}\n", encoding="utf-8")
        destination.chmod(0o755)
    env = _tool_environment(tmp_path, hooks_dir)
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("HOOKS_DIR", env["HOOKS_DIR"])
    real_exchange = install_git_hooks._exchange_paths

    def assert_complete_during_publication(first: Path, second: Path) -> None:
        real_exchange(first, second)
        if second in destinations:
            assert all(path.is_file() for path in destinations)

    monkeypatch.setattr(install_git_hooks, "_exchange_paths", assert_complete_during_publication)

    install_git_hooks.install(repo_root=linked, lock_timeout=1, command_timeout=2, kill_after=1)

    assert all(path.is_file() for path in destinations)
    assert all(_mode(path) == 0o755 for path in destinations)


def test_hook_install_syncs_staged_files_and_publication_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that hook install syncs staged files and publication directories."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    env = _tool_environment(tmp_path, hooks_dir)
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setenv("HOOKS_DIR", env["HOOKS_DIR"])
    synced_modes: list[int] = []

    def record_sync(descriptor: int) -> None:
        synced_modes.append(os.fstat(descriptor).st_mode)

    monkeypatch.setattr(os, "fsync", record_sync)

    install_git_hooks.install(repo_root=linked, lock_timeout=1, command_timeout=2, kill_after=1)

    regular_file_syncs = [mode for mode in synced_modes if stat.S_ISREG(mode)]
    directory_syncs = [mode for mode in synced_modes if stat.S_ISDIR(mode)]
    assert len(regular_file_syncs) >= len(install_git_hooks.HOOK_NAMES)
    assert len(directory_syncs) >= len(install_git_hooks.HOOK_NAMES)


def test_failure_description_preserves_nested_notes_and_cause() -> None:
    """Verify that failure description preserves nested notes and cause."""
    cause = OSError("disk failure")
    cause.add_note("directory metadata was not durable")
    failure = hook_install_runtime.InstallError("cleanup failed")
    failure.__cause__ = cause

    description = hook_install_runtime._failure_description(failure)

    assert "InstallError: cleanup failed" in description
    assert "OSError: disk failure" in description
    assert "directory metadata was not durable" in description


def test_sync_file_fsyncs_regular_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that sync file fsyncs regular file."""
    target = tmp_path / "hook"
    target.write_text("hook\n", encoding="utf-8")
    synced_modes: list[int] = []

    def record_sync(descriptor: int) -> None:
        synced_modes.append(os.fstat(descriptor).st_mode)

    monkeypatch.setattr(os, "fsync", record_sync)

    install_git_hooks._sync_file(target)

    assert len(synced_modes) == 1
    assert stat.S_ISREG(synced_modes[0])


def test_sync_directory_fsyncs_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that sync directory fsyncs directory."""
    synced_modes: list[int] = []

    def record_sync(descriptor: int) -> None:
        synced_modes.append(os.fstat(descriptor).st_mode)

    monkeypatch.setattr(os, "fsync", record_sync)

    install_git_hooks._sync_directory(tmp_path)

    assert len(synced_modes) == 1
    assert stat.S_ISDIR(synced_modes[0])


@pytest.mark.parametrize("sync_name", ["_sync_file", "_sync_directory"])
def test_sync_helpers_close_descriptor_after_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sync_name: str
) -> None:
    """Verify that sync helpers close descriptor after fsync failure."""
    target = tmp_path / "hook"
    target.write_text("hook\n", encoding="utf-8")
    sync = getattr(install_git_hooks, sync_name)
    sync_target = target if sync_name == "_sync_file" else tmp_path
    real_close = os.close
    closed_descriptors: list[int] = []

    def fail_sync(_descriptor: int) -> None:
        raise OSError("injected fsync failure")

    def record_close(descriptor: int) -> None:
        closed_descriptors.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(os, "fsync", fail_sync)
    monkeypatch.setattr(os, "close", record_close)

    with pytest.raises(OSError, match="injected fsync failure"):
        sync(sync_target)

    assert len(closed_descriptors) == 1


def test_run_bounds_reap_after_sigkill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that run bounds reap after sigkill."""

    class UnreapableProcess:
        pid = 12345

        def wait(self, timeout: float) -> int:
            raise subprocess.TimeoutExpired("uvx", timeout)

    process = UnreapableProcess()
    signals: list[signal.Signals] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(os, "killpg", lambda pid, sent_signal: signals.append(sent_signal))

    with pytest.raises(hook_install_runtime.InstallError, match="command did not exit after termination"):
        hook_install_runtime._run(["uvx"], cwd=tmp_path, timeout_seconds=0.01, kill_after_seconds=0.01)

    assert signals == [signal.SIGTERM, signal.SIGKILL]


def test_run_terminates_process_group_before_propagating_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that run terminates process group before propagating interrupt."""

    class InterruptedProcess:
        pid = 12345
        waits = 0

        def wait(self, timeout: float) -> int:
            self.waits += 1
            if self.waits == 1:
                raise KeyboardInterrupt
            return 0

    process = InterruptedProcess()
    signals: list[signal.Signals] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(os, "killpg", lambda pid, sent_signal: signals.append(sent_signal))

    with pytest.raises(KeyboardInterrupt):
        hook_install_runtime._run(["uvx"], cwd=tmp_path, timeout_seconds=1, kill_after_seconds=0.01)

    assert process.waits == 3
    assert signals == [signal.SIGTERM, signal.SIGKILL]


def test_termination_signal_runs_process_group_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that termination signal runs process group cleanup."""

    class SignaledProcess:
        pid = 12345
        waits = 0

        def wait(self, timeout: float) -> int:
            self.waits += 1
            if self.waits == 1:
                handler = signal.getsignal(signal.SIGTERM)
                assert callable(handler)
                handler(signal.SIGTERM, None)
            return 0

    process = SignaledProcess()
    signals: list[signal.Signals] = []
    previous_handler = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(os, "killpg", lambda pid, sent_signal: signals.append(sent_signal))

    with (
        pytest.raises(hook_install_runtime.InstallInterrupted, match="SIGTERM"),
        hook_install_runtime._termination_signals_as_interrupts() as termination_signals,
    ):
        hook_install_runtime._run(
            ["uvx"],
            cwd=tmp_path,
            timeout_seconds=1,
            kill_after_seconds=0.01,
            termination_signals=termination_signals,
        )

    assert process.waits == 3
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert signal.getsignal(signal.SIGTERM) is previous_handler


def test_signal_during_process_creation_terminates_acquired_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that signal during process creation terminates acquired child."""

    class AcquiredProcess:
        pid = 12345

        def wait(self, timeout: float) -> int:
            return 0

    process = AcquiredProcess()
    signals: list[signal.Signals] = []

    def create_then_signal(*args: object, **kwargs: object) -> AcquiredProcess:
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        return process

    monkeypatch.setattr(subprocess, "Popen", create_then_signal)
    monkeypatch.setattr(os, "killpg", lambda pid, sent_signal: signals.append(sent_signal))

    with (
        pytest.raises(hook_install_runtime.InstallInterrupted, match="SIGTERM"),
        hook_install_runtime._termination_signals_as_interrupts() as termination_signals,
    ):
        hook_install_runtime._run(
            ["uvx"],
            cwd=tmp_path,
            timeout_seconds=1,
            kill_after_seconds=0.1,
            termination_signals=termination_signals,
        )

    assert signals == [signal.SIGTERM, signal.SIGKILL]


def test_main_reports_termination_signal(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify that main reports termination signal."""

    def terminate(**kwargs: object) -> None:
        os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(install_git_hooks, "install", terminate)

    assert install_git_hooks.main([]) == 1
    assert "hooks: interrupted by SIGTERM" in capsys.readouterr().err


def test_main_reports_failure_before_queued_termination(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify that main reports failure before queued termination."""

    def fail(**kwargs: object) -> None:
        error = OSError("injected publication failure")
        error.add_note("termination requested by SIGTERM")
        raise error

    monkeypatch.setattr(install_git_hooks, "install", fail)

    assert install_git_hooks.main([]) == 1
    error_output = capsys.readouterr().err
    assert "hooks: injected publication failure" in error_output
    assert "termination requested by SIGTERM" in error_output


def test_run_kills_and_reaps_after_interrupt_during_termination_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that run kills and reaps after interrupt during termination wait."""

    class TwiceInterruptedProcess:
        pid = 12345
        waits = 0

        def wait(self, timeout: float) -> int:
            self.waits += 1
            if self.waits == 1:
                raise KeyboardInterrupt
            if self.waits == 2:
                raise KeyboardInterrupt
            return 0

    process = TwiceInterruptedProcess()
    signals: list[signal.Signals] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(os, "killpg", lambda pid, sent_signal: signals.append(sent_signal))

    with pytest.raises(KeyboardInterrupt) as exc_info:
        hook_install_runtime._run(["uvx"], cwd=tmp_path, timeout_seconds=1, kill_after_seconds=0.01)

    assert process.waits == 3
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert exc_info.value.__notes__ == ["KeyboardInterrupt"]


def test_run_retries_final_reap_after_interrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that run retries final reap after interrupt."""

    class FinalReapInterruptedProcess:
        pid = 12345
        waits = 0

        def wait(self, timeout: float) -> int:
            self.waits += 1
            if self.waits == 1:
                raise KeyboardInterrupt
            if self.waits == 2:
                raise subprocess.TimeoutExpired("uvx", timeout)
            if self.waits == 3:
                raise KeyboardInterrupt
            return 0

    process = FinalReapInterruptedProcess()
    signals: list[signal.Signals] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(os, "killpg", lambda pid, sent_signal: signals.append(sent_signal))

    with pytest.raises(KeyboardInterrupt) as exc_info:
        hook_install_runtime._run(["uvx"], cwd=tmp_path, timeout_seconds=1, kill_after_seconds=0.1)

    assert process.waits == 4
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert exc_info.value.__notes__ == ["KeyboardInterrupt"]


def test_run_kills_term_ignoring_child_after_leader_exits(tmp_path: Path) -> None:
    """Verify that run kills term ignoring child after leader exits."""
    ready = tmp_path / "child-ready"
    marker = tmp_path / "surviving-child"
    child = (
        "import pathlib,signal,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f"pathlib.Path({str(ready)!r}).write_text('ready');"
        "time.sleep(1);"
        f"pathlib.Path({str(marker)!r}).write_text('survived')"
    )
    leader = (
        "import pathlib,subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{child!r}]);"
        f"ready=pathlib.Path({str(ready)!r});"
        "deadline=time.monotonic()+2;"
        'exec("while not ready.exists() and time.monotonic() < deadline:\\n time.sleep(0.01)");'
        "time.sleep(5)"
    )

    with pytest.raises(hook_install_runtime.InstallError, match="command timed out"):
        hook_install_runtime._run(
            [sys.executable, "-c", leader],
            cwd=tmp_path,
            timeout_seconds=0.5,
            kill_after_seconds=0.1,
        )

    assert ready.exists()
    time.sleep(1.1)
    assert not marker.exists()


def test_temporary_allocation_cleans_path_when_descriptor_close_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that temporary allocation cleans path when descriptor close is interrupted."""
    real_close = os.close
    close_calls = 0

    def interrupt_first_close(descriptor: int) -> None:
        nonlocal close_calls
        close_calls += 1
        if close_calls == 1:
            raise KeyboardInterrupt
        real_close(descriptor)

    monkeypatch.setattr(os, "close", interrupt_first_close)

    cleanup_errors: list[BaseException] = []
    with pytest.raises(KeyboardInterrupt), ExitStack() as cleanup:
        install_git_hooks._temporary_path(
            tmp_path,
            "pre-push",
            cleanup,
            set(),
            cleanup_errors,
            hook_install_runtime.TerminationSignals(),
        )

    assert close_calls == 2
    assert cleanup_errors == []
    assert not list(tmp_path.iterdir())


def test_temporary_file_signal_is_delivered_after_cleanup_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that temporary file signal is delivered after cleanup registration."""
    real_mkstemp = tempfile.mkstemp
    allocated: list[Path] = []

    def allocate_then_signal(*, prefix: str, dir: Path) -> tuple[int, str]:
        descriptor, raw_path = real_mkstemp(prefix=prefix, dir=dir)
        allocated.append(Path(raw_path))
        os.kill(os.getpid(), signal.SIGTERM)
        return descriptor, raw_path

    monkeypatch.setattr(tempfile, "mkstemp", allocate_then_signal)
    cleanup_errors: list[BaseException] = []
    cleanup = ExitStack()

    with (
        pytest.raises(hook_install_runtime.InstallInterrupted, match="SIGTERM"),
        hook_install_runtime._termination_signals_as_interrupts() as termination_signals,
    ):
        install_git_hooks._temporary_path(
            tmp_path,
            "pre-push",
            cleanup,
            set(),
            cleanup_errors,
            termination_signals,
        )

    cleanup.close()
    assert cleanup_errors == []
    assert len(allocated) == 1
    assert not allocated[0].exists()


def test_staging_directory_signal_is_delivered_after_cleanup_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that staging directory signal is delivered after cleanup registration."""
    real_mkdtemp = tempfile.mkdtemp
    allocated: list[Path] = []

    def allocate_then_signal(*, prefix: str, dir: Path) -> str:
        path = Path(real_mkdtemp(prefix=prefix, dir=dir))
        allocated.append(path)
        os.kill(os.getpid(), signal.SIGTERM)
        return str(path)

    monkeypatch.setattr(tempfile, "mkdtemp", allocate_then_signal)
    cleanup_errors: list[BaseException] = []
    cleanup = ExitStack()

    with (
        pytest.raises(hook_install_runtime.InstallInterrupted, match="SIGTERM"),
        hook_install_runtime._termination_signals_as_interrupts() as termination_signals,
    ):
        install_git_hooks._temporary_directory(tmp_path, cleanup, cleanup_errors, termination_signals)

    cleanup.close()
    assert cleanup_errors == []
    assert len(allocated) == 1
    assert not allocated[0].exists()


def test_staging_directory_sigint_is_delivered_after_cleanup_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that staging directory sigint is delivered after cleanup registration."""
    real_mkdtemp = tempfile.mkdtemp
    allocated: list[Path] = []

    def allocate_then_signal(*, prefix: str, dir: Path) -> str:
        path = Path(real_mkdtemp(prefix=prefix, dir=dir))
        allocated.append(path)
        os.kill(os.getpid(), signal.SIGINT)
        return str(path)

    monkeypatch.setattr(tempfile, "mkdtemp", allocate_then_signal)
    cleanup_errors: list[BaseException] = []
    cleanup = ExitStack()

    with (
        pytest.raises(hook_install_runtime.InstallInterrupted, match="SIGINT"),
        hook_install_runtime._termination_signals_as_interrupts() as termination_signals,
    ):
        install_git_hooks._temporary_directory(tmp_path, cleanup, cleanup_errors, termination_signals)

    cleanup.close()
    assert cleanup_errors == []
    assert len(allocated) == 1
    assert not allocated[0].exists()


def test_large_custom_hook_is_rejected_before_reading(tmp_path: Path) -> None:
    """Verify that large custom hook is rejected before reading."""
    source = tmp_path / "oversized-hook"
    source.write_bytes(b"x" * (install_git_hooks.MAX_CUSTOM_HOOK_BYTES + 1))
    cleanup_errors: list[BaseException] = []
    cleanup = ExitStack()

    with pytest.raises(hook_install_runtime.InstallError, match="custom hook source exceeds"):
        install_git_hooks._stage(
            source,
            tmp_path,
            "pre-push",
            cleanup,
            set(),
            cleanup_errors,
            hook_install_runtime.TerminationSignals(),
        )

    cleanup.close()
    assert cleanup_errors == []
    assert not list(tmp_path.glob(".pre-push.fieldkit.*"))


def test_partial_staging_failure_cleans_first_temporary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that partial staging failure cleans first temporary."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    real_temporary_path = install_git_hooks._temporary_path
    allocations = 0

    def fail_second_allocation(
        hooks_directory: Path,
        prefix: str,
        cleanup: ExitStack,
        preserved: set[Path],
        errors: list[BaseException],
        termination_signals: hook_install_runtime.TerminationSignals,
    ) -> Path:
        nonlocal allocations
        allocations += 1
        if allocations == 2:
            raise OSError("injected staging failure")
        return real_temporary_path(hooks_directory, prefix, cleanup, preserved, errors, termination_signals)

    monkeypatch.setattr(install_git_hooks, "_temporary_path", fail_second_allocation)

    with pytest.raises(OSError, match="injected staging failure"):
        install_git_hooks.install(repo_root=linked, lock_timeout=1, command_timeout=2, kill_after=1)

    assert not list(hooks_dir.glob(".*.fieldkit.*"))


def test_partial_snapshot_failure_cleans_prior_backups(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that partial snapshot failure cleans prior backups."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    for name in ("pre-commit", "pre-push"):
        (hooks_dir / name).write_text(f"existing {name}\n", encoding="utf-8")
    env = _tool_environment(tmp_path, hooks_dir)
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("HOOKS_DIR", env["HOOKS_DIR"])
    real_snapshot = install_git_hooks._snapshot
    snapshots = 0

    def fail_second_snapshot(
        destination: Path,
        hooks_directory: Path,
        cleanup: ExitStack,
        preserved: set[Path],
        cleanup_errors: list[BaseException],
        termination_signals: hook_install_runtime.TerminationSignals,
    ) -> install_git_hooks.Snapshot:
        nonlocal snapshots
        snapshots += 1
        if snapshots == 2:
            raise OSError("injected snapshot failure")
        return real_snapshot(destination, hooks_directory, cleanup, preserved, cleanup_errors, termination_signals)

    monkeypatch.setattr(install_git_hooks, "_snapshot", fail_second_snapshot)

    with pytest.raises(OSError, match="injected snapshot failure"):
        install_git_hooks.install(repo_root=linked, lock_timeout=1, command_timeout=2, kill_after=1)

    assert (hooks_dir / "pre-commit").read_text(encoding="utf-8") == "existing pre-commit\n"
    assert (hooks_dir / "pre-push").read_text(encoding="utf-8") == "existing pre-push\n"
    assert not list(hooks_dir.glob(".*.fieldkit.*"))


def test_snapshot_rejects_destination_replaced_by_fifo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that snapshot rejects destination replaced by fifo."""
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    pre_commit = hooks_dir / "pre-commit"
    pre_commit.write_text("existing pre-commit\n", encoding="utf-8")
    real_open = os.open
    injected = False
    destination_opens = 0

    def replace_with_fifo_before_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal destination_opens, injected
        if dir_fd is None and Path(path) == pre_commit:
            destination_opens += 1
        if destination_opens == 2 and not injected:
            injected = True
            pre_commit.unlink()
            os.mkfifo(pre_commit)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", replace_with_fifo_before_open)

    with (
        ExitStack() as cleanup,
        pytest.raises(hook_install_runtime.InstallError, match="destination changed while being captured"),
    ):
        install_git_hooks._snapshot(
            pre_commit,
            hooks_dir,
            cleanup,
            set(),
            [],
            hook_install_runtime.TerminationSignals(),
        )

    assert injected
    assert stat.S_ISFIFO(pre_commit.lstat().st_mode)
    assert not list(hooks_dir.glob(".*.fieldkit.*"))


def test_snapshot_content_signature_distinguishes_regular_file_from_symlink() -> None:
    """Verify that snapshot content signature distinguishes regular file from symlink."""
    digest = b"same digest"
    regular: install_git_hooks.PathIdentity = (1, 2, 11, 3, stat.S_IFREG, 0o777, digest)
    symlink: install_git_hooks.PathIdentity = (4, 5, 11, 6, stat.S_IFLNK, 0o777, digest)

    assert install_git_hooks._content_signature(regular) != install_git_hooks._content_signature(symlink)


@pytest.mark.parametrize("failure", ["commit-msg", "timeout"])
def test_hook_install_failure_restores_complete_hook_set(tmp_path: Path, failure: str) -> None:
    """Verify that hook install failure restores complete hook set."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    expected: dict[str, tuple[bytes, int]] = {}
    for index, name in enumerate(install_git_hooks.HOOK_NAMES):
        destination = hooks_dir / name
        destination.write_text(f"existing {name}\n", encoding="utf-8")
        destination.chmod(0o700 + index)
        expected[name] = (destination.read_bytes(), _mode(destination))
    env = _tool_environment(tmp_path, hooks_dir)
    env["FAILURE"] = failure
    arguments = (
        (
            "HOOK_INSTALL_COMMAND_TIMEOUT_SECONDS=0.1",
            "HOOK_INSTALL_KILL_AFTER_SECONDS=0.1",
        )
        if failure == "timeout"
        else ()
    )

    result = _make(*arguments, cwd=linked, env=env)

    assert result.returncode != 0
    for name, (content, mode) in expected.items():
        destination = hooks_dir / name
        assert destination.read_bytes() == content
        assert _mode(destination) == mode
    assert not list(hooks_dir.glob(".*.fieldkit.*"))
    with (hooks_dir / ".fieldkit-install.lock").open("a+b") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_rollback_owns_temporary_before_descriptor_close_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify rollback remains complete when closing its temporary descriptor fails."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    env = _tool_environment(tmp_path, hooks_dir)
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setenv("HOOKS_DIR", env["HOOKS_DIR"])
    real_publish = install_git_hooks._publish_hook
    real_mkstemp = tempfile.mkstemp
    real_close = os.close
    rollback_descriptor: int | None = None
    close_failed = False

    def fail_post_commit(
        staged: Path,
        destination: Path,
        snapshot: install_git_hooks.Snapshot,
        preserved: set[Path],
    ) -> install_git_hooks.PathIdentity:
        if destination.name == "post-commit":
            raise OSError("injected publication failure")
        return real_publish(staged, destination, snapshot, preserved)

    def record_rollback_temporary(*, prefix: str, dir: Path) -> tuple[int, str]:
        nonlocal rollback_descriptor
        descriptor, path = real_mkstemp(prefix=prefix, dir=dir)
        if ".commit-msg.rollback.fieldkit." in path:
            rollback_descriptor = descriptor
        return descriptor, path

    def fail_rollback_close(descriptor: int) -> None:
        nonlocal close_failed
        if descriptor == rollback_descriptor and not close_failed:
            close_failed = True
            raise OSError("injected rollback close failure")
        real_close(descriptor)

    monkeypatch.setattr(install_git_hooks, "_publish_hook", fail_post_commit)
    monkeypatch.setattr(tempfile, "mkstemp", record_rollback_temporary)
    monkeypatch.setattr(os, "close", fail_rollback_close)

    with pytest.raises(hook_install_runtime.InstallError, match="injected rollback close failure"):
        install_git_hooks.install(repo_root=linked, lock_timeout=1, command_timeout=2, kill_after=1)

    assert close_failed
    assert all(not (hooks_dir / name).exists() for name in install_git_hooks.HOOK_NAMES)
    assert not list(hooks_dir.glob(".*.fieldkit.*"))


def test_signal_after_publication_failure_is_deferred_until_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that signal after publication failure is deferred until rollback."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    expected: dict[str, bytes] = {}
    for name in install_git_hooks.HOOK_NAMES:
        destination = hooks_dir / name
        destination.write_text(f"existing {name}\n", encoding="utf-8")
        expected[name] = destination.read_bytes()
    env = _tool_environment(tmp_path, hooks_dir)
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setenv("HOOKS_DIR", env["HOOKS_DIR"])
    real_publish = install_git_hooks._publish_hook

    def fail_and_signal(
        staged: Path,
        destination: Path,
        snapshot: install_git_hooks.Snapshot,
        preserved: set[Path],
    ) -> install_git_hooks.PathIdentity:
        if destination.name == "commit-msg":
            os.kill(os.getpid(), signal.SIGTERM)
            raise OSError("injected publication failure")
        return real_publish(staged, destination, snapshot, preserved)

    monkeypatch.setattr(install_git_hooks, "_publish_hook", fail_and_signal)

    with (
        pytest.raises(OSError, match="injected publication failure") as exc_info,
        hook_install_runtime._termination_signals_as_interrupts() as termination_signals,
    ):
        install_git_hooks.install(
            repo_root=linked,
            lock_timeout=1,
            command_timeout=2,
            kill_after=1,
            termination_signals=termination_signals,
        )

    assert exc_info.value.__notes__ == ["termination requested by SIGTERM"]
    assert all((hooks_dir / name).read_bytes() == content for name, content in expected.items())
    assert not list(hooks_dir.glob(".*.fieldkit.*"))


def test_signal_during_final_cleanup_does_not_mask_installation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that signal during final cleanup does not mask installation failure."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    env = _tool_environment(tmp_path, hooks_dir)
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setenv("HOOKS_DIR", env["HOOKS_DIR"])
    real_publish = install_git_hooks._publish_hook
    real_discard = install_git_hooks.Snapshot.discard
    signaled = False

    def fail_commit_msg_publication(
        staged: Path,
        destination: Path,
        snapshot: install_git_hooks.Snapshot,
        preserved: set[Path],
    ) -> install_git_hooks.PathIdentity:
        if destination.name == "commit-msg":
            raise OSError("injected publication failure")
        return real_publish(staged, destination, snapshot, preserved)

    def signal_during_first_discard(snapshot: install_git_hooks.Snapshot) -> None:
        nonlocal signaled
        if not signaled:
            signaled = True
            os.kill(os.getpid(), signal.SIGTERM)
        real_discard(snapshot)

    monkeypatch.setattr(install_git_hooks, "_publish_hook", fail_commit_msg_publication)
    monkeypatch.setattr(install_git_hooks.Snapshot, "discard", signal_during_first_discard)

    with (
        pytest.raises(OSError, match="injected publication failure") as exc_info,
        hook_install_runtime._termination_signals_as_interrupts() as termination_signals,
    ):
        install_git_hooks.install(
            repo_root=linked,
            lock_timeout=1,
            command_timeout=2,
            kill_after=1,
            termination_signals=termination_signals,
        )

    assert signaled
    assert exc_info.value.__notes__ == ["termination requested by SIGTERM"]
    assert not list(hooks_dir.glob(".*.fieldkit.*"))


def test_signal_between_failure_capture_and_cleanup_does_not_mask_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that signal between failure capture and cleanup does not mask failure."""

    class SignalBeforeNextCriticalSection(hook_install_runtime.TerminationSignals):
        armed = False

        @contextmanager
        def defer(self) -> Iterator[None]:
            if self.armed:
                self.armed = False
                self.handle(signal.SIGTERM, None)
            with super().defer():
                yield

    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    env = _tool_environment(tmp_path, hooks_dir)
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setenv("HOOKS_DIR", env["HOOKS_DIR"])
    signal_state = SignalBeforeNextCriticalSection()
    real_publish = install_git_hooks._publish_hook

    def fail_and_arm_signal(
        staged: Path,
        destination: Path,
        snapshot: install_git_hooks.Snapshot,
        preserved: set[Path],
    ) -> install_git_hooks.PathIdentity:
        if destination.name == "commit-msg":
            signal_state.armed = True
            raise OSError("injected publication failure")
        return real_publish(staged, destination, snapshot, preserved)

    monkeypatch.setattr(install_git_hooks, "_publish_hook", fail_and_arm_signal)

    with pytest.raises(OSError, match="injected publication failure") as exc_info:
        install_git_hooks.install(
            repo_root=linked,
            lock_timeout=1,
            command_timeout=2,
            kill_after=1,
            termination_signals=signal_state,
        )

    assert exc_info.value.__notes__ == ["termination requested by SIGTERM"]
    assert not list(hooks_dir.glob(".*.fieldkit.*"))


def test_recorded_signal_is_attached_to_unrelated_failure() -> None:
    """Verify that recorded signal is attached to unrelated failure."""
    with (
        pytest.raises(hook_install_runtime.InstallError, match="unrelated failure") as exc_info,
        hook_install_runtime._termination_signals_as_interrupts() as termination_signals,
    ):
        termination_signals.handle(signal.SIGTERM, None)
        raise hook_install_runtime.InstallError("unrelated failure")

    assert exc_info.value.__notes__ == ["termination requested by SIGTERM"]


def test_second_publication_failure_restores_directory_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that second publication failure restores directory symlink."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    legacy_dir = tmp_path / "legacy-pre-push"
    legacy_dir.mkdir()
    pre_push = hooks_dir / "pre-push"
    pre_push.symlink_to(legacy_dir)
    env = _tool_environment(tmp_path, hooks_dir)
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("HOOKS_DIR", env["HOOKS_DIR"])
    real_publish = install_git_hooks._publish_hook

    def fail_after_second_publication(
        staged: Path,
        destination: Path,
        snapshot: install_git_hooks.Snapshot,
        preserved: set[Path],
    ) -> install_git_hooks.PathIdentity:
        if destination.name == "commit-msg":
            raise OSError("injected second publication failure")
        return real_publish(staged, destination, snapshot, preserved)

    monkeypatch.setattr(install_git_hooks, "_publish_hook", fail_after_second_publication)

    with pytest.raises(OSError, match="injected second publication failure"):
        install_git_hooks.install(repo_root=linked, lock_timeout=1, command_timeout=2, kill_after=1)

    assert pre_push.is_symlink()
    assert pre_push.resolve() == legacy_dir
    assert not any(legacy_dir.iterdir())


def test_rollback_preserves_concurrent_external_hook_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that rollback preserves concurrent external hook update."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    pre_commit = hooks_dir / "pre-commit"
    pre_commit.write_text("existing pre-commit\n", encoding="utf-8")
    env = _tool_environment(tmp_path, hooks_dir)
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setenv("HOOKS_DIR", env["HOOKS_DIR"])
    real_publish = install_git_hooks._publish_hook

    def update_then_fail(
        staged: Path,
        destination: Path,
        snapshot: install_git_hooks.Snapshot,
        preserved: set[Path],
    ) -> install_git_hooks.PathIdentity:
        if destination.name == "commit-msg":
            replacement = hooks_dir / ".external-update"
            replacement.write_text("external update\n", encoding="utf-8")
            replacement.replace(pre_commit)
            raise OSError("injected publication failure")
        return real_publish(staged, destination, snapshot, preserved)

    monkeypatch.setattr(install_git_hooks, "_publish_hook", update_then_fail)

    with pytest.raises(hook_install_runtime.InstallError, match="destination changed outside this installation"):
        install_git_hooks.install(repo_root=linked, lock_timeout=1, command_timeout=2, kill_after=1)

    assert pre_commit.read_text(encoding="utf-8") == "external update\n"
    assert list(hooks_dir.glob(".pre-commit.backup.fieldkit.*"))


def test_existing_hook_rollback_preserves_update_arriving_at_exchange(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that existing hook rollback preserves update arriving at exchange."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    pre_commit = hooks_dir / "pre-commit"
    pre_commit.write_text("existing pre-commit\n", encoding="utf-8")
    env = _tool_environment(tmp_path, hooks_dir)
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setenv("HOOKS_DIR", env["HOOKS_DIR"])
    real_publish = install_git_hooks._publish_hook
    real_exchange = install_git_hooks._exchange_paths
    injected = False

    def fail_commit_msg(
        staged: Path,
        destination: Path,
        snapshot: install_git_hooks.Snapshot,
        preserved: set[Path],
    ) -> install_git_hooks.PathIdentity:
        if destination.name == "commit-msg":
            raise OSError("injected publication failure")
        return real_publish(staged, destination, snapshot, preserved)

    def update_before_rollback_exchange(first: Path, second: Path) -> None:
        nonlocal injected
        if not injected and first.name.startswith(".pre-commit.backup.fieldkit."):
            injected = True
            replacement = hooks_dir / ".external-update"
            replacement.write_text("external rollback update\n", encoding="utf-8")
            replacement.replace(pre_commit)
        real_exchange(first, second)

    monkeypatch.setattr(install_git_hooks, "_publish_hook", fail_commit_msg)
    monkeypatch.setattr(install_git_hooks, "_exchange_paths", update_before_rollback_exchange)

    with pytest.raises(hook_install_runtime.InstallError, match="destination changed outside this installation"):
        install_git_hooks.install(repo_root=linked, lock_timeout=1, command_timeout=2, kill_after=1)

    assert injected
    assert pre_commit.read_text(encoding="utf-8") == "external rollback update\n"


def test_absent_hook_rollback_preserves_update_arriving_at_exchange(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that absent hook rollback preserves update arriving at exchange."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    commit_msg = hooks_dir / "commit-msg"
    env = _tool_environment(tmp_path, hooks_dir)
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setenv("HOOKS_DIR", env["HOOKS_DIR"])
    real_publish = install_git_hooks._publish_hook
    real_exchange = install_git_hooks._exchange_paths
    injected = False

    def fail_post_commit(
        staged: Path,
        destination: Path,
        snapshot: install_git_hooks.Snapshot,
        preserved: set[Path],
    ) -> install_git_hooks.PathIdentity:
        if destination.name == "post-commit":
            raise OSError("injected publication failure")
        return real_publish(staged, destination, snapshot, preserved)

    def update_before_absent_rollback_exchange(first: Path, second: Path) -> None:
        nonlocal injected
        if not injected and first.name.startswith(".commit-msg.rollback.fieldkit."):
            injected = True
            replacement = hooks_dir / ".external-update"
            replacement.write_text("external absent update\n", encoding="utf-8")
            replacement.replace(commit_msg)
        real_exchange(first, second)

    monkeypatch.setattr(install_git_hooks, "_publish_hook", fail_post_commit)
    monkeypatch.setattr(install_git_hooks, "_exchange_paths", update_before_absent_rollback_exchange)

    with pytest.raises(hook_install_runtime.InstallError, match="destination changed outside this installation"):
        install_git_hooks.install(repo_root=linked, lock_timeout=1, command_timeout=2, kill_after=1)

    assert injected
    assert commit_msg.read_text(encoding="utf-8") == "external absent update\n"


@pytest.mark.parametrize(
    ("hook_name", "exchange_prefix"),
    [
        ("pre-commit", ".pre-commit.backup.fieldkit."),
        ("commit-msg", ".commit-msg.rollback.fieldkit."),
    ],
)
def test_rollback_reverses_exchange_when_displaced_inspection_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hook_name: str,
    exchange_prefix: str,
) -> None:
    """Verify that rollback reverses exchange when displaced inspection fails."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    destination = hooks_dir / hook_name
    if hook_name == "pre-commit":
        destination.write_text("existing pre-commit\n", encoding="utf-8")
    env = _tool_environment(tmp_path, hooks_dir)
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setenv("HOOKS_DIR", env["HOOKS_DIR"])
    real_publish = install_git_hooks._publish_hook
    real_exchange = install_git_hooks._exchange_paths
    real_identity = install_git_hooks._path_identity
    inspect_path: Path | None = None

    def fail_post_commit(
        staged: Path,
        publish_destination: Path,
        snapshot: install_git_hooks.Snapshot,
        preserved: set[Path],
    ) -> install_git_hooks.PathIdentity:
        if publish_destination.name == "post-commit":
            raise OSError("injected publication failure")
        return real_publish(staged, publish_destination, snapshot, preserved)

    def arm_inspection_failure(first: Path, second: Path) -> None:
        nonlocal inspect_path
        real_exchange(first, second)
        if inspect_path is None and first.name.startswith(exchange_prefix):
            inspect_path = first

    def fail_armed_inspection(path: Path) -> install_git_hooks.PathIdentity | None:
        nonlocal inspect_path
        if inspect_path == path:
            inspect_path = None
            raise OSError("injected rollback inspection failure")
        return real_identity(path)

    monkeypatch.setattr(install_git_hooks, "_publish_hook", fail_post_commit)
    monkeypatch.setattr(install_git_hooks, "_exchange_paths", arm_inspection_failure)
    monkeypatch.setattr(install_git_hooks, "_path_identity", fail_armed_inspection)

    with pytest.raises(hook_install_runtime.InstallError, match="injected rollback inspection failure"):
        install_git_hooks.install(repo_root=linked, lock_timeout=1, command_timeout=2, kill_after=1)

    assert destination.is_file()
    assert destination.stat().st_size > 0
    assert _mode(destination) == 0o755
    assert not list(hooks_dir.glob(f".{hook_name}.rollback.fieldkit.*"))


def test_absent_rollback_removes_placeholder_when_inspection_and_reverse_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that absent rollback removes placeholder when inspection and reverse fail."""
    destination = tmp_path / "commit-msg"
    destination.write_text("published commit-msg\n", encoding="utf-8")
    destination.chmod(0o755)
    published_identity = install_git_hooks._path_identity(destination)
    assert published_identity is not None
    snapshot = install_git_hooks.Snapshot(destination, None, None)
    preserved: set[Path] = set()
    real_exchange = install_git_hooks._exchange_paths
    real_identity = install_git_hooks._path_identity
    exchanges = 0

    def fail_reverse_exchange(first: Path, second: Path) -> None:
        nonlocal exchanges
        exchanges += 1
        if exchanges == 2:
            raise OSError("injected reverse exchange failure")
        real_exchange(first, second)

    def fail_displaced_inspection(path: Path) -> install_git_hooks.PathIdentity | None:
        if path.name.startswith(".commit-msg.rollback.fieldkit.") and exchanges == 1:
            raise OSError("injected rollback inspection failure")
        return real_identity(path)

    monkeypatch.setattr(install_git_hooks, "_exchange_paths", fail_reverse_exchange)
    monkeypatch.setattr(install_git_hooks, "_path_identity", fail_displaced_inspection)

    with pytest.raises(hook_install_runtime.InstallError, match="cannot reverse rollback exchange") as exc_info:
        snapshot.restore(published_identity, preserved)

    assert not os.path.lexists(destination)
    assert len(preserved) == 1
    recovery = next(iter(preserved))
    assert recovery.read_text(encoding="utf-8") == "published commit-msg\n"
    assert str(recovery) in str(exc_info.value)
    assert exc_info.value.__notes__ == [
        "rollback inspection failed: OSError: injected rollback inspection failure",
        "reverse exchange failed: OSError: injected reverse exchange failure",
    ]


def test_absent_rollback_preserves_update_arriving_during_placeholder_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that absent rollback preserves update arriving during placeholder removal."""
    destination = tmp_path / "commit-msg"
    destination.write_text("published commit-msg\n", encoding="utf-8")
    destination.chmod(0o755)
    published_identity = install_git_hooks._path_identity(destination)
    assert published_identity is not None
    snapshot = install_git_hooks.Snapshot(destination, None, None)
    preserved: set[Path] = set()
    real_exchange = install_git_hooks._exchange_paths
    real_identity = install_git_hooks._path_identity
    real_move = install_git_hooks._move_no_replace
    exchanges = 0
    updated = False

    def fail_reverse_exchange(first: Path, second: Path) -> None:
        nonlocal exchanges
        exchanges += 1
        if exchanges == 2:
            raise OSError("injected reverse exchange failure")
        real_exchange(first, second)

    def fail_displaced_inspection(path: Path) -> install_git_hooks.PathIdentity | None:
        if path.name.startswith(".commit-msg.rollback.fieldkit.") and exchanges == 1:
            raise OSError("injected rollback inspection failure")
        return real_identity(path)

    def update_before_atomic_removal(source: Path, recovery: Path) -> None:
        nonlocal updated
        updated = True
        replacement = tmp_path / ".external-update"
        replacement.write_text("external rollback update\n", encoding="utf-8")
        replacement.replace(destination)
        real_move(source, recovery)

    monkeypatch.setattr(install_git_hooks, "_exchange_paths", fail_reverse_exchange)
    monkeypatch.setattr(install_git_hooks, "_path_identity", fail_displaced_inspection)
    monkeypatch.setattr(install_git_hooks, "_move_no_replace", update_before_atomic_removal)

    with pytest.raises(hook_install_runtime.InstallError, match="cannot reverse rollback exchange"):
        snapshot.restore(published_identity, preserved)

    assert updated
    assert destination.read_text(encoding="utf-8") == "external rollback update\n"
    assert all(path.read_text(encoding="utf-8") == "published commit-msg\n" for path in preserved)


def test_absent_rollback_preserves_update_replacing_verified_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that absent rollback preserves update replacing verified placeholder."""
    destination = tmp_path / "commit-msg"
    destination.write_text("published commit-msg\n", encoding="utf-8")
    destination.chmod(0o755)
    published_identity = install_git_hooks._path_identity(destination)
    assert published_identity is not None
    snapshot = install_git_hooks.Snapshot(destination, None, None)
    real_remove = install_git_hooks._remove_matching_destination
    replacement_attempted = False

    def replace_before_atomic_removal(
        remove_destination: Path,
        expected: install_git_hooks.PathIdentity,
        preserved: set[Path],
    ) -> None:
        nonlocal replacement_attempted
        replacement_attempted = True
        replacement = tmp_path / ".external-update"
        replacement.write_text("external rollback update\n", encoding="utf-8")
        replacement.replace(remove_destination)
        real_remove(remove_destination, expected, preserved)

    monkeypatch.setattr(install_git_hooks, "_remove_matching_destination", replace_before_atomic_removal)

    with pytest.raises(hook_install_runtime.InstallError, match="destination changed during rollback cleanup"):
        snapshot.restore(published_identity, set())

    assert replacement_attempted
    assert destination.read_text(encoding="utf-8") == "external rollback update\n"


def test_signal_after_final_snapshot_rolls_back_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that signal after final snapshot rolls back publication."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    expected = {name: f"existing {name}\n".encode() for name in install_git_hooks.HOOK_NAMES}
    for name, content in expected.items():
        destination = hooks_dir / name
        destination.write_bytes(content)
        destination.chmod(0o755)
    env = _tool_environment(tmp_path, hooks_dir)
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setenv("HOOKS_DIR", env["HOOKS_DIR"])
    termination_signals = hook_install_runtime.TerminationSignals()
    real_snapshot = install_git_hooks._snapshot
    snapshots = 0

    def signal_after_snapshot(
        destination: Path,
        hooks_directory: Path,
        cleanup: ExitStack,
        preserved: set[Path],
        errors: list[BaseException],
        signals: hook_install_runtime.TerminationSignals,
    ) -> install_git_hooks.Snapshot:
        nonlocal snapshots
        snapshot = real_snapshot(destination, hooks_directory, cleanup, preserved, errors, signals)
        snapshots += 1
        if snapshots == len(install_git_hooks.HOOK_NAMES):
            termination_signals.handle(signal.SIGTERM, None)
        return snapshot

    monkeypatch.setattr(install_git_hooks, "_snapshot", signal_after_snapshot)

    with pytest.raises(hook_install_runtime.InstallInterrupted, match="SIGTERM"):
        install_git_hooks.install(
            repo_root=linked,
            lock_timeout=1,
            command_timeout=2,
            kill_after=1,
            termination_signals=termination_signals,
        )

    assert {name: (hooks_dir / name).read_bytes() for name in expected} == expected
    assert not list(hooks_dir.glob(".*.fieldkit.*"))


def test_publication_refuses_hook_changed_after_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that publication refuses hook changed after snapshot."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    pre_commit = hooks_dir / "pre-commit"
    pre_commit.write_text("existing pre-commit\n", encoding="utf-8")
    env = _tool_environment(tmp_path, hooks_dir)
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setenv("HOOKS_DIR", env["HOOKS_DIR"])
    real_snapshot = install_git_hooks._snapshot
    snapshots = 0

    def update_after_final_snapshot(
        destination: Path,
        hooks_directory: Path,
        cleanup: ExitStack,
        preserved: set[Path],
        errors: list[BaseException],
        termination_signals: hook_install_runtime.TerminationSignals,
    ) -> install_git_hooks.Snapshot:
        nonlocal snapshots
        snapshot = real_snapshot(
            destination,
            hooks_directory,
            cleanup,
            preserved,
            errors,
            termination_signals,
        )
        snapshots += 1
        if snapshots == len(install_git_hooks.HOOK_NAMES):
            replacement = hooks_dir / ".external-update"
            replacement.write_text("external update\n", encoding="utf-8")
            replacement.replace(pre_commit)
        return snapshot

    monkeypatch.setattr(install_git_hooks, "_snapshot", update_after_final_snapshot)

    with pytest.raises(hook_install_runtime.InstallError, match="destination changed during publication"):
        install_git_hooks.install(repo_root=linked, lock_timeout=1, command_timeout=2, kill_after=1)

    assert pre_commit.read_text(encoding="utf-8") == "external update\n"
    assert not list(hooks_dir.glob(".*.fieldkit.*"))


def test_publication_preserves_update_immediately_before_atomic_exchange(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that publication preserves update immediately before atomic exchange."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    pre_commit = hooks_dir / "pre-commit"
    pre_commit.write_text("existing pre-commit\n", encoding="utf-8")
    env = _tool_environment(tmp_path, hooks_dir)
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setenv("HOOKS_DIR", env["HOOKS_DIR"])
    real_exchange = install_git_hooks._exchange_paths
    updated = False

    def update_then_exchange(first: Path, second: Path) -> None:
        nonlocal updated
        if not updated and second.name == "pre-commit":
            updated = True
            replacement = hooks_dir / ".external-update"
            replacement.write_text("external update\n", encoding="utf-8")
            replacement.replace(pre_commit)
        real_exchange(first, second)

    monkeypatch.setattr(install_git_hooks, "_exchange_paths", update_then_exchange)

    with pytest.raises(hook_install_runtime.InstallError, match="destination changed during publication"):
        install_git_hooks.install(repo_root=linked, lock_timeout=1, command_timeout=2, kill_after=1)

    assert updated
    assert pre_commit.read_text(encoding="utf-8") == "external update\n"
    assert not list(hooks_dir.glob(".*.fieldkit.*"))


def test_publication_refuses_in_place_update_after_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that publication refuses in place update after snapshot."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    pre_commit = hooks_dir / "pre-commit"
    pre_commit.write_text("existing pre-commit\n", encoding="utf-8")
    original_times = pre_commit.stat()
    env = _tool_environment(tmp_path, hooks_dir)
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setenv("HOOKS_DIR", env["HOOKS_DIR"])
    real_snapshot = install_git_hooks._snapshot
    snapshots = 0

    def rewrite_after_final_snapshot(
        destination: Path,
        hooks_directory: Path,
        cleanup: ExitStack,
        preserved: set[Path],
        errors: list[BaseException],
        termination_signals: hook_install_runtime.TerminationSignals,
    ) -> install_git_hooks.Snapshot:
        nonlocal snapshots
        snapshot = real_snapshot(destination, hooks_directory, cleanup, preserved, errors, termination_signals)
        snapshots += 1
        if snapshots == len(install_git_hooks.HOOK_NAMES):
            pre_commit.write_text("external pre-commit\n", encoding="utf-8")
            os.utime(pre_commit, ns=(original_times.st_atime_ns, original_times.st_mtime_ns))
        return snapshot

    monkeypatch.setattr(install_git_hooks, "_snapshot", rewrite_after_final_snapshot)

    with pytest.raises(hook_install_runtime.InstallError, match="destination changed during publication"):
        install_git_hooks.install(repo_root=linked, lock_timeout=1, command_timeout=2, kill_after=1)

    assert pre_commit.read_text(encoding="utf-8") == "external pre-commit\n"
    assert not list(hooks_dir.glob(".*.fieldkit.*"))


def test_final_validation_rejects_and_preserves_external_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that final validation rejects and preserves an external replacement."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    post_commit = hooks_dir / "post-commit"
    post_commit.write_text("existing post-commit\n", encoding="utf-8")
    env = _tool_environment(tmp_path, hooks_dir)
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setenv("HOOKS_DIR", env["HOOKS_DIR"])
    real_validate = install_git_hooks._validate_installed_hooks

    def replace_before_validation(
        destinations: dict[str, Path], published: dict[str, install_git_hooks.PathIdentity]
    ) -> None:
        replacement = hooks_dir / ".external-post-commit"
        replacement.write_text("external post-commit\n", encoding="utf-8")
        replacement.chmod(0o755)
        replacement.replace(post_commit)
        real_validate(destinations, published)

    monkeypatch.setattr(install_git_hooks, "_validate_installed_hooks", replace_before_validation)

    with pytest.raises(hook_install_runtime.InstallError, match="installed hook changed after publication") as exc_info:
        install_git_hooks.install(repo_root=linked, lock_timeout=1, command_timeout=2, kill_after=1)

    assert post_commit.read_text(encoding="utf-8") == "external post-commit\n"
    backups = list(hooks_dir.glob(".post-commit.backup.fieldkit.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "existing post-commit\n"
    assert str(backups[0]) in str(exc_info.value)


def test_publication_preserves_second_update_before_reverse_exchange(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that publication preserves second update before reverse exchange."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    pre_commit = hooks_dir / "pre-commit"
    pre_commit.write_text("existing pre-commit\n", encoding="utf-8")
    env = _tool_environment(tmp_path, hooks_dir)
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setenv("HOOKS_DIR", env["HOOKS_DIR"])
    real_exchange = install_git_hooks._exchange_paths
    exchanges = 0

    def update_around_exchanges(first: Path, second: Path) -> None:
        nonlocal exchanges
        if second.name == "pre-commit":
            exchanges += 1
            if exchanges <= 2:
                replacement = hooks_dir / ".external-update"
                replacement.write_text(f"external update {exchanges}\n", encoding="utf-8")
                replacement.replace(pre_commit)
        real_exchange(first, second)

    monkeypatch.setattr(install_git_hooks, "_exchange_paths", update_around_exchanges)

    with pytest.raises(hook_install_runtime.InstallError, match="destination changed during publication"):
        install_git_hooks.install(repo_root=linked, lock_timeout=1, command_timeout=2, kill_after=1)

    recovery = list(hooks_dir.glob(".pre-commit.fieldkit.*"))
    assert exchanges == 3
    assert pre_commit.read_text(encoding="utf-8") == "external update 2\n"
    assert recovery == []


def test_continuous_publication_updates_fail_bounded_and_release_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that continuous publication updates fail bounded and release lock."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    pre_commit = hooks_dir / "pre-commit"
    pre_commit.write_text("existing pre-commit\n", encoding="utf-8")
    env = _tool_environment(tmp_path, hooks_dir)
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setenv("HOOKS_DIR", env["HOOKS_DIR"])
    real_exchange = install_git_hooks._exchange_paths
    exchanges = 0

    def update_before_every_exchange(first: Path, second: Path) -> None:
        nonlocal exchanges
        if second.name == "pre-commit":
            exchanges += 1
            replacement = hooks_dir / ".external-update"
            replacement.write_text(f"external update {exchanges}\n", encoding="utf-8")
            replacement.replace(pre_commit)
        real_exchange(first, second)

    monkeypatch.setattr(install_git_hooks, "_exchange_paths", update_before_every_exchange)

    with pytest.raises(hook_install_runtime.InstallError, match="newest displaced hook preserved at") as exc_info:
        install_git_hooks.install(repo_root=linked, lock_timeout=1, command_timeout=2, kill_after=1)

    recovery = list(hooks_dir.glob(".pre-commit.fieldkit.*"))
    assert exchanges == install_git_hooks.MAX_DISPLACED_UPDATE_EXCHANGES + 1
    assert len(recovery) == 1
    assert recovery[0].read_text(encoding="utf-8") == f"external update {exchanges}\n"
    assert str(recovery[0]) in str(exc_info.value)
    with (hooks_dir / ".fieldkit-install.lock").open("a+b") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_publication_preserves_original_when_inspection_and_reverse_exchange_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that publication preserves original when inspection and reverse exchange fail."""
    destination = tmp_path / "pre-commit"
    destination.write_text("existing pre-commit\n", encoding="utf-8")
    original_identity = install_git_hooks._path_identity(destination)
    assert original_identity is not None
    backup = tmp_path / ".pre-commit.backup.fieldkit.test"
    shutil.copy2(destination, backup)
    staged = tmp_path / ".pre-commit.fieldkit.test"
    staged.write_text("successor pre-commit\n", encoding="utf-8")
    snapshot = install_git_hooks.Snapshot(destination, backup, original_identity)
    preserved: set[Path] = set()
    real_exchange = install_git_hooks._exchange_paths
    real_identity = install_git_hooks._path_identity
    exchanges = 0

    def fail_reverse_exchange(first: Path, second: Path) -> None:
        nonlocal exchanges
        exchanges += 1
        if exchanges == 2:
            raise OSError("injected reverse exchange failure")
        real_exchange(first, second)

    def fail_displaced_inspection(path: Path) -> install_git_hooks.PathIdentity | None:
        if path == staged and exchanges == 1:
            raise OSError("injected identity failure")
        return real_identity(path)

    monkeypatch.setattr(install_git_hooks, "_exchange_paths", fail_reverse_exchange)
    monkeypatch.setattr(install_git_hooks, "_path_identity", fail_displaced_inspection)

    with pytest.raises(hook_install_runtime.InstallError, match="displaced hook preserved at") as exc_info:
        install_git_hooks._publish_hook(staged, destination, snapshot, preserved)

    assert destination.read_text(encoding="utf-8") == "existing pre-commit\n"
    assert staged.read_text(encoding="utf-8") == "existing pre-commit\n"
    assert staged in preserved
    assert str(staged) in str(exc_info.value)
    assert exc_info.value.__notes__ == [
        "publication inspection failed: OSError: injected identity failure",
        "atomic exchange rollback failed: OSError: injected reverse exchange failure",
    ]


def test_exchange_reports_missing_linux_libc_symbol(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that exchange reports missing linux libc symbol."""

    class LibcWithoutRenameAt2:
        pass

    monkeypatch.setattr("scripts.install_git_hooks.ctypes.CDLL", lambda *_args, **_kwargs: LibcWithoutRenameAt2())
    monkeypatch.setattr("scripts.install_git_hooks.sys.platform", "linux")

    with pytest.raises(hook_install_runtime.InstallError, match="atomic hook exchange is unavailable on linux"):
        install_git_hooks._exchange_paths(tmp_path / "first", tmp_path / "second")


def test_failed_symlink_rollback_reports_backup_not_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that failed symlink rollback reports backup not target."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    target = tmp_path / "legacy-target"
    target.write_text("legacy hook\n", encoding="utf-8")
    (hooks_dir / "pre-commit").symlink_to(target)
    env = _tool_environment(tmp_path, hooks_dir)
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setenv("HOOKS_DIR", env["HOOKS_DIR"])
    real_publish = install_git_hooks._publish_hook
    real_restore = install_git_hooks.Snapshot.restore

    def fail_commit_msg(
        staged: Path,
        destination: Path,
        snapshot: install_git_hooks.Snapshot,
        preserved: set[Path],
    ) -> install_git_hooks.PathIdentity:
        if destination.name == "commit-msg":
            raise OSError("injected publication failure")
        return real_publish(staged, destination, snapshot, preserved)

    def fail_pre_commit_restore(
        snapshot: install_git_hooks.Snapshot,
        published_identity: install_git_hooks.PathIdentity,
        preserved: set[Path],
    ) -> None:
        if snapshot.destination.name == "pre-commit":
            raise OSError("injected symlink rollback failure")
        real_restore(snapshot, published_identity, preserved)

    monkeypatch.setattr(install_git_hooks, "_publish_hook", fail_commit_msg)
    monkeypatch.setattr(install_git_hooks.Snapshot, "restore", fail_pre_commit_restore)

    with pytest.raises(hook_install_runtime.InstallError, match="backups preserved at") as exc_info:
        install_git_hooks.install(repo_root=linked, lock_timeout=1, command_timeout=2, kill_after=1)

    backups = list(hooks_dir.glob(".pre-commit.backup.fieldkit.*"))
    assert len(backups) == 1
    assert backups[0].is_symlink()
    assert str(backups[0]) in str(exc_info.value)
    assert str(target) not in str(exc_info.value)


def test_installation_does_not_follow_existing_hook_symlink(tmp_path: Path) -> None:
    """Verify that installation does not follow existing hook symlink."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    external_hook = tmp_path / "external-pre-push"
    external_hook.write_text("external hook\n", encoding="utf-8")
    (hooks_dir / "pre-push").symlink_to(external_hook)
    env = _tool_environment(tmp_path, hooks_dir)

    result = _make(cwd=linked, env=env)

    assert result.returncode == 0
    assert external_hook.read_text(encoding="utf-8") == "external hook\n"
    assert (hooks_dir / "pre-push").is_file()
    assert not (hooks_dir / "pre-push").is_symlink()


def test_hooks_directory_swap_cannot_redirect_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that hooks directory swap cannot redirect publication."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    existing = {name: f"existing {name}\n" for name in install_git_hooks.HOOK_NAMES}
    for name, content in existing.items():
        (hooks_dir / name).write_text(content, encoding="utf-8")
    external = tmp_path / "external-hooks"
    external.mkdir()
    detached = tmp_path / "detached-hooks"
    env = _tool_environment(tmp_path, hooks_dir)
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setenv("HOOKS_DIR", env["HOOKS_DIR"])
    real_lock = install_git_hooks._installation_lock

    @contextmanager
    def swap_before_lock(
        path: Path,
        *,
        timeout_seconds: float,
        termination_signals: hook_install_runtime.TerminationSignals | None = None,
    ) -> Iterator[None]:
        hooks_dir.rename(detached)
        hooks_dir.symlink_to(external, target_is_directory=True)
        with real_lock(path, timeout_seconds=timeout_seconds, termination_signals=termination_signals):
            yield

    monkeypatch.setattr(install_git_hooks, "_installation_lock", swap_before_lock)

    with pytest.raises(hook_install_runtime.InstallError, match="shared hooks directory changed during installation"):
        install_git_hooks.install(repo_root=linked, lock_timeout=1, command_timeout=2, kill_after=1)

    assert list(external.iterdir()) == []
    assert {name: (detached / name).read_text(encoding="utf-8") for name in existing} == existing
    assert not list(detached.glob(".*.fieldkit.*"))


def test_failed_rollback_preserves_recovery_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that failed rollback preserves recovery backup."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    pre_commit = hooks_dir / "pre-commit"
    pre_commit.write_text("existing pre-commit\n", encoding="utf-8")
    pre_push = hooks_dir / "pre-push"
    pre_push.write_text("existing pre-push\n", encoding="utf-8")
    env = _tool_environment(tmp_path, hooks_dir)
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("HOOKS_DIR", env["HOOKS_DIR"])
    real_restore = install_git_hooks.Snapshot.restore
    real_publish = install_git_hooks._publish_hook

    def fail_commit_msg_publication(
        staged: Path,
        destination: Path,
        snapshot: install_git_hooks.Snapshot,
        preserved: set[Path],
    ) -> install_git_hooks.PathIdentity:
        if destination.name == "commit-msg":
            raise OSError("injected publication failure")
        return real_publish(staged, destination, snapshot, preserved)

    def fail_selected_restores(
        snapshot: install_git_hooks.Snapshot,
        published_identity: install_git_hooks.PathIdentity,
        preserved: set[Path],
    ) -> None:
        if snapshot.destination.name == "pre-commit":
            raise KeyboardInterrupt
        if snapshot.destination.name == "pre-push":
            cause = OSError("injected rollback cause")
            cause.add_note("rollback cause note")
            failure = OSError("injected rollback failure")
            failure.add_note("rollback failure note")
            raise failure from cause
        real_restore(snapshot, published_identity, preserved)

    monkeypatch.setattr(install_git_hooks.Snapshot, "restore", fail_selected_restores)
    monkeypatch.setattr(install_git_hooks, "_publish_hook", fail_commit_msg_publication)

    with pytest.raises(hook_install_runtime.InstallError, match="backups preserved at") as exc_info:
        install_git_hooks.install(repo_root=linked, lock_timeout=1, command_timeout=2, kill_after=1)

    backups = sorted(hooks_dir.glob(".*.backup.fieldkit.*"))
    assert len(backups) == 2
    assert [backup.read_text(encoding="utf-8") for backup in backups] == [
        "existing pre-commit\n",
        "existing pre-push\n",
    ]
    message = str(exc_info.value)
    assert all(str(backup) in message for backup in backups)
    assert message.index(str(backups[0])) < message.index(str(backups[1]))
    assert "installation failure: OSError: injected publication failure" in message
    assert "KeyboardInterrupt" in message
    assert "OSError: injected rollback failure" in message
    assert "rollback failure note" in message
    assert "OSError: injected rollback cause" in message
    assert "rollback cause note" in message


def test_cleanup_failure_after_validation_preserves_successor_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that cleanup failure after validation preserves successor hooks."""
    primary, linked = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    env = _tool_environment(tmp_path, hooks_dir)
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setenv("HOOKS_DIR", env["HOOKS_DIR"])

    def fail_staging_cleanup(path: Path) -> None:
        raise OSError(f"injected cleanup failure: {path}")

    monkeypatch.setattr(shutil, "rmtree", fail_staging_cleanup)

    with pytest.raises(hook_install_runtime.InstallError, match="injected cleanup failure"):
        install_git_hooks.install(repo_root=linked, lock_timeout=1, command_timeout=2, kill_after=1)

    for name in install_git_hooks.HOOK_NAMES:
        destination = hooks_dir / name
        assert destination.is_file()
        assert not destination.is_symlink()
        assert _mode(destination) == 0o755
    assert (hooks_dir / "pre-push").read_bytes() == (linked / "hooks" / "pre-push-git-hook").read_bytes()
    assert (hooks_dir / "post-commit").read_bytes() == (linked / "hooks" / "post_commit.py").read_bytes()
    assert len(list(hooks_dir.glob(".install-git.fieldkit.*"))) == 1


def test_hooks_install_from_primary_checkout(tmp_path: Path) -> None:
    """Verify that hooks install from primary checkout."""
    primary, _ = _repository_with_linked_worktree(tmp_path)
    _prepare_sources(primary)
    hooks_dir = primary / ".git" / "hooks"
    env = _tool_environment(tmp_path, hooks_dir)

    result = _make(cwd=primary, env=env)

    assert result.returncode == 0
    assert all((hooks_dir / name).is_file() for name in install_git_hooks.HOOK_NAMES)
    assert all(_mode(hooks_dir / name) == 0o755 for name in install_git_hooks.HOOK_NAMES)


def test_installed_hooks_survive_linked_worktree_removal(tmp_path: Path) -> None:
    """Verify that installed hooks survive linked worktree removal."""
    literal_path_root = tmp_path / "fieldkit-$HOME"
    literal_path_root.mkdir()
    primary, linked = _repository_with_linked_worktree(literal_path_root)
    _prepare_sources(linked)
    hooks_dir = primary / ".git" / "hooks"
    env = _tool_environment(tmp_path, hooks_dir)

    result = _make(cwd=linked, env=env)

    assert result.returncode == 0
    for name in install_git_hooks.HOOK_NAMES:
        destination = hooks_dir / name
        assert destination.is_file()
        assert not destination.is_symlink()
        assert _mode(destination) == 0o755
    assert (hooks_dir / "pre-push").read_bytes() == (linked / "hooks" / "pre-push-git-hook").read_bytes()
    assert (hooks_dir / "post-commit").read_bytes() == (linked / "hooks" / "post_commit.py").read_bytes()

    remove = _run("git", "worktree", "remove", "--force", str(linked), cwd=primary)

    assert remove.returncode == 0
    assert (hooks_dir / "pre-push").is_file()
    assert (hooks_dir / "post-commit").is_file()
    retained_hook = _run(str(hooks_dir / "post-commit"), cwd=primary, env=_isolated_env(tmp_path / "hook-home"))
    assert retained_hook.returncode == 0
