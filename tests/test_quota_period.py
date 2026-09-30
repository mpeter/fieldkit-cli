"""The quota calendar contract has one strict, payload-safe interpretation."""

from datetime import date

import pytest

from fieldkit.quota_period import quota_period_end_date

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("period", "expected"),
    [
        ("2026-H1", date(2026, 6, 30)),
        ("2026-H2", date(2026, 12, 31)),
        ("2026-Q1", date(2026, 3, 31)),
        ("2026-Q2", date(2026, 6, 30)),
        ("2026-Q3", date(2026, 9, 30)),
        ("2026-Q4", date(2026, 12, 31)),
        ("2024-Q1", date(2024, 3, 31)),
        ("2023-Q1", date(2023, 3, 31)),
        ("1900-H1", date(1900, 6, 30)),
        ("2000-H1", date(2000, 6, 30)),
        ("0001-Q1", date(1, 3, 31)),
        ("9999-H2", date(9999, 12, 31)),
    ],
)
def test_quota_period_end_date_matches_calendar(period: str, expected: date) -> None:
    result = quota_period_end_date(period)

    assert result == expected


@pytest.mark.parametrize(
    "period",
    [
        "2026-H3",
        "2026-H4",
        "2026-Q0",
        "2026-Q5",
        "0000-H1",
        "26-H1",
        "202-H1",
        "10000-Q4",
        "\uff12\uff10\uff12\uff16-H1",
        "\u0662\u0660\u0662\u0666-H1",
        "2026-h1",
        "2026H1",
        "2026-H1-junk",
        "2026-H1\n",
        " 2026-H1",
        "2026-H1 ",
        "",
        "private-period-sentinel",
    ],
)
def test_quota_period_rejects_invalid_input_without_reflecting_it(period: str) -> None:
    with pytest.raises(ValueError, match=r"^Invalid quota period;") as failure:
        quota_period_end_date(period)

    assert str(failure.value) == "Invalid quota period; expected YYYY-H1/H2 or YYYY-Q1/Q2/Q3/Q4."
