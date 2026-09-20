#!/usr/bin/env python3
"""Validate GitHub Actions workflows against fieldkit's public trust policy."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = Path(".github/workflows")

_FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")
_USES_LINE = re.compile(
    r"^\s*(?:-\s+)?uses:\s*(?P<reference>[^\s#]+)(?P<suffix>[^\n]*)",
    flags=re.MULTILINE,
)
_DOCKER_DIGEST = re.compile(r"docker://[^\s@]+@sha256:[0-9a-fA-F]{64}")
_VERSION_COMMENT = re.compile(r"#\s*v\d")
_SECRET_EXPRESSION = re.compile(r"\$\{\{\s*secrets(?:\.|\[)")
_GITHUB_TOKEN_EXPRESSION = re.compile(r"\$\{\{\s*github\.token\s*\}\}")
_EVENT_RUN_EXPRESSION = re.compile(r"\$\{\{[^}\n]*\bgithub\.event\b[^}\n]*\}\}")
_MATRIX_RUNNER = re.compile(r"\$\{\{\s*matrix\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_DEPENDENCY_INSTALL = re.compile(
    r"\b(?:uv\s+(?:sync|run)|pip3?\s+install|python3?\s+-m\s+pip\s+install|npm\s+(?:ci|install)|bun\s+install)\b"
)
_COMMUNITY_EVENTS = frozenset({"issues", "issue_comment", "pull_request_review_comment", "pull_request_target"})
_COMMUNITY_WRITE_SCOPES = frozenset({"issues", "pull-requests"})
_PRIVILEGED_EVENTS = frozenset(
    {"issues", "issue_comment", "pull_request_review_comment", "pull_request_target", "workflow_run"}
)
_CLASSIFIED_EVENTS = _PRIVILEGED_EVENTS | frozenset(
    {"pull_request", "push", "schedule", "workflow_call", "workflow_dispatch"}
)
_SAFE_PRIVILEGED_REFS = frozenset({"${{ github.sha }}", "${{ github.event.pull_request.base.sha }}"})
_ARTIFACT_REVISION_EXPRESSIONS = ("${{ github.sha }}", "${{ github.event.pull_request.head.sha }}")
_ARTIFACT_EVENT_EXPRESSIONS = ("${{ github.run_id }}", "${{ github.event.pull_request.number }}")


@dataclass(frozen=True, order=True)
class Finding:
    """One payload-safe workflow policy violation."""

    code: str
    path: str
    subject: str


@dataclass(frozen=True)
class ValidationReport:
    """Versioned workflow-policy result."""

    schema_version: int
    scanned_workflows: int
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "status": "pass" if self.ok else "fail",
            "scanned_workflows": self.scanned_workflows,
            "findings": [asdict(finding) for finding in self.findings],
        }


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _events(document: dict[str, Any]) -> frozenset[str]:
    value = document.get("on")
    if isinstance(value, str):
        return frozenset({value})
    if isinstance(value, list):
        return frozenset(str(item) for item in value)
    return frozenset(_mapping(value))


def _permission_writes(value: object) -> frozenset[str]:
    return frozenset(key for key, permission in _mapping(value).items() if permission == "write")


def _runner_values(job: dict[str, Any]) -> tuple[str, ...]:
    value = job.get("runs-on")
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    if not isinstance(value, str):
        return ()
    matrix_match = _MATRIX_RUNNER.fullmatch(value)
    if matrix_match is None:
        return (value,)
    strategy = _mapping(job.get("strategy"))
    matrix = _mapping(strategy.get("matrix"))
    choices = matrix.get(matrix_match.group(1))
    if not isinstance(choices, list):
        return (value,)
    return tuple(str(item) for item in choices)


def _is_github_hosted_runner(value: str) -> bool:
    if "${{" in value:
        return False
    return value.startswith(("ubuntu-", "macos-", "windows-"))


def _steps(job: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    raw_steps = job.get("steps")
    if not isinstance(raw_steps, list):
        return ()
    return tuple(_mapping(step) for step in raw_steps if isinstance(step, dict))


def _is_codeql_analysis_job(job: dict[str, Any]) -> bool:
    """Return whether a job contains both official CodeQL analysis boundaries."""
    allowed_actions = {
        "actions/checkout",
        "github/codeql-action/init",
        "github/codeql-action/analyze",
    }
    actions: set[str] = set()
    for step in _steps(job):
        uses = step.get("uses")
        if not isinstance(uses, str):
            return False
        action = uses.partition("@")[0]
        if action not in allowed_actions:
            return False
        actions.add(action)
    return {"github/codeql-action/init", "github/codeql-action/analyze"} <= actions


def _validate_artifact_upload(path: str, job_name: str, step: dict[str, Any], findings: list[Finding]) -> None:
    uses = step.get("uses")
    if not isinstance(uses, str) or not uses.startswith("actions/upload-artifact@"):
        return
    options = _mapping(step.get("with"))
    name = options.get("name")
    retention = options.get("retention-days")
    has_attributable_name = (
        isinstance(name, str)
        and any(revision in name for revision in _ARTIFACT_REVISION_EXPRESSIONS)
        and any(event in name for event in _ARTIFACT_EVENT_EXPRESSIONS)
    )
    try:
        retention_days = int(retention) if isinstance(retention, str) else 0
    except ValueError:
        retention_days = 0
    if options.get("if-no-files-found") != "error" or not 1 <= retention_days <= 90 or not has_attributable_name:
        findings.append(Finding("WF012", path, f"artifact upload lacks fail-closed attributable policy: {job_name}"))


def _validate_permissions(path: str, document: dict[str, Any], findings: list[Finding]) -> None:
    if "permissions" not in document or not isinstance(document["permissions"], dict):
        findings.append(Finding("WF001", path, "top-level permissions must be an explicit mapping"))
        return
    for scope in sorted(_permission_writes(document["permissions"])):
        findings.append(Finding("WF002", path, f"top-level write permission: {scope}"))


def _validate_events(path: str, events: frozenset[str], findings: list[Finding]) -> None:
    if not events:
        findings.append(Finding("WF011", path, "workflow trigger is missing"))
        return
    for event in sorted(events - _CLASSIFIED_EVENTS):
        findings.append(Finding("WF011", path, f"unclassified workflow trigger: {event}"))


def _validate_uses(path: str, text: str, findings: list[Finding]) -> None:
    for match in _USES_LINE.finditer(text):
        reference = match.group("reference").strip("\"'")
        if reference.startswith("./"):
            continue
        if reference.startswith("docker://"):
            if _DOCKER_DIGEST.fullmatch(reference) is None:
                findings.append(Finding("WF003", path, f"mutable external action: {reference}"))
            continue
        _, separator, revision = reference.rpartition("@")
        if not separator or _FULL_SHA.fullmatch(revision) is None:
            findings.append(Finding("WF003", path, f"mutable external action: {reference}"))
        elif _VERSION_COMMENT.search(match.group("suffix")) is None:
            findings.append(Finding("WF010", path, f"pinned action lacks version comment: {reference}"))


def _validate_jobs(
    path: str,
    text: str,
    document: dict[str, Any],
    events: frozenset[str],
    findings: list[Finding],
) -> None:
    jobs = _mapping(document.get("jobs"))
    is_pull_request = "pull_request" in events
    is_privileged = bool(events & _PRIVILEGED_EVENTS)
    is_community_mutation = bool(events & _COMMUNITY_EVENTS)

    if is_pull_request and (_SECRET_EXPRESSION.search(text) or _GITHUB_TOKEN_EXPRESSION.search(text)):
        findings.append(Finding("WF006", path, "pull_request workflow explicitly references a repository token"))
    if is_privileged and (
        "github.event.pull_request.head" in text
        or "github.event.workflow_run.head" in text
        or "github.head_ref" in text
        or "actions/download-artifact@" in text
        or "actions/cache@" in text
        or re.search(r"^\s+enable-cache:\s*true\s*$", text, flags=re.MULTILINE)
    ):
        findings.append(Finding("WF007", path, "privileged workflow consumes contributor or shared cached content"))

    for job_name, raw_job in jobs.items():
        job = _mapping(raw_job)
        runners = _runner_values(job)
        is_reusable_job = isinstance(job.get("uses"), str)
        if (not runners and not is_reusable_job) or any(
            not _is_github_hosted_runner(runner.lower()) for runner in runners
        ):
            findings.append(Finding("WF004", path, f"non-GitHub-hosted or dynamic runner in job {job_name}"))
        if "permissions" in job and not isinstance(job["permissions"], dict):
            findings.append(Finding("WF005", path, f"job permissions must be an explicit mapping: {job_name}"))
        job_writes = _permission_writes(job.get("permissions"))
        if is_community_mutation:
            for scope in sorted(job_writes - _COMMUNITY_WRITE_SCOPES):
                findings.append(Finding("WF005", path, f"unrelated community-event write in job {job_name}: {scope}"))
        if is_pull_request:
            if job.get("secrets") not in (None, {}):
                findings.append(Finding("WF006", path, f"pull_request job receives secrets: {job_name}"))
            if job.get("environment") is not None:
                findings.append(Finding("WF006", path, f"pull_request job targets an environment: {job_name}"))
            allowed_writes = frozenset({"security-events"}) if _is_codeql_analysis_job(job) else frozenset()
            for scope in sorted(job_writes - allowed_writes):
                findings.append(Finding("WF005", path, f"pull_request write in job {job_name}: {scope}"))
        if is_privileged and "timeout-minutes" not in job:
            findings.append(Finding("WF008", path, f"privileged job lacks timeout: {job_name}"))
        if is_privileged and (job.get("container") is not None or job.get("services") is not None):
            findings.append(Finding("WF007", path, f"privileged job uses a container or service: {job_name}"))
        for step in _steps(job):
            _validate_artifact_upload(path, job_name, step, findings)
            command = step.get("run")
            if isinstance(command, str) and _EVENT_RUN_EXPRESSION.search(command):
                findings.append(Finding("WF009", path, f"event expression interpolated into run in job {job_name}"))
            if is_privileged and isinstance(command, str) and _DEPENDENCY_INSTALL.search(command):
                findings.append(Finding("WF007", path, f"privileged job installs or resolves dependencies: {job_name}"))
            if not is_privileged:
                continue
            uses = step.get("uses")
            if not isinstance(uses, str) or not uses.startswith("actions/checkout@"):
                continue
            options = _mapping(step.get("with"))
            if options.get("ref") not in _SAFE_PRIVILEGED_REFS or options.get("persist-credentials") != "false":
                findings.append(Finding("WF007", path, f"privileged checkout is not pinned to trusted ref: {job_name}"))


def validate(repo_root: Path = REPO_ROOT) -> ValidationReport:
    """Validate every workflow without executing or interpolating its contents."""
    workflow_root = repo_root / WORKFLOW_DIR
    paths = sorted((*workflow_root.glob("*.yml"), *workflow_root.glob("*.yaml")))
    findings: list[Finding] = []
    for workflow_path in paths:
        relative = workflow_path.relative_to(repo_root).as_posix()
        text = workflow_path.read_text(encoding="utf-8")
        try:
            document = _mapping(yaml.load(text, Loader=yaml.BaseLoader))
        except yaml.YAMLError:
            findings.append(Finding("WF000", relative, "workflow is not valid YAML"))
            continue
        events = _events(document)
        _validate_events(relative, events, findings)
        _validate_permissions(relative, document, findings)
        _validate_uses(relative, text, findings)
        _validate_jobs(relative, text, document, events, findings)
    return ValidationReport(schema_version=1, scanned_workflows=len(paths), findings=tuple(sorted(set(findings))))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = validate(args.repo_root)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    elif report.ok:
        print(f"Workflow security check: PASS ({report.scanned_workflows} workflow(s))")
    else:
        print(f"Workflow security check: FAIL ({len(report.findings)} finding(s))")
        for finding in report.findings:
            print(f"  {finding.code} {finding.path}: {finding.subject}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
