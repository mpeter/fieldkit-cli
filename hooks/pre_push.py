#!/usr/bin/env python3
"""Pre-push hook: block direct pushes to main.

Rule source: AGENTS.md — "MUST NOT commit directly to main.
All changes via feature branches and PRs."

This is a git pre-push hook (exit 1 = block push).
NOT a Claude Code hook (which uses exit 2).

Git passes the remote name and URL as argv[1] and argv[2].
Stdin contains lines of:  <local-ref> <local-sha1> <remote-ref> <remote-sha1>

We block any push where remote-ref is refs/heads/main.
Bypass: git push --no-verify  (acknowledged bypass — no GitHub enforcement)
"""

import sys

_PROTECTED = "refs/heads/main"
_RULE = "AGENTS.md: MUST NOT commit directly to main — all changes via feature branches and PRs."


def main() -> int:
    # argv[1] = remote name, argv[2] = remote URL (not used — we check ref names)
    blocked: list[str] = []

    for line in sys.stdin:
        parts = line.strip().split()
        if len(parts) < 4:
            continue
        _local_ref, _local_sha, remote_ref, _remote_sha = parts[:4]

        if remote_ref == _PROTECTED:
            blocked.append(_local_ref or "(HEAD)")

    if blocked:
        print("pre-push: direct push to main is not allowed.", file=sys.stderr)
        print(f"  {_RULE}", file=sys.stderr)
        print("", file=sys.stderr)
        print("  Open a PR instead:", file=sys.stderr)
        print("    git checkout -b your-branch-name", file=sys.stderr)
        print("    git push origin your-branch-name", file=sys.stderr)
        print("    gh pr create", file=sys.stderr)
        print("", file=sys.stderr)
        print("  To bypass (emergency only): git push --no-verify", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
