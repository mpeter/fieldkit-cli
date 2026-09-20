"""Constant and noun-governance validation for the skill-integrity corpus."""

import re
from collections.abc import Iterator
from pathlib import Path

from skill_integrity import model

# C001/C002 declaration patterns
_C001_PATTERNS = [
    re.compile(r"fail_under\s*=\s*(\d+)"),
    re.compile(r"--cov-fail-under[= ](\d+)"),
]
_C002_PATTERN = re.compile(r"--max-crapload[= ](\d+)")


def _md_files_for_constants(context: model.IntegrityContext, registry: model.Registry) -> Iterator[Path]:
    """Yield .md files to scan for constant declarations: skills + packs."""
    for path in registry.project_skills.values():
        yield path
    for path in registry.fieldkit_skills.values():
        yield path
    if context.packs_dir.is_dir():
        for f in sorted(context.packs_dir.iterdir()):
            if f.is_file() and f.suffix == ".md":
                yield f


def check_constants(context: model.IntegrityContext, registry: model.Registry) -> list[model.Violation]:
    """Detect stale constant declarations in skill and pack files (C001, C002).

    Args:
        registry: Populated corpus registry.

    Returns:
        List of stale-constant violations.
    """
    violations: list[model.Violation] = []

    for md_path in _md_files_for_constants(context, registry):
        try:
            text = md_path.read_text(encoding="utf-8")
        except OSError:
            continue

        rel_path = str(md_path.relative_to(context.repo_root))
        lines_list = text.splitlines()

        for i, line in enumerate(lines_list, start=1):
            # C001 — coverage floor declarations
            for pattern in _C001_PATTERNS:
                for m in pattern.finditer(line):
                    declared = int(m.group(1))
                    if declared != registry.coverage_floor:
                        violations.append(
                            model.Violation(
                                code="C001",
                                file=rel_path,
                                line=i,
                                message=(
                                    f"coverage floor declaration '{declared}' != "
                                    f"pyproject.toml value '{registry.coverage_floor}'"
                                ),
                            )
                        )

            # C002 — CRAP threshold declarations
            for m in _C002_PATTERN.finditer(line):
                declared = int(m.group(1))
                if declared != registry.crap_threshold:
                    violations.append(
                        model.Violation(
                            code="C002",
                            file=rel_path,
                            line=i,
                            message=(
                                f"CRAP threshold declaration '{declared}' != Makefile value '{registry.crap_threshold}'"
                            ),
                        )
                    )

    return violations


def check_noun_governance(context: model.IntegrityContext, registry: model.Registry) -> list[model.Violation]:
    """Detect fieldkit skill dirs not listed in the context's skill roots (N001).

    Args:
        registry: Populated corpus registry.

    Returns:
        List of noun-governance violations.
    """
    violations: list[model.Violation] = []

    for skill_name, skill_path in sorted(registry.fieldkit_skills.items()):
        if skill_name not in context.skill_roots:
            violations.append(
                model.Violation(
                    code="N001",
                    file=str(skill_path.relative_to(context.repo_root)),
                    line=None,
                    message=(
                        f"skill '{skill_name}' is not in SKILL_ROOTS — "
                        f"orphan top-level skills are not allowed by the D1 governance mandate"
                    ),
                )
            )

    return violations
