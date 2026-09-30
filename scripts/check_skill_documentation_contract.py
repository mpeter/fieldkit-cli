#!/usr/bin/env python3
"""Run fixed semantic owners for packaged fieldkit skill documentation."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from fieldkit.util.text_snapshot import read_text_snapshot

if TYPE_CHECKING or __package__:
    from scripts.documentation_candidate_execution import candidate_binding
    from scripts.documentation_command_runner import _inherited_semantic_sandbox, _isolated_environment, _run_bounded
    from scripts.json_policy import load_json_bytes
else:
    from documentation_candidate_execution import candidate_binding
    from documentation_command_runner import _inherited_semantic_sandbox, _isolated_environment, _run_bounded
    from json_policy import load_json_bytes

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONTRACT_PATH = Path("docs/documentation-contract.json")
_OWNER = "skill_semantic_contract"
_OUTPUT_LIMIT = 4000
_TIMEOUT_SECONDS = 300
_MAX_WORKERS = 4
_MAX_CONTRACT_BYTES = 5 * 1024 * 1024
_MAX_COVERAGE_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class SemanticSuite:
    """One fixed semantic test command and the pages it exclusively owns."""

    identifier: str
    paths: tuple[str, ...]
    argv: tuple[str, ...]


@dataclass(frozen=True)
class SuiteResult:
    """Bounded result for one semantic suite."""

    identifier: str
    paths: tuple[str, ...]
    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    consumed_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class SemanticReport:
    """Deterministic result for every registered skill-document owner."""

    status: str
    results: tuple[SuiteResult, ...]
    candidate: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Render stable machine-readable evidence."""
        return {
            "schema_version": 1,
            "status": self.status,
            "verification_scope": "structural_semantic_not_behavioral",
            "candidate": self.candidate,
            "results": [asdict(result) for result in self.results],
        }


def _pytest_command(*test_paths: str, expression: str | None = None) -> tuple[str, ...]:
    """Build one fixed, serial pytest command without shell interpretation."""
    argv = ["uv", "run", "pytest", *test_paths]
    if expression is not None:
        argv.extend(("-k", expression))
    argv.extend(("-q", "-n", "0"))
    return tuple(argv)


_SUITES = (
    SemanticSuite(
        "brief",
        (
            "src/fieldkit/skills/brief/SKILL.md",
            "src/fieldkit/skills/brief/ops/update.md",
            "src/fieldkit/skills/brief/ops/week-end.md",
            "src/fieldkit/skills/brief/ops/week-start.md",
            "src/fieldkit/skills/brief/references/comprehensive-scan.md",
        ),
        _pytest_command("tests/test_brief_skill_documentation.py"),
    ),
    SemanticSuite(
        "always-start-post-meeting",
        (
            "src/fieldkit/skills/always-on-guidance/SKILL.md",
            "src/fieldkit/skills/post-meeting/SKILL.md",
            "src/fieldkit/skills/start/SKILL.md",
        ),
        _pytest_command("tests/test_start_post_meeting_documentation.py"),
    ),
    SemanticSuite(
        "contact",
        (
            "src/fieldkit/skills/contact/SKILL.md",
            "src/fieldkit/skills/contact/ops/competitive-intel-battlecard-template.md",
            "src/fieldkit/skills/contact/ops/competitive-intel.md",
            "src/fieldkit/skills/contact/ops/contact-enrich.md",
            "src/fieldkit/skills/contact/ops/contact-lookup.md",
        ),
        _pytest_command(
            "tests/test_contact_skill_documentation.py",
            "tests/test_contact_enrich_documentation.py",
        ),
    ),
    SemanticSuite(
        "contract",
        (
            "src/fieldkit/skills/contract/SKILL.md",
            "src/fieldkit/skills/contract/ops/contract-check.md",
            "src/fieldkit/skills/contract/ops/contract-extract.md",
            "src/fieldkit/skills/contract/ops/contract-redemption-form.md",
            "src/fieldkit/skills/contract/ops/deal-desk.md",
        ),
        _pytest_command("tests/test_contract_skill_documentation.py"),
    ),
    SemanticSuite(
        "create-cli",
        ("src/fieldkit/skills/create-cli/SKILL.md",),
        _pytest_command("tests/test_create_cli_skill_documentation.py"),
    ),
    SemanticSuite(
        "draft-review",
        ("src/fieldkit/skills/draft-review/SKILL.md",),
        _pytest_command("tests/test_draft_review_skill_documentation.py"),
    ),
    SemanticSuite(
        "followup-draft",
        (
            "src/fieldkit/skills/followup-draft/SKILL.md",
            "src/fieldkit/skills/followup-draft/email-template.md",
        ),
        _pytest_command("tests/test_followup_draft_documentation.py"),
    ),
    SemanticSuite(
        "humanizer",
        ("src/fieldkit/skills/humanizer/SKILL.md",),
        _pytest_command("tests/test_humanizer_skill_documentation.py"),
    ),
    SemanticSuite(
        "ingest",
        (
            "src/fieldkit/skills/ingest/SKILL.md",
            "src/fieldkit/skills/ingest/ops/gmail-refresh.md",
        ),
        _pytest_command("tests/test_ingest_skill_documentation.py"),
    ),
    SemanticSuite(
        "handoff-workflows",
        (
            "src/fieldkit/skills/handoffs/SKILL.md",
            "src/fieldkit/skills/pickup/SKILL.md",
        ),
        _pytest_command("tests/test_handoff_skill_documentation.py"),
    ),
    SemanticSuite(
        "meeting",
        (
            "src/fieldkit/skills/meeting/SKILL.md",
            "src/fieldkit/skills/meeting/brief-template.md",
            "src/fieldkit/skills/meeting/ops/account-snapshot.md",
            "src/fieldkit/skills/meeting/ops/qbr-prep-output-template.md",
            "src/fieldkit/skills/meeting/ops/qbr-prep.md",
        ),
        _pytest_command("tests/test_tool_routing_cli_first.py"),
    ),
    SemanticSuite(
        "memory-management",
        ("src/fieldkit/skills/memory-management/SKILL.md",),
        _pytest_command("tests/test_memory_management_skill.py"),
    ),
    SemanticSuite(
        "meeting-reports",
        (
            "src/fieldkit/skills/meeting/ops/account-pulse.md",
            "src/fieldkit/skills/meeting/ops/stakeholder-map.md",
            "src/fieldkit/skills/meeting/ops/one-on-one.md",
        ),
        _pytest_command("tests/test_meeting_workflow_scenarios.py"),
    ),
    SemanticSuite(
        "pipeline-sf-sync",
        (
            "src/fieldkit/skills/pipeline/SKILL.md",
            "src/fieldkit/skills/pipeline/ops/engagement-health.md",
            "src/fieldkit/skills/pipeline/ops/forecast.md",
            "src/fieldkit/skills/pipeline/ops/pipeline-health.md",
            "src/fieldkit/skills/sf-sync/SKILL.md",
        ),
        _pytest_command("tests/test_pipeline_sf_sync_documentation.py"),
    ),
    SemanticSuite(
        "pursuit-auditor",
        ("src/fieldkit/skills/pursuit-auditor/SKILL.md",),
        _pytest_command("tests/test_pursuit_auditor_skill_documentation.py"),
    ),
    SemanticSuite(
        "remaining-public-skills",
        (
            "src/fieldkit/skills/companion/SKILL.md",
            "src/fieldkit/skills/grill/SKILL.md",
            "src/fieldkit/skills/pursuit-advance/SKILL.md",
            "src/fieldkit/skills/pursuit-advance/gate-reference.md",
            "src/fieldkit/_data/pursuit-narrative-template.md",
        ),
        _pytest_command("tests/test_remaining_public_skill_contracts.py"),
    ),
    SemanticSuite(
        "task-skills",
        (
            "src/fieldkit/skills/task-management/SKILL.md",
            "src/fieldkit/skills/task-sync/SKILL.md",
        ),
        _pytest_command("tests/test_task_skills_contract.py"),
    ),
    SemanticSuite(
        "tool-routing",
        (
            "src/fieldkit/skills/tool-routing/SKILL.md",
            "src/fieldkit/skills/tool-routing/ops/workspace-tool-catalog.md",
            "src/fieldkit/skills/tool-routing/references/cli-routes.md",
            "src/fieldkit/skills/tool-routing/references/developer-search.md",
            "src/fieldkit/skills/tool-routing/references/docs-layout-workflow.md",
            "src/fieldkit/skills/tool-routing/references/failure-scenarios.md",
            "src/fieldkit/skills/tool-routing/references/sf-next-steps-protocol.md",
            "src/fieldkit/skills/tool-routing/references/slack-search-protocol.md",
            "src/fieldkit/skills/tool-routing/references/vault.md",
            "src/fieldkit/skills/tool-routing/references/web-search.md",
            "src/fieldkit/skills/tool-routing/workflows/first-time-setup.md",
        ),
        _pytest_command("tests/test_tool_routing_public_contract.py"),
    ),
    SemanticSuite(
        "win-loss",
        ("src/fieldkit/skills/win-loss/SKILL.md",),
        _pytest_command("tests/test_win_loss_skill_documentation.py"),
    ),
    SemanticSuite(
        "workstream-discover",
        ("src/fieldkit/skills/workstream-discover/SKILL.md",),
        _pytest_command("tests/test_workstream_discover_skill_documentation.py"),
    ),
    SemanticSuite(
        "skill-index",
        ("src/fieldkit/skills/README.md",),
        _pytest_command("tests/test_check_documentation_contract.py", expression="skill_index"),
    ),
    SemanticSuite(
        "slack-digest",
        ("src/fieldkit/skills/slack-digest/SKILL.md",),
        _pytest_command("tests/test_slack_digest_skill_documentation.py"),
    ),
)


def _expected_paths() -> tuple[str, ...]:
    """Return every page owned by exactly one committed semantic suite."""
    identifiers = [suite.identifier for suite in _SUITES]
    paths = [path for suite in _SUITES for path in suite.paths]
    commands = [suite.argv for suite in _SUITES]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("skill semantic suite identifiers must be unique")
    if len(paths) != len(set(paths)):
        raise ValueError("skill semantic document paths must have exactly one suite")
    if len(commands) != len(set(commands)):
        raise ValueError("skill semantic suite commands must be unique")
    return tuple(sorted(paths))


def _contract_paths(repo_root: Path) -> tuple[str, ...]:
    """Load the exact paths assigned to the semantic owner."""
    try:
        snapshot = read_text_snapshot(repo_root / _CONTRACT_PATH, max_bytes=_MAX_CONTRACT_BYTES)
        contract = load_json_bytes(snapshot.content.encode("utf-8"))
    except (OSError, ValueError):
        raise ValueError("invalid semantic documentation contract") from None
    verification = contract.get("verification") if isinstance(contract, dict) else None
    owner = verification.get(_OWNER) if isinstance(verification, dict) else None
    paths = owner.get("paths") if isinstance(owner, dict) else None
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        raise ValueError("invalid semantic documentation contract")
    if len(paths) != len(set(paths)):
        raise ValueError("invalid semantic documentation contract")
    return tuple(sorted(paths))


def _validate_evidence_paths(repo_root: Path) -> None:
    """Require every owned document and fixed test module to exist."""
    for path in _expected_paths():
        if not (repo_root / path).is_file():
            raise ValueError(f"semantic document does not exist: {path}")
    for suite in _SUITES:
        for argument in suite.argv:
            if argument.startswith("tests/") and not (repo_root / argument).is_file():
                raise ValueError(f"semantic test evidence does not exist: {argument}")


def _consumed_paths(path: Path, owned_paths: tuple[str, ...]) -> tuple[str, ...]:
    """Read bounded LF-delimited observation records, not authenticated proof."""
    try:
        snapshot = read_text_snapshot(path, max_bytes=_MAX_COVERAGE_BYTES)
    except FileNotFoundError:
        return ()
    except (OSError, ValueError):
        raise ValueError("invalid semantic document coverage") from None
    if any(
        character != "\n" and (ord(character) < 32 or character in "\x7f\x85\u2028\u2029")
        for character in snapshot.content
    ):
        raise ValueError("invalid semantic document coverage")
    return tuple(sorted(set(snapshot.content.split("\n")) & set(owned_paths)))


def _run_process(repo_root: Path, argv: tuple[str, ...], environment: dict[str, str]) -> tuple[int, str, str]:
    """Run through the shared bounded, isolated process implementation."""
    if argv not in {suite.argv for suite in _SUITES}:
        raise ValueError("unknown semantic suite command")
    inherited_owner: Literal["skill_semantic_contract"] | None = (
        "skill_semantic_contract" if _inherited_semantic_sandbox(repo_root, _OWNER) else None
    )
    result = _run_bounded(repo_root, argv, environment, timeout=_TIMEOUT_SECONDS, inherited_owner=inherited_owner)
    return result.exit_code, result.stdout, result.stderr


def _run(repo_root: Path, argv: tuple[str, ...]) -> SuiteResult:
    """Run one fixed suite in its own bounded process group."""
    suite = next((candidate for candidate in _SUITES if candidate.argv == argv), None)
    if suite is None:
        raise ValueError(f"unknown semantic suite command: {argv}")
    temporary_parent = Path(os.environ.get("TMPDIR", repo_root.parent))
    with tempfile.TemporaryDirectory(prefix="skill-documentation-contract-", dir=temporary_parent) as directory:
        temporary_root = Path(directory)
        coverage_path = temporary_root / "consumed-paths.txt"
        environment = _isolated_environment(temporary_root, repo_root=repo_root.resolve(), coverage_path=coverage_path)
        exit_code, stdout, stderr = _run_process(repo_root, argv, environment)
        consumed = _consumed_paths(coverage_path, suite.paths)
    missing = sorted(set(suite.paths) - set(consumed))
    if exit_code == 0 and missing:
        exit_code = 1
        stderr = f"{stderr}\nunconsumed semantic documents: {missing}".strip()
    return SuiteResult(
        suite.identifier,
        suite.paths,
        argv,
        exit_code,
        stdout[-_OUTPUT_LIMIT:],
        stderr[-_OUTPUT_LIMIT:],
        consumed,
    )


def check(repo_root: Path = _REPO_ROOT) -> SemanticReport:
    """Run all semantic suites after proving the contract path set is exact."""
    expected = _expected_paths()
    declared = _contract_paths(repo_root)
    if declared != expected:
        raise ValueError("skill semantic path ownership mismatch")
    _validate_evidence_paths(repo_root)
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
        futures = [executor.submit(_run, repo_root, suite.argv) for suite in _SUITES]
        results = tuple(future.result() for future in futures)
    return SemanticReport(
        status="pass" if all(result.exit_code == 0 for result in results) else "fail",
        results=results,
        candidate=candidate_binding(repo_root, [suite.argv for suite in _SUITES]),
    )


def main(argv: list[str] | None = None) -> int:
    """Run the semantic owners and optionally emit JSON evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=_REPO_ROOT)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    try:
        report = check(args.repo_root.resolve())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Skill documentation semantics: ERROR: {error}", file=sys.stderr)
        return 2
    if args.as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(f"Skill documentation semantics: {report.status.upper()} ({len(report.results)} suites)")
    if report.status != "pass":
        for result in report.results:
            if result.exit_code == 0:
                continue
            print(f"FAIL: {result.identifier}: {' '.join(result.argv)}", file=sys.stderr)
            if result.stdout:
                print(result.stdout, file=sys.stderr)
            if result.stderr:
                print(result.stderr, file=sys.stderr)
    return 0 if report.status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
