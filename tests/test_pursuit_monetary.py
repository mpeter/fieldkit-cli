"""Regression tests for pursuit monetary normalization."""

import pytest

from fieldkit.pursuit.models import PursuitFrontmatter
from fieldkit.pursuit.utils import _parse_monetary

pytestmark = pytest.mark.unit


def test_parse_monetary_dollar_string() -> None:
    """implementation change: dollar strings normalize to floats."""
    assert _parse_monetary("$500,000") == 500000.0


def test_parse_monetary_float_passthrough() -> None:
    """implementation change: float input passes through unchanged."""
    assert _parse_monetary(500000.0) == 500000.0


def test_parse_monetary_none_returns_none() -> None:
    """implementation change: None input returns None."""
    assert _parse_monetary(None) is None


def test_parse_monetary_empty_string_returns_none() -> None:
    """implementation change: an empty string returns None."""
    assert _parse_monetary("") is None


def test_parse_monetary_plain_string_number() -> None:
    """implementation change: plain numeric strings normalize to floats."""
    assert _parse_monetary("500000") == 500000.0


def test_parse_monetary_int_input() -> None:
    """implementation change: integer input converts to a float."""
    assert _parse_monetary(500000) == 500000.0


def test_pursuit_frontmatter_normalizes_sf_arr_from_string() -> None:
    """implementation change: PursuitFrontmatter normalizes dollar-string ARR."""
    model = PursuitFrontmatter(stage="discover", **{"gate-status": "pending"}, sf_arr="$500,000")
    assert model.sf_arr == 500000.0


def test_pursuit_frontmatter_normalizes_sf_acv_from_string() -> None:
    """implementation change: PursuitFrontmatter normalizes dollar-string ACV."""
    model = PursuitFrontmatter(stage="discover", **{"gate-status": "pending"}, sf_acv="$250,000.00")
    assert model.sf_acv == 250000.0


def test_pursuit_frontmatter_normalizes_sf_consulting_acv_from_string() -> None:
    """implementation change: PursuitFrontmatter normalizes consulting ACV."""
    model = PursuitFrontmatter(stage="discover", **{"gate-status": "pending"}, sf_consulting_acv="$75,000")
    assert model.sf_consulting_acv == 75000.0
