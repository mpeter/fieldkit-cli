"""Contract tests for bounded PR validation and complete post-merge enforcement."""

import re
from pathlib import Path

import pytest
import yaml

from scripts import check_documentation_contract, quality_plan

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent


def test_ci_change_filter_uses_canonical_prose_classifier() -> None:
    """Shipped Markdown must not bypass CI through a blanket extension allowlist."""
    workflow = yaml.safe_load((_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    step = next(step for step in workflow["jobs"]["changes"]["steps"] if step.get("id") == "filter")
    assert step["run"].strip() == (
        'python3 scripts/semantic_python_changes.py --base "$BASE_SHA" --candidate "$HEAD_SHA" >> "$GITHUB_OUTPUT"'
    )
    assert "continue-on-error" not in step


def _recipe(name: str, next_heading: str) -> str:
    makefile = (_ROOT / "Makefile").read_text(encoding="utf-8")
    return makefile.split(f"{name}:\n", maxsplit=1)[1].split(next_heading, maxsplit=1)[0]


def test_quality_uses_impact_selected_serial_tests() -> None:
    """Bounded validation must select impacted tests without a partial coverage claim."""
    plan = quality_plan.build_plan("pr", repo=_ROOT, quality_base="a" * 40)
    impact = next(stage for stage in plan.stages if stage.label == "impact-pytest")

    assert impact.commands[0].argv == (
        "uv",
        "run",
        "python",
        "scripts/run_impact_tests.py",
        "--base",
        "a" * 40,
    )
    assert impact.skip_policy == "prose-only"
    assert all("--cov" not in command.argv for stage in plan.stages for command in stage.commands)
    quality_contract = next(stage for stage in plan.stages if stage.label == "quality-contract")
    assert quality_contract.commands[0].argv == (
        "uv",
        "run",
        "pytest",
        "tests/test_quality_contract.py",
        "-q",
        "-n",
        "0",
    )


def test_quality_full_retains_all_current_enforcement_commands() -> None:
    """The deferred full gate retains every former quality command and threshold."""
    plan = quality_plan.build_plan("full", repo=_ROOT)
    commands = {stage.label: stage.commands for stage in plan.stages}

    assert commands["ruff-check"][0].argv == ("uv", "run", "ruff", "check", ".")
    assert commands["ruff-format"][0].argv == ("uv", "run", "ruff", "format", "--check", ".")
    assert commands["tach"][0].argv == ("uv", "run", "--locked", "tach", "check")
    assert commands["dependency-profiles"][0].argv[-1] == "scripts/check_dependency_profiles.py"
    assert commands["public-identity"][0].argv[-1] == "scripts/check_public_identity.py"
    assert commands["workflow-security"][0].argv[-1] == "scripts/check_workflow_security.py"
    assert commands["release-workflow-policy"][0].argv[-1] == "scripts/release_workflow_policy.py"
    assert commands["supply-chain-policy"][0].argv[-2:] == ("scripts/check_supply_chain_policy.py", "policy")
    assert commands["release-policy"][0].argv[-2:] == ("scripts/check_release.py", "policy")
    assert "--cov-report=json:coverage.json" in commands["pytest-coverage"][0].argv
    assert "--baseline" in commands["gazepy-baseline"][0].argv
    assert commands["gazepy-ceiling"][0].argv[-2:] == ("--max-crapload", "67")
    assert commands["gazepy-contract-coverage"][0].argv[-2:] == ("--min-contract-coverage", "50")
    assert commands["flag-contract"][0].argv[-1] == "scripts/check_flag_contract.py"
    assert commands["agentready-assess"][0].argv[-1] == "agentready==2.49.0"


def test_broad_pytest_targets_use_bounded_overridable_worker_count() -> None:
    """Full-suite targets must not let xdist size itself to every CPU."""
    makefile = (_ROOT / "Makefile").read_text(encoding="utf-8")

    assert "PYTEST_XDIST_WORKERS ?= 4" in makefile
    assert "-n $(PYTEST_XDIST_WORKERS)" in _recipe("verify", "# test")
    assert "-n $(PYTEST_XDIST_WORKERS)" in _recipe("test", "# lint")
    assert '--workers "$(PYTEST_XDIST_WORKERS)"' in _recipe("quality-full", "# coverage.json")
    plan = quality_plan.build_plan("full", repo=_ROOT, workers=4)
    assert next(stage for stage in plan.stages if stage.label == "pytest-coverage").commands[0].argv[-1] == "4"
    assert "-n auto" not in makefile


def test_quality_recipes_are_thin_wrappers_around_the_fixed_plan_owner() -> None:
    """Make remains contributor UI and does not implement either quality tier."""
    makefile = (_ROOT / "Makefile").read_text(encoding="utf-8")
    quality_recipe = _recipe("quality", "# quality-full")
    full_recipe = _recipe("quality-full", "# coverage.json")

    assert "RUN_QUALITY_STAGE" not in makefile
    assert "RUN_FULL_QUALITY_STAGE" not in makefile
    assert "scripts/quality_gate.py run --tier pr" in quality_recipe
    assert '--base "$(QUALITY_BASE)"' in quality_recipe
    assert "--receipt reports/quality-pr.json" in quality_recipe
    assert "scripts/quality_gate.py run --tier full" in full_recipe
    assert "--receipt reports/quality-full.json" in full_recipe
    assert "scripts/check_agent_instruction_surface.py" not in makefile


@pytest.mark.parametrize("tier", ["pr", "full"])
def test_architecture_gate_uses_locked_project_tool(tier: quality_plan.Tier) -> None:
    plan = quality_plan.build_plan(tier, repo=_ROOT, quality_base="a" * 40 if tier == "pr" else None)
    command = next(stage for stage in plan.stages if stage.label == "tach").commands[0].argv

    assert command == ("uv", "run", "--locked", "tach", "check")


def test_public_contributor_targets_use_locked_environment_and_canonical_gate() -> None:
    """README contributor commands must map to deterministic repository targets."""
    makefile = (_ROOT / "Makefile").read_text(encoding="utf-8")

    assert "bootstrap:\n\tuv sync --frozen --all-extras --dev\n\t$(MAKE) contributor-hooks" in makefile
    contributor_recipe = _recipe("contributor-hooks", "# pr-check")
    assert contributor_recipe.count("uvx pre-commit==4.6.1 install -f") == 3
    assert "scripts/check_precommit_version.py" not in contributor_recipe
    assert "post-commit" not in contributor_recipe
    assert "post_commit" not in contributor_recipe
    assert "pr-check: quality" in makefile
    assert "docs-site:\n\tuv run python scripts/check_documentation_contract.py" in makefile
    assert "docs-examples:\n\tuv run python -m scripts.check_documentation_examples" in makefile
    assert "\t$(MAKE) docs-examples" in makefile
    assert "\tuv run mkdocs build --strict --site-dir build/site" in makefile
    assert "scripts/check_public_docs.py build/site" in makefile


def test_contributor_gate_resolves_fetched_base_to_required_immutable_revision() -> None:
    """The documented local gate supplies a SHA, not an unsupported ref name."""
    blocks = check_documentation_contract.fenced_blocks(_ROOT / "CONTRIBUTING.md")
    gate = next(block for block in blocks if "make pr-check" in block.body)

    assert gate.language == "console"
    assert gate.body == 'git fetch upstream main\nQUALITY_BASE="$(git rev-parse upstream/main)" make pr-check\n'
    with pytest.raises(ValueError, match="full 40-character hexadecimal revision"):
        quality_plan.build_plan("pr", repo=_ROOT, quality_base="upstream/main")
    resolved_revision = "a" * 40
    plan = quality_plan.build_plan("pr", repo=_ROOT, quality_base=resolved_revision)
    assert plan.quality_base == resolved_revision
    impact = next(stage for stage in plan.stages if stage.label == "impact-pytest")
    assert impact.commands[0].argv[-1] == resolved_revision


def test_hosted_public_tree_scans_install_the_pinned_scanner_on_path() -> None:
    """The scanner's installed filename must match the command used by the gate."""
    ci = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    release = (_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert ci.count('binary="$RUNNER_TEMP/gitleaks"') == 2
    assert 'scanner_binary="$RUNNER_TEMP/gitleaks"' in release
    assert 'echo "$RUNNER_TEMP" >> "$GITHUB_PATH"' in ci
    assert 'echo "$RUNNER_TEMP" >> "$GITHUB_PATH"' in release


def test_pr_ci_uses_impact_tests_and_preserves_required_contexts() -> None:
    """The required PR contexts stay stable while test selection becomes bounded."""
    workflow = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    for context in ("Lint (ruff)", "Test (pytest)", "Skillsaw (skill lint)", "AgentReady score gate"):
        assert f"name: {context}" in workflow
    assert "scripts/quality_gate.py run --tier impact" in workflow
    assert '--base "$BASE_SHA" --head "$HEAD_SHA" --workers 4' in workflow
    assert "--github-output reports/pytest-github-output" in workflow
    assert "--receipt reports/quality-impact.json" in workflow
    assert 'cat reports/pytest-github-output >> "$GITHUB_OUTPUT"' in workflow
    assert '--scope "$TEST_SCOPE"' in workflow
    assert "TEST_SCOPE: ${{ steps.pytest.outputs.test_scope }}" in workflow
    assert "scripts/check_dependency_profiles.py" in workflow
    assert "scripts/check_public_identity.py" in workflow
    assert "scripts/check_workflow_security.py" in workflow
    assert "scripts/check_supply_chain_policy.py policy" in workflow
    for command in (
        "uv run python scripts/check_documentation_contract.py",
        "uv run python -m scripts.check_documentation_examples",
        "uv run mkdocs build --strict --site-dir build/site",
        "uv run python scripts/check_public_docs.py build/site",
    ):
        assert command in workflow
    docs_step = workflow.split("- name: Validate public documentation", maxsplit=1)[1].split("\n      - ", maxsplit=1)[
        0
    ]
    assert "if:" not in docs_step
    public_identity_step = workflow.split("- name: Enforce public identity policy", maxsplit=1)[1].split(
        "\n      - ", maxsplit=1
    )[0]
    assert "if:" not in public_identity_step
    assert "--cov-report=json:coverage.json" not in workflow
    assert re.search(r"test:.*?fetch-depth: 0", workflow, flags=re.DOTALL) is not None
    changelog = (_ROOT / ".github" / "workflows" / "changelog.yml").read_text(encoding="utf-8")
    assert "name: Changelog fragment" in changelog


def test_pr_ci_exposes_stable_bounded_aggregate_and_keeps_compatibility_dispatchable() -> None:
    """Ordinary PRs report bounded checks while release compatibility stays explicit."""
    workflow = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    compatibility = (_ROOT / ".github" / "workflows" / "compatibility.yml").read_text(encoding="utf-8")
    workflow_document = yaml.load(workflow, Loader=yaml.BaseLoader)
    compatibility_document = yaml.load(compatibility, Loader=yaml.BaseLoader)
    jobs = workflow_document["jobs"]
    required = jobs["required-checks"]
    required_text = workflow.split("  required-checks:\n", maxsplit=1)[1]

    assert required["name"] == "Required checks"
    assert required["if"] == "always()"
    assert required["needs"] == [
        "changes",
        "commit-msg-pii",
        "fast-checks",
        "test",
        "skillsaw",
        "agentready",
    ]
    assert "uses: actions/checkout@" in required_text
    assert "if: needs.changes.outputs.code != 'true'" not in required_text
    assert "actions/download-artifact@" not in required_text
    assert (
        "compatibility-candidate-${{ github.run_id }}-${{ github.run_attempt }}-${{ github.sha }}" not in required_text
    )
    assert "ci-evaluator-" not in workflow
    assert "compatibility" not in jobs
    assert set(compatibility_document["on"]) == {"workflow_call", "workflow_dispatch"}
    assert compatibility_document["jobs"]["core"]["strategy"]["max-parallel"] == "4"
    for child_name in (
        "Build release candidate once",
        "Core ${{ matrix.python-pair.first }} + ${{ matrix.python-pair.second }}",
        "Optional profiles on Ubuntu",
        "Optional profiles on macOS",
        "Core in fedora:43",
    ):
        assert child_name in compatibility
    build = compatibility.split("  build:\n", maxsplit=1)[1].split("\n  core:\n", maxsplit=1)[0]
    core = compatibility.split("  core:\n", maxsplit=1)[1].split("\n  optional-ubuntu:\n", maxsplit=1)[0]
    optional_ubuntu = compatibility.split("  optional-ubuntu:\n", maxsplit=1)[1].split(
        "\n  optional-macos:\n", maxsplit=1
    )[0]
    optional_macos = compatibility.split("  optional-macos:\n", maxsplit=1)[1].split(
        "\n  fedora-family:\n", maxsplit=1
    )[0]
    fedora = compatibility.split("  fedora-family:\n", maxsplit=1)[1]
    assert "uv build --out-dir dist" in build
    assert "pyproject.toml" in build
    assert "scripts/check_artifacts.py" not in build
    assert "actions/checkout@" not in core
    assert "--profile base" in core
    assert "--profile all" not in core
    assert "--profile all" in optional_ubuntu
    assert "--profile all" in optional_macos
    assert "scripts/check_artifacts.py" in optional_ubuntu
    assert "scripts/smoke_artifact.py --json --installer uv dist/*.tar.gz" in optional_ubuntu
    assert "compatibility-evidence-${{ github.run_id }}" in optional_ubuntu
    assert "profile_pid=$!" in optional_ubuntu
    assert 'wait "$profile_pid"' in optional_ubuntu
    for status in ("profile_status", "artifact_status", "identity_status", "sdist_status"):
        assert f'test "${status}" -eq 0' in optional_ubuntu
    for optional_job in (optional_ubuntu, optional_macos):
        assert "astral-sh/setup-uv@" in optional_job
        assert "--installer uv" in optional_job
        assert "cache: pip" not in optional_job
    assert "actions/checkout@" not in fedora
    assert "dnf install -y python3" in fedora
    assert "python3-pip" not in fedora
    assert "astral-sh/setup-uv@" in fedora
    assert "--profile base" in fedora
    assert "--installer uv" in fedora
    for member in ("first", "second"):
        assert f"python${{{{ matrix.python-pair.{member} }}}} scripts/smoke_artifact.py" in core
        assert f"reports/smoke-${{{{ matrix.os }}}}-py${{{{ matrix.python-pair.{member} }}}}.json" in core
    assert (
        "compatibility-smoke-${{ matrix.os }}-py${{ matrix.python-pair.first }}-py${{ matrix.python-pair.second }}"
        in core
    )
    assert "reports/smoke-fedora-43.json" in fedora
    assert "compatibility-smoke-fedora-43-${{ github.run_id }}" in fedora


def test_ci_artifacts_are_revision_named_bounded_and_fail_closed() -> None:
    """Every CI artifact has an attributable name, retention, and explicit missing-file policy."""
    ci = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    compatibility = (_ROOT / ".github" / "workflows" / "compatibility.yml").read_text(encoding="utf-8")
    full = (_ROOT / ".github" / "workflows" / "full-enforcement.yml").read_text(encoding="utf-8")

    assert "--junitxml=reports/pytest.xml" in ci
    assert "scripts/ci_evidence.py junit" in ci
    assert "pytest-tach-${{ github.event.pull_request.number }}-${{ github.event.pull_request.head.sha }}" in ci
    assert "required-checks-${{ github.event.pull_request.number }}-${{ github.event.pull_request.head.sha }}" in ci
    assert ci.count("if-no-files-found: error") == ci.count("actions/upload-artifact@")
    assert ci.count("retention-days:") == ci.count("actions/upload-artifact@")

    assert "compatibility-candidate-${{ github.run_id }}-${{ github.run_attempt }}-${{ github.sha }}" in compatibility
    assert compatibility.count("if-no-files-found: error") == compatibility.count("actions/upload-artifact@")
    assert compatibility.count("retention-days:") == compatibility.count("actions/upload-artifact@")

    assert "scripts/ci_evidence.py coverage" in full
    assert "reports/coverage-summary.json" in full
    assert "reports/quality-full.json" in full
    assert "coverage-full-${{ github.run_id }}-${{ github.run_attempt }}-${{ github.sha }}" in full
    assert "quality-full-receipt-${{ github.run_id }}-${{ github.run_attempt }}-${{ github.sha }}" in full
    assert full.count("if-no-files-found: error") == full.count("actions/upload-artifact@")
    assert full.count("retention-days:") == full.count("actions/upload-artifact@")


def test_required_check_migration_order_is_documented_and_fail_closed() -> None:
    """Maintainers cannot require a context before a real public run has emitted it."""
    path = _ROOT / "docs" / "release-readiness" / "required-check-migration.md"
    if not path.is_file():
        pytest.skip("private release-operations runbook is excluded from the public tree")
    document = path.read_text(encoding="utf-8")
    ordered_steps = [
        "## 1. Land without changing repository rules",
        "## 2. Prove both pull-request classes",
        "## 3. Prove a real fork",
        "## 4. Switch required contexts atomically",
        "## 5. Refetch and challenge the rule",
        "## 6. Retire legacy contexts",
    ]

    assert [document.index(step) for step in ordered_steps] == sorted(document.index(step) for step in ordered_steps)
    assert "`Required checks` and `Changelog fragment`" in document
    assert "must not be required" in document
    assert "failure, cancellation, and unexpected skip" in document
    assert "Rollback" in document


def test_workflow_cancellation_groups_are_ref_or_pull_request_specific() -> None:
    """A new branch or pull request cannot cancel evidence for an unrelated revision."""
    ci = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    changelog = (_ROOT / ".github" / "workflows" / "changelog.yml").read_text(encoding="utf-8")
    compatibility = (_ROOT / ".github" / "workflows" / "compatibility.yml").read_text(encoding="utf-8")
    full = (_ROOT / ".github" / "workflows" / "full-enforcement.yml").read_text(encoding="utf-8")

    assert "group: ${{ github.workflow }}-${{ github.event.pull_request.number }}" in ci
    assert "group: ${{ github.workflow }}-${{ github.ref }}" in changelog
    assert "group: compatibility-${{ github.ref }}" in compatibility
    assert "group: ${{ github.workflow }}-${{ github.ref }}" in full


@pytest.mark.parametrize(
    "path",
    [
        "RELEASING.md",
        "ROADMAP.md",
        "THREAT_MODEL.md",
        ".github/PULL_REQUEST_TEMPLATE.md",
        "src/README.md",
        "src/fieldkit/ingest/AGENTS.md",
        "src/fieldkit/commands/gmail/README.md",
        "src/fieldkit/skills/start/SKILL.md",
        "docs/concepts.md",
    ],
)
def test_markdown_link_check_selects_public_prose(path: str) -> None:
    configuration = yaml.safe_load((_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hook = next(
        hook for repo in configuration["repos"] for hook in repo["hooks"] if hook["id"] == "markdown-link-check"
    )
    result = re.search(hook["files"], path)
    assert result is not None


def test_markdown_link_check_covers_agent_content_and_required_pr_context() -> None:
    """Relative Markdown links are checked locally and on every pull request."""
    pre_commit = (_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    workflow = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    scope = (
        r"files: ^([^/]+\.md|\.github/.*\.md|"
        r"docs/.*\.md|src/.*\.md)$"
    )
    command = "uvx pre-commit==4.6.1 run markdown-link-check --all-files"
    fast_checks = workflow.split("  fast-checks:\n", maxsplit=1)[1].split("\n  test:\n", maxsplit=1)[0]
    setup_step = fast_checks.split("- uses: astral-sh/setup-uv", maxsplit=1)[1].split("\n      - ", maxsplit=1)[0]
    link_step = fast_checks.split("- name: Verify relative Markdown links", maxsplit=1)[1].split(
        "\n      - ", maxsplit=1
    )[0]
    install_step = fast_checks.split("- name: Install dependencies", maxsplit=1)[1].split("\n      - ", maxsplit=1)[0]

    assert scope in pre_commit
    plan = quality_plan.build_plan("pr", repo=_ROOT, quality_base="a" * 40)
    assert next(stage for stage in plan.stages if stage.label == "markdown-links").commands[0].argv == (
        "uvx",
        "pre-commit==4.6.1",
        "run",
        "markdown-link-check",
        "--all-files",
    )
    assert command in link_step
    assert "if:" not in setup_step
    assert "if:" not in link_step
    assert "if:" not in install_step
    assert "uv sync --locked --all-extras --dev" in install_step


def test_full_enforcement_runs_on_schedule_and_dispatch() -> None:
    """Full validation remains enforceable outside the bounded pull-request path."""
    workflow = (_ROOT / ".github" / "workflows" / "full-enforcement.yml").read_text(encoding="utf-8")

    assert "push:" not in workflow
    assert "schedule:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "scripts/quality_gate.py run --tier full --workers 4" in workflow
    assert "--receipt reports/quality-full.json" in workflow
    assert "make quality-full" not in workflow
    assert "name: coverage-full-${{ github.run_id }}-${{ github.run_attempt }}-${{ github.sha }}" in workflow
    assert "name: agentready-report-${{ github.run_id }}-${{ github.run_attempt }}-${{ github.sha }}" in workflow
