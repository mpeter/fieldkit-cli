"""Tests for fieldkit.web — data providers, server routes, SSE, chat."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from fieldkit.companion.runner import ActionResult
from fieldkit.errors import WebDataError
from fieldkit.web.data import BriefDoc, DataSource
from fieldkit.web.events import alert_event_stream
from fieldkit.web.server import create_app

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def source(tmp_path: Path) -> DataSource:
    """A DataSource over tmp dirs with a stub CLI runner."""
    briefs = tmp_path / "briefs"
    watchers = tmp_path / "watchers"
    briefs.mkdir(mode=0o700)
    watchers.mkdir(mode=0o700)

    def fake_cli(args: list[str]) -> Any:
        key = " ".join(args)
        payloads: dict[str, Any] = {
            "pursuit health --json": [
                {
                    "relative_path": "acme/big-deal.md",
                    "stage": "propose",
                    "qualification_status": "unavailable",
                    "close_date_str": "2026-08-01",
                    "days_until_close": 21,
                    "days_in_stage": 9,
                    "risk_tier": "MEDIUM",
                    "risk_reasons": ["close date approaching at an early stage"],
                    "sf_opportunity_id": "006XX0000012345",
                }
            ],
            "pursuit forecast --json": {"commit": 100000.0, "weighted": 250000.0},
            "pipeline quota --json": {"target": 2000000, "closed_won": 400000, "gap": 1600000},
        }
        if key not in payloads:
            raise WebDataError(f"unexpected CLI call: {key}")
        return payloads[key]

    return DataSource(
        briefs_dir=briefs,
        watchers_dir=watchers,
        cli_json=fake_cli,
        doctor_json=lambda: [{"service": "google", "healthy": True, "configured": True, "message": "token valid"}],
    )


@pytest.fixture
def client(source: DataSource) -> TestClient:
    # base_url sets the Host header; tokenless mode only accepts loopback hosts.
    return TestClient(create_app(source), base_url="http://127.0.0.1")


def _write_brief(source: DataSource, name: str = "morning-brief-2026-07-11.md") -> Path:
    path = source.briefs_dir / name
    path.write_text("# Morning Brief\n\n- item one\n", encoding="utf-8")
    return path


def test_feed_route_uses_non_consuming_json_lines(source: DataSource) -> None:
    calls: list[list[str]] = []
    source.cli_json_lines = lambda args: calls.append(args) or [{"item_id": "abc", "summary": "Review"}]
    response = TestClient(create_app(source), base_url="http://127.0.0.1").get("/api/feed")
    assert response.status_code == 200
    assert calls == [["companion", "feed", "--json", "--all"]]
    assert response.json()["items"][0]["summary"] == "Review"


def test_companion_status_reports_write_contract(source: DataSource, tmp_path: Path) -> None:
    response = TestClient(
        create_app(
            source,
            token="secret",
            tier_provider=lambda: "propose",
            allowlist_provider=lambda: [],
            data_path_provider=lambda: tmp_path,
        ),
        base_url="http://127.0.0.1",
    ).get("/api/companion", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200
    assert response.json() == {"tier": "propose", "writes_enabled": True, "minimum_write_tier": "propose"}


@pytest.mark.parametrize("error", [WebDataError("bad config"), OSError("private path")])
def test_companion_status_maps_provider_failures(source: DataSource, error: Exception) -> None:
    def fail() -> str:
        raise error

    response = TestClient(create_app(source, token="secret", tier_provider=fail), base_url="http://127.0.0.1").get(
        "/api/companion", headers={"Authorization": "Bearer secret"}
    )
    assert response.status_code == 502


def test_task_write_auth_precedes_all_providers(source: DataSource, tmp_path: Path) -> None:
    calls: list[str] = []
    client = TestClient(
        create_app(
            source,
            tier_provider=lambda: calls.append("tier") or "act",
            allowlist_provider=lambda: calls.append("allowlist") or [],
            data_path_provider=lambda: calls.append("data") or tmp_path,
        ),
        base_url="http://127.0.0.1",
    )
    response = client.post("/api/tasks/create", json={"title": "x", "section": "today"})
    assert response.status_code == 403
    assert calls == []


def test_authenticated_create_maps_proposal_status(source: DataSource, tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            source,
            token="secret",
            tier_provider=lambda: "propose",
            allowlist_provider=lambda: [],
            data_path_provider=lambda: tmp_path,
        ),
        base_url="http://127.0.0.1",
    )
    response = client.post(
        "/api/tasks/create",
        headers={"Authorization": "Bearer secret"},
        json={"title": "Call customer", "section": "today"},
    )
    assert response.status_code == 202
    assert response.json()["state"] == "proposed"


def test_authenticated_complete_executes_through_injected_runner(source: DataSource, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], **_: object) -> ActionResult:
        calls.append(argv)
        return ActionResult(argv, 0, False, "", "")

    client = TestClient(
        create_app(
            source,
            token="secret",
            tier_provider=lambda: "act",
            allowlist_provider=lambda: ["allowed"],
            data_path_provider=lambda: tmp_path,
            action_runner=runner,
        ),
        base_url="http://127.0.0.1",
    )
    response = client.post("/api/tasks/task-1/complete", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200
    assert calls == [["gtask", "complete", "task-1", "--confirm"]]


@pytest.mark.parametrize(
    ("path", "body"),
    [("/api/tasks/create", {"title": "x", "section": "today"}), ("/api/tasks/task-1/complete", None)],
)
def test_task_filesystem_failures_are_bounded_502(
    source: DataSource, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, body: dict[str, str] | None
) -> None:
    target = "create_task" if path.endswith("create") else "complete_task"

    def fail(*_: object, **__: object) -> None:
        raise OSError("disk unavailable at /var/lib/fieldkit/private-token")

    monkeypatch.setattr(f"fieldkit.web.actions.{target}", fail)
    client = TestClient(
        create_app(
            source,
            token="secret",
            tier_provider=lambda: "propose",
            allowlist_provider=lambda: [],
            data_path_provider=lambda: tmp_path,
        ),
        base_url="http://127.0.0.1",
    )
    response = client.post(path, headers={"Authorization": "Bearer secret"}, json=body)
    assert response.status_code == 502
    assert response.json() == {"error": "dashboard action storage unavailable"}
    assert "/var/lib/fieldkit/private-token" not in response.text


@pytest.mark.parametrize("body", [{}, {"title": "x", "section": "bogus"}])
def test_tokenless_create_rejects_before_body_validation(source: DataSource, body: dict[str, str]) -> None:
    response = TestClient(create_app(source), base_url="http://127.0.0.1").post("/api/tasks/create", json=body)
    assert response.status_code == 403


def test_approval_missing_and_not_approvable_have_typed_statuses(source: DataSource, tmp_path: Path) -> None:
    directory = tmp_path / "companion-outbox"
    directory.mkdir(mode=0o700)
    (directory / "invalid.proposal.json").write_text("{}", encoding="utf-8")
    client = TestClient(
        create_app(
            source,
            token="secret",
            tier_provider=lambda: "act",
            allowlist_provider=lambda: [],
            data_path_provider=lambda: tmp_path,
        ),
        base_url="http://127.0.0.1",
    )
    headers = {"Authorization": "Bearer secret"}
    assert client.post("/api/proposals/missing.proposal.json/approve", headers=headers).status_code == 404
    assert client.post("/api/proposals/invalid.proposal.json/approve", headers=headers).status_code == 409


# ---------------------------------------------------------------------------
# DataSource
# ---------------------------------------------------------------------------


def test_latest_brief_returns_none_when_empty(source: DataSource) -> None:
    result = source.latest_brief()
    assert result is None


def test_latest_brief_picks_most_recent_by_name(source: DataSource) -> None:
    _write_brief(source, "morning-brief-2026-07-01.md")
    _write_brief(source, "morning-brief-2026-07-11.md")
    result = source.latest_brief()
    assert isinstance(result, BriefDoc)
    assert result.name == "morning-brief-2026-07-11.md"
    assert "Morning Brief" in result.markdown


def test_latest_brief_ignores_watchers_dir(source: DataSource) -> None:
    """Briefs consolidated to briefs_dir (PR3) — watchers_dir is alerts-only now."""
    _write_brief(source, "morning-brief-2026-07-01.md")
    watcher_brief = source.watchers_dir / "morning-brief-2026-07-11.md"
    watcher_brief.write_text("# Watcher Brief\n", encoding="utf-8")
    result = source.latest_brief()
    assert isinstance(result, BriefDoc)
    assert result.name == "morning-brief-2026-07-01.md"


def test_list_alerts_orders_newest_first(source: DataSource) -> None:
    old = source.watchers_dir / "pursuit-stall-alerts.md"
    new = source.watchers_dir / "contract-expiry-alerts.md"
    old.write_text("## old\n", encoding="utf-8")
    new.write_text("## new\n", encoding="utf-8")
    import os

    os.utime(old, (1_000_000, 1_000_000))
    result = source.list_alerts()
    assert len(result) == 2
    assert result[0].name == "contract-expiry-alerts.md"


def test_alert_mtimes_ignores_non_alert_files(source: DataSource) -> None:
    (source.watchers_dir / "pursuit-stall-alerts.md").write_text("x", encoding="utf-8")
    (source.watchers_dir / "state.json").write_text("{}", encoding="utf-8")
    result = source.alert_mtimes()
    assert set(result) == {"pursuit-stall-alerts.md"}


def test_alert_read_survives_file_vanishing_between_glob_and_read(
    source: DataSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    # TOCTOU: watcher rewrites via delete+recreate between glob and read.
    survivor = source.watchers_dir / "contract-expiry-alerts.md"
    survivor.write_text("## ok\n", encoding="utf-8")
    ghost = source.watchers_dir / "pursuit-stall-alerts.md"
    ghost.write_text("## gone\n", encoding="utf-8")

    original_read = Path.read_text

    def racy_read(self: Path, *args: Any, **kwargs: Any) -> str:
        if self.name == ghost.name:
            raise FileNotFoundError(ghost)
        return original_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", racy_read)
    result = source.list_alerts()
    assert [a.name for a in result] == ["contract-expiry-alerts.md"]


def test_missing_dirs_degrade_to_empty(tmp_path: Path) -> None:
    src = DataSource(briefs_dir=tmp_path / "nope", watchers_dir=tmp_path / "nope2")
    assert src.latest_brief() is None
    assert src.list_alerts() == []
    assert src.alert_mtimes() == {}


def test_from_config_reads_briefs_from_workspace_and_proposals_from_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fieldkit.web.data import DataSource

    home = tmp_path / "home"
    data = tmp_path / "data"
    watchers = home / "watchers"
    monkeypatch.setattr("fieldkit.config.get_fieldkit_home", lambda: home)
    monkeypatch.setattr("fieldkit.config.get_fieldkit_data", lambda: data)
    monkeypatch.setattr("fieldkit.config.get_watchers_dir", lambda: watchers)

    result = DataSource.from_config()

    assert result.briefs_dir == home / "briefs"
    assert result.watchers_dir == watchers
    assert result.data_dir == data


def test_latest_companion_timestamp_ignores_invalid_journal_records(source: DataSource) -> None:
    source.data_dir = source.briefs_dir.parent / "data"
    source.data_dir.mkdir(mode=0o700)
    (source.data_dir / "companion-journal-2026-08.jsonl").write_text(
        '{"timestamp":"2026-08-16T06:00:00Z"}\n{"timestamp": 42}\n[]\n{"timestamp":"2026-08-16T07:00:00Z"}\n',
        encoding="utf-8",
    )
    (source.data_dir / "companion-journal-2026-07.jsonl").write_text("not json\n", encoding="utf-8")

    assert source._latest_companion_timestamp() == "2026-08-16T07:00:00Z"


def test_operations_prioritizes_auth_failure_and_exposes_pending_proposals(source: DataSource) -> None:
    """The mother-hen surface makes a stale chain and its safe next step explicit."""
    source.data_dir = source.briefs_dir.parent / "data"
    outbox = source.data_dir / "companion-outbox"
    outbox.mkdir(parents=True, mode=0o700)
    proposal = outbox / "2026-08-16-0123456789abcdef-task-management.md"
    proposal.write_text("# Companion proposal\n\nTake the safe next step.\n", encoding="utf-8")

    def cli(args: list[str]) -> Any:
        if args == ["watch", "status", "--json"]:
            return {"items": [{"watcher": "slack-threads", "outcome": "ok", "last_run": "2026-08-01T10:00:00Z"}]}
        if args == ["driver", "status", "--json"]:
            return {"items": [{"outcome": "skipped", "ts": "2026-08-16T10:00:00Z", "error": "cap reached"}]}
        raise WebDataError(f"unexpected CLI call: {args}")

    source.cli_json = cli
    source.doctor_json = lambda: [
        {"service": "google", "healthy": False, "configured": True, "message": "token expired"}
    ]

    result = source.operations()

    stages = {stage["name"]: stage for stage in result["stages"]}
    assert stages["doctor"]["status"] == "error"
    assert stages["watchers"]["status"] == "stale"
    assert result["next_action"] == "Run fieldkit doctor to restore an unavailable credential."
    assert result["proposals"][0]["name"] == proposal.name
    assert result["proposals"][0]["approvable"] is False
    assert result["proposals"][0]["validation_error"] is None
    assert "markdown" not in result["proposals"][0]


def test_operations_treats_completed_partial_watcher_run_as_healthy(source: DataSource) -> None:
    source.now = lambda: datetime(2026, 8, 16, 8, tzinfo=UTC)

    def cli(args: list[str]) -> Any:
        if args == ["watch", "status", "--json"]:
            return {"items": [{"watcher": "morning-brief", "outcome": "partial", "last_run": "2026-08-16T07:00:00Z"}]}
        if args == ["driver", "status", "--json"]:
            return {"items": []}
        raise WebDataError(f"unexpected CLI call: {args}")

    source.cli_json = cli

    stages = {stage["name"]: stage for stage in source.operations()["stages"]}

    assert stages["watchers"]["status"] == "ok"
    assert stages["watchers"]["detail"] == "Watcher data is current."


def test_operations_treats_an_unexpected_doctor_payload_as_an_error(source: DataSource) -> None:
    source.doctor_json = lambda: {"services": []}

    stages = {stage["name"]: stage for stage in source.operations()["stages"]}

    assert stages["doctor"]["status"] == "error"
    assert "Unexpected doctor payload" in stages["doctor"]["detail"]


def test_operations_surfaces_denied_developer_admission(source: DataSource) -> None:
    source.data_dir = source.briefs_dir.parent / "data"
    ledger = source.data_dir / "driver" / "developer-admission.json"
    ledger.parent.mkdir(parents=True, mode=0o700)
    ledger.write_text(
        '{"decisions":[{"allowed":false,"job":"driver","reason_code":"spend-cap-reached",'
        '"detail":"daily cap reached","ts":"2026-08-16T10:00:00Z"}]}',
        encoding="utf-8",
    )
    source.cli_json = lambda args: {"items": []}

    stages = {stage["name"]: stage for stage in source.operations()["stages"]}

    assert stages["developer"]["status"] == "error"
    assert stages["developer"]["detail"] == "Developer admission denied: spend-cap-reached — daily cap reached"


def test_operations_uses_the_latest_admission_decision(source: DataSource) -> None:
    source.data_dir = source.briefs_dir.parent / "data"
    ledger = source.data_dir / "driver" / "developer-admission.json"
    ledger.parent.mkdir(parents=True, mode=0o700)
    ledger.write_text(
        '{"decisions":[{"allowed":false,"job":"driver","reason_code":"spend-cap-reached",'
        '"detail":"daily cap reached","ts":"2026-08-16T10:00:00Z"},'
        '{"allowed":true,"job":"driver","reason_code":"released","detail":"lease released",'
        '"ts":"2026-08-16T10:01:00Z"}]}',
        encoding="utf-8",
    )
    source.cli_json = lambda args: {"items": []}

    stages = {stage["name"]: stage for stage in source.operations()["stages"]}

    assert stages["developer"]["status"] == "missing"


def test_operations_treats_lease_contention_as_information(source: DataSource) -> None:
    source.data_dir = source.briefs_dir.parent / "data"
    ledger = source.data_dir / "driver" / "developer-admission.json"
    ledger.parent.mkdir(parents=True, mode=0o700)
    ledger.write_text(
        '{"decisions":[{"allowed":false,"job":"proctor","reason_code":"lease-held",'
        '"detail":"developer lease is held by driver","ts":"2026-08-16T10:00:00Z"}]}',
        encoding="utf-8",
    )
    source.cli_json = lambda args: {"items": []}

    stages = {stage["name"]: stage for stage in source.operations()["stages"]}

    assert stages["developer"]["status"] == "ok"


def test_operations_treats_daily_run_limit_as_information(source: DataSource) -> None:
    source.data_dir = source.briefs_dir.parent / "data"
    ledger = source.data_dir / "driver" / "developer-admission.json"
    ledger.parent.mkdir(parents=True, mode=0o700)
    ledger.write_text(
        '{"decisions":[{"allowed":false,"job":"driver","reason_code":"daily-run-limit-reached",'
        '"detail":"daily developer run limit 1 has been reached","ts":"2026-08-16T10:00:00Z"}]}',
        encoding="utf-8",
    )

    stages = {stage["name"]: stage for stage in source.operations()["stages"]}

    assert stages["developer"]["status"] == "ok"
    assert stages["developer"]["action"] == ""


def test_operations_uses_released_admission_as_the_latest_developer_freshness(source: DataSource) -> None:
    source.data_dir = source.briefs_dir.parent / "data"
    ledger = source.data_dir / "driver" / "developer-admission.json"
    ledger.parent.mkdir(parents=True, mode=0o700)
    ledger.write_text(
        '{"decisions":[{"allowed":true,"job":"driver","reason_code":"released",'
        '"detail":"developer lease released","ts":"2026-08-16T10:00:00Z"}]}',
        encoding="utf-8",
    )
    source.cli_json = lambda args: {"items": [{"outcome": "skipped", "ts": "2026-08-13T21:06:55Z"}]}

    stages = {stage["name"]: stage for stage in source.operations()["stages"]}

    assert stages["developer"]["updated_at"] == "2026-08-16T10:00:00Z"
    assert stages["developer"]["detail"] == "Latest developer admission: released. Latest driver outcome: skipped."


def test_operations_route_returns_dashboard_contract(client: TestClient) -> None:
    result = client.get("/api/operations")

    assert result.status_code == 200
    payload = result.json()
    assert "stages" in payload
    assert "next_action" in payload
    assert "proposals" in payload


def test_proposal_route_loads_one_explicitly_selected_proposal(client: TestClient, source: DataSource) -> None:
    source.data_dir = source.briefs_dir.parent / "data"
    outbox = source.data_dir / "companion-outbox"
    outbox.mkdir(parents=True, mode=0o700)
    proposal = outbox / "2026-08-16-example-task-management.md"
    proposal.write_text("# Companion proposal\n", encoding="utf-8")

    result = client.get(f"/api/proposals/{proposal.name}")

    assert result.status_code == 200
    assert result.json()["markdown"] == "# Companion proposal\n"
    assert result.json()["approvable"] is False


def test_data_source_lists_current_and_invalid_proposals(source: DataSource) -> None:
    source.data_dir = source.briefs_dir.parent / "data"
    outbox = source.data_dir / "companion-outbox"
    outbox.mkdir(parents=True, mode=0o700)
    (outbox / "current.proposal.json").write_text(
        '{"command_argv":["gtask","complete","task-1","--confirm"],'
        '"created_at":"2026-09-10T12:00:00+00:00","item_id":"0123456789abcdef",'
        '"markdown":"# Current\\n","version":1}',
        encoding="utf-8",
    )
    (outbox / "invalid.proposal.json").write_text("{}", encoding="utf-8")

    result = source.list_proposals()

    assert len(result) == 2
    by_name = {proposal.name: proposal for proposal in result}
    assert by_name["current.proposal.json"].approvable is True
    assert by_name["current.proposal.json"].validation_error is None
    assert by_name["invalid.proposal.json"].approvable is False
    assert by_name["invalid.proposal.json"].validation_error is not None


@pytest.mark.parametrize("name", ["../secrets.md", "proposal.txt"])
def test_proposal_route_rejects_non_outbox_paths(client: TestClient, name: str) -> None:
    assert client.get(f"/api/proposals/{name}").status_code == 404


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_status_route_reports_availability(client: TestClient, source: DataSource) -> None:
    result = client.get("/api/status")
    assert result.status_code == 200
    assert result.json()["ok"] is True
    assert result.json()["has_brief"] is False


def test_brief_route_404_when_absent(client: TestClient) -> None:
    result = client.get("/api/brief")
    assert result.status_code == 404
    assert "fieldkit brief" in result.json()["error"]


def test_brief_route_returns_markdown(client: TestClient, source: DataSource) -> None:
    _write_brief(source)
    result = client.get("/api/brief")
    assert result.status_code == 200
    assert result.json()["name"] == "morning-brief-2026-07-11.md"
    assert result.json()["markdown"].startswith("# Morning Brief")


def test_alerts_route_returns_files(client: TestClient, source: DataSource) -> None:
    (source.watchers_dir / "pursuit-stall-alerts.md").write_text("## stalled\n", encoding="utf-8")
    result = client.get("/api/alerts")
    assert result.status_code == 200
    alerts = result.json()["alerts"]
    assert len(alerts) == 1
    assert alerts[0]["name"] == "pursuit-stall-alerts.md"


def test_pipeline_health_route_proxies_cli(client: TestClient) -> None:
    result = client.get("/api/pipeline/health")
    assert result.status_code == 200
    assert result.json()["data"][0]["risk_tier"] == "MEDIUM"


def test_quota_route_proxies_cli(client: TestClient) -> None:
    result = client.get("/api/pipeline/quota")
    assert result.status_code == 200
    assert result.json()["data"]["gap"] == 1600000


def test_cli_failure_maps_to_502(source: DataSource) -> None:
    def broken(args: list[str]) -> Any:
        raise WebDataError("boom")

    source.cli_json = broken
    with TestClient(create_app(source), base_url="http://127.0.0.1") as client:
        result = client.get("/api/pipeline/health")
        assert result.status_code == 502
        assert "boom" in result.json()["error"]


def test_index_serves_pwa_shell(client: TestClient) -> None:
    result = client.get("/")
    assert result.status_code == 200
    assert "fieldkit" in result.text
    assert "Ops" in result.text
    assert "manifest.json" in result.text


def test_manifest_and_sw_served_at_scope_root(client: TestClient) -> None:
    result = client.get("/manifest.json")
    assert result.status_code == 200
    assert result.json()["name"] == "fieldkit"
    sw = client.get("/sw.js")
    assert sw.status_code == 200
    assert sw.headers["content-type"].startswith("application/javascript")
    assert 'addEventListener("fetch"' in sw.text


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_token_guard_rejects_missing_bearer(source: DataSource) -> None:
    with TestClient(create_app(source, token="s3cret")) as client:
        result = client.get("/api/status")
        assert result.status_code == 401


def test_token_guard_rejects_wrong_token(source: DataSource) -> None:
    with TestClient(create_app(source, token="s3cret")) as client:
        result = client.get("/api/status", headers={"Authorization": "Bearer wrong"})
        assert result.status_code == 401


def test_token_guard_accepts_valid_bearer(source: DataSource) -> None:
    with TestClient(create_app(source, token="s3cret")) as client:
        result = client.get("/api/status", headers={"Authorization": "Bearer s3cret"})
        assert result.status_code == 200
        assert result.json()["ok"] is True


def test_token_guard_requires_bearer_header_and_rejects_query_credentials(source: DataSource) -> None:
    with TestClient(create_app(source, token="s3cret")) as client:
        denied = client.get("/events")
        assert denied.status_code == 401
        query = client.get("/api/status", params={"token": "s3cret"})
        assert query.status_code == 401


@pytest.mark.parametrize("path", ["/", "/manifest.json", "/sw.js", "/static/app.js"])
def test_token_guard_leaves_static_open(source: DataSource, path: str) -> None:
    with TestClient(create_app(source, token="s3cret")) as client:
        result = client.get(path)
        assert result.status_code == 200


def test_empty_token_is_rejected_at_app_creation(source: DataSource) -> None:
    with pytest.raises(WebDataError, match="empty"):
        create_app(source, token="   ")


def test_tokenless_mode_rejects_foreign_host_header(source: DataSource) -> None:
    # DNS-rebinding guard: attacker page rebound to 127.0.0.1 sends its own
    # domain in the Host header and must get 403, not deal data.
    with TestClient(create_app(source), base_url="http://evil.example.com") as client:
        result = client.get("/api/status")
        assert result.status_code == 403


def test_tokenless_mode_accepts_loopback_hosts_with_port(source: DataSource) -> None:
    with TestClient(create_app(source), base_url="http://localhost:6096") as client:
        result = client.get("/api/status")
        assert result.status_code == 200


def test_token_mode_sets_no_store_on_api(source: DataSource) -> None:
    """Token mode: /api/* responses carry Cache-Control: no-store (implementation change)."""
    with TestClient(create_app(source, token="s3cret")) as client:
        result = client.get("/api/status", headers={"Authorization": "Bearer s3cret"})
        assert result.status_code == 200
        assert result.headers["Cache-Control"] == "no-store"


def test_tokenless_mode_omits_no_store_on_api(source: DataSource) -> None:
    """Tokenless (loopback) mode: /api/* must NOT set no-store — offline brief cache is fine."""
    with TestClient(create_app(source), base_url="http://localhost:6096") as client:
        result = client.get("/api/status")
        assert result.status_code == 200
        assert result.headers.get("Cache-Control") != "no-store"


def test_token_mode_unauthorized_response_sets_no_store(source: DataSource) -> None:
    """401 responses also carry no-store — they return before the /api/* middleware block runs."""
    with TestClient(create_app(source, token="s3cret")) as client:
        result = client.get("/api/status", headers={"Authorization": "Bearer wrong"})
        assert result.status_code == 401
        assert result.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("token", [None, "s3cret"])
def test_serve_refuses_non_loopback_bind_even_with_token(token: str | None) -> None:
    from fieldkit.web.server import serve

    with pytest.raises(WebDataError, match="loopback"):
        serve(host="0.0.0.0", token=token)


def test_serve_starts_on_loopback() -> None:
    """A supported loopback bind reaches Uvicorn with the requested address."""
    from fieldkit.web.server import serve

    with patch("uvicorn.run") as run:
        serve(host="127.0.0.1", port=8811, token=None)

    assert run.call_args.kwargs["host"] == "127.0.0.1"
    assert run.call_args.kwargs["port"] == 8811


def test_all_responses_prevent_framing(client: TestClient) -> None:
    response = client.get("/")
    assert response.headers["Content-Security-Policy"] == "frame-ancestors 'none'"
    assert response.headers["X-Frame-Options"] == "DENY"


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------


def test_chat_answers_with_injected_synthesize(source: DataSource) -> None:
    from fieldkit.web.chat import answer

    _write_brief(source)
    captured: dict[str, str] = {}

    def fake_synthesize(prompt: str, **kwargs: Any) -> str:
        captured["prompt"] = prompt
        captured["system"] = kwargs.get("system", "")
        return "Focus on acme/big-deal."

    result = answer("what should I do?", source, synthesize_fn=fake_synthesize)
    assert result == "Focus on acme/big-deal."
    assert "Morning Brief" in captured["prompt"]
    assert "acme/big-deal.md" in captured["prompt"]
    assert "what should I do?" in captured["prompt"]
    assert "Never fabricate" in captured["system"]
    assert "untrusted user input" in captured["system"]
    assert "<user_data label='latest_morning_brief'>" in captured["prompt"]
    assert "<user_data label='operator_question'>" in captured["prompt"]


def test_chat_rejects_empty_message(source: DataSource) -> None:
    from fieldkit.web.chat import answer

    with pytest.raises(WebDataError, match="empty"):
        answer("   ", source, synthesize_fn=lambda p, **k: "x")


def test_chat_rejects_oversized_message(source: DataSource) -> None:
    from fieldkit.web.chat import answer

    with pytest.raises(WebDataError, match="exceeds"):
        answer("x" * 5000, source, synthesize_fn=lambda p, **k: "x")


def test_chat_context_degrades_when_cli_fails(tmp_path: Path) -> None:
    from fieldkit.web.chat import build_context

    def broken(args: list[str]) -> Any:
        raise WebDataError("cli down")

    src = DataSource(briefs_dir=tmp_path / "b", watchers_dir=tmp_path / "w", cli_json=broken)
    result = build_context(src)
    assert "unavailable" in result


def test_chat_route_maps_empty_message_to_400(client: TestClient) -> None:
    result = client.post("/api/chat", json={"message": "  "})
    assert result.status_code == 400


def test_chat_route_success_via_injected_synthesize(source: DataSource) -> None:
    app = create_app(source, synthesize_fn=lambda p, **k: "Do the acme follow-up.")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        result = client.post("/api/chat", json={"message": "priorities?"})
        assert result.status_code == 200
        assert result.json()["reply"] == "Do the acme follow-up."


def test_chat_route_accepts_the_maximum_message_length(source: DataSource) -> None:
    app = create_app(source, synthesize_fn=lambda p, **k: "ok")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        result = client.post("/api/chat", json={"message": "x" * 4_000})

    assert result.status_code == 200


def test_chat_route_rejects_history_turns_larger_than_the_context_limit(client: TestClient) -> None:
    response = client.post(
        "/api/chat",
        json={"message": "priorities?", "history": [{"role": "user", "content": "x" * 2_001}]},
    )

    assert response.status_code == 422


def test_chat_route_maps_llm_failure_to_503(source: DataSource) -> None:
    from fieldkit.errors import LLMError

    def broken(prompt: str, **kwargs: Any) -> str:
        raise LLMError("vertex down")

    with TestClient(create_app(source, synthesize_fn=broken), base_url="http://127.0.0.1") as client:
        result = client.post("/api/chat", json={"message": "priorities?"})
        assert result.status_code == 503
        assert "LLM unavailable" in result.json()["error"]


def test_chat_route_rejects_oversized_history_before_prompt_assembly(client: TestClient) -> None:
    response = client.post(
        "/api/chat",
        json={"message": "priorities?", "history": [{"role": "user", "content": "x"}] * 7},
    )
    assert response.status_code == 422


def test_chat_route_rejects_oversized_body(client: TestClient) -> None:
    response = client.post("/api/chat", content=b"x" * 33_000, headers={"content-type": "application/json"})
    assert response.status_code == 413
    assert response.headers["Content-Security-Policy"] == "frame-ancestors 'none'"


def test_chat_history_turns_are_clipped(source: DataSource) -> None:
    from fieldkit.web.chat import answer

    captured: dict[str, str] = {}

    def fake_synthesize(prompt: str, **kwargs: Any) -> str:
        captured["prompt"] = prompt
        return "ok"

    huge_history = [{"role": "user", "content": "x" * 60_000} for _ in range(6)]
    result = answer("q?", source, history=huge_history, synthesize_fn=fake_synthesize)
    assert result == "ok"
    # 6 turns x 60k raw would be 360k chars; clipping caps each at ~2k.
    assert len(captured["prompt"]) < 60_000


# ---------------------------------------------------------------------------
# SSE events
# ---------------------------------------------------------------------------


def _drain(gen: Any, n: int) -> list[str]:
    """Collect up to n frames from the async generator."""

    async def _run() -> list[str]:
        frames = []
        async for frame in gen:
            frames.append(frame)
            if len(frames) >= n:
                break
        return frames

    return asyncio.run(_run())


def test_event_stream_emits_hello_then_alert_on_change(source: DataSource) -> None:
    alert = source.watchers_dir / "pursuit-stall-alerts.md"

    async def _run() -> list[str]:
        frames = []
        gen = alert_event_stream(source, poll_seconds=0.01, max_polls=3)
        async for frame in gen:
            frames.append(frame)
            if len(frames) == 1:
                alert.write_text("## new stall\n", encoding="utf-8")
        return frames

    result = asyncio.run(_run())
    assert result[0].startswith("event: hello")
    alert_frames = [f for f in result if f.startswith("event: alert")]
    assert len(alert_frames) == 1
    payload = json.loads(alert_frames[0].split("data: ", 1)[1].strip())
    assert payload["name"] == "pursuit-stall-alerts.md"


def test_event_stream_pings_when_quiet(source: DataSource) -> None:
    result = _drain(alert_event_stream(source, poll_seconds=0.01, max_polls=2), 3)
    assert result[0].startswith("event: hello")
    assert all(f == ": ping\n\n" for f in result[1:])


# ---------------------------------------------------------------------------
# run_cli_json subprocess contract
# ---------------------------------------------------------------------------


def test_run_cli_json_raises_on_bad_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    from fieldkit.web.data import run_cli_json

    def fake_run(*args: Any, **kwargs: Any) -> Any:
        return subprocess.CompletedProcess(args=args, returncode=2, stdout="", stderr="auth expired")

    monkeypatch.setattr("fieldkit.web.data.subprocess.run", fake_run)
    with pytest.raises(WebDataError, match="exited 2"):
        run_cli_json(["sf", "listview", "--json"])


def test_run_cli_json_accepts_exit_1_with_json(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    from fieldkit.web.data import run_cli_json

    def fake_run(*args: Any, **kwargs: Any) -> Any:
        return subprocess.CompletedProcess(args=args, returncode=1, stdout='[{"risk_tier": "HIGH"}]', stderr="")

    monkeypatch.setattr("fieldkit.web.data.subprocess.run", fake_run)
    result = run_cli_json(["pursuit", "health", "--json"])
    assert result == [{"risk_tier": "HIGH"}]


def test_run_cli_json_raises_on_non_json(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    from fieldkit.web.data import run_cli_json

    def fake_run(*args: Any, **kwargs: Any) -> Any:
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="not json", stderr="")

    monkeypatch.setattr("fieldkit.web.data.subprocess.run", fake_run)
    with pytest.raises(WebDataError, match="non-JSON"):
        run_cli_json(["pursuit", "health", "--json"])


@pytest.mark.parametrize("stdout", ['{"item_id":"ok"}\nnot-json\n', "[]\n"])
def test_run_cli_json_lines_rejects_entire_malformed_feed(monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
    import subprocess

    from fieldkit.web.data import run_cli_json_lines

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="feed diagnostic")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(WebDataError, match=r"invalid JSON-lines.*feed diagnostic"):
        run_cli_json_lines(["companion", "feed", "--json", "--all"])
