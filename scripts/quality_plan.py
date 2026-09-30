"""Canonical fixed-argv quality plans and their deterministic identity."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fieldkit.config import TIMEOUT_HEALTH_GATE

Tier = Literal["pr", "full", "impact"]
SkipPolicy = Literal["never", "prose-only"]

PR_STAGE_TIMEOUT_SECONDS = 120
FULL_STAGE_TIMEOUT_SECONDS = TIMEOUT_HEALTH_GATE
DEFAULT_WORKERS = 4

_REVISION = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class CommandSpec:
    """One immutable subprocess invocation and its explicit environment overlay."""

    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class StageSpec:
    """One logical gate that may contain a fixed ordered command sequence."""

    label: str
    commands: tuple[CommandSpec, ...]
    skip_policy: SkipPolicy = "never"


@dataclass(frozen=True)
class QualityPlan:
    """Complete ordered execution plan for one supported quality tier."""

    tier: Tier
    stages: tuple[StageSpec, ...]
    stage_timeout_seconds: float
    quality_base: str | None
    candidate_head: str | None
    workers: int


def _command(*argv: str, environment: tuple[tuple[str, str], ...] = ()) -> CommandSpec:
    if not argv or any(not value for value in argv):
        raise ValueError("quality command argv must contain non-empty strings")
    return CommandSpec(argv=tuple(argv), environment=environment)


def _stage(label: str, *commands: CommandSpec, skip_policy: SkipPolicy = "never") -> StageSpec:
    if not label or not commands:
        raise ValueError("quality stages require a label and at least one command")
    return StageSpec(label=label, commands=tuple(commands), skip_policy=skip_policy)


def _hook_paths(repo: Path) -> tuple[str, ...]:
    """Expand the reviewed hook glob deterministically without invoking a shell."""
    hooks = repo / "hooks"
    paths = tuple(sorted(hooks.glob("*.py")))
    if not paths:
        raise ValueError("quality plan requires at least one regular Python hook")
    if len(paths) > 48:
        raise ValueError("quality plan hook expansion exceeds its argv bound")
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise ValueError("quality plan hook expansion requires regular non-symlink files")
    relative = tuple(path.relative_to(repo).as_posix() for path in paths)
    if any(len(path) > 1024 for path in relative):
        raise ValueError("quality plan hook path exceeds its argv bound")
    return relative


def _validate_revision(value: str | None, name: str, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"{name} is required")
        return None
    normalized = value.lower()
    if _REVISION.fullmatch(normalized) is None:
        raise ValueError(f"{name} must be a full 40-character hexadecimal revision")
    return normalized


def report_path(value: str | None, name: str) -> str | None:
    """Validate one bounded repository-relative report destination."""
    if value is None:
        return None
    path = Path(value)
    if path.is_absolute() or not path.parts or path.parts[0] != "reports" or ".." in path.parts or len(value) > 240:
        raise ValueError(f"{name} must be a bounded repository-relative path under reports/")
    return path.as_posix()


def _docs_stage() -> StageSpec:
    return _stage(
        "docs-site",
        _command("uv", "run", "python", "scripts/check_documentation_contract.py"),
        _command("uv", "run", "python", "-m", "scripts.check_documentation_examples"),
        _command("uv", "run", "mkdocs", "build", "--strict", "--site-dir", "build/site"),
        _command("uv", "run", "python", "scripts/check_public_docs.py", "build/site"),
    )


def _common_stages(repo: Path) -> tuple[StageSpec, ...]:
    return (
        _stage("ruff-check", _command("uv", "run", "ruff", "check", ".")),
        _stage("ruff-format", _command("uv", "run", "ruff", "format", "--check", ".")),
        _stage("markdown-links", _command("uvx", "pre-commit==4.6.1", "run", "markdown-link-check", "--all-files")),
        _docs_stage(),
        _stage(
            "mypy",
            _command("uv", "run", "mypy", "src/fieldkit/", *_hook_paths(repo), "--no-error-summary"),
        ),
        _stage("tach", _command("uv", "run", "--locked", "tach", "check")),
        _stage("dependency-profiles", _command("uv", "run", "python", "scripts/check_dependency_profiles.py")),
        _stage("compatibility-policy", _command("uv", "run", "python", "scripts/check_compatibility_policy.py")),
        _stage("public-identity", _command("uv", "run", "python", "scripts/check_public_identity.py")),
        _stage(
            "structured-document-contracts",
            _command("uv", "run", "python", "scripts/check_structured_document_contracts.py"),
        ),
        _stage(
            "public-tree-safety", _command("uv", "run", "python", "scripts/check_public_tree_safety.py", "--repo", ".")
        ),
        _stage("workflow-security", _command("uv", "run", "python", "scripts/check_workflow_security.py")),
        _stage("release-workflow-policy", _command("uv", "run", "python", "scripts/release_workflow_policy.py")),
        _stage(
            "supply-chain-policy", _command("uv", "run", "python", "scripts/check_supply_chain_policy.py", "policy")
        ),
        _stage("release-policy", _command("uv", "run", "python", "scripts/check_release.py", "policy")),
    )


def _impact_command(
    quality_base: str,
    *,
    candidate_head: str | None,
    github_output: str | None,
    selection_report: str | None,
    junitxml: str | None,
) -> CommandSpec:
    argv = ["uv", "run", "python", "scripts/run_impact_tests.py", "--base", quality_base]
    if candidate_head is not None:
        argv.extend(("--head", candidate_head))
    if github_output is not None:
        argv.extend(("--github-output", github_output))
    if selection_report is not None:
        argv.extend(("--selection-report", selection_report))
    if junitxml is not None:
        argv.append(f"--junitxml={junitxml}")
    return _command(*argv)


def build_plan(
    tier: Tier,
    *,
    repo: Path,
    quality_base: str | None = None,
    candidate_head: str | None = None,
    workers: int = DEFAULT_WORKERS,
    github_output: str | None = None,
    selection_report: str | None = None,
    junitxml: str | None = None,
) -> QualityPlan:
    """Build one supported plan; callers cannot supply arbitrary child argv."""
    if workers < 1 or workers > 32:
        raise ValueError("workers must be between 1 and 32")
    required_base = tier in {"pr", "impact"}
    base = _validate_revision(quality_base, "quality base", required=required_base)
    head = _validate_revision(candidate_head, "candidate head", required=tier == "impact")
    github_output = report_path(github_output, "GitHub output")
    selection_report = report_path(selection_report, "selection report")
    junitxml = report_path(junitxml, "JUnit report")
    if tier != "impact" and any(value is not None for value in (head, github_output, selection_report, junitxml)):
        raise ValueError("candidate and report arguments are supported only for the impact tier")
    if tier == "full" and base is not None:
        raise ValueError("the full tier does not accept a quality base")

    if tier == "impact":
        if base is None or head is None:
            raise ValueError("impact execution requires base and candidate revisions")
        return QualityPlan(
            tier=tier,
            stages=(
                _stage(
                    "impact-pytest",
                    _impact_command(
                        base,
                        candidate_head=head,
                        github_output=github_output,
                        selection_report=selection_report,
                        junitxml=junitxml,
                    ),
                ),
            ),
            stage_timeout_seconds=PR_STAGE_TIMEOUT_SECONDS,
            quality_base=base,
            candidate_head=head,
            workers=workers,
        )

    common = _common_stages(repo)
    if tier == "pr":
        if base is None:
            raise ValueError("PR execution requires a base revision")
        stages = (
            *common[:4],
            _stage(
                "agent-instruction-surface",
                _command("uv", "run", "python", "scripts/check_agent_instruction_surface.py"),
            ),
            *common[4:],
            _stage(
                "quality-contract", _command("uv", "run", "pytest", "tests/test_quality_contract.py", "-q", "-n", "0")
            ),
            _stage(
                "impact-pytest",
                _impact_command(
                    base,
                    candidate_head=None,
                    github_output=None,
                    selection_report=None,
                    junitxml=None,
                ),
                skip_policy="prose-only",
            ),
        )
        return QualityPlan(
            tier=tier,
            stages=stages,
            stage_timeout_seconds=PR_STAGE_TIMEOUT_SECONDS,
            quality_base=base,
            candidate_head=None,
            workers=workers,
        )

    stages = (
        *common,
        _stage(
            "skill-integrity",
            _command(
                "uv", "run", "python", "scripts/check_skill_integrity.py", "--report", "reports/skill-integrity.json"
            ),
        ),
        _stage(
            "agent-instruction-surface", _command("uv", "run", "python", "scripts/check_agent_instruction_surface.py")
        ),
        _stage(
            "click-params", _command("uv", "run", "python", "scripts/check_click_params.py", "src/fieldkit/commands/")
        ),
        _stage("cli-docs", _command("uv", "run", "python", "scripts/generate_cli_docs.py", "--check")),
        _stage("dependency-map", _command("uv", "run", "python", "scripts/generate_dep_map.py", "--check")),
        _stage("documentation-contract", _command("uv", "run", "python", "scripts/check_documentation_contract.py")),
        _stage("schema-sync", _command("uv", "run", "python", "scripts/check_schema_sync.py")),
        _stage("changelog-fragment", _command("uv", "run", "python", "scripts/check_changelog_fragment.py")),
        _stage("skillsaw", _command("uv", "run", "python", "scripts/check_skillsaw.py")),
        _stage(
            "pytest-coverage",
            _command(
                "uv",
                "run",
                "pytest",
                "tests/",
                "--cov",
                "--cov-report=term-missing",
                "--cov-report=json:coverage.json",
                "-q",
                "-n",
                str(workers),
            ),
        ),
        _stage(
            "gazepy-baseline",
            _command(
                "uv",
                "run",
                "gazepy",
                "crap",
                "src/fieldkit/",
                "--coverprofile",
                "coverage.json",
                "--baseline",
                ".gaze/baseline.json",
            ),
        ),
        _stage(
            "gazepy-ceiling",
            _command(
                "uv",
                "run",
                "gazepy",
                "crap",
                "src/fieldkit/",
                "--coverprofile",
                "coverage.json",
                "--max-crapload",
                "67",
            ),
        ),
        _stage(
            "gazepy-contract-coverage",
            _command("uv", "run", "gazepy", "quality", "src/fieldkit/", "--min-contract-coverage", "50"),
        ),
        _stage(
            "agentready-assess", _command("uv", "run", "python", "scripts/agentready_assess.py", "agentready==2.49.0")
        ),
        _stage("agentready-check", _command("uv", "run", "python", "scripts/check_agentready.py")),
        _stage(
            "behavioral-skill-eval",
            _command(
                "uv",
                "run",
                "fieldkit",
                "skill",
                "eval",
                "--behavioral",
                "--all",
                environment=(("FIELDKIT_NO_LLM", "1"),),
            ),
        ),
        _stage("flag-contract", _command("uv", "run", "python", "scripts/check_flag_contract.py")),
    )
    return QualityPlan(
        tier="full",
        stages=stages,
        stage_timeout_seconds=FULL_STAGE_TIMEOUT_SECONDS,
        quality_base=None,
        candidate_head=None,
        workers=workers,
    )


def plan_sha256(plan: QualityPlan) -> str:
    """Return a stable digest of the complete expanded plan."""
    document = {
        "tier": plan.tier,
        "quality_base": plan.quality_base,
        "candidate_head": plan.candidate_head,
        "workers": plan.workers,
        "stage_timeout_seconds": plan.stage_timeout_seconds,
        "stages": [
            {
                "label": stage.label,
                "skip_policy": stage.skip_policy,
                "commands": [
                    {"argv": list(command.argv), "environment": dict(command.environment)} for command in stage.commands
                ],
            }
            for stage in plan.stages
        ],
    }
    serialized = json.dumps(document, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(serialized).hexdigest()
