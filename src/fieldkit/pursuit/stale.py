#!/usr/bin/env python3
"""stale-prose-check.py — Detect contradictions between frontmatter and prose body.

Scans pursuit files and prints warnings when frontmatter facts contradict
body text. Used by morning-brief.sh and the update skill to surface stale prose.

Usage:
    python3 -m fieldkit.lib.stale_prose_check [<pursuit-file.md> ...]
    python3 -m fieldkit.lib.stale_prose_check accounts/acme-bank/pursuits/

Exits 0 on success, 1 if no arguments provided (warnings go to stdout for pipeline capture).
"""

import re
import sys
from collections.abc import Callable
from pathlib import Path

from fieldkit.errors import PursuitStaleError
from fieldkit.pursuit.io import extract_frontmatter_text

# Each check: (description, frontmatter_condition_fn, prose_pattern)
# frontmatter_condition_fn(fm) → True means the fm state makes the prose stale.
# prose_pattern is a regex matched against the body (case-insensitive).
CHECKS: list[tuple[str, Callable[[dict[str, str]], bool], str]] = [
    (
        "SF opportunity ID exists but prose says 'no SF opportunity'",
        lambda fm: bool(fm.get("sf_opportunity_id")),
        r"no\s+SF\s+opportunity|needs\s+to\s+be\s+created|not\s+yet\s+in\s+Salesforce|pipeline\s+\(no\s+SF\s+opp",
    ),
    (
        "sf_arr is set but prose says ACV is unknown or needed",
        lambda fm: bool(fm.get("sf_arr") and fm["sf_arr"] not in ("", "[DATA NEEDED]")),
        r"ACV.*\[DATA\s+NEEDED\]|\[DATA\s+NEEDED\].*ACV|ACV\s+unknown|ACV:\s*\[TBD",
    ),
    (
        "sf_close_date is set but prose says close date is TBD",
        lambda fm: bool(fm.get("sf_close_date") and fm["sf_close_date"] not in ("", "[TBD")),
        r"Close\s+Date.*\[TBD|\[TBD.*close\s+date",
    ),
    (
        "sf_stage is set but prose says 'Pipeline (no SF opp)'",
        lambda fm: bool(fm.get("sf_stage")),
        r"Pipeline\s+\(no\s+SF\s+opp",
    ),
]


def _parse_frontmatter(text: str) -> dict[str, str]:
    fm_text = extract_frontmatter_text(text)
    if not fm_text:
        return {}
    result = {}
    for line in fm_text.splitlines():
        if ":" in line and not line.startswith(" "):
            key, _, val = line.partition(":")
            result[key.strip()] = val.strip().strip('"').strip("'")
    return result


def _get_body(text: str) -> str:
    """Return text after the closing --- of frontmatter."""
    m = re.match(r"^---\n.*?\n---\n", text, re.DOTALL)
    if m:
        return text[m.end() :]
    return text


def check_file(path: str) -> list[str]:
    """Return list of warning strings for this file. Empty = no issues."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return [f"ERROR reading {path}"]

    fm = _parse_frontmatter(text)
    if not fm:
        return []

    body = _get_body(text)
    warnings = []

    for description, fm_condition, prose_pattern in CHECKS:
        if fm_condition(fm) and re.search(prose_pattern, body, re.IGNORECASE):
            warnings.append(f"  ⚠ {description}")

    return warnings


def main() -> None:
    """CLI entry point. Accepts file or directory arguments and prints stale-prose warnings."""
    args = sys.argv[1:]
    if not args:
        print("Usage: stale-prose-check.py <file.md|dir> [...]", file=sys.stderr)
        raise PursuitStaleError("No arguments provided — usage: stale-prose-check.py <file.md|dir> [...]")

    paths = []
    for arg in args:
        p = Path(arg)
        if p.is_dir():
            paths.extend(sorted(p.glob("*.md")))
        elif p.is_file():
            paths.append(p)
        else:
            # glob pattern
            paths.extend(Path().glob(arg))

    # Exclude template and gmail-intel files
    paths = [p for p in paths if p.stem not in ("template", "gmail-intel")]

    found_any = False
    for path in paths:
        warnings = check_file(str(path))
        if warnings:
            if not found_any:
                print("STALE PROSE WARNINGS — frontmatter contradicts body text:")
            print(f"\n{path}:")
            for w in warnings:
                print(w)
            found_any = True

    if not found_any:
        print("No stale prose contradictions found.")


if __name__ == "__main__":
    main()
