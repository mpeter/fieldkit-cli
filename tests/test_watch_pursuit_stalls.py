"""Tests for routines/watch_pursuit_stalls.py.

Covers:
- scan_pursuit_file() detects stall when last_transition_date is 20 days ago
- scan_pursuit_file() returns non-stalled result for fresh transition (today)
- scan_pursuit_file() skips terminal stages (closed-won, closed-lost)
- scan_pursuit_file() returns None when no last-transition date is present
- scan_pursuit_file() falls back to transition-history list for the date
- coerce_date() handles both datetime.date objects and ISO string values
- _find_last_transition_date() picks the most recent date from history list
- append_stall_alert() writes alert markdown (account, pursuit, stage, days)
- append_stall_alert() dry_run=True writes nothing to disk
- pursuit-stall state load/save round-trip
- pursuit-stall state returns {} for a missing or corrupt state file
- collect_pursuit_files() respects account filter and per-account threshold
"""

import datetime
import json
import re
import textwrap
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import fieldkit.watch._pursuit_stall_render as stall_render
import fieldkit.watch._pursuit_stall_scan as stall_scan
import fieldkit.watch._pursuit_stall_state as stall_state
import fieldkit.watch.pursuit_stalls as wps

# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------

_TOOLS_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TODAY = datetime.date(2026, 5, 26)


def _make_pursuit_md(
    stage: str = "discovery",
    last_transition: str | None = None,
    transition_history: list[dict[str, str]] | None = None,
) -> str:
    """Build a minimal pursuit markdown file with frontmatter."""
    fm_lines = ["---", f"stage: {stage}"]
    if last_transition is not None:
        fm_lines.append(f"last-transition: {last_transition}")
    if transition_history is not None:
        fm_lines.append("transition-history:")
        for entry in transition_history:
            for k, v in entry.items():
                fm_lines.append(f"  - {k}: {v}")
    fm_lines.append("---")
    fm_lines.append("")
    fm_lines.append("# Pursuit body")
    return "\n".join(fm_lines)


# ---------------------------------------------------------------------------
# scan_pursuit_file — stall detection
# ---------------------------------------------------------------------------


# ── TestScanPursuitFileStall (flattened) ─────────────────────────────────────────────


def _patch_fieldkit_home_scan_pursuit_file_stall(tmp_path: Path) -> None:
    with patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path):
        yield


def test_detects_stall_20_days_ago(tmp_path: Path) -> None:
    stale_date = (_TODAY - datetime.timedelta(days=20)).isoformat()
    md = _make_pursuit_md(stage="discovery", last_transition=stale_date)
    pursuit_file = tmp_path / "deal.md"
    pursuit_file.write_text(md, encoding="utf-8")

    result = stall_scan.scan_pursuit_file(pursuit_file, threshold_days=14, today=_TODAY)

    assert result is not None
    assert result["is_stalled"] is True
    assert result["days_since_transition"] == 20
    assert result["stage"] == "discovery"
    assert result["threshold_days"] == 14
    assert result["pursuit"] == "deal"


def test_no_stall_for_fresh_transition(tmp_path: Path) -> None:
    fresh_date = _TODAY.isoformat()
    md = _make_pursuit_md(stage="proposal", last_transition=fresh_date)
    pursuit_file = tmp_path / "fresh.md"
    pursuit_file.write_text(md, encoding="utf-8")

    result = stall_scan.scan_pursuit_file(pursuit_file, threshold_days=14, today=_TODAY)

    assert result is not None
    assert result["is_stalled"] is False
    assert result["days_since_transition"] == 0


def test_stall_exactly_at_threshold_not_stalled(tmp_path: Path) -> None:
    """days_since == threshold_days is NOT stalled (strict >)."""
    threshold_date = (_TODAY - datetime.timedelta(days=14)).isoformat()
    md = _make_pursuit_md(stage="negotiation", last_transition=threshold_date)
    pursuit_file = tmp_path / "borderline.md"
    pursuit_file.write_text(md, encoding="utf-8")

    result = stall_scan.scan_pursuit_file(pursuit_file, threshold_days=14, today=_TODAY)

    assert result is not None
    assert result["is_stalled"] is False
    assert result["days_since_transition"] == 14


def test_stall_one_day_past_threshold(tmp_path: Path) -> None:
    over_date = (_TODAY - datetime.timedelta(days=15)).isoformat()
    md = _make_pursuit_md(stage="negotiation", last_transition=over_date)
    pursuit_file = tmp_path / "over.md"
    pursuit_file.write_text(md, encoding="utf-8")

    result = stall_scan.scan_pursuit_file(pursuit_file, threshold_days=14, today=_TODAY)

    assert result is not None
    assert result["is_stalled"] is True


def test_fallback_to_transition_history(tmp_path: Path) -> None:
    """When last-transition is absent, fall back to most recent history entry."""
    recent_date = (_TODAY - datetime.timedelta(days=20)).isoformat()
    older_date = (_TODAY - datetime.timedelta(days=40)).isoformat()
    # Write history with two entries — most recent should win
    md = textwrap.dedent(f"""\
        ---
        stage: discovery
        transition-history:
          - date: {older_date}
            from: prospecting
            to: discovery
          - date: {recent_date}
            from: initial
            to: discovery
        ---

        # Pursuit body
    """)
    pursuit_file = tmp_path / "history.md"
    pursuit_file.write_text(md, encoding="utf-8")

    result = stall_scan.scan_pursuit_file(pursuit_file, threshold_days=14, today=_TODAY)

    assert result is not None
    assert result["is_stalled"] is True
    assert result["days_since_transition"] == 20
    assert result["last_transition_date"] == recent_date


# ---------------------------------------------------------------------------
# scan_pursuit_file — skip conditions
# ---------------------------------------------------------------------------


# ── TestScanPursuitFileSkips (flattened) ─────────────────────────────────────────────


@pytest.mark.parametrize("stage", ["closed-won", "closed-lost", "closed", "won", "lost"])
def test_skips_terminal_stages(stage: str, tmp_path: Path) -> None:
    date = (_TODAY - datetime.timedelta(days=30)).isoformat()
    md = _make_pursuit_md(stage=stage, last_transition=date)
    pursuit_file = tmp_path / "terminal.md"
    pursuit_file.write_text(md, encoding="utf-8")

    result = stall_scan.scan_pursuit_file(pursuit_file, threshold_days=14, today=_TODAY)

    assert result is None


def test_returns_none_for_missing_transition_date(tmp_path: Path) -> None:
    md = _make_pursuit_md(stage="discovery")  # no last-transition
    pursuit_file = tmp_path / "nodates.md"
    pursuit_file.write_text(md, encoding="utf-8")

    result = stall_scan.scan_pursuit_file(pursuit_file, threshold_days=14, today=_TODAY)

    assert result is None


def test_returns_none_for_empty_frontmatter(tmp_path: Path) -> None:
    pursuit_file = tmp_path / "empty.md"
    pursuit_file.write_text("# No frontmatter here", encoding="utf-8")

    result = stall_scan.scan_pursuit_file(pursuit_file, threshold_days=14, today=_TODAY)

    assert result is None


def test_returns_none_for_unreadable_file(tmp_path: Path) -> None:
    pursuit_file = tmp_path / "unreadable.md"
    # Don't create the file — simulate OSError via a missing path
    result = stall_scan.scan_pursuit_file(pursuit_file, threshold_days=14, today=_TODAY)

    assert result is None


# ---------------------------------------------------------------------------
# _to_date — coercion helper
# ---------------------------------------------------------------------------


# ── TestToDate (flattened) ─────────────────────────────────────────────


def test_to_date_accepts_date_object() -> None:
    d = datetime.date(2026, 1, 15)
    assert stall_state.coerce_date(d) == d


def test_to_date_accepts_iso_string() -> None:
    assert stall_state.coerce_date("2026-01-15") == datetime.date(2026, 1, 15)


def test_to_date_accepts_us_format() -> None:
    assert stall_state.coerce_date("01/15/2026") == datetime.date(2026, 1, 15)


def test_to_date_accepts_slash_iso_format() -> None:
    assert stall_state.coerce_date("2026/01/15") == datetime.date(2026, 1, 15)


def test_to_date_returns_none_for_invalid_string() -> None:
    assert stall_state.coerce_date("not-a-date") is None


def test_to_date_returns_none_for_none() -> None:
    assert stall_state.coerce_date(None) is None


def test_to_date_returns_none_for_integer() -> None:
    assert stall_state.coerce_date(12345) is None


# ---------------------------------------------------------------------------
# _find_last_transition_date
# ---------------------------------------------------------------------------


# ── TestFindLastTransitionDate (flattened) ─────────────────────────────────────────────


def test_find_last_transition_date_prefers_last_transition_field() -> None:
    fm = {
        "last-transition": "2026-01-10",
        "transition-history": [{"date": "2025-12-01"}],
    }
    result = stall_scan.find_last_transition_date(fm)
    assert result == datetime.date(2026, 1, 10)


def test_find_last_transition_date_falls_back_to_history() -> None:
    fm = {
        "transition-history": [
            {"date": "2025-12-01"},
            {"date": "2026-02-15"},
        ]
    }
    result = stall_scan.find_last_transition_date(fm)
    assert result == datetime.date(2026, 2, 15)


def test_find_last_transition_date_returns_none_when_no_date_fields() -> None:
    fm = {"stage": "discovery"}
    assert stall_scan.find_last_transition_date(fm) is None


def test_find_last_transition_date_skips_non_dict_history_entries() -> None:
    fm = {"transition-history": ["not-a-dict", {"date": "2026-03-01"}]}
    result = stall_scan.find_last_transition_date(fm)
    assert result == datetime.date(2026, 3, 1)


# ---------------------------------------------------------------------------
# append_stall_alert
# ---------------------------------------------------------------------------


# ── TestAppendStallAlert (flattened) ─────────────────────────────────────────────


def _make_result_append_stall_alert(
    *,
    account: str = "global-pay",
    pursuit: str = "virtualization-deal",
    stage: str = "discovery",
    days: int = 20,
    threshold: int = 14,
    last_date: str = "2026-05-06",
) -> dict:
    return {
        "account": account,
        "pursuit": pursuit,
        "stage": stage,
        "days_since_transition": days,
        "threshold_days": threshold,
        "last_transition_date": last_date,
        "path": f"accounts/{account}/pursuits/{pursuit}.md",
        "is_stalled": True,
        "native_qualification": "unavailable (live ClosePlan fetch required)",
    }


def test_append_stall_alert_alert_written_to_file(tmp_path: Path) -> None:
    alerts_file = tmp_path / "pursuit-stall-alerts.md"
    result = _make_result_append_stall_alert()

    with (
        patch.object(stall_render, "_alerts_file", return_value=alerts_file),
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
    ):
        stall_render.append_stall_alert(result, dry_run=False)

    content = alerts_file.read_text()  # pii-guard: ignore
    assert "global-pay" in content
    assert "virtualization-deal" in content
    assert "discovery" in content
    assert "20" in content
    assert "14" in content
    assert "2026-05-06" in content


def test_render_owner_writes_stall_alert(tmp_path: Path) -> None:
    """Alert rendering has one canonical private owner."""
    alerts_file = tmp_path / "pursuit-stall-alerts.md"
    result = _make_result_append_stall_alert()

    with patch.object(stall_render, "_alerts_file", return_value=alerts_file):
        stall_render.append_stall_alert(result, dry_run=False)

    assert "virtualization-deal" in alerts_file.read_text(encoding="utf-8")


def test_append_stall_alert_dry_run_does_not_write_file(tmp_path: Path) -> None:
    alerts_file = tmp_path / "pursuit-stall-alerts.md"
    result = _make_result_append_stall_alert()  # pii-guard: ignore

    with (
        patch.object(stall_render, "_alerts_file", return_value=alerts_file),
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
    ):
        stall_render.append_stall_alert(result, dry_run=True)

    assert not alerts_file.exists()


def test_append_stall_alert_alert_appends_on_second_call(tmp_path: Path) -> None:
    alerts_file = tmp_path / "pursuit-stall-alerts.md"
    result1 = _make_result_append_stall_alert(account="acme", pursuit="deal-a")
    result2 = _make_result_append_stall_alert(account="globalpay", pursuit="deal-b")

    with (
        patch.object(stall_render, "_alerts_file", return_value=alerts_file),
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
    ):
        stall_render.append_stall_alert(result1, dry_run=False)
        stall_render.append_stall_alert(result2, dry_run=False)

    content = alerts_file.read_text()
    assert "acme" in content
    assert "globalpay" in content


def test_append_stall_alert_timestamp_present_in_alert(tmp_path: Path) -> None:
    alerts_file = tmp_path / "pursuit-stall-alerts.md"
    result = _make_result_append_stall_alert()

    with (
        patch.object(stall_render, "_alerts_file", return_value=alerts_file),
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
    ):
        stall_render.append_stall_alert(result, dry_run=False)

    content = alerts_file.read_text()
    assert re.search(r"\d{4}-\d{2}-\d{2}", content)


# ---------------------------------------------------------------------------
# load_state / save_state
# ---------------------------------------------------------------------------


# ── TestStateRoundTrip (flattened) ─────────────────────────────────────────────


def test_save_and_load(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    watchers_dir = tmp_path
    data = {
        "global-pay/virtualization-deal": {
            "is_stalled": True,
            "days_since_transition": 20,
            "checked_at": "2026-05-26T07:05:00Z",
        }
    }
    with (
        patch.object(stall_state, "state_file", return_value=state_file),
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=watchers_dir.parent),
    ):
        result = stall_state.save_state(data)
        loaded = stall_state.load_state()
    assert result is None
    assert loaded == data


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    state_file = tmp_path / "nonexistent.json"  # pii-guard: ignore
    with patch.object(stall_state, "state_file", return_value=state_file):
        assert stall_state.load_state() == {}


def test_load_corrupt_file_returns_empty(tmp_path: Path) -> None:
    state_file = tmp_path / "corrupt.json"
    state_file.write_text("not valid json {{{", encoding="utf-8")
    with patch.object(stall_state, "state_file", return_value=state_file):  # pii-guard: ignore
        assert stall_state.load_state() == {}


# ---------------------------------------------------------------------------
# collect_pursuit_files — account filter + threshold
# ---------------------------------------------------------------------------


# ── TestCollectPursuitFiles (flattened) ─────────────────────────────────────────────


def _make_accounts_config_collect_pursuit_files(
    tmp_path: Path,
    accounts: dict,
) -> dict:
    """Build a minimal accounts_config dict pointing to tmp_path."""
    return {"accounts": accounts}


def test_scan_owner_collects_pursuit_files(tmp_path: Path) -> None:
    """Collection has one canonical private scan owner."""
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    pursuits.mkdir(parents=True)
    (pursuits / "deal.md").write_text("---\nstage: discovery\n---\n", encoding="utf-8")
    config = {"accounts": {"acme": {"pursuit_dir": "accounts/acme/pursuits"}}}

    with patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path):
        results = stall_scan.collect_pursuit_files(accounts_config=config, account_filter=None)

    assert results == [(pursuits / "deal.md", 14)]


def test_collect_pursuit_files_respects_account_filter(tmp_path: Path) -> None:
    # Set up two accounts with pursuit dirs
    acme_pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    globalpay_pursuits = tmp_path / "accounts" / "globalpay" / "pursuits"
    acme_pursuits.mkdir(parents=True)
    globalpay_pursuits.mkdir(parents=True)
    (acme_pursuits / "deal.md").write_text("---\nstage: discovery\n---\n", encoding="utf-8")
    (globalpay_pursuits / "other.md").write_text("---\nstage: proposal\n---\n", encoding="utf-8")

    accounts_cfg = {
        "acme": {"pursuit_dir": "accounts/acme/pursuits"},
        "globalpay": {"pursuit_dir": "accounts/globalpay/pursuits"},
    }
    config = {"accounts": accounts_cfg}

    with patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path):
        results = stall_scan.collect_pursuit_files(
            accounts_config=config,
            account_filter="acme",
        )

    # Only acme pursuit returned
    paths = [p for p, _ in results]
    assert len(paths) == 1
    assert paths[0].name == "deal.md"


def test_collect_pursuit_files_uses_per_account_threshold(tmp_path: Path) -> None:
    pursuits = tmp_path / "accounts" / "corp" / "pursuits"
    pursuits.mkdir(parents=True)
    (pursuits / "deal.md").write_text("---\nstage: discovery\n---\n", encoding="utf-8")

    accounts_cfg = {
        "corp": {
            "pursuit_dir": "accounts/corp/pursuits",
            "stall_threshold_days": 21,
        }
    }
    config = {"accounts": accounts_cfg}

    with patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path):
        results = stall_scan.collect_pursuit_files(
            accounts_config=config,
            account_filter=None,
        )

    assert len(results) == 1
    _, threshold = results[0]
    assert threshold == 21


def test_collect_pursuit_files_skips_template_and_index_files(tmp_path: Path) -> None:
    pursuits = tmp_path / "accounts" / "corp" / "pursuits"
    pursuits.mkdir(parents=True)
    (pursuits / "template.md").write_text("---\nstage: discovery\n---\n", encoding="utf-8")
    (pursuits / "index.md").write_text("---\nstage: discovery\n---\n", encoding="utf-8")
    (pursuits / "real-deal.md").write_text("---\nstage: discovery\n---\n", encoding="utf-8")

    accounts_cfg = {
        "corp": {"pursuit_dir": "accounts/corp/pursuits"},
    }
    config = {"accounts": accounts_cfg}

    with patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path):
        results = stall_scan.collect_pursuit_files(
            accounts_config=config,
            account_filter=None,
        )

    paths = [p for p, _ in results]
    assert len(paths) == 1
    assert paths[0].name == "real-deal.md"


def test_collect_pursuit_files_excludes_internal_account_before_directory_scan(tmp_path: Path) -> None:
    config = {"accounts": {"internal": {"internal": True, "pursuit_dir": "missing/pursuits"}}}

    with patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path) as get_home:
        results = stall_scan.collect_pursuit_files(accounts_config=config, account_filter=None)

    assert results == []
    get_home.assert_not_called()


# ---------------------------------------------------------------------------
# historic regression: Stage change resets stall timer
# ---------------------------------------------------------------------------


# ── TestBug091StageReset (flattened) ─────────────────────────────────────────────


def _make_pursuit_file_bug091_stage_reset(tmp_path: Path, stage: str, days_ago: int) -> Path:
    """Write a pursuit markdown file with last-transition = days_ago."""
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    pursuits.mkdir(parents=True, exist_ok=True)
    last_transition = (datetime.datetime.now(tz=datetime.UTC).date() - datetime.timedelta(days=days_ago)).isoformat()
    content = textwrap.dedent(f"""\
        ---
        stage: {stage}
        last-transition: {last_transition}
        ---
        # Deal
    """)
    p = pursuits / "deal.md"
    p.write_text(content, encoding="utf-8")
    return p


def test_stage_change_resets_days_to_zero(tmp_path: Path) -> None:
    """When pursuit stage changed since last run, updated state shows days=0."""
    # Pursuit file now shows 'discover' stage, 5 days since last-transition
    _make_pursuit_file_bug091_stage_reset(tmp_path, stage="discover", days_ago=5)
    config = {
        "accounts": {
            "acme": {"pursuit_dir": "accounts/acme/pursuits", "stall_threshold_days": 14},
        }
    }
    # Prior state recorded stage='qualifying' (now changed to 'discover')
    prior_state = {
        "acme/deal": {
            "account": "acme",
            "pursuit": "deal",
            "stage": "qualifying",
            "last_transition_date": "2026-01-01",
            "days_since_transition": 120,
            "is_stalled": True,
            "threshold_days": 14,
            "alerted_days_tier": 30,
            "checked_at": "2026-01-01T00:00:00Z",
        }
    }

    state_file = tmp_path / "state.json"
    alerts_file = tmp_path / "stall-alerts.md"
    # Pre-populate state file so _run_pursuit_stalls loads prior state
    state_file.write_text(json.dumps(prior_state), encoding="utf-8")

    with (
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.get_accounts_config", return_value=config),
        patch.object(stall_state, "state_file", return_value=state_file),
        patch.object(stall_render, "_alerts_file", return_value=alerts_file),
        patch("fieldkit.watch.pursuit_stalls.was_run_today", return_value=False),
    ):
        rc = wps._run_pursuit_stalls(threshold=14, account=None, dry_run=False)

    assert rc == 0

    saved = json.loads(state_file.read_text(encoding="utf-8"))
    entry = saved.get("acme/deal", {})
    assert entry["stage"] == "discover", "Stage must be updated to new value"
    assert entry["days_since_transition"] == 0, "Days must be reset to 0 on stage change"
    assert not entry["is_stalled"], "Pursuit must not be stalled after stage reset"


def test_stage_change_resets_alerted_days_tier(tmp_path: Path) -> None:
    """historic regression: alerted_days_tier must be reset to 0 when stage changes.

    Prior state recorded alerted_days_tier=30 in stage 'discover'.  The
    pursuit file now shows stage 'validate'.  The saved state entry must
    have alerted_days_tier=0 so the old tier cannot suppress alerts in the
    new stall cycle.
    """
    # Pursuit file shows new stage 'validate', 3 days since last-transition
    _make_pursuit_file_bug091_stage_reset(tmp_path, stage="validate", days_ago=3)
    config = {
        "accounts": {
            "acme": {"pursuit_dir": "accounts/acme/pursuits", "stall_threshold_days": 14},
        }
    }
    # Prior state: old stage 'discover' with a non-zero alerted_days_tier
    prior_state = {
        "acme/deal": {
            "account": "acme",
            "pursuit": "deal",
            "stage": "discover",
            "last_transition_date": "2026-01-01",
            "days_since_transition": 35,
            "is_stalled": True,
            "threshold_days": 14,
            "alerted_days_tier": 30,
            "checked_at": "2026-01-01T00:00:00Z",
        }
    }

    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(prior_state), encoding="utf-8")
    alerts_file = tmp_path / "stall-alerts.md"

    with (
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.get_accounts_config", return_value=config),
        patch.object(stall_state, "state_file", return_value=state_file),
        patch.object(stall_render, "_alerts_file", return_value=alerts_file),
        patch("fieldkit.watch.pursuit_stalls.was_run_today", return_value=False),
    ):
        rc = wps._run_pursuit_stalls(threshold=14, account=None, dry_run=False)

    assert rc == 0
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    entry = saved.get("acme/deal", {})
    assert entry["stage"] == "validate", "Stage must be updated to new value"
    assert entry["days_since_transition"] == 0, "Days must be reset to 0 on stage change"
    assert not entry["is_stalled"], "Pursuit must not be stalled after stage reset"
    # historic regression: the old tier from the prior stage must not carry forward
    assert entry["alerted_days_tier"] == 0, "alerted_days_tier must be reset to 0 on stage change, not carried forward"


def test_no_stage_change_preserves_days(tmp_path: Path) -> None:
    """When stage is unchanged, days_since_transition is computed from frontmatter."""
    # 20 days in same stage — above 14-day threshold
    _make_pursuit_file_bug091_stage_reset(tmp_path, stage="qualifying", days_ago=20)
    config = {
        "accounts": {
            "acme": {"pursuit_dir": "accounts/acme/pursuits", "stall_threshold_days": 14},
        }
    }
    prior_state = {
        "acme/deal": {
            "account": "acme",
            "pursuit": "deal",
            "stage": "qualifying",  # same stage — no reset
            "last_transition_date": "2026-01-01",
            "days_since_transition": 15,
            "is_stalled": True,
            "threshold_days": 14,
            "alerted_days_tier": 14,
            "checked_at": "2026-01-01T00:00:00Z",
        }
    }

    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(prior_state), encoding="utf-8")
    alerts_file = tmp_path / "stall-alerts.md"

    with (
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.get_accounts_config", return_value=config),
        patch.object(stall_state, "state_file", return_value=state_file),
        patch.object(stall_render, "_alerts_file", return_value=alerts_file),
        patch("fieldkit.watch.pursuit_stalls.was_run_today", return_value=False),
    ):
        rc = wps._run_pursuit_stalls(threshold=14, account=None, dry_run=False)

    assert rc == 0
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    entry = saved.get("acme/deal", {})
    assert entry["days_since_transition"] == 20, "Days should reflect actual frontmatter"
    assert entry["is_stalled"], "Pursuit should still be stalled"


# ---------------------------------------------------------------------------
# historic regression: Named failures in log output
# ---------------------------------------------------------------------------


# ── TestBug092NamedFailures (flattened) ─────────────────────────────────────────────


def test_no_frontmatter_produces_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A file with no frontmatter now emits WARNING from scan_pursuit_file (implementation note).

    Group-A change: the no-frontmatter path was promoted from DEBUG to WARNING
    so operators can identify files that need frontmatter without --verbose.
    Both scan_pursuit_file (A1) and _scan_pursuit_files (A2) emit WARNINGs.
    """
    import logging

    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    pursuits.mkdir(parents=True)
    # Write a file with no frontmatter so scan_pursuit_file returns None via warning path
    bad_file = pursuits / "bad-deal.md"
    bad_file.write_text("# No frontmatter here\n", encoding="utf-8")

    config = {
        "accounts": {
            "acme": {"pursuit_dir": "accounts/acme/pursuits", "stall_threshold_days": 14},
        }
    }
    state_file = tmp_path / "state.json"
    alerts_file = tmp_path / "stall-alerts.md"

    with (
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.get_accounts_config", return_value=config),
        patch.object(stall_state, "state_file", return_value=state_file),
        patch.object(stall_render, "_alerts_file", return_value=alerts_file),
        patch("fieldkit.watch.pursuit_stalls.was_run_today", return_value=False),
        patch.object(wps, "watcher_logging", return_value=nullcontext()),
        caplog.at_level(logging.WARNING),
    ):
        rc = wps._run_pursuit_stalls(threshold=14, account=None, dry_run=False)

    # historic regression: skipped=1 returns exit code 1 (partial).
    assert rc == 1
    # A1: scan_pursuit_file must emit WARNING mentioning the file name.
    warning_msgs = [
        r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING and "bad-deal" in r.getMessage()
    ]
    assert warning_msgs, "Expected WARNING for no-frontmatter skip; got no matching records"


def test_oserror_file_produces_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """An OSError (unreadable/missing file) still produces a WARNING via scan_pursuit_file.

    scan_pursuit_file() emits log.warning internally when it catches an OSError.
    After the Task-3 caller-side warning was removed this is the only WARNING
    path for I/O failures — confirm it is still firing.

    We inject the non-existent path directly into collect_pursuit_files' return
    value via mock so that _run_pursuit_stalls feeds it to scan_pursuit_file
    even though the file doesn't exist on disk.
    """
    import logging

    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    pursuits.mkdir(parents=True)
    # A path that never existed on disk — reading it will raise FileNotFoundError
    missing_file = pursuits / "missing-deal.md"

    config = {
        "accounts": {
            "acme": {"pursuit_dir": "accounts/acme/pursuits", "stall_threshold_days": 14},
        }
    }
    state_file = tmp_path / "state.json"
    alerts_file = tmp_path / "stall-alerts.md"

    with (
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.get_accounts_config", return_value=config),
        patch.object(stall_state, "state_file", return_value=state_file),
        patch.object(stall_render, "_alerts_file", return_value=alerts_file),
        patch("fieldkit.watch.pursuit_stalls.was_run_today", return_value=False),
        patch.object(wps, "watcher_logging", return_value=nullcontext()),
        # Inject the non-existent path so scan_pursuit_file is called with it
        patch.object(stall_scan, "collect_pursuit_files_details", return_value=([(missing_file, 14)], 0, 0, set())),
        caplog.at_level(logging.WARNING),
    ):
        rc = wps._run_pursuit_stalls(threshold=14, account=None, dry_run=False)

    # historic regression: skipped=1 (OSError) now returns exit code 1 (partial).
    assert rc == 1
    # A WARNING must be emitted from scan_pursuit_file for the OSError
    warning_msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("missing-deal" in msg for msg in warning_msgs), (
        f"Expected WARNING for OSError on missing-deal.md; got: {warning_msgs}"
    )


def test_terminal_pursuit_is_an_intentional_exclusion_with_a_reason(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    terminal = tmp_path / "accounts" / "acme" / "pursuits" / "closed.md"
    terminal.parent.mkdir(parents=True)
    terminal.write_text(_make_pursuit_md(stage="closed-won", last_transition="2026-05-01"), encoding="utf-8")

    with caplog.at_level(logging.INFO, logger="fieldkit.watch"):
        checked, _stalled, _snoozed, failures, exclusions, _ = wps._scan_pursuit_files(
            pursuit_files=[(terminal, 14)], state={}, updated_state={}, today=_TODAY, dry_run=True
        )

    assert checked == 0
    assert failures == 0
    assert exclusions == 1
    assert any("terminal stage: closed-won" in record.getMessage() for record in caplog.records)


def test_intentional_only_run_is_ok(tmp_path: Path) -> None:
    _make_pursuit_file_run_pursuit_stalls_branches(tmp_path, "acme", "closed", stage="closed-won", days_ago=20)
    config = {"accounts": {"acme": {"pursuit_dir": "accounts/acme/pursuits"}}}

    with (
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.get_accounts_config", return_value=config),
        patch("fieldkit.watch.pursuit_stalls.was_run_today", return_value=False),
        patch.object(wps, "write_run_status") as write_status,
    ):
        rc = wps._run_pursuit_stalls(threshold=14, account=None, dry_run=True)

    assert rc == 0
    assert write_status.call_args.kwargs["outcome"] == "ok"
    assert write_status.call_args.kwargs["failures"] == 0


@pytest.mark.parametrize(
    ("checked", "failures", "exclusions", "expected"),
    [
        (1, 0, 0, "ok"),
        (1, 1, 0, "partial"),
        (0, 1, 0, "fatal"),
        (0, 0, 0, "fatal"),
        (0, 0, 1, "ok"),
    ],
)
def test_watcher_outcome_distinguishes_empty_failure_and_intentional_runs(
    checked: int, failures: int, exclusions: int, expected: wps.WatcherOutcome
) -> None:
    assert wps._watcher_outcome(checked, failures, exclusions) == expected


def test_empty_noninternal_pursuit_directory_is_fatal(tmp_path: Path) -> None:
    """An empty configured directory is not evidence that the watcher is healthy."""
    (tmp_path / "accounts" / "acme" / "pursuits").mkdir(parents=True)
    config = {"accounts": {"acme": {"pursuit_dir": "accounts/acme/pursuits"}}}

    with (
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.get_accounts_config", return_value=config),
        patch("fieldkit.watch.pursuit_stalls.was_run_today", return_value=False),
        patch.object(wps, "write_run_status") as write_status,
    ):
        rc = wps._run_pursuit_stalls(threshold=14, account=None, dry_run=True)

    assert rc == 1
    assert write_status.call_args.kwargs["outcome"] == "fatal"


def test_empty_account_filtered_pursuit_directory_is_fatal(tmp_path: Path) -> None:
    """Accounts outside an explicit filter do not make an empty scan healthy."""
    (tmp_path / "accounts" / "acme" / "pursuits").mkdir(parents=True)
    config = {
        "accounts": {
            "acme": {"pursuit_dir": "accounts/acme/pursuits"},
            "other": {"pursuit_dir": "accounts/other/pursuits"},
        }
    }

    with (
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.get_accounts_config", return_value=config),
        patch("fieldkit.watch.pursuit_stalls.was_run_today", return_value=False),
        patch.object(wps, "write_run_status") as write_status,
    ):
        rc = wps._run_pursuit_stalls(threshold=14, account="acme", dry_run=True)

    assert rc == 1
    assert write_status.call_args.kwargs["outcome"] == "fatal"


def test_mixed_data_failures_produce_partial_outcome(tmp_path: Path) -> None:
    _make_pursuit_file_run_pursuit_stalls_branches(tmp_path, "acme", "good", stage="discovery", days_ago=5)
    bad_file = tmp_path / "accounts" / "acme" / "pursuits" / "bad.md"
    bad_file.write_text("# No frontmatter\n", encoding="utf-8")
    config = {
        "accounts": {
            "acme": {"pursuit_dir": "accounts/acme/pursuits"},
            "missing": {"pursuit_dir": "accounts/missing/pursuits"},
        }
    }

    with (
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.get_accounts_config", return_value=config),
        patch("fieldkit.watch.pursuit_stalls.was_run_today", return_value=False),
        patch.object(wps, "write_run_status") as write_status,
    ):
        rc = wps._run_pursuit_stalls(threshold=14, account=None, dry_run=True)

    assert rc == 1
    assert write_status.call_args.kwargs["outcome"] == "partial"
    assert write_status.call_args.kwargs["failures"] == 2


def test_invalid_yaml_frontmatter_produces_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A1 (implementation note): invalid YAML frontmatter emits WARNING from scan_pursuit_file.

    scan_pursuit_file calls _parse_frontmatter_raw which already emits a
    WARNING for YAML parse errors.  After the parse error returns {}, the
    no-frontmatter branch (A1) also emits a WARNING with the filename so
    the operator sees the file name at WARNING level in the pursuit_stalls logger.
    """
    import logging

    pursuit_file = tmp_path / "bad-yaml.md"
    # Unclosed bracket makes yaml.safe_load raise YAMLError
    pursuit_file.write_text("---\nkey: [unclosed\n---\n# Body\n", encoding="utf-8")

    with (
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        caplog.at_level(logging.WARNING, logger="fieldkit.watch"),
    ):
        result = stall_scan.scan_pursuit_file(pursuit_file, threshold_days=14, today=_TODAY)

    assert result is None, "Expected None for invalid YAML frontmatter"
    # A1: WARNING must mention the file name
    warning_msgs = [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and str(pursuit_file.name) in r.getMessage()
    ]
    assert warning_msgs, (
        f"Expected WARNING containing '{pursuit_file.name}' for YAML parse error; "
        f"got records: {[r.getMessage() for r in caplog.records]}"
    )


def test_scan_pursuit_files_emits_caller_warning_for_none_result(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A2 (implementation note): _scan_pursuit_files emits caller-side WARNING for every None result.

    When scan_pursuit_file returns None (for any reason), _scan_pursuit_files
    must emit its own WARNING so the path is always visible in the log even
    if the inner function's WARNING was at a different logger level.
    We test this by passing a missing file path directly to _scan_pursuit_files.
    """
    import logging

    missing_file = tmp_path / "ghost.md"  # never written — causes OSError in scan_pursuit_file

    with caplog.at_level(logging.WARNING, logger="fieldkit.watch"):
        checked, _stalled, _snoozed, failures, exclusions, _ = wps._scan_pursuit_files(
            pursuit_files=[(missing_file, 14)],
            state={},
            updated_state={},
            today=_TODAY,
            dry_run=True,
        )

    assert failures == 1
    assert exclusions == 0
    assert checked == 0
    # A2: caller-side WARNING must mention the file path
    warning_msgs = [
        r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING and str(missing_file) in r.getMessage()
    ]
    assert warning_msgs, (
        f"Expected caller-side WARNING from _scan_pursuit_files for {missing_file}; "
        f"got records: {[r.getMessage() for r in caplog.records]}"
    )
    assert any("cannot read file" in message for message in warning_msgs)


# ---------------------------------------------------------------------------
# historic regression: Stale state pruning for deleted pursuit files
# ---------------------------------------------------------------------------


# ── TestBug173StaleStatePruning (flattened) ─────────────────────────────────────────────


def test_prune_stale_state_removes_deleted_pursuit(tmp_path: Path) -> None:
    """prune_stale_state drops keys whose pursuit files no longer exist."""
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    pursuits.mkdir(parents=True)
    live_file = pursuits / "deal.md"
    live_file.write_text("---\nstage: discovery\n---\n", encoding="utf-8")

    live_paths: list[tuple[Path, int]] = [(live_file, 14)]
    state: dict = {
        "acme/deal": {"stage": "discovery", "days_since_transition": 5},
        "acme/deleted-deal": {"stage": "negotiation", "days_since_transition": 30},
    }

    pruned = stall_state.prune_stale_state(state, live_paths)

    assert "acme/deal" in pruned
    assert "acme/deleted-deal" not in pruned


def test_prune_stale_state_empty_state() -> None:
    """prune_stale_state on empty state returns empty dict."""
    assert stall_state.prune_stale_state({}, []) == {}


def test_prune_stale_state_all_live(tmp_path: Path) -> None:
    """When all state keys correspond to live files, nothing is pruned."""
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    pursuits.mkdir(parents=True)
    deal_a = pursuits / "deal-a.md"
    deal_a.write_text("---\nstage: discovery\n---\n", encoding="utf-8")
    deal_b = pursuits / "deal-b.md"
    deal_b.write_text("---\nstage: discovery\n---\n", encoding="utf-8")

    live_paths: list[tuple[Path, int]] = [(deal_a, 14), (deal_b, 14)]
    state: dict = {
        "acme/deal-a": {"stage": "discovery"},
        "acme/deal-b": {"stage": "proposal"},
    }

    pruned = stall_state.prune_stale_state(state, live_paths)
    assert pruned == state


def test_run_prunes_stale_ghost_entries(tmp_path: Path) -> None:
    """End-to-end: _run_pursuit_stalls does not persist state for deleted pursuits."""
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    pursuits.mkdir(parents=True)
    last_transition = (datetime.datetime.now(tz=datetime.UTC).date() - datetime.timedelta(days=20)).isoformat()
    live_file = pursuits / "active-deal.md"
    live_file.write_text(
        f"---\nstage: discovery\nlast-transition: {last_transition}\n---\n",
        encoding="utf-8",
    )

    config = {
        "accounts": {
            "acme": {"pursuit_dir": "accounts/acme/pursuits", "stall_threshold_days": 14},
        }
    }
    # State has an entry for a deleted pursuit that no longer exists on disk
    prior_state = {
        "acme/active-deal": {
            "account": "acme",
            "pursuit": "active-deal",
            "stage": "discovery",
            "last_transition_date": last_transition,
            "days_since_transition": 20,
            "is_stalled": True,
            "threshold_days": 14,
            "alerted_days_tier": 14,
            "checked_at": "2026-01-01T00:00:00Z",
        },
        "acme/ghost-deal": {
            "account": "acme",
            "pursuit": "ghost-deal",
            "stage": "proposal",
            "last_transition_date": "2025-01-01",
            "days_since_transition": 500,
            "is_stalled": True,
            "threshold_days": 14,
            "alerted_days_tier": 30,
            "checked_at": "2026-01-01T00:00:00Z",
        },
    }

    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(prior_state), encoding="utf-8")
    alerts_file = tmp_path / "stall-alerts.md"

    with (
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.get_accounts_config", return_value=config),
        patch.object(stall_state, "state_file", return_value=state_file),
        patch.object(stall_render, "_alerts_file", return_value=alerts_file),
        patch("fieldkit.watch.pursuit_stalls.was_run_today", return_value=False),
    ):
        rc = wps._run_pursuit_stalls(threshold=14, account=None, dry_run=False)

    assert rc == 0
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    # Ghost deal must not be persisted in saved state
    assert "acme/ghost-deal" not in saved
    # Live deal should still be present
    assert "acme/active-deal" in saved


# ---------------------------------------------------------------------------
# historic regression: Once-per-day guard in _run_pursuit_stalls
# ---------------------------------------------------------------------------


# ── TestBug120OncePerDayGuard (flattened) ─────────────────────────────────────────────


def test_skips_when_already_ran_today(tmp_path: Path) -> None:
    """When was_run_today('pursuit-stalls') is True, return 0 without scanning."""
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    pursuits.mkdir(parents=True)
    last_transition = (datetime.datetime.now(tz=datetime.UTC).date() - datetime.timedelta(days=20)).isoformat()
    (pursuits / "deal.md").write_text(
        f"---\nstage: discovery\nlast-transition: {last_transition}\n---\n",
        encoding="utf-8",
    )

    config = {
        "accounts": {
            "acme": {"pursuit_dir": "accounts/acme/pursuits", "stall_threshold_days": 14},
        }
    }
    state_file = tmp_path / "state.json"
    alerts_file = tmp_path / "stall-alerts.md"

    with (
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.get_accounts_config", return_value=config),
        patch.object(stall_state, "state_file", return_value=state_file),
        patch.object(stall_render, "_alerts_file", return_value=alerts_file),
        # historic regression: simulate watcher already ran today
        patch("fieldkit.watch.pursuit_stalls.was_run_today", return_value=True),
        # historic regression: get_last_run_outcome reads the live watcher-run-status.json;
        # mock it to "ok" so the test is isolated from whatever the live file says.
        patch("fieldkit.watch.pursuit_stalls.get_last_run_outcome", return_value="ok"),
    ):
        rc = wps._run_pursuit_stalls(threshold=14, account=None, dry_run=False)

    assert rc == 0
    # No pursuits were processed — state file must not have been written
    assert not state_file.exists(), "State file must not be written when skipping"
    # No alerts written
    assert not alerts_file.exists(), "Alerts file must not be written when skipping"


def test_runs_normally_when_not_run_today(tmp_path: Path) -> None:
    """When was_run_today returns False, _run_pursuit_stalls proceeds normally."""
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    pursuits.mkdir(parents=True)
    last_transition = (datetime.datetime.now(tz=datetime.UTC).date() - datetime.timedelta(days=20)).isoformat()
    (pursuits / "deal.md").write_text(
        f"---\nstage: discovery\nlast-transition: {last_transition}\n---\n",
        encoding="utf-8",
    )

    config = {
        "accounts": {
            "acme": {"pursuit_dir": "accounts/acme/pursuits", "stall_threshold_days": 14},
        }
    }
    state_file = tmp_path / "state.json"
    alerts_file = tmp_path / "stall-alerts.md"

    with (
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.get_accounts_config", return_value=config),
        patch.object(stall_state, "state_file", return_value=state_file),
        patch.object(stall_render, "_alerts_file", return_value=alerts_file),
        patch("fieldkit.watch.pursuit_stalls.was_run_today", return_value=False),
    ):
        rc = wps._run_pursuit_stalls(threshold=14, account=None, dry_run=False)

    assert rc == 0
    # State file should have been written — pursuits were scanned
    assert state_file.exists(), "State file must be written when running normally"


def test_dry_run_bypasses_guard(tmp_path: Path) -> None:
    """dry_run=True always runs even when was_run_today would return True."""
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    pursuits.mkdir(parents=True)
    last_transition = (datetime.datetime.now(tz=datetime.UTC).date() - datetime.timedelta(days=20)).isoformat()
    (pursuits / "deal.md").write_text(
        f"---\nstage: discovery\nlast-transition: {last_transition}\n---\n",
        encoding="utf-8",
    )

    config = {
        "accounts": {
            "acme": {"pursuit_dir": "accounts/acme/pursuits", "stall_threshold_days": 14},
        }
    }
    state_file = tmp_path / "state.json"
    alerts_file = tmp_path / "stall-alerts.md"

    # was_run_today is never called in dry_run mode — guard is skipped entirely
    with (
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.get_accounts_config", return_value=config),
        patch.object(stall_state, "state_file", return_value=state_file),
        patch.object(stall_render, "_alerts_file", return_value=alerts_file),
        patch("fieldkit.watch.pursuit_stalls.was_run_today", return_value=True) as mock_wrt,
    ):
        rc = wps._run_pursuit_stalls(threshold=14, account=None, dry_run=True)

    assert rc == 0
    # Guard must not have been consulted in dry_run mode
    mock_wrt.assert_not_called()


# ---------------------------------------------------------------------------
# Task 3 — no spurious WARNING for terminal-stage skips (BUG: caller warning)
# ---------------------------------------------------------------------------


# ── TestScanPursuitFileLogging (flattened) ─────────────────────────────────────────────


def test_terminal_stage_produces_no_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A closed-won pursuit yields DEBUG only — no WARNING at all.

    Before the Task-3 fix the caller emitted an extra WARNING for every
    None return including intentional terminal-stage skips.  After the fix
    only scan_pursuit_file's own log.debug fires, so no WARNING should
    appear in the log records for this file.
    """
    last_transition = (_TODAY - datetime.timedelta(days=30)).isoformat()
    md = _make_pursuit_md(stage="closed-won", last_transition=last_transition)
    pursuit_file = tmp_path / "won.md"
    pursuit_file.write_text(md, encoding="utf-8")

    import logging

    with caplog.at_level(logging.DEBUG):
        result = stall_scan.scan_pursuit_file(pursuit_file, threshold_days=14, today=_TODAY)

    assert result is None, "Expected None for terminal stage"
    # No WARNING record should reference this file
    warning_msgs = [
        r.message for r in caplog.records if r.levelno >= logging.WARNING and str(pursuit_file) in r.getMessage()
    ]
    assert warning_msgs == [], f"Expected no WARNING for terminal-stage skip, got: {warning_msgs}"


def test_unreadable_file_produces_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """An OSError (e.g. missing file) emits a WARNING from scan_pursuit_file.

    scan_pursuit_file catches OSError internally and emits log.warning before
    returning None.  This test confirms that WARNING is still produced for
    genuine I/O failures after the Task-3 caller-side warning was removed.
    """
    pursuit_file = tmp_path / "missing.md"
    # File does not exist — read_text will raise OSError (FileNotFoundError)

    import logging

    with caplog.at_level(logging.WARNING):
        result = stall_scan.scan_pursuit_file(pursuit_file, threshold_days=14, today=_TODAY)

    assert result is None, "Expected None for unreadable file"
    warning_msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(str(pursuit_file) in msg for msg in warning_msgs), (
        f"Expected a WARNING containing the file path for OSError, got: {warning_msgs}"
    )


# ---------------------------------------------------------------------------
# Task 1 — alert dedup: append_stall_alert skips duplicates for same date+slug
# ---------------------------------------------------------------------------


# ── TestAppendStallAlertDedup (flattened) ─────────────────────────────────────────────

_STALE_DATE_append_stall_alert_dedup = (_TODAY - datetime.timedelta(days=20)).isoformat()


def _patch_fieldkit_home_append_stall_alert_dedup(tmp_path: Path) -> None:
    with patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path):
        yield


def _make_result_append_stall_alert_dedup(account: str = "acme", pursuit: str = "deal-a") -> dict:
    return {
        "account": account,
        "pursuit": pursuit,
        "stage": "discovery",
        "days_since_transition": 20,
        "last_transition_date": _STALE_DATE_append_stall_alert_dedup,
        "threshold_days": 14,
        "path": f"accounts/{account}/pursuits/{pursuit}.md",
        "champion": "Alice",
        "sf_next_steps": "Follow up",
        "native_qualification": "unavailable (no Salesforce opportunity link)",
    }


def test_first_write_is_appended(tmp_path: Path) -> None:
    """First alert for a date+slug is written to the alert file."""
    alerts_file = tmp_path / "pursuit-stall-alerts.md"
    alerts_file.write_text("# Pursuit Stall Alerts\n\nAutomated alerts.\n", encoding="utf-8")

    with patch.object(stall_render, "_alerts_file", return_value=alerts_file):
        original_size = alerts_file.stat().st_size
        stall_render.append_stall_alert(_make_result_append_stall_alert_dedup(), dry_run=False)

    assert alerts_file.stat().st_size > original_size, "Alert file must grow on the first write"


def test_duplicate_alert_is_not_written(tmp_path: Path) -> None:
    """A second call with the same date+slug must not grow the alert file."""
    import datetime as dt

    today_str = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")
    account = "acme"
    pursuit = "deal-a"
    alerts_file = tmp_path / "pursuit-stall-alerts.md"
    # Pre-populate with a heading that matches what append_stall_alert would write
    alerts_file.write_text(
        "# Pursuit Stall Alerts\n\n"
        f"## {today_str} — {account} / {pursuit} — stalled in discovery\n\n"
        f"- **Pursuit:** [{pursuit}](accounts/{account}/pursuits/{pursuit}.md)\n",
        encoding="utf-8",
    )

    with patch.object(stall_render, "_alerts_file", return_value=alerts_file):
        size_before = alerts_file.stat().st_size
        stall_render.append_stall_alert(
            _make_result_append_stall_alert_dedup(account=account, pursuit=pursuit), dry_run=False
        )
        size_after = alerts_file.stat().st_size

    assert size_after == size_before, "Alert file must NOT grow when the heading already exists (dedup check)"


def test_different_pursuit_is_written(tmp_path: Path) -> None:
    """A different pursuit on the same date IS written (no false suppression)."""
    import datetime as dt

    today_str = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")
    account = "acme"
    pursuit_a = "deal-a"
    pursuit_b = "deal-b"

    alerts_file = tmp_path / "pursuit-stall-alerts.md"
    # Pre-populate only deal-a
    alerts_file.write_text(
        "# Pursuit Stall Alerts\n\n"
        f"## {today_str} — {account} / {pursuit_a} — stalled in discovery\n\n"
        f"- **Pursuit:** [{pursuit_a}](accounts/{account}/pursuits/{pursuit_a}.md)\n",
        encoding="utf-8",
    )

    with patch.object(stall_render, "_alerts_file", return_value=alerts_file):
        size_before = alerts_file.stat().st_size
        stall_render.append_stall_alert(
            _make_result_append_stall_alert_dedup(account=account, pursuit=pursuit_b), dry_run=False
        )
        content = alerts_file.read_text(encoding="utf-8")

    assert alerts_file.stat().st_size > size_before, (
        "A new pursuit slug must be written even when another slug exists for the same date"
    )
    assert pursuit_b in content


# ---------------------------------------------------------------------------
# Task 4.7 — Branch coverage: dedup suppression and cross-account stall alert
# ---------------------------------------------------------------------------


# ── TestBranchCoverage (flattened) ─────────────────────────────────────────────


def _make_pursuit_file_branch_coverage(
    tmp_path: Path,
    account: str,
    pursuit: str,
    stage: str,
    days_ago: int,
) -> Path:
    """Write a pursuit markdown file with last-transition = days_ago."""
    pursuits = tmp_path / "accounts" / account / "pursuits"
    pursuits.mkdir(parents=True, exist_ok=True)
    last_transition = (datetime.datetime.now(tz=datetime.UTC).date() - datetime.timedelta(days=days_ago)).isoformat()
    content = f"---\nstage: {stage}\nlast-transition: {last_transition}\n---\n# Deal\n"
    p = pursuits / f"{pursuit}.md"
    p.write_text(content, encoding="utf-8")
    return p


def test_dedup_suppresses_repeated_stall_alert(tmp_path: Path) -> None:
    """Task 4.7a: a stalled pursuit already alerted at the same tier is suppressed.

    Prior state records alerted_days_tier=14 and is_stalled=True for the
    same stage.  The pursuit is still stalled at 20 days (tier=14).
    should_suppress_alert must return True and no new alert must be written.
    """
    _make_pursuit_file_branch_coverage(tmp_path, "acme", "deal", stage="discovery", days_ago=20)

    config = {
        "accounts": {
            "acme": {"pursuit_dir": "accounts/acme/pursuits", "stall_threshold_days": 14},
        }
    }
    # Prior state: same stage, already alerted at tier=14 (20 days > 14 threshold)
    prior_state = {
        "acme/deal": {
            "account": "acme",
            "pursuit": "deal",
            "stage": "discovery",
            "last_transition_date": (
                datetime.datetime.now(tz=datetime.UTC).date() - datetime.timedelta(days=20)
            ).isoformat(),
            "days_since_transition": 20,
            "is_stalled": True,
            "threshold_days": 14,
            "alerted_days_tier": 14,  # already alerted at this tier
            "checked_at": "2026-01-01T00:00:00Z",
        }
    }

    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(prior_state), encoding="utf-8")
    alerts_file = tmp_path / "stall-alerts.md"

    with (
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.get_accounts_config", return_value=config),
        patch.object(stall_state, "state_file", return_value=state_file),
        patch.object(stall_render, "_alerts_file", return_value=alerts_file),
        patch("fieldkit.watch.pursuit_stalls.was_run_today", return_value=False),
        patch.object(stall_render, "append_stall_alert") as mock_alert,
    ):
        rc = wps._run_pursuit_stalls(threshold=14, account=None, dry_run=False)

    assert rc == 0
    (
        mock_alert.assert_not_called(),
        ("Stall alert must be suppressed when the same tier was already alerted in state"),
    )


def test_emits_cross_account_stall_alert(tmp_path: Path) -> None:
    """Task 4.7b: stall alerts fire for pursuits across multiple accounts.

    Two pursuits from different accounts (acme and globalpay) are both
    stalled past the threshold with no prior state.  Both must emit alerts.
    """
    _make_pursuit_file_branch_coverage(tmp_path, "acme", "deal-a", stage="discovery", days_ago=20)
    _make_pursuit_file_branch_coverage(tmp_path, "globalpay", "deal-b", stage="proposal", days_ago=25)

    config = {
        "accounts": {
            "acme": {"pursuit_dir": "accounts/acme/pursuits", "stall_threshold_days": 14},
            "globalpay": {"pursuit_dir": "accounts/globalpay/pursuits", "stall_threshold_days": 14},
        }
    }

    state_file = tmp_path / "state.json"
    alerts_file = tmp_path / "stall-alerts.md"

    with (
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.get_accounts_config", return_value=config),
        patch.object(stall_state, "state_file", return_value=state_file),
        patch.object(stall_render, "_alerts_file", return_value=alerts_file),
        patch("fieldkit.watch.pursuit_stalls.was_run_today", return_value=False),
        patch.object(stall_render, "append_stall_alert") as mock_alert,
    ):
        rc = wps._run_pursuit_stalls(threshold=14, account=None, dry_run=False)

    assert rc == 0
    assert mock_alert.call_count == 2, f"Expected 2 stall alerts (one per account), got {mock_alert.call_count}"
    alerted_accounts = {call.args[0]["account"] for call in mock_alert.call_args_list}
    assert "acme" in alerted_accounts, "acme pursuit must have generated a stall alert"
    assert "globalpay" in alerted_accounts, "globalpay pursuit must have generated a stall alert"


# ---------------------------------------------------------------------------
# Additional branch coverage for _run_pursuit_stalls (cc=28)
# ---------------------------------------------------------------------------


# ── TestRunPursuitStallsBranches (flattened) ─────────────────────────────────────────────


def _make_pursuit_file_run_pursuit_stalls_branches(
    tmp_path: Path,
    account: str,
    pursuit: str,
    stage: str,
    days_ago: int,
) -> Path:
    pursuits = tmp_path / "accounts" / account / "pursuits"
    pursuits.mkdir(parents=True, exist_ok=True)
    last_transition = (datetime.datetime.now(tz=datetime.UTC).date() - datetime.timedelta(days=days_ago)).isoformat()
    content = f"---\nstage: {stage}\nlast-transition: {last_transition}\n---\n# Deal\n"
    p = pursuits / f"{pursuit}.md"
    p.write_text(content, encoding="utf-8")
    return p


def test_fatal_prior_run_returns_exit_1(tmp_path: Path) -> None:
    """Lines 757-762: prior run outcome=fatal → return 1 immediately."""
    config = {"accounts": {"acme": {"pursuit_dir": "accounts/acme/pursuits", "stall_threshold_days": 14}}}
    state_file = tmp_path / "state.json"
    alerts_file = tmp_path / "stall-alerts.md"

    with (
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.get_accounts_config", return_value=config),
        patch.object(stall_state, "state_file", return_value=state_file),
        patch.object(stall_render, "_alerts_file", return_value=alerts_file),
        patch("fieldkit.watch.pursuit_stalls.was_run_today", return_value=True),
        patch("fieldkit.watch.pursuit_stalls.get_last_run_outcome", return_value="fatal"),
    ):
        rc = wps._run_pursuit_stalls(threshold=14, account=None, dry_run=False)

    assert rc == 1


def test_empty_accounts_returns_exit_1(tmp_path: Path) -> None:
    """Lines 771-773: no accounts in config → return 1."""
    config: dict[str, Any] = {"accounts": {}}
    state_file = tmp_path / "state.json"
    alerts_file = tmp_path / "stall-alerts.md"

    with (
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.get_accounts_config", return_value=config),
        patch("fieldkit.watch.pursuit_stalls.get_config_path", return_value=tmp_path / "accounts.yaml"),
        patch.object(stall_state, "state_file", return_value=state_file),
        patch.object(stall_render, "_alerts_file", return_value=alerts_file),
        patch("fieldkit.watch.pursuit_stalls.was_run_today", return_value=False),
    ):
        rc = wps._run_pursuit_stalls(threshold=14, account=None, dry_run=False)

    assert rc == 1


def test_unknown_account_filter_returns_exit_1(tmp_path: Path) -> None:
    """Lines 775-777: account_filter not in accounts → return 1."""
    config = {"accounts": {"acme": {"pursuit_dir": "accounts/acme/pursuits"}}}
    state_file = tmp_path / "state.json"
    alerts_file = tmp_path / "stall-alerts.md"

    with (
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.get_accounts_config", return_value=config),
        patch.object(stall_state, "state_file", return_value=state_file),
        patch.object(stall_render, "_alerts_file", return_value=alerts_file),
        patch("fieldkit.watch.pursuit_stalls.was_run_today", return_value=False),
    ):
        rc = wps._run_pursuit_stalls(threshold=14, account="nonexistent", dry_run=False)

    assert rc == 1


def test_state_write_failure_is_fatal_and_returns_nonzero(tmp_path: Path) -> None:
    """A state write failure is reported as fatal after the scan completes."""
    _make_pursuit_file_run_pursuit_stalls_branches(tmp_path, "acme", "deal", stage="discovery", days_ago=5)
    config = {"accounts": {"acme": {"pursuit_dir": "accounts/acme/pursuits", "stall_threshold_days": 14}}}
    state_file = tmp_path / "state.json"
    alerts_file = tmp_path / "stall-alerts.md"

    with (
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.get_accounts_config", return_value=config),
        patch.object(stall_state, "state_file", return_value=state_file),
        patch.object(stall_render, "_alerts_file", return_value=alerts_file),
        patch("fieldkit.watch.pursuit_stalls.was_run_today", return_value=False),
        patch.object(stall_state, "save_state", side_effect=OSError("disk full")),
        patch.object(wps, "write_run_status") as write_status,
    ):
        rc = wps._run_pursuit_stalls(threshold=14, account=None, dry_run=False)

    assert rc == 1
    assert write_status.call_args.kwargs["outcome"] == "fatal"
    assert write_status.call_args.kwargs["failures"] == 1


def test_snoozed_pursuit_counted_but_not_alerted(tmp_path: Path) -> None:
    """Lines 879-887: snoozed pursuit increments snoozed_count but emits no alert."""
    _make_pursuit_file_run_pursuit_stalls_branches(tmp_path, "acme", "deal", stage="discovery", days_ago=20)
    config = {"accounts": {"acme": {"pursuit_dir": "accounts/acme/pursuits", "stall_threshold_days": 14}}}

    # State has snoozed_until in the future
    snooze_until = (datetime.datetime.now(tz=datetime.UTC).date() + datetime.timedelta(days=5)).isoformat()
    prior_state = {
        "acme/deal": {
            "account": "acme",
            "pursuit": "deal",
            "stage": "discovery",
            "last_transition_date": (
                datetime.datetime.now(tz=datetime.UTC).date() - datetime.timedelta(days=20)
            ).isoformat(),
            "days_since_transition": 20,
            "is_stalled": True,
            "threshold_days": 14,
            "alerted_days_tier": 14,
            "snoozed_until": snooze_until,
            "checked_at": "2026-01-01T00:00:00Z",
        }
    }

    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(prior_state), encoding="utf-8")
    alerts_file = tmp_path / "stall-alerts.md"

    with (
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.get_accounts_config", return_value=config),
        patch.object(stall_state, "state_file", return_value=state_file),
        patch.object(stall_render, "_alerts_file", return_value=alerts_file),
        patch("fieldkit.watch.pursuit_stalls.was_run_today", return_value=False),
        patch.object(stall_render, "append_stall_alert") as mock_alert,
    ):
        rc = wps._run_pursuit_stalls(threshold=14, account=None, dry_run=False)

    assert rc == 0
    mock_alert.assert_not_called()


def test_account_scoped_prune_preserves_other_accounts(tmp_path: Path) -> None:
    """Lines 808-816: when account filter is set, other accounts' state is preserved."""
    _make_pursuit_file_run_pursuit_stalls_branches(tmp_path, "acme", "deal", stage="discovery", days_ago=5)
    config = {
        "accounts": {
            "acme": {"pursuit_dir": "accounts/acme/pursuits", "stall_threshold_days": 14},
            "globalpay": {"pursuit_dir": "accounts/globalpay/pursuits", "stall_threshold_days": 14},
        }
    }

    # State has entries for both accounts; acme/deal exists, globalpay/deal does not
    prior_state = {
        "acme/deal": {
            "account": "acme",
            "pursuit": "deal",
            "stage": "discovery",
            "last_transition_date": (
                datetime.datetime.now(tz=datetime.UTC).date() - datetime.timedelta(days=5)
            ).isoformat(),
            "days_since_transition": 5,
            "is_stalled": False,
            "threshold_days": 14,
            "alerted_days_tier": 0,
            "checked_at": "2026-01-01T00:00:00Z",
        },
        "globalpay/other-deal": {
            "account": "globalpay",
            "pursuit": "other-deal",
            "stage": "proposal",
            "last_transition_date": "2026-01-01",
            "days_since_transition": 100,
            "is_stalled": True,
            "threshold_days": 14,
            "alerted_days_tier": 30,
            "checked_at": "2026-01-01T00:00:00Z",
        },
    }

    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(prior_state), encoding="utf-8")
    alerts_file = tmp_path / "stall-alerts.md"

    with (
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.get_accounts_config", return_value=config),
        patch.object(stall_state, "state_file", return_value=state_file),
        patch.object(stall_render, "_alerts_file", return_value=alerts_file),
        patch("fieldkit.watch.pursuit_stalls.was_run_today", return_value=False),
    ):
        rc = wps._run_pursuit_stalls(threshold=14, account="acme", dry_run=False)

    assert rc == 0
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    # globalpay entry must be preserved (not pruned) when filtering to acme only
    assert "globalpay/other-deal" in saved, "Other-account state must be preserved when account filter is set"


def test_collection_failure_preserves_existing_state_for_unavailable_account(tmp_path: Path) -> None:
    """A missing account pursuit directory is not evidence that its state is stale."""
    _make_pursuit_file_run_pursuit_stalls_branches(tmp_path, "acme", "deal", stage="discovery", days_ago=5)
    config = {
        "accounts": {
            "acme": {"pursuit_dir": "accounts/acme/pursuits", "stall_threshold_days": 14},
            "unavailable": {"pursuit_dir": "accounts/unavailable/pursuits", "stall_threshold_days": 14},
        }
    }
    prior_state = {"unavailable/old-deal": {"account": "unavailable", "pursuit": "old-deal", "is_stalled": True}}
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(prior_state), encoding="utf-8")

    with (
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.get_accounts_config", return_value=config),
        patch.object(stall_state, "state_file", return_value=state_file),
        patch.object(stall_render, "_alerts_file", return_value=tmp_path / "stall-alerts.md"),
        patch("fieldkit.watch.pursuit_stalls.was_run_today", return_value=False),
    ):
        rc = wps._run_pursuit_stalls(threshold=14, account=None, dry_run=False)

    assert rc == 1
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["unavailable/old-deal"] == prior_state["unavailable/old-deal"]


def test_non_dict_account_config_is_data_failure(tmp_path: Path) -> None:
    """A malformed account config makes an otherwise successful run partial."""
    # One valid account, one invalid (string instead of dict)
    _make_pursuit_file_run_pursuit_stalls_branches(tmp_path, "acme", "deal", stage="discovery", days_ago=20)
    config = {
        "accounts": {
            "acme": {"pursuit_dir": "accounts/acme/pursuits", "stall_threshold_days": 14},
            "bad-account": "this-is-not-a-dict",
        }
    }
    state_file = tmp_path / "state.json"
    alerts_file = tmp_path / "stall-alerts.md"

    with (
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.get_accounts_config", return_value=config),
        patch.object(stall_state, "state_file", return_value=state_file),
        patch.object(stall_render, "_alerts_file", return_value=alerts_file),
        patch("fieldkit.watch.pursuit_stalls.was_run_today", return_value=False),
    ):
        rc = wps._run_pursuit_stalls(threshold=14, account=None, dry_run=False)

    assert rc == 1


def test_invalid_stall_threshold_uses_default(tmp_path: Path) -> None:
    """Lines 374-376: non-integer stall_threshold_days falls back to _DEFAULT_STALL_DAYS."""
    _make_pursuit_file_run_pursuit_stalls_branches(tmp_path, "acme", "deal", stage="discovery", days_ago=20)
    config = {
        "accounts": {
            "acme": {
                "pursuit_dir": "accounts/acme/pursuits",
                "stall_threshold_days": "not-a-number",  # invalid type
            }
        }
    }
    state_file = tmp_path / "state.json"
    alerts_file = tmp_path / "stall-alerts.md"

    with (
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.get_accounts_config", return_value=config),
        patch.object(stall_state, "state_file", return_value=state_file),
        patch.object(stall_render, "_alerts_file", return_value=alerts_file),
        patch("fieldkit.watch.pursuit_stalls.was_run_today", return_value=False),
        patch.object(stall_render, "append_stall_alert") as mock_alert,
    ):
        rc = wps._run_pursuit_stalls(threshold=14, account=None, dry_run=False)

    assert rc == 0
    # 20 days > default threshold of 14 → should still stall
    mock_alert.assert_called_once()


# ---------------------------------------------------------------------------
# Additional coverage for helper functions
# ---------------------------------------------------------------------------


# ── TestNormalizeTransitionEntry (flattened) ─────────────────────────────────────────────


def test_normalize_transition_entry_schema_a_stage_snapshot() -> None:
    """Schema A: {stage, date} → from_stage='', to_stage=stage."""
    entry = {"stage": "discovery", "date": "2026-01-15"}
    result = stall_scan.normalize_transition_entry(entry)
    assert result is not None
    from_stage, to_stage, d = result
    assert from_stage == ""
    assert to_stage == "discovery"
    assert d == datetime.date(2026, 1, 15)


def test_normalize_transition_entry_schema_b_explicit_from_to() -> None:
    """Schema B: {from, to, date} → explicit from/to."""
    entry = {"from": "prospecting", "to": "discovery", "date": "2026-02-01"}
    result = stall_scan.normalize_transition_entry(entry)
    assert result is not None
    from_stage, to_stage, d = result
    assert from_stage == "prospecting"
    assert to_stage == "discovery"
    assert d == datetime.date(2026, 2, 1)


def test_normalize_transition_entry_non_dict_returns_none() -> None:
    """Non-dict entry returns None."""
    assert stall_scan.normalize_transition_entry("not-a-dict") is None  # type: ignore[arg-type]


def test_normalize_transition_entry_missing_date_returns_none() -> None:
    """Entry with no parseable date returns None."""
    assert stall_scan.normalize_transition_entry({"stage": "discovery"}) is None


def test_normalize_transition_entry_legacy_last_transition_key() -> None:
    """Fallback to 'last-transition' key when 'date' is absent."""
    entry = {"stage": "proposal", "last-transition": "2026-03-10"}
    result = stall_scan.normalize_transition_entry(entry)
    assert result is not None
    _, _, d = result
    assert d == datetime.date(2026, 3, 10)


# ── Native qualification availability ───────────────────────────────────────


def test_native_qualification_status_linked_requires_live_fetch() -> None:
    from fieldkit.pursuit.qualification import native_qualification_status

    assert native_qualification_status("006example") == ("pending (live ClosePlan fetch required)")


def test_native_qualification_status_unlinked_is_unavailable() -> None:
    from fieldkit.pursuit.qualification import native_qualification_status

    assert native_qualification_status(None) == "unavailable (no Salesforce opportunity link)"


# ── TestIsSnoozed (flattened) ─────────────────────────────────────────────


def test_is_snoozed_no_snoozed_until_returns_false() -> None:
    entry: dict[str, Any] = {"stage": "discovery"}
    snoozed, reason = stall_state.is_snoozed(entry, current_stage="discovery")
    assert snoozed is False
    assert reason == ""


def test_is_snoozed_active_snooze_returns_true() -> None:
    future = (datetime.datetime.now(tz=datetime.UTC).date() + datetime.timedelta(days=5)).isoformat()
    entry: dict[str, Any] = {"stage": "discovery", "snoozed_until": future}
    snoozed, reason = stall_state.is_snoozed(entry, current_stage="discovery")
    assert snoozed is True
    assert "snoozed until" in reason


def test_is_snoozed_expired_snooze_returns_false_and_clears() -> None:
    past = (datetime.datetime.now(tz=datetime.UTC).date() - datetime.timedelta(days=1)).isoformat()
    entry: dict[str, Any] = {"stage": "discovery", "snoozed_until": past}
    snoozed, _reason = stall_state.is_snoozed(entry, current_stage="discovery")
    assert snoozed is False
    assert "snoozed_until" not in entry  # cleared in-place


def test_is_snoozed_stage_change_clears_snooze() -> None:
    future = (datetime.datetime.now(tz=datetime.UTC).date() + datetime.timedelta(days=5)).isoformat()
    entry: dict[str, Any] = {
        "stage": "discovery",
        "snoozed_until": future,
        "account": "acme",
        "pursuit": "deal",
    }
    snoozed, _reason = stall_state.is_snoozed(entry, current_stage="proposal")
    assert snoozed is False
    assert "snoozed_until" not in entry  # cleared on stage change


def test_is_snoozed_invalid_snoozed_until_returns_false() -> None:
    entry: dict[str, Any] = {"stage": "discovery", "snoozed_until": "not-a-date"}
    snoozed, _ = stall_state.is_snoozed(entry, current_stage="discovery")
    assert snoozed is False


# ---------------------------------------------------------------------------
# Additional coverage: save_state OSError, YAML parse error, missing pursuit dir
# ---------------------------------------------------------------------------


# ── TestPursuitStallsAdditionalBranches (flattened) ─────────────────────────────────────────────


def test_save_state_oserror_propagates(tmp_path: Path) -> None:
    """save_state propagates storage failures from the shared persistence helper."""
    state_file = tmp_path / "state.json"
    with (
        patch.object(stall_state, "state_file", return_value=state_file),
        patch.object(stall_state, "merge_state", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full") as exc_info,
    ):
        stall_state.save_state({"key": "value"})
    assert exc_info.type is OSError


def test_yaml_parse_error_in_frontmatter_returns_empty(tmp_path: Path) -> None:
    """Lines 121-131: YAML parse error in frontmatter -> returns {}."""
    # Write a file with malformed YAML frontmatter
    pursuit_file = tmp_path / "bad-yaml.md"
    pursuit_file.write_text(
        "---\nstage: discovery\nbad: [\nunclosed bracket\n---\n# Body\n",
        encoding="utf-8",
    )
    result = stall_scan.parse_frontmatter(
        "---\nstage: discovery\nbad: [\nunclosed bracket\n---\n",
        path=pursuit_file,
    )
    assert result == {}


def test_collect_pursuit_files_skips_missing_pursuit_dir(tmp_path: Path) -> None:
    """Lines 383-384: account with non-existent pursuit_dir is skipped."""
    config = {
        "accounts": {
            "acme": {
                "pursuit_dir": "accounts/acme/pursuits",  # dir doesn't exist
            }
        }
    }
    with patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path):
        results = stall_scan.collect_pursuit_files(
            accounts_config=config,
            account_filter=None,
        )
    assert results == []


def test_run_pursuit_stalls_injects_cli_threshold_for_accounts_without_override(tmp_path: Path) -> None:
    """Line 782: CLI threshold is injected for accounts without stall_threshold_days."""
    pursuits = tmp_path / "accounts" / "acme" / "pursuits"
    pursuits.mkdir(parents=True)
    last_transition = (datetime.datetime.now(tz=datetime.UTC).date() - datetime.timedelta(days=20)).isoformat()
    (pursuits / "deal.md").write_text(
        f"---\nstage: discovery\nlast-transition: {last_transition}\n---\n",
        encoding="utf-8",
    )
    # Account config has NO stall_threshold_days — CLI threshold should be injected
    config = {
        "accounts": {
            "acme": {"pursuit_dir": "accounts/acme/pursuits"},
        }
    }
    state_file = tmp_path / "state.json"
    alerts_file = tmp_path / "stall-alerts.md"

    with (
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.get_accounts_config", return_value=config),
        patch.object(stall_state, "state_file", return_value=state_file),
        patch.object(stall_render, "_alerts_file", return_value=alerts_file),
        patch("fieldkit.watch.pursuit_stalls.was_run_today", return_value=False),
        patch.object(stall_render, "append_stall_alert") as mock_alert,
    ):
        # Use threshold=7 (lower than 20 days) so the pursuit is stalled
        rc = wps._run_pursuit_stalls(threshold=7, account=None, dry_run=False)

    assert rc == 0
    # 20 days > 7 threshold → stall alert should fire
    mock_alert.assert_called_once()


# ── historic regression: Stall timer state persistence ───────────────────────────────────


# ── TestBug132StallTimerPersistence (flattened) ─────────────────────────────────────────────


def _make_config_and_pursuit_bug132_stall_timer_persistence(
    tmp_path: Path,
    stage: str,
    last_transition_iso: str,
    account: str = "acme",
) -> tuple[Path, dict]:
    pursuits_dir = tmp_path / "accounts" / account / "pursuits"
    pursuits_dir.mkdir(parents=True)
    pursuit_file = pursuits_dir / "deal.md"
    pursuit_file.write_text(
        f"---\nstage: {stage}\nlast-transition: {last_transition_iso}\n---\n# Body\n",
        encoding="utf-8",
    )
    config = {
        "accounts": {
            account: {
                "pursuit_dir": f"accounts/{account}/pursuits",
                "stall_threshold_days": 30,
            }
        }
    }
    return pursuit_file, config


def test_detected_transition_date_persisted_on_stage_change(tmp_path: Path) -> None:
    """When stage changes without frontmatter update, detected_transition_date is saved."""
    from unittest.mock import patch

    old_date = (datetime.datetime.now(tz=datetime.UTC).date() - datetime.timedelta(days=40)).isoformat()
    _, config = _make_config_and_pursuit_bug132_stall_timer_persistence(tmp_path, "validate", old_date)

    state_file = tmp_path / "state.json"
    alerts_file = tmp_path / "stall-alerts.md"

    # Prior state claims pursuit was at "discover" — stage change will be detected.
    prior_state = {
        "acme/deal": {
            "account": "acme",
            "pursuit": "deal",
            "stage": "discover",  # different from current "validate"
            "last_transition_date": old_date,  # same as frontmatter — advance skill didn't update
            "days_since_transition": 40,
            "is_stalled": True,
            "threshold_days": 30,
            "alerted_days_tier": 2,
            "checked_at": "2026-06-01T00:00:00Z",
        }
    }
    state_file.write_text(json.dumps(prior_state), encoding="utf-8")

    today_str = datetime.datetime.now(tz=datetime.UTC).date().isoformat()

    with (
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.get_accounts_config", return_value=config),
        patch.object(stall_state, "state_file", return_value=state_file),
        patch.object(stall_render, "_alerts_file", return_value=alerts_file),
        patch("fieldkit.watch.pursuit_stalls.was_run_today", return_value=False),
        patch.object(stall_render, "append_stall_alert"),
    ):
        rc = wps._run_pursuit_stalls(threshold=30, account=None, dry_run=False)

    assert rc == 0
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    entry = saved.get("acme/deal", {})
    # detected_transition_date must be set to today
    assert "detected_transition_date" in entry, "historic regression: detected_transition_date not persisted"
    assert entry["detected_transition_date"] == today_str
    # days_since_transition must be 0 (or very small) on the change run
    assert entry["days_since_transition"] == 0


def test_stall_timer_uses_detected_date_on_subsequent_run(tmp_path: Path) -> None:
    """On next run after stage change, detected_transition_date keeps timer near 0."""
    from unittest.mock import patch

    old_date = (datetime.datetime.now(tz=datetime.UTC).date() - datetime.timedelta(days=40)).isoformat()
    detected_date = (datetime.datetime.now(tz=datetime.UTC).date() - datetime.timedelta(days=2)).isoformat()
    _, config = _make_config_and_pursuit_bug132_stall_timer_persistence(tmp_path, "validate", old_date)

    state_file = tmp_path / "state.json"
    alerts_file = tmp_path / "stall-alerts.md"

    # Simulate "day after stage change": same stage in prior, detected_transition_date set.
    prior_state = {
        "acme/deal": {
            "account": "acme",
            "pursuit": "deal",
            "stage": "validate",  # same as current — no stage change this run
            "last_transition_date": old_date,  # frontmatter still stale
            "days_since_transition": 0,
            "is_stalled": False,
            "threshold_days": 30,
            "alerted_days_tier": 0,
            "checked_at": "2026-06-16T00:00:00Z",
            "detected_transition_date": detected_date,  # set by prior run
        }
    }
    state_file.write_text(json.dumps(prior_state), encoding="utf-8")

    with (
        patch("fieldkit.watch._pursuit_stall_scan.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.pursuit_stalls.get_accounts_config", return_value=config),
        patch.object(stall_state, "state_file", return_value=state_file),
        patch.object(stall_render, "_alerts_file", return_value=alerts_file),
        patch("fieldkit.watch.pursuit_stalls.was_run_today", return_value=False),
        patch.object(stall_render, "append_stall_alert") as mock_alert,
    ):
        rc = wps._run_pursuit_stalls(threshold=30, account=None, dry_run=False)

    assert rc == 0
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    entry = saved.get("acme/deal", {})
    # days_since_transition must reflect detected_date (2 days), not stale frontmatter (40 days).
    # Allow {2, 3} to handle tests running across midnight boundary.
    detected_dt = datetime.date.fromisoformat(detected_date)
    expected_days = (datetime.datetime.now(tz=datetime.UTC).date() - detected_dt).days
    assert entry["days_since_transition"] in {expected_days, expected_days + 1}, (
        f"historic regression: stall timer jumped back to stale frontmatter date — "
        f"got {entry['days_since_transition']} days, expected {expected_days} (±1 for midnight)"
    )
    # Must NOT be stalled (2 days << 30 threshold)
    assert not entry["is_stalled"]
    # No stall alert should fire
    mock_alert.assert_not_called()


# ── Direct unit tests for _apply_bug132_transition_date ─────────────────────


# ── TestApplyBug132TransitionDate (flattened) ─────────────────────────────────────────────


def _make_result_apply_bug132_transition_date(last_transition_date: str, days: int = 5, threshold: int = 30) -> dict:
    return {
        "account": "acme",
        "pursuit": "deal",
        "stage": "validate",
        "last_transition_date": last_transition_date,
        "days_since_transition": days,
        "is_stalled": days > threshold,
        "threshold_days": threshold,
    }


def test_apply_bug132_transition_date_branch1_stage_changed_fm_not_updated_stores_sentinel() -> None:
    """Branch 1: stage_changed=True, fm_date == prior_fm_date → store detected_date."""
    old_date = "2026-01-01"
    result = _make_result_apply_bug132_transition_date(old_date, days=40)
    prior = {"last_transition_date": old_date}
    today = datetime.date(2026, 6, 18)

    out = stall_state.apply_detected_transition_date(result, prior, stage_changed=True, today=today)

    assert out["detected_transition_date"] == "2026-06-18"
    # Input must not be mutated
    assert "detected_transition_date" not in result


def test_apply_bug132_transition_date_branch2_stage_changed_fm_updated_clears_sentinel() -> None:
    """Branch 2: stage_changed=True, fm_date != prior_fm_date → clear sentinel."""
    old_date = "2026-01-01"
    new_date = "2026-06-18"
    result = _make_result_apply_bug132_transition_date(new_date, days=0)
    # Simulate result had a detected date from a prior run
    result["detected_transition_date"] = "2026-06-15"
    prior = {"last_transition_date": old_date}
    today = datetime.date(2026, 6, 18)

    out = stall_state.apply_detected_transition_date(result, prior, stage_changed=True, today=today)

    assert "detected_transition_date" not in out


def test_apply_bug132_transition_date_branch3_no_stage_change_prior_none_returns_unchanged() -> None:
    """Branch 3: stage_changed=False, prior=None → early return, result unchanged."""
    result = _make_result_apply_bug132_transition_date("2026-01-01", days=40)
    today = datetime.date(2026, 6, 18)

    out = stall_state.apply_detected_transition_date(result, None, stage_changed=False, today=today)

    assert out == result


def test_apply_bug132_transition_date_branch4_sentinel_cleared_when_fm_catches_up() -> None:
    """Branch 4: fm_date >= detected_str → sentinel cleared."""
    detected = "2026-06-10"
    fm_date = "2026-06-10"  # equal (>= not just >)
    result = _make_result_apply_bug132_transition_date(fm_date, days=8)
    prior = {"last_transition_date": "2026-01-01", "detected_transition_date": detected}
    today = datetime.date(2026, 6, 18)

    out = stall_state.apply_detected_transition_date(result, prior, stage_changed=False, today=today)

    assert "detected_transition_date" not in out


def test_apply_bug132_transition_date_branch4b_sentinel_cleared_when_fm_exceeds_detected() -> None:
    """Branch 4b: fm_date > detected_str → sentinel also cleared."""
    detected = "2026-06-10"
    fm_date = "2026-06-15"  # strictly greater
    result = _make_result_apply_bug132_transition_date(fm_date, days=3)
    prior = {"last_transition_date": "2026-01-01", "detected_transition_date": detected}
    today = datetime.date(2026, 6, 18)

    out = stall_state.apply_detected_transition_date(result, prior, stage_changed=False, today=today)

    assert "detected_transition_date" not in out


def test_apply_bug132_transition_date_branch5_fm_still_stale_uses_detected_date() -> None:
    """Branch 5: fm_date < detected_str → use detected date, recalculate timer."""
    detected = "2026-06-16"  # 2 days ago
    fm_date = "2026-01-01"  # 40 days ago (stale)
    result = _make_result_apply_bug132_transition_date(fm_date, days=40)
    prior = {"last_transition_date": "2026-01-01", "detected_transition_date": detected}
    today = datetime.date(2026, 6, 18)

    out = stall_state.apply_detected_transition_date(result, prior, stage_changed=False, today=today)

    assert out["last_transition_date"] == detected
    assert out["detected_transition_date"] == detected
    assert out["days_since_transition"] == 2
    assert not out["is_stalled"]  # 2 < threshold of 30


def test_apply_bug132_transition_date_branch6_malformed_detected_date_reverts_state() -> None:
    """Branch 6: malformed detected_transition_date → revert mutation, no sentinel."""
    fm_date = "2026-01-01"
    result = _make_result_apply_bug132_transition_date(fm_date, days=40)
    prior = {"last_transition_date": "2026-01-01", "detected_transition_date": "not-a-date"}
    today = datetime.date(2026, 6, 18)

    out = stall_state.apply_detected_transition_date(result, prior, stage_changed=False, today=today)

    # Must revert: last_transition_date back to frontmatter value
    assert out["last_transition_date"] == fm_date
    # Must not persist the malformed sentinel
    assert "detected_transition_date" not in out
    # days_since_transition and is_stalled must remain at original values (not updated)
    assert out["days_since_transition"] == 40


def test_apply_bug132_transition_date_expired_sentinel_is_cleared() -> None:
    """A5.3: sentinel older than stall_state._DETECTED_DATE_MAX_AGE_DAYS (91 days) is cleared."""

    today = datetime.date(2026, 6, 18)
    expired_date = (today - datetime.timedelta(days=stall_state._DETECTED_DATE_MAX_AGE_DAYS + 1)).isoformat()
    fm_date = "2026-01-01"  # still older than expired_date, so fm hasn't caught up

    result = _make_result_apply_bug132_transition_date(fm_date, days=100)
    prior = {"last_transition_date": "2026-01-01", "detected_transition_date": expired_date}

    out = stall_state.apply_detected_transition_date(result, prior, stage_changed=False, today=today)

    # Expired sentinel must NOT be applied — absent from output
    assert "detected_transition_date" not in out
    # days_since_transition must NOT be updated to use the expired detected date
    assert out["days_since_transition"] == 100  # unchanged from input
    # last_transition_date must also be unchanged
    assert out["last_transition_date"] == fm_date


def test_apply_bug132_transition_date_sentinel_at_exact_ttl_boundary_is_not_expired() -> None:
    """A5: sentinel exactly stall_state._DETECTED_DATE_MAX_AGE_DAYS old is NOT expired (strict >)."""

    today = datetime.date(2026, 6, 18)
    boundary_date = (today - datetime.timedelta(days=stall_state._DETECTED_DATE_MAX_AGE_DAYS)).isoformat()
    fm_date = "2026-01-01"

    result = _make_result_apply_bug132_transition_date(fm_date, days=100)
    prior = {"last_transition_date": "2026-01-01", "detected_transition_date": boundary_date}

    out = stall_state.apply_detected_transition_date(result, prior, stage_changed=False, today=today)

    # Exactly at boundary → NOT expired (strict > not >=)
    assert "detected_transition_date" in out
    assert out["days_since_transition"] == stall_state._DETECTED_DATE_MAX_AGE_DAYS  # applied correctly


def test_apply_bug132_transition_date_non_expired_sentinel_is_applied() -> None:
    """A5.4: sentinel within TTL (30 days) is applied with correct days_since_transition."""
    today = datetime.date(2026, 6, 18)
    recent_date = (today - datetime.timedelta(days=30)).isoformat()
    fm_date = "2026-01-01"  # stale — hasn't caught up

    result = _make_result_apply_bug132_transition_date(fm_date, days=100)
    prior = {"last_transition_date": "2026-01-01", "detected_transition_date": recent_date}

    out = stall_state.apply_detected_transition_date(result, prior, stage_changed=False, today=today)

    # Sentinel within TTL must still be applied
    assert "detected_transition_date" in out
    assert out["last_transition_date"] == recent_date
    # days_since_transition must be recalculated from detected date (30 days), not fm (100)
    assert out["days_since_transition"] == 30
