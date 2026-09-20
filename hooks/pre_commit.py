#!/usr/bin/env python3
"""Pre-commit hook: block staged .sh files.

This is a git pre-commit hook helper (exit 1 = block commit).
NOT a Claude Code hook (which uses exit 2).

Usage: called automatically by .git/hooks/pre-commit
"""

import subprocess
import sys


def main() -> int:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        print(f"pre_commit.py: git diff failed: {result.stderr}", file=sys.stderr)
        return 1

    staged = [f.strip() for f in result.stdout.splitlines() if f.strip()]
    offenders = [f for f in staged if f.endswith(".sh")]

    if offenders:
        print(
            "pre-commit: .sh files are not allowed in this codebase. All scripts must be Python.",
            file=sys.stderr,
        )
        for path in offenders:
            print(f"  blocked: {path}", file=sys.stderr)
        print(
            "pre-commit: Remove or convert to Python, then re-stage.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
