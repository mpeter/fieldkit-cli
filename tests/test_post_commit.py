"""Unit tests for hooks/post_commit.py.

Covers all four code paths:
  A: git diff-tree succeeds, no src/fieldkit/ files touched → return 0, no install
  B: src/fieldkit/ touched, uv install succeeds → return 0
  C: src/fieldkit/ touched, uv install fails → return 0 with warning to stderr
  D: git diff-tree itself fails → return 0 with warning to stderr

Also covers:
  E: uv not found on PATH → return 0 with warning
  F: uv install times out → return 0 with warning
"""

import sys
from collections.abc import Iterator
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = ROOT / "hooks"
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

import hooks.post_commit as pc  # noqa: E402

_REAL_GIT_CONTEXT = pc._git_context


@pytest.fixture(autouse=True)
def _stable_git_context() -> Iterator[None]:
    """Keep legacy main() tests focused on behavior after root discovery."""
    with patch.object(pc, "_git_context", return_value=(ROOT, "0123456789ab")):
        yield


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> CompletedProcess[str]:
    return CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.mark.parametrize("revision", ["0123456789ab", "0123456789abc", "0" * 64])
def test_git_context_returns_validated_worktree_and_revision(tmp_path: Path, revision: str) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'fixture'\n")
    with patch(
        "subprocess.run",
        return_value=_proc(stdout=f"{tmp_path}\n{revision}\n"),
    ) as mock_run:
        context = _REAL_GIT_CONTEXT("/usr/bin/git")

    assert context == (tmp_path.resolve(), revision)
    assert mock_run.call_args.args[0] == [
        "/usr/bin/git",
        "rev-parse",
        "--show-toplevel",
        "--short=12",
        "HEAD",
    ]


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        (1, ""),
        (0, ""),
        (0, "/worktree\n"),
        (0, "/worktree\n0123456789ab\nextra\n"),
        (0, "relative/path\n0123456789ab\n"),
        (0, "/worktree\nnot-a-commit\n"),
    ],
)
def test_git_context_rejects_failed_or_malformed_output(returncode: int, stdout: str) -> None:
    with patch("subprocess.run", return_value=_proc(returncode=returncode, stdout=stdout)):
        assert _REAL_GIT_CONTEXT("/usr/bin/git") is None


def test_git_context_rejects_root_without_project_metadata(tmp_path: Path) -> None:
    with patch("subprocess.run", return_value=_proc(stdout=f"{tmp_path}\n0123456789ab\n")):
        assert _REAL_GIT_CONTEXT("/usr/bin/git") is None


@pytest.mark.parametrize("revision", ["0123456789a", "0" * 65])
def test_git_context_rejects_out_of_bounds_revision_lengths(tmp_path: Path, revision: str) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'fixture'\n")
    with patch("subprocess.run", return_value=_proc(stdout=f"{tmp_path}\n{revision}\n")):
        assert _REAL_GIT_CONTEXT("/usr/bin/git") is None


@pytest.mark.parametrize("error", [TimeoutExpired(cmd=["git"], timeout=30), PermissionError()])
def test_git_context_launch_failure_returns_none(error: BaseException) -> None:
    with patch("subprocess.run", side_effect=error):
        assert _REAL_GIT_CONTEXT("/usr/bin/git") is None


def test_main_skips_checkout_actions_when_git_context_is_unavailable(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch.object(pc, "_git_context", return_value=None),
        patch("subprocess.run") as mock_run,
        patch("shutil.which", return_value="/usr/bin/git"),
    ):
        result = pc.main()

    assert result == 0
    mock_run.assert_not_called()
    assert "cannot identify the invoking Git worktree" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# A: No src/fieldkit/ files touched
# ---------------------------------------------------------------------------


# ── TestNoSrcTouched (flattened) ────────────────────────────────────────────


def test_no_src_touched_docs_only_commit_returns_0_without_install() -> None:
    """A commit touching only docs/ must return 0 without running uv install."""
    with patch("subprocess.run", return_value=_proc(stdout="docs/README.md\nAGENTS.md\n")) as mock_run:
        result = pc.main()
    assert result == 0
    # Only one call: git diff-tree; no uv install call
    assert mock_run.call_count == 1


def test_no_src_touched_hooks_only_commit_returns_0_without_install() -> None:
    """A commit touching only hooks/ must NOT trigger a reinstall.

    hooks/ files are not part of the installed fieldkit package; uv tool
    install would be a no-op and adds unnecessary latency.
    """
    with patch("subprocess.run", return_value=_proc(stdout="hooks/pii_guard.py\n")) as mock_run:
        result = pc.main()
    assert result == 0
    assert mock_run.call_count == 1


def test_no_src_touched_empty_diff_returns_0() -> None:
    """An empty diff (no files changed) returns 0 without install."""
    with patch("subprocess.run", return_value=_proc(stdout="")) as mock_run:
        result = pc.main()
    assert result == 0
    assert mock_run.call_count == 1


def test_no_src_touched_tests_only_commit_returns_0_without_install() -> None:
    """A commit touching only tests/ must return 0 without install."""
    with patch(
        "subprocess.run",
        return_value=_proc(stdout="tests/test_foo.py\ntests/conftest.py\n"),
    ) as mock_run:
        result = pc.main()
    assert result == 0
    assert mock_run.call_count == 1


@pytest.mark.parametrize("changed_file", ["pyproject.toml", "uv.lock"])
def test_dependency_input_touched_triggers_install(changed_file: str) -> None:
    """Package metadata and lock changes must refresh the global tool environment."""
    with (
        patch("subprocess.run", side_effect=[_proc(stdout=f"{changed_file}\n"), _proc(returncode=0)]) as mock_run,
        patch("shutil.which", return_value="/usr/local/bin/uv"),
    ):
        result = pc.main()

    assert result == 0
    assert mock_run.call_count == 2


# ---------------------------------------------------------------------------
# B: src/fieldkit/ touched — successful install
# ---------------------------------------------------------------------------


# ── TestSrcTouchedInstallSucceeds (flattened) ───────────────────────────────


def test_src_touched_install_succeeds_src_touched_runs_uv_install_and_returns_0(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When src/fieldkit/ is touched, uv install runs and hook returns 0."""
    diff_proc = _proc(stdout="src/fieldkit/sf/client.py\n")
    install_proc = _proc(returncode=0)
    with (
        patch("subprocess.run", side_effect=[diff_proc, install_proc]) as mock_run,
        patch("shutil.which", return_value="/usr/local/bin/uv"),
    ):
        result = pc.main()
    assert result == 0
    assert mock_run.call_count == 2
    # Second call is uv tool install
    install_call_args = mock_run.call_args_list[1]
    cmd = install_call_args[0][0]
    assert cmd == [
        "/usr/local/bin/uv",
        "tool",
        "install",
        ".[all]",
        "--reinstall",
        "--force",
        "--python",
        "3.11",
    ]
    output = capsys.readouterr().out
    assert "HEAD 0123456789ab" in output
    assert "uncommitted package changes" in output


def test_make_install_uses_same_uv_replacement_flags() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert 'uv tool install ".[all]" --reinstall --force --python 3.11' in makefile


def test_src_touched_install_succeeds_multiple_src_files_triggers_install_once() -> None:
    """Multiple src/fieldkit/ files in one commit trigger exactly one install."""
    diff_proc = _proc(stdout="src/fieldkit/sf/client.py\nsrc/fieldkit/llm/core.py\n")
    install_proc = _proc(returncode=0)
    with (
        patch("subprocess.run", side_effect=[diff_proc, install_proc]),
        patch("shutil.which", return_value="/usr/local/bin/uv"),
    ):
        result = pc.main()
    assert result == 0


def test_src_touched_install_succeeds_mixed_commit_with_src_triggers_install() -> None:
    """A commit with both src/fieldkit/ and docs/ files triggers install."""
    diff_proc = _proc(stdout="src/fieldkit/__main__.py\nAGENTS.md\ndocs/README.md\n")
    install_proc = _proc(returncode=0)
    with (
        patch("subprocess.run", side_effect=[diff_proc, install_proc]),
        patch("shutil.which", return_value="/usr/local/bin/uv"),
    ):
        result = pc.main()
    assert result == 0


def test_src_touched_install_succeeds_install_uses_repo_root_cwd() -> None:
    """uv install must be called with cwd=repo_root, not implicit CWD."""
    diff_proc = _proc(stdout="src/fieldkit/sf/client.py\n")
    install_proc = _proc(returncode=0)
    with (
        patch("subprocess.run", side_effect=[diff_proc, install_proc]) as mock_run,
        patch("shutil.which", return_value="/usr/local/bin/uv"),
    ):
        result = pc.main()
    assert result == 0
    install_kwargs = mock_run.call_args_list[1][1]
    assert "cwd" in install_kwargs
    # cwd must be a Path object pointing to the repo root (parent of hooks/)
    cwd = install_kwargs["cwd"]
    assert isinstance(cwd, Path)
    assert cwd == ROOT
    assert mock_run.call_args_list[0].kwargs["cwd"] == ROOT


# ---------------------------------------------------------------------------
# C: src/fieldkit/ touched — install fails
# ---------------------------------------------------------------------------


# ── TestSrcTouchedInstallFails (flattened) ──────────────────────────────────


def test_src_touched_install_fails_install_failure_returns_0_and_warns(capsys: pytest.CaptureFixture[str]) -> None:
    """Install failure must return 0 (never block commit) and warn to stderr."""
    diff_proc = _proc(stdout="src/fieldkit/sf/client.py\n")
    install_proc = _proc(returncode=1, stderr="uv: error: package not found")
    with (
        patch("subprocess.run", side_effect=[diff_proc, install_proc]),
        patch("shutil.which", return_value="/usr/local/bin/uv"),
    ):
        result = pc.main()
    assert result == 0
    captured = capsys.readouterr()
    assert "make install" in captured.err


def test_src_touched_install_fails_install_stderr_truncated_to_500_chars(capsys: pytest.CaptureFixture[str]) -> None:
    """stderr output from failed install must be truncated to ≤500 chars."""
    diff_proc = _proc(stdout="src/fieldkit/sf/client.py\n")
    long_stderr = "x" * 1000
    install_proc = _proc(returncode=1, stderr=long_stderr)
    with (
        patch("subprocess.run", side_effect=[diff_proc, install_proc]),
        patch("shutil.which", return_value="/usr/local/bin/uv"),
    ):
        result = pc.main()
    assert result == 0
    captured = capsys.readouterr()
    # Total = warning line (~113) + 500-char truncated stderr + newlines ≈ 615.
    # Assert the 1000-char stderr was truncated — bound set to <620 to catch
    # regressions where the truncation limit is raised above 500.
    assert len(captured.err) < 620  # truncated: warning line + ≤500 chars + newlines
    assert len(captured.err) < len("x" * 1000)  # definitely shorter than raw stderr


# ---------------------------------------------------------------------------
# D: git diff-tree fails
# ---------------------------------------------------------------------------


# ── TestGitDiffTreeFails (flattened) ────────────────────────────────────────


def test_git_diff_tree_fails_git_failure_returns_0_and_warns(capsys: pytest.CaptureFixture[str]) -> None:
    """git diff-tree failure must return 0 (non-blocking) and warn to stderr."""
    with (
        patch("subprocess.run", return_value=_proc(returncode=1, stderr="fatal: bad HEAD")),
        patch("shutil.which", return_value="/usr/bin/git"),
    ):
        result = pc.main()
    assert result == 0
    captured = capsys.readouterr()
    # Warning must be emitted — the git-failure path has a specific stderr message
    assert "git diff-tree" in captured.err or "failed" in captured.err


def test_git_diff_tree_fails_git_failure_does_not_call_install() -> None:
    """git diff-tree failure must not proceed to uv install."""
    with (
        patch("subprocess.run", return_value=_proc(returncode=1, stderr="fatal: bad HEAD")) as mock_run,
        patch("shutil.which", return_value="/usr/bin/git"),
    ):
        result = pc.main()
    assert result == 0
    assert mock_run.call_count == 1  # only the git call, no install


def test_git_diff_tree_permission_error_returns_0_and_warns(capsys: pytest.CaptureFixture[str]) -> None:
    """An OS launch error from git must not block the commit."""
    with (
        patch("subprocess.run", side_effect=PermissionError("git cannot execute")),
        patch("shutil.which", return_value="/usr/bin/git"),
    ):
        result = pc.main()
    assert result == 0
    assert "git diff-tree failed" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# E: uv not found on PATH
# ---------------------------------------------------------------------------


# ── TestUvNotOnPath (flattened) ─────────────────────────────────────────────


def test_uv_not_on_path_uv_not_found_returns_0_and_warns(capsys: pytest.CaptureFixture[str]) -> None:
    """Missing uv on PATH must return 0 (non-blocking) with a warning."""
    diff_proc = _proc(stdout="src/fieldkit/sf/client.py\n")
    with (
        patch("subprocess.run", return_value=diff_proc),
        patch("shutil.which", side_effect=lambda b: "/usr/bin/git" if b == "git" else None),
    ):
        result = pc.main()
    assert result == 0
    assert "uv" in capsys.readouterr().err


def test_uv_not_on_path_uv_not_found_does_not_call_subprocess_install() -> None:
    """When uv is not found, subprocess.run must not be called for install."""
    diff_proc = _proc(stdout="src/fieldkit/sf/client.py\n")
    with (
        patch("subprocess.run", return_value=diff_proc) as mock_run,
        patch("shutil.which", side_effect=lambda b: "/usr/bin/git" if b == "git" else None),
    ):
        result = pc.main()
    assert result == 0
    # Only git diff-tree call; no uv install call
    assert mock_run.call_count == 1


# ---------------------------------------------------------------------------
# F: uv install times out
# ---------------------------------------------------------------------------


# ── TestInstallTimeout (flattened) ──────────────────────────────────────────


def test_install_timeout_timeout_returns_0_and_warns(capsys: pytest.CaptureFixture[str]) -> None:
    """TimeoutExpired from uv install must return 0 with an actionable warning."""
    diff_proc = _proc(stdout="src/fieldkit/sf/client.py\n")

    def side_effect(cmd: list[str], **_: object) -> CompletedProcess[str]:
        if "diff-tree" in cmd:
            return diff_proc
        raise TimeoutExpired(cmd=cmd, timeout=120)

    with (
        patch("subprocess.run", side_effect=side_effect),
        patch("shutil.which", return_value="/usr/local/bin/uv"),
    ):
        result = pc.main()
    assert result == 0
    captured = capsys.readouterr()
    assert "timed out" in captured.err or "make install" in captured.err


def test_install_permission_error_returns_0_and_warns(capsys: pytest.CaptureFixture[str]) -> None:
    """An OS launch error from uv must not block the commit."""
    diff_proc = _proc(stdout="src/fieldkit/sf/client.py\n")

    def side_effect(cmd: list[str], **_: object) -> CompletedProcess[str]:
        if "diff-tree" in cmd:
            return diff_proc
        raise PermissionError("uv cannot execute")

    with (
        patch("subprocess.run", side_effect=side_effect),
        patch("shutil.which", return_value="/usr/local/bin/uv"),
    ):
        result = pc.main()
    assert result == 0
    assert "failed to start" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# G: .opencode/ agents or commands touched → sync .claude/
# ---------------------------------------------------------------------------


def test_opencode_agents_touched_runs_sync_script(tmp_path: Path) -> None:
    """Committing a file under .opencode/agents/ must trigger sync_claude_dir.py."""
    sync_script = tmp_path / "scripts" / "sync_claude_dir.py"
    sync_script.parent.mkdir()
    sync_script.write_text("# stub")

    with (
        patch("subprocess.run", return_value=_proc(stdout=".opencode/agents/cobalt-crush-dev.md\n")),
        patch("shutil.which", side_effect=lambda b: "/usr/bin/python3" if b == "python3" else None),
        patch.object(pc, "__file__", str(tmp_path / "hooks" / "post_commit.py")),
    ):
        # Should not raise; sync is best-effort
        result = pc.main()

    assert result == 0


def test_opencode_commands_touched_runs_sync_script(tmp_path: Path) -> None:
    """Committing a file under .opencode/commands/ must trigger sync_claude_dir.py."""
    sync_script = tmp_path / "scripts" / "sync_claude_dir.py"
    sync_script.parent.mkdir()
    sync_script.write_text("# stub")

    with (
        patch("subprocess.run", return_value=_proc(stdout=".opencode/commands/workflow-seed.md\n")),
        patch("shutil.which", side_effect=lambda b: "/usr/bin/python3" if b == "python3" else None),
        patch.object(pc, "__file__", str(tmp_path / "hooks" / "post_commit.py")),
    ):
        result = pc.main()

    assert result == 0


def test_opencode_sync_permission_error_returns_0_and_warns(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An OS launch error from the sync script must not block the commit."""
    sync_script = tmp_path / "scripts" / "sync_claude_dir.py"
    sync_script.parent.mkdir()
    sync_script.write_text("# stub")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'fixture'\n")

    def side_effect(cmd: list[str], **_: object) -> CompletedProcess[str]:
        if "diff-tree" in cmd:
            return _proc(stdout=".opencode/agents/cobalt-crush-dev.md\n")
        raise PermissionError("python cannot execute")

    with (
        patch("subprocess.run", side_effect=side_effect),
        patch("shutil.which", side_effect=lambda b: "/usr/bin/python3" if b == "python3" else "/usr/bin/git"),
        patch.object(pc, "_git_context", return_value=(tmp_path, "0123456789ab")),
    ):
        result = pc.main()
    assert result == 0
    assert ".claude/ sync failed" in capsys.readouterr().err


def test_src_touched_but_not_opencode_skips_sync() -> None:
    """A src/fieldkit/ change with no .opencode/ change must not run the sync."""
    call_args_list: list[tuple[object, ...]] = []

    def _fake_run(cmd: object, **kwargs: object) -> CompletedProcess[str]:
        call_args_list.append((cmd,))
        return _proc(stdout="src/fieldkit/config/_loader.py\n")

    with (
        patch("subprocess.run", side_effect=_fake_run),
        patch(
            "shutil.which", side_effect=lambda b: "/usr/bin/uv" if b == "uv" else "/usr/bin/git" if b == "git" else None
        ),
    ):
        pc.main()

    # sync_claude_dir.py should NOT be in any call's command
    all_cmds = [str(c) for (c,) in call_args_list]
    assert not any("sync_claude_dir" in c for c in all_cmds)


# ---------------------------------------------------------------------------
# H: repo_root must resolve through the .git/hooks symlink
#
# `make hooks` installs .git/hooks/post-commit as a symlink to hooks/post_commit.py,
# so __file__ is the symlink path. Without .resolve(), repo_root became <repo>/.git:
# `uv tool install` ran against .git ("not a Python project") and the sync script
# lookup missed, silently skipping the .claude/ prune.
# ---------------------------------------------------------------------------


def _linked_worktree_hook(tmp_path: Path) -> tuple[Path, Path]:
    """Build a shared hook source and a distinct invoking linked worktree."""
    primary = tmp_path / "primary"
    linked = tmp_path / "linked"
    (primary / "hooks").mkdir(parents=True)
    (primary / ".git" / "hooks").mkdir(parents=True)
    (linked / "scripts").mkdir(parents=True)
    (linked / "pyproject.toml").write_text("[project]\nname = 'fixture'\n")
    real = primary / "hooks" / "post_commit.py"
    real.write_text("# real hook body")
    (linked / "scripts" / "sync_claude_dir.py").write_text("# stub")
    link = primary / ".git" / "hooks" / "post-commit"
    link.symlink_to(real)
    return linked, link


def test_linked_worktree_root_drives_uv_install(tmp_path: Path) -> None:
    """uv tool install uses Git's invoking worktree, not the shared hook source."""
    linked, link = _linked_worktree_hook(tmp_path)
    calls: list[tuple[object, object]] = []

    def _fake_run(cmd: object, **kwargs: object) -> CompletedProcess[str]:
        calls.append((cmd, kwargs.get("cwd")))
        return _proc(stdout="src/fieldkit/config/_loader.py\n")

    with (
        patch("subprocess.run", side_effect=_fake_run),
        patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
        patch.object(pc, "_git_context", return_value=(linked, "abcdef012345")),
        patch.object(pc, "__file__", str(link)),
    ):
        assert pc.main() == 0

    install_cwds = [cwd for cmd, cwd in calls if "tool" in str(cmd) and "install" in str(cmd)]
    assert install_cwds == [linked]
    assert link.resolve().is_relative_to(tmp_path / "primary")


def test_linked_worktree_root_drives_agent_surface_sync(tmp_path: Path) -> None:
    """The .claude/ sync script and cwd come from Git's invoking worktree."""
    linked, link = _linked_worktree_hook(tmp_path)
    calls: list[tuple[object, object]] = []

    def _fake_run(cmd: object, **kwargs: object) -> CompletedProcess[str]:
        calls.append((cmd, kwargs.get("cwd")))
        return _proc(stdout=".opencode/agents/some-agent.md\n")

    with (
        patch("subprocess.run", side_effect=_fake_run),
        patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
        patch.object(pc, "_git_context", return_value=(linked, "abcdef012345")),
        patch.object(pc, "__file__", str(link)),
    ):
        assert pc.main() == 0

    sync_calls = [(cmd, cwd) for cmd, cwd in calls if "sync_claude_dir" in str(cmd)]
    assert sync_calls, f"sync_claude_dir.py was never invoked; calls={calls}"
    assert "--prune" in str(sync_calls[0][0])
    assert str(linked / "scripts" / "sync_claude_dir.py") in str(sync_calls[0][0])
    assert sync_calls[0][1] == linked


def test_missing_sync_script_warns_instead_of_skipping_silently(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unfindable sync script must warn — silent skips let .claude/ drift unnoticed."""
    linked, link = _linked_worktree_hook(tmp_path)
    (linked / "scripts" / "sync_claude_dir.py").unlink()

    with (
        patch("subprocess.run", return_value=_proc(stdout=".opencode/commands/some-cmd.md\n")),
        patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
        patch.object(pc, "_git_context", return_value=(linked, "abcdef012345")),
        patch.object(pc, "__file__", str(link)),
    ):
        assert pc.main() == 0

    assert "cannot sync .claude/" in capsys.readouterr().err
