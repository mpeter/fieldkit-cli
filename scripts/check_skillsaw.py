"""Check skillsaw grade density ratchet.

Runs skillsaw in JSON mode and fails if the grade density
(weighted violations per 10k tokens) exceeds the recorded ceiling.

Info-level violations count toward the grade and density ratchet.
New violations that push density above the ceiling fail CI immediately.

Usage:
    uv run python scripts/check_skillsaw.py

Exit codes:
    0 — density <= ceiling (pass)
    1 — density exceeds ceiling, or skillsaw failed to run (fail)
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

if __package__:
    from scripts import process_supervision
else:
    import process_supervision

# Frozen density ceiling: weighted violations per 10k tokens.
_DENSITY_CEILING = 2.7
_SKILLSAW_TIMEOUT_SECONDS = 120
_OUTPUT_LIMIT_BYTES = 8 * 1024**2
_SKILLSAW_VERSION = "0.16.0"

# Letter grade floor in Skillsaw's ordered grading scale.
# Fail if grade drops below this letter.
_GRADE_FLOOR = "A"

_GRADE_ORDER = ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "F"]


def _grade_ok(actual: str, floor: str) -> bool:
    """Return True if actual grade is at or better than floor."""
    try:
        return _GRADE_ORDER.index(actual) <= _GRADE_ORDER.index(floor)
    except ValueError:
        return False


def _validate_report(data: object) -> None:
    """Reject incomplete or invalid metrics before applying the frozen gates."""
    if not isinstance(data, dict) or not isinstance(data.get("summary"), dict):
        raise ValueError("missing summary object")
    summary = data["summary"]
    for field in ("errors", "warnings", "info", "baseline_suppressed"):
        value = summary.get(field)
        if type(value) is not int or value < 0:
            raise ValueError(f"invalid {field} count")
    grade = summary.get("grade")
    if not isinstance(grade, dict) or grade.get("letter") not in _GRADE_ORDER:
        raise ValueError("missing or invalid grade")
    density = grade.get("density")
    if isinstance(density, bool) or not isinstance(density, (int, float)):
        raise ValueError("invalid grade density")
    if not math.isfinite(density) or density < 0:
        raise ValueError("invalid grade density")


def run_check(repo: Path, *, allow_baseline: bool) -> None:
    """Apply the frozen metrics; baseline use requires an explicit caller policy."""
    try:
        if version("skillsaw") != _SKILLSAW_VERSION:
            raise ValueError("installed skillsaw version does not match the locked policy")
        result = process_supervision.run_bounded(
            [
                sys.executable,
                "-I",
                "-m",
                "skillsaw",
                "lint",
                ".",
                "--format",
                "json",
                "--no-progress",
                "--fail-on",
                "warning",
                "--no-custom-rules",
                "--no-plugins",
                *([] if allow_baseline else ["--no-baseline"]),
            ],
            timeout_seconds=_SKILLSAW_TIMEOUT_SECONDS,
            output_limit=_OUTPUT_LIMIT_BYTES,
            cwd=repo,
        )
    except (
        OSError,
        ValueError,
        PackageNotFoundError,
        process_supervision.ProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"skillsaw could not complete: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    if result.returncode != 0:
        print(f"skillsaw failed (exit {result.returncode}):", file=sys.stderr)

    try:
        data = json.loads(result.stdout)
        _validate_report(data)
    except (ValueError, RecursionError) as exc:
        print(f"skillsaw: invalid report: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    summary = data.get("summary", {})
    grade_info = summary.get("grade", {})
    density: float | None = grade_info.get("density")
    letter: str = grade_info.get("letter", "?")
    errors: int = summary.get("errors", 0)
    warnings: int = summary.get("warnings", 0)
    info: int = summary.get("info", 0)
    suppressed: int = summary.get("baseline_suppressed", 0)

    print(
        f"skillsaw: grade={letter}  density={density:.2f}/10k  "
        f"errors={errors} warnings={warnings} info={info} suppressed={suppressed}"
    )

    failures: list[str] = []

    if result.returncode != 0:
        failures.append(f"  tool exited with status {result.returncode}")

    if suppressed and not allow_baseline:
        failures.append("  baseline suppressions are forbidden in the public gate")

    if errors > 0:
        failures.append(f"  {errors} error(s) — fix before merge")

    if warnings > 0:
        failures.append(f"  {warnings} warning(s) — fix before merge")

    if density is not None and density > _DENSITY_CEILING:
        failures.append(
            f"  grade density {density:.2f} exceeds ceiling {_DENSITY_CEILING:.2f} — "
            "fix violations without changing the frozen ceiling"
        )

    if letter != "?" and not _grade_ok(letter, _GRADE_FLOOR):
        failures.append(
            f"  grade {letter} below floor {_GRADE_FLOOR} — fix violations without changing the frozen grade floor"
        )

    if failures:
        print("skillsaw FAIL:", file=sys.stderr)
        for line in failures:
            print(line, file=sys.stderr)
        raise SystemExit(1)

    print("skillsaw ✓")


def main() -> None:
    """Run the public gate without baseline suppressions."""
    run_check(Path.cwd(), allow_baseline=False)


if __name__ == "__main__":
    main()
