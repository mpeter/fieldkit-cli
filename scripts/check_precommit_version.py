#!/usr/bin/env python3
"""Assert the installed pre-commit satisfies the floor in .pre-commit-config.yaml.

pre-commit enforces ``minimum_pre_commit_version`` itself, but only at the moment
a hook runs — i.e. on someone's first commit, as an aborted commit. Running this
from ``make hooks`` surfaces the same requirement at setup time instead.

The floor is read from the config rather than hardcoded, so there is exactly one
place to bump it.

Usage:
    python scripts/check_precommit_version.py

Exit codes:
    0 — installed pre-commit satisfies the floor (or no floor is declared)
    1 — pre-commit is missing, unparseable, or below the floor
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = REPO_ROOT / ".pre-commit-config.yaml"

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _declared_floor() -> tuple[int, int, int] | None:
    """Return the minimum_pre_commit_version from the config, or None if unset."""
    if not CONFIG.is_file():
        return None
    config = yaml.safe_load(CONFIG.read_text())
    if not isinstance(config, dict):
        return None
    floor = config.get("minimum_pre_commit_version")
    if not isinstance(floor, str):
        return None
    match = _VERSION_RE.search(floor)
    if not match:
        return None
    return (int(match[1]), int(match[2]), int(match[3]))


def _wrapper_python() -> Path | None:
    """Return the INSTALL_PYTHON baked into the generated pre-commit hook wrapper.

    `pre-commit install` writes a wrapper script whose templated INSTALL_PYTHON
    is preferred over PATH whenever it still exists — see the `if -x` branch in
    the generated .git/hooks/pre-commit. Returns None if there is no wrapper yet
    (before the first `pre-commit install`) or its baked path no longer exists,
    in which case the wrapper itself falls back to PATH and so do we.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    hook_path = REPO_ROOT / proc.stdout.strip() / "hooks" / "pre-commit"
    if not hook_path.is_file():
        return None
    for raw_line in hook_path.read_text().splitlines():
        line = raw_line.strip()
        if line.startswith("INSTALL_PYTHON="):
            python_path = Path(line.split("=", 1)[1])
            return python_path if python_path.is_file() else None
    return None


def _installed_version() -> tuple[int, int, int] | None:
    """Return the version of the pre-commit that git hooks will actually invoke.

    The generated .git/hooks/* wrapper prefers a baked-in INSTALL_PYTHON path and
    falls back to whatever `pre-commit` resolves to on PATH only once that baked
    path no longer exists. Mirror that: check the wrapper's own Python when one
    is baked in, otherwise fall back to PATH like the wrapper does.
    """
    wrapper_python = _wrapper_python()
    cmd = [str(wrapper_python), "-mpre_commit", "--version"] if wrapper_python else ["pre-commit", "--version"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    match = _VERSION_RE.search(proc.stdout)
    if not match:
        return None
    return (int(match[1]), int(match[2]), int(match[3]))


def main() -> int:
    floor = _declared_floor()
    if floor is None:
        return 0

    floor_str = ".".join(str(part) for part in floor)
    installed = _installed_version()
    if installed is None:
        print(f"ERROR: pre-commit not found or version unreadable; {floor_str} or newer is required.")
        print("Fix: pip install --user --upgrade pre-commit")
        return 1

    installed_str = ".".join(str(part) for part in installed)
    if installed < floor:
        print(f"ERROR: pre-commit {installed_str} is installed but {floor_str} or newer is required.")
        print(f"  Declared by minimum_pre_commit_version in {CONFIG.name}.")
        print("  Below the floor, `language: node` hooks install via npm with --ignore-prepublish,")
        print("  which npm >=12 rejects — every commit aborts, including commits touching no markdown.")
        print("Fix: pip install --user --upgrade pre-commit")
        return 1

    print(f"OK: pre-commit {installed_str} satisfies the {floor_str} floor.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
