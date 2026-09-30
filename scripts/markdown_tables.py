"""Bounded, fail-closed GitHub-Flavored Markdown table discovery."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

MAX_DOCUMENT_BYTES = 128 * 1_024
MAX_TABLE_COLUMNS = 12
MAX_TABLE_ROWS = 64
MAX_TABLE_CELL_CHARACTERS = 1_024

_CONTAINER_PREFIX = re.compile(r"(?: {0,3}>[ \t]?| {0,3}(?:[-+*]|\d+[.)])[ \t]+)")
_SEPARATOR_CELL = re.compile(r":?-{3,}:?")
_HYPHEN_CELL = re.compile(r":?-+:?")
_FENCE_DELIMITER = re.compile(r" {0,3}(?:`{3,}|~{3,})")


@dataclass(frozen=True)
class MarkdownTable:
    """One complete table and the exact source bytes represented as text."""

    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    source_text: str
    start_line: int
    end_line: int


def _content_line(line: str) -> str:
    """Remove Markdown container markers without changing cell content."""
    content = line.rstrip("\r\n")
    while True:
        match = _CONTAINER_PREFIX.match(content)
        if match is None:
            return content.lstrip()
        content = content[match.end() :]


def _pipe_positions(line: str) -> tuple[int, ...]:
    positions: list[int] = []
    backslashes = 0
    for index, character in enumerate(line):
        if character == "\\":
            backslashes += 1
            continue
        if character == "|" and backslashes % 2 == 0:
            positions.append(index)
        backslashes = 0
    return tuple(positions)


def _cells(line: str) -> tuple[str, ...]:
    content = _content_line(line).strip()
    positions = _pipe_positions(content)
    if not positions:
        raise ValueError("Markdown table row must contain an unescaped pipe")
    boundaries = (-1, *positions, len(content))
    cells = [content[boundaries[index] + 1 : boundaries[index + 1]] for index in range(len(boundaries) - 1)]
    if cells and not cells[0].strip():
        cells.pop(0)
    if cells and not cells[-1].strip():
        cells.pop()
    normalized = tuple(cell.strip().replace(r"\|", "|") for cell in cells)
    if not normalized or len(normalized) > MAX_TABLE_COLUMNS:
        raise ValueError("Markdown table column bound exceeded")
    if any(len(cell) > MAX_TABLE_CELL_CHARACTERS for cell in normalized):
        raise ValueError("Markdown table cell bound exceeded")
    return normalized


def _separator_candidate(line: str) -> bool:
    """Return whether a line is shaped like an attempted delimiter row."""
    if "|" not in _content_line(line):
        return False
    try:
        cells = _cells(line)
    except ValueError:
        return False
    return any(_HYPHEN_CELL.fullmatch(cell) for cell in cells)


def parse_markdown_tables(text: str) -> tuple[MarkdownTable, ...]:
    """Return every complete table from one bounded Markdown string.

    Outer pipes are optional. Block-quote, list, and fenced example content is
    inspected with the same grammar. A table-like delimiter or row that cannot
    be represented with the header's exact column count is rejected rather than
    omitted from the ownership inventory.
    """
    try:
        raw = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Markdown table source must be UTF-8") from exc
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ValueError("Markdown table source exceeds the byte bound")
    lines = text.splitlines(keepends=True)

    tables: list[MarkdownTable] = []
    index = 0
    while index + 1 < len(lines):
        header_line = _content_line(lines[index])
        if "|" not in header_line or not _separator_candidate(lines[index + 1]):
            index += 1
            continue
        try:
            header = _cells(lines[index])
            separator = _cells(lines[index + 1])
        except ValueError as exc:
            raise ValueError(f"Malformed Markdown table at line {index + 1}") from exc
        if not all(header):
            raise ValueError(f"Markdown table header is empty at line {index + 1}")
        if len(set(header)) != len(header):
            raise ValueError(f"Markdown table header is duplicated at line {index + 1}")
        if len(separator) != len(header) or not all(_SEPARATOR_CELL.fullmatch(cell) for cell in separator):
            raise ValueError(f"Malformed Markdown table separator at line {index + 2}")

        start = index
        index += 2
        rows: list[tuple[str, ...]] = []
        while index < len(lines):
            content = _content_line(lines[index])
            if not content.strip() or _FENCE_DELIMITER.fullmatch(content.strip()):
                break
            if "|" not in content:
                raise ValueError(f"Malformed Markdown table row at line {index + 1}")
            try:
                row = _cells(lines[index])
            except ValueError as exc:
                raise ValueError(f"Malformed Markdown table row at line {index + 1}") from exc
            if len(row) != len(header):
                raise ValueError(f"Markdown table row column count differs from header at line {index + 1}")
            rows.append(row)
            if len(rows) > MAX_TABLE_ROWS:
                raise ValueError("Markdown table row bound exceeded")
            index += 1
        tables.append(
            MarkdownTable(
                header=header,
                rows=tuple(rows),
                source_text="".join(lines[start:index]),
                start_line=start,
                end_line=index,
            )
        )
    return tuple(tables)


def markdown_tables(path: Path) -> tuple[MarkdownTable, ...]:
    """Read one bounded UTF-8 Markdown file and return its complete tables."""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ValueError("Markdown table source cannot be opened safely")
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | nofollow)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Markdown table source must be a regular file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            raw = stream.read(MAX_DOCUMENT_BYTES + 1)
    except OSError as exc:
        raise ValueError("Markdown table source is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ValueError("Markdown table source exceeds the byte bound")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Markdown table source must be UTF-8") from exc
    return parse_markdown_tables(text)
