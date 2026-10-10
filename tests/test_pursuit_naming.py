"""Pursuit naming rules: one slug rule, and the path-component guard for existing names."""

import pytest

from fieldkit.pursuit.io import is_pursuit_path_component, is_pursuit_slug, slugify_pursuit_name

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param("New Deal", "new-deal", id="spaces-and-case"),
        pytest.param("Acme Corp — Q3 renewal!", "acme-corp-q3-renewal", id="punctuation-runs"),
        pytest.param("../moved", "moved", id="traversal-collapses"),
        pytest.param("a/b", "a-b", id="separator-becomes-hyphen"),
        pytest.param("--edge--", "edge", id="trimmed"),
        pytest.param("", "", id="empty"),
    ],
)
def test_slugify_pursuit_name(name: str, expected: str) -> None:
    assert slugify_pursuit_name(name) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("new-deal", True, id="slug"),
        pytest.param("q3-2026", True, id="digits"),
        pytest.param("", False, id="empty"),
        pytest.param("New-Deal", False, id="uppercase"),
        pytest.param("new_deal", False, id="underscore"),
        pytest.param("new--deal", False, id="double-hyphen"),
        pytest.param("-new", False, id="leading-hyphen"),
        pytest.param("../moved", False, id="traversal"),
    ],
)
def test_is_pursuit_slug_accepts_only_the_slugified_form(value: str, expected: bool) -> None:
    assert is_pursuit_slug(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("Old_Deal", True, id="legacy-name"),
        pytest.param("o'brien deal", True, id="quote-and-space"),
        pytest.param("", False, id="empty"),
        pytest.param(".", False, id="current-directory"),
        pytest.param("..", False, id="parent-directory"),
        pytest.param("a/b", False, id="slash"),
        pytest.param("a\\b", False, id="backslash"),
        pytest.param("a\0b", False, id="nul"),
    ],
)
def test_is_pursuit_path_component_keeps_names_inside_their_directory(value: str, expected: bool) -> None:
    assert is_pursuit_path_component(value) is expected
