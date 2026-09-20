#!/usr/bin/env python3
"""check_skill_integrity.py — Deterministic validator for the .opencode/ and fieldkit/skills/ corpus.

Detects schema violations, dead references, DAG cycles, orphaned skills, and stale
numeric constants. No LLM. No network calls.

Usage:
    uv run python scripts/check_skill_integrity.py [--report <path>]

Exit codes:
    0 — no error-severity violations (warnings are acceptable)
    1 — one or more error-severity violations found
"""

from __future__ import annotations

import sys
from pathlib import Path

from skill_integrity import command_refs, corpus, governance, graph, model, references, reporting

REPO_ROOT = Path(__file__).resolve().parent.parent


def parse_args(argv: list[str]) -> str | None:
    """Parse --report <path> from argv. Returns report_path or None.

    Args:
        argv: Command-line arguments (including script name at index 0).

    Returns:
        report_path or None if --report was not supplied.
    """
    report_path: str | None = None
    i = 1
    while i < len(argv):
        if argv[i] == "--report" and i + 1 < len(argv):
            report_path = argv[i + 1]
            i += 2
        else:
            i += 1

    # Detect --report with no value argument
    if "--report" in argv and argv.index("--report") == len(argv) - 1:
        print("ERROR: --report requires a path argument", file=sys.stderr)
        sys.exit(1)

    return report_path


def main(argv: list[str] | None = None, *, context: model.IntegrityContext | None = None) -> None:
    """Main entry point for the skill integrity validator.

    Args:
        argv: Command-line arguments (defaults to sys.argv if None).
    """
    if argv is None:
        argv = sys.argv
    active_context = context or model.IntegrityContext.from_root(REPO_ROOT)

    report_path = parse_args(argv)

    # Auto-create the report directory if needed
    if report_path is not None:
        parent = Path(report_path).parent
        parent.mkdir(parents=True, exist_ok=True)

    registry = corpus.build_registry(active_context)
    violations: list[model.Violation] = []
    violations += corpus.validate_schemas(active_context, registry)
    violations += references.check_dead_refs(active_context, registry)
    violations += graph.check_dag(active_context, registry)
    violations += governance.check_constants(active_context, registry)
    violations += governance.check_noun_governance(active_context, registry)
    violations += command_refs.check_command_refs(active_context, registry)

    try:
        reporting.emit_report(violations, registry, report_path)
    except OSError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    has_errors = any(v.severity == "error" for v in violations)
    sys.exit(1 if has_errors else 0)


if __name__ == "__main__":
    main(sys.argv)
