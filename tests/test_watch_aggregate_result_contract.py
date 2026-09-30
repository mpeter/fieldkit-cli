"""Aggregate admission and daily suppression never manufacture same-run evidence."""

import importlib
import json
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from unittest.mock import patch

import httpx
import pytest
import yaml
from click.testing import CliRunner, Result

from fieldkit.commands.watch.cli import cli
from fieldkit.config import clear_config_caches
from fieldkit.errors import EmptyOutputError
from fieldkit.watch import (
    backstory_health,
    close_date_countdown,
    contract_expiry,
    draft_queue,
    mcp,
    morning_brief,
    pursuit_stalls,
    slack_threads,
    status,
    waiting_on_tracker,
)
from fieldkit.watch._morning_brief_types import BriefWriteResult
from fieldkit.watch.integration_plan import IntegrationPlan
from fieldkit.watch.status import RunStatusWriteResult, WatcherOutcome, WatcherRunResult, was_run_today
from fieldkit.watch.status import write_run_status as real_write_run_status

pytestmark = pytest.mark.unit
watch_cli = importlib.import_module("fieldkit.commands.watch.cli")


@pytest.mark.parametrize("invalid", [0, 1, 2, 3, -1, 4, False, None, "1"])
def test_aggregate_rejects_bare_process_codes_as_execution_evidence(invalid: object) -> None:
    result = watch_cli._invoke_watcher("waiting-on-tracker", lambda: invalid)

    assert result == WatcherRunResult("fatal", False, None)


@pytest.mark.parametrize("allow_partial", [False, True])
@pytest.mark.parametrize("prior", ["ok", "partial", "fatal", "unknown", None])
def test_daily_suppression_preserves_nonpassing_outcomes_without_dispatch(
    prior: str | None,
    allow_partial: bool,
) -> None:
    entry: dict[str, str] = {"last_run": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if prior is not None:
        entry["outcome"] = prior
    args = ["run", "--all"]
    if allow_partial:
        args.append("--allow-partial")
    with ExitStack() as stack:
        load = stack.enter_context(patch("fieldkit.watch.status._load_run_status", return_value={"run-all": entry}))
        stack.enter_context(patch("fieldkit.watch.status.was_run_today", was_run_today))
        stack.enter_context(
            patch(
                "fieldkit.watch.integration_plan.build_integration_plan",
                return_value=IntegrationPlan((), (), False, False, ()),
            )
        )
        stack.enter_context(patch("fieldkit.watch.preflight.preflight_check", return_value=[]))
        writer = stack.enter_context(patch("fieldkit.watch.status.write_run_status", return_value="written"))
        brief = stack.enter_context(patch("fieldkit.commands.brief.generate._run_generate_inner"))
        domains = [
            stack.enter_context(patch(target))
            for target in (
                "fieldkit.watch.waiting_on_tracker._run",
                "fieldkit.watch.pursuit_stalls._run_pursuit_stalls",
                "fieldkit.watch.close_date_countdown._run_countdown",
                "fieldkit.watch.contract_expiry._run_contract_expiry",
                "fieldkit.watch.backstory_health._run_backstory_health",
                "fieldkit.watch.slack_threads._run_slack_threads",
                "fieldkit.watch.draft_queue._run_draft_queue",
            )
        ]

        result = CliRunner().invoke(cli, args)

    assert result.exit_code == (0 if prior == "ok" else 1)
    load.assert_called_once()
    writer.assert_not_called()
    brief.assert_not_called()
    for domain in domains:
        domain.assert_not_called()


@pytest.mark.parametrize(
    "run,expected",
    [
        (WatcherRunResult("partial", True, "written"), 0),
        (WatcherRunResult("fatal", True, "written"), 1),
        (WatcherRunResult("fatal", True, "failed"), 1),
        (WatcherRunResult("partial", False, "written"), 1),
        (WatcherRunResult("partial", False, None), 1),
        (WatcherRunResult("ok", True, None), 1),
        (WatcherRunResult("ok", True, "skipped"), 1),
        (WatcherRunResult("partial", True, "written", 2), 2),
        (WatcherRunResult("fatal", False, None, 3), 3),
    ],
)
def test_allowance_requires_completed_partial_and_live_status(run: WatcherRunResult, expected: int) -> None:
    admitted = watch_cli._invoke_watcher("waiting-on-tracker", lambda: run)
    assert admitted == status.validate_watcher_result(run, dry_run=False)
    assert watch_cli._run_all_exit_code([admitted], allow_partial=True, dry_run=False) == expected


@pytest.mark.parametrize("allow_partial", [False, True])
def test_partial_preview_never_qualifies_for_allowance(allow_partial: bool) -> None:
    result = watch_cli._invoke_watcher(
        "waiting-on-tracker",
        lambda: WatcherRunResult("partial", True, None),
        dry_run=True,
    )
    assert result == WatcherRunResult("partial", True, None)
    assert watch_cli._run_all_exit_code([result], allow_partial=allow_partial, dry_run=True) == 1


@pytest.mark.parametrize("cleanup_failed", [False, True])
def test_brief_empty_output_is_data_failure_with_sanitized_diagnostic(
    caplog: pytest.LogCaptureFixture,
    cleanup_failed: bool,
) -> None:
    with patch(
        "fieldkit.commands.brief.generate._run_generate_inner",
        side_effect=EmptyOutputError(cleanup_failed=cleanup_failed),
    ):
        result, elapsed = watch_cli._run_brief_step(dry_run=False, no_llm=True)
    assert result == WatcherRunResult("fatal", False, None, 3)
    assert elapsed >= 0
    assert ("empty output cleanup failed" if cleanup_failed else "produced empty output") in caplog.text


@pytest.mark.parametrize("writer_response", ["skipped", "failed", None, True, "WRITTEN"])
def test_aggregate_requires_exact_written_response_before_allowance(writer_response: object) -> None:
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "fieldkit.watch.integration_plan.build_integration_plan",
                return_value=IntegrationPlan((), (), False, False, ()),
            )
        )
        stack.enter_context(patch("fieldkit.watch.preflight.preflight_check", return_value=[]))
        writer = stack.enter_context(patch("fieldkit.watch.status.write_run_status", return_value=writer_response))
        for target in (
            "fieldkit.watch.waiting_on_tracker._run",
            "fieldkit.watch.pursuit_stalls._run_pursuit_stalls",
            "fieldkit.watch.close_date_countdown._run_countdown",
            "fieldkit.watch.contract_expiry._run_contract_expiry",
            "fieldkit.commands.brief.generate._run_generate_inner",
        ):
            stack.enter_context(patch(target, return_value=WatcherRunResult("partial", True, "written")))
        result = CliRunner().invoke(cli, ["run", "--all", "--force", "--allow-partial"])

    assert result.exit_code == 1
    assert "run status was not persisted" in result.output
    assert writer.call_args.kwargs["outcome"] == "partial"


def test_public_aggregate_slack_preview_omits_provider_and_all_runtime_writes(
    documented_workspace: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fieldkit.__main__ import main

    before = {
        path.relative_to(documented_workspace): path.read_bytes()
        for path in documented_workspace.rglob("*")
        if path.is_file()
    }
    with (
        patch(
            "fieldkit.watch.integration_plan.build_integration_plan",
            return_value=IntegrationPlan((), ("slack-threads",), False, False, ()),
        ),
        patch("fieldkit.watch.slack_threads._run_slack_threads") as slack,
        patch("fieldkit.watch.status.write_run_status") as writer,
        patch("fieldkit.commands.watch.cli._run_preflight_guard") as preflight,
        patch("subprocess.run") as subprocess_run,
        patch("fieldkit.watch.waiting_on_tracker._run", return_value=WatcherRunResult("ok", True, None)),
        patch("fieldkit.watch.pursuit_stalls._run_pursuit_stalls", return_value=WatcherRunResult("ok", True, None)),
        patch("fieldkit.watch.close_date_countdown._run_countdown", return_value=WatcherRunResult("ok", True, None)),
        patch("fieldkit.watch.contract_expiry._run_contract_expiry", return_value=WatcherRunResult("ok", True, None)),
        patch("fieldkit.commands.brief.generate._run_generate_inner", return_value=WatcherRunResult("ok", True, None)),
    ):
        result = main(["watch", "run", "--all", "--dry-run", "--slack"])

    assert result == 0
    slack.assert_not_called()
    writer.assert_not_called()
    preflight.assert_not_called()
    subprocess_run.assert_not_called()
    assert "Slack selected; provider scan not run in aggregate preview" in capsys.readouterr().out
    assert {
        path.relative_to(documented_workspace): path.read_bytes()
        for path in documented_workspace.rglob("*")
        if path.is_file()
    } == before


@pytest.mark.parametrize("fault", [None, "leaf-status", "aggregate-status", "state"])
@pytest.mark.parametrize("allow_partial", [False, True])
def test_real_completed_partial_and_persistence_faults(
    documented_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str | None,
    allow_partial: bool,
) -> None:
    """A real mixed-record scan and exact replacement failures govern allowance."""
    today = datetime.now(UTC).date()
    pursuits = documented_workspace / "accounts/acme-corp/pursuits"
    (pursuits / "valid.md").write_text(
        f"---\nstage: qualify\nsf_close_date: {today}\n---\nBody\n",
        encoding="utf-8",
    )
    (pursuits / "broken.md").write_text("---\nstage: [\n---\n", encoding="utf-8")
    monkeypatch.setattr(status, "get_fieldkit_home", lambda: documented_workspace)
    monkeypatch.setattr(status, "write_run_status", real_write_run_status)
    monkeypatch.setattr(close_date_countdown, "write_run_status", real_write_run_status)
    original_replace = Path.replace
    replacements = 0

    def replace(path: Path, target: str | Path) -> Path:
        nonlocal replacements
        destination = Path(target)
        if destination.name == "watcher-run-status.json":
            replacements += 1
            if (fault == "leaf-status" and replacements == 1) or (fault == "aggregate-status" and replacements == 2):
                raise OSError("fictional storage failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", replace)
    if fault == "state":
        monkeypatch.setattr(
            close_date_countdown, "_save_state", lambda *args, **kwargs: (_ for _ in ()).throw(OSError())
        )
    leaf_results: list[WatcherRunResult] = []
    real_countdown = close_date_countdown._run_countdown

    def countdown(**kwargs: object) -> WatcherRunResult:
        result = real_countdown(
            threshold_red=14,
            threshold_yellow=30,
            threshold_green=60,
            account_filter=None,
            dry_run=False,
            as_json=False,
        )
        leaf_results.append(result)
        return result

    args = ["run", "--all", "--force"] + (["--allow-partial"] if allow_partial else [])
    with (
        patch(
            "fieldkit.watch.integration_plan.build_integration_plan",
            return_value=IntegrationPlan((), (), False, False, ()),
        ),
        patch("fieldkit.watch.preflight.preflight_check", return_value=[]),
        patch("fieldkit.watch.waiting_on_tracker._run", return_value=WatcherRunResult("ok", False, None)),
        patch("fieldkit.watch.pursuit_stalls._run_pursuit_stalls", return_value=WatcherRunResult("ok", False, None)),
        patch("fieldkit.watch.close_date_countdown._run_countdown", side_effect=countdown),
        patch("fieldkit.watch.contract_expiry._run_contract_expiry", return_value=WatcherRunResult("ok", False, None)),
        patch("fieldkit.commands.brief.generate._run_generate_inner", return_value=WatcherRunResult("ok", False, None)),
    ):
        result = CliRunner().invoke(cli, args)

    assert result.exit_code == (0 if fault is None and allow_partial else 1)
    assert leaf_results == [
        WatcherRunResult(
            "fatal" if fault in ("leaf-status", "state") else "partial",
            True,
            "failed" if fault == "leaf-status" else "written",
        )
    ]
    saved = json.loads((documented_workspace / "watchers/watcher-run-status.json").read_text(encoding="utf-8"))
    if fault == "aggregate-status":
        assert "run-all" not in saved
    else:
        assert saved["run-all"]["outcome"] == ("fatal" if fault in ("leaf-status", "state") else "partial")
    if fault == "leaf-status":
        assert "close-date-countdown" not in saved
    else:
        assert saved["close-date-countdown"]["records_checked"] == 1


_REAL_LEAVES = (
    "waiting-on-tracker",
    "pursuit-stalls",
    "close-date-countdown",
    "contract-expiry",
    "backstory-health",
    "slack-threads",
    "draft-queue",
    "morning-brief",
)


@dataclass(frozen=True)
class _StatusAttempt:
    watcher: str
    outcome: WatcherOutcome
    records_checked: int
    alerts_generated: int
    failures: int
    result: RunStatusWriteResult


@dataclass
class _AggregateRehearsal:
    home: Path
    fault_leaf: str | None = None
    fault: Literal["status", "state", "artifact"] = "status"
    provider_mode: Literal["ok", "backstory-failed", "backstory-mixed", "draft-malformed", "slack-provider"] = "ok"
    active_leaf: str | None = None
    active_status: str | None = None
    order: list[str] = field(default_factory=list)
    runs: dict[str, WatcherRunResult] = field(default_factory=dict)
    attempts: list[_StatusAttempt] = field(default_factory=list)
    replacements: list[Path] = field(default_factory=list)
    failed_replacements: list[Path] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)
    slack_searches: int = 0
    slack: slack_threads.SlackRunOutcome | None = None
    brief: BriefWriteResult | None = None

    @property
    def status_path(self) -> Path:
        return self.home / "watchers/watcher-run-status.json"

    @property
    def brief_path(self) -> Path:
        return self.home / "briefs" / f"morning-brief-{datetime.now(UTC).date()}.md"

    def attempt(self, watcher: str) -> _StatusAttempt:
        return next(attempt for attempt in self.attempts if attempt.watcher == watcher)

    def saved(self) -> dict[str, dict[str, object]]:
        document: object = json.loads(self.status_path.read_text(encoding="utf-8"))
        assert isinstance(document, dict)
        saved: dict[str, dict[str, object]] = {}
        for key, entry in document.items():
            assert isinstance(key, str)
            assert isinstance(entry, dict)
            assert all(isinstance(field, str) for field in entry)
            saved[key] = entry
        return saved


@pytest.fixture
def aggregate_rehearsal(documented_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> _AggregateRehearsal:
    """Keep actual scans and persistence; replace only external business responses."""
    edge = _AggregateRehearsal(documented_workspace)
    today = datetime.now(UTC).date()
    (edge.home / "TASKS.md").write_text(
        f"# Tasks\n\n## Waiting On\n\n- Vendor reply ({today})\n\n## Done\n",
        encoding="utf-8",
    )
    (edge.home / "accounts/acme-corp/pursuits/valid.md").write_text(
        f"---\nstage: qualify\nlast-transition: {today}\nsf_close_date: {today + timedelta(days=45)}\n---\nBody\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FIELDKIT_USER_EMAIL", "seller@example.com")
    monkeypatch.setenv("SLACK_USERNAME", "fictional-seller")
    monkeypatch.setattr(status, "get_fieldkit_home", lambda: edge.home)
    for module in (backstory_health, draft_queue):
        monkeypatch.setattr(module, "get_mcp_endpoint", lambda _name: "https://provider.example.com/mcp")
    monkeypatch.setattr(
        "fieldkit.watch.integration_plan.build_integration_plan",
        lambda **_kwargs: IntegrationPlan((), ("backstory-health", "slack-threads", "draft-queue"), False, False, ()),
    )
    monkeypatch.setattr("fieldkit.watch.preflight.preflight_check", lambda *_args, **_kwargs: [])

    def write_status(
        *,
        watcher: str,
        outcome: WatcherOutcome,
        records_checked: int,
        alerts_generated: int,
        failures: int,
        elapsed_seconds: float,
        dry_run: bool,
    ) -> RunStatusWriteResult:
        edge.active_status = watcher
        try:
            result = real_write_run_status(
                watcher=watcher,
                outcome=outcome,
                records_checked=records_checked,
                alerts_generated=alerts_generated,
                failures=failures,
                elapsed_seconds=elapsed_seconds,
                dry_run=dry_run,
            )
        finally:
            edge.active_status = None
        edge.attempts.append(_StatusAttempt(watcher, outcome, records_checked, alerts_generated, failures, result))
        return result

    for module in (
        status,
        waiting_on_tracker,
        pursuit_stalls,
        close_date_countdown,
        contract_expiry,
        backstory_health,
        slack_threads,
        draft_queue,
        morning_brief,
    ):
        monkeypatch.setattr(module, "write_run_status", write_status)

    original_replace = Path.replace

    def replace(source: Path, target: str | Path) -> Path:
        destination = Path(target)
        edge.replacements.append(destination)
        selected_status = (
            edge.fault == "status" and edge.active_status == edge.fault_leaf and destination == edge.status_path
        )
        selected_state = (
            edge.fault == "state" and edge.active_leaf == edge.fault_leaf and destination.name.endswith("-state.json")
        )
        selected_artifact = (
            edge.fault == "artifact"
            and edge.active_leaf == edge.fault_leaf
            and destination in (edge.brief_path, edge.home / "watchers/draft-queue-alerts.md")
        )
        if edge.fault_leaf is not None and (selected_status or selected_state or selected_artifact):
            edge.failed_replacements.append(destination)
            raise OSError("Synthetic selected publication failure")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", replace)

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        method = payload["method"]
        edge.methods.append(method)
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
        if tool == "backstory__find_account":
            failed = edge.provider_mode == "backstory-failed" or (
                edge.provider_mode == "backstory-mixed"
                and payload["params"]["arguments"]["account_name"] == "broken-corp"
            )
            data: object = {} if failed else {"peopleai_account_id": 1, "opportunities": [{"engagement_level": 80}]}
        else:
            data = (
                {"messages": [{"subject": "Missing identity"}]}
                if edge.provider_mode == "draft-malformed"
                else {
                    "messages": [
                        {"id": "fictional-draft", "subject": "Follow up", "to": "contact@acme-corp.example.com"}
                    ],
                }
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

    def slack_search(_cmd: list[str]) -> tuple[int, bytes, bytes]:
        edge.slack_searches += 1
        if edge.provider_mode == "slack-provider":
            return 1, b"", b"Synthetic provider failure"
        return 0, b'{"query":"fictional","total":0,"matches":[]}', b""

    monkeypatch.setattr(slack_threads, "_run_slack_search_once", slack_search)

    def record(name: str, run: Callable[..., object]) -> Callable[..., object]:
        def observed(**kwargs: object) -> object:
            edge.order.append(name)
            edge.active_leaf = name
            try:
                result = run(**kwargs)
            finally:
                edge.active_leaf = None
            if isinstance(result, slack_threads.SlackRunOutcome):
                edge.slack = result
                edge.runs[name] = result.run
            else:
                assert isinstance(result, WatcherRunResult)
                edge.runs[name] = result
            return result

        return observed

    import fieldkit.brief.merged as brief_merged
    import fieldkit.commands.brief.generate as brief_generate

    for name, module, attr in (
        ("waiting-on-tracker", waiting_on_tracker, "_run"),
        ("pursuit-stalls", pursuit_stalls, "_run_pursuit_stalls"),
        ("close-date-countdown", close_date_countdown, "_run_countdown"),
        ("contract-expiry", contract_expiry, "_run_contract_expiry"),
        ("backstory-health", backstory_health, "_run_backstory_health"),
        ("slack-threads", slack_threads, "_run_slack_threads"),
        ("draft-queue", draft_queue, "_run_draft_queue"),
        ("morning-brief", brief_generate, "_run_generate_inner"),
    ):
        monkeypatch.setattr(module, attr, record(name, getattr(module, attr)))

    real_publish: Callable[..., BriefWriteResult] = morning_brief._write_brief_to_disk

    def publish(*args: object, **kwargs: object) -> BriefWriteResult:
        # The helper's own result carries actual publication counts; no projection is invented.
        result = real_publish(*args, **kwargs)
        edge.brief = result
        return result

    monkeypatch.setattr(brief_merged, "_write_brief_to_disk", publish)
    return edge


def _invoke_real_aggregate(*, allow_partial: bool = True, dry_run: bool = False, force: bool = True) -> Result:
    args = ["run", "--all", "--slack"]
    if allow_partial:
        args.append("--allow-partial")
    if dry_run:
        args.append("--dry-run")
    if force:
        args.append("--force")
    return CliRunner().invoke(cli, args)


@pytest.mark.integration
@pytest.mark.parametrize("leaf", _REAL_LEAVES)
@pytest.mark.parametrize("allow_partial", [False, True])
def test_real_leaf_status_failure_survives_later_successful_aggregate_write(
    aggregate_rehearsal: _AggregateRehearsal,
    leaf: str,
    allow_partial: bool,
) -> None:
    edge = aggregate_rehearsal
    assert (
        real_write_run_status(
            watcher=leaf,
            outcome="ok",
            records_checked=99,
            alerts_generated=0,
            failures=0,
            elapsed_seconds=0.1,
            dry_run=False,
        )
        == "written"
    )
    prior = edge.saved()[leaf]
    edge.fault_leaf = leaf
    edge.brief_path.parent.mkdir(parents=True)
    edge.brief_path.write_text("Prior report\n", encoding="utf-8")

    result = _invoke_real_aggregate(allow_partial=allow_partial)

    assert result.exit_code == 1, result.output
    assert edge.runs[leaf] == WatcherRunResult("fatal", True, "failed")
    assert edge.order == list(_REAL_LEAVES)
    attempt = edge.attempt(leaf)
    assert attempt.result == "failed"
    assert attempt.outcome == "ok"
    assert attempt.records_checked == (7 if leaf == "morning-brief" else 1)
    assert attempt.alerts_generated == int(
        leaf in {"close-date-countdown", "contract-expiry", "draft-queue", "morning-brief"}
    )
    assert attempt.failures == 0
    assert edge.failed_replacements == [edge.status_path]
    assert edge.attempt("run-all").result == "written"
    saved = edge.saved()
    assert saved[leaf] == prior
    assert saved["run-all"]["outcome"] == "fatal"
    assert saved["run-all"]["records_checked"] == 7
    assert saved["run-all"]["failures"] == 1
    assert edge.brief is not None
    assert edge.brief.written is True
    assert edge.brief_path.read_text(encoding="utf-8").strip()
    assert edge.brief_path.read_text(encoding="utf-8") != "Prior report\n"
    if leaf == "morning-brief":
        assert edge.brief.failures == 1
        assert edge.brief.records_checked == 7
    if leaf == "slack-threads":
        assert edge.slack is not None
        assert edge.slack.failures == 1
        assert edge.slack.records_checked == 1


@pytest.mark.integration
@pytest.mark.parametrize("allow_partial", [False, True])
def test_real_clean_chain_persists_every_completed_leaf(
    aggregate_rehearsal: _AggregateRehearsal, allow_partial: bool
) -> None:
    edge = aggregate_rehearsal
    result = _invoke_real_aggregate(allow_partial=allow_partial)

    assert result.exit_code == 0, result.output
    assert edge.runs == {leaf: WatcherRunResult("ok", True, "written") for leaf in _REAL_LEAVES}
    assert edge.order == list(_REAL_LEAVES)
    assert all(attempt.result == "written" and attempt.failures == 0 for attempt in edge.attempts)
    assert edge.saved()["run-all"]["outcome"] == "ok"
    assert edge.brief is not None and edge.brief.written


@pytest.mark.integration
@pytest.mark.parametrize("all_failed", [False, True])
@pytest.mark.parametrize("allow_partial", [False, True])
def test_real_malformed_and_mixed_local_scans_govern_allowance(
    aggregate_rehearsal: _AggregateRehearsal,
    all_failed: bool,
    allow_partial: bool,
) -> None:
    edge = aggregate_rehearsal
    pursuits = edge.home / "accounts/acme-corp/pursuits"
    if all_failed:
        (pursuits / "valid.md").unlink()
    (pursuits / "broken.md").write_text("---\nstage: [\n---\n", encoding="utf-8")

    result = _invoke_real_aggregate(allow_partial=allow_partial)

    assert result.exit_code == (0 if allow_partial and not all_failed else 1), result.output
    for leaf in ("pursuit-stalls", "close-date-countdown", "contract-expiry"):
        assert edge.runs[leaf] == WatcherRunResult("fatal" if all_failed else "partial", True, "written")
        attempt = edge.attempt(leaf)
        assert attempt.records_checked == int(not all_failed)
        assert attempt.failures == 1
    assert edge.saved()["run-all"]["outcome"] == ("fatal" if all_failed else "partial")
    assert edge.order == list(_REAL_LEAVES)
    assert edge.brief is not None and edge.brief.written


@pytest.mark.integration
@pytest.mark.parametrize(
    "mode,leaf,completed,checked",
    [
        ("backstory-failed", "backstory-health", True, 0),
        ("draft-malformed", "draft-queue", False, 0),
        ("slack-provider", "slack-threads", False, 0),
    ],
)
def test_real_provider_and_malformed_business_response_never_qualify_for_allowance(
    aggregate_rehearsal: _AggregateRehearsal,
    mode: Literal["backstory-failed", "draft-malformed", "slack-provider"],
    leaf: str,
    completed: bool,
    checked: int,
) -> None:
    edge = aggregate_rehearsal
    edge.provider_mode = mode
    result = _invoke_real_aggregate()

    assert result.exit_code == 1, result.output
    assert edge.runs[leaf] == WatcherRunResult("fatal", completed, "written")
    assert edge.attempt(leaf).records_checked == checked
    assert edge.attempt(leaf).failures == 1
    assert edge.saved()["run-all"]["outcome"] == "fatal"
    assert edge.order == list(_REAL_LEAVES)
    assert edge.brief is not None and edge.brief.written


@pytest.mark.integration
@pytest.mark.parametrize("allow_partial", [False, True])
def test_real_completed_backstory_partial_is_eligible_only_after_all_required_writes(
    aggregate_rehearsal: _AggregateRehearsal,
    allow_partial: bool,
) -> None:
    edge = aggregate_rehearsal
    edge.provider_mode = "backstory-mixed"
    accounts_path = edge.home / "config/accounts.yaml"
    config = yaml.safe_load(accounts_path.read_text(encoding="utf-8"))
    config["accounts"]["broken-corp"] = {"domains": ["broken-corp.example"]}
    accounts_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    (edge.home / "accounts/broken-corp/pursuits").mkdir(parents=True)
    clear_config_caches()

    result = _invoke_real_aggregate(allow_partial=allow_partial)

    assert result.exit_code == (0 if allow_partial else 1), result.output
    assert edge.runs["backstory-health"] == WatcherRunResult("partial", True, "written")
    assert edge.attempt("backstory-health").records_checked == 1
    assert edge.attempt("backstory-health").failures == 1
    assert edge.saved()["run-all"]["outcome"] == "partial"
    assert edge.order == list(_REAL_LEAVES)
    assert edge.brief is not None and edge.brief.written


@pytest.mark.integration
@pytest.mark.parametrize("leaf", _REAL_LEAVES[:6])
def test_real_required_state_failure_cannot_be_allowed_after_later_chain_success(
    aggregate_rehearsal: _AggregateRehearsal,
    leaf: str,
) -> None:
    edge = aggregate_rehearsal
    edge.fault_leaf = leaf
    edge.fault = "state"

    result = _invoke_real_aggregate()

    assert result.exit_code == 1, result.output
    assert edge.runs[leaf] == WatcherRunResult("fatal", True, "written")
    assert edge.attempt(leaf).records_checked == 1
    assert edge.attempt(leaf).failures == 1
    assert len(edge.failed_replacements) == 1
    assert edge.failed_replacements[0].name.endswith("-state.json")
    assert edge.attempt("run-all").result == "written"
    assert edge.saved()["run-all"]["outcome"] == "fatal"
    assert edge.order == list(_REAL_LEAVES)
    assert edge.brief is not None and edge.brief.written


@pytest.mark.integration
@pytest.mark.parametrize("leaf", ["draft-queue", "morning-brief"])
def test_real_required_artifact_failure_preserves_prior_artifact_without_allowance(
    aggregate_rehearsal: _AggregateRehearsal,
    leaf: str,
) -> None:
    edge = aggregate_rehearsal
    edge.fault_leaf = leaf
    edge.fault = "artifact"
    target = edge.brief_path if leaf == "morning-brief" else edge.home / "watchers/draft-queue-alerts.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("Prior artifact\n", encoding="utf-8")

    result = _invoke_real_aggregate()

    assert result.exit_code == 1, result.output
    assert edge.runs[leaf] == WatcherRunResult("fatal", leaf != "morning-brief", "written")
    assert edge.attempt(leaf).records_checked == (7 if leaf == "morning-brief" else 1)
    assert edge.attempt(leaf).failures == 1
    assert edge.failed_replacements == [target]
    assert target.read_text(encoding="utf-8") == "Prior artifact\n"
    assert edge.saved()["run-all"]["outcome"] == "fatal"
    assert edge.order == list(_REAL_LEAVES)
    assert edge.brief is not None
    assert edge.brief.written is (leaf != "morning-brief")
    if leaf == "morning-brief":
        assert edge.brief.alerts_generated == 0
        assert edge.brief.failures == 1


@pytest.mark.integration
@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("allow_partial", [False, True])
def test_real_preview_preserves_files_and_never_allows_unpersisted_partial_work(
    aggregate_rehearsal: _AggregateRehearsal,
    partial: bool,
    allow_partial: bool,
) -> None:
    edge = aggregate_rehearsal
    if partial:
        (edge.home / "accounts/acme-corp/pursuits/broken.md").write_text("---\nstage: [\n---\n", encoding="utf-8")
    before = {path.relative_to(edge.home): path.read_bytes() for path in edge.home.rglob("*") if path.is_file()}

    result = _invoke_real_aggregate(dry_run=True, allow_partial=allow_partial)

    assert result.exit_code == int(partial), result.output
    assert edge.runs["pursuit-stalls"] == WatcherRunResult("partial" if partial else "ok", True, "skipped")
    assert edge.runs["backstory-health"] == WatcherRunResult("ok", True, None)
    assert edge.runs["draft-queue"] == WatcherRunResult("ok", True, "skipped")
    assert edge.runs["morning-brief"].status_write is None
    assert edge.runs["morning-brief"].completed is True
    assert "slack-threads" not in edge.runs
    assert edge.methods == []
    assert edge.slack_searches == 0
    assert edge.replacements == []
    assert edge.brief is None
    assert {path.relative_to(edge.home): path.read_bytes() for path in edge.home.rglob("*") if path.is_file()} == before


@pytest.mark.integration
@pytest.mark.parametrize("prior", ["partial", "fatal"])
@pytest.mark.parametrize("allow_partial", [False, True])
def test_real_daily_nonpassing_suppression_has_no_new_completion_or_writes(
    aggregate_rehearsal: _AggregateRehearsal,
    prior: WatcherOutcome,
    allow_partial: bool,
) -> None:
    edge = aggregate_rehearsal
    assert (
        real_write_run_status(
            watcher="run-all",
            outcome=prior,
            records_checked=7,
            alerts_generated=0,
            failures=1,
            elapsed_seconds=0.1,
            dry_run=False,
        )
        == "written"
    )
    before = edge.status_path.read_bytes()
    edge.replacements.clear()

    result = _invoke_real_aggregate(force=False, allow_partial=allow_partial)

    assert result.exit_code == 1, result.output
    assert edge.runs == {}
    assert edge.order == []
    assert edge.attempts == []
    assert edge.methods == []
    assert edge.slack_searches == 0
    assert edge.replacements == []
    assert edge.status_path.read_bytes() == before
