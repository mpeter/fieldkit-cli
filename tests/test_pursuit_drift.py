"""Unit tests for fieldkit.pursuit.drift — pursuit-vs-Salesforce comparison rules."""

from datetime import date
from typing import Any

import pytest

from fieldkit.pursuit.drift import DriftFlag, LiveOpportunity, assess_drift, drift_status

TODAY = date(2026, 10, 9)


def _frontmatter(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "stage": "propose",
        "sf_stage": "Propose",
        "sf_close_date": "2027-03-01",
        "sf_consulting_acv": "$100,000",
    }
    return {**base, **overrides}


def _live(**overrides: Any) -> LiveOpportunity:
    base: LiveOpportunity = {
        "stage": "Propose",
        "close_date": "2027-03-01",
        "consulting_acv": 100000.0,
        "is_closed": False,
    }
    return {**base, **overrides}  # type: ignore[typeddict-item]


def _codes(flags: list[DriftFlag]) -> list[str]:
    return [flag.code for flag in flags]


@pytest.mark.unit
def test_matching_snapshot_has_no_flags() -> None:
    flags = assess_drift(_frontmatter(), _live(), TODAY)
    assert flags == []
    assert drift_status(flags) == "GREEN"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("frontmatter", "live", "expected"),
    [
        pytest.param(_frontmatter(), _live(stage="Negotiate"), ["stage-mismatch", "sf-stage-drift"], id="sf-advanced"),
        pytest.param(
            _frontmatter(sf_stage="Negotiate", stage="negotiate"),
            _live(),
            ["stage-mismatch", "sf-stage-drift"],
            id="sf-regressed",
        ),
        pytest.param(_frontmatter(sf_close_date="2027-01-15"), _live(), ["close-date-drift"], id="close-date"),
        pytest.param(_frontmatter(sf_close_date=None), _live(), ["close-date-drift"], id="close-date-missing-locally"),
        pytest.param(_frontmatter(sf_consulting_acv="$90,000"), _live(), ["acv-drift"], id="acv-changed"),
        pytest.param(
            _frontmatter(sf_consulting_acv="$50,000"), _live(consulting_acv=None), ["acv-drift"], id="acv-cleared-in-sf"
        ),
        pytest.param(
            _frontmatter(sf_consulting_acv=""), _live(consulting_acv=250000.0), ["acv-drift"], id="acv-new-in-sf"
        ),
    ],
)
def test_snapshot_divergence_is_yellow(frontmatter: dict[str, Any], live: LiveOpportunity, expected: list[str]) -> None:
    flags = assess_drift(frontmatter, live, TODAY)
    assert _codes(flags) == expected
    assert drift_status(flags) == "YELLOW"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stored", "live_value"),
    [
        pytest.param("", None, id="both-blank"),
        pytest.param("", 0.0, id="blank-vs-zero"),
        pytest.param("$0", None, id="zero-vs-null"),
        pytest.param(100000, 100000.4, id="numeric-and-cents"),
    ],
)
def test_equal_amounts_do_not_drift(stored: object, live_value: float | None) -> None:
    flags = assess_drift(_frontmatter(sf_consulting_acv=stored), _live(consulting_acv=live_value), TODAY)
    assert "acv-drift" not in _codes(flags)


@pytest.mark.unit
def test_yaml_date_values_compare_by_day() -> None:
    flags = assess_drift(_frontmatter(sf_close_date=date(2027, 3, 1)), _live(close_date="2027-03-01"), TODAY)
    assert flags == []


@pytest.mark.unit
def test_organization_stage_names_skip_local_comparison() -> None:
    """A Salesforce stage outside fieldkit's lifecycle cannot be compared with local stage."""
    flags = assess_drift(
        _frontmatter(stage="qualify", sf_stage="Stage 2 - Develop"),
        _live(stage="Stage 2  -  develop"),
        TODAY,
    )
    assert flags == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("close_date", "expected_code", "expected_level"),
    [
        pytest.param("2026-10-08", "overdue", "RED", id="yesterday"),
        pytest.param("2026-10-09", "closing-14d", "RED", id="today"),
        pytest.param("2026-10-23", "closing-14d", "RED", id="day-14"),
        pytest.param("2026-10-24", "closing-30d", "YELLOW", id="day-15"),
        pytest.param("2026-11-08", "closing-30d", "YELLOW", id="day-30"),
    ],
)
def test_close_window(close_date: str, expected_code: str, expected_level: str) -> None:
    flags = assess_drift(_frontmatter(sf_close_date=close_date), _live(close_date=close_date), TODAY)
    assert _codes(flags) == [expected_code]
    assert flags[0].level == expected_level


@pytest.mark.unit
def test_close_date_beyond_window_is_clean() -> None:
    flags = assess_drift(_frontmatter(sf_close_date="2026-11-09"), _live(close_date="2026-11-09"), TODAY)
    assert flags == []


@pytest.mark.unit
def test_closed_in_salesforce_while_open_locally_is_red() -> None:
    flags = assess_drift(
        _frontmatter(sf_close_date="2026-09-01"),
        _live(stage="Closed Won", close_date="2026-09-01", is_closed=True),
        TODAY,
    )
    # The snapshot also drifted; no close-window flag for a closed opportunity.
    assert _codes(flags) == ["sf-closed-local-open", "sf-stage-drift"]
    assert drift_status(flags) == "RED"


@pytest.mark.unit
def test_custom_closed_stage_uses_salesforce_closed_flag() -> None:
    flags = assess_drift(
        _frontmatter(sf_stage="Closed Booked"),
        _live(stage="Closed Booked", is_closed=True),
        TODAY,
    )
    assert _codes(flags) == ["sf-closed-local-open"]


@pytest.mark.unit
def test_closed_locally_and_in_salesforce_is_clean() -> None:
    flags = assess_drift(
        _frontmatter(stage="closed-won", sf_stage="Closed Won"),
        _live(stage="Closed Won", is_closed=True),
        TODAY,
    )
    assert flags == []


@pytest.mark.unit
def test_unparseable_live_close_date_skips_window() -> None:
    flags = assess_drift(_frontmatter(sf_close_date="not-a-date"), _live(close_date="not-a-date"), TODAY)
    assert flags == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("levels", "expected"),
    [
        pytest.param([], "GREEN", id="none"),
        pytest.param(["YELLOW"], "YELLOW", id="yellow"),
        pytest.param(["YELLOW", "RED"], "RED", id="red-wins"),
    ],
)
def test_drift_status(levels: list[str], expected: str) -> None:
    flags = [DriftFlag(level, "acv-drift", "x") for level in levels]  # type: ignore[arg-type]
    assert drift_status(flags) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "stored",
    [
        pytest.param(float("nan"), id="yaml-nan"),
        pytest.param("NaN", id="nan-string"),
        pytest.param("inf", id="inf-string"),
        pytest.param("1e400", id="overflow"),
        pytest.param("TBD", id="placeholder-text"),
    ],
)
def test_unusable_stored_amount_is_flagged_not_crashed(stored: object) -> None:
    flags = assess_drift(_frontmatter(sf_consulting_acv=stored), _live(), TODAY)
    assert flags == [DriftFlag("YELLOW", "acv-drift", "stored consulting ACV is not a number")]


@pytest.mark.unit
def test_non_finite_live_amount_is_not_compared() -> None:
    flags = assess_drift(_frontmatter(), _live(consulting_acv=float("inf")), TODAY)
    assert flags == []


@pytest.mark.unit
def test_hyphenated_keys_are_ignored() -> None:
    """The SF pipeline reads only underscore-keyed ``sf_*`` fields (pursuit AGENTS.md R05)."""
    frontmatter = {
        "stage": "propose",
        "sf-stage": "Propose",
        "sf-close-date": "2027-03-01",
        "sf-consulting-acv": "$100,000",
    }
    flags = assess_drift(frontmatter, _live(), TODAY)
    assert _codes(flags) == ["sf-stage-drift", "close-date-drift", "acv-drift"]


@pytest.mark.unit
@pytest.mark.parametrize("live_acv", [None, 0.0], ids=["live-null", "live-zero"])
def test_placeholder_stored_amount_never_matches_empty_live_amount(live_acv: float | None) -> None:
    flags = assess_drift(_frontmatter(sf_consulting_acv="TBD"), _live(consulting_acv=live_acv), TODAY)
    assert flags == [DriftFlag("YELLOW", "acv-drift", "stored consulting ACV is not a number")]
    assert drift_status(flags) != "GREEN"
