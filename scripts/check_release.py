#!/usr/bin/env python3
"""Validate fieldkit release policy and emit revision-independent gate evidence."""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

import _release_policy as policy_check


def _parser() -> argparse.ArgumentParser:
    """Build the release validation command surface."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    policy = subparsers.add_parser("policy", help="validate checked-in release policy")
    policy.add_argument("--repo-root", type=Path, default=policy_check.REPO_ROOT, help=argparse.SUPPRESS)
    policy.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _run_policy(args: argparse.Namespace) -> int:
    """Validate policy and render its stable result."""
    report = policy_check.validate_repository(args.repo_root.resolve())
    if args.as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    elif report.ok:
        print("Release policy: PASS")
    else:
        print(f"Release policy: FAIL ({len(report.findings)} finding(s))")
        for finding in report.findings:
            print(f"  {finding.criterion_id} {finding.subject}: {finding.message}")
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    """Run one release validation operation with stable process statuses."""
    args = _parser().parse_args(argv)
    try:
        return _run_policy(args)
    except (OSError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        print(f"Release policy: ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
