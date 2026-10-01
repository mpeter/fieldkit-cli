#!/usr/bin/env python3
"""Require a Conventional Commits pull-request title.

The repository squash-merges with the PR title as the commit subject, so the
title is what lands on ``main``. The local ``commit-msg`` hook never sees that
commit, so CI checks the title instead. The allowed types are read from the
``conventional-pre-commit`` hook in ``.pre-commit-config.yaml`` so the two gates
cannot drift.

Usage:
    PR_TITLE="fix(web): drop the stale stage" python3 scripts/check_pr_title.py

Exit 0: the title is a valid Conventional Commits subject.
Exit 1: the title is missing or malformed.
Exit 3: the allowed types could not be read from the pre-commit config.
"""

import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PRE_COMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"
_HOOK_ID = "conventional-pre-commit"


def allowed_types(config: Path | None = None) -> tuple[str, ...]:
    """Return the commit types the ``conventional-pre-commit`` hook accepts.

    Stdlib only, so the workflow needs no dependency install: find the hook's
    ``id`` line, then the first ``args: [...]`` list after it.

    Raises:
        ValueError: If the hook or its ``args`` list is absent.
    """
    config = PRE_COMMIT_CONFIG if config is None else config
    lines = config.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.strip() == f"- id: {_HOOK_ID}":
            for candidate in lines[index + 1 :]:
                stripped = candidate.strip()
                if stripped.startswith("- id:"):
                    break
                match = re.fullmatch(r"args:\s*\[(.*)\]", stripped)
                if match:
                    types = tuple(item.strip() for item in match.group(1).split(",") if item.strip())
                    if types:
                        return types
            break
    raise ValueError(f"{config.name} has no args list for the {_HOOK_ID} hook")


def title_error(title: str, types: tuple[str, ...]) -> str | None:
    """Return why ``title`` is not a Conventional Commits subject, or ``None`` if it is."""
    pattern = rf"(?:{'|'.join(map(re.escape, types))})(?:\([^()\s]+\))?!?: \S.*"
    if not title.strip():
        return "the pull-request title is empty"
    if re.fullmatch(pattern, title) is None:
        return (
            f"{title!r} is not a Conventional Commits subject; "
            f"use '<type>[(scope)][!]: <description>' with a type from: {', '.join(types)}"
        )
    return None


def main() -> int:
    """Check ``PR_TITLE`` and report the result."""
    try:
        types = allowed_types()
    except (OSError, ValueError) as exc:
        print(f"PR title check: cannot read allowed types: {exc}", file=sys.stderr)
        return 3
    error = title_error(os.environ.get("PR_TITLE", ""), types)
    if error is not None:
        print(f"PR title check: FAIL: {error}", file=sys.stderr)
        return 1
    print("PR title check: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
