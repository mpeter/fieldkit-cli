#!/usr/bin/env python3
"""Run fixed automated owners for public examples and report pending rehearsals."""

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from jsonschema import Draft202012Validator

if __package__:
    from scripts.json_policy import reject_duplicate_json_keys
else:
    from json_policy import reject_duplicate_json_keys

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONTRACT_PATH = Path("docs/documentation-contract.json")
_REHEARSAL_SCHEMA_PATH = Path("docs/release-readiness/rehearsal-evidence.schema.json")
_OUTPUT_LIMIT = 4000
_TIMEOUT_SECONDS = 120
# This fixed owner builds one wheel and one sdist, then performs a complete
# fresh-environment smoke journey for each. Its bound must exceed the sum of
# the nested build, setup, install, and command bounds while retaining those
# more specific failure diagnostics.
_ARTIFACT_SCENARIO_TIMEOUT_SECONDS = 1_800
_GENERATED_REFERENCE = "automated.generated-reference"
_GENERATED_DEPENDENCY_MAP = "automated.generated-dependency-map"
_INSTALLED_BASE_ARTIFACT = "automated.installed-base-artifact"
_COMPATIBILITY_POLICY = "automated.compatibility-policy"
_CONFIGURATION_EXAMPLE_CONTRACT = "automated.configuration-example-contract"
_DOCUMENTATION_CONTRACT = "automated.documentation-contract"
_EXIT_CODE_CONTRACT = "automated.exit-code-contract"
_ROADMAP_CONTRACT = "automated.roadmap-contract"
_RELEASE_WORKFLOW_POLICY = "automated.release-workflow-policy"
_CONTRACT_COMMAND = ("uv", "run", "python", "scripts/check_documentation_contract.py")
_AUTOMATED_COMMANDS = {
    _COMPATIBILITY_POLICY: (("uv", "run", "python", "scripts/check_compatibility_policy.py"),),
    _CONFIGURATION_EXAMPLE_CONTRACT: (
        ("uv", "run", "pytest", "tests/test_documentation_configuration_examples.py", "-q", "-n", "4"),
    ),
    _DOCUMENTATION_CONTRACT: (_CONTRACT_COMMAND,),
    _EXIT_CODE_CONTRACT: (("uv", "run", "pytest", "tests/test_cli_exit.py", "-q", "-n", "4"),),
    _GENERATED_DEPENDENCY_MAP: (("uv", "run", "python", "scripts/generate_dep_map.py", "--check"),),
    _GENERATED_REFERENCE: (("uv", "run", "python", "scripts/generate_cli_docs.py", "--check"),),
    _INSTALLED_BASE_ARTIFACT: (("uv", "run", "python", "scripts/check_documentation_example_scenarios.py", "--json"),),
    _ROADMAP_CONTRACT: (("uv", "run", "python", "scripts/check_roadmap_contract.py"),),
    _RELEASE_WORKFLOW_POLICY: (("make", "release-workflow-policy-check"),),
}
_COMMAND_TIMEOUTS = {
    _AUTOMATED_COMMANDS[_INSTALLED_BASE_ARTIFACT][0]: _ARTIFACT_SCENARIO_TIMEOUT_SECONDS,
}
_PUBLIC_REPOSITORY_URLS = frozenset(
    {
        "https://github.com/mpeter/fieldkit-cli",
        "https://github.com/mpeter/fieldkit-cli.git",
        "git@github.com:mpeter/fieldkit-cli.git",  # pii-guard: ignore - Git SSH service account
        "ssh://git@github.com/mpeter/fieldkit-cli.git",  # pii-guard: ignore - Git SSH service account
    }
)
_FINAL_REHEARSAL_SCENARIOS = {
    "external-contributor-bootstrap": ("external_contributor", ("make", "bootstrap")),
    "external-contributor-changelog": (
        "external_contributor",
        ("uv", "run", "python", "scripts/check_changelog_fragment.py"),
    ),
    "external-contributor-docs-only": ("external_contributor", ("git", "diff", "--check")),
    "external-contributor-fork-ci": ("external_contributor", ("gh", "pr", "checks", "--watch")),
    "external-contributor-hooks": ("external_contributor", ("pre-commit", "run", "--all-files")),
    "external-contributor-python-tests": ("external_contributor", ("uv", "run", "pytest", "tests/", "-q", "-n", "4")),
    "external-user-offline-base": ("external_user", ("fieldkit", "doctor", "--json")),
    "external-user-recovery-isolation": ("external_user", ("python", "-m", "pip", "uninstall", "-y", "fieldkit-cli")),
    "external-user-sdist-install": ("external_user", ("python", "-m", "pip", "install", "fieldkit_cli-1.0.0.tar.gz")),
    "external-user-wheel-install": (
        "external_user",
        ("python", "-m", "pip", "install", "fieldkit_cli-1.0.0-py3-none-any.whl"),
    ),
}
_FINAL_REHEARSAL_ASSERTIONS = {
    "external-contributor-bootstrap": (("stdout", "pre-commit"),),
    "external-contributor-changelog": (("stdout", "PASS"),),
    "external-contributor-docs-only": (),
    "external-contributor-fork-ci": (("stdout", "pass"),),
    "external-contributor-hooks": (("stdout", "Passed"),),
    "external-contributor-python-tests": (("stdout", "passed"),),
    "external-user-offline-base": (("stdout", '"status"'),),
    "external-user-recovery-isolation": (("stdout", "Successfully uninstalled"),),
    "external-user-sdist-install": (("stdout", "Successfully installed"),),
    "external-user-wheel-install": (("stdout", "Successfully installed"),),
}
_MANUAL_PROOF_TYPES = {
    "manual.credentialed-integration": "credentialed-integration",
    "manual.release-cutover": "release-cutover",
}
_MANUAL_PROOF_ACTORS = {
    "credentialed-integration": "maintainer",
    "release-cutover": "operator",
}


@dataclass(frozen=True)
class CommandResult:
    """Bounded result for one fixed verification command."""

    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str


def _run(repo_root: Path, argv: tuple[str, ...]) -> CommandResult:
    """Run one committed scenario without evaluating documentation text."""
    temporary_parent = Path(os.environ.get("TMPDIR", repo_root.parent))
    with tempfile.TemporaryDirectory(prefix="documentation-examples-", dir=temporary_parent) as temporary_directory:
        temporary_root = Path(temporary_directory)
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(temporary_root / "home"),
                "TMPDIR": str(temporary_root),
                "UV_NO_CACHE": "1",
                "PYTEST_XDIST_WORKERS": "4",
                "XDG_CACHE_HOME": str(temporary_root / "xdg-cache"),
                "XDG_CONFIG_HOME": str(temporary_root / "xdg-config"),
                "XDG_DATA_HOME": str(temporary_root / "xdg-data"),
                "XDG_STATE_HOME": str(temporary_root / "xdg-state"),
            }
        )
        return _run_bounded(repo_root, argv, environment)


def _run_bounded(repo_root: Path, argv: tuple[str, ...], environment: dict[str, str] | None = None) -> CommandResult:
    """Run one command in its own process group with a bounded result."""
    process = subprocess.Popen(
        argv,
        cwd=repo_root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=_COMMAND_TIMEOUTS.get(argv, _TIMEOUT_SECONDS))
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        return CommandResult(
            argv=argv,
            exit_code=124,
            stdout=stdout[-_OUTPUT_LIMIT:],
            stderr=stderr[-_OUTPUT_LIMIT:],
        )
    return CommandResult(
        argv=argv,
        exit_code=process.returncode,
        stdout=stdout[-_OUTPUT_LIMIT:],
        stderr=stderr[-_OUTPUT_LIMIT:],
    )


def _manual_block_proofs(documents: object) -> dict[str, tuple[str, str]]:
    """Return the proof route and reviewed block digest for every manual example."""
    if not isinstance(documents, dict):
        raise ValueError("documentation contract verification registry is invalid")
    proofs: dict[str, tuple[str, str]] = {}
    for entry in documents.values():
        if not isinstance(entry, dict):
            continue
        for field in ("fenced_blocks", "tables"):
            records = entry.get(field, [])
            if not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, dict):
                    continue
                verification_id = record.get("verification_id")
                identifier = record.get("id")
                digest = record.get("sha256")
                if not isinstance(verification_id, str):
                    continue
                proof_type = _MANUAL_PROOF_TYPES.get(verification_id)
                if proof_type is None:
                    continue
                if not isinstance(identifier, str) or not isinstance(digest, str) or len(digest) != 64:
                    raise ValueError("manual documentation block needs a stable identifier and reviewed digest")
                if identifier in proofs:
                    raise ValueError("manual documentation block identifier must be unique")
                proofs[identifier] = (proof_type, digest)
    return proofs


def _public_command(repository: Path, argv: tuple[str, ...]) -> str:
    """Run one bounded public-proof command and return its text output."""
    try:
        completed = _run_bounded(repository, argv)
    except OSError as exc:
        raise ValueError(f"public proof command could not run: {' '.join(argv)}") from exc
    if completed.exit_code != 0:
        raise ValueError(f"public proof command failed: {' '.join(argv)}")
    return completed.stdout.strip()


def _validate_public_rehearsal(subject: dict[str, object], public_repository: Path) -> None:
    """Prove a rehearsal's public commit and CI run match its retained export."""
    if not public_repository.is_dir() or not (public_repository / ".git").exists():
        raise ValueError("public repository must be a fresh Git checkout")
    origin = _public_command(public_repository, ("git", "remote", "get-url", "origin"))
    if origin not in _PUBLIC_REPOSITORY_URLS:
        raise ValueError("public repository origin does not identify mpeter/fieldkit-cli")
    public_commit = subject["public_commit_sha"]
    clean_export_tree = subject["clean_export_tree"]
    if not isinstance(public_commit, str) or not isinstance(clean_export_tree, str):
        raise ValueError("rehearsal evidence public identity is invalid")
    status = _public_command(public_repository, ("git", "status", "--porcelain=v1", "--untracked-files=all"))
    if status:
        raise ValueError("public rehearsal requires a clean public checkout")
    head = _public_command(public_repository, ("git", "rev-parse", "HEAD"))
    if head != public_commit:
        raise ValueError("public checkout HEAD does not match the retained public commit")
    commit = _public_command(public_repository, ("git", "rev-parse", "--verify", f"{public_commit}^{{commit}}"))
    tree = _public_command(public_repository, ("git", "rev-parse", f"{public_commit}^{{tree}}"))
    if commit != public_commit or tree != clean_export_tree:
        raise ValueError("public commit does not match the retained clean export tree")
    raw_repository = _public_command(public_repository, ("gh", "api", "repos/mpeter/fieldkit-cli"))
    try:
        repository = json.loads(raw_repository, object_pairs_hook=reject_duplicate_json_keys)
    except json.JSONDecodeError as exc:
        raise ValueError("public repository proof returned invalid JSON") from exc
    if not isinstance(repository, dict) or (
        repository.get("full_name") != "mpeter/fieldkit-cli"
        or repository.get("private") is not False
        or repository.get("visibility") != "public"
    ):
        raise ValueError("public repository API does not confirm public visibility")
    run_id = subject["workflow_run_id"]
    run_attempt = subject["workflow_run_attempt"]
    workflow_name = subject["workflow_name"]
    workflow_path = subject["workflow_path"]
    workflow_event = subject["workflow_event"]
    if (
        not isinstance(run_id, int)
        or not isinstance(run_attempt, int)
        or workflow_name != "Cutover verification"
        or workflow_path != ".github/workflows/cutover.yml"
        or workflow_event != "push"
    ):
        raise ValueError("rehearsal evidence workflow identity is invalid")
    raw_run = _public_command(
        public_repository,
        (
            "gh",
            "api",
            f"repos/mpeter/fieldkit-cli/actions/runs/{run_id}",
        ),
    )
    try:
        run = json.loads(raw_run, object_pairs_hook=reject_duplicate_json_keys)
    except json.JSONDecodeError as exc:
        raise ValueError("public CI proof returned invalid JSON") from exc
    if not isinstance(run, dict) or (
        run.get("id") != run_id
        or run.get("run_attempt") != run_attempt
        or run.get("head_sha") != public_commit
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or run.get("name") != workflow_name
        or run.get("event") != workflow_event
        or not isinstance(run.get("path"), str)
        or run["path"].partition("@")[0] != workflow_path
    ):
        raise ValueError("public cutover workflow run does not prove the retained public commit")


def _automated_verification_identifiers(documents: object) -> tuple[str, ...]:
    """Return declared automated owners in stable order."""
    if not isinstance(documents, dict):
        raise ValueError("documentation contract verification registry is invalid")
    identifiers: set[str] = set()
    for entry in documents.values():
        if not isinstance(entry, dict):
            continue
        for field in ("fenced_blocks", "tables"):
            records = entry.get(field, [])
            if not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, dict):
                    continue
                verification_id = record.get("verification_id")
                if isinstance(verification_id, str) and verification_id in _AUTOMATED_COMMANDS:
                    identifiers.add(verification_id)
    return tuple(sorted(identifiers))


def _validated_rehearsal_blocks(
    repo_root: Path,
    evidence_path: Path,
    candidate_report_path: Path,
    required_blocks: dict[str, tuple[str, str]],
    public_repository: Path | None,
) -> tuple[str, ...]:
    """Accept final proof only when it binds this exact candidate and documentation contract."""
    if not evidence_path.is_file():
        raise ValueError("rehearsal evidence must be a regular file")
    if not candidate_report_path.is_file():
        raise ValueError("candidate report must be a regular file")
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_json_keys)
    candidate_report = json.loads(
        candidate_report_path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_json_keys
    )
    schema = json.loads(
        (repo_root / _REHEARSAL_SCHEMA_PATH).read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_json_keys
    )
    if not isinstance(evidence, dict) or not isinstance(schema, dict):
        raise ValueError("rehearsal evidence and its schema must be JSON objects")
    errors = sorted(Draft202012Validator(schema).iter_errors(evidence), key=lambda error: list(error.absolute_path))
    if errors:
        location = ".".join(str(part) for part in errors[0].absolute_path) or "<root>"
        raise ValueError(f"rehearsal evidence schema violation at {location}: {errors[0].message}")
    subject = evidence.get("subject")
    if not isinstance(subject, dict):
        raise ValueError("rehearsal evidence subject must be an object")
    if not isinstance(candidate_report, dict) or candidate_report.get("status") != "pass":
        raise ValueError("candidate report must be a passing object")
    manifest = candidate_report.get("export_manifest")
    validation = candidate_report.get("artifact_validation")
    if not isinstance(manifest, dict) or not isinstance(validation, dict):
        raise ValueError("candidate report must contain export and artifact validation evidence")
    expected_subject = {
        "source_sha": manifest.get("source_commit"),
        "clean_export_tree": manifest.get("exported_tree"),
        "repository": manifest.get("expected_repository"),
    }
    if any(not isinstance(value, str) for value in expected_subject.values()):
        raise ValueError("candidate report has incomplete export identity")
    if any(subject.get(field) != value for field, value in expected_subject.items()):
        raise ValueError("rehearsal evidence does not bind the retained public candidate")
    if validation.get("source_revision") != expected_subject["source_sha"]:
        raise ValueError("candidate artifact validation does not bind its exported source")
    candidate_artifacts = validation.get("artifacts")
    if not isinstance(candidate_artifacts, list):
        raise ValueError("candidate report artifact validation must list artifacts")
    expected_artifacts = {
        (artifact.get("name"), artifact.get("sha256"))
        for artifact in candidate_artifacts
        if isinstance(artifact, dict) and artifact.get("status") == "pass"
    }
    provided_artifacts = evidence.get("artifacts")
    if not isinstance(provided_artifacts, list):
        raise ValueError("rehearsal evidence artifacts must be a list")
    if {
        (artifact.get("name"), artifact.get("sha256")) for artifact in provided_artifacts if isinstance(artifact, dict)
    } != expected_artifacts:
        raise ValueError("rehearsal evidence artifacts do not match the retained public candidate")
    expected_contract_sha256 = hashlib.sha256((repo_root / _CONTRACT_PATH).read_bytes()).hexdigest()
    if subject.get("documentation_contract_sha256") != expected_contract_sha256:
        raise ValueError("rehearsal evidence does not bind the current documentation contract")
    review = evidence.get("review")
    expected_review_url = f"https://github.com/mpeter/fieldkit-cli/actions/runs/{subject['workflow_run_id']}"
    if not isinstance(review, dict) or review.get("immutable_url") != expected_review_url:
        raise ValueError("rehearsal evidence review receipt does not identify the cutover workflow run")
    verified_blocks = evidence.get("verified_blocks")
    if not isinstance(verified_blocks, list) or not all(isinstance(block, str) for block in verified_blocks):
        raise ValueError("rehearsal evidence verified_blocks must be a string list")
    if tuple(sorted(verified_blocks)) != tuple(sorted(required_blocks)):
        raise ValueError("rehearsal evidence does not cover exactly the required manual documentation blocks")
    scenarios = evidence.get("scenarios")
    if not isinstance(scenarios, list) or any(not isinstance(scenario, dict) for scenario in scenarios):
        raise ValueError("rehearsal evidence scenarios must be objects")
    scenarios_by_id = {scenario.get("id"): scenario for scenario in scenarios if isinstance(scenario.get("id"), str)}
    manual_scenario_ids = {
        f"{proof_type}:{block_identifier}" for block_identifier, (proof_type, _) in required_blocks.items()
    }
    expected_scenario_ids = set(_FINAL_REHEARSAL_SCENARIOS) | manual_scenario_ids
    if set(scenarios_by_id) != expected_scenario_ids or len(scenarios_by_id) != len(scenarios):
        raise ValueError("rehearsal evidence must contain exactly the required scenarios")
    for identifier, (actor, expected_argv) in _FINAL_REHEARSAL_SCENARIOS.items():
        scenario = scenarios_by_id[identifier]
        if scenario.get("actor") != actor or scenario.get("status") != "pass":
            raise ValueError(f"rehearsal scenario {identifier} has the wrong actor or non-passing status")
        commands = scenario.get("commands")
        if not isinstance(commands, list) or any(
            not isinstance(command, dict) or command.get("exit_code") != 0 for command in commands
        ):
            raise ValueError(f"rehearsal scenario {identifier} lacks successful command evidence")
        if not any(command.get("argv") == list(expected_argv) for command in commands if isinstance(command, dict)):
            raise ValueError(f"rehearsal scenario {identifier} lacks its canonical command evidence")
        canonical_commands = [
            command for command in commands if isinstance(command, dict) and command.get("argv") == list(expected_argv)
        ]
        if len(canonical_commands) != 1:
            raise ValueError(f"rehearsal scenario {identifier} has ambiguous canonical command evidence")
        canonical_command = canonical_commands[0]
        stdout = canonical_command.get("stdout")
        stderr = canonical_command.get("stderr")
        if not isinstance(stdout, str) or not isinstance(stderr, str):
            raise ValueError(f"rehearsal scenario {identifier} has no retained command transcript")
        if (
            canonical_command.get("stdout_sha256") != hashlib.sha256(stdout.encode()).hexdigest()
            or canonical_command.get("stderr_sha256") != hashlib.sha256(stderr.encode()).hexdigest()
        ):
            raise ValueError(f"rehearsal scenario {identifier} transcript digest does not match")
        expected_assertions = [
            {"stream": stream, "contains": text} for stream, text in _FINAL_REHEARSAL_ASSERTIONS[identifier]
        ]
        if canonical_command.get("assertions") != expected_assertions:
            raise ValueError(f"rehearsal scenario {identifier} does not assert its required observation")
        if any(
            assertion["contains"] not in (stdout if assertion["stream"] == "stdout" else stderr)
            for assertion in expected_assertions
        ):
            raise ValueError(f"rehearsal scenario {identifier} observation does not match its transcript")
        blocks = scenario.get("verified_blocks")
        if (
            blocks != []
            or scenario.get("proof_type") != "release-cutover"
            or scenario.get("documented_block_sha256") is not None
        ):
            raise ValueError(f"rehearsal scenario {identifier} has invalid block ownership")
    for block_identifier, (proof_type, block_digest) in required_blocks.items():
        scenario = scenarios_by_id[f"{proof_type}:{block_identifier}"]
        if (
            scenario.get("actor") != _MANUAL_PROOF_ACTORS[proof_type]
            or scenario.get("proof_type") != proof_type
            or scenario.get("documented_block_sha256") != block_digest
            or scenario.get("status") != "pass"
            or scenario.get("verified_blocks") != [block_identifier]
        ):
            raise ValueError(f"rehearsal block {block_identifier} lacks its typed proof route")
        commands = scenario.get("commands")
        if not isinstance(commands, list) or not commands:
            raise ValueError(f"rehearsal block {block_identifier} has no command evidence")
        for command in commands:
            if not isinstance(command, dict) or command.get("exit_code") != 0:
                raise ValueError(f"rehearsal block {block_identifier} has non-passing command evidence")
            stdout = command.get("stdout")
            stderr = command.get("stderr")
            assertions = command.get("assertions")
            if (
                not isinstance(stdout, str)
                or not isinstance(stderr, str)
                or not isinstance(assertions, list)
                or not assertions
                or command.get("stdout_sha256") != hashlib.sha256(stdout.encode()).hexdigest()
                or command.get("stderr_sha256") != hashlib.sha256(stderr.encode()).hexdigest()
            ):
                raise ValueError(f"rehearsal block {block_identifier} has invalid transcript evidence")
            if any(
                not isinstance(assertion, dict)
                or not isinstance(assertion.get("stream"), str)
                or not isinstance(assertion.get("contains"), str)
                or assertion["contains"] not in (stdout if assertion["stream"] == "stdout" else stderr)
                for assertion in assertions
            ):
                raise ValueError(f"rehearsal block {block_identifier} assertion does not match its transcript")
    if public_repository is None:
        raise ValueError("rehearsal evidence requires a fresh public repository checkout")
    _validate_public_rehearsal(subject, public_repository)
    return tuple(sorted(verified_blocks))


def check(
    repo_root: Path = _REPO_ROOT,
    *,
    rehearsal_evidence: Path | None = None,
    candidate_report: Path | None = None,
    public_repository: Path | None = None,
) -> tuple[list[CommandResult], tuple[str, ...]]:
    """Run every automated owner and return explicit pending block identifiers."""
    contract_result = _run(repo_root, _CONTRACT_COMMAND)
    if contract_result.exit_code != 0:
        raise ValueError("documentation contract validation failed")
    contract = json.loads((repo_root / _CONTRACT_PATH).read_text(encoding="utf-8"))
    if not isinstance(contract, dict):
        raise ValueError("documentation contract must be an object")
    verifications = contract.get("example_verifications")
    documents = contract.get("documents")
    if not isinstance(verifications, dict) or not isinstance(documents, dict):
        raise ValueError("documentation contract verification registry is invalid")
    results: list[CommandResult] = []
    for verification_id in _automated_verification_identifiers(documents):
        if verification_id not in verifications:
            raise ValueError(f"missing automated verification: {verification_id}")
        commands = _AUTOMATED_COMMANDS[verification_id]
        results.extend(_run(repo_root, argv) for argv in commands)
    manual_blocks = _manual_block_proofs(documents)
    pending = tuple(sorted(manual_blocks))
    if rehearsal_evidence is not None:
        if candidate_report is None:
            raise ValueError("rehearsal evidence requires a retained candidate report")
        _validated_rehearsal_blocks(repo_root, rehearsal_evidence, candidate_report, manual_blocks, public_repository)
        pending = ()
    return results, pending


def main(argv: list[str] | None = None) -> int:
    """Run automated checks, optionally requiring all rehearsal evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=_REPO_ROOT)
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--rehearsal-evidence", type=Path)
    parser.add_argument("--candidate-report", type=Path)
    parser.add_argument("--public-repository", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        results, pending = check(
            args.repo_root,
            rehearsal_evidence=args.rehearsal_evidence,
            candidate_report=args.candidate_report,
            public_repository=args.public_repository,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Documentation examples: ERROR: {error}", file=sys.stderr)
        return 2
    failed = [result for result in results if result.exit_code != 0]
    status = "fail" if failed else "pending" if pending else "pass"
    report = {
        "schema_version": 1,
        "status": status,
        "automated": [asdict(result) for result in results],
        "pending": list(pending),
    }
    if args.report is not None:
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if failed:
        for result in failed:
            print(f"FAIL: {' '.join(result.argv)}", file=sys.stderr)
            if result.stdout:
                print(result.stdout, file=sys.stderr)
            if result.stderr:
                print(result.stderr, file=sys.stderr)
        return 1
    if args.require_complete and pending:
        print(f"Documentation examples: PENDING ({len(pending)} blocks)", file=sys.stderr)
        return 1
    label = "PASS" if status == "pass" else "PENDING"
    print(f"Documentation examples: {label} ({len(results)} automated command(s), {len(pending)} pending block(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
