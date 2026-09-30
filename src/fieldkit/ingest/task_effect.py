"""Replay recorded active-task decisions without consulting live configuration."""

from pathlib import Path

from fieldkit.config import get_fieldkit_data
from fieldkit.ingest.prepared import ReplayIntent
from fieldkit.tasks.classifier import ClassifiedItem, ItemClass
from fieldkit.tasks.effects import TaskEffect
from fieldkit.tasks.writer import append_to_tasks
from fieldkit.util.workspace_paths import resolve_workspace_output


def publish_prepared_tasks(intent: ReplayIntent, workspace: Path) -> tuple[int, int]:
    """Apply saved active tasks with their original unfiltered source positions.

    Waiting-on decisions remain available to interactive promotion. The workspace
    directory namespace must remain stable throughout publication.
    """
    prepared = intent.prepared
    effects = [
        TaskEffect(
            prepared.source_id,
            position,
            ClassifiedItem(task.text, ItemClass(task.cls), task.owner, task.rationale, task.pursuit_label),
        )
        for position, task in enumerate(prepared.tasks)
        if task.cls == "my_task"
    ]
    if not effects:
        return (0, 0)
    path = resolve_workspace_output(workspace, "TASKS.md")
    return append_to_tasks(
        effects,
        path,
        meeting_date=prepared.meeting_date,
        meeting_title=prepared.meeting_title,
        runtime_root=get_fieldkit_data(),
    )
