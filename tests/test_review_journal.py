"""Tests for fieldkit.review.journal — append-only review run journal."""

import json
from pathlib import Path
from typing import Any

import pytest

from fieldkit.review.journal import (
    _count_phase3_verdicts,
    append_annotation,
    append_entry,
    journal_path,
    read_entries,
)


@pytest.mark.unit
class TestAppendEntry:
    def _make_report(self, **overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "runId": "test-run-001",
            "catalogueVersion": "1.0",
            "triageSummary": {
                "changeType": "feature",
                "size": "moderate",
                "lensesRun": ["correctness", "security"],
                "lensesSkipped": [],
            },
            "findings": [
                {
                    "id": "F1",
                    "severity": "HIGH",
                    "cls": "CODE_FRAGILITY",
                    "category": "A4",
                    "lens": "correctness",
                    "file": "src/fieldkit/sf/client.py",
                    "line": 42,
                    "summary": "Unguarded index access on possibly-empty list",
                    "failureScenario": "Empty SOSL response → IndexError",
                    "verdict": "CONFIRMED",
                },
            ],
            "tokenEstimate": 15000,
        }
        base.update(overrides)
        return base

    def test_writes_valid_jsonl(self, tmp_path: Path) -> None:
        report = self._make_report()
        append_entry(tmp_path, report=report, mode="full")

        lines = journal_path(tmp_path).read_text().strip().splitlines()
        assert len(lines) == 1

        entry = json.loads(lines[0])
        assert entry["type"] == "run"
        assert entry["runId"] == "test-run-001"
        assert entry["mode"] == "full"
        assert entry["catalogueVersion"] == "1.0"
        assert "timestamp" in entry

    def test_finding_counts_by_severity(self, tmp_path: Path) -> None:
        report = self._make_report()
        append_entry(tmp_path, report=report, mode="full")

        entry = json.loads(journal_path(tmp_path).read_text().strip())
        counts = entry["findingCounts"]
        assert counts["bySeverity"]["HIGH"] == 1
        assert counts["byCategory"]["A4"] == 1
        assert counts["byClass"]["CODE_FRAGILITY"] == 1

    def test_preserves_phase3_outcomes_dropped_from_rendered_findings(self, tmp_path: Path) -> None:
        """Journal preserves raw Phase-3 outcomes absent from the rendered report."""
        report = self._make_report(
            phase3Verdicts={
                "confirmed": 1,
                "refuted": 2,
                "unresolvable": 1,
                "byCategory": {
                    "A4": {"confirmed": 1, "refuted": 1, "unresolvable": 0},
                    "B2": {"confirmed": 0, "refuted": 1, "unresolvable": 1},
                },
            }
        )

        append_entry(tmp_path, report=report, mode="full")

        entry = json.loads(journal_path(tmp_path).read_text().strip())
        assert entry["phase3Verdicts"] == report["phase3Verdicts"]

    def test_phase3_outcomes_default_to_empty_aggregate(self) -> None:
        """Missing Phase-3 outcome evidence serializes as an empty aggregate."""
        counts = _count_phase3_verdicts({})

        assert counts == {
            "confirmed": 0,
            "refuted": 0,
            "unresolvable": 0,
            "byCategory": {},
        }

    def test_phase3_outcomes_ignore_malformed_counts_and_categories(self) -> None:
        """Phase-3 outcome parsing ignores malformed counts and category records."""
        counts = _count_phase3_verdicts(
            {
                "phase3Verdicts": {
                    "confirmed": True,
                    "refuted": -1,
                    "unresolvable": "one",
                    "byCategory": {
                        "A4": {"confirmed": 1, "refuted": None, "unresolvable": 0},
                        "B2": "invalid",
                        3: {"confirmed": 1},
                    },
                }
            }
        )

        assert counts == {
            "confirmed": 0,
            "refuted": 0,
            "unresolvable": 0,
            "byCategory": {"A4": {"confirmed": 1, "refuted": 0, "unresolvable": 0}},
        }

    def test_records_pr_identifier(self, tmp_path: Path) -> None:
        append_entry(
            tmp_path,
            report=self._make_report(),
            mode="full",
            pr_identifier="#1445",
        )

        entry = json.loads(journal_path(tmp_path).read_text().strip())
        assert entry["prIdentifier"] == "#1445"

    def test_fire_and_forget_on_permission_error(self, tmp_path: Path) -> None:
        readonly_dir = tmp_path / "readonly"
        readonly_dir.mkdir()
        readonly_dir.chmod(0o444)

        append_entry(readonly_dir, report=self._make_report(), mode="full")

        readonly_dir.chmod(0o755)

    def test_multiple_appends_accumulate(self, tmp_path: Path) -> None:
        for i in range(3):
            append_entry(
                tmp_path,
                report=self._make_report(runId=f"run-{i}"),
                mode="full",
            )

        lines = journal_path(tmp_path).read_text().strip().splitlines()
        assert len(lines) == 3
        run_ids = [json.loads(line)["runId"] for line in lines]
        assert run_ids == ["run-0", "run-1", "run-2"]


@pytest.mark.unit
class TestAppendAnnotation:
    def test_writes_annotation_line(self, tmp_path: Path) -> None:
        append_annotation(
            tmp_path,
            run_id="run-001",
            finding_id="F1",
            verdict="false-positive",
        )

        entries = read_entries(tmp_path)
        assert len(entries) == 1
        assert entries[0]["type"] == "annotation"
        assert entries[0]["runId"] == "run-001"
        assert entries[0]["findingId"] == "F1"
        assert entries[0]["verdict"] == "false-positive"

    def test_annotation_linked_to_run(self, tmp_path: Path) -> None:
        report: dict[str, Any] = {
            "runId": "run-002",
            "findings": [],
        }
        append_entry(tmp_path, report=report, mode="quick")
        append_annotation(
            tmp_path,
            run_id="run-002",
            finding_id="F1",
            verdict="true-positive",
        )

        entries = read_entries(tmp_path)
        runs = [e for e in entries if e["type"] == "run"]
        annotations = [e for e in entries if e["type"] == "annotation"]
        assert len(runs) == 1
        assert len(annotations) == 1
        assert annotations[0]["runId"] == runs[0]["runId"]


@pytest.mark.unit
class TestReadEntries:
    def test_empty_when_no_file(self, tmp_path: Path) -> None:
        entries = read_entries(tmp_path)
        assert entries == []

    def test_skips_malformed_lines(self, tmp_path: Path) -> None:
        jpath = journal_path(tmp_path)
        jpath.parent.mkdir(parents=True, exist_ok=True)
        jpath.write_text('{"type":"run","runId":"ok"}\nnot valid json\n{"type":"annotation","runId":"also-ok"}\n')

        entries = read_entries(tmp_path)
        assert len(entries) == 2
        assert entries[0]["runId"] == "ok"
        assert entries[1]["runId"] == "also-ok"
