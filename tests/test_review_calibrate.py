"""Tests for fieldkit.review.calibrate — quality metrics from the review journal."""

import json
from pathlib import Path

import pytest

from fieldkit.review.calibrate import compute_metrics
from fieldkit.review.journal import journal_path


def _write_journal(tmp_path: Path, entries: list[dict]) -> None:  # type: ignore[type-arg]
    jpath = journal_path(tmp_path)
    jpath.parent.mkdir(parents=True, exist_ok=True)
    with jpath.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


def _make_run(
    run_id: str,
    change_type: str = "feature",
    lenses: list[str] | None = None,
    categories: dict[str, int] | None = None,
    phase3_verdicts: dict[str, object] | None = None,
) -> dict:  # type: ignore[type-arg]
    return {
        "type": "run",
        "runId": run_id,
        "timestamp": "2026-07-19T12:00:00Z",
        "triageSummary": {
            "changeType": change_type,
            "lensesRun": lenses or ["correctness"],
        },
        "findingCounts": {
            "bySeverity": {"HIGH": sum((categories or {}).values())},
            "byCategory": categories or {},
            "byClass": {},
        },
        "phase3Verdicts": phase3_verdicts or {"confirmed": 0, "refuted": 0, "unresolvable": 0, "byCategory": {}},
    }


@pytest.mark.unit
class TestComputeMetrics:
    def test_empty_journal(self, tmp_path: Path) -> None:
        result = compute_metrics(tmp_path)
        assert result["empty"] is True

    def test_single_run_basic_metrics(self, tmp_path: Path) -> None:
        _write_journal(
            tmp_path,
            [_make_run("r1", categories={"A4": 2, "B1": 1})],
        )

        result = compute_metrics(tmp_path)
        assert result["empty"] is False
        assert result["totalRuns"] == 1
        assert result["analyzedRuns"] == 1
        assert result["categoryFalsePositiveRate"]["A4"]["surfaced"] == 2
        assert result["categoryFalsePositiveRate"]["B1"]["surfaced"] == 1

    def test_category_false_positive_rate_uses_phase3_outcomes(self, tmp_path: Path) -> None:
        """Calibration counts refuted Phase-3 outcomes omitted from rendered findings."""
        _write_journal(
            tmp_path,
            [
                _make_run(
                    "r1",
                    categories={"A4": 1},
                    phase3_verdicts={
                        "confirmed": 1,
                        "refuted": 2,
                        "unresolvable": 1,
                        "byCategory": {"A4": {"confirmed": 1, "refuted": 2, "unresolvable": 1}},
                    },
                )
            ],
        )

        result = compute_metrics(tmp_path)
        rate = result["categoryFalsePositiveRate"]["A4"]
        assert rate == {"surfaced": 4, "refuted": 2, "rate": 0.5}

    def test_category_false_positive_rate_ignores_malformed_phase3_categories(self, tmp_path: Path) -> None:
        """Calibration fails closed on malformed Phase-3 category aggregates."""
        _write_journal(
            tmp_path,
            [
                _make_run(
                    "r1",
                    categories={"B1": 2},
                    phase3_verdicts={
                        "byCategory": {
                            "A4": {"confirmed": 1, "refuted": True, "unresolvable": -1},
                            "B1": "invalid",
                            3: {"confirmed": 1},
                        }
                    },
                )
            ],
        )

        result = compute_metrics(tmp_path)

        assert result["categoryFalsePositiveRate"] == {
            "3": {"surfaced": 1, "refuted": 0, "rate": 0.0},
            "A4": {"surfaced": 1, "refuted": 0, "rate": 0.0},
        }

    def test_triage_distribution(self, tmp_path: Path) -> None:
        _write_journal(
            tmp_path,
            [
                _make_run("r1", change_type="feature"),
                _make_run("r2", change_type="bugfix"),
                _make_run("r3", change_type="feature"),
            ],
        )

        result = compute_metrics(tmp_path)
        dist = result["triageDistribution"]
        assert dist["feature"] == 2
        assert dist["bugfix"] == 1

    def test_lens_yield_counts_runs_per_lens(self, tmp_path: Path) -> None:
        _write_journal(
            tmp_path,
            [
                _make_run("r1", lenses=["correctness", "security"]),
                _make_run("r2", lenses=["correctness"]),
                _make_run("r3", lenses=["security", "operations"]),
            ],
        )

        result = compute_metrics(tmp_path)
        lens_yield = result["lensYield"]
        assert lens_yield["correctness"]["runs"] == 2
        assert lens_yield["security"]["runs"] == 2
        assert lens_yield["operations"]["runs"] == 1

    def test_catalogue_drift_finds_unused_categories(self, tmp_path: Path) -> None:
        _write_journal(
            tmp_path,
            [_make_run("r1", categories={"A1": 1, "B1": 1, "C1": 1})],
        )

        result = compute_metrics(tmp_path)
        drift = result["catalogueDrift"]
        assert "A1" not in drift
        assert "B1" not in drift
        assert "C1" not in drift
        assert "A2" in drift
        assert "C7" in drift

    def test_last_n_limits_analyzed_window(self, tmp_path: Path) -> None:
        runs = [_make_run(f"r{i}") for i in range(10)]
        _write_journal(tmp_path, runs)

        result = compute_metrics(tmp_path, last_n=3)
        assert result["totalRuns"] == 10
        assert result["analyzedRuns"] == 3

    def test_annotation_rate(self, tmp_path: Path) -> None:
        entries: list[dict] = [  # type: ignore[type-arg]
            _make_run("r1"),
            {
                "type": "annotation",
                "runId": "r1",
                "findingId": "F1",
                "verdict": "true-positive",
            },
            {
                "type": "annotation",
                "runId": "r1",
                "findingId": "F2",
                "verdict": "false-positive",
            },
            {
                "type": "annotation",
                "runId": "r1",
                "findingId": "F3",
                "verdict": "true-positive",
            },
        ]
        _write_journal(tmp_path, entries)

        result = compute_metrics(tmp_path)
        rates = result["categoryAnnotationRate"]
        assert rates["true-positive"] == 2
        assert rates["false-positive"] == 1
