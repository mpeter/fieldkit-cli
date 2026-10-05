"""Checkout-only tests must run regardless of the production-code diff."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.repo_tool_selection import repo_tool_test_paths

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
_SELECTION_TIMEOUT_SECONDS = 60


@pytest.mark.parametrize(
    "source",
    [
        "import hooks.fresh_hook",
        "from hooks import fresh_hook",
        "from scripts import fresh_script",
        "import fresh_script",
        'importlib.import_module("fresh_script")',
        'Path("scripts/fresh_script.py")',
        'ROOT / "hooks" / "fresh_hook.py"',
        'spec_from_file_location("tool", SCRIPT_DIR / "fresh_script.py")',
    ],
)
def test_new_tool_tests_are_discovered(tmp_path: Path, source: str) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/fresh_script.py").write_text("", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    target = tests / "test_new_feature.py"
    target.write_text(source, encoding="utf-8")
    unrelated = tests / "test_domain.py"
    unrelated.write_text("from fieldkit import errors", encoding="utf-8")

    result = repo_tool_test_paths(tmp_path)

    assert result == {target}
    assert unrelated not in result


def test_tool_references_through_test_helpers_are_selected(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    helper = tests / "new_helper.py"
    helper.write_text("import scripts.new_tool", encoding="utf-8")
    intermediary = tests / "another_helper.py"
    intermediary.write_text("from new_helper import run", encoding="utf-8")
    target = tests / "test_indirect.py"
    target.write_text("from another_helper import run", encoding="utf-8")

    result = repo_tool_test_paths(tmp_path)

    assert result == {helper, intermediary, target}


def test_inventory_includes_tools_added_after_previous_collection(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    tests = tmp_path / "tests"
    tests.mkdir()
    target = tests / "test_future.py"
    target.write_text("import future_tool", encoding="utf-8")

    assert repo_tool_test_paths(tmp_path) == set()
    (tmp_path / "scripts/future_tool.py").write_text("", encoding="utf-8")
    assert repo_tool_test_paths(tmp_path) == {target}


@pytest.mark.integration
@pytest.mark.parametrize("disable_tach", [True, False])
def test_native_pytest_selection(tmp_path: Path, disable_tach: bool) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_tools.py").write_text(
        'def test_hook():\n    path = "hooks/new_hook.py"\n    assert path\n', encoding="utf-8"
    )
    (tests / "test_domain.py").write_text("def test_domain():\n    assert False\n", encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(_ROOT)}
    command = [sys.executable, "-m", "pytest", "tests", "-p", "scripts.repo_tool_selection"]
    if disable_tach:
        command.extend(["-p", "no:tach"])
    result = subprocess.run(
        [*command, "--repo-tools-only", "-q", "-o", "addopts="],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=_SELECTION_TIMEOUT_SECONDS,
    )

    assert result.returncode == (0 if disable_tach else 4), result.stdout + result.stderr
    if disable_tach:
        assert "1 passed, 1 deselected" in result.stdout
    else:
        assert "--repo-tools-only requires -p no:tach" in result.stderr
