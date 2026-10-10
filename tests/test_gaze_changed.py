"""Tests for scripts/gaze_changed.py, the pull-request CRAP regression filter."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

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


def _target(function: str, package: str = "io.py") -> dict[str, object]:
    return {"package": package, "function": function, "receiver": None}


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


def _run_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    proposed: str,
    gazepy: object,
    changed: tuple[str, ...] = ("contact/enrich.py",),
) -> int:
    coverage = tmp_path / "coverage.json"
    coverage.write_text("{}", encoding="utf-8")
    (tmp_path / ".gaze").mkdir()
    (tmp_path / ".gaze" / "baseline.json").write_text(proposed, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with (
        patch("gaze_changed.merge_base", return_value="abc123"),
        patch("gaze_changed.changed_source_files", return_value=list(changed)),
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

    def gazepy(_coverage: Path, baseline: Path, _measure_contracts: bool) -> dict[str, object]:
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


@pytest.mark.parametrize(
    ("proposed", "expected"),
    [
        pytest.param([3.0, 9.0], [], id="lowered-first-is-accepted"),
        pytest.param([4.5, 3.0], ["io.py:query (#1): crap raised 4.00 -> 4.50"], id="raised-first-hidden-by-lower-max"),
        pytest.param([4.0, 9.0, 12.0], [], id="third-namesake-is-an-addition"),
    ],
)
def test_ratchet_baseline_pairs_same_named_functions_by_position(proposed: list[float], expected: list[str]) -> None:
    """gazepy matches same-key entries one-to-one in order, so each pair is ratcheted on its own."""
    base = _baseline(_entry("query", 4.0), _entry("query", 9.0))

    _, violations = gaze_changed.ratchet_baseline(base, _baseline(*(_entry("query", s) for s in proposed)))

    assert [f"{v.key}: {v.detail}" for v in violations] == expected


@pytest.mark.parametrize(
    ("base_entry", "proposed_entry", "expected"),
    [
        pytest.param(
            _entry("f", 5.0, 8.0),
            _entry("f", 5.0, 0.0),
            ["io.py:f: gaze_crap 8.00 removed"],
            id="zero-gaze-disables-the-gate",
        ),
        pytest.param(_entry("f", 5.0, 0.0), _entry("f", 5.0, 30.0), [], id="ungated-gaze-may-be-set"),
        pytest.param(
            {**_entry("f", 5.0), "crap": None},
            _entry("f", 2.0),
            ["io.py:f: crap raised 0.00 -> 2.00"],
            id="missing-crap-scores-zero",
        ),
        pytest.param(_entry("f", 5.0), {**_entry("f", 5.0), "crap": None}, [], id="clearing-crap-tightens"),
    ],
)
def test_ratchet_baseline_follows_gazepys_score_defaults(
    base_entry: dict[str, object], proposed_entry: dict[str, object], expected: list[str]
) -> None:
    _, violations = gaze_changed.ratchet_baseline(_baseline(base_entry), _baseline(proposed_entry))

    assert [f"{v.key}: {v.detail}" for v in violations] == expected


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "7"])
@pytest.mark.parametrize("origin", ["base", "proposed"])
def test_ratchet_baseline_rejects_non_finite_or_non_numeric_scores(value: object, origin: str) -> None:
    """NaN compares false both ways, so it would slip past every rise and regression check."""
    bad = _baseline({**_entry("f", 5.0), "crap": value})
    good = _baseline(_entry("f", 5.0))
    base, proposed = (bad, good) if origin == "base" else (good, bad)

    with pytest.raises(ValueError, match=f"{origin} baseline: non-numeric or non-finite CRAP value"):
        gaze_changed.ratchet_baseline(base, proposed)


@pytest.mark.parametrize(
    ("live", "proposed", "expected"),
    [
        pytest.param(
            2, 1, ["io.py:enqueue: 1 of 2 entries removed while the functions still exist"], id="one-of-two-live"
        ),
        pytest.param(1, 1, [], id="namesake-deleted"),
        pytest.param(2, 2, [], id="both-kept"),
    ],
)
def test_dropped_entries_counts_same_named_functions(live: int, proposed: int, expected: list[str]) -> None:
    target = {"package": "io.py", "function": "enqueue", "receiver": None}
    report = {"results": [{"target": target}] * live}
    base = _baseline(_entry("enqueue", 3.21), _entry("enqueue", 3.33))

    dropped = gaze_changed.dropped_entries(base, _baseline(*[_entry("enqueue", 3.0)] * proposed), report)

    assert [f"{v.key}: {v.detail}" for v in dropped] == expected


def test_added_above_threshold_treats_an_extra_namesake_as_an_addition() -> None:
    added = gaze_changed.added_above_threshold(
        _baseline(_entry("query", 4.0)), _baseline(_entry("query", 4.0), _entry("query", 30.0)), 15.0
    )

    assert [f"{v.key}: {v.detail}" for v in added] == ["io.py:query (#2): entry added at CRAP 30.00 > threshold 15.00"]


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


def test_changed_regressions_counts_edited_entries_outside_changed_files() -> None:
    """A score lowered below the current measurement fails here, not in scheduled enforcement after merge."""
    target = {"location": "watch/repair.py:40", "package": "watch/repair.py", "function": "repair", "receiver": None}
    result = {"target": target, "status": "regression", "crap": 10.0, "baseline_crap": 5.0}

    edited = gaze_changed.changed_regressions({"results": [result]}, [], ["watch/repair.py:repair"])
    untouched = gaze_changed.changed_regressions({"results": [result]}, [], ["watch/other.py:repair"])

    assert [r.location for r in edited] == ["watch/repair.py:40"]
    assert untouched == []


def test_edited_keys_names_added_removed_and_rescored_entries() -> None:
    base = _baseline(_entry("same", 2.0), _entry("lowered", 5.0), _entry("removed", 3.0))
    proposed = _baseline(_entry("same", 2.0), _entry("lowered", 4.0), _entry("added", 1.0))

    assert gaze_changed.edited_keys(base, proposed) == {"io.py:lowered", "io.py:removed", "io.py:added"}


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param("not-an-object", id="non-object"),
        pytest.param({"crap": 1.0}, id="missing-target"),
        pytest.param({"target": "io.py:f", "crap": 1.0}, id="non-object-target"),
        pytest.param({"target": {"function": "f"}, "crap": 1.0}, id="missing-package"),
        pytest.param({"target": {"package": "io.py"}, "crap": 1.0}, id="missing-function"),
    ],
)
def test_ratchet_baseline_rejects_entries_gazepy_cannot_load(entry: object) -> None:
    """Filtering them would pass a file the scheduled gazepy run then refuses to read."""
    proposed = json.dumps({"results": [_entry("f", 1.0), entry]})

    with pytest.raises(ValueError, match="proposed baseline entry 1 needs a target with package and function"):
        gaze_changed.ratchet_baseline(_baseline(_entry("f", 1.0)), proposed)


@pytest.mark.parametrize(
    ("proposed", "expected"),
    [
        pytest.param(_baseline(_entry("f", 5.0, 8.0)), False, id="unchanged"),
        pytest.param(_baseline(_entry("f", 4.0, 8.0)), False, id="crap-only-edit"),
        pytest.param(_baseline(_entry("f", 5.0, 6.0)), True, id="lowered-gaze"),
        pytest.param(_baseline(_entry("f", 5.0, 8.0), _entry("g", 2.0, 3.0)), True, id="added-entry-with-gaze"),
        pytest.param(_baseline(_entry("f", 5.0, 8.0), _entry("g", 2.0)), False, id="added-entry-without-gaze"),
        pytest.param(_baseline(), False, id="removed-entry"),
    ],
)
def test_gaze_scores_edited_detects_scores_only_contract_analysis_can_verify(proposed: str, expected: bool) -> None:
    assert gaze_changed.gaze_scores_edited(_baseline(_entry("f", 5.0, 8.0)), proposed) is expected


@pytest.mark.parametrize(("measure", "expected_tests"), [(True, "tests"), (False, None)])
def test_gazepy_report_measures_contracts_only_when_asked(
    tmp_path: Path, measure: bool, expected_tests: str | None
) -> None:
    completed = gaze_changed.subprocess.CompletedProcess(args=[], returncode=0, stdout='{"results": []}', stderr="")

    with patch("gaze_changed.subprocess.run", return_value=completed) as run:
        gaze_changed._gazepy_report(tmp_path / "coverage.json", tmp_path / "baseline.json", measure)

    command = run.call_args.args[0]
    tests = command[command.index("--tests") + 1]
    assert tests == expected_tests if expected_tests else tests != "tests"


def test_changed_regressions_names_a_gaze_crap_regression() -> None:
    target = {"location": "io.py:9", "package": "io.py", "function": "f", "receiver": None}
    result = {"target": target, "status": "regression", "crap": 5.0, "baseline_crap": 5.0}
    result |= {"gaze_crap": 8.0, "gaze_crap_delta": 2.0}

    regressions = gaze_changed.changed_regressions({"results": [result]}, [], ["io.py:f"])

    assert [r.describe() for r in regressions] == ["io.py:9 f: GazeCRAP 6.00 -> 8.00 (+2.00)"]


def test_changed_regressions_matches_an_edited_legacy_method_key() -> None:
    """gazepy falls back to the bare package:function key for methods, so an edit there counts too."""
    target = {"location": "io.py:9", "package": "io.py", "function": "query", "receiver": "Client"}
    result = {"target": target, "status": "regression", "crap": 6.0, "baseline_crap": 2.0}

    regressions = gaze_changed.changed_regressions({"results": [result]}, [], ["io.py:query"])

    assert [r.location for r in regressions] == ["io.py:9"]


@pytest.mark.parametrize(
    ("base_entries", "expected"),
    [
        pytest.param(
            [_entry("query", 4.0)], ["io.py:query: entry removed while the function still exists"], id="bare-fallback"
        ),
        pytest.param(
            [_entry("query", 4.0), {**_entry("query", 6.0), "target": {**_target("query"), "receiver": "Client"}}],
            [],
            id="qualified-match-leaves-the-module-function-deletable",
        ),
    ],
)
def test_dropped_entries_counts_methods_under_gazepys_fallback_key(
    base_entries: list[dict[str, object]], expected: list[str]
) -> None:
    """A live method matched by its pre-0.9.1 bare key keeps that entry; one matched qualified does not."""
    method = {"target": {**_target("query"), "receiver": "Client"}}
    kept = [e for e in base_entries if isinstance(e["target"], dict) and e["target"].get("receiver")]

    dropped = gaze_changed.dropped_entries(_baseline(*base_entries), _baseline(*kept), {"results": [method]})

    assert [f"{v.key}: {v.detail}" for v in dropped] == expected


def test_score_key_qualifies_methods_with_their_receiver() -> None:
    assert (
        gaze_changed.score_key({"package": "io.py", "receiver": "Client", "function": "query"}) == "io.py:Client.query"
    )
    assert gaze_changed.score_key({"package": "io.py", "receiver": None, "function": "query"}) == "io.py:query"


def test_main_skips_gazepy_when_neither_source_nor_baseline_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gazepy = MagicMock()

    status = _run_main(tmp_path, monkeypatch, _baseline(_entry("kept", 5.0)), gazepy, changed=())

    assert status == 0
    gazepy.assert_not_called()
    assert "nothing to check" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("proposed", "report", "expected_status", "expected_line"),
    [
        pytest.param(
            _baseline(_entry("kept", 4.0)),
            {"results": [], "comparison": _COMPARISON},
            0,
            "PASS — no CRAP regressions in 0 changed file(s) and .gaze/baseline.json",
            id="lowered-score-passes",
        ),
        pytest.param(
            _baseline(),
            {
                "results": [{"target": {"package": "io.py", "function": "kept", "receiver": None}}],
                "comparison": _COMPARISON,
            },
            1,
            ".gaze/baseline.json io.py:kept: entry removed while the function still exists",
            id="dropped-live-entry-fails",
        ),
        pytest.param(
            _baseline(_entry("kept", 5.0), _entry("big", 30.0)),
            {"results": [], "comparison": _COMPARISON},
            1,
            ".gaze/baseline.json io.py:big: entry added at CRAP 30.00 > threshold 15.00",
            id="oversized-addition-fails",
        ),
    ],
)
def test_main_checks_a_baseline_only_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    proposed: str,
    report: dict[str, object],
    expected_status: int,
    expected_line: str,
) -> None:
    """A pull request that edits only the baseline cannot loosen it unchecked."""
    status = _run_main(tmp_path, monkeypatch, proposed, lambda *_: report, changed=())

    assert status == expected_status
    assert expected_line in capsys.readouterr().out


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
