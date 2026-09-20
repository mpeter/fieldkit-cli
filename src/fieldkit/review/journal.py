"""fieldkit.review.journal — append-only review run journal.

One JSON line per ``/review`` run. Records triage decisions, finding
counts, Phase 3 verdicts, and token estimates for calibration.
Annotations (true/false-positive flags) are separate lines linked by
runId.
"""

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_JOURNAL_NAME = "review-journal.jsonl"


def journal_path(data_path: Path) -> Path:
    return data_path / _JOURNAL_NAME


def generate_run_id() -> str:
    return str(uuid.uuid4())


def append_entry(
    data_path: Path,
    *,
    report: dict[str, Any],
    mode: str,
    pr_identifier: str | None = None,
    wall_clock_ms: int = 0,
) -> None:
    """Append a journal entry after a /review run. Fire-and-forget."""
    try:
        _do_append_entry(
            data_path,
            report=report,
            mode=mode,
            pr_identifier=pr_identifier,
            wall_clock_ms=wall_clock_ms,
        )
    except OSError:
        logger.warning("Failed to write review journal entry", exc_info=True)


def _do_append_entry(
    data_path: Path,
    *,
    report: dict[str, Any],
    mode: str,
    pr_identifier: str | None,
    wall_clock_ms: int,
) -> None:
    findings = report.get("findings", [])
    finding_counts = _count_findings(findings)
    phase3 = _count_phase3_verdicts(report)

    entry: dict[str, Any] = {
        "type": "run",
        "runId": report.get("runId", generate_run_id()),
        "timestamp": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "prIdentifier": pr_identifier,
        "catalogueVersion": report.get("catalogueVersion", "unknown"),
        "mode": mode,
        "triageSummary": report.get("triageSummary", {}),
        "findingCounts": finding_counts,
        "phase3Verdicts": phase3,
        "tokenEstimate": report.get("tokenEstimate", 0),
        "wallClockMs": wall_clock_ms,
    }

    path = journal_path(data_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def append_annotation(
    data_path: Path,
    *,
    run_id: str,
    finding_id: str,
    verdict: str,
) -> None:
    """Append a user verdict annotation linked to a run."""
    entry = {
        "type": "annotation",
        "runId": run_id,
        "findingId": finding_id,
        "verdict": verdict,
        "timestamp": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path = journal_path(data_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def read_entries(data_path: Path) -> list[dict[str, Any]]:
    """Read all journal entries."""
    path = journal_path(data_path)
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            entries.append(json.loads(stripped))
        except json.JSONDecodeError:
            logger.warning("Skipping malformed journal line")
    return entries


def _count_findings(findings: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    by_severity: dict[str, int] = {}
    by_class: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for f in findings:
        sev = f.get("severity", "UNKNOWN")
        by_severity[sev] = by_severity.get(sev, 0) + 1
        cls = f.get("cls", "UNKNOWN")
        by_class[cls] = by_class.get(cls, 0) + 1
        cat = f.get("category", "UNKNOWN")
        by_category[cat] = by_category.get(cat, 0) + 1
    return {"bySeverity": by_severity, "byClass": by_class, "byCategory": by_category}


def _count_phase3_verdicts(report: dict[str, Any]) -> dict[str, Any]:
    """Return the synthesis-provided Phase 3 outcome aggregate.

    Refuted findings are intentionally absent from the rendered ``findings``
    list, so the aggregate must travel separately from Phase 4 for calibration.
    """
    phase3 = report.get("phase3Verdicts")
    if not isinstance(phase3, dict):
        return _empty_phase3_verdicts()
    return _normalise_phase3_verdicts(phase3)


def _normalise_phase3_verdicts(phase3: dict[Any, Any]) -> dict[str, Any]:
    counts = {verdict: _nonnegative_int(phase3.get(verdict)) for verdict in ("confirmed", "refuted", "unresolvable")}
    by_category = phase3.get("byCategory")
    if not isinstance(by_category, dict):
        return {**counts, "byCategory": {}}

    category_counts = {
        category: _normalise_category_verdicts(raw_counts)
        for category, raw_counts in by_category.items()
        if isinstance(category, str) and isinstance(raw_counts, dict)
    }
    return {**counts, "byCategory": category_counts}


def _empty_phase3_verdicts() -> dict[str, Any]:
    return {"confirmed": 0, "refuted": 0, "unresolvable": 0, "byCategory": {}}


def _nonnegative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _normalise_category_verdicts(counts: dict[Any, Any]) -> dict[str, int]:
    return {verdict: _nonnegative_int(counts.get(verdict)) for verdict in ("confirmed", "refuted", "unresolvable")}
