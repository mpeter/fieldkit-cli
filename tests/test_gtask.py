"""Public contracts for preview-first Google Tasks mutations (implementation change)."""

import json
from collections.abc import Callable
from typing import Any, cast

import pytest
from click.testing import CliRunner

from fieldkit.__main__ import cli as root_cli
from fieldkit.__main__ import main
from fieldkit.errors import AuthError, WebDataError
from fieldkit.gtask.client import (
    GTaskValidationError,
    TaskMutation,
    complete_task,
    create_task,
)


@pytest.mark.unit
def test_create_preview_only_reads_task_list() -> None:
    calls: list[list[str]] = []

    def runner(args: list[str]) -> str:
        calls.append(args)
        return '{"items":[{"id":"list-1","title":"fieldkit"}]}'

    result = create_task("Call customer", section="today", account="acme", due="2026-09-11", gws_runner=runner)

    assert result == TaskMutation(
        "create", "list-1", None, "Call customer", "today", "acme", "2026-09-11T00:00:00.000Z", False
    )
    assert calls == [["tasks", "tasklists", "list", "--params", "{}"]]


@pytest.mark.unit
def test_create_confirmed_uses_exact_wire_grammar() -> None:
    calls: list[list[str]] = []
    responses = iter(['{"items":[{"id":"list-1","title":"fieldkit"}]}', '{"id":"task-9"}'])

    def runner(args: list[str]) -> str:
        calls.append(args)
        return next(responses)

    result = create_task(
        "Call customer", section="active", account="acme", due="2026-09-11", confirm=True, gws_runner=runner
    )

    assert result.task_id == "task-9"
    assert result.confirmed is True
    assert calls[1] == [
        "tasks",
        "tasks",
        "insert",
        "--params",
        '{"tasklist":"list-1"}',
        "--json",
        '{"due":"2026-09-11T00:00:00.000Z","notes":"section:active\\naccount:acme","title":"Call customer"}',
    ]


@pytest.mark.unit
def test_complete_confirmed_uses_exact_wire_grammar() -> None:
    calls: list[list[str]] = []
    responses = iter(['{"items":[{"id":"list-1","title":"fieldkit"}]}', '{"id":"task-9"}'])

    def runner(args: list[str]) -> str:
        calls.append(args)
        return next(responses)

    result = complete_task("task-9", confirm=True, gws_runner=runner)

    assert result.task_id == "task-9"
    assert result.confirmed is True
    assert calls[1] == [
        "tasks",
        "tasks",
        "patch",
        "--params",
        '{"task":"task-9","tasklist":"list-1"}',
        "--json",
        '{"status":"completed"}',
    ]


InvalidCall = Callable[[Callable[[list[str]], str]], TaskMutation]


@pytest.mark.unit
@pytest.mark.parametrize(
    "invoke",
    [
        lambda run: create_task("", section="today", gws_runner=run),
        lambda run: create_task(" " * 2, section="today", gws_runner=run),
        lambda run: create_task("x" * 1025, section="today", gws_runner=run),
        lambda run: create_task("x", section=cast(Any, "later"), gws_runner=run),
        lambda run: create_task("x", section="today", account="bad slug", gws_runner=run),
        lambda run: create_task("x", section="today", due="2026-02-30", gws_runner=run),
        lambda run: create_task("x", section="today", due="20260911", gws_runner=run),
        lambda run: create_task("x", section="today", due="2026-W37-5", gws_runner=run),
        lambda run: complete_task("bad/id", gws_runner=run),
    ],
)
def test_invalid_arguments_make_zero_gws_calls(invoke: InvalidCall) -> None:
    calls: list[list[str]] = []

    with pytest.raises(GTaskValidationError):
        invoke(lambda args: calls.append(args) or "{}")

    assert calls == []


@pytest.mark.unit
@pytest.mark.parametrize("response", ["{}", "[]", "not-json", '{"id":""}'])
def test_confirmed_create_rejects_malformed_response(response: str) -> None:
    replies = iter(['{"items":[{"id":"list-1","title":"fieldkit"}]}', response])

    with pytest.raises(WebDataError, match="mutation"):
        create_task("x", section="today", confirm=True, gws_runner=lambda _args: next(replies))


def _mutation(operation: str, *, confirmed: bool) -> TaskMutation:
    return TaskMutation(
        cast(Any, operation), "list-1", "task-9" if confirmed else None, "Title", "today", None, None, confirmed
    )


@pytest.mark.unit
def test_cli_conflicting_flags_stop_before_domain(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    called = False

    def fail(*_args: object, **_kwargs: object) -> TaskMutation:
        nonlocal called
        called = True
        raise AssertionError

    monkeypatch.setattr("fieldkit.commands.gtask.cli.create_task", fail)
    result = main(["gtask", "create", "Title", "--section", "today", "--dry-run", "--confirm"])

    assert result == 3
    assert "mutually exclusive" in capsys.readouterr().err
    assert called is False


@pytest.mark.unit
def test_cli_json_and_human_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "fieldkit.commands.gtask.cli.create_task", lambda *_args, **_kwargs: _mutation("create", confirmed=False)
    )
    preview = CliRunner().invoke(root_cli, ["gtask", "create", "Title", "--section", "today", "--json"])
    assert preview.exit_code == 0
    assert json.loads(preview.output)["confirmed"] is False

    monkeypatch.setattr(
        "fieldkit.commands.gtask.cli.complete_task", lambda *_args, **_kwargs: _mutation("complete", confirmed=True)
    )
    confirmed = CliRunner().invoke(root_cli, ["gtask", "complete", "task-9", "--confirm"])
    assert confirmed.exit_code == 0
    assert "Task task-9 completed." in confirmed.output


@pytest.mark.unit
def test_root_dispatcher_help_lists_gtask() -> None:
    result = CliRunner().invoke(root_cli, ["--help"])
    assert result.exit_code == 0
    assert "gtask" in result.output


@pytest.mark.unit
def test_root_maps_auth_error_to_exit_2(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def fail(*_args: object, **_kwargs: object) -> TaskMutation:
        raise AuthError("reauthenticate")

    monkeypatch.setattr("fieldkit.commands.gtask.cli.complete_task", fail)
    result = main(["gtask", "complete", "task-9"])
    assert result == 2
    assert "reauthenticate" in capsys.readouterr().err


@pytest.mark.unit
def test_root_maps_validation_to_exit_3(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def fail(*_args: object, **_kwargs: object) -> TaskMutation:
        raise GTaskValidationError("invalid task")

    monkeypatch.setattr("fieldkit.commands.gtask.cli.complete_task", fail)
    result = main(["gtask", "complete", "task-9"])
    assert result == 3
    assert "invalid task" in capsys.readouterr().err
