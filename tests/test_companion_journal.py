"""Tests for fieldkit.companion.journal — append-only outcome journal with month rotation."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fieldkit.companion.journal import append_journal, journal_path

pytestmark = pytest.mark.unit

_JUNE = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
_JULY = datetime(2026, 7, 1, 0, 0, 5, tzinfo=UTC)


def test_journal_path_is_month_suffixed(tmp_path: Path) -> None:
    result = journal_path(tmp_path, when=_JUNE)
    assert result == tmp_path / "companion-journal-2026-06.jsonl"


def test_append_journal_writes_one_line_per_action(tmp_path: Path) -> None:
    result = append_journal(tmp_path, item_id="a1", action="pursuit health", exit_code=0, when=_JUNE)
    append_journal(tmp_path, item_id="a2", action="gmail query", exit_code=1, when=_JUNE)

    lines = result.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first == {
        "item_id": "a1",
        "action": "pursuit health",
        "exit_code": 0,
        "timestamp": "2026-06-15T12:00:00Z",
    }


def test_append_journal_records_decision_provenance(tmp_path: Path) -> None:
    result = append_journal(
        tmp_path,
        item_id="a1",
        action="pursuit advance acme --dry-run",
        exit_code=3,
        when=_JUNE,
        decision_provenance="llm",
        fallback_category="invalid-command",
    )

    record = json.loads(result.read_text(encoding="utf-8"))
    assert record["decision_provenance"] == "llm"
    assert record["fallback_category"] == "invalid-command"


def test_append_journal_rotates_on_month_boundary(tmp_path: Path) -> None:
    june_file = append_journal(tmp_path, item_id="a1", action="x", exit_code=0, when=_JUNE)
    july_file = append_journal(tmp_path, item_id="a2", action="y", exit_code=0, when=_JULY)

    assert june_file != july_file
    assert june_file.name == "companion-journal-2026-06.jsonl"
    assert july_file.name == "companion-journal-2026-07.jsonl"
    assert len(june_file.read_text(encoding="utf-8").splitlines()) == 1
    assert len(july_file.read_text(encoding="utf-8").splitlines()) == 1
