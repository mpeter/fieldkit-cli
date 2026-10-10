"""Unit tests for fieldkit.util.jsonio — the ``default=`` callable for ``--json`` output."""

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import pytest

from fieldkit.util.jsonio import json_default

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(dt.datetime(2026, 7, 27, 12, 34, 56, tzinfo=dt.UTC), "2026-07-27T12:34:56Z", id="utc-datetime"),
        pytest.param(
            dt.datetime(2026, 7, 27, 12, 34, 56, tzinfo=dt.timezone(dt.timedelta(hours=2))),
            "2026-07-27T12:34:56+02:00",
            id="offset-datetime",
        ),
        pytest.param(dt.datetime(2026, 7, 27, 12, 34, 56), "2026-07-27T12:34:56", id="naive-datetime"),
        pytest.param(dt.date(2026, 7, 27), "2026-07-27", id="date"),
        pytest.param(dt.time(9, 5), "09:05:00", id="time"),
    ],
)
def test_json_default_renders_temporal_values_as_iso_8601(value: object, expected: str) -> None:
    """Dates and times use the ``T`` separator that ``default=str`` omits."""
    assert json_default(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(Decimal("1.50"), "1.50", id="decimal"),
        pytest.param(Path("notes/acme-corp.md"), "notes/acme-corp.md", id="path"),
    ],
)
def test_json_default_falls_back_to_str_for_other_values(value: object, expected: str) -> None:
    assert json_default(value) == expected


def test_json_default_is_a_drop_in_default_for_json_dumps() -> None:
    payload = {"when": dt.datetime(2026, 7, 27, 12, 0, tzinfo=dt.UTC), "amount": Decimal("3")}

    rendered = json.dumps(payload, default=json_default)

    assert rendered == '{"when": "2026-07-27T12:00:00Z", "amount": "3"}'
    assert dt.datetime.fromisoformat(json.loads(rendered)["when"]).tzinfo == dt.UTC
