"""Shared release fixtures have canonical support modules, independent of tests."""

import importlib

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("module_name", "builder"),
    [("tests.release_bundle_support", "candidate"), ("tests.rehearsal_support", "fixture_receipt")],
)
def test_release_fixture_builder_has_one_support_owner(module_name: str, builder: str) -> None:
    support = importlib.import_module(module_name)
    assert getattr(support, builder).__module__ == module_name
    assert all(".test_" not in getattr(value, "__module__", "") for value in vars(support).values())
