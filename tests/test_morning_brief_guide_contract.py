"""Whole-page morning brief inventory and real offline boundary scenarios."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest

from fieldkit.config import get_mcp_endpoint
from fieldkit.watch import morning_brief, status
from fieldkit.watch.status import write_run_status as real_write_run_status
from scripts.documentation_commands import DOCUMENT_COMMANDS
from tests.documentation_workflow_support import (
    invoke_workflow,
    snapshot_workflow,
    write_documented_pursuit_data,
    write_second_documented_account,
)

pytestmark = pytest.mark.integration
PAGE = Path("docs/guides/morning-brief.md")
OWNER = "morning_brief_guide_contract"
MORNING_BRIEF_GUIDE_NODES = (
    "tests/test_morning_brief_guide_contract.py",
    "tests/test_brief_documentation_scenarios.py",
    "tests/test_brief_optional_integrations.py",
    "tests/test_brief_workspace_required.py",
    "tests/test_brief_account_scope.py",
    "tests/test_saved_report_viewers.py",
    "tests/test_brief_cli_open.py",
    "tests/test_morning_brief_inner.py::test_run_generate_inner_dry_run_overrides_selected_llm",
    "tests/test_morning_brief_inner.py::test_preview_retains_actual_source_failure_outcome_without_writes",
    "tests/test_morning_brief_inner.py::test_run_generate_inner_labels_unconfigured_optional_alert_inputs",
    "tests/test_morning_brief_write_result.py::test_calendar_auth_preserves_report_and_propagates_through_generator",
    "tests/test_morning_brief_write_result.py::test_pipeline_user_action_errors_propagate_without_publication",
    "tests/test_morning_brief_write_result.py::test_written_artifact_retains_source_failure_status",
    "tests/test_morning_brief_write_result.py::test_generator_json_reports_actual_publication_on_partial_exit",
    "tests/test_morning_brief.py::test_calendar_not_configured_is_explicitly_not_run",
    "tests/test_morning_brief.py::test_collect_tasks_reads_from_data_root",
    "tests/test_morning_brief.py::test_collect_tasks_does_not_read_from_fieldkit_root",
    "tests/test_morning_brief_collect.py::test_collect_decay_signals_returns_sentinel_when_no_gmail_db",
)


@dataclass(frozen=True)
class GuideBlock:
    kind: str
    text: str
    inline: tuple[str, ...]
    links: tuple[tuple[str, str], ...]


def semantic_blocks(text: str) -> tuple[GuideBlock, ...]:
    """Retain every nonblank block, including unsupported syntax, in order."""
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
                tuple(re.findall(r"(?<!`)`([^`\n]+)`(?!`)", body)),
                tuple(re.findall(r"\[([^\]]+)\]\(([^)]+)\)", body)),
            )
        )
    return tuple(result)


@dataclass(frozen=True)
class GuideClaim:
    identifier: str
    classification: Literal["structure", "guidance", "behavior", "navigation", "manual"]
    evidence: str
    text: str


# Literal page-specific review inventory; prose changes require renewed evidence.
INVENTORY = (
    GuideClaim(
        "metadata",
        "structure",
        "page-layout",
        "---\nlast_reviewed: 2026-09-29\ncovers:\n  - src/fieldkit/brief/\n  - src/fieldkit/commands/brief/\n  - src/fieldkit/watch/\naudience: user\n---",
    ),
    GuideClaim("title", "structure", "page-layout", "# Generate a morning brief"),
    GuideClaim(
        "available-inputs",
        "behavior",
        "brief_optional_integrations",
        "The morning brief combines available watcher output, meetings, and pipeline\ncontext into Markdown. Its contents depend on the services and workspace data\nyou have configured; an optional section being unavailable does not imply that\nthe portable core is broken.",
    ),
    GuideClaim("preview-heading", "structure", "page-layout", "## Preview without writing a file"),
    GuideClaim("diagnostics-first", "guidance", "operator-guidance", "Run diagnostics first:"),
    GuideClaim("doctor-command", "behavior", "automated.installed-base-artifact", "```console\nfieldkit doctor\n```"),
    GuideClaim("stdout-guidance", "guidance", "operator-guidance", "Then generate to standard output:"),
    GuideClaim(
        "preview-command",
        "behavior",
        "brief_documentation_scenarios",
        "```console\nfieldkit brief generate --dry-run\n```",
    ),
    GuideClaim(
        "preview-boundaries",
        "behavior",
        "brief_documentation_scenarios+morning_brief_inner",
        "`--dry-run` prevents the brief file from being written and does not contact the\nconfigured calendar or model provider. The rendered brief marks those inputs as\nnot run for the preview.",
    ),
    GuideClaim(
        "account-boundaries",
        "behavior",
        "real-two-account-preview+brief_account_scope",
        "To scope account-specific pipeline and project-health inputs to one configured\naccount, use `--account`. Existing shared watcher snapshots and global\ncross-account signals can still include other accounts:",
    ),
    GuideClaim(
        "account-command",
        "behavior",
        "brief_documentation_scenarios",
        "```console\nfieldkit brief generate --dry-run --account acme-corp\n```",
    ),
    GuideClaim("pipeline-heading", "structure", "page-layout", "## Use local pipeline context only"),
    GuideClaim(
        "pipeline-boundaries",
        "behavior",
        "brief_documentation_scenarios",
        "When watcher aggregation or calendar access is not wanted, use the narrower\npipeline path. Add `--no-llm` to avoid model synthesis:",
    ),
    GuideClaim(
        "pipeline-command",
        "behavior",
        "brief_documentation_scenarios",
        "```console\nfieldkit brief generate --pipeline-only --no-llm --dry-run\n```",
    ),
    GuideClaim(
        "local-cache-prerequisites",
        "behavior",
        "morning_brief_tasks+decay-no-gmail",
        "This is the safest way to inspect brief rendering from local pursuit, task, and\ncached signal data. Whether a specific cache exists still depends on prior use.",
    ),
    GuideClaim(
        "workspace-required",
        "behavior",
        "brief_workspace_required",
        "The pipeline path requires a configured workspace, including with `--dry-run`.\nIf configuration is missing or invalid, it exits `3` before collecting data or\ncalling the model and directs you to `fieldkit init`. It never substitutes the\napplication directory for the workspace.",
    ),
    GuideClaim("write-heading", "structure", "page-layout", "## Write the brief"),
    GuideClaim("write-guidance", "guidance", "operator-guidance", "Remove `--dry-run` after reviewing the preview:"),
    GuideClaim(
        "write-command", "manual", "manual.credentialed-integration", "```console\nfieldkit brief generate\n```"
    ),
    GuideClaim(
        "publication-result",
        "behavior",
        "morning_brief_write_result",
        "The default command writes a dated Markdown file below the configured\n`<fieldkit_home>/briefs/` directory. Use `--json` when a caller needs the\ngeneration result as machine-readable output.",
    ),
    GuideClaim(
        "optional-provider-selection",
        "behavior",
        "brief_optional_integrations+calendar-not-configured",
        "Without `llm_model`, the command uses the local deterministic pipeline renderer.\nWhen `mcp_endpoints.calendar` is absent, the calendar section says it was not\nconfigured and independent local sections still render. Configuring either\ncapability explicitly selects its provider checks for a non-dry run.",
    ),
    GuideClaim("degraded-heading", "structure", "page-layout", "## Interpret missing or degraded sections"),
    GuideClaim(
        "missing-degraded-and-exits",
        "behavior",
        "real-disabled-endpoint-cache+morning_brief_write_result+morning_brief_inner",
        "- A disabled optional watcher with no corresponding cached output produces a\n  not-run message. Existing cached output remains visible. Configure its stated\n  endpoint, or select Slack explicitly, before running the named watcher.\n- An unavailable configured calendar or other provider is reported as degraded\n  while independent sections continue where possible.\n- An authentication error needs user action and exits `2`.\n- A partial run may exit `1`; inspect the named source and retry only when the\n  failure is transient.",
    ),
    GuideClaim(
        "diagnostic-guidance",
        "guidance",
        "operator-guidance",
        "Use `fieldkit watch status`, `fieldkit watch logs --list`, and the relevant\n`fieldkit doctor <service>` command before changing configuration.",
    ),
    GuideClaim("options-heading", "structure", "page-layout", "## Other useful options"),
    GuideClaim(
        "date-verbose-commands",
        "behavior",
        "brief_documentation_scenarios",
        "```console\nfieldkit brief generate --date 2026-09-10 --dry-run\nfieldkit brief generate --verbose --dry-run\n```",
    ),
    GuideClaim(
        "help-boundaries",
        "behavior",
        "brief_documentation_scenarios",
        "List the current options without reading configuration or contacting a provider:",
    ),
    GuideClaim(
        "help-command", "behavior", "brief_documentation_scenarios", "```console\nfieldkit brief generate --help\n```"
    ),
    GuideClaim(
        "cli-navigation",
        "navigation",
        "local-link-and-anchor",
        "The generated [CLI reference](../cli-reference.md#fieldkit-brief-generate) is\nauthoritative for current option spelling.",
    ),
    GuideClaim("saved-heading", "structure", "page-layout", "## Inspect a saved brief"),
    GuideClaim(
        "selection-guidance",
        "guidance",
        "operator-guidance",
        "Select the newest saved brief without launching a viewer:",
    ),
    GuideClaim(
        "selection-command",
        "behavior",
        "brief_documentation_scenarios",
        "```console\nfieldkit brief open --no-open --json\n```",
    ),
    GuideClaim(
        "saved-validation",
        "behavior",
        "saved_report_viewers+brief_cli_open",
        "The result identifies the selected file, its age, and `opened: false`. Pipeline\nreviews have the same selection mode: `fieldkit pipeline open --no-open --json`.\nThese commands require a non-empty UTF-8 report of at most 4 MiB in the configured\nworkspace's `briefs/` directory; symlinked report files or that directory are\nrejected with exit `3`.",
    ),
    GuideClaim(
        "viewer-result-and-limit",
        "behavior",
        "saved_report_viewers+brief_cli_open",
        "Without `--no-open`, both commands request the system viewer, including with\n`--json`. `opened: true` means the browser accepted the file URI, not that the\nreport rendered. A declined or failed launch returns `opened: false` and exits\n`1`; open the selected file manually. File validation is not a sandbox against\nanother process replacing files after selection.",
    ),
)
EXPECTED_TEXT = tuple(claim.text for claim in INVENTORY)
EXPECTED_IDS = tuple(claim.identifier for claim in INVENTORY)
EVIDENCE_NODES = {
    "brief_optional_integrations": (MORNING_BRIEF_GUIDE_NODES[2],),
    "brief_documentation_scenarios": (MORNING_BRIEF_GUIDE_NODES[1],),
    "brief_documentation_scenarios+morning_brief_inner": (
        MORNING_BRIEF_GUIDE_NODES[1],
        *MORNING_BRIEF_GUIDE_NODES[7:10],
    ),
    "real-two-account-preview+brief_account_scope": (
        "tests/test_morning_brief_guide_contract.py::test_real_two_account_preview_scopes_inputs_but_preserves_shared_signals",
        MORNING_BRIEF_GUIDE_NODES[4],
    ),
    "morning_brief_tasks+decay-no-gmail": MORNING_BRIEF_GUIDE_NODES[15:18],
    "brief_workspace_required": (MORNING_BRIEF_GUIDE_NODES[3],),
    "morning_brief_write_result": MORNING_BRIEF_GUIDE_NODES[10:14],
    "brief_optional_integrations+calendar-not-configured": (
        MORNING_BRIEF_GUIDE_NODES[2],
        MORNING_BRIEF_GUIDE_NODES[14],
    ),
    "real-disabled-endpoint-cache+morning_brief_write_result+morning_brief_inner": (
        "tests/test_morning_brief_guide_contract.py::test_real_disabled_endpoints_keep_cached_alerts_visible",
        *MORNING_BRIEF_GUIDE_NODES[7:14],
    ),
    "saved_report_viewers+brief_cli_open": MORNING_BRIEF_GUIDE_NODES[5:7],
}


def assert_guide(text: str) -> tuple[GuideBlock, ...]:
    """Reject any unreviewed block or semantic change, including unknown syntax."""
    actual = semantic_blocks(text)
    assert actual == semantic_blocks("\n\n".join(EXPECTED_TEXT)), "morning brief guide semantic inventory changed"
    return actual


def test_complete_ordered_morning_brief_inventory() -> None:
    """Every visible block has one stable ID, classification, and evidence owner."""
    blocks = assert_guide(PAGE.read_text(encoding="utf-8"))
    assert len(blocks) == len(INVENTORY) == len(set(EXPECTED_IDS)) == 34
    assert sum(block.kind == "fence" for block in blocks) == 8
    assert all(claim.evidence for claim in INVENTORY)
    assert {claim.classification for claim in INVENTORY} == {
        "structure",
        "guidance",
        "behavior",
        "navigation",
        "manual",
    }
    assert {claim.evidence for claim in INVENTORY} == set(EVIDENCE_NODES) | {
        "page-layout",
        "operator-guidance",
        "automated.installed-base-artifact",
        "manual.credentialed-integration",
        "local-link-and-anchor",
    }
    assert all(
        node in MORNING_BRIEF_GUIDE_NODES or node.split("::", 1)[0] in MORNING_BRIEF_GUIDE_NODES
        for nodes in EVIDENCE_NODES.values()
        for node in nodes
    )


@pytest.mark.parametrize("index", range(len(INVENTORY)), ids=EXPECTED_IDS)
@pytest.mark.parametrize("mutation", ("insert", "remove", "alter", "negate", "duplicate", "reorder"))
def test_every_semantic_block_mutation_is_rejected(index: int, mutation: str) -> None:
    """A page addition, deletion, changed promise, or reordering needs review."""
    blocks = list(EXPECTED_TEXT)
    if mutation == "insert":
        blocks.insert(index, "Unknown unsupported morning brief promise.")
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


@pytest.mark.parametrize("index", (5, 7, 10, 13, 18, 25, 27, 31))
@pytest.mark.parametrize("mutation", ("language", "body"))
def test_every_fence_language_and_body_mutation_is_rejected(index: int, mutation: str) -> None:
    """Neither an executable command nor its fence language can drift unnoticed."""
    blocks = list(EXPECTED_TEXT)
    blocks[index] = (
        blocks[index].replace("console", "bash", 1)
        if mutation == "language"
        else blocks[index].replace("fieldkit", "unsupported", 1)
    )
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide("\n\n".join(blocks))


@pytest.mark.parametrize("index,label,target", [(28, "CLI reference", "../cli-reference.md#fieldkit-brief-generate")])
@pytest.mark.parametrize("mutation", ("label", "target"))
def test_every_link_mutation_is_rejected(index: int, label: str, target: str, mutation: str) -> None:
    """Link labels and destinations are both part of the reviewed inventory."""
    blocks = list(EXPECTED_TEXT)
    original = f"[{label}]({target})"
    replacement = f"[unsupported]({target})" if mutation == "label" else f"[{label}](unknown-target.md)"
    blocks[index] = blocks[index].replace(original, replacement)
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
    """Inline flags, paths, exit statuses, and commands retain reviewed spelling."""
    blocks = list(EXPECTED_TEXT)
    blocks[index] = blocks[index].replace(f"`{inline}`", "`unsupported-value`", 1)
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide("\n\n".join(blocks))


@pytest.mark.parametrize("item", ("- A disabled", "- An unavailable", "- An authentication", "- A partial"))
def test_each_degraded_list_item_mutation_is_rejected(item: str) -> None:
    """Each missing-source and exit-status qualification is independently protected."""
    blocks = list(EXPECTED_TEXT)
    blocks[22] = blocks[22].replace(item, "- Unsupported", 1)
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide("\n\n".join(blocks))


@pytest.mark.parametrize(
    "index,overclaim",
    (
        (9, "To limit the result to one configured account:"),
        (
            22,
            "- A disabled optional watcher produces a not-run message. Configure its stated\n"
            "  endpoint, or select Slack explicitly, before running the named watcher.\n"
            "- An unavailable configured calendar or other provider is reported as degraded\n"
            "  while independent sections continue where possible.\n"
            "- An authentication error needs user action and exits `2`.\n"
            "- A partial run may exit `1`; inspect the named source and retry only when the\n"
            "  failure is transient.",
        ),
    ),
    ids=("account-isolation-overclaim", "disabled-watcher-cache-overclaim"),
)
def test_previous_scope_and_cache_overclaims_are_rejected(index: int, overclaim: str) -> None:
    """The two original inaccurate promises cannot return through a prose edit."""
    blocks = list(EXPECTED_TEXT)
    blocks[index] = overclaim
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide("\n\n".join(blocks))


def test_navigation_link_and_eight_independent_fence_owners() -> None:
    """Whole-page ownership does not promote manual generation to offline proof."""
    blocks = assert_guide(PAGE.read_text(encoding="utf-8"))
    assert blocks[28].links == (("CLI reference", "../cli-reference.md#fieldkit-brief-generate"),)
    reference = PAGE.parent / "../cli-reference.md"
    assert reference.is_file()
    assert "### `fieldkit brief generate`" in reference.read_text(encoding="utf-8")
    contract = json.loads(Path("docs/documentation-contract.json").read_text(encoding="utf-8"))
    records = contract["documents"][PAGE.as_posix()]["fenced_blocks"]
    assert tuple(record["verification_id"] for record in records) == (
        "automated.installed-base-artifact",
        "automated.brief-workflow-scenarios",
        "automated.brief-workflow-scenarios",
        "automated.brief-workflow-scenarios",
        "manual.credentialed-integration",
        "automated.brief-workflow-scenarios",
        "automated.brief-workflow-scenarios",
        "automated.brief-workflow-scenarios",
    )


def test_canonical_owner_executes_fixed_manifest() -> None:
    """The registered owner must run exactly the finite reviewed evidence set."""
    commands = DOCUMENT_COMMANDS.get(OWNER)
    assert commands is not None, "whole-page owner is not registered"
    assert len(commands) == 1
    assert commands[0] == ("uv", "run", "pytest", *MORNING_BRIEF_GUIDE_NODES, "-q", "-n", "0")


def forbidden_boundary(*_args: object, **_kwargs: object) -> None:
    """Fail on any credential, model, or provider boundary during local previews."""
    raise AssertionError("offline morning brief scenario reached an external boundary")


@pytest.fixture
def isolated_brief_paths(documented_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear the five distinct cached alert paths around each real CLI scenario."""
    paths = (
        morning_brief._backstory_alerts_file,
        morning_brief._pursuit_stall_alerts_file,
        morning_brief._slack_alerts_file,
        morning_brief._contract_expiry_alerts_file,
        morning_brief._draft_queue_alerts_file,
    )
    for path in paths:
        path.cache_clear()
    monkeypatch.setattr(morning_brief, "write_run_status", real_write_run_status)
    monkeypatch.setattr(status, "get_fieldkit_home", lambda: documented_workspace)
    yield
    for path in paths:
        path.cache_clear()


def test_real_two_account_preview_scopes_inputs_but_preserves_shared_signals(
    documented_workspace: Path, monkeypatch: pytest.MonkeyPatch, isolated_brief_paths: None
) -> None:
    """Actual CLI collection scopes pipeline/projects and retains global cached data."""
    write_documented_pursuit_data(documented_workspace)
    write_second_documented_account(documented_workspace)
    today = datetime.now(UTC).date()
    for slug, end in (("acme-corp", today - timedelta(days=10)), ("beta-corp", today + timedelta(days=15))):
        project = documented_workspace / "accounts" / slug / "projects/implementation.md"
        project.write_text(
            f'---\nsf_stage: "In Progress"\nsf_contract_end: "{end}"\nsf_opportunity: "{slug} Rollout"\n---\n',
            encoding="utf-8",
        )
        (documented_workspace / "accounts" / slug / "gmail-intel.md").write_text(
            "# Fictional intelligence\n\nkubernetes kubernetes\n", encoding="utf-8"
        )
    watchers = documented_workspace / "watchers"
    watchers.mkdir()
    (watchers / "backstory-alerts.md").write_text(
        f"## {today} — beta-corp\n\nShared Beta cached health marker.\n", encoding="utf-8"
    )
    monkeypatch.setattr("fieldkit.brief.pipeline_only.synthesize", forbidden_boundary)
    monkeypatch.setattr("fieldkit.pipeline.main.synthesize", forbidden_boundary)
    monkeypatch.setattr("fieldkit.watch.preflight.preflight_check", forbidden_boundary)
    monkeypatch.setattr("fieldkit.watch.mcp.MCPSession.initialize", forbidden_boundary)
    before = snapshot_workflow(documented_workspace.parent)

    result = invoke_workflow(["brief", "generate", "--dry-run", "--account", "acme-corp"])

    assert result.exit_code == 0, result.output
    pipeline = result.stdout.split("## Pipeline Review", 1)[1].split("## Quota Gap", 1)[0]
    assert "acme-corp" in pipeline
    assert "beta-corp" not in pipeline
    assert "ZOMBIE" in result.stdout
    assert "EXPIRING" not in result.stdout
    assert "Shared Beta cached health marker." in result.stdout
    assert "**kubernetes** — acme-corp, beta-corp" in result.stdout
    assert snapshot_workflow(documented_workspace.parent) == before


@pytest.mark.parametrize("cached", (False, True), ids=("absent-cache", "populated-cache"))
def test_real_disabled_endpoints_keep_cached_alerts_visible(
    documented_workspace: Path, monkeypatch: pytest.MonkeyPatch, isolated_brief_paths: None, cached: bool
) -> None:
    """Disabled endpoints select missing-file notices without discarding saved alerts."""
    write_documented_pursuit_data(documented_workspace)
    assert get_mcp_endpoint("backstory") is None
    assert get_mcp_endpoint("draft_queue") is None
    if cached:
        watchers = documented_workspace / "watchers"
        watchers.mkdir()
        today = datetime.now(UTC).date()
        for filename, marker in (
            ("backstory-alerts.md", "Cached health marker"),
            ("draft-queue-alerts.md", "Cached draft marker"),
            ("slack-thread-alerts.md", "Cached Slack marker"),
        ):
            (watchers / filename).write_text(f"## {today} — acme-corp\n\n{marker}\n", encoding="utf-8")
    monkeypatch.setattr("fieldkit.brief.pipeline_only.synthesize", forbidden_boundary)
    monkeypatch.setattr("fieldkit.pipeline.main.synthesize", forbidden_boundary)
    monkeypatch.setattr("fieldkit.watch.preflight.preflight_check", forbidden_boundary)
    monkeypatch.setattr("fieldkit.watch.mcp.MCPSession.initialize", forbidden_boundary)
    before = snapshot_workflow(documented_workspace.parent)

    result = invoke_workflow(["brief", "generate", "--dry-run"])

    assert result.exit_code == 0, result.output
    for label in ("Backstory", "Draft-queue", "Slack"):
        assert (f"{label} input was not run" in result.stdout) is (not cached)
    for marker in ("Cached health marker", "Cached draft marker", "Cached Slack marker"):
        assert (marker in result.stdout) is cached
    assert "Calendar input was not run for this local preview" in result.stdout
    assert snapshot_workflow(documented_workspace.parent) == before
