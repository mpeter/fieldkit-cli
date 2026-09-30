"""Whole-page troubleshooting inventory and isolated executable diagnostics."""

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest
import yaml

import fieldkit.config as config
from fieldkit.watch.status import WatcherOutcome, write_run_status
from tests.documentation_workflow_support import (
    invoke_workflow,
    snapshot_workflow,
    write_documented_pursuit_data,
)

pytestmark = pytest.mark.integration
PAGE = Path("docs/reference/troubleshooting.md")
TROUBLESHOOTING_GUIDE_NODES = (
    "tests/test_troubleshooting_guide_contract.py",
    "tests/test_documentation_exit_examples.py::test_troubleshooting_table_matches_runtime_and_recovery",
    "tests/test_doctor.py::test_gmail_doctor_diagnostics_do_not_expose_selected_paths",
    "tests/test_doctor.py::test_gmail_data_failures_are_non_destructive_data_errors",
    "tests/test_sf_session_check.py::test_documentation_session_transcripts",
    "tests/test_sf_session_check.py::test_session_diagnostics_are_payload_free",
    "tests/test_watch_cli_logs.py",
    "tests/test_brief_workspace_required.py",
    "tests/test_ingest_run_lock.py::test_busy_run_has_no_database_or_discovery_effects",
    "tests/test_ingest_prepared_store.py::test_command_recovers_real_interrupted_state_only_under_lock",
    "tests/test_ingest_prepared_store.py::test_source_processor_replays_before_fetching",
    "tests/test_ingest_prepared_store.py::test_resumed_source_hides_private_replay_failure",
    "tests/test_ingest_prepared_store.py::test_replay_missing_pursuit_then_repair_preserves_note_edits",
    "tests/test_ingest_prepared_store.py::test_replay_failure_retains_intent",
    "tests/test_pursuit_effects.py::test_activity_requires_legacy_reconciliation",
    "tests/test_pursuit_effects.py::test_activity_rejects_ambiguous_marker",
    "tests/test_task_effects.py::test_ambiguous_markers_fail_closed",
    "tests/test_cli_exit.py::test_missing_optional_dependency_is_actionable_without_traceback",
)


@dataclass(frozen=True)
class GuideClaim:
    identifier: str
    classification: Literal["structure", "guidance", "behavior", "navigation", "manual"]
    evidence: str
    text: str


INVENTORY = (
    GuideClaim(
        "metadata",
        "structure",
        "layout",
        "---\nlast_reviewed: 2026-09-27\ncovers:\n  - src/fieldkit/commands/doctor/\n  - src/fieldkit/cli_exit.py\naudience: user\n---",
    ),
    GuideClaim("title", "structure", "layout", "# Troubleshooting"),
    GuideClaim(
        "private-support-guidance",
        "guidance",
        "operator",
        "Start with the smallest command that demonstrates the failure. Do not post\ncredentials, cookies, tokens, customer records, email content, private hostnames,\nor absolute home paths while asking for help.",
    ),
    GuideClaim("capture-heading", "structure", "layout", "## Capture a safe diagnostic"),
    GuideClaim(
        "diagnostic-commands",
        "behavior",
        "real-diagnostics",
        "```console\nfieldkit --version\nfieldkit doctor\nfieldkit doctor --json\n```",
    ),
    GuideClaim(
        "service-diagnostic-guidance",
        "behavior",
        "real-diagnostics",
        "Then run the relevant service check, such as `fieldkit doctor sf`,\n`fieldkit doctor google`, or `fieldkit doctor gmail`. Record the command, exit\ncode, operating system, Python version, and sanitized error text.",
    ),
    GuideClaim("exits-guidance", "guidance", "operator", "Exit codes identify the next action:"),
    GuideClaim(
        "exit-table",
        "behavior",
        "exit-contract",
        "| Exit | Meaning | Next step |\n| --- | --- | --- |\n| `0` | Selected work succeeded | No repair needed |\n| `1` | Partial result; retry may help | Inspect the named record or provider failure |\n| `2` | Authentication needs user action | Reauthenticate through the documented provider flow |\n| `3` | Invalid or incomplete data/configuration | Correct the named input; retrying unchanged input will not help |",
    ),
    GuideClaim("base-heading", "structure", "layout", "## Base installation fails"),
    GuideClaim("version-guidance", "guidance", "operator", "Confirm the executable and Python version:"),
    GuideClaim(
        "version-commands", "behavior", "installed-base", "```console\nfieldkit --version\npython3 --version\n```"
    ),
    GuideClaim(
        "installation-guidance",
        "navigation",
        "links",
        "For a released uv-tool install, reinstall the same public version and rerun the\n[offline first-success path](../getting-started.md). In a contributor checkout,\nrun commands as `uv run fieldkit` so the checkout—not another globally installed\ncopy—is under test.",
    ),
    GuideClaim("configuration-heading", "structure", "layout", "## Configuration is missing or invalid"),
    GuideClaim(
        "configuration-recovery",
        "behavior",
        "missing-invalid",
        "Run `fieldkit init --minimal <path>` for a credential-free trial or `fieldkit init`\nfor interactive configuration. fieldkit reports the configuration problem and exits\n`3`; do not repeatedly retry unchanged configuration.",
    ),
    GuideClaim(
        "configuration-link",
        "navigation",
        "links",
        "The [configuration reference](config-file.md) explains the workspace, runtime,\nand user-configuration roots.",
    ),
    GuideClaim("sf-heading", "structure", "layout", "## Salesforce authentication fails"),
    GuideClaim(
        "sf-commands",
        "manual",
        "manual.credentialed-integration",
        "```console\nfieldkit doctor sf\nfieldkit sf session-check\n```",
    ),
    GuideClaim(
        "session-status-and-recovery",
        "behavior",
        "synthetic-sf-status",
        "For `sf session-check`, exit `2` also covers connectivity failures and unexpected\nHTTP responses; inspect the accompanying message before replacing credentials.\nIf it reports a missing or rejected session, follow\n[Connect Salesforce](../guides/salesforce-auth.md) using an organization and\nsession you are authorized to access. Never attach the `sid` value to an issue.",
    ),
    GuideClaim("google-heading", "structure", "layout", "## Google or Gmail fails"),
    GuideClaim(
        "google-commands",
        "manual",
        "manual.credentialed-integration",
        "```console\nfieldkit doctor google\nfieldkit doctor gmail\n```",
    ),
    GuideClaim(
        "consent-cache-recovery",
        "guidance",
        "operator+cache-errors",
        "Complete a first OAuth consent flow in an interactive terminal. An unattended\njob cannot repair missing user consent. If the local Gmail cache is damaged,\npreserve a backup before rebuilding it so unexpected data loss remains\nrecoverable. See [Connect Gmail](../guides/gmail.md).",
    ),
    GuideClaim("ingest-heading", "structure", "layout", "## Transcript ingest cannot resume"),
    GuideClaim(
        "locked-ingest-replay",
        "behavior",
        "ingest-recovery",
        "If the command reports `ingest_busy`, wait for the active run to finish and rerun\n`fieldkit ingest run --pipeline transcript-ingest`. Do not remove its lock file.\nInterrupted sources are recovered under that lock; saved decisions replay without\nanother document fetch or classification pass.",
    ),
    GuideClaim(
        "required-write-reconciliation",
        "behavior",
        "ingest-ownership",
        "A required-write failure leaves the source incomplete. Check that its required\npursuit files still exist and that note, pursuit, or task ownership comments have not been\nremoved or copied. Restore missing files or original provenance from your backup\nbefore retrying. Do not delete checkpoints or ownership comments to force success:\nunmarked legacy output and conflicting ownership require explicit reconciliation.\nPreserve a backup and use the support path if the conflict is unclear.",
    ),
    GuideClaim("watcher-heading", "structure", "layout", "## A watcher or brief is incomplete"),
    GuideClaim(
        "watcher-commands",
        "behavior",
        "real-watcher-reads",
        "```console\nfieldkit watch status\nfieldkit watch logs --list\nfieldkit watch logs <watcher> --tail 100\n```",
    ),
    GuideClaim(
        "local-preview-isolation",
        "behavior",
        "real-local-preview",
        "A not-yet-run optional source is different from a failed source. Investigate\n`partial` and `fatal` outcomes individually; do not restart or reconfigure an\nunrelated service. Use `fieldkit brief generate --pipeline-only --no-llm --dry-run`\nto isolate local pipeline rendering from watcher aggregation and model synthesis.",
    ),
    GuideClaim("dependency-heading", "structure", "layout", "## An optional dependency is missing"),
    GuideClaim(
        "dependency-profile-guidance",
        "behavior",
        "optional-dependency",
        "The error names the required profile. Reinstall with that extra, for example:",
    ),
    GuideClaim(
        "release-install-command",
        "manual",
        "manual.release-cutover",
        "```console\nuv tool install --force 'fieldkit-cli[google]'\n```",
    ),
    GuideClaim(
        "integration-auth-guidance",
        "guidance",
        "operator",
        "Installing a profile does not configure credentials. Follow the corresponding\nintegration guide after installation.",
    ),
    GuideClaim("support-heading", "structure", "layout", "## Ask for help"),
    GuideClaim(
        "support-security-links",
        "navigation",
        "links",
        "Search existing issues, then follow [SUPPORT.md](https://github.com/mpeter/fieldkit-cli/blob/main/SUPPORT.md).\nUse [SECURITY.md](https://github.com/mpeter/fieldkit-cli/blob/main/SECURITY.md) for a\nsuspected vulnerability instead of disclosing details publicly.",
    ),
)
EXPECTED_TEXT = tuple(claim.text for claim in INVENTORY)
EXPECTED_IDS = tuple(claim.identifier for claim in INVENTORY)
EVIDENCE_NODES = {
    "real-diagnostics": (
        "tests/test_troubleshooting_guide_contract.py::test_actual_unconfigured_diagnostics_have_explicit_statuses",
    ),
    "exit-contract": (TROUBLESHOOTING_GUIDE_NODES[1],),
    "missing-invalid": (
        "tests/test_troubleshooting_guide_contract.py::test_actual_invalid_and_missing_configuration_is_nonpassing_without_writes",
        TROUBLESHOOTING_GUIDE_NODES[7],
    ),
    "synthetic-sf-status": TROUBLESHOOTING_GUIDE_NODES[4:6],
    "operator+cache-errors": TROUBLESHOOTING_GUIDE_NODES[2:4],
    "ingest-recovery": TROUBLESHOOTING_GUIDE_NODES[8:12],
    "ingest-ownership": TROUBLESHOOTING_GUIDE_NODES[12:17],
    "real-watcher-reads": (
        "tests/test_troubleshooting_guide_contract.py::test_actual_local_watcher_status_and_log_reads_are_read_only",
        TROUBLESHOOTING_GUIDE_NODES[6],
    ),
    "real-local-preview": (
        "tests/test_troubleshooting_guide_contract.py::test_actual_local_pipeline_preview_never_calls_a_provider_or_writes",
        TROUBLESHOOTING_GUIDE_NODES[7],
    ),
    "optional-dependency": (TROUBLESHOOTING_GUIDE_NODES[17],),
}


def semantic_blocks(text: str) -> tuple[str, ...]:
    """Keep every nonblank block, including unknown syntax, in source order."""
    return tuple(re.split(r"\n\s*\n", text.strip()))


def assert_guide(text: str) -> tuple[str, ...]:
    actual = semantic_blocks(text)
    assert actual == EXPECTED_TEXT, "troubleshooting semantic inventory changed"
    return actual


def test_complete_ordered_troubleshooting_inventory() -> None:
    blocks = assert_guide(PAGE.read_text(encoding="utf-8"))
    assert len(blocks) == len(INVENTORY) == len(set(EXPECTED_IDS)) == 33
    assert sum(block.startswith("```") for block in blocks) == 6
    assert {claim.classification for claim in INVENTORY} == {
        "structure",
        "guidance",
        "behavior",
        "navigation",
        "manual",
    }
    assert {claim.evidence for claim in INVENTORY} == set(EVIDENCE_NODES) | {
        "layout",
        "operator",
        "links",
        "installed-base",
        "manual.credentialed-integration",
        "manual.release-cutover",
    }
    assert all(
        node in TROUBLESHOOTING_GUIDE_NODES or node.split("::", 1)[0] in TROUBLESHOOTING_GUIDE_NODES
        for nodes in EVIDENCE_NODES.values()
        for node in nodes
    )


def test_manual_fence_owners_are_not_promoted_by_synthetic_diagnostics() -> None:
    contract = json.loads(Path("docs/documentation-contract.json").read_text(encoding="utf-8"))
    records = contract["documents"][PAGE.as_posix()]["fenced_blocks"]
    assert len(records) == 6
    assert [(records[index]["classification"], records[index]["verification_id"]) for index in (2, 3, 5)] == [
        ("credentialed_manual_integration", "manual.credentialed-integration"),
        ("credentialed_manual_integration", "manual.credentialed-integration"),
        ("exact_release_cutover_proof", "manual.release-cutover"),
    ]


@pytest.mark.parametrize("index", range(len(INVENTORY)), ids=EXPECTED_IDS)
@pytest.mark.parametrize("mutation", ("insert", "remove", "alter", "negate", "duplicate", "reorder"))
def test_every_semantic_block_mutation_is_rejected(index: int, mutation: str) -> None:
    blocks = list(EXPECTED_TEXT)
    if mutation == "insert":
        blocks.insert(index, "<aside>Unreviewed diagnostic promise.</aside>")
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


@pytest.mark.parametrize("index", (4, 10, 16, 19, 25, 29))
@pytest.mark.parametrize("mutation", ("language", "body"))
def test_every_fence_language_and_body_mutation_is_rejected(index: int, mutation: str) -> None:
    blocks = list(EXPECTED_TEXT)
    lines = blocks[index].splitlines()
    lines[0 if mutation == "language" else 1] += "-unsupported"
    blocks[index] = "\n".join(lines)
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide("\n\n".join(blocks))


@pytest.mark.parametrize(
    "index,inline",
    [
        (index, inline)
        for index, block in enumerate(EXPECTED_TEXT)
        for inline in re.findall(r"(?<!`)`([^`\n]+)`(?!`)", block)
    ],
)
def test_every_inline_value_and_command_mutation_is_rejected(index: int, inline: str) -> None:
    blocks = list(EXPECTED_TEXT)
    blocks[index] = blocks[index].replace(f"`{inline}`", "`unsupported`", 1)
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide("\n\n".join(blocks))


@pytest.mark.parametrize(
    "index,label,target",
    [
        (index, label, target)
        for index, block in enumerate(EXPECTED_TEXT)
        for label, target in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", block)
    ],
)
@pytest.mark.parametrize("mutation", ("label", "target"))
def test_every_link_label_and_target_mutation_is_rejected(index: int, label: str, target: str, mutation: str) -> None:
    blocks = list(EXPECTED_TEXT)
    replacement = f"[unsupported]({target})" if mutation == "label" else f"[{label}](unknown.md)"
    blocks[index] = blocks[index].replace(f"[{label}]({target})", replacement)
    with pytest.raises(AssertionError, match="semantic inventory"):
        assert_guide("\n\n".join(blocks))


def test_reviewed_links_resolve_to_local_documents_or_canonical_support() -> None:
    targets = [target for block in EXPECTED_TEXT for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", block)]
    assert targets == [
        "../getting-started.md",
        "config-file.md",
        "../guides/salesforce-auth.md",
        "../guides/gmail.md",
        "https://github.com/mpeter/fieldkit-cli/blob/main/SUPPORT.md",
        "https://github.com/mpeter/fieldkit-cli/blob/main/SECURITY.md",
    ]
    assert all((PAGE.parent / target).is_file() for target in targets[:4])
    assert all(Path(name).is_file() for name in ("SUPPORT.md", "SECURITY.md"))


@pytest.fixture
def diagnostic_workspace(documented_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Remove optional integration selection; block subprocesses as well as sockets."""
    workspace = documented_workspace
    config.CONFIG_PATH.write_text(
        yaml.safe_dump({"fieldkit_home": str(workspace), "gmail_db": str(workspace / "data" / "gmail.db")}),
        encoding="utf-8",
    )
    (workspace / "config" / "accounts.yaml").write_text("internal_domains: []\naccounts: {}\n", encoding="utf-8")
    for name in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "SHADOWBOT_ASSISTANT_ID"):
        monkeypatch.delenv(name, raising=False)
    config.clear_config_caches()
    # Restore the real configured accessor after the shared watcher isolation fixture.
    monkeypatch.setattr("fieldkit.watch.logging.get_fieldkit_home", config.get_fieldkit_home)
    monkeypatch.setattr("fieldkit.watch.status.get_fieldkit_home", config.get_fieldkit_home)

    def reject_subprocess(*_args: object, **_kwargs: object) -> None:
        pytest.fail("local diagnostic attempted subprocess or agent-harness discovery")

    monkeypatch.setattr(subprocess, "run", reject_subprocess)
    monkeypatch.setattr(subprocess, "Popen", reject_subprocess)
    return workspace


@pytest.mark.parametrize(
    "argv,expected",
    (
        (["--version"], 0),
        (["doctor"], 0),
        (["doctor", "--json"], 0),
        (["doctor", "sf"], 2),
        (["doctor", "google"], 2),
        (["doctor", "gmail"], 3),
    ),
)
def test_actual_unconfigured_diagnostics_have_explicit_statuses(
    diagnostic_workspace: Path, argv: list[str], expected: int
) -> None:
    before = snapshot_workflow(diagnostic_workspace.parent)
    result = invoke_workflow(argv)
    assert result.exit_code == expected, result.output
    assert snapshot_workflow(diagnostic_workspace.parent) == before
    assert "Traceback" not in result.output
    if argv == ["doctor", "--json"]:
        payload = json.loads(result.stdout)
        assert {entry["service"] for entry in payload} == {"sf", "google", "gmail", "shadowbot"}
        assert all(not entry["configured"] and entry["configuration_state"] == "disabled" for entry in payload)


@pytest.mark.parametrize("problem", ("missing", "malformed", "nonmapping"))
def test_actual_invalid_and_missing_configuration_is_nonpassing_without_writes(
    diagnostic_workspace: Path, problem: str
) -> None:
    path = config.CONFIG_PATH
    if problem == "missing":
        path.unlink()
    else:
        path.write_text(
            "[fictional-sensitive-sentinel" if problem == "malformed" else "- fictional-sensitive-sentinel\n",
            encoding="utf-8",
        )
    config.clear_config_caches()
    before = snapshot_workflow(diagnostic_workspace.parent)
    result = invoke_workflow(["brief", "generate", "--pipeline-only", "--no-llm", "--dry-run"])
    assert result.exit_code == 3, result.output
    assert "fieldkit init" in result.output
    assert "fictional-sensitive-sentinel" not in result.output
    assert str(diagnostic_workspace.parent) not in result.output
    assert "Traceback" not in result.output
    assert snapshot_workflow(diagnostic_workspace.parent) == before


def test_actual_local_watcher_status_and_log_reads_are_read_only(diagnostic_workspace: Path) -> None:
    outcomes: tuple[tuple[str, WatcherOutcome], ...] = (("backstory-health", "partial"), ("pursuit-stalls", "fatal"))
    for watcher, outcome in outcomes:
        result = write_run_status(
            watcher=watcher,
            outcome=outcome,
            records_checked=2,
            alerts_generated=0,
            failures=1,
            elapsed_seconds=0.1,
            dry_run=False,
        )
        assert result == "written"
    logs = diagnostic_workspace / "logs" / "watchers"
    logs.mkdir(parents=True)
    log_path = logs / "backstory-health-2026-09-29-090000.log"
    log_path.write_text(
        "outside-tail\n" + "\n".join(f"fictional-line-{index}" for index in range(100)) + "\n", encoding="utf-8"
    )
    before = snapshot_workflow(diagnostic_workspace.parent)
    result = invoke_workflow(["watch", "status"])
    assert result.exit_code == 0, result.output
    assert "partial" in result.stdout and "fatal" in result.stdout
    result = invoke_workflow(["watch", "logs", "--list"])
    assert result.exit_code == 0, result.output
    assert log_path.name in result.stdout and "fictional-line-" not in result.stdout
    result = invoke_workflow(["watch", "logs", "backstory-health", "--tail", "100"])
    assert result.exit_code == 0, result.output
    assert "fictional-line-0" in result.stdout and "fictional-line-99" in result.stdout
    assert "outside-tail" not in result.stdout
    assert snapshot_workflow(diagnostic_workspace.parent) == before


def test_actual_local_pipeline_preview_never_calls_a_provider_or_writes(diagnostic_workspace: Path) -> None:
    write_documented_pursuit_data(diagnostic_workspace)
    before = snapshot_workflow(diagnostic_workspace.parent)
    result = invoke_workflow(["brief", "generate", "--pipeline-only", "--no-llm", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Brief saved" not in result.output
    assert "pipeline" in result.stdout.lower() or "pursuit" in result.stdout.lower()
    assert snapshot_workflow(diagnostic_workspace.parent) == before
