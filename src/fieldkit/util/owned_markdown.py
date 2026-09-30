"""CommonMark ownership of named sections and top-level bullet lines."""

import re
from dataclasses import dataclass

from markdown_it import MarkdownIt


@dataclass(frozen=True)
class OwnedLine:
    """One first-line bullet and its final parsed HTML inline token."""

    section: str
    final_html: str


@dataclass(frozen=True)
class OwnedDocument:
    """Source offsets for writable sections and actual top-level bullet lines."""

    section_ends: dict[str, list[int]]
    bullet_lines: dict[int, OwnedLine]


@dataclass(frozen=True)
class OwnedMarker:
    """One effect's saved meaning and its actual document section."""

    fingerprint: str
    section: str


def read_owned_markers(
    content: str, *, prefix: str, section_titles: tuple[str, ...], error_message: str
) -> dict[str, OwnedMarker]:
    """Accept only unique v1 digest markers on visible top-level bullet entries."""
    pattern = re.compile(rf"[-+*] .+ (<!-- {re.escape(prefix)}v1:([a-f0-9]{{64}}):([a-f0-9]{{64}}) -->)")
    document = inspect_owned_markdown(content, section_titles=section_titles)
    markers: dict[str, OwnedMarker] = {}
    for index, line in enumerate(content.splitlines()):
        if prefix not in line:
            continue
        match = pattern.fullmatch(line)
        bullet = document.bullet_lines.get(index)
        text = re.sub(r"^[-+*] (?:\[[ xX]\] )?", "", line.split("<!--", 1)[0]).strip()
        if (
            match is None
            or bullet is None
            or not text
            or line.count(prefix) != 1
            or bullet.final_html != match[1]
            or match[2] in markers
        ):
            raise ValueError(error_message)
        markers[match[2]] = OwnedMarker(match[3], bullet.section)
    return markers


def inspect_owned_markdown(content: str, *, section_titles: tuple[str, ...]) -> OwnedDocument:
    """Exclude examples, nested lists, quotations, and HTML blocks from ownership."""
    tokens = MarkdownIt("commonmark").parse(content)
    offsets = [0]
    for line in content.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    sections: dict[str, list[int]] = {title: [] for title in section_titles}
    bullet_lines: dict[int, OwnedLine] = {}
    section = ""
    first_line: int | None = None
    for index, token in enumerate(tokens):
        if token.map is None:
            continue
        if token.type == "heading_open" and token.tag in {"h1", "h2"} and token.level == 0:
            title = tokens[index + 1].content
            section = title if token.tag == "h2" and title in sections else ""
            if section:
                sections[title].append(offsets[token.map[1]])
                if len(sections[title]) > 1:
                    raise ValueError("Ambiguous Markdown section")
        if token.type == "list_item_open" and token.level == 1:
            first_line = token.map[0] if token.markup in {"-", "*", "+"} else None
        if (
            section
            and first_line is not None
            and token.type == "inline"
            and token.level == 3
            and token.map == [first_line, first_line + 1]
        ):
            children = token.children or []
            final_html = children[-1].content if children and children[-1].type == "html_inline" else ""
            bullet_lines[token.map[0]] = OwnedLine(section, final_html)
    return OwnedDocument(sections, bullet_lines)
