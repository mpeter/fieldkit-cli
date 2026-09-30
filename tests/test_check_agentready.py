"""Tests for scripts/check_agentready.py.

``scripts/`` is added to sys.path by conftest.py (line 20), so the module
can be imported directly as ``check_agentready``.

JSON_PATH is patched via monkeypatch.setattr so no real filesystem state is
required and tests are fully isolated.
"""

import json
from pathlib import Path

import agentready_policy
import check_agentready
import pytest


def _valid_report(score: float) -> dict[str, object]:
    """Represent every frozen floor and an explicit synthetic candidate."""
    findings = [
        {
            "attribute": {"id": name},
            "status": "not_applicable" if name in agentready_policy.NOT_APPLICABLE else "pass",
            "score": None if name in agentready_policy.NOT_APPLICABLE else score,
        }
        for name in agentready_policy.WEIGHTS
    ]
    return {
        "overall_score": score,
        "schema_version": agentready_policy.REPORT_SCHEMA_VERSION,
        "metadata": {"agentready_version": agentready_policy.TOOL_VERSION},
        "tier": "Gold",
        "repository": {"commit_hash": "a" * 40},
        "findings": findings,
        "attributes_total": len(findings),
        "attributes_assessed": len(findings) - len(agentready_policy.NOT_APPLICABLE),
        "attributes_skipped": len(agentready_policy.NOT_APPLICABLE),
    }


@pytest.fixture(autouse=True)
def isolate_candidate_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not depend on the developer checkout's current revision."""
    monkeypatch.setattr(check_agentready, "_current_head", lambda: "a" * 40)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("overall_score", True),
        ("overall_score", -1),
        ("overall_score", 101),
        ("overall_score", float("nan")),
        ("overall_score", float("inf")),
        ("findings", {}),
        ("findings", [None]),
        ("findings", [{"attribute": None}]),
        ("findings", [{"attribute": {"id": ""}}]),
        ("repository", None),
        ("repository", {"commit_hash": "b" * 40}),
        ("repository", {"commit_hash": "A" * 40}),
        ("repository", {"commit_hash": "short"}),
    ],
)
def test_invalid_complete_report_fails(field: str, value: object) -> None:
    """Invalid claims cannot be rescued by otherwise complete floor evidence."""
    report = _valid_report(100)
    report[field] = value

    with pytest.raises(ValueError, match=r"score|finding|attribute|repository|revision|identity"):
        check_agentready._validate_evidence(report)


@pytest.mark.unit
@pytest.mark.parametrize("score", [None, True, "100", "nan", float("nan"), float("inf"), -1, 101])
def test_invalid_attribute_score_fails(score: object) -> None:
    """Every applicable score must be a finite numeric percentage."""
    report = _valid_report(100)
    report["findings"] = [{"attribute": {"id": "inline_documentation"}, "status": "pass", "score": score}]

    with pytest.raises(ValueError, match="invalid finding status or score"):
        check_agentready._validate_evidence(report)


@pytest.mark.unit
def test_duplicate_attributes_fail() -> None:
    """Repeated claims have no defined winner."""
    report = _valid_report(100)
    finding = {"attribute": {"id": "inline_documentation"}, "status": "pass", "score": 100}
    report["findings"] = [finding, finding]

    with pytest.raises(ValueError, match="duplicate attribute"):
        check_agentready._validate_evidence(report)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("attr_id", "status", "score"),
    [("structured_logging", "fail", 0), ("container_setup", "not_applicable", None)],
)
def test_untracked_findings_remain_informational(attr_id: str, status: str, score: int | None) -> None:
    """Known optional scores remain informational rather than becoming floors."""
    report = _valid_report(100)
    findings = report["findings"]
    assert isinstance(findings, list)
    findings[:] = [finding for finding in findings if finding["attribute"]["id"] != attr_id]
    findings.append({"attribute": {"id": attr_id}, "status": status, "score": score})
    report["attributes_total"] = len(findings)
    skipped = sum(finding["status"] == "not_applicable" for finding in findings)
    report["attributes_skipped"] = skipped
    report["attributes_assessed"] = len(findings) - skipped
    report["overall_score"] = agentready_policy.aggregate_score(
        {finding["attribute"]["id"]: finding["score"] for finding in findings if finding["score"] is not None}
    )

    result = check_agentready._validate_evidence(report)

    assert result is None


@pytest.mark.unit
@pytest.mark.parametrize("attr_id", ["structured_logging", "container_setup"])
def test_applicability_cannot_be_changed_to_adjust_aggregate(attr_id: str) -> None:
    """Applicability is candidate policy, independent of local floor tracking."""
    report = _valid_report(100)
    findings = report["findings"]
    assert isinstance(findings, list)
    for finding in findings:
        if finding["attribute"]["id"] == attr_id:
            finding["status"] = "not_applicable" if attr_id == "structured_logging" else "pass"
            finding["score"] = None if attr_id == "structured_logging" else 100
    skipped = sum(finding["status"] == "not_applicable" for finding in findings)
    report["attributes_skipped"] = skipped
    report["attributes_assessed"] = len(findings) - skipped
    report["overall_score"] = agentready_policy.aggregate_score(
        {finding["attribute"]["id"]: finding["score"] for finding in findings if finding["score"] is not None}
    )

    with pytest.raises(ValueError, match="applicability"):
        check_agentready._validate_evidence(report)


@pytest.mark.unit
@pytest.mark.parametrize("field", ["attributes_total", "attributes_assessed", "attributes_skipped"])
@pytest.mark.parametrize("value", [None, True, -1, 500, "26", 26.0])
def test_inconsistent_summary_counts_fail(field: str, value: object) -> None:
    """Summary counts must be exact integers matching the findings."""
    report = _valid_report(100)
    report[field] = value

    with pytest.raises(ValueError, match="summary count"):
        check_agentready._validate_evidence(report)


@pytest.mark.unit
def test_one_missing_floor_fails() -> None:
    """Every frozen numeric floor is required, even when all others are present."""
    report = _valid_report(100)
    findings = report["findings"]
    assert isinstance(findings, list)
    findings[:] = [finding for finding in findings if finding["attribute"]["id"] != "test_execution"]

    with pytest.raises(ValueError, match="missing required attribute evidence"):
        check_agentready._validate_evidence(report)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "2.0.0"),
        ("metadata", {}),
        ("metadata", {"agentready_version": "2.52.3"}),
        ("overall_score", 99),
    ],
)
def test_pinned_report_and_aggregate_are_verified(field: str, value: object) -> None:
    """A valid-looking report cannot change the version or invent an aggregate."""
    report = _valid_report(100)
    report[field] = value

    with pytest.raises(ValueError, match=r"schema|version|aggregate"):
        check_agentready._validate_evidence(report)


@pytest.mark.unit
@pytest.mark.parametrize("payload", ['{"overall_score":100,"overall_score":0}', '{"overall_score":NaN}'])
def test_ambiguous_json_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: str) -> None:
    """Duplicate keys and nonstandard constants never reach gate evaluation."""
    json_file = tmp_path / "assessment.json"
    json_file.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(check_agentready, "JSON_PATH", json_file)

    with pytest.raises(SystemExit) as exc_info:
        check_agentready.main()

    assert exc_info.value.code == 1


@pytest.mark.unit
@pytest.mark.parametrize("extra_bytes", [0, 1])
def test_report_read_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra_bytes: int) -> None:
    """A valid report at the byte ceiling passes; one additional byte fails."""
    payload = json.dumps(_valid_report(100)).encode("utf-8")
    report = tmp_path / "assessment.json"
    report.write_bytes(payload + b" " * extra_bytes)
    monkeypatch.setattr(check_agentready, "JSON_PATH", report)
    monkeypatch.setattr(check_agentready, "_MAX_REPORT_BYTES", len(payload), raising=False)

    if extra_bytes:
        with pytest.raises(SystemExit) as exc_info:
            check_agentready.main()
        assert exc_info.value.code == 1
    else:
        result = check_agentready.main()
        assert result is None


@pytest.mark.unit
def test_tier_cannot_inject_log_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Untrusted tier labels remain on one bounded output line."""
    payload = _valid_report(100)
    payload["tier"] = "Gold\nFORGED"
    report = tmp_path / "assessment.json"
    report.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(check_agentready, "JSON_PATH", report)

    result = check_agentready.main()

    assert result is None
    assert "(Gold?FORGED)" in capsys.readouterr().out


@pytest.mark.unit
@pytest.mark.parametrize(
    "findings",
    [
        None,
        [],
        [{"attribute": {"id": "inline_documentation"}, "status": "pass", "score": None}],
        [{"attribute": {"id": "inline_documentation"}, "status": "not_applicable", "score": None}],
        [{"attribute": {"id": "inline_documentation"}, "status": "pass", "score": "nan"}],
        [{"attribute": {"id": "inline_documentation"}, "status": "pass", "score": float("inf")}],
    ],
)
def test_incomplete_attribute_evidence_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, findings: object) -> None:
    """An overall score cannot replace complete finite floor evidence."""
    report = {"overall_score": 100, "findings": findings}
    json_file = tmp_path / "assessment.json"
    json_file.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(check_agentready, "JSON_PATH", json_file)

    with pytest.raises(SystemExit) as exc_info:
        check_agentready.main()

    assert exc_info.value.code == 1


@pytest.mark.unit
def test_pass_score_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Score >= 85 returns normally (exit 0) and prints score, tier, and checkmark."""
    json_file = tmp_path / ".agentready" / "assessment-latest.json"
    json_file.parent.mkdir(parents=True)
    json_file.write_text(json.dumps(_valid_report(100)), encoding="utf-8")

    monkeypatch.setattr("check_agentready.JSON_PATH", json_file)

    # Should not raise at all
    check_agentready.main()

    captured = capsys.readouterr()
    assert "100.0" in captured.out
    assert "Gold" in captured.out
    assert "✓" in captured.out


@pytest.mark.unit
def test_fail_score_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Score < 85 exits 1 and prints score, tier, FAIL, and threshold."""
    json_file = tmp_path / ".agentready" / "assessment-latest.json"
    json_file.parent.mkdir(parents=True)
    json_file.write_text(json.dumps(_valid_report(72.1)), encoding="utf-8")

    monkeypatch.setattr("check_agentready.JSON_PATH", json_file)

    with pytest.raises(SystemExit) as exc_info:
        check_agentready.main()

    assert exc_info.value.code == 1

    captured = capsys.readouterr()
    assert "72.1" in captured.out
    assert "FAIL" in captured.out
    assert "85" in captured.out


@pytest.mark.unit
def test_missing_file_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Missing JSON file exits 1 with absolute path and repo root hint."""
    json_file = tmp_path / ".agentready" / "assessment-latest.json"
    # Do NOT create the file — it must be absent.

    monkeypatch.setattr("check_agentready.JSON_PATH", json_file)

    with pytest.raises(SystemExit) as exc_info:
        check_agentready.main()

    assert exc_info.value.code == 1

    captured = capsys.readouterr()
    # Error goes to stderr
    assert str(json_file.resolve()) in captured.err
    assert "repo root" in captured.err


@pytest.mark.unit
def test_deeply_nested_json_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Excessive JSON nesting produces a controlled non-passing result."""
    report = tmp_path / "assessment.json"
    report.write_text("[" * 1200 + "0" + "]" * 1200, encoding="utf-8")
    monkeypatch.setattr(check_agentready, "JSON_PATH", report)

    with pytest.raises(SystemExit) as exc_info:
        check_agentready.main()

    assert exc_info.value.code == 1
    assert "malformed JSON" in capsys.readouterr().err


@pytest.mark.unit
def test_malformed_json_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Malformed JSON exits 1."""
    json_file = tmp_path / ".agentready" / "assessment-latest.json"
    json_file.parent.mkdir(parents=True)
    json_file.write_text("not json{{{", encoding="utf-8")

    monkeypatch.setattr("check_agentready.JSON_PATH", json_file)

    with pytest.raises(SystemExit) as exc_info:
        check_agentready.main()

    assert exc_info.value.code == 1


@pytest.mark.unit
def test_missing_overall_score_field_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """JSON without 'overall_score' field exits 1."""
    json_file = tmp_path / ".agentready" / "assessment-latest.json"
    json_file.parent.mkdir(parents=True)
    json_file.write_text(json.dumps({"tier": "Bronze"}), encoding="utf-8")

    monkeypatch.setattr("check_agentready.JSON_PATH", json_file)

    with pytest.raises(SystemExit) as exc_info:
        check_agentready.main()

    assert exc_info.value.code == 1


@pytest.mark.unit
def test_non_numeric_score_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """'overall_score' with a non-numeric value exits 1 with a type error message."""
    json_file = tmp_path / ".agentready" / "assessment-latest.json"
    json_file.parent.mkdir(parents=True)
    json_file.write_text(json.dumps({"overall_score": "Gold", "tier": "Gold"}), encoding="utf-8")

    monkeypatch.setattr("check_agentready.JSON_PATH", json_file)

    with pytest.raises(SystemExit) as exc_info:
        check_agentready.main()

    assert exc_info.value.code == 1

    captured = capsys.readouterr()
    assert "not numeric" in captured.err
    assert "str" in captured.err
