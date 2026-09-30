"""Tests for fieldkit.companion.runner — gate + execute + journal in one call."""

import json
import shlex
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from fieldkit.commands.companion.cli import _act_policy_validation
from fieldkit.companion.gate import NO_ACT_POLICY
from fieldkit.companion.runner import EXIT_DENIED, ActionResult, run_action

_CONFIGURED_ALLOWLIST: list[str] = []


@pytest.fixture(autouse=True)
def _configured_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    _CONFIGURED_ALLOWLIST.clear()
    monkeypatch.setattr("fieldkit.config.get_companion_tier", lambda: "act")
    monkeypatch.setattr("fieldkit.config.get_companion_act_allowlist", lambda: list(_CONFIGURED_ALLOWLIST))


def _preview_policy(entry: str):
    _CONFIGURED_ALLOWLIST[:] = [entry]
    validation = _act_policy_validation("act", [entry])
    assert validation.policy is not None
    return validation.policy


def _read_journal(data_path: Path) -> list[dict[str, object]]:
    lines: list[dict[str, object]] = []
    for path in sorted(data_path.glob("companion-journal-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            assert isinstance(record, dict)
            lines.append(record)
    return lines


pytestmark = pytest.mark.unit


def test_journal_action_preserves_argument_boundaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spawn = MagicMock(return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""))
    monkeypatch.setattr("fieldkit.companion.runner.subprocess.run", spawn)
    argv = ["contact", "find", "Ada Lovelace --json"]

    result = run_action(argv, tier="read", policy=NO_ACT_POLICY, data_path=tmp_path)

    assert result.exit_code == 0
    action = _read_journal(tmp_path)[0]["action"]
    assert isinstance(action, str)
    assert shlex.split(action) == argv


def test_run_action_uses_current_installation_not_path_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/untrusted/fieldkit")
    spawn = MagicMock(return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""))
    monkeypatch.setattr("fieldkit.companion.runner.subprocess.run", spawn)

    result = run_action(["version"], tier="read", policy=NO_ACT_POLICY, data_path=tmp_path)

    assert result.exit_code == 0
    assert spawn.call_args.args[0] == [sys.executable, "-I", "-m", "fieldkit", "version"]


@pytest.mark.parametrize("shadow_source", ["cwd", "pythonpath"])
def test_run_action_ignores_workspace_and_pythonpath_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shadow_source: str
) -> None:
    package = tmp_path / "shadow" / "fieldkit"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text('print("UNTRUSTED-MODULE")\n', encoding="utf-8")
    if shadow_source == "cwd":
        monkeypatch.chdir(package.parent)
    else:
        monkeypatch.setenv("PYTHONPATH", str(package.parent))

    result = run_action(["version"], tier="read", policy=NO_ACT_POLICY, data_path=tmp_path, journal=False)

    assert result.exit_code == 0
    assert "fieldkit" in result.stdout
    assert "UNTRUSTED-MODULE" not in result.stdout


def test_run_action_denied_never_spawns_subprocess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spawn = MagicMock()
    monkeypatch.setattr("fieldkit.companion.runner.subprocess.run", spawn)

    result = run_action(
        ["sf", "set-next-steps", "006XX", "text"],
        tier="read",
        policy=NO_ACT_POLICY,
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
        policy=NO_ACT_POLICY,
        data_path=tmp_path,
        item_id="item-2",
    )
    assert result.denied is False
    assert result.exit_code == 0
    assert result.stdout == "ok\n"
    spawn.assert_called_once()
    # The isolated interpreter is followed by the approved command tokens.
    invoked = spawn.call_args.args[0]
    assert invoked[-3:] == ["pursuit", "health", "--json"]

    records = _read_journal(tmp_path)
    assert records[0] == {
        "item_id": "item-2",
        "action": "pursuit health --json",
        "exit_code": 0,
        "timestamp": records[0]["timestamp"],
    }


@pytest.mark.parametrize(
    ("preview", "confirmed"),
    [
        (
            "gtask create 'Review forecast' --section today --dry-run",
            ["gtask", "create", "Review forecast", "--section", "today", "--confirm"],
        ),
        ("gtask complete task-1 --dry-run", ["gtask", "complete", "task-1", "--confirm"]),
    ],
)
def test_confirmed_task_mutation_denied_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preview: str, confirmed: list[str]
) -> None:
    spawn = MagicMock()
    monkeypatch.setattr("fieldkit.companion.runner.subprocess.run", spawn)

    result = run_action(
        confirmed,
        tier="act",
        policy=_preview_policy(preview),
        data_path=tmp_path,
        item_id="task-preview",
    )

    assert result.exit_code == EXIT_DENIED
    assert result.denied is True
    spawn.assert_not_called()
    records = _read_journal(tmp_path)
    assert len(records) == 1
    assert shlex.split(str(records[0]["action"])) == confirmed
    assert records[0]["exit_code"] == EXIT_DENIED


def test_run_action_propagates_nonzero_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    completed = subprocess.CompletedProcess(args=[], returncode=2, stdout="", stderr="auth expired\n")
    monkeypatch.setattr("fieldkit.companion.runner.subprocess.run", MagicMock(return_value=completed))

    result = run_action(["pursuit", "health"], tier="read", policy=NO_ACT_POLICY, data_path=tmp_path)
    assert result.exit_code == 2
    assert result.stderr == "auth expired\n"
    assert _read_journal(tmp_path)[0]["exit_code"] == 2


def test_run_action_timeout_journals_exit_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "fieldkit.companion.runner.subprocess.run",
        MagicMock(side_effect=subprocess.TimeoutExpired(cmd="fieldkit", timeout=120)),
    )

    result = run_action(["pursuit", "health"], tier="read", policy=NO_ACT_POLICY, data_path=tmp_path)
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
        policy=_preview_policy("pursuit advance acme/deal --dry-run"),
        data_path=tmp_path,
        item_id="item-3",
    )
    assert result.denied is False
    spawn.assert_called_once()


def test_run_action_rejects_unvalidated_raw_allowlist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spawn = MagicMock()
    monkeypatch.setattr("fieldkit.companion.runner.subprocess.run", spawn)
    raw_policy: Any = ["watch run pursuit-stalls --dry-run"]

    result = run_action(
        ["watch", "run", "pursuit-stalls", "--dry-run"],
        tier="act",
        policy=raw_policy,
        data_path=tmp_path,
    )

    assert result.denied is True
    assert result.exit_code == EXIT_DENIED
    spawn.assert_not_called()


def test_run_action_rejects_copied_policy_before_journal_or_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn = MagicMock()
    monkeypatch.setattr("fieldkit.companion.runner.subprocess.run", spawn)
    forged = replace(
        NO_ACT_POLICY,
        _entries=frozenset({("watch", "run", "pursuit-stalls", "--dry-run")}),
    )

    result = run_action(
        ["watch", "run", "pursuit-stalls", "--dry-run"],
        tier="act",
        policy=forged,
        data_path=tmp_path,
    )

    assert result == ActionResult(
        argv=["watch", "run", "pursuit-stalls", "--dry-run"],
        exit_code=EXIT_DENIED,
        denied=True,
        stdout="",
        stderr="invalid companion action authority",
    )
    assert _read_journal(tmp_path) == []
    spawn.assert_not_called()


def test_run_action_rejects_two_field_policy_copy_not_backed_by_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn = MagicMock()
    monkeypatch.setattr("fieldkit.companion.runner.subprocess.run", spawn)
    canonical = _preview_policy("watch run pursuit-stalls --dry-run")
    forged = replace(
        NO_ACT_POLICY,
        _entries=canonical._entries,
        _configured_entries=canonical._configured_entries,
    )
    _CONFIGURED_ALLOWLIST.clear()

    result = run_action(
        ["watch", "run", "pursuit-stalls", "--dry-run"],
        tier="act",
        policy=forged,
        data_path=tmp_path,
    )

    assert result.denied is True
    assert result.stderr == "invalid companion action authority"
    assert _read_journal(tmp_path) == []
    spawn.assert_not_called()
