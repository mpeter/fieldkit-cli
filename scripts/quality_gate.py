#!/usr/bin/env python3
"""Execute or verify fieldkit's canonical fixed-argv quality plans."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from fieldkit.util.bounded_process import BoundedProcessError

if TYPE_CHECKING or __package__:
    from scripts import quality_execution as _execution
    from scripts import quality_plan as _plan
    from scripts import quality_receipt as _receipt
    from scripts import quality_source as _source
else:
    import quality_execution as _execution
    import quality_plan as _plan
    import quality_receipt as _receipt
    import quality_source as _source


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    for operation, help_text in (
        ("run", "execute one canonical quality tier"),
        ("verify", "verify a receipt against the current canonical plan"),
    ):
        command = subparsers.add_parser(operation, help=help_text)
        command.add_argument("--tier", choices=("pr", "full", "impact"), required=True)
        command.add_argument("--base")
        command.add_argument("--head")
        command.add_argument("--workers", type=int, default=_plan.DEFAULT_WORKERS)
        command.add_argument("--github-output")
        command.add_argument("--selection-report")
        command.add_argument("--junitxml")
        command.add_argument("--receipt", required=True)
    return parser


def _plan_from_args(args: argparse.Namespace, repo: Path) -> _plan.QualityPlan:
    return _plan.build_plan(
        cast(Literal["pr", "full", "impact"], args.tier),
        repo=repo,
        quality_base=args.base,
        candidate_head=args.head,
        workers=args.workers,
        github_output=args.github_output,
        selection_report=args.selection_report,
        junitxml=args.junitxml,
    )


def main(argv: list[str] | None = None) -> int:
    """Execute or verify a canonical plan with payload-safe diagnostics."""
    args = _parser().parse_args(argv)
    repo = Path.cwd().resolve(strict=True)
    try:
        plan = _plan_from_args(args, repo)
        destination = _receipt.receipt_path(repo, args.receipt)
        receipt: object
        if args.operation == "run":
            generated_receipt = _execution.execute_plan(plan, repo=repo)
            _receipt.write_receipt(destination, generated_receipt)
            receipt = generated_receipt
        else:
            receipt = _receipt.read_bounded_json(destination, _receipt.RECEIPT_LIMIT_BYTES)
        expected_source = _source.capture_source_state(repo) if args.operation == "verify" else None
        findings = _receipt.validate_receipt(receipt, plan=plan, expected_source=expected_source)
    except (BoundedProcessError, OSError, UnicodeError, ValueError, json.JSONDecodeError):
        print("Quality gate: ERROR: controller could not produce valid evidence", file=sys.stderr)
        return 2
    if findings:
        print("Quality gate: FAIL: receipt verification failed", file=sys.stderr)
        for finding in findings:
            print(f"quality-receipt:{finding}", file=sys.stderr)
        return 1
    if not _receipt.receipt_passes(receipt, plan=plan, expected_source=expected_source):
        print("Quality gate: FAIL", file=sys.stderr)
        return 1
    print(f"Quality gate: PASS ({plan.tier}; unqualified controller)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
