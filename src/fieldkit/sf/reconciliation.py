"""Pure reconciliation of Salesforce values into a pursuit's Key Fields table."""

import re
from collections.abc import Mapping
from dataclasses import dataclass

FIELD_MAP = {"stage": "sf_stage", "close date": "sf_close_date", "acv": "sf_arr"}
_SECTION_RE = re.compile(
    r"(^## Key Fields[^\n]*\n)(?:(?!^## ).)*?(\| Field[^\n]*\n[^\n]*\n)((?:\|[^\n]*\n)+)",
    re.MULTILINE | re.DOTALL,
)


@dataclass(frozen=True)
class TableChange:
    """One rendered table cell change, available to CLI adapters for display."""

    field: str
    previous: str
    updated: str


@dataclass(frozen=True)
class TableReconciliation:
    """Rewritten body and structured changes, with no I/O or output side effects."""

    body: str
    changes: tuple[TableChange, ...]


def reconcile_key_fields(body: str, frontmatter: Mapping[str, object]) -> TableReconciliation:
    """Update mapped nonempty scalar cells without changing unmapped rows."""
    match = _SECTION_RE.search(body)
    if match is None:
        return TableReconciliation(body, ())
    rows: list[str] = []
    changes: list[TableChange] = []
    for row in match.group(3).splitlines(keepends=True):
        cells = [part.strip() for part in row.strip().strip("|").split("|")]
        if len(cells) != 2:
            rows.append(row)
            continue
        value = frontmatter.get(FIELD_MAP.get(cells[0].lower(), ""))
        if value is None or isinstance(value, (dict, list, tuple)) or value == "":
            rows.append(row)
            continue
        rendered = str(value).replace("\r", " ").replace("\n", " ").replace("|", "&#124;")
        if rendered == cells[1]:
            rows.append(row)
            continue
        changes.append(TableChange(cells[0], cells[1], rendered))
        rows.append(f"| {cells[0]} | {rendered} |\n")
    rewritten = body[: match.start(3)] + "".join(rows) + body[match.end(3) :]
    return TableReconciliation(rewritten, tuple(changes))
