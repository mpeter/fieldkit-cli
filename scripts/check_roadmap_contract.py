#!/usr/bin/env python3
"""Validate the structure of fieldkit's public planned-work ledger."""

from __future__ import annotations

import argparse
import sys
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING

from markdown_it import MarkdownIt

if TYPE_CHECKING or __package__:
    from scripts.markdown_tables import MAX_DOCUMENT_BYTES, MarkdownTable, parse_markdown_tables
else:
    from markdown_tables import MAX_DOCUMENT_BYTES, MarkdownTable, parse_markdown_tables

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ROADMAP_PATH = Path("ROADMAP.md")
_SECTIONS = {
    "Release safety and future delivery": 13,
    "Product reliability and integrations": 20,
    "Architecture, quality, and contributor experience": 12,
    "Compatibility candidates": 2,
    "Community growth": 1,
}
_STATUSES = frozenset({"In progress", "Pending", "Backlog"})


def _read_roadmap(path: Path) -> str:
    """Read a bounded UTF-8 source and reject known symlink inputs."""
    if any(component.is_symlink() for component in (path, *path.parents)):
        raise ValueError("roadmap source must not use symlinks")
    with path.open("rb") as source:
        raw = source.read(MAX_DOCUMENT_BYTES + 1)
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ValueError("roadmap source exceeds the byte bound")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("roadmap source must be UTF-8") from error


def _table_findings(table: MarkdownTable, section: str, outcomes: set[str]) -> tuple[str, ...]:
    """Require the reviewed columns, statuses, row total, and unique outcomes."""
    if table.header != ("Status", "Outcome"):
        return (f"{section}: roadmap table must use Status and Outcome columns",)
    findings: list[str] = []
    if len(table.rows) != _SECTIONS[section]:
        findings.append(f"{section}: expected {_SECTIONS[section]} rows, found {len(table.rows)}")
    for status, outcome in table.rows:
        if status not in _STATUSES:
            findings.append("unknown status")
        tokens = MarkdownIt("commonmark").parseInline(outcome)[0].children or []
        if any(token.type == "html_inline" for token in tokens):
            findings.append("roadmap outcome must not contain hidden HTML")
        visible = "".join(token.content for token in tokens if token.type in {"text", "code_inline"})
        if any(unicodedata.category(character).startswith("C") for character in visible):
            findings.append("roadmap outcome must not contain invisible control or format characters")
        normalized = " ".join(visible.split()).casefold()
        if not any(character.isalnum() for character in normalized):
            findings.append("roadmap outcome must be nonempty")
        elif normalized in outcomes:
            findings.append("duplicate outcome")
        outcomes.add(normalized)
    return tuple(findings)


def validate(path: Path) -> tuple[str, ...]:
    """Return all deterministic roadmap-structure violations in source order."""
    if not path.is_file():
        return ("missing roadmap",)
    text = _read_roadmap(path)
    lines = text.splitlines()
    if not lines or lines[0] != "# fieldkit roadmap":
        return ("roadmap requires its canonical title",)
    if any(token.type in {"fence", "code_block", "html_block"} for token in MarkdownIt("commonmark").parse(text)):
        return ("roadmap must not hide structured content in code or HTML",)
    try:
        tables = parse_markdown_tables(text)
    except ValueError:
        return ("malformed roadmap table",)
    headings = [(index, line.removeprefix("## ")) for index, line in enumerate(lines) if line.startswith("## ")]
    findings: list[str] = []
    if tuple(section for _, section in headings) != tuple(_SECTIONS):
        findings.append("sections must be unique and in the reviewed order")
    first_heading = headings[0][0] if headings else len(lines)
    if any(table.start_line < first_heading for table in tables):
        findings.append("unowned roadmap table")
    outcomes: set[str] = set()
    for position, (start, section) in enumerate(headings):
        end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        if section not in _SECTIONS:
            findings.append("unowned roadmap section")
            continue
        owned = [table for table in tables if start < table.start_line and table.end_line <= end]
        if len(owned) != 1:
            findings.append(f"{section}: requires exactly one table")
        else:
            findings.extend(_table_findings(owned[0], section, outcomes))
        covered = {index for table in owned for index in range(table.start_line, table.end_line)}
        if any(lines[index].strip() and index not in covered for index in range(start + 1, end)):
            findings.append(f"{section}: unowned section content")
    return tuple(findings)


def main(argv: list[str] | None = None) -> int:
    """Render stable success or fail-closed structural-validation output."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roadmap", type=Path, default=_REPO_ROOT / _ROADMAP_PATH)
    args = parser.parse_args(argv)
    try:
        findings = validate(args.roadmap)
    except (OSError, ValueError):
        print("Roadmap contract: ERROR: unable to read a bounded UTF-8 roadmap", file=sys.stderr)
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
