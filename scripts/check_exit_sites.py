"""Ratchet the process-exit sites that bypass the CLI exit boundary.

Only ``cli_exit.cli_main()`` and the ``__main__.main()`` dispatcher end the
process. Command adapters signal failure by raising a typed exception that
``handle_cli_exception()`` maps. Older adapters still raise ``SystemExit`` or
call ``ctx.exit()`` directly; this check records how many each function holds
in ``.exit-sites-baseline.json`` and fails when:

- domain code outside ``commands/`` exits at all,
- a function gains a site, or a function without a baseline entry adds one, or
- a function has fewer sites than its baseline, so the baseline is lowered with
  the migration that removed them and can never be spent on a new site.

The body of an exact ``if __name__ == "__main__":`` guard is a separate process entry
point and is not counted.

Usage:
    uv run python scripts/check_exit_sites.py                   # check
    uv run python scripts/check_exit_sites.py --write-baseline  # after removing or moving sites

``--write-baseline`` refuses to record a file whose total grew. A site that
moved to another function within its file is recorded, and the move is visible
in the baseline diff for review.

Exit codes:
    0 — every function matches its baseline (pass)
    1 — a new, domain, moved, or already-removed site was found (fail)
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections.abc import Iterator
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOT = _ROOT / "src" / "fieldkit"
BASELINE_PATH = _ROOT / ".exit-sites-baseline.json"

# The two process boundaries; every other function raises or returns instead.
BOUNDARY_SCOPES = frozenset({("cli_exit.py", "cli_main"), ("__main__.py", "main")})
COMMANDS_PREFIX = "commands/"
MODULE_SCOPE = "<module>"
_EXIT_EXCEPTIONS = frozenset({"SystemExit", "Exit"})
_EXIT_CALLS = frozenset({"exit", "_exit"})
_EXIT_BUILTINS = frozenset({"exit", "quit"})
# Modules whose exit functions and exceptions can be imported under another name.
_EXIT_MODULES = frozenset({"sys", "os", "builtins", "click", "click.exceptions"})
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

Sites = dict[str, dict[str, list[int]]]
Baseline = dict[str, dict[str, int]]


def _is_main_guard(node: ast.AST) -> bool:
    """Match exactly ``if __name__ == "__main__":``."""
    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return False
    test = node.test
    return (
        isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


def _callee_name(node: ast.expr) -> str | None:
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _exit_aliases(tree: ast.AST) -> tuple[frozenset[str], frozenset[str]]:
    """Return local names bound to exit exceptions and exit functions by ``from ... import``."""
    exceptions: set[str] = set()
    calls: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module not in _EXIT_MODULES:
            continue
        for alias in node.names:
            if alias.name in _EXIT_EXCEPTIONS:
                exceptions.add(alias.asname or alias.name)
            elif alias.name in _EXIT_CALLS | _EXIT_BUILTINS:
                calls.add(alias.asname or alias.name)
    return frozenset(exceptions), frozenset(calls)


def _is_exit_site(node: ast.AST, exceptions: frozenset[str], calls: frozenset[str]) -> bool:
    if isinstance(node, ast.Raise) and node.exc is not None:
        return _callee_name(node.exc) in _EXIT_EXCEPTIONS | exceptions
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Attribute):
        return node.func.attr in _EXIT_CALLS
    return isinstance(node.func, ast.Name) and node.func.id in _EXIT_BUILTINS | calls


def exit_sites(source: str) -> dict[str, list[int]]:
    """Map each enclosing function's qualified name to its exit-site lines."""

    tree = ast.parse(source)
    exceptions, calls = _exit_aliases(tree)

    def walk_nodes(nodes: list[ast.stmt], scope: str) -> Iterator[tuple[str, int]]:
        for node in nodes:
            yield from visit(node, scope)

    def visit(node: ast.AST, scope: str) -> Iterator[tuple[str, int]]:
        if isinstance(node, ast.If) and _is_main_guard(node):
            # Only the guarded body is a separate entry point; ``else`` runs on import.
            yield from walk_nodes(node.orelse, scope)
            return
        child_scope = scope
        if isinstance(node, _SCOPES):
            child_scope = node.name if scope == MODULE_SCOPE else f"{scope}.{node.name}"
        elif _is_exit_site(node, exceptions, calls):
            yield scope, getattr(node, "lineno", 0)
        for child in ast.iter_child_nodes(node):
            yield from visit(child, child_scope)

    sites: dict[str, list[int]] = {}
    for scope, line in walk_nodes(tree.body, MODULE_SCOPE):
        sites.setdefault(scope, []).append(line)
    return {scope: sorted(lines) for scope, lines in sorted(sites.items())}


def collect(source_root: Path) -> Sites:
    """Map each module path (relative to ``source_root``) to its per-function sites."""
    sites: Sites = {}
    for path in sorted(source_root.rglob("*.py")):
        relative = path.relative_to(source_root).as_posix()
        found = {
            scope: lines
            for scope, lines in exit_sites(path.read_text(encoding="utf-8")).items()
            if (relative, scope) not in BOUNDARY_SCOPES
        }
        if found:
            sites[relative] = found
    return sites


def _counts(sites: Sites) -> Baseline:
    return {relative: {scope: len(lines) for scope, lines in scopes.items()} for relative, scopes in sites.items()}


def violations(sites: Sites, baseline: Baseline) -> list[str]:
    """Describe every difference between the measured sites and the baseline."""
    problems: list[str] = []
    for relative, scopes in sorted(sites.items()):
        allowed = baseline.get(relative, {})
        for scope, lines in scopes.items():
            where = f"src/fieldkit/{relative}:{','.join(map(str, lines))} ({scope})"
            if not relative.startswith(COMMANDS_PREFIX):
                problems.append(f"{where}: domain code must raise a typed FieldkitError instead of exiting")
            elif len(lines) > allowed.get(scope, 0):
                problems.append(
                    f"{where}: {len(lines)} exit sites, baseline {allowed.get(scope, 0)}; "
                    "raise a typed FieldkitError for handle_cli_exception() to map instead"
                )
    for relative, allowed in sorted(baseline.items()):
        measured = sites.get(relative, {})
        for scope, count in sorted(allowed.items()):
            if len(measured.get(scope, [])) < count:
                problems.append(
                    f"src/fieldkit/{relative} ({scope}): {len(measured.get(scope, []))} exit sites, "
                    f"baseline {count}; lower the baseline with --write-baseline"
                )
    return problems


def growth(sites: Sites, baseline: Baseline) -> list[str]:
    """Return the files whose recording would admit a domain exit or a larger total."""
    problems: list[str] = []
    for relative, scopes in sorted(sites.items()):
        total = sum(map(len, scopes.values()))
        allowed = sum(baseline.get(relative, {}).values())
        if not relative.startswith(COMMANDS_PREFIX):
            problems.append(f"src/fieldkit/{relative}: domain code must raise a typed FieldkitError instead of exiting")
        elif total > allowed:
            problems.append(f"src/fieldkit/{relative}: {total} exit sites, baseline {allowed}; the total may not grow")
    return problems


def load_baseline(path: Path) -> Baseline:
    data = json.loads(path.read_text(encoding="utf-8"))
    files = data["files"]
    valid = isinstance(files, dict) and all(
        isinstance(scopes, dict) and all(isinstance(count, int) for count in scopes.values())
        for scopes in files.values()
    )
    if not valid:
        raise ValueError(f"{path.name}: 'files' must map module paths to per-function integer counts")
    return files


def write_baseline(sites: Sites, path: Path) -> None:
    document = {
        "description": (
            "Process-exit sites in command adapters that predate the exit boundary rule, "
            "counted per enclosing function. Counts may only fall; see scripts/check_exit_sites.py."
        ),
        "files": _counts(sites),
    }
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def _summary(sites: Sites) -> str:
    total = sum(len(lines) for scopes in sites.values() for lines in scopes.values())
    return f"{total} legacy sites in {len(sites)} command files"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ratchet process-exit sites outside the CLI exit boundary.")
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="Record the current counts after removing or moving sites; refuses to record growth.",
    )
    args = parser.parse_args(argv)

    sites = collect(SOURCE_ROOT)
    baseline = load_baseline(BASELINE_PATH)
    if args.write_baseline:
        refused = growth(sites, baseline)
        if refused:
            print("\n".join(refused), file=sys.stderr)
            return 1
        write_baseline(sites, BASELINE_PATH)
        print(f"Exit-site baseline: {_summary(sites)}")
        return 0

    problems = violations(sites, baseline)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print(f"Exit sites: PASS ({_summary(sites)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
