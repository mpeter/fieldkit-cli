"""Fail on CRAP regressions in the functions a change touches.

Runs ``gazepy crap`` over the whole ``src/fieldkit/`` tree against the committed
``.gaze/baseline.json`` so every target's ``location`` key matches the baseline,
then applies the baseline gate's two failure rules to files changed relative to
``--base``: a tracked function whose CRAP rose (a regression), and a function the
baseline does not track whose CRAP exceeds the new-function threshold (a new
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

The baseline is a one-way ratchet. The comparison starts from the base revision's
committed baseline and takes the change's own ``.gaze/baseline.json`` edits only
where they add an entry or lower a score, so a pull request can re-track moved
functions or lock in a gain by running ``make gaze-baseline``. A raised score, a
dropped entry for a function that still exists, or an added entry above the
new-function threshold fails the check: that file becomes the baseline scheduled
enforcement reads once the change merges, and an added entry would otherwise
exempt its function from the threshold. A change to the baseline alone is checked
the same way, so no pull request raises a score.

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
from collections import defaultdict
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
# Allowance for float noise when comparing recorded scores; gazepy's own epsilon is 0.
_SCORE_TOLERANCE = 1e-9
_SCORE_FIELDS = ("crap", "gaze_crap")
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
                f"{self.location} {self.function}: new function CRAP {self.crap:.2f} > threshold {self.threshold:.2f}"
            )
        delta = self.crap - self.baseline_crap
        return f"{self.location} {self.function}: CRAP {self.baseline_crap:.2f} -> {self.crap:.2f} (+{delta:.2f})"


@dataclass(frozen=True)
class BaselineViolation:
    """An edit to the committed baseline that would loosen the gate once merged."""

    key: str
    detail: str

    def describe(self) -> str:
        """Render the violation as one reviewable line."""
        return f"{_BASELINE} {self.key}: {self.detail}"


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
        # Strictly above, as gazepy's baseline comparison classifies a new violation.
        if crap > threshold and location.rpartition(":")[0] in changed:
            failures.append(
                Regression(location=location, function=str(target.get("function", "")), crap=crap, threshold=threshold)
            )
    return failures


def score_key(target: Mapping[str, object]) -> str:
    """Return gazepy's baseline match key, ``package:receiver.function``."""
    receiver = target.get("receiver")
    qualifier = f"{receiver}." if isinstance(receiver, str) and receiver else ""
    return f"{target.get('package', '')}:{qualifier}{target.get('function', '')}"


def _baseline_entries(text: str, origin: str) -> dict[str, list[dict[str, object]]]:
    """Group a baseline's entries by match key; a few same-named functions share one."""
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError(f"{origin} baseline has no 'results' list")
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for entry in _entries(data, "results"):
        grouped[score_key(cast(dict[str, object], entry["target"]))].append(entry)
    return grouped


def _highest(entries: list[dict[str, object]], field: str) -> float | None:
    scores = [_number(entry[field]) for entry in entries if entry.get(field) is not None]
    return max(scores) if scores else None


def _raised_scores(
    key: str, base: list[dict[str, object]], proposed: list[dict[str, object]]
) -> list[BaselineViolation]:
    violations = []
    for field in _SCORE_FIELDS:
        before, after = _highest(base, field), _highest(proposed, field)
        if before is None:
            continue
        if after is None:
            violations.append(BaselineViolation(key, f"{field} {before:.2f} removed"))
        elif after > before + _SCORE_TOLERANCE:
            violations.append(BaselineViolation(key, f"{field} raised {before:.2f} -> {after:.2f}"))
    return violations


def ratchet_baseline(base_text: str, proposed_text: str) -> tuple[dict[str, object], list[BaselineViolation]]:
    """Merge the change's baseline into the base one, accepting only tightening edits.

    Returns the baseline to compare against and every score the change raised.
    Entries the change adds are taken as recorded; a key whose scores did not
    rise takes the change's entries, so a lowered score is held to its new
    value; a raised key keeps the base entries and is reported.
    """
    base = _baseline_entries(base_text, "base")
    proposed = _baseline_entries(proposed_text, "proposed")
    merged = dict(base)
    violations: list[BaselineViolation] = []
    for key, entries in proposed.items():
        raised = _raised_scores(key, base[key], entries) if key in base else []
        violations.extend(raised)
        if not raised:
            merged[key] = entries
    results = [entry for entries in merged.values() for entry in entries]
    return {"results": results}, violations


def dropped_entries(base_text: str, proposed_text: str, report: Mapping[str, object]) -> list[BaselineViolation]:
    """Return base entries the change deleted although their function still exists.

    Deleting the entry of a deleted function is ordinary cleanup; deleting a
    live one would put it back under the new-function threshold.
    """
    removed = _baseline_entries(base_text, "base").keys() - _baseline_entries(proposed_text, "proposed").keys()
    current = {
        score_key(cast(dict[str, object], entry["target"]))
        for key in ("results", "new_functions")
        for entry in _entries(report, key)
    }
    return [
        BaselineViolation(key, "entry removed while the function still exists") for key in sorted(removed & current)
    ]


def added_above_threshold(base_text: str, proposed_text: str, threshold: float) -> list[BaselineViolation]:
    """Return entries the change added whose CRAP exceeds the new-function threshold.

    An untracked function is held to the threshold; recording it in the
    baseline at a higher score would grant it an allowance the threshold
    refuses, so only entries within it may be added.
    """
    base = _baseline_entries(base_text, "base")
    violations = []
    for key, entries in sorted(_baseline_entries(proposed_text, "proposed").items()):
        crap = _highest(entries, "crap")
        if key not in base and crap is not None and crap > threshold:
            violations.append(BaselineViolation(key, f"entry added at CRAP {crap:.2f} > threshold {threshold:.2f}"))
    return violations


def _git(*arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        capture_output=True,
        text=True,
        check=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    ).stdout


def merge_base(base: str) -> str:
    """Return the commit where ``HEAD`` diverged from ``base``."""
    return _git("merge-base", base, "HEAD").strip()


def changed_source_files(since: str) -> list[str]:
    """Return ``src/fieldkit`` Python files changed since commit ``since``, relative to that root.

    Compares ``since`` with the working tree and adds untracked, non-ignored
    files, so a local run also checks uncommitted and unstaged new files; in CI
    the tree equals ``HEAD``.
    """
    root = f"{_SRC_ROOT}/"
    tracked = _git("diff", "--name-only", "--diff-filter=AMR", since, "--", root)
    untracked = _git("ls-files", "--others", "--exclude-standard", "--", root)
    paths = {line for line in (tracked + untracked).splitlines() if line.startswith(root) and line.endswith(".py")}
    return sorted(path[len(root) :] for path in paths)


def base_baseline(since: str) -> str:
    """Return the baseline as committed at ``since``, the floor the ratchet starts from."""
    return _git("show", f"{since}:{_BASELINE}")


def _gazepy_report(coverprofile: Path, baseline: Path) -> dict[str, object]:
    """Run gazepy against ``baseline`` and return its JSON report.

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
                str(baseline),
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


@dataclass(frozen=True)
class Evaluation:
    """What the check examined and every failure it found."""

    changed: list[str]
    baseline_changed: bool
    failures: list[Regression | BaselineViolation]


def _evaluate(coverprofile: Path, base: str) -> Evaluation:
    since = merge_base(base)
    changed = changed_source_files(since)
    base_text = base_baseline(since)
    proposed_text = Path(_BASELINE).read_text(encoding="utf-8")
    # A baseline-only change is checked too: it is the file scheduled enforcement reads after merge.
    baseline_changed = proposed_text != base_text
    if not changed and not baseline_changed:
        return Evaluation(changed, baseline_changed, [])
    merged, violations = ratchet_baseline(base_text, proposed_text)
    with tempfile.TemporaryDirectory(prefix="gaze-changed-baseline-") as directory:
        baseline = Path(directory) / "baseline.json"
        baseline.write_text(json.dumps(merged), encoding="utf-8")
        report = _gazepy_report(coverprofile, baseline)
    failures: list[Regression | BaselineViolation] = [*violations, *dropped_entries(base_text, proposed_text, report)]
    failures.extend(added_above_threshold(base_text, proposed_text, _new_function_threshold(report)))
    failures.extend(changed_regressions(report, changed))
    return Evaluation(changed, baseline_changed, failures)


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
        evaluation = _evaluate(args.coverprofile, args.base)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, ValueError) as exc:
        print(f"gaze-changed: {exc}", file=sys.stderr)
        return _EXIT_INVALID

    if not evaluation.changed and not evaluation.baseline_changed:
        print(f"gaze-changed: no src/fieldkit Python files or {_BASELINE} changed; nothing to check.")
        return 0
    if not evaluation.failures:
        scope = f"{len(evaluation.changed)} changed file(s)" + (
            f" and {_BASELINE}" if evaluation.baseline_changed else ""
        )
        print(f"gaze-changed: PASS — no CRAP regressions in {scope}.")
        return 0
    print(f"gaze-changed: FAIL — {len(evaluation.failures)} CRAP failure(s):")
    for failure in evaluation.failures:
        print(f"  {failure.describe()}")
    print(
        f"Add tests or decompose the function. {_BASELINE} only ratchets down: a change may add "
        "entries within the new-function threshold or lower scores, and nothing raises one."
    )
    return _EXIT_REGRESSION


if __name__ == "__main__":
    raise SystemExit(main())
