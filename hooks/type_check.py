#!/usr/bin/env python3
"""Wrapper around 'uv run mypy' for pre-commit.

Pre-commit truncates hook failure output in error summaries (only the first
stderr line is shown). This wrapper prepends a clear one-liner that names
the failing tool and the first flagged file, so any summary view is useful.

Usage (in .pre-commit-config.yaml):
    entry: uv run python hooks/type_check.py
"""

import subprocess
import sys


def main() -> int:
    result = subprocess.run(
        [
            "uv",
            "run",
            "mypy",
            "src/fieldkit/",
            "hooks/",
            "--no-error-summary",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    if result.returncode != 0:
        # Extract the first flagged file from mypy output for the summary line.
        # mypy output format: "path/to/file.py:line: error: message  [code]"
        first_file = ""
        for line in result.stdout.splitlines():
            if ": error:" in line:
                first_file = line.split(":")[0].strip()
                break

        summary = "mypy failed"
        if first_file:
            summary = f"mypy failed in {first_file}"
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
