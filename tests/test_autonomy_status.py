"""Tests for the read-only autonomy status snapshot and CLI."""

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from fieldkit.autonomy.status import build_status
from fieldkit.commands.autonomy.cli import _render_status

pytestmark = pytest.mark.unit


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _object(payload: dict[str, object], key: str) -> dict[str, object]:
    value = payload[key]
    assert isinstance(value, dict)
    return value


def test_missing_health_state_is_explicit_and_never_healthy(tmp_path: Path) -> None:
    snapshot = build_status(tmp_path, now=datetime(2026, 9, 15, tzinfo=UTC), spend_reader=lambda: 0.0)

    payload = snapshot.to_dict()

    assert _object(payload, "health")["state"] == "missing"
    assert _object(payload, "next_action")["kind"] == "health-record-missing"


def test_malformed_health_takes_precedence_over_every_other_observation(tmp_path: Path) -> None:
    health_path = tmp_path / "logs" / "health" / "health-run-status.json"
    health_path.parent.mkdir(parents=True, exist_ok=True)
    health_path.write_text("not-json", encoding="utf-8")

    snapshot = build_status(tmp_path, now=datetime(2026, 9, 15, tzinfo=UTC), spend_reader=lambda: 0.0)

    assert _object(snapshot.to_dict(), "next_action")["kind"] == "health-state-malformed"


def test_stale_health_precedes_a_failed_driver_run(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "logs" / "health" / "health-run-status.json",
        {"runs": [{"ts": "2026-09-13T00:00:00Z", "outcome": "ok", "gate_failures": []}]},
    )
    _write_json(
        tmp_path / "logs" / "driver" / "driver-run-status.json",
        {"runs": [{"ts": "2026-09-14T23:00:00Z", "outcome": "failed", "error": "worktree creation failed"}]},
    )

    snapshot = build_status(tmp_path, now=datetime(2026, 9, 15, 12, tzinfo=UTC), spend_reader=lambda: 1.25)

    payload = snapshot.to_dict()

    assert _object(payload, "health")["state"] == "stale"
    assert _object(payload, "driver")["state"] == "available"
    assert _object(payload, "next_action")["kind"] == "health-stale"


def test_failed_driver_is_the_next_action_when_health_is_current(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "logs" / "health" / "health-run-status.json",
        {"runs": [{"ts": "2026-09-15T00:00:00Z", "outcome": "ok", "gate_failures": []}]},
    )
    _write_json(
        tmp_path / "logs" / "driver" / "driver-run-status.json",
        {"runs": [{"ts": "2026-09-15T01:00:00Z", "outcome": "failed", "error": "worktree creation failed"}]},
    )

    snapshot = build_status(tmp_path, now=datetime(2026, 9, 15, 12, tzinfo=UTC), spend_reader=lambda: 1.25)

    assert _object(snapshot.to_dict(), "next_action")["kind"] == "driver-failed"


def test_denied_admission_is_the_next_action_when_other_evidence_is_current(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "logs" / "health" / "health-run-status.json",
        {"runs": [{"ts": "2026-09-15T00:00:00Z", "outcome": "ok", "gate_failures": []}]},
    )
    _write_json(
        tmp_path / "driver" / "developer-admission.json",
        {"decisions": [{"ts": "2026-09-15T01:00:00Z", "allowed": False, "reason_code": "daily-cap"}]},
    )

    snapshot = build_status(tmp_path, now=datetime(2026, 9, 15, 12, tzinfo=UTC), spend_reader=lambda: 1.25)

    assert _object(snapshot.to_dict(), "next_action")["kind"] == "admission-denied"


def test_malformed_admission_is_the_next_action_when_health_is_current(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "logs" / "health" / "health-run-status.json",
        {"runs": [{"ts": "2026-09-15T00:00:00Z", "outcome": "ok", "gate_failures": []}]},
    )
    admission_path = tmp_path / "driver" / "developer-admission.json"
    admission_path.parent.mkdir(parents=True, exist_ok=True)
    admission_path.write_text("not-json", encoding="utf-8")

    snapshot = build_status(tmp_path, now=datetime(2026, 9, 15, 12, tzinfo=UTC), spend_reader=lambda: 1.25)

    assert _object(snapshot.to_dict(), "next_action")["kind"] == "admission-state-malformed"


def test_malformed_driver_state_remains_explicit_while_other_sources_render(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "logs" / "health" / "health-run-status.json",
        {"runs": [{"ts": "2026-09-15T00:00:00Z", "outcome": "ok", "gate_failures": []}]},
    )
    driver_path = tmp_path / "logs" / "driver" / "driver-run-status.json"
    driver_path.parent.mkdir(parents=True, exist_ok=True)
    driver_path.write_text("not-json", encoding="utf-8")

    snapshot = build_status(tmp_path, now=datetime(2026, 9, 15, 12, tzinfo=UTC), spend_reader=lambda: None)

    payload = snapshot.to_dict()

    assert _object(payload, "health")["state"] == "available"
    assert _object(payload, "driver")["state"] == "malformed"
    spend = _object(payload, "spend")
    assert spend["state"] == "unavailable"
    assert "usd" not in spend
    assert _object(payload, "next_action")["kind"] == "driver-state-malformed"


def test_current_health_regressions_are_the_next_action(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "logs" / "health" / "health-run-status.json",
        {"runs": [{"ts": "2026-09-15T00:00:00Z", "outcome": "failed", "gate_failures": ["quality"]}]},
    )

    snapshot = build_status(tmp_path, now=datetime(2026, 9, 15, 12, tzinfo=UTC), spend_reader=lambda: 1.25)

    assert _object(snapshot.to_dict(), "next_action")["kind"] == "health-regressions-open"


def test_unavailable_spend_is_the_next_action_after_current_evidence(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "logs" / "health" / "health-run-status.json",
        {"runs": [{"ts": "2026-09-15T00:00:00Z", "outcome": "ok", "gate_failures": []}]},
    )

    snapshot = build_status(tmp_path, now=datetime(2026, 9, 15, 12, tzinfo=UTC), spend_reader=lambda: None)

    assert _object(snapshot.to_dict(), "next_action")["kind"] == "spend-unavailable"


def test_current_available_observations_need_no_immediate_action(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "logs" / "health" / "health-run-status.json",
        {"runs": [{"ts": "2026-09-15T00:00:00Z", "outcome": "ok", "gate_failures": []}]},
    )

    snapshot = build_status(tmp_path, now=datetime(2026, 9, 15, 12, tzinfo=UTC), spend_reader=lambda: 1.25)

    assert _object(snapshot.to_dict(), "next_action")["kind"] == "none"


def test_render_status_writes_human_readable_snapshot(tmp_path: Path) -> None:
    snapshot = build_status(tmp_path, now=datetime(2026, 9, 15, tzinfo=UTC), spend_reader=lambda: 0.0)

    with patch("fieldkit.commands.autonomy.cli.click.echo") as echo:
        _render_status(snapshot)

    rendered = [call.args[0] for call in echo.call_args_list]
    assert rendered == [
        "Autonomy status",
        "  health: missing — health record does not exist",
        "  driver: missing — driver record does not exist",
        "  admission: missing — developer admission record does not exist",
        "  spend: available — measured developer spend for the current UTC day",
        "Next action: health-record-missing — No health evidence is available",
    ]
