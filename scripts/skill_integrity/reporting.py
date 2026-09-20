"""Human and JSON reporting for skill-integrity validation."""

import json
from datetime import UTC, datetime
from pathlib import Path

from skill_integrity import model


def emit_report(
    violations: list[model.Violation],
    registry: model.Registry,
    report_path: str | None,
) -> None:
    """Print violations to stdout and optionally write JSON report.

    Args:
        violations: All violations collected from every check layer.
        registry: Populated corpus registry (used for scanned counts).
        report_path: Optional filesystem path to write a JSON report. If None,
            no file is written.

    Returns:
        None.

    Raises:
        OSError: If report_path cannot be written.
    """
    errors = [v for v in violations if v.severity == "error"]
    warnings = [v for v in violations if v.severity == "warning"]

    n_project = len(registry.project_skills)
    n_fieldkit = len(registry.fieldkit_skills)
    n_agents = len(registry.agents)
    n_commands = len(registry.commands)

    print("Checking skill integrity across 2 namespaces...")
    print(
        f"  Scanned: {n_project} project skills, {n_fieldkit} fieldkit skills, {n_agents} agents, {n_commands} commands"
    )
    print()

    if errors:
        print(f"ERRORS ({len(errors)}):")
        for v in errors:
            loc = f"{v.file}:{v.line}" if v.line is not None else v.file
            print(f"  {v.code:<6}{loc:<55}  {v.message}")
        print()

    if warnings:
        print(f"WARNINGS ({len(warnings)}):")
        for v in warnings:
            loc = f"{v.file}:{v.line}" if v.line is not None else v.file
            print(f"  {v.code:<6}{loc:<55}  {v.message}")
        print()

    n_err = len(errors)
    n_warn = len(warnings)
    print(f"Skill integrity: {n_err} error{'s' if n_err != 1 else ''}, {n_warn} warning{'s' if n_warn != 1 else ''}")

    if n_err > 0:
        print("Run /check-skill-integrity to auto-fix errors where possible.")

    if report_path is not None:
        report: dict[str, object] = {
            "generated": datetime.now(UTC).isoformat(),
            "scanned": {
                "project_skills": n_project,
                "fieldkit_skills": n_fieldkit,
                "agents": n_agents,
                "commands": n_commands,
            },
            "summary": {"errors": n_err, "warnings": n_warn},
            "violations": [v.to_dict() for v in violations],
        }
        try:
            Path(report_path).write_text(json.dumps(report, indent=2), encoding="utf-8")
        except OSError as e:
            raise OSError(f"could not write report to '{report_path}': {e}") from e
