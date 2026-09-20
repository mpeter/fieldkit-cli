#!/usr/bin/env python3
"""Post-commit hook: auto-reinstall fieldkit when its package inputs change.

Runs `uv tool install ".[all]" --reinstall --force --python 3.11` after any commit that modifies files
under src/fieldkit/ or changes ``pyproject.toml`` / ``uv.lock``, keeping the
installed binary in sync with the latest committed code and dependencies.

Never blocks the commit — install failure is an environment problem, not a
code problem. Prints a warning on failure so the developer knows to run
`make install` manually.

Usage: registered by `make hooks` in Git's shared hooks directory
"""

import shutil
import subprocess
import sys
from pathlib import Path

_GIT_TIMEOUT_SECONDS = 30
_REVISION_LENGTH = 12
_MAX_REVISION_LENGTH = 64


def _git_context(git_bin: str) -> tuple[Path, str] | None:
    """Return the worktree and revision from Git's hook invocation context."""
    try:
        result = subprocess.run(
            [git_bin, "rev-parse", "--show-toplevel", f"--short={_REVISION_LENGTH}", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    lines = result.stdout.splitlines()
    if result.returncode != 0 or len(lines) != 2:
        return None
    root_text, revision = lines
    root = Path(root_text)
    if (
        not root.is_absolute()
        or not _REVISION_LENGTH <= len(revision) <= _MAX_REVISION_LENGTH
        or any(character not in "0123456789abcdef" for character in revision)
    ):
        return None
    try:
        resolved_root = root.resolve()
        is_project = (resolved_root / "pyproject.toml").is_file()
    except OSError:
        return None
    if not is_project:
        return None
    return resolved_root, revision


def main() -> int:
    git_bin = shutil.which("git")
    if not git_bin:
        print(
            "post-commit: WARNING — `git` not found on PATH. Run `make install` manually.",
            file=sys.stderr,
        )
        return 0
    context = _git_context(git_bin)
    if context is None:
        print(
            "post-commit: WARNING — cannot identify the invoking Git worktree. "
            "Run `make install` manually before using the CLI.",
            file=sys.stderr,
        )
        return 0
    repo_root, revision = context

    # Get files changed in the most recent commit.
    try:
        result = subprocess.run(
            [git_bin, "diff-tree", "--no-commit-id", "-r", "--name-only", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        print("post-commit: WARNING — git diff-tree failed. Run `make install` manually.", file=sys.stderr)
        return 0
    if result.returncode != 0:
        print(
            f"post_commit.py: git diff-tree failed: {result.stderr.strip()}",
            file=sys.stderr,
        )
        return 0  # don't block

    changed = [f.strip() for f in result.stdout.splitlines() if f.strip()]
    install_inputs_touched = any(f.startswith("src/fieldkit/") or f in {"pyproject.toml", "uv.lock"} for f in changed)
    opencode_touched = any(f.startswith(".opencode/agents/") or f.startswith(".opencode/commands/") for f in changed)

    # Sync .opencode/ → .claude/ when agents or commands change (with prune).
    if opencode_touched:
        python_bin = shutil.which("python3") or shutil.which("python")
        sync_script = repo_root / "scripts" / "sync_claude_dir.py"
        if python_bin and sync_script.is_file():
            print("post-commit: .opencode/ changed — syncing .claude/ (with prune)…")
            try:
                subprocess.run(
                    [python_bin, str(sync_script), "--prune"],
                    cwd=repo_root,
                    check=False,
                    timeout=30,
                )
            except (subprocess.TimeoutExpired, OSError):
                print(
                    "post-commit: WARNING — .claude/ sync failed. Run `make sync-claude` manually.",
                    file=sys.stderr,
                )
        else:
            # Never skip silently: an unfindable sync script means .claude/ quietly
            # stops being pruned, and the drift only surfaces much later as a failing
            # check on an unrelated PR.
            missing = "python3" if not python_bin else str(sync_script)
            print(
                f"post-commit: WARNING — cannot sync .claude/ ({missing} not found). Run `make sync-claude` manually.",
                file=sys.stderr,
            )

    if not install_inputs_touched:
        return 0

    uv_bin = shutil.which("uv")
    if not uv_bin:
        print(
            "post-commit: WARNING — `uv` not found on PATH. Run `make install` manually before using the CLI.",
            file=sys.stderr,
        )
        return 0

    print("post-commit: fieldkit package inputs changed — reinstalling fieldkit CLI…")
    try:
        install = subprocess.run(
            [uv_bin, "tool", "install", ".[all]", "--reinstall", "--force", "--python", "3.11"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError):
        print(
            "post-commit: WARNING — `uv tool install` failed to start or timed out. "
            "Run `make install` manually before using the CLI.",
            file=sys.stderr,
        )
        return 0

    if install.returncode != 0:
        print(
            "post-commit: WARNING — `uv tool install . --reinstall` failed. "
            "Run `make install` manually before using the CLI.",
            file=sys.stderr,
        )
        if install.stderr:
            # Truncate to avoid leaking environment details in error output
            print(install.stderr.strip()[:500], file=sys.stderr)
        return 0  # never block the commit

    print(
        f"post-commit: fieldkit reinstalled successfully from worktree at HEAD {revision}; "
        "uncommitted package changes, if any, were included."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
