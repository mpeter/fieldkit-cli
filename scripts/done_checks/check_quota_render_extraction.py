#!/usr/bin/env python3
"""Check that quota rendering symbols were moved without compatibility shims."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_SYMBOLS = (
    "QUOTA_STAGE_WEIGHTS",
    "QuotaGapResult",
    "_parse_sf_amount",
    "calculate_quota_gap",
)
_COLLECTOR = "_collect_pursuits_for_quota"
_OLD_PREFIX = "commands.pipeline.quota."
_QUOTA_BINDING = '_CMD_QUOTA__QUOTA_MODULE = "fieldkit.commands.pipeline.quota"'
_CALC_BINDING = '_CMD_QUOTA__CALC_MODULE = "fieldkit.watch.morning_brief_render"'


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        print(f"cannot read {path}: {exc}")
        return None


def _bound_names(source: str, label: str) -> list[str] | None:
    try:
        tree = ast.parse(source, filename=label)
    except SyntaxError as exc:
        print(f"cannot parse {label}: {exc}")
        return None
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.extend(target.id for target in targets if isinstance(target, ast.Name))
    return names


def _imported_names(source: str, label: str) -> list[str] | None:
    try:
        tree = ast.parse(source, filename=label)
    except SyntaxError as exc:
        print(f"cannot parse {label}: {exc}")
        return None
    return [
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    ]


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print("usage: check_quota_render_extraction.py QUOTA RENDER SRC_ROOT TESTS_ROOT PIPELINE_TEST")
        return 1
    quota_path, render_path, src_root, tests_root, pipeline_test = map(Path, argv)
    quota = _read(quota_path)
    render = _read(render_path)
    pipeline = _read(pipeline_test)
    if quota is None or render is None or pipeline is None:
        return 1
    quota_bindings = _bound_names(quota, str(quota_path))
    render_bindings = _bound_names(render, str(render_path))
    quota_imports = _imported_names(quota, str(quota_path))
    if quota_bindings is None or render_bindings is None or quota_imports is None:
        return 1

    failures: list[str] = []
    for symbol in _SYMBOLS:
        if symbol in quota_bindings:
            failures.append(f"{symbol} remains in quota module")
        if symbol in quota_imports:
            failures.append(f"{symbol} is re-exported from quota module")
        if render_bindings.count(symbol) != 1:
            failures.append(f"{symbol} must be bound exactly once in render module")
    if quota_bindings.count(_COLLECTOR) != 1:
        failures.append(f"{_COLLECTOR} must remain bound exactly once in quota module")
    if _COLLECTOR in render_bindings:
        failures.append(f"{_COLLECTOR} must not move into render module")
    render_tree = ast.parse(render, filename=str(render_path))
    for node in ast.walk(render_tree):
        if isinstance(node, ast.Import) and any(alias.name.startswith("fieldkit.sf") for alias in node.names):
            failures.append(f"render module imports forbidden SF dependency at line {node.lineno}")
        if isinstance(node, ast.ImportFrom) and node.module == "fieldkit.sf":
            failures.append(f"render module imports forbidden SF dependency at line {node.lineno}")
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("fieldkit.sf."):
            failures.append(f"render module imports forbidden SF dependency at line {node.lineno}")
    for root in (src_root, tests_root):
        if not root.is_dir():
            failures.append(f"missing search root {root}")
            continue
        for path in root.rglob("*.py"):
            text = _read(path)
            if text is None:
                failures.append(f"could not inspect {path}")
            elif any(f"{_OLD_PREFIX}{symbol}" in text for symbol in _SYMBOLS):
                failures.append(f"old quota-qualified reference remains in {path}")
    if pipeline.count(_QUOTA_BINDING) != 1:
        failures.append("pipeline CLI collector-module binding must occur exactly once")
    if pipeline.count(_CALC_BINDING) != 1:
        failures.append("pipeline CLI calculation-module binding must occur exactly once")
    if failures:
        for failure in failures:
            print(f"quota extraction: {failure}")
        return 1
    print("quota extraction: valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
