#!/usr/bin/env python3
"""Create bounded, revision-attributable evidence for GitHub Actions checks."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

MAX_JUNIT_BYTES = 16 * 1024 * 1024
MAX_COVERAGE_BYTES = 32 * 1024 * 1024
MAX_FAILURE_NAMES = 20
MAX_FAILURE_NAME_LENGTH = 200
SCHEMA_VERSION = 1

_REVISION = re.compile(r"[0-9a-f]{40}")
_CHILD_RESULTS = frozenset({"success", "failure", "cancelled", "skipped", "missing"})
_REQUIRED_CHILDREN = (
    "Detect code changes",
    "Commit-message PII guard",
    "Lint (ruff)",
    "Test (pytest)",
    "Skillsaw (skill lint)",
    "AgentReady score gate",
)


def _generated_at() -> str:
    return datetime.now(tz=UTC).isoformat()


def _validate_revision(value: str) -> str:
    normalized = value.lower()
    if _REVISION.fullmatch(normalized) is None:
        raise ValueError("source revision must be a full 40-character hexadecimal commit SHA")
    return normalized


def _read_bounded(path: Path, maximum_bytes: int) -> str:
    size = path.stat().st_size
    if size > maximum_bytes:
        raise ValueError(f"{path.name} exceeds the {maximum_bytes}-byte evidence limit")
    return path.read_text(encoding="utf-8")


def _write_json(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(f"{json.dumps(report, indent=2, sort_keys=True)}\n", encoding="utf-8")
    temporary.replace(path)


def _append_summary(path: Path | None, text: str) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(text)


def _integer_attribute(element: ET.Element, name: str) -> int:
    raw = element.attrib.get(name)
    if raw is None:
        raise ValueError(f"JUnit root is missing the {name!r} count")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"JUnit {name!r} count must be an integer") from exc
    if value < 0:
        raise ValueError(f"JUnit {name!r} count must not be negative")
    return value


def _float_attribute(element: ET.Element, name: str) -> float:
    raw = element.attrib.get(name, "0")
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"JUnit {name!r} value must be numeric") from exc
    if value < 0:
        raise ValueError(f"JUnit {name!r} value must not be negative")
    return value


def _junit_suites(root: ET.Element) -> tuple[ET.Element, ...]:
    if root.tag not in {"testsuite", "testsuites"}:
        raise ValueError("JUnit root must be testsuite or testsuites")
    if root.tag == "testsuite":
        return (root,)
    suites = tuple(root.findall("testsuite"))
    if not suites:
        raise ValueError("JUnit testsuites root contains no testsuite evidence")
    return suites


def _junit_counts(suites: Sequence[ET.Element]) -> dict[str, int]:
    return {
        name: sum(_integer_attribute(suite, name) for suite in suites)
        for name in ("tests", "failures", "errors", "skipped")
    }


def _failure_names(root: ET.Element) -> list[str]:
    names: list[str] = []
    for case in root.iter("testcase"):
        if case.find("failure") is None and case.find("error") is None:
            continue
        class_name = case.attrib.get("classname", "")[:MAX_FAILURE_NAME_LENGTH]
        test_name = case.attrib.get("name", "unknown")[:MAX_FAILURE_NAME_LENGTH]
        names.append(f"{class_name}::{test_name}" if class_name else test_name)
        if len(names) == MAX_FAILURE_NAMES:
            break
    return names


def _junit_report(args: argparse.Namespace) -> dict[str, object]:
    root = ET.fromstring(_read_bounded(args.input, MAX_JUNIT_BYTES))
    suites = _junit_suites(root)
    counts = _junit_counts(suites)
    status = "fail" if counts["failures"] or counts["errors"] else "pass"
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "junit-summary",
        "status": status,
        "source_revision": _validate_revision(args.source_revision),
        "scope": args.scope,
        "command": args.command,
        "tool": {"name": "pytest", "version": importlib.metadata.version("pytest")},
        "generated_at": _generated_at(),
        "duration_seconds": sum(_float_attribute(suite, "time") for suite in suites),
        "counts": counts,
        "failures": _failure_names(root),
    }


def _coverage_report(args: argparse.Namespace) -> dict[str, object]:
    document = json.loads(_read_bounded(args.input, MAX_COVERAGE_BYTES))
    if not isinstance(document, dict):
        raise ValueError("coverage evidence root must be an object")
    meta = document.get("meta")
    totals = document.get("totals")
    if not isinstance(meta, dict) or not isinstance(totals, dict):
        raise ValueError("coverage evidence must contain meta and totals objects")
    version = meta.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("coverage metadata must contain a tool version")
    numeric_names = ("covered_lines", "num_statements", "percent_covered", "missing_lines", "excluded_lines")
    if any(not isinstance(totals.get(name), int | float) for name in numeric_names):
        raise ValueError("coverage totals must contain numeric line and percentage fields")
    percent = float(totals["percent_covered"])
    status = "pass" if percent >= args.minimum_percent else "fail"
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "coverage-summary",
        "status": status,
        "source_revision": _validate_revision(args.source_revision),
        "scope": args.scope,
        "command": args.command,
        "tool": {"name": "coverage.py", "version": version},
        "generated_at": _generated_at(),
        "coverage_timestamp": meta.get("timestamp"),
        "minimum_percent": args.minimum_percent,
        "totals": {name: totals[name] for name in numeric_names},
    }


def _parse_children(values: Sequence[str]) -> tuple[dict[str, str | None], ...]:
    parsed: dict[str, str] = {}
    for value in values:
        name, separator, result = value.rpartition("=")
        if not separator or name not in _REQUIRED_CHILDREN or result not in _CHILD_RESULTS:
            raise ValueError("child results must use a required name and a supported conclusion")
        if name in parsed:
            raise ValueError(f"duplicate child result: {name}")
        parsed[name] = result
    children: list[dict[str, str | None]] = []
    for name in _REQUIRED_CHILDREN:
        result = parsed.get(name, "missing")
        children.append({"name": name, "result": result, "skip_rationale": None})
    return tuple(children)


def _required_report(args: argparse.Namespace) -> dict[str, object]:
    children = list(_parse_children(args.child))
    passed = all(child["result"] == "success" for child in children)
    if not args.run_url.startswith("https://") or len(args.run_url) > 500:
        raise ValueError("run URL must be a bounded HTTPS URL")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "required-checks-summary",
        "status": "pass" if passed else "fail",
        "source_revision": _validate_revision(args.source_revision),
        "scope": args.scope,
        "command": "python3 scripts/ci_evidence.py required",
        "tool": {"name": "fieldkit-ci-evidence", "version": str(SCHEMA_VERSION)},
        "generated_at": _generated_at(),
        "run_url": args.run_url,
        "children": children,
    }


def _junit_markdown(report: dict[str, object]) -> str:
    counts = report["counts"]
    duration = report["duration_seconds"]
    if not isinstance(counts, dict) or not isinstance(duration, int | float):
        raise ValueError("JUnit report counts are invalid")
    return (
        "## Pull-request test evidence\n\n"
        f"Scope: `{report['scope']}` · revision: `{report['source_revision']}` · status: **{report['status']}**\n\n"
        "| Tests | Failures | Errors | Skipped | Duration |\n"
        "| ---: | ---: | ---: | ---: | ---: |\n"
        f"| {counts['tests']} | {counts['failures']} | {counts['errors']} | {counts['skipped']} | "
        f"{duration:.2f}s |\n"
    )


def _coverage_markdown(report: dict[str, object]) -> str:
    totals = report["totals"]
    minimum = report["minimum_percent"]
    if not isinstance(totals, dict) or not isinstance(minimum, int | float):
        raise ValueError("coverage report totals are invalid")
    percent = totals.get("percent_covered")
    if not isinstance(percent, int | float):
        raise ValueError("coverage report percentage is invalid")
    return (
        "## Full-repository coverage evidence\n\n"
        f"Revision: `{report['source_revision']}` · status: **{report['status']}** · "
        f"coverage: **{percent:.2f}%** "
        f"(minimum {minimum:.2f}%)\n"
    )


def _required_markdown(report: dict[str, object]) -> str:
    children = report["children"]
    if not isinstance(children, list):
        raise ValueError("required-check report children are invalid")
    rows = ["## Required checks", "", f"Result: **{report['status']}** · scope: `{report['scope']}`", ""]
    rows.extend(("| Child | Result | Rationale |", "| --- | --- | --- |"))
    for child in children:
        if not isinstance(child, dict):
            raise ValueError("required-check child is invalid")
        rows.append(f"| {child['name']} | {child['result']} | {child['skip_rationale'] or ''} |")
    rows.extend(("", f"[Open this workflow run]({report['run_url']})", ""))
    return "\n".join(rows)


def _add_common_evidence_arguments(parser: argparse.ArgumentParser, *, scope: str) -> None:
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--step-summary", type=Path)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--scope", choices=(scope,), required=True)
    parser.add_argument("--command", required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="kind", required=True)

    junit = subparsers.add_parser("junit")
    _add_common_evidence_arguments(junit, scope="tach-selected")

    coverage = subparsers.add_parser("coverage")
    _add_common_evidence_arguments(coverage, scope="full-repository")
    coverage.add_argument("--minimum-percent", type=float, required=True)

    required = subparsers.add_parser("required")
    required.add_argument("--output", type=Path, required=True)
    required.add_argument("--step-summary", type=Path)
    required.add_argument("--source-revision", required=True)
    required.add_argument("--scope", choices=("code", "docs-only"), required=True)
    required.add_argument("--run-url", required=True)
    required.add_argument("--child", action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Create one evidence report and return its represented result."""
    args = _parser().parse_args(argv)
    try:
        if args.kind == "junit":
            report = _junit_report(args)
            summary = _junit_markdown(report)
        elif args.kind == "coverage":
            report = _coverage_report(args)
            summary = _coverage_markdown(report)
        else:
            report = _required_report(args)
            summary = _required_markdown(report)
        _write_json(args.output, report)
        _append_summary(args.step_summary, summary)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, ET.ParseError) as exc:
        print(f"CI evidence: ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"CI evidence: {report['kind']} {report['status']}")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
