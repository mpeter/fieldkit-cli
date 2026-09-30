#!/usr/bin/env python3
"""Run fixed automated owners for public examples and report pending rehearsals."""

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import ExitStack
from dataclasses import asdict
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    canonicalize_name,
    parse_sdist_filename,
    parse_wheel_filename,
)
from packaging.version import Version

if TYPE_CHECKING or __package__:
    from scripts.documentation_candidate_execution import (
        BoundExecution,
        diagnostic_tools,
        execute_bound_candidate,
        external_report,
    )
    from scripts.documentation_command_runner import CommandResult, _run
    from scripts.documentation_commands import (
        CONTRIBUTOR_JOURNEY_EVIDENCE,
        DOCUMENT_COMMANDS,
        OWNER_PHASES,
    )
    from scripts.documentation_commands import EXAMPLE_COMMANDS as _AUTOMATED_COMMANDS
    from scripts.documentation_dependencies import prepare_dependencies
    from scripts.documentation_manual import manual_scenarios
    from scripts.documentation_runtime import DependencyInput, RuntimeTools
    from scripts.json_policy import load_json_bytes
    from scripts.rehearsal_acquisition import acquire_rehearsal, open_real_directory, read_relative_bytes
    from scripts.rehearsal_evidence import MAX_EVIDENCE_BYTES, RehearsalValidation
else:
    from documentation_candidate_execution import (
        BoundExecution,
        diagnostic_tools,
        execute_bound_candidate,
        external_report,
    )
    from documentation_command_runner import CommandResult, _run
    from documentation_commands import (
        CONTRIBUTOR_JOURNEY_EVIDENCE,
        DOCUMENT_COMMANDS,
        OWNER_PHASES,
    )
    from documentation_commands import EXAMPLE_COMMANDS as _AUTOMATED_COMMANDS
    from documentation_dependencies import prepare_dependencies
    from documentation_manual import manual_scenarios
    from documentation_runtime import DependencyInput, RuntimeTools
    from json_policy import load_json_bytes
    from rehearsal_acquisition import acquire_rehearsal, open_real_directory, read_relative_bytes
    from rehearsal_evidence import MAX_EVIDENCE_BYTES, RehearsalValidation

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONTRACT_PATH = Path("docs/documentation-contract.json")
_REHEARSAL_SCHEMA_PATH = Path("docs/release-readiness/rehearsal-evidence.schema.json")
_GENERATED_REFERENCE = "automated.generated-reference"
_GENERATED_DEPENDENCY_MAP = "automated.generated-dependency-map"
_INSTALLED_BASE_ARTIFACT = "automated.installed-base-artifact"
_COMPATIBILITY_POLICY = "automated.compatibility-policy"
_CONFIGURATION_EXAMPLE_CONTRACT = "automated.configuration-example-contract"
_DOCUMENTATION_CONTRACT = "automated.documentation-contract"
_EXIT_CODE_CONTRACT = "automated.exit-code-contract"
_PIPELINE_EXAMPLE_CONTRACT = "automated.pipeline-example-contract"
_ROADMAP_CONTRACT = "automated.roadmap-contract"
_RELEASE_WORKFLOW_POLICY = "automated.release-workflow-policy"
_MEETING_TEMPLATE_CONTRACT = "automated.meeting-template-contract"
_WIN_LOSS_TEMPLATE_CONTRACT = "automated.win-loss-template-contract"
_HUMANIZER_STRUCTURE_CONTRACT = "automated.humanizer-structure-contract"
_HANDOFF_TEMPLATE_CONTRACT = "automated.handoff-template-contract"
_DRAFT_REVIEW_STRUCTURE_CONTRACT = "automated.draft-review-structure-contract"
_WORKSTREAM_DISCOVER_STRUCTURE_CONTRACT = "automated.workstream-discover-structure-contract"
_CONTRACT_WORKFLOW_STRUCTURE_CONTRACT = "automated.contract-workflow-structure-contract"
_PENDING_DOCUMENT_VERIFICATIONS = frozenset({"contributor_gate", "live_cutover", "policy_validator"})
_QUALIFIED_DOCUMENT_VERIFICATIONS = frozenset(
    {
        "configuration_contract",
        "generated_dependency_map",
        "generated_reference",
        "init_guide_contract",
        "morning_brief_guide_contract",
        "pipeline_workflow_guide_contract",
        "troubleshooting_guide_contract",
        "watcher_guide_contract",
    }
)
_OWNER_RESOURCE_SLOTS = 4
# Performance heuristics only: the frozen plan remains the execution and evidence contract.
_EXPENSIVE_OWNER_COMMANDS = (
    _AUTOMATED_COMMANDS[_INSTALLED_BASE_ARTIFACT][0],
    DOCUMENT_COMMANDS["first_user_guides_contract"][0],
    DOCUMENT_COMMANDS["integrations_page_contract"][0],
    DOCUMENT_COMMANDS["configuration_contract"][0],
    _AUTOMATED_COMMANDS[_CONFIGURATION_EXAMPLE_CONTRACT][0],
    DOCUMENT_COMMANDS["pipeline_workflow_guide_contract"][0],
    _AUTOMATED_COMMANDS[_PIPELINE_EXAMPLE_CONTRACT][0],
)


def _owner_weight(argv: tuple[str, ...]) -> int:
    """Reserve both slots used by the concurrent wheel and sdist smokes."""
    return 2 if argv == _AUTOMATED_COMMANDS[_INSTALLED_BASE_ARTIFACT][0] else 1


def _run_owners(
    commands: tuple[tuple[str, ...], ...], run: Callable[[tuple[str, ...]], CommandResult]
) -> list[CommandResult]:
    """Admit fixed owners within four resource slots and report in plan order."""
    priorities = {argv: index for index, argv in enumerate(_EXPENSIVE_OWNER_COMMANDS)}
    pending = sorted(range(len(commands)), key=lambda index: (priorities.get(commands[index], len(priorities)), index))
    active: dict[Future[CommandResult], tuple[int, int]] = {}
    results: dict[int, CommandResult] = {}
    errors: dict[int, BaseException] = {}
    occupied = 0
    with ThreadPoolExecutor(max_workers=_OWNER_RESOURCE_SLOTS) as executor:
        while pending or active:
            while pending and occupied + _owner_weight(commands[pending[0]]) <= _OWNER_RESOURCE_SLOTS:
                index = pending.pop(0)
                weight = _owner_weight(commands[index])
                active[executor.submit(run, commands[index])] = (index, weight)
                occupied += weight
            completed, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in sorted(completed, key=lambda item: active[item][0]):
                index, weight = active.pop(future)
                error = future.exception()
                if error is None:
                    results[index] = future.result()
                else:
                    errors[index] = error
                occupied -= weight
    if errors:
        raise errors[min(errors)]
    return [results[index] for index in range(len(commands))]


def _summary_header(
    report: object, *, diagnostic: bool
) -> tuple[dict[str, list[str]], list[str], list[dict[str, object]]] | None:
    """Require the producer's complete header and two artifact records."""
    if not isinstance(report, dict) or set(report) != {
        "schema_version",
        "status",
        "scenarios",
        "failures",
        "artifacts",
    }:
        return None
    if type(report["schema_version"]) is not int or report["schema_version"] != 1:
        return None
    status, failures, scenarios = report["status"], report["failures"], report["scenarios"]
    if status not in (("diagnostic", "fail") if diagnostic else ("pass",)):
        return None
    if not isinstance(failures, list) or not all(isinstance(item, str) and item for item in failures):
        return None
    if (status == "fail") != bool(failures):
        return None
    if not isinstance(scenarios, dict) or not scenarios:
        return None
    for block, identifiers in scenarios.items():
        if (
            not isinstance(block, str)
            or not block
            or not isinstance(identifiers, list)
            or not identifiers
            or not all(isinstance(item, str) and re.fullmatch(r"SMOKE[0-9]{3}", item) for item in identifiers)
            or len(set(identifiers)) != len(identifiers)
        ):
            return None
    raw = report["artifacts"]
    if not isinstance(raw, list) or len(raw) != 2 or not all(isinstance(item, dict) for item in raw):
        return None
    return scenarios, failures, raw


def _summary_identity(name: object, *, expected_name: str, expected_version: Version) -> str | None:
    """Require a safe wheel or sdist filename for the selected candidate."""
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.+-]+", name):
        return None
    try:
        if name.endswith(".whl"):
            distribution, version, _build, _tags = parse_wheel_filename(name)
            kind = "wheel"
        elif name.endswith(".tar.gz"):
            distribution, version = parse_sdist_filename(name)
            kind = "sdist"
        else:
            return None
    except (InvalidWheelFilename, InvalidSdistFilename):
        return None
    return kind if distribution == canonicalize_name(expected_name) and version == expected_version else None


def _summary_outcomes(criteria: object) -> dict[str, str] | None:
    """Reject malformed or duplicate dirty smoke outcomes."""
    if not isinstance(criteria, list) or not criteria:
        return None
    outcomes: dict[str, str] = {}
    for criterion in criteria:
        if not isinstance(criterion, dict) or set(criterion) != {"criterion_id", "status"}:
            return None
        identifier, outcome = criterion["criterion_id"], criterion["status"]
        if (
            not isinstance(identifier, str)
            or not re.fullmatch(r"SMOKE[0-9]{3}", identifier)
            or identifier in outcomes
            or outcome not in ("pass", "fail")
        ):
            return None
        outcomes[identifier] = outcome
    return outcomes


def _summary_artifacts(
    report: object, *, diagnostic: bool, expected_name: str, expected_version: Version
) -> list[dict[str, object]]:
    """Validate the entire pair before retaining any artifact."""
    header = _summary_header(report, diagnostic=diagnostic)
    if header is None:
        return []
    scenarios, failures, raw = header
    artifacts: list[dict[str, object]] = []
    identities: set[str] = set()
    revisions: set[str] = set()
    observed: list[dict[str, str]] = []
    expected_failures: list[str] = []
    for artifact in raw:
        keys = {"name", "sha256", "source_revision"} | ({"source_binding", "criteria"} if diagnostic else set())
        if set(artifact) != keys:
            return []
        name, digest, revision = artifact["name"], artifact["sha256"], artifact["source_revision"]
        kind = _summary_identity(name, expected_name=expected_name, expected_version=expected_version)
        if (
            kind is None
            or kind in identities
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            return []
        identities.add(kind)
        if diagnostic:
            if revision is not None or artifact["source_binding"] != "unattested-dirty":
                return []
            outcomes = _summary_outcomes(artifact["criteria"])
            if outcomes is None:
                return []
            expected_failures.extend(
                f"{name}:{identifier}" for identifier, outcome in outcomes.items() if outcome != "pass"
            )
            observed.append(outcomes)
        elif not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
            return []
        else:
            revisions.add(revision)
        artifacts.append(dict(artifact))
    if not diagnostic and len(revisions) != 1:
        return []
    if diagnostic:
        expected_failures.extend(
            f"{block}:{identifier}"
            for block, identifiers in scenarios.items()
            for identifier in identifiers
            if any(outcomes.get(identifier) != "pass" for outcomes in observed)
        )
        if len(set(failures)) != len(failures) or set(failures) != set(expected_failures):
            return []
    return artifacts


def _artifact_digests(
    results: list[CommandResult], *, expected_name: str, expected_version: Version
) -> list[dict[str, object]]:
    """Retain exact artifact identities emitted by the installed-artifact owner."""
    artifacts: list[dict[str, object]] = []
    artifact_argv = _AUTOMATED_COMMANDS[_INSTALLED_BASE_ARTIFACT][0]
    for result in results:
        diagnostic = result.argv == (*artifact_argv, "--diagnostic-dirty")
        if (
            result.argv not in (artifact_argv, (*artifact_argv, "--diagnostic-dirty"))
            or type(result.exit_code) is not int
            or result.exit_code != int(diagnostic)
        ):
            continue
        try:
            if len(result.stdout) > MAX_EVIDENCE_BYTES:
                continue
            data = result.stdout.encode("utf-8")
            if len(data) > MAX_EVIDENCE_BYTES:
                continue
            report = load_json_bytes(data)
        except ValueError:
            continue
        artifacts.extend(
            _summary_artifacts(
                report, diagnostic=diagnostic, expected_name=expected_name, expected_version=expected_version
            )
        )
    return artifacts


def _manual_block_proofs(documents: object) -> dict[str, tuple[str, str]]:
    """Project the canonical pending registrations after catalog comparison."""
    return {scenario.identifier: (scenario.proof_type, scenario.sha256) for scenario in manual_scenarios(documents)}


def _automated_verification_identifiers(documents: object, verifications: object) -> tuple[str, ...]:
    """Return declared automated owners in stable order."""
    if not isinstance(documents, dict) or not isinstance(verifications, dict):
        raise ValueError("documentation contract verification registry is invalid")
    identifiers: set[str] = set()
    contributor_subject_seen = False
    for path, entry in documents.items():
        if not isinstance(entry, dict):
            continue
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
                raise ValueError("contributor journey subject requires its canonical owner")
        for field in ("fenced_blocks", "tables"):
            records = entry.get(field, [])
            if not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, dict):
                    continue
                verification_id = record.get("verification_id")
                if not isinstance(verification_id, str):
                    continue
                verification = verifications.get(verification_id)
                if not isinstance(verification, dict):
                    raise ValueError(f"documentation owner is not registered: {verification_id}")
                if verification.get("mode") != "automated":
                    continue
                if verification_id not in _AUTOMATED_COMMANDS:
                    raise ValueError(f"automated documentation owner has no fixed command: {verification_id}")
                if verification.get("phase", "leaf") != OWNER_PHASES[verification_id]:
                    raise ValueError(f"unsupported documentation owner phase: {verification_id}")
                if OWNER_PHASES[verification_id] == "contributor_journey":
                    if path != "AGENTS.md" or record.get("id") != "agents.md.block-1" or field != "fenced_blocks":
                        raise ValueError("contributor journey owner has an unsupported subject")
                    if contributor_subject_seen:
                        raise ValueError("contributor journey owner has a duplicate subject")
                    contributor_subject_seen = True
                    if verification.get("evidence") != CONTRIBUTOR_JOURNEY_EVIDENCE:
                        raise ValueError(f"example verification evidence does not match fixed argv: {verification_id}")
                    continue
                evidence = verification.get("evidence")
                owner_commands = _AUTOMATED_COMMANDS[verification_id]
                if (
                    not isinstance(evidence, str)
                    or len(owner_commands) != 1
                    or tuple(shlex.split(evidence)) != owner_commands[0]
                ):
                    raise ValueError(f"example verification evidence does not match fixed argv: {verification_id}")
                identifiers.add(verification_id)
    return tuple(sorted(identifiers))


def _structural_example_pending(documents: dict[str, object], verifications: dict[str, object]) -> tuple[str, ...]:
    """Keep structural-only block and table claims explicitly unproven."""
    pending: set[str] = set()
    if "automated.contributor-journey" in verifications:
        pending.add("contributor-journey:qualified-external-execution")
    for entry in documents.values():
        if not isinstance(entry, dict):
            continue
        for field in ("fenced_blocks", "tables"):
            for record in entry.get(field, []):
                owner = verifications.get(record["verification_id"])
                if isinstance(owner, dict) and (
                    owner.get("classification") == "structural_assertion"
                    or OWNER_PHASES.get(record["verification_id"]) == "contributor_journey"
                ):
                    identifier = record.get("id")
                    if not isinstance(identifier, str) or not identifier:
                        raise ValueError("structural documentation claim needs a stable identifier")
                    pending.add(f"example-claims:{identifier}")
    return tuple(sorted(pending))


def _document_verification_plan(verification: object) -> tuple[tuple[tuple[str, ...], ...], tuple[str, ...]]:
    """Return fixed commands and explicit pending pages for every document owner."""
    if verification is None:
        return (), ()
    if not isinstance(verification, dict):
        raise ValueError("documentation contract document verification registry is invalid")
    commands: list[tuple[str, ...]] = []
    pending: list[str] = []
    supported = set(DOCUMENT_COMMANDS) | set(_PENDING_DOCUMENT_VERIFICATIONS)
    if set(verification) - supported:
        raise ValueError(
            f"document verification owners have no execution plan: {sorted(set(verification) - supported)}"
        )
    for owner, entry in verification.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("paths"), list):
            raise ValueError(f"document verification owner is invalid: {owner}")
        paths = entry["paths"]
        if not all(isinstance(path, str) for path in paths):
            raise ValueError(f"document verification owner paths are invalid: {owner}")
        if entry.get("phase", "leaf") != OWNER_PHASES[owner]:
            raise ValueError(f"unsupported document verification phase: {owner}")
        if OWNER_PHASES[owner] == "contributor_journey" and entry.get("evidence") != CONTRIBUTOR_JOURNEY_EVIDENCE:
            raise ValueError(f"document verification evidence does not match fixed argv: {owner}")
        if "CONTRIBUTING.md" in paths and owner != "contributor_gate":
            raise ValueError("contributor journey document requires its canonical owner")
        if OWNER_PHASES[owner] == "contributor_journey" and paths != ["CONTRIBUTING.md"]:
            raise ValueError("contributor journey document owner has unsupported paths")
        if owner in DOCUMENT_COMMANDS:
            evidence = entry.get("evidence")
            owner_commands = DOCUMENT_COMMANDS[owner]
            if (
                not isinstance(evidence, str)
                or len(owner_commands) != 1
                or tuple(shlex.split(evidence)) != owner_commands[0]
            ):
                raise ValueError(f"document verification evidence does not match fixed argv: {owner}")
        if owner in _PENDING_DOCUMENT_VERIFICATIONS:
            pending.extend(f"document:{owner}:{path}" for path in paths)
        if owner in DOCUMENT_COMMANDS and owner not in _QUALIFIED_DOCUMENT_VERIFICATIONS:
            pending.extend(f"document-claims:{owner}:{path}" for path in paths)
        if owner in DOCUMENT_COMMANDS and paths:
            commands.extend(DOCUMENT_COMMANDS[owner])
    return tuple(commands), tuple(sorted(pending))


def _document_verification_commands(verification: object) -> tuple[tuple[str, ...], ...]:
    """Compatibility-free internal accessor for executable document owners."""
    return _document_verification_plan(verification)[0]


def _owner_commands(repo_root: Path) -> tuple[tuple[str, ...], ...]:
    """Resolve every fixed owner from the candidate contract."""
    contract, _ = _load_contract(repo_root)
    verifications = contract.get("example_verifications")
    documents = contract.get("documents")
    if not isinstance(verifications, dict) or not isinstance(documents, dict):
        raise ValueError("documentation contract verification registry is invalid")
    manual_scenarios(documents)
    document_commands, _ = _document_verification_plan(contract.get("verification"))
    commands: list[tuple[str, ...]] = [*document_commands]
    for verification_id in _automated_verification_identifiers(documents, verifications):
        if verification_id not in verifications:
            raise ValueError(f"missing automated verification: {verification_id}")
        commands.extend(_AUTOMATED_COMMANDS[verification_id])
    return tuple(dict.fromkeys(commands))


def _selected_file_bytes(path: Path) -> bytes:
    """Acquire a bounded file without following symlinked path components."""
    descriptor = open_real_directory(path.parent)
    try:
        return read_relative_bytes(descriptor, path.name, maximum_bytes=MAX_EVIDENCE_BYTES)
    finally:
        os.close(descriptor)


def _load_contract(repo_root: Path) -> tuple[dict[str, object], bytes]:
    """Read one bounded canonical control without reflecting its contents."""
    try:
        data = _selected_file_bytes(repo_root / _CONTRACT_PATH)
        contract = load_json_bytes(data)
        if not isinstance(contract, dict):
            raise ValueError("documentation contract must be an object")
        return contract, data
    except (OSError, ValueError):
        raise ValueError("documentation contract is invalid or unavailable") from None


def _acquire_rehearsal_from_paths(
    repo_root: Path,
    *,
    evidence_path: Path | None,
    candidate_report_path: Path | None,
    member_root: Path | None,
    bundle_root: Path | None,
    private_candidate_report_path: Path | None = None,
) -> RehearsalValidation | None:
    """Acquire once before owners; selected roots confer no protected authority."""
    if evidence_path is None:
        if any(
            path is not None
            for path in (candidate_report_path, member_root, bundle_root, private_candidate_report_path)
        ):
            raise ValueError("rehearsal inputs require explicit evidence")
        return None
    if candidate_report_path is None or member_root is None or bundle_root is None:
        raise ValueError("rehearsal evidence requires explicit report, member root, and candidate bundle")
    try:
        receipt_bytes = _selected_file_bytes(evidence_path)
        report_bytes = _selected_file_bytes(candidate_report_path)
        private_report_bytes = (
            _selected_file_bytes(private_candidate_report_path) if private_candidate_report_path is not None else None
        )
        schema_bytes = _selected_file_bytes(repo_root / _REHEARSAL_SCHEMA_PATH)
        contract_bytes = _selected_file_bytes(repo_root / _CONTRACT_PATH)
        with ExitStack() as resources:
            evidence_fd = open_real_directory(member_root)
            resources.callback(os.close, evidence_fd)
            bundle_fd = open_real_directory(bundle_root)
            resources.callback(os.close, bundle_fd)
            diagnostic = acquire_rehearsal(
                receipt_bytes,
                schema_bytes=schema_bytes,
                evidence_root_fd=evidence_fd,
                bundle_root_fd=bundle_fd,
                candidate_report_bytes=report_bytes,
                private_candidate_report_bytes=private_report_bytes,
            )
            if diagnostic.subject.documentation_contract_sha256 != hashlib.sha256(contract_bytes).hexdigest():
                raise ValueError("rehearsal documentation contract differs from selected source")
            return diagnostic
    except (OSError, ValueError):
        raise ValueError("rehearsal input is invalid or unavailable") from None


def check(
    repo_root: Path = _REPO_ROOT,
    *,
    rehearsal_validation: RehearsalValidation | None = None,
    runtime_root: Path | None = None,
    runtime_tools: RuntimeTools | None = None,
    dependency_input: DependencyInput | None = None,
    source_revision: str | None = None,
    diagnostic_dirty: bool = False,
    execution_commands: tuple[tuple[str, ...], ...] | None = None,
) -> tuple[list[CommandResult], tuple[str, ...]]:
    """Run every automated owner and return explicit pending block identifiers."""
    if diagnostic_dirty and source_revision is not None:
        raise ValueError("dirty diagnostic artifacts cannot claim a source revision")
    contract, contract_bytes = _load_contract(repo_root)
    if (
        rehearsal_validation is not None
        and rehearsal_validation.subject.documentation_contract_sha256 != hashlib.sha256(contract_bytes).hexdigest()
    ):
        raise ValueError("rehearsal documentation contract differs from selected source")
    manual_blocks = _manual_block_proofs(contract.get("documents"))
    contract_result = _run(
        repo_root,
        DOCUMENT_COMMANDS["source_contract"][0],
        runtime_root=runtime_root,
        runtime_tools=runtime_tools,
        dependency_input=dependency_input,
        source_revision=source_revision,
    )
    if contract_result.exit_code != 0:
        raise ValueError("documentation contract validation failed")
    verifications = contract.get("example_verifications")
    documents = contract.get("documents")
    if not isinstance(verifications, dict) or not isinstance(documents, dict):
        raise ValueError("documentation contract verification registry is invalid")
    commands: list[tuple[str, ...]] = []
    document_commands, document_pending = _document_verification_plan(contract.get("verification"))
    commands.extend(document_commands)
    for verification_id in _automated_verification_identifiers(documents, verifications):
        if verification_id not in verifications:
            raise ValueError(f"missing automated verification: {verification_id}")
        commands.extend(_AUTOMATED_COMMANDS[verification_id])
    commands = list(dict.fromkeys(commands))
    frozen_commands = tuple(commands)
    if execution_commands is not None and execution_commands != frozen_commands:
        raise ValueError("frozen documentation execution plan does not match the candidate contract")
    run_owner = partial(
        _run,
        repo_root,
        runtime_root=runtime_root,
        runtime_tools=runtime_tools,
        dependency_input=dependency_input,
        source_revision=source_revision,
    )

    def run_command(command: tuple[str, ...]) -> CommandResult:
        if diagnostic_dirty and command == _AUTOMATED_COMMANDS[_INSTALLED_BASE_ARTIFACT][0]:
            return run_owner((*command, "--diagnostic-dirty"))
        return run_owner(command)

    results = _run_owners(execution_commands or frozen_commands, run_command)
    structural_pending = _structural_example_pending(documents, verifications)
    rehearsal_pending = rehearsal_validation.pending_scenarios if rehearsal_validation is not None else ()
    pending = tuple(sorted({*manual_blocks, *document_pending, *structural_pending, *rehearsal_pending}))
    return results, pending


def main(argv: list[str] | None = None, *, dependency_input: DependencyInput | None = None) -> int:
    """Run automated checks, optionally requiring all rehearsal evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=_REPO_ROOT)
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--rehearsal-evidence", type=Path)
    parser.add_argument("--candidate-report", type=Path)
    parser.add_argument("--private-candidate-report", type=Path)
    parser.add_argument("--rehearsal-member-root", type=Path)
    parser.add_argument("--candidate-bundle", type=Path)
    parser.add_argument(
        "--report",
        metavar="PATH|-",
        help="Write JSON to '-' or to a path on a filesystem separate from the candidate",
    )
    parser.add_argument("--trusted-uv-sha256")
    args = parser.parse_args(argv)
    resources = ExitStack()
    report_to_stdout = args.report == "-"
    try:
        repo_root = args.repo_root.absolute()
        try:
            project = tomllib.loads(_selected_file_bytes(repo_root / "pyproject.toml").decode("utf-8")).get("project")
            if (
                not isinstance(project, dict)
                or not isinstance(project.get("name"), str)
                or not isinstance(project.get("version"), str)
            ):
                raise ValueError("candidate project metadata requires name and version")
            expected_name = canonicalize_name(project["name"], validate=True)
            expected_version = Version(project["version"])
        except ValueError:
            raise ValueError("candidate project metadata requires a valid package name and version") from None
        rehearsal_validation = _acquire_rehearsal_from_paths(
            repo_root,
            evidence_path=args.rehearsal_evidence,
            candidate_report_path=args.candidate_report,
            member_root=args.rehearsal_member_root,
            bundle_root=args.candidate_bundle,
            private_candidate_report_path=args.private_candidate_report,
        )
        report_target = resources.enter_context(external_report(repo_root, args.report))

        def diagnostic_runner(
            source: Path, owner_commands: tuple[tuple[str, ...], ...]
        ) -> tuple[list[dict[str, object]], tuple[str, ...]]:
            with (
                diagnostic_tools() as tools,
                prepare_dependencies(source, tools, supplied=dependency_input) as dependencies,
            ):
                diagnostic_results, diagnostic_pending = check(
                    source,
                    rehearsal_validation=rehearsal_validation,
                    runtime_root=source,
                    runtime_tools=tools,
                    dependency_input=dependencies,
                    source_revision=None,
                    diagnostic_dirty=True,
                    execution_commands=owner_commands,
                )
                return [asdict(result) for result in diagnostic_results], diagnostic_pending

        def supervised_runner(context: BoundExecution) -> tuple[list[dict[str, object]], tuple[str, ...]]:
            supervised_results, supervised_pending = check(
                context.snapshot,
                rehearsal_validation=rehearsal_validation,
                runtime_root=context.runtime_root,
                runtime_tools=context.tools,
                dependency_input=context.dependency_input,
                source_revision=context.source_revision,
                execution_commands=context.commands,
            )
            return [asdict(result) for result in supervised_results], supervised_pending

        raw_results, pending, binding = execute_bound_candidate(
            repo_root,
            _owner_commands,
            diagnostic_runner,
            supervised_runner=supervised_runner,
            trusted_uv_sha256=args.trusted_uv_sha256,
            dependency_input=dependency_input,
        )
        results = []
        for raw_result in raw_results:
            result_argv = raw_result.get("argv")
            exit_code = raw_result.get("exit_code")
            stdout = raw_result.get("stdout")
            stderr = raw_result.get("stderr")
            if (
                not isinstance(result_argv, (list, tuple))
                or not all(isinstance(part, str) for part in result_argv)
                or not isinstance(exit_code, int)
                or isinstance(exit_code, bool)
                or not isinstance(stdout, str)
                or not isinstance(stderr, str)
            ):
                raise ValueError("documentation supervisor produced an invalid command result")
            results.append(CommandResult(tuple(result_argv), exit_code, stdout, stderr))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        resources.close()
        print(f"Documentation examples: ERROR: {error}", file=sys.stderr)
        return 2
    failed = [result for result in results if result.exit_code != 0]
    bound_release_candidate = bool(
        binding["clean"]
        and binding["immutable_snapshot"]
        and binding["runtime_environment_checked"]
        and binding["runtime_tool_approval"] == "approved"
        and binding["source_unchanged"]
    )
    status = "fail" if failed else "pending" if pending or not bound_release_candidate else "pass"
    artifacts = _artifact_digests(results, expected_name=expected_name, expected_version=expected_version)
    artifact_owner_passed = any(
        result.argv == _AUTOMATED_COMMANDS[_INSTALLED_BASE_ARTIFACT][0] and result.exit_code == 0 for result in results
    )
    artifact_evidence_missing = artifact_owner_passed and not artifacts
    artifact_mismatch = bool(binding["clean"]) and any(
        artifact["source_revision"] != binding["source_revision"] for artifact in artifacts
    )
    source_changed = bool(binding["immutable_snapshot"]) and not bool(binding["source_unchanged"])
    if artifact_mismatch or artifact_evidence_missing or source_changed:
        status = "fail"
    report = {
        "schema_version": 1,
        "status": status,
        "candidate": binding,
        "artifacts": artifacts,
        "automated": [asdict(result) for result in results],
        "pending": list(pending),
    }
    if report_target is not None:
        try:
            report_target.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
        except (OSError, ValueError) as error:
            resources.close()
            print(f"Documentation examples: ERROR: {error}", file=sys.stderr)
            return 2
    if failed or artifact_mismatch or artifact_evidence_missing or source_changed:
        for result in failed:
            print(f"FAIL: {' '.join(result.argv)}", file=sys.stderr)
            if result.stdout:
                print(result.stdout, file=sys.stderr)
            if result.stderr:
                print(result.stderr, file=sys.stderr)
        if artifact_evidence_missing:
            print("FAIL: installed-artifact owner returned no exact artifact digests", file=sys.stderr)
        if artifact_mismatch:
            print("FAIL: installed-artifact revision does not match the clean candidate", file=sys.stderr)
        if source_changed:
            print("FAIL: documentation source changed during verification", file=sys.stderr)
        resources.close()
        return 1
    if args.require_complete and (pending or not bound_release_candidate):
        if pending:
            reason = f"{len(pending)} pending proof(s)"
        elif not binding["clean"] or not binding["immutable_snapshot"] or not binding["source_unchanged"]:
            reason = "dirty or unbound candidate"
        else:
            reason = "runtime tool identity awaits explicit approval evidence"
        print(f"Documentation examples: PENDING ({reason})", file=sys.stderr)
        resources.close()
        return 1
    label = "PASS" if status == "pass" else "PENDING"
    print(
        f"Documentation examples: {label} ({len(results)} automated command(s), {len(pending)} pending proof(s))",
        file=sys.stderr if report_to_stdout else sys.stdout,
    )
    resources.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
