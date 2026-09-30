"""Dependency security guidance is anchored to policy and executable controls."""

import json
import shlex
from pathlib import Path

import pytest
import yaml

from scripts import _supply_chain_policy as policy_checks
from scripts.documentation_commands import DOCUMENT_COMMANDS

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parent.parent
_PAGE = "docs/dependency-security.md"


def _section(heading: str) -> str:
    page = (_REPO / _PAGE).read_text(encoding="utf-8")
    return " ".join(page.split(f"## {heading}\n", 1)[1].split("\n## ", 1)[0].split())


def test_documented_allowlist_and_threshold_match_loaded_policy() -> None:
    policy = policy_checks.load_policy(_REPO / policy_checks.POLICY_PATH)

    assert policy.vulnerability_threshold == "low"
    section = _section("Blocking policy")
    assert f"threshold is `{policy.vulnerability_threshold}` or higher" in section
    declared = section.split("allowlist currently covers ", 1)[1].split(". It reflects", 1)[0]
    identifiers = declared.replace(", and ", ", ").split(", ")
    assert set(identifiers) == set(policy.allowed_spdx_licenses)
    assert policy.unknown_license_policy == "fail-unless-excepted"
    assert "current, reviewed exception" in section


@pytest.mark.parametrize("severity", ["low", "moderate", "high", "critical"])
def test_documented_published_severities_reject_new_vulnerabilities(severity: str) -> None:
    policy = policy_checks.load_policy(_REPO / policy_checks.POLICY_PATH)
    report = policy_checks.review_dependency_changes(
        [
            {
                "change_type": "added",
                "package_url": "pkg:pypi/example@1.0",
                "license": "MIT",
                "vulnerabilities": [
                    {
                        "severity": severity,
                        "advisory_ghsa_id": "GHSA-example",
                        "advisory_url": "https://example.com/advisory",
                    }
                ],
            }
        ],
        policy,
    )

    assert not report.ok
    assert report.findings[0].criterion_id == "DEP104"
    assert "any published severity fails review" in _section("Blocking policy")


@pytest.mark.parametrize("license_expression", [None, "UNKNOWN", "NOASSERTION", "MIT OR GPL-3.0-only"])
def test_documented_unknown_and_nonallowlisted_branches_fail(license_expression: str | None) -> None:
    policy = policy_checks.load_policy(_REPO / policy_checks.POLICY_PATH)
    report = policy_checks.review_dependency_changes(
        [{"change_type": "added", "package_url": "pkg:pypi/example@1.0", "license": license_expression}],
        policy,
    )

    assert not report.ok
    assert "Every identifier must be allowlisted, including every branch of `OR`" in _section(
        "What happens on a pull request"
    )


def test_documented_scanner_failure_is_not_zero_vulnerabilities() -> None:
    policy = policy_checks.load_policy(_REPO / policy_checks.POLICY_PATH)
    report = policy_checks.review_dependency_changes([], policy, upstream_outcome="failure")

    assert not report.ok
    assert report.findings[0].criterion_id == "DEP100"
    assert "A scanner error is a failed control" in _section("Blocking policy")
    with pytest.raises(ValueError, match="no valid audit result"):
        policy_checks.build_audit_evidence(
            {}, policy, scope="runtime-all-extras", revision="a" * 40, tool_version="uv", scanner_exit_code=2
        )


def test_documented_update_and_audit_scopes_match_workflow_wiring() -> None:
    updates = yaml.safe_load((_REPO / ".github/dependabot.yml").read_text(encoding="utf-8"))["updates"]
    assert {entry["package-ecosystem"] for entry in updates} == {"uv", "github-actions"}
    assert all(entry["schedule"]["interval"] == "weekly" for entry in updates)
    assert "separate weekly update groups" in _section("What happens on a pull request")

    workflow = yaml.safe_load((_REPO / ".github/workflows/dependency-review.yml").read_text(encoding="utf-8"))
    assert workflow["permissions"] == {"contents": "read"}
    steps = workflow["jobs"]["dependency-review"]["steps"]
    review = next(step for step in steps if step.get("id") == "review")
    assert review["with"]["fail-on-scopes"] == "runtime, development, unknown"
    assert review["with"]["fail-on-severity"] == policy_checks.load_policy().vulnerability_threshold
    assert "read-only token" in _section("What happens on a pull request")

    audit = yaml.safe_load((_REPO / ".github/workflows/dependency-audit.yml").read_text(encoding="utf-8"))
    assert audit["jobs"]["audit"]["strategy"]["matrix"]["scope"] == ["runtime-all-extras", "development"]
    section = _section("Continuous locked-graph audit")
    assert "every published optional profile" in section
    assert "locked development dependency group" in section


def test_documented_candidate_receipt_and_release_limits_remain_explicit() -> None:
    section = _section("Release-candidate license evidence")
    assert "isolated Python and site startup disabled (`-I -S`)" in section
    assert "never imports installed dependency or QA code" in section
    assert "License receipt version 2" in section
    assert "`runtime-license-observations.json`" in section
    assert "`platform-all-extras-requirements.txt` byte digests" in section
    assert "runtime's complete PEP 508 marker environment" in section
    assert "does not prove license approval for every platform from one interpreter" in section
    assert "source validation alone is insufficient" in section
    assert "Live public-repository settings and release enforcement remain release prerequisites" in _section(
        "Maintainer verification"
    )


def test_dependency_security_has_one_fixed_executable_owner() -> None:
    contract = json.loads((_REPO / "docs/documentation-contract.json").read_text(encoding="utf-8"))
    owners = {name: row for name, row in contract["verification"].items() if _PAGE in row["paths"]}

    assert set(owners) == {"dependency_security_documentation"}
    command = DOCUMENT_COMMANDS["dependency_security_documentation"]
    assert len(command) == 1
    assert owners["dependency_security_documentation"]["evidence"] == shlex.join(command[0])
    assert "tests/test_runtime_license_inventory.py" in command[0]
    assert "tests/test_check_supply_chain_policy.py" in command[0]
    assert any("test_verify_rejects_rechecksummed_license_input_swap" in argument for argument in command[0])
