"""Contracts for the least-privilege release workflow policy."""

import shutil
from pathlib import Path

import pytest
import yaml

from scripts import release_workflow_policy

pytestmark = pytest.mark.unit

_RECORDED = release_workflow_policy.recorded_boundary_actions(
    (release_workflow_policy.REPO_ROOT / release_workflow_policy.EVIDENCE_PATH).read_text(encoding="utf-8")
)
_STALE_REVISION = "0" * 40


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


_CANDIDATE = "release-candidate-${{ github.run_id }}-${{ github.sha }}"
_PUSH_ONLY = "github.event_name == 'push'"


def _download(path: str = ".") -> dict[str, object]:
    return {
        "uses": "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
        "with": {"name": _CANDIDATE, "path": path},
    }


def _consumer(needs: list[str], endpoint: str, host: str) -> dict[str, object]:
    return {
        "if": _PUSH_ONLY,
        "needs": needs,
        "runs-on": "ubuntu-24.04",
        "timeout-minutes": 30,
        "permissions": {"contents": "read"},
        "steps": [
            _download("release-inputs"),
            {"uses": "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97"},
            {
                "run": (
                    "python -m scripts.check_release_consumer \\\n"
                    f'  --index-endpoint "{endpoint}$VERSION/json" \\\n'
                    f"  --download-host {host} \\\n"
                    "  --allow-live-index"
                )
            },
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
    }


def _workflow() -> dict[str, object]:
    return {
        "on": {"workflow_dispatch": None, "push": {"tags": ["v*.*.*"]}},
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
                "steps": [
                    {
                        "run": (
                            'if [ "$GITHUB_EVENT_NAME" = "workflow_dispatch" ]; then '
                            'test "$GITHUB_REF" = "refs/heads/main"; fi && '
                            "timeout 60s gh api tags/$GITHUB_REF_NAME && "
                            "jq '.verification.verified == true and .object.sha == $GITHUB_SHA'\n"
                            'git merge-base --is-ancestor "$GITHUB_SHA" refs/remotes/origin/main'
                        )
                    },
                    {
                        "uses": "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
                        "with": {
                            "name": "release-evidence-tools-${{ github.run_id }}-${{ github.sha }}",
                            "path": "scripts/release_promotion_evidence.py scripts/_release_identity.py scripts/release_bundle.py scripts/release_consumer.py scripts/release_wheelhouse.py pyproject.toml",
                            "if-no-files-found": "error",
                            "retention-days": "90",
                        },
                    },
                ],
            },
            "compatibility": {
                "needs": "context",
                "uses": "./.github/workflows/compatibility.yml",
                "permissions": {"contents": "read"},
            },
            "build": {
                "needs": ["context", "compatibility"],
                "outputs": {"bundle_manifest_sha256": "${{ steps.bundle_manifest.outputs.sha256 }}"},
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 30,
                "permissions": {"contents": "read"},
                "steps": [
                    {
                        "name": "Install the pinned export secret scanner",
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
                        "run": "uv run python scripts/check_public_candidate.py --output-dir candidate",
                    },
                    {
                        "uses": "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
                        "with": {"name": _CANDIDATE, "path": "candidate/"},
                    },
                    {
                        "id": "bundle_manifest",
                        "run": 'echo "sha256=$(sha256sum candidate/bundle/SHA256SUMS)" >> "$GITHUB_OUTPUT"',
                    },
                ],
            },
            "validate": {
                "needs": "build",
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 30,
                "permissions": {"contents": "read"},
                "steps": [_download()],
            },
            "attest": {
                "if": _PUSH_ONLY,
                "needs": ["build", "validate"],
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 15,
                "permissions": {"attestations": "write", "id-token": "write"},
                "steps": [
                    _download(),
                    _sealed_distribution_step(),
                    {
                        "uses": _RECORDED["attest"],
                        "with": {"subject-path": "release-dist"},
                    },
                ],
            },
            "publish_testpypi": {
                "if": _PUSH_ONLY,
                "needs": ["build", "validate", "attest"],
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 15,
                "environment": "testpypi",
                "permissions": {"id-token": "write"},
                "steps": [
                    _download(),
                    _sealed_distribution_step(),
                    {
                        "uses": _RECORDED["publish_testpypi"],
                        "with": {"packages-dir": "release-dist", "attestations": True},
                    },
                ],
            },
            "consumer_testpypi": _consumer(
                ["context", "build", "validate", "publish_testpypi"],
                "https://test.pypi.org/pypi/fieldkit-cli/",
                "test-files.pythonhosted.org",
            ),
            "publish_pypi": {
                "if": _PUSH_ONLY,
                "needs": ["build", "validate", "attest", "consumer_testpypi"],
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 15,
                "environment": "pypi",
                "permissions": {"id-token": "write"},
                "steps": [
                    _download(),
                    _sealed_distribution_step(),
                    {
                        "uses": _RECORDED["publish_pypi"],
                        "with": {"packages-dir": "release-dist", "attestations": True},
                    },
                ],
            },
            "consumer_pypi": _consumer(
                ["context", "build", "validate", "publish_pypi"],
                "https://pypi.org/pypi/fieldkit-cli/",
                "files.pythonhosted.org",
            ),
            "github_release": {
                "if": _PUSH_ONLY,
                "needs": ["build", "validate", "attest", "publish_pypi", "consumer_pypi"],
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 15,
                "permissions": {"contents": "write"},
                "steps": [
                    _download(),
                    _sealed_distribution_step(),
                    {"run": "gh release create $GITHUB_REF_NAME release-dist/* --generate-notes"},
                ],
            },
            "promotion_evidence": {
                "if": "always() && github.event_name == 'push'",
                "needs": [
                    "build",
                    "validate",
                    "attest",
                    "publish_testpypi",
                    "consumer_testpypi",
                    "publish_pypi",
                    "consumer_pypi",
                    "github_release",
                ],
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 10,
                "permissions": {"contents": "read"},
                "steps": [
                    {
                        "id": "evidence_tools",
                        "continue-on-error": True,
                        "uses": "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
                    },
                    {**_download("release-inputs"), "id": "candidate", "continue-on-error": True},
                    {"uses": "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97"},
                    {"run": "python -m scripts.release_promotion_evidence --candidate-unavailable"},
                    {
                        "run": (
                            f"boundary_actions = {{'attest': '{_RECORDED['attest']}', "
                            f"'publish_testpypi': '{_RECORDED['publish_testpypi']}', "
                            f"'publish_pypi': '{_RECORDED['publish_pypi']}'}}"
                        )
                    },
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
        },
    }


def _job(workflow: dict[str, object], name: str) -> dict[str, object]:
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    job = jobs[name]
    assert isinstance(job, dict)
    return job


def test_policy_accepts_separate_artifact_only_authority_jobs() -> None:
    report = release_workflow_policy.validate_document(_workflow())

    assert report.ok is True
    assert report.findings == ()


@pytest.mark.parametrize("dispatch", [{"inputs": {"mode": {"type": "choice"}}}, {"inputs": {}}])
def test_policy_rejects_dispatch_inputs(dispatch: dict[str, object]) -> None:
    workflow = _workflow()
    triggers = workflow["on"]
    assert isinstance(triggers, dict)
    triggers["workflow_dispatch"] = dispatch

    report = release_workflow_policy.validate_document(workflow)

    assert {(finding.code, finding.job) for finding in report.findings} == {("RWF001", "workflow")}


def test_policy_rejects_a_production_tag_off_main() -> None:
    workflow = _workflow()
    step = _job(workflow, "context")["steps"][0]
    assert isinstance(step, dict)
    step["run"] = str(step["run"]).replace("git merge-base --is-ancestor", "git merge-base")

    report = release_workflow_policy.validate_document(workflow)

    assert {(finding.code, finding.job) for finding in report.findings} == {("RWF030", "context")}


@pytest.mark.parametrize(
    "step_name", ["Install the pinned export secret scanner", "Build the one retained public candidate"]
)
def test_policy_rejects_a_conditional_candidate_build(step_name: str) -> None:
    workflow = _workflow()
    steps = _job(workflow, "build")["steps"]
    assert isinstance(steps, list)
    next(step for step in steps if step.get("name") == step_name)["if"] = "github.event_name == 'workflow_dispatch'"

    report = release_workflow_policy.validate_document(workflow)

    assert {(finding.code, finding.job) for finding in report.findings} == {("RWF026", "build")}


@pytest.mark.parametrize("job_name", ["build", "validate", "compatibility"])
def test_policy_rejects_a_condition_that_could_run_past_a_failed_dependency(job_name: str) -> None:
    workflow = _workflow()
    _job(workflow, job_name)["if"] = "always()"

    report = release_workflow_policy.validate_document(workflow)

    code = "RWF027" if job_name == "compatibility" else "RWF012"
    assert {(finding.code, finding.job) for finding in report.findings} == {(code, job_name)}


@pytest.mark.parametrize(
    "candidate_name",
    [
        "release-candidate-${{ github.run_id }}-${{ github.run_attempt }}-${{ github.sha }}",
        "release-candidate-${{ github.sha }}",
    ],
)
@pytest.mark.parametrize("job_name", ["build", "publish_pypi"])
def test_policy_rejects_an_attempt_scoped_candidate_artifact(job_name: str, candidate_name: str) -> None:
    workflow = _workflow()
    steps = _job(workflow, job_name)["steps"]
    assert isinstance(steps, list)
    for step in steps:
        if isinstance(step.get("with"), dict) and step["with"].get("name") == _CANDIDATE:
            step["with"]["name"] = candidate_name

    report = release_workflow_policy.validate_document(workflow)

    assert {(finding.code, finding.job) for finding in report.findings} == {("RWF031", job_name)}


def test_policy_rejects_overwriting_the_candidate_artifact() -> None:
    workflow = _workflow()
    steps = _job(workflow, "build")["steps"]
    assert isinstance(steps, list)
    upload = next(step for step in steps if str(step.get("uses", "")).startswith("actions/upload-artifact@"))
    upload["with"]["overwrite"] = "true"

    report = release_workflow_policy.validate_document(workflow)

    assert {(finding.code, finding.job) for finding in report.findings} == {("RWF031", "build")}


@pytest.mark.parametrize(
    ("job_name", "replacement"),
    [
        ("consumer_testpypi", ("https://test.pypi.org/", "https://pypi.org/")),
        ("consumer_testpypi", ("test-files.pythonhosted.org", "files.pythonhosted.org")),
        ("consumer_pypi", ("files.pythonhosted.org", "files.pythonhosted.org --download-host example.com")),
    ],
)
def test_policy_binds_each_consumer_to_its_own_index(job_name: str, replacement: tuple[str, str]) -> None:
    workflow = _workflow()
    steps = _job(workflow, job_name)["steps"]
    assert isinstance(steps, list)
    for step in steps:
        if isinstance(step.get("run"), str):
            step["run"] = step["run"].replace(*replacement)

    report = release_workflow_policy.validate_document(workflow)

    assert {(finding.code, finding.job) for finding in report.findings} == {("RWF014", job_name)}


def test_policy_requires_the_testpypi_consumer_before_production_publication() -> None:
    workflow = _workflow()
    _job(workflow, "publish_pypi")["needs"] = ["build", "validate", "attest"]

    report = release_workflow_policy.validate_document(workflow)

    assert {(finding.code, finding.job) for finding in report.findings} == {("RWF008", "publish_pypi")}


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


def test_repository_validator_accepts_checked_in_yaml_shape(tmp_path: Path) -> None:
    workflow_path = tmp_path / ".github" / "workflows" / "release.yml"
    workflow_path.parent.mkdir(parents=True)
    workflow_path.write_text(yaml.dump(_workflow()), encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    shutil.copy(
        release_workflow_policy.REPO_ROOT / release_workflow_policy.EVIDENCE_PATH,
        tmp_path / release_workflow_policy.EVIDENCE_PATH,
    )

    report = release_workflow_policy.validate_repository(tmp_path)

    assert report.ok is True


@pytest.mark.parametrize(
    ("job_name", "boundary"),
    [("attest", "attest"), ("publish_testpypi", "publish_testpypi"), ("publish_pypi", "publish_pypi")],
)
def test_policy_rejects_an_action_revision_the_evidence_does_not_record(job_name: str, boundary: str) -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    steps = jobs[job_name]["steps"]
    assert isinstance(steps, list)
    action = _RECORDED[boundary].partition("@")[0]
    for step in steps:
        if step.get("uses") == _RECORDED[boundary]:
            step["uses"] = f"{action}@{_STALE_REVISION}"

    report = release_workflow_policy.validate_recorded_actions(workflow, _RECORDED)

    assert report.findings == (
        release_workflow_policy.Finding(
            "RWF028", job_name, f"job must run the action promotion evidence records for {boundary!r}"
        ),
    )


def test_policy_rejects_unavailable_candidate_evidence_with_a_stale_action_revision() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    steps = jobs["promotion_evidence"]["steps"]
    assert isinstance(steps, list)
    publisher = _RECORDED["publish_pypi"]
    for step in steps:
        if isinstance(step.get("run"), str):
            step["run"] = step["run"].replace(publisher, f"{publisher.partition('@')[0]}@{_STALE_REVISION}")

    report = release_workflow_policy.validate_recorded_actions(workflow, _RECORDED)

    assert {(finding.code, finding.job) for finding in report.findings} == {("RWF028", "promotion_evidence")}


def test_policy_rejects_unavailable_candidate_evidence_that_swaps_boundary_actions() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    steps = jobs["promotion_evidence"]["steps"]
    assert isinstance(steps, list)
    attest, publisher = _RECORDED["attest"], _RECORDED["publish_pypi"]
    for step in steps:
        if isinstance(step.get("run"), str) and attest in step["run"]:
            step["run"] = step["run"].replace(attest, "SWAP").replace(publisher, attest).replace("SWAP", publisher)

    report = release_workflow_policy.validate_recorded_actions(workflow, _RECORDED)

    assert {(finding.code, finding.job) for finding in report.findings} == {("RWF028", "promotion_evidence")}


def test_recorded_boundary_actions_reads_the_renderer_table_without_importing_it() -> None:
    source = "import missing_module\n_BOUNDARY_ACTIONS = {'publish_pypi': 'owner/action@" + "a" * 40 + "'}\n"

    recorded = release_workflow_policy.recorded_boundary_actions(source)

    assert recorded == {"publish_pypi": "owner/action@" + "a" * 40}


@pytest.mark.parametrize(
    "source",
    [
        "_OTHER = {}\n",
        "_BOUNDARY_ACTIONS = dict(publish_pypi='owner/action@main')\n",
        "_BOUNDARY_ACTIONS = {'publish_pypi': 1}\n",
        "_BOUNDARY_ACTIONS = {\n",
    ],
)
def test_unreadable_recorded_actions_fail_the_policy(source: str) -> None:
    recorded = release_workflow_policy.recorded_boundary_actions(source)

    report = release_workflow_policy.validate_recorded_actions(_workflow(), recorded)

    assert recorded == {}
    assert {finding.job for finding in report.findings} == {
        "attest",
        "publish_pypi",
        "publish_testpypi",
        "promotion_evidence",
    }


@pytest.mark.parametrize("job_name", ["publish_testpypi", "publish_pypi"])
@pytest.mark.parametrize("attestations", [None, False, "false"])
def test_policy_requires_explicit_pep740_attestations(job_name: str, attestations: object) -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    for step in jobs[job_name]["steps"]:
        if isinstance(step.get("with"), dict) and "attestations" in step["with"]:
            if attestations is None:
                del step["with"]["attestations"]
            else:
                step["with"]["attestations"] = attestations

    report = release_workflow_policy.validate_document(workflow)

    assert report.findings == (
        release_workflow_policy.Finding(
            "RWF029", job_name, "package publication must explicitly attach PEP 740 attestations"
        ),
    )


@pytest.mark.parametrize("job_name", ["publish_testpypi", "publish_pypi"])
def test_policy_requires_attestations_on_the_publisher_step_itself(job_name: str) -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    for step in jobs[job_name]["steps"]:
        uses = step.get("uses")
        if isinstance(uses, str) and uses.startswith("pypa/gh-action-pypi-publish@"):
            step["with"]["attestations"] = False
        elif isinstance(uses, str) and uses.startswith("actions/download-artifact@"):
            step["with"] = {"attestations": True}

    report = release_workflow_policy.validate_document(workflow)

    assert report.findings == (
        release_workflow_policy.Finding(
            "RWF029", job_name, "package publication must explicitly attach PEP 740 attestations"
        ),
    )


def test_checked_in_release_workflow_satisfies_policy() -> None:
    report = release_workflow_policy.validate_repository(release_workflow_policy.REPO_ROOT)

    assert report.ok is True


def test_checked_in_workflow_preserves_the_candidate_and_renderer_layout() -> None:
    workflow = yaml.safe_load((release_workflow_policy.REPO_ROOT / ".github/workflows/release.yml").read_text())
    jobs = workflow["jobs"]
    candidate_name = _CANDIDATE

    build_upload = jobs["build"]["steps"][-2]["with"]
    assert build_upload["path"].split() == [
        "candidate/",
        "scripts/check_release_consumer.py",
        "scripts/_release_identity.py",
        "scripts/release_bundle.py",
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
    for job_name in ("consumer_testpypi", "consumer_pypi", "promotion_evidence"):
        download = next(
            step["with"] for step in jobs[job_name]["steps"] if step.get("with", {}).get("name") == candidate_name
        )
        assert download["path"] == "release-inputs"
