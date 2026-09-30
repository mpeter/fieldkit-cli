"""Contracts for the least-privilege release workflow policy."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import release_workflow_policy

pytestmark = pytest.mark.unit


def _sealed_distribution_step() -> dict[str, object]:
    return {
        "run": (
            'test "$(sha256sum candidate/bundle/SHA256SUMS | cut -d\' "\' -f1)" = '
            '"$EXPECTED_BUNDLE_MANIFEST_SHA256"\n'
            "(cd candidate/bundle && sha256sum --strict --check SHA256SUMS)\n"
            "mkdir release-dist\n"
            "cp candidate/bundle/*.whl candidate/bundle/*.tar.gz release-dist/"
        )
    }


def _workflow() -> dict[str, object]:
    return {
        "on": {
            "workflow_dispatch": {
                "inputs": {
                    "mode": {
                        "required": True,
                        "type": "choice",
                        "options": ["dry-run", "testpypi"],
                    }
                }
            },
            "push": {"tags": ["v*.*.*"]},
        },
        "permissions": {},
        "env": {
            "GITLEAKS_VERSION": release_workflow_policy.GITLEAKS_VERSION,
            "GITLEAKS_LINUX_X64_SHA256": release_workflow_policy.GITLEAKS_LINUX_X64_SHA256,
        },
        "concurrency": {"group": "release-${{ github.ref }}", "cancel-in-progress": False},
        "jobs": {
            "context": {
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 10,
                "permissions": {"contents": "read"},
                "outputs": {
                    "approval_run": "${{ steps.approval_metadata.outputs.run }}",
                    "approval_manifest_sha256": "${{ steps.approval_metadata.outputs.sha256 }}",
                },
                "steps": [
                    {
                        "run": (
                            'if [ "$GITHUB_EVENT_NAME" = "workflow_dispatch" ]; then '
                            'test "$GITHUB_REF" = "refs/heads/main"; fi && '
                            "timeout 60s gh api tags/$GITHUB_REF_NAME && "
                            "jq '.verification.verified == true and .object.sha == $GITHUB_SHA'"
                        )
                    },
                    {
                        "id": "approval_metadata",
                        "run": (
                            "Release-approval-run: Release-approval-manifest-sha256: "
                            '[[ "$run" =~ ^[1-9][0-9]*$ ]]\n'
                            '[[ "$digest" =~ ^[0-9a-f]{64}$ ]]\n'
                            'wc -l <<< "$run"\n'
                            'wc -l <<< "$digest"\n'
                            'echo "run=$run" >> "$GITHUB_OUTPUT"\n'
                            'echo "sha256=$digest" >> "$GITHUB_OUTPUT"'
                        ),
                    },
                    {
                        "uses": "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
                        "with": {
                            "name": "release-evidence-tools-${{ github.run_id }}-${{ github.run_attempt }}-${{ github.sha }}",
                            "path": "scripts/release_promotion_evidence.py scripts/release_bundle.py scripts/runtime_license_inventory.py scripts/release_consumer.py scripts/release_wheelhouse.py pyproject.toml",
                            "if-no-files-found": "error",
                            "retention-days": "90",
                        },
                    },
                ],
            },
            "build": {
                "if": "always() && ((github.event_name == 'push' && needs.approval.result == 'success' && needs.compatibility.result == 'success') || (github.event_name == 'workflow_dispatch' && needs.approval.result == 'skipped' && needs.compatibility.result == 'skipped'))",
                "needs": ["context", "approval", "compatibility"],
                "outputs": {"bundle_manifest_sha256": "${{ steps.bundle_manifest.outputs.sha256 }}"},
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 30,
                "permissions": {"contents": "read", "actions": "read"},
                "steps": [
                    {
                        "run": (
                            "APPROVAL_RUN APPROVAL_MANIFEST_SHA256 actions/runs/$APPROVAL_RUN "
                            ".github/workflows/release-approval.yml@ "
                            "release-approval-input-${APPROVAL_RUN}-${approval_attempt}-${GITHUB_SHA} "
                            "timeout 60s gh api --paginate --slurp jq -r --arg name length == 1 actions/artifacts/$artifact_id/zip "
                            "scripts/release_approval_archive.py\n"
                            "python scripts/release_approval_input.py --input approved-artifact/approval-input "
                            "--controller-root controller --expected-selection expected.json\n"
                            "approved-artifact/approval-input/public-candidate/report.json "
                            "approved-artifact/approval-input/public-candidate/bundle"
                        )
                    },
                    {
                        "name": "Install the pinned export secret scanner",
                        "if": "github.event_name == 'workflow_dispatch'",
                        "run": (
                            "curl --fail --location --proto '=https' --tlsv1.2 --retry 3 --connect-timeout 10 "
                            "--max-time 60 --output gitleaks.tar.gz\n"
                            "sha256sum --check\n"
                            'tar --extract --file "$archive" --directory "$scanner_root" gitleaks\n'
                            'install -m 0755 "$scanner_root/gitleaks" "$scanner_binary"\n'
                            '"$scanner_binary" version'
                        ),
                    },
                    {
                        "name": "Build the one retained public candidate",
                        "if": "github.event_name == 'workflow_dispatch'",
                        "id": "bundle_manifest",
                        "run": 'echo "sha256=$(sha256sum candidate/bundle/SHA256SUMS)" >> "$GITHUB_OUTPUT"',
                    },
                ],
            },
            "approval": {
                "if": "github.event_name == 'push'",
                "needs": "context",
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 10,
                "environment": "release-approval",
                "permissions": {"contents": "read"},
            },
            "compatibility": {
                "if": "github.event_name == 'push'",
                "needs": ["context", "approval"],
                "uses": "./.github/workflows/compatibility.yml",
                "permissions": {"contents": "read"},
            },
            "validate": {
                "if": "always() && needs.build.result == 'success'",
                "needs": "build",
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 30,
                "permissions": {"contents": "read"},
            },
            "consumer_pypi": {
                "if": "always() && needs.context.result == 'success' && needs.build.result == 'success' && needs.validate.result == 'success' && needs.publish_pypi.result == 'success' && github.event_name == 'push'",
                "needs": ["context", "build", "validate", "publish_pypi"],
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 30,
                "permissions": {"contents": "read"},
                "steps": [
                    {"uses": "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"},
                    {"uses": "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97"},
                    {"run": "python -m scripts.check_release_consumer"},
                    {
                        "if": "always()",
                        "uses": "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
                        "with": {
                            "path": "consumer-evidence.json",
                            "if-no-files-found": "error",
                            "retention-days": "90",
                        },
                    },
                ],
            },
            "promotion_evidence": {
                "if": "always() && github.event_name == 'push'",
                "needs": ["build", "validate", "attest", "publish_pypi", "consumer_pypi", "github_release"],
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 10,
                "permissions": {"contents": "read"},
                "steps": [
                    {
                        "id": "evidence_tools",
                        "continue-on-error": True,
                        "uses": "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
                    },
                    {
                        "id": "candidate",
                        "continue-on-error": True,
                        "uses": "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
                    },
                    {"uses": "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97"},
                    {"run": "python -m scripts.release_promotion_evidence --candidate-unavailable"},
                    {
                        "if": "always()",
                        "uses": "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
                        "with": {
                            "path": "promotion-evidence.json",
                            "if-no-files-found": "error",
                            "retention-days": "90",
                        },
                    },
                ],
            },
            "attest": {
                "if": "always() && needs.build.result == 'success' && needs.validate.result == 'success' && (github.event_name == 'push' || inputs.mode == 'testpypi')",
                "needs": ["build", "validate"],
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 15,
                "permissions": {"attestations": "write", "id-token": "write"},
                "steps": [
                    {"uses": "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"},
                    _sealed_distribution_step(),
                    {
                        "uses": "actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6",
                        "with": {"subject-path": "release-dist"},
                    },
                ],
            },
            "publish_testpypi": {
                "if": "always() && needs.build.result == 'success' && needs.validate.result == 'success' && needs.attest.result == 'success' && github.event_name == 'workflow_dispatch' && inputs.mode == 'testpypi'",
                "needs": ["build", "validate", "attest"],
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 15,
                "environment": "testpypi",
                "permissions": {"id-token": "write"},
                "steps": [
                    {"uses": "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"},
                    _sealed_distribution_step(),
                    {
                        "uses": "pypa/gh-action-pypi-publish@ec4db0b4ddc65acdf4bff5fa45ac92d78b56bdf0",
                        "with": {"packages-dir": "release-dist"},
                    },
                ],
            },
            "publish_pypi": {
                "if": "always() && needs.build.result == 'success' && needs.validate.result == 'success' && needs.attest.result == 'success' && github.event_name == 'push'",
                "needs": ["build", "validate", "attest"],
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 15,
                "environment": "pypi",
                "permissions": {"id-token": "write"},
                "steps": [
                    {"uses": "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"},
                    _sealed_distribution_step(),
                    {
                        "uses": "pypa/gh-action-pypi-publish@ec4db0b4ddc65acdf4bff5fa45ac92d78b56bdf0",
                        "with": {"packages-dir": "release-dist"},
                    },
                ],
            },
            "github_release": {
                "if": "always() && needs.build.result == 'success' && needs.validate.result == 'success' && needs.attest.result == 'success' && needs.publish_pypi.result == 'success' && needs.consumer_pypi.result == 'success' && github.event_name == 'push'",
                "needs": ["build", "validate", "attest", "publish_pypi", "consumer_pypi"],
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 15,
                "permissions": {"contents": "write"},
                "steps": [
                    {"uses": "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"},
                    _sealed_distribution_step(),
                    {"run": "gh release create $GITHUB_REF_NAME release-dist/* --generate-notes"},
                ],
            },
        },
    }


def test_policy_accepts_separate_artifact_only_authority_jobs() -> None:
    report = release_workflow_policy.validate_document(_workflow())

    assert report.ok is True
    assert report.findings == ()


def test_policy_rejects_obsolete_build_verifier_invocation() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    build = jobs["build"]
    assert isinstance(build, dict)
    steps = build["steps"]
    assert isinstance(steps, list)
    step = steps[0]
    assert isinstance(step, dict)
    step["run"] = str(step["run"]).replace(
        "--controller-root controller --expected-selection expected.json", "--repo-root ."
    )

    report = release_workflow_policy.validate_document(workflow)

    assert ("RWF028", "build") in {(finding.code, finding.job) for finding in report.findings}


def test_approval_policy_rejects_an_operator_supplied_archive_url() -> None:
    document = yaml.safe_load(
        (release_workflow_policy.REPO_ROOT / release_workflow_policy.APPROVAL_WORKFLOW_PATH).read_text(encoding="utf-8")
    )
    job = document["jobs"]["approve"]
    for step in job["steps"]:
        if step.get("id") == "input_token":
            step["with"]["repositories"] = "another-repository"

    report = release_workflow_policy.validate_approval_document(document)

    assert ("RWA006", "approve") in {(finding.code, finding.job) for finding in report.findings}


@pytest.mark.parametrize(
    "predicate",
    [
        ".repository.id == 1378745365",
        ".repository.owner.id == 1717694",
        ".head_repository.id == 1378745365",
        ".workflow_id == 362870199",
        '.event == "workflow_dispatch"',
        '.head_branch == "main"',
        '.path == ".github/workflows/retain-release-input.yml@main"',
        '.status == "completed" and .conclusion == "success"',
    ],
)
def test_approval_policy_requires_exact_private_producer_run_identity(predicate: str) -> None:
    document = yaml.load(
        (release_workflow_policy.REPO_ROOT / release_workflow_policy.APPROVAL_WORKFLOW_PATH).read_text(
            encoding="utf-8"
        ),
        Loader=yaml.BaseLoader,
    )
    steps = document["jobs"]["approve"]["steps"]
    step = next(item for item in steps if "actions/runs/$SOURCE_RUN_ID" in item.get("run", ""))
    assert predicate in step["run"]
    step["run"] = step["run"].replace(predicate, "true")

    report = release_workflow_policy.validate_approval_document(document)

    assert ("RWA007", "approve") in {(finding.code, finding.job) for finding in report.findings}


def test_approval_policy_rejects_identity_predicate_parked_in_comment() -> None:
    document = yaml.load(
        (release_workflow_policy.REPO_ROOT / release_workflow_policy.APPROVAL_WORKFLOW_PATH).read_text(
            encoding="utf-8"
        ),
        Loader=yaml.BaseLoader,
    )
    steps = document["jobs"]["approve"]["steps"]
    step = next(item for item in steps if "actions/runs/$SOURCE_RUN_ID" in item.get("run", ""))
    predicate = ".repository.id == 1378745365"
    step["run"] = step["run"].replace(predicate, "true", 1) + f"\n# {predicate}\n"

    report = release_workflow_policy.validate_approval_document(document)

    assert ("RWA007", "approve") in {(finding.code, finding.job) for finding in report.findings}


def test_approval_policy_rejects_commented_out_source_run_filter() -> None:
    document = yaml.load(
        (release_workflow_policy.REPO_ROOT / release_workflow_policy.APPROVAL_WORKFLOW_PATH).read_text(
            encoding="utf-8"
        ),
        Loader=yaml.BaseLoader,
    )
    steps = document["jobs"]["approve"]["steps"]
    step = next(item for item in steps if "actions/runs/$SOURCE_RUN_ID" in item.get("run", ""))
    assert "jq -e '\n" in step["run"]
    step["run"] = step["run"].replace("jq -e '\n", "# jq -e '\n", 1)

    report = release_workflow_policy.validate_approval_document(document)

    assert ("RWA007", "approve") in {(finding.code, finding.job) for finding in report.findings}


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required to execute the workflow's filter")
@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("repository", "id"), 1),
        (("repository", "owner", "id"), 1),
        (("head_repository", "id"), 1),
        (("workflow_id",), 1),
        (("event",), "push"),
        (("head_branch",), "feature"),
        (("path",), ".github/workflows/other.yml@main"),
        (("path",), ".github/workflows/retain-release-input.yml@main-unapproved"),
        (("status",), "in_progress"),
        (("conclusion",), "failure"),
        (("run_attempt",), "first"),
        (("head_sha",), "invalid"),
    ],
)
def test_approval_workflow_jq_rejects_wrong_source_run_identity(path: tuple[str, ...], replacement: object) -> None:
    workflow = yaml.load(
        (release_workflow_policy.REPO_ROOT / release_workflow_policy.APPROVAL_WORKFLOW_PATH).read_text(
            encoding="utf-8"
        ),
        Loader=yaml.BaseLoader,
    )
    command = next(
        step["run"]
        for step in workflow["jobs"]["approve"]["steps"]
        if "actions/runs/$SOURCE_RUN_ID" in step.get("run", "")
    )
    assert command.count("jq -e '") == 1
    program = command.split("jq -e '", 1)[1].split('\' <<< "$run"', 1)[0]
    run = {
        "repository": {"id": 1378745365, "owner": {"id": 1717694}},
        "head_repository": {"id": 1378745365},
        "workflow_id": 362870199,
        "event": "workflow_dispatch",
        "head_branch": "main",
        "path": ".github/workflows/retain-release-input.yml@main",
        "status": "completed",
        "conclusion": "success",
        "run_attempt": 1,
        "head_sha": "a" * 40,
    }

    accepted = subprocess.run(
        ["jq", "-e", program], input=json.dumps(run), text=True, capture_output=True, timeout=5, check=False
    )
    assert accepted.returncode == 0

    target = run
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = replacement
    rejected = subprocess.run(
        ["jq", "-e", program], input=json.dumps(run), text=True, capture_output=True, timeout=5, check=False
    )
    assert rejected.returncode != 0


@pytest.mark.parametrize(
    ("command", "expected_code"),
    [
        ("python scripts/release_approval_input.py --input approval-artifact/approval-input --repo-root .", "RWA009"),
        (
            "python scripts/release_approval_input.py --input approval-artifact/approval-input --controller-root controller",
            "RWA009",
        ),
        (
            "python scripts/release_approval_input.py --input approval-artifact/approval-input --controller-root . --expected-selection expected.json",
            "RWA009",
        ),
    ],
)
def test_approval_policy_rejects_invalid_verifier_invocation(command: str, expected_code: str) -> None:
    document = yaml.load(
        (release_workflow_policy.REPO_ROOT / release_workflow_policy.APPROVAL_WORKFLOW_PATH).read_text(
            encoding="utf-8"
        ),
        Loader=yaml.BaseLoader,
    )
    steps = document["jobs"]["approve"]["steps"]
    step = next(item for item in steps if "scripts/release_approval_input.py" in item.get("run", ""))
    step["run"] = step["run"].replace(
        "python scripts/release_approval_input.py --input approval-artifact/approval-input --repo-root .", command
    )

    report = release_workflow_policy.validate_approval_document(document)

    assert (expected_code, "approve") in {(finding.code, finding.job) for finding in report.findings}


def test_approval_policy_accepts_structural_cli_without_granting_approval_authority() -> None:
    document = yaml.load(
        (release_workflow_policy.REPO_ROOT / release_workflow_policy.APPROVAL_WORKFLOW_PATH).read_text(
            encoding="utf-8"
        ),
        Loader=yaml.BaseLoader,
    )
    steps = document["jobs"]["approve"]["steps"]
    step = next(item for item in steps if "scripts/release_approval_input.py" in item.get("run", ""))
    step["run"] = step["run"].replace(
        "--repo-root .", "--controller-root controller --expected-selection expected.json"
    )

    report = release_workflow_policy.validate_approval_document(document)

    assert report.ok is False
    assert {(finding.code, finding.job) for finding in report.findings} == {("RWA010", "approve")}


@pytest.mark.parametrize("environment", ["release-approval", {"name": "release-approval"}])
def test_approval_policy_rejects_validation_inside_initial_protected_job(environment: object) -> None:
    document = yaml.load(
        (release_workflow_policy.REPO_ROOT / release_workflow_policy.APPROVAL_WORKFLOW_PATH).read_text(
            encoding="utf-8"
        ),
        Loader=yaml.BaseLoader,
    )
    document["jobs"]["approve"]["environment"] = environment

    report = release_workflow_policy.validate_approval_document(document)

    assert report.ok is False
    assert ("RWA010", "approve") in {(finding.code, finding.job) for finding in report.findings}


def test_approval_policy_does_not_authorize_an_unimplemented_preflight() -> None:
    document = yaml.load(
        (release_workflow_policy.REPO_ROOT / release_workflow_policy.APPROVAL_WORKFLOW_PATH).read_text(
            encoding="utf-8"
        ),
        Loader=yaml.BaseLoader,
    )
    document["jobs"]["preflight"] = {
        "runs-on": "ubuntu-24.04",
        "permissions": {"contents": "read"},
        "steps": [{"run": "echo preflight"}],
    }
    document["jobs"]["approve"]["needs"] = ["preflight"]

    report = release_workflow_policy.validate_approval_document(document)

    assert report.ok is False
    assert ("RWA003", "workflow") in {(finding.code, finding.job) for finding in report.findings}


def test_policy_rejects_missing_nonempty_approval_tag_metadata_validation() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    context = jobs["context"]
    assert isinstance(context, dict)
    steps = context["steps"]
    assert isinstance(steps, list)
    metadata = steps[1]
    assert isinstance(metadata, dict)
    metadata["run"] = str(metadata["run"]).replace('[[ "$run" =~ ^[1-9][0-9]*$ ]]\n', "")

    report = release_workflow_policy.validate_document(workflow)

    assert ("RWF025", "context") in {(finding.code, finding.job) for finding in report.findings}


def test_policy_rejects_production_acquisition_without_the_manifest_digest() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    build = jobs["build"]
    assert isinstance(build, dict)
    steps = build["steps"]
    assert isinstance(steps, list)
    acquisition = steps[0]
    assert isinstance(acquisition, dict)
    acquisition["run"] = str(acquisition["run"]).replace("APPROVAL_MANIFEST_SHA256 ", "", 1)

    report = release_workflow_policy.validate_document(workflow)

    assert ("RWF026", "build") in {(finding.code, finding.job) for finding in report.findings}


def test_policy_rejects_unbounded_approval_input_requests() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    build = jobs["build"]
    assert isinstance(build, dict)
    steps = build["steps"]
    assert isinstance(steps, list)
    acquisition = steps[0]
    assert isinstance(acquisition, dict)
    acquisition["run"] = str(acquisition["run"]).replace("timeout 60s gh api", "gh api", 1)

    report = release_workflow_policy.validate_document(workflow)

    assert ("RWF026", "build") in {(finding.code, finding.job) for finding in report.findings}


@pytest.mark.parametrize("job_name", ["attest", "publish_testpypi", "publish_pypi", "github_release"])
def test_policy_rejects_source_checkout_in_an_authority_job(job_name: str) -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    job = jobs[job_name]
    assert isinstance(job, dict)
    steps = job["steps"]
    assert isinstance(steps, list)
    steps.append({"uses": "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"})

    report = release_workflow_policy.validate_document(workflow)

    assert report.ok is False
    assert ("RWF004", job_name) in {(finding.code, finding.job) for finding in report.findings}


def test_policy_rejects_authority_job_without_sealed_distribution_verification() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    job = jobs["publish_pypi"]
    assert isinstance(job, dict)
    steps = job["steps"]
    assert isinstance(steps, list)
    steps.pop(1)

    report = release_workflow_policy.validate_document(workflow)

    assert ("RWF017", "publish_pypi") in {(finding.code, finding.job) for finding in report.findings}


def test_policy_rejects_consumer_checkout_or_missing_evidence_retention() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    consumer = jobs["consumer_pypi"]
    assert isinstance(consumer, dict)
    steps = consumer["steps"]
    assert isinstance(steps, list)
    steps.append({"uses": "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"})

    report = release_workflow_policy.validate_document(workflow)

    assert ("RWF014", "consumer_pypi") in {(finding.code, finding.job) for finding in report.findings}


def test_policy_rejects_promotion_evidence_without_partial_run_support() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    promotion = jobs["promotion_evidence"]
    assert isinstance(promotion, dict)
    steps = promotion["steps"]
    assert isinstance(steps, list)
    run = steps[3]
    assert isinstance(run, dict)
    run["run"] = "python -m scripts.release_promotion_evidence"

    report = release_workflow_policy.validate_document(workflow)

    assert ("RWF015", "promotion_evidence") in {(finding.code, finding.job) for finding in report.findings}


def test_policy_rejects_missing_context_evidence_tools() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    context = jobs["context"]
    assert isinstance(context, dict)
    steps = context["steps"]
    assert isinstance(steps, list)
    steps.pop()

    report = release_workflow_policy.validate_document(workflow)

    assert ("RWF016", "context") in {(finding.code, finding.job) for finding in report.findings}


def test_policy_rejects_stored_index_credentials() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    job = jobs["publish_pypi"]
    assert isinstance(job, dict)
    job["env"] = {"PYPI_TOKEN": "${{ secrets.PYPI_TOKEN }}"}

    report = release_workflow_policy.validate_document(workflow)

    assert report.ok is False
    assert {(finding.code, finding.job) for finding in report.findings} == {("RWF005", "publish_pypi")}


def test_policy_rejects_production_tag_path_without_signature_verification() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    context = jobs["context"]
    assert isinstance(context, dict)
    context["steps"] = [{"run": "true"}]

    report = release_workflow_policy.validate_document(workflow)

    assert ("RWF012", "context") in {(finding.code, finding.job) for finding in report.findings}


def test_policy_rejects_dispatch_that_does_not_require_main() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    context = jobs["context"]
    assert isinstance(context, dict)
    steps = context["steps"]
    assert isinstance(steps, list)
    step = steps[0]
    assert isinstance(step, dict)
    step["run"] = (
        "timeout 60s gh api tags/$GITHUB_REF_NAME && jq '.verification.verified == true and .object.sha == $GITHUB_SHA'"
    )

    report = release_workflow_policy.validate_document(workflow)

    assert ("RWF013", "context") in {(finding.code, finding.job) for finding in report.findings}


def test_policy_rejects_build_that_cannot_follow_a_skipped_dispatch_approval() -> None:
    """A deliberate non-production dispatch must not inherit a skipped-job failure."""
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    build = jobs["build"]
    assert isinstance(build, dict)
    build.pop("if")

    report = release_workflow_policy.validate_document(workflow)

    assert ("RWF012", "build") in {(finding.code, finding.job) for finding in report.findings}


def test_policy_rejects_scanner_install_to_its_extraction_directory() -> None:
    """The scanner must be a runnable file, not the directory created for extraction."""
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    build = jobs["build"]
    assert isinstance(build, dict)
    steps = build["steps"]
    assert isinstance(steps, list)
    step = next(item for item in steps if item.get("name") == "Install the pinned export secret scanner")
    assert isinstance(step, dict)
    command = step["run"]
    assert isinstance(command, str)
    step["run"] = command.replace('"$scanner_binary" version', '"$scanner_root" version')

    report = release_workflow_policy.validate_document(workflow)

    assert ("RWF022", "build") in {(finding.code, finding.job) for finding in report.findings}


def test_repository_validator_rejects_checked_in_obsolete_approval_invocation(tmp_path: Path) -> None:
    workflow_path = tmp_path / ".github" / "workflows" / "release.yml"
    workflow_path.parent.mkdir(parents=True)
    workflow_path.write_text(yaml.dump(_workflow()), encoding="utf-8")
    shutil.copy(
        release_workflow_policy.REPO_ROOT / release_workflow_policy.APPROVAL_WORKFLOW_PATH,
        workflow_path.parent / "release-approval.yml",
    )

    report = release_workflow_policy.validate_repository(tmp_path)

    assert report.ok is False
    assert ("RWA009", "approve") in {(finding.code, finding.job) for finding in report.findings}


def test_checked_in_release_workflow_reports_obsolete_verifier_invocations() -> None:
    report = release_workflow_policy.validate_repository(release_workflow_policy.REPO_ROOT)

    assert report.ok is False
    assert {("RWA009", "approve"), ("RWA010", "approve"), ("RWF028", "build")} <= {
        (finding.code, finding.job) for finding in report.findings
    }


def test_checked_in_workflow_preserves_the_candidate_and_renderer_layout() -> None:
    workflow = yaml.safe_load((release_workflow_policy.REPO_ROOT / ".github/workflows/release.yml").read_text())
    jobs = workflow["jobs"]
    candidate_name = "release-candidate-${{ github.run_id }}-${{ github.run_attempt }}-${{ github.sha }}"

    build_upload = jobs["build"]["steps"][-2]["with"]
    assert build_upload["path"].split() == [
        "candidate/",
        "scripts/check_release_consumer.py",
        "scripts/release_bundle.py",
        "scripts/runtime_license_inventory.py",
        "scripts/release_consumer.py",
        "scripts/release_promotion_evidence.py",
        "scripts/release_wheelhouse.py",
        "pyproject.toml",
    ]
    for job_name in ("validate", "attest", "publish_testpypi", "publish_pypi", "github_release"):
        download = next(
            step["with"]
            for step in jobs[job_name]["steps"]
            if step.get("uses", "").startswith("actions/download-artifact@")
        )
        assert download["name"] == candidate_name
        assert download["path"] == "."
    for job_name in ("consumer_pypi", "promotion_evidence"):
        download = next(
            step["with"] for step in jobs[job_name]["steps"] if step.get("with", {}).get("name") == candidate_name
        )
        assert download["path"] == "release-inputs"
