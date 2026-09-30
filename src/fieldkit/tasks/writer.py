"""Atomic meeting task updates with source-position provenance.

A matching ownership marker and decision fingerprint preserves user edits.
Conflicting or ambiguous provenance is never silently adopted or overwritten.
"""

import re
from pathlib import Path

from fieldkit.tasks.classifier import ItemClass
from fieldkit.tasks.effects import PreparedTaskLine, TaskEffect, prepare_effect, read_task_markers
from fieldkit.util.atomic import atomic_text_write, exclusive_file_lock, prepare_runtime_lock_path
from fieldkit.util.owned_markdown import inspect_owned_markdown
from fieldkit.util.text_snapshot import read_text_snapshot

TASK_WRITE_TIMEOUT_SECONDS = 5
MAX_TASK_BYTES = 4_000_000

_COMMENT_RE = re.compile(r"<!--[^\n]*-->\n?")


def _insert_lines(content: str, lines: list[str], *, waiting_on: bool) -> str:
    """Insert one batch below its unique section heading, creating it if absent."""
    if not lines:
        return content
    label = "Waiting On" if waiting_on else "Active"
    matches = inspect_owned_markdown(content, section_titles=("Active", "Waiting On")).section_ends[label]
    block = "\n".join(lines)
    if not matches:
        return content + f"\n## {label}\n{block}\n"
    position = matches[0]
    comment = _COMMENT_RE.match(content, position)
    if comment:
        position = comment.end()
    return content[:position] + "\n" + block + "\n" + content[position:]


def _pending_lines(content: str, prepared: list[tuple[ItemClass, PreparedTaskLine]]) -> tuple[list[str], list[str]]:
    """Verify all ownership before any new task is persisted."""
    markers = read_task_markers(content)
    unmarked = {re.sub(r"^- \[[ xX]\] ", "- ", line) for line in content.splitlines() if "fieldkit-task:" not in line}
    active: list[str] = []
    waiting: list[str] = []
    for classification, task in prepared:
        existing = markers.get(task.identity)
        if existing is not None:
            expected_section = "Active" if classification == ItemClass.MY_TASK else "Waiting On"
            if existing.fingerprint != task.fingerprint or existing.section != expected_section:
                raise ValueError("Task provenance conflicts with saved decision")
            continue
        if task.line.split(" <!-- fieldkit-task:", 1)[0] in unmarked:
            raise ValueError("Unmarked task requires explicit reconciliation")
        (active if classification == ItemClass.MY_TASK else waiting).append(task.line)
    return active, waiting


def append_to_tasks(
    effects: list[TaskEffect],
    tasks_path: Path,
    *,
    runtime_root: Path,
    meeting_date: str,
    meeting_title: str,
) -> tuple[int, int]:
    """Apply already-classified effects and return active/waiting-on counts.

    Callers retain original source positions before filtering task decisions.
    Keep provenance markers while a source may be retried. Unmarked legacy
    tasks require operator reconciliation; edited unmarked tasks cannot be
    reliably recognized as prior effects.
    """
    prepared: list[tuple[ItemClass, PreparedTaskLine]] = []
    identities: set[str] = set()
    for effect in effects:
        task = prepare_effect(effect, meeting_date=meeting_date, meeting_title=meeting_title)
        if task.identity in identities:
            raise ValueError("Duplicate task effect identity")
        identities.add(task.identity)
        if effect.item.cls != ItemClass.DROP:
            prepared.append((effect.item.cls, task))
    if not prepared:
        return 0, 0
    lock_path = prepare_runtime_lock_path(tasks_path, runtime_root, "tasks")
    with exclusive_file_lock(lock_path, timeout_seconds=TASK_WRITE_TIMEOUT_SECONDS):
        if tasks_path.is_symlink():
            raise ValueError("Refusing symlinked tasks file")
        try:
            content = read_text_snapshot(tasks_path, max_bytes=MAX_TASK_BYTES).content
        except FileNotFoundError:
            content = ""
        active, waiting = _pending_lines(content, prepared)
        updated = _insert_lines(content, active, waiting_on=False)
        updated = _insert_lines(updated, waiting, waiting_on=True)
        if active or waiting:
            if len(updated.encode("utf-8")) > MAX_TASK_BYTES:
                raise ValueError("Task update exceeds byte limit")
            read_task_markers(updated)
            atomic_text_write(tasks_path, updated)
    return len(active), len(waiting)
