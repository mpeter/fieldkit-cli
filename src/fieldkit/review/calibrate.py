"""fieldkit.review.calibrate — compute quality metrics from the review journal."""

from pathlib import Path
from typing import Any

from fieldkit.review.journal import read_entries


def compute_metrics(
    data_path: Path,
    *,
    last_n: int = 50,
) -> dict[str, Any]:
    """Compute calibration metrics from the review journal.

    Returns per-category false-positive rate, per-lens finding yield,
    catalogue drift, and annotation rates.
    """
    all_entries = read_entries(data_path)
    runs = [e for e in all_entries if e.get("type") == "run"]
    annotations = [e for e in all_entries if e.get("type") == "annotation"]

    if not runs:
        return {"empty": True, "message": "No journal entries found."}

    recent = runs[-last_n:]

    return {
        "empty": False,
        "totalRuns": len(runs),
        "analyzedRuns": len(recent),
        "categoryFalsePositiveRate": _category_fp_rate(recent),
        "categoryAnnotationRate": _category_annotation_rate(recent, annotations),
        "lensYield": _lens_yield(recent),
        "catalogueDrift": _catalogue_drift(recent),
        "triageDistribution": _triage_distribution(recent),
    }


def _category_fp_rate(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per-category false-positive rate from Phase 3 verdicts."""
    return _category_fp_rates_from_runs(runs)


def _category_fp_rates_from_runs(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    surfaced: dict[str, int] = {}
    refuted: dict[str, int] = {}

    for run in runs:
        phase3_categories = _phase3_category_counts(run)
        if phase3_categories is None:
            phase3_categories = _legacy_category_counts(run)
        for category, counts in phase3_categories.items():
            surfaced[category] = surfaced.get(category, 0) + _outcome_total(counts)
            refuted[category] = refuted.get(category, 0) + _valid_count(counts.get("refuted"))

    result: dict[str, dict[str, Any]] = {}
    for cat in sorted(surfaced):
        s = surfaced[cat]
        r = refuted.get(cat, 0)
        result[cat] = {
            "surfaced": s,
            "refuted": r,
            "rate": round(r / s, 3) if s > 0 else 0.0,
        }
    return result


def _phase3_category_counts(run: dict[str, Any]) -> dict[str, dict[Any, Any]] | None:
    phase3 = run.get("phase3Verdicts")
    if not isinstance(phase3, dict):
        return None
    by_category = phase3.get("byCategory")
    if not isinstance(by_category, dict) or not by_category:
        return None
    return {
        category: counts
        for category, counts in by_category.items()
        if isinstance(category, str) and isinstance(counts, dict)
    }


def _legacy_category_counts(run: dict[str, Any]) -> dict[str, dict[str, int]]:
    finding_counts = run.get("findingCounts")
    if not isinstance(finding_counts, dict):
        return {}
    by_category = finding_counts.get("byCategory")
    if not isinstance(by_category, dict):
        return {}
    return {
        category: {"confirmed": count, "refuted": 0, "unresolvable": 0}
        for category, count in by_category.items()
        if isinstance(category, str) and _valid_count(count) == count
    }


def _outcome_total(counts: dict[Any, Any]) -> int:
    return sum(_valid_count(counts.get(verdict)) for verdict in ("confirmed", "refuted", "unresolvable"))


def _valid_count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _category_annotation_rate(
    runs: list[dict[str, Any]],
    annotations: list[dict[str, Any]],
) -> dict[str, int]:
    """Count user annotations by verdict type."""
    counts: dict[str, int] = {"true-positive": 0, "false-positive": 0}
    for a in annotations:
        v = a.get("verdict", "")
        if v in counts:
            counts[v] += 1
    return counts


def _lens_yield(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per-lens finding yield: findings from lens / runs where lens was selected."""
    lens_runs: dict[str, int] = {}
    lens_findings: dict[str, int] = {}

    for run in runs:
        triage = run.get("triageSummary", {})
        for lens in triage.get("lensesRun", []):
            lens_runs[lens] = lens_runs.get(lens, 0) + 1

        counts = run.get("findingCounts", {})
        by_cat = counts.get("byCategory", {})
        for cat, count in by_cat.items():
            lens_findings[cat] = lens_findings.get(cat, 0) + count

    result: dict[str, dict[str, Any]] = {}
    for lens in sorted(lens_runs):
        r = lens_runs[lens]
        result[lens] = {
            "runs": r,
        }
    return result


def _catalogue_drift(runs: list[dict[str, Any]]) -> list[str]:
    """Categories with zero findings across all analyzed runs."""
    all_categories = {
        f"{prefix}{i}" for prefix, rng in [("A", range(1, 11)), ("B", range(1, 8)), ("C", range(1, 8))] for i in rng
    }

    seen: set[str] = set()
    for run in runs:
        counts = run.get("findingCounts", {})
        by_cat = counts.get("byCategory", {})
        seen.update(by_cat.keys())

    return sorted(all_categories - seen)


def _triage_distribution(runs: list[dict[str, Any]]) -> dict[str, int]:
    """Distribution of changeType classifications."""
    dist: dict[str, int] = {}
    for run in runs:
        triage = run.get("triageSummary", {})
        ct = triage.get("changeType", "unknown")
        dist[ct] = dist.get(ct, 0) + 1
    return dist
