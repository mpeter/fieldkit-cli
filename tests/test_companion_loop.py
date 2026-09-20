"""Tests for fieldkit.companion.loop.run_once — the single-pass headless brain.

Covers the tier ladder, journal-based idempotency, the auth-vs-partial exit
distinction (proctor C1), and the write-before-journal ordering (proctor C2).
"""

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from unittest.mock import MagicMock

import pytest

from fieldkit.companion import loop as loop_module
from fieldkit.companion.decide import ProposedAction
from fieldkit.companion.feed import FeedParseError, get_feed
from fieldkit.companion.llm_decide import DecisionResult
from fieldkit.companion.loop import run_once
from fieldkit.companion.outbox import list_proposals, outbox_dir
from fieldkit.companion.suppress import DEFAULT_COOLDOWN, retired_item_ids
from fieldkit.watch.constants import KNOWN_WATCHERS

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _disable_live_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep loop unit tests offline unless they replace the decision boundary."""
    monkeypatch.setenv("NO_LLM", "1")


# Two dated stall blocks, each carrying an Account bullet, so the feed yields two
# account-bearing items whose deterministic command is a read-only pursuit health.
_ALERTS = """# Pursuit Stall Alerts

## 2026-06-03 — acme / add-on — stalled in validate

- **Account:** `acme`
- **Days stuck:** 29

## 2026-06-04 — globex / ocp — stalled in discover

- **Account:** `globex`
- **Days stuck:** 15
"""


def _make_home(tmp_path: Path, *, alerts: str = _ALERTS) -> Path:
    home = tmp_path / "home"
    (home / "watchers").mkdir(parents=True)
    status = {
        "run-all": {
            "outcome": "ok",
            "failures": 0,
            "last_run": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    }
    (home / "watchers" / "watcher-run-status.json").write_text(json.dumps(status), encoding="utf-8")
    (home / "watchers" / "pursuit-stall-alerts.md").write_text(alerts, encoding="utf-8")
    return home


def _make_data(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    return data


def _read_journal(data_path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in sorted(data_path.glob("companion-journal-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            records.append(json.loads(line))
    return records


def _patch_subprocess(monkeypatch: pytest.MonkeyPatch, *, returncode: int, stdout: str = "") -> MagicMock:
    completed = subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")
    spawn = MagicMock(return_value=completed)
    monkeypatch.setattr("fieldkit.companion.runner.subprocess.run", spawn)
    return spawn


def test_propose_writes_outbox_and_journals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, data = _make_home(tmp_path), _make_data(tmp_path)
    spawn = _patch_subprocess(monkeypatch, returncode=0, stdout="HEALTH-JSON")

    result = run_once(home, data, tier="propose", allowlist=[])
    assert result.proposed == 2  # return-bound var asserted first (gaze CR-014)
    assert result.enriched == 2
    assert result.acted == 0
    assert result.auth_failures == 0

    files = list_proposals(data)
    assert len(files) == 2
    assert "HEALTH-JSON" in files[0].markdown
    journal = _read_journal(data)
    assert len(journal) == 2
    assert all(record["decision_provenance"] == "deterministic" for record in journal)
    assert all(record["fallback_category"] == "disabled" for record in journal)
    # Only read-only pursuit health ran — no mutating command.
    for call in spawn.call_args_list:
        assert "health" in call.args[0]


def test_read_tier_triages_without_outbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, data = _make_home(tmp_path), _make_data(tmp_path)
    _patch_subprocess(monkeypatch, returncode=0, stdout="ctx")

    result = run_once(home, data, tier="read", allowlist=[])
    assert result.triaged == 2
    assert result.proposed == 0
    assert not outbox_dir(data).exists()
    assert len(_read_journal(data)) == 2


def _llm_candidate(item: object, baseline: ProposedAction, _enrichment: str | None) -> DecisionResult:
    account = baseline.enrichment_argv[-2] if baseline.enrichment_argv is not None else "acme"
    action = ProposedAction(
        skill="grill",
        rationale="The pursuit is stalled.",
        enrichment_argv=baseline.enrichment_argv,
        command_argv=("pursuit", "advance", account, "--dry-run"),
        recommendation="Preview the next stage change.",
    )
    return DecisionResult(action, "llm", attempted=True)


def test_propose_writes_llm_candidate_without_executing_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, data = _make_home(tmp_path), _make_data(tmp_path)
    spawn = _patch_subprocess(monkeypatch, returncode=0, stdout="HEALTH-JSON")
    result = run_once(home, data, tier="propose", allowlist=[], decision_fn=_llm_candidate)

    assert result.proposed == 2
    assert result.llm_attempts == 2
    assert result.llm_decisions == 2
    assert result.denied == 0
    assert spawn.call_count == 2
    assert all(proposal.command_argv is not None for proposal in list_proposals(data))


@pytest.mark.parametrize(
    "allowlist,expected_acted,expected_denied,expected_spawns",
    [([], 0, 2, 2), (["pursuit advance --dry-run"], 2, 0, 4)],
)
def test_act_still_applies_existing_gate_to_llm_candidate(
    allowlist: list[str],
    expected_acted: int,
    expected_denied: int,
    expected_spawns: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, data = _make_home(tmp_path), _make_data(tmp_path)
    spawn = _patch_subprocess(monkeypatch, returncode=0, stdout="ok")
    result = run_once(home, data, tier="act", allowlist=allowlist, decision_fn=_llm_candidate)

    assert result.acted == expected_acted
    assert result.denied == expected_denied
    assert spawn.call_count == expected_spawns
    journal = _read_journal(data)
    assert len(journal) == 4
    assert sum("health" in str(record["action"]) for record in journal) == 2
    assert sum("advance" in str(record["action"]) for record in journal) == 2


def test_act_journals_auth_failed_enrichment_before_distinct_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, data = _make_home(tmp_path), _make_data(tmp_path)

    def auth_failure(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "advance" in argv:
            account = argv[-2]
            latest = _read_journal(data)[-1]
            assert latest["action"] == f"pursuit health --account {account} --json"
            assert latest["exit_code"] == 2
        return subprocess.CompletedProcess(args=argv, returncode=2, stdout="", stderr="auth failed")

    spawn = MagicMock(side_effect=auth_failure)
    monkeypatch.setattr("fieldkit.companion.runner.subprocess.run", spawn)

    result = run_once(
        home,
        data,
        tier="act",
        allowlist=["pursuit advance --dry-run"],
        decision_fn=_llm_candidate,
    )

    assert result.auth_failures == 4
    assert spawn.call_count == 4
    journal = _read_journal(data)
    assert len(journal) == 4
    enrichment_records = [record for record in journal if "health" in str(record["action"])]
    assert len(enrichment_records) == 2
    assert all(record["exit_code"] == 2 for record in enrichment_records)


@pytest.mark.parametrize("tier", ["read", "propose", "act"])
def test_run_once_keeps_unresolved_items_live_without_action_artifacts(
    tier: Literal["read", "propose", "act"], tmp_path: Path
) -> None:
    alerts = """# Pursuit Stall Alerts

## 2026-06-03 — stalled in validate

- **Days stuck:** 29
"""
    home, data = _make_home(tmp_path, alerts=alerts), _make_data(tmp_path)

    result = run_once(home, data, tier=tier, allowlist=[])

    assert result.unresolved == 1
    assert result.handled == 1
    assert result.proposed == 0
    assert result.triaged == 0
    assert result.enriched == 0
    assert result.acted == 0
    assert not outbox_dir(data).exists()
    assert _read_journal(data) == []
    assert retired_item_ids(data) == set()

    next_result = run_once(home, data, tier=tier, allowlist=[])
    assert next_result.unresolved == 1


def test_second_pass_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, data = _make_home(tmp_path), _make_data(tmp_path)
    _patch_subprocess(monkeypatch, returncode=0, stdout="ctx")

    run_once(home, data, tier="propose", allowlist=[])
    second = run_once(home, data, tier="propose", allowlist=[])
    assert second.handled == 0
    assert second.proposed == 0
    assert len(list_proposals(data)) == 2  # no new proposals


def test_auth_failure_leaves_the_item_live_for_the_next_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An expired credential must not cost an item its place in the feed.

    Previously every path journaled, and journaling retired the item, so items
    live during an auth outage were gone permanently — re-authenticating did not
    bring them back.
    """
    home, data = _make_home(tmp_path), _make_data(tmp_path)
    _patch_subprocess(monkeypatch, returncode=2)

    first = run_once(home, data, tier="read", allowlist=[])

    assert first.auth_failures == 2
    assert len(_read_journal(data)) == 2  # recorded as evidence
    assert retired_item_ids(data) == set()  # but not retired

    _patch_subprocess(monkeypatch, returncode=0, stdout="ctx")
    second = run_once(home, data, tier="read", allowlist=[])
    assert second.triaged == 2


def test_enrich_failure_leaves_the_item_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient nonzero exit is retryable, so the item must survive it."""
    home, data = _make_home(tmp_path), _make_data(tmp_path)
    _patch_subprocess(monkeypatch, returncode=1)

    result = run_once(home, data, tier="read", allowlist=[])

    assert result.enrich_failures == 2
    assert retired_item_ids(data) == set()


def test_pursuit_health_exit_1_remains_a_visible_enrichment_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default health reports exit 0, so exit 1 remains a visible failure."""
    home, data = _make_home(tmp_path), _make_data(tmp_path)
    _patch_subprocess(monkeypatch, returncode=1, stdout='[{"risk_tier": "HIGH"}]')

    result = run_once(home, data, tier="propose", allowlist=[])

    assert result.enriched == 0
    assert result.enrich_failures == 2


def test_read_tier_retires_only_until_the_cooldown_expires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Read tier withholds an item for a cooldown, not forever.

    The condition (a stalled pursuit) is still true after the pass, so the item
    has to come back. A cooldown only stops the hourly timer from re-spawning
    the same enrichment subprocess all day.
    """
    home, data = _make_home(tmp_path), _make_data(tmp_path)
    _patch_subprocess(monkeypatch, returncode=0, stdout="ctx")

    first = run_once(home, data, tier="read", allowlist=[])

    assert first.triaged == 2
    assert len(retired_item_ids(data)) == 2

    second = run_once(home, data, tier="read", allowlist=[])
    assert second.handled == 0  # not re-spawned during the cooldown

    later = datetime.now(tz=UTC) + DEFAULT_COOLDOWN + timedelta(minutes=1)
    assert retired_item_ids(data, now=later) == set()  # back afterwards


def test_malformed_feed_raises_feedparseerror(tmp_path: Path) -> None:
    home, data = _make_home(tmp_path), _make_data(tmp_path)
    (home / "watchers" / "watcher-run-status.json").write_text("{bad json", encoding="utf-8")
    with pytest.raises(FeedParseError, match="unparseable"):
        run_once(home, data, tier="read", allowlist=[])


def test_enrichment_failure_degrades_to_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, data = _make_home(tmp_path), _make_data(tmp_path)
    _patch_subprocess(monkeypatch, returncode=1, stdout="")

    result = run_once(home, data, tier="propose", allowlist=[])
    assert result.enrich_failures == 2  # return-bound var asserted first
    assert result.partial is True
    assert result.auth_failures == 0
    files = list_proposals(data)
    assert len(files) == 2
    assert "enrichment unavailable" in files[0].markdown


def test_auth_failure_is_distinct_not_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A routed command exiting 2 (auth) must count as auth_failure, not enrich (proctor C1)."""
    home, data = _make_home(tmp_path), _make_data(tmp_path)
    _patch_subprocess(monkeypatch, returncode=2, stdout="")

    result = run_once(home, data, tier="propose", allowlist=[])
    assert result.auth_failures == 2  # return-bound var asserted first
    assert result.enrich_failures == 0
    assert result.partial is False
    # The proposal is still written even when the context read auth-failed.
    assert len(list_proposals(data)) == 2


def test_write_failure_leaves_item_unjournaled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """proctor C2: proposal is journaled only after it is durable, so a write failure re-attempts."""
    home, data = _make_home(tmp_path), _make_data(tmp_path)
    _patch_subprocess(monkeypatch, returncode=0, stdout="ctx")
    monkeypatch.setattr(loop_module, "write_proposal", MagicMock(side_effect=OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        run_once(home, data, tier="propose", allowlist=[])
    # Nothing journaled → the item is not suppressed → it is retried next pass.
    assert _read_journal(data) == []


# historic regression: gate wiring reached only through a monkeypatched mutating proposal.
@pytest.mark.characterization
def test_act_runs_allowlisted_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, data = _make_home(tmp_path), _make_data(tmp_path)
    mutating = ProposedAction(
        skill=None, rationale="r", enrichment_argv=None, command_argv=("pursuit", "advance", "acme", "--dry-run")
    )
    monkeypatch.setattr(loop_module, "propose_for", lambda _item: mutating)
    spawn = _patch_subprocess(monkeypatch, returncode=0, stdout="")

    result = run_once(home, data, tier="act", allowlist=["pursuit advance --dry-run"])
    assert result.acted == 2  # return-bound var asserted first
    assert result.denied == 0
    assert result.enriched == 0
    spawn.assert_called()


# historic regression: gate wiring reached only through a monkeypatched mutating proposal.
@pytest.mark.characterization
def test_act_denies_non_allowlisted_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, data = _make_home(tmp_path), _make_data(tmp_path)
    mutating = ProposedAction(
        skill=None, rationale="r", enrichment_argv=None, command_argv=("sf", "set-next-steps", "006", "x")
    )
    monkeypatch.setattr(loop_module, "propose_for", lambda _item: mutating)
    spawn = _patch_subprocess(monkeypatch, returncode=0, stdout="")

    result = run_once(home, data, tier="act", allowlist=[])
    assert result.denied == 2  # return-bound var asserted first
    assert result.acted == 0
    spawn.assert_not_called()  # denial never spawns a subprocess
    assert len(list_proposals(data)) == 2  # proposal still written


def test_dry_run_has_no_side_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, data = _make_home(tmp_path), _make_data(tmp_path)
    spawn = _patch_subprocess(monkeypatch, returncode=0, stdout="")

    result = run_once(home, data, tier="propose", allowlist=[], dry_run=True)
    assert result.proposed == 2
    spawn.assert_not_called()
    assert not outbox_dir(data).exists()
    assert not list(data.glob("companion-journal-*.jsonl"))


def test_dry_run_does_not_invoke_decision_provider(tmp_path: Path) -> None:
    home, data = _make_home(tmp_path), _make_data(tmp_path)
    decision = MagicMock()

    result = run_once(home, data, tier="propose", allowlist=[], dry_run=True, decision_fn=decision)

    assert result.llm_attempts == 0
    assert result.llm_fallbacks == 2
    decision.assert_not_called()


def test_feed_is_capped_at_max_items_per_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Items beyond _MAX_ITEMS_PER_PASS are dropped; criticals-first ordering makes truncation safe.

    "Safe" specifically means the retained items are the FEED's first N (get_feed
    already sorts critical-first), not just any N — so this asserts on identity/order,
    not only on count (proctor F6).
    """
    home, data = _make_home(tmp_path), _make_data(tmp_path)

    oversized = [MagicMock(item_id=f"item-{i}", source="test") for i in range(loop_module._MAX_ITEMS_PER_PASS + 5)]
    monkeypatch.setattr(loop_module, "get_feed", lambda *_a, **_kw: oversized)
    propose_spy = MagicMock(
        return_value=ProposedAction(skill=None, rationale="r", enrichment_argv=None, command_argv=None)
    )
    monkeypatch.setattr(loop_module, "propose_for", propose_spy)

    result = run_once(home, data, tier="read", allowlist=[])
    assert result.unresolved == loop_module._MAX_ITEMS_PER_PASS
    assert result.dropped == 5

    processed_ids = [call.args[0].item_id for call in propose_spy.call_args_list]
    assert processed_ids == [item.item_id for item in oversized[: loop_module._MAX_ITEMS_PER_PASS]]


def test_feed_overflow_logs_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A capped pass is not a silent tail-drop — it must be observable (proctor F4)."""
    home, data = _make_home(tmp_path), _make_data(tmp_path)

    oversized = [MagicMock(item_id=f"item-{i}", source="test") for i in range(loop_module._MAX_ITEMS_PER_PASS + 5)]
    monkeypatch.setattr(loop_module, "get_feed", lambda *_a, **_kw: oversized)
    monkeypatch.setattr(
        loop_module,
        "propose_for",
        lambda _: ProposedAction(skill=None, rationale="r", enrichment_argv=None, command_argv=None),
    )

    with caplog.at_level("WARNING", logger=loop_module.__name__):
        run_once(home, data, tier="read", allowlist=[])
    assert any("dropping 5" in record.message for record in caplog.records)


def test_feed_under_cap_does_not_log_or_drop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, data = _make_home(tmp_path), _make_data(tmp_path)
    _patch_subprocess(monkeypatch, returncode=0, stdout="ctx")

    result = run_once(home, data, tier="read", allowlist=[])
    assert result.dropped == 0


def test_round_robin_gives_every_severity_tier_a_share_each_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """proctor F3, fixed (Option B — round-robin across severity tiers).

    A failed enrichment never cools down (suppress.py: cooldown is a success-only
    artifact), so a feed with more than _MAX_ITEMS_PER_PASS always-failing
    critical items reproduces an identical overflow on every pass. Before this
    fix, naive severity-first truncation meant the same critical head consumed
    the whole cap and permanently starved a coexisting lower-severity item.
    Round-robin selection guarantees every present tier gets a slot each pass,
    so the info item below is never excluded — verified across 3 consecutive
    passes, not just the first.
    """
    home = tmp_path / "home"
    (home / "watchers").mkdir(parents=True)
    leaf_watchers = sorted(KNOWN_WATCHERS - {"run-all"})[:8]
    status = {
        watcher: {"outcome": "fatal", "failures": 1, "last_run": "2026-07-27T00:00:00Z"} for watcher in leaf_watchers
    }
    status["run-all"] = {
        "outcome": "ok",
        "failures": 0,
        "last_run": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    (home / "watchers" / "watcher-run-status.json").write_text(json.dumps(status), encoding="utf-8")
    # An account bullet gives this item a real read-only command (decide.py
    # propose_for -> _account_read), so it goes through run_action like the
    # criticals below and fails the same way. An account-less item remains live
    # but has no failing command result, which is not what this test exercises.
    (home / "TASKS.md").write_text("## Waiting On\n\n- **[acme]** an info-severity item\n", encoding="utf-8")
    data = _make_data(tmp_path)
    _patch_subprocess(monkeypatch, returncode=1)  # every enrichment fails -> never cools down

    feed = get_feed(home, data, since_cursor=False, suppressed=set())
    info_id = next(i.item_id for i in feed if i.severity == "info")

    seen_so_far = 0
    for _ in range(3):
        result = run_once(home, data, tier="read", allowlist=[])
        assert result.dropped == 2  # 9 items over a cap of 7, every pass (nothing ever cools down)
        journal = _read_journal(data)
        this_pass_ids = {r["item_id"] for r in journal[seen_so_far:]}
        seen_so_far = len(journal)
        assert len(this_pass_ids) == 7
        assert info_id in this_pass_ids  # the info tier is never fully excluded


def test_duplicate_command_argv_runs_subprocess_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """implementation note: two items with identical command_argv must not spawn two subprocesses."""
    home, data = _make_home(tmp_path), _make_data(tmp_path)
    shared_cmd = ("pursuit", "health", "acme", "--json")
    shared_action = ProposedAction(skill=None, rationale="r", enrichment_argv=None, command_argv=shared_cmd)
    monkeypatch.setattr(loop_module, "propose_for", lambda _item: shared_action)
    spawn = _patch_subprocess(monkeypatch, returncode=0, stdout="HEALTH-JSON")

    result = run_once(home, data, tier="read", allowlist=[])

    assert result.enriched == 2  # both items received enrichment context (gaze CR-014)
    assert spawn.call_count == 1  # deduplicated: one subprocess for the shared command_argv


def test_duplicate_command_argv_failure_not_cached_for_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """proctor finding 1: a failed (non-denied) attempt must not be cached.

    Two items share command_argv. The shared command fails with a nonzero,
    non-auth exit code — a transient blip. Each item must still get its own
    independent subprocess attempt rather than inheriting the first item's
    failure, and both failures must be counted and journaled per-item.
    """
    home, data = _make_home(tmp_path), _make_data(tmp_path)
    shared_cmd = ("pursuit", "health", "acme", "--json")
    shared_action = ProposedAction(skill=None, rationale="r", enrichment_argv=None, command_argv=shared_cmd)
    monkeypatch.setattr(loop_module, "propose_for", lambda _item: shared_action)
    spawn = _patch_subprocess(monkeypatch, returncode=1)

    result = run_once(home, data, tier="read", allowlist=[])

    assert result.enrich_failures == 2  # both items independently failed, none reused a cached failure
    assert spawn.call_count == 2  # not deduplicated: a failed result gives no chance-to-succeed to skip
    journal = _read_journal(data)
    assert len(journal) == 2
    assert all(r["exit_code"] == 1 for r in journal)


def test_duplicate_command_argv_denial_reused_with_correct_per_item_counters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """proctor finding 1/3: a denied result is safe to reuse (deterministic gate decision).

    Two items share command_argv that isn't on the act allowlist. Reusing the
    cached denial is fine — the gate would deny it again anyway — but each
    item must still get its own `denied` count and its own journal entry.
    """
    home, data = _make_home(tmp_path), _make_data(tmp_path)
    shared_cmd = ("pursuit", "health", "acme", "--json")
    shared_action = ProposedAction(skill=None, rationale="r", enrichment_argv=None, command_argv=shared_cmd)
    monkeypatch.setattr(loop_module, "propose_for", lambda _item: shared_action)
    monkeypatch.setattr("fieldkit.companion.runner.is_allowed", lambda *_a, **_kw: False)
    spawn = _patch_subprocess(monkeypatch, returncode=0, stdout="HEALTH-JSON")

    result = run_once(home, data, tier="act", allowlist=[])

    assert result.denied == 2  # both items counted as denied, not just the first
    assert spawn.call_count == 0  # denial never reaches the subprocess, cached or not
    journal = _read_journal(data)
    assert len(journal) == 2
    assert all(r["exit_code"] == 3 for r in journal)  # EXIT_DENIED, per item
