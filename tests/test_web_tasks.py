"""Public-contract tests for the read-only Google Tasks dashboard slice."""

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from fieldkit.errors import AuthError, WebDataError
from fieldkit.gtask.client import GoogleTask, list_tasks, run_gws
from fieldkit.web.data import DataSource
from fieldkit.web.server import create_app

pytestmark = pytest.mark.unit


def _runner(*responses: object) -> tuple[Callable[[list[str]], str], list[list[str]]]:
    pending = list(responses)
    calls: list[list[str]] = []

    def run(args: list[str]) -> str:
        calls.append(args)
        response = pending.pop(0)
        return response if isinstance(response, str) else json.dumps(response)

    return run, calls


def _params(call: list[str]) -> dict[str, object]:
    return json.loads(call[call.index("--params") + 1])


def _source(tmp_path: Path) -> DataSource:
    briefs = tmp_path / "briefs"
    watchers = tmp_path / "watchers"
    briefs.mkdir()
    watchers.mkdir()
    return DataSource(briefs_dir=briefs, watchers_dir=watchers, cli_json=lambda _args: {})


def test_list_tasks_discovers_list_and_parses_first_page() -> None:
    runner, calls = _runner(
        {"items": [{"id": "list-1", "title": "fieldkit"}]},
        {
            "items": [
                {
                    "id": "task-1",
                    "title": "Prepare brief",
                    "notes": "section:today\naccount:acme-corp\n<!-- gtask:task-1 -->",
                    "status": "needsAction",
                    "due": "2026-09-10T00:00:00.000Z",
                }
            ]
        },
    )

    result = list_tasks(gws_runner=runner)

    assert result
    assert result[0] == GoogleTask(
        id="task-1",
        title="Prepare brief",
        notes="section:today\naccount:acme-corp\n<!-- gtask:task-1 -->",
        status="needsAction",
        due="2026-09-10T00:00:00.000Z",
        completed=None,
        section="today",
        account="acme-corp",
    )
    assert calls[0][:3] == ["tasks", "tasklists", "list"]
    assert _params(calls[0]) == {}
    assert calls[1][:3] == ["tasks", "tasks", "list"]
    assert _params(calls[1]) == {"showCompleted": True, "showHidden": True, "tasklist": "list-1"}


def test_list_tasks_follows_task_list_pagination() -> None:
    runner, calls = _runner(
        {"items": [{"id": "other", "title": "Other"}], "nextPageToken": "lists-2"},
        {"items": [{"id": "list-1", "title": "fieldkit"}]},
        {"items": []},
    )

    result = list_tasks(gws_runner=runner)

    assert result == []
    assert _params(calls[1]) == {"pageToken": "lists-2"}


def test_list_tasks_aggregates_pages_once_in_api_order() -> None:
    runner, calls = _runner(
        {"items": [{"id": "list-1", "title": "fieldkit"}]},
        {
            "items": [
                {"id": "one", "title": "First", "notes": "section:active"},
                {"id": "two", "title": "Second"},
            ],
            "nextPageToken": "tasks-2",
        },
        {
            "items": [
                {"id": "two", "title": "Duplicate"},
                {"id": "three", "title": "Third", "status": "completed", "completed": "2026-09-09T12:00:00Z"},
            ]
        },
    )

    result = list_tasks(gws_runner=runner)

    assert result
    assert [task.id for task in result] == ["one", "two", "three"]
    assert result[2].completed == "2026-09-09T12:00:00Z"
    assert _params(calls[2]) == {
        "pageToken": "tasks-2",
        "showCompleted": True,
        "showHidden": True,
        "tasklist": "list-1",
    }


@pytest.mark.parametrize(
    ("notes", "section", "account"),
    [
        ("section:active\naccount:acme-corp", "active", "acme-corp"),
        ("intro\nsection:today\naccount:late", "other", None),
        ("section:today\naccount:", "today", None),
        ("account:wrong-line\nsection:active", "other", None),
    ],
)
def test_list_tasks_uses_exact_metadata_lines(notes: str, section: str, account: str | None) -> None:
    runner, _calls = _runner(
        {"items": [{"id": "list-1", "title": "fieldkit"}]},
        {"items": [{"id": "task-1", "title": "Task", "notes": notes}]},
    )

    result = list_tasks(gws_runner=runner)

    assert result
    assert result[0].section == section
    assert result[0].account == account


@pytest.mark.parametrize(
    ("responses", "message"),
    [
        (({"items": []},), "not found"),
        (("not json",), "non-JSON"),
        (([],), "non-object"),
        (({"items": {}},), "non-list items"),
        (
            (
                {"items": [{"id": "list-1", "title": "fieldkit"}]},
                {"items": [{"id": "", "title": "Task"}]},
            ),
            "empty id",
        ),
        (
            (
                {"items": [{"id": "list-1", "title": "fieldkit"}]},
                {"items": [{"id": "task-1", "title": ""}]},
            ),
            "empty title",
        ),
    ],
)
def test_list_tasks_rejects_invalid_payloads(responses: tuple[object, ...], message: str) -> None:
    runner, _calls = _runner(*responses)

    with pytest.raises(WebDataError, match=message):
        list_tasks(gws_runner=runner)


def test_list_tasks_rejects_repeated_page_token() -> None:
    runner, _calls = _runner(
        {"items": [], "nextPageToken": "repeat"},
        {"items": [], "nextPageToken": "repeat"},
    )

    with pytest.raises(WebDataError, match="repeated a page token"):
        list_tasks(gws_runner=runner)


def test_list_tasks_stops_after_one_hundred_pages() -> None:
    responses = tuple({"items": [], "nextPageToken": f"page-{number}"} for number in range(100))
    runner, calls = _runner(*responses)

    with pytest.raises(WebDataError, match="exceeded 100 pages"):
        list_tasks(gws_runner=runner)

    assert len(calls) == 100


def test_run_gws_reports_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fieldkit.gtask.client.shutil.which", lambda _name: None)

    with pytest.raises(WebDataError, match="gws CLI not found"):
        run_gws(["tasks", "tasklists", "list"])


def test_run_gws_reports_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fieldkit.gtask.client.shutil.which", lambda _name: "/usr/bin/gws")

    def timeout(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="gws", timeout=30)

    monkeypatch.setattr("fieldkit.gtask.client.subprocess.run", timeout)

    with pytest.raises(WebDataError, match="timed out after 30s"):
        run_gws(["tasks", "tasklists", "list"])


def test_run_gws_reports_launch_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fieldkit.gtask.client.shutil.which", lambda _name: "/usr/bin/gws")

    def fail_to_start(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("sensitive local detail")

    monkeypatch.setattr("fieldkit.gtask.client.subprocess.run", fail_to_start)

    with pytest.raises(
        WebDataError, match=r"verify the gws installation.*run the gws auth flow in a terminal"
    ) as exc_info:
        run_gws(["tasks", "tasklists", "list"])
    assert "sensitive local detail" not in str(exc_info.value)


def test_run_gws_reports_nonzero_with_auth_guidance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fieldkit.gtask.client.shutil.which", lambda _name: "/usr/bin/gws")
    monkeypatch.setattr(
        "fieldkit.gtask.client.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=9, stdout="", stderr="arbitrary failure\n"),
    )

    with pytest.raises(WebDataError, match=r"exited 9.*run the gws auth flow in a terminal"):
        run_gws(["tasks", "tasks", "list"])


def test_run_gws_classifies_auth_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fieldkit.gtask.client.shutil.which", lambda _name: "/usr/bin/gws")
    monkeypatch.setattr(
        "fieldkit.gtask.client.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="", stderr="credentials expired\n"),
    )

    with pytest.raises(AuthError, match="credentials expired"):
        run_gws(["tasks", "tasks", "list"])


def test_run_gws_allows_successful_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fieldkit.gtask.client.shutil.which", lambda _name: "/usr/bin/gws")
    invocation: dict[str, object] = {}

    def success(args: list[str], **kwargs: object) -> SimpleNamespace:
        invocation.update(args=args, **kwargs)
        return SimpleNamespace(returncode=0, stdout='{"items": []}', stderr="diagnostic\n")

    monkeypatch.setattr("fieldkit.gtask.client.subprocess.run", success)

    result = run_gws(["tasks", "tasks", "list"])

    assert result == '{"items": []}'
    assert invocation == {
        "args": ["/usr/bin/gws", "tasks", "tasks", "list"],
        "capture_output": True,
        "text": True,
        "timeout": 30,
        "check": False,
    }


def test_tasks_route_returns_public_records(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    task = GoogleTask("task-1", "Prepare brief", "", "needsAction", None, None, "today", None)
    monkeypatch.setattr("fieldkit.web.server.tasks_mod.list_tasks", lambda: [task])
    app = create_app(_source(tmp_path))

    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get("/api/tasks")

    assert response.status_code == 200
    assert response.json() == {"tasks": [task.to_dict()]}


def test_tasks_route_isolates_read_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fail() -> list[GoogleTask]:
        raise WebDataError("tasks unavailable")

    monkeypatch.setattr("fieldkit.web.server.tasks_mod.list_tasks", fail)
    app = create_app(_source(tmp_path))

    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get("/api/tasks")

    assert response.status_code == 502
    assert response.json() == {"error": "tasks unavailable"}


def test_tasks_route_isolates_auth_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fail() -> list[GoogleTask]:
        raise AuthError("credentials expired")

    monkeypatch.setattr("fieldkit.web.server.tasks_mod.list_tasks", fail)
    app = create_app(_source(tmp_path))

    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get("/api/tasks")

    assert response.status_code == 502
    assert response.json() == {"error": "credentials expired"}


def test_tasks_route_maps_gws_launch_failure_to_502(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("fieldkit.gtask.client.shutil.which", lambda _name: "/usr/bin/gws")

    def fail_to_start(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr("fieldkit.gtask.client.subprocess.run", fail_to_start)
    app = create_app(_source(tmp_path))

    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get("/api/tasks")

    assert response.status_code == 502
    assert response.json() == {
        "error": "gws could not start — verify the gws installation and run the gws auth flow in a terminal"
    }


def test_static_shell_contains_gated_tasks_surface() -> None:
    static = Path(__file__).parents[1] / "src" / "fieldkit" / "web" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    javascript = (static / "app.js").read_text(encoding="utf-8")
    service_worker = (static / "sw.js").read_text(encoding="utf-8")

    assert 'data-tab="tasks"' in html
    assert 'id="tasks-list"' in html
    assert 'id="task-create-form"' in html
    assert 'api("/api/tasks")' in javascript
    assert 'api("/api/companion")' in javascript
    assert 'api("/api/tasks/create"' in javascript
    assert '"/complete", { method: "POST" }' in javascript
    assert "esc(task.title)" in javascript
    assert "esc(task.due.slice(0, 10))" in javascript
    assert "esc(task.account)" in javascript
    assert "esc(task.completed.slice(0, 10))" in javascript
    assert 'const CACHE = "fieldkit-v3";' in service_worker
