"""fieldkit.tasks.writer — Append classified action items to TASKS.md.

Reads the existing TASKS.md, inserts MY_TASK items under ## Active and
WAITING_ON items under ## Waiting On, then writes the file back.

Format mirrors the existing TASKS.md conventions:
  Active:     - **[Account/Pursuit]** <task text>
  Waiting On: - **[Account/Pursuit]** Waiting on <owner> re: <topic> — from meeting <date>

Idempotent: items already present (matched by text substring) are skipped.

Public API
----------
append_to_tasks(classified_items, tasks_path, meeting_date, meeting_title)
"""

import re
from collections.abc import Callable
from pathlib import Path

from fieldkit.tasks.classifier import ClassifiedItem, ItemClass

# Section header patterns
_ACTIVE_RE = re.compile(r"^## Active\s*$", re.MULTILINE)
_WAITING_ON_RE = re.compile(r"^## Waiting On\s*$", re.MULTILINE)

# Matches an HTML comment line immediately following a section heading
_COMMENT_RE = re.compile(r"\n<!--[^\n]*-->\n?")


def _format_active(item: ClassifiedItem) -> str:
    tag = f"**[{item.pursuit_label}]** " if item.pursuit_label else ""
    return f"- {tag}{item.text}"


def _format_waiting_on(item: ClassifiedItem, meeting_date: str, meeting_title: str) -> str:
    tag = f"**[{item.pursuit_label}]** " if item.pursuit_label else ""
    owner_str = f" {item.owner}" if item.owner not in ("Unknown", "Team") else ""
    # Shorten the raw text into a topic description
    topic = item.text
    # Strip owner prefix if present (e.g. "Brooke: confirm budget" → "confirm budget")
    topic = re.sub(r"^[A-Z][a-z]+(?: [A-Z][a-z]+)*\s*[:\—\-]\s*", "", topic).strip()
    return f"- {tag}Waiting on{owner_str} re: {topic} — from {meeting_title} ({meeting_date})"


def _insert_items(
    content: str,
    items: list[ClassifiedItem],
    header_re: re.Pattern[str],
    format_fn: Callable[[ClassifiedItem], str],
    section_label: str,
) -> tuple[str, int]:
    """Insert *items* into the named section of *content*.

    Finds the section matched by *header_re*, skips any trailing HTML comment,
    then inserts formatted lines for items not already present (idempotent by
    first-60-chars key).

    If the section is absent, appends a new ``## <section_label>`` block for
    each non-duplicate item individually (standard TASKS.md fallback).

    Returns ``(updated_content, count_added)``.
    """
    match = header_re.search(content)
    if match:
        insert_pos = match.end()
        # Skip any comment line immediately after the heading
        comment_m = _COMMENT_RE.match(content, insert_pos)
        if comment_m:
            insert_pos = comment_m.end()

        lines_to_add: list[str] = []
        for item in items:
            if item.text[:60] not in content:
                lines_to_add.append(format_fn(item))

        if lines_to_add:
            block = "\n" + "\n".join(lines_to_add)
            content = content[:insert_pos] + block + content[insert_pos:]

        return content, len(lines_to_add)

    # Section absent — append a new heading per item (shouldn't happen with standard TASKS.md)
    added = 0
    for item in items:
        if item.text[:60] not in content:
            content += f"\n## {section_label}\n{format_fn(item)}\n"
            added += 1
    return content, added


def append_to_tasks(
    classified_items: list[ClassifiedItem],
    tasks_path: Path,
    *,
    meeting_date: str,
    meeting_title: str,
) -> tuple[int, int]:
    """Write MY_TASK and WAITING_ON items into TASKS.md.

    Returns (my_tasks_added, waiting_on_added) counts.
    Skips items whose text already appears in the file (idempotent).
    """
    my_tasks = [i for i in classified_items if i.cls == ItemClass.MY_TASK]
    waiting_on = [i for i in classified_items if i.cls == ItemClass.WAITING_ON]

    if not my_tasks and not waiting_on:
        return 0, 0

    content = tasks_path.read_text(encoding="utf-8") if tasks_path.exists() else ""

    my_added = 0
    wo_added = 0

    if my_tasks:
        content, my_added = _insert_items(content, my_tasks, _ACTIVE_RE, _format_active, "Active")

    if waiting_on:
        # Bind meeting context into the format callable so _insert_items stays generic
        def _fmt_wo(item: ClassifiedItem) -> str:
            return _format_waiting_on(item, meeting_date, meeting_title)

        content, wo_added = _insert_items(content, waiting_on, _WAITING_ON_RE, _fmt_wo, "Waiting On")

    if my_added or wo_added:
        tasks_path.write_text(content, encoding="utf-8")
    return my_added, wo_added
