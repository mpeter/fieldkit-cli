"""Real watcher orchestration and status publication at bounded external boundaries."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx
import pytest

import fieldkit.watch.backstory_health as backstory
import fieldkit.watch.draft_queue as drafts
import fieldkit.watch.logging as watcher_logging
import fieldkit.watch.mcp as mcp
import fieldkit.watch.status as status
import fieldkit.watch.waiting_on_tracker as waiting
from fieldkit.__main__ import main
from fieldkit.watch.status import write_run_status

pytestmark = pytest.mark.unit


@dataclass
class Boundary:
    home: Path
    mode: Literal["ok", "auth", "provider"] = "ok"
    fail_status: bool = False
    publications: list[Path] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)

    @property
    def status_file(self) -> Path:
        return self.home / "watchers" / "watcher-run-status.json"


@pytest.fixture
def boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Boundary:
    edge = Boundary(tmp_path)
    watchers = tmp_path / "watchers"
    for module in (backstory, drafts, waiting):
        # Replace the autouse no-op with the original function, not a recording mock.
        monkeypatch.setattr(module, "write_run_status", write_run_status)
        monkeypatch.setattr(module, "get_watchers_dir", lambda: watchers)
    for module in (status, waiting, watcher_logging):
        monkeypatch.setattr(module, "get_fieldkit_home", lambda: tmp_path)
    monkeypatch.setattr(backstory, "get_accounts_config", lambda **kwargs: {"accounts": {"acme": {}}})
    for module in (backstory, drafts):
        monkeypatch.setattr(module, "get_mcp_endpoint", lambda name: "https://gateway.example.com/mcp")
    monkeypatch.setenv("FIELDKIT_USER_EMAIL", "contributor@example.com")

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        method = payload["method"]
        edge.methods.append(method)
        if edge.mode == "auth":
            return httpx.Response(401, json={"error": "synthetic authentication failure"})
        if edge.mode == "provider":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload.get("id"), "error": {"code": -1}})
        if method == "initialize":
            return httpx.Response(
                200,
                headers={"mcp-session-id": "fictional-session"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fictional-provider", "version": "1"},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        tool = payload["params"]["name"]
        data = (
            {"peopleai_account_id": 1, "opportunities": [{"engagement_level": 80}]}
            if tool == "backstory__find_account"
            else {"messages": []}
        )
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"content": [{"type": "text", "text": json.dumps(data)}]},
            },
        )

    async def new_client(timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(respond), timeout=timeout)

    monkeypatch.setattr(mcp, "_new_http_client", new_client)
    original_replace = Path.replace

    def replace(source: Path, target: str | Path) -> Path:
        destination = Path(target)
        edge.publications.append(destination)
        if destination == edge.status_file and edge.fail_status:
            raise OSError("synthetic status publication failure")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", replace)
    return edge


def _invoke(watcher: str, *, dry_run: bool = False) -> int:
    argv = ["watch", "run", watcher, "--json"]
    if dry_run:
        argv.append("--dry-run")
    return main(argv)


@pytest.mark.parametrize("watcher", ["backstory-health", "draft-queue", "waiting-on-tracker"])
@pytest.mark.parametrize("fail_status", [False, True])
def test_clean_scan_status_publication_controls_exit_and_json(
    boundary: Boundary,
    capsys: pytest.CaptureFixture[str],
    watcher: str,
    fail_status: bool,
) -> None:
    if watcher == "waiting-on-tracker":
        (boundary.home / "TASKS.md").write_text("# Tasks\n\n## Waiting On\n\n## Done\n", encoding="utf-8")
    boundary.fail_status = fail_status
    result = _invoke(watcher)
    assert result == (1 if fail_status else 0)
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == ("fatal" if fail_status else "ok")
    assert payload["failures"] == int(fail_status)
    assert boundary.status_file in boundary.publications
    assert boundary.status_file.exists() is not fail_status
    assert list((boundary.home / "watchers").glob(".watcher-run-status.json.*.tmp")) == []


@pytest.mark.parametrize("fail_status", [False, True])
def test_missing_tasks_preserves_empty_success_unless_status_publication_fails(
    boundary: Boundary,
    capsys: pytest.CaptureFixture[str],
    fail_status: bool,
) -> None:
    boundary.fail_status = fail_status
    result = _invoke("waiting-on-tracker")
    assert result == (1 if fail_status else 0)
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == ("fatal" if fail_status else "ok")
    assert payload["records_checked"] == 0
    assert payload["failures"] == int(fail_status)
    assert not (boundary.home / "TASKS.md").exists()


@pytest.mark.parametrize("watcher", ["backstory-health", "draft-queue", "waiting-on-tracker"])
def test_dry_run_bypasses_status_publication_and_provider(
    boundary: Boundary,
    capsys: pytest.CaptureFixture[str],
    watcher: str,
) -> None:
    boundary.fail_status = True
    result = _invoke(watcher, dry_run=True)
    assert result == 0
    output = capsys.readouterr().out
    if watcher == "draft-queue":
        preview, separator, document = output.partition("{\n")
        assert "No stale drafts found." in preview
        payload = json.loads(separator + document)
    else:
        payload = json.loads(output)
    assert payload["outcome"] == "ok"
    assert payload["failures"] == 0
    assert payload["dry_run"] is True
    assert boundary.publications == []
    assert boundary.methods == []
    assert list(boundary.home.iterdir()) == []


@pytest.mark.parametrize("watcher", ["backstory-health", "draft-queue"])
@pytest.mark.parametrize("fail_status", [False, True])
def test_authentication_precedes_status_publication_at_real_cli(
    boundary: Boundary,
    capsys: pytest.CaptureFixture[str],
    watcher: str,
    fail_status: bool,
) -> None:
    boundary.mode = "auth"
    boundary.fail_status = fail_status
    result = _invoke(watcher)
    assert result == 2
    assert "Auth error" in capsys.readouterr().err
    assert boundary.status_file not in boundary.publications
    assert not boundary.status_file.exists()


@pytest.mark.parametrize("watcher", ["backstory-health", "draft-queue"])
@pytest.mark.parametrize("fail_status", [False, True])
def test_existing_provider_failure_remains_nonpassing(
    boundary: Boundary,
    capsys: pytest.CaptureFixture[str],
    watcher: str,
    fail_status: bool,
) -> None:
    boundary.mode = "provider"
    boundary.fail_status = fail_status
    result = _invoke(watcher)
    assert result == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "fatal"
    assert payload["failures"] == 1 + int(fail_status)


def test_status_failure_preserves_prior_entry_and_successful_suppression_state(
    boundary: Boundary,
    capsys: pytest.CaptureFixture[str],
) -> None:
    boundary.status_file.parent.mkdir()
    prior = {"backstory-health": {"last_run": "2026-01-01T00:00:00Z", "outcome": "ok"}}
    boundary.status_file.write_text(json.dumps(prior), encoding="utf-8")
    boundary.fail_status = True
    result = _invoke("backstory-health")
    assert result == 1
    assert json.loads(capsys.readouterr().out)["outcome"] == "fatal"
    assert json.loads(boundary.status_file.read_text(encoding="utf-8")) == prior
    state = json.loads((boundary.home / "watchers" / "backstory-health-state.json").read_text(encoding="utf-8"))
    assert state["acme"]["health_score"] == 80
