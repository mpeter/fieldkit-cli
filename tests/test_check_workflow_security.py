"""Contracts for public GitHub Actions trust and dependency pinning."""

from pathlib import Path

import check_workflow_security
import pytest

pytestmark = pytest.mark.unit

_PIN = "3d3c42e5aac5ba805825da76410c181273ba90b1"


def _workflow(tmp_path: Path, body: str, *, name: str = "ci.yml") -> Path:
    path = tmp_path / ".github" / "workflows" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _codes(tmp_path: Path) -> set[str]:
    return {finding.code for finding in check_workflow_security.validate(tmp_path).findings}


def test_missing_top_level_permissions_fails(tmp_path: Path) -> None:
    """A workflow must declare its token baseline instead of inheriting repository defaults."""
    _workflow(
        tmp_path,
        """name: CI
on: pull_request
jobs:
  test:
    runs-on: ubuntu-24.04
    steps: []
""",
    )

    assert _codes(tmp_path) == {"WF001"}


def test_missing_trigger_fails(tmp_path: Path) -> None:
    """A workflow without an event contract must fail closed."""
    _workflow(
        tmp_path,
        """name: CI
permissions: {}
jobs:
  test:
    runs-on: ubuntu-24.04
    steps: []
""",
    )

    assert _codes(tmp_path) == {"WF011"}


def test_unclassified_trigger_fails_closed(tmp_path: Path) -> None:
    """A new event cannot gain implicit trust before policy classifies it."""
    _workflow(
        tmp_path,
        """name: CI
on: discussion_comment
permissions: {}
jobs:
  test:
    runs-on: ubuntu-24.04
    steps: []
""",
    )

    assert _codes(tmp_path) == {"WF011"}


def test_top_level_write_permission_fails(tmp_path: Path) -> None:
    """Repository-wide write authority is never an acceptable workflow default."""
    _workflow(
        tmp_path,
        """name: CI
on: push
permissions:
  contents: write
jobs:
  test:
    runs-on: ubuntu-24.04
    steps: []
""",
    )

    assert _codes(tmp_path) == {"WF002"}


def test_mutable_external_action_reference_fails(tmp_path: Path) -> None:
    """A moving action tag cannot silently replace reviewed executable code."""
    _workflow(
        tmp_path,
        """name: CI
on: push
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v7  # v7.0.1
""",
    )

    assert _codes(tmp_path) == {"WF003"}


def test_external_action_requires_version_comment(tmp_path: Path) -> None:
    """An immutable action pin retains a human-reviewable upstream version."""
    _workflow(
        tmp_path,
        f"""name: CI
on: push
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@{_PIN}
""",
    )

    assert _codes(tmp_path) == {"WF010"}


@pytest.mark.parametrize(
    "options",
    [
        "name: report-${{ github.run_id }}-${{ github.sha }}\n          path: reports/result.json\n          retention-days: 14",
        "name: report-${{ github.run_id }}-${{ github.sha }}\n          path: reports/result.json\n          if-no-files-found: error",
        "name: report\n          path: reports/result.json\n          if-no-files-found: error\n          retention-days: 14",
    ],
)
def test_artifact_upload_requires_missing_policy_retention_and_attributable_name(tmp_path: Path, options: str) -> None:
    """An archive cannot silently disappear, persist indefinitely, or collide across revisions."""
    _workflow(
        tmp_path,
        f"""name: CI
on: pull_request
permissions:
  contents: read
jobs:
  evidence:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/upload-artifact@{_PIN}  # v7.0.1
        with:
          {options}
""",
    )

    assert _codes(tmp_path) == {"WF012"}


def test_attributable_fail_closed_artifact_upload_passes(tmp_path: Path) -> None:
    """A revision-and-run-specific archive with bounded retention satisfies evidence policy."""
    _workflow(
        tmp_path,
        f"""name: CI
on: pull_request
permissions:
  contents: read
jobs:
  evidence:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/upload-artifact@{_PIN}  # v7.0.1
        with:
          name: report-${{{{ github.run_id }}}}-${{{{ github.sha }}}}
          path: reports/result.json
          if-no-files-found: error
          retention-days: 14
""",
    )

    assert _codes(tmp_path) == set()


def test_mutable_docker_action_reference_fails(tmp_path: Path) -> None:
    """A Docker action must use a content digest rather than a mutable tag."""
    _workflow(
        tmp_path,
        """name: CI
on: push
permissions: {}
jobs:
  test:
    runs-on: ubuntu-24.04
    steps:
      - uses: docker://example/tool:latest  # v1.0.0
""",
    )

    assert _codes(tmp_path) == {"WF003"}


def test_self_hosted_runner_fails(tmp_path: Path) -> None:
    """Internet-proposed code cannot execute on a private or persistent runner."""
    _workflow(
        tmp_path,
        """name: CI
on: push
permissions: {}
jobs:
  test:
    runs-on: [self-hosted, linux]
    steps: []
""",
    )

    assert _codes(tmp_path) == {"WF004"}


def test_self_hosted_matrix_runner_fails(tmp_path: Path) -> None:
    """Matrix expansion cannot conceal a self-hosted runner label."""
    _workflow(
        tmp_path,
        """name: CI
on: pull_request
permissions: {}
jobs:
  test:
    strategy:
      matrix:
        os: [ubuntu-24.04, self-hosted]
    runs-on: ${{ matrix.os }}
    steps: []
""",
    )

    assert _codes(tmp_path) == {"WF004"}


def test_unverifiable_runner_expression_fails(tmp_path: Path) -> None:
    """Dynamic runner selection fails when policy cannot prove hosted execution."""
    _workflow(
        tmp_path,
        """name: CI
on: pull_request
permissions: {}
jobs:
  test:
    runs-on: ${{ inputs.runner }}
    steps: []
""",
    )

    assert _codes(tmp_path) == {"WF004"}


def test_pull_request_secret_reference_and_job_write_fail(tmp_path: Path) -> None:
    """A pull-request job cannot receive a secret or repository write scope."""
    _workflow(
        tmp_path,
        """name: CI
on: pull_request
permissions:
  contents: read
jobs:
  test:
    permissions:
      issues: write
    runs-on: ubuntu-24.04
    steps:
      - env:
          TOKEN: ${{ secrets.PROJECT_TOKEN }}
        run: python3 -m pytest
""",
    )

    assert _codes(tmp_path) == {"WF005", "WF006"}


def test_codeql_pull_request_job_may_upload_security_events(tmp_path: Path) -> None:
    """CodeQL receives only its documented result-upload permission on pull requests."""
    _workflow(
        tmp_path,
        f"""name: CodeQL
on: pull_request
permissions:
  contents: read
jobs:
  analyze:
    permissions:
      contents: read
      security-events: write
    runs-on: ubuntu-24.04
    steps:
      - uses: github/codeql-action/init@{_PIN}  # v3.38.0
      - uses: github/codeql-action/analyze@{_PIN}  # v3.38.0
""",
    )

    assert _codes(tmp_path) == set()


def test_arbitrary_pull_request_job_cannot_upload_security_events(tmp_path: Path) -> None:
    """The CodeQL exception cannot grant write authority to unrelated jobs."""
    _workflow(
        tmp_path,
        """name: Pretend scanner
on: pull_request
permissions:
  contents: read
jobs:
  analyze:
    permissions:
      security-events: write
    runs-on: ubuntu-24.04
    steps:
      - run: python3 scanner.py
""",
    )

    assert _codes(tmp_path) == {"WF005"}


def test_codeql_permission_exception_rejects_tokenized_shell_step(tmp_path: Path) -> None:
    """Official action names cannot disguise an arbitrary token-bearing PR job."""
    _workflow(
        tmp_path,
        f"""name: CodeQL with shell
on: pull_request
permissions:
  contents: read
jobs:
  analyze:
    permissions:
      security-events: write
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@{_PIN}  # v7.0.1
      - uses: github/codeql-action/init@{_PIN}  # v3.38.0
      - env:
          TOKEN: ${{{{ github.token }}}}
        run: python3 upload_result.py
      - uses: github/codeql-action/analyze@{_PIN}  # v3.38.0
""",
    )

    assert _codes(tmp_path) == {"WF005", "WF006"}


def test_pull_request_write_all_and_spaced_secret_reference_fail(tmp_path: Path) -> None:
    """Alternate formatting cannot bypass broad-write or secret detection."""
    _workflow(
        tmp_path,
        """name: CI
on: pull_request
permissions:
  contents: read
jobs:
  test:
    permissions: write-all
    runs-on: ubuntu-24.04
    steps:
      - env:
          TOKEN: ${{secrets.PROJECT_TOKEN}}
        run: python3 -m pytest
""",
    )

    assert _codes(tmp_path) == {"WF005", "WF006"}


def test_pull_request_bracket_secret_reference_fails(tmp_path: Path) -> None:
    """Bracket-style secret expressions remain forbidden on pull requests."""
    _workflow(
        tmp_path,
        """name: CI
on: pull_request
permissions: {}
jobs:
  test:
    runs-on: ubuntu-24.04
    steps:
      - env:
          TOKEN: ${{ secrets['PROJECT_TOKEN'] }}
        run: python3 -m pytest
""",
    )

    assert _codes(tmp_path) == {"WF006"}


def test_pull_request_reusable_workflow_cannot_inherit_secrets(tmp_path: Path) -> None:
    """A reusable workflow called by a pull request cannot inherit secrets."""
    _workflow(
        tmp_path,
        """name: CI
on: pull_request
permissions: {}
jobs:
  test:
    uses: ./.github/workflows/reusable.yml
    secrets: inherit
""",
    )

    assert _codes(tmp_path) == {"WF006"}


def test_pull_request_environment_fails(tmp_path: Path) -> None:
    """Pull-request jobs cannot target environments that may expose credentials."""
    _workflow(
        tmp_path,
        """name: CI
on: pull_request
permissions: {}
jobs:
  test:
    environment: production
    runs-on: ubuntu-24.04
    steps: []
""",
    )

    assert _codes(tmp_path) == {"WF006"}


def test_write_all_job_fails_on_trusted_trigger_too(tmp_path: Path) -> None:
    """Even trusted events must spell out narrow job permissions."""
    _workflow(
        tmp_path,
        """name: CI
on: push
permissions: {}
jobs:
  test:
    permissions: write-all
    runs-on: ubuntu-24.04
    steps: []
""",
    )

    assert _codes(tmp_path) == {"WF005"}


def test_event_expression_interpolated_into_run_fails(tmp_path: Path) -> None:
    """Untrusted event fields cannot be interpolated into generated shell text."""
    _workflow(
        tmp_path,
        """name: CI
on: pull_request
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-24.04
    steps:
      - run: echo "${{ github.event.pull_request.title }}"
""",
    )

    assert _codes(tmp_path) == {"WF009"}


def test_function_wrapped_event_expression_interpolated_into_run_fails(tmp_path: Path) -> None:
    """Expression helpers cannot disguise direct event-to-shell interpolation."""
    _workflow(
        tmp_path,
        """name: CI
on: issue_comment
permissions: {}
jobs:
  test:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    steps:
      - run: echo '${{ toJSON(github.event) }}'
""",
    )

    assert _codes(tmp_path) == {"WF009"}


def test_pull_request_target_cannot_checkout_contributor_ref(tmp_path: Path) -> None:
    """A privileged pull-request-target job cannot execute the contributor ref."""
    _workflow(
        tmp_path,
        f"""name: Community mutation
on: pull_request_target
permissions:
  contents: read
jobs:
  mutate:
    permissions:
      pull-requests: write
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@{_PIN}  # v7.0.1
        with:
          ref: ${{{{ github.event.pull_request.head.sha }}}}
          persist-credentials: false
""",
    )

    assert _codes(tmp_path) == {"WF007"}


def test_community_mutation_cannot_request_unrelated_write_scope(tmp_path: Path) -> None:
    """Community-event mutation is limited to issue and pull-request effects."""
    _workflow(
        tmp_path,
        """name: Community mutation
on: pull_request_target
permissions: {}
jobs:
  mutate:
    permissions:
      packages: write
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    steps: []
""",
    )

    assert _codes(tmp_path) == {"WF005"}


@pytest.mark.parametrize(
    "job_fragment",
    [
        pytest.param(
            """    steps:
      - env:
          HEAD_REF: ${{ github.head_ref }}
        run: python3 trusted.py""",
            id="head-ref-environment",
        ),
        pytest.param(
            """    steps:
      - run: uv sync --all-extras""",
            id="dependency-install",
        ),
        pytest.param(
            """    container: example/tool:latest
    steps: []""",
            id="privileged-container",
        ),
    ],
)
def test_privileged_workflow_rejects_untrusted_execution_surfaces(tmp_path: Path, job_fragment: str) -> None:
    _workflow(
        tmp_path,
        f"""name: Community mutation
on: pull_request_target
permissions: {{}}
jobs:
  mutate:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
{job_fragment}
""",
    )

    assert _codes(tmp_path) == {"WF007"}


def test_privileged_trigger_requires_job_timeout(tmp_path: Path) -> None:
    _workflow(
        tmp_path,
        """name: Community mutation
on: issues
permissions:
  contents: read
jobs:
  mutate:
    permissions:
      issues: write
    runs-on: ubuntu-24.04
    steps: []
""",
    )

    assert _codes(tmp_path) == {"WF008"}


def test_trusted_default_branch_mutation_passes(tmp_path: Path) -> None:
    _workflow(
        tmp_path,
        f"""name: Community mutation
on:
  pull_request_target:
    types: [opened, edited]
  issues:
    types: [opened, edited]
permissions:
  contents: read
jobs:
  mutate:
    permissions:
      contents: read
      issues: write
      pull-requests: write
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@{_PIN}  # v7.0.1
        with:
          ref: ${{{{ github.sha }}}}
          persist-credentials: false
      - run: python3 scripts/check_pii_payload.py
""",
    )

    report = check_workflow_security.validate(tmp_path)

    assert report.ok is True
    assert report.findings == ()


@pytest.mark.parametrize("trigger", ["push", "schedule", "workflow_dispatch", "workflow_call"])
def test_unprivileged_trusted_trigger_and_local_action_pass(tmp_path: Path, trigger: str) -> None:
    _workflow(
        tmp_path,
        f"""name: Trusted automation
on: {trigger}
permissions: {{}}
jobs:
  test:
    runs-on: ubuntu-24.04
    steps:
      - uses: ./.github/actions/local
""",
    )

    assert _codes(tmp_path) == set()


def test_pull_request_local_reusable_workflow_without_secrets_passes(tmp_path: Path) -> None:
    _workflow(
        tmp_path,
        """name: CI
on: pull_request
permissions: {}
jobs:
  test:
    uses: ./.github/workflows/reusable.yml
""",
    )

    assert _codes(tmp_path) == set()


def test_hosted_matrix_runners_pass(tmp_path: Path) -> None:
    _workflow(
        tmp_path,
        """name: CI
on: pull_request
permissions: {}
jobs:
  test:
    strategy:
      matrix:
        os: [ubuntu-24.04, macos-15]
    runs-on: ${{ matrix.os }}
    steps: []
""",
    )

    assert _codes(tmp_path) == set()


def test_workflow_run_artifact_consumption_fails(tmp_path: Path) -> None:
    _workflow(
        tmp_path,
        f"""name: Privileged follow-up
on: workflow_run
permissions: {{}}
jobs:
  consume:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    steps:
      - uses: actions/download-artifact@{_PIN}  # v6.0.0
""",
    )

    assert _codes(tmp_path) == {"WF007"}


def test_invalid_yaml_fails_closed(tmp_path: Path) -> None:
    _workflow(tmp_path, "name: [unterminated")

    assert _codes(tmp_path) == {"WF000"}


def test_repository_workflows_satisfy_public_policy() -> None:
    report = check_workflow_security.validate(check_workflow_security.REPO_ROOT)

    assert report.ok is True
    assert report.scanned_workflows >= 5


def test_repository_pii_workflow_executes_only_trusted_standard_library_handler() -> None:
    workflow = (check_workflow_security.REPO_ROOT / ".github" / "workflows" / "pii-guard.yml").read_text(
        encoding="utf-8"
    )

    assert "\n  pull_request_target:" in workflow
    assert "\n  pull_request:" not in workflow
    assert "timeout-minutes: 5" in workflow
    assert "ref: ${{ github.sha }}" in workflow
    assert "persist-credentials: false" in workflow
    assert "run: python3 scripts/check_pii_payload.py" in workflow
    assert "setup-uv" not in workflow
    assert "uv sync" not in workflow
    assert "secrets." not in workflow
