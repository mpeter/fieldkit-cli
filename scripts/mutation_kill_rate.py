"""Report mutation-testing kill rate per module (sensor, not a gate).

Reads ``mutants/<module>.meta`` files written by a prior ``uv run mutmut run`` and
reports, per module configured in ``[tool.mutmut].source_paths`` (pyproject.toml),
how many generated mutants were killed vs. survived vs. other outcomes (timeout,
no tests, etc.).

This answers "are these tests actually doing anything" with a number instead of an
argument (docs/succession-plan.md Section 3.6.2). Explicitly not wired into `make
quality` or CI — reporting only, no threshold, no gate.

Usage:
    uv run mutmut run
    uv run python scripts/mutation_kill_rate.py

Writes a JSON report to reports/mutation-kill-rate.json and prints a human-readable
table to stdout. Exits 0 always (reporting only).
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path
from typing import Any

try:
    from mutmut.__main__ import status_by_exit_code
except ImportError as exc:
    raise SystemExit(
        "mutation_kill_rate.py depends on mutmut's private CLI internals "
        "(mutmut.__main__.status_by_exit_code), which are not part of mutmut's "
        "public API and may change between releases. This import failed — check "
        "mutmut's CHANGELOG for the new location, or pin mutmut in pyproject.toml "
        "to a version where this still works."
    ) from exc


def _repo_root() -> Path:
    """Return the repository root (parent of this script's directory)."""
    return Path(__file__).parent.parent.resolve()


def _source_paths(repo_root: Path) -> list[str]:
    """Read [tool.mutmut].source_paths from pyproject.toml — single source of truth."""
    pyproject = repo_root / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text("utf-8"))
    paths: list[str] = data["tool"]["mutmut"]["source_paths"]
    return paths


def _kill_rate_for_module(repo_root: Path, module_path: str) -> dict[str, Any]:
    """Compute killed/survived/other counts and kill rate for one module.

    Reads mutants/<module_path>.meta, written by `mutmut run`. Returns zeroed
    counts and kill_rate None when the meta file does not exist (mutmut has not
    been run yet, or the module produced zero mutants).
    """
    meta_path = repo_root / "mutants" / f"{module_path}.meta"
    if not meta_path.is_file():
        return {
            "module": module_path,
            "total": 0,
            "killed": 0,
            "survived": 0,
            "other": 0,
            "kill_rate": None,
        }

    meta = json.loads(meta_path.read_text("utf-8"))
    exit_codes: dict[str, int] = meta.get("exit_code_by_key", {})

    killed = 0
    survived = 0
    other = 0
    for exit_code in exit_codes.values():
        status = status_by_exit_code[exit_code]
        if status == "killed":
            killed += 1
        elif status == "survived":
            survived += 1
        else:
            other += 1

    total = len(exit_codes)
    kill_rate = killed / total if total else None
    return {
        "module": module_path,
        "total": total,
        "killed": killed,
        "survived": survived,
        "other": other,
        "kill_rate": kill_rate,
    }


def main() -> int:
    repo_root = _repo_root()
    modules = _source_paths(repo_root)
    results = [_kill_rate_for_module(repo_root, module) for module in modules]

    print(f"{'module':<45} {'killed':>7} {'survived':>9} {'other':>6} {'kill_rate':>10}")
    for r in results:
        rate_str = f"{r['kill_rate']:.1%}" if r["kill_rate"] is not None else "n/a"
        print(f"{r['module']:<45} {r['killed']:>7} {r['survived']:>9} {r['other']:>6} {rate_str:>10}")

    reports_dir = repo_root / "reports"
    reports_dir.mkdir(exist_ok=True)
    report_path = reports_dir / "mutation-kill-rate.json"
    report_path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {report_path.relative_to(repo_root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
