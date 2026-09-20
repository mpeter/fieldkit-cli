#!/usr/bin/env python3
"""Validate the structure of fieldkit's public planned-work ledger."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ROADMAP_PATH = Path("ROADMAP.md")
_SECTIONS = {
    "Release safety and future delivery": 5,
    "Product reliability and integrations": 9,
    "Architecture, quality, and contributor experience": 9,
    "Compatibility candidates": 2,
    "Community growth": 1,
}
_STATUSES = frozenset({"In progress", "Pending", "Backlog"})


def _table_status(line: str) -> str | None:
    """Return a data-row status, excluding Markdown table headers and dividers."""
    if not line.startswith("|") or line.startswith("| ---"):
        return None
    cells = [cell.strip() for cell in line.split("|")]
    return None if len(cells) < 4 or cells[1] == "Status" else cells[1]


def validate(path: Path) -> tuple[str, ...]:
    """Return all deterministic roadmap-structure violations in source order."""
    if not path.is_file():
        return (f"missing roadmap: {path}",)
    section: str | None = None
    counts = dict.fromkeys(_SECTIONS, 0)
    findings: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            section = line.removeprefix("## ")
            continue
        status = _table_status(line)
        if status is None:
            continue
        if section not in _SECTIONS:
            findings.append(f"unowned roadmap table row: {status}")
            continue
        counts[section] += 1
        if status not in _STATUSES:
            findings.append(f"unknown status: {status}")
    for name, expected_count in _SECTIONS.items():
        observed_count = counts[name]
        if observed_count != expected_count:
            findings.append(f"{name}: expected {expected_count} rows, found {observed_count}")
    return tuple(findings)


def main(argv: list[str] | None = None) -> int:
    """Render stable success or fail-closed structural-validation output."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roadmap", type=Path, default=_REPO_ROOT / _ROADMAP_PATH)
    args = parser.parse_args(argv)
    try:
        findings = validate(args.roadmap)
    except OSError as error:
        print(f"Roadmap contract: ERROR: {error}", file=sys.stderr)
        return 2
    if findings:
        print(f"Roadmap contract: FAIL ({len(findings)} finding(s))")
        for finding in findings:
            print(f"  {finding}")
        return 1
    print("Roadmap contract: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
