"""Canonical fixture builders for documentation validation and execution tests."""

import json
import os
import shlex
from collections.abc import Iterator
from contextlib import nullcontext
from pathlib import Path
from typing import TypedDict

import pytest

from scripts import (
    check_documentation_contract,
    check_documentation_examples,
    documentation_command_runner,
    documentation_commands,
)
from scripts.documentation_runtime import DependencyInput, RuntimeTools


class _ValidationContract(TypedDict):
    schema_version: int
    roadmap: str
    example_verifications: dict[str, dict[str, str]]
    verification: dict[str, dict[str, object]]
    documents: dict[str, dict[str, object]]


@pytest.fixture
def prepared_diagnostic_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Owner-result unit tests deliberately skip dependency acquisition."""
    monkeypatch.setattr(check_documentation_examples, "diagnostic_tools", lambda: nullcontext(RuntimeTools(-1, -2)))
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(
        check_documentation_examples,
        "prepare_dependencies",
        lambda *_args, **_options: nullcontext(DependencyInput(descriptor, "a" * 64)),
    )
    try:
        yield
    finally:
        os.close(descriptor)


def write_fixture_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_contract_validation_fixture(
    repo: Path, *, fingerprint: str = "sha256:" + "0" * 64, excluded: list[str] | None = None
) -> None:
    write_fixture_file(repo / "Makefile", "artifact-check:\npr-check:\nquality:\n")
    write_fixture_file(repo / "scripts/generate_cli_docs.py", "")
    write_fixture_file(repo / "scripts/generate_dep_map.py", "")
    write_fixture_file(repo / "scripts/check_documentation_contract.py", "")
    write_fixture_file(repo / "scripts/documentation_commands.py", "")
    write_fixture_file(repo / "scripts/check_documentation_examples.py", "")
    write_fixture_file(repo / "scripts/check_skill_documentation_contract.py", "")
    write_fixture_file(repo / "scripts/check_roadmap_contract.py", "")
    write_fixture_file(repo / "scripts/check_documentation_example_scenarios.py", "")
    write_fixture_file(repo / "scripts/release_workflow_policy.py", "")
    write_fixture_file(repo / "scripts/check_compatibility_policy.py", "")
    write_fixture_file(repo / "scripts/smoke_artifact.py", "")
    write_fixture_file(repo / "tests/test_cli_exit.py", "")
    write_fixture_file(repo / "tests/test_threat_model_contract.py", "")
    write_fixture_file(repo / "tests/test_ingest_paths.py", "")
    write_fixture_file(repo / "tests/test_ingest_prepared.py", "")
    write_fixture_file(repo / "tests/test_ingest_prepared_store.py", "")
    write_fixture_file(repo / "tests/test_pursuit_effects.py", "")
    write_fixture_file(repo / "tests/test_task_effects.py", "")
    write_fixture_file(repo / "tests/test_owned_markdown.py", "")
    write_fixture_file(repo / "tests/test_text_snapshot.py", "")
    write_fixture_file(repo / "tests/test_atomic_text_create.py", "")
    write_fixture_file(repo / "tests/test_web_server.py", "")
    write_fixture_file(repo / "tests/test_forecast.py", "")
    write_fixture_file(repo / "tests/test_pipeline_quota.py", "")
    write_fixture_file(repo / "tests/test_documentation_pursuit_workflow.py", "")
    write_fixture_file(repo / "tests/test_smoke_gmail_examples.py", "")
    write_fixture_file(repo / "tests/test_brief_documentation_scenarios.py", "")
    write_fixture_file(repo / "tests/test_documentation_contributor_design.py", "")
    write_fixture_file(repo / "tests/test_watcher_documentation_scenarios.py", "")
    write_fixture_file(repo / "tests/test_documentation_exit_examples.py", "")
    write_fixture_file(repo / "tests/test_integrations_page_contract.py", "")
    write_fixture_file(repo / "tests/test_documentation_integration_profiles.py", "")
    write_fixture_file(repo / "tests/test_installation_profiles.py", "")
    write_fixture_file(repo / "scripts/check_dependency_profiles.py", "")
    write_fixture_file(repo / "tests/test_documentation_release_mode_table.py", "")
    write_fixture_file(repo / "tests/test_documentation_security_support.py", "")
    write_fixture_file(repo / "tests/test_documentation_privacy_destinations.py", "")
    write_fixture_file(repo / "scripts/_release_policy.py", "")
    write_fixture_file(repo / "tests/test_meeting_workflow_scenarios.py", "")
    write_fixture_file(repo / "tests/documentation_workflow_support.py", "")
    write_fixture_file(repo / "tests/test_remaining_public_skill_contracts.py", "")
    write_fixture_file(repo / "tests/test_ingest_workflow_scenarios.py", "")
    write_fixture_file(repo / "tests/test_gmail_refresh_input_selection.py", "")
    write_fixture_file(repo / "tests/test_ingest_approval_effects.py", "")
    write_fixture_file(repo / "scripts/markdown_tables.py", "")
    write_fixture_file(repo / "tests/test_auth_sf.py", "")
    write_fixture_file(repo / "tests/test_sf_session_check.py", "")
    write_fixture_file(repo / "tests/test_documentation_configuration_examples.py", "")
    write_fixture_file(repo / "tests/test_config_xdg.py", "")
    write_fixture_file(repo / "tests/test_tool_routing_cli_first.py", "")
    write_fixture_file(repo / "tests/test_win_loss_skill_documentation.py", "")
    write_fixture_file(repo / "tests/test_humanizer_skill_documentation.py", "")
    write_fixture_file(repo / "tests/test_handoff_skill_documentation.py", "")
    write_fixture_file(repo / "tests/test_draft_review_skill_documentation.py", "")
    write_fixture_file(repo / "tests/test_workstream_discover_skill_documentation.py", "")
    write_fixture_file(repo / "tests/test_contract_skill_documentation.py", "")
    schema = Path(__file__).parents[1] / "docs/release-readiness/documentation-contract.schema.json"
    write_fixture_file(
        repo / "docs/release-readiness/documentation-contract.schema.json", schema.read_text(encoding="utf-8")
    )
    evidence_schema = Path(__file__).parents[1] / "docs/release-readiness/rehearsal-evidence.schema.json"
    write_fixture_file(
        repo / "docs/release-readiness/rehearsal-evidence.schema.json", evidence_schema.read_text(encoding="utf-8")
    )
    contract: _ValidationContract = {
        "schema_version": 2,
        "roadmap": "ROADMAP.md",
        "example_verifications": {
            "automated.threat-model-contract": {
                "classification": "structural_assertion",
                "mode": "automated",
                "evidence": shlex.join(documentation_commands.EXAMPLE_COMMANDS["automated.threat-model-contract"][0]),
            },
            "automated.ingest-workflow-scenarios": {
                "classification": "safe_automated_command",
                "mode": "automated",
                "evidence": "uv run pytest tests/test_ingest_workflow_scenarios.py tests/test_gmail_refresh_input_selection.py tests/test_ingest_approval_effects.py -q -n 0",
            },
            "automated.remaining-public-scenarios": {
                "classification": "safe_automated_command",
                "mode": "automated",
                "evidence": "uv run pytest tests/test_remaining_public_skill_contracts.py -q -n 0",
            },
            "automated.remaining-public-structure": {
                "classification": "structural_assertion",
                "mode": "automated",
                "evidence": "uv run pytest tests/test_remaining_public_skill_contracts.py -q -n 0",
            },
            "automated.meeting-report-scenarios": {
                "classification": "safe_automated_command",
                "mode": "automated",
                "evidence": "uv run pytest tests/test_meeting_workflow_scenarios.py -q -n 0",
            },
            "automated.meeting-report-structure": {
                "classification": "structural_assertion",
                "mode": "automated",
                "evidence": "uv run pytest tests/test_meeting_workflow_scenarios.py -q -n 0",
            },
            "automated.contributor-instruction-structure": {
                "classification": "structural_assertion",
                "evidence": "uv run pytest tests/test_documentation_contributor_design.py -q -n 0",
                "mode": "automated",
            },
            "automated.contributor-journey": {
                "classification": "safe_automated_command",
                "mode": "automated",
                "phase": "contributor_journey",
                "evidence": "make bootstrap ; make pr-check ; uv run pytest tests/ -q ; make quality-full ; make docs",
            },
            "automated.brief-workflow-scenarios": {
                "classification": "safe_automated_command",
                "evidence": "uv run pytest tests/test_brief_documentation_scenarios.py -q -n 0",
                "mode": "automated",
            },
            "automated.watcher-workflow-scenarios": {
                "classification": "safe_automated_command",
                "evidence": "uv run pytest tests/test_watcher_documentation_scenarios.py -q -n 0",
                "mode": "automated",
            },
            "automated.salesforce-auth-contract": {
                "classification": "structural_assertion",
                "evidence": "uv run pytest tests/test_auth_sf.py tests/test_sf_session_check.py -k documentation -q -n 0",
                "mode": "automated",
            },
            "automated.gmail-example-contract": {
                "classification": "structural_assertion",
                "evidence": "uv run pytest tests/test_smoke_gmail_examples.py -k documentation -q -n 0",
                "mode": "automated",
            },
            "automated.gmail-import-scenario": {
                "classification": "safe_automated_command",
                "evidence": "uv run pytest tests/test_smoke_gmail_examples.py -k documentation_import -q -n 0",
                "mode": "automated",
            },
            "automated.roadmap-contract": {
                "classification": "structural_assertion",
                "evidence": "uv run python scripts/check_roadmap_contract.py",
                "mode": "automated",
            },
            "automated.compatibility-policy": {
                "classification": "structural_assertion",
                "evidence": "uv run python scripts/check_compatibility_policy.py",
                "mode": "automated",
            },
            "automated.generated-reference": {
                "classification": "generated_reference",
                "evidence": "uv run python scripts/generate_cli_docs.py --check",
                "mode": "automated",
            },
            "automated.generated-dependency-map": {
                "classification": "generated_reference",
                "evidence": "uv run python scripts/generate_dep_map.py --check",
                "mode": "automated",
            },
            "automated.installed-base-artifact": {
                "classification": "safe_automated_command",
                "evidence": "uv run python scripts/check_documentation_example_scenarios.py --json",
                "mode": "automated",
            },
            "automated.release-workflow-policy": {
                "classification": "safe_automated_command",
                "evidence": "uv run python scripts/release_workflow_policy.py",
                "mode": "automated",
            },
            "automated.exit-code-contract": {
                "classification": "structural_assertion",
                "evidence": "uv run pytest tests/test_cli_exit.py tests/test_documentation_exit_examples.py -q -n 0",
                "mode": "automated",
            },
            "automated.integration-profile-contract": {
                "classification": "structural_assertion",
                "evidence": "uv run pytest tests/test_integrations_page_contract.py tests/test_documentation_integration_profiles.py tests/test_installation_profiles.py -q -n 0",
                "mode": "automated",
            },
            "automated.release-mode-table-contract": {
                "classification": "structural_assertion",
                "evidence": "uv run pytest tests/test_documentation_release_mode_table.py -q -n 0",
                "mode": "automated",
            },
            "automated.security-support-contract": {
                "classification": "structural_assertion",
                "evidence": "uv run pytest tests/test_documentation_security_support.py -q -n 0",
                "mode": "automated",
            },
            "automated.privacy-destination-contract": {
                "classification": "structural_assertion",
                "evidence": "uv run pytest tests/test_documentation_privacy_destinations.py -q -n 0",
                "mode": "automated",
            },
            "automated.pipeline-example-contract": {
                "classification": "structural_assertion",
                "evidence": "uv run pytest tests/test_forecast.py tests/test_pipeline_quota.py tests/test_documentation_pursuit_workflow.py -k documentation -q -n 0",
                "mode": "automated",
            },
            "automated.meeting-template-contract": {
                "classification": "structural_assertion",
                "evidence": "uv run pytest tests/test_tool_routing_cli_first.py -k 'qbr_output_template or meeting_brief_template' -q -n 0",
                "mode": "automated",
            },
            "automated.win-loss-template-contract": {
                "classification": "structural_assertion",
                "evidence": "uv run pytest tests/test_win_loss_skill_documentation.py -q -n 0",
                "mode": "automated",
            },
            "automated.humanizer-structure-contract": {
                "classification": "structural_assertion",
                "evidence": "uv run pytest tests/test_humanizer_skill_documentation.py -q -n 0",
                "mode": "automated",
            },
            "automated.handoff-template-contract": {
                "classification": "structural_assertion",
                "evidence": "uv run pytest tests/test_handoff_skill_documentation.py -q -n 0",
                "mode": "automated",
            },
            "automated.draft-review-structure-contract": {
                "classification": "structural_assertion",
                "evidence": "uv run pytest tests/test_draft_review_skill_documentation.py -q -n 0",
                "mode": "automated",
            },
            "automated.workstream-discover-structure-contract": {
                "classification": "structural_assertion",
                "evidence": "uv run pytest tests/test_workstream_discover_skill_documentation.py -q -n 0",
                "mode": "automated",
            },
            "automated.contract-workflow-structure-contract": {
                "classification": "structural_assertion",
                "evidence": "uv run pytest tests/test_contract_skill_documentation.py -q -n 0",
                "mode": "automated",
            },
            "automated.configuration-example-contract": {
                "classification": "structural_assertion",
                "evidence": "uv run pytest tests/test_documentation_configuration_examples.py tests/test_config_xdg.py -q -n 0",
                "mode": "automated",
            },
            "automated.documentation-contract": {
                "classification": "structural_assertion",
                "evidence": "uv run python scripts/check_documentation_contract.py",
                "mode": "automated",
            },
            "manual.credentialed-integration": {
                "classification": "credentialed_manual_integration",
                "evidence": "docs/release-readiness/rehearsal-evidence.schema.json",
                "mode": "manual_evidence",
            },
            "manual.release-cutover": {
                "classification": "exact_release_cutover_proof",
                "evidence": "docs/release-readiness/rehearsal-evidence.schema.json",
                "mode": "manual_evidence",
            },
        },
        "verification": {
            mechanism: {
                "evidence": evidence,
                "phase": documentation_commands.OWNER_PHASES[mechanism],
                "paths": [],
            }
            for mechanism, evidence in check_documentation_contract._VERIFICATION_EVIDENCE.items()
        },
        "documents": {
            "README.md": {
                "content_type": "overview",
                "reader_action": "Choose an installation path.",
                "sources": ["src/fieldkit/__main__.py"],
                "source_fingerprint": fingerprint,
            },
            "ROADMAP.md": {
                "content_type": "roadmap",
                "reader_action": "Review work that is not shipped.",
                "sources": ["pyproject.toml"],
                "source_fingerprint": fingerprint,
            },
        },
    }
    contract["verification"]["first_user_guides_contract"]["paths"] = ["README.md"]
    contract["verification"]["live_cutover"]["paths"] = ["ROADMAP.md"]
    for path, entry in contract["documents"].items():
        document = repo / path
        if not document.is_file():
            continue
        try:
            inventory = check_documentation_contract._block_inventory(document)
        except ValueError:
            inventory = []
        entry["fenced_blocks"] = [
            {
                "id": f"{path.lower().replace('/', '.').replace('.md', '')}.block-{index}",
                **block,
                "classification": "generated_reference",
                "verification_id": "automated.generated-reference",
            }
            for index, block in enumerate(inventory, start=1)
        ]
    write_fixture_file(repo / "docs/documentation-contract.json", json.dumps(contract))
    policy = {
        "schema_version": 1,
        "categories": {
            "public_entrypoint": ["README.md", "ROADMAP.md"],
            "public_site": [],
            "public_repository_only": [],
            "private_history_excluded": excluded or [],
        },
        "issue_forms": {
            "required": ["bug.yml"],
            "allowed_labels": ["bug"],
            "contact_links": ["https://example.com/security", "https://example.com/support"],
        },
    }
    write_fixture_file(repo / "docs/release-readiness/public-surface-policy.json", json.dumps(policy))


def write_example_verification_fixture(repo: Path) -> None:
    write_fixture_file(repo / "pyproject.toml", '[project]\nname = "fieldkit-cli"\nversion = "1.0.0"\n')
    contract = {
        "example_verifications": {
            "automated.compatibility-policy": {
                "classification": "structural_assertion",
                "evidence": "uv run python scripts/check_compatibility_policy.py",
                "mode": "automated",
            },
            "automated.generated-reference": {
                "classification": "generated_reference",
                "evidence": "uv run python scripts/generate_cli_docs.py --check",
                "mode": "automated",
            },
            "automated.generated-dependency-map": {
                "classification": "generated_reference",
                "evidence": "uv run python scripts/generate_dep_map.py --check",
                "mode": "automated",
            },
            "automated.installed-base-artifact": {
                "classification": "safe_automated_command",
                "evidence": "uv run python scripts/check_documentation_example_scenarios.py --json",
                "mode": "automated",
            },
            "automated.exit-code-contract": {
                "classification": "structural_assertion",
                "evidence": "uv run pytest tests/test_cli_exit.py tests/test_documentation_exit_examples.py -q -n 0",
                "mode": "automated",
            },
            "automated.pipeline-example-contract": {
                "classification": "structural_assertion",
                "evidence": "uv run pytest tests/test_forecast.py tests/test_pipeline_quota.py tests/test_documentation_pursuit_workflow.py -k documentation -q -n 0",
                "mode": "automated",
            },
            "automated.configuration-example-contract": {
                "classification": "structural_assertion",
                "evidence": "uv run pytest tests/test_documentation_configuration_examples.py tests/test_config_xdg.py -q -n 0",
                "mode": "automated",
            },
            "manual.release-cutover": {
                "classification": "exact_release_cutover_proof",
                "evidence": "docs/release-readiness/rehearsal-evidence.schema.json",
                "mode": "manual_evidence",
            },
        },
        "documents": {
            "README.md": {
                "fenced_blocks": [
                    {"id": "readme.compatibility", "verification_id": "automated.compatibility-policy"},
                    {"id": "readme.generated", "verification_id": "automated.generated-reference"},
                    {"id": "readme.dependency-map", "verification_id": "automated.generated-dependency-map"},
                    {"id": "readme.installed", "verification_id": "automated.installed-base-artifact"},
                    {"id": "readme.exit-codes", "verification_id": "automated.exit-code-contract"},
                    {"id": "readme.configuration", "verification_id": "automated.configuration-example-contract"},
                    {"id": "readme.forecast", "verification_id": "automated.pipeline-example-contract"},
                    {"id": "readme.quota", "verification_id": "automated.pipeline-example-contract"},
                    {
                        "id": "readme.release",
                        "classification": "exact_release_cutover_proof",
                        "verification_id": "manual.release-cutover",
                        "sha256": "a" * 64,
                    },
                ]
            }
        },
    }
    path = repo / "docs/documentation-contract.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(contract), encoding="utf-8")
    schema_source = Path(__file__).parents[1] / "docs/release-readiness/rehearsal-evidence.schema.json"
    schema_path = repo / "docs/release-readiness/rehearsal-evidence.schema.json"
    schema_path.parent.mkdir(parents=True)
    schema_path.write_text(schema_source.read_text(encoding="utf-8"), encoding="utf-8")


def artifact_summary_fixture(*, diagnostic: bool = False) -> dict[str, object]:
    """Mirror DocumentationScenarioEvidence.summary with a complete package pair."""
    return {
        "schema_version": 1,
        "status": "diagnostic" if diagnostic else "pass",
        "scenarios": {"readme.md.block-1": ["SMOKE101"]},
        "failures": [],
        "artifacts": [
            {
                "name": name,
                "sha256": "a" * 64,
                "source_revision": None if diagnostic else "b" * 40,
                **(
                    {
                        "source_binding": "unattested-dirty",
                        "criteria": [{"criterion_id": "SMOKE101", "status": "pass"}],
                    }
                    if diagnostic
                    else {}
                ),
            }
            for name in ("fieldkit_cli-1.0.0-py3-none-any.whl", "fieldkit_cli-1.0.0.tar.gz")
        ],
    }


def passing_documentation_run(
    repo_root: Path, argv: tuple[str, ...], **_options: object
) -> documentation_command_runner.CommandResult:
    stdout = ""
    if argv == documentation_commands.EXAMPLE_COMMANDS[check_documentation_examples._INSTALLED_BASE_ARTIFACT][0]:
        stdout = json.dumps(artifact_summary_fixture())
    return documentation_command_runner.CommandResult(argv=argv, exit_code=0, stdout=stdout, stderr="")
