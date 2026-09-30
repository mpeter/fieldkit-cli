"""Bounded execution of one complete canonical quality plan."""

from __future__ import annotations

import shutil
import stat
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TextIO, cast

from fieldkit.util.bounded_process import (
    BoundedProcessBytesResult,
    BoundedProcessError,
    run_bounded_process_bytes,
)

if TYPE_CHECKING or __package__:
    from scripts.git_worktree import git_environment
    from scripts.quality_plan import CommandSpec, QualityPlan, StageSpec, plan_sha256
    from scripts.quality_receipt import runner_sha256, sha256_bytes, sha256_file
    from scripts.quality_source import capture_source_state
    from scripts.semantic_python_changes import has_test_relevant_changes
else:
    from git_worktree import git_environment
    from quality_plan import CommandSpec, QualityPlan, StageSpec, plan_sha256
    from quality_receipt import runner_sha256, sha256_bytes, sha256_file
    from quality_source import capture_source_state
    from semantic_python_changes import has_test_relevant_changes

SCHEMA_VERSION = 1
OUTPUT_LIMIT_BYTES = 8 * 1024 * 1024
CLEANUP_TIMEOUT_SECONDS = 5
TOOL_DIGEST_LIMIT_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class ToolIdentity:
    """Resolved executable identity without serializing a host-private path."""

    argv0: str
    resolved_name: str
    sha256: str | None


def resolve_tool_identity(argv: tuple[str, ...], environment: dict[str, str]) -> ToolIdentity:
    """Resolve and hash the launched executable without retaining its host path."""
    resolved = shutil.which(argv[0], path=environment.get("PATH"))
    if resolved is None:
        return ToolIdentity(argv0=argv[0], resolved_name=argv[0], sha256=None)
    path = Path(resolved).resolve(strict=True)
    digest: str | None = None
    if stat.S_ISREG(path.stat().st_mode):
        try:
            digest = sha256_file(path, maximum_bytes=TOOL_DIGEST_LIMIT_BYTES)
        except (OSError, ValueError):
            digest = None
    return ToolIdentity(argv0=argv[0], resolved_name=path.name, sha256=digest)


def _output_record(content: bytes) -> dict[str, object]:
    return {"status": "complete", "bytes": len(content), "sha256": sha256_bytes(content)}


def _absent_output(status: Literal["unavailable", "not-run"]) -> dict[str, object]:
    return {"status": status, "bytes": None, "sha256": None}


def _write_diagnostics(label: str, stream: TextIO, content: bytes) -> None:
    if not content:
        return
    text = content.decode("utf-8", errors="replace")
    for line in text.splitlines(keepends=True):
        stream.write(f"[quality:{label}] {line}")
    if text and not text.endswith(("\n", "\r")):
        stream.write("\n")


def _command_template(command: CommandSpec, timeout: float, status: str) -> dict[str, object]:
    output_status: Literal["unavailable", "not-run"] = "unavailable" if status == "fail" else "not-run"
    return {
        "argv": list(command.argv),
        "environment": dict(command.environment),
        "timeout_seconds": timeout,
        "status": status,
        "exit_status": None,
        "failure_reason": None,
        "elapsed_seconds": 0.0,
        "stdout": _absent_output(output_status),
        "stderr": _absent_output(output_status),
        "tool": None,
    }


def _run_command(command: CommandSpec, *, repo: Path, timeout: float, label: str) -> dict[str, object]:
    environment = git_environment()
    environment.update(dict(command.environment))
    tool = resolve_tool_identity(command.argv, environment)
    started = time.monotonic()
    result: BoundedProcessBytesResult | None = None
    failure_reason: str | None = None
    try:
        result = run_bounded_process_bytes(
            list(command.argv),
            timeout=timeout,
            stdout_limit=OUTPUT_LIMIT_BYTES,
            stderr_limit=OUTPUT_LIMIT_BYTES,
            cleanup_timeout=CLEANUP_TIMEOUT_SECONDS,
            cwd=repo,
            env=environment,
        )
    except BoundedProcessError as error:
        failure_reason = error.reason
    elapsed = time.monotonic() - started
    if result is None:
        return {
            **_command_template(command, timeout, "fail"),
            "failure_reason": failure_reason,
            "elapsed_seconds": elapsed,
            "tool": asdict(tool),
        }
    _write_diagnostics(f"{label}:stdout", sys.stdout, result.stdout)
    _write_diagnostics(f"{label}:stderr", sys.stderr, result.stderr)
    return {
        "argv": list(command.argv),
        "environment": dict(command.environment),
        "timeout_seconds": timeout,
        "status": "pass" if result.returncode == 0 else "fail",
        "exit_status": result.returncode,
        "failure_reason": None if result.returncode == 0 else "nonzero-exit",
        "elapsed_seconds": elapsed,
        "stdout": _output_record(result.stdout),
        "stderr": _output_record(result.stderr),
        "tool": asdict(tool),
    }


def _not_run_stage(stage: StageSpec, timeout: float) -> dict[str, object]:
    return {
        "label": stage.label,
        "skip_policy": stage.skip_policy,
        "status": "not-run",
        "elapsed_seconds": 0.0,
        "commands": [_command_template(command, timeout, "not-run") for command in stage.commands],
    }


def _skip_stage(stage: StageSpec, timeout: float) -> dict[str, object]:
    return {
        "label": stage.label,
        "skip_policy": stage.skip_policy,
        "status": "skipped-prose-only",
        "elapsed_seconds": 0.0,
        "commands": [_command_template(command, timeout, "skipped-prose-only") for command in stage.commands],
    }


def _run_stage(stage: StageSpec, *, repo: Path, timeout: float) -> dict[str, object]:
    started = time.monotonic()
    deadline = started + timeout
    records: list[dict[str, object]] = []
    stage_failed = False
    for command in stage.commands:
        if stage_failed:
            records.append(_command_template(command, max(0.001, deadline - time.monotonic()), "not-run"))
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            record = _command_template(command, 0.001, "fail")
            record["failure_reason"] = "timeout"
        else:
            record = _run_command(command, repo=repo, timeout=remaining, label=stage.label)
        records.append(record)
        stage_failed = record["status"] != "pass"
    return {
        "label": stage.label,
        "skip_policy": stage.skip_policy,
        "status": "fail" if stage_failed else "pass",
        "elapsed_seconds": time.monotonic() - started,
        "commands": records,
    }


def execute_plan(plan: QualityPlan, *, repo: Path) -> dict[str, object]:
    """Execute a complete plan and derive aggregate authority from child outcomes."""
    source_before = capture_source_state(repo)
    records: list[dict[str, object]] = []
    stopped = False
    for stage in plan.stages:
        if stopped:
            records.append(_not_run_stage(stage, plan.stage_timeout_seconds))
            continue
        if (
            stage.skip_policy == "prose-only"
            and plan.tier == "pr"
            and plan.quality_base is not None
            and not has_test_relevant_changes(plan.quality_base, repo)
        ):
            records.append(_skip_stage(stage, plan.stage_timeout_seconds))
            continue
        record = _run_stage(stage, repo=repo, timeout=plan.stage_timeout_seconds)
        records.append(record)
        stopped = record["status"] == "fail"
    source_after = capture_source_state(repo)
    source_drift = source_before != source_after
    complete = all(
        command["status"] not in {"not-run", "fail"}
        for stage in records
        for command in cast(list[dict[str, object]], stage["commands"])
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "fieldkit-quality-gate-receipt",
        "status": "fail" if stopped or source_drift else "pass",
        "complete": complete,
        "failure_reason": "source-drift" if source_drift else ("stage-failed" if stopped else None),
        "candidate_scope": "clean-head" if source_before.worktree == "clean" else "dirty-local",
        "trust": "unqualified-controller",
        "immutable_release_evidence": False,
        "unproven_gates": [
            "external-controller-qualification",
            "maintained-runtime-qualification",
            "filesystem-isolation",
            "network-isolation",
            "pinned-toolchain-qualification",
            "immutable-same-candidate-release-evidence",
        ],
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "plan": {
            "tier": plan.tier,
            "sha256": plan_sha256(plan),
            "runner_sha256": runner_sha256(),
            "quality_base": plan.quality_base,
            "candidate_head": plan.candidate_head,
            "workers": plan.workers,
            "stage_timeout_seconds": plan.stage_timeout_seconds,
        },
        "source_before": asdict(source_before),
        "source_after": asdict(source_after),
        "stages": records,
    }
