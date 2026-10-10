"""Tests for scripts/gaze_changed.py, the pull-request CRAP regression filter."""

import json
from pathlib import Path
from unittest.mock import patch

import gaze_changed
import pytest

pytestmark = pytest.mark.unit

_COMPARISON = {"new_function_threshold": 15.0}


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
        pytest.param(15.01, ["io/new.py"], ["io/new.py:3"], id="new-function-above-threshold-fails"),
        pytest.param(15.0, ["io/new.py"], [], id="new-function-at-threshold-passes"),
        pytest.param(40.0, ["other.py"], [], id="new-function-in-unchanged-file-passes"),
    ],
)
def test_changed_regressions_holds_untracked_functions_to_the_new_function_threshold(
    crap: float, changed: list[str], expected_locations: list[str]
) -> None:
    """Functions absent from the baseline (new, moved or renamed) fail above the threshold, as the full gate does."""
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
    assert failures[0].describe() == "io/new.py:3 build: new function CRAP 20.00 > threshold 15.00"


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


def _entry(function: str, crap: float, gaze_crap: float | None = None, package: str = "io.py") -> dict[str, object]:
    return {
        "target": {"package": package, "function": function, "receiver": None},
        "crap": crap,
        "gaze_crap": gaze_crap,
    }


def _baseline(*entries: dict[str, object]) -> str:
    return json.dumps({"summary": {}, "results": list(entries)})


def _scores(merged: dict[str, object]) -> dict[str, object]:
    results = merged["results"]
    assert isinstance(results, list)
    return {r["target"]["function"]: r["crap"] for r in results}


def _run_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proposed: str, gazepy: object) -> int:
    coverage = tmp_path / "coverage.json"
    coverage.write_text("{}", encoding="utf-8")
    (tmp_path / ".gaze").mkdir()
    (tmp_path / ".gaze" / "baseline.json").write_text(proposed, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with (
        patch("gaze_changed.merge_base", return_value="abc123"),
        patch("gaze_changed.changed_source_files", return_value=["contact/enrich.py"]),
        patch("gaze_changed.base_baseline", return_value=_baseline(_entry("kept", 5.0))) as base,
        patch("gaze_changed._gazepy_report", side_effect=gazepy),
    ):
        status = gaze_changed.main(["--coverprofile", str(coverage), "--base", "origin/main"])
    assert base.call_args.args == ("abc123",)
    return status


def test_main_fails_on_regression_in_changed_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    report = {"results": [_result("contact/enrich.py:10", "regression")], "comparison": _COMPARISON}

    status = _run_main(tmp_path, monkeypatch, _baseline(_entry("kept", 5.0)), lambda *_: report)

    assert status == 1
    output = capsys.readouterr().out
    assert "contact/enrich.py:10 enrich: CRAP 6.00 -> 12.00 (+6.00)" in output
    assert "only ratchets down" in output


def test_main_compares_against_the_ratcheted_baseline_and_reports_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A raised entry fails the check and gazepy still sees the base score, not the raised one."""
    seen: list[dict[str, object]] = []

    def gazepy(_coverage: Path, baseline: Path) -> dict[str, object]:
        seen.append(json.loads(baseline.read_text(encoding="utf-8")))
        return {"results": [], "comparison": _COMPARISON}

    status = _run_main(tmp_path, monkeypatch, _baseline(_entry("kept", 9.0)), gazepy)

    assert status == 1
    assert _scores(seen[0]) == {"kept": 5.0}
    assert ".gaze/baseline.json io.py:kept: crap raised 5.00 -> 9.00" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("base", "proposed", "expected_scores", "expected_violations"),
    [
        pytest.param(
            [_entry("f", 5.0)],
            [_entry("f", 5.0), _entry("moved", 7.0)],
            {"f": 5.0, "moved": 7.0},
            [],
            id="added-entry-is-tracked",
        ),
        pytest.param([_entry("f", 5.0)], [_entry("f", 3.0)], {"f": 3.0}, [], id="lowered-score-is-held"),
        pytest.param(
            [_entry("f", 5.0)],
            [_entry("f", 6.0)],
            {"f": 5.0},
            ["io.py:f: crap raised 5.00 -> 6.00"],
            id="raised-score-is-rejected",
        ),
        pytest.param(
            [_entry("f", 5.0, 8.0)],
            [_entry("f", 5.0, 9.0)],
            {"f": 5.0},
            ["io.py:f: gaze_crap raised 8.00 -> 9.00"],
            id="raised-gaze-crap-is-rejected",
        ),
        pytest.param(
            [_entry("f", 5.0, 8.0)],
            [_entry("f", 5.0)],
            {"f": 5.0},
            ["io.py:f: gaze_crap 8.00 removed"],
            id="removed-gaze-crap-is-rejected",
        ),
        pytest.param([_entry("f", 5.0)], [_entry("f", 5.0, 4.0)], {"f": 5.0}, [], id="added-gaze-crap-is-tracked"),
        pytest.param(
            [_entry("f", 5.0), _entry("gone", 4.0)],
            [_entry("f", 5.0)],
            {"f": 5.0, "gone": 4.0},
            [],
            id="dropped-entry-keeps-base-for-comparison",
        ),
    ],
)
def test_ratchet_baseline_accepts_only_tightening_edits(
    base: list[dict[str, object]],
    proposed: list[dict[str, object]],
    expected_scores: dict[str, float],
    expected_violations: list[str],
) -> None:
    merged, violations = gaze_changed.ratchet_baseline(_baseline(*base), _baseline(*proposed))

    assert _scores(merged) == expected_scores
    assert [f"{v.key}: {v.detail}" for v in violations] == expected_violations


def test_ratchet_baseline_compares_same_named_functions_by_their_highest_score() -> None:
    """gazepy keys a few same-named functions in one file alike; a raise on either is caught."""
    base = _baseline(_entry("query", 4.0), _entry("query", 9.0))

    _, accepted = gaze_changed.ratchet_baseline(base, _baseline(_entry("query", 9.0), _entry("query", 3.0)))
    _, raised = gaze_changed.ratchet_baseline(base, _baseline(_entry("query", 4.0), _entry("query", 11.0)))

    assert accepted == []
    assert raised == [gaze_changed.BaselineViolation("io.py:query", "crap raised 9.00 -> 11.00")]


def test_ratchet_baseline_rejects_a_baseline_without_results() -> None:
    with pytest.raises(ValueError, match="proposed baseline has no 'results' list"):
        gaze_changed.ratchet_baseline(_baseline(), json.dumps({"summary": {}}))


@pytest.mark.parametrize(
    ("report_key", "expected"),
    [
        pytest.param("results", ["io.py:live: entry removed while the function still exists"], id="tracked-function"),
        pytest.param(
            "new_functions", ["io.py:live: entry removed while the function still exists"], id="untracked-function"
        ),
        pytest.param(None, [], id="deleted-function"),
    ],
)
def test_dropped_entries_flags_only_functions_that_still_exist(report_key: str | None, expected: list[str]) -> None:
    report: dict[str, object] = {"results": [], "new_functions": []}
    if report_key is not None:
        report[report_key] = [{"target": {"package": "io.py", "function": "live", "receiver": None}}]

    dropped = gaze_changed.dropped_entries(
        _baseline(_entry("live", 4.0), _entry("kept", 2.0)), _baseline(_entry("kept", 2.0)), report
    )

    assert [v.describe().removeprefix(".gaze/baseline.json ") for v in dropped] == expected


@pytest.mark.parametrize(
    ("crap", "expected"),
    [
        pytest.param(15.0, [], id="at-threshold-is-allowed"),
        pytest.param(
            15.01, ["io.py:new: entry added at CRAP 15.01 > threshold 15.00"], id="above-threshold-is-rejected"
        ),
    ],
)
def test_added_above_threshold_keeps_new_functions_under_the_threshold(crap: float, expected: list[str]) -> None:
    """Recording a new function's score cannot exempt it from the new-function threshold."""
    added = gaze_changed.added_above_threshold(
        _baseline(_entry("old", 40.0)), _baseline(_entry("old", 40.0), _entry("new", crap)), 15.0
    )

    assert [f"{v.key}: {v.detail}" for v in added] == expected


def test_main_rejects_a_new_function_recorded_above_the_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """gazepy sees the recorded function as tracked and unchanged, so only the ratchet catches it."""
    report = {"results": [_result("io.py:3", "unchanged", crap=42.0)], "comparison": _COMPARISON}
    proposed = _baseline(_entry("kept", 5.0), _entry("big", 42.0))

    status = _run_main(tmp_path, monkeypatch, proposed, lambda *_: report)

    assert status == 1
    assert ".gaze/baseline.json io.py:big: entry added at CRAP 42.00 > threshold 15.00" in capsys.readouterr().out


def test_score_key_qualifies_methods_with_their_receiver() -> None:
    assert (
        gaze_changed.score_key({"package": "io.py", "receiver": "Client", "function": "query"}) == "io.py:Client.query"
    )
    assert gaze_changed.score_key({"package": "io.py", "receiver": None, "function": "query"}) == "io.py:query"


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


def test_base_baseline_reads_the_committed_baseline_not_the_working_copy() -> None:
    """The ratchet's floor comes from the base revision, so a change cannot raise it."""
    with patch("gaze_changed.subprocess.run", return_value=_completed('{"results": []}')) as run:
        assert gaze_changed.base_baseline("abc123") == '{"results": []}'

    assert run.call_args.args[0] == ["git", "show", "abc123:.gaze/baseline.json"]


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
