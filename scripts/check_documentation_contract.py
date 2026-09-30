#!/usr/bin/env python3
"""Enforce fieldkit's versioned public-documentation contract."""

import argparse
import fnmatch
import hashlib
import json
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from jsonschema import Draft202012Validator
from markdown_it import MarkdownIt

if TYPE_CHECKING or __package__:
    from scripts.documentation_commands import (
        CONTRIBUTOR_JOURNEY_EVIDENCE,
        DOCUMENT_COMMANDS,
        EXAMPLE_COMMANDS,
        OWNER_PHASES,
    )
    from scripts.documentation_manual import manual_scenarios
    from scripts.markdown_tables import markdown_tables
else:
    from documentation_commands import (
        CONTRIBUTOR_JOURNEY_EVIDENCE,
        DOCUMENT_COMMANDS,
        EXAMPLE_COMMANDS,
        OWNER_PHASES,
    )
    from documentation_manual import manual_scenarios
    from markdown_tables import markdown_tables

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONTRACT_PATH = Path("docs/documentation-contract.json")
_SURFACE_POLICY_PATH = Path("docs/release-readiness/public-surface-policy.json")
_SCHEMA_PATH = Path("docs/release-readiness/documentation-contract.schema.json")
_CONTENT_TYPES = frozenset({"concept", "how-to", "overview", "policy", "reference", "roadmap", "tutorial"})
_FUTURE_STATUS = re.compile(
    r"(?i)\b(?:in[ -]flight|backlog|coming soon|under development|we will support|"
    r"future (?:contributors?|release|work)|pending (?:implementation|release|support|work)|"
    r"not yet (?:implemented|published|supported)|planned (?:first |public )?(?:release|work))\b"
)
_VERIFICATION_EVIDENCE = {
    **{owner: shlex.join(commands[0]) for owner, commands in DOCUMENT_COMMANDS.items()},
    "contributor_gate": CONTRIBUTOR_JOURNEY_EVIDENCE,
    "live_cutover": "ROADMAP.md",
    "policy_validator": "make quality",
}
_AUTOMATED_EXAMPLE_CLASSES = frozenset({"generated_reference", "safe_automated_command", "structural_assertion"})
_MANUAL_EXAMPLE_CLASSES = frozenset({"credentialed_manual_integration", "exact_release_cutover_proof"})
_EXAMPLE_VERIFICATION_EVIDENCE = {
    "automated.threat-model-contract": (
        "structural_assertion",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.threat-model-contract"][0]),
        (
            "tests/test_threat_model_contract.py",
            "tests/test_ingest_paths.py",
            "tests/test_ingest_prepared.py",
            "tests/test_ingest_prepared_store.py",
            "tests/test_pursuit_effects.py",
            "tests/test_task_effects.py",
            "tests/test_owned_markdown.py",
            "tests/test_text_snapshot.py",
            "tests/test_atomic_text_create.py",
            "tests/test_web_server.py",
            "tests/test_cli_exit.py",
            "scripts/markdown_tables.py",
        ),
    ),
    "automated.contributor-journey": (
        "safe_automated_command",
        "automated",
        CONTRIBUTOR_JOURNEY_EVIDENCE,
        ("Makefile",),
    ),
    "automated.contributor-instruction-structure": (
        "structural_assertion",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.contributor-instruction-structure"][0]),
        ("tests/test_documentation_contributor_design.py",),
    ),
    "automated.ingest-workflow-scenarios": (
        "safe_automated_command",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.ingest-workflow-scenarios"][0]),
        ("tests/test_ingest_workflow_scenarios.py", "tests/documentation_workflow_support.py"),
    ),
    "automated.remaining-public-scenarios": (
        "safe_automated_command",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.remaining-public-scenarios"][0]),
        ("tests/test_remaining_public_skill_contracts.py", "tests/documentation_workflow_support.py"),
    ),
    "automated.remaining-public-structure": (
        "structural_assertion",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.remaining-public-structure"][0]),
        ("tests/test_remaining_public_skill_contracts.py", "scripts/markdown_tables.py"),
    ),
    "automated.brief-workflow-scenarios": (
        "safe_automated_command",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.brief-workflow-scenarios"][0]),
        ("tests/test_brief_documentation_scenarios.py", "tests/documentation_workflow_support.py"),
    ),
    "automated.watcher-workflow-scenarios": (
        "safe_automated_command",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.watcher-workflow-scenarios"][0]),
        ("tests/test_watcher_documentation_scenarios.py", "tests/documentation_workflow_support.py"),
    ),
    "automated.salesforce-auth-contract": (
        "structural_assertion",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.salesforce-auth-contract"][0]),
        ("tests/test_auth_sf.py", "tests/test_sf_session_check.py"),
    ),
    "automated.roadmap-contract": (
        "structural_assertion",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.roadmap-contract"][0]),
        ("scripts/check_roadmap_contract.py",),
    ),
    "automated.compatibility-policy": (
        "structural_assertion",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.compatibility-policy"][0]),
        ("scripts/check_compatibility_policy.py",),
    ),
    "automated.configuration-example-contract": (
        "structural_assertion",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.configuration-example-contract"][0]),
        ("tests/test_documentation_configuration_examples.py", "tests/test_config_xdg.py"),
    ),
    "automated.documentation-contract": (
        "structural_assertion",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.documentation-contract"][0]),
        ("scripts/check_documentation_contract.py",),
    ),
    "automated.exit-code-contract": (
        "structural_assertion",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.exit-code-contract"][0]),
        ("tests/test_cli_exit.py", "tests/test_documentation_exit_examples.py", "scripts/markdown_tables.py"),
    ),
    "automated.integration-profile-contract": (
        "structural_assertion",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.integration-profile-contract"][0]),
        (
            "tests/test_integrations_page_contract.py",
            "tests/test_documentation_integration_profiles.py",
            "tests/test_installation_profiles.py",
            "scripts/markdown_tables.py",
            "scripts/check_dependency_profiles.py",
        ),
    ),
    "automated.release-mode-table-contract": (
        "structural_assertion",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.release-mode-table-contract"][0]),
        ("tests/test_documentation_release_mode_table.py", "scripts/release_workflow_policy.py"),
    ),
    "automated.security-support-contract": (
        "structural_assertion",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.security-support-contract"][0]),
        ("tests/test_documentation_security_support.py", "scripts/_release_policy.py", "scripts/markdown_tables.py"),
    ),
    "automated.privacy-destination-contract": (
        "structural_assertion",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.privacy-destination-contract"][0]),
        ("tests/test_documentation_privacy_destinations.py", "scripts/markdown_tables.py"),
    ),
    "automated.pipeline-example-contract": (
        "structural_assertion",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.pipeline-example-contract"][0]),
        ("tests/test_forecast.py", "tests/test_pipeline_quota.py", "tests/test_documentation_pursuit_workflow.py"),
    ),
    "automated.meeting-template-contract": (
        "structural_assertion",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.meeting-template-contract"][0]),
        ("tests/test_tool_routing_cli_first.py",),
    ),
    "automated.meeting-report-scenarios": (
        "safe_automated_command",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.meeting-report-scenarios"][0]),
        ("tests/test_meeting_workflow_scenarios.py", "tests/documentation_workflow_support.py"),
    ),
    "automated.meeting-report-structure": (
        "structural_assertion",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.meeting-report-structure"][0]),
        ("tests/test_meeting_workflow_scenarios.py",),
    ),
    "automated.win-loss-template-contract": (
        "structural_assertion",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.win-loss-template-contract"][0]),
        ("tests/test_win_loss_skill_documentation.py",),
    ),
    "automated.humanizer-structure-contract": (
        "structural_assertion",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.humanizer-structure-contract"][0]),
        ("tests/test_humanizer_skill_documentation.py",),
    ),
    "automated.handoff-template-contract": (
        "structural_assertion",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.handoff-template-contract"][0]),
        ("tests/test_handoff_skill_documentation.py",),
    ),
    "automated.draft-review-structure-contract": (
        "structural_assertion",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.draft-review-structure-contract"][0]),
        ("tests/test_draft_review_skill_documentation.py",),
    ),
    "automated.workstream-discover-structure-contract": (
        "structural_assertion",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.workstream-discover-structure-contract"][0]),
        ("tests/test_workstream_discover_skill_documentation.py",),
    ),
    "automated.contract-workflow-structure-contract": (
        "structural_assertion",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.contract-workflow-structure-contract"][0]),
        ("tests/test_contract_skill_documentation.py",),
    ),
    "automated.generated-dependency-map": (
        "generated_reference",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.generated-dependency-map"][0]),
        ("scripts/generate_dep_map.py",),
    ),
    "automated.generated-reference": (
        "generated_reference",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.generated-reference"][0]),
        ("scripts/generate_cli_docs.py",),
    ),
    "automated.installed-base-artifact": (
        "safe_automated_command",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.installed-base-artifact"][0]),
        ("scripts/check_documentation_example_scenarios.py", "scripts/smoke_artifact.py"),
    ),
    "automated.gmail-example-contract": (
        "structural_assertion",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.gmail-example-contract"][0]),
        ("tests/test_smoke_gmail_examples.py",),
    ),
    "automated.gmail-import-scenario": (
        "safe_automated_command",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.gmail-import-scenario"][0]),
        ("tests/test_smoke_gmail_examples.py",),
    ),
    "automated.release-workflow-policy": (
        "safe_automated_command",
        "automated",
        shlex.join(EXAMPLE_COMMANDS["automated.release-workflow-policy"][0]),
        ("scripts/documentation_commands.py", "scripts/release_workflow_policy.py"),
    ),
    "manual.credentialed-integration": (
        "credentialed_manual_integration",
        "manual_evidence",
        "docs/release-readiness/rehearsal-evidence.schema.json",
        ("docs/release-readiness/rehearsal-evidence.schema.json",),
    ),
    "manual.release-cutover": (
        "exact_release_cutover_proof",
        "manual_evidence",
        "docs/release-readiness/rehearsal-evidence.schema.json",
        ("docs/release-readiness/rehearsal-evidence.schema.json",),
    ),
}


@dataclass(frozen=True, order=True)
class Finding:
    """One documentation-contract violation."""

    criterion_id: str
    path: str


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build an object while rejecting ambiguous duplicate JSON keys."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path) -> dict[str, object]:
    """Load one contract object with strict duplicate-key handling."""
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_pairs)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def _public_markdown_paths(repo_root: Path) -> frozenset[str]:
    """Resolve every Markdown file classified for the public repository."""
    policy = _load_json(repo_root / _SURFACE_POLICY_PATH)
    categories = policy.get("categories")
    if not isinstance(categories, dict):
        raise ValueError(f"{_SURFACE_POLICY_PATH}: categories must be an object")
    patterns: list[str] = []
    for category in ("public_entrypoint", "public_site", "public_repository_only"):
        values = categories.get(category)
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError(f"{_SURFACE_POLICY_PATH}: {category} must be a string list")
        patterns.extend(values)
    candidates = (
        path.relative_to(repo_root).as_posix() for path in repo_root.rglob("*.md") if ".git" not in path.parts
    )
    return frozenset(path for path in candidates if any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns))


def _skill_index_findings(repo_root: Path) -> tuple[Finding, ...]:
    """Require a complete, unique link index for packaged skill entry points."""
    skill_root = repo_root / "src/fieldkit/skills"
    if not skill_root.is_dir():
        return ()
    index = skill_root / "README.md"
    expected = [
        f"- [{path.parent.name}]({path.parent.name}/SKILL.md)"
        for path in sorted(skill_root.glob("*/SKILL.md"))
        if path.is_file()
    ]
    if index.is_file():
        lines = index.read_text(encoding="utf-8").splitlines()
        headings = [
            (position, re.sub(r"[ \t]+#+[ \t]*$", "", match[1]).strip())
            for position, line in enumerate(lines)
            if (match := re.fullmatch(r" {0,3}##[ \t]+(.*)", line))
        ]
        index_headings = [position for position, title in headings if title == "Skill index"]
        if len(index_headings) == 1:
            start = index_headings[0] + 1
            end = next((position for position, _ in headings if position >= start), len(lines))
            observed = [line for line in lines[start:end] if line.strip()]
            if observed == expected:
                return ()
    return (Finding("DOC411", "src/fieldkit/skills/README.md"),)


def _private_patterns(repo_root: Path) -> tuple[str, ...]:
    """Return source patterns excluded from the clean public repository."""
    policy = _load_json(repo_root / _SURFACE_POLICY_PATH)
    categories = policy.get("categories")
    values = categories.get("private_history_excluded") if isinstance(categories, dict) else None
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError(f"{_SURFACE_POLICY_PATH}: private_history_excluded must be a string list")
    return tuple(value for value in values if isinstance(value, str))


def _source_files(repo_root: Path, patterns: list[str]) -> tuple[Path, ...]:
    """Resolve contract source patterns without following source symlinks."""
    matches: set[Path] = set()
    for pattern in patterns:
        for path in repo_root.glob(pattern):
            if any(
                component.is_symlink() for component in (path, *path.parents) if component.is_relative_to(repo_root)
            ):
                raise ValueError(f"{_CONTRACT_PATH}: source patterns must not resolve through symlinks")
            if path.is_file():
                matches.add(path)
    return tuple(sorted(matches))


def _validate_source_patterns(patterns: list[str], document: str) -> None:
    """Reject source patterns that can escape the repository root."""
    for pattern in patterns:
        relative = PurePosixPath(pattern)
        if not pattern or pattern.startswith("/") or "\\" in pattern or ".." in relative.parts:
            raise ValueError(f"{_CONTRACT_PATH}: {document} sources must be repository-relative patterns")


def _has_unresolved_pattern(repo_root: Path, patterns: list[str]) -> bool:
    """Report whether any declared source pattern matches no file."""
    return any(not any(path.is_file() for path in repo_root.glob(pattern)) for pattern in patterns)


def _fingerprint(repo_root: Path, patterns: list[str]) -> str:
    """Hash stable repository-relative names and contents for source inputs."""
    digest = hashlib.sha256()
    for path in _source_files(repo_root, patterns):
        digest.update(path.relative_to(repo_root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


@dataclass(frozen=True)
class FencedBlock:
    """One parsed fence with command text and its newline-normalized source slice."""

    language: str
    body: str
    source_body: str


def fenced_blocks(document: Path) -> list[FencedBlock]:
    """Parse CommonMark fences while rejecting implicitly closed blocks."""
    text = document.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    blocks: list[FencedBlock] = []
    for token in MarkdownIt("commonmark").parse(text):
        if token.type != "fence":
            continue
        if token.map is None:
            raise ValueError(f"{document}: fenced block has no source range")
        start, end = token.map
        # CommonMark permits EOF to close a fence. Release evidence does not:
        # an explicit closing line is excluded from the parsed body line count.
        if len(token.content.splitlines()) != end - start - 2:
            raise ValueError(f"{document}: unclosed fenced block")
        info = token.info.strip()
        blocks.append(
            FencedBlock(info.split(maxsplit=1)[0] if info else "", token.content, "".join(lines[start + 1 : end - 1]))
        )
    return blocks


def _block_inventory(document: Path) -> list[dict[str, str]]:
    """Bind every parsed fence to its reviewed textual source content."""
    return [
        {
            "language": block.language,
            "sha256": hashlib.sha256(f"{block.language}\0{block.source_body}".encode()).hexdigest(),
        }
        for block in fenced_blocks(document)
    ]


def _table_inventory(document: Path) -> list[dict[str, str]]:
    """Inventory complete pipe-table structures by their reviewed content."""
    return [{"sha256": hashlib.sha256(table.source_text.encode()).hexdigest()} for table in markdown_tables(document)]


def _documents(contract: dict[str, object]) -> dict[str, dict[str, object]]:
    """Validate the contract envelope and return its document records."""
    if contract.get("schema_version") != 2:
        raise ValueError(f"{_CONTRACT_PATH}: schema_version must be 2")
    raw_documents = contract.get("documents")
    if not isinstance(raw_documents, dict):
        raise ValueError(f"{_CONTRACT_PATH}: documents must be an object")
    documents: dict[str, dict[str, object]] = {}
    for path, value in raw_documents.items():
        if not isinstance(path, str) or not isinstance(value, dict):
            raise ValueError(f"{_CONTRACT_PATH}: document entries must be objects keyed by paths")
        documents[path] = value
    return documents


def _validate_schema(repo_root: Path, contract: dict[str, object]) -> None:
    """Validate the complete contract against its checked JSON Schema."""
    schema = _load_json(repo_root / _SCHEMA_PATH)
    json_instance = json.loads(json.dumps(contract))
    errors = sorted(
        Draft202012Validator(schema).iter_errors(json_instance), key=lambda error: list(error.absolute_path)
    )
    if not errors:
        return
    first = errors[0]
    location = ".".join(str(part) for part in first.absolute_path) or "<root>"
    raise ValueError(f"{_CONTRACT_PATH}: schema violation at {location}: {first.message}")


def _validate_example_ownership(contract: dict[str, object], documents: dict[str, dict[str, object]]) -> None:
    """Require stable, unique ownership for every fenced block and table."""
    automated_owners = {
        owner for owner, metadata in _EXAMPLE_VERIFICATION_EVIDENCE.items() if metadata[1] == "automated"
    }
    if set(EXAMPLE_COMMANDS) != automated_owners:
        raise ValueError("automated command registry does not match ownership")
    raw_verifications = contract.get("example_verifications")
    if not isinstance(raw_verifications, dict):
        raise ValueError(f"{_CONTRACT_PATH}: example_verifications must be an object")
    if set(raw_verifications) != set(_EXAMPLE_VERIFICATION_EVIDENCE):
        raise ValueError(f"{_CONTRACT_PATH}: example verifications must be the supported set")
    for expected_verification_id, expected in _EXAMPLE_VERIFICATION_EVIDENCE.items():
        verification = raw_verifications.get(expected_verification_id)
        if not isinstance(verification, dict):
            raise ValueError(f"{_CONTRACT_PATH}: missing example verification: {expected_verification_id}")
        expected_classification, expected_mode, expected_evidence, _ = expected
        expected_route = {
            "classification": expected_classification,
            "mode": expected_mode,
            "evidence": expected_evidence,
        }
        if OWNER_PHASES.get(expected_verification_id) == "contributor_journey" or "phase" in verification:
            expected_route["phase"] = OWNER_PHASES[expected_verification_id]
        if verification != expected_route:
            raise ValueError(f"{_CONTRACT_PATH}: unsupported example verification route: {expected_verification_id}")
    identifiers: list[str] = []
    for path, entry in documents.items():
        if path == "AGENTS.md":
            blocks = entry.get("fenced_blocks")
            if (
                not isinstance(blocks, list)
                or not blocks
                or not isinstance(blocks[0], dict)
                or (
                    blocks[0].get("id") != "agents.md.block-1"
                    or blocks[0].get("verification_id") != "automated.contributor-journey"
                )
            ):
                raise ValueError(f"{_CONTRACT_PATH}: contributor journey subject requires its canonical owner")
        for field, subject in (("fenced_blocks", "fenced block"), ("tables", "table")):
            records = entry.get(field, [])
            if not isinstance(records, list):
                raise ValueError(f"{_CONTRACT_PATH}: {path} {field} must be a list")
            for record in records:
                if not isinstance(record, dict):
                    raise ValueError(f"{_CONTRACT_PATH}: {path} {subject} entries must be objects")
                identifier = record.get("id")
                classification = record.get("classification")
                verification_id = record.get("verification_id")
                if (
                    path == "AGENTS.md"
                    and identifier == "agents.md.block-1"
                    and verification_id != "automated.contributor-journey"
                ):
                    raise ValueError(f"{_CONTRACT_PATH}: contributor journey subject requires its canonical owner")
                if not isinstance(identifier, str) or not isinstance(verification_id, str):
                    raise ValueError(f"{_CONTRACT_PATH}: {path} {subject}s need stable ownership identifiers")
                identifiers.append(identifier)
                verification = raw_verifications.get(verification_id)
                if OWNER_PHASES.get(verification_id) == "contributor_journey" and (
                    path != "AGENTS.md" or identifier != "agents.md.block-1" or field != "fenced_blocks"
                ):
                    raise ValueError(f"{_CONTRACT_PATH}: contributor journey owner has an unsupported subject")
                if not isinstance(verification, dict) or verification.get("classification") != classification:
                    raise ValueError(
                        f"{_CONTRACT_PATH}: {identifier} has unknown or incompatible verification ownership"
                    )
                mode = verification.get("mode")
                if classification in _AUTOMATED_EXAMPLE_CLASSES and mode != "automated":
                    raise ValueError(f"{_CONTRACT_PATH}: {identifier} automated verification has an unsafe mode")
                if classification in _MANUAL_EXAMPLE_CLASSES and mode != "manual_evidence":
                    raise ValueError(f"{_CONTRACT_PATH}: {identifier} manual verification has an unsafe mode")
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"{_CONTRACT_PATH}: fenced block identifiers must be unique")
    manual_scenarios(documents)


def _validate_verification_ownership(contract: dict[str, object], documents: dict[str, dict[str, object]]) -> None:
    """Require exactly one supported verification owner per public document."""
    raw = contract.get("verification")
    if not isinstance(raw, dict) or set(raw) != set(_VERIFICATION_EVIDENCE):
        raise ValueError(f"{_CONTRACT_PATH}: verification mechanisms must be the supported set")
    owners: list[str] = []
    for mechanism, value in raw.items():
        expected_keys = {"evidence", "paths"}
        if OWNER_PHASES[mechanism] == "contributor_journey" or (isinstance(value, dict) and "phase" in value):
            expected_keys.add("phase")
        if not isinstance(value, dict) or set(value) != expected_keys:
            raise ValueError(f"{_CONTRACT_PATH}: verification entries need evidence and paths")
        if value.get("phase", "leaf") != OWNER_PHASES[mechanism]:
            raise ValueError(f"{_CONTRACT_PATH}: unsupported document verification phase: {mechanism}")
        paths = value.get("paths")
        if isinstance(paths, list) and "CONTRIBUTING.md" in paths and mechanism != "contributor_gate":
            raise ValueError(f"{_CONTRACT_PATH}: contributor journey document requires its canonical owner")
        if mechanism == "contributor_gate" and "CONTRIBUTING.md" in documents and paths != ["CONTRIBUTING.md"]:
            raise ValueError(f"{_CONTRACT_PATH}: contributor journey document requires its canonical owner")
        if OWNER_PHASES[mechanism] == "contributor_journey" and paths not in ([], ["CONTRIBUTING.md"]):
            raise ValueError(f"{_CONTRACT_PATH}: contributor journey document owner has unsupported paths")
        if (
            value.get("evidence") != _VERIFICATION_EVIDENCE[mechanism]
            or not isinstance(paths, list)
            or not all(isinstance(path, str) for path in paths)
        ):
            raise ValueError(f"{_CONTRACT_PATH}: verification evidence must match the enforced route")
        owners.extend(path for path in paths if isinstance(path, str))
    if len(owners) != len(set(owners)) or set(owners) != set(documents):
        raise ValueError(f"{_CONTRACT_PATH}: every document needs exactly one verification owner")


def _validate_verification_evidence(repo_root: Path) -> None:
    """Ensure every declared verification route exists in the repository."""
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    required_targets = {
        "pr-check:": _VERIFICATION_EVIDENCE["contributor_gate"],
        "quality:": "make quality",
    }
    for target, evidence in required_targets.items():
        if target not in makefile:
            raise ValueError(f"{_CONTRACT_PATH}: verification evidence is unavailable: {evidence}")
    required_files = (
        "docs/release-readiness/documentation-contract.schema.json",
        "scripts/generate_cli_docs.py",
        "scripts/generate_dep_map.py",
        "scripts/check_documentation_contract.py",
        "scripts/check_documentation_examples.py",
        "scripts/check_skill_documentation_contract.py",
        "ROADMAP.md",
    )
    for relative in required_files:
        if not (repo_root / relative).is_file():
            raise ValueError(f"{_CONTRACT_PATH}: verification evidence is unavailable: {relative}")
    for _, _, _, evidence_paths in _EXAMPLE_VERIFICATION_EVIDENCE.values():
        for relative in evidence_paths:
            if not (repo_root / relative).is_file():
                raise ValueError(f"{_CONTRACT_PATH}: example verification evidence is unavailable: {relative}")


def _has_future_status(path: str, text: str) -> bool:
    """Keep project plans in ROADMAP without mistaking literal workspace task headings for plans."""
    runtime_heading = {
        "src/fieldkit/skills/task-management/SKILL.md": "In TASKS.md, the local queued section is named `## Backlog`.",
        "src/fieldkit/skills/task-sync/SKILL.md": "The `Backlog` section in TASKS.md is local and outside the sync markers.",
    }.get(path)
    if runtime_heading is not None:
        text = "\n\n".join(
            paragraph for paragraph in text.split("\n\n") if " ".join(paragraph.split()) != runtime_heading
        )
    return _FUTURE_STATUS.search(text) is not None


def validate(repo_root: Path = _REPO_ROOT) -> tuple[Finding, ...]:
    """Return all fail-closed public-documentation contract findings."""
    contract = _load_json(repo_root / _CONTRACT_PATH)
    _validate_schema(repo_root, contract)
    documents = _documents(contract)
    _validate_verification_ownership(contract, documents)
    _validate_example_ownership(contract, documents)
    _validate_verification_evidence(repo_root)
    roadmap = contract.get("roadmap")
    if not isinstance(roadmap, str) or documents.get(roadmap, {}).get("content_type") != "roadmap":
        raise ValueError(f"{_CONTRACT_PATH}: roadmap must name the roadmap document")
    findings: list[Finding] = list(_skill_index_findings(repo_root))
    public_paths = _public_markdown_paths(repo_root)
    private_patterns = _private_patterns(repo_root)
    for path in sorted(public_paths - documents.keys()):
        findings.append(Finding("DOC401", path))
    for path in sorted(documents.keys() - public_paths):
        findings.append(Finding("DOC402", path))
    for path, entry in sorted(documents.items()):
        document = repo_root / path
        if not document.is_file():
            findings.append(Finding("DOC403", path))
            continue
        content_type = entry.get("content_type")
        reader_action = entry.get("reader_action")
        sources = entry.get("sources")
        if content_type not in _CONTENT_TYPES or not isinstance(reader_action, str) or not reader_action.strip():
            findings.append(Finding("DOC404", path))
            continue
        if not isinstance(sources, list) or not sources or not all(isinstance(source, str) for source in sources):
            findings.append(Finding("DOC405", path))
            continue
        source_patterns = [source for source in sources if isinstance(source, str)]
        _validate_source_patterns(source_patterns, path)
        if _has_unresolved_pattern(repo_root, source_patterns):
            findings.append(Finding("DOC405", path))
            continue
        source_paths = _source_files(repo_root, source_patterns)
        if any(
            any(fnmatch.fnmatchcase(source.relative_to(repo_root).as_posix(), pattern) for pattern in private_patterns)
            for source in source_paths
        ):
            findings.append(Finding("DOC409", path))
        if entry.get("source_fingerprint") != _fingerprint(repo_root, source_patterns):
            findings.append(Finding("DOC406", path))
        if path != roadmap and _has_future_status(path, document.read_text(encoding="utf-8")):
            findings.append(Finding("DOC407", path))
        declared_blocks = entry.get("fenced_blocks")
        reviewed_inventory = (
            [{"language": block.get("language"), "sha256": block.get("sha256")} for block in declared_blocks]
            if isinstance(declared_blocks, list) and all(isinstance(block, dict) for block in declared_blocks)
            else None
        )
        if reviewed_inventory != _block_inventory(document):
            findings.append(Finding("DOC408", path))
        declared_tables = entry.get("tables", [])
        reviewed_tables = (
            [{"sha256": table.get("sha256")} for table in declared_tables]
            if isinstance(declared_tables, list) and all(isinstance(table, dict) for table in declared_tables)
            else None
        )
        if reviewed_tables != _table_inventory(document):
            findings.append(Finding("DOC410", path))
    return tuple(sorted(findings))


def refresh_fingerprints(repo_root: Path = _REPO_ROOT, *, paths: tuple[str, ...] | None = None) -> None:
    """Refresh only selected reviewed documents, or all documents for fixture setup."""
    contract_path = repo_root / _CONTRACT_PATH
    contract = _load_json(contract_path)
    documents = _documents(contract)
    selected = tuple(dict.fromkeys(paths)) if paths is not None else tuple(documents)
    if not selected:
        raise ValueError("no documents selected for fingerprint refresh")
    unknown = set(selected) - set(documents)
    if unknown:
        raise ValueError(f"unknown document for fingerprint refresh: {sorted(unknown)}")
    for path in selected:
        entry = documents[path]
        sources = entry.get("sources")
        if not isinstance(sources, list) or not sources or not all(isinstance(source, str) for source in sources):
            raise ValueError(f"{_CONTRACT_PATH}: {path} has invalid sources")
        source_patterns = [source for source in sources if isinstance(source, str)]
        _validate_source_patterns(source_patterns, path)
        if _has_unresolved_pattern(repo_root, source_patterns):
            raise ValueError(f"{_CONTRACT_PATH}: {path} has an unresolved source pattern")
        entry["source_fingerprint"] = _fingerprint(repo_root, source_patterns)
    temporary = contract_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(contract_path)


def refresh_block_inventory(repo_root: Path = _REPO_ROOT) -> None:
    """Refresh fenced-block hashes after every example has been reviewed."""
    contract_path = repo_root / _CONTRACT_PATH
    contract = _load_json(contract_path)
    for path, entry in _documents(contract).items():
        document = repo_root / path
        if not document.is_file():
            raise ValueError(f"{_CONTRACT_PATH}: {path} does not exist")
        current = entry.get("fenced_blocks")
        inventory = _block_inventory(document)
        if not isinstance(current, list) or len(current) != len(inventory):
            raise ValueError(f"{_CONTRACT_PATH}: {path} block ownership must be reviewed before refreshing hashes")
        refreshed: list[dict[str, object]] = []
        for declared, observed in zip(current, inventory, strict=True):
            if not isinstance(declared, dict) or not {"id", "classification", "verification_id"} <= set(declared):
                raise ValueError(f"{_CONTRACT_PATH}: {path} block ownership must be reviewed before refreshing hashes")
            reviewed = {"language": declared.get("language"), "sha256": declared.get("sha256")}
            if reviewed != observed:
                raise ValueError(f"{_CONTRACT_PATH}: {path} changed blocks require explicit ownership review")
            refreshed.append(declared)
        entry["fenced_blocks"] = refreshed
    temporary = contract_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(contract_path)


def main(argv: list[str] | None = None) -> int:
    """Run the contract check or explicitly refresh reviewed fingerprints."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=_REPO_ROOT)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--refresh-path", action="append", default=[])
    parser.add_argument("--refresh-blocks", action="store_true")
    args = parser.parse_args(argv)
    if args.refresh and not args.refresh_path:
        parser.error("--refresh requires at least one --refresh-path")
    if args.refresh_path and not args.refresh:
        parser.error("--refresh-path requires --refresh")
    try:
        if args.refresh:
            refresh_fingerprints(args.repo_root, paths=tuple(args.refresh_path))
        if args.refresh_blocks:
            refresh_block_inventory(args.repo_root)
        findings = validate(args.repo_root)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"Documentation contract: ERROR: {error}", file=sys.stderr)
        return 2
    if findings:
        for finding in findings:
            print(f"{finding.criterion_id}: {finding.path}")
        return 1
    print("Documentation contract: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
