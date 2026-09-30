"""Exact-version license reviews never become broad UNKNOWN bypasses."""

from datetime import date

import pytest

from scripts import _supply_chain_policy as checker

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("package_url", "license"),
    [
        ("pkg:pypi/jinja2@3.1.6", "BSD-3-Clause"),
        ("pkg:pypi/python-dateutil@2.9.0.post0", "BSD-3-Clause"),
        ("pkg:pypi/prompt-toolkit@3.0.52", "BSD-3-Clause"),
        ("pkg:pypi/fastuuid@0.14.0", "BSD-3-Clause"),
        ("pkg:pypi/tiktoken@0.13.0", "MIT"),
        ("pkg:pypi/pyasn1-modules@0.4.2", "BSD-2-Clause"),
    ],
)
def test_reviewed_unknown_license_is_exact_version_only(package_url: str, license: str) -> None:
    policy = checker.load_policy(today=date(2026, 9, 29))
    reviewed = {item.package_url: item for item in policy.license_exceptions}[package_url]

    assert reviewed.spdx_license == license
    assert reviewed.owner == "@mpeter"
    assert reviewed.expires_on == date(2026, 12, 31)
    assert checker.review_dependency_changes(
        [{"change_type": "added", "package_url": package_url, "license": "UNKNOWN"}],
        policy,
        today=date(2026, 9, 29),
    ).ok
    assert not checker.review_dependency_changes(
        [{"change_type": "added", "package_url": package_url.rsplit("@", 1)[0] + "@999.0", "license": "UNKNOWN"}],
        policy,
        today=date(2026, 9, 29),
    ).ok
    assert not checker.review_dependency_changes(
        [{"change_type": "added", "package_url": package_url, "license": "GPL-3.0-only"}],
        policy,
        today=date(2026, 9, 29),
    ).ok
