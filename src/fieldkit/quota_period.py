"""Calendar boundaries for supported quota periods."""

import calendar
from datetime import date

_PERIOD_END_MONTH = {"H1": 6, "H2": 12, "Q1": 3, "Q2": 6, "Q3": 9, "Q4": 12}


def quota_period_end_date(period: str) -> date:
    """Return a period's final date, rejecting invalid input without reflecting it."""
    year_text, _, unit = period.partition("-")
    if (
        len(year_text) != 4
        or any(character < "0" or character > "9" for character in year_text)
        or year_text == "0000"
        or unit not in _PERIOD_END_MONTH
    ):
        raise ValueError("Invalid quota period; expected YYYY-H1/H2 or YYYY-Q1/Q2/Q3/Q4.")
    year = int(year_text)
    month = _PERIOD_END_MONTH[unit]
    return date(year, month, calendar.monthrange(year, month)[1])
