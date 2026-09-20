"""Contract tests for bounded PR validation and complete post-merge enforcement."""

import json
import re
import signal
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from fieldkit.config import TIMEOUT_HEALTH_GATE
from scripts import quality_stage

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent


def _recipe(name: str, next_heading: str) -> str:
    makefile = (_ROOT / "Makefile").read_text(encoding="utf-8")
    return makefile.split(f"{name}:\n", maxsplit=1)[1].split(next_heading, maxsplit=1)[0]


def test_quality_uses_impact_selected_serial_tests() -> None:
    """Bounded validation must select impacted tests without a partial coverage claim."""
    recipe = _recipe("quality", "# quality-full")

    assert '--tach --tach-base "$(QUALITY_BASE)" -q -n 0' in recipe
    assert "--skip-when-docs-only" in recipe
    assert "--cov" not in recipe
    assert "tests/test_quality_contract.py" in recipe


def test_quality_full_retains_all_current_enforcement_commands() -> None:
    """The deferred full gate retains every former quality command and threshold."""
    recipe = _recipe("quality-full", "# coverage.json")

    assert "uv run ruff check ." in recipe
    assert "uv run ruff format --check ." in recipe
    assert "uv run mypy src/fieldkit/ hooks/*.py --no-error-summary" in recipe
    assert "uvx tach check" in recipe
    assert "scripts/check_dependency_profiles.py" in recipe
    assert "scripts/check_public_identity.py" in recipe
    assert "scripts/check_workflow_security.py" in recipe
    assert "scripts/release_workflow_policy.py" in recipe
    assert "scripts/check_supply_chain_policy.py policy" in recipe
    assert "scripts/check_release.py policy" in recipe
    assert "--cov-report=json:coverage.json" in recipe
    assert "--baseline .gaze/baseline.json" in recipe
    assert "--max-crapload 67" in recipe
    assert "--min-contract-coverage 50" in recipe
    assert "scripts/check_flag_contract.py" in recipe
    assert "scripts/agentready_assess.py agentready==2.49.0" in recipe


def test_broad_pytest_targets_use_bounded_overridable_worker_count() -> None:
    """Full-suite targets must not let xdist size itself to every CPU."""
    makefile = (_ROOT / "Makefile").read_text(encoding="utf-8")

    assert "PYTEST_XDIST_WORKERS ?= 4" in makefile
    assert "-n $(PYTEST_XDIST_WORKERS)" in _recipe("verify", "# test")
    assert "-n $(PYTEST_XDIST_WORKERS)" in _recipe("test", "# lint")
    assert "-n $(PYTEST_XDIST_WORKERS)" in _recipe("quality-full", "# coverage.json")
    assert "-n auto" not in makefile


def test_quality_recipes_run_each_stage_through_timing_runner() -> None:
    """Both recipes must publish timing evidence without changing their stage argv."""
    makefile = (_ROOT / "Makefile").read_text(encoding="utf-8")
    quality_recipe = _recipe("quality", "# quality-full")
    full_recipe = _recipe("quality-full", "# coverage.json")

    assert "scripts/quality_stage.py" in makefile
    assert " --full-enforcement $(2) -- " in makefile
    assert quality_recipe.count("$(call RUN_QUALITY_STAGE") == 17
    assert full_recipe.count("$(call RUN_FULL_QUALITY_STAGE") == 31
    assert '--quality-base "$(QUALITY_BASE)"' in quality_recipe
    assert "scripts/sync_claude_dir.py --check" in quality_recipe
    assert "scripts/check_dependency_profiles.py" in quality_recipe
    assert "scripts/check_compatibility_policy.py" in quality_recipe
    assert "scripts/check_public_identity.py" in quality_recipe
    assert "make public-tree-safety" in quality_recipe
    assert "scripts/check_workflow_security.py" in quality_recipe
    assert "scripts/release_workflow_policy.py" in quality_recipe
    assert "scripts/check_supply_chain_policy.py policy" in quality_recipe
    assert "make docs-site" in quality_recipe
    assert "scripts/check_release.py policy" in quality_recipe
    assert "--env NO_LLM=1" in full_recipe


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
    assert "docs-examples:\n\tuv run python scripts/check_documentation_examples.py" in makefile
    assert "\t$(MAKE) docs-examples" in makefile
    assert "\tuv run mkdocs build --strict --site-dir build/site" in makefile
    assert "scripts/check_public_docs.py build/site" in makefile


def test_hosted_public_tree_scans_install_the_pinned_scanner_on_path() -> None:
    """The scanner's installed filename must match the command used by the gate."""
    ci = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    release = (_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert ci.count('binary="$RUNNER_TEMP/gitleaks"') == 2
    assert 'scanner_binary="$RUNNER_TEMP/gitleaks"' in release
    assert 'echo "$RUNNER_TEMP" >> "$GITHUB_PATH"' in ci
    assert 'echo "$RUNNER_TEMP" >> "$GITHUB_PATH"' in release


@pytest.mark.parametrize(
    ("arguments", "expected_timeout"),
    [
        (["--label", "fast", "--", "true"], quality_stage.QUALITY_STAGE_TIMEOUT),
        (["--label", "full", "--full-enforcement", "--", "true"], TIMEOUT_HEALTH_GATE),
    ],
)
def test_quality_stage_uses_the_selected_stage_timeout(
    arguments: list[str], expected_timeout: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fast validation cannot inherit the long full-enforcement timeout."""
    captured: dict[str, object] = {}

    class _Process:
        pid = 123
        returncode = 0

        def communicate(self, *, timeout: int | None = None) -> tuple[str, str]:
            captured["timeout"] = timeout
            return "", ""

    def _popen(*args: object, **kwargs: object) -> _Process:
        captured.update(kwargs)
        return _Process()

    monkeypatch.setattr(quality_stage.subprocess, "Popen", _popen)
    monkeypatch.setattr(quality_stage, "_revision", lambda: "test-revision")

    result = quality_stage.main(arguments)

    assert result == 0
    assert captured["timeout"] == expected_timeout


def test_quality_stage_allows_impact_tests_to_finish_within_two_minutes() -> None:
    """The bounded developer gate retains the operator-approved 120-second ceiling."""
    assert quality_stage.QUALITY_STAGE_TIMEOUT == 120


def test_quality_stage_records_docs_only_impact_skip(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A docs-only Python change skips only the impact-test subprocess."""
    monkeypatch.setattr(quality_stage, "_revision", lambda: "candidate")
    monkeypatch.setattr(quality_stage, "has_semantic_python_changes", lambda *_: False)

    def _popen(*args: object, **kwargs: object) -> None:
        raise AssertionError("docs-only skip must not start the impact-test subprocess")

    monkeypatch.setattr(quality_stage.subprocess, "Popen", _popen)

    result = quality_stage.main(
        ["--label", "impact-pytest", "--quality-base", "base", "--skip-when-docs-only", "--", "pytest"]
    )

    record = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result == 0
    assert record["status"] == "skipped-docs-only"
    assert record["revision"] == {"head": "candidate", "quality_base": "base"}


def test_quality_stage_timeout_retains_byte_output_and_kills_process_group(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Timeouts kill the new process group and preserve partial byte diagnostics."""
    kill_calls: list[tuple[int, signal.Signals]] = []

    class _Process:
        pid = 456

        def __init__(self) -> None:
            self._timed_out = False

        def communicate(self, *, timeout: int | None = None) -> tuple[str, str]:
            if not self._timed_out:
                self._timed_out = True
                raise subprocess.TimeoutExpired(
                    ["stage"], timeout or 0, output=b"partial stdout\n", stderr=b"partial stderr\n"
                )
            return "", ""

    captured: dict[str, object] = {}

    def _popen(*args: object, **kwargs: object) -> _Process:
        captured.update(kwargs)
        return _Process()

    def _killpg(process_group: int, sig: signal.Signals) -> None:
        kill_calls.append((process_group, sig))

    monkeypatch.setattr(quality_stage.subprocess, "Popen", _popen)
    monkeypatch.setattr(quality_stage.os, "killpg", _killpg)
    monkeypatch.setattr(quality_stage, "_revision", lambda: "test-revision")

    result = quality_stage.main(["--label", "timeout", "--", "stage"])

    captured_output = capsys.readouterr()
    record = json.loads(captured_output.out.splitlines()[-1])
    assert result == 124
    assert captured["start_new_session"] is True
    assert kill_calls == [(456, signal.SIGKILL)]
    assert "[quality:timeout:stdout] partial stdout" in captured_output.out
    assert "[quality:timeout:stderr] partial stderr" in captured_output.err
    assert record["exit_status"] == 124


def test_quality_stage_records_timing_provenance_and_labelled_diagnostics() -> None:
    """A successful stage exposes replayed output and its machine-readable record."""
    script = _ROOT / "scripts" / "quality_stage.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--label",
            "demo",
            "--",
            sys.executable,
            "-c",
            "import sys; print('standard output'); print('standard error', file=sys.stderr)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    record = json.loads(result.stdout.splitlines()[-1])
    assert result.returncode == 0
    assert "[quality:demo:stdout] standard output" in result.stdout
    assert "[quality:demo:stderr] standard error" in result.stderr
    assert record["stage"] == "demo"
    assert record["argv"] == [
        sys.executable,
        "-c",
        "import sys; print('standard output'); print('standard error', file=sys.stderr)",
    ]
    assert record["elapsed_seconds"] >= 0
    assert record["exit_status"] == 0
    assert record["revision"]["head"] is not None


def test_quality_stage_reports_failing_command_output_and_status() -> None:
    """A failure still publishes the complete labelled diagnostic evidence."""
    script = _ROOT / "scripts" / "quality_stage.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--label",
            "failure",
            "--",
            sys.executable,
            "-c",
            "import sys; print('failed', file=sys.stderr); sys.exit(3)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    record = json.loads(result.stdout)
    assert result.returncode == 3
    assert "[quality:failure:stderr] failed" in result.stderr
    assert record["exit_status"] == 3


def test_pr_ci_uses_impact_tests_and_preserves_required_contexts() -> None:
    """The required PR contexts stay stable while test selection becomes bounded."""
    workflow = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    for context in ("Lint (ruff)", "Test (pytest)", "Skillsaw (skill lint)", "AgentReady score gate"):
        assert f"name: {context}" in workflow
    assert '--tach --tach-base "$BASE_SHA" --tach-head "$HEAD_SHA" -q -n 0' in workflow
    assert "scripts/check_dependency_profiles.py" in workflow
    assert "scripts/check_public_identity.py" in workflow
    assert "scripts/check_workflow_security.py" in workflow
    assert "scripts/check_supply_chain_policy.py policy" in workflow
    assert "make docs-site" in workflow
    docs_step = workflow.split("- name: Validate public documentation", maxsplit=1)[1].split("\n      - ", maxsplit=1)[
        0
    ]
    assert "if:" not in docs_step
    policy_arm = "docs/documentation-contract.json|docs/release-readiness/*.json) code=true ;;"
    docs_arm = "docs/*|openspec/*|.opencode/*|.specify/*|*.md) ;;"
    assert workflow.index(policy_arm) < workflow.index(docs_arm)
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
    assert "coverage-full-${{ github.run_id }}-${{ github.run_attempt }}-${{ github.sha }}" in full
    assert full.count("if-no-files-found: error") == full.count("actions/upload-artifact@")
    assert full.count("retention-days:") == full.count("actions/upload-artifact@")


def test_required_check_migration_order_is_documented_and_fail_closed() -> None:
    """Maintainers cannot require a context before a real public run has emitted it."""
    path = _ROOT / "docs" / "release-readiness" / "required-check-migration.md"
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


def test_markdown_link_check_covers_agent_content_and_required_pr_context() -> None:
    """Relative Markdown links are checked locally and on every pull request."""
    pre_commit = (_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    makefile = (_ROOT / "Makefile").read_text(encoding="utf-8")
    workflow = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    scope = (
        r"files: ^((AGENTS|CHANGELOG|CONTRIBUTING|GOVERNANCE|README|SECURITY|SUPPORT)\.md|"
        r"docs/.*\.md|\.opencode/(agents|commands|skills)/.*\.md|\.claude/(agents|commands)/.*\.md|"
        r"src/fieldkit/skills/.*\.md)$"
    )
    command = "uvx pre-commit==4.6.1 run markdown-link-check --all-files"
    fast_checks = workflow.split("  fast-checks:\n", maxsplit=1)[1].split("\n  test:\n", maxsplit=1)[0]
    setup_step = fast_checks.split("- uses: astral-sh/setup-uv", maxsplit=1)[1].split("\n      - ", maxsplit=1)[0]
    link_step = fast_checks.split("- name: Verify relative Markdown links", maxsplit=1)[1].split(
        "\n      - ", maxsplit=1
    )[0]
    install_step = fast_checks.split("- name: Install dependencies", maxsplit=1)[1].split("\n      - ", maxsplit=1)[0]

    assert scope in pre_commit
    assert makefile.count(command) == 2
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
    assert "make quality-full" in workflow
    assert "name: coverage-full-${{ github.run_id }}-${{ github.run_attempt }}-${{ github.sha }}" in workflow
    assert "name: agentready-report-${{ github.run_id }}-${{ github.run_attempt }}-${{ github.sha }}" in workflow
