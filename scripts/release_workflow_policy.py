"""Validate the dedicated release workflow's authority boundaries as data."""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING or __package__:
    from scripts.public_tree_scan import GITLEAKS_LINUX_X64_SHA256, GITLEAKS_VERSION
else:
    from public_tree_scan import GITLEAKS_LINUX_X64_SHA256, GITLEAKS_VERSION

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = Path(".github/workflows/release.yml")
EVIDENCE_PATH = Path("scripts/release_promotion_evidence.py")

_AUTHORITY_JOBS = frozenset({"attest", "publish_testpypi", "publish_pypi", "github_release"})
_COMPATIBILITY_JOB = "compatibility"
# Consumer job -> (index endpoint prefix, the only download host it may trust).
_CONSUMER_INDEXES = {
    "consumer_testpypi": ("https://test.pypi.org/pypi/fieldkit-cli/", "test-files.pythonhosted.org"),
    "consumer_pypi": ("https://pypi.org/pypi/fieldkit-cli/", "files.pythonhosted.org"),
}
_PROMOTION_EVIDENCE_JOB = "promotion_evidence"
_REQUIRED_JOBS = frozenset(
    {
        "context",
        _COMPATIBILITY_JOB,
        "build",
        "validate",
        *_CONSUMER_INDEXES,
        _PROMOTION_EVIDENCE_JOB,
        *_AUTHORITY_JOBS,
    }
)
_READ_ONLY_JOBS = frozenset({"context", "build", "validate", *_CONSUMER_INDEXES, _PROMOTION_EVIDENCE_JOB})
_EXPECTED_NEEDS = {
    "context": frozenset(),
    _COMPATIBILITY_JOB: frozenset({"context"}),
    "build": frozenset({"context", _COMPATIBILITY_JOB}),
    "validate": frozenset({"build"}),
    "attest": frozenset({"build", "validate"}),
    "publish_testpypi": frozenset({"build", "validate", "attest"}),
    "consumer_testpypi": frozenset({"context", "build", "validate", "publish_testpypi"}),
    "publish_pypi": frozenset({"build", "validate", "attest", "consumer_testpypi"}),
    "consumer_pypi": frozenset({"context", "build", "validate", "publish_pypi"}),
    "github_release": frozenset({"build", "validate", "attest", "publish_pypi", "consumer_pypi"}),
    _PROMOTION_EVIDENCE_JOB: frozenset(
        {
            "build",
            "validate",
            "attest",
            "publish_testpypi",
            "consumer_testpypi",
            "publish_pypi",
            "consumer_pypi",
            "github_release",
        }
    ),
}
_EXPECTED_PERMISSIONS = {
    **{name: {"contents": "read"} for name in _READ_ONLY_JOBS},
    "attest": {"attestations": "write", "id-token": "write"},
    "publish_testpypi": {"id-token": "write"},
    "publish_pypi": {"id-token": "write"},
    "github_release": {"contents": "write"},
}
_EXPECTED_ENVIRONMENTS = {"publish_testpypi": "testpypi", "publish_pypi": "pypi"}
# None means the job must not declare a condition, so GitHub's implicit
# success() requires every upstream job to have passed.
_PUSH_ONLY = "github.event_name == 'push'"
_EXPECTED_CONDITIONS: dict[str, str | None] = {
    "context": None,
    _COMPATIBILITY_JOB: None,
    "build": None,
    "validate": None,
    "attest": _PUSH_ONLY,
    "publish_testpypi": _PUSH_ONLY,
    "consumer_testpypi": _PUSH_ONLY,
    "publish_pypi": _PUSH_ONLY,
    "consumer_pypi": _PUSH_ONLY,
    "github_release": _PUSH_ONLY,
    _PROMOTION_EVIDENCE_JOB: "always() && github.event_name == 'push'",
}
# Fixed across run attempts: a partial re-run resumes against the same bundle,
# and a full re-run fails to upload instead of replacing it.
_CANDIDATE_ARTIFACT = "release-candidate-${{ github.run_id }}-${{ github.sha }}"
_EXPECTED_ACTIONS = {
    "attest": frozenset({"actions/download-artifact", "actions/attest"}),
    "publish_testpypi": frozenset({"actions/download-artifact", "pypa/gh-action-pypi-publish"}),
    "publish_pypi": frozenset({"actions/download-artifact", "pypa/gh-action-pypi-publish"}),
    "github_release": frozenset({"actions/download-artifact"}),
}
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
# Authority job -> the promotion-evidence boundary whose recorded action it runs.
_RECORDED_ACTION_JOBS = {"attest": "attest", "publish_testpypi": "publish_testpypi", "publish_pypi": "publish_pypi"}
_PINNED_ACTION_REFERENCE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}")
_FORBIDDEN_AUTHORITY_COMMAND = re.compile(r"(?:\b(?:uv|pip|python)\b|scripts/)")
_SECRET_REFERENCE = re.compile(r"\$\{\{\s*secrets(?:\.|\[)")
_CONSUMER_ACTIONS = frozenset({"actions/download-artifact", "actions/setup-python", "actions/upload-artifact"})
_PROMOTION_EVIDENCE_ACTIONS = _CONSUMER_ACTIONS
_CONTEXT_EVIDENCE_PATHS = frozenset(
    {
        "scripts/release_promotion_evidence.py",
        "scripts/_release_identity.py",
        "scripts/release_bundle.py",
        "scripts/release_consumer.py",
        "scripts/release_wheelhouse.py",
        "pyproject.toml",
    }
)
_SEALED_DISTRIBUTION_COMMANDS = (
    "EXPECTED_BUNDLE_MANIFEST_SHA256",
    "sha256sum candidate/bundle/SHA256SUMS",
    "cd candidate/bundle && sha256sum --strict --check SHA256SUMS",
    "mkdir release-dist",
    "cp candidate/bundle/*.whl candidate/bundle/*.tar.gz release-dist/",
)
_GITLEAKS_INSTALL_COMMANDS = (
    "curl --fail --location --proto '=https' --tlsv1.2 --retry 3 --connect-timeout 10 --max-time 60",
    "sha256sum --check",
    'tar --extract --file "$archive" --directory "$scanner_root" gitleaks',
    'install -m 0755 "$scanner_root/gitleaks" "$scanner_binary"',
    '"$scanner_binary" version',
)


@dataclass(frozen=True, order=True)
class Finding:
    """One deterministic release-workflow policy failure."""

    code: str
    job: str
    message: str


@dataclass(frozen=True)
class Report:
    """Versioned result for the release-workflow policy."""

    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        """Return whether the workflow satisfies every checked boundary."""
        return not self.findings

    def to_dict(self) -> dict[str, object]:
        """Render stable machine-readable policy evidence."""
        return {
            "schema_version": 1,
            "status": "pass" if self.ok else "fail",
            "findings": [asdict(finding) for finding in self.findings],
        }


def _mapping(value: object) -> dict[str, Any]:
    """Return a string-keyed mapping or an empty invalid sentinel."""
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _strings(value: object) -> frozenset[str]:
    """Normalize one GitHub dependency declaration."""
    if isinstance(value, str):
        return frozenset({value})
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return frozenset(value)
    return frozenset()


def _is_true(value: object) -> bool:
    """Return whether a YAML scalar represents true."""
    return value is True or value == "true"


def _is_false(value: object) -> bool:
    """Return whether a YAML scalar represents false."""
    return value is False or value == "false"


def _integer(value: object) -> int | None:
    """Return an integer scalar without accepting booleans."""
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _steps(job: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Return only structurally valid step mappings."""
    raw_steps = job.get("steps")
    if not isinstance(raw_steps, list):
        return ()
    return tuple(_mapping(step) for step in raw_steps if isinstance(step, dict))


def _contains_secret(value: object) -> bool:
    """Return whether a nested workflow value references repository secrets."""
    if isinstance(value, str):
        return _SECRET_REFERENCE.search(value) is not None
    if isinstance(value, dict):
        return any(_contains_secret(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_secret(item) for item in value)
    return False


def _add(findings: list[Finding], code: str, job: str, message: str) -> None:
    findings.append(Finding(code, job, message))


def _validate_top_level(document: dict[str, Any], findings: list[Finding]) -> None:
    triggers = _mapping(document.get("on"))
    dispatch = triggers.get("workflow_dispatch")
    push = _mapping(triggers.get("push"))
    if (
        set(triggers) != {"workflow_dispatch", "push"}
        or dispatch not in (None, "", "null", "~", {})
        or push.get("tags") != ["v*.*.*"]
    ):
        _add(findings, "RWF001", "workflow", "release triggers must be an input-free dry-run dispatch and SemVer tags")
    if document.get("permissions") != {}:
        _add(findings, "RWF002", "workflow", "top-level permissions must be empty")
    concurrency = _mapping(document.get("concurrency"))
    if concurrency.get("group") != "release-${{ github.ref }}" or not _is_false(concurrency.get("cancel-in-progress")):
        _add(findings, "RWF003", "workflow", "release concurrency must retain every deliberate run")


def _validate_job_shape(name: str, job: dict[str, Any], findings: list[Finding]) -> None:
    if name == _COMPATIBILITY_JOB:
        if (
            job.get("uses") != "./.github/workflows/compatibility.yml"
            or job.get("permissions") != {"contents": "read"}
            or job.get("environment") is not None
            or job.get("if") != _EXPECTED_CONDITIONS[name]
            or _strings(job.get("needs")) != _EXPECTED_NEEDS[name]
        ):
            _add(
                findings, "RWF027", name, "production compatibility must run the pinned reusable workflow before build"
            )
        return
    if job.get("permissions") != _EXPECTED_PERMISSIONS[name]:
        _add(findings, "RWF006", name, "job permissions exceed or omit its single responsibility")
    timeout = _integer(job.get("timeout-minutes"))
    if timeout is None or not 1 <= timeout <= 60:
        _add(findings, "RWF007", name, "job timeout must be an integer from 1 through 60")
    if _strings(job.get("needs")) != _EXPECTED_NEEDS[name]:
        _add(findings, "RWF008", name, "job dependencies do not preserve the candidate bundle chain")
    expected_environment = _EXPECTED_ENVIRONMENTS.get(name)
    if job.get("environment") != expected_environment:
        _add(findings, "RWF009", name, "job environment does not match its publication boundary")
    if job.get("if") != _EXPECTED_CONDITIONS[name]:
        _add(findings, "RWF012", name, "job condition does not match its publication boundary")


def _validates_signed_production_tag(job: dict[str, Any]) -> bool:
    """Return whether the source-capable context job checks GitHub's signed-tag record."""
    commands = tuple(command for step in _steps(job) if isinstance(command := step.get("run"), str))
    command_text = "\n".join(commands)
    required = ("timeout 60s gh api", "verification.verified == true", "GITHUB_REF_NAME", "GITHUB_SHA")
    return all(fragment in command_text for fragment in required) and _all_gh_requests_are_bounded(command_text)


def _requires_tag_on_main(job: dict[str, Any]) -> bool:
    """Return whether a production tag must name a commit already on the protected branch."""
    commands = tuple(command for step in _steps(job) if isinstance(command := step.get("run"), str))
    return any('git merge-base --is-ancestor "$GITHUB_SHA" refs/remotes/origin/main' in command for command in commands)


def _all_gh_requests_are_bounded(command_text: str) -> bool:
    """Require a fixed per-request deadline for every GitHub CLI API call."""
    return all("timeout 60s gh api" in line for line in command_text.splitlines() if "gh api" in line)


def _requires_main_for_dispatch(job: dict[str, Any]) -> bool:
    """Return whether manual runs are restricted to the protected main ref."""
    commands = tuple(command for step in _steps(job) if isinstance(command := step.get("run"), str))
    command_text = "\n".join(commands)
    required = ("GITHUB_EVENT_NAME", "workflow_dispatch", "GITHUB_REF", "refs/heads/main")
    return all(fragment in command_text for fragment in required)


def _validate_authority_job(name: str, job: dict[str, Any], findings: list[Finding]) -> None:
    steps = _steps(job)
    references = tuple(uses for step in steps if isinstance(uses := step.get("uses"), str))
    actions = frozenset(reference.partition("@")[0] for reference in references)
    revisions = tuple(reference.partition("@")[2] for reference in references)
    if actions != _EXPECTED_ACTIONS[name] or any(_FULL_SHA.fullmatch(revision) is None for revision in revisions):
        _add(findings, "RWF010", name, "authority job must use only pinned approved actions")
    commands = tuple(command for step in steps if isinstance(command := step.get("run"), str))
    if any(action in {"actions/checkout", "actions/cache"} or action.startswith("./") for action in actions) or any(
        _FORBIDDEN_AUTHORITY_COMMAND.search(command) for command in commands
    ):
        _add(findings, "RWF004", name, "authority job must not execute project source or dependency tooling")
    if _contains_secret(job) or any("skip-existing" in command for command in commands):
        _add(findings, "RWF005", name, "authority job must not use stored credentials or skip duplicate versions")
    command_text = "\n".join(commands)
    if not all(fragment in command_text for fragment in _SEALED_DISTRIBUTION_COMMANDS):
        _add(
            findings,
            "RWF017",
            name,
            "authority job must verify the fixed bundle manifest and use only sealed distributions",
        )
    if name == "attest" and not any(_mapping(step.get("with")).get("subject-path") == "release-dist" for step in steps):
        _add(findings, "RWF018", name, "attestation must cover only the sealed distribution directory")
    if name in {"publish_testpypi", "publish_pypi"} and not any(
        _mapping(step.get("with")).get("packages-dir") == "release-dist" for step in steps
    ):
        _add(findings, "RWF019", name, "package publication must receive only the sealed distribution directory")
    publisher_steps = tuple(
        step
        for step in steps
        if isinstance(uses := step.get("uses"), str) and uses.partition("@")[0] == "pypa/gh-action-pypi-publish"
    )
    if name in {"publish_testpypi", "publish_pypi"} and (
        not publisher_steps
        or not all(_is_true(_mapping(step.get("with")).get("attestations")) for step in publisher_steps)
    ):
        _add(findings, "RWF029", name, "package publication must explicitly attach PEP 740 attestations")
    if name == "github_release" and not any(
        "gh release create" in command and "release-dist/*" in command for command in commands
    ):
        _add(findings, "RWF020", name, "GitHub release must attach only sealed distributions")


def _validates_build_manifest_output(job: dict[str, Any]) -> bool:
    """Require an immutable cross-job digest for authority-boundary verification."""
    outputs = _mapping(job.get("outputs"))
    if outputs.get("bundle_manifest_sha256") != "${{ steps.bundle_manifest.outputs.sha256 }}":
        return False
    return any(
        step.get("id") == "bundle_manifest"
        and isinstance(step.get("run"), str)
        and "sha256sum candidate/bundle/SHA256SUMS" in step["run"]
        and "GITHUB_OUTPUT" in step["run"]
        for step in _steps(job)
    )


def _validates_pinned_gitleaks_install(workflow: dict[str, Any], build: dict[str, Any]) -> bool:
    """Require the export scanner's exact version and archive digest before candidate creation."""
    environment = _mapping(workflow.get("env"))
    if environment.get("GITLEAKS_VERSION") != GITLEAKS_VERSION:
        return False
    if environment.get("GITLEAKS_LINUX_X64_SHA256") != GITLEAKS_LINUX_X64_SHA256:
        return False
    commands = tuple(command for step in _steps(build) if isinstance(command := step.get("run"), str))
    return all(fragment in "\n".join(commands) for fragment in _GITLEAKS_INSTALL_COMMANDS)


def _validates_unconditional_candidate_build(build: dict[str, Any]) -> bool:
    """Require every run to scan and build its own candidate rather than acquire one."""
    steps = _steps(build)
    candidate_builds = [step for step in steps if step.get("name") == "Build the one retained public candidate"]
    scanner_installs = [step for step in steps if step.get("name") == "Install the pinned export secret scanner"]
    return (
        len(candidate_builds) == 1
        and "if" not in candidate_builds[0]
        and "scripts/check_public_candidate.py" in str(candidate_builds[0].get("run", ""))
        and len(scanner_installs) == 1
        and "if" not in scanner_installs[0]
    )


def _validate_candidate_identity(jobs: dict[str, Any], findings: list[Finding]) -> None:
    """Require one attempt-independent candidate artifact, uploaded once and never overwritten."""
    for name in sorted(_REQUIRED_JOBS & set(jobs)):
        for step in _steps(_mapping(jobs[name])):
            uses = step.get("uses")
            if not isinstance(uses, str) or not uses.startswith(
                ("actions/upload-artifact@", "actions/download-artifact@")
            ):
                continue
            artifact = str(_mapping(step.get("with")).get("name", ""))
            if artifact.startswith("release-candidate-") and (
                artifact != _CANDIDATE_ARTIFACT
                or (
                    uses.startswith("actions/upload-artifact@")
                    and (name != "build" or "overwrite" in _mapping(step.get("with")))
                )
            ):
                _add(
                    findings, "RWF031", name, "the candidate artifact must keep one run-scoped identity across re-runs"
                )


def _validate_consumer_job(name: str, job: dict[str, Any], findings: list[Finding]) -> None:
    """Require a post-publication verifier to consume only retained inputs from its own index."""
    steps = _steps(job)
    actions = frozenset(uses.partition("@")[0] for step in steps if isinstance(uses := step.get("uses"), str))
    commands = tuple(command for step in steps if isinstance(command := step.get("run"), str))
    uploads = tuple(
        _mapping(step.get("with"))
        for step in steps
        if isinstance(step.get("uses"), str) and step["uses"].startswith("actions/upload-artifact@")
    )
    valid_upload = (
        len(uploads) == 1
        and uploads[0].get("path") == "consumer-evidence.json"
        and uploads[0].get("if-no-files-found") == "error"
        and uploads[0].get("retention-days") == "90"
    )
    endpoint, download_host = _CONSUMER_INDEXES[name]
    verifiers = [command for command in commands if "python -m scripts.check_release_consumer" in command]
    if (
        actions != _CONSUMER_ACTIONS
        or any(action == "actions/checkout" or action.startswith("./") for action in actions)
        or len(verifiers) != 1
        or f'--index-endpoint "{endpoint}' not in verifiers[0]
        or re.findall(r"--download-host (\S+)", verifiers[0]) != [download_host]
        or not valid_upload
        or not any(step.get("if") == "always()" for step in steps)
        or _contains_secret(job)
    ):
        _add(findings, "RWF014", name, "consumer verifier must be artifact-only, index-bound, and retain evidence")


def _validate_promotion_evidence_job(job: dict[str, Any], findings: list[Finding]) -> None:
    """Require retained evidence even when an earlier candidate artifact is absent."""
    steps = _steps(job)
    actions = frozenset(uses.partition("@")[0] for step in steps if isinstance(uses := step.get("uses"), str))
    commands = tuple(command for step in steps if isinstance(command := step.get("run"), str))
    uploads = tuple(
        _mapping(step.get("with"))
        for step in steps
        if isinstance(step.get("uses"), str) and step["uses"].startswith("actions/upload-artifact@")
    )
    valid_upload = (
        len(uploads) == 1
        and uploads[0].get("path") == "promotion-evidence.json"
        and uploads[0].get("if-no-files-found") == "error"
        and uploads[0].get("retention-days") == "90"
    )
    candidate_downloads = tuple(
        step
        for step in steps
        if isinstance(step.get("uses"), str)
        and step["uses"].startswith("actions/download-artifact@")
        and step.get("id") == "candidate"
    )
    if (
        actions != _PROMOTION_EVIDENCE_ACTIONS
        or any(action == "actions/checkout" or action.startswith("./") for action in actions)
        or not any("python -m scripts.release_promotion_evidence" in command for command in commands)
        or not any("--candidate-unavailable" in command for command in commands)
        or len(candidate_downloads) != 1
        or not _is_true(candidate_downloads[0].get("continue-on-error"))
        or not valid_upload
        or not any(step.get("if") == "always()" for step in steps)
        or _contains_secret(job)
    ):
        _add(
            findings,
            "RWF015",
            _PROMOTION_EVIDENCE_JOB,
            "promotion evidence must be artifact-only, partial-run capable, and retained",
        )


def _validate_context_evidence_tools(job: dict[str, Any], findings: list[Finding]) -> None:
    """Require a run-scoped renderer artifact before later jobs can become partial."""
    uploads = tuple(
        _mapping(step.get("with"))
        for step in _steps(job)
        if isinstance(step.get("uses"), str) and step["uses"].startswith("actions/upload-artifact@")
    )
    valid_upload = (
        len(uploads) == 1
        and uploads[0].get("name") == "release-evidence-tools-${{ github.run_id }}-${{ github.sha }}"
        and frozenset(str(uploads[0].get("path", "")).split()) == _CONTEXT_EVIDENCE_PATHS
        and uploads[0].get("if-no-files-found") == "error"
        and uploads[0].get("retention-days") == "90"
    )
    if not valid_upload:
        _add(
            findings,
            "RWF016",
            "context",
            "context must retain the exact promotion-evidence renderer closure",
        )


def recorded_boundary_actions(source: str) -> dict[str, str]:
    """Read the boundary actions promotion evidence records, without importing the renderer."""
    try:
        body = ast.parse(source).body
    except SyntaxError:
        return {}
    for node in body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_BOUNDARY_ACTIONS" for target in node.targets
        ):
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                return {}
            if isinstance(value, dict) and all(isinstance(item, str) for item in (*value, *value.values())):
                return value
    return {}


def validate_recorded_actions(document: object, recorded: Mapping[str, str]) -> Report:
    """Require promotion evidence to name the exact action revisions the workflow runs."""
    jobs = _mapping(_mapping(document).get("jobs"))
    findings: list[Finding] = []
    for name, boundary in sorted(_RECORDED_ACTION_JOBS.items()):
        expected = recorded.get(boundary, "")
        action = expected.partition("@")[0]
        references = {
            uses
            for step in _steps(_mapping(jobs.get(name)))
            if isinstance(uses := step.get("uses"), str) and uses.partition("@")[0] == action
        }
        if not _PINNED_ACTION_REFERENCE.fullmatch(expected) or references != {expected}:
            _add(findings, "RWF028", name, f"job must run the action promotion evidence records for {boundary!r}")
    fallback = "\n".join(
        command
        for step in _steps(_mapping(jobs.get(_PROMOTION_EVIDENCE_JOB)))
        if isinstance(command := step.get("run"), str)
    )
    pinned = {name: value for name, value in recorded.items() if _PINNED_ACTION_REFERENCE.fullmatch(value)}
    if (
        not pinned
        or set(_PINNED_ACTION_REFERENCE.findall(fallback)) != set(pinned.values())
        or not all(
            re.search(rf"""["']{re.escape(name)}["']\s*:\s*["']{re.escape(value)}["']""", fallback)
            for name, value in pinned.items()
        )
    ):
        _add(
            findings,
            "RWF028",
            _PROMOTION_EVIDENCE_JOB,
            "unavailable-candidate evidence must record the same action revisions",
        )
    return Report(tuple(sorted(set(findings))))


def validate_document(document: object) -> Report:
    """Validate one parsed release workflow without evaluating workflow expressions."""
    workflow = _mapping(document)
    findings: list[Finding] = []
    _validate_top_level(workflow, findings)
    jobs = _mapping(workflow.get("jobs"))
    if set(jobs) != _REQUIRED_JOBS:
        _add(findings, "RWF011", "workflow", "release workflow must contain exactly the declared jobs")
    for name in sorted(_REQUIRED_JOBS & set(jobs)):
        job = _mapping(jobs[name])
        _validate_job_shape(name, job, findings)
        if name in _AUTHORITY_JOBS:
            _validate_authority_job(name, job, findings)
    for name in sorted(_CONSUMER_INDEXES.keys() & jobs.keys()):
        _validate_consumer_job(name, _mapping(jobs[name]), findings)
    _validate_candidate_identity(jobs, findings)
    promotion_evidence = _mapping(jobs.get(_PROMOTION_EVIDENCE_JOB))
    if promotion_evidence:
        _validate_promotion_evidence_job(promotion_evidence, findings)
    context = _mapping(jobs.get("context"))
    if context:
        _validate_context_evidence_tools(context, findings)
    build = _mapping(jobs.get("build"))
    if not _validates_build_manifest_output(build):
        _add(findings, "RWF021", "build", "build must expose the sealed bundle manifest digest to authority jobs")
    if not _validates_pinned_gitleaks_install(workflow, build):
        _add(
            findings, "RWF022", "build", "build must install the pinned export secret scanner before candidate creation"
        )
    if not _validates_unconditional_candidate_build(build):
        _add(findings, "RWF026", "build", "every run must scan and build its own single candidate")
    if not _validates_signed_production_tag(context):
        _add(findings, "RWF012", "context", "production tags must be verified through GitHub's signed-tag record")
    if not _requires_main_for_dispatch(context):
        _add(findings, "RWF013", "context", "manual release runs must start from the protected main ref")
    if not _requires_tag_on_main(context):
        _add(findings, "RWF030", "context", "production tags must name a commit on the protected main branch")
    return Report(tuple(sorted(set(findings))))


def validate_repository(repo_root: Path) -> Report:
    """Validate the checked-in release workflow without evaluating expressions."""
    workflow_path = repo_root / WORKFLOW_PATH
    document = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    recorded = recorded_boundary_actions((repo_root / EVIDENCE_PATH).read_text(encoding="utf-8"))
    findings = {
        *validate_document(document).findings,
        *validate_recorded_actions(document, recorded).findings,
    }
    return Report(tuple(sorted(findings)))


def _parser() -> argparse.ArgumentParser:
    """Build the release-workflow policy command surface."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Validate the checked-in release workflow and render stable evidence."""
    args = _parser().parse_args(argv)
    try:
        report = validate_repository(args.repo_root.resolve())
    except (OSError, yaml.YAMLError) as exc:
        print(f"Release workflow policy: ERROR: {exc}", file=sys.stderr)
        return 2
    if args.as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    elif report.ok:
        print("Release workflow policy: PASS")
    else:
        print(f"Release workflow policy: FAIL ({len(report.findings)} finding(s))")
        for finding in report.findings:
            print(f"  {finding.code} {finding.job}: {finding.message}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
