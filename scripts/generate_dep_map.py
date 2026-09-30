#!/usr/bin/env python3
"""Generate the dependency reference from Python imports, CLI registration, and Tach policy.

The reference describes static imports, not runtime reachability or a passing
architecture gate. Invalid or unreadable inputs stop generation.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import os
import stat
import sys
import tomllib
from collections.abc import Iterator
from importlib.util import resolve_name
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = REPO_ROOT / "docs" / "dependency-map.md"
MAX_SOURCE_BYTES = 1024 * 1024

sys.path.insert(0, str(REPO_ROOT / "src"))
from fieldkit.__main__ import _COMMANDS  # noqa: E402
from fieldkit.provenance import derived_doc_banner, derived_doc_marker  # noqa: E402

_MARKER = derived_doc_marker(
    caste="derived",
    derived_from=["src/fieldkit/", "hooks/", "src/fieldkit/__main__.py", "tach.toml"],
    generated_by="scripts/generate_dep_map.py",
)


def _scan_error(error: OSError) -> None:
    raise error


def _read_source(path: Path) -> str:
    """Read one bounded UTF-8 regular file without following its final symlink."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("source input must be a regular file")
        content = stream.read(MAX_SOURCE_BYTES + 1)
    if len(content) > MAX_SOURCE_BYTES:
        raise ValueError("source input exceeds the size limit")
    return content.decode("utf-8")


def _python_files(root: Path) -> Iterator[Path]:
    """Enumerate source files without silently skipping inaccessible directories."""
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"source root must be a regular directory: {root.name}")
    for directory, children, files in os.walk(root, onerror=_scan_error):
        children.sort()
        for name in [*children, *files]:
            if (Path(directory) / name).is_symlink():
                raise ValueError("source inventory cannot follow symlinks")
        for name in sorted(files):
            if name.endswith(".py"):
                path = Path(directory) / name
                if not path.is_file():
                    raise ValueError("source input must be a regular file")
                yield path


def _source_modules() -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for root in (REPO_ROOT / "src" / "fieldkit", REPO_ROOT / "hooks"):
        for path in _python_files(root):
            relative = path.relative_to(root.parent).with_suffix("")
            parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
            name = ".".join(parts)
            if name in modules:
                raise ValueError(f"ambiguous Python module: {name}")
            modules[name] = path
    return modules


def _known_modules(modules: dict[str, Path] | dict[str, set[str]]) -> set[str]:
    """Include namespace packages as well as concrete Python modules."""
    return {
        ".".join(parts[:length])
        for name in modules
        for parts in [name.split(".")]
        for length in range(1, len(parts) + 1)
    }


def _fieldkit_imports(tree: ast.Module, package: str, known: set[str]) -> set[str]:
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = resolve_name("." * node.level + base, package)
            if base == "fieldkit":
                imports.update(
                    f"fieldkit.{alias.name}" if f"fieldkit.{alias.name}" in known else base for alias in node.names
                )
            else:
                imports.add(base)
    result = {name for name in imports if name == "fieldkit" or name.startswith("fieldkit.")}
    missing = result - known
    if missing:
        raise ValueError(f"unresolved fieldkit imports: {', '.join(sorted(missing))}")
    return result


def read_imports() -> dict[str, set[str]]:
    """Read every application and hook Python file with strict syntax and encoding."""
    modules = _source_modules()
    known = _known_modules(modules)
    imports: dict[str, set[str]] = {}
    for name, path in sorted(modules.items()):
        tree = ast.parse(_read_source(path), filename=str(path.relative_to(REPO_ROOT)))
        package = name if path.name == "__init__.py" else name.rpartition(".")[0]
        imports[name] = _fieldkit_imports(tree, package, known)
    return imports


def _area(module: str) -> str:
    return ".".join(module.split(".")[:2]) if module.startswith("fieldkit.") else module.split(".")[0]


def _declared_boundaries(known: set[str]) -> dict[str, list[str]]:
    policy = tomllib.loads(_read_source(REPO_ROOT / "tach.toml"))
    entries = policy.get("modules")
    if not isinstance(entries, list):
        raise ValueError("Tach policy must declare a modules list")
    boundaries: dict[str, list[str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Tach module entry must be an object")
        name, dependencies = entry.get("path"), entry.get("depends_on")
        if not isinstance(name, str) or name not in known or name in boundaries:
            raise ValueError("Tach module path is unknown or duplicated")
        if not isinstance(dependencies, list) or not all(
            isinstance(dependency, str) and dependency in known for dependency in dependencies
        ):
            raise ValueError(f"Tach dependencies are invalid for {name}")
        boundaries[name] = sorted(set(dependencies))
    return boundaries


def _labels(names: set[str] | list[str], *, empty: str) -> str:
    return ", ".join(f"`{name}`" for name in sorted(names)) or empty


def generate() -> str:
    """Render the same structural reference for the same source content."""
    imports = read_imports()
    boundaries = _declared_boundaries(_known_modules(imports))
    lines = [
        _MARKER.rstrip("\n"),
        "",
        "# fieldkit dependency map",
        "",
        derived_doc_banner(),
        "",
        "This reference is derived from Python source, the public command registry, and",
        "Tach configuration. Source files are parsed for graph discovery, not imported.",
        "This reference does not claim that a quality gate passed.",
        "The contributor gate verifies the declared architecture boundaries.",
        "",
        "## Public command adapters",
        "",
        "The registry determines public command names. Import areas below include direct",
        "imports from the adapter package and its descendants; they are not runtime traces.",
        "",
        "| Command | Registered module | Imported fieldkit areas |",
        "| --- | --- | --- |",
    ]
    for command, (_, module) in sorted(_COMMANDS.items()):
        if module not in imports:
            raise ValueError(f"registered command module is absent: {module}")
        package = module.rpartition(".")[0]
        areas = {
            _area(target)
            for source, targets in imports.items()
            if source == package or source.startswith(package + ".")
            for target in targets
        }
        lines.append(f"| `fieldkit {command}` | `{module}` | {_labels(areas, empty='None observed')} |")
    lines.extend(
        [
            "",
            "## Observed cross-area imports",
            "",
            "Each area is an immediate fieldkit package/module or the hooks directory.",
            "All Python import statements are inspected, including conditional and type-checking",
            "imports. Same-area imports are omitted; dynamic imports are not inferred.",
            "An empty row means no cross-area fieldkit import was observed, not that runtime",
            "coupling is impossible.",
            "",
            "| Source area | Imported fieldkit areas |",
            "| --- | --- |",
        ]
    )
    areas_by_source: dict[str, set[str]] = {}
    for source, targets in imports.items():
        area = _area(source)
        areas_by_source.setdefault(area, set()).update(_area(target) for target in targets if _area(target) != area)
    for area, targets in sorted(areas_by_source.items()):
        labels = _labels(targets, empty="None observed")
        lines.append(f"| `{area}` | {labels} |")
    lines.extend(
        [
            "",
            "## Declared architecture boundaries",
            "",
            "These are the module paths and allowed dependencies declared in `tach.toml`.",
            "They describe policy, not observed imports or the result of executing Tach.",
            "See the [contribution guide](../CONTRIBUTING.md) for the enforced verification path.",
            "",
            "| Declared module | Allowed dependencies |",
            "| --- | --- |",
        ]
    )
    for name, dependencies in sorted(boundaries.items()):
        lines.append(f"| `{name}` | {_labels(dependencies, empty='None declared')} |")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    """Generate the reference or verify its content without modifying it."""
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--check", action="store_true")
    check_mode = parser.parse_args(argv).check
    if check_mode and not OUTPUT.exists():
        print("ERROR: docs/dependency-map.md does not exist — run 'make docs'", file=sys.stderr)
        return 1
    content = generate()
    if check_mode:
        existing = OUTPUT.read_text(encoding="utf-8")
        if existing != content:
            diff = list(
                difflib.unified_diff(
                    existing.splitlines(), content.splitlines(), fromfile="committed", tofile="generated", lineterm=""
                )
            )
            print("\n".join(diff[:40]), file=sys.stderr)
            print("ERROR: docs/dependency-map.md is stale — run 'make docs'", file=sys.stderr)
            return 1
        print("docs/dependency-map.md is up to date ✓", file=sys.stderr)
    else:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
