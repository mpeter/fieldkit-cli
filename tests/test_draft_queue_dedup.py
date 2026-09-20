"""Tests for draft_queue.write_alerts date-based deduplication."""

import re
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.watch.draft_queue import write_alerts

pytestmark = pytest.mark.unit


@pytest.fixture()
def alerts_file(tmp_path: Path) -> Generator[Path, None, None]:
    """Patch _alerts_file and get_watchers_dir to use a temp dir."""
    f = tmp_path / "draft-queue-alerts.md"
    import fieldkit.watch.draft_queue as dq

    with (
        patch.object(dq, "_alerts_file", return_value=f),
        patch.object(dq, "get_watchers_dir", return_value=tmp_path),
    ):
        yield f


def _count_date_sections(text: str, date_label: str) -> int:
    return len(re.findall(rf"^## {re.escape(date_label)}", text, re.MULTILINE))


def test_first_write_creates_section(alerts_file: Path) -> None:

    write_alerts([], dry_run=False)
    content = alerts_file.read_text(encoding="utf-8")
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert _count_date_sections(content, today) == 1


def test_second_write_same_day_produces_one_section(alerts_file: Path) -> None:

    write_alerts([], dry_run=False)
    write_alerts([], dry_run=False)
    content = alerts_file.read_text(encoding="utf-8")
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert _count_date_sections(content, today) == 1


def test_three_writes_same_day_produces_one_section(alerts_file: Path) -> None:

    drafts_a = [{"subject": "Hello", "to": "a@example.com", "age": "2d 0h", "draft_id": "id1"}]  # pii-guard: ignore
    drafts_b = [{"subject": "World", "to": "b@example.com", "age": "3d 1h", "draft_id": "id2"}]  # pii-guard: ignore

    write_alerts(drafts_a, dry_run=False)
    write_alerts(drafts_b, dry_run=False)
    write_alerts([], dry_run=False)

    content = alerts_file.read_text(encoding="utf-8")
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert _count_date_sections(content, today) == 1
    # Final write (empty) should be the surviving snapshot.
    assert "No stale drafts found." in content
    assert "Hello" not in content
    assert "World" not in content


def test_existing_prior_day_section_preserved(alerts_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Writing today must not disturb an earlier day's section."""
    # Seed file with a "yesterday" section.
    alerts_file.write_text(
        "# Draft Queue Alerts\n\nAutomated alerts written by draft_queue.py.\n"
        "\n## 2000-01-01\n\n### Pending Outbox\n\nNo stale drafts found.\n",
        encoding="utf-8",
    )

    write_alerts([], dry_run=False)

    content = alerts_file.read_text(encoding="utf-8")
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert _count_date_sections(content, "2000-01-01") == 1
    assert _count_date_sections(content, today) == 1


def test_dry_run_does_not_write(alerts_file: Path, capsys: pytest.CaptureFixture[str]) -> None:

    write_alerts([], dry_run=True)
    # File should not have been created.
    assert not alerts_file.exists()
    captured = capsys.readouterr()
    assert "No stale drafts found." in captured.out
