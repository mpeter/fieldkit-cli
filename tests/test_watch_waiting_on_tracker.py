"""Tests for fieldkit.watch.waiting_on_tracker (domain) and CLI adapter.

Imports domain functions directly from ``fieldkit.watch.waiting_on_tracker``.
The command module is limited to Click wiring.
"""

import datetime
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import fieldkit.watch.waiting_on_tracker as tracker

pytestmark = pytest.mark.unit


def test_state_write_failure_is_fatal(tmp_path: Path) -> None:
    """A completed tracker scan must fail closed when state cannot persist."""
    state_file = tmp_path / "waiting-on-state.json"
    with (
        patch.object(tracker, "_read_tasks_md", return_value="## Waiting On\n- Reply (2026-01-01)\n"),
        patch.object(tracker, "_state_file", return_value=state_file),
        patch.object(tracker, "_save_state", side_effect=OSError("disk full")),
        patch.object(tracker, "write_run_status") as write_status,
    ):
        rc = tracker._run_inner(threshold=7, dry_run=False)

    assert rc == 1
    assert write_status.call_args.kwargs["outcome"] == "fatal"
    assert write_status.call_args.kwargs["failures"] == 1


def test_save_state_returns_none_after_persisting(tmp_path: Path) -> None:
    """The tracker state helper delegates the completed write to the shared lock."""
    state_file = tmp_path / "waiting-on-state.json"
    with patch.object(tracker, "_state_file", return_value=state_file):
        result = tracker._save_state({"item": {"alerted": True}}, previous_state={})

    assert result is None
    assert json.loads(state_file.read_text(encoding="utf-8")) == {"item": {"alerted": True}}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tasks_md(items: list[str], extra_sections: str = "") -> str:
    """Build a minimal TASKS.md with a Waiting On section."""
    body = "\n".join(f"- {item}" for item in items)
    return f"# TASKS\n\n## Waiting On\n\n{body}\n\n## Done\n\n{extra_sections}"


def _today_minus(days: int) -> str:
    d = datetime.date.today() - datetime.timedelta(days=days)
    return d.isoformat()


# ---------------------------------------------------------------------------
# Unit: parsing helpers
# ---------------------------------------------------------------------------


# ── TestExtractWaitingOnLines (flattened) ───────────────────────────────────


def test_extract_waiting_on_lines_extracts_items() -> None:
    md = _make_tasks_md(["item A (2024-01-01)", "item B (2024-02-01)"])
    lines = tracker._extract_waiting_on_lines(md)
    assert len(lines) == 2
    assert any("item A" in line for line in lines)


def test_extract_waiting_on_lines_stops_at_next_section() -> None:
    md = "## Waiting On\n\n- stale (2024-01-01)\n\n## Done\n\n- other (2024-01-01)\n"
    lines = tracker._extract_waiting_on_lines(md)
    assert len(lines) == 1
    assert "stale" in lines[0]


def test_extract_waiting_on_lines_empty_section() -> None:
    md = "## Waiting On\n\n## Done\n"
    assert tracker._extract_waiting_on_lines(md) == []


# ── TestItemDate (flattened) ────────────────────────────────────────────────


def test_item_date_valid_date() -> None:
    d = tracker._item_date("Waiting for vendor response (2024-06-01)")
    assert d == datetime.date(2024, 6, 1)


def test_item_date_sent_date_takes_precedence_over_trailing_date() -> None:
    d = tracker._item_date("Waiting for vendor — sent 2024-06-01, follow up after (2024-01-01)")
    assert d == datetime.date(2024, 6, 1)


def test_item_date_no_date_returns_none() -> None:
    assert tracker._item_date("No date here") is None


def test_item_date_malformed_date_returns_none() -> None:
    assert tracker._item_date("Bad (2024-99-99)") is None


def test_item_date_with_reason_reports_malformed_sent_date() -> None:
    item_date, reason = tracker._item_date_with_reason("Waiting for vendor — sent 2024-99-99, follow up")
    assert item_date is None
    assert reason == "invalid sent date '2024-99-99'"


# ---------------------------------------------------------------------------
# Integration: _run via monkeypatched paths
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_roots(tmp_path: Path):
    """Return (fieldkit_root, data_root) as tmp subdirs."""
    fk_root = tmp_path / "fieldkit"
    data_root = tmp_path / "data"
    fk_root.mkdir()
    data_root.mkdir()
    (data_root / "watchers").mkdir()
    return fk_root, data_root


def _patch_roots(fk_root: Path, data_root: Path):
    """Context manager patching get_fieldkit_home and get_watchers_dir in the domain module."""
    return patch.multiple(
        "fieldkit.watch.waiting_on_tracker",
        get_fieldkit_home=lambda: data_root,
        get_watchers_dir=lambda: data_root / "watchers",
    )


# ── TestRun (flattened) ─────────────────────────────────────────────────────


def _run_run(fk_root: Path, data_root: Path, **kwargs) -> int:
    # Must clear @cache on path helpers between tests
    tracker._alerts_file.cache_clear()
    tracker._state_file.cache_clear()
    tracker.get_watchers_dir.cache_clear()
    with _patch_roots(fk_root, data_root):
        return tracker._run(**{"threshold": 7, "dry_run": False, **kwargs})


def test_run_missing_tasks_md_exits_0(tmp_roots) -> None:
    fk_root, data_root = tmp_roots
    # No TASKS.md created
    rc = _run_run(fk_root, data_root)
    assert rc == 0
    alerts_file = data_root / "watchers" / "waiting-on-alerts.md"
    assert not alerts_file.exists()


def test_run_stale_item_emits_alert(tmp_roots) -> None:
    fk_root, data_root = tmp_roots
    stale = _today_minus(10)
    (data_root / "TASKS.md").write_text(_make_tasks_md([f"Vendor quote ({stale})"]), encoding="utf-8")
    rc = _run_run(fk_root, data_root)
    assert rc == 0
    alerts = (data_root / "watchers" / "waiting-on-alerts.md").read_text(encoding="utf-8")
    assert "Vendor quote" in alerts


def test_run_stale_sent_item_emits_alert(tmp_roots) -> None:
    fk_root, data_root = tmp_roots
    stale = _today_minus(10)
    item = f"Vendor quote — sent {stale}, awaiting approval"
    (data_root / "TASKS.md").write_text(_make_tasks_md([item]), encoding="utf-8")

    rc = _run_run(fk_root, data_root)

    alerts = (data_root / "watchers" / "waiting-on-alerts.md").read_text(encoding="utf-8")
    state = json.loads((data_root / "watchers" / "waiting-on-state.json").read_text(encoding="utf-8"))
    assert rc == 0
    assert item in alerts
    assert next(iter(state.values()))["item_date"] == stale


def test_run_fresh_item_no_alert(tmp_roots) -> None:
    fk_root, data_root = tmp_roots
    fresh = _today_minus(2)
    (data_root / "TASKS.md").write_text(_make_tasks_md([f"Recent request ({fresh})"]), encoding="utf-8")
    rc = _run_run(fk_root, data_root)
    assert rc == 0
    alerts_file = data_root / "watchers" / "waiting-on-alerts.md"
    # Either file doesn't exist or doesn't mention the item
    if alerts_file.exists():
        assert "Recent request" not in alerts_file.read_text(encoding="utf-8")


def test_run_item_without_date_skipped(tmp_roots) -> None:
    fk_root, data_root = tmp_roots
    (data_root / "TASKS.md").write_text(_make_tasks_md(["No date item"]), encoding="utf-8")
    rc = _run_run(fk_root, data_root)
    assert rc == 0
    # No alert emitted since date is absent
    alerts_file = data_root / "watchers" / "waiting-on-alerts.md"
    if alerts_file.exists():
        assert "No date item" not in alerts_file.read_text(encoding="utf-8")


def test_run_idempotency_no_duplicate_alerts(tmp_roots) -> None:
    fk_root, data_root = tmp_roots
    stale = _today_minus(10)
    (data_root / "TASKS.md").write_text(_make_tasks_md([f"Waiting on approval ({stale})"]), encoding="utf-8")
    # First run
    _run_run(fk_root, data_root)
    alerts_file = data_root / "watchers" / "waiting-on-alerts.md"
    content_after_first = alerts_file.read_text(encoding="utf-8")
    count_first = content_after_first.count("Waiting on approval")

    # Second run — should not emit another alert
    _run_run(fk_root, data_root)
    content_after_second = alerts_file.read_text(encoding="utf-8")
    count_second = content_after_second.count("Waiting on approval")

    assert count_first == count_second, "Duplicate alert emitted on second run"


def test_run_dry_run_no_file_writes(tmp_roots) -> None:
    fk_root, data_root = tmp_roots
    stale = _today_minus(10)
    (data_root / "TASKS.md").write_text(_make_tasks_md([f"DryRun item ({stale})"]), encoding="utf-8")
    rc = _run_run(fk_root, data_root, dry_run=True)
    assert rc == 0
    alerts_file = data_root / "watchers" / "waiting-on-alerts.md"
    assert not alerts_file.exists()
    state_file = data_root / "watchers" / "waiting-on-state.json"
    assert not state_file.exists()


def test_run_mixed_ages_only_stale_alerted(tmp_roots) -> None:
    fk_root, data_root = tmp_roots
    stale = _today_minus(15)
    fresh = _today_minus(3)
    (data_root / "TASKS.md").write_text(
        _make_tasks_md(
            [
                f"Old item ({stale})",
                f"New item ({fresh})",
            ]
        ),
        encoding="utf-8",
    )
    rc = _run_run(fk_root, data_root)
    assert rc == 0
    alerts = (data_root / "watchers" / "waiting-on-alerts.md").read_text(encoding="utf-8")
    assert "Old item" in alerts
    assert "New item" not in alerts


def test_run_state_file_written_with_keys(tmp_roots) -> None:
    fk_root, data_root = tmp_roots
    stale = _today_minus(10)
    item = f"Check status ({stale})"
    (data_root / "TASKS.md").write_text(_make_tasks_md([item]), encoding="utf-8")
    _run_run(fk_root, data_root)
    state_file = data_root / "watchers" / "waiting-on-state.json"
    assert state_file.exists()
    state = json.loads(state_file.read_text(encoding="utf-8"))
    # State should have one entry
    assert len(state) == 1
    entry = next(iter(state.values()))
    assert entry["alerted"] is True
    assert entry["item_date"] == stale
