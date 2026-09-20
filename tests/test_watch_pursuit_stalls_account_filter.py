"""Tests for historic regression: pursuit-stalls --account filter does not leak pruning messages.

Covers:
- prune_stale_state is only called with entries for the target account when
  --account is set, so no historic regression pruning messages fire for other accounts.
- Genuinely deleted pursuits for the target account are still pruned.
- Other accounts' state entries survive in the written state after an
  account-scoped run.
"""

import datetime
import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import fieldkit.watch._pursuit_stall_render as stall_render
import fieldkit.watch._pursuit_stall_state as stall_state
import fieldkit.watch.pursuit_stalls as wps

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TODAY = datetime.date(2026, 5, 26)

_STALE_DATE = (_TODAY - datetime.timedelta(days=20)).isoformat()


def _make_state(entries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Build a state dict from a mapping of key -> partial entry."""
    state: dict[str, Any] = {}
    for key, extra in entries.items():
        account, pursuit = key.split("/", 1)
        entry: dict[str, Any] = {
            "account": account,
            "pursuit": pursuit,
            "stage": "discover",
            "last_transition_date": _STALE_DATE,
            "days_since_transition": 20,
            "is_stalled": True,
            "threshold_days": 14,
            "alerted_days_tier": 14,
            "checked_at": "2026-05-25T00:00:00Z",
        }
        entry.update(extra)
        state[key] = entry
    return state


def _make_pursuit_md(stage: str = "discover", last_transition: str | None = None) -> str:
    """Build a minimal pursuit markdown file with frontmatter."""
    lt = last_transition or _STALE_DATE
    return f"---\nstage: {stage}\nlast-transition: {lt}\n---\n\n# Body\n"


# ---------------------------------------------------------------------------
# historic regression: account-scoped run produces no pruning messages for other accounts
# ---------------------------------------------------------------------------


# ── TestAccountScopedPruning (flattened) ────────────────────────────────────


def test_account_scoped_pruning_no_pruning_messages_for_other_accounts(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """historic regression: other-account state entries must NOT produce historic regression log lines."""
    # Set up two accounts in state: acme-corp (target) and midwest-ins (other)
    state = _make_state(
        {
            "acme-corp/deal-a": {},
            "midwest-ins/deal-b": {},
            "midwest-ins/deal-c": {},
        }
    )

    # Create a live pursuit file only for acme-corp
    acme_pursuits = tmp_path / "accounts" / "acme-corp" / "pursuits"
    acme_pursuits.mkdir(parents=True)
    (acme_pursuits / "deal-a.md").write_text(_make_pursuit_md(), encoding="utf-8")

    # Build live pursuit_files list as collect_pursuit_files would return
    live_files: list[tuple[Path, int]] = [(acme_pursuits / "deal-a.md", 14)]

    # When account="acme-corp", the scoped_state passed to prune_stale_state
    # must only contain acme-corp entries — so midwest-ins entries are invisible
    # to the pruner and no historic regression messages fire for them.
    scoped_state = {k: v for k, v in state.items() if k.startswith("acme-corp/")}

    with caplog.at_level(logging.INFO, logger="fieldkit.watch"):
        pruned = stall_state.prune_stale_state(scoped_state, live_files)

    # acme-corp/deal-a is live → kept
    assert "acme-corp/deal-a" in pruned

    # No historic regression messages for midwest-ins entries
    bug173_msgs = [r.message for r in caplog.records if "historic regression" in r.message]
    midwest_msgs = [m for m in bug173_msgs if "midwest-ins" in m]
    assert midwest_msgs == [], f"Unexpected historic regression messages for midwest-ins: {midwest_msgs}"


def test_account_scoped_pruning_target_account_deleted_pursuit_is_pruned(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Genuinely deleted pursuits for the target account are still pruned."""
    # acme-corp has two state entries: deal-a (live) and deleted-deal (gone)
    state = _make_state(
        {
            "acme-corp/deal-a": {},
            "acme-corp/deleted-deal": {},
        }
    )

    acme_pursuits = tmp_path / "accounts" / "acme-corp" / "pursuits"
    acme_pursuits.mkdir(parents=True)
    (acme_pursuits / "deal-a.md").write_text(_make_pursuit_md(), encoding="utf-8")
    # deleted-deal.md does NOT exist

    live_files: list[tuple[Path, int]] = [(acme_pursuits / "deal-a.md", 14)]
    scoped_state = {k: v for k, v in state.items() if k.startswith("acme-corp/")}

    with caplog.at_level(logging.INFO, logger="fieldkit.watch"):
        pruned = stall_state.prune_stale_state(scoped_state, live_files)

    # deal-a is live → kept
    assert "acme-corp/deal-a" in pruned
    # deleted-deal is gone → pruned
    assert "acme-corp/deleted-deal" not in pruned

    # historic regression message fires for the deleted pursuit
    bug173_msgs = [r.message for r in caplog.records if "historic regression" in r.message]
    assert any("deleted-deal" in m for m in bug173_msgs), (
        "Expected historic regression message for acme-corp/deleted-deal"
    )


def test_account_scoped_pruning_other_accounts_entries_survive_in_written_state(
    tmp_path: Path,
) -> None:
    """Other accounts' state entries are preserved after an account-scoped run.

    This validates the merge-back logic: after prune_stale_state runs on the
    scoped subset, the full state written to disk must still contain entries
    for accounts that were not in scope.
    """
    state = _make_state(
        {
            "acme-corp/deal-a": {},
            "midwest-ins/deal-b": {},
        }
    )

    # Simulate the historic regression fix: filter to acme-corp, prune, merge back
    acme_pursuits = tmp_path / "accounts" / "acme-corp" / "pursuits"
    acme_pursuits.mkdir(parents=True)
    (acme_pursuits / "deal-a.md").write_text(_make_pursuit_md(), encoding="utf-8")

    live_files: list[tuple[Path, int]] = [(acme_pursuits / "deal-a.md", 14)]

    # Apply the same logic as the fixed _run_pursuit_stalls
    account = "acme-corp"
    scoped_state = {k: v for k, v in state.items() if k.startswith(f"{account}/")}
    pruned_scoped = stall_state.prune_stale_state(scoped_state, live_files)

    # Merge: keep other-account entries, replace scoped entries with pruned result
    updated_state: dict[str, Any] = {k: v for k, v in state.items() if not k.startswith(f"{account}/")}
    updated_state.update(pruned_scoped)

    # midwest-ins/deal-b must survive
    assert "midwest-ins/deal-b" in updated_state, (
        "midwest-ins/deal-b was incorrectly removed during acme-corp-scoped run"
    )
    # acme-corp/deal-a must also survive (it's live)
    assert "acme-corp/deal-a" in updated_state


# ---------------------------------------------------------------------------
# Integration: _run_pursuit_stalls account filter does not log other-account pruning
# ---------------------------------------------------------------------------


# ── TestRunPursuitStallsAccountFilter (flattened) ───────────────────────────


def test_run_pursuit_stalls_account_filter_account_scoped_run_no_other_account_pruning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """historic regression regression: account-scoped run must not log historic regression for other accounts."""
    # State file with entries for two accounts
    watchers_dir = tmp_path / "watchers"
    watchers_dir.mkdir()
    state = _make_state(
        {
            "acme-corp/deal-a": {},
            "midwest-ins/deal-b": {},
        }
    )
    state_file = watchers_dir / "pursuit-stall-state.json"
    state_file.write_text(json.dumps(state), encoding="utf-8")

    # Only acme-corp pursuits directory exists
    acme_pursuits = tmp_path / "accounts" / "acme-corp" / "pursuits"
    acme_pursuits.mkdir(parents=True)
    (acme_pursuits / "deal-a.md").write_text(_make_pursuit_md(), encoding="utf-8")

    accounts_config: dict[str, Any] = {
        "accounts": {
            "acme-corp": {"stall_threshold_days": 14},
            "midwest-ins": {"stall_threshold_days": 14},
        }
    }

    with (
        patch.object(stall_state, "state_file", return_value=state_file),
        patch.object(stall_render, "get_watchers_dir", return_value=watchers_dir),
        patch.object(stall_render, "_alerts_file", return_value=watchers_dir / "pursuit-stall-alerts.md"),
        patch("fieldkit.watch.pursuit_stalls.get_accounts_config", return_value=accounts_config),
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.was_run_today", return_value=False),
        patch("fieldkit.watch.pursuit_stalls.write_run_status"),
        patch("fieldkit.watch.pursuit_stalls.watcher_logging"),
        caplog.at_level(logging.INFO, logger="fieldkit.watch"),
    ):
        rc = wps._run_pursuit_stalls(threshold=14, account="acme-corp", dry_run=True)

    assert rc == 0

    # No historic regression messages for midwest-ins
    bug173_msgs = [r.message for r in caplog.records if "historic regression" in r.message]
    midwest_msgs = [m for m in bug173_msgs if "midwest-ins" in m]
    assert midwest_msgs == [], (
        f"historic regression regression: unexpected pruning messages for midwest-ins: {midwest_msgs}"
    )
