"""Rewrite the Key Fields table in a pursuit file from frontmatter values.

Updates only rows that map to known sf_* frontmatter fields:
  Stage      -> sf_stage
  Close Date -> sf_close_date
  ACV        -> sf_arr

Rows the script cannot map are left untouched.

Usage via CLI:
    python -m sf_pipeline reconcile <pursuit-file.md>
"""

import json
import logging
import re
import tempfile
from pathlib import Path

import click
import yaml

from fieldkit.cli_exit import EXIT_PARTIAL
from fieldkit.cli_registry import declare_write
from fieldkit.pursuit.io import extract_frontmatter_text, load_pursuit

log = logging.getLogger(__name__)

FIELD_MAP = {
    "stage": "sf_stage",
    "close date": "sf_close_date",
    "acv": "sf_arr",
}

_SECTION_RE = re.compile(
    r"(## Key Fields[^\n]*\n)"
    r"(.*?)"
    r"(\| Field[^\n]*\n[^\n]*\n)"
    r"((?:\|[^\n]*\n)+)",
    re.DOTALL,
)


def _parse_frontmatter(text: str) -> dict[str, str]:
    fm_text = extract_frontmatter_text(text)
    if not fm_text:
        return {}
    try:
        raw = yaml.safe_load(fm_text)
    except yaml.YAMLError:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        k: str(v) for k, v in raw.items() if isinstance(k, str) and v is not None and not isinstance(v, (dict, list))
    }


def _rewrite_key_fields_table(text: str, fm: dict[str, str]) -> tuple[str, int]:
    match = _SECTION_RE.search(text)
    if not match:
        return text, 0

    header = match.group(1)
    pre_table = match.group(2)
    table_header = match.group(3)
    table_rows_block = match.group(4)

    rows = table_rows_block.splitlines(keepends=True)

    parsed_rows: list[tuple[str, list[str], str | None, str | None]] = []
    col1_w = 0
    col2_w = 0
    for row in rows:
        parts = [p.strip() for p in row.strip().strip("|").split("|")]
        if len(parts) < 2:
            parsed_rows.append((row, parts, None, None))
            continue
        field_label = parts[0].strip().lower()
        fm_key = FIELD_MAP.get(field_label)
        new_value = fm.get(fm_key, "") if fm_key else None
        if not new_value:
            new_value = None
        col1_w = max(col1_w, len(parts[0]))
        col2_w = max(col2_w, len(parts[1]), len(new_value) if new_value else 0)
        parsed_rows.append((row, parts, fm_key, new_value))

    header_line = table_header.split("\n")[0] if "\n" in table_header else table_header
    header_parts = [p.strip() for p in header_line.strip().strip("|").split("|")]
    if len(header_parts) >= 2:
        col1_w = max(col1_w, len(header_parts[0]))
        col2_w = max(col2_w, len(header_parts[1]))

    new_rows = []
    changes = 0

    for row, parts, fm_key, new_value in parsed_rows:
        if len(parts) < 2 or fm_key is None or new_value is None:
            new_rows.append(row)
            continue
        old_value = parts[1].strip()
        if new_value == old_value:
            new_rows.append(row)
            continue
        new_row = f"| {parts[0]:<{col1_w}} | {new_value:<{col2_w}} |\n"
        new_rows.append(new_row)
        changes += 1
        click.echo(f"  {parts[0]}: {old_value!r} → {new_value!r}", err=True)

    new_block = header + pre_table + table_header + "".join(new_rows)
    new_text = text[: match.start()] + new_block + text[match.end() :]
    return new_text, changes


def _write_atomic(path: str, content: str) -> None:
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=Path(path).resolve().parent, delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    Path(tmp_path).replace(path)


# ---------------------------------------------------------------------------
# Dry-run mode (historic regression) — preview without writing
# ---------------------------------------------------------------------------


def _run_reconcile_dry(path: str) -> str:
    """Dry-run: show what would be changed without writing any files.

    Returns the same status strings as _run_reconcile: "updated", "ok", "skip".
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        click.echo(f"NOT FOUND: {path}", err=True)
        return "error"

    try:
        model, _, _ = load_pursuit(path)
        fm: dict[str, str] = {
            "sf_stage": str(model.sf_stage) if model.sf_stage else "",
            "sf_close_date": str(model.sf_close_date) if model.sf_close_date else "",
            "sf_arr": str(model.sf_arr) if model.sf_arr else "",
        }
    except Exception:  # noqa: BLE001
        # WARNING, not DEBUG: the CLI sets basicConfig(level=INFO) in __main__.py and
        # reconcile has no --verbose escape, so a DEBUG record here would be invisible
        # in every normal run — which is the silence historic regression exists to remove. Matches
        # the level chosen by the sibling fixes for this bug class (historic regression, historic regression).
        log.warning("load_pursuit failed for %s, falling back to raw frontmatter parse", path, exc_info=True)
        fm = _parse_frontmatter(text)
        if not fm:
            click.echo(f"SKIP (no frontmatter): {path}", err=True)
            return "skip"

    has_sf = any(fm.get(v) for v in FIELD_MAP.values())
    if not has_sf:
        click.echo(f"SKIP (no sf_stage/sf_close_date/sf_arr): {path}", err=True)
        return "skip"

    _, changes = _rewrite_key_fields_table(text, fm)
    if changes == 0:
        click.echo(f"[dry-run] OK (no changes needed): {path}", err=True)
        return "ok"

    click.echo(f"[dry-run] WOULD UPDATE ({changes} field(s)): {path}", err=True)
    return "updated"


# ---------------------------------------------------------------------------
# Click command — used by `fieldkit sf reconcile` via the sf.py group.
# ---------------------------------------------------------------------------


@declare_write("workspace")
@click.command("reconcile")
@click.argument("pursuit_file")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview what would be changed without writing any files.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the reconcile outcome as JSON.")
def cli(pursuit_file: str, dry_run: bool, as_json: bool) -> None:
    """Rewrite Key Fields table in a pursuit file from frontmatter values."""
    status = _run_reconcile_dry(pursuit_file) if dry_run else _run_reconcile(pursuit_file)
    if as_json:
        # Per-field before/after detail is already on stderr; stdout carries the outcome.
        click.echo(
            json.dumps(
                {"file": pursuit_file, "status": status, "dry_run": dry_run, "changed": status == "updated"},
                indent=2,
                default=str,
            )
        )
    if status == "error":
        raise SystemExit(EXIT_PARTIAL)


# ---------------------------------------------------------------------------
# Core logic — extracted so both the Click command and legacy main() share it.
# historic regression fix: returns a status string instead of calling sys.exit(), so callers
# (e.g. _process_opp in listview.py) are not killed by a SystemExit propagating.
# ---------------------------------------------------------------------------


def _run_reconcile(path: str) -> str:
    """Execute the reconcile logic; returns a status string.

    Returns one of: "updated", "ok", "skip".
    Raises FileNotFoundError when the pursuit file does not exist (historic regression fix:
    callers such as do_write_opp in sync.py can catch this without the whole
    listview account loop being killed by a SystemExit).
    """
    if not Path(path).is_file():
        raise FileNotFoundError(f"Pursuit file not found: {path}")

    with Path(path).open(encoding="utf-8") as f:
        text = f.read()

    try:
        model, _, _ = load_pursuit(path)
        fm = {
            "sf_stage": str(model.sf_stage) if model.sf_stage else "",
            "sf_close_date": str(model.sf_close_date) if model.sf_close_date else "",
            "sf_arr": str(model.sf_arr) if model.sf_arr else "",
        }
    except Exception:  # noqa: BLE001
        # WARNING, not DEBUG: the CLI sets basicConfig(level=INFO) in __main__.py and
        # reconcile has no --verbose escape, so a DEBUG record here would be invisible
        # in every normal run — which is the silence historic regression exists to remove. Matches
        # the level chosen by the sibling fixes for this bug class (historic regression, historic regression).
        log.warning("load_pursuit failed for %s, falling back to raw frontmatter parse", path, exc_info=True)
        fm = _parse_frontmatter(text)
        if not fm:
            click.echo(f"SKIP (no frontmatter): {path}", err=True)
            return "skip"

    has_sf = any(fm.get(v) for v in FIELD_MAP.values())
    if not has_sf:
        click.echo(f"SKIP (no sf_stage/sf_close_date/sf_arr): {path}", err=True)
        return "skip"

    new_text, changes = _rewrite_key_fields_table(text, fm)

    if changes == 0:
        click.echo(f"OK (no changes needed): {path}", err=True)
        return "ok"

    _write_atomic(path, new_text)
    click.echo(f"UPDATED ({changes} field(s)): {path}", err=True)
    return "updated"


if __name__ == "__main__":
    cli()
