"""fieldkit.companion.journal — outcome journal, not memory (design D5).

One JSON line per completed action: fieldkit stores facts (what ran, what exit
code, when), never agent beliefs. Month-suffixed rotation caps file size.

**Append-only, and never read back to change behavior.** The journal is the
operator's evidence base for tier graduation, nothing more. It previously also
drove feed suppression, which meant a fact could not be recorded without hiding
an item, and un-hiding an item required deleting a line from the audit trail.
What the feed withholds now lives in `fieldkit.companion.suppress`.

Every path journals — including auth failures, timeouts, and gate denials.
A failure is evidence too, and journaling it no longer costs the item its place
in the feed.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

_JOURNAL_STEM = "companion-journal"


def _decision_fields(provenance: str | None, fallback: str | None) -> dict[str, str | None]:
    if provenance is None:
        return {}
    return {"decision_provenance": provenance, "fallback_category": fallback}


def journal_path(data_path: Path, *, when: datetime | None = None) -> Path:
    """Return the journal file for *when*'s month (``companion-journal-YYYY-MM.jsonl``)."""
    moment = when if when is not None else datetime.now(tz=UTC)
    return data_path / f"{_JOURNAL_STEM}-{moment.strftime('%Y-%m')}.jsonl"


def append_journal(
    data_path: Path,
    *,
    item_id: str,
    action: str,
    exit_code: int,
    when: datetime | None = None,
    decision_provenance: str | None = None,
    fallback_category: str | None = None,
) -> Path:
    """Append one action record; returns the file written.

    A new file appears automatically on the month boundary because the
    filename derives from the timestamp — no rotation bookkeeping.
    Appends are single ``write`` syscalls of one line, safe for the
    single-operator concurrency this journal sees.
    """
    moment = when if when is not None else datetime.now(tz=UTC)
    path = journal_path(data_path, when=moment)
    record = {
        "item_id": item_id,
        "action": action,
        "exit_code": exit_code,
        "timestamp": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    record.update(_decision_fields(decision_provenance, fallback_category))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return path
