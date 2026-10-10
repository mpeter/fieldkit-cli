"""Fail on CRAP regressions in the functions a change touches.

Runs ``gazepy crap`` over the whole ``src/fieldkit/`` tree against the committed
``.gaze/baseline.json`` so every target's ``location`` key matches the baseline,
then applies the baseline gate's two failure rules to files changed relative to
``--base``: a tracked function whose CRAP rose (a regression), and a function the
baseline does not track whose CRAP reaches the new-function threshold (a new
violation). The
complete baseline, ceiling and contract-coverage gates stay in
``make quality-full``; this check reports the subset a pull request introduced.

gazepy 0.9 JSON shape relied on here::

    {"results": [{"target": {"location": "contact/_enrich_helpers.py:696",
                             "function": "run_enrichment_pipeline", ...},
                  "status": "regression" | "improvement" | "unchanged",
                  "crap": 18.0, "baseline_crap": 11.0, ...}],
     "new_functions": [{"target": {...}, "status": "new", "crap": 5.7, ...}],
     "comparison": {"new_function_threshold": 15.0, "passed": true, ...}}

``location`` is relative to the scanned directory. Functions that moved or were
renamed since the baseline was recorded appear under ``new_functions``, so they
are held to the new-function threshold rather than their old score.

Usage:
    uv run python scripts/gaze_changed.py --coverprofile coverage-changed.json --base origin/main

Exit codes: 0 no regression in a changed file, 1 one or more regressions,
3 invalid input (missing coverage, unreadable report, failed git or gazepy).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

_SRC_ROOT = "src/fieldkit"
_BASELINE = ".gaze/baseline.json"
_GIT_TIMEOUT_SECONDS = 30
_GAZEPY_TIMEOUT_SECONDS = 300
# 0: comparison passed; 1: comparison failed somewhere in the tree. Both print the report.
_GAZEPY_REPORT_EXITS = frozenset({0, 1})
_EXIT_REGRESSION = 1
_EXIT_INVALID = 3


@dataclass(frozen=True)
class Regression:
    """One function that fails the baseline gate: a rise over its baseline, or a new function over the threshold."""

    location: str
    function: str
    crap: float
    baseline_crap: float | None = None
    threshold: float | None = None

    def describe(self) -> str:
        """Render the failure as one reviewable line."""
        if self.baseline_crap is None:
            return (
                f"{self.location} {self.function}: new function CRAP {self.crap:.2f} >= threshold {self.threshold:.2f}"
            )
        delta = self.crap - self.baseline_crap
        return f"{self.location} {self.function}: CRAP {self.baseline_crap:.2f} -> {self.crap:.2f} (+{delta:.2f})"


def _entries(report: Mapping[str, object], key: str) -> list[dict[str, object]]:
    entries = report.get(key, [])
    if not isinstance(entries, list):
        raise ValueError(f"gazepy report '{key}' is not a list")
    return [entry for entry in entries if isinstance(entry, dict) and isinstance(entry.get("target"), dict)]


def _number(value: object) -> float:
    if not isinstance(value, int | float):
        raise ValueError(f"gazepy report has a non-numeric CRAP value: {value!r}")
    return float(value)


def _new_function_threshold(report: Mapping[str, object]) -> float:
    comparison = report.get("comparison")
    threshold = comparison.get("new_function_threshold") if isinstance(comparison, dict) else None
    if not isinstance(threshold, int | float):
        raise ValueError("gazepy report has no comparison.new_function_threshold")
    return float(threshold)


def changed_regressions(report: Mapping[str, object], changed_files: Iterable[str]) -> list[Regression]:
    """Return baseline-gate failures in ``report`` whose file is one of ``changed_files``.

    ``changed_files`` are paths relative to ``src/fieldkit/``, the root gazepy
    scanned, so they compare directly with the file part of each ``location``.
    """
    changed = set(changed_files)
    if "results" not in report:
        raise ValueError("gazepy report has no 'results' list")
    failures: list[Regression] = []
    for result in _entries(report, "results"):
        target = cast(dict[str, object], result["target"])
        location = str(target.get("location", ""))
        if result.get("status") == "regression" and location.rpartition(":")[0] in changed:
            failures.append(
                Regression(
                    location=location,
                    function=str(target.get("function", "")),
                    crap=_number(result.get("crap")),
                    baseline_crap=_number(result.get("baseline_crap")),
                )
            )
    new_functions = _entries(report, "new_functions")
    if not new_functions:
        return failures
    threshold = _new_function_threshold(report)
    for result in new_functions:
        target = cast(dict[str, object], result["target"])
        location = str(target.get("location", ""))
        crap = _number(result.get("crap"))
        if crap >= threshold and location.rpartition(":")[0] in changed:
            failures.append(
                Regression(location=location, function=str(target.get("function", "")), crap=crap, threshold=threshold)
            )
    return failures


def changed_source_files(base: str) -> list[str]:
    """Return ``src/fieldkit`` Python files changed since ``base``, relative to that root.

    Compares the merge base of ``base`` and ``HEAD`` with the working tree, so a
    local run also checks uncommitted edits; in CI the tree equals ``HEAD``.
    """
    merge_base = subprocess.run(
        ["git", "merge-base", base, "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    ).stdout.strip()
    completed = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=AMR", merge_base, "--", f"{_SRC_ROOT}/"],
        capture_output=True,
        text=True,
        check=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    prefix = f"{_SRC_ROOT}/"
    return [
        line[len(prefix) :]
        for line in completed.stdout.splitlines()
        if line.startswith(prefix) and line.endswith(".py")
    ]


def _gazepy_report(coverprofile: Path) -> dict[str, object]:
    """Run gazepy against the baseline and return its JSON report.

    gazepy exits 1 when the whole-tree comparison fails but still prints the
    report, so exit 1 is a result to filter rather than an error.

    ``--tests`` points at an empty directory. Test analysis only feeds contract
    coverage and GazeCRAP, which this check does not gate, and skipping it cuts
    the scan from about 110 s to 10 s with identical CRAP scores. GazeCRAP stays
    gated by the complete baseline run in ``make quality-full``.
    """
    with tempfile.TemporaryDirectory(prefix="gaze-changed-no-tests-") as no_tests:
        completed = subprocess.run(
            [
                "gazepy",
                "crap",
                f"{_SRC_ROOT}/",
                "--coverprofile",
                str(coverprofile),
                "--baseline",
                _BASELINE,
                "--tests",
                no_tests,
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=_GAZEPY_TIMEOUT_SECONDS,
        )
    if completed.returncode not in _GAZEPY_REPORT_EXITS:
        raise ValueError(f"gazepy exited {completed.returncode}: {completed.stderr.strip()[:500]}")
    report = json.loads(completed.stdout)
    if not isinstance(report, dict):
        raise ValueError("gazepy report is not a JSON object")
    return report


def main(argv: list[str] | None = None) -> int:
    """Run the changed-function CRAP check and return its exit status."""
    parser = argparse.ArgumentParser(description="Fail on CRAP regressions in the functions a change touches.")
    parser.add_argument("--coverprofile", type=Path, required=True, help="coverage.py JSON report for the suite")
    parser.add_argument("--base", required=True, help="revision the change is compared against")
    args = parser.parse_args(argv)

    if not args.coverprofile.is_file():
        print(f"gaze-changed: coverage report not found: {args.coverprofile}", file=sys.stderr)
        return _EXIT_INVALID
    try:
        changed = changed_source_files(args.base)
        if not changed:
            print("gaze-changed: no src/fieldkit Python files changed; nothing to check.")
            return 0
        regressions = changed_regressions(_gazepy_report(args.coverprofile), changed)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, ValueError) as exc:
        print(f"gaze-changed: {exc}", file=sys.stderr)
        return _EXIT_INVALID

    if not regressions:
        print(f"gaze-changed: PASS — no CRAP regressions in {len(changed)} changed file(s).")
        return 0
    print(f"gaze-changed: FAIL — {len(regressions)} CRAP failure(s) in changed files:")
    for regression in regressions:
        print(f"  {regression.describe()}")
    print(
        "Add tests or decompose the function. If the score is intended, record it in "
        f"{_BASELINE} in this pull request so the change is visible in review."
    )
    return _EXIT_REGRESSION


if __name__ == "__main__":
    raise SystemExit(main())
