"""Whole-page watcher guide inventory and bounded real execution evidence."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path

import httpx
import pytest
import yaml

from fieldkit.config import clear_config_caches
from fieldkit.ingest.router import route_by_domains
from fieldkit.util.bounded_process import BoundedProcessBytesResult
from fieldkit.watch import (
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
from fieldkit.watch.status import write_run_status as real_write_run_status
from scripts.documentation_commands import DOCUMENT_COMMANDS
from tests.documentation_workflow_support import invoke_workflow, snapshot_workflow

pytestmark = pytest.mark.integration
PAGE = Path("docs/guides/watchers.md")
OWNER = "watcher_guide_contract"
WATCHER_GUIDE_NODES = (
    "tests/test_watcher_guide_contract.py",
    "tests/test_watch_aggregate_result_contract.py",
    "tests/test_watcher_documentation_scenarios.py",
    "tests/test_optional_integration_plan.py::test_base_plan_selects_only_local_work",
    "tests/test_optional_integration_plan.py::test_selected_services_and_watchers_have_one_ordered_plan",
    "tests/test_optional_integration_plan.py::test_disabled_llm_wins_over_configured_model",
    "tests/test_watch_backstory_health.py::test_run_backstory_health_dry_run_does_not_read_accounts_or_provider",
    "tests/test_watch_backstory_health.py::test_compute_health_score_rejects_invalid_numeric_values",
    "tests/test_watch_backstory_health.py::test_check_account_missing_scorable_opportunities_records_failure_without_alert_or_state",
    "tests/test_watch_backstory_health.py::test_check_account_rejects_malformed_provider_shape",
    "tests/test_watch_backstory_health.py::test_malformed_backstory_result_preserves_existing_state_without_write",
    "tests/test_watch_backstory_health.py::test_scan_result_reports_total_fatal_or_completed_partial",
    "tests/test_watch_slack_threads.py::test_decoded_search_envelope_fails_closed",
    "tests/test_watch_slack_threads.py::test_valid_zero_match_envelope_is_success",
    "tests/test_watch_slack_threads.py::test_successful_producer_zero_match_empty_stdout_is_success",
    "tests/test_watch_slack_threads.py::test_unsuccessful_empty_stdout_cannot_manufacture_zero_match_success",
    "tests/test_watch_slack_threads.py::test_decoded_provider_error_is_incomplete_at_domain_and_real_cli",
    "tests/test_draft_queue.py::test_scoped_draft_scan_preserves_global_snapshot",
    "tests/test_draft_queue.py::test_run_counts_only_published_draft_alerts",
    "tests/test_watcher_status.py::test_classify_watcher_outcome_preserves_partial_failures",
)


@dataclass(frozen=True)
class GuideBlock:
    kind: str
    text: str
    inline: tuple[str, ...]
    links: tuple[tuple[str, str], ...]


def semantic_blocks(text: str) -> tuple[GuideBlock, ...]:
    """Keep every nonblank block, including unknown syntax, in document order."""
    result = []
    for index, body in enumerate(re.split(r"\n\s*\n", text.strip())):
        kind = (
            "frontmatter"
            if index == 0 and body.startswith("---\n")
            else "fence"
            if body.startswith("```")
            else "heading"
            if body.startswith("#")
            else "list"
            if body.startswith("- ")
            else "paragraph"
        )
        result.append(
            GuideBlock(
                kind,
                body,
                tuple(re.findall(r"(?<!`)\`([^`\n]+)\`(?!`)", body)),
                tuple(re.findall(r"\[([^\]]+)\]\(([^)]+)\)", body)),
            )
        )
    return tuple(result)


# This literal inventory must be reviewed with behavioral evidence when prose changes.
EXPECTED_TEXT: tuple[str, ...] = (
    "---\nlast_reviewed: 2026-09-28\ncovers:\n  - src/fieldkit/commands/watch/\n  - src/fieldkit/watch/\naudience: user\n---",
    "# Run watchers",
    "Watchers inspect configured workspace or integration data and record conditions\nthat may need attention. Each watcher has its own prerequisites. You do not need\nto configure every watcher, and organization-provided data sources are not part\nof the portable-core guarantee.",
    "## Discover available watchers",
    "```console\nfieldkit watch run --help\n```",
    "The current command lists watchers for pursuit stalls, close-date countdowns,\ncontract expiry, waiting-on items, draft queues, Slack threads, and optional\naccount-health data. Read a watcher's help before its first run:",
    "```console\nfieldkit watch run pursuit-stalls --help\n```",
    "## Preview a single watcher",
    "Where a watcher supports `--dry-run`, use it before writing alert or state files:",
    "```console\nfieldkit watch run pursuit-stalls --dry-run\n```",
    "This preview does not write pursuit-stall alerts or watcher state. A direct\noptional-provider preview, such as `backstory-health --dry-run`, also does not\nread credentials or contact the provider; it reports that provider input was not\nrun. A direct Slack preview still reads Slack and only suppresses runtime writes;\nuse an aggregate preview for offline inspection. Individual local watcher help\nremains authoritative for any diagnostic-log effects.",
    "The thread watcher requires a separately installed and authenticated `slackcli`.\nIts tested search-output contract is\n[`slackcli 0.13.0`](https://github.com/shaharia-lab/slackcli/blob/0f81d64270239616a185c4f6e5d3f57116ebeeb1/src/commands/search.ts):\nJSON contains a nonnegative integer `total` and a list of message mappings in\n`matches`; a successful zero-match search can return empty stdout instead.\nFailed processes and malformed JSON remain nonpassing. Other client versions\nare unverified, and installing a fieldkit profile neither installs this client\nnor grants Slack access.",
    "Then run without `--dry-run` when the reported scope is correct. Use only\nwatchers whose workspace fields and external services you have configured.",
    "Backstory health requires at least one finite engagement score from `0` through\n`100` for each checked account. An empty or malformed opportunity result is a\nprovider-data failure: fieldkit writes neither a fictional zero-score alert nor\nhealth state for that account. A mixed run can be partial; a run with no\nsuccessfully processed accounts is fatal.",
    "`fieldkit watch run draft-queue --account SLUG` reports the scoped count in its\nrun outcome, but does not replace `draft-queue-alerts.md`; that file is the\nall-account daily snapshot used by the morning brief. Run draft queue without\n`--account` to update the snapshot.",
    "## Preview or run the configured set",
    "```console\nfieldkit watch run --all --dry-run\n```",
    "After reviewing the preview, run the configured set or explicitly add Slack:",
    "```console\nfieldkit watch run --all\nfieldkit watch run --all --slack\n```",
    "The aggregate command always runs the local waiting, pursuit-stall,\nclose-date, and contract-expiry watchers. It adds Backstory and draft-queue only\nwhen their explicit endpoints are configured, and adds Slack only with\n`--slack`. Its summary lists every optional input that was not run. It then\ngenerates a morning brief, using local no-LLM rendering unless a model is\nexplicitly configured. `--dry-run` performs no credential preflight or provider\nrequest. When Slack is selected for an aggregate preview, the summary explicitly\nreports that its provider scan was not run; this is not a completed Slack scan.",
    "A once-per-day guard suppresses duplicate live aggregate dispatch after selected\ncredential preflight succeeds. A prior same-day `ok` returns success; a prior\n`partial`, `fatal`, or unrecognized outcome returns `1` without new work or\nstatus publication. `--allow-partial` does not accept a suppressed run. Use\n`--force` for a fresh live attempt; dry runs bypass the guard.",
    "`--allow-partial` accepts only a live completed partial pass whose failed steps\ncompleted and successfully published their status during that invocation. The\naggregate must also successfully publish its own `partial` status. Fatal,\ninterrupted, authentication, data, and persistence failures remain nonpassing.\nPartial previews remain nonpassing, and an allowed pass is still recorded as\n`partial`, not `ok`.",
    "## Inspect outcomes",
    "```console\nfieldkit watch status\nfieldkit watch status --json\nfieldkit watch logs --list\nfieldkit watch logs pursuit-stalls --tail 100\n```",
    "Outcomes have distinct meanings:",
    "- `ok`: all selected records completed without a recorded failure;\n- `partial`: some selected records failed, so inspect the log and decide whether\n  retrying is safe; and\n- `fatal`: the requested work could not complete safely, or required alert,\n  state, report, or status publication failed.",
    "Expected exclusions, such as terminal pursuits or an account intentionally\nmarked internal, are not failures.",
    "## Schedule only after a clean manual run",
    "Preview the exact user crontab entry without reading or changing the host\ncrontab:",
    "```console\nfieldkit watch run --all --install-cron --cron-time '0 6 * * *' --dry-run\n```",
    "After a clean manual `fieldkit watch run --all`, install that reviewed entry:",
    "```console\nfieldkit watch run --all --install-cron --cron-time '0 6 * * *'\n```",
    "Verify `fieldkit watch status`, and confirm that the scheduler inherits the\nrequired environment and credential access.\nPlatform scheduling behavior is outside the portable-core support contract.",
    "The generated [CLI reference](../cli-reference.md#fieldkit-watch) documents each\nwatcher and its exact options.",
)
EXPECTED_IDS = (
    "structure-0",
    "structure-1",
    "prerequisites",
    "structure-3",
    "structure-4",
    "discovery",
    "structure-6",
    "structure-7",
    "preview-first",
    "structure-9",
    "preview-boundaries",
    "slack-producer",
    "configured-live",
    "backstory-data",
    "draft-snapshot",
    "structure-15",
    "structure-16",
    "aggregate-live",
    "structure-18",
    "aggregate-selection",
    "daily-guard",
    "partial-admission",
    "structure-22",
    "structure-23",
    "outcome-introduction",
    "outcome-values",
    "expected-exclusions",
    "structure-27",
    "cron-preview",
    "structure-29",
    "cron-live",
    "structure-31",
    "scheduler-qualification",
    "cli-navigation",
)


def assert_guide(text: str) -> tuple[GuideBlock, ...]:
    actual = semantic_blocks(text)
    expected = semantic_blocks("\n\n".join(EXPECTED_TEXT))
    assert actual == expected, "watcher guide semantic inventory changed"
    return actual


def test_complete_ordered_watcher_guide_inventory() -> None:
    blocks = assert_guide(PAGE.read_text(encoding="utf-8"))
    assert len(blocks) == len(EXPECTED_IDS) == 34
    assert len(set(EXPECTED_IDS)) == len(EXPECTED_IDS)
    assert sum(block.kind == "fence" for block in blocks) == 8


@pytest.mark.parametrize("index", range(len(EXPECTED_TEXT)), ids=EXPECTED_IDS)
@pytest.mark.parametrize("mutation", ("insert", "remove", "alter", "negate", "duplicate", "reorder"))
def test_every_semantic_block_mutation_is_rejected(index: int, mutation: str) -> None:
    blocks = list(EXPECTED_TEXT)
    if mutation == "insert":
        blocks.insert(index, "Unknown unsupported watcher promise.")
    elif mutation == "remove":
        blocks.pop(index)
    elif mutation == "alter":
        blocks[index] += " changed"
    elif mutation == "negate":
        blocks[index] = "Not " + blocks[index]
    elif mutation == "duplicate":
        blocks.insert(index, blocks[index])
    else:
        other = (index + 1) % len(blocks)
        blocks[index], blocks[other] = blocks[other], blocks[index]
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide("\n\n".join(blocks))


@pytest.mark.parametrize("index", [4, 6, 9, 16, 18, 23, 29, 31])
@pytest.mark.parametrize("mutation", ("language", "body"))
def test_every_fence_language_and_body_mutation_is_rejected(index: int, mutation: str) -> None:
    blocks = list(EXPECTED_TEXT)
    blocks[index] = (
        blocks[index].replace("console", "bash", 1)
        if mutation == "language"
        else blocks[index].replace("fieldkit", "unsupported", 1)
    )
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide("\n\n".join(blocks))


@pytest.mark.parametrize("index", [11, 33])
def test_every_link_target_mutation_is_rejected(index: int) -> None:
    blocks = list(EXPECTED_TEXT)
    blocks[index] = re.sub(r"\]\([^)]+\)", "](unknown-target.md)", blocks[index])
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide("\n\n".join(blocks))


@pytest.mark.parametrize(
    "index,inline",
    [
        (index, inline)
        for index, block in enumerate(semantic_blocks("\n\n".join(EXPECTED_TEXT)))
        for inline in block.inline
    ],
)
def test_every_inline_command_or_value_mutation_is_rejected(index: int, inline: str) -> None:
    blocks = list(EXPECTED_TEXT)
    blocks[index] = blocks[index].replace(f"`{inline}`", "`unsupported-value`", 1)
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide("\n\n".join(blocks))


@pytest.mark.parametrize("item", ["- `ok`:", "- `partial`:", "- `fatal`:"])
def test_each_outcome_list_item_mutation_is_rejected(item: str) -> None:
    blocks = list(EXPECTED_TEXT)
    blocks[25] = blocks[25].replace(item, "- unsupported:", 1)
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide("\n\n".join(blocks))


def test_navigation_links_and_independent_fence_owners() -> None:
    blocks = assert_guide(PAGE.read_text(encoding="utf-8"))
    assert blocks[11].links == (
        (
            "`slackcli 0.13.0`",
            "https://github.com/shaharia-lab/slackcli/blob/0f81d64270239616a185c4f6e5d3f57116ebeeb1/src/commands/search.ts",
        ),
    )
    assert blocks[33].links == (("CLI reference", "../cli-reference.md#fieldkit-watch"),)
    target = PAGE.parent / "../cli-reference.md"
    assert target.is_file()
    assert "fieldkit watch" in target.read_text(encoding="utf-8").lower()
    contract = json.loads(Path("docs/documentation-contract.json").read_text(encoding="utf-8"))
    records = contract["documents"][PAGE.as_posix()]["fenced_blocks"]
    assert tuple(record["verification_id"] for record in records) == (
        "automated.installed-base-artifact",
        "automated.installed-base-artifact",
        "automated.watcher-workflow-scenarios",
        "automated.watcher-workflow-scenarios",
        "manual.credentialed-integration",
        "automated.installed-base-artifact",
        "automated.watcher-workflow-scenarios",
        "manual.credentialed-integration",
    )


def test_canonical_owner_executes_fixed_manifest() -> None:
    commands = DOCUMENT_COMMANDS.get(OWNER)
    assert commands is not None, "whole-page owner is not registered"
    assert len(commands) == 1
    argv = commands[0]
    assert argv[:3] == ("uv", "run", "pytest")
    assert argv[3:] == (*WATCHER_GUIDE_NODES, "-q", "-n", "0")


@pytest.fixture
def real_local_workspace(documented_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    today = datetime.now(UTC).date()
    (documented_workspace / "TASKS.md").write_text("# Tasks\n\n## Waiting On\n\n## Done\n", encoding="utf-8")
    (documented_workspace / "accounts/acme-corp/pursuits/valid.md").write_text(
        f"---\nstage: qualify\nlast-transition: {today}\nsf_close_date: {today + timedelta(days=45)}\n---\nBody\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(status, "get_fieldkit_home", lambda: documented_workspace)
    monkeypatch.setattr("fieldkit.ingest.router.get_fieldkit_home", lambda: documented_workspace)
    for module in (
        status,
        waiting_on_tracker,
        pursuit_stalls,
        close_date_countdown,
        contract_expiry,
        draft_queue,
        morning_brief,
        slack_threads,
    ):
        monkeypatch.setattr(module, "write_run_status", real_write_run_status)
    yield documented_workspace


def forbidden_boundary(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("unselected provider or credential boundary invoked")


@pytest.mark.parametrize("detached_parent_module", [False, True])
def test_real_unconfigured_aggregate_runs_four_locals_and_nonempty_no_llm_brief(
    real_local_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    detached_parent_module: bool,
) -> None:
    llm_core = import_module("fieldkit.llm.core")
    if detached_parent_module:
        monkeypatch.delattr(import_module("fieldkit.llm"), "core", raising=False)
    monkeypatch.setattr("fieldkit.watch.preflight._check_llm_available", forbidden_boundary)
    monkeypatch.setattr("fieldkit.watch.preflight._check_gmail_token", forbidden_boundary)
    monkeypatch.setattr("fieldkit.watch.preflight._check_sf_session", forbidden_boundary)
    monkeypatch.setattr(llm_core, "synthesize", forbidden_boundary)
    monkeypatch.setattr(import_module("fieldkit.pipeline.main"), "synthesize", forbidden_boundary)
    monkeypatch.setattr("fieldkit.watch.mcp.MCPSession.initialize", forbidden_boundary)
    monkeypatch.setattr(slack_threads, "run_bounded_process_bytes", forbidden_boundary)
    result = invoke_workflow(["watch", "run", "--all", "--force"])
    assert result.exit_code == 0, result.output
    saved = json.loads((real_local_workspace / "watchers/watcher-run-status.json").read_text(encoding="utf-8"))
    assert set(saved) == {
        "waiting-on-tracker",
        "pursuit-stalls",
        "close-date-countdown",
        "contract-expiry",
        "morning-brief",
        "run-all",
    }
    assert all(entry["outcome"] == "ok" for entry in saved.values())
    briefs = list(real_local_workspace.rglob("*brief*.md"))
    assert any(path.read_text(encoding="utf-8").strip() for path in briefs)
    for omitted in ("backstory-health", "draft-queue", "slack-threads", "calendar", "llm"):
        assert omitted in result.output


def test_real_direct_slack_preview_reads_raw_transport_without_writes(
    real_local_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def transport(argv: list[str], **_kwargs: object) -> BoundedProcessBytesResult:
        calls.append(argv)
        return BoundedProcessBytesResult(0, b'{"total":0,"matches":[]}', b"")

    monkeypatch.setattr(slack_threads, "run_bounded_process_bytes", transport)
    before = snapshot_workflow(real_local_workspace.parent)
    result = invoke_workflow(["watch", "run", "slack-threads", "--dry-run", "--json"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0][:2] == ["slackcli", "search"]
    assert json.loads(result.stdout)["failures"] == 0
    assert snapshot_workflow(real_local_workspace.parent) == before


def test_public_scoped_draft_adapter_preserves_snapshot_and_brief_consumes_global(
    real_local_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIELDKIT_USER_EMAIL", "seller@example.com")
    config_file = real_local_workspace / "config/accounts.yaml"
    config = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    config["accounts"]["beta-corp"] = {"domains": ["beta-corp.example.com"]}
    config_file.write_text(yaml.safe_dump(config), encoding="utf-8")
    clear_config_caches()
    monkeypatch.setattr(draft_queue, "get_mcp_endpoint", lambda _name: "https://provider.example.com/mcp")
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        method = payload["method"]
        requests.append(method)
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
        assert payload["params"]["name"] == "google_workspace__search_gmail_messages"
        data = {
            "messages": [
                {"id": "draft-acme", "subject": "Acme planning", "to": "contact@acme-corp.example.com"},
                {"id": "draft-beta", "subject": "Beta planning", "to": "contact@beta-corp.example.com"},
            ]
        }
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
    routed = route_by_domains(["contact@acme-corp.example.com"])
    assert routed.accounts == ["acme-corp"]
    global_run = invoke_workflow(["watch", "run", "draft-queue", "--json"])
    assert global_run.exit_code == 0, global_run.output
    assert json.loads(global_run.stdout)["alerts_generated"] == 2
    alerts = real_local_workspace / "watchers/draft-queue-alerts.md"
    before = (alerts.stat().st_mtime_ns, alerts.read_bytes())
    scoped_run = invoke_workflow(["watch", "run", "draft-queue", "--account", "acme-corp", "--json"])
    assert scoped_run.exit_code == 0, scoped_run.output
    assert json.loads(scoped_run.stdout)["records_checked"] == 1
    assert json.loads(scoped_run.stdout)["alerts_generated"] == 0
    assert (alerts.stat().st_mtime_ns, alerts.read_bytes()) == before
    collected = morning_brief._collect_alert_source(alerts, datetime.now(UTC).date(), "Draft Queue")
    assert isinstance(collected, list)
    assert "Acme planning" in "\n".join(collected)
    assert "Beta planning" in "\n".join(collected)
    assert requests == ["initialize", "notifications/initialized", "tools/call"] * 2


def test_real_terminal_and_internal_exclusions_have_zero_failures(
    real_local_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pursuit = real_local_workspace / "accounts/acme-corp/pursuits/valid.md"
    pursuit.write_text("---\nstage: closed-won\nlast-transition: 2020-01-01\n---\nDone\n", encoding="utf-8")
    terminal = invoke_workflow(["watch", "run", "pursuit-stalls", "--force"])
    assert terminal.exit_code == 0, terminal.output
    config_file = real_local_workspace / "config/accounts.yaml"
    config = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    config["accounts"]["acme-corp"]["internal"] = True
    config["accounts"]["beta-corp"] = {"domains": ["beta-corp.example.com"]}
    config_file.write_text(yaml.safe_dump(config), encoding="utf-8")
    clear_config_caches()
    searches: list[list[str]] = []

    def transport(argv: list[str], **_kwargs: object) -> BoundedProcessBytesResult:
        searches.append(argv)
        return BoundedProcessBytesResult(0, b'{"total":0,"matches":[]}', b"")

    monkeypatch.setattr(slack_threads, "run_bounded_process_bytes", transport)
    internal = invoke_workflow(["watch", "run", "slack-threads", "--json"])
    assert internal.exit_code == 0, internal.output
    assert json.loads(internal.stdout)["failures"] == 0
    assert len(searches) == 1
    assert "beta" in " ".join(searches[0])
    assert "acme" not in " ".join(searches[0])
    saved = json.loads((real_local_workspace / "watchers/watcher-run-status.json").read_text(encoding="utf-8"))
    assert saved["pursuit-stalls"]["failures"] == 0
    assert saved["slack-threads"]["failures"] == 0


@pytest.mark.parametrize("authenticated", [False, True])
def test_real_same_day_record_requires_selected_preflight_before_suppression(
    real_local_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    authenticated: bool,
) -> None:
    real_write_run_status(
        watcher="run-all",
        outcome="ok",
        records_checked=0,
        alerts_generated=0,
        failures=0,
        elapsed_seconds=0,
        dry_run=False,
    )
    monkeypatch.delenv("FIELDKIT_NO_LLM", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setattr("fieldkit.watch.integration_plan.get_llm_model", lambda: "vertex_ai/fictional-model")
    if authenticated:
        # The real preflight checks only this configured ADC location's presence in the environment.
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/fictional/unused-adc.json")
    for module, attr in (
        (waiting_on_tracker, "_run"),
        (pursuit_stalls, "_run_pursuit_stalls"),
        (close_date_countdown, "_run_countdown"),
        (contract_expiry, "_run_contract_expiry"),
    ):
        monkeypatch.setattr(module, attr, forbidden_boundary)
    before = snapshot_workflow(real_local_workspace.parent)
    result = invoke_workflow(["watch", "run", "--all"])
    assert result.exit_code == (0 if authenticated else 2), result.output
    assert snapshot_workflow(real_local_workspace.parent) == before


@dataclass(frozen=True)
class GuideClaim:
    identifier: str
    text: str
    nodes: tuple[str, ...]
    qualifier: str


CLAIMS = (
    GuideClaim(
        EXPECTED_IDS[2],
        EXPECTED_TEXT[2],
        ("tests/test_optional_integration_plan.py::test_base_plan_selects_only_local_work",),
        "Local configuration selection only; organization data remains outside portable core.",
    ),
    GuideClaim(
        EXPECTED_IDS[5],
        EXPECTED_TEXT[5],
        ("tests/test_watcher_guide_contract.py::test_real_local_help_and_outcome_inspection",),
        "Checkout command discovery; installed artifact fences retain separate ownership.",
    ),
    GuideClaim(
        EXPECTED_IDS[8],
        EXPECTED_TEXT[8],
        ("tests/test_watcher_documentation_scenarios.py::test_documented_watcher_previews_are_offline_and_read_only",),
        "Published supported previews only.",
    ),
    GuideClaim(
        EXPECTED_IDS[10],
        EXPECTED_TEXT[10],
        (
            "tests/test_watcher_documentation_scenarios.py::test_documented_watcher_previews_are_offline_and_read_only",
            "tests/test_watch_backstory_health.py::test_run_backstory_health_dry_run_does_not_read_accounts_or_provider",
            "tests/test_watcher_guide_contract.py::test_real_direct_slack_preview_reads_raw_transport_without_writes",
        ),
        "Synthetic Slack transport; local watcher help qualifies diagnostic log effects.",
    ),
    GuideClaim(
        EXPECTED_IDS[11],
        EXPECTED_TEXT[11],
        (
            "tests/test_watch_slack_threads.py::test_decoded_search_envelope_fails_closed",
            "tests/test_watch_slack_threads.py::test_valid_zero_match_envelope_is_success",
            "tests/test_watch_slack_threads.py::test_successful_producer_zero_match_empty_stdout_is_success",
            "tests/test_watch_slack_threads.py::test_unsuccessful_empty_stdout_cannot_manufacture_zero_match_success",
            "tests/test_watch_slack_threads.py::test_decoded_provider_error_is_incomplete_at_domain_and_real_cli",
        ),
        "Pinned producer envelope only; installation, authentication and other versions unverified.",
    ),
    GuideClaim(
        EXPECTED_IDS[12],
        EXPECTED_TEXT[12],
        ("tests/test_optional_integration_plan.py::test_selected_services_and_watchers_have_one_ordered_plan",),
        "Configuration selection; live credentialed fence remains manual.",
    ),
    GuideClaim(
        EXPECTED_IDS[13],
        EXPECTED_TEXT[13],
        (
            "tests/test_watch_backstory_health.py::test_compute_health_score_rejects_invalid_numeric_values",
            "tests/test_watch_backstory_health.py::test_check_account_missing_scorable_opportunities_records_failure_without_alert_or_state",
            "tests/test_watch_backstory_health.py::test_check_account_rejects_malformed_provider_shape",
            "tests/test_watch_backstory_health.py::test_malformed_backstory_result_preserves_existing_state_without_write",
            "tests/test_watch_backstory_health.py::test_scan_result_reports_total_fatal_or_completed_partial",
        ),
        "Finite synthetic provider records; no real account data.",
    ),
    GuideClaim(
        EXPECTED_IDS[14],
        EXPECTED_TEXT[14],
        (
            "tests/test_watcher_guide_contract.py::test_public_scoped_draft_adapter_preserves_snapshot_and_brief_consumes_global",
        ),
        "Synthetic raw HTTP and fictional account routing; real artifact and collector.",
    ),
    GuideClaim(
        EXPECTED_IDS[17],
        EXPECTED_TEXT[17],
        ("tests/test_optional_integration_plan.py::test_selected_services_and_watchers_have_one_ordered_plan",),
        "Selected inputs only; live credentialed fence remains manual.",
    ),
    GuideClaim(
        EXPECTED_IDS[19],
        EXPECTED_TEXT[19],
        (
            "tests/test_watcher_guide_contract.py::test_real_unconfigured_aggregate_runs_four_locals_and_nonempty_no_llm_brief",
            "tests/test_optional_integration_plan.py::test_selected_services_and_watchers_have_one_ordered_plan",
            "tests/test_optional_integration_plan.py::test_disabled_llm_wins_over_configured_model",
            "tests/test_watcher_documentation_scenarios.py::test_documented_watcher_previews_are_offline_and_read_only",
            "tests/test_watch_aggregate_result_contract.py::test_public_aggregate_slack_preview_omits_provider_and_all_runtime_writes",
        ),
        "Local no-LLM run and integration selection; optional provider execution uses separate synthetic evidence.",
    ),
    GuideClaim(
        EXPECTED_IDS[20],
        EXPECTED_TEXT[20],
        (
            "tests/test_watcher_guide_contract.py::test_real_same_day_record_requires_selected_preflight_before_suppression",
            "tests/test_watch_aggregate_result_contract.py::test_real_daily_nonpassing_suppression_has_no_new_completion_or_writes",
        ),
        "Real daily record; local ADC selection only, no actual provider authentication.",
    ),
    GuideClaim(
        EXPECTED_IDS[21],
        EXPECTED_TEXT[21],
        (
            "tests/test_watch_aggregate_result_contract.py::test_real_completed_partial_and_persistence_faults",
            "tests/test_watch_aggregate_result_contract.py::test_real_leaf_status_failure_survives_later_successful_aggregate_write",
            "tests/test_watch_aggregate_result_contract.py::test_real_provider_and_malformed_business_response_never_qualify_for_allowance",
            "tests/test_watch_aggregate_result_contract.py::test_real_preview_preserves_files_and_never_allows_unpersisted_partial_work",
        ),
        "Real completion and publication with bounded synthetic failure injection; no live providers.",
    ),
    GuideClaim(
        EXPECTED_IDS[24],
        EXPECTED_TEXT[24],
        ("tests/test_watcher_status.py::test_classify_watcher_outcome_preserves_partial_failures",),
        "Outcome classification only.",
    ),
    GuideClaim(
        EXPECTED_IDS[25],
        EXPECTED_TEXT[25],
        (
            "tests/test_watcher_status.py::test_classify_watcher_outcome_preserves_partial_failures",
            "tests/test_watch_aggregate_result_contract.py::test_real_clean_chain_persists_every_completed_leaf",
            "tests/test_watch_aggregate_result_contract.py::test_real_malformed_and_mixed_local_scans_govern_allowance",
            "tests/test_watch_aggregate_result_contract.py::test_real_required_artifact_failure_preserves_prior_artifact_without_allowance",
        ),
        "Finite success, partial and publication-failure records.",
    ),
    GuideClaim(
        EXPECTED_IDS[26],
        EXPECTED_TEXT[26],
        ("tests/test_watcher_guide_contract.py::test_real_terminal_and_internal_exclusions_have_zero_failures",),
        "Terminal pursuit and explicitly internal Slack account; no general skip guarantee.",
    ),
    GuideClaim(
        EXPECTED_IDS[28],
        EXPECTED_TEXT[28],
        (
            "tests/test_watcher_documentation_scenarios.py::test_documented_cron_preview_uses_fixed_binary_without_subprocess_or_writes",
        ),
        "Preview only; host crontab untouched.",
    ),
    GuideClaim(
        EXPECTED_IDS[30],
        EXPECTED_TEXT[30],
        (
            "tests/test_watcher_documentation_scenarios.py::test_documented_cron_preview_uses_fixed_binary_without_subprocess_or_writes",
        ),
        "Reviewed preview syntax only; live installation stays manual and pending.",
    ),
    GuideClaim(
        EXPECTED_IDS[32],
        EXPECTED_TEXT[32],
        ("tests/test_watcher_guide_contract.py::test_real_local_help_and_outcome_inspection",),
        "Status inspection only; scheduler environment and platform operation remain manual qualifications.",
    ),
    GuideClaim(
        EXPECTED_IDS[33],
        EXPECTED_TEXT[33],
        ("tests/test_watcher_guide_contract.py::test_navigation_links_and_independent_fence_owners",),
        "Local navigation target; installed CLI reference requires independent artifact evidence.",
    ),
)


def test_every_prose_claim_has_bounded_existing_execution_nodes() -> None:
    blocks = assert_guide(PAGE.read_text(encoding="utf-8"))
    prose = tuple(index for index, block in enumerate(blocks) if block.kind in ("paragraph", "list"))
    assert tuple(claim.text for claim in CLAIMS) == tuple(EXPECTED_TEXT[index] for index in prose)
    assert tuple(claim.identifier for claim in CLAIMS) == tuple(EXPECTED_IDS[index] for index in prose)
    for claim in CLAIMS:
        assert claim.qualifier and claim.nodes
        for node in claim.nodes:
            filename, function = node.split("::")
            assert filename in WATCHER_GUIDE_NODES or node in WATCHER_GUIDE_NODES
            assert f"def {function}(" in Path(filename).read_text(encoding="utf-8")


def test_real_local_help_and_outcome_inspection(real_local_workspace: Path) -> None:
    help_result = invoke_workflow(["watch", "run", "--help"])
    assert help_result.exit_code == 0, help_result.output
    for watcher in (
        "pursuit-stalls",
        "close-date-countdown",
        "contract-expiry",
        "waiting-on-tracker",
        "draft-queue",
        "slack-threads",
        "backstory-health",
    ):
        assert watcher in help_result.stdout
    before = snapshot_workflow(real_local_workspace.parent)
    status_result = invoke_workflow(["watch", "status", "--json"])
    assert status_result.exit_code == 0, status_result.output
    assert json.loads(status_result.stdout) == {"count": 0, "filters": {}, "items": []}
    logs_result = invoke_workflow(["watch", "logs", "--list"])
    assert logs_result.exit_code == 0, logs_result.output
    assert snapshot_workflow(real_local_workspace.parent) == before
