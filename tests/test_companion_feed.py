"""Tests for fieldkit.companion.feed — parsers, cursor, feed assembly."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fieldkit.companion.feed import (
    AttentionItem,
    FeedParseError,
    get_feed,
    load_cursor,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)

_STATUS = {
    "run-all": {
        "outcome": "ok",
        "failures": 0,
        "last_run": "2026-09-10T11:00:00Z",
        "alerts_generated": 0,
    },
    "pursuit-stalls": {
        "outcome": "ok",
        "failures": 0,
        "last_run": "2026-07-09T21:49:31Z",
        "alerts_generated": 13,
    },
    "close-date-countdown": {
        "outcome": "ok",
        "failures": 1,
        "last_run": "2026-07-09T21:49:31Z",
        "alerts_generated": 0,
    },
    "backstory-health": {
        "outcome": "fatal",
        "failures": 3,
        "last_run": "2026-07-09T21:49:37Z",
        "alerts_generated": 0,
    },
}

_STALL_ALERTS = """# Pursuit Stall Alerts

Automated alerts written by watch_pursuit_stalls.py.

## 2026-06-03 — acme / add-on-services — stalled in validate

- **Account:** `acme`
- **Pursuit:** [add-on-services](accounts/acme/pursuits/add-on-services.md)
- **Days stuck:** 29 (threshold: 14)

## 2026-06-04 — globex / ocp-virt — stalled in discover

- **Account:** `globex`
- **Days stuck:** 15 (threshold: 14)
"""

_TASKS_MD = """# Tasks

## Today

- [ ] something

## Waiting On
<!-- Format: - **[Account/Pursuit]** Waiting on [person] -->

- **[Acme]** Waiting on llesosky re: migration hours

## Done
"""


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, Path]:
    """(home, data_path) with watchers dir, status JSON, alerts, TASKS.md."""
    home = tmp_path / "home"
    data = tmp_path / "data"
    watchers = home / "watchers"
    watchers.mkdir(parents=True)
    data.mkdir()
    (watchers / "watcher-run-status.json").write_text(json.dumps(_STATUS), encoding="utf-8")
    (watchers / "pursuit-stall-alerts.md").write_text(_STALL_ALERTS, encoding="utf-8")
    (home / "TASKS.md").write_text(_TASKS_MD, encoding="utf-8")
    return home, data


def test_feed_returns_well_formed_items(workspace: tuple[Path, Path]) -> None:
    home, data = workspace
    result = get_feed(home, data, since_cursor=False, now=_NOW)
    assert len(result) == 5
    assert all(isinstance(i, AttentionItem) for i in result)
    for item in result:
        d = item.to_dict()
        assert set(d) == {
            "item_id",
            "source",
            "account",
            "severity",
            "summary",
            "evidence_path",
            "suggested_skill",
            "observed_at",
        }
        assert d["item_id"]
        assert d["severity"] in ("critical", "warning", "info")


def test_feed_surfaces_watcher_failures_and_fatal(workspace: tuple[Path, Path]) -> None:
    home, data = workspace
    result = get_feed(home, data, since_cursor=False)
    sources = {i.source: i for i in result}
    # ok + 0 failures → suppressed; failures>0 or fatal → surfaced
    assert "run-status/pursuit-stalls" not in sources
    assert sources["run-status/close-date-countdown"].severity == "critical"
    assert sources["run-status/backstory-health"].severity == "critical"  # fatal escalates


def test_run_status_item_id_is_stable_across_watcher_reruns(workspace: tuple[Path, Path]) -> None:
    """A stuck watcher must keep its item_id when only last_run advances.

    The watcher timer rewrites last_run on every fire. If the id were derived
    from it, journal suppression would never match, so each companion pass would
    write another proposal file and spawn another subprocess — unbounded growth
    for one unchanged condition.
    """
    home, data = workspace
    status_path = home / "watchers" / "watcher-run-status.json"

    before = {i.source: i.item_id for i in get_feed(home, data, since_cursor=False)}

    rerun = json.loads(status_path.read_text(encoding="utf-8"))
    for entry in rerun.values():
        entry["last_run"] = "2026-07-09T23:59:59Z"
    status_path.write_text(json.dumps(rerun), encoding="utf-8")

    after = {i.source: i.item_id for i in get_feed(home, data, since_cursor=False)}

    assert after["run-status/backstory-health"] == before["run-status/backstory-health"]
    assert after["run-status/close-date-countdown"] == before["run-status/close-date-countdown"]


def test_run_status_item_id_changes_when_condition_changes(workspace: tuple[Path, Path]) -> None:
    """A genuinely different outcome or failure count is a new item.

    The counterpart to stability: suppression must not hide a watcher that has
    degraded further since it was last handled.
    """
    home, data = workspace
    status_path = home / "watchers" / "watcher-run-status.json"

    before = {i.source: i.item_id for i in get_feed(home, data, since_cursor=False)}

    degraded = json.loads(status_path.read_text(encoding="utf-8"))
    degraded["backstory-health"]["failures"] = 99
    status_path.write_text(json.dumps(degraded), encoding="utf-8")

    after = {i.source: i.item_id for i in get_feed(home, data, since_cursor=False)}

    assert after["run-status/backstory-health"] != before["run-status/backstory-health"]


def test_feed_parses_alert_blocks_with_account_and_skill(workspace: tuple[Path, Path]) -> None:
    home, data = workspace
    result = get_feed(home, data, since_cursor=False)
    stalls = [i for i in result if i.source == "alert/pursuit-stall"]
    assert len(stalls) == 2
    accounts = {i.account for i in stalls}
    assert accounts == {"acme", "globex"}
    assert all(i.suggested_skill == "grill" for i in stalls)
    assert all(i.severity == "warning" for i in stalls)


def test_feed_parses_waiting_on_from_tasks_md(workspace: tuple[Path, Path]) -> None:
    home, data = workspace
    result = get_feed(home, data, since_cursor=False)
    waiting = [i for i in result if i.source == "tasks/waiting-on"]
    assert len(waiting) == 1
    assert waiting[0].account == "acme"
    assert "llesosky" in waiting[0].summary
    assert waiting[0].suggested_skill == "task-management"


def test_feed_severity_orders_critical_first(workspace: tuple[Path, Path]) -> None:
    home, data = workspace
    result = get_feed(home, data, since_cursor=False)
    severities = [i.severity for i in result]
    assert severities == sorted(severities, key={"critical": 0, "warning": 1, "info": 2}.__getitem__)


def test_cursor_advances_and_suppresses_redelivery(workspace: tuple[Path, Path]) -> None:
    home, data = workspace
    first = get_feed(home, data, since_cursor=True, now=_NOW)
    assert len(first) == 5
    second = get_feed(home, data, since_cursor=True, now=_NOW)
    assert second == []
    # --all still sees everything and does not advance the cursor
    everything = get_feed(home, data, since_cursor=False, now=_NOW)
    assert len(everything) == len(first)
    assert load_cursor(data) == {i.item_id for i in first}


def test_feed_account_filter(workspace: tuple[Path, Path]) -> None:
    home, data = workspace
    result = get_feed(home, data, since_cursor=False, account_slug="globex")
    assert len(result) == 1
    assert all(i.account == "globex" for i in result)


def test_feed_journal_suppression(workspace: tuple[Path, Path]) -> None:
    home, data = workspace
    all_items = get_feed(home, data, since_cursor=False)
    handled = all_items[0].item_id
    result = get_feed(home, data, since_cursor=False, suppressed={handled})
    assert handled not in {i.item_id for i in result}
    assert len(result) == len(all_items) - 1


def test_missing_watcher_directory_emits_stable_critical_liveness_item(tmp_path: Path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    home.mkdir()
    data.mkdir()
    first = get_feed(home, data, since_cursor=False, now=_NOW)
    assert len(first) == 1
    assert first[0].source == "source-liveness/watchers"
    assert first[0].severity == "critical"
    assert "missing" in first[0].summary.lower()

    second = get_feed(home, data, since_cursor=False, now=_NOW)
    assert second[0].item_id == first[0].item_id


def test_missing_status_file_emits_liveness_item(tmp_path: Path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    (home / "watchers").mkdir(parents=True)
    data.mkdir()

    result = get_feed(home, data, since_cursor=False, now=_NOW)
    assert len(result) == 1
    assert "status is missing" in result[0].summary.lower()


def test_recent_aggregate_status_with_no_items_is_healthy_empty(tmp_path: Path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    watchers = home / "watchers"
    watchers.mkdir(parents=True)
    data.mkdir()
    status = {"run-all": {"outcome": "ok", "failures": 0, "last_run": "2026-09-10T11:00:00Z"}}
    (watchers / "watcher-run-status.json").write_text(json.dumps(status), encoding="utf-8")

    result = get_feed(home, data, since_cursor=False, now=_NOW)
    assert result == []


@pytest.mark.parametrize(
    ("status", "summary_fragment"),
    [
        ({}, "lacks the aggregate"),
        ({"run-all": {"last_run": "not-a-time"}}, "timestamp is invalid"),
        ({"run-all": {"last_run": "2026-09-09T08:00:00Z"}}, "older than 26 hours"),
    ],
)
def test_unhealthy_aggregate_status_emits_liveness_item(
    tmp_path: Path, status: dict[str, object], summary_fragment: str
) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    watchers = home / "watchers"
    watchers.mkdir(parents=True)
    data.mkdir()
    (watchers / "watcher-run-status.json").write_text(json.dumps(status), encoding="utf-8")

    result = get_feed(home, data, since_cursor=False, now=_NOW)
    assert len(result) == 1
    assert summary_fragment in result[0].summary


def test_stale_source_preserves_coexisting_alert_items(workspace: tuple[Path, Path]) -> None:
    home, data = workspace
    status_path = home / "watchers" / "watcher-run-status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["run-all"]["last_run"] = "2026-09-09T08:00:00Z"
    status_path.write_text(json.dumps(status), encoding="utf-8")

    result = get_feed(home, data, since_cursor=False, now=_NOW)
    assert len(result) == 6
    assert {item.source for item in result} >= {"source-liveness/watchers", "alert/pursuit-stall"}


def test_feed_item_ids_stable_across_reruns(workspace: tuple[Path, Path]) -> None:
    home, data = workspace
    first = {i.item_id for i in get_feed(home, data, since_cursor=False)}
    second = {i.item_id for i in get_feed(home, data, since_cursor=False)}
    assert first == second


def test_malformed_status_json_raises_parse_error(workspace: tuple[Path, Path]) -> None:
    home, data = workspace
    (home / "watchers" / "watcher-run-status.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(FeedParseError, match="unparseable"):
        get_feed(home, data, since_cursor=False)
