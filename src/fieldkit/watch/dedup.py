"""Write-deduplication helpers for watcher alert appenders.

``alert_block_exists`` scans an alerts file line-by-line for a heading that
starts with ``## <heading_prefix>`` and returns True on the first match.
Callers use this to skip appending a block that was already written in a
previous run today.
"""

import logging
from pathlib import Path

log = logging.getLogger(__name__)


def alert_block_exists(alerts_path: Path, heading_prefix: str) -> bool:
    """Return True if *alerts_path* already contains a heading matching *heading_prefix*.

    The check is a case-sensitive prefix scan: a line that starts with
    ``## {heading_prefix}`` (possibly followed by more text) is considered a match.

    Args:
        alerts_path: Path to the alerts markdown file.  If the file does not
            exist, returns False immediately.
        heading_prefix: The ``## ``-less prefix to look for, e.g.
            ``"2026-06-06 — acme / my-deal"``.

    Returns:
        True if a matching heading is found; False otherwise.
    """
    if not alerts_path.exists():
        return False

    target = f"## {heading_prefix}"
    try:
        with alerts_path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(target):
                    log.info(
                        "Alert block already exists for %s, skipping",
                        heading_prefix,
                    )
                    return True
    except OSError as exc:
        log.warning("Could not read alerts file %s: %s", alerts_path, exc)
        return False

    return False


def scrub_duplicate_alerts(alerts_path: Path) -> int:
    """Deduplicate pursuit-stall-alerts.md in-place.

    Keeps the first occurrence of each ``## YYYY-MM-DD — account / pursuit``
    heading block. Removes subsequent blocks with identical headings. Writes
    back atomically via a .tmp rename. Returns the number of duplicate blocks
    removed.

    This is a one-time operator action for cleaning pre-existing bloated files.
    The within-day dedup in ``append_stall_alert`` prevents future accumulation.
    """
    if not alerts_path.exists():
        return 0

    content = alerts_path.read_text(encoding="utf-8")
    lines = content.splitlines(keepends=True)

    seen_headings: set[str] = set()
    output_lines: list[str] = []
    skip_block = False
    removed = 0

    for line in lines:
        if line.startswith("## "):
            heading = line.rstrip("\n")
            if heading in seen_headings:
                skip_block = True
                removed += 1
                continue
            seen_headings.add(heading)
            skip_block = False
        elif skip_block:
            continue
        output_lines.append(line)

    new_content = "".join(output_lines)
    if new_content != content:
        # Use string concatenation, not with_suffix(), to avoid ValueError on Python 3.12+
        # where with_suffix(".md.tmp") raises because ".md.tmp" contains an embedded dot.
        tmp = alerts_path.parent / (alerts_path.name + ".tmp")
        tmp.write_text(new_content, encoding="utf-8")
        tmp.replace(alerts_path)

    return removed
