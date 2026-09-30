"""Schema, identity, persistence, and validation for quality receipts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from jsonschema import Draft202012Validator, FormatChecker

from fieldkit.util.atomic import atomic_text_write

if TYPE_CHECKING or __package__:
    from scripts.json_policy import reject_duplicate_json_keys
    from scripts.quality_plan import QualityPlan, plan_sha256, report_path
    from scripts.quality_source import SourceState
else:
    from json_policy import reject_duplicate_json_keys
    from quality_plan import QualityPlan, plan_sha256, report_path
    from quality_source import SourceState

SCHEMA_LIMIT_BYTES = 256 * 1024
RECEIPT_LIMIT_BYTES = 4 * 1024 * 1024
SOURCE_DIGEST_LIMIT_BYTES = 64 * 1024 * 1024

_ROOT = Path(__file__).resolve().parent.parent
_SCHEMA_PATH = _ROOT / "docs/release-readiness/quality-gate-receipt.schema.json"
_RUNNER_SOURCE_PATHS = (
    Path("docs/release-readiness/quality-gate-receipt.schema.json"),
    Path("scripts/git_worktree.py"),
    Path("scripts/json_policy.py"),
    Path("scripts/quality_execution.py"),
    Path("scripts/quality_gate.py"),
    Path("scripts/quality_plan.py"),
    Path("scripts/quality_receipt.py"),
    Path("scripts/quality_source.py"),
    Path("scripts/semantic_python_changes.py"),
    Path("src/fieldkit/config/__init__.py"),
    Path("src/fieldkit/config/_timeouts.py"),
    Path("src/fieldkit/util/atomic.py"),
    Path("src/fieldkit/util/bounded_process.py"),
)


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path, *, maximum_bytes: int | None = None) -> str:
    digest = hashlib.sha256()
    consumed = 0
    with path.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            consumed += len(chunk)
            if maximum_bytes is not None and consumed > maximum_bytes:
                raise ValueError("file exceeds the digest size limit")
            digest.update(chunk)
    return digest.hexdigest()


def runner_sha256() -> str:
    """Bind the controller digest to every local module used for execution authority."""
    digest = hashlib.sha256()
    for relative in _RUNNER_SOURCE_PATHS:
        path = _ROOT / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError("quality runner source must be a regular non-symlink file")
        relative_bytes = relative.as_posix().encode()
        content_digest = bytes.fromhex(sha256_file(path, maximum_bytes=SOURCE_DIGEST_LIMIT_BYTES))
        digest.update(len(relative_bytes).to_bytes(2, "big"))
        digest.update(relative_bytes)
        digest.update(content_digest)
    return digest.hexdigest()


def read_bounded_json(path: Path, maximum_bytes: int) -> object:
    if path.is_symlink() or not path.is_file():
        raise ValueError("quality JSON input must be a regular non-symlink file")
    with path.open("rb") as stream:
        raw = stream.read(maximum_bytes + 1)
    if len(raw) > maximum_bytes:
        raise ValueError("quality JSON input exceeds its byte bound")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("quality JSON input must be UTF-8") from error
    return json.loads(text, object_pairs_hook=reject_duplicate_json_keys)


def _load_schema() -> dict[str, object]:
    value = read_bounded_json(_SCHEMA_PATH, SCHEMA_LIMIT_BYTES)
    if not isinstance(value, dict):
        raise ValueError("quality receipt schema root must be an object")
    Draft202012Validator.check_schema(value)
    return cast(dict[str, object], value)


def _validator() -> Draft202012Validator:
    return Draft202012Validator(_load_schema(), format_checker=FormatChecker())


def _validate_command(
    record: dict[str, object],
    *,
    expected_argv: tuple[str, ...],
    expected_environment: tuple[tuple[str, str], ...],
    maximum_timeout: float,
    location: str,
) -> list[str]:
    findings: list[str] = []
    if record.get("argv") != list(expected_argv):
        findings.append(f"{location}:argv")
    if record.get("environment") != dict(expected_environment):
        findings.append(f"{location}:environment")
    timeout = record.get("timeout_seconds")
    if not isinstance(timeout, (int, float)) or timeout <= 0 or timeout > maximum_timeout:
        findings.append(f"{location}:timeout")
    status = record.get("status")
    exit_status = record.get("exit_status")
    failure_reason = record.get("failure_reason")
    stdout_status = cast(dict[str, object], record.get("stdout", {})).get("status")
    stderr_status = cast(dict[str, object], record.get("stderr", {})).get("status")
    tool = record.get("tool")
    if isinstance(tool, dict) and tool.get("argv0") != expected_argv[0]:
        findings.append(f"{location}:tool")
    if status == "pass" and (
        exit_status != 0
        or failure_reason is not None
        or stdout_status != "complete"
        or stderr_status != "complete"
        or not isinstance(tool, dict)
    ):
        findings.append(f"{location}:pass-state")
    elif status == "fail":
        nonzero = (
            failure_reason == "nonzero-exit"
            and isinstance(exit_status, int)
            and exit_status != 0
            and stdout_status == "complete"
            and stderr_status == "complete"
        )
        bounded = (
            failure_reason in {"start", "pipes", "timeout", "overflow", "cleanup"}
            and exit_status is None
            and stdout_status == "unavailable"
            and stderr_status == "unavailable"
        )
        if (not nonzero and not bounded) or not isinstance(tool, dict):
            findings.append(f"{location}:fail-state")
    elif status in {"not-run", "skipped-prose-only"} and (
        exit_status is not None
        or failure_reason is not None
        or stdout_status != "not-run"
        or stderr_status != "not-run"
        or tool is not None
    ):
        findings.append(f"{location}:inactive-state")
    return findings


def validate_receipt(
    receipt: object, *, plan: QualityPlan, expected_source: SourceState | None = None
) -> tuple[str, ...]:
    """Return deterministic receipt findings for schema and execution completeness."""
    schema_errors = sorted(_validator().iter_errors(cast(Any, receipt)), key=lambda error: list(error.path))
    if schema_errors:
        return tuple(f"schema:{'/'.join(str(part) for part in error.path) or 'root'}" for error in schema_errors)
    if not isinstance(receipt, dict) or not isinstance(receipt.get("plan"), dict):
        return ("schema:root",)
    findings: list[str] = []
    plan_record = cast(dict[str, object], receipt["plan"])
    expected_plan = {
        "tier": plan.tier,
        "sha256": plan_sha256(plan),
        "runner_sha256": runner_sha256(),
        "quality_base": plan.quality_base,
        "candidate_head": plan.candidate_head,
        "workers": plan.workers,
        "stage_timeout_seconds": plan.stage_timeout_seconds,
    }
    for name, expected in expected_plan.items():
        if plan_record.get(name) != expected:
            findings.append(f"plan:{name}")
    source_before = receipt["source_before"]
    source_after = receipt["source_after"]
    if source_before != source_after:
        findings.append("source:drift")
    if expected_source is not None and source_after != asdict(expected_source):
        findings.append("source:current")
    if plan.candidate_head is not None and (
        not isinstance(source_before, dict) or source_before.get("head") != plan.candidate_head
    ):
        findings.append("source:candidate-head")
    stages = receipt["stages"]
    if not isinstance(stages, list) or len(stages) != len(plan.stages):
        return tuple(sorted({*findings, "stages:count"}))
    for index, (record, spec) in enumerate(zip(stages, plan.stages, strict=True)):
        if not isinstance(record, dict) or record.get("label") != spec.label:
            findings.append(f"stages:{index}:label")
            continue
        if record.get("skip_policy") != spec.skip_policy:
            findings.append(f"stages:{index}:skip-policy")
        commands = record.get("commands")
        if not isinstance(commands, list) or len(commands) != len(spec.commands):
            findings.append(f"stages:{index}:commands:count")
            continue
        for command_index, (command_record, command_spec) in enumerate(zip(commands, spec.commands, strict=True)):
            location = f"stages:{index}:commands:{command_index}"
            if not isinstance(command_record, dict):
                findings.append(f"{location}:record")
                continue
            findings.extend(
                _validate_command(
                    command_record,
                    expected_argv=command_spec.argv,
                    expected_environment=command_spec.environment,
                    maximum_timeout=plan.stage_timeout_seconds,
                    location=location,
                )
            )
        timeouts = [command.get("timeout_seconds") for command in commands if isinstance(command, dict)]
        if timeouts and any(
            not isinstance(current, (int, float)) or not isinstance(previous, (int, float)) or current > previous
            for previous, current in pairwise(timeouts)
        ):
            findings.append(f"stages:{index}:timeouts")
        statuses = [command.get("status") for command in commands if isinstance(command, dict)]
        stage_status = record.get("status")
        uniform = {"pass": {"pass"}, "skipped-prose-only": {"skipped-prose-only"}, "not-run": {"not-run"}}
        if (stage_status in uniform and set(statuses) != uniform[cast(str, stage_status)]) or (
            stage_status == "fail" and "fail" not in statuses
        ):
            findings.append(f"stages:{index}:status")
        if stage_status == "fail" and "fail" in statuses:
            failure_index = statuses.index("fail")
            expected_statuses = ["pass"] * failure_index + ["fail"] + ["not-run"] * (len(statuses) - failure_index - 1)
            if statuses != expected_statuses:
                findings.append(f"stages:{index}:sequence")
    for index, record in enumerate(stages):
        if (
            isinstance(record, dict)
            and record.get("status") == "skipped-prose-only"
            and (plan.tier != "pr" or plan.stages[index].label != "impact-pytest")
        ):
            findings.append(f"stages:{index}:unsupported-skip")
    derived_complete = all(
        isinstance(command, dict) and command.get("status") not in {"not-run", "fail"}
        for record in stages
        if isinstance(record, dict)
        for command in cast(list[object], record.get("commands", []))
    )
    if receipt.get("complete") is not derived_complete:
        findings.append("aggregate:completeness")
    source_drift = source_before != source_after
    failed_stage = any(isinstance(record, dict) and record.get("status") == "fail" for record in stages)
    derived_status = "fail" if source_drift or failed_stage else "pass"
    derived_reason = "source-drift" if source_drift else ("stage-failed" if failed_stage else None)
    if receipt.get("status") != derived_status:
        findings.append("aggregate:status")
    if receipt.get("failure_reason") != derived_reason:
        findings.append("aggregate:failure-reason")
    candidate_scope = (
        "clean-head" if isinstance(source_before, dict) and source_before.get("worktree") == "clean" else "dirty-local"
    )
    if receipt.get("candidate_scope") != candidate_scope:
        findings.append("aggregate:candidate-scope")
    if receipt.get("status") == "pass" and receipt.get("complete") is not True:
        findings.append("aggregate:incomplete-pass")
    return tuple(sorted(set(findings)))


def receipt_passes(receipt: object, *, plan: QualityPlan, expected_source: SourceState | None = None) -> bool:
    """Return whether a receipt proves complete success for its declared local tier."""
    return (
        isinstance(receipt, dict)
        and receipt.get("status") == "pass"
        and receipt.get("complete") is True
        and not validate_receipt(receipt, plan=plan, expected_source=expected_source)
    )


def receipt_path(repo: Path, value: str) -> Path:
    """Resolve and prepare one safe repository-relative receipt path."""
    relative = report_path(value, "receipt")
    if relative is None:
        raise ValueError("receipt path is required")
    destination = repo / relative
    current = repo
    for part in Path(relative).parts[:-1]:
        current = current / part
        if current.exists() and (current.is_symlink() or not current.is_dir()):
            raise ValueError("receipt parent must be a regular repository directory")
        current.mkdir(exist_ok=True)
    if destination.is_symlink():
        raise ValueError("receipt destination must not be a symlink")
    return destination


def write_receipt(path: Path, receipt: dict[str, object]) -> None:
    """Atomically replace one schema-valid local quality receipt."""
    if list(_validator().iter_errors(cast(Any, receipt))):
        raise ValueError("quality receipt does not satisfy its schema")
    atomic_text_write(path, f"{json.dumps(receipt, indent=2, sort_keys=True)}\n", mode=0o600)
