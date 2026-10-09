"""Enforce checkout hook import boundaries without adding overlapping Tach roots."""

import ast
import sys
import tomllib
from pathlib import Path

# Preserve the hook dependency policy formerly declared in tach.toml.
HOOK_DEPENDENCIES = frozenset({"fieldkit", "fieldkit.config", "fieldkit.enrich", "fieldkit.errors"})
ROOT = Path(__file__).resolve().parents[1]


def imported_modules(tree: ast.AST) -> list[tuple[int, str]]:
    """Include imported members so `from fieldkit import commands` cannot bypass policy."""
    imports: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.append((node.lineno, node.module))
            imports.extend((node.lineno, f"{node.module}.{alias.name}") for alias in node.names)
    return imports


def check_boundaries(root: Path) -> list[str]:
    """Resolve imports against Tach's most specific module, as the original policy did."""
    config = tomllib.loads((root / "tach.toml").read_text(encoding="utf-8"))
    modules = sorted((module["path"] for module in config["modules"]), key=len, reverse=True)
    violations: list[str] = []
    for directory in (root / "hooks", root / "src"):
        for path in sorted(directory.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for line, imported in imported_modules(tree):
                if directory.name == "src":
                    forbidden = imported == "hooks" or imported.startswith("hooks.")
                else:
                    dependency = next(
                        (module for module in modules if imported == module or imported.startswith(module + ".")),
                        None,
                    )
                    forbidden = dependency is not None and dependency not in HOOK_DEPENDENCIES
                if forbidden:
                    violations.append(f"{path.relative_to(root)}:{line}: forbidden import {imported}")
    return violations


def main() -> int:
    violations = check_boundaries(ROOT)
    for violation in violations:
        print(violation, file=sys.stderr)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
