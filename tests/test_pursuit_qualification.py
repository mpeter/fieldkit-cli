"""Tests for current native ClosePlan qualification status."""

import pytest

from fieldkit.pursuit.qualification import native_qualification_status

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("opportunity_id", "expected"),
    [
        ("006000000000AAA", "pending (live ClosePlan fetch required)"),
        (None, "unavailable (no Salesforce opportunity link)"),
        ("", "unavailable (no Salesforce opportunity link)"),
    ],
)
def test_native_qualification_status_requires_current_closeplan_read(
    opportunity_id: str | None,
    expected: str,
) -> None:
    assert native_qualification_status(opportunity_id) == expected
