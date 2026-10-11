"""Ratchet the process-exit sites that bypass the CLI exit boundary.

Only ``cli_exit.cli_main()`` and the ``__main__.main()`` dispatcher end the
process. Command adapters signal failure by raising a typed exception that
``handle_cli_exception()`` maps. Older adapters still raise ``SystemExit`` or
call ``ctx.exit()`` directly; this check records them per file in
``.exit-sites-baseline.json`` and fails when:

- domain code outside ``commands/`` exits at all,
- a command file gains a site or a file without a baseline entry adds one, or
- a file has fewer sites than its baseline, so the baseline is lowered with the
  migration that removed them and can never be spent on a new site.

Code under an ``if __name__ == "__main__":`` guard is a separate process entry
point and is not counted.

Usage:
    uv run python scripts/check_exit_sites.py                   # check
    uv run python scripts/check_exit_sites.py --write-baseline  # after removing sites

Exit codes:
    0 — every file matches its baseline (pass)
    1 — a new, domain, or already-removed site was found (fail)
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

# The two process boundaries; every other module raises or returns instead.
BOUNDARY_FILES = frozenset({"cli_exit.py", "__main__.py"})
COMMANDS_PREFIX = "commands/"
_EXIT_EXCEPTIONS = frozenset({"SystemExit", "Exit"})
_EXIT_CALLS = frozenset({"exit", "_exit"})
_EXIT_BUILTINS = frozenset({"exit", "quit"})


def _is_main_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return False
    return isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__"


def _callee_name(node: ast.expr) -> str | None:
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _is_exit_site(node: ast.AST) -> bool:
    if isinstance(node, ast.Raise) and node.exc is not None:
        return _callee_name(node.exc) in _EXIT_EXCEPTIONS
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Attribute):
        return node.func.attr in _EXIT_CALLS
    return isinstance(node.func, ast.Name) and node.func.id in _EXIT_BUILTINS


def exit_site_lines(source: str) -> list[int]:
    """Return the line of each process-exit site outside a ``__main__`` guard."""

    def walk(node: ast.AST) -> Iterator[int]:
        for child in ast.iter_child_nodes(node):
            if _is_main_guard(child):
                continue
            if _is_exit_site(child):
                yield getattr(child, "lineno", 0)
            yield from walk(child)

    return sorted(walk(ast.parse(source)))


def collect(source_root: Path = SOURCE_ROOT) -> dict[str, list[int]]:
    """Map each module path (relative to ``source_root``) to its exit-site lines."""
    sites: dict[str, list[int]] = {}
    for path in sorted(source_root.rglob("*.py")):
        relative = path.relative_to(source_root).as_posix()
        if relative in BOUNDARY_FILES:
            continue
        lines = exit_site_lines(path.read_text(encoding="utf-8"))
        if lines:
            sites[relative] = lines
    return sites


def violations(sites: dict[str, list[int]], baseline: dict[str, int]) -> list[str]:
    """Describe every difference between the measured sites and the baseline."""
    problems: list[str] = []
    for relative, lines in sorted(sites.items()):
        where = f"src/fieldkit/{relative}:{','.join(map(str, lines))}"
        if not relative.startswith(COMMANDS_PREFIX):
            problems.append(f"{where}: domain code must raise a typed FieldkitError instead of exiting")
        elif len(lines) > baseline.get(relative, 0):
            problems.append(
                f"{where}: {len(lines)} exit sites, baseline {baseline.get(relative, 0)}; "
                "raise a typed FieldkitError for handle_cli_exception() to map instead"
            )
    for relative, allowed in sorted(baseline.items()):
        if len(sites.get(relative, [])) < allowed:
            problems.append(
                f"src/fieldkit/{relative}: {len(sites.get(relative, []))} exit sites, baseline {allowed}; "
                "lower the baseline with --write-baseline"
            )
    return problems


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    files = data["files"]
    if not isinstance(files, dict) or not all(isinstance(count, int) for count in files.values()):
        raise ValueError(f"{path.name}: 'files' must map module paths to integer counts")
    return files


def write_baseline(sites: dict[str, list[int]], path: Path = BASELINE_PATH) -> None:
    document = {
        "description": (
            "Process-exit sites in command adapters that predate the exit boundary rule. "
            "Counts may only fall; see scripts/check_exit_sites.py."
        ),
        "files": {relative: len(lines) for relative, lines in sorted(sites.items())},
    }
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ratchet process-exit sites outside the CLI exit boundary.")
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="Record the current counts after removing sites; refuses to record new ones.",
    )
    args = parser.parse_args(argv)

    sites = collect(SOURCE_ROOT)
    baseline = load_baseline(BASELINE_PATH)
    if args.write_baseline:
        # Only removals may be recorded; anything else would launder a new site.
        growth = [problem for problem in violations(sites, baseline) if "lower the baseline" not in problem]
        if growth:
            print("\n".join(growth), file=sys.stderr)
            return 1
        write_baseline(sites, BASELINE_PATH)
        print(f"Exit-site baseline: {sum(map(len, sites.values()))} sites in {len(sites)} files")
        return 0

    problems = violations(sites, baseline)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print(f"Exit sites: PASS ({sum(map(len, sites.values()))} legacy sites in {len(sites)} command files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
