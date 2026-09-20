"""Tests for ClosePlan field extraction and MEDDPICC display formatting."""

from typing import Any

import pytest

from fieldkit.commands.sf.meddpicc import _fmt_score, _fmt_score_ratio
from fieldkit.sf.meddpicc import extract_answer, extract_category, field_value

pytestmark = pytest.mark.unit


def test_field_value_extracts_nested_ui_api_value() -> None:
    """Prefer the raw UI API value envelope over its container."""
    record = {"fields": {"MyField__c": {"value": "hello"}}}
    result = field_value(record, "MyField__c")
    assert result == "hello"


def test_field_value_falls_back_to_direct_key() -> None:
    """Retain support for direct-key sObject REST records."""
    record = {"Id": "abc123"}
    result = field_value(record, "Id")
    assert result == "abc123"


def test_field_value_returns_none_for_missing_key() -> None:
    """Represent a missing field without inventing a value."""
    record: dict[str, Any] = {}
    result = field_value(record, "Missing__c")
    assert result is None


def test_fmt_score_ratio_formats_float_as_percentage() -> None:
    """Render fractional score ratios as human-readable percentages."""
    result = _fmt_score_ratio(0.75)
    assert result == "75%"


def test_fmt_score_ratio_returns_not_set_for_none() -> None:
    """Distinguish an absent ratio from a numeric zero."""
    result = _fmt_score_ratio(None)
    assert result == "(not set)"


def test_fmt_score_ratio_handles_zero() -> None:
    result = _fmt_score_ratio(0.0)
    assert result == "0%"


def test_fmt_score_ratio_handles_one() -> None:
    result = _fmt_score_ratio(1.0)
    assert result == "100%"


def test_fmt_score_ratio_treats_above_one_as_already_a_percentage() -> None:
    """Preserve live org ratios that are already expressed as percentages."""
    result = _fmt_score_ratio(63.0)
    assert result == "63%"


def test_fmt_score_ratio_handles_hundred_as_already_a_percentage() -> None:
    result = _fmt_score_ratio(100.0)
    assert result == "100%"


@pytest.mark.parametrize("value", [True, "not-a-number", object()])
def test_fmt_score_ratio_preserves_non_numeric_values(value: object) -> None:
    """Preserve unexpected raw ratios rather than hiding source evidence."""
    result = _fmt_score_ratio(value)
    assert result == str(value)


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_fmt_score_ratio_preserves_non_finite_values(value: float) -> None:
    """Avoid integer conversion for non-finite ratio values."""
    result = _fmt_score_ratio(value)
    assert result == str(value)


def test_fmt_score_formats_float_as_int_string() -> None:
    result = _fmt_score(42.0)
    assert result == "42"


def test_fmt_score_returns_not_set_for_none() -> None:
    result = _fmt_score(None)
    assert result == "(not set)"


def test_fmt_score_handles_zero() -> None:
    result = _fmt_score(0.0)
    assert result == "0"


@pytest.mark.parametrize("value", [True, "not-a-number", object()])
def test_fmt_score_preserves_non_integral_values(value: object) -> None:
    """Preserve non-integral score evidence without coercion."""
    result = _fmt_score(value)
    assert result == str(value)


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_fmt_score_preserves_non_finite_values(value: float) -> None:
    """Avoid integer conversion for non-finite score values."""
    result = _fmt_score(value)
    assert result == str(value)


def test_extract_category_splits_on_hyphen() -> None:
    result = extract_category("ECONOMIC BUYER - Individual within the customer's organization")
    assert result == "ECONOMIC BUYER"


def test_extract_category_splits_on_en_dash() -> None:
    result = extract_category("METRICS \u2013 Quantifiable measurements and proof of business benefits")
    assert result == "METRICS"


def test_extract_category_splits_on_em_dash() -> None:
    """Accept the common word-processor em-dash substitution in templates."""
    result = extract_category("ECONOMIC BUYER \u2014 Individual within the organization")
    assert result == "ECONOMIC BUYER"


def test_extract_category_returns_none_without_separator() -> None:
    """Refuse to infer a category when the naming delimiter is absent."""
    result = extract_category("Identify Economic Buyer")
    assert result is None


def test_extract_category_returns_none_for_none() -> None:
    assert extract_category(None) is None


def test_extract_category_returns_none_for_empty_string() -> None:
    assert extract_category("") is None


def test_extract_category_returns_none_for_non_string() -> None:
    assert extract_category(42) is None


def test_extract_answer_prefers_text_answer() -> None:
    record = {
        "fields": {
            "TSPC__TextAnswer__c": {"value": "CFO engaged"},
            "TSPC__RichTextAnswer__c": {"value": "<p>ignored</p>"},
        }
    }
    assert extract_answer(record) == "CFO engaged"


def test_extract_answer_falls_back_to_rich_text_and_strips_html() -> None:
    record = {"fields": {"TSPC__RichTextAnswer__c": {"value": "<p>Success will go one of two ways</p>"}}}
    assert extract_answer(record) == "Success will go one of two ways"


def test_extract_answer_unescapes_html_entities_before_stripping_tags() -> None:
    """Decode the literal HTML entities observed in live rich-text values."""
    record = {
        "fields": {
            "TSPC__RichTextAnswer__c": {
                "value": "&lt;p&gt;Success will go one of two ways&lt;/p&gt;",
            }
        }
    }
    assert extract_answer(record) == "Success will go one of two ways"


def test_extract_answer_unescapes_apostrophe_entity() -> None:
    record = {"fields": {"TSPC__RichTextAnswer__c": {"value": "The customer&#39;s pain point"}}}
    assert extract_answer(record) == "The customer's pain point"


def test_extract_answer_returns_none_when_both_empty() -> None:
    record = {"fields": {"TSPC__TextAnswer__c": {"value": None}, "TSPC__RichTextAnswer__c": {"value": None}}}
    assert extract_answer(record) is None


def test_extract_answer_returns_none_when_both_missing() -> None:
    assert extract_answer({"fields": {}}) is None


def test_extract_answer_ignores_blank_text_answer() -> None:
    """Fall through from a whitespace-only text answer to usable rich text."""
    record = {
        "fields": {"TSPC__TextAnswer__c": {"value": "   "}, "TSPC__RichTextAnswer__c": {"value": "<p>real answer</p>"}}
    }
    assert extract_answer(record) == "real answer"
