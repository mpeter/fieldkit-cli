"""fieldkit.companion.feed — the cursored attention feed (design D2).

Reads the three stable input surfaces (watcher run-status JSON, alert
files, TASKS.md) and normalizes them into ``AttentionItem``s. A cursor
in ``<fieldkit_data>/companion-cursor.json`` makes repeated polls
deliver each item exactly once; ``--all`` bypasses it. Layer-1 only:
fully deterministic, no LLM, testable under ``NO_LLM=1``.
"""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fieldkit.companion.mapping import WATCHER_SEVERITY_MAP, suggested_skill_for
from fieldkit.errors import FieldkitError
from fieldkit.util.atomic import locked_json_update

_CURSOR_FILENAME = "companion-cursor.json"
WATCHER_FRESHNESS_WINDOW = timedelta(hours=26)

# Alert block heading: "## 2026-06-03 — <account-slug> / <pursuit> — stalled in discover"
_ALERT_HEADING = re.compile(r"^## (\d{4}-\d{2}-\d{2}) — (.+)$")
_ACCOUNT_BULLET = re.compile(r"\*\*Account:\*\*\s*`?([A-Za-z0-9_-]+)`?")


class FeedParseError(FieldkitError):
    """Raised when an upstream feed input is structurally unparseable (exit 3)."""


@dataclass(frozen=True)
class AttentionItem:
    """One thing that may deserve the agent's attention.

    ``item_id`` is a stable hash of (source, heading, date) so re-polls
    and re-runs dedupe identically — the same identity discipline
    ``watch/dedup.alert_block_exists()`` uses.
    """

    item_id: str
    source: str
    account: str | None
    severity: str
    summary: str
    evidence_path: str
    suggested_skill: str | None
    observed_at: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return asdict(self)


def _make_item_id(source: str, heading: str, discriminator: str) -> str:
    """Return a stable 16-hex id for an attention item.

    *discriminator* MUST stay constant for as long as the underlying condition
    persists — it is what the journal keys suppression on. A value that changes
    on every observation (a timestamp, say) mints a fresh id each pass, so the
    item is never recognized as already-handled and the loop re-proposes it
    forever.
    """
    digest = hashlib.sha256(f"{source}\n{heading}\n{discriminator}".encode()).hexdigest()
    return digest[:16]


def _severity_for_watcher(watcher: str) -> str:
    return WATCHER_SEVERITY_MAP.get(watcher, "warning")


# ---------------------------------------------------------------------------
# Parsers — one per input surface
# ---------------------------------------------------------------------------


def _load_status_json(path: Path) -> dict[str, Any] | None:
    """Load watcher run status, preserving missing and malformed states."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FeedParseError(f"unparseable watcher run-status JSON: {path}") from exc
    if not isinstance(data, dict):
        raise FeedParseError(f"watcher run-status is not a JSON object: {path}")
    return data


def _source_liveness_items(path: Path, status: dict[str, Any] | None, *, now: datetime | None) -> list[AttentionItem]:
    """Return one critical item when aggregate watcher status is absent or stale."""
    condition: str | None = None
    detail = ""
    observed_at = ""
    if status is None:
        condition = "missing-status"
        detail = "Watcher run status is missing. Run fieldkit watch run --all."
    else:
        aggregate = status.get("run-all")
        if not isinstance(aggregate, dict):
            condition = "missing-run-all"
            detail = "Watcher run status lacks the aggregate run-all record. Run fieldkit watch run --all."
        else:
            raw_last_run = aggregate.get("last_run")
            observed_at = raw_last_run if isinstance(raw_last_run, str) else ""
            try:
                observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
                if observed.tzinfo is None:
                    raise ValueError("timestamp has no timezone")
            except (TypeError, ValueError):
                condition = "invalid-run-all-timestamp"
                detail = "Watcher run-all timestamp is invalid. Run fieldkit watch run --all."
            else:
                reference = now or datetime.now(tz=UTC)
                if reference.astimezone(UTC) - observed.astimezone(UTC) > WATCHER_FRESHNESS_WINDOW:
                    condition = "stale-run-all"
                    detail = "Watcher aggregate status is older than 26 hours. Run fieldkit watch run --all."

    if condition is None:
        return []
    return [
        AttentionItem(
            item_id=_make_item_id("source-liveness", "watcher run-all", condition),
            source="source-liveness/watchers",
            account=None,
            severity="critical",
            summary=detail,
            evidence_path=str(path),
            suggested_skill=None,
            observed_at=observed_at,
        )
    ]


def _parse_status_json(path: Path, status: dict[str, Any] | None) -> list[AttentionItem]:
    """Surface non-ok watcher outcomes and failure counts from run-status JSON.

    Raises:
        FeedParseError: When the file exists but is not valid JSON — malformed
            upstream state is an investigation (exit 3), not a retry.
    """
    if status is None:
        return []

    items: list[AttentionItem] = []
    for watcher, entry in sorted(status.items()):
        if not isinstance(entry, dict):
            continue
        outcome = str(entry.get("outcome", ""))
        failures = int(entry.get("failures", 0) or 0)
        last_run = str(entry.get("last_run", ""))
        if outcome in ("ok", "") and failures == 0:
            continue
        summary = f"watcher {watcher}: outcome={outcome or 'unknown'}, failures={failures}"
        severity = "critical" if outcome == "fatal" else _severity_for_watcher(watcher)
        items.append(
            AttentionItem(
                # Keyed on the condition, NOT on last_run: the watcher timer
                # rewrites last_run every hour, which would mint a new id each
                # pass and defeat journal suppression — the loop would write a
                # fresh proposal file and spawn a subprocess forever for a
                # watcher stuck in one non-ok state. The id changes only when
                # the condition itself does, which is when it deserves
                # re-surfacing.
                item_id=_make_item_id("run-status", watcher, f"{outcome}:{failures}"),
                source=f"run-status/{watcher}",
                account=None,
                severity=severity,
                summary=summary,
                evidence_path=str(path),
                suggested_skill=None,
                observed_at=last_run,
            )
        )
    return items


def _parse_alert_files(watchers_dir: Path) -> list[AttentionItem]:
    """Turn each dated alert block in ``*-alerts.md`` into an AttentionItem."""
    if not watchers_dir.is_dir():
        return []
    items: list[AttentionItem] = []
    for alert_file in sorted(watchers_dir.glob("*-alerts.md")):
        stem = alert_file.name.removesuffix("-alerts.md")
        skill = suggested_skill_for(stem)
        severity = _severity_for_watcher_stem(stem)
        try:
            lines = alert_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        current: AttentionItem | None = None
        block_lines: list[str] = []

        def flush(item: AttentionItem | None, block: list[str]) -> None:
            if item is None:
                return
            account = None
            for line in block:
                found = _ACCOUNT_BULLET.search(line)
                if found:
                    account = found.group(1)
                    break
            items.append(
                AttentionItem(
                    item_id=item.item_id,
                    source=item.source,
                    account=account or item.account,
                    severity=item.severity,
                    summary=item.summary,
                    evidence_path=item.evidence_path,
                    suggested_skill=item.suggested_skill,
                    observed_at=item.observed_at,
                )
            )

        for line in lines:
            heading = _ALERT_HEADING.match(line)
            if heading:
                flush(current, block_lines)
                block_lines = []
                date, rest = heading.group(1), heading.group(2)
                current = AttentionItem(
                    item_id=_make_item_id(alert_file.name, rest, date),
                    source=f"alert/{stem}",
                    account=None,
                    severity=severity,
                    summary=rest,
                    evidence_path=str(alert_file),
                    suggested_skill=skill,
                    observed_at=date,
                )
            elif current is not None:
                block_lines.append(line)
        flush(current, block_lines)
    return items


def _severity_for_watcher_stem(stem: str) -> str:
    """Map an alert-file stem to severity via the watcher table (longest prefix)."""
    for watcher in sorted(WATCHER_SEVERITY_MAP, key=len, reverse=True):
        if stem.startswith(watcher) or watcher.startswith(stem):
            return WATCHER_SEVERITY_MAP[watcher]
    return "warning"


def _parse_tasks_md(home: Path) -> list[AttentionItem]:
    """Surface Waiting On items from TASKS.md as attention items."""
    tasks_path = home / "TASKS.md"
    if not tasks_path.is_file():
        return []
    items: list[AttentionItem] = []
    in_waiting = False
    for line in tasks_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_waiting = line.strip() == "## Waiting On"
            continue
        if not in_waiting:
            continue
        text = line.strip()
        if not text.startswith("- ") or text.startswith("- <!--"):
            continue
        summary = text[2:].strip()
        if not summary:
            continue
        account_match = re.match(r"\*\*\[([^\]/]+)", summary)
        items.append(
            AttentionItem(
                item_id=_make_item_id("tasks-md", summary, "waiting-on"),
                source="tasks/waiting-on",
                account=account_match.group(1).strip().lower() if account_match else None,
                severity="info",
                summary=summary,
                evidence_path=str(tasks_path),
                suggested_skill="task-management",
                observed_at="",
            )
        )
    return items


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------


def _cursor_path(data_path: Path) -> Path:
    return data_path / _CURSOR_FILENAME


def load_cursor(data_path: Path) -> set[str]:
    """Return the set of item_ids already delivered."""
    path = _cursor_path(data_path)
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    delivered = data.get("delivered", []) if isinstance(data, dict) else []
    return {str(x) for x in delivered}


def save_cursor(data_path: Path, delivered: set[str]) -> None:
    """Merge *delivered* item_ids into the cursor file (lock-serialized).

    ``locked_json_update`` guarantees two concurrent polls cannot drop
    each other's delivery records (design D2).
    """
    with locked_json_update(_cursor_path(data_path)) as data:
        existing = {str(x) for x in data.get("delivered", [])}
        data["delivered"] = sorted(existing | delivered)


# ---------------------------------------------------------------------------
# Feed assembly
# ---------------------------------------------------------------------------


def get_feed(
    home: Path,
    data_path: Path,
    *,
    since_cursor: bool = True,
    account_slug: str | None = None,
    suppressed: set[str] | None = None,
    now: datetime | None = None,
) -> list[AttentionItem]:
    """Assemble the attention feed from all input surfaces.

    Args:
        home: The fieldkit workspace root (contains ``watchers/``, ``TASKS.md``).
        data_path: The fieldkit data root (holds the cursor and journal).
        since_cursor: When True (default), deliver only items not yet seen
            and advance the cursor. When False, return everything.
        account_slug: Optional account filter.
        suppressed: item_ids currently retired from the feed — a live cooldown
            or a pending outbox proposal. Compute via
            ``fieldkit.companion.suppress.retired_item_ids`` so every caller
            agrees on what is withheld; a journaled outcome alone no longer
            suppresses anything.
        now: Aware timestamp used to evaluate watcher-source freshness.

    Returns:
        Deduplicated items, critical first, then warning, then info.
    """
    watchers_dir = home / "watchers"
    status_path = watchers_dir / "watcher-run-status.json"
    status = _load_status_json(status_path)
    items = (
        _source_liveness_items(status_path, status, now=now)
        + _parse_status_json(status_path, status)
        + _parse_alert_files(watchers_dir)
        + _parse_tasks_md(home)
    )

    seen: set[str] = set()
    unique: list[AttentionItem] = []
    for item in items:
        if item.item_id in seen:
            continue
        seen.add(item.item_id)
        unique.append(item)

    if account_slug is not None:
        unique = [i for i in unique if i.account == account_slug]

    withheld = suppressed or set()
    unique = [i for i in unique if i.item_id not in withheld]

    if since_cursor:
        delivered = load_cursor(data_path)
        unique = [i for i in unique if i.item_id not in delivered]
        if unique:
            save_cursor(data_path, {i.item_id for i in unique})

    order = {"critical": 0, "warning": 1, "info": 2}
    return sorted(unique, key=lambda i: (order.get(i.severity, 3), i.source, i.item_id))
