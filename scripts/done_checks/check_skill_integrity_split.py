#!/usr/bin/env python3
"""Verify the accepted implementation change skill-integrity module boundaries."""

import ast
from pathlib import Path

_REQUIRED_DEFINITIONS = {
    "model.py": frozenset({"IntegrityContext", "Violation", "Registry"}),
    "corpus.py": frozenset(
        {
            "_frontmatter_string",
            "_parse_frontmatter",
            "_parse_jsonc",
            "_read_coverage_floor",
            "_read_crap_threshold",
            "_skill_dirs",
            "build_registry",
            "validate_schemas",
        }
    ),
    "references.py": frozenset(
        {
            "_all_md_files",
            "_all_skill_names",
            "_split_body_and_related_skills",
            "_strip_fenced_code_blocks",
            "check_dead_refs",
        }
    ),
    "graph.py": frozenset({"_extract_related_skill_names", "_extract_skills_use_names", "check_dag"}),
    "governance.py": frozenset({"_md_files_for_constants", "check_constants", "check_noun_governance"}),
    "reporting.py": frozenset({"emit_report"}),
}
_MOVED_DEFINITIONS = frozenset().union(*_REQUIRED_DEFINITIONS.values())


def _top_level_definitions(tree: ast.Module) -> set[str]:
    return {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}


def _directly_imported_names(tree: ast.Module) -> set[str]:
    return {alias.name for node in tree.body if isinstance(node, ast.ImportFrom) for alias in node.names}


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    executable = repo_root / "scripts" / "check_skill_integrity.py"
    package = repo_root / "scripts" / "skill_integrity"
    failures: list[str] = []

    executable_lines = _line_count(executable)
    if executable_lines >= 500:
        failures.append(f"{executable.relative_to(repo_root)}: {executable_lines} lines (must be below 500)")

    if not package.is_dir():
        failures.append("scripts/skill_integrity: package is missing")
    else:
        definitions_by_module: dict[str, set[str]] = {}
        for module in sorted(package.glob("*.py")):
            module_lines = _line_count(module)
            if module_lines > 400:
                failures.append(f"{module.relative_to(repo_root)}: {module_lines} lines (maximum 400)")
            definitions_by_module[module.name] = _top_level_definitions(
                ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
            )

        for module_name, required in sorted(_REQUIRED_DEFINITIONS.items()):
            module = package / module_name
            if not module.is_file():
                failures.append(f"{module.relative_to(repo_root)}: required module is missing")
                continue
            definitions = definitions_by_module[module_name]
            missing = sorted(required - definitions)
            if missing:
                failures.append(f"{module.relative_to(repo_root)}: required definitions missing: {', '.join(missing)}")

        for module_name, required in sorted(_REQUIRED_DEFINITIONS.items()):
            for definition in sorted(required):
                owners = sorted(
                    name for name, definitions in definitions_by_module.items() if definition in definitions
                )
                if owners != [module_name]:
                    failures.append(
                        f"scripts/skill_integrity/{definition}: expected owner {module_name}; found {', '.join(owners) or 'none'}"
                    )

    tree = ast.parse(executable.read_text(encoding="utf-8"), filename=str(executable))
    remaining = sorted(_top_level_definitions(tree) & _MOVED_DEFINITIONS)
    if remaining:
        failures.append(f"scripts/check_skill_integrity.py: moved definitions remain: {', '.join(remaining)}")
    reexports = sorted(_directly_imported_names(tree) & _MOVED_DEFINITIONS)
    if reexports:
        failures.append(
            "scripts/check_skill_integrity.py: moved definitions imported directly instead of module-qualified: "
            + ", ".join(reexports)
        )

    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print("skill-integrity split: shape checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
