#!/usr/bin/env python3
"""Validate a release-governance policy against one verified candidate report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _release_governance import validate

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POLICY = REPO_ROOT / "docs" / "release-readiness" / "release-governance-policy.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--candidate-report", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Return 0 only when the candidate has every required external control."""
    args = _parser().parse_args(argv)
    try:
        report = validate(args.policy, args.candidate_report)
    except ValueError as exc:
        sys.stderr.write(f"release governance validation error: {exc}\n")
        return 3
    sys.stdout.write(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n")
    return 0 if report.publication_authorized else 1


if __name__ == "__main__":
    raise SystemExit(main())
