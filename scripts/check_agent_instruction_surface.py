#!/usr/bin/env python3
"""Validate the public repository's canonical agent-instruction surface."""

from __future__ import annotations

import argparse
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING or __package__:
    from scripts import export_public_tree, quality_plan
else:
    import export_public_tree
    import quality_plan

_POLICY_PATH = Path("docs/release-readiness/public-tree-policy.json")
_PUBLIC_GATE = "scripts/check_agent_instruction_surface.py"
_PUBLIC_TEST = "tests/test_agent_instruction_surface.py"
_QUALITY_RUNNER = "scripts/quality_gate.py"
_QUALITY_SOURCE = "scripts/quality_source.py"
_QUALITY_PLAN = "scripts/quality_plan.py"
_QUALITY_EXECUTION = "scripts/quality_execution.py"
_QUALITY_RECEIPT = "scripts/quality_receipt.py"
_QUALITY_TEST = "tests/test_quality_gate.py"
_QUALITY_SCHEMA = "docs/release-readiness/quality-gate-receipt.schema.json"
_RETIRED_PATHS = (
    "scripts/sync_claude_dir.py",
    "tests/test_sync_claude_dir.py",
    "openspec/specs/claude-skills-symlink-integrity/spec.md",
    "scripts/quality_stage.py",
)
_RETIRED_TOKENS = ("sync_claude_dir.py", "sync-claude", "claude-sync", "quality_stage.py")
_PUBLIC_INSTRUCTION_PATHS = (
    "AGENTS.md",
    "src/fieldkit/ingest/AGENTS.md",
    "src/fieldkit/llm/AGENTS.md",
    "src/fieldkit/pursuit/AGENTS.md",
    "src/fieldkit/sf/AGENTS.md",
    "src/fieldkit/watch/AGENTS.md",
)
_IGNORED_SCAN_DIRECTORIES = frozenset(
    {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "__pycache__", "build"}
)
_CANONICAL_GATE_ARGV = ("uv", "run", "python", _PUBLIC_GATE)


@dataclass(frozen=True, order=True)
class Finding:
    """One deterministic agent-surface contract violation."""

    code: str
    path: str


def _classification(
    policy: export_public_tree.Policy,
    path: str,
    mode: str = "100644",
) -> tuple[str, export_public_tree.TreeEntry]:
    included, excluded = export_public_tree._classify(((path, mode, "0" * 40),), policy)
    if len(included) == 1 and not excluded:
        return "include", included[0]
    if len(excluded) == 1 and not included:
        return "exclude", excluded[0]
    raise export_public_tree.ExportError(f"agent surface path has no unique classification: {path}")


def _read(repo: Path, relative: str) -> str:
    try:
        return (repo / relative).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError(f"cannot read required agent-surface file: {relative}") from error


def _discovered_instruction_paths(repo: Path) -> tuple[str, ...]:
    discovered: list[str] = []
    for root, directories, files in os.walk(repo):
        directories[:] = sorted(name for name in directories if name not in _IGNORED_SCAN_DIRECTORIES)
        if "AGENTS.md" in files:
            discovered.append((Path(root) / "AGENTS.md").relative_to(repo).as_posix())
    return tuple(sorted(discovered))


def _quality_plan_contains_gate(repo: Path, tier: quality_plan.Tier) -> bool:
    plan = quality_plan.build_plan(tier, repo=repo, quality_base="0" * 40 if tier == "pr" else None)
    matches = [stage for stage in plan.stages if stage.label == "agent-instruction-surface"]
    return (
        len(matches) == 1
        and matches[0].skip_policy == "never"
        and len(matches[0].commands) == 1
        and matches[0].commands[0].argv == _CANONICAL_GATE_ARGV
    )


def validate(repo: Path) -> tuple[Finding, ...]:
    """Return public instruction-surface findings without changing the repository."""
    policy = export_public_tree._load_policy(repo / _POLICY_PATH)
    findings: list[Finding] = []
    required_patterns = {
        "AGENTS.md": ("include", "contribution"),
        "CLAUDE.md": ("exclude", "local_tool_state"),
        ".claude/**": ("exclude", "local_tool_state"),
        ".opencode/**": ("exclude", "local_tool_state"),
        _PUBLIC_GATE: ("include", "release_infrastructure"),
        _PUBLIC_TEST: ("include", "test"),
        _QUALITY_RUNNER: ("include", "release_infrastructure"),
        _QUALITY_SOURCE: ("include", "release_infrastructure"),
        _QUALITY_PLAN: ("include", "release_infrastructure"),
        _QUALITY_EXECUTION: ("include", "release_infrastructure"),
        _QUALITY_RECEIPT: ("include", "release_infrastructure"),
        _QUALITY_TEST: ("include", "test"),
        _QUALITY_SCHEMA: ("include", "release_infrastructure"),
    }
    declarations: dict[str, list[tuple[str, str]]] = {pattern: [] for pattern in required_patterns}
    for rule in policy.rules:
        for pattern in rule.patterns:
            if pattern in declarations:
                declarations[pattern].append((rule.action, rule.category))
    for pattern, expected_declaration in required_patterns.items():
        observed = declarations[pattern]
        if len(observed) != 1:
            findings.append(Finding("AIS001", pattern))
        elif observed[0] != expected_declaration:
            findings.append(Finding("AIS002", pattern))

    expected = {
        "AGENTS.md": ("include", "contribution", "100644"),
        **{path: ("include", "product", "100644") for path in _PUBLIC_INSTRUCTION_PATHS if path != "AGENTS.md"},
        "CLAUDE.md": ("exclude", "local_tool_state", "120000"),
        ".claude/agents/example.md": ("exclude", "local_tool_state", "100644"),
        ".opencode/commands/example.md": ("exclude", "local_tool_state", "100644"),
        _PUBLIC_GATE: ("include", "release_infrastructure", "100755"),
        _PUBLIC_TEST: ("include", "test", "100644"),
        _QUALITY_RUNNER: ("include", "release_infrastructure", "100644"),
        _QUALITY_SOURCE: ("include", "release_infrastructure", "100644"),
        _QUALITY_PLAN: ("include", "release_infrastructure", "100644"),
        _QUALITY_EXECUTION: ("include", "release_infrastructure", "100644"),
        _QUALITY_RECEIPT: ("include", "release_infrastructure", "100644"),
        _QUALITY_TEST: ("include", "test", "100644"),
        _QUALITY_SCHEMA: ("include", "release_infrastructure", "100644"),
    }
    for path, (action, category, mode) in expected.items():
        try:
            observed_action, entry = _classification(policy, path, mode)
        except export_public_tree.ExportError:
            findings.append(Finding("AIS001", path))
            continue
        if (observed_action, entry.category) != (action, category):
            findings.append(Finding("AIS002", path))

    for path in (
        *_PUBLIC_INSTRUCTION_PATHS,
        _PUBLIC_GATE,
        _PUBLIC_TEST,
        _QUALITY_RUNNER,
        _QUALITY_SOURCE,
        _QUALITY_PLAN,
        _QUALITY_EXECUTION,
        _QUALITY_RECEIPT,
        _QUALITY_TEST,
        _QUALITY_SCHEMA,
    ):
        candidate = repo / path
        if not candidate.is_file() or candidate.is_symlink():
            findings.append(Finding("AIS008", path))
    gate = repo / _PUBLIC_GATE
    if gate.is_file() and not gate.is_symlink() and not stat.S_IMODE(gate.stat().st_mode) & stat.S_IXUSR:
        findings.append(Finding("AIS008", _PUBLIC_GATE))

    for path in _RETIRED_PATHS:
        if (repo / path).exists() or (repo / path).is_symlink():
            findings.append(Finding("AIS003", path))

    for path in _discovered_instruction_paths(repo):
        try:
            action, _entry = _classification(policy, path)
        except export_public_tree.ExportError:
            findings.append(Finding("AIS001", path))
            continue
        if action == "include" and path not in _PUBLIC_INSTRUCTION_PATHS:
            findings.append(Finding("AIS009", path))

    try:
        if not all(_quality_plan_contains_gate(repo, tier) for tier in ("pr", "full")):
            findings.append(Finding("AIS004", _QUALITY_RUNNER))
    except (OSError, ValueError):
        findings.append(Finding("AIS004", _QUALITY_RUNNER))
    makefile = _read(repo, "Makefile")
    if any(token in makefile for token in _RETIRED_TOKENS):
        findings.append(Finding("AIS005", "Makefile"))

    reviewed_surfaces = (
        "hooks/post_commit.py",
        ".pre-commit-config.yaml",
        "tests/test_quality_contract.py",
        "openspec/specs/post-commit-worktree-install/spec.md",
        "openspec/specs/markdown-link-integrity/spec.md",
    )
    for path in reviewed_surfaces:
        content = _read(repo, path)
        if any(token in content for token in (*_RETIRED_TOKENS, ".claude/", ".opencode/")):
            findings.append(Finding("AIS006", path))

    policy_text = _read(repo, _POLICY_PATH.as_posix())
    if any(path in policy_text for path in _RETIRED_PATHS):
        findings.append(Finding("AIS007", _POLICY_PATH.as_posix()))
    return tuple(sorted(set(findings)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path())
    args = parser.parse_args(argv)
    try:
        findings = validate(args.repo.resolve(strict=True))
    except (OSError, ValueError, export_public_tree.ExportError):
        print("Agent instruction surface: ERROR: validation could not complete", file=sys.stderr)
        return 2
    if findings:
        print("Agent instruction surface: FAIL", file=sys.stderr)
        for finding in findings:
            print(f"{finding.code}: {finding.path}", file=sys.stderr)
        return 1
    print("Agent instruction surface: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
