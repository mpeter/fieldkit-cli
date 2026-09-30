"""fieldkit ingest promote — interactively promote meeting action items to TASKS.md.

Shows each action item from a processed meeting note with a suggested classification,
lets the user confirm, override, or skip, then writes confirmed items to TASKS.md.

Usage:
    fieldkit ingest promote <meeting-file>
    fieldkit ingest promote --recent          # promote from last N meetings
"""

import re
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

import click
import yaml

from fieldkit.cli_exit import EXIT_DATA, EXIT_PARTIAL
from fieldkit.config import get_fieldkit_data
from fieldkit.ingest.note_effect import MAX_NOTE_BYTES
from fieldkit.ingest.paths import recent_meeting_paths, resolve_promote_input
from fieldkit.ingest.writeback import build_pursuit_label
from fieldkit.pursuit.io import extract_frontmatter_text
from fieldkit.tasks.classifier import ClassifiedItem, ItemClass
from fieldkit.tasks.effects import TaskEffect, validate_task_source_id
from fieldkit.tasks.writer import append_to_tasks
from fieldkit.util.atomic import PathLockTimeoutError
from fieldkit.util.text_snapshot import read_text_snapshot

# ---------------------------------------------------------------------------
# Suggestion engine — lightweight, no classification machinery
# ---------------------------------------------------------------------------

_HIGH_CONFIDENCE_RE = re.compile(
    r"\b(docusign|purchase order|\bpo\b|sow|statement of work|contract|signature|sign off"
    r"|amend|approv|budget|funding|milestone|close date|mvp|july 1|q[1-4]\b"
    r"|proposal|quote|pricing|fiscal|payment|invoice)\b",
    re.IGNORECASE,
)


def _suggest(item: str, user_name: str, user_email: str) -> str:
    """Return 'm' (mine), 'w' (waiting_on hint), or 's' (skip hint)."""
    # Owner extraction — name before colon/dash/to
    m = re.match(r"^([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)\s*[:\-]", item)
    owner = m.group(1).lower() if m else ""
    user_lower = user_name.lower()
    user_local = user_email.split("@")[0].lower()

    if owner and (
        owner in user_lower or user_local in owner or any(t in owner for t in user_lower.split() if len(t) > 2)
    ):
        return "m"
    if _HIGH_CONFIDENCE_RE.search(item):
        return "w"
    return "s"


# ---------------------------------------------------------------------------
# Interactive prompt for one item
# ---------------------------------------------------------------------------

_LABEL = {
    "m": click.style("MY TASK  ", fg="green", bold=True),
    "w": click.style("WAITING  ", fg="yellow", bold=True),
    "s": click.style("skip     ", fg="bright_black"),
}

_CHOICE_MAP = {
    "m": "m",
    "mine": "m",
    "my": "m",
    "1": "m",
    "w": "w",
    "wait": "w",
    "waiting": "w",
    "2": "w",
    "s": "s",
    "skip": "s",
    "n": "s",
    "3": "s",
    "q": "q",
    "quit": "q",
}


def _prompt_item(idx: int, total: int, item: str, suggestion: str) -> str:
    """Show one action item and return the user's choice: m/w/s/q.

    Returns "q" on stdin EOF or Ctrl-C (click.exceptions.Abort) so the caller
    can treat non-interactive / CI contexts as a graceful quit rather than a
    crash.  This mirrors the EOFError guard in reprocess._prompt_reprocess_choice.
    """
    click.echo("")
    click.echo(f"  [{idx}/{total}] {item}")
    click.echo(f"         Suggested: {_LABEL[suggestion]}  (m)ine  (w)aiting-on  (s)kip  (q)uit")
    while True:
        try:
            # implementation change: catch Abort (Ctrl-C or stdin EOF signalled by Click) and
            # plain EOFError (piped stdin exhausted before click.prompt reads it).
            # Both are treated as "q" so the existing quit path is reused cleanly.
            raw = (
                click.prompt("         Choice", default=suggestion, show_default=True, prompt_suffix=" → ")
                .strip()
                .lower()
            )
        except (click.exceptions.Abort, EOFError):
            return "q"
        choice = _CHOICE_MAP.get(raw)
        if choice:
            return choice
        click.echo("         Please enter m, w, s, or q.")


# ---------------------------------------------------------------------------
# TASKS.md writers
# ---------------------------------------------------------------------------


def _write_active(tasks_path: Path, item: str, pursuit_label: str, *, source_id: str, position: int) -> bool:
    """Persist a confirmed personal task through the shared task writer."""
    added, _ = append_to_tasks(
        [TaskEffect(source_id, position, ClassifiedItem(item, ItemClass.MY_TASK, "", "User confirmed", pursuit_label))],
        tasks_path,
        meeting_date="",
        meeting_title="",
        runtime_root=get_fieldkit_data(),
    )
    if not added:
        click.echo("         → already in TASKS.md, skipped")
    return bool(added)


def _write_waiting(
    tasks_path: Path,
    item: str,
    pursuit_label: str,
    *,
    meeting_title: str,
    meeting_date: str,
    source_id: str,
    position: int,
) -> bool:
    """Persist a confirmed waiting-on task through the shared task writer."""
    om = re.match(r"^([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+)*)\s*[:\-]", item)
    owner = om.group(1) if om else "Unknown"
    _, added = append_to_tasks(
        [
            TaskEffect(
                source_id, position, ClassifiedItem(item, ItemClass.WAITING_ON, owner, "User confirmed", pursuit_label)
            )
        ],
        tasks_path,
        meeting_date=meeting_date,
        meeting_title=meeting_title,
        runtime_root=get_fieldkit_data(),
    )
    if not added:
        click.echo("         → already in TASKS.md, skipped")
    return bool(added)


# ---------------------------------------------------------------------------
# Core promote logic for one meeting file
# ---------------------------------------------------------------------------


def _metadata_text(metadata: Mapping[str, object], key: str, default: str) -> str:
    """Read text without coercion; YAML calendar dates have one explicit representation."""
    if key not in metadata:
        return default
    value = metadata[key]
    if key == "meeting_date" and type(value) is date:
        value = value.isoformat()
    if not isinstance(value, str) or not value.strip() or len(value) > 8192:
        raise ValueError("Invalid meeting metadata")
    if key == "meeting_date" and date.fromisoformat(value).isoformat() != value:
        raise ValueError("Invalid meeting date")
    return value


def _metadata_strings(metadata: Mapping[str, object], key: str, limit: int) -> list[str]:
    """Require bounded string sequences before prompting or making durable writes."""
    value = metadata.get(key, [])
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError("Invalid meeting metadata")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > 8192:
            raise ValueError("Invalid meeting metadata")
        result.append(item)
    return result


def _promote_file(meeting_path: Path, tasks_path: Path, user_name: str, user_email: str) -> bool:
    """Interactively promote action items from one meeting note.

    Returns True if the user chose to quit early.
    """
    try:
        text = read_text_snapshot(meeting_path, max_bytes=MAX_NOTE_BYTES).content
    except FileNotFoundError:
        raise ValueError("Meeting note is unavailable") from None
    fm_text = extract_frontmatter_text(text)
    if not fm_text:
        raise ValueError("Invalid meeting frontmatter")

    try:
        fm: dict[str, Any] = yaml.safe_load(fm_text)
    except (yaml.YAMLError, RecursionError):
        raise ValueError("Invalid meeting frontmatter") from None

    if not isinstance(fm, dict):
        raise ValueError("Invalid meeting metadata")
    action_items = _metadata_strings(fm, "action_items", 2000)
    if not action_items:
        click.echo(f"  {meeting_path.name}: no action items, skipping.")
        return False

    source_id = validate_task_source_id(fm.get("source_id"))

    account = _metadata_text(fm, "account", "unknown")
    pursuits_list = _metadata_strings(fm, "pursuits", 1000)
    meeting_title = _metadata_text(fm, "meeting_title", meeting_path.stem)
    meeting_date = _metadata_text(fm, "meeting_date", "")
    label = build_pursuit_label(account, pursuits_list)

    click.echo("")
    click.echo(click.style("━" * 70, fg="bright_black"))
    click.echo(click.style(f"  📋 {meeting_title}", bold=True))
    click.echo(f"  {meeting_date}  ·  {meeting_path.name}")
    click.echo(click.style("━" * 70, fg="bright_black"))
    click.echo(f"  {len(action_items)} action item(s)  ·  label: {label}")

    n_mine = n_wait = n_skip = 0
    for i, item in enumerate(action_items, 1):
        suggestion = _suggest(item, user_name, user_email)
        choice = _prompt_item(i, len(action_items), item, suggestion)

        if choice == "q":
            click.echo("  Quitting. Progress saved.")
            return True
        elif choice == "m":
            if _write_active(tasks_path, item, label, source_id=source_id, position=i - 1):
                click.echo("         → added to Active")
                n_mine += 1
            else:
                n_skip += 1
        elif choice == "w":
            if _write_waiting(
                tasks_path,
                item,
                label,
                meeting_title=meeting_title,
                meeting_date=meeting_date,
                source_id=source_id,
                position=i - 1,
            ):
                click.echo("         → added to Waiting On")
                n_wait += 1
            else:
                n_skip += 1
        else:
            n_skip += 1

    click.echo("")
    click.echo(f"  Done: {n_mine} active, {n_wait} waiting-on, {n_skip} skipped.")
    return False


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


def _help_callback(ctx: click.Context, _param: click.Parameter, val: bool) -> None:
    if val:
        click.echo(ctx.get_help())
        ctx.exit(0)


@click.command("promote")
@click.argument("meeting_file", required=False, type=click.Path(exists=True, path_type=Path))
@click.option(
    "--recent", "-r", default=0, metavar="N", help="Promote from the N most recently written meeting notes (omit FILE)."
)
@click.option("--account", "-a", default=None, help="Filter --recent to a specific account slug.")
# show_help uses expose_value=False — handled by _help_callback, not passed to cli().
@click.option(
    "-h",
    "--help",
    "show_help",
    is_flag=True,
    is_eager=True,
    expose_value=False,
    callback=_help_callback,
)
def cli(meeting_file: Path | None, recent: int, account: str | None) -> None:
    """Interactively promote meeting action items to TASKS.md.

    Meeting notes need a stable, unique source_id and a list of action_items.
    For manually authored notes, choose an ID using letters, digits, underscores,
    hyphens, or colons (at most 256 characters); never reuse it for another note.
    Keep action-item order and source_id unchanged while retrying promotion.

    Task provenance comments preserve edited tasks on identical retries. Keep
    those comments when editing or checking off a task. Conflicting decisions
    and matching unmarked legacy tasks are refused, not overwritten. To keep an
    existing task, rerun and skip that action item; edit the existing task by
    hand while retaining its provenance instead of promoting a replacement.

    Exit 1 means TASKS.md is missing or busy, or no meeting input was selected.
    Create the task file, select a meeting file or --recent N, or wait for the
    other writer as directed. Exit 3 means invalid metadata or ambiguous
    provenance; correct the note or restore
    its original provenance from your backup before retrying. Earlier confirmed
    items may already have been saved when a later item fails.

    Review each action item from a processed meeting note and choose:

    \b
      m  →  add to Active (it's your task)
      w  →  add to Waiting On (you're waiting on someone else)
      s  →  skip (delivery noise, not relevant to you)
      q  →  quit and save progress

    \b
    Examples:
      fieldkit ingest promote fieldkit-data/accounts/<acct>/meetings/2026-05-13-*.md
      fieldkit ingest promote --recent 5
      fieldkit ingest promote --recent 10 --account <acct>
    """
    from fieldkit.config import get_fieldkit_home
    from fieldkit.config import get_user_email as _get_user_email
    from fieldkit.config import get_user_name as _get_user_name

    # Load user identity
    user_name = _get_user_name()
    user_email = _get_user_email() or ""

    data_root = get_fieldkit_home()
    tasks_path = data_root / "TASKS.md"

    if not tasks_path.exists():
        click.echo(f"Error: TASKS.md not found at {tasks_path}", err=True)
        raise SystemExit(EXIT_PARTIAL)

    try:
        if meeting_file:
            files = [resolve_promote_input(data_root, meeting_file)]
        elif recent > 0:
            files = recent_meeting_paths(data_root, account=account, limit=recent)
        else:
            click.echo("Provide a MEETING_FILE or use --recent N.", err=True)
            raise SystemExit(EXIT_PARTIAL)
    except ValueError:
        click.echo("Cannot safely select meeting notes; use a literal account slug and non-redirected paths.", err=True)
        raise SystemExit(EXIT_DATA) from None

    if not meeting_file:
        if not files:
            click.echo("No meeting files found.")
            raise SystemExit(0)
        click.echo(
            f"Promoting from {len(files)} most recent meeting(s)"
            + (f" in account '{account}'" if account else "")
            + ":"
        )
        for f in files:
            click.echo(f"  {f.relative_to(data_root.resolve())}")

    # Process each file
    try:
        for f in files:
            quit_early = _promote_file(resolve_promote_input(data_root, f), tasks_path, user_name, user_email)
            if quit_early:
                break
    except PathLockTimeoutError:
        click.echo("TASKS.md is busy; retry promotion after the current writer finishes.", err=True)
        raise SystemExit(EXIT_PARTIAL) from None
    except ValueError:
        click.echo(
            "Promotion refused ambiguous metadata or task provenance; reconcile the meeting and TASKS.md before retrying.",
            err=True,
        )
        raise SystemExit(EXIT_DATA) from None

    click.echo("")
    click.echo("✓ Promotion complete. Review TASKS.md to verify.")
