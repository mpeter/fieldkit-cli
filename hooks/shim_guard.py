#!/usr/bin/env python3
"""Pre-commit hook: block backward-compatibility shim resurrections.

A shim is a .py file whose entire substantive content is re-export
aliases — either star-imports (`from X import *`) or explicit re-export
chains (`from X import Y as Y`) — with no real logic: no function
definitions, no class definitions, no constants, no executable statements
beyond the imports themselves.

Shims are the structural signature of an incomplete refactor. R25 states:
"A refactor is not done until the origin is deleted." A shim at the old
path is evidence the refactor was not finished.

New files are classified structurally, so the guard catches future whole-file
shims regardless of their names. Modified files are checked for redundant
aliases of the finite error-symbol surfaces previously removed under R25.

Exemptions:
  - Files named __init__.py — legitimate package aggregators are
    structurally similar to shims but serve a valid purpose: they
    always coexist with sibling modules inside a package directory.
  - Files with a `# shim-guard: ignore` comment anywhere in the source —
    for intentional re-export modules that are not backward-compat bridges
    (document why in the comment).

Exit 1 = block commit. Exit 0 = allow.
"""

import ast
import subprocess
import sys
from pathlib import Path


def _get_staged_python_files() -> list[Path]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=A"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        print(f"shim_guard.py: git diff failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    paths = []
    for line in result.stdout.splitlines():
        p = Path(line.strip())
        if p.suffix == ".py" and p.name != "__init__.py":
            paths.append(p)
    return paths


_REEXPORT_SENTINEL_NAMES: frozenset[str] = frozenset(
    {
        "FrontmatterStalenessError",
        "GmailAuthError",
        "LLMError",
        "LLMErrorCategory",
    }
)


def _get_modified_python_files() -> list[Path]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=MR"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        print(f"shim_guard.py: git diff failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    return [
        path
        for line in result.stdout.splitlines()
        if (path := Path(line.strip())).suffix == ".py" and path.name != "__init__.py"
    ]


def _read_staged_source(path: Path) -> str:
    result = subprocess.run(
        ["git", "show", f":{path.as_posix()}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        print(f"shim_guard.py: cannot read staged {path}: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    return result.stdout


def _reexport_alias_violations(source: str) -> list[tuple[int, str]]:
    """Return line and symbol pairs for protected redundant import aliases."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    return [
        (node.lineno, alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.asname == alias.name and alias.name in _REEXPORT_SENTINEL_NAMES
    ]


def _is_shim(source: str) -> bool:
    """Return True if the file's substantive content is only re-export aliases.

    A file is a shim if every top-level node is one of:
      - a module docstring (Expr containing a Constant string)
      - a `from __future__ import annotations` import
      - a `from X import *` import (ImportFrom with names=[alias('*')])
      - a `from X import Y as Y` import where every alias has asname == name
        (the explicit re-export pattern that satisfies mypy --strict)
      - an `__all__ = [...]` assignment
      - a comment (not visible in AST — handled implicitly)

    Everything else (function defs, class defs, assignments other than
    __all__, non-re-export imports, expressions) makes the file non-shim.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False

    for node in ast.iter_child_nodes(tree):
        # Module docstring
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue

        # `from __future__ import annotations`
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue

        # `from X import *`
        if isinstance(node, ast.ImportFrom) and node.names and node.names[0].name == "*":
            continue

        # `from X import Y as Y` — every alias must be an explicit re-export
        # (asname equals name), which is the mypy-compatible re-export pattern.
        if isinstance(node, ast.ImportFrom):
            if all(alias.asname is not None and alias.asname == alias.name for alias in node.names):
                continue
            # Mixed import: some aliases without asname, or asname != name —
            # this is real import logic, not a pure re-export.
            return False

        # `__all__ = [...]` assignment
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "__all__"
        ):
            continue

        # Anything else: function, class, non-__all__ assignment, etc.
        return False

    # Every node passed — but a completely empty file (or docstring-only) is
    # not a shim, it's just empty. Require at least one import to qualify.
    import_nodes = [
        n
        for n in ast.iter_child_nodes(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
        and not (isinstance(n, ast.ImportFrom) and n.module == "__future__")
    ]
    return len(import_nodes) > 0


def main() -> int:
    staged = _get_staged_python_files()
    offenders: list[tuple[Path, str]] = []
    reexport_offenders: list[tuple[Path, int, str]] = []

    for path in staged:
        source = _read_staged_source(path)

        # Honour explicit opt-out
        if "# shim-guard: ignore" in source:
            continue

        if _is_shim(source):
            # Determine what it re-exports from, for a helpful message
            try:
                tree = ast.parse(source)
                sources = {
                    node.module
                    for node in ast.iter_child_nodes(tree)
                    if isinstance(node, ast.ImportFrom) and node.module and node.module != "__future__"
                }
                from_desc = ", ".join(sorted(s for s in sources if s))
            except SyntaxError:
                from_desc = "unknown"

            offenders.append((path, from_desc))

    for path in _get_modified_python_files():
        source = _read_staged_source(path)
        if "# shim-guard: ignore" in source:
            continue

        reexport_offenders.extend((path, lineno, symbol) for lineno, symbol in _reexport_alias_violations(source))

    if not offenders and not reexport_offenders:
        return 0

    if offenders:
        print("shim-guard: New backward-compatibility shim file(s) detected.", file=sys.stderr)
        print("  R25: A refactor is not done until the origin is deleted.", file=sys.stderr)
        print("  Update consumers to the new import path, then delete the old file.", file=sys.stderr)
        print("", file=sys.stderr)
        for path, from_desc in offenders:
            print(f"  blocked: {path}", file=sys.stderr)
            if from_desc:
                print(f"           (re-exports from: {from_desc})", file=sys.stderr)

    if reexport_offenders:
        if offenders:
            print("", file=sys.stderr)
        print("shim-guard: Retired error re-export(s) detected in modified file(s).", file=sys.stderr)
        print("  R25: deleted error surfaces must not be restored outside package aggregators.", file=sys.stderr)
        print("", file=sys.stderr)
        for path, lineno, symbol in reexport_offenders:
            print(f"  blocked: {path}:{lineno} ({symbol} as {symbol})", file=sys.stderr)

    print("", file=sys.stderr)
    print("  To override for a legitimate re-export module, add a", file=sys.stderr)
    print("  `# shim-guard: ignore` comment and document why.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
