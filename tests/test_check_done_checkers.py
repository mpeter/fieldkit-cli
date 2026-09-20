"""Tests for standalone done-condition checker isolation and behavior."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent
_POLICY = _ROOT / "scripts" / "check_done_checkers.py"
_CHECKERS = _ROOT / "scripts" / "done_checks"


def _load_policy() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_done_checkers", _POLICY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


policy = _load_policy()


def _run(name: str, *args: Path | str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_CHECKERS / name), *(str(arg) for arg in args)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_committed_checkers_pass_isolation_policy() -> None:
    violations = policy.validate_all(_CHECKERS)
    assert violations == ()


@pytest.mark.parametrize(
    "source",
    [
        "from .shared import check\n",
        "import fieldkit\n",
        "import pytest\n",
        "import importlib\nimportlib.import_module('pathlib')\n",
        "__import__('pathlib')\n",
        "eval('1 + 1')\n",
        "exec('answer = 2')\n",
        "import subprocess\nsubprocess.run(['true'])\n",
        "import os\nos.system('true')\n",
        "import runpy\nrunpy.run_path('candidate.py')\n",
        "import socket\n",
        "from urllib.request import urlopen\n",
        "from pathlib import Path\nPath('/var/tmp/result')\n",
    ],
)
def test_policy_rejects_forbidden_checker_capabilities(tmp_path: Path, source: str) -> None:
    checker = tmp_path / "unsafe.py"
    checker.write_text(source, encoding="utf-8")
    violations = policy.validate_checker(checker)
    assert violations


def test_policy_accepts_a_standalone_stdlib_checker(tmp_path: Path) -> None:
    checker = tmp_path / "safe.py"
    checker.write_text(
        "from pathlib import Path\nimport sys\nprint(Path(sys.argv[1]).read_text(encoding='utf-8'))\n",
        encoding="utf-8",
    )
    assert policy.validate_checker(checker) == ()


def test_policy_parses_without_executing_checker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "executed"
    checker = tmp_path / "checker.py"
    checker.write_text("from pathlib import Path\nPath('executed').touch()\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    violations = policy.validate_checker(checker)
    assert violations == ()
    assert not marker.exists()


def test_future_annotations_checker_enforces_exact_count(tmp_path: Path) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("from __future__ import annotations\n", encoding="utf-8")
    second.write_text('TEXT = "from __future__ import annotations"\n', encoding="utf-8")

    passing = _run("check_future_annotations.py", "1", first, second)
    failing = _run("check_future_annotations.py", "0", first, second)

    assert passing.returncode == 0
    assert failing.returncode == 1
    assert "expected 0, found 1" in failing.stdout


def test_acv_checker_covers_presence_absence_and_fallback(tmp_path: Path) -> None:
    components = tmp_path / "components.py"
    account = tmp_path / "account.py"
    forecast = tmp_path / "forecast.py"
    quota = tmp_path / "quota.py"
    tests = tmp_path / "tests.py"
    components.write_text("def effective_net_consulting_acv():\n    pass\n", encoding="utf-8")
    account.write_text("effective_net_consulting_acv()\n", encoding="utf-8")
    forecast.write_text("effective_net_consulting_acv()\n", encoding="utf-8")
    quota.write_text("effective_net_consulting_acv()\n", encoding="utf-8")
    tests.write_text(
        "effective_net_consulting_acv()\n"
        "pytest.approx(_GROSS)\n"
        "def test_effective_net_consulting_acv_fixed_price_acv_none_falls_back_to_gross(): pass\n",
        encoding="utf-8",
    )

    passing = _run("check_acv_contract.py", components, account, forecast, quota, tests)
    quota.write_text('effective_net_consulting_acv()\nvalue = "fixed_price"\n', encoding="utf-8")
    failing = _run("check_acv_contract.py", components, account, forecast, quota, tests)

    assert passing.returncode == 0
    assert failing.returncode == 1
    assert "fixed_price literal remains" in failing.stdout


def test_quota_checker_preserves_exact_extraction_counts(tmp_path: Path) -> None:
    quota = tmp_path / "quota.py"
    render = tmp_path / "render.py"
    src = tmp_path / "src"
    tests = tmp_path / "tests"
    pipeline_test = tests / "test_pipeline.py"
    src.mkdir()
    tests.mkdir()
    quota.write_text("def _collect_pursuits_for_quota(): pass\n", encoding="utf-8")
    render.write_text(
        "QUOTA_STAGE_WEIGHTS = {}\n"
        "class QuotaGapResult: pass\n"
        "def _parse_sf_amount(): pass\n"
        "def calculate_quota_gap() -> QuotaGapResult:\n    return QuotaGapResult()\n",
        encoding="utf-8",
    )
    pipeline_test.write_text(
        '_CMD_QUOTA__QUOTA_MODULE = "fieldkit.commands.pipeline.quota"\n'
        '_CMD_QUOTA__CALC_MODULE = "fieldkit.watch.morning_brief_render"\n',
        encoding="utf-8",
    )

    passing = _run("check_quota_render_extraction.py", quota, render, src, tests, pipeline_test)
    quota.write_text(
        "def _collect_pursuits_for_quota(): pass\ncalculate_quota_gap = object()\n",
        encoding="utf-8",
    )
    failing = _run("check_quota_render_extraction.py", quota, render, src, tests, pipeline_test)
    quota.write_text("def _collect_pursuits_for_quota(): pass\n", encoding="utf-8")
    render.write_text(
        "from somewhere import QUOTA_STAGE_WEIGHTS, QuotaGapResult, _parse_sf_amount, calculate_quota_gap\n",
        encoding="utf-8",
    )
    reexport = _run("check_quota_render_extraction.py", quota, render, src, tests, pipeline_test)
    render.write_text(
        "QUOTA_STAGE_WEIGHTS = {}\n"
        "class QuotaGapResult: pass\n"
        "def _parse_sf_amount(): pass\n"
        "def calculate_quota_gap() -> QuotaGapResult:\n    return QuotaGapResult()\n",
        encoding="utf-8",
    )
    quota.write_text(
        "def _collect_pursuits_for_quota(): pass\n"
        "from fieldkit.watch.morning_brief_render import calculate_quota_gap as calculate_quota_gap\n",
        encoding="utf-8",
    )
    origin_reexport = _run("check_quota_render_extraction.py", quota, render, src, tests, pipeline_test)

    assert passing.returncode == 0
    assert failing.returncode == 1
    assert "calculate_quota_gap remains" in failing.stdout
    assert reexport.returncode == 1
    assert "must be bound exactly once" in reexport.stdout
    assert origin_reexport.returncode == 1
    assert "calculate_quota_gap is re-exported" in origin_reexport.stdout


def test_morning_brief_render_checker_enforces_deleted_origin(tmp_path: Path) -> None:
    src = tmp_path / "src"
    tests = tmp_path / "tests"
    files = {
        "fieldkit/pursuit/projects.py": (
            "from fieldkit.pursuit.io import parse_frontmatter_fallback\n"
            "COMPLETED_STAGES = set()\nclass ProjectRow: pass\ndef _parse_iso_date(): pass\n"
            "def _extract_project_name(): pass\ndef classify_project(): pass\ndef health_check(): pass\n"
        ),
        "fieldkit/commands/pursuit/projects_health.py": (
            "from fieldkit.pursuit.projects import ProjectRow, health_check\n"
        ),
        "fieldkit/pursuit/io.py": "def parse_frontmatter_fallback(): pass\n",
        "fieldkit/commands/pursuit/audit.py": "from fieldkit.pursuit.io import parse_frontmatter_fallback\n",
        "fieldkit/watch/morning_brief_render.py": (
            "def _render_project_health_section(): pass\ndef _render_quota_section(): pass\n"
            "def _render_companion_outbox_pointer(): pass\n"
            "def render_brief():\n    _render_quota_section()\n    _render_project_health_section()\n"
            "    _render_companion_outbox_pointer()\n"
        ),
        "fieldkit/commands/brief/generate.py": "quota_collector=_collect_pursuits_for_quota\n",
        "fieldkit/companion/outbox.py": (
            "POINTER = 'watch/morning_brief_render.py:_render_companion_outbox_pointer'\n"
        ),
        "fieldkit/pursuit/AGENTS.md": "Use fieldkit.pursuit.io.parse_frontmatter_fallback.\n",
    }
    for relative, content in files.items():
        path = src / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    tests.mkdir()
    required = (
        "test_classify_project_empty_frontmatter_is_unknown",
        "test_run_generate_inner_passes_quota_collector_identity_to_render_brief",
        "test_quota_section_passes_collector_result_identity_to_calculator",
        "test_quota_section_without_config_does_not_collect",
        "test_companion_pointer_missing_outbox_is_empty",
        "test_companion_pointer_empty_outbox_is_empty",
        "test_companion_pointer_unreadable_outbox_is_empty",
        "test_companion_pointer_populated_outbox_has_exact_output",
        "test_render_brief_places_companion_pointer_before_footer",
    )
    contract_tests = "\n".join(f"def {name}(): pass" for name in required)
    for name in (
        "test_morning_brief_companion_pointer.py",
        "test_projects_health_classify.py",
        "test_morning_brief.py",
        "test_morning_brief_inner.py",
    ):
        (tests / name).write_text(contract_tests, encoding="utf-8")

    passing = _run("check_morning_brief_render_domain.py", src, tests)
    pursuit_agents = src / "fieldkit/pursuit/AGENTS.md"
    pursuit_agents.write_text("Consumers import `_parse_frontmatter` from `audit.py`.\n", encoding="utf-8")
    stale_guidance = _run("check_morning_brief_render_domain.py", src, tests)
    pursuit_agents.write_text("Use fieldkit.pursuit.io.parse_frontmatter_fallback.\n", encoding="utf-8")
    old_render = src / "fieldkit/commands/watch/morning_brief_render.py"
    old_render.parent.mkdir(parents=True)
    old_render.write_text("def render_brief(): pass\n", encoding="utf-8")
    failing = _run("check_morning_brief_render_domain.py", src, tests)

    assert passing.returncode == 0
    assert stale_guidance.returncode == 1
    assert "stale pursuit parser guidance" in stale_guidance.stderr
    assert failing.returncode == 1
    assert "old command render module still exists" in failing.stderr


def test_companion_checker_reads_junit_and_structural_contract(tmp_path: Path) -> None:
    junit = tmp_path / "result.xml"
    decide = tmp_path / "decide.py"
    loop = tmp_path / "loop.py"
    cli = tmp_path / "cli.py"
    service = tmp_path / "service"
    tests = tmp_path / "tests.py"
    succession = tmp_path / "succession.md"
    changelog = tmp_path / "changelog.md"
    junit.write_text('<testsuite tests="2" failures="0" errors="0" skipped="0"/>', encoding="utf-8")
    decide.write_text("Commands run at every tier without consulting the act allowlist.\n", encoding="utf-8")
    for path in (loop, cli, service):
        path.write_text("historic regression structurally inert\n", encoding="utf-8")
    tests.write_text(
        "@pytest.mark.characterization\n"
        "def test_act_runs_allowlisted_mutation(): pass\n"
        "@pytest.mark.characterization\n"
        "def test_act_denies_non_allowlisted_mutation(): pass\n",
        encoding="utf-8",
    )
    succession.write_text("structurally\nhistoric regression\n", encoding="utf-8")
    changelog.write_text("historic regression\n", encoding="utf-8")

    passing = _run("check_companion_inert_docs.py", junit, decide, loop, cli, service, tests, succession, changelog)
    decide.write_text("The act allowlist controls every command.\n", encoding="utf-8")
    stale_decide = _run(
        "check_companion_inert_docs.py", junit, decide, loop, cli, service, tests, succession, changelog
    )
    decide.write_text("Commands run at every tier without consulting the act allowlist.\n", encoding="utf-8")
    junit.write_text('<testsuite tests="2" failures="1" errors="0" skipped="0"/>', encoding="utf-8")
    failing = _run("check_companion_inert_docs.py", junit, decide, loop, cli, service, tests, succession, changelog)

    assert passing.returncode == 0
    assert stale_decide.returncode == 1
    assert "decide command_argv contract" in stale_decide.stdout
    assert failing.returncode == 1
    assert "found (2, 1, 0, 0)" in failing.stdout
