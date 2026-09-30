"""Bind the documented release modes to the canonical release authority policy."""

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from scripts import _release_policy, release_workflow_policy
from scripts.markdown_tables import MarkdownTable, markdown_tables, parse_markdown_tables

pytestmark = pytest.mark.unit
_ROOT = Path(__file__).resolve().parent.parent


def _assert_modes(table: MarkdownTable) -> None:
    assert table.header == ("Mode", "Trigger", "Result", "Required approval")
    assert len(table.rows) == 3, "Release mode inventory differs"
    assert table.rows == (
        (
            "Dry run",
            "Protected default branch dispatch",
            "Builds and validates one retained candidate",
            "Workflow dispatch approval",
        ),
        (
            "TestPyPI",
            "Protected default branch dispatch",
            "Attests and publishes the retained candidate to TestPyPI",
            "TestPyPI and operator approval",
        ),
        (
            "Production",
            "Verified signed `v<project-version>` tag",
            "Attests, publishes, verifies consumers, then creates the GitHub release",
            "Final operator approval",
        ),
    ), "Release mode policy wording differs"


def _workflow() -> dict[str, object]:
    value = yaml.load(
        (_ROOT / release_workflow_policy.WORKFLOW_PATH).read_text(encoding="utf-8"), Loader=yaml.BaseLoader
    )
    assert isinstance(value, dict)
    return value


def test_release_mode_table_matches_planned_policy_while_workflow_fails_closed() -> None:
    policy_report = _release_policy.validate_repository(_ROOT)
    assert policy_report.ok, policy_report.findings
    workflow_report = release_workflow_policy.validate_repository(_ROOT)
    assert {finding.code for finding in workflow_report.findings} == {"RWA009", "RWA010", "RWF028"}
    tables = markdown_tables(_ROOT / "RELEASING.md")
    assert len(tables) == 1
    _assert_modes(tables[0])
    policy = _release_policy.load_policy(_ROOT / _release_policy.POLICY_PATH)
    assert policy.candidate_trigger == "protected-main-workflow-dispatch"
    assert policy.production_trigger == "protected-signed-semver-tag"
    assert policy.build_policy == "build-once-promote-by-digest"
    workflow = _workflow()
    triggers = workflow["on"]
    assert isinstance(triggers, dict)
    assert triggers["workflow_dispatch"]["inputs"]["mode"]["options"] == ["dry-run", "testpypi"]
    assert triggers["push"]["tags"] == ["v*.*.*"]
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    assert jobs["publish_testpypi"]["environment"] == policy.testpypi_environment
    assert jobs["publish_pypi"]["environment"] == policy.pypi_environment
    assert jobs["approval"]["environment"] == "release-approval"
    assert set(jobs["publish_testpypi"]["needs"]) == {"build", "validate", "attest"}
    assert set(jobs["github_release"]["needs"]) == {"build", "validate", "attest", "publish_pypi", "consumer_pypi"}


def test_release_guide_distinguishes_governance_and_checkout_boundaries() -> None:
    text = (_ROOT / "RELEASING.md").read_text(encoding="utf-8")
    jobs = _workflow()["jobs"]
    assert isinstance(jobs, dict)

    assert "Exit 3 means the candidate or policy record is invalid" in text
    assert "Exit 2 is a command-line usage error" in text
    assert "build and validation jobs check out" in text
    assert "attestation and publisher jobs do not check out source" in text
    assert all(
        any("actions/checkout@" in step.get("uses", "") for step in jobs[name]["steps"])
        for name in ("build", "validate")
    )
    assert all(
        not any("actions/checkout@" in step.get("uses", "") for step in jobs[name]["steps"])
        for name in ("attest", "publish_testpypi", "publish_pypi")
    )


def test_release_guide_discloses_current_nonpassing_workflow_policy() -> None:
    text = (_ROOT / "RELEASING.md").read_text(encoding="utf-8")
    report = release_workflow_policy.validate_repository(_ROOT)

    assert {finding.code for finding in report.findings} == {"RWA009", "RWA010", "RWF028"}
    assert all(code in text for code in ("RWA009", "RWA010", "RWF028"))
    assert "does not establish release authority" in text


@pytest.mark.parametrize(
    ("job", "key", "value"),
    [
        ("approval", "environment", "unprotected"),
        ("publish_testpypi", "if", "github.event_name == 'workflow_dispatch'"),
        ("publish_pypi", "environment", "testpypi"),
        ("github_release", "needs", ["build", "validate", "attest", "publish_pypi"]),
        ("attest", "if", "always()"),
    ],
)
def test_release_mode_workflow_rejects_missing_authority_controls(job: str, key: str, value: object) -> None:
    workflow = deepcopy(_workflow())
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    jobs[job][key] = value
    report = release_workflow_policy.validate_document(workflow)
    assert not report.ok
    assert any(finding.job == job for finding in report.findings)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("Protected default branch dispatch", "Any branch dispatch"),
        ("Verified signed `v<project-version>` tag", "Unsigned version tag"),
        ("Builds and validates one retained candidate", "Publishes directly to PyPI"),
        (
            "Attests and publishes the retained candidate to TestPyPI",
            "Does not attest or publish the retained candidate to TestPyPI",
        ),
        (
            "Attests, publishes, verifies consumers, then creates the GitHub release",
            "Creates the GitHub release before verifying consumers",
        ),
        ("Workflow dispatch approval", "No workflow dispatch approval"),
        ("TestPyPI and operator approval", "No TestPyPI or operator approval"),
        ("Final operator approval", "No final operator approval"),
        ("| Dry run |", "| Production |"),
    ],
)
def test_release_mode_table_rejects_false_authority_claims(before: str, after: str) -> None:
    source = markdown_tables(_ROOT / "RELEASING.md")[0].source_text
    assert before in source
    tables = parse_markdown_tables(source.replace(before, after))
    assert len(tables) == 1
    with pytest.raises(AssertionError, match=r"mode|policy"):
        _assert_modes(tables[0])
