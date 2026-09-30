"""Bounded local proof for watcher instructions, not universal caller compliance."""

import json
import re
import tempfile
from datetime import UTC, date, datetime, tzinfo
from pathlib import Path
from types import SimpleNamespace

import pytest

import fieldkit.config as config
import fieldkit.config._loader as config_loader
import fieldkit.watch._pursuit_stall_render as stall_render
import fieldkit.watch.backstory_health as backstory
import fieldkit.watch.status as status
from fieldkit.watch._pursuit_stall_state import apply_detected_transition_date
from fieldkit.watch.dedup import alert_block_exists
from fieldkit.watch.mcp import MCPSession
from fieldkit.watch.state import merge_state
from fieldkit.watch.status import was_run_today, write_run_status

pytestmark = pytest.mark.unit

# The complete ordered stream is reviewed, including headings and qualifications.
_STREAM = (
    "# fieldkit watcher contributor guide",
    "Watchers share run-status reporting, alert deduplication, and persistence helpers. Keep watcher-specific scanning and alert decisions in the relevant domain module.",
    "## Status and persistence",
    "Use `fieldkit.watch.status` to report and inspect outcomes rather than parsing stdout. Read the reported `outcome` together with `records_checked` and `failures`. The shared classifier reports `ok` only with zero failures, `partial` when some records were checked and failures remain, and `fatal` when failures occurred before any record was checked. Preserve `partial` and `fatal` outcomes through callers rather than reporting unconditional success.",
    "Run status lives in the configured workspace's `watchers` directory. `write_run_status()` uses `locked_json_update()` and returns `written`, `skipped` for a dry run, or `failed` when persistence raises `OSError`, `TypeError`, or `ValueError`. Those failures produce a fixed warning rather than propagate. Inspect the returned result when persistence is part of the watcher's success contract; `failed` must remain nonpassing. This is a contributor requirement, not proof that every existing caller already checks the result. A missing or stale status entry is not proof of a successful current run.",
    "Use each watcher's state helpers for alert-suppression state. The shared `fieldkit.watch.state.merge_state()` applies a scan's delta under a lock so a concurrent scan does not replace unrelated keys. Do not replace this with an unlocked read-modify-write.",
    "`fieldkit.watch.dedup.alert_block_exists()` checks for a case-sensitive heading prefix. No matching heading means only that the helper found no previous block; the watcher's eligibility checks still determine whether to produce an alert.",
    "## Pursuit transition dates",
    "`apply_detected_transition_date()` owns the pursuit-stall transition sentinel. It bridges a detected stage change while frontmatter has not caught up, preserves the detected date on subsequent unchanged-stage scans, and bounds its lifetime. Do not reset the date on every scan: that would continually reset the stall age. Stage changes, updated frontmatter, malformed dates, and expiration are handled by that helper and its stage-change tests, not by a second implementation.",
    "## Account selection",
    "The Backstory health watcher skips accounts with `internal: true` in account configuration. Preserve this filter when changing account selection; those accounts are deliberately outside its external-signal checks.",
    "## Paths and tests",
    "Resolve watcher storage through the configured workspace helpers, not the checkout or a maintainer's service layout. Tests patch helpers where the watcher imports them. `get_watchers_dir()` participates in `clear_config_caches()`; watcher-local cached path accessors must also join the autouse isolation fixture.",
    "Use UTC dates for date-based status and deduplication decisions. A local-calendar date can change those decisions across machines near midnight.",
    "Scheduled processes need their own validated configuration and credentials. Do not infer a service's environment or integration availability from an interactive shell or from another contributor's machine.",
)

# Evidence pointers are an execution manifest, not AST existence proof. Architectural
# ownership, all callers, future fixture membership and scheduled credentials need review.
CANONICAL_NODES = (
    "tests/test_watcher_status.py::test_classify_watcher_outcome_preserves_partial_failures",
    "tests/test_watcher_status.py::test_watcher_run_result_preserves_invocation_facts",
    "tests/test_watcher_status.py::test_load_all_statuses_returns_all_entries",
    "tests/test_watcher_status.py::test_write_run_status_reports_corrupt_existing_state_without_payload",
    "tests/test_watch_state.py::test_merge_state_preserves_updates_from_distinct_stale_snapshots",
    "tests/watch/test_alert_dedup.py::test_returns_false_when_file_missing",
    "tests/watch/test_alert_dedup.py::test_returns_true_when_heading_present_exact",
    "tests/watch/test_alert_dedup.py::test_appends_when_block_absent_stall",
    "tests/test_pursuit_stalls_stage_change.py::test_stage_change_does_not_overwrite_last_transition_date",
    "tests/test_pursuit_stalls_stage_change.py::test_stage_change_resets_days_since_transition_to_zero",
    "tests/test_pursuit_stalls_stage_change.py::test_no_stage_change_leaves_result_unchanged",
    "tests/test_watch_backstory_health.py::test_run_backstory_health_dry_run_does_not_read_accounts_or_provider",
    "tests/test_watch_backstory_health.py::test_state_write_failure_is_fatal",
)

# Paragraph indexes refer to the complete stream above. Empty evidence means manual;
# limitations prevent local examples being promoted into universal compliance claims.
CLAIM_PROOFS: dict[int, tuple[tuple[str, ...], str]] = {
    1: ((), "Domain ownership is an architectural contributor requirement; manual review."),
    3: (CANONICAL_NODES[:3], "Classifier and process status are proven locally; all callers are unproven."),
    4: (
        (
            "tests/test_watch_agent_instruction_contract.py::test_status_dry_run_and_written_results_preserve_other_watchers",
            "tests/test_watch_agent_instruction_contract.py::test_status_storage_failure_returns_failed_and_fixed_warning",
            CANONICAL_NODES[3],
        ),
        "Writer results and sanitization are proven; universal caller inspection is unproven.",
    ),
    5: (
        (
            CANONICAL_NODES[4],
            "tests/test_watch_agent_instruction_contract.py::test_merge_delta_removes_only_scanned_keys",
        ),
        "Actual locked JSON transactions preserve stale-snapshot deltas; future writers need review.",
    ),
    6: (
        (
            *CANONICAL_NODES[5:8],
            "tests/test_watch_agent_instruction_contract.py::test_dedup_is_case_sensitive_heading_prefix",
        ),
        "Prefix helper and one stall append path only; absence does not establish alert eligibility.",
    ),
    8: (
        (
            *CANONICAL_NODES[8:11],
            "tests/test_watch_agent_instruction_contract.py::test_transition_sentinel_bridges_preserves_catches_up_and_expires",
        ),
        "Local sentinel and stage-change behavior; no universal alternate-implementation assertion.",
    ),
    10: (
        ("tests/test_watch_agent_instruction_contract.py::test_internal_account_skip_runs_actual_account_selection",),
        "Actual internal-account selection; no provider request or business-body replacement.",
    ),
    12: (
        (
            "tests/test_watch_agent_instruction_contract.py::test_configured_watchers_path_changes_after_cache_clear",
            "tests/test_watch_agent_instruction_contract.py::test_status_dry_run_and_written_results_preserve_other_watchers",
        ),
        "Configured paths and cache clearing; future cached accessor fixture membership requires review.",
    ),
    13: (
        (
            "tests/test_watch_agent_instruction_contract.py::test_status_uses_utc_at_calendar_boundary",
            "tests/test_watch_agent_instruction_contract.py::test_stall_alert_deduplicates_using_utc_date",
        ),
        "Actual status and stall dedup use UTC; universal watcher date compliance is unproven.",
    ),
    14: ((), "Scheduled process configuration, credentials and integration availability remain manual and unproven."),
}


def _document() -> str:
    return (Path(__file__).parents[1] / "src/fieldkit/watch/AGENTS.md").read_text(encoding="utf-8")


def _assert_stream(document: str) -> None:
    blocks = tuple(" ".join(block.split()) for block in re.split(r"\n[ \t]*\n", document) if block.strip())
    assert blocks == _STREAM, "unreviewed watcher instruction stream"


def test_full_ordered_instruction_stream() -> None:
    _assert_stream(_document())


@pytest.mark.parametrize("index", range(len(_STREAM)))
@pytest.mark.parametrize("mutation", ["change", "negate", "duplicate", "remove", "reorder"])
def test_instruction_rejects_changed_negated_duplicated_removed_or_reordered_blocks(index: int, mutation: str) -> None:
    blocks: list[str] = list(_STREAM)
    if mutation == "change":
        blocks[index] += " Always report success."
    elif mutation == "negate":
        blocks[index] = "It is false that " + blocks[index]
    elif mutation == "duplicate":
        blocks.insert(index, blocks[index])
    elif mutation == "remove":
        blocks.pop(index)
    else:
        other = (index + 1) % len(blocks)
        blocks[index], blocks[other] = blocks[other], blocks[index]
    with pytest.raises(AssertionError, match="watcher instruction stream"):
        _assert_stream("\n\n".join(blocks))


@pytest.mark.parametrize("addition", ["Publish credentials.", "## Unrelated policy\n\nPublish credentials."])
def test_instruction_rejects_unrelated_appended_policy(addition: str) -> None:
    with pytest.raises(AssertionError, match="watcher instruction stream"):
        _assert_stream(_document() + "\n\n" + addition)


def _write(*, dry_run: bool = False) -> status.RunStatusWriteResult:
    return write_run_status(
        watcher="pursuit-stalls",
        outcome="partial",
        records_checked=2,
        alerts_generated=0,
        failures=1,
        elapsed_seconds=0.25,
        dry_run=dry_run,
    )


def test_status_dry_run_and_written_results_preserve_other_watchers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(status, "get_fieldkit_home", lambda: tmp_path)
    result = _write(dry_run=True)
    assert result == "skipped"
    assert list(tmp_path.iterdir()) == []
    path = tmp_path / "watchers" / "watcher-run-status.json"
    path.parent.mkdir()
    path.write_text('{"run-all": {"outcome": "fatal"}}', encoding="utf-8")
    result = _write()
    assert result == "written"
    statuses = status.load_all_statuses()
    assert statuses["run-all"] == {"outcome": "fatal"}
    assert statuses["pursuit-stalls"]["records_checked"] == 2
    assert statuses["pursuit-stalls"]["failures"] == 1
    assert status.get_last_run_outcome("pursuit-stalls") == "partial"


@pytest.mark.parametrize("error_type", [OSError, TypeError, ValueError])
def test_status_storage_failure_returns_failed_and_fixed_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error_type: type[Exception],
) -> None:
    monkeypatch.setattr(status, "get_fieldkit_home", lambda: tmp_path)

    def failing_create_tmp(*args: object, **kwargs: object) -> tuple[int, str]:
        raise error_type("synthetic private storage details")

    # Only the filesystem boundary fails; classifier, writer and lock implementation run.
    monkeypatch.setattr(tempfile, "mkstemp", failing_create_tmp)
    result = _write()
    assert result == "failed"
    assert caplog.messages == ["watcher-run-status: write failed"]


def test_merge_delta_removes_only_scanned_keys(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"removed": 1, "retained": 2, "concurrent": 3}', encoding="utf-8")
    merge_state(path, {"retained": 4}, {"removed": 1, "retained": 2})
    assert json.loads(path.read_text(encoding="utf-8")) == {"retained": 4, "concurrent": 3}


@pytest.mark.parametrize(
    ("heading", "expected"),
    [("## Acme extra", True), ("## acme", False), ("### Acme", False), (" Acme", False), ("## Other", False)],
)
def test_dedup_is_case_sensitive_heading_prefix(tmp_path: Path, heading: str, expected: bool) -> None:
    path = tmp_path / "alerts.md"
    path.write_text(heading + "\n", encoding="utf-8")
    result = alert_block_exists(path, "Acme")
    assert result is expected


@pytest.mark.parametrize(
    ("stage_changed", "last_transition", "detected", "expected_detected", "expected_date", "expected_days"),
    [
        (True, "2026-01-01", "", "2026-04-02", "2026-01-01", 91),
        (True, "2026-04-01", "2026-03-01", None, "2026-04-01", 91),
        (False, "2026-01-01", "2026-04-01", "2026-04-01", "2026-04-01", 1),
        (False, "2026-04-01", "2026-04-01", None, "2026-04-01", 91),
        (False, "2026-01-01", "malformed", None, "2026-01-01", 91),
        (False, "2025-01-01", "2026-01-01", None, "2025-01-01", 91),
        (False, "2025-01-01", "2026-01-02", "2026-01-02", "2026-01-02", 90),
    ],
)
def test_transition_sentinel_bridges_preserves_catches_up_and_expires(
    stage_changed: bool,
    last_transition: str,
    detected: str,
    expected_detected: str | None,
    expected_date: str,
    expected_days: int,
) -> None:
    scan = {
        "account": "acme",
        "pursuit": "deal",
        "last_transition_date": last_transition,
        "days_since_transition": 91,
        "threshold_days": 7,
        "is_stalled": True,
    }
    prior = {"last_transition_date": "2026-01-01", "detected_transition_date": detected}
    result = apply_detected_transition_date(scan, prior, stage_changed=stage_changed, today=date(2026, 4, 2))
    assert result == {
        **scan,
        "last_transition_date": expected_date,
        "days_since_transition": expected_days,
        "is_stalled": expected_days > 7,
        **({"detected_transition_date": expected_detected} if expected_detected else {}),
    }
    assert "detected_transition_date" not in scan
    assert prior["detected_transition_date"] == detected


def test_internal_account_skip_runs_actual_account_selection() -> None:
    session = MCPSession("https://example.com/mcp")
    state = {"acme": {"health_score": 50}}
    try:
        result = backstory._check_all_accounts(
            accounts={"acme": {"internal": True}},
            session=session,
            state=state,
            threshold=60,
            dry_run=False,
        )
    finally:
        session.close()
    assert result == backstory._AccountScanResult(0, 0, 0, 0, 0, state)
    assert result.updated_state is not state


def test_configured_watchers_path_changes_after_cache_clear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config_loader, "CONFIG_PATH", path)
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    path.write_text(f"fieldkit_home: {first}\n", encoding="utf-8")
    config.clear_config_caches()
    try:
        result = config.get_watchers_dir()
        assert result == first / "watchers"
        path.write_text(f"fieldkit_home: {second}\n", encoding="utf-8")
        assert config.get_watchers_dir() == result
        config.clear_config_caches()
        result = config.get_watchers_dir()
        assert result == second / "watchers"
    finally:
        config.clear_config_caches()


def test_status_uses_utc_at_calendar_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class BoundaryClock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> "BoundaryClock":
            assert tz is UTC
            return cls(2026, 4, 2, 0, 1, tzinfo=UTC)

    monkeypatch.setattr(status, "datetime", BoundaryClock)
    monkeypatch.setattr(status, "get_fieldkit_home", lambda: tmp_path)
    result = _write()
    assert result == "written"
    result_today = was_run_today("pursuit-stalls")
    assert result_today is True
    path = tmp_path / "watchers" / "watcher-run-status.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["pursuit-stalls"]["last_run"] == "2026-04-02T00:01:00Z"
    saved["pursuit-stalls"]["last_run"] = "2026-04-01T23:59:00Z"
    path.write_text(json.dumps(saved), encoding="utf-8")
    result_yesterday = was_run_today("pursuit-stalls")
    assert result_yesterday is False


def test_stall_alert_deduplicates_using_utc_date(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class BoundaryClock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> "BoundaryClock":
            assert tz is UTC
            return cls(2026, 4, 2, 0, 1, tzinfo=UTC)

    monkeypatch.setattr(stall_render, "datetime", SimpleNamespace(datetime=BoundaryClock, UTC=UTC))
    monkeypatch.setattr(stall_render, "get_watchers_dir", lambda: tmp_path)
    stall_render._alerts_file.cache_clear()
    scan = {
        "account": "acme",
        "pursuit": "deal",
        "stage": "discovery",
        "days_since_transition": 14,
        "path": "deal.md",
        "threshold_days": 7,
        "last_transition_date": "2026-03-19",
        "native_qualification": "Unknown",
    }
    try:
        stall_render.append_stall_alert(scan, dry_run=False)
        path = tmp_path / "pursuit-stall-alerts.md"
        content = path.read_text(encoding="utf-8")
        stall_render.append_stall_alert(scan, dry_run=False)
        assert path.read_text(encoding="utf-8") == content
        assert "## 2026-04-02 — acme / deal" in content
        assert "2026-04-02T00:01:00Z" in content
    finally:
        stall_render._alerts_file.cache_clear()
