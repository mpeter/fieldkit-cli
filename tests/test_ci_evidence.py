"""Contracts for bounded, revision-attributable CI evidence."""

import json
from pathlib import Path

import pytest

from scripts import ci_evidence

pytestmark = pytest.mark.unit

_REQUIRED_CHILDREN = (
    "Detect code changes",
    "Commit-message PII guard",
    "Lint (ruff)",
    "Test (pytest)",
    "Skillsaw (skill lint)",
    "AgentReady score gate",
)


def _children(**overrides: str) -> list[str]:
    return [f"{name}={overrides.get(name, 'success')}" for name in _REQUIRED_CHILDREN]


def _write_junit(path: Path, *, tests: int = 3, failures: int = 0, errors: int = 0, skipped: int = 1) -> None:
    failure = '<testcase classname="tests.test_demo" name="test_failure"><failure>private output</failure></testcase>'
    cases = failure if failures else '<testcase classname="tests.test_demo" name="test_success" />'
    path.write_text(
        (
            '<testsuites name="pytest tests">'
            f'<testsuite name="pytest" tests="{tests}" failures="{failures}" errors="{errors}" '
            f'skipped="{skipped}" time="1.25">{cases}</testsuite></testsuites>'
        ),
        encoding="utf-8",
    )


def _write_coverage(path: Path, *, percent: float = 92.5) -> None:
    path.write_text(
        json.dumps(
            {
                "meta": {"version": "7.10.0", "timestamp": "2026-09-11T01:02:03+00:00"},
                "totals": {
                    "covered_lines": 925,
                    "num_statements": 1000,
                    "percent_covered": percent,
                    "missing_lines": 75,
                    "excluded_lines": 10,
                },
            }
        ),
        encoding="utf-8",
    )


def test_junit_evidence_records_scope_revision_counts_and_bounded_failures(tmp_path: Path) -> None:
    """A failed test run is attributable without copying failure payloads into the report."""
    junit = tmp_path / "pytest.xml"
    output = tmp_path / "pytest-summary.json"
    summary = tmp_path / "step-summary.md"
    _write_junit(junit, failures=1)

    result = ci_evidence.main(
        [
            "junit",
            "--input",
            str(junit),
            "--output",
            str(output),
            "--step-summary",
            str(summary),
            "--source-revision",
            "a" * 40,
            "--scope",
            "tach-selected",
            "--command",
            "uv run pytest tests/ --tach",
        ]
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert result == 1
    assert report["status"] == "fail"
    assert report["source_revision"] == "a" * 40
    assert report["scope"] == "tach-selected"
    assert report["command"] == "uv run pytest tests/ --tach"
    assert report["tool"]["name"] == "pytest"
    assert report["counts"] == {"errors": 0, "failures": 1, "skipped": 1, "tests": 3}
    assert report["failures"] == ["tests.test_demo::test_failure"]
    assert "private output" not in output.read_text(encoding="utf-8")
    assert "private output" not in summary.read_text(encoding="utf-8")


def test_junit_evidence_rejects_missing_input(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Missing evidence fails closed with a payload-safe diagnostic."""
    missing = tmp_path / "missing.xml"
    output = tmp_path / "summary.json"

    result = ci_evidence.main(
        [
            "junit",
            "--input",
            str(missing),
            "--output",
            str(output),
            "--source-revision",
            "b" * 40,
            "--scope",
            "tach-selected",
            "--command",
            "pytest",
        ]
    )

    assert result == 2
    assert not output.exists()
    assert "missing.xml" in capsys.readouterr().err


def test_junit_evidence_rejects_oversized_input(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A contributor cannot make the summarizer consume an unbounded XML report."""
    junit = tmp_path / "oversized.xml"
    output = tmp_path / "summary.json"
    junit.write_bytes(b"x" * (ci_evidence.MAX_JUNIT_BYTES + 1))

    result = ci_evidence.main(
        [
            "junit",
            "--input",
            str(junit),
            "--output",
            str(output),
            "--source-revision",
            "b" * 40,
            "--scope",
            "tach-selected",
            "--command",
            "pytest",
        ]
    )

    assert result == 2
    assert not output.exists()
    assert "exceeds" in capsys.readouterr().err


def test_scope_labels_cannot_overstate_partial_or_full_evidence(tmp_path: Path) -> None:
    """The CLI cannot label selected tests as full coverage or full coverage as selected tests."""
    junit = tmp_path / "pytest.xml"
    coverage = tmp_path / "coverage.json"
    _write_junit(junit)
    _write_coverage(coverage)

    with pytest.raises(SystemExit) as junit_exit:
        ci_evidence.main(
            [
                "junit",
                "--input",
                str(junit),
                "--output",
                str(tmp_path / "junit.json"),
                "--source-revision",
                "b" * 40,
                "--scope",
                "full-repository",
                "--command",
                "pytest",
            ]
        )
    with pytest.raises(SystemExit) as coverage_exit:
        ci_evidence.main(
            [
                "coverage",
                "--input",
                str(coverage),
                "--output",
                str(tmp_path / "coverage-summary.json"),
                "--source-revision",
                "b" * 40,
                "--scope",
                "tach-selected",
                "--command",
                "pytest --cov",
                "--minimum-percent",
                "80",
            ]
        )

    assert junit_exit.value.code == 2
    assert coverage_exit.value.code == 2


@pytest.mark.parametrize(("percent", "expected_status", "expected_exit"), [(92.5, "pass", 0), (79.9, "fail", 1)])
def test_coverage_evidence_enforces_floor_and_records_full_scope(
    tmp_path: Path, percent: float, expected_status: str, expected_exit: int
) -> None:
    """The full-suite report keeps the existing 80 percent floor explicit."""
    coverage = tmp_path / "coverage.json"
    output = tmp_path / "coverage-summary.json"
    _write_coverage(coverage, percent=percent)

    result = ci_evidence.main(
        [
            "coverage",
            "--input",
            str(coverage),
            "--output",
            str(output),
            "--source-revision",
            "c" * 40,
            "--scope",
            "full-repository",
            "--command",
            "make quality-full",
            "--minimum-percent",
            "80",
        ]
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert result == expected_exit
    assert report["status"] == expected_status
    assert report["scope"] == "full-repository"
    assert report["tool"] == {"name": "coverage.py", "version": "7.10.0"}
    assert report["minimum_percent"] == 80.0


def test_required_checks_passes_only_when_every_bounded_child_succeeds(tmp_path: Path) -> None:
    """The aggregate treats every ordinary pull-request child as mandatory."""
    output = tmp_path / "required-checks.json"
    children = _children()

    result = ci_evidence.main(
        [
            "required",
            "--output",
            str(output),
            "--source-revision",
            "d" * 40,
            "--scope",
            "code",
            "--run-url",
            "https://example.com/actions/runs/123",
            *(argument for child in children for argument in ("--child", child)),
        ]
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "pass"
    assert [child["name"] for child in report["children"]] == [item.split("=", maxsplit=1)[0] for item in children]


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", "missing"])
def test_required_checks_rejects_incomplete_code_child(tmp_path: Path, result: str) -> None:
    """Failure, cancellation, absence, and an unexpected code-path skip all block the aggregate."""
    output = tmp_path / "required-checks.json"

    exit_code = ci_evidence.main(
        [
            "required",
            "--output",
            str(output),
            "--source-revision",
            "e" * 40,
            "--scope",
            "code",
            "--run-url",
            "https://example.com/actions/runs/456",
            *(argument for child in _children(**{"Test (pytest)": result}) for argument in ("--child", child)),
        ]
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert report["status"] == "fail"
    assert report["children"][3]["result"] == result


def test_required_checks_rejects_docs_only_skipped_child(tmp_path: Path) -> None:
    """A skipped ordinary pull-request child never becomes passing evidence."""
    output = tmp_path / "required-checks.json"
    common = [
        "--source-revision",
        "f" * 40,
        "--scope",
        "docs-only",
        "--run-url",
        "https://example.com/actions/runs/789",
    ]

    exit_code = ci_evidence.main(
        [
            "required",
            "--output",
            str(output),
            *common,
            *(argument for child in _children(**{"Test (pytest)": "skipped"}) for argument in ("--child", child)),
        ]
    )

    assert exit_code == 1
    assert json.loads(output.read_text(encoding="utf-8"))["children"][3]["result"] == "skipped"
