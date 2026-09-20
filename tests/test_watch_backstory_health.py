"""Tests for routines/watch_backstory_health.py.

Covers:
- Score drop below threshold → alert appended to backstory-alerts.md
- Score above threshold → no alert written
- Sidecar JSON updated with current scores after a run
- API failure (find_account raises) → error line written instead of crashing
- compute_health_score() edge cases (empty list, missing key)
- append_alert() output format (delta, threshold, timestamp present)
- append_api_error() output format
- load_state() / save_state() round-trip
- check_account() orchestration with mocked MCPSession
- main() integration path (accounts.yaml fixture, mocked MCP)
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------

_TOOLS_ROOT = Path(__file__).resolve().parents[1]

from click.testing import CliRunner  # noqa: E402

import fieldkit.watch.backstory_health as wbh  # noqa: E402
from fieldkit.commands.watch.backstory_health import cli  # noqa: E402

pytestmark = pytest.mark.unit


def test_state_paths_use_the_configured_roots(tmp_path: Path) -> None:
    """Path helpers keep all health-watcher artifacts under their canonical roots."""
    with (
        patch.object(wbh, "get_fieldkit_home", return_value=tmp_path / "home"),
        patch.object(wbh, "get_watchers_dir", return_value=tmp_path / "watchers"),
    ):
        wbh._accounts_config.cache_clear()
        wbh._alerts_file.cache_clear()
        wbh._state_file.cache_clear()
        accounts_path = wbh._accounts_config()
        alerts_path = wbh._alerts_file()
        state_path = wbh._state_file()

    assert accounts_path == tmp_path / "home" / "config" / "accounts.yaml"
    assert alerts_path == tmp_path / "watchers" / "backstory-alerts.md"
    assert state_path == tmp_path / "watchers" / "backstory-health-state.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_find_account_result(
    account_id: int = 42,
    opportunity_levels: list[float] | None = None,
) -> dict[str, object]:
    """Build a minimal backstory__find_account response dict."""
    if opportunity_levels is None:
        opportunity_levels = [75.0, 75.0]  # default: healthy
    return {
        "peopleai_account_id": account_id,
        "opportunities": [{"engagement_level": lvl} for lvl in opportunity_levels],
    }


def _mock_session(
    *,
    find_account_result: dict[str, object] | None = None,
    find_account_raises: Exception | None = None,
    get_status_result: str = "Risks:\n - some risk\n",
    get_status_raises: Exception | None = None,
) -> MagicMock:
    """Return a MagicMock MCPSession whose call_tool is configured."""
    session = MagicMock(spec=wbh.MCPSession)

    def _call_tool(tool_name: str, arguments: dict[str, object]) -> object:
        if tool_name == "backstory__find_account":
            if find_account_raises:
                raise find_account_raises
            return find_account_result or _make_find_account_result()
        if tool_name == "backstory__get_account_status":
            if get_status_raises:
                raise get_status_raises
            return get_status_result
        raise AssertionError(f"Unexpected tool call: {tool_name!r}")

    session.call_tool.side_effect = _call_tool
    return session


# ---------------------------------------------------------------------------
# compute_health_score
# ---------------------------------------------------------------------------


# ── TestComputeHealthScore (flattened) ─────────────────────────────────────────────


def test_compute_health_score_empty_list_returns_zero() -> None:
    assert wbh.compute_health_score([]) == 0.0


def test_compute_health_score_single_opportunity() -> None:
    assert wbh.compute_health_score([{"engagement_level": 80}]) == pytest.approx(80.0)


def test_compute_health_score_mean_of_multiple() -> None:
    opps = [{"engagement_level": 60}, {"engagement_level": 40}]
    assert wbh.compute_health_score(opps) == pytest.approx(50.0)


def test_compute_health_score_opportunities_missing_engagement_level_are_excluded() -> None:
    opps = [{"engagement_level": 80}, {"name": "opp_without_level"}]
    assert wbh.compute_health_score(opps) == pytest.approx(80.0)


def test_compute_health_score_all_missing_engagement_levels_returns_zero() -> None:
    opps = [{"name": "no_level"}, {"other": "key"}]
    assert wbh.compute_health_score(opps) == 0.0


# ---------------------------------------------------------------------------
# append_alert
# ---------------------------------------------------------------------------


# ── TestAppendAlert (flattened) ─────────────────────────────────────────────


def test_append_alert_alert_written_to_file(tmp_path: Path) -> None:
    alerts_file = tmp_path / "backstory-alerts.md"
    with (
        patch.object(wbh, "_alerts_file", return_value=alerts_file),
        patch.object(wbh, "get_watchers_dir", return_value=tmp_path),
    ):
        wbh.append_alert(
            account_key="test-corp",
            current_score=55.0,
            previous_score=75.0,
            threshold=60,
            risk_count=2,
            dry_run=False,
        )
    content = alerts_file.read_text()
    assert "test-corp" in content
    assert "55.0" in content
    assert "Δ -20.0" in content
    assert "75.0" in content
    assert "threshold" in content.lower()


def test_append_alert_dry_run_does_not_write_file(tmp_path: Path) -> None:
    alerts_file = tmp_path / "backstory-alerts.md"
    with (
        patch.object(wbh, "_alerts_file", return_value=alerts_file),
        patch.object(wbh, "get_watchers_dir", return_value=tmp_path),
    ):
        wbh.append_alert(
            account_key="test-corp",
            current_score=30.0,
            previous_score=None,
            threshold=60,
            risk_count=0,
            dry_run=True,
        )
    assert not alerts_file.exists()


def test_append_alert_no_previous_score_omits_delta(tmp_path: Path) -> None:
    alerts_file = tmp_path / "backstory-alerts.md"
    with (
        patch.object(wbh, "_alerts_file", return_value=alerts_file),
        patch.object(wbh, "get_watchers_dir", return_value=tmp_path),
    ):
        wbh.append_alert(
            account_key="corp",
            current_score=40.0,
            previous_score=None,
            threshold=60,
            risk_count=-1,
            dry_run=False,
        )
    content = alerts_file.read_text()
    assert "Δ" not in content


def test_append_alert_timestamp_present_in_alert(tmp_path: Path) -> None:
    alerts_file = tmp_path / "backstory-alerts.md"
    with (
        patch.object(wbh, "_alerts_file", return_value=alerts_file),
        patch.object(wbh, "get_watchers_dir", return_value=tmp_path),
    ):
        wbh.append_alert(
            account_key="corp",
            current_score=40.0,
            previous_score=None,
            threshold=60,
            risk_count=0,
            dry_run=False,
        )
    content = alerts_file.read_text()
    # ISO timestamp present (at minimum YYYY-MM-DD)
    import re

    assert re.search(r"\d{4}-\d{2}-\d{2}", content)


# ---------------------------------------------------------------------------
# append_api_error
# ---------------------------------------------------------------------------


# ── TestAppendApiError (flattened) ─────────────────────────────────────────────


def test_append_api_error_error_written_to_file(tmp_path: Path) -> None:
    alerts_file = tmp_path / "backstory-alerts.md"
    with (
        patch.object(wbh, "_alerts_file", return_value=alerts_file),
        patch.object(wbh, "get_watchers_dir", return_value=tmp_path),
    ):
        wbh.append_api_error("acme", "connection refused", dry_run=False)
    content = alerts_file.read_text()
    assert "acme" in content
    assert "connection refused" in content


def test_append_api_error_dry_run_does_not_write(tmp_path: Path) -> None:
    alerts_file = tmp_path / "backstory-alerts.md"
    with (
        patch.object(wbh, "_alerts_file", return_value=alerts_file),
        patch.object(wbh, "get_watchers_dir", return_value=tmp_path),
    ):
        wbh.append_api_error("acme", "timeout", dry_run=True)
    assert not alerts_file.exists()


# ---------------------------------------------------------------------------
# load_state / save_state
# ---------------------------------------------------------------------------


# ── TestStateRoundTrip (flattened) ─────────────────────────────────────────────


def test_save_and_load(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    watchers_dir = tmp_path
    with (
        patch.object(wbh, "_state_file", return_value=state_file),
        patch.object(wbh, "get_watchers_dir", return_value=watchers_dir),
    ):
        data = {"acme": {"health_score": 72.5, "checked_at": "2026-01-01T07:00:00Z"}}
        result = wbh.save_state(data)
        loaded = wbh.load_state()
    assert result is None
    assert loaded == data


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    state_file = tmp_path / "nonexistent.json"
    with patch.object(wbh, "_state_file", return_value=state_file):
        assert wbh.load_state() == {}


def test_load_corrupt_file_returns_empty(tmp_path: Path) -> None:
    state_file = tmp_path / "corrupt.json"
    state_file.write_text("not-json{{{{", encoding="utf-8")
    with patch.object(wbh, "_state_file", return_value=state_file):
        assert wbh.load_state() == {}


# ---------------------------------------------------------------------------
# check_account — score drop → alert written
# ---------------------------------------------------------------------------


# ── TestCheckAccountAlertOnDrop (flattened) ─────────────────────────────────────────────


def test_score_drop_writes_alert(tmp_path: Path) -> None:
    alerts_file = tmp_path / "backstory-alerts.md"
    watchers_dir = tmp_path

    # Previous state: healthy score of 75
    prior_state = {"global-pay": {"health_score": 75.0}}

    # Mock: find_account returns score of 55 (two opps avg = 55)
    session = _mock_session(
        find_account_result=_make_find_account_result(account_id=99, opportunity_levels=[55.0, 55.0])
    )

    with (
        patch.object(wbh, "_alerts_file", return_value=alerts_file),
        patch.object(wbh, "get_watchers_dir", return_value=watchers_dir),
    ):
        result = wbh.check_account(
            session=session,
            account_key="global-pay",
            account_cfg={"keywords": ["GlobalPay"]},
            state=prior_state,
            default_threshold=60,
            dry_run=False,
        )

    assert result is not None
    assert result["health_score"] == pytest.approx(55.0)

    content = alerts_file.read_text()
    assert "global-pay" in content
    assert "55.0" in content
    # Delta from 75 to 55 = -20
    assert "Δ -20.0" in content


def test_score_drop_uses_account_threshold(tmp_path: Path) -> None:
    """Account-level threshold from cfg overrides default."""
    alerts_file = tmp_path / "backstory-alerts.md"
    watchers_dir = tmp_path

    session = _mock_session(
        find_account_result=_make_find_account_result(
            opportunity_levels=[65.0]  # above default 60 but below account 70
        )
    )
    with (
        patch.object(wbh, "_alerts_file", return_value=alerts_file),
        patch.object(wbh, "get_watchers_dir", return_value=watchers_dir),
    ):
        wbh.check_account(
            session=session,
            account_key="corp",
            account_cfg={"keywords": ["Corp"], "health_score_threshold": 70},
            state={},
            default_threshold=60,
            dry_run=False,
        )
    # Score 65 < per-account threshold 70 → alert expected
    assert alerts_file.exists()
    assert "corp" in alerts_file.read_text()


# ---------------------------------------------------------------------------
# check_account — score above threshold → no alert
# ---------------------------------------------------------------------------


# ── TestCheckAccountNoAlertWhenHealthy (flattened) ─────────────────────────────────────────────


# pii-guard: ignore
def test_no_alert_when_score_above_threshold(tmp_path: Path) -> None:  # pii-guard: ignore
    alerts_file = tmp_path / "backstory-alerts.md"
    watchers_dir = tmp_path

    session = _mock_session(
        find_account_result=_make_find_account_result(
            opportunity_levels=[80.0, 90.0]  # mean = 85 > threshold 60
        )
    )

    with (
        patch.object(wbh, "_alerts_file", return_value=alerts_file),
        patch.object(wbh, "get_watchers_dir", return_value=watchers_dir),
    ):
        result = wbh.check_account(
            session=session,
            account_key="globalpay",
            account_cfg={"keywords": ["Globalpay"]},
            state={},
            default_threshold=60,
            dry_run=False,
        )

    assert result is not None
    assert result["health_score"] == pytest.approx(85.0)
    # No alert file written
    assert not alerts_file.exists()


def test_score_exactly_at_threshold_is_not_alerted(tmp_path: Path) -> None:
    """Score == threshold is not < threshold, so no alert."""
    alerts_file = tmp_path / "backstory-alerts.md"
    watchers_dir = tmp_path

    session = _mock_session(find_account_result=_make_find_account_result(opportunity_levels=[60.0]))
    with (
        patch.object(wbh, "_alerts_file", return_value=alerts_file),
        patch.object(wbh, "get_watchers_dir", return_value=watchers_dir),
    ):
        wbh.check_account(
            session=session,
            account_key="corp",
            account_cfg={},
            state={},
            default_threshold=60,
            dry_run=False,
        )
    assert not alerts_file.exists()


# ---------------------------------------------------------------------------
# Sidecar JSON updated with current scores
# ---------------------------------------------------------------------------


# ── TestSidecarJsonUpdated (flattened) ─────────────────────────────────────────────


def test_sidecar_contains_current_score(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    watchers_dir = tmp_path
    alerts_file = tmp_path / "backstory-alerts.md"

    session = _mock_session(
        find_account_result=_make_find_account_result(account_id=7, opportunity_levels=[82.0, 78.0])
    )

    with (
        patch.object(wbh, "_state_file", return_value=state_file),
        patch.object(wbh, "get_watchers_dir", return_value=watchers_dir),
        patch.object(wbh, "_alerts_file", return_value=alerts_file),
    ):
        result = wbh.check_account(
            session=session,
            account_key="acme",
            account_cfg={"keywords": ["Acme"]},
            state={},
            default_threshold=60,
            dry_run=False,
        )

    assert result is not None
    assert result["health_score"] == pytest.approx(80.0)  # mean(82, 78)
    assert result["peopleai_account_id"] == 7
    assert "checked_at" in result


def test_main_persists_state_after_successful_check(tmp_path: Path) -> None:
    """main() saves updated state to the sidecar JSON file."""
    state_file = tmp_path / "state.json"
    alerts_file = tmp_path / "backstory-alerts.md"
    accounts_yaml = tmp_path / "accounts.yaml"
    accounts_yaml.write_text("accounts:\n  acme:\n    keywords:\n      - Acme Corp\n", encoding="utf-8")

    find_result = _make_find_account_result(account_id=5, opportunity_levels=[70.0])

    def _fake_initialize(self_: object) -> None:
        pass

    def _fake_close(self_: object) -> None:
        pass

    def _fake_call_tool(self_: object, tool_name: str, arguments: dict[str, object]) -> object:
        if tool_name == "backstory__find_account":
            return find_result
        if tool_name == "backstory__get_account_status":
            return "Summary: all good"
        raise AssertionError(f"Unexpected: {tool_name}")

    acme_config = {"accounts": {"acme": {"keywords": ["Acme Corp"]}}}

    with (
        patch("fieldkit.watch.backstory_health.get_accounts_config", return_value=acme_config),
        patch.object(wbh, "_state_file", return_value=state_file),
        patch.object(wbh, "get_watchers_dir", return_value=tmp_path),
        patch.object(wbh, "_alerts_file", return_value=alerts_file),
        patch.object(wbh.MCPSession, "initialize", _fake_initialize),
        patch.object(wbh.MCPSession, "close", _fake_close),
        patch.object(wbh.MCPSession, "call_tool", _fake_call_tool),
    ):
        result = CliRunner().invoke(cli, [])

    assert result.exit_code == 0
    assert state_file.exists()
    state = json.loads(state_file.read_text())
    assert "acme" in state
    assert state["acme"]["health_score"] == pytest.approx(70.0)


# ---------------------------------------------------------------------------
# API failure → error line written instead of crashing
# ---------------------------------------------------------------------------


# ── TestApiFailureHandling (flattened) ─────────────────────────────────────────────


def test_api_failure_writes_error_line(tmp_path: Path) -> None:
    alerts_file = tmp_path / "backstory-alerts.md"
    watchers_dir = tmp_path

    session = _mock_session(find_account_raises=RuntimeError("MCP HTTP error: connection refused"))

    with (
        patch.object(wbh, "_alerts_file", return_value=alerts_file),
        patch.object(wbh, "get_watchers_dir", return_value=watchers_dir),
    ):
        result = wbh.check_account(
            session=session,
            account_key="failing-corp",
            account_cfg={"keywords": ["Failing"]},
            state={},
            default_threshold=60,
            dry_run=False,
        )

    # Returns None on total failure
    assert result is None
    # But error logged to the alerts file
    assert alerts_file.exists()
    content = alerts_file.read_text()
    assert "failing-corp" in content
    assert "API error" in content or "error" in content.lower()


def test_api_failure_in_main_counted_in_summary(tmp_path: Path) -> None:
    """main() does not exit 1 on per-account API failure; it counts failures."""
    accounts_yaml = tmp_path / "accounts.yaml"
    accounts_yaml.write_text("accounts:\n  bad-corp:\n    keywords:\n      - Bad\n", encoding="utf-8")
    state_file = tmp_path / "state.json"
    alerts_file = tmp_path / "backstory-alerts.md"

    def _fake_initialize(self_: object) -> None:
        pass

    def _fake_close(self_: object) -> None:
        pass

    def _fake_call_tool(self_: object, tool_name: str, arguments: dict[str, object]) -> object:
        raise RuntimeError("gateway down")

    bad_corp_config = {"accounts": {"bad-corp": {"keywords": ["Bad"]}}}

    with (
        patch("fieldkit.watch.backstory_health.get_accounts_config", return_value=bad_corp_config),
        patch.object(wbh, "_state_file", return_value=state_file),
        patch.object(wbh, "get_watchers_dir", return_value=tmp_path),
        patch.object(wbh, "_alerts_file", return_value=alerts_file),
        patch.object(wbh.MCPSession, "initialize", _fake_initialize),
        patch.object(wbh.MCPSession, "close", _fake_close),
        patch.object(wbh.MCPSession, "call_tool", _fake_call_tool),
    ):
        result = CliRunner().invoke(cli, [])

    # Process must not crash (exit 0 even with per-account failure)
    assert result.exit_code == 0
    assert alerts_file.exists()
    content = alerts_file.read_text()
    assert "bad-corp" in content


def test_non_dict_response_writes_error(tmp_path: Path) -> None:
    """Non-dict find_account response treated as API error, no crash."""
    alerts_file = tmp_path / "backstory-alerts.md"
    watchers_dir = tmp_path

    session = MagicMock(spec=wbh.MCPSession)
    session.call_tool.return_value = "unexpected string response"

    with (
        patch.object(wbh, "_alerts_file", return_value=alerts_file),
        patch.object(wbh, "get_watchers_dir", return_value=watchers_dir),
    ):
        result = wbh.check_account(
            session=session,
            account_key="weird-corp",
            account_cfg={},
            state={},
            default_threshold=60,
            dry_run=False,
        )

    assert result is None
    assert alerts_file.exists()


def test_missing_account_id_writes_error(tmp_path: Path) -> None:
    """find_account response without peopleai_account_id treated as error."""
    alerts_file = tmp_path / "backstory-alerts.md"
    watchers_dir = tmp_path

    session = MagicMock(spec=wbh.MCPSession)
    session.call_tool.return_value = {"opportunities": [{"engagement_level": 70.0}]}
    # No peopleai_account_id key

    with (
        patch.object(wbh, "_alerts_file", return_value=alerts_file),
        patch.object(wbh, "get_watchers_dir", return_value=watchers_dir),
    ):
        result = wbh.check_account(
            session=session,
            account_key="corp",
            account_cfg={"keywords": ["Corp"]},
            state={},
            default_threshold=60,
            dry_run=False,
        )

    assert result is None
    assert alerts_file.exists()


# ---------------------------------------------------------------------------
# get_risk_count
# ---------------------------------------------------------------------------


# ── TestGetRiskCount (flattened) ─────────────────────────────────────────────


def test_get_risk_count_counts_risks_in_status_narrative() -> None:
    session = MagicMock(spec=wbh.MCPSession)
    session.call_tool.return_value = "Summary: good\nRisks:\n - risk one\n - risk two\nOpportunities:\n - opp\n"
    count = wbh.get_risk_count(session, account_id=1)
    assert count == 2


def test_get_risk_count_returns_minus_one_on_api_error() -> None:
    session = MagicMock(spec=wbh.MCPSession)
    session.call_tool.side_effect = RuntimeError("api error")
    count = wbh.get_risk_count(session, account_id=1)
    assert count == -1


def test_get_risk_count_returns_zero_when_no_risks_section() -> None:
    session = MagicMock(spec=wbh.MCPSession)
    session.call_tool.return_value = "All clear. No issues found."
    count = wbh.get_risk_count(session, account_id=1)
    assert count == 0


# ---------------------------------------------------------------------------
# T06 regression tests: non-numeric engagement_level and check_account exception
# ---------------------------------------------------------------------------


# ── TestNonNumericEngagementLevel (flattened) ─────────────────────────────────────────────


def test_mixed_numeric_and_string_returns_numeric_mean() -> None:
    """String 'high' is skipped; numeric 75 is kept; result is 75.0."""
    opps = [{"engagement_level": "high"}, {"engagement_level": 75}]
    assert wbh.compute_health_score(opps) == pytest.approx(75.0)


def test_none_engagement_level_is_skipped() -> None:
    """None engagement_level is skipped without raising."""
    opps = [{"engagement_level": None}, {"engagement_level": 50.0}]
    assert wbh.compute_health_score(opps) == pytest.approx(50.0)


def test_all_non_numeric_returns_zero() -> None:
    """All non-numeric engagement levels → 0.0 (no crash)."""
    opps = [{"engagement_level": "low"}, {"engagement_level": "high"}, {"engagement_level": None}]
    assert wbh.compute_health_score(opps) == 0.0


# ── TestRunBackstoryHealthExceptionHandling (flattened) ─────────────────────────────────────────────


def test_check_account_raises_increments_api_failures_and_returns_zero(tmp_path: Path) -> None:
    """When check_account raises ValueError, api_failures is incremented and exit code is 0."""
    state_file = tmp_path / "state.json"
    alerts_file = tmp_path / "backstory-alerts.md"
    acme_config = {"accounts": {"acme": {"keywords": ["Acme Corp"]}}}

    def _fake_initialize(self_: object) -> None:
        pass

    def _fake_close(self_: object) -> None:
        pass

    def _fake_call_tool(self_: object, tool_name: str, arguments: dict[str, object]) -> object:
        raise ValueError("non-numeric engagement_level: 'very high'")

    with (
        patch("fieldkit.watch.backstory_health.get_accounts_config", return_value=acme_config),
        patch.object(wbh, "_state_file", return_value=state_file),
        patch.object(wbh, "get_watchers_dir", return_value=tmp_path),
        patch.object(wbh, "_alerts_file", return_value=alerts_file),
        patch.object(wbh.MCPSession, "initialize", _fake_initialize),
        patch.object(wbh.MCPSession, "close", _fake_close),
        patch.object(wbh.MCPSession, "call_tool", _fake_call_tool),
    ):
        rc = wbh._run_backstory_health(threshold=60, account=None, dry_run=False)

    # Exit code must be 0 (not a fatal crash)
    assert rc == 0


# ---------------------------------------------------------------------------
# historic regression: internal:true accounts must be skipped in the accounts loop
# ---------------------------------------------------------------------------


# ── TestBug213InternalAccountSkip (flattened) ─────────────────────────────────────────────


def _make_patches_bug213_internal_account_skip(
    tmp_path: Path,
    accounts_cfg: dict[str, object],
) -> tuple[object, ...]:
    """Return context-manager patches for a _run_backstory_health call."""
    state_file = tmp_path / "state.json"
    alerts_file = tmp_path / "backstory-alerts.md"

    def _fake_initialize(self_: object) -> None:
        pass

    def _fake_close(self_: object) -> None:
        pass

    return (
        patch("fieldkit.watch.backstory_health.get_accounts_config", return_value=accounts_cfg),
        patch.object(wbh, "_state_file", return_value=state_file),
        patch.object(wbh, "get_watchers_dir", return_value=tmp_path),
        patch.object(wbh, "_alerts_file", return_value=alerts_file),
        patch.object(wbh.MCPSession, "initialize", _fake_initialize),
        patch.object(wbh.MCPSession, "close", _fake_close),
    )


def test_internal_account_is_not_checked(tmp_path: Path) -> None:
    """An account with internal:true must never call check_account."""
    cfg = {
        "accounts": {
            "internal-example": {"keywords": ["Example Vendor"], "internal": True},
        }
    }
    patches = _make_patches_bug213_internal_account_skip(tmp_path, cfg)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patch.object(wbh, "check_account") as mock_check,
    ):
        rc = wbh._run_backstory_health(threshold=60, account=None, dry_run=False)

    mock_check.assert_not_called()
    assert rc == 0


def test_internal_false_account_is_checked(tmp_path: Path) -> None:
    """An account with internal:false (or no internal key) must still be checked."""
    cfg = {
        "accounts": {
            "acme": {"keywords": ["Acme Corp"], "internal": False},
        }
    }
    patches = _make_patches_bug213_internal_account_skip(tmp_path, cfg)
    fake_result = {"health_score": 80.0, "account_key": "acme"}
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patch.object(wbh, "check_account", return_value=fake_result) as mock_check,
    ):
        rc = wbh._run_backstory_health(threshold=60, account=None, dry_run=False)

    assert rc == 0
    mock_check.assert_called_once()


def test_mixed_accounts_only_non_internal_checked(tmp_path: Path) -> None:
    """With one internal and one external account, only the external one is checked."""
    cfg = {
        "accounts": {
            "internal-example": {"keywords": ["Example Vendor"], "internal": True},
            "acme": {"keywords": ["Acme Corp"]},
        }
    }
    patches = _make_patches_bug213_internal_account_skip(tmp_path, cfg)
    fake_result = {"health_score": 70.0, "account_key": "acme"}
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patch.object(wbh, "check_account", return_value=fake_result) as mock_check,
    ):
        rc = wbh._run_backstory_health(threshold=60, account=None, dry_run=False)

    assert rc == 0
    # Only the non-internal account should have been checked
    assert mock_check.call_count == 1
    call_kwargs = mock_check.call_args.kwargs
    assert call_kwargs["account_key"] == "acme"


# ---------------------------------------------------------------------------
# historic regression: suppression threshold — suppress when abs(delta) < 1.0
# ---------------------------------------------------------------------------


# ── TestBug089SuppressionThreshold (flattened) ─────────────────────────────────────────────


def _make_session_bug089_suppression_threshold(current_score: float) -> MagicMock:
    session = MagicMock(spec=wbh.MCPSession)
    session.call_tool.return_value = {
        "peopleai_account_id": 1,
        "opportunities": [{"engagement_level": current_score}],
    }
    return session


def test_delta_zero_suppressed(tmp_path: Path) -> None:
    """Delta = 0.0 is always suppressed (score unchanged)."""
    alerts_file = tmp_path / "backstory-alerts.md"
    session = _make_session_bug089_suppression_threshold(50.0)  # below threshold=60
    prior_state = {"health_score": 50.0}

    with (
        patch.object(wbh, "_alerts_file", return_value=alerts_file),
        patch.object(wbh, "get_watchers_dir", return_value=tmp_path),
    ):
        result = wbh.check_account(
            session=session,
            account_key="acme",
            account_cfg={"keywords": ["Acme"]},
            state={"acme": prior_state},
            default_threshold=60,
            dry_run=False,
        )

    assert result is not None
    assert not alerts_file.exists(), "No alert should be written when delta=0.0"


def test_delta_half_fires_alert(tmp_path: Path) -> None:
    """historic regression: Delta = 0.5 now fires an alert (threshold tightened to 0.01).

    Previously (< 1.0 threshold) this was suppressed, hiding genuine 1-point
    score changes. After the fix, any delta >= 0.01 triggers an alert.
    """
    alerts_file = tmp_path / "backstory-alerts.md"
    watchers_dir = tmp_path
    prior_state = {"health_score": 50.0}

    session = MagicMock(spec=wbh.MCPSession)
    session.call_tool.side_effect = [
        {"peopleai_account_id": 1, "opportunities": [{"engagement_level": 50.5}]},
        "risk narrative",
    ]

    with (
        patch.object(wbh, "_alerts_file", return_value=alerts_file),
        patch.object(wbh, "get_watchers_dir", return_value=watchers_dir),
    ):
        wbh.check_account(
            session=session,
            account_key="acme",
            account_cfg={"keywords": ["Acme"]},
            state={"acme": prior_state},
            default_threshold=60,
            dry_run=False,
        )

    assert alerts_file.exists(), "Delta=0.5 should fire an alert with 0.01 threshold (historic regression)"


def test_delta_below_float_noise_suppressed(tmp_path: Path) -> None:
    """historic regression: Delta < 0.01 (float noise) is still suppressed."""
    alerts_file = tmp_path / "backstory-alerts.md"
    # previous=50.0, current=50.005 → delta=0.005 < 0.01 → suppressed
    session = _make_session_bug089_suppression_threshold(50.005)
    prior_state = {"health_score": 50.0}

    with (
        patch.object(wbh, "_alerts_file", return_value=alerts_file),
        patch.object(wbh, "get_watchers_dir", return_value=tmp_path),
    ):
        wbh.check_account(
            session=session,
            account_key="acme",
            account_cfg={"keywords": ["Acme"]},
            state={"acme": prior_state},
            default_threshold=60,
            dry_run=False,
        )

    assert not alerts_file.exists(), "Delta < 0.01 should be suppressed (float noise)"


def test_delta_one_fires_alert(tmp_path: Path) -> None:
    """Delta = 1.0 (>= 1.0) fires an alert — meaningful score change."""
    alerts_file = tmp_path / "backstory-alerts.md"
    watchers_dir = tmp_path
    session = _make_session_bug089_suppression_threshold(49.0)  # previous=50.0, delta=-1.0
    prior_state = {"health_score": 50.0}

    session.call_tool.side_effect = [
        {"peopleai_account_id": 1, "opportunities": [{"engagement_level": 49.0}]},
        "risk narrative",  # get_account_status response
    ]

    with (
        patch.object(wbh, "_alerts_file", return_value=alerts_file),
        patch.object(wbh, "get_watchers_dir", return_value=watchers_dir),
    ):
        wbh.check_account(
            session=session,
            account_key="acme",
            account_cfg={"keywords": ["Acme"]},
            state={"acme": prior_state},
            default_threshold=60,
            dry_run=False,
        )

    assert alerts_file.exists(), "Delta=1.0 should fire an alert"
    content = alerts_file.read_text()
    assert "acme" in content


def test_delta_large_fires_alert(tmp_path: Path) -> None:
    """Delta = 5.0 fires an alert."""
    alerts_file = tmp_path / "backstory-alerts.md"
    watchers_dir = tmp_path
    prior_state = {"health_score": 55.0}

    session = MagicMock(spec=wbh.MCPSession)
    session.call_tool.side_effect = [
        {"peopleai_account_id": 1, "opportunities": [{"engagement_level": 50.0}]},
        "risks: none",
    ]

    with (
        patch.object(wbh, "_alerts_file", return_value=alerts_file),
        patch.object(wbh, "get_watchers_dir", return_value=watchers_dir),
    ):
        wbh.check_account(
            session=session,
            account_key="acme",
            account_cfg={"keywords": ["Acme"]},
            state={"acme": prior_state},
            default_threshold=60,
            dry_run=False,
        )

    assert alerts_file.exists(), "Delta=5.0 should fire an alert"


def test_no_prior_state_always_fires(tmp_path: Path) -> None:
    """First detection (no prior state) always fires regardless of score."""
    alerts_file = tmp_path / "backstory-alerts.md"
    watchers_dir = tmp_path

    session = MagicMock(spec=wbh.MCPSession)
    session.call_tool.side_effect = [
        {"peopleai_account_id": 1, "opportunities": [{"engagement_level": 50.0}]},
        "no risks",
    ]

    with (
        patch.object(wbh, "_alerts_file", return_value=alerts_file),
        patch.object(wbh, "get_watchers_dir", return_value=watchers_dir),
    ):
        wbh.check_account(
            session=session,
            account_key="new-account",
            account_cfg={"keywords": ["New"]},
            state={},  # no prior state
            default_threshold=60,
            dry_run=False,
        )

    assert alerts_file.exists(), "First detection must always fire"


# ---------------------------------------------------------------------------
# Task 11.5 — _run_backstory_health handles empty accounts config
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_backstory_health_handles_empty_accounts(tmp_path: Path) -> None:
    """_run_backstory_health returns exit code 1 when accounts config is empty."""
    from unittest.mock import patch

    from fieldkit.watch.backstory_health import _run_backstory_health

    # Patch load_accounts_config to return a config with no accounts
    empty_config: dict[str, object] = {"accounts": {}}

    with (
        patch("fieldkit.watch.backstory_health.load_accounts_config", return_value=empty_config),
        patch("fieldkit.watch.backstory_health.get_fieldkit_home", return_value=tmp_path),
        patch("fieldkit.watch.backstory_health.watcher_logging"),
    ):
        rc = _run_backstory_health(threshold=60, account=None, dry_run=True)

    # Empty accounts → exit 1 (no accounts to check is a configuration error)
    assert rc == 1


# ---------------------------------------------------------------------------
# Additional branch coverage for _run_backstory_health (cc=16)
# ---------------------------------------------------------------------------


# ── TestRunBackstoryHealthBranches (flattened) ─────────────────────────────────────────────


def _make_patches_run_backstory_health_branches(
    tmp_path: Path,
    accounts_cfg: dict[str, object],
) -> tuple[object, ...]:
    state_file = tmp_path / "state.json"
    alerts_file = tmp_path / "backstory-alerts.md"

    def _fake_initialize(self_: object) -> None:
        pass

    def _fake_close(self_: object) -> None:
        pass

    return (
        patch("fieldkit.watch.backstory_health.get_accounts_config", return_value=accounts_cfg),
        patch.object(wbh, "_state_file", return_value=state_file),
        patch.object(wbh, "get_watchers_dir", return_value=tmp_path),
        patch.object(wbh, "_alerts_file", return_value=alerts_file),
        patch.object(wbh.MCPSession, "initialize", _fake_initialize),
        patch.object(wbh.MCPSession, "close", _fake_close),
    )


def test_load_accounts_config_failure_returns_exit_1(tmp_path: Path) -> None:
    """Lines 479-480: RuntimeError from load_accounts_config → return 1."""
    state_file = tmp_path / "state.json"
    alerts_file = tmp_path / "backstory-alerts.md"

    with (
        patch(
            "fieldkit.watch.backstory_health.load_accounts_config",
            side_effect=RuntimeError("config error"),
        ),
        patch.object(wbh, "_state_file", return_value=state_file),
        patch.object(wbh, "get_watchers_dir", return_value=tmp_path),
        patch.object(wbh, "_alerts_file", return_value=alerts_file),
        patch("fieldkit.watch.backstory_health.watcher_logging"),
    ):
        rc = wbh._run_backstory_health(threshold=60, account=None, dry_run=False)

    assert rc == 1


def test_mcp_initialize_failure_returns_exit_1(tmp_path: Path) -> None:
    """Lines 500-503: MCPSession.initialize raises RuntimeError → return 1."""
    cfg = {"accounts": {"acme": {"keywords": ["Acme Corp"]}}}
    state_file = tmp_path / "state.json"
    alerts_file = tmp_path / "backstory-alerts.md"

    def _fail_initialize(self_: object) -> None:
        raise RuntimeError("cannot reach mcpjungle")

    def _fake_close(self_: object) -> None:
        pass

    with (
        patch("fieldkit.watch.backstory_health.get_accounts_config", return_value=cfg),
        patch.object(wbh, "_state_file", return_value=state_file),
        patch.object(wbh, "get_watchers_dir", return_value=tmp_path),
        patch.object(wbh, "_alerts_file", return_value=alerts_file),
        patch.object(wbh.MCPSession, "initialize", _fail_initialize),
        patch.object(wbh.MCPSession, "close", _fake_close),
    ):
        rc = wbh._run_backstory_health(threshold=60, account=None, dry_run=False)

    assert rc == 1


def test_unknown_account_filter_returns_exit_1(tmp_path: Path) -> None:
    """Line 542: account filter not in accounts → return 1."""
    cfg = {"accounts": {"acme": {"keywords": ["Acme Corp"]}}}
    patches = _make_patches_run_backstory_health_branches(tmp_path, cfg)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        rc = wbh._run_backstory_health(threshold=60, account="nonexistent", dry_run=False)

    assert rc == 1


def test_non_dict_account_cfg_is_skipped(tmp_path: Path) -> None:
    """Lines 512-514: non-dict account_cfg is skipped with warning."""
    cfg = {
        "accounts": {
            "bad-account": "not-a-dict",
            "acme": {"keywords": ["Acme Corp"]},
        }
    }
    patches = _make_patches_run_backstory_health_branches(tmp_path, cfg)
    fake_result = {"health_score": 80.0, "account_key": "acme"}
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patch.object(wbh, "check_account", return_value=fake_result) as mock_check,
    ):
        rc = wbh._run_backstory_health(threshold=60, account=None, dry_run=False)

    assert rc == 0
    # Only the valid account should be checked
    assert mock_check.call_count == 1
    assert mock_check.call_args.kwargs["account_key"] == "acme"


def test_dry_run_does_not_save_state(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Lines 548-552: dry_run=True → save_state is never called."""
    cfg = {"accounts": {"acme": {"keywords": ["Acme Corp"]}}}
    patches = _make_patches_run_backstory_health_branches(tmp_path, cfg)
    fake_result = {"health_score": 50.0, "account_key": "acme"}
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patch.object(wbh, "check_account", return_value=fake_result),
        patch.object(wbh, "save_state") as mock_save,
    ):
        rc = wbh._run_backstory_health(threshold=60, account=None, dry_run=True)

    assert rc == 0
    assert capsys.readouterr().out == "[DRY-RUN] backstory-health: 1 account(s) scanned, 0 alert(s) would fire\n"
    mock_save.assert_not_called()


def test_state_write_failure_is_fatal(tmp_path: Path) -> None:
    """A successful scan must still fail closed when its state cannot persist."""
    cfg = {"accounts": {"acme": {"keywords": ["Acme Corp"]}}}
    patches = _make_patches_run_backstory_health_branches(tmp_path, cfg)
    fake_result = {"health_score": 50.0, "account_key": "acme"}
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patch.object(wbh, "check_account", return_value=fake_result),
        patch.object(wbh, "save_state", side_effect=OSError("disk full")),
        patch.object(wbh, "write_run_status") as write_status,
    ):
        rc = wbh._run_backstory_health(threshold=60, account=None, dry_run=False)

    assert rc == 1
    assert write_status.call_args.kwargs["outcome"] == "fatal"
    assert write_status.call_args.kwargs["failures"] == 1


# ---------------------------------------------------------------------------
# Additional coverage for find_account_id and account_threshold helpers
# ---------------------------------------------------------------------------


# ── TestFindAccountId (flattened) ─────────────────────────────────────────────


def test_find_account_id_returns_account_id_on_success() -> None:
    session = MagicMock(spec=wbh.MCPSession)
    session.call_tool.return_value = {"peopleai_account_id": 42}
    result = wbh.find_account_id(session, "Acme Corp")
    assert result == 42


def test_find_account_id_returns_none_on_runtime_error() -> None:
    session = MagicMock(spec=wbh.MCPSession)
    session.call_tool.side_effect = RuntimeError("MCP error")
    result = wbh.find_account_id(session, "Acme Corp")
    assert result is None


def test_find_account_id_returns_none_for_non_dict_response() -> None:
    session = MagicMock(spec=wbh.MCPSession)
    session.call_tool.return_value = "unexpected string"
    result = wbh.find_account_id(session, "Acme Corp")
    assert result is None


def test_find_account_id_returns_none_when_id_missing() -> None:
    session = MagicMock(spec=wbh.MCPSession)
    session.call_tool.return_value = {"opportunities": []}
    result = wbh.find_account_id(session, "Acme Corp")
    assert result is None


def test_find_account_id_returns_none_when_id_is_string() -> None:
    """peopleai_account_id must be int; string is rejected."""
    session = MagicMock(spec=wbh.MCPSession)
    session.call_tool.return_value = {"peopleai_account_id": "not-an-int"}
    result = wbh.find_account_id(session, "Acme Corp")
    assert result is None


# ── TestAccountThreshold (flattened) ─────────────────────────────────────────────


def test_account_threshold_returns_configured_threshold() -> None:
    assert wbh.account_threshold({"health_score_threshold": 70}, 60) == 70


def test_account_threshold_falls_back_to_default_when_missing() -> None:
    assert wbh.account_threshold({}, 60) == 60


def test_account_threshold_falls_back_to_default_on_type_error() -> None:
    assert wbh.account_threshold({"health_score_threshold": "not-a-number"}, 60) == 60


def test_account_threshold_falls_back_to_default_on_none() -> None:
    assert wbh.account_threshold({"health_score_threshold": None}, 60) == 60


# ── TestGetRiskCountNonString (flattened) ─────────────────────────────────────────────


def test_non_string_response_returns_minus_one() -> None:
    session = MagicMock(spec=wbh.MCPSession)
    session.call_tool.return_value = {"some": "dict"}  # not a string
    count = wbh.get_risk_count(session, account_id=1)
    assert count == -1


def test_risk_count_stops_at_new_section() -> None:
    """Risk counting stops when a non-indented line follows the risk bullets."""
    session = MagicMock(spec=wbh.MCPSession)
    session.call_tool.return_value = "Risks:\n - risk one\n - risk two\nNext Steps:\n - not a risk\n"
    count = wbh.get_risk_count(session, account_id=1)
    assert count == 2


# ── TestAppendAlertDedup (flattened) ─────────────────────────────────────────────


def test_duplicate_alert_not_written(tmp_path: Path) -> None:
    """Second append_alert call with same date+account key is skipped."""
    import datetime as dt

    alerts_file = tmp_path / "backstory-alerts.md"
    date_label = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")
    account_key = "acme"
    # Pre-populate with a heading that matches what append_alert would write
    alerts_file.write_text(
        "# Backstory Health Alerts\n\n"
        f"## {date_label} — {account_key} health alert\n\n"
        f"- **Account:** `{account_key}`\n",
        encoding="utf-8",
    )

    with (
        patch.object(wbh, "_alerts_file", return_value=alerts_file),
        patch.object(wbh, "get_watchers_dir", return_value=tmp_path),
    ):
        size_before = alerts_file.stat().st_size
        wbh.append_alert(
            account_key=account_key,
            current_score=40.0,
            previous_score=None,
            threshold=60,
            risk_count=0,
            dry_run=False,
        )
        size_after = alerts_file.stat().st_size

    assert size_after == size_before, "Duplicate alert must not grow the file"


# ---------------------------------------------------------------------------
# Additional coverage: get_engagement_score, load_accounts_config, save_state
# ---------------------------------------------------------------------------


# ── TestBackstoryHealthAdditionalBranches (flattened) ─────────────────────────────────────────────


def test_get_engagement_score_returns_none() -> None:
    """Lines 114-125: get_engagement_score always returns None (sentinel)."""
    session = MagicMock()
    result = wbh.get_engagement_score(session, account_id=1)
    assert result is None


def test_load_accounts_config_raises_on_failure() -> None:
    """Lines 193-195: load_accounts_config wraps exception in RuntimeError."""
    import re

    with (
        patch("fieldkit.watch.backstory_health.get_accounts_config", side_effect=Exception("yaml error")),
        pytest.raises(RuntimeError, match=re.escape("accounts.yaml load failed")),
    ):
        wbh.load_accounts_config()


def test_save_state_oserror_propagates(tmp_path: Path) -> None:
    """save_state propagates storage failures from the shared persistence helper."""
    state_file = tmp_path / "state.json"
    with (
        patch.object(wbh, "_state_file", return_value=state_file),
        patch.object(wbh, "merge_state", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full") as exc_info,
    ):
        wbh.save_state({"key": "value"})
    assert exc_info.type is OSError


def test_run_backstory_health_account_filter_scopes_to_one(tmp_path: Path) -> None:
    """Line 491: account filter narrows accounts dict to single entry."""
    cfg = {
        "accounts": {
            "acme": {"keywords": ["Acme Corp"]},
            "globalpay": {"keywords": ["GlobalPay"]},
        }
    }
    state_file = tmp_path / "state.json"
    alerts_file = tmp_path / "backstory-alerts.md"
    fake_result = {"health_score": 80.0, "account_key": "acme"}

    def _fake_initialize(self_: object) -> None:
        pass

    def _fake_close(self_: object) -> None:
        pass

    with (
        patch("fieldkit.watch.backstory_health.get_accounts_config", return_value=cfg),
        patch.object(wbh, "_state_file", return_value=state_file),
        patch.object(wbh, "get_watchers_dir", return_value=tmp_path),
        patch.object(wbh, "_alerts_file", return_value=alerts_file),
        patch.object(wbh.MCPSession, "initialize", _fake_initialize),
        patch.object(wbh.MCPSession, "close", _fake_close),
        patch.object(wbh, "check_account", return_value=fake_result) as mock_check,
    ):
        rc = wbh._run_backstory_health(threshold=60, account="acme", dry_run=False)

    assert rc == 0
    # Only acme should be checked
    assert mock_check.call_count == 1
    assert mock_check.call_args.kwargs["account_key"] == "acme"
