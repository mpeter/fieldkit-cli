#!/usr/bin/env python3
"""Verify the active global ``fieldkit`` runtime includes Chrome authentication.

``uv tool install`` generates the ``fieldkit`` launcher with its tool-venv
Python in the shebang. This check follows that runtime rather than the project
environment, so it detects an out-of-date global installation.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def _runtime_for(launcher: Path) -> Path | None:
    """Return the Python runtime embedded in a generated ``fieldkit`` launcher."""
    try:
        first_line = launcher.resolve().read_text(encoding="utf-8").splitlines()[0]
    except (IndexError, OSError, UnicodeDecodeError):
        return None
    if not first_line.startswith("#!"):
        return None
    return Path(first_line.removeprefix("#!").split(maxsplit=1)[0])


def main() -> int:
    """Return nonzero when the global tool lacks the chrome-auth extra."""
    launcher_path = shutil.which("fieldkit")
    if launcher_path is None:
        print("ERROR: active global fieldkit launcher is not on PATH.", file=sys.stderr)
        print("Fix: make install", file=sys.stderr)
        return 1

    runtime = _runtime_for(Path(launcher_path))
    if runtime is None:
        print(f"ERROR: cannot identify the Python runtime for global fieldkit: {launcher_path}", file=sys.stderr)
        print("Fix: make install", file=sys.stderr)
        return 1

    try:
        check = subprocess.run(
            [
                str(runtime),
                "-c",
                "import cryptography; import secretstorage; from fieldkit.shadowbot.auth import _HAS_CHROME_AUTH; assert _HAS_CHROME_AUTH",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"ERROR: global fieldkit Chrome auth check could not run: {exc}", file=sys.stderr)
        print("Fix: make install", file=sys.stderr)
        return 1

    if check.returncode != 0:
        print(
            "ERROR: active global fieldkit runtime lacks chrome-auth requirements (cryptography, secretstorage).",
            file=sys.stderr,
        )
        if check.stderr:
            print(check.stderr.strip(), file=sys.stderr)
        print("Fix: make install", file=sys.stderr)
        return 1

    print(f"OK: global fieldkit runtime {runtime} includes chrome-auth requirements (cryptography, secretstorage).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
