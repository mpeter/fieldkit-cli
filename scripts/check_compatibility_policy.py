#!/usr/bin/env python3
"""Validate compatibility policy, workflow coverage, and documentation claims."""

import argparse
import json
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = Path("docs/release-readiness/compatibility-policy.json")
WORKFLOW_PATH = Path(".github/workflows/compatibility.yml")
DOCUMENTATION_PATH = Path("docs/getting-started.md")
COMPATIBILITY_DOCUMENTATION_PATH = Path("docs/compatibility.md")
PYPROJECT_PATH = Path("pyproject.toml")


@dataclass(frozen=True, order=True)
class Finding:
    """One compatibility-policy drift finding."""

    criterion_id: str
    subject: str
    message: str


@dataclass(frozen=True)
class Report:
    """Versioned compatibility-policy validation result."""

    schema_version: int
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        """Return whether policy, workflow, metadata, and docs agree."""
        return not self.findings

    def to_dict(self) -> dict[str, object]:
        """Render the stable machine-readable result."""
        return {
            "schema_version": self.schema_version,
            "status": "pass" if self.ok else "fail",
            "findings": [asdict(finding) for finding in self.findings],
        }


def _object(value: object, subject: str) -> dict[str, object]:
    """Require an object with string keys."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{subject} must be an object with string keys")
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _strings(value: object, subject: str) -> tuple[str, ...]:
    """Require a non-empty list of unique non-empty strings."""
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{subject} must be a non-empty string list")
    result = tuple(item for item in value if isinstance(item, str))
    if len(result) != len(set(result)):
        raise ValueError(f"{subject} must not contain duplicates")
    return result


def _exact_keys(value: dict[str, object], expected: set[str], subject: str) -> None:
    """Reject missing or unknown schema fields."""
    if set(value) != expected:
        raise ValueError(f"{subject} keys must be exactly {sorted(expected)}")


def _platform_name(system: str) -> str:
    if system.startswith("ubuntu-"):
        return "Ubuntu"
    if system.startswith("macos-"):
        return "macOS"
    return system


def _human_join(values: tuple[str, ...]) -> str:
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} and {values[1]}"
    return f"{', '.join(values[:-1])}, and {values[-1]}"


def _documentation_block(
    pythons: tuple[str, ...],
    systems: tuple[str, ...],
    container: str,
    linux_python: str,
    profile_python: str,
    profile_systems: tuple[str, ...],
) -> str:
    """Render the policy-owned support statement embedded in getting started."""
    python_text = _human_join(tuple(f"CPython {version}" for version in pythons))
    system_text = _human_join(tuple(f"`{system}`" for system in systems))
    return (
        "<!-- compatibility-policy:start -->\n"
        f"fieldkit's portable core is tested on {python_text} across {system_text}. "
        f"The same core contract runs with {container.replace(':', ' ').title()}'s {linux_python} Python in a "
        f"`{container}` container. CI separately installs the exact candidate with its `all` optional profile on "
        f"CPython {profile_python} for {' and '.join(_platform_name(system) for system in profile_systems)}, then "
        "imports every advertised integration dependency. Native Windows is experimental. "
        "Linux-only integrations such as browser credential extraction and systemd units have narrower "
        "requirements than the core.\n"
        "<!-- compatibility-policy:end -->"
    )


def _compatibility_table(pythons: tuple[str, ...], systems: tuple[str, ...], container: str) -> str:
    """Render the policy-owned public support matrix."""
    python_text = ", ".join(pythons)
    system_rows = [f"| {_platform_name(system)} | {python_text} | Supported core |" for system in systems]
    container_name, _, container_version = container.partition(":")
    container_text = f"{container_name.title()} {container_version} system Python"
    return "\n".join(
        [
            "| Operating system | Python | Status |",
            "| --- | --- | --- |",
            *system_rows,
            f"| Fedora/RHEL family | {container_text} | Supported core |",
            f"| Windows | {pythons[0]} or newer | Experimental |",
        ]
    )


def _job_has_profile_smoke(job: dict[object, object], python_version: str) -> bool:
    steps = job.get("steps")
    if not isinstance(steps, list):
        return False
    python_step: int | None = None
    profile_step: int | None = None
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        configuration = step.get("with")
        action = step.get("uses")
        if (
            isinstance(action, str)
            and action.startswith("actions/setup-python@")
            and isinstance(configuration, dict)
            and configuration.get("python-version") == python_version
        ):
            python_step = index
        command = step.get("run")
        if isinstance(command, str) and "--profile all" in command:
            profile_step = index
    return python_step is not None and profile_step is not None and python_step < profile_step


def validate(repo_root: Path = REPO_ROOT) -> Report:
    """Validate every supported claim against checked-in policy and CI evidence."""
    raw = _object(json.loads((repo_root / POLICY_PATH).read_text(encoding="utf-8")), str(POLICY_PATH))
    _exact_keys(
        raw,
        {
            "schema_version",
            "evidence_schema_version",
            "core",
            "linux_family",
            "optional_profiles",
            "experimental",
            "integration_limits",
        },
        str(POLICY_PATH),
    )
    if raw.get("schema_version") != 1 or raw.get("evidence_schema_version") != 5:
        raise ValueError(f"{POLICY_PATH}: policy schema must be 1 and evidence schema must be 5")
    core = _object(raw.get("core"), "core")
    _exact_keys(core, {"status", "python", "operating_systems"}, "core")
    if core.get("status") != "supported":
        raise ValueError("core.status must be supported")
    pythons = _strings(core.get("python"), "core.python")
    systems = _strings(core.get("operating_systems"), "core.operating_systems")
    linux = _object(raw.get("linux_family"), "linux_family")
    _exact_keys(linux, {"status", "container", "python"}, "linux_family")
    container = linux.get("container")
    linux_python = linux.get("python")
    if (
        linux.get("status") != "supported"
        or not isinstance(container, str)
        or not container
        or not isinstance(linux_python, str)
        or not linux_python
    ):
        raise ValueError("linux_family must declare a supported container")
    experimental = raw.get("experimental")
    if not isinstance(experimental, list) or not any(
        isinstance(item, dict) and item.get("operating_system") == "windows" for item in experimental
    ):
        raise ValueError("experimental must explicitly classify windows")
    limits = _object(raw.get("integration_limits"), "integration_limits")
    if not limits:
        raise ValueError("integration_limits must not be empty")
    profiles = _object(raw.get("optional_profiles"), "optional_profiles")
    _exact_keys(profiles, {"status", "artifact_smoke", "operating_systems", "python"}, "optional_profiles")
    profile_python = profiles.get("python")
    profile_systems = _strings(profiles.get("operating_systems"), "optional_profiles.operating_systems")
    if (
        profiles.get("status") != "supported"
        or profiles.get("artifact_smoke") != "all"
        or not isinstance(profile_python, str)
        or not profile_python
    ):
        raise ValueError("optional_profiles must require the all-profile artifact smoke")

    findings: list[Finding] = []
    workflow = (repo_root / WORKFLOW_PATH).read_text(encoding="utf-8")
    workflow_document = yaml.safe_load(workflow)
    workflow_jobs = workflow_document.get("jobs") if isinstance(workflow_document, dict) else None
    if not isinstance(workflow_jobs, dict):
        raise ValueError(f"{WORKFLOW_PATH}: jobs must be an object")
    core_job = workflow_jobs.get("core")
    core_strategy = core_job.get("strategy") if isinstance(core_job, dict) else None
    core_matrix = core_strategy.get("matrix") if isinstance(core_strategy, dict) else None
    expected_pairs = [
        {"first": first, "second": second} for first, second in zip(pythons[::2], pythons[1::2], strict=True)
    ]
    if (
        not isinstance(core_matrix, dict)
        or core_matrix.get("os") != list(systems)
        or core_matrix.get("python-pair") != expected_pairs
    ):
        findings.append(Finding("COMP101", "core", "supported policy matrix is absent from workflow"))
    fedora_job = workflow_jobs.get("fedora-family")
    if not isinstance(fedora_job, dict) or fedora_job.get("container") != container:
        findings.append(Finding("COMP101", container, "supported Linux-family container is absent from workflow"))
    if "permissions:\n  contents: read" not in workflow or "secrets." in workflow or "pull_request_target" in workflow:
        findings.append(Finding("COMP102", str(WORKFLOW_PATH), "workflow must be read-only and fork-safe"))
    if "scripts/smoke_artifact.py" not in workflow or "actions/download-artifact@" not in workflow:
        findings.append(Finding("COMP103", str(WORKFLOW_PATH), "workflow must reuse the installed-artifact runner"))
    for system in profile_systems:
        matching_jobs = [
            job for job in workflow_jobs.values() if isinstance(job, dict) and job.get("runs-on") == system
        ]
        if not any(_job_has_profile_smoke(job, profile_python) for job in matching_jobs):
            findings.append(Finding("COMP106", system, f"CPython {profile_python} all-profile job is absent"))

    project = _object(tomllib.loads((repo_root / PYPROJECT_PATH).read_text(encoding="utf-8")).get("project"), "project")
    classifiers = _strings(project.get("classifiers"), "project.classifiers")
    for version in pythons:
        if f"Programming Language :: Python :: {version}" not in classifiers:
            findings.append(Finding("COMP104", version, "supported Python is absent from package classifiers"))

    docs = (repo_root / DOCUMENTATION_PATH).read_text(encoding="utf-8")
    expected_block = _documentation_block(pythons, systems, container, linux_python, profile_python, profile_systems)
    if expected_block not in docs:
        findings.append(Finding("COMP105", str(DOCUMENTATION_PATH), "support statement does not match policy"))
    compatibility_docs = (repo_root / COMPATIBILITY_DOCUMENTATION_PATH).read_text(encoding="utf-8")
    if _compatibility_table(pythons, systems, container) not in compatibility_docs:
        findings.append(
            Finding("COMP107", str(COMPATIBILITY_DOCUMENTATION_PATH), "support matrix does not match policy")
        )
    return Report(1, tuple(sorted(findings)))


def _parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run policy validation and return a stable process status."""
    args = _parser().parse_args(argv)
    try:
        report = validate(args.repo_root.resolve())
    except (OSError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        print(f"Compatibility policy: ERROR: {exc}", file=sys.stderr)
        return 2
    if args.as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    elif report.ok:
        print("Compatibility policy: PASS")
    else:
        print(f"Compatibility policy: FAIL ({len(report.findings)} finding(s))")
        for finding in report.findings:
            print(f"  {finding.criterion_id} {finding.subject}: {finding.message}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
