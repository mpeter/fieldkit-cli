"""Tests for scripts/gaze_changed.py, the pull-request CRAP regression filter."""

from pathlib import Path
from unittest.mock import patch

import gaze_changed
import pytest

pytestmark = pytest.mark.unit


def _result(location: str, status: str, crap: float = 12.0, baseline: float = 6.0) -> dict[str, object]:
    return {
        "target": {"location": location, "function": location.split(":")[0].rsplit("/", 1)[-1][:-3]},
        "status": status,
        "crap": crap,
        "baseline_crap": baseline,
    }


@pytest.mark.parametrize(
    ("result", "changed", "expected_locations"),
    [
        pytest.param(
            _result("contact/enrich.py:10", "regression"),
            ["contact/enrich.py"],
            ["contact/enrich.py:10"],
            id="regression-in-changed-file-fails",
        ),
        pytest.param(
            _result("contact/enrich.py:10", "regression"),
            ["pursuit/io.py"],
            [],
            id="regression-in-unchanged-file-passes",
        ),
        pytest.param(
            _result("contact/enrich.py:10", "improvement", 3.0), ["contact/enrich.py"], [], id="improvement-passes"
        ),
        pytest.param(_result("contact/enrich.py:10", "new"), ["contact/enrich.py"], [], id="new-function-passes"),
        pytest.param(_result("contact/enrich.py:10", "regression"), [], [], id="empty-change-list-passes"),
        pytest.param(
            _result("contact/enrich.py.bak:10", "regression"), ["contact/enrich.py"], [], id="file-match-is-exact"
        ),
    ],
)
def test_changed_regressions_keeps_only_regressions_in_changed_files(
    result: dict[str, object], changed: list[str], expected_locations: list[str]
) -> None:
    regressions = gaze_changed.changed_regressions({"results": [result]}, changed)

    assert [r.location for r in regressions] == expected_locations


@pytest.mark.parametrize(
    ("crap", "changed", "expected_locations"),
    [
        pytest.param(15.0, ["io/new.py"], ["io/new.py:3"], id="new-function-at-threshold-fails"),
        pytest.param(14.9, ["io/new.py"], [], id="new-function-below-threshold-passes"),
        pytest.param(40.0, ["other.py"], [], id="new-function-in-unchanged-file-passes"),
    ],
)
def test_changed_regressions_holds_untracked_functions_to_the_new_function_threshold(
    crap: float, changed: list[str], expected_locations: list[str]
) -> None:
    """Functions absent from the baseline (new, moved or renamed) fail at the threshold, as the full gate does."""
    report = {
        "results": [],
        "new_functions": [{"target": {"location": "io/new.py:3", "function": "build"}, "status": "new", "crap": crap}],
        "comparison": {"new_function_threshold": 15.0},
    }

    failures = gaze_changed.changed_regressions(report, changed)

    assert [f.location for f in failures] == expected_locations


def test_new_function_failure_description_names_the_threshold() -> None:
    report = {
        "results": [],
        "new_functions": [{"target": {"location": "io/new.py:3", "function": "build"}, "status": "new", "crap": 20.0}],
        "comparison": {"new_function_threshold": 15.0},
    }

    failures = gaze_changed.changed_regressions(report, ["io/new.py"])

    assert failures == [gaze_changed.Regression("io/new.py:3", "build", 20.0, threshold=15.0)]
    assert failures[0].describe() == "io/new.py:3 build: new function CRAP 20.00 >= threshold 15.00"


def test_changed_regressions_requires_the_threshold_when_new_functions_exist() -> None:
    report = {"results": [], "new_functions": [{"target": {"location": "a.py:1", "function": "f"}, "crap": 1.0}]}

    with pytest.raises(ValueError, match="new_function_threshold"):
        gaze_changed.changed_regressions(report, ["a.py"])


def test_regression_description_names_function_and_delta() -> None:
    regressions = gaze_changed.changed_regressions(
        {"results": [_result("watch/repair.py:40", "regression", crap=18.5, baseline=11.0)]}, ["watch/repair.py"]
    )

    assert regressions == [gaze_changed.Regression("watch/repair.py:40", "repair", 18.5, baseline_crap=11.0)]
    assert regressions[0].describe() == "watch/repair.py:40 repair: CRAP 11.00 -> 18.50 (+7.50)"


def test_changed_regressions_rejects_a_report_without_results() -> None:
    with pytest.raises(ValueError, match="results"):
        gaze_changed.changed_regressions({"summary": {}}, ["a.py"])


def test_main_fails_on_regression_in_changed_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    coverage = tmp_path / "coverage.json"
    coverage.write_text("{}", encoding="utf-8")
    report = {"results": [_result("contact/enrich.py:10", "regression")]}

    with (
        patch("gaze_changed.merge_base", return_value="abc123"),
        patch("gaze_changed.changed_source_files", return_value=["contact/enrich.py"]),
        patch("gaze_changed.write_base_baseline") as write_baseline,
        patch("gaze_changed._gazepy_report", return_value=report) as gazepy,
    ):
        status = gaze_changed.main(["--coverprofile", str(coverage), "--base", "origin/main"])

    assert status == 1
    output = capsys.readouterr().out
    assert "contact/enrich.py:10 enrich: CRAP 6.00 -> 12.00 (+6.00)" in output
    assert "does not clear a failure" in output
    assert write_baseline.call_args.args[0] == "abc123"
    assert gazepy.call_args.args[1] == write_baseline.call_args.args[1]


def test_main_skips_gazepy_when_no_source_changed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    coverage = tmp_path / "coverage.json"
    coverage.write_text("{}", encoding="utf-8")

    with (
        patch("gaze_changed.merge_base", return_value="abc123"),
        patch("gaze_changed.changed_source_files", return_value=[]),
        patch("gaze_changed._gazepy_report") as gazepy,
    ):
        status = gaze_changed.main(["--coverprofile", str(coverage), "--base", "origin/main"])

    assert status == 0
    gazepy.assert_not_called()
    assert "nothing to check" in capsys.readouterr().out


def test_main_reports_missing_coverage_as_invalid_input(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    status = gaze_changed.main(["--coverprofile", str(tmp_path / "absent.json"), "--base", "origin/main"])

    assert status == 3
    assert "coverage report not found" in capsys.readouterr().err


def _completed(stdout: str) -> object:
    return gaze_changed.subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def test_changed_source_files_includes_modified_and_untracked_python_under_src_root() -> None:
    """Unstaged new files count as changes, so a local run cannot skip a brand-new function."""
    tracked = "src/fieldkit/contact/enrich.py\nsrc/fieldkit/skills/brief/SKILL.md\nscripts/x.py\n"
    untracked = "src/fieldkit/contact/new_module.py\nsrc/fieldkit/contact/enrich.py\n"

    with patch("gaze_changed.subprocess.run", side_effect=[_completed(tracked), _completed(untracked)]) as run:
        files = gaze_changed.changed_source_files("abc123")

    assert files == ["contact/enrich.py", "contact/new_module.py"]
    assert run.call_args_list[0].args[0][:5] == ["git", "diff", "--name-only", "--diff-filter=AMR", "abc123"]
    assert run.call_args_list[1].args[0][:4] == ["git", "ls-files", "--others", "--exclude-standard"]
    assert all(call.kwargs["timeout"] == gaze_changed._GIT_TIMEOUT_SECONDS for call in run.call_args_list)


def test_merge_base_asks_git_for_the_divergence_point() -> None:
    with patch("gaze_changed.subprocess.run", return_value=_completed("abc123\n")) as run:
        assert gaze_changed.merge_base("origin/main") == "abc123"

    assert run.call_args.args[0] == ["git", "merge-base", "origin/main", "HEAD"]


def test_write_base_baseline_reads_the_committed_baseline_not_the_working_copy(tmp_path: Path) -> None:
    """A change cannot clear its own regression by raising its local baseline entry."""
    destination = tmp_path / "baseline.json"

    with patch("gaze_changed.subprocess.run", return_value=_completed('{"results": []}')) as run:
        gaze_changed.write_base_baseline("abc123", destination)

    assert run.call_args.args[0] == ["git", "show", "abc123:.gaze/baseline.json"]
    assert destination.read_text(encoding="utf-8") == '{"results": []}'


@pytest.mark.parametrize(("returncode", "accepted"), [(0, True), (1, True), (2, False)])
def test_gazepy_report_accepts_a_failed_comparison_but_not_a_crash(
    tmp_path: Path, returncode: int, accepted: bool
) -> None:
    """gazepy exits 1 with a full report when any comparison fails; only other exits are errors."""
    completed = gaze_changed.subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout='{"results": []}', stderr="boom"
    )
    baseline = tmp_path / "base-baseline.json"

    with patch("gaze_changed.subprocess.run", return_value=completed) as run:
        if accepted:
            assert gaze_changed._gazepy_report(tmp_path / "coverage.json", baseline) == {"results": []}
        else:
            with pytest.raises(ValueError, match="gazepy exited 2"):
                gaze_changed._gazepy_report(tmp_path / "coverage.json", baseline)

    assert run.call_args.kwargs["timeout"] == gaze_changed._GAZEPY_TIMEOUT_SECONDS
    command = run.call_args.args[0]
    assert command[command.index("--tests") + 1] != "tests"
    assert command[command.index("--baseline") + 1] == str(baseline)
