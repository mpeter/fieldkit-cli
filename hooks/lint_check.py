#!/usr/bin/env python3
"""Wrapper around 'uv run ruff check --fix --unsafe-fixes' for pre-commit.

Pre-commit truncates hook failure output in error summaries (only the first
stderr line is shown). This wrapper prepends a clear one-liner that names
the failing tool and the first flagged file, so any summary view is useful.

Usage (in .pre-commit-config.yaml):
    entry: uv run python hooks/lint_check.py
"""

import re
import subprocess
import sys

# Matches ruff's "  --> path/to/file.py:line:col" location lines.
_RUFF_LOCATION = re.compile(r"-->\s+(\S+\.py):\d+")


def _first_flagged_file(output: str) -> str:
    for line in output.splitlines():
        m = _RUFF_LOCATION.search(line)
        if m:
            return m.group(1)
    return ""


def main() -> int:
    result = subprocess.run(
        ["uv", "run", "ruff", "check", "--fix", "--unsafe-fixes", *sys.argv[1:]],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    if result.returncode != 0:
        first_file = _first_flagged_file(result.stdout)
        summary = f"ruff-check failed in {first_file}" if first_file else "ruff-check failed"
        print(summary, file=sys.stderr)
        if result.stdout:
            print(result.stdout, file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        return result.returncode

    if result.stdout:
        print(result.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
