"""Check skillsaw grade density ratchet.

Runs skillsaw in JSON mode (no baseline) and fails if the grade density
(weighted violations per 10k tokens) exceeds the recorded ceiling.

This makes the 210 info-level violations that count toward the grade
visible as a ratchet: the density can only decrease (improve) over time.
New violations that push density above the ceiling fail CI immediately.

Usage:
    uv run python scripts/check_skillsaw.py

Exit codes:
    0 — density <= ceiling (pass)
    1 — density exceeds ceiling, or skillsaw failed to run (fail)
"""

from __future__ import annotations

import json
import subprocess
import sys

# Density ceiling: weighted violations per 10k tokens.
# To tighten after improvement: lower this value and commit.
# To accept a regression: raise this value with a comment explaining why.
# Earlier adjustments and their reasons are in this file's git history.
_DENSITY_CEILING = 2.7

# Letter grade floor. Grades: A, B, C, D, F (A is best).
# Fail if grade drops below this letter.
_GRADE_FLOOR = "A"

_GRADE_ORDER = ["A", "B", "C", "D", "F"]


def _grade_ok(actual: str, floor: str) -> bool:
    """Return True if actual grade is at or better than floor."""
    try:
        return _GRADE_ORDER.index(actual) <= _GRADE_ORDER.index(floor)
    except ValueError:
        return False


def main() -> None:
    result = subprocess.run(
        ["uvx", "skillsaw==0.16.0", "--format", "json", "--no-progress"],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0 and not result.stdout.strip():
        print(f"skillsaw failed (exit {result.returncode}):", file=sys.stderr)
        print(result.stderr[:500], file=sys.stderr)
        raise SystemExit(1)

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        print(f"skillsaw: malformed JSON output: {exc}", file=sys.stderr)
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

    if errors > 0:
        failures.append(f"  {errors} error(s) — fix before merge")

    if warnings > 0:
        failures.append(f"  {warnings} warning(s) — fix or baseline")

    if density is not None and density > _DENSITY_CEILING:
        failures.append(
            f"  grade density {density:.2f} exceeds ceiling {_DENSITY_CEILING:.2f} — "
            "new violations added; fix or update ceiling with a comment"
        )

    if letter != "?" and not _grade_ok(letter, _GRADE_FLOOR):
        failures.append(
            f"  grade {letter} below floor {_GRADE_FLOOR} — "
            "quality regressed; fix violations or lower _GRADE_FLOOR with a comment"
        )

    if failures:
        print("skillsaw FAIL:", file=sys.stderr)
        for line in failures:
            print(line, file=sys.stderr)
        raise SystemExit(1)

    print("skillsaw ✓")


if __name__ == "__main__":
    main()
