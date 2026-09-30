"""The reviewed ISC policy addition preserves fail-closed license controls."""

from datetime import date

import pytest

from scripts import _supply_chain_policy as checker
from tests.test_spdx_license_policy import _MARKER_ENVIRONMENT

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("mode", ["review", "candidate"])
@pytest.mark.parametrize(
    ("expression", "accepted", "unknown"),
    [
        ("ISC", True, False),
        ("ISC AND MIT", True, False),
        ("GPL-3.0-only", False, False),
        ("ISC OR GPL-3.0-only", False, False),
        ("UNKNOWN", False, True),
        ("ISC AND", False, False),
    ],
)
def test_reviewed_isc_policy_preserves_license_rejections(
    mode: str, expression: str, accepted: bool, unknown: bool
) -> None:
    policy = checker.load_policy(today=date(2026, 9, 29))
    package_url = "pkg:pypi/requests-oauthlib@2.0.0"
    if mode == "review":
        report = checker.review_dependency_changes(
            [{"change_type": "added", "package_url": package_url, "license": expression}],
            policy,
            today=date(2026, 9, 29),
        )
        assert report.ok is accepted
        findings = report.findings
        criterion = "DEP102" if unknown else "DEP101"
    else:
        evidence = checker.build_license_evidence(
            [{"name": "requests-oauthlib", "version": "2.0.0", "license_expression": expression}],
            policy,
            scope="runtime-all-extras",
            revision="d" * 40,
            export_policy_sha256="e" * 64,
            sbom_sha256="f" * 64,
            observations_sha256="a" * 64,
            platform_requirements_sha256="b" * 64,
            marker_environment=_MARKER_ENVIRONMENT,
            expected_package_urls=(package_url,),
            today=date(2026, 9, 29),
        )
        assert evidence.status == ("pass" if accepted else "fail")
        findings = evidence.findings
        criterion = "DEP302" if unknown else "DEP301"
    assert {finding.criterion_id for finding in findings} == (set() if accepted else {criterion})
    assert policy.unknown_license_policy == "fail-unless-excepted"
    assert "pkg:pypi/requests-oauthlib@2.0.0" not in {exception.package_url for exception in policy.license_exceptions}
