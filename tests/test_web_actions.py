"""Public contracts for dashboard actions."""

import datetime as dt
import json
from pathlib import Path

import pytest

from fieldkit.companion.lifecycle import read_outcomes
from fieldkit.companion.runner import ActionResult
from fieldkit.web.actions import approve, complete_task, create_task

pytestmark = pytest.mark.unit


class Recorder:
    def __init__(self, result: ActionResult | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.result = result

    def __call__(self, argv: list[str], **kwargs: object) -> ActionResult:
        self.calls.append((argv, kwargs))
        return self.result or ActionResult(argv, 0, False, "", "")


def test_read_create_has_no_side_effects(tmp_path: Path) -> None:
    runner = Recorder()
    result = create_task(
        "  title  ", "today", None, None, tier="unknown", allowlist=[], data_path=tmp_path, runner=runner
    )
    assert result.state == "disabled"
    assert runner.calls == []
    assert not (tmp_path / "companion-outbox").exists()


def test_propose_create_has_exact_normalized_contract_and_stable_id(tmp_path: Path) -> None:
    now = dt.datetime(2026, 9, 10, 12, tzinfo=dt.UTC)
    first = create_task(
        "  Call customer  ", "active", " acme ", "2026-09-11", tier="propose", allowlist=[], data_path=tmp_path, now=now
    )
    second = create_task(
        "Call customer", "active", "acme", "2026-09-11", tier="propose", allowlist=[], data_path=tmp_path, now=now
    )
    assert first.state == second.state == "proposed"
    assert first.item_id == second.item_id
    assert first.proposal_name == second.proposal_name
    files = list((tmp_path / "companion-outbox").glob("*.proposal.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["command_argv"] == [
        "gtask",
        "create",
        "Call customer",
        "--section",
        "active",
        "--account",
        "acme",
        "--due",
        "2026-09-11",
        "--confirm",
    ]
    assert "web://tasks" in payload["markdown"]
    assert "web/google-tasks-create" in payload["markdown"]


def test_act_complete_calls_exact_gate_contract(tmp_path: Path) -> None:
    runner = Recorder()
    result = complete_task(" task-1 ", tier="act", allowlist=["exact"], data_path=tmp_path, runner=runner)
    assert result.state == "executed"
    assert runner.calls == [
        (
            ["gtask", "complete", "task-1", "--confirm"],
            {"tier": "act", "allowlist": ["exact"], "data_path": tmp_path, "item_id": result.item_id},
        )
    ]


@pytest.mark.parametrize("denied,exit_code,state", [(True, 3, "denied"), (False, 1, "failed")])
def test_act_maps_runner_failures(tmp_path: Path, denied: bool, exit_code: int, state: str) -> None:
    runner = Recorder(ActionResult([], exit_code, denied, "", "failure"))
    assert complete_task("task-1", tier="act", allowlist=[], data_path=tmp_path, runner=runner).state == state


def test_approve_executes_recorded_argv_and_deletes_on_success(tmp_path: Path) -> None:
    proposed = complete_task("task-1", tier="propose", allowlist=[], data_path=tmp_path)
    assert proposed.proposal_name is not None
    runner = Recorder()
    result = approve(proposed.proposal_name, tier="propose", allowlist=["allowed"], data_path=tmp_path, runner=runner)
    assert result.state == "executed"
    assert runner.calls[0][0] == ["gtask", "complete", "task-1", "--confirm"]
    assert runner.calls[0][1]["tier"] == "act"
    assert not (tmp_path / "companion-outbox" / proposed.proposal_name).exists()
    outcomes = read_outcomes(tmp_path)
    assert [(row.proposal_name, row.item_id, row.outcome) for row in outcomes] == [
        (proposed.proposal_name, proposed.item_id, "approved")
    ]


def test_approve_denial_restores_and_second_success_executes_once_more(tmp_path: Path) -> None:
    proposed = complete_task("task-1", tier="propose", allowlist=[], data_path=tmp_path)
    assert proposed.proposal_name is not None
    denied_runner = Recorder(ActionResult([], 3, True, "", ""))
    assert (
        approve(proposed.proposal_name, tier="act", allowlist=[], data_path=tmp_path, runner=denied_runner).state
        == "denied"
    )
    payload = json.loads((tmp_path / "companion-outbox" / proposed.proposal_name).read_text(encoding="utf-8"))
    assert payload["command_argv"] is not None
    assert read_outcomes(tmp_path) == []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"title": " ", "section": "today", "account": None, "due": None}, "title"),
        ({"title": "x", "section": "today", "account": "bad/name", "due": None}, "account"),
        ({"title": "x", "section": "today", "account": None, "due": "tomorrow"}, "due"),
    ],
)
def test_create_validation(tmp_path: Path, kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        create_task(**kwargs, tier="act", allowlist=[], data_path=tmp_path)  # type: ignore[arg-type]
