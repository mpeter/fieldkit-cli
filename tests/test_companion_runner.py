"""Tests for fieldkit.companion.runner — gate + execute + journal in one call."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fieldkit.companion.runner import EXIT_DENIED, run_action


def _read_journal(data_path: Path) -> list[dict[str, object]]:
    lines: list[dict[str, object]] = []
    for path in sorted(data_path.glob("companion-journal-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            assert isinstance(record, dict)
            lines.append(record)
    return lines


pytestmark = pytest.mark.unit


def test_run_action_denied_never_spawns_subprocess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spawn = MagicMock()
    monkeypatch.setattr("fieldkit.companion.runner.subprocess.run", spawn)

    result = run_action(
        ["sf", "set-next-steps", "006XX", "text"],
        tier="read",
        allowlist=[],
        data_path=tmp_path,
        item_id="item-1",
    )
    assert result.denied is True
    assert result.exit_code == EXIT_DENIED
    spawn.assert_not_called()

    records = _read_journal(tmp_path)
    assert len(records) == 1
    assert records[0]["item_id"] == "item-1"
    assert records[0]["action"] == "sf set-next-steps 006XX text"
    assert records[0]["exit_code"] == EXIT_DENIED


def test_run_action_permitted_executes_and_journals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok\n", stderr="")
    spawn = MagicMock(return_value=completed)
    monkeypatch.setattr("fieldkit.companion.runner.subprocess.run", spawn)

    result = run_action(
        ["pursuit", "health", "--json"],
        tier="read",
        allowlist=[],
        data_path=tmp_path,
        item_id="item-2",
    )
    assert result.denied is False
    assert result.exit_code == 0
    assert result.stdout == "ok\n"
    spawn.assert_called_once()
    # the invoked argv ends with our command tokens (prefixed by the fieldkit binary)
    invoked = spawn.call_args.args[0]
    assert invoked[-3:] == ["pursuit", "health", "--json"]

    records = _read_journal(tmp_path)
    assert records[0] == {
        "item_id": "item-2",
        "action": "pursuit health --json",
        "exit_code": 0,
        "timestamp": records[0]["timestamp"],
    }


def test_run_action_propagates_nonzero_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    completed = subprocess.CompletedProcess(args=[], returncode=2, stdout="", stderr="auth expired\n")
    monkeypatch.setattr("fieldkit.companion.runner.subprocess.run", MagicMock(return_value=completed))

    result = run_action(["pursuit", "health"], tier="read", allowlist=[], data_path=tmp_path)
    assert result.exit_code == 2
    assert result.stderr == "auth expired\n"
    assert _read_journal(tmp_path)[0]["exit_code"] == 2


def test_run_action_timeout_journals_exit_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "fieldkit.companion.runner.subprocess.run",
        MagicMock(side_effect=subprocess.TimeoutExpired(cmd="fieldkit", timeout=120)),
    )

    result = run_action(["pursuit", "health"], tier="read", allowlist=[], data_path=tmp_path)
    assert result.exit_code == 1
    assert "timed out" in result.stderr
    assert _read_journal(tmp_path)[0]["exit_code"] == 1


def test_run_action_act_tier_allowlisted_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    spawn = MagicMock(return_value=completed)
    monkeypatch.setattr("fieldkit.companion.runner.subprocess.run", spawn)

    result = run_action(
        ["pursuit", "advance", "acme/deal", "--dry-run"],
        tier="act",
        allowlist=["pursuit advance --dry-run"],
        data_path=tmp_path,
        item_id="item-3",
    )
    assert result.denied is False
    spawn.assert_called_once()
