"""Tests for the global developer-schedule admission gate."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from fieldkit.commands.driver.cli import cli
from fieldkit.driver import admission, spend

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _admission_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admission, "get_fieldkit_data", lambda: tmp_path)
    monkeypatch.delenv("CLAUDE_CODE_USE_VERTEX", raising=False)
    monkeypatch.delenv("FIELDKIT_DEVELOPER_DAILY_RUN_LIMIT", raising=False)
    monkeypatch.delenv("FIELDKIT_DEVELOPER_SPEND_CAP", raising=False)


def _configure(monkeypatch: pytest.MonkeyPatch, *, limit: str = "1", cap: str = "5.00") -> None:
    monkeypatch.setenv("FIELDKIT_DEVELOPER_DAILY_RUN_LIMIT", limit)
    monkeypatch.setenv("FIELDKIT_DEVELOPER_SPEND_CAP", cap)


def test_admit_fails_closed_when_run_budget_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIELDKIT_DEVELOPER_SPEND_CAP", "5.00")

    decision = admission.admit("driver")

    assert decision.allowed is False
    assert decision.reason_code == "daily-run-limit-unset"


def test_admit_records_lease_and_blocks_other_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(spend, "get_daily_developer_spend_total", lambda: 0.0)

    first = admission.admit("driver")
    second = admission.admit("proctor")

    assert first.allowed is True
    assert second.allowed is False
    assert second.reason_code == "lease-held"
    ledger = json.loads((admission._ledger_path()).read_text(encoding="utf-8"))
    assert [entry["reason_code"] for entry in ledger["decisions"]] == ["admitted", "lease-held"]


def test_release_allows_next_job_until_daily_run_budget_is_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, limit="2")
    monkeypatch.setattr(spend, "get_daily_developer_spend_total", lambda: 0.0)

    assert admission.admit("driver").allowed is True
    assert admission.release("driver").allowed is True
    assert admission.admit("proctor").allowed is True
    assert admission.release("proctor").allowed is True

    denied = admission.admit("assayer")

    assert denied.allowed is False
    assert denied.reason_code == "daily-run-limit-reached"


def test_admission_run_budget_resets_on_a_new_utc_day(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(spend, "get_daily_developer_spend_total", lambda: 0.0)
    monkeypatch.setattr(admission, "_timestamp", lambda: "2026-08-16T23:59:00Z")

    assert admission.admit("driver").allowed is True
    assert admission.release("driver").allowed is True

    monkeypatch.setattr(admission, "_timestamp", lambda: "2026-08-17T00:01:00Z")

    assert admission.admit("proctor").allowed is True


def test_admit_reaps_a_stale_lease_and_records_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(spend, "get_daily_developer_spend_total", lambda: 0.0)
    stale_at = (datetime.now(UTC) - admission._MAX_LEASE_AGE - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ledger = admission._ledger_path()
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({"active": {"job": "driver", "started_at": stale_at}}), encoding="utf-8")

    decision = admission.admit("proctor")

    assert decision.allowed is True
    saved = json.loads(ledger.read_text(encoding="utf-8"))
    assert [entry["reason_code"] for entry in saved["decisions"]] == ["stale-lease-reaped", "admitted"]


def test_admit_rejects_vertex_contamination(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")

    decision = admission.admit("driver")

    assert decision.allowed is False
    assert decision.reason_code == "vertex-env-present"


def test_admit_rejects_unreadable_or_exhausted_spend(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(spend, "get_daily_developer_spend_total", lambda: None)

    unreadable = admission.admit("driver")

    monkeypatch.setattr(spend, "get_daily_developer_spend_total", lambda: 5.0)
    exhausted = admission.admit("driver")

    assert unreadable.reason_code == "spend-unreadable"
    assert exhausted.reason_code == "spend-cap-reached"


def test_admit_cli_emits_machine_readable_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(spend, "get_daily_developer_spend_total", lambda: 0.0)

    result = CliRunner().invoke(cli, ["admit", "--job", "driver", "--json"])

    assert result.exit_code == 0
    assert '"reason_code": "admitted"' in result.output


def test_admit_cli_returns_nonzero_for_denial(monkeypatch: pytest.MonkeyPatch) -> None:
    result = CliRunner().invoke(cli, ["admit", "--job", "driver", "--json"])

    assert result.exit_code == 1
    assert '"reason_code": "daily-run-limit-unset"' in result.output
