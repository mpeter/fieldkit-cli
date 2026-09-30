"""Contracts for canonical public Markdown table discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.markdown_tables import MAX_DOCUMENT_BYTES, markdown_tables, parse_markdown_tables

pytestmark = pytest.mark.unit


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_outer_pipe_and_outer_pipeless_tables_share_one_grammar(tmp_path: Path) -> None:
    document = _write(
        tmp_path / "tables.md",
        "| A | B |\n|---|---|\n| x | y |\n\nC | D\n---|---\nz | w\n",
    )

    tables = markdown_tables(document)

    assert [(table.header, table.rows) for table in tables] == [
        (("A", "B"), (("x", "y"),)),
        (("C", "D"), (("z", "w"),)),
    ]
    assert tables[0].source_text == "| A | B |\n|---|---|\n| x | y |\n"
    assert tables[1].source_text == "C | D\n---|---\nz | w\n"
    assert (tables[0].start_line, tables[0].end_line) == (0, 3)
    assert (tables[1].start_line, tables[1].end_line) == (4, 7)


def test_containers_escapes_and_fenced_templates_are_discovered(tmp_path: Path) -> None:
    document = _write(
        tmp_path / "containers.md",
        "> | A | B |\n> |---|---|\n> | x \\| detail | y |\n\n"
        "- C | D\n  ---|---\n  z | w\n\n"
        "```text\nE | F\n---|---\nq | r\n```\n",
    )

    tables = markdown_tables(document)

    assert len(tables) == 3
    assert tables[0].rows == (("x | detail", "y"),)
    assert tables[1].header == ("C", "D")
    assert tables[2].rows == (("q", "r"),)
    assert tables[2].source_text == "E | F\n---|---\nq | r\n"


@pytest.mark.parametrize(
    "text",
    [
        "| A | B |\n|---|---|\n| only one |\n",
        "| A | B |\n|---|---|\n| x | y | extra |\n",
        "| A | B |\n|--|---|\n| x | y |\n",
        "| A | A |\n|---|---|\n| x | y |\n",
        "| | B |\n|---|---|\n| x | y |\n",
        "A | B\n--- | nope\nx | y\n",
        "A | B\n--- | --- | nope\nx | y | z\n",
        "A | B\n-- | --\nx | y\n",
        "A | B\n-- | nope\nx | y\n",
    ],
)
def test_malformed_table_like_input_fails_closed(tmp_path: Path, text: str) -> None:
    document = _write(tmp_path / "malformed.md", text)

    with pytest.raises(ValueError, match="Markdown table"):
        markdown_tables(document)


def test_bounds_and_utf8_are_enforced_before_structure_is_trusted(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized.md"
    oversized.write_bytes(b"x" * (MAX_DOCUMENT_BYTES + 1))
    invalid = tmp_path / "invalid.md"
    invalid.write_bytes(b"A | B\n---|---\n\xff | y\n")
    too_many_rows = _write(
        tmp_path / "rows.md",
        "A | B\n---|---\n" + "x | y\n" * 65,
    )

    with pytest.raises(ValueError, match="byte bound"):
        markdown_tables(oversized)
    with pytest.raises(ValueError, match="UTF-8"):
        markdown_tables(invalid)
    with pytest.raises(ValueError, match="row bound"):
        markdown_tables(too_many_rows)


def test_path_reader_rejects_symlinks_and_nonregular_sources(tmp_path: Path) -> None:
    target = _write(tmp_path / "private.md", "A | B\n---|---\nx | y\n")
    redirect = tmp_path / "redirect.md"
    redirect.symlink_to(target)

    with pytest.raises(ValueError, match="unavailable"):
        markdown_tables(redirect)
    with pytest.raises(ValueError, match="regular file"):
        markdown_tables(tmp_path)


def test_text_parser_preserves_crlf_source_and_positions() -> None:
    text = "intro\r\nA | B\r\n---|---\r\nx | y\r\n"

    table = parse_markdown_tables(text)[0]

    assert table.source_text == "A | B\r\n---|---\r\nx | y\r\n"
    assert (table.start_line, table.end_line) == (1, 4)
