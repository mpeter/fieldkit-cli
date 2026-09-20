#!/usr/bin/env python3
"""CI guard: fail if any commit message in a PR range contains PII.

The local ``pii-guard --commit-msg`` hook (see hooks/pii_guard.py) is
bypassable with ``git commit --no-verify``; this CI check is the non-bypassable
backstop. It scans every commit message in ``BASE_SHA..HEAD_SHA`` and fails if
any contains a real account slug, personal email, absolute home path, or Slack
handle — the same categories the redaction engine detects.

Reported output names only the *category* found (never the matched value), so
the CI log itself never re-leaks the PII — this reuses ``redact_text`` from
scripts/pii_patterns.py, which was hardened for exactly that historical gap.

Environment
-----------
    BASE_SHA — base commit of the PR range (required)
    HEAD_SHA — head commit of the PR range (required)

Exit codes
----------
    0 — no commit message contains PII (or the range is empty)
    1 — at least one commit message contains PII, or the range is unreadable
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR.parent / "src"))

from pii_patterns import redact_text, summarize_changes  # noqa: E402

_DELIM = "\x1e"  # ASCII record separator — will not appear in a commit message.


def _iter_commit_messages(base: str, head: str) -> list[tuple[str, str]]:
    """Return [(sha, message)] for each commit in base..head."""
    result = subprocess.run(
        ["git", "log", f"{base}..{head}", f"--format=%H%n%B%n{_DELIM}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        print(f"error: git log {base}..{head} failed: {result.stderr.strip()}", file=sys.stderr)
        raise SystemExit(1)

    commits: list[tuple[str, str]] = []
    for raw_block in result.stdout.split(_DELIM):
        block = raw_block.strip("\n")
        if not block:
            continue
        sha, _, message = block.partition("\n")
        commits.append((sha.strip(), message))
    return commits


def main() -> int:
    base = os.environ.get("BASE_SHA", "")
    head = os.environ.get("HEAD_SHA", "")
    if not base or not head:
        print("error: BASE_SHA and HEAD_SHA must both be set", file=sys.stderr)
        return 1

    commits = _iter_commit_messages(base, head)
    failed = False
    for sha, message in commits:
        _, changes = redact_text(message)
        if changes:
            failed = True
            categories = ", ".join(summarize_changes(changes))
            print(f"::error::commit {sha[:12]} message contains PII — {categories}")

    if failed:
        print(
            "\npii-guard: rewrite the offending commit message(s) to remove real "
            "account slugs / personal data, then force-push the branch. Use a "
            "placeholder (<account-slug>) or a fictional stand-in (acme-corp).",
            file=sys.stderr,
        )
        return 1

    print(f"pii-guard: {len(commits)} commit message(s) scanned, no PII found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
