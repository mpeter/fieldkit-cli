"""Direct coverage for the small helpers behind health, forecast and companion feed output."""

import sys
from datetime import date
from pathlib import Path

import click
import pytest

from fieldkit.cli_exit import EXIT_DATA, EXIT_PARTIAL, _exit_code_for
from fieldkit.commands.pursuit.audit import assessment_failure_warnings, assessment_summary_line
from fieldkit.commands.pursuit.forecast import _quota_standing, _resolve_quota
from fieldkit.commands.pursuit.pipeline_health import (
    RiskItem,
    _close_label,
    _health_exit_code,
    _items_in_tier,
    _reportable_item,
)
from fieldkit.companion.feed import FeedParseError, _failure_count
from fieldkit.pursuit.io import ReportAssessment, ReportFailure, ReportInput

pytestmark = pytest.mark.unit

TODAY = date(2026, 10, 1)


def _risk(close: str = "2026-10-10", days: int | None = 9, tier: str = "LOW") -> RiskItem:
    return RiskItem(
        relative_path="acme-fictional/pursuits/pilot.md",
        stage="validate",
        qualification_status="unavailable",
        close_date_str=close,
        days_until_close=days,
        days_in_stage=3,
        risk_tier=tier,
        risk_reasons=[],
        sf_opportunity_id="",
    )


def _input(tmp_path: Path, stage: str) -> ReportInput:
    path = tmp_path / "accounts/acme-fictional/pursuits/pilot.md"
    return ReportInput(path=path, relative_path="acme-fictional/pursuits/pilot.md", frontmatter={"stage": stage})


@pytest.mark.parametrize(
    ("stage", "include_prospect", "expected_stage"),
    [
        ("validate", False, "validate"),
        ("prospect", False, None),
        ("prospect", True, "prospect"),
        ("closed-won", True, None),
    ],
)
def test_reportable_item_filters_closed_and_early_stages(
    tmp_path: Path, stage: str, include_prospect: bool, expected_stage: str | None
) -> None:
    item = _reportable_item(_input(tmp_path, stage), TODAY, include_prospect)

    assert (item.stage if item else None) == expected_stage


@pytest.mark.parametrize(
    ("close", "days", "expected"),
    [
        ("2026-09-28", -3, "2026-09-28 (3d OVR)"),
        ("2026-10-10", 9, "2026-10-10 (9d)"),
        ("2026-12-31", 91, "2026-12-31"),
        ("2026-12-31", None, "2026-12-31"),
        ("", None, "—"),
    ],
)
def test_close_label(close: str, days: int | None, expected: str) -> None:
    assert _close_label(_risk(close=close, days=days)) == expected


@pytest.mark.parametrize(
    ("has_high", "strict", "failed", "expected"),
    [
        (False, False, False, 0),
        (True, False, False, 0),
        (True, True, False, EXIT_PARTIAL),
        (False, True, False, 0),
        (False, False, True, EXIT_PARTIAL),
    ],
)
def test_health_exit_code(has_high: bool, strict: bool, failed: bool, expected: int) -> None:
    assessment = ReportAssessment(failures=[ReportFailure("a/pursuits/x.md", "malformed YAML")] if failed else [])

    assert _health_exit_code([_risk(tier="HIGH")] if has_high else [], strict, assessment) == expected


def test_health_exit_code_without_assessment_follows_strict_high() -> None:
    assert _health_exit_code([_risk(tier="HIGH")], True, None) == EXIT_PARTIAL
    assert _health_exit_code([], True, None) == 0


def test_items_in_tier_keeps_only_matching_tier() -> None:
    high, low = _risk(tier="HIGH"), _risk(tier="LOW")

    assert _items_in_tier([high, low], "HIGH") == [high]


def test_assessment_text_names_each_failure_and_count() -> None:
    assessment = ReportAssessment(scanned=4, failures=[ReportFailure("acme-fictional/pursuits/x.md", "malformed YAML")])
    assessment.finish(2)

    assert assessment_failure_warnings(assessment) == [
        "WARNING: acme-fictional/pursuits/x.md: malformed YAML — assessment incomplete"
    ]
    assert assessment_summary_line(assessment) == "Assessment incomplete: 4 scanned, 2 included, 1 excluded, 1 failed."


@pytest.mark.parametrize(
    ("explicit", "configured", "expected"),
    [
        (5.0, {"target": 9}, 5.0),
        (None, {"target": 2000000}, 2000000.0),
        (None, {"target": "lots"}, None),
        (None, None, None),
    ],
)
def test_resolve_quota(
    monkeypatch: pytest.MonkeyPatch,
    explicit: float | None,
    configured: dict[str, object] | None,
    expected: float | None,
) -> None:
    monkeypatch.setattr("fieldkit.commands.pursuit.forecast.get_pipeline_quota", lambda: configured)

    assert _resolve_quota(explicit) == expected


@pytest.mark.parametrize(("gap", "expected"), [(-1.0, "over quota"), (0.0, "over quota"), (1.0, "under quota")])
def test_quota_standing(gap: float, expected: str) -> None:
    assert _quota_standing(gap) == expected


@pytest.mark.parametrize(
    ("entry", "expected"),
    [({}, 0), ({"failures": None}, 0), ({"failures": "3"}, 3), ({"failures": 2}, 2)],
)
def test_failure_count_reads_numeric_values(entry: dict[str, object], expected: int) -> None:
    assert _failure_count(entry, Path("watcher-run-status.json")) == expected


@pytest.mark.parametrize("bad", ["many", [1], {"n": 1}])
def test_failure_count_rejects_non_numeric_values(bad: object) -> None:
    with pytest.raises(FeedParseError, match="non-numeric failure count"):
        _failure_count({"failures": bad}, Path("watcher-run-status.json"))


def test_exit_code_for_click_error_shows_message_and_exits_data(capsys: pytest.CaptureFixture[str]) -> None:
    code = _exit_code_for(click.ClickException("bad target"))

    assert code == EXIT_DATA
    assert "Error: bad target" in capsys.readouterr().err


def test_exit_code_for_non_click_error_uses_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "click")

    assert _exit_code_for(ValueError("boom")) == EXIT_DATA
